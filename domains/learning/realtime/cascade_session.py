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
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from typing import Any, Protocol

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketState

from core import gemini_chat
from core import stt as stt_mod
from core import tts
from core.config import settings
from core.persona_prompt import build_system_instruction, seed_opening
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
from domains.learning.realtime.cascade_reply import (
    MARKER,
    SentenceBuffer,
    speak_stream,
    split_by_language,
    strip_markers,
)
from domains.learning.realtime.cascade_usage import CascadeUsage, log_usage_summary
from domains.learning.realtime.protocol import ServerError, ServerPong

logger = logging.getLogger(__name__)

_DEFAULT_SAMPLE_RATE = 16000
_EOS = object()  # 큐 종료 센티널
_RMS_STRIDE = 8  # 에너지 계산 표본 간격(전 샘플을 돌 필요 없다 — 게이트용 근사면 충분)
# 유령 턴 판정의 여유 — 같은 발화의 꼬리 전사는 앞 턴의 끝과 사실상 같은 지점을 가리킨다.
# 200ms 는 "새 발화라면 최소 이만큼은 뒤여야 한다"는 하한(사람이 그보다 빨리 새 말을 시작하면
# 어차피 같은 턴으로 이어진다).
_STALE_OFFSET_EPSILON_MS = 200
# 이 값을 넘는 파이프라인 지연은 계측이 아니라 **버그 신호**다(리전 왕복은 1초 수준).
# 넘는 오프셋은 계측에 쓰지 않고 미상으로 거절한다(_sanitize_offset).
_LAG_SANITY_MS = 3000
# 오프셋이 우리 카운터보다 살짝 앞설 수는 있다(우리가 센 바이트와 엔진이 받은 바이트의 미세한
# 시차). 이만큼은 정상으로 본다.
_OFFSET_FUTURE_TOLERANCE_MS = 500
# barge-in 에너지 이력: 이만큼의 오디오를 (시각, RMS)로 들고 있다가 **이벤트가 가리키는
# 시각**에서 찾아본다. 파이프라인 지연(실측 0.8~0.9초)보다 넉넉해야 한다.
_RMS_HISTORY_MS = 4000
_RMS_WINDOW_MS = 400        # 그 시각 ± 이 범위에서 가장 큰 에너지를 본다
# 에코 판정: 이보다 짧은 발화는 겹침을 재도 의미가 없다(짧은 맞장구를 버리면 더 나쁘다).
_ECHO_MIN_CHARS = 6
_ECHO_OVERLAP = 0.6         # 글자 2-gram 이 이 비율 이상 겹치면 비버 자기 목소리로 본다
# 화면에서 고를 수 있는 엔진 값(서버가 아는 것만 받는다). Gemini 쪽 값은 core.tts 가 소유한다.
_CHIRP_CHOICE = "chirp3-hd"
_STYLE_PROMPT_MAX = 200     # 스타일 문구 상한 — 길어지면 지연 비교가 오염된다
# (TTS 벤더 이름은 core.tts 가 소유한다 — 엔진 A/B 로 값이 바뀌므로 _tts_vendor() 로 읽는다)


def _marker_state(text: str) -> str:
    """이 문장에서 언어 마커가 어떤 상태였나 — **셋을 갈라야** 판정이 된다.

      없음      : 모델이 마커를 안 썼다(규칙이 안 먹혔다)
      있음      : 짝이 맞는 마커가 있었다(설계대로 동작)
      짝안맞음  : 모델이 반만 지켰다 → 통째로 기본 언어로 폴백했다

    ⚠ '구간 1개'만 보면 "마커가 없었다"와 "마커가 있었지만 전부 한 언어였다"가 섞인다.
      그 둘이 섞이면 실험이 성립하지 않는다.
    """
    count = (text or "").count(MARKER)
    if count == 0:
        return "없음"
    return "있음" if count % 2 == 0 else "짝안맞음"


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

    def __init__(self, transport: CascadeTransport, genai_client: Any = None) -> None:
        self.transport = transport
        # P1: 비버가 말하려면 LLM 클라이언트가 있어야 한다. 없으면 **턴 감지만**(P0 동작) —
        # 키가 없다고 통화가 죽으면 안 된다(R5).
        self._genai_client = genai_client
        self._history: list[dict] = []
        # 상한을 넘어 이력에서 잘라낸 줄들. 지금은 안 쓰지만 **버리지 않고 들고 있는다** —
        # 나중에 요약해 되먹이려면 원문이 우리 손에 있어야 한다(Live 는 이게 불가능했다).
        self._history_dropped: list[dict] = []
        self._reply_task: asyncio.Task | None = None
        self._reply_cancelled = False
        self._system_cache: str | None = None
        self._tts_engines: set[str] = set()   # **이 대답에서** 실제로 소리를 낸 엔진(A/B 로그용)
        # TTS 선택은 **세션 값**이다(예전엔 매 문장 settings 를 읽었다). 클라가 start 에서
        # 고르면 그 값으로, 안 고르면 서버 설정으로 통화 내내 일관되게 간다.
        self._tts_engine = (settings.CASCADE_TTS_ENGINE or "").strip()
        self._tts_rate: float | None = None
        self._tts_style: str | None = None
        self._marker_seen: dict[str, int] = {}   # 언어 마커 상태별 문장 수(실험 성립 판정)
        # 429 백오프는 **세션 단위**다(프로세스 전역이면 쿼터가 회복돼도 영영 Chirp 이다).
        self._tts_gemini_off = False
        self._tts_gemini_calls = 0
        # barge-in 보류 상태(전사 확인 대기 마감 시각) / 끊겨서 못 들려준 대답
        self._bargein_at: float | None = None
        self._interrupted: dict | None = None
        # 비버가 말하는 동안 들어온 발화(대답이 끝나면 답한다) / 지금 비버가 하는 말(에코 판정)
        self._pending_user_text = ""
        self._speaking_text = ""
        self._rms_log: deque[tuple[float, float]] = deque()
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
        self._lag_warned = False              # 비정상 지연은 **한 번만** 크게 알린다
        # 방금 닫은 턴(유령 턴 차단 — 같은 발화가 턴 2개가 되는 것을 막는다)
        self._closed_turn_id: str | None = None
        self._closed_end_offset_ms = -1
        self._closed_text = ""
        self._closed_at = 0.0
        # barge-in 에코 2차 방어(세션 단위 — 기기/라우트마다 달라야 한다)
        self._bargein_confirm = settings.CASCADE_BARGEIN_CONFIRM
        self._aec_mode = "unknown"
        self._recent_rms = 0.0
        # 비버 출력(P1: TTS 송출·원장·페이서). P0 에서는 오디오를 내지 않지만, 클라가
        # 되보내는 playback_progress 를 **턴별 원장에 대조**하려면 지금부터 있어야 한다.
        self.beaver = BeaverOutput(transport)
        self._spoken_by_turn: dict[str, str] = {}   # turn_id → 실제로 들린 대사(이력용)
        # 원가 계측 — 캐스케이드의 **유일한 동기**가 원가라 세션이 끝나면 반드시 한 줄 남긴다.
        # P0 에서 실제로 도는 구간은 STT 뿐이고, LLM·TTS 수집 지점은 P1 이 붙인다(cascade_usage).
        self.usage = CascadeUsage()
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
            raw = ctrl.get("sampleRate") or ctrl.get("sample_rate")
            # ⭐ 샘플레이트는 **조용히 어긋나는** 자리다. 클라가 안 보내면 서버는 16000 을
            #   가정하는데, 클라 마이크가 16k 가 아니면 오디오는 정상 재생되면서 목소리만
            #   이상해진다(느려지거나 빨라진다) — 에러가 없어 원인 찾기가 제일 나쁜 종류다.
            #   지금 앱은 이 값을 **안 보낸다**(2026-08-07 확인). 그래서 우연히 맞는 상태다.
            #   고칠 쪽은 클라지만, 서버는 최소한 **무엇을 가정했는지 로그로 드러낸다**.
            if raw is None:
                logger.info(
                    "cascade start: sample_rate 미전송 → 기본 %dHz 가정(클라 마이크가 다르면 "
                    "목소리가 이상해진다 — 에러는 안 난다)", _DEFAULT_SAMPLE_RATE,
                )
            try:
                sample_rate = int(raw) if raw is not None else _DEFAULT_SAMPLE_RATE
            except (TypeError, ValueError):
                logger.warning("cascade start: sample_rate 해석 실패(%r) → %dHz 로 진행",
                               raw, _DEFAULT_SAMPLE_RATE)
                sample_rate = _DEFAULT_SAMPLE_RATE
            if sample_rate != _DEFAULT_SAMPLE_RATE:
                logger.warning(
                    "cascade start: 클라 sample_rate=%dHz 가 서버 기대(%dHz)와 다르다 — STT 는 "
                    "이 값으로 설정하지만 오디오 타임라인·지연 계측이 이 값에 의존한다",
                    sample_rate, _DEFAULT_SAMPLE_RATE,
                )
            self._apply_tts_choice(ctrl)
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
                # ⭐ 선톡 — 비버가 먼저 인사한다(Live 와 같은 규약: call_session.py:1574).
                #   안 하면 둘 다 서로 말하기를 기다려 통화가 조용히 멈춘다. 덤으로 콜드
                #   스타트를 흡수한다: 실측에서 첫 대답만 9971ms 였고 그다음은 2.6~3.0초였다.
                #   사용자가 마이크를 허용하고 자세를 잡는 사이에 그 10초가 인사말에 실린다.
                if settings.CASCADE_GREETING:
                    self._start_reply(seed_opening(), is_greeting=True)
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
            # ⭐ 원가 한 줄. **stream.close() 뒤**에 걷는다 — 마지막 스트림이 닫히면서
            # 그 스트림의 과금 계측이 세션 누계로 넘어오기 때문이다(core/stt.py _absorb_usage).
            # 계측 전 구간이 예외를 흡수하므로 여기서 통화가 죽을 일은 없다(R5).
            self.usage.record_stt(stream, stt_mod.stt_v2_engine_name())
            log_usage_summary(
                self.usage,
                duration_s=time.monotonic() - self._t0,
                turns=self._turn_seq,
            )

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
                    # ⭐ **에너지도 오디오 시각과 함께** 남긴다(2026-08-07, 오늘 세 번째 '두 시계').
                    #   예전엔 '지금 프레임의 RMS' 한 값만 들고 있었는데, barge-in 판정은
                    #   **~800ms 전 오디오**를 가리키는 speech_begin 으로 일어난다. 짧게 말하면
                    #   이벤트가 도착했을 땐 이미 조용해서 RMS≈0 → 기각(실측 0.0000~0.0017).
                    #   그래서 **그때 그 오디오의 에너지**를 찾아볼 수 있게 짧은 이력을 둔다.
                    self._rms_log.append((self._audio_ms, _frame_rms(inb.audio)))
                    cutoff = self._audio_ms - _RMS_HISTORY_MS
                    while self._rms_log and self._rms_log[0][0] < cutoff:
                        self._rms_log.popleft()
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
            deadlines = [
                d for d in (self._close_at, self._turn_deadline, self._bargein_at)
                if d is not None
            ]
            timeout = max(0.0, min(deadlines) - now) if deadlines else None
            try:
                if timeout is None:
                    item = await self._q.get()
                else:
                    item = await asyncio.wait_for(self._q.get(), timeout)
            except asyncio.TimeoutError:
                # 보류 중이던 barge-in 의 지속 시간이 찼다 — 전사가 안 와도 이만큼 이어지는
                # 발성은 잡음이 아니다(STT 지연으로 진짜 끼어들기가 영영 안 먹는 걸 막는다).
                if self._bargein_at is not None and time.monotonic() >= self._bargein_at:
                    if self._speech_active:
                        await self._confirm_bargein(None, "음성 지속")
                    else:
                        self._bargein_at = None
                        logger.info("cascade barge-in 기각 — 전사도 지속도 없었다(잡음 추정)")
                    continue
                # 침묵 타이머 만료 = 턴 종료. **이 판정이 캐스케이드의 심장이다.**
                expired_silence = (
                    self._close_at is not None and time.monotonic() >= self._close_at
                )
                await self._close_turn("silence" if expired_silence else "max")
                continue
            if item is _EOS:
                raise _Stop
            await self._handle(item)

    def _sanitize_offset(self, event: SttV2Event) -> None:
        """⭐ 상식 밖 오프셋은 **미상(-1)으로 거절한다** — 결함 A(2026-08-07 확정).

        실측: `lag=79377ms kind=speech_begin offset_ms=860 audio_ms=73113`, 그리고 같은 통화의
        `stt_streams=1` — **롤오버가 0회인데 79초가 튀었다.** 우리 기준점(rebase) 문제가 아니라
        `speech_event_offset` 자체가 전역 오디오 타임라인이 아니라는 뜻이다(문서에는 'beginning
        of the audio' 라고 돼 있는데 실측이 다르다).

        ⛔ 그래서 **벤더의 의미를 안다고 가정하지 않는다.** 우리가 확실히 아는 것은 하나뿐이다 —
        "우리가 STT 로 흘려보낸 오디오보다 한참 과거를 가리키는 값은 침묵 계산에 쓸 수 없다".
        그런 값은 버리고 미상으로 둔다. 그러면 기존 폴백(오프셋 없이 전체 침묵 대기)으로 흐른다.

        오염값 하나가 **세 곳을 동시에** 망가뜨리기 때문에 여기 한 곳에서 막는다:
          ① 계측(pipeline_lag) ② 침묵 타이머 remain(=0 이 되어 턴이 즉시 닫힌다 — 결함 B 재발)
          ③ barge-in 최소 지속 게이트(audio_ms − offset >= min_ms 가 **무조건 참**이 된다.
             마이크 상시개방을 켜면 이게 곧바로 드러난다 — 잔여 에코 한 번에 비버가 끊긴다)
        """
        if event.offset_ms < 0:
            return
        drift_ms = self._audio_ms - event.offset_ms
        if -_OFFSET_FUTURE_TOLERANCE_MS <= drift_ms <= _LAG_SANITY_MS:
            return
        if not self._lag_warned:
            self._lag_warned = True
            logger.warning(
                "cascade 오프셋 거절: kind=%s offset_ms=%d audio_ms=%.0f 차이=%.0fms "
                "(전역 오디오 시각이 아니다 → 미상 처리, 침묵은 전체 대기로 폴백)",
                event.kind, event.offset_ms, self._audio_ms, drift_ms,
            )
        event.offset_ms = -1

    async def _handle(self, event: SttV2Event) -> None:
        self._sanitize_offset(event)
        if event.kind == SPEECH_BEGIN:
            await self._on_speech_begin(event)
        elif event.kind == TRANSCRIPT:
            await self._on_transcript(event)
        elif event.kind == SPEECH_END:
            await self._on_speech_end(event)
        elif event.kind == STREAM_ROLLOVER:
            # ⭐ 서버 로그에도 남긴다(2026-08-07). 지금까지 롤오버는 WS 이벤트로만 나가서
            # **서버 로그에서는 보이지 않았다** — 그래서 실통화에서 지연이 79초로 튀었을 때
            # 롤오버가 있었는지조차 확인할 수 없었다. 진단은 로그로 한다.
            logger.info(
                "cascade stt 롤오버: reason=%s gap_ms=%d audio_ms=%.0f 다음턴부터 새 스트림",
                event.detail, event.gap_ms, self._audio_ms,
            )
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
        # 보류해 둔 barge-in 이 있으면 **여기가 확정 지점**이다 — 잡음은 전사를 못 만든다.
        if self._bargein_at is not None:
            if len((event.text or "").strip()) >= max(1, settings.CASCADE_BARGEIN_MIN_CHARS):
                await self._confirm_bargein(event, "전사 확인")
            elif not event.text.strip():
                return
        # ⭐ **열린 턴이 없으면 연다 — 비버가 말하는 중이어도.**(2026-08-07)
        #   예전엔 `state == IDLE` 일 때만 열었다. 그러면 barge-in 이 기각된 뒤 사용자가 말하면
        #   state 가 BEAVER_SPEAKING 인 채라 **턴이 안 열리고**, 전사는 화면에 뜨는데
        #   (input_transcript 는 그대로 나간다) 닫을 턴이 없어 **LLM 이 영영 안 불렸다.**
        #   사장님 증상이 정확히 이것이었다: "전사는 되는데 대답이 없어."
        #   기각의 의미는 "비버를 끊지 않는다"지 "사용자 말을 무시한다"가 아니다.
        if self._turn_id is None:
            if self._is_stale_tail(event):
                return
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

    def _is_stale_tail(self, event: SttV2Event) -> bool:
        """방금 닫은 턴의 **꼬리**인가 — 그렇다면 새 턴을 열지 않는다(유령 턴 차단).

        왜 필요한가: 턴은 서버 타이머가 닫는데, 그 발화의 최종 전사가 **닫힌 뒤에** 도착할
        수 있다(파이프라인 지연·롤오버 재인식). 그걸 그대로 받으면 IDLE 상태라 새 턴이
        열리고, 같은 말이 턴 2개가 된다 — P1 에서 비버가 두 번 대답하는 버그가 된다.

        판정은 **오디오 시각**으로 한다: 이 전사가 가리키는 오디오 끝이 방금 닫은 턴의 마지막
        음성 지점보다 **뒤로 가지 않으면** 새 소리가 아니다. 오프셋을 모르는 엔진(페이크·구
        버전)에서는 같은 텍스트인지로만 본다 — 판정 재료가 없을 때 새 턴을 막는 쪽이 더 위험해
        (진짜 발화를 삼킨다) 유예 시간(CASCADE_STALE_FINAL_MS) 안에서만 막는다.
        """
        if self._closed_at <= 0.0:
            return False
        if (time.monotonic() - self._closed_at) * 1000.0 > max(0, settings.CASCADE_STALE_FINAL_MS):
            return False
        # ⭐⭐ **닫힌 턴이 비어 있었으면 절대 버리지 않는다**(2026-08-07 사장님 통화).
        #   중복 차단은 "이미 전달한 말을 또 내지 않기" 위한 것이다. 아무것도 전달하지 않은
        #   턴이라면 중복이 생길 수가 없고, 늦게 온 그 전사가 **곧 그 턴의 내용**이다.
        #   실제로 이것 때문에 진짜 발화가 사라졌다: u2/u4/u7/u9 가 speech_ms=1~4초인데
        #   text='' 로 닫혔고("안녕 두 글자를 인식 못 할 때가 있네"), 뒤늦게 온 진짜 전사는
        #   꼬리로 분류돼 버려졌다. 중복을 막으려다 발화를 버리면 더 나쁘다.
        if not self._closed_text:
            logger.info("cascade 늦은 전사 수용 — 직전 턴(%s)이 비어 있었다: %r",
                        self._closed_turn_id, (event.text or "").strip()[:40])
            return False
        text = (event.text or "").strip()
        if event.offset_ms >= 0 and self._closed_end_offset_ms >= 0:
            fresh = event.offset_ms > self._closed_end_offset_ms + _STALE_OFFSET_EPSILON_MS
        else:
            fresh = bool(text) and text != self._closed_text
        if fresh:
            return False
        logger.info(
            "cascade 유령 턴 차단: 방금 닫은 %s 의 꼬리 전사(offset=%d ≤ %d) text=%r",
            self._closed_turn_id, event.offset_ms, self._closed_end_offset_ms, text,
        )
        return True

    def _mark_voice(self, event: SttV2Event) -> None:
        """마지막 음성 활동 지점을 오디오 시각으로 기록 + 파이프라인 지연 계측."""
        self._last_voice_at = event.at
        if event.offset_ms >= 0:
            self._last_voice_offset_ms = event.offset_ms
            # 여기 도착하는 오프셋은 _sanitize_offset 을 이미 통과했다(상식 밖 값은 -1 로
            # 걸러져 이 분기에 들어오지 않는다) — 그래서 이 지연값은 계측으로 믿을 수 있다.
            self._pipeline_lag_ms = int(max(0.0, self._audio_ms - event.offset_ms))

    def _arm_close_timer(self, event: SttV2Event) -> None:
        """침묵 카운트다운을 건다 — **이미 흘러간 침묵은 빼되, 바닥 아래로는 안 내린다.**

        이벤트가 오디오 시각(offset)을 들고 오면 `audio_ms_sent − offset` 이 이미 지난
        침묵이다. 그만큼을 빼야 리전 왕복·인식 지연이 임계에 얹히지 않는다(오프셋이 없으면
        0으로 보고 그냥 전체를 기다린다 — 페이크·구버전 대비 폴백).

        ⭐ 2026-08-07 실통화가 이 뺄셈의 전제를 깼다. 전제는 **파이프라인 지연 < 침묵 임계**
        인데, 실측 지연이 810~914ms 로 임계(800ms)를 넘었다. 그러면 남은 대기가 0 이 되어
        **턴이 speech_end 를 처리하는 순간 닫히고**, 0.02~0.9초 뒤 도착한 최종 전사가 IDLE
        상태에서 **같은 발화로 턴을 하나 더 연다**(u2/u3, u16/u17 … speech_ms=0 짜리 유령 턴).
        P1 에서는 비버가 같은 말에 두 번 대답하게 된다.

        그래서 바닥(CASCADE_TURN_MIN_WAIT_MS)을 둔다 — 지연이 임계를 넘어도 **최종 전사가
        도착할 시간은 항상 남긴다.** 지연이 임계보다 작을 때의 동작은 예전과 같다.
        """
        already_ms = 0.0
        # ⭐ **VAD 이벤트의 오프셋으로는 빼지 않는다**(2026-08-07). 두 시계가 다르기 때문이다:
        #   우리가 기다리는 건 **최종 전사**인데, 그 지연(실측 723~870ms)이 VAD 이벤트 지연
        #   (실측 291~348ms)보다 훨씬 크다. VAD 기준으로 300ms 를 빼고 500ms 만 기다리면
        #   전사는 그 뒤에 도착하고, 그 턴은 **말을 했는데 빈 채로** 닫힌다(u2/u4/u7/u9 —
        #   speech_ms 가 1~4초인데 text=''). 전사 오프셋일 때만 빼는 게 맞다.
        if event.offset_ms >= 0 and event.kind == TRANSCRIPT:
            already_ms = max(0.0, self._audio_ms - event.offset_ms)
        remain_s = max(0.0, (self._silence_ms - already_ms) / 1000.0)
        floor_s = max(0, settings.CASCADE_TURN_MIN_WAIT_MS) / 1000.0
        self._close_at = time.monotonic() + max(remain_s, floor_s)

    async def _open_turn(self, at: float) -> None:
        self._turn_seq += 1
        self._turn_id = f"u{self._turn_seq}"
        self._turn_began_at = at
        self._finals = []
        self._partial = ""
        self._close_at = None
        self._turn_deadline = at + max(5, settings.CASCADE_TURN_MAX_S)
        # ⛔ 비버가 말하는 중이면 **상태를 뺏지 않는다.** 마이크가 열려 있으면 사용자 턴과
        #   비버 턴은 겹칠 수 있다(그게 barge-in 이 가능한 이유다). 여기서 상태를 덮으면
        #   비버 쪽 취소·종료 경로가 자기 상태를 잃는다.
        if self.state not in (TurnState.BEAVER_SPEAKING, TurnState.THINKING,
                              TurnState.CANCELLING):
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
        # 유령 턴 차단용 기록 — **닫은 턴의 끝**을 오디오 시각으로 남긴다(_is_stale_tail).
        self._closed_turn_id = self._turn_id
        self._closed_end_offset_ms = self._last_voice_offset_ms
        self._closed_text = text
        self._closed_at = now
        if not text:
            # 빈 턴: 상태가 굳으면 안 되니 **닫기는 한다**. 다만 이건 사용자 발화가 아니다 —
            # ⛔ text=='' 인 턴으로 LLM 을 부르지 않는다(빈 입력 호출 = 원가 + 헛대답).
            logger.info("cascade 빈 턴: id=%s reason=%s — 발화 없음(LLM 호출 금지)",
                        self._turn_id, reason)
        else:
            # 사용자가 실제로 말했다 = 대화가 진행됐다. 끊겼던 대답을 되살릴 이유가 없다.
            self._interrupted = None
        self._turn_id = None
        self._close_at = None
        self._turn_deadline = None
        self._finals = []
        self._partial = ""
        # 비버가 말하는 중이었다면 그 상태를 그대로 둔다(위 _open_turn 과 같은 이유).
        if self.state == TurnState.USER_SPEAKING:
            self.state = TurnState.IDLE
        # ⭐ 여기서 비버가 대답한다(P1). ⛔ 빈 텍스트면 부르지 않는다 — 빈 입력 LLM 호출은
        # 원가만 나가고 헛대답을 만든다(결함 C 판단, 2026-08-07).
        if text:
            self._start_reply(text)
        else:
            # ⭐ 빈 턴이라고 침묵으로 두지 않는다 — 직전에 **비버를 죽였다면** 하던 말을
            #   이어서 한다. 사장님 45분 통화에서 이 자리가 dead air 였다.
            self._resume_interrupted()

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
        # ⭐ ⓪-1 **비버가 실제로 들리고 있나.** 사용자가 한 글자도 못 들었으면 끼어든 게
        #   아니다 — 그냥 기다리다 소리를 낸 것이다. 끊어봐야 멈출 소리가 없고(이득 0),
        #   준비한 대답만 통째로 사라진다(손실 큼). 2026-08-07 45분 통화에서 취소 14건 중
        #   7건이 이 경우였고 그 뒤가 전부 빈 턴 → 침묵이었다("너가 말을 안 듣잖아").
        #   ⚠ 판정은 **오디오 시간**으로 한다(들린 '글자'는 문장 단위라 2초를 들었어도 0 일
        #   수 있다). 이 관문은 진짜 barge-in 을 느리게 하지 않는다 — 진짜 끼어들기는 비버가
        #   들릴 때 일어난다.
        audible_ms = self._audible_ms()
        min_audible = max(0, settings.CASCADE_BARGEIN_MIN_AUDIBLE_MS)
        if audible_ms < min_audible:
            logger.info(
                "cascade barge-in 기각 — 비버가 아직 안 들린다(들린 %dms < %dms). 대답을 살린다",
                audible_ms, min_audible,
            )
            return False
        # ⓪ 마이크 상시 개방이 꺼져 있으면 barge-in 을 시도하지 않는다.
        #   그 모드에서는 클라가 비버 발화 중 마이크를 닫으므로, 이때 들어오는 음성 활동은
        #   **에코이거나 게이팅 타이밍 결함**일 가능성이 높다(실측: call 855 에서 유저 턴의
        #   절반이 비버 대사였다). 서버는 두 모드를 모두 견뎌야 하고, OFF 에서는 견디는 방법이
        #   "끼어들지 않는 것"이다.
        if not settings.CASCADE_MIC_ALWAYS_OPEN:
            logger.info("cascade barge-in 기각 — 마이크 상시개방 OFF(에코/게이팅 잔여 추정)")
            return False
        # ① 에너지 임계 — 잔여 에코는 대개 original 보다 작다(0 이면 비활성).
        #   ⛔ **'지금'이 아니라 '그 이벤트가 가리키는 오디오'의 에너지를 본다.** 임계값은
        #   그대로다(에코 리그 실측으로 잡을 값이다) — 고친 건 **언제 재느냐**다.
        threshold = settings.CASCADE_BARGEIN_RMS
        if threshold > 0:
            rms = self._rms_at(event.offset_ms)
            if rms < threshold:
                logger.info(
                    "cascade barge-in 기각 — 에너지 %.4f < 임계 %.4f(에코 추정, offset=%d)",
                    rms, threshold, event.offset_ms,
                )
                return False
        # ② 최소 지속 — 순간 튐으로 비버를 끊지 않는다.
        min_ms = max(0, settings.CASCADE_BARGEIN_MIN_MS)
        # ⚠ min_ms=0 이어도 여기서 바로 True 를 돌려주면 **아래 ③ 전사 확인이 통째로
        #   건너뛰어진다.** 예전 코드가 그랬다(그래서 지속 시간을 0 으로 두면 잡음이 전부
        #   통과했다). 지속 대기만 건너뛰고 관문은 계속 태운다.
        # 지속 판정은 오디오 시각으로 한다 — 도착 시각으로 재면 리전 왕복이 섞인다.
        if min_ms > 0 and not (event.offset_ms >= 0 and (self._audio_ms - event.offset_ms) >= min_ms):
            await asyncio.sleep(min_ms / 1000.0)
            if not self._speech_active:
                logger.info("cascade barge-in 기각 — 최소 지속 %dms 미달(에코/잡음 추정)", min_ms)
                return False
        # ③ 전사 확인 — **설계에 있다고 적어 두고 P1 에서 안 붙였던 관문**이다(2026-08-07).
        #   그동안 barge-in 은 에너지+지속만으로 발동했고, 기침·키보드·숨소리가 전부 통과했다.
        #   여기서 True 를 돌려주면 즉시 취소되므로, transcript 모드는 **판정을 미룬다**:
        #   전사가 오거나(_on_transcript) 음성이 길게 이어지면(_pump_turn 타이머) 그때 친다.
        if self._bargein_confirm == "transcript":
            self._bargein_at = time.monotonic() + max(0, settings.CASCADE_BARGEIN_SUSTAIN_MS) / 1000.0
            logger.info("cascade barge-in 보류 — 전사 확인 대기(잡음이면 여기서 끝난다)")
            return False
        return True

    def _looks_like_echo(self, text: str) -> bool:
        """비버가 방금 한(또는 하는 중인) 말과 겹치나 — 겹치면 **에코**다.

        ⛔ 이게 없으면 (A)가 위험해진다: 기각된 발화도 답하게 만들었으므로, 그게 에코라면
        **비버가 자기 말에 답한다.** 잡음은 전사를 못 만들지만 에코는 만든다 — 그래서
        "전사가 나왔다"만으로는 못 가른다. 우리는 비버 대사를 갖고 있으니 그걸로 가른다.

        판정은 글자 2-gram 겹침이다(전사는 조사·띄어쓰기가 흔들려서 완전일치로는 못 잡는다).
        짧은 맞장구("네", "응")는 겹침을 재기엔 정보가 없어 **에코로 보지 않는다** — 진짜
        발화를 버리는 쪽이 더 나쁘다.
        """
        probe = "".join((text or "").split())
        if len(probe) < _ECHO_MIN_CHARS:
            return False
        said = self._beaver_said_recently()
        if not said:
            return False
        grams = {probe[i:i + 2] for i in range(len(probe) - 1)}
        if not grams:
            return False
        hit = sum(1 for g in grams if g in said)
        return hit / len(grams) >= _ECHO_OVERLAP

    def _beaver_said_recently(self) -> str:
        """비버가 방금 한 말(이력의 마지막 model 발화 + 지금 생성 중인 대사)."""
        parts = [self._speaking_text]
        for item in reversed(self._history):
            if item.get("role") == "model":
                parts.append(item.get("text") or "")
                break
        return "".join("".join(p.split()) for p in parts)

    def _rms_at(self, offset_ms: int) -> float:
        """그 오디오 시각 부근의 **최대** 에너지. 이력이 없으면 0.

        최대를 쓰는 이유: 이벤트 오프셋에는 오차가 있고(엔진마다 기준이 다르다 — 결함 A),
        발화의 시작 프레임은 원래 작다. 창 안에서 가장 큰 값이 "그때 소리가 났나"에 가장
        가까운 답이다. 오프셋을 모르면(거절됐거나 미제공) **최근 창 전체**에서 찾는다 —
        그 경우 관문이 느슨해지지만, 발화를 통째로 버리는 쪽이 더 나쁘다.
        """
        if not self._rms_log:
            return 0.0
        if offset_ms < 0:
            return max(rms for _, rms in self._rms_log)
        lo, hi = offset_ms - _RMS_WINDOW_MS, offset_ms + _RMS_WINDOW_MS
        window = [rms for at, rms in self._rms_log if lo <= at <= hi]
        return max(window) if window else max(rms for _, rms in self._rms_log)

    def _audible_ms(self) -> int:
        """지금 비버 턴에서 **사용자가 들었을** 오디오 길이(ms, 짧은 쪽 편향 추정)."""
        turn_id = self.beaver.turn_id
        if turn_id is None:
            return 0
        return int(self.beaver.estimated_played_bytes(turn_id) / BEAVER_BYTES_PER_MS)

    async def _confirm_bargein(self, event: SttV2Event | None, reason: str) -> None:
        """보류해 둔 barge-in 을 확정한다(전사가 왔거나 음성이 길게 이어졌다)."""
        self._bargein_at = None
        if self.state not in (TurnState.BEAVER_SPEAKING, TurnState.THINKING):
            return
        if self._audible_ms() < max(0, settings.CASCADE_BARGEIN_MIN_AUDIBLE_MS):
            logger.info("cascade barge-in 취소 — 확인 사이에 비버가 안 들리는 상태가 됐다")
            return
        logger.info("cascade barge-in 확정(%s)", reason)
        await self._on_barge_in(event or SttV2Event(kind=SPEECH_BEGIN))

    async def _on_barge_in(self, event: SttV2Event) -> None:
        """barge-in 취소 배관 — **세 곳을 동시에** 친다(설계 §4).

          ① LLM·TTS 태스크 cancel(안 그러면 계속 생성돼 과금된다)
          ② 서버 송출 중단 + epoch += 1
          ③ audio_cancel 전송 → 클라가 버퍼 폐기 + played_server_bytes 회신(이력 절단 근거)

        ①을 **await 하지 않는다**: 취소 완료를 기다리면 그만큼 비버가 더 말한다. 취소된
        태스크가 뒤늦게 send() 를 부르면 턴이 이미 닫혀 InvariantError 가 나는데, 그건
        정상 경로라 태스크 쪽에서 로그만 남긴다.
        """
        self.state = TurnState.CANCELLING
        logger.info("cascade barge-in 감지 — 취소 배관 실행")
        self._cancel_reply()
        if self.beaver.turn_id is not None:
            await self.beaver.cancel(reason="barge_in")

    # ── P1: LLM → TTS → 비버 송출 ──
    def _reply_enabled(self) -> bool:
        """비버가 말할 수 있는 상태인가(클라이언트·모델·TaskGroup). 아니면 P0 처럼 턴만 감지한다."""
        return self._genai_client is not None and self._tg is not None

    def _cancel_reply(self) -> None:
        task, self._reply_task = self._reply_task, None
        if task is not None and not task.done():
            self._reply_cancelled = True
            task.cancel()

    def _start_reply(self, user_text: str, is_greeting: bool = False) -> None:
        if not self._reply_enabled() or not user_text.strip():
            return
        if self._looks_like_echo(user_text):
            # ⛔ 비버 자기 목소리가 전사된 것 — 답하면 **비버가 자기 말에 답한다.**
            logger.info("cascade 발화 무시 — 비버 대사와 겹친다(에코 추정): %r", user_text[:40])
            return
        if self._reply_task is not None and not self._reply_task.done():
            # 앞 대답이 흐르는 중이면 겹쳐 말하지 않는다(불변식 I1 — 비버 턴은 하나).
            # ⭐ 그렇다고 **버리지도 않는다**(2026-08-07). 예전엔 여기서 그냥 건너뛰어서
            #   "대답 다 해도 내가 중간에 말한 거에 답을 안 해" 가 됐다. 줄 세워 뒀다가
            #   대답이 끝나면 그때 답한다.
            self._pending_user_text = user_text
            logger.info("cascade 발화 대기열 — 비버가 말하는 중이라 대답 뒤로 미룬다: %r",
                        user_text[:40])
            return
        self.state = TurnState.THINKING
        self._reply_task = self._tg.create_task(self._run_reply(user_text, is_greeting))

    async def _run_reply(self, user_text: str, is_greeting: bool = False) -> None:
        """사용자 발화 1건에 대한 비버의 대답 — LLM 스트리밍 → 문장 분할 → TTS → 송출."""
        self._reply_cancelled = False
        # ⛔ **대답마다 비운다.** 안 비우면 이전 턴의 엔진·마커 집계가 누적돼 로그가
        #   `tts=chirp+gemini` 처럼 섞여 찍히고, **어느 엔진이 낸 소리인지 못 가린다**
        #   (A/B 판정이 오염된다 — 2026-08-07 실측 로그에서 그 증상이 나왔다).
        self._tts_engines.clear()
        self._marker_seen.clear()
        chat = gemini_chat.open_chat_stream(
            self._genai_client,
            settings.CASCADE_LLM_MODEL,
            system_instruction=self._system_instruction(),
            history=self._history,
            user_text=user_text,
            thinking_budget=settings.CASCADE_LLM_THINKING_BUDGET,
        )
        if chat is None:
            self.state = TurnState.IDLE
            return
        self._history.append({"role": "user", "text": user_text})
        buffer = SentenceBuffer()
        turn_id: str | None = None
        spoken_chars = 0
        began = time.monotonic()
        first_audio_ms = -1
        try:
            # ⭐ **첫 문장은 즉시, 그 뒤는 묶어서** 합성한다(2026-08-07 실통화의 429 대응).
            #   문장마다 스트림을 열면 요청 수가 턴당 7회까지 갔고(57 calls / 8턴) 분당 요청
            #   쿼터에 걸렸다(429). 묶으면 요청이 크게 줄고 **문장 간 억양도 이어진다**
            #   — 사장님이 "문장마다 톤이 바뀐다"고 하신 것이 같은 원인이다.
            #   ⛔ 첫 문장은 절대 묶지 않는다. 첫 소리가 그만큼 늦어진다.
            pending: list[str] = []

            async def _flush_batch() -> None:
                nonlocal turn_id, first_audio_ms, spoken_chars, pending
                if not pending:
                    return
                text_batch, pending = " ".join(pending), []
                turn_id = turn_id or await self._begin_beaver_turn()
                sent = await self._speak(text_batch)
                if sent and first_audio_ms < 0:
                    first_audio_ms = int((time.monotonic() - began) * 1000)
                spoken_chars += len(text_batch)

            async for piece in chat.chunks():
                self._speaking_text = chat.text     # 에코 판정 재료(지금 하는 말)
                for sentence in buffer.push(piece):
                    if first_audio_ms < 0 and not pending:
                        pending.append(sentence)
                        await _flush_batch()        # 첫 문장 = 단독 즉시 송출
                        continue
                    pending.append(sentence)
                    if sum(len(x) for x in pending) >= max(1, settings.CASCADE_TTS_BATCH_CHARS):
                        await _flush_batch()
            tail = buffer.flush()
            if tail:
                pending.append(tail)
            await _flush_batch()
            if turn_id is not None:
                await self.beaver.end()
            self._remember_beaver(turn_id, chat.text)
            # ⭐ TTS 엔진을 같이 찍는다 — 이 줄만 보고 A/B(첫소리 지연)를 가를 수 있어야 한다.
            #   폴백이 일어나면 실제로 소리를 낸 엔진이 여기 남는다(의도한 엔진이 아니라).
            logger.info(
                "cascade 대답%s: turn=%s 첫소리=%dms 글자=%d 문장모델=%s tts=%s 마커=%s "
                "gemini호출=%d %s",
                "(선톡)" if is_greeting else "", turn_id, first_audio_ms, spoken_chars,
                settings.CASCADE_LLM_MODEL,
                "+".join(sorted(self._tts_engines)) or self._tts_vendor(),
                ",".join(f"{k}{v}" for k, v in sorted(self._marker_seen.items())) or "-",
                self._tts_gemini_calls, "고정" if self._tts_gemini_off else "-",
            )
        except asyncio.CancelledError:
            # ⚠ 우리가 건 취소(barge-in)는 여기서 흡수하고 정상 종료한다 — 이 태스크는 세션
            # TaskGroup 의 자식이라, 밖에서 취소된 채 다시 올리면 **세션 전체가 무너진다**.
            if not self._reply_cancelled:
                raise
            self._on_reply_cancelled(turn_id, chat.text)
        except InvariantError:
            logger.info("cascade 대답 송출 중단(턴이 이미 닫힘) turn=%s", turn_id)
        finally:
            self._speaking_text = ""
            self.usage.record_llm(chat.usage_metadata, vendor=settings.CASCADE_LLM_MODEL)
            if self.state in (TurnState.THINKING, TurnState.BEAVER_SPEAKING):
                self.state = TurnState.IDLE
            self._drain_pending_user_text()

    def _drain_pending_user_text(self) -> None:
        """비버가 말하는 동안 들어온 발화에 **이제** 답한다(줄 세워 둔 것 하나).

        ⛔ 여기서 안 부르면 그 발화는 영영 답을 못 받는다 — 사장님이 겪으신 그 증상이다.
        """
        pending, self._pending_user_text = self._pending_user_text, ""
        if not pending:
            return
        self._reply_task = None      # 방금 끝난 태스크 참조를 비워야 새 대답이 시작된다
        logger.info("cascade 대기열 발화에 답한다: %r", pending[:40])
        self._start_reply(pending)

    async def _begin_beaver_turn(self) -> str:
        self.state = TurnState.BEAVER_SPEAKING
        return await self.beaver.begin()

    async def _speak(self, sentence: str) -> int:
        """문장 하나를 **언어 구간별로** 합성해 송출한다.

        비버는 타깃 언어 부분을 __이렇게__ 감싸서 낸다. 그 경계로 잘라 구간마다 그 언어로
        읽는다 — 감싼 부분을 모국어 발음으로 읽으면 학습에 방해가 되기 때문이다.
        마커가 없으면(또는 짝이 안 맞으면) 통째로 기본 언어로 나간다(설계 폴백).
        """
        segments = split_by_language(
            sentence, settings.CASCADE_TTS_LANGUAGE, settings.CASCADE_TTS_TARGET_LANGUAGE
        )
        # ⭐ **마커가 실제로 걸렸는지**를 로그로 남긴다. 이게 없으면 실험이 성립하지 않는다 —
        #   폴백이 조용해서(마커를 안 써도 통째 재생돼 소리는 정상) "끊김이 줄었다"는 판단이
        #   '마커가 걸린 상태'에서 나온 건지 '안 걸린 상태'에서 나온 건지 못 가른다.
        #   ⛔ 대사 원문은 찍지 않는다(통화 내용이 로그에 남는다). 구간 수·언어·마커 상태면 된다.
        marker_state = _marker_state(sentence)
        self._marker_seen[marker_state] = self._marker_seen.get(marker_state, 0) + 1
        logger.info(
            "cascade 언어구간: %d개 %s 마커=%s",
            len(segments), "/".join(lang for _, lang in segments) or "-", marker_state,
        )
        if len(segments) <= 1:
            return await self._speak_one(strip_markers(sentence).strip(),
                                         settings.CASCADE_TTS_LANGUAGE)
        sent = 0
        for text, language in segments:
            sent += await self._speak_one(text, language)
        return sent

    async def _speak_one(self, sentence: str, language: str) -> int:
        """구간 하나를 합성해 송출하고, **API 에 넘긴 문자 수**를 원가에 기록한다."""
        if not sentence:
            return 0
        # ⭐ 문자는 **API 에 넘기는 시점**에 센다. 과금은 우리가 텍스트를 보낸 순간 일어나므로,
        #   barge-in 으로 이 문장이 중간에 끊겨도(= 아래 await 가 취소돼 돌아오지 않아도)
        #   그 문장 값은 이미 나갔다. 다 나온 뒤에 세면 끊긴 문장이 통째로 장부에서 사라진다.
        self.usage.record_tts(sentence, vendor=self._tts_vendor())
        report: dict = {}
        # ⭐ 이 통화에서 이미 429 를 맞았으면 **Gemini 를 다시 찌르지 않는다**(세션 단위 백오프).
        #   실측: 한도가 분당 10회인데 수요가 평균 19.2 / 피크 27 이었다. 소진된 상태에서
        #   문장마다 찔러봐야 **실패해도 요청은 나가고**(회복이 늦어진다) 첫소리만 늘어난다.
        #   ⛔ 프로세스 전역으로 고정하면 쿼터가 회복돼도 영영 Chirp 이다 — 세션 단위여야 한다.
        allow_gemini = not self._tts_gemini_off
        if allow_gemini and self._tts_vendor() != tts.CHIRP3_ENGINE:
            # 백오프 전까지의 Gemini 호출 수 — ①(요청 수 줄이기)의 효과를 재는 유일한 값이다
            # (백오프가 호출 자체를 막으므로 tts_calls 로는 못 잰다).
            self._tts_gemini_calls += 1
        stream = await tts.synthesize_stream(
            sentence,
            language=language,
            voice=settings.CASCADE_TTS_VOICE,
            report=report,
            allow_gemini=allow_gemini,
            engine=self._tts_engine or None,
            speaking_rate=self._tts_rate,
            style_prompt=self._tts_style,
        )
        sent = await speak_stream(self.beaver, stream, sentence)
        # ⭐ 내보낸 오디오 초 — Gemini-TTS 단가의 기준(문자가 아니라 출력 오디오 토큰이다).
        self.usage.record_tts_audio(sent)
        engine = report.get("engine")
        if engine:
            self._tts_engines.add(engine)
        if report.get("quota") and not self._tts_gemini_off:
            # ⛔ 엔진이 통화 중간에 바뀐 사실을 반드시 남긴다 — A/B 판정이 이 줄에 걸린다.
            self._tts_gemini_off = True
            logger.warning(
                "cascade tts 엔진 고정: gemini 호출 %d회 만에 429(분당 쿼터) → 이 통화 동안 %s "
                "로 고정한다(다음 통화는 다시 gemini 로 시작)",
                self._tts_gemini_calls, tts.CHIRP3_ENGINE,
            )
        if report.get("fallback_from"):
            # 폴백은 A/B 비교를 오염시킨다 — 이 통화의 '첫소리'가 어느 엔진 것인지 흐려진다.
            # 로그 한 줄로 드러내고, 원가는 **실제로 소리를 낸 엔진** 이름으로 남긴다.
            self.usage.record_tts("", vendor=report["fallback_from"], failed=True)
            if engine:
                self.usage.record_tts(sentence, vendor=engine)
        if not sent:
            # 오디오가 한 조각도 안 나왔다 = 합성 실패. 건수만 따로 센다(문자는 위에서 이미).
            self.usage.record_tts("", vendor=self._tts_vendor(), failed=True)
        return sent

    def _tts_vendor(self) -> str:
        """원가 벤더 문자열 = **의도한 엔진**. 실제로 다른 엔진이 냈으면 위에서 보정한다."""
        if self._tts_engine == tts.GEMINI_ENGINE:
            return (settings.CASCADE_TTS_GEMINI_MODEL or tts.GEMINI_ENGINE).strip()
        return tts.CHIRP3_ENGINE

    def _remember_beaver(self, turn_id: str | None, generated: str) -> None:
        """이력에는 **실제로 들린 데까지**만 남긴다(설계 §5).

        끊기지 않은 턴은 생성 전체가 곧 들린 말이다. 끊긴 턴은 원장이 답을 안다.
        """
        if turn_id is None or not generated.strip():
            return
        self._history.append({"role": "model", "text": generated.strip()})
        self._trim_history()

    def _on_reply_cancelled(self, turn_id: str | None, generated: str) -> None:
        """barge-in 으로 끊긴 대답 — 들린 데까지만 이력에 남기고, 못 들려준 문자를 원가에 센다."""
        spoken = ""
        if turn_id is not None:
            spoken = self.beaver.spoken_text(
                turn_id, self.beaver.estimated_played_bytes(turn_id)
            ) or ""
        if spoken:
            self._history.append({"role": "model", "text": f"{spoken} …(사용자가 끼어들어 끊김)"})
            self._trim_history()
        unheard = max(0, len(generated.strip()) - len(spoken))
        if unheard:
            self.usage.record_tts_unheard(unheard)
        # ⭐ 못 들려준 나머지를 들고 있는다 — 사용자가 결국 아무 말도 안 하면(빈 턴) 이어서
        #   말한다. 침묵으로 끝내지 않기 위한 것이고, **LLM 은 다시 부르지 않는다**(빈 입력
        #   호출 금지 판단은 그대로다 — 이미 만든 말을 소리로만 다시 낸다).
        remaining = self._remaining_after(generated.strip(), spoken)
        self._interrupted = (
            {"text": remaining, "at": time.monotonic()} if remaining else None
        )
        logger.info(
            "cascade 대답 취소됨: turn=%s 들린글자=%d 못들려준글자=%d",
            turn_id, len(spoken), unheard,
        )

    @staticmethod
    def _remaining_after(generated: str, spoken: str) -> str:
        """생성문 중 **아직 안 들려준 부분**. 어디까지 들렸는지 모르면 되살리지 않는다.

        되풀이가 침묵보다 나쁠 수 있어서(이미 들은 말을 또 하면 대화가 이상해진다) 모르면
        빈 문자열을 돌려준다 — 안전한 쪽은 '안 하는 것'이다.
        """
        if not generated:
            return ""
        if not spoken:
            return generated          # 한 글자도 못 들었다 → 통째로 다시
        tail = spoken.strip().split()[-1] if spoken.strip() else ""
        idx = generated.rfind(tail) if tail else -1
        if idx < 0:
            return ""
        return generated[idx + len(tail):].strip()

    def _resume_interrupted(self) -> bool:
        """빈 턴 뒤의 침묵을 막는다 — 끊겼던 대답의 나머지를 이어서 말한다(LLM 호출 0).

        되살리기는 **한 번만**이고 유예(CASCADE_RESUME_WINDOW_MS) 안에서만이다. 그 사이
        사용자가 실제로 말했으면 되살리지 않는다 — 그건 대화가 진행된 것이다.
        """
        pending, self._interrupted = self._interrupted, None
        if not pending or not self._reply_enabled():
            return False
        age_ms = (time.monotonic() - pending["at"]) * 1000.0
        if age_ms > max(0, settings.CASCADE_RESUME_WINDOW_MS):
            return False
        if self._reply_task is not None and not self._reply_task.done():
            return False
        logger.info("cascade 대답 이어가기: %d자(사용자가 결국 말하지 않았다 — 침묵 방지)",
                    len(pending["text"]))
        self.state = TurnState.THINKING
        self._reply_task = self._tg.create_task(self._run_resume(pending["text"]))
        return True

    async def _run_resume(self, text: str) -> None:
        """끊겼던 말의 나머지를 그대로 소리로 낸다. ⛔ LLM 호출 없음(새 입력이 없으니 새 말도 없다)."""
        self._reply_cancelled = False
        buffer = SentenceBuffer()
        turn_id: str | None = None
        try:
            for sentence in buffer.push(text) + [buffer.flush()]:
                if not sentence:
                    continue
                turn_id = turn_id or await self._begin_beaver_turn()
                await self._speak(sentence)
            if turn_id is not None:
                await self.beaver.end()
            self._remember_beaver(turn_id, text)
        except asyncio.CancelledError:
            if not self._reply_cancelled:
                raise
            self._on_reply_cancelled(turn_id, text)
        except InvariantError:
            logger.info("cascade 이어가기 중단(턴이 이미 닫힘) turn=%s", turn_id)
        finally:
            if self.state in (TurnState.THINKING, TurnState.BEAVER_SPEAKING):
                self.state = TurnState.IDLE

    def _trim_history(self) -> None:
        """⭐ 이력은 **글자 수**로 막는다 — 턴 수가 아니라.

        예전엔 12턴 상한이었는데 두 가지로 틀렸다. ①어제 Live 15분 통화가 72턴이었다 —
        12턴이면 2~3분치라 15분 데모에서 비버가 앞부분을 통째로 잊는다. ②턴 수는 애초에
        잘못된 단위다. 긴 발화 몇 개가 짧은 턴 12개보다 훨씬 크다(과금은 글자·토큰으로 된다).

        상한은 **정상 통화가 절대 안 걸리게** 잡았다(15분 정상 통화의 몇 배). 이건 정책이
        아니라 병적으로 긴 통화를 막는 백스톱이다.

        ⛔ 잘라낸 내용을 그냥 버리지 않는다. Live 는 압축으로 날아간 대화를 **되살릴 수
        없었다**(오디오가 구글 서버 안에서 사라진다). 캐스케이드는 우리가 우리 손으로
        자르는 것이라 그 텍스트가 우리에게 남아 있다 — 나중에 요약해서 되먹일 수 있게
        `_history_dropped` 에 들고 있는다. 그리고 **버린 사실을 로그로 남긴다**: 조용히
        버리면 "비버가 왜 앞을 까먹지"를 아무도 못 찾는다.
        """
        limit = max(1000, settings.CASCADE_HISTORY_MAX_CHARS)
        total = sum(len(item.get("text") or "") for item in self._history)
        if total <= limit:
            return
        dropped_chars = 0
        dropped_turns = 0
        # 가장 오래된 것부터 버린다. 마지막 한 턴은 남긴다(문맥이 통째로 사라지면 안 된다).
        while self._history and len(self._history) > 1 and total > limit:
            item = self._history.pop(0)
            self._history_dropped.append(item)
            dropped_chars += len(item.get("text") or "")
            dropped_turns += 1
            total -= len(item.get("text") or "")
        logger.info(
            "cascade 이력 절단: %d줄 %d자 버림(상한 %d자, 남은 %d줄 %d자) — 버린 내용은 "
            "요약 재주입용으로 보관 중",
            dropped_turns, dropped_chars, limit, len(self._history), total,
        )

    def _system_instruction(self) -> str:
        """페르소나는 **새로 만들지 않는다** — normalcall 과 같은 조립기를 쓴다(설계 §1-2).

        두 엔진이 같은 지시문을 써야 품질 비교가 성립한다. 캐스케이드 데모는 DB 가 없어
        캐릭터·레벨을 못 읽으므로 데모용 기본값으로 채운다(P1 이후 통화 기록에 올릴 때
        normalcall 과 같은 값으로 바꾼다).
        """
        if self._system_cache is None:
            self._system_cache = build_system_instruction(
                role=settings.CASCADE_PERSONA_ROLE,
                personality=settings.CASCADE_PERSONA_PERSONALITY,
                level_profile=settings.CASCADE_PERSONA_LEVEL,
                locale=settings.CASCADE_PERSONA_LOCALE,
                interests=[],
                target_language=settings.CASCADE_TTS_TARGET_LANGUAGE_LABEL,
                # ⭐ 마커 표기 규칙을 켠다(캐스케이드 전용). normalcall 은 기본값 False 라
                #   출력이 바이트 동일하게 유지된다.
                language_marker=True,
            )
        return self._system_cache

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

    def _apply_tts_choice(self, ctrl: dict) -> None:
        """start 에서 온 TTS 선택을 세션 값으로 잡는다(⛔ **dev 데모 한정 편의**).

        화면에서 엔진을 골라 A/B 하기 위한 통로다 — env 를 고치고 새 리비전을 띄우는 왕복
        없이 통화마다 바꿔 들을 수 있다.
        ⚠ 이건 **클라가 서버 기본값을 덮는 구조**다. 앱이 캐스케이드를 타게 되면 이 통로를
          그대로 열어 두면 안 된다 — 엔진마다 단가와 쿼터가 달라 **원가 통제가 클라로 넘어간다.**
        ⚠ 알 수 없는 값은 **거절하고 서버 기본값**을 쓴다(클라가 아무 문자열이나 보내면 안 된다).
        """
        picked = str(ctrl.get("ttsEngine") or ctrl.get("tts_engine") or "").strip()
        source = "서버 기본값"
        if picked:
            if picked in (tts.GEMINI_ENGINE, _CHIRP_CHOICE):
                self._tts_engine, source = picked, "클라 지정"
            else:
                logger.warning("cascade tts 엔진 값 거절: %r — 서버 기본값으로 진행", picked[:40])
        raw_rate = ctrl.get("speakingRate", ctrl.get("speaking_rate"))
        if raw_rate is not None:
            try:
                self._tts_rate = float(raw_rate)
            except (TypeError, ValueError):
                logger.warning("cascade speaking_rate 값 거절: %r", raw_rate)
        style = ctrl.get("stylePrompt", ctrl.get("style_prompt"))
        if isinstance(style, str):
            self._tts_style = style.strip()[:_STYLE_PROMPT_MAX]
        # ⭐ 세션 시작에 한 줄 — 이 통화의 소리가 어느 엔진 것인지 여기서 확정된다.
        logger.info(
            "cascade 엔진 선택: %s (%s) speaking_rate=%s style=%r",
            self._tts_engine or tts.CHIRP3_ENGINE, source,
            self._tts_rate if self._tts_rate is not None else "서버값",
            (self._tts_style if self._tts_style is not None else "서버값")[:40],
        )

    def _apply_aec_hint(self, aec: Any) -> None:
        """start.aec 힌트로 **세션별** barge-in 정책을 정한다.

        AEC 는 기기·라우트마다 다르다(이어폰=사실상 무해 / 스피커폰=최악). 전역 설정 하나로는
        이 차이를 표현할 수 없어서 세션 값으로 둔다. 힌트가 없으면 전역 기본값.

        ⚠ **지금 앱은 이 필드를 보내지 않는다**(2026-08-07 확인). 필드는 프로토콜에 있는데
          클라가 안 실어서, 실사용 세션은 **전부 기본값 `transcript`** 로 간다 — "기기별로 다르게
          잡는다"는 위 설명은 아직 **의도이지 현실이 아니다.** 실패 방향은 안전한 쪽이고
          (이어폰이어도 전사를 기다린다) 비용은 지연뿐이다. 앱이 보내기 시작하면 그때부터
          위 설명대로 돈다. dev 데모(cascade_demo.html)는 보내고 있어 경로 자체는 살아 있다.
        """
        if not isinstance(aec, dict):
            logger.info(
                "cascade aec 힌트 없음 → 전 세션 공통 bargein_confirm=%s (앱이 아직 안 보낸다)",
                self._bargein_confirm,
            )
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


async def run_cascade(websocket: WebSocket, genai_client: Any = None) -> None:
    """WS 캐스케이드 세션 구동(라우터에서 accept 후 위임). 소켓 정리까지 책임.

    genai_client 가 None 이면 비버는 말하지 않고 턴 감지만 돈다(P0 동작 — R5).
    """
    session = CascadeSession(WsCascadeTransport(websocket), genai_client)
    try:
        await session.run()
    finally:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass
