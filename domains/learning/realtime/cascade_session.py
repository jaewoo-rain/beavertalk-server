"""캐스케이드 통화 세션 — WS ↔ STT v2 브리지 + **턴 상태기계**.

⛔ normalcall(`call_session.py`)과 발음챌린지(`stt_session.py`)는 건드리지 않는다. 이 파일은
   같은 골격(2펌프 + TaskGroup)을 따르되 **출력 펌프에 턴 상태기계가 들어간다**.

펌프 3개(TaskGroup — 하나 끝나면 동반 취소):
  ① _pump_in    client → (PCM16/16k) → STT v2
  ② _pump_stt   STT v2 events()      → 내부 큐   (읽기만 — 상태기계와 분리)
  ③ _pump_turn  큐 → 상태기계 → client (turn_start / input_transcript / turn_end)

왜 ②와 ③을 나눴나: **턴 종료를 서버 타이머가 판정**하므로 "이벤트를 기다리되 타임아웃이
있는" 대기가 필요하다. async generator 를 wait_for 로 감싸 취소하면 제너레이터가 깨진다 —
그래서 읽기 전용 펌프가 큐에 넣고, 상태기계는 큐를 타임아웃과 함께 읽는다.

⛔ 턴 종료를 STT 설정으로 못 하는 이유(proto 원문 직접 확인, 2026-08-05):
  voice_activity_timeout = "the server will automatically close the stream after the
  specified duration has elapsed after the last VOICE_ACTIVITY speech event has been sent."
  → 그 필드에 800ms 를 넣으면 사용자가 0.8초 쉴 때마다 **스트림이 죽는다**. 그래서
  **턴 시작 = STT 의 SPEECH_ACTIVITY_BEGIN 이벤트 / 턴 종료 = 서버 자체 타이머**로 나눈다.

타이머를 **오디오 시각**으로 재는 이유: STT v2 는 서울·도쿄 리전이 없어 global/us 로 나간다.
이벤트 도착 시각으로 침묵을 재면 태평양 왕복 + 인식 지연이 임계에 그대로 얹혀 턴이 늦게
끊긴다. 이벤트가 들고 오는 offset(speech_event_offset / result_end_offset)과 우리가 보낸
오디오 총량을 견주면 **이미 흘러간 침묵**을 알 수 있고, 남은 시간만 기다리면 된다.
그 차이(audio_ms_sent − offset_ms)가 곧 파이프라인 지연이라 계측도 같이 나온다.

상태 (설계 §3 — docs/20260805_1720_캐스케이드-턴감지-최소루프-설계.md):
  IDLE → USER_SPEAKING → THINKING → BEAVER_SPEAKING → (barge-in) CANCELLING → USER_SPEAKING
P0 은 THINKING 이후가 없다(LLM·TTS 미연결). speech_end 즉시 최종 전사를 에코하고 IDLE 로
돌아간다. 상태 enum·전이 훅은 P1 이 그대로 채우도록 미리 둔다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Any, Protocol

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketState

from core import stt as stt_mod
from core.config import settings
from core.stt import (
    SPEECH_BEGIN,
    SPEECH_END,
    STREAM_ERROR,
    STREAM_ROLLOVER,
    TRANSCRIPT,
    SttV2Event,
)
from domains.learning.realtime.cascade_protocol import (
    BEAVER_FRAME_INTERVAL_MS,
    ClientPlaybackProgress,
    ClientTestBeaver,
    ServerAudioCancel,
    ServerCascadeReady,
    ServerTurnEnd,
    ServerTurnStart,
    ServerUserTurnEnd,
    ServerUserTurnStart,
    ServerInputPartial,
    ServerSttRollover,
    ServerTestCancelReport,
    cascade_server_adapter,
)
from domains.learning.realtime.protocol import ServerError, ServerPong

logger = logging.getLogger(__name__)

_DEFAULT_SAMPLE_RATE = 16000
_EOS = object()  # 큐 종료 센티널
_RMS_STRIDE = 8  # 에너지 계산 표본 간격(전 샘플을 돌 필요 없다 — 게이트용 근사면 충분)


def _frame_rms(pcm: bytes) -> float:
    """PCM16 프레임의 정규화 RMS(0~1). 에코 2차 방어의 '임계 상향'에 쓴다.

    잔여 에코는 대개 원음보다 작다 — 비버 발화 중에는 이 값이 임계를 넘어야 barge-in 으로
    친다. 표본을 stride 로 건너뛰어 계산 비용을 낮춘다(게이트 판정에 정밀도는 불필요).
    파이썬 3.13 에서 audioop 이 제거돼 순수 파이썬으로 잰다.
    """
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    total = 0.0
    count = 0
    for i in range(0, n, _RMS_STRIDE):
        sample = int.from_bytes(pcm[i * 2 : i * 2 + 2], "little", signed=True)
        total += sample * sample
        count += 1
    if not count:
        return 0.0
    return (total / count) ** 0.5 / 32768.0


@lru_cache(maxsize=4)
def _tone_frame(ms: int) -> bytes:
    """[dev 훅] 440Hz 톤 PCM24k 프레임. 100ms 는 정확히 44주기라 이어 붙여도 위상이 끊기지 않는다."""
    n = int(24000 * ms / 1000)
    amp = 8000  # 귀에 편한 수준(-12dBFS 부근)
    out = bytearray()
    for i in range(n):
        out += int(amp * math.sin(2 * math.pi * 440 * i / 24000)).to_bytes(2, "little", signed=True)
    return bytes(out)


@lru_cache(maxsize=4)
def _silence_frame(ms: int) -> bytes:
    """[dev 훅] 무음 PCM24k 프레임(샘플당 2바이트 = 전부 0)."""
    return bytes(2 * int(24000 * ms / 1000))


class TurnState(str, Enum):
    IDLE = "idle"
    USER_SPEAKING = "user_speaking"
    THINKING = "thinking"          # P1: LLM 응답 대기
    BEAVER_SPEAKING = "beaver_speaking"  # P1: TTS 재생 중(barge-in 대상)
    CANCELLING = "cancelling"      # P1: 취소 배관 진행 중


@dataclass
class CascadeInbound:
    """WS 인바운드 1건: 바이너리=오디오, 텍스트=JSON 제어, 끊김=disconnect."""

    kind: str  # 'audio' | 'control' | 'disconnect'
    audio: bytes | None = None
    control: dict | None = None


class CascadeTransport(Protocol):
    """WS 어댑터 인터페이스(테스트는 스텁으로 대체 — stt_session 의 SttTransport 와 같은 규율)."""

    async def send_event(self, event: dict) -> None: ...
    async def send_audio(self, frame: bytes) -> None: ...
    async def receive(self) -> CascadeInbound: ...


class WsCascadeTransport:
    """starlette WebSocket → CascadeTransport 어댑터(bytes/text 분기)."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send_event(self, event: dict) -> None:
        await self._ws.send_json(event)

    async def send_audio(self, frame: bytes) -> None:
        await self._ws.send_bytes(frame)

    async def receive(self) -> CascadeInbound:
        msg = await self._ws.receive()
        if msg.get("type") == "websocket.disconnect":
            return CascadeInbound(kind="disconnect")
        if msg.get("bytes") is not None:
            return CascadeInbound(kind="audio", audio=msg["bytes"])
        text = msg.get("text")
        if text is not None:
            try:
                ctrl = json.loads(text)
            except (ValueError, TypeError):
                ctrl = {}
            return CascadeInbound(kind="control", control=ctrl)
        return CascadeInbound(kind="control", control={})


class _Stop(Exception):
    """펌프를 정상 종료시키는 내부 신호(에러 아님)."""


class InvariantError(RuntimeError):
    """서버 출력 불변식 위반 — **클라가 정상 오디오를 조용히 버리게 되는** 버그다."""


# 서버→클라 오디오 = PCM16 / 24kHz mono = 48,000 bytes/s (고정 비트레이트 = 바이트↔ms 는 산수)
BEAVER_BYTES_PER_MS = 24000 * 2 / 1000.0


@dataclass(slots=True)
class SpokenChunk:
    """송출 원장 1건 — **송출 바이트 오프셋 → 실제 대사** 매핑.

    text="" 은 페이서가 끼운 **무음 패딩**이다. 무음도 바이트 오프셋에는 포함된다 —
    클라는 서버발 바이트를 구분할 수 없으니(패딩도 대사도 똑같이 도착한다) **대사/무음
    분리는 서버가 한다**. 이게 원장을 ms 가 아니라 바이트로 키잉하는 이유다.
    """

    start_byte: int
    end_byte: int
    text: str = ""

    @property
    def is_silence(self) -> bool:
        return not self.text


@dataclass
class _TurnRecord:
    """비버 턴 1건의 송출 기록 — 원장은 **턴마다 따로** 산다(늦게 오는 progress 대비)."""

    turn_id: str
    started_at: float
    sent_bytes: int = 0
    cancelled: bool = False
    epoch: int = 0
    ledger: list[SpokenChunk] = field(default_factory=list)


class BeaverOutput:
    """(P1) 비버 턴의 송출 게이트 + 재생 원장 + 페이서.

    ⛔ **불변식** — 클라 판별식("audio_cancel ~ 다음 turn_start 사이 바이너리 = 취소 잔여")의
    근거다. 하나라도 어기면 클라가 **정상 오디오를 조용히 버린다**:

      I1. 비버 턴 밖에서는 오디오를 일절 보내지 않는다
      I2. 모든 비버 턴은 `turn_start` 로 시작한다 — 오디오 첫 바이트보다 **먼저**
      I3. 누적 송출량 ≤ 실시간 레이트 + 선행버퍼(lead)
      I4. 취소된 턴에는 `turn_end` 를 보내지 않는다 — `audio_cancel` 이 종결을 겸한다
      I5. `turn_end` 는 **마지막 오디오 바이트를 보낸 뒤**에 낸다(LLM 텍스트 완료 시점 아님)

    I3 이 서버 책임인 이유: 실시간보다 빨리 밀어내면 클라 버퍼가 무한히 부푼다 →
    barge-in 취소가 늦게 먹히고(사용자가 끊었는데 계속 들린다) 이력 절단도 같이 틀어진다.
    클라의 백로그 계측은 **안전망이지 대책이 아니다.**
    """

    _HISTORY_MAX = 4  # 최근 N개 턴의 원장을 남긴다(늦게 오는 progress 대비, 메모리 상한)

    def __init__(
        self,
        transport: CascadeTransport,
        *,
        now: Any = time.monotonic,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._now = now
        self._sleep = sleep
        self._turn_seq = 0
        self._epoch = 0
        self._cur: _TurnRecord | None = None
        # turn_id → 원장. **턴별로 따로 보관하는 이유**: playback_progress 는 비동기라
        # 서버가 이미 다음 턴을 시작한 뒤에 도착할 수 있다. 하나의 원장만 들고 있으면
        # 늦게 온 이전 턴 진행도가 **새 턴의 원장에 적용돼** 엉뚱한 대사를 자른다.
        self._records: dict[str, _TurnRecord] = {}
        self._order: list[str] = []

    # ── 상태 ──
    @property
    def turn_id(self) -> str | None:
        return self._cur.turn_id if self._cur else None

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def sent_bytes(self) -> int:
        return self._cur.sent_bytes if self._cur else 0

    def ledger(self, turn_id: str | None = None) -> list[SpokenChunk]:
        record = self._record(turn_id)
        return list(record.ledger) if record else []

    def sent_bytes_of(self, turn_id: str) -> int:
        """그 턴에서 **보낸** 총 바이트(진행도와 견줘 '안 들린 양'을 낸다)."""
        record = self._records.get(turn_id)
        return record.sent_bytes if record else 0

    def _record(self, turn_id: str | None) -> "_TurnRecord | None":
        if turn_id is None:
            return self._cur
        return self._records.get(turn_id)

    # ── 턴 수명 ──
    async def begin(self) -> str:
        """비버 턴 시작 — **오디오보다 먼저** turn_start 를 낸다(I2)."""
        if self._cur is not None:
            raise InvariantError("이미 열린 비버 턴이 있다(중첩 금지)")
        self._turn_seq += 1
        turn_id = f"b{self._turn_seq}"
        self._cur = _TurnRecord(turn_id=turn_id, started_at=self._now())
        self._records[turn_id] = self._cur
        self._order.append(turn_id)
        while len(self._order) > self._HISTORY_MAX:
            self._records.pop(self._order.pop(0), None)
        await self._transport.send_event(
            json.loads(cascade_server_adapter.dump_json(ServerTurnStart(turn_id=turn_id)).decode())
        )
        return turn_id

    async def send(self, pcm: bytes, text: str = "") -> None:
        """오디오 청크 1개 송출 + 원장 기록 + 페이싱(I1·I3).

        text 는 이 청크가 실제로 발음하는 대사(무음 패딩이면 빈 문자열).
        """
        if self._cur is None:
            raise InvariantError("비버 턴 밖에서 오디오를 보내려 했다(I1 위반)")
        if not pcm:
            return
        await self._pace()
        start = self._cur.sent_bytes
        self._cur.sent_bytes += len(pcm)
        self._cur.ledger.append(
            SpokenChunk(start_byte=start, end_byte=self._cur.sent_bytes, text=text)
        )
        await self._transport.send_audio(pcm)

    async def end(self) -> None:
        """턴 종료 — **마지막 바이트를 보낸 뒤**에 turn_end(I5). 취소된 턴이면 내지 않는다(I4)."""
        if self._cur is None:
            return
        if not self._cur.cancelled:
            await self._transport.send_event(
                json.loads(
                    cascade_server_adapter.dump_json(
                        ServerTurnEnd(turn_id=self._cur.turn_id)
                    ).decode()
                )
            )
        self._cur = None

    async def cancel(self, reason: str = "barge_in") -> None:
        """barge-in 취소 — 송출 중단 + audio_cancel. **turn_end 는 보내지 않는다**(I4).

        `audio_cancel` 은 **turn_id 를 반드시 싣는다**(필수 필드). 클라가 되보내는
        playback_progress 를 서버가 그 turn_id 로 대조해 **그 턴의 원장에만** 적용하기
        위해서다 — 진행도가 도착할 즈음 서버는 이미 다음 턴을 시작했을 수 있다.
        turn_start/turn_end/output_transcript 가 이미 turn_id 를 싣는 것과 같은 규약이다.

        서버 안의 ①TTS 합성 cancel ②송신 큐 drain 은 호출부가 함께 친다(설계 §4-3).
        """
        if self._cur is None:
            return
        self._cur.cancelled = True
        self._epoch += 1
        self._cur.epoch = self._epoch
        await self._transport.send_event(
            json.loads(
                cascade_server_adapter.dump_json(
                    ServerAudioCancel(
                        turn_id=self._cur.turn_id, epoch=self._epoch, reason=reason
                    )
                ).decode()
            )
        )
        self._cur = None

    # ── 페이싱(I3) ──
    async def _pace(self) -> None:
        """실시간보다 앞서 나가면 그만큼 기다린다.

        허용 선행 = CASCADE_TTS_LEAD_MS. 이걸 넘겨 밀어내면 클라 버퍼가 부푼다.
        """
        if self._cur is None:
            return
        lead_ms = max(0, settings.CASCADE_TTS_LEAD_MS)
        elapsed_ms = (self._now() - self._cur.started_at) * 1000.0
        sent_ms = self._cur.sent_bytes / BEAVER_BYTES_PER_MS
        ahead_ms = sent_ms - elapsed_ms - lead_ms
        if ahead_ms > 0:
            await self._sleep(ahead_ms / 1000.0)

    # ── 이력 정합성(§5) ──
    def spoken_text(
        self, turn_id: str, played_server_bytes: int, sampled_at: str = "stop"
    ) -> str | None:
        """**그 턴에서** 실제로 들린 데까지의 대사 — LLM 이력에 남길 값.

        turn_id 를 반드시 받는다. 모르는(또는 이미 밀려난) 턴이면 **None** 을 돌려주고
        호출부는 무시한다 — 늦게 도착한 이전 턴 진행도가 새 턴을 오염시키면 안 된다.

        played_server_bytes = **서버가 보낸 오디오 중 실제로 스피커로 나간 바이트 수**.
        클라 자체 생성 무음 필러는 세지 않는다(클라는 서버발 패딩과 대사를 구분 못 하므로,
        구분은 이 원장이 한다).

        sampled_at="cancel" 이면 클라가 취소를 받은 순간의 값이라 실제 정지까지의 지연
        (CASCADE_CANCEL_STOP_MS, 50~120ms)만큼 더 들렸다 — 그만큼 보정한다.

        **걸친 청크는 버린다(짧은 쪽 편향).** 못 들은 말을 들었다고 치는 쪽이 그 반대보다
        훨씬 나쁘다 — 사용자가 모르는 정보를 전제로 대화가 진행되면 어긋난다.
        """
        record = self._records.get(turn_id)
        if record is None:
            return None
        played = max(0, int(played_server_bytes))
        if sampled_at == "cancel":
            played += int(settings.CASCADE_CANCEL_STOP_MS * BEAVER_BYTES_PER_MS)
        parts = [c.text for c in record.ledger if c.end_byte <= played and not c.is_silence]
        return " ".join(p.strip() for p in parts if p.strip())

    def estimated_played_bytes(self, turn_id: str | None = None) -> int:
        """progress 를 못 받았을 때의 서버 추정(§5-3) — 보수적으로 **짧은 쪽**."""
        record = self._record(turn_id)
        if record is None:
            return 0
        buffer_bytes = int(settings.CASCADE_CLIENT_BUFFER_MS * BEAVER_BYTES_PER_MS)
        return max(0, record.sent_bytes - buffer_bytes)


class CascadeSession:
    """WS 1개 = 캐스케이드 세션 1건. P0 은 턴 감지만 한다."""

    def __init__(self, transport: CascadeTransport) -> None:
        self.transport = transport
        self.state = TurnState.IDLE
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._t0 = time.monotonic()
        self._sample_rate = _DEFAULT_SAMPLE_RATE
        self._audio_ms = 0.0        # 클라에서 받아 STT 로 흘린 오디오 총량(오디오 타임라인)
        # 턴 누적
        self._turn_seq = 0
        self._turn_id: str | None = None
        self._turn_began_at = 0.0
        self._finals: list[str] = []
        self._partial = ""
        # 턴 종료 타이머
        self._silence_ms = max(0, settings.CASCADE_TURN_SILENCE_MS)
        self._close_at: float | None = None   # 침묵 데드라인(monotonic). None 이면 카운트다운 없음
        self._turn_deadline: float | None = None  # 턴 상한 데드라인(안전망)
        self._speech_active = False           # STT 가 "발화 중"이라고 보는가(BEGIN..END)
        self._last_voice_offset_ms = -1       # 마지막 음성 활동의 오디오 시각
        self._last_voice_at = 0.0             # 동 — 도착 시각(오프셋 미상일 때 폴백)
        self._pipeline_lag_ms = 0             # audio_ms_sent − offset (리전 왕복 + 인식 지연)
        # barge-in 에코 2차 방어(세션 단위 — 기기/라우트마다 달라야 한다)
        self._bargein_confirm = settings.CASCADE_BARGEIN_CONFIRM
        self._aec_mode = "unknown"
        self._recent_rms = 0.0
        # 비버 출력(P1: TTS 송출·원장·페이서). P0 에서는 오디오를 내지 않지만, 클라가
        # 되보내는 playback_progress 를 **턴별 원장에 대조**하려면 지금부터 있어야 한다.
        self.beaver = BeaverOutput(transport)
        self._spoken_by_turn: dict[str, str] = {}   # turn_id → 실제로 들린 대사(이력용)
        # [dev 훅] 취소 배관 실측용
        self._tg: asyncio.TaskGroup | None = None
        self._fake_beaver_task: asyncio.Task | None = None
        self._fake_beaver_cancelled = False
        self._cancel_sent_at = 0.0
        self._cancel_turn_id: str | None = None

    # ── 수명주기 ──
    async def run(self) -> None:
        try:
            first = await self.transport.receive()
        except Exception:  # noqa: BLE001 - 연결이 바로 끊긴 경우
            return
        if first.kind == "disconnect":
            return

        sample_rate = _DEFAULT_SAMPLE_RATE
        pending_audio: bytes | None = None
        if first.kind == "control" and (first.control or {}).get("type") == "start":
            ctrl = first.control or {}
            raw = ctrl.get("sampleRate") or ctrl.get("sample_rate") or _DEFAULT_SAMPLE_RATE
            try:
                sample_rate = int(raw)
            except (TypeError, ValueError):
                sample_rate = _DEFAULT_SAMPLE_RATE
            self._apply_aec_hint(ctrl.get("aec"))
        elif first.kind == "audio":
            pending_audio = first.audio
        self._sample_rate = sample_rate

        stream = stt_mod.make_stt_v2_stream(sample_rate)
        try:
            await stream.start()
        except Exception as exc:  # noqa: BLE001 - 개시 실패는 이 세션만 실패(R5)
            logger.exception("캐스케이드 STT v2 개시 실패")
            await self._safe(ServerError(code="stt_start_failed", message=str(exc), recoverable=False))
            return

        self._t0 = time.monotonic()
        await self._safe(
            ServerCascadeReady(
                engine=stt_mod.stt_v2_engine_name(),
                turn_silence_ms=self._silence_ms,
                sample_rate=sample_rate,
                language=settings.STT_V2_LANGUAGE or settings.STT_LANGUAGE,
                bargein_confirm=self._bargein_confirm,
                bargein_min_ms=settings.CASCADE_BARGEIN_MIN_MS,
                mic_always_open=settings.CASCADE_MIC_ALWAYS_OPEN,
            )
        )

        try:
            async with asyncio.TaskGroup() as tg:
                self._tg = tg  # [dev 훅] 가짜 비버 태스크를 같은 그룹에 붙이기 위해
                tg.create_task(self._pump_in(stream, pending_audio))
                tg.create_task(self._pump_stt(stream))
                tg.create_task(self._pump_turn())
        except* _Stop:
            pass  # 정상 종료(클라 stop / 스트림 끝 / disconnect)
        except* Exception as eg:  # noqa: BLE001
            self._log_pump_errors(eg)
            await self._safe(ServerError(code="cascade_error", message="cascade_stream_error"))
        finally:
            self._tg = None
            try:
                await stream.close()
            except Exception:  # noqa: BLE001
                pass

    # ── ① client → STT ──
    async def _pump_in(self, stream: Any, pending_audio: bytes | None) -> None:
        if pending_audio:
            await stream.push_audio(pending_audio)
        while True:
            inb = await self.transport.receive()
            if inb.kind == "disconnect":
                raise _Stop
            if inb.kind == "control":
                ctrl = inb.control or {}
                ctype = ctrl.get("type")
                if ctype == "stop":
                    raise _Stop
                if ctype == "ping":
                    await self._safe(ServerPong(t=ctrl.get("t")))
                elif ctype == "playback_progress":
                    await self._on_playback_progress(ctrl)
                elif ctype == "__test_beaver":   # dev 훅(가짜 비버 오디오)
                    await self._start_fake_beaver(ctrl)
                elif ctype == "__test_cancel":   # dev 훅(취소 배관)
                    await self._cancel_fake_beaver(ctrl)
                elif ctype == "__test_say":  # dev 훅(페이크 STT 구동)
                    stream.feed_test(str(ctrl.get("text") or ""))
                elif ctype == "__test_event":
                    stream.feed_test_event(str(ctrl.get("event") or ""))
                continue
            if inb.kind == "audio" and inb.audio:
                # 오디오 타임라인을 서버가 직접 센다 — 턴 타이머와 지연 계측의 기준자다.
                self._audio_ms += len(inb.audio) / (self._sample_rate * 2) * 1000.0
                if settings.CASCADE_BARGEIN_RMS > 0:
                    self._recent_rms = _frame_rms(inb.audio)
                await stream.push_audio(inb.audio)

    # ── ② STT → 큐 (읽기 전용) ──
    async def _pump_stt(self, stream: Any) -> None:
        try:
            async for event in stream.events():
                await self._q.put(event)
        finally:
            await self._q.put(_EOS)

    # ── ③ 큐 → 턴 상태기계 → client ──
    async def _pump_turn(self) -> None:
        while True:
            now = time.monotonic()
            # 두 개의 데드라인 중 이른 것: ① 침묵 타이머 ② 턴 상한(안전망).
            # ②가 필요한 이유: 엔진이 SPEECH_ACTIVITY_END 를 영영 안 주면(스트림이 발화 중
            # 닫히면 END 는 발송되지 않는다) 턴이 열린 채 굳는다.
            deadlines = [d for d in (self._close_at, self._turn_deadline) if d is not None]
            timeout = max(0.0, min(deadlines) - now) if deadlines else None
            try:
                if timeout is None:
                    item = await self._q.get()
                else:
                    item = await asyncio.wait_for(self._q.get(), timeout)
            except asyncio.TimeoutError:
                # 침묵 타이머 만료 = 턴 종료. **이 판정이 캐스케이드의 심장이다.**
                expired_silence = (
                    self._close_at is not None and time.monotonic() >= self._close_at
                )
                await self._close_turn("silence" if expired_silence else "max")
                continue
            if item is _EOS:
                raise _Stop
            await self._handle(item)

    async def _handle(self, event: SttV2Event) -> None:
        if event.kind == SPEECH_BEGIN:
            await self._on_speech_begin(event)
        elif event.kind == TRANSCRIPT:
            await self._on_transcript(event)
        elif event.kind == SPEECH_END:
            await self._on_speech_end(event)
        elif event.kind == STREAM_ROLLOVER:
            await self._safe(ServerSttRollover(reason=event.detail, gap_ms=event.gap_ms))
        elif event.kind == STREAM_ERROR:
            await self._safe(
                ServerError(code="stt_stream_error", message=event.detail, recoverable=False)
            )
            raise _Stop

    # ── 전이 ──
    async def _on_speech_begin(self, event: SttV2Event) -> None:
        self._speech_active = True
        self._mark_voice(event)
        if self.state in (TurnState.BEAVER_SPEAKING, TurnState.THINKING):
            # 여기가 barge-in 트리거다. 다만 **에코 2차 방어**를 통과해야 진짜로 친다.
            if not await self._bargein_allowed(event):
                return
            await self._on_barge_in(event)
        if self.state == TurnState.USER_SPEAKING:
            self._close_at = None  # 발화 재개 — 카운트다운 취소(같은 턴 계속)
            return
        await self._open_turn(event.at)

    async def _on_transcript(self, event: SttV2Event) -> None:
        if self.state == TurnState.IDLE:
            # 방어: VAD BEGIN 없이 전사가 먼저 오는 엔진/설정도 있다. 전사를 턴 시작으로 본다.
            await self._open_turn(event.at)
        if event.is_final:
            if event.text:
                self._finals.append(event.text)
            self._partial = ""
        else:
            self._partial = event.text
        self._mark_voice(event)
        await self._safe(
            ServerInputPartial(text=event.text, final=event.is_final, turn_id=self._turn_id)
        )
        # 발화 중(BEGIN..END)이면 카운트다운을 걸지 않는다 — 엔진이 "말하는 중"이라고 본다.
        # 반대로 VAD 이벤트를 아예 안 주는 엔진/설정에서는 전사 오프셋이 타이머를 몬다.
        if not self._speech_active:
            self._arm_close_timer(event)

    async def _on_speech_end(self, event: SttV2Event) -> None:
        self._speech_active = False
        self._mark_voice(event)
        if self.state != TurnState.USER_SPEAKING or self._turn_id is None:
            return  # 열린 턴이 없으면 무시(스트림 시작 직후의 잔여 이벤트 등)
        self._arm_close_timer(event)

    def _mark_voice(self, event: SttV2Event) -> None:
        """마지막 음성 활동 지점을 오디오 시각으로 기록 + 파이프라인 지연 계측."""
        self._last_voice_at = event.at
        if event.offset_ms >= 0:
            self._last_voice_offset_ms = event.offset_ms
            self._pipeline_lag_ms = int(max(0.0, self._audio_ms - event.offset_ms))

    def _arm_close_timer(self, event: SttV2Event) -> None:
        """침묵 카운트다운을 건다 — **이미 흘러간 침묵은 빼고** 남은 만큼만.

        이벤트가 오디오 시각(offset)을 들고 오면 `audio_ms_sent − offset` 이 이미 지난
        침묵이다. 그만큼을 빼야 리전 왕복·인식 지연이 임계에 얹히지 않는다(오프셋이 없으면
        0으로 보고 그냥 전체를 기다린다 — 페이크·구버전 대비 폴백).
        """
        already_ms = 0.0
        if event.offset_ms >= 0:
            already_ms = max(0.0, self._audio_ms - event.offset_ms)
        remain_s = max(0.0, (self._silence_ms - already_ms) / 1000.0)
        self._close_at = time.monotonic() + remain_s

    async def _open_turn(self, at: float) -> None:
        self._turn_seq += 1
        self._turn_id = f"u{self._turn_seq}"
        self._turn_began_at = at
        self._finals = []
        self._partial = ""
        self._close_at = None
        self._turn_deadline = at + max(5, settings.CASCADE_TURN_MAX_S)
        self.state = TurnState.USER_SPEAKING
        await self._safe(ServerUserTurnStart(turn_id=self._turn_id, at_ms=self._ms(at)))

    async def _close_turn(self, reason: str = "silence") -> None:
        if self._turn_id is None:
            self._close_at = None
            return
        now = time.monotonic()
        # 실제 발화가 끝난 시각 = 마지막 음성 활동 시각(침묵 임계 이전).
        end_at = self._last_voice_at or now
        text = " ".join(t.strip() for t in self._finals if t.strip())
        if not text and self._partial.strip():
            text = self._partial.strip()  # 최종이 안 왔으면 마지막 부분 전사로 대체
        speech_ms = int(max(0.0, end_at - self._turn_began_at) * 1000)
        await self._safe(
            ServerUserTurnEnd(
                turn_id=self._turn_id,
                text=text,
                at_ms=self._ms(end_at),
                speech_ms=speech_ms,
                silence_ms=self._silence_ms,
                pipeline_lag_ms=self._pipeline_lag_ms,
                end_lag_ms=int(max(0.0, now - end_at) * 1000),
                reason=reason,
            )
        )
        logger.info(
            "cascade turn: id=%s reason=%s speech_ms=%d silence_ms=%d pipeline_lag_ms=%d text=%r",
            self._turn_id, reason, speech_ms, self._silence_ms, self._pipeline_lag_ms, text,
        )
        self._turn_id = None
        self._close_at = None
        self._turn_deadline = None
        self._finals = []
        self._partial = ""
        # P1: 여기서 THINKING 으로 넘어가 LLM 을 부른다. P0 은 곧장 IDLE.
        self.state = TurnState.IDLE

    # ── barge-in ──
    async def _bargein_allowed(self, event: SttV2Event) -> bool:
        """에코 2차 방어 — AEC 가 **부분적**이라는 클라 조사 결론에 따른 필수 관문.

        플랫폼 AEC 를 제대로 켜도 잔여 에코가 남는다(스피커폰 최악, 이어폰 무해). 지금
        Android 재생은 USAGE_MEDIA 라 AEC 기준 경로 밖이어서 사실상 AEC 가 안 걸린다.
        그 상태로 speech_begin 하나에 비버를 끊으면 **비버가 자기 목소리에 끊긴다.**

        관문 2개(설계 §3):
          ① 최소 지속 — 순간 튐으로 발동 금지(CASCADE_BARGEIN_MIN_MS, 기본 150ms)
          ② confirm=transcript — 비어있지 않은 전사가 최소 글자수 이상 나와야 인정
        ①은 지금 구현하고, ②는 P1(TTS 연결)에서 재생 구간과 함께 붙인다. 세션 단위 값이라
        `start.aec` 힌트로 기기·라우트마다 다르게 잡는다(이어폰이면 immediate).
        """
        # ⓪ 마이크 상시 개방이 꺼져 있으면 barge-in 을 시도하지 않는다.
        #   그 모드에서는 클라가 비버 발화 중 마이크를 닫으므로, 이때 들어오는 음성 활동은
        #   **에코이거나 게이팅 타이밍 결함**일 가능성이 높다(실측: call 855 에서 유저 턴의
        #   절반이 비버 대사였다). 서버는 두 모드를 모두 견뎌야 하고, OFF 에서는 견디는 방법이
        #   "끼어들지 않는 것"이다.
        if not settings.CASCADE_MIC_ALWAYS_OPEN:
            logger.info("cascade barge-in 기각 — 마이크 상시개방 OFF(에코/게이팅 잔여 추정)")
            return False
        # ① 에너지 임계 — 잔여 에코는 대개 original 보다 작다(0 이면 비활성).
        threshold = settings.CASCADE_BARGEIN_RMS
        if threshold > 0 and self._recent_rms < threshold:
            logger.info(
                "cascade barge-in 기각 — 에너지 %.4f < 임계 %.4f(에코 추정)",
                self._recent_rms, threshold,
            )
            return False
        # ② 최소 지속 — 순간 튐으로 비버를 끊지 않는다.
        min_ms = max(0, settings.CASCADE_BARGEIN_MIN_MS)
        if min_ms <= 0:
            return True
        # 지속 판정은 오디오 시각으로 한다 — 도착 시각으로 재면 리전 왕복이 섞인다.
        if event.offset_ms >= 0 and (self._audio_ms - event.offset_ms) >= min_ms:
            return True
        await asyncio.sleep(min_ms / 1000.0)
        if not self._speech_active:
            logger.info("cascade barge-in 기각 — 최소 지속 %dms 미달(에코/잡음 추정)", min_ms)
            return False
        return True

    async def _on_barge_in(self, event: SttV2Event) -> None:
        """(P1) barge-in 취소 배관. P0 은 TTS 가 없어 호출될 일이 없다.

        구현 시 **세 곳을 동시에** 친다(설계 §4):
          ① TTS 합성 태스크 cancel  ② 서버 송신 큐 drain + epoch += 1
          ③ audio_cancel 전송 → 클라가 버퍼 폐기 + played_server_bytes 회신(이력 절단 근거)
        ③ 이후 클라가 실제로 조용해지기까지 50~120ms 더 들린다 — 이력 절단은 그 지연을
        포함한 값(정지 후 샘플된 네이티브 카운터)으로 해야 한다(설계 §5-3).
        """
        self.state = TurnState.CANCELLING
        logger.info("cascade barge-in 감지(P0: TTS 미연결 — 취소 배관 미실행)")

    # ── [dev 훅] 가짜 비버 오디오 — 클라 취소 배관을 P1 없이 실기기에서 검증한다 ──
    async def _start_fake_beaver(self, ctrl: dict) -> None:
        """톤/무음 PCM24k 를 **실시간 레이트로** 흘린다. 진짜 TTS·LLM 은 붙지 않는다.

        왜 필요한가: 지금 서버는 오디오를 낼 일이 없어 `audio_cancel` 을 보낼 수가 없고,
        그래서 클라가 만들어 둔 네이티브 clear() 를 한 줄도 못 돌린다. 이 훅이 그 통로다.

        ⛔ 훅이라도 **불변식은 그대로 지킨다** — 송출은 전부 BeaverOutput 을 통과하므로
        turn_start 가 오디오보다 먼저 나가고(I2), 페이싱이 실시간을 앞지르지 않으며(I3),
        취소 시 turn_end 를 내지 않는다(I4). 훅이 불변식을 어기면 클라 판별식이 깨진다.
        """
        if self._fake_beaver_task is not None and not self._fake_beaver_task.done():
            logger.info("cascade [dev] 가짜 비버가 이미 흐르는 중 — 무시")
            return
        try:
            request = ClientTestBeaver.model_validate(ctrl)
        except ValidationError as exc:
            logger.warning("cascade [dev] __test_beaver 형식 오류(무시) — %s", exc)
            return
        if self._tg is None:
            return
        self._fake_beaver_task = self._tg.create_task(self._run_fake_beaver(request))

    async def _run_fake_beaver(self, request: ClientTestBeaver) -> None:
        frame_ms = BEAVER_FRAME_INTERVAL_MS
        frame = _tone_frame(frame_ms) if request.tone else _silence_frame(frame_ms)
        total_frames = max(1, int(request.seconds * 1000 / frame_ms))
        per_sentence = max(1, int(request.sentence_ms / frame_ms))
        self.state = TurnState.BEAVER_SPEAKING
        self._fake_beaver_cancelled = False
        turn_id = await self.beaver.begin()
        logger.info(
            "cascade [dev] 가짜 비버 시작: turn=%s %.1fs %s",
            turn_id, request.seconds, "톤" if request.tone else "무음",
        )
        try:
            for i in range(total_frames):
                # "문장"은 **마지막 프레임에만** 이름표를 단다. 원장 절단이 "그 문장을 끝까지
                # 들었을 때만 이력에 남긴다"이므로, 종료 지점에 텍스트를 두는 게 실제 TTS
                # 청크 스트림과 같은 의미가 된다(걸친 문장은 버려진다).
                sentence_no = i // per_sentence + 1
                is_sentence_end = (i + 1) % per_sentence == 0
                await self.beaver.send(frame, f"문장{sentence_no}" if is_sentence_end else "")
            await self.beaver.end()
            logger.info("cascade [dev] 가짜 비버 정상 종료: turn=%s", turn_id)
        except asyncio.CancelledError:
            # ⚠ 우리가 건 취소는 **여기서 흡수하고 정상 종료**한다. 이 태스크는 세션의
            # TaskGroup 자식이라, 밖에서 취소된 채로 CancelledError 를 다시 올리면
            # TaskGroup 이 그걸 그룹 취소로 보고 **세션 전체를 무너뜨린다**(실측 확인).
            # 세션 종료로 인한 취소(플래그 미설정)는 그대로 올려보내야 정리가 된다.
            if not self._fake_beaver_cancelled:
                raise
            logger.info("cascade [dev] 가짜 비버 송출 취소됨 turn=%s", turn_id)
        except InvariantError:
            # 취소가 먼저 들어와 턴이 닫힌 뒤의 잔여 송출 — 정상 경로다(설계 §5 실행 상세).
            logger.info("cascade [dev] 가짜 비버 송출 중단(턴이 이미 닫힘) turn=%s", turn_id)
        finally:
            if self.state == TurnState.BEAVER_SPEAKING:
                self.state = TurnState.IDLE

    async def _cancel_fake_beaver(self, ctrl: dict) -> None:
        """barge-in 과 **같은 취소 배관**을 탄다: 송출 태스크 cancel → audio_cancel.

        ⭐ **STT 음성활동 감지를 타지 않는다.** 버튼 → 서버가 직접 audio_cancel 을 쏜다.
        그래서 `CASCADE_MIC_ALWAYS_OPEN` 이 꺼져 있어도(=AEC 정비 전이라 켤 수 없어도)
        **취소 경로만 독립적으로** 잴 수 있다. 우리가 재려는 건 취소 배관의 지연이지
        음성 barge-in 판정이 아니다. `_bargein_allowed()`(플래그 게이트)는 speech_begin
        경로에만 걸려 있고 이 훅은 그 위를 지나가지 않는다.
        """
        turn_id = self.beaver.turn_id
        if turn_id is None:
            logger.info("cascade [dev] 취소할 비버 턴이 없다")
            return
        task, self._fake_beaver_task = self._fake_beaver_task, None
        if task is not None and not task.done():
            self._fake_beaver_cancelled = True
            task.cancel()  # await 하지 않는다 — 기다리면 그만큼 더 들린다(설계 §5)
        self._cancel_sent_at = time.monotonic()
        self._cancel_turn_id = turn_id
        await self.beaver.cancel(str(ctrl.get("reason") or "barge_in"))
        self.state = TurnState.IDLE
        logger.info("cascade [dev] audio_cancel 발신: turn=%s", turn_id)

    async def _on_playback_progress(self, ctrl: dict) -> None:
        """클라의 재생 진행도 → **그 턴의 원장에만** 적용해 실제로 들린 대사를 확정한다.

        ⚠ 이 메시지는 비동기라 **서버가 이미 다음 턴을 시작한 뒤** 도착할 수 있다. 그래서
        메시지의 turn_id 로 대조하고, 모르는(또는 밀려난) 턴이면 **버린다** — 늦게 온 이전
        턴 진행도가 새 턴 원장에 적용되면 엉뚱한 대사가 잘린다.

        source="estimate"(Dart/JS 외삽, ±50~150ms)도 버린다 — 오차가 절단 단위와 같은
        자릿수라 '짧은 쪽 편향' 원칙이 무의미해진다(CASCADE_TRUST_ESTIMATED_PROGRESS 로
        강제 허용은 가능).
        """
        try:
            progress = ClientPlaybackProgress.model_validate(ctrl)
        except ValidationError as exc:
            logger.warning("cascade playback_progress 형식 오류(무시) — %s", exc)
            return
        # [dev 훅] 취소 배관 실측: audio_cancel 을 쓴 시각 → 이 메시지가 도착한 시각.
        # 클라가 말한 '폐기 실효지연 50~120ms'의 실측치다(지금까지는 추정이었다).
        rtt_ms = 0
        measured = bool(self._cancel_sent_at) and self._cancel_turn_id == progress.turn_id
        if measured:
            rtt_ms = int((time.monotonic() - self._cancel_sent_at) * 1000)
        # 분해: 클라가 자기 소요를 실어 보내면 네트워크 왕복을 갈라낼 수 있다.
        # 못 갈라내면 rtt_ms 는 '왕복 포함'으로만 읽어야 한다(화면에 그렇게 표기한다).
        client_stop_ms = progress.client_stop_ms
        network_ms = (
            max(0, rtt_ms - client_stop_ms) if (measured and client_stop_ms >= 0) else -1
        )
        # ⭐ 그 값이 실제 무음 시각인지 **하한**인지. 명시가 없으면 하한으로 본다 —
        #   낙관 편향된 값으로 '50~120ms 합격'을 내면 실기기에서 뒤집힌다.
        lower_bound = progress.stop_measure != "hal_drained"

        async def report(accepted: bool, note: str, spoken: str = "") -> None:
            sent = self.beaver.sent_bytes_of(progress.turn_id)
            unplayed = max(0, sent - progress.played_server_bytes)
            await self._safe(
                ServerTestCancelReport(
                    turn_id=progress.turn_id,
                    rtt_ms=rtt_ms,
                    client_stop_ms=client_stop_ms,
                    client_stop_is_lower_bound=lower_bound,
                    stop_measure=progress.stop_measure,
                    platform=progress.platform,
                    audio_route=progress.audio_route,
                    network_ms=network_ms,
                    sent_bytes=sent,
                    played_server_bytes=progress.played_server_bytes,
                    unplayed_ms=int(unplayed / BEAVER_BYTES_PER_MS),
                    spoken_text=spoken,
                    source=progress.source,
                    sampled_at=progress.sampled_at,
                    accepted=accepted,
                    note=note,
                )
            )

        if progress.source != "native" and not settings.CASCADE_TRUST_ESTIMATED_PROGRESS:
            logger.info(
                "cascade playback_progress 무시 — source=%s(추정치는 절단 근거로 못 쓴다) turn=%s",
                progress.source, progress.turn_id,
            )
            await report(False, "source=estimate — 추정치는 절단 근거로 쓰지 않는다")
            return
        spoken = self.beaver.spoken_text(
            progress.turn_id, progress.played_server_bytes, progress.sampled_at
        )
        if spoken is None:
            logger.info(
                "cascade playback_progress 무시 — 미상/만료된 turn_id=%s(현재 턴=%s)",
                progress.turn_id, self.beaver.turn_id,
            )
            await report(False, "미상/만료된 turn_id — 다른 턴 원장에 적용하지 않는다")
            return
        self._spoken_by_turn[progress.turn_id] = spoken
        logger.info(
            "cascade 재생 진행도: turn=%s played=%dB rtt=%dms(왕복포함) client_stop=%s%dms"
            "(%s) network=%dms sampled_at=%s → 이력 반영 %r",
            progress.turn_id, progress.played_server_bytes, rtt_ms,
            "≥" if lower_bound else "", client_stop_ms, progress.stop_measure,
            network_ms, progress.sampled_at, spoken,
        )
        await report(True, "", spoken)

    def _apply_aec_hint(self, aec: Any) -> None:
        """start.aec 힌트로 **세션별** barge-in 정책을 정한다.

        AEC 는 기기·라우트마다 다르다(이어폰=사실상 무해 / 스피커폰=최악). 전역 설정 하나로는
        이 차이를 표현할 수 없어서 세션 값으로 둔다. 힌트가 없으면 전역 기본값.
        """
        if not isinstance(aec, dict):
            return
        mode = str(aec.get("mode") or "unknown")
        self._aec_mode = mode
        if mode == "headset":
            self._bargein_confirm = "immediate"   # 음향 결합이 없다 — 빠르게 반응
        elif mode in ("none", "unknown"):
            self._bargein_confirm = "transcript"  # AEC 없음 — 전사 확인까지 요구
        logger.info("cascade aec 힌트: mode=%s → bargein_confirm=%s", mode, self._bargein_confirm)

    # ── 유틸 ──
    def _ms(self, at: float) -> int:
        return int(max(0.0, at - self._t0) * 1000)

    async def _safe(self, message: Any) -> None:
        try:
            await self.transport.send_event(
                json.loads(cascade_server_adapter.dump_json(message).decode("utf-8"))
            )
        except Exception:  # noqa: BLE001 - 이미 닫히는 중일 수 있음
            pass

    def _log_pump_errors(self, eg: BaseExceptionGroup) -> None:
        from starlette.websockets import WebSocketDisconnect

        real = eg.subgroup(
            lambda e: not isinstance(
                e, (WebSocketDisconnect, ConnectionError, asyncio.CancelledError)
            )
        )
        if real is not None:
            logger.error("캐스케이드 pump 오류: %r", real.exceptions)


async def run_cascade(websocket: WebSocket) -> None:
    """WS 캐스케이드 세션 구동(라우터에서 accept 후 위임). 소켓 정리까지 책임."""
    session = CascadeSession(WsCascadeTransport(websocket))
    try:
        await session.run()
    finally:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass
