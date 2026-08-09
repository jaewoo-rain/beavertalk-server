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
import itertools
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
from core import elevenlabs_tts, tts
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
    ServerBeaverPreparing,
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
from domains.learning.realtime.protocol import AecHint, ServerError, ServerPong

logger = logging.getLogger(__name__)

_DEFAULT_SAMPLE_RATE = 16000
_EOS = object()  # 큐 종료 센티널
_RMS_STRIDE = 8  # 에너지 계산 표본 간격(전 샘플을 돌 필요 없다 — 게이트용 근사면 충분)
# 유령 턴 판정의 여유 — 같은 발화의 꼬리 전사는 앞 턴의 끝과 사실상 같은 지점을 가리킨다.
# 200ms 는 "새 발화라면 최소 이만큼은 뒤여야 한다"는 하한(사람이 그보다 빨리 새 말을 시작하면
# 어차피 같은 턴으로 이어진다).
_STALE_OFFSET_EPSILON_MS = 200
# barge-in 판정 표본 상한 — 병적 세션에서 메모리가 새지 않게(통화당 수십 건이 정상).
_BARGEIN_OBS_MAX = 500
# 버린 대답이 죽기를 기다리는 상한. 취소는 다음 await 에서 곧바로 걸리므로 보통 수 ms 다 —
# 이 값은 "네트워크 호출 안에서 안 풀리는 병적 케이스"의 바닥이고, 넘으면 그냥 진행한다.
_REPLY_CANCEL_WAIT_S = 0.5
# ⭐ **타이머 조기 발화 허용오차**(2026-08-08 실측: `만료된 데드라인 없이 깨어났다` 6건).
#   데드라인은 `time.monotonic()` 으로 세우는데 대기는 이벤트 루프 타이머가 깨운다. 그 둘은
#   **같은 시계가 아니다** — uvicorn[standard] 는 uvloop 를 쓰고(Dockerfile: uvicorn main:app),
#   uvloop 의 `loop.time()` 은 루프 반복마다 갱신되는 **캐시된 시각**이라 실시간과 어긋난다.
#   그 어긋남이 줄어드는 방향이면 타이머가 monotonic 기준으로 **조금 일찍** 깬다.
#   허용오차 없이 재면 두 가지가 난다:
#     ① 다 된 침묵 타이머를 "만료 안 됨"으로 보고 사유가 뒤바뀐다(`reason=max` 오표기)
#     ② timeout=0 으로 즉시 다시 깨는 **바쁜 대기**(cpu=1 에서는 그냥 손해다)
#   5ms 는 판정에 영향이 없는 폭이고(침묵 임계가 800~1500ms 다), 조기 발화 폭보다 넉넉하다.
_DEADLINE_EPS_S = 0.005
# ⭐ 세션 일련번호 — **로그에 세션 식별자가 없어서** 08-08 u7 을 로그만으로 못 갈랐다.
#   두 세션이 겹쳐 돌면(탭 두 개, 재접속) 두 상태기계의 줄이 한 스트림에 섞이는데, 그걸
#   구분할 방법이 없어 "한 세션의 모순"처럼 보였다. 프로세스 안에서만 유일하면 충분하다
#   (인스턴스가 다르면 Cloud Run 이 resource.labels 로 이미 갈라 준다).
_session_seq = itertools.count(1)
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
# 화면에서 고를 수 있는 엔진 값(서버가 아는 것만 받는다). Gemini 쪽 값은 core.tts 가 소유한다.
_CHIRP_CHOICE = "chirp3-hd"
# ⭐ Gemini 배치 모드 — **실시간을 포기하고** 전체를 합성한 뒤 한 번에 들려준다.
#   Gemini 는 합성 배속이 1.3x 라 실시간을 못 따라가고(실측), 그래서 문장 중간에 끊긴다.
#   끊긴 소리로는 **감정·발음이 좋은지 판단할 수가 없다.** 판정을 위해 지연을 내주는 모드다.
#   ⛔ 프로덕션 방식이 아니다. Chirp 은 지금 방식 그대로 간다.
_GEMINI_BATCH_CHOICE = "gemini-batch"
# ElevenLabs 2종 — 구글이 아니라 별도 어댑터(core/elevenlabs_tts.py)를 탄다.
#   flash: "Ultra-low latency(~75ms†)" 라 실시간용 / v3: 인라인 감정 태그([laughs] 등)가 되지만
#   문서가 실시간엔 flash 를 권한다 — 느리면 배치 경로로 돌린다.
_ELEVEN_FLASH_CHOICE = "elevenlabs-flash"
_ELEVEN_V3_CHOICE = "elevenlabs-v3"
# ⭐ 중간 등급 — 문서상 "Lifelike, consistent quality". 사장님 목적이 **목소리**라서 필요하다:
#   빠른 모델(flash)은 표현력을 깎아서 빠르므로 "AI 티"가 그대로 남을 수 있다.
_ELEVEN_MULTI_CHOICE = "elevenlabs-multilingual"
_ELEVEN_CHOICES = (_ELEVEN_FLASH_CHOICE, _ELEVEN_MULTI_CHOICE, _ELEVEN_V3_CHOICE)
_ELEVEN_MODEL_BY_CHOICE = {
    _ELEVEN_FLASH_CHOICE: elevenlabs_tts.FLASH_MODEL,
    _ELEVEN_MULTI_CHOICE: elevenlabs_tts.MULTILINGUAL_MODEL,
    _ELEVEN_V3_CHOICE: elevenlabs_tts.V3_MODEL,
}
_STYLE_PROMPT_MAX = 200     # 스타일 문구 상한 — 길어지면 지연 비교가 오염된다
# (TTS 벤더 이름은 core.tts 가 소유한다 — 엔진 A/B 로 값이 바뀌므로 _tts_vendor() 로 읽는다)


def _looks_unfinished(text: str) -> bool:
    """이 발화가 **문장 중간에 잘린 것으로 보이나** — ⛔ 관측 전용이다.

    사장님 아이디어("작고 빠른 LLM 으로 문장이 끝났는지 확인")는 오바가 아니라 업계의 smart
    turn detection 이다. 다만 **큰 구멍(전사 기준 종료)을 먼저 막고, 실제로 얼마나 자주
    잘리는지 세어 본 뒤** 붙일지 정한다. 그 카운트를 위한 값이라 **판정에는 절대 쓰지 않는다.**

    한국어는 조사·연결어미로 끝나면 미완 신호다("제가", "그런데", "~고", "~는데", "~서").
    ⚠ 규칙 기반이라 완벽하지 않다 — 그래서 동작을 바꾸지 않고 **세기만** 한다.
    """
    tail = (text or "").strip().rstrip("\"'’”)]}")
    if not tail:
        return False
    if tail[-1] in "?!.。！？…":
        return False                      # 종결부호가 있으면 끝난 문장으로 본다
    last = tail.split()[-1] if tail.split() else tail
    return any(last.endswith(suffix) for suffix in _UNFINISHED_TAILS)


# 조사·연결어미(끝나면 뒤에 말이 더 온다는 신호). ⚠ 관측 전용 목록이다.
_UNFINISHED_TAILS = (
    "은", "는", "이", "가", "을", "를", "에", "에서", "으로", "로", "와", "과", "랑", "의",
    "고", "며", "면서", "는데", "은데", "ㄴ데", "서", "니까", "지만", "거나", "든지",
    "그리고", "그런데", "그래서", "하지만", "왜냐하면",
)


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
    # 이 턴에서 **첫 오디오 바이트가 실제로 나간 시각**(monotonic). 음수면 아직 안 나갔다.
    # ⛔ `started_at` 과 다르다 — 그건 합성을 시작한 시각이라 벤더 대기가 통째로 들어 있다.
    # ⚠ 기본값이 0.0 이면 안 된다 — 0 은 falsy 라 "아직 안 나갔다"와 구분이 안 된다.
    first_audio_at: float = -1.0
    cancelled: bool = False
    epoch: int = 0
    ledger: list[SpokenChunk] = field(default_factory=list)


class _ReplyTiming:
    """첫소리 분해 — **어디서 늦는지**를 로그 한 줄로 가른다(2026-08-08).

    첫소리가 06:07 ~3.1초 → 07:03 ~2.7초 → 08:12 ~5.9초로 2배가 됐는데, 한 숫자로 뭉쳐
    있어서 원인을 못 짚었다. 타임스탬프 역산은 변수가 셋인데 방정식이 하나라 신뢰할 수 없다
    ("여러 지표가 같은 방향을 가리켜도 원인이 확인된 게 아니다").

        첫소리 = ①LLM 첫 조각까지 + ②첫 문장이 완성될 때까지 + ③TTS 첫 바이트까지
    ⚠ **대기열 대기는 첫소리 밖이다**(`began` 이 _run_reply 안에서 찍힌다). 사용자가 체감하는
      지연은 둘의 합이라 따로 싣는다.
    ⛔ '출력 대기'는 항목에 없다 — 페이서는 **그 비버 턴의 시작 시각** 기준으로 재우므로
      (`_pace`: elapsed = now − _cur.started_at) 새 턴의 첫 조각은 절대 안 기다린다.
      앞 대답이 흐르는 동안은 애초에 `_run_reply` 가 시작되지 않는다(대기열).
    """

    __slots__ = ("began", "queued_ms", "chunk_at", "sentence_at", "audio_at",
                 "batch_at", "vendor_ms", "batch_audio_ms")

    def __init__(self, began: float, queued_ms: int = 0) -> None:
        self.began = began
        self.queued_ms = max(0, queued_ms)
        self.chunk_at = 0.0
        self.sentence_at = 0.0
        self.audio_at = 0.0        # 첫 바이트가 **클라로 나간** 시각(사용자가 듣기 시작한 때)
        self.batch_at = 0.0        # 첫 배치가 **전량** 나간 시각(페이서가 실시간으로 흘린다)
        self.vendor_ms = -1        # 벤더가 첫 오디오를 주기까지(report["ttfb_ms"])
        self.batch_audio_ms = 0    # 첫 배치의 오디오 길이

    def mark_chunk(self) -> None:
        self.chunk_at = self.chunk_at or time.monotonic()

    def mark_sentence(self) -> None:
        self.mark_chunk()      # 첫 문장이 먼저 보이는 구현이어도 순서가 뒤집히지 않게
        self.sentence_at = self.sentence_at or time.monotonic()

    def mark_audio(self, at: float = 0.0) -> None:
        """첫 바이트가 나간 시각. 인자로 **원장이 기록한 실제 시각**을 받는다."""
        self.mark_sentence()
        self.audio_at = self.audio_at or at or time.monotonic()

    def mark_batch(self, audio_ms: int = 0) -> None:
        """첫 배치 전량 송출 완료 — 여기까지가 예전 `TTS` 항목이었다."""
        if not self.batch_at:
            self.batch_at = time.monotonic()
            self.batch_audio_ms = max(0, audio_ms)

    @property
    def first_sound_ms(self) -> int:
        return int((self.audio_at - self.began) * 1000) if self.audio_at else -1

    def summary(self) -> str:
        """⚠ **2026-08-09 부터 `첫소리` 의 뜻이 바뀌었다** — 예전 값과 직접 비교하지 마라.

        예전: 첫 배치가 **전량 송출**될 때까지. 그 안에 페이서(실시간 송출)가 통째로 들어 있어
              "이미 소리가 나가고 있는 시간"을 지연으로 세고 있었다.
        지금: **첫 바이트가 클라로 나간 시각**까지 = 사용자가 기다리는 시간.
        값이 뚝 떨어지는데 **코드가 빨라진 게 아니다. 재던 구간이 달랐다.**
        `첫배치` 는 그 뒤에 따로 싣는다(페이서가 얼마를 먹었는지 보이게).
        """
        if not self.audio_at:
            return "첫소리=없음(소리가 한 조각도 안 나갔다)"
        ms = lambda a, b: int(max(0.0, a - b) * 1000)  # noqa: E731
        vendor = ms(self.audio_at, self.sentence_at) if self.vendor_ms < 0 else self.vendor_ms
        line = (
            "첫소리=%dms(대기열 %d + LLM첫조각 %d + 문장완성 %d + 벤더 %d + 송출 %d)"
            % (self.first_sound_ms, self.queued_ms,
               ms(self.chunk_at, self.began),
               ms(self.sentence_at, self.chunk_at),
               vendor,
               max(0, ms(self.audio_at, self.sentence_at) - max(0, vendor)))
        )
        if self.batch_at:
            line += " 첫배치=%dms(오디오 %dms 페이서 %dms)" % (
                ms(self.batch_at, self.began), self.batch_audio_ms,
                ms(self.batch_at, self.audio_at),
            )
        return line


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
        # 세션이 엔진에 맞춰 덮어쓴다(None 이면 전역 설정). 엔진마다 합성이 뒤처지는 폭이 달라
        # 상수 하나로 쓰면 한쪽은 끊기고 한쪽은 버퍼가 부푼다.
        self.lead_ms: int | None = None
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

    @property
    def first_audio_at(self) -> float:
        """이 턴의 첫 오디오가 나간 시각(0 = 아직). '첫소리' 계측의 기준점이다."""
        if self._cur is None or self._cur.first_audio_at < 0:
            return 0.0
        return self._cur.first_audio_at

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
        if self._cur.first_audio_at < 0:
            self._cur.first_audio_at = self._now()
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

        허용 선행 = CASCADE_TTS_LEAD_MS(기본) 또는 **세션이 엔진에 맞춰 지정한 값**.
        이걸 넘겨 밀어내면 클라 버퍼가 부풀고, 반대로 **너무 작으면 언더런이 난다** —
        합성이 재생보다 뒤처지는 폭이 엔진마다 다르기 때문이다(Gemini 최대 1.5초).
        """
        if self._cur is None:
            return
        lead_ms = max(0, self.lead_ms if self.lead_ms is not None else settings.CASCADE_TTS_LEAD_MS)
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
        self._batch_synthesizing = False   # 배치 합성 중(소리가 아직 안 나갔다)
        self._batch_synth_s: float | None = None   # 배치 합성 소요(대답 줄의 합성배속 재료)
        self._marker_seen: dict[str, int] = {}   # 언어 마커 상태별 문장 수(실험 성립 판정)
        # 대답별 **읽기 속도 실측**용. (언어, 들린 글자, 오디오 바이트) — 소리가 실제로 나간 것만.
        # ⛔ 원가용 tts_chars 와 다르다: 그건 'API 에 넘긴 글자'(끊겨도 돈은 나간다)라
        #   분모(오디오)와 모집단이 어긋나 **읽기 속도로 쓰면 28자/초 같은 값이 나온다.**
        self._reply_spans: list[tuple[str, int, int]] = []
        # 429 백오프는 **세션 단위**다(프로세스 전역이면 쿼터가 회복돼도 영영 Chirp 이다).
        self._tts_gemini_off = False
        self._tts_gemini_calls = 0
        self._tts_ttfb_ms = -1
        # barge-in 보류 상태(전사 확인 대기 마감 시각) / 끊겨서 못 들려준 대답
        self._bargein_at: float | None = None
        self._interrupted: dict | None = None
        # 비버가 말하는 동안 들어온 발화(대답이 끝나면 답한다) / 지금 비버가 하는 말(에코 판정)
        self._pending_user_text = ""
        self._pending_since = 0.0     # 대기열에 들어간 시각
        self._reply_queued_ms = 0     # 이번 대답이 대기열에서 기다린 시간(첫소리 분해용)
        self._rms_log: deque[tuple[float, float]] = deque()
        self.state = TurnState.IDLE
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._t0 = time.monotonic()
        self._sid = "s%d" % next(_session_seq)
        self._sample_rate = _DEFAULT_SAMPLE_RATE
        self._audio_ms = 0.0        # 클라에서 받아 STT 로 흘린 오디오 총량(오디오 타임라인)
        # 턴 누적
        self._turn_seq = 0
        self._turn_id: str | None = None
        self._turn_began_at = 0.0
        # ⭐ **이 턴이 열린 순간** 비버가 안 들리고 있었나. 판정 시점을 '말을 시작한 때'로
        #   옮기기 위한 값이다(2026-08-08). 자세한 이유는 _open_turn·_start_reply 주석.
        self._turn_beaver_unheard = False
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
        # ⭐ 에너지 관문을 이 세션에서 **돌릴 것인가**(AEC 선언이면 끈다 — _apply_aec_hint).
        #   기본은 켬 = 안전 쪽. start 를 못 받은 세션도 여기에 떨어진다.
        self._energy_gate = True
        # barge-in 판정 계측(관측 전용) — 보류 시각·그때 에너지 / 판정별 에너지 표본.
        self._bargein_pending_at = 0.0
        self._bargein_pending_rms = 0.0
        self._bargein_obs: list[tuple[str, float]] = []
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
            # start 없이 오디오부터 온 세션도 **어느 체제로 도는지**는 로그에 남아야 한다.
            self._apply_aec_hint(None)
            pending_audio = first.audio
        self._sample_rate = sample_rate

        stream = stt_mod.make_stt_v2_stream(sample_rate, self._stt_language_codes())
        # ⭐ **무슨 언어로 듣고 있는지 로그만 보고 알 수 있어야 한다**(2026-08-08). 지금까지는
        #   코드를 읽고 env 를 조회해야 알 수 있었고, 그래서 "영어가 안 들린다"를 실통화 5건이
        #   빈 턴으로 닫힌 뒤에야 찾았다.
        logger.info(
            "cascade stt: %s 인식언어=%s 모델=%s 위치=%s",
            self._sid, getattr(stream, "language_codes", None) or self._stt_language_codes(),
            settings.STT_V2_MODEL, settings.STT_V2_LOCATION,
        )
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
                    await self._start_reply(seed_opening(), is_greeting=True)
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
            self._log_bargein_summary()

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
                # ⭐ **어느 데드라인이 깨웠는지 명시적으로 고른다**(2026-08-08).
                #   예전엔 "barge-in 이 아니면 턴 종료"로 흘리고, 사유는 `_close_at` 을 다시
                #   재서 붙였다. 그러면 **아무것도 만료되지 않은 깨어남**이 `reason=max` 로
                #   둔갑해 턴을 조용히 죽인다. 만료된 게 없으면 **닫지 않고 다시 기다린다** —
                #   데드라인은 그대로라 곧 다시 깬다(무한 스핀도 없다: 미래 데드라인이면
                #   timeout 이 양수로 다시 잡힌다).
                woke = time.monotonic()
                # 보류 중이던 barge-in 의 지속 시간이 찼다 — STT 가 먹통일 때만 오는 자리다
                # (정상 경로는 전사가 항상 먼저 이긴다: 실측 476~620ms vs 안전망 3.5초).
                if self._bargein_at is not None and woke >= self._bargein_at - _DEADLINE_EPS_S:
                    if self._speech_active:
                        await self._confirm_bargein(
                            None, "안전망 — STT 무응답 %dms" % settings.CASCADE_BARGEIN_SUSTAIN_MS
                        )
                    else:
                        self._bargein_at = None
                        self._note_bargein("보류만료", self._bargein_pending_rms)
                        logger.info(
                            "cascade barge-in 기각 — 전사도 지속도 없었다(잡음 추정, rms=%.4f)",
                            self._bargein_pending_rms,
                        )
                    continue
                # 침묵 타이머 만료 = 턴 종료. **이 판정이 캐스케이드의 심장이다.**
                if self._close_at is not None and woke >= self._close_at - _DEADLINE_EPS_S:
                    await self._close_turn("silence")
                    continue
                if self._turn_deadline is not None and woke >= self._turn_deadline - _DEADLINE_EPS_S:
                    await self._close_turn("max")
                    continue
                # 허용오차를 넘어 일찍 깼다 = 우리가 모르는 일이다. **닫지 않고** 조금 쉬었다
                # 다시 잰다 — 여기서 곧장 continue 하면 timeout=0 으로 스핀이 된다(cpu=1).
                await asyncio.sleep(_DEADLINE_EPS_S)
                logger.warning(
                    "cascade ⚠ 만료된 데드라인 없이 깨어났다 — 턴을 닫지 않는다"
                    "(close=%s turn=%s barge=%s 남은=%.3f/%.3f/%.3f초)",
                    self._close_at is not None, self._turn_deadline is not None,
                    self._bargein_at is not None,
                    (self._close_at or woke) - woke, (self._turn_deadline or woke) - woke,
                    (self._bargein_at or woke) - woke,
                )
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
            # ⚠ 글자가 이미 나온 턴에서는 **취소하지 않는다.** 잡음이 speech_begin 을 계속
            #   만들어 내면 전사 기준 카운트다운이 매번 취소돼 위 규칙이 무력해진다.
            #   진짜로 말을 이어가면 **새 전사가 도착해 다시 건다**(그게 정상 경로다).
            if not self._turn_has_text():
                self._close_at = None  # 아직 글자 없음 = 진짜 발화 시작을 기다리는 중
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
        # ⭐ **판정 축을 음향에서 전사로 옮긴다**(2026-08-08, 사장님 지적).
        #   지금까지는 VAD 가 조용해져야 카운트다운을 걸었다. 그런데 차·카페·에어컨에서는
        #   VAD 가 **영영 조용해지지 않는다** — 잡음은 소리는 내지만 단어를 못 만든다.
        #   그러면 턴 상한(CASCADE_TURN_MAX_S)까지 열려 있어 "안녕하세요" 한 마디에 수십 초를
        #   기다리게 된다.
        # ⚠ 그냥 뒤집으면 **말 시작 전에 턴이 닫힌다**(숨 고르는 동안엔 아직 글자가 없다).
        #   그래서 조건을 건다:
        #     글자가 아직 없다  → 예전대로 VAD 기준(엔진이 "말하는 중"이면 안 닫는다)
        #     글자가 나온 뒤부터 → **전사 정지 기준**(VAD 가 활성이어도 닫는다)
        #   = "말을 시작한 뒤부터는 잡음이 무의미해진다".
        if not self._speech_active:
            self._arm_close_timer(event)
        elif self._turn_has_text():
            self._arm_close_timer(
                event, silence_ms=settings.CASCADE_TURN_TRANSCRIPT_SILENCE_MS
            )

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

    def _turn_has_text(self) -> bool:
        """이 턴에서 **글자가 한 번이라도 나왔나** — 전사 기준 판정의 전제다."""
        return bool(self._finals) or bool(self._partial.strip())

    def _arm_close_timer(self, event: SttV2Event, silence_ms: int | None = None) -> None:
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
        threshold_ms = self._silence_ms if silence_ms is None else max(0, silence_ms)
        remain_s = max(0.0, (threshold_ms - already_ms) / 1000.0)
        floor_s = max(0, settings.CASCADE_TURN_MIN_WAIT_MS) / 1000.0
        self._close_at = time.monotonic() + max(remain_s, floor_s)

    async def _open_turn(self, at: float) -> None:
        self._turn_seq += 1
        self._turn_id = f"u{self._turn_seq}"
        # ⭐⭐ **말을 시작한 순간**의 '안 들림'을 여기서 굳힌다(2026-08-08 실통화).
        #   같은 술어를 두 시점에 재면 그 사이에 답이 바뀐다:
        #     말 시작(비버 아직 무음) → barge-in 기각("대답을 살린다")   ← 너무 일러서 못 끊고
        #     턴 닫힘(그새 소리가 남) → 버리기 조건 불성립 → 대기열      ← 이미 늦어서 못 버린다
        #   그 사이 간격이 **일상적으로 열린다**: 말 시작→턴 닫힘 = 침묵 800ms + 파이프라인
        #   지연 ~900ms ≈ 1.7~2초인데, 대답 첫소리는 ~3초다.
        #   ⛔ 새 술어를 만들지 않는다 — 두 곳이 갈리면 또 구멍이 생긴다. 같은 `_beaver_unheard()`
        #   를 **더 이른 시점에** 한 번 재서 그 턴에 붙여 둔다.
        self._turn_beaver_unheard = self._beaver_unheard()
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
        # ⭐ `열림`·`마지막음성`을 같이 남긴다(2026-08-08). u7(열린 지 얼마 안 된 턴이
        #   reason=max 로 닫혔다) 같은 사건을 **로그만으로** 가르려면 이 둘이 있어야 한다 —
        #   지금까지 턴이 언제 열렸는지가 로그에 없어 사후 재구성이 불가능했다.
        logger.info(
            "cascade turn: %s/%s reason=%s speech_ms=%d silence_ms=%d pipeline_lag_ms=%d "
            "열림=%.1f초전 마지막음성=%.1f초전 미완=%s text=%r",
            self._sid, self._turn_id, reason, speech_ms, self._silence_ms, self._pipeline_lag_ms,
            max(0.0, now - self._turn_began_at),
            max(0.0, now - self._last_voice_at) if self._last_voice_at else -1.0,
            "yes" if _looks_unfinished(text) else "no", text,
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
            await self._start_reply(text)
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
        # 판정 계측용 에너지 — **관문을 돌리든 말든** 값은 항상 걷는다(관측 전용).
        rms = self._rms_at(event.offset_ms)
        audible_ms = self._audible_ms()
        min_audible = max(0, settings.CASCADE_BARGEIN_MIN_AUDIBLE_MS)
        if audible_ms < min_audible:
            # ⭐ 배치 모드에서는 이 상태가 **20초 넘게 지속된다**(전체를 합성한 뒤에야 소리가
            #   난다). 거기서 "여보세요?" 한 번에 21초치 합성이 날아가면 그 모드의 목적
            #   (끊김 없이 소리를 들어보기)이 배반된다 — 같은 관문이 그대로 막아 준다.
            #   ⛔ 발화를 버리는 게 아니다: 취소만 안 하고, 그 말은 대답이 끝난 뒤 답한다
            #   (_pending_user_text 대기열).
            if self._batch_synthesizing:
                logger.info(
                    "cascade barge-in 무시 — 배치 합성 중(아직 소리가 안 나갔다). "
                    "이 발화는 대답이 끝난 뒤 답한다"
                )
            else:
                logger.info(
                    "cascade barge-in 기각 — 비버가 아직 안 들린다(들린 %dms < %dms). 대답을 살린다",
                    audible_ms, min_audible,
                )
            self._note_bargein("기각-안들림", rms)
            return False
        # ⓪ 마이크 상시 개방이 꺼져 있으면 barge-in 을 시도하지 않는다.
        #   그 모드에서는 클라가 비버 발화 중 마이크를 닫으므로, 이때 들어오는 음성 활동은
        #   **에코이거나 게이팅 타이밍 결함**일 가능성이 높다(실측: call 855 에서 유저 턴의
        #   절반이 비버 대사였다). 서버는 두 모드를 모두 견뎌야 하고, OFF 에서는 견디는 방법이
        #   "끼어들지 않는 것"이다.
        if not settings.CASCADE_MIC_ALWAYS_OPEN:
            logger.info("cascade barge-in 기각 — 마이크 상시개방 OFF(에코/게이팅 잔여 추정)")
            self._note_bargein("기각-마이크닫힘", rms)
            return False
        # ① 에너지 임계 — **에코 2차 방어 전용**이다(2026-08-08 역할 재정의).
        #   ⭐ 여기서 묻는 건 "사용자가 말했나"가 아니다(그건 전사가 답한다). "이게 비버 자기
        #   목소리인가"다. 그래서 AEC 를 선언한 세션에서는 **아예 돌리지 않는다** — 막을 대상이
        #   없는 관문이고(재생 중 전사 0건으로 실증), 돌리면 잔여 에코가 아니라 **진짜 발화**만
        #   걸린다(08-08 오전 기각 17건).
        #   ⛔ **'지금'이 아니라 '그 이벤트가 가리키는 오디오'의 에너지를 본다.**
        threshold = settings.CASCADE_BARGEIN_RMS
        if self._energy_gate and threshold > 0:
            if rms < threshold:
                logger.info(
                    "cascade barge-in 기각 — 에너지 %.4f < 임계 %.4f(에코 추정, offset=%d)",
                    rms, threshold, event.offset_ms,
                )
                self._note_bargein("기각-에너지", rms)
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
                self._note_bargein("기각-지속", rms)
                return False
        # ③ 전사 확인 — **설계에 있다고 적어 두고 P1 에서 안 붙였던 관문**이다(2026-08-07).
        #   그동안 barge-in 은 에너지+지속만으로 발동했고, 기침·키보드·숨소리가 전부 통과했다.
        #   여기서 True 를 돌려주면 즉시 취소되므로, transcript 모드는 **판정을 미룬다**:
        #   전사가 오거나(_on_transcript) 음성이 길게 이어지면(_pump_turn 타이머) 그때 친다.
        if self._bargein_confirm == "transcript":
            self._bargein_pending_at = time.monotonic()
            self._bargein_pending_rms = rms
            self._bargein_at = (
                self._bargein_pending_at + max(0, settings.CASCADE_BARGEIN_SUSTAIN_MS) / 1000.0
            )
            logger.info(
                "cascade barge-in 보류 — %s 전사 확인 대기(rms=%.4f 게이트=%s offset=%d, "
                "잡음이면 여기서 끝난다)",
                self._sid, rms, "on" if self._energy_gate else "off", event.offset_ms,
            )
            return False
        # ⛔ 여기(=immediate)는 **글자 없이 소리만으로 끊는 경로**다 — 사장님 규칙 위반이라
        #   aec 힌트로는 절대 선택되지 않는다(env 로 강제한 경우만 온다. _apply_aec_hint 가 경고).
        self._note_bargein("확정-즉시", rms)
        return True

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

    # ── barge-in 판정 계측(관측 전용) ──
    def _note_bargein(self, outcome: str, rms: float) -> None:
        """판정 1건의 (결과, 그때 에너지)를 모은다. ⛔ 동작에 쓰지 않는다."""
        if len(self._bargein_obs) < _BARGEIN_OBS_MAX:
            self._bargein_obs.append((outcome, rms))

    def _log_bargein_summary(self) -> None:
        """통화당 **한 줄** — barge-in 판정 에너지 분포.

        ⭐ 분류를 **에너지가 아니라 결과**로 한다. 임계로 갈라낸 표본으로 임계를 정하면
          순환논법이다 — 실제로 그렇게 모은 표본이 "발화 최저 0.0110" 을 만들었는데, 그건
          **임계를 못 넘어 기각된 것들만** 모인 값이라 진짜 하단이 아니었다.
          여기서는 `전사확정`(= 글자가 나왔다 = 진짜 발화)과 `보류만료`(= 글자가 끝내 안 나왔다
          = 잡음)를 **전사로** 가른다. 그러면 게이트를 꺼도 표본이 안 잘린다.
        ⛔ 프레임마다 찍지 않는다(통화당 판정이 수십 건이면 사람이 못 읽는다). 판정 시점만
          모아 세션 끝에 한 줄로 낸다. R5 — 계측이 통화를 죽이지 않는다.
        """
        try:
            if not self._bargein_obs:
                return
            groups: dict[str, list[float]] = {}
            for outcome, rms in self._bargein_obs:
                groups.setdefault(outcome, []).append(rms)
            parts = []
            for outcome, vals in sorted(groups.items()):
                vals.sort()
                parts.append("%s %d건 %.4f/%.4f/%.4f"
                             % (outcome, len(vals), vals[0], vals[len(vals) // 2], vals[-1]))
            logger.info(
                "cascade barge-in 요약(rms min/중앙/max): %s 게이트=%s 임계=%.4f mode=%s | %s",
                self._sid, "on" if self._energy_gate else "off", settings.CASCADE_BARGEIN_RMS,
                self._aec_mode, " | ".join(parts),
            )
        except Exception as exc:  # noqa: BLE001 - 계측 실패로 통화가 죽지 않는다(R5)
            logger.warning("cascade barge-in 요약 실패(무시) — %s", exc)

    async def _confirm_bargein(self, event: SttV2Event | None, reason: str) -> None:
        """보류해 둔 barge-in 을 확정한다(전사가 왔거나 STT 무응답 안전망이 찼다).

        ⭐ 확정 줄에 **보류→확정 ms 와 그때 에너지**를 같이 남긴다(2026-08-08). 없을 때는
          두 줄의 타임스탬프를 사람이 손으로 빼서 "전사가 안전망보다 먼저 이긴다"를 증명해야
          했다. 같은 계산을 매번 손으로 하게 두지 않는다.
        """
        rms = self._bargein_pending_rms
        waited_ms = (
            int(max(0.0, time.monotonic() - self._bargein_pending_at) * 1000)
            if self._bargein_pending_at else -1
        )
        self._bargein_at = None
        self._bargein_pending_at = 0.0
        # ⚠ 상태 검사는 남긴다 — 이건 진입 관문의 중복이 아니라 **끊을 대상이 이미 없는
        #   경우**다(보류 0.5초 사이에 대답이 끝났다). 여기서 치면 끝난 턴에 audio_cancel 이
        #   나가고 state 가 CANCELLING 으로 굳는다.
        if self.state not in (TurnState.BEAVER_SPEAKING, TurnState.THINKING):
            self._note_bargein("확정취소-상태", rms)
            return
        # ⛔ **'안 들림' 재검사는 걷어냈다**(2026-08-08). 같은 판정을 진입부에서 이미 했고,
        #   0.5초 뒤에 한 번 더 재는 것이 08-08 통화에서 확정 3건을 죽였다(확정취소-안들림).
        #   진입 때 들렸으면 지금은 **더 들렸다** — 다시 물을 이유가 없다.
        self._note_bargein("안전망확정" if reason.startswith("안전망") else "전사확정", rms)
        logger.info("cascade barge-in 확정(%s) — %s 보류→확정 %dms rms=%.4f",
                    reason, self._sid, waited_ms, rms)
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

    def _pending_wait_ms(self) -> int:
        """대기열에서 기다린 시간 — **첫소리(첫 소리까지)에는 안 들어가는 구간**이다.

        `began` 은 `_run_reply` 안에서 찍히므로 대기열 대기는 그 밖이다. 사용자가 체감하는
        지연은 둘의 합이라, 따로 세지 않으면 "왜 늦나"를 로그로 못 가른다.
        """
        if not self._pending_since:
            return 0
        return int(max(0.0, time.monotonic() - self._pending_since) * 1000)

    def _beaver_unheard(self) -> bool:
        """준비 중인 대답을 **사용자가 아직 한 조각도 못 들었나.**

        ⛔ barge-in 진입 관문(`_bargein_allowed` ⓪-1)과 **같은 술어**를 쓴다. 그래야
          "안 들려서 안 끊었다"와 "안 들렸으니 버린다"가 어긋날 수 없다.
        """
        return self._audible_ms() < max(0, settings.CASCADE_BARGEIN_MIN_AUDIBLE_MS)

    async def _start_reply(self, user_text: str, is_greeting: bool = False) -> None:
        if not self._reply_enabled() or not user_text.strip():
            return
        if self._reply_task is not None and not self._reply_task.done():
            # ⭐⭐ **아무도 안 들은 대답은 버린다**(2026-08-08 사장님 증상: "음성이 끊겼으면
            #   삭제돼야 하는데 계속 나온다"). THINKING 구간(LLM 생성)에는 오디오가 아예
            #   없어서 `_audible_ms()` 가 항상 0 인데, 실측 첫소리가 3.5~8초라 그동안은
            #   ①barge-in 이 "안 들림"으로 기각되고 ②발화는 대기열로 밀리고 ③비버는 아무도
            #   안 듣는 대답을 끝까지 하고 ④그 뒤에 **낡은 말**에 답했다.
            #   버려서 잃는 것은 0 이다(누구도 못 들었다). 안 버리면 사용자는 이미 지나간
            #   말에 대한 답을 듣는다 — 그게 증상 자체다.
            #   ⭐ 판정은 **말을 시작한 시점** 기준이다(2026-08-08). 그때 안 들렸으면 그 대답은
            #   아무도 못 들은 것이다 — 사용자가 그걸 듣고 반응한 게 아니다. 말하는 도중에
            #   소리가 나기 시작했더라도 마찬가지다(이미 말하고 있었으면 그 대답을 원한 게 아니다).
            #   닫힘 시점 검사도 함께 둔다(OR): 그새 **다른** 대답이 새로 시작됐을 수 있고,
            #   그건 더 낡은 발화에 대한 답이라 역시 버리는 게 맞다.
            if (self._turn_beaver_unheard or self._beaver_unheard())                     and not self._batch_synthesizing:
                await self._discard_unheard_reply(user_text)
            else:
                # 앞 대답이 **들리고 있으면** 겹쳐 말하지 않는다(불변식 I1 — 비버 턴은 하나).
                # ⭐ 그렇다고 버리지도 않는다(2026-08-07). 예전엔 여기서 그냥 건너뛰어서
                #   "대답 다 해도 내가 중간에 말한 거에 답을 안 해" 가 됐다. 줄 세워 뒀다가
                #   대답이 끝나면 그때 답한다.
                self._pending_user_text = user_text
                self._pending_since = time.monotonic()
                logger.info("cascade 발화 대기열 — 비버가 말하는 중이라 대답 뒤로 미룬다: %r",
                            user_text[:40])
                return
        self.state = TurnState.THINKING
        self._reply_task = self._tg.create_task(self._run_reply(user_text, is_greeting))

    async def _discard_unheard_reply(self, user_text: str) -> None:
        """준비 중이던 대답을 버리고 새 발화에 답할 자리를 비운다.

        ⛔ **클라 버퍼도 같이 지운다.** 소리가 아직 안 났어도 바이트는 이미 나가 있을 수
          있다(선행 버퍼). 그걸 안 지우면 버린 대답이 **그대로 재생된다** — 사장님이 겪으신
          "삭제돼야 하는데 계속 나온다"가 정확히 이것이다.
        ⚠ 취소 완료를 **기다린다**(barge-in 경로와 다르다). 안 기다리면 죽은 태스크의
          finally 가 뒤늦게 돌면서 새 대답의 state(THINKING)를 IDLE 로 덮는다. 어차피 지금은
          아무 소리도 안 나가고 있어 기다려서 잃는 시간이 없다.
        """
        task, cancelled_turn = self._reply_task, self.beaver.turn_id
        logger.info(
            "cascade 준비 중이던 대답을 버린다 — 아직 아무도 못 들었다(들린 %dms). "
            "새 발화에 답한다: %r", self._audible_ms(), user_text[:40],
        )
        self._cancel_reply()
        if cancelled_turn is not None:
            await self.beaver.cancel(reason="unheard_replaced")
        if task is not None:
            done, _ = await asyncio.wait({task}, timeout=_REPLY_CANCEL_WAIT_S)
            if not done:
                logger.warning("cascade 버린 대답이 %.1f초 안에 안 죽었다 — 그대로 진행한다",
                               _REPLY_CANCEL_WAIT_S)
        # 되살릴 이유가 없다(아무도 안 들었다) + 대기열에 낡은 말이 남아 있으면 안 된다.
        self._interrupted = None
        self._pending_user_text = ""

    async def _run_reply(self, user_text: str, is_greeting: bool = False) -> None:
        """사용자 발화 1건에 대한 비버의 대답 — LLM 스트리밍 → 문장 분할 → TTS → 송출."""
        self._reply_cancelled = False
        # ⛔ **대답마다 비운다.** 안 비우면 이전 턴의 엔진·마커 집계가 누적돼 로그가
        #   `tts=chirp+gemini` 처럼 섞여 찍히고, **어느 엔진이 낸 소리인지 못 가린다**
        #   (A/B 판정이 오염된다 — 2026-08-07 실측 로그에서 그 증상이 나왔다).
        self._tts_engines.clear()
        self._marker_seen.clear()
        self._reply_spans.clear()
        self._tts_ttfb_ms = -1      # 이 대답의 **첫** 합성 요청이 첫 오디오를 받기까지
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
        timing = _ReplyTiming(began, self._reply_queued_ms)
        self._reply_queued_ms = 0
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
                first_audio_at = self.beaver.first_audio_at
                sent = await self._speak(text_batch)
                if sent and first_audio_ms < 0:
                    # ⭐ 첫 바이트가 **실제로 나간 시각**을 원장에서 받는다. 여기(=배치 전량
                    #   송출 완료) 시각을 쓰면 페이서 대기가 '첫소리'에 통째로 들어간다.
                    timing.mark_audio(first_audio_at or self.beaver.first_audio_at)
                    timing.vendor_ms = self._tts_ttfb_ms
                    timing.mark_batch(int(sent / BEAVER_BYTES_PER_MS))
                    first_audio_ms = timing.first_sound_ms
                spoken_chars += len(text_batch)

            if self._tts_engine == _GEMINI_BATCH_CHOICE:
                turn_id, first_audio_ms, spoken_chars = await self._run_batch_reply(chat, timing)
                self._remember_beaver(turn_id, chat.text)
                logger.info(
                    "cascade 대답%s(배치): turn=%s %s 글자=%d 문장모델=%s tts=%s %s",
                    "(선톡)" if is_greeting else "", turn_id, timing.summary(), spoken_chars,
                    settings.CASCADE_LLM_MODEL,
                    "+".join(sorted(self._tts_engines)) or self._tts_vendor(),
                    self._reading_summary(self._batch_synth_s),
                )
                return
            async for piece in chat.chunks():
                timing.mark_chunk()
                for sentence in buffer.push(piece):
                    timing.mark_sentence()
                    # ⚠ **첫 문장 단독**은 Chirp 규칙이다. Gemini 는 짧은 요청이 특히
                    #   불리하고(고정 오버헤드 ≈1.3초), 어차피 선행 버퍼로 1.5초를 기다리므로
                    #   첫 문장만 따로 쏘면 손해만 본다 — 그래서 Gemini 는 묶어서 낸다.
                    if (first_audio_ms < 0 and not pending
                            and not self._gemini_realtime()
                            and self._tts_engine not in _ELEVEN_CHOICES):
                        pending.append(sentence)
                        await _flush_batch()        # 첫 문장 = 단독 즉시 송출
                        continue
                    pending.append(sentence)
                    if sum(len(x) for x in pending) >= self._batch_chars():
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
                "cascade 대답%s: turn=%s %s 글자=%d 문장모델=%s tts=%s 마커=%s "
                "gemini호출=%d %s %s",
                "(선톡)" if is_greeting else "", turn_id, timing.summary(), spoken_chars,
                settings.CASCADE_LLM_MODEL,
                "+".join(sorted(self._tts_engines)) or self._tts_vendor(),
                ",".join(f"{k}{v}" for k, v in sorted(self._marker_seen.items())) or "-",
                self._tts_gemini_calls, "고정" if self._tts_gemini_off else "-",
                self._reading_summary(None),
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
            self._batch_synthesizing = False
            self.usage.record_llm(chat.usage_metadata, vendor=settings.CASCADE_LLM_MODEL)
            if self.state in (TurnState.THINKING, TurnState.BEAVER_SPEAKING):
                self.state = TurnState.IDLE
            await self._drain_pending_user_text()

    async def _drain_pending_user_text(self) -> None:
        """비버가 말하는 동안 들어온 발화에 **이제** 답한다(줄 세워 둔 것 하나).

        ⛔ 여기서 안 부르면 그 발화는 영영 답을 못 받는다 — 사장님이 겪으신 그 증상이다.
        """
        pending, self._pending_user_text = self._pending_user_text, ""
        waited_ms, self._pending_since = self._pending_wait_ms(), 0.0
        if not pending:
            return
        self._reply_task = None      # 방금 끝난 태스크 참조를 비워야 새 대답이 시작된다
        logger.info("cascade 대기열 발화에 답한다(%dms 기다렸다): %r", waited_ms, pending[:40])
        self._reply_queued_ms = waited_ms
        await self._start_reply(pending)

    async def _run_batch_reply(self, chat: Any, timing: "_ReplyTiming") -> tuple[str | None, int, int]:
        """⭐ **전체를 합성한 뒤 한 번에 들려준다**(Gemini 전용 배치 모드).

        왜 이런 걸 만드나: Gemini 는 합성 배속이 1.3x 라 실시간 재생을 못 따라간다(실측).
        버퍼가 안 쌓여 문장 중간에 끊기고, **끊긴 소리로는 감정·발음이 좋은지 판단할 수가
        없다.** 이 모드는 판정을 위해 지연을 내주는 것이지 프로덕션 방식이 아니다.

        ⛔ 침묵을 설명하지 않으면 사용자는 "끊겼나?" 하고 통화를 끊는다 — 단계마다
          beaver_preparing 을 보낸다(지연은 비용이 아니지만 **설명되지 않는 침묵은 비용**이다).
        ⛔ 구간을 **병렬로 쏘지 않는다.** 순간 집중이 429 를 부른다(분당 10회 한도).
          어차피 이 모드에서 지연은 비용이 아니다.
        """
        self._batch_synthesizing = True
        await self._safe(ServerBeaverPreparing(stage="llm"))
        async for _ in chat.chunks():                 # 전체 텍스트가 완성될 때까지 받는다
            timing.mark_chunk()
        timing.mark_sentence()   # 배치는 '전체 텍스트 완성'이 곧 문장 완성 시점이다
        text = strip_markers(chat.text).strip() and chat.text.strip()
        if not text:
            self._batch_synthesizing = False
            return None, -1, 0

        segments = split_by_language(
            text, settings.CASCADE_TTS_LANGUAGE, settings.CASCADE_TTS_TARGET_LANGUAGE
        )
        marker_state = _marker_state(text)
        self._marker_seen[marker_state] = self._marker_seen.get(marker_state, 0) + 1
        budget_s = max(5, settings.CASCADE_TTS_BATCH_TIMEOUT_S)
        started = time.monotonic()
        parts: list[tuple[bytes, str]] = []
        per_segment: list[str] = []
        for i, (seg_text, language) in enumerate(segments, start=1):
            await self._safe(
                ServerBeaverPreparing(
                    stage="tts", index=i, total=len(segments),
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            )
            left = budget_s - (time.monotonic() - started)
            if left <= 0:
                # ⛔ 조용히 멈추지 않는다 — 여기까지 만든 것만 들려주고 그 사실을 남긴다.
                logger.warning(
                    "cascade 배치 합성 상한(%ds) 초과 — 구간 %d/%d 부터 버린다",
                    budget_s, i, len(segments),
                )
                break
            seg_started = time.monotonic()
            pcm = await self._synthesize_all(seg_text, language, left)
            per_segment.append(
                f"{language}:{len(seg_text)}자/{(time.monotonic() - seg_started):.1f}s"
                f"/{len(pcm) / BEAVER_BYTES_PER_MS / 1000:.1f}s"
            )
            if pcm:
                parts.append((pcm, seg_text, language))
        if not parts:
            self._batch_synthesizing = False
            logger.warning("cascade 배치 합성: 오디오가 한 조각도 안 나왔다(구간 %d개)", len(segments))
            return None, -1, 0

        audio_s = sum(len(p) for p, _, _ in parts) / BEAVER_BYTES_PER_MS / 1000
        synth_s = time.monotonic() - started
        self._batch_synth_s = synth_s
        logger.info(
            "cascade 배치 합성: 구간 %d개 %s 텍스트 %d자 → 합성 %.1f초 오디오 %.1f초 "
            "합성배속 %.2fx [%s]",
            len(segments), "/".join(lang for _, lang in segments) or "-", len(text),
            synth_s, audio_s, (audio_s / synth_s) if synth_s > 0 else 0.0,
            " ".join(per_segment),
        )

        # 이어붙인 오디오를 **한 번에** 송출한다(페이서가 실시간 속도로 흘려보낸다 — I3).
        self._batch_synthesizing = False   # 여기서부터 소리가 난다 = barge-in 유효
        turn_id = await self._begin_beaver_turn()
        first_audio_ms = -1
        spoken = 0
        for pcm, seg_text, language in parts:
            label = strip_markers(seg_text).strip()
            sent = await self._speak_pcm(pcm, label)
            if sent and first_audio_ms < 0:
                timing.mark_audio(self.beaver.first_audio_at)
                timing.vendor_ms = self._tts_ttfb_ms
                timing.mark_batch(int(sent / BEAVER_BYTES_PER_MS))
                first_audio_ms = timing.first_sound_ms
            if sent:
                self._reply_spans.append((language, len(label), sent))
            spoken += len(seg_text)
        await self.beaver.end()
        return turn_id, first_audio_ms, spoken

    async def _synthesize_all(self, text: str, language: str, budget_s: float) -> bytes:
        """구간 하나를 **끝까지** 합성해 PCM 을 모은다(스트리밍 송출 없음)."""
        chunks: list[bytes] = []
        try:
            async with asyncio.timeout(max(1.0, budget_s)):
                stream = await tts.synthesize_stream(
                    text,
                    language=language,
                    voice=settings.CASCADE_TTS_VOICE,
                    engine=tts.GEMINI_ENGINE,
                    speaking_rate=self._tts_rate,
                    style_prompt=self._tts_style,
                    allow_gemini=not self._tts_gemini_off,
                )
                async for chunk in stream:
                    chunks.append(chunk)
        except asyncio.TimeoutError:
            logger.warning("cascade 배치 합성: 구간 시간 초과 — 받은 %d조각만 쓴다", len(chunks))
        self.usage.record_tts(text, vendor=self._tts_vendor())
        pcm = b"".join(chunks)
        self.usage.record_tts_audio(len(pcm))
        if pcm:
            self._tts_engines.add(self._tts_vendor())
        return pcm

    async def _speak_pcm(self, pcm: bytes, label: str) -> int:
        """이미 만들어 둔 PCM 을 프레임으로 쪼개 송출한다(이름표는 **마지막 조각**에)."""
        step = int(BEAVER_FRAME_INTERVAL_MS * BEAVER_BYTES_PER_MS)
        frames = [pcm[i : i + step] for i in range(0, len(pcm), step)] or [b""]

        async def _gen():
            for frame in frames:
                if frame:
                    yield frame

        return await speak_stream(self.beaver, _gen(), label)

    def _reading_summary(self, synth_s: float | None) -> str:
        """이 대답의 **읽기 속도**를 실측으로 요약한다(언어별).

        ⛔ 분자는 **실제로 소리가 나간 글자**다. 원가용 tts_chars(=API 에 넘긴 글자)를 쓰면
          barge-in 으로 끊긴 몫까지 분자에 들어가 **28자/초 같은 불가능한 값**이 나온다.
        ⚠ 언어를 반드시 붙인다 — 같은 시간을 말해도 영어와 한국어는 글자 수가 다르다
          ("Hello, how are you today?" 25자 vs "안녕하세요 오늘 어때요?" 14자).
          **언어를 빼고 자/초를 비교하면 틀린 결론이 나온다.**
        합성 배속은 배치에서만 잰다 — 실시간은 합성과 재생이 겹쳐 '합성 소요'가 정의되지 않는다.
        ⛔ 억지 숫자를 만들어 배치 값과 나란히 두지 않는다. 못 재면 못 잰다고 적는다.
        """
        if not self._reply_spans:
            return "오디오=0초 읽기=측정불가(소리 없음)"
        by_lang: dict[str, list[int]] = {}
        for language, chars, audio_bytes in self._reply_spans:
            row = by_lang.setdefault(language, [0, 0])
            row[0] += chars
            row[1] += audio_bytes
        total_chars = sum(r[0] for r in by_lang.values())
        total_s = sum(r[1] for r in by_lang.values()) / BEAVER_BYTES_PER_MS / 1000
        per_lang = " ".join(
            f"{lang}:{r[0]}자/{r[1] / BEAVER_BYTES_PER_MS / 1000:.1f}초"
            f"/{(r[0] / (r[1] / BEAVER_BYTES_PER_MS / 1000)):.1f}자per초"
            for lang, r in sorted(by_lang.items()) if r[1] > 0
        )
        speed = f"{total_chars / total_s:.1f}" if total_s > 0 else "-"
        synth = (
            f"합성배속={(total_s / synth_s):.2f}x" if synth_s and synth_s > 0
            else "합성배속=측정불가(실시간은 합성·재생이 겹친다)"
        )
        return (
            f"들린글자={total_chars} 오디오={total_s:.1f}초 읽기={speed}자per초 "
            f"[{per_lang}] {synth}"
        )

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
        if self._tts_engine in _ELEVEN_CHOICES:
            # ⛔ 구글이 아니다 — 별도 어댑터를 탄다. 폴백도 하지 않는다(엔진을 골라 듣는 중인데
            #   조용히 다른 소리가 나면 A/B 가 거짓말이 된다).
            stream = elevenlabs_tts.synthesize_stream(
                sentence,
                model_id=_ELEVEN_MODEL_BY_CHOICE[self._tts_engine],
                voice_id=self._eleven_voice_for(language),
                speaking_rate=self._tts_rate,
                report=report,
            )
            sent = await speak_stream(self.beaver, stream, sentence)
            if self._tts_ttfb_ms < 0 and report.get("ttfb_ms") is not None:
                self._tts_ttfb_ms = int(report["ttfb_ms"])
            # ⛔ 여기서 문자를 **또 세지 않는다**(2026-08-09 수정). 위에서 이미 셌는데 이 가지가
            #   한 번 더 세고 있었다 = ElevenLabs 원가가 **두 배**로 잡혔다. 원가가 이 프로젝트의
            #   동기라 이런 이중계상은 결론을 통째로 뒤집는다.
            self.usage.record_tts_audio(sent)
            if sent:
                self._tts_engines.add(report.get("engine") or self._tts_vendor())
                self._reply_spans.append((language, len(sentence), sent))
            else:
                self.usage.record_tts("", vendor=self._tts_vendor(), failed=True)
            return sent
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
            engine=(tts.GEMINI_ENGINE if self._tts_engine == _GEMINI_BATCH_CHOICE
                    else self._tts_engine or None),
            speaking_rate=self._tts_rate,
            style_prompt=self._tts_style,
        )
        sent = await speak_stream(self.beaver, stream, sentence)
        # ⭐ 내보낸 오디오 초 — Gemini-TTS 단가의 기준(문자가 아니라 출력 오디오 토큰이다).
        self.usage.record_tts_audio(sent)
        if sent:
            # 소리가 실제로 나간 것만 읽기 속도의 재료가 된다(합성 실패는 글자도 안 센다).
            self._reply_spans.append((language, len(sentence), sent))
        if self._tts_ttfb_ms < 0 and report.get("ttfb_ms") is not None:
            self._tts_ttfb_ms = int(report["ttfb_ms"])
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

    def _eleven_voice_for(self, language: str) -> str | None:
        """이 구간을 읽을 ElevenLabs 음성 — **타깃 언어는 따로 지정할 수 있다.**

        ⛔ ElevenLabs 는 다국어 음성 **하나**가 두 언어를 다 읽는다. 그 음성이 영어권 화자에서
          만들어졌으면 한국어가 **외국인 억양**으로 나온다. 비버는 발음 선생님이고 학습자가
          그대로 따라 한다 — 목소리가 사람 같아도 발음이 틀리면 못 쓴다.
        ⚠ 미설정이면 None 을 돌려 기존 동작(음성 하나가 다 읽는다)을 그대로 둔다. 마커 분할은
          이미 하고 있으므로(`_speak`), 음성만 갈아 끼우면 Chirp 경로와 같은 구조가 된다.
        """
        target = (settings.CASCADE_TTS_TARGET_LANGUAGE or "").strip().lower()
        if target and language.strip().lower() == target:
            return (settings.CASCADE_TTS_ELEVEN_VOICE_ID_TARGET or "").strip() or None
        return None

    def _gemini_realtime(self) -> bool:
        """지금 세션이 **Gemini 실시간** 모드인가(배치는 별도 경로라 제외)."""
        return self._tts_engine == tts.GEMINI_ENGINE

    def _batch_chars(self) -> int:
        """문장을 얼마나 모아 한 번에 합성할지 — **엔진마다 다르다.**

        Gemini 는 요청마다 고정 오버헤드(≈1.3초)가 붙어 짧은 요청이 특히 손해다. 반대로
        TTFB 는 길이와 거의 무관했으므로(49자 1,328ms / 196자 1,188ms) 크게 묶어도 첫 소리가
        그만큼 늦지 않는다. 요청 수가 줄어 분당 쿼터(10회)에도 유리하다.
        """
        if self._tts_engine in (tts.GEMINI_ENGINE, _GEMINI_BATCH_CHOICE):
            return max(1, settings.CASCADE_TTS_BATCH_CHARS_GEMINI)
        if self._tts_engine in _ELEVEN_CHOICES:
            return max(1, settings.CASCADE_TTS_BATCH_CHARS_ELEVEN)
        return max(1, settings.CASCADE_TTS_BATCH_CHARS)

    def _tts_vendor(self) -> str:
        """원가 벤더 문자열 = **의도한 엔진**. 실제로 다른 엔진이 냈으면 위에서 보정한다.

        ⚠ 모델별로 단가가 다르다 — flash 와 v3 를 뭉개면 원가를 못 가른다.
        """
        if self._tts_engine in (tts.GEMINI_ENGINE, _GEMINI_BATCH_CHOICE):
            return (settings.CASCADE_TTS_GEMINI_MODEL or tts.GEMINI_ENGINE).strip()
        if self._tts_engine in _ELEVEN_CHOICES:
            return _ELEVEN_MODEL_BY_CHOICE[self._tts_engine]
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
            if picked in _ELEVEN_CHOICES and not elevenlabs_tts.is_configured():
                # ⛔ 키가 없으면 **명확히 거절한다.** 조용히 다른 엔진으로 바꾸면 사장님이
                #   "ElevenLabs 소리"로 착각하신다(오늘 폴백에서 배운 그대로다).
                logger.warning(
                    "cascade tts 엔진 거절: %s — API 키 미설정(CASCADE_TTS_ELEVEN_API_KEY)", picked
                )
            elif picked in (tts.GEMINI_ENGINE, _CHIRP_CHOICE, _GEMINI_BATCH_CHOICE,
                            *_ELEVEN_CHOICES):
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
        # ⭐ 엔진에 맞는 선행 버퍼를 잡는다. Gemini 는 합성이 재생보다 최대 1.5초 뒤처져서
        #   200ms 만 모으고 시작하면 **반드시 언더런이 난다**(그게 '끊긴다'의 정체였다).
        #   배속 자체는 1.7~1.9x 라 초반만 견디면 격차가 벌어져 안 끊긴다.
        if self._tts_engine in (tts.GEMINI_ENGINE, _GEMINI_BATCH_CHOICE):
            self.beaver.lead_ms = max(0, settings.CASCADE_TTS_LEAD_MS_GEMINI)
        elif self._tts_engine in _ELEVEN_CHOICES:
            # ⚠ 세 모델이 같은 값을 쓴다. multilingual_v2 는 문서가 **실시간 최적화가 아니라고
            #   명시**하므로(“Most stable on long-form generations”) 더 뒤처질 수 있는데,
            #   측정 없이 모델별 상수를 새로 만들면 그게 곧 근거 없는 숫자가 된다.
            #   보수적인 현재 값(Gemini 급)으로 시작하고, **끊김이 관측되면 이 값부터** 올려라.
            self.beaver.lead_ms = max(0, settings.CASCADE_TTS_LEAD_MS_ELEVEN)
        else:
            self.beaver.lead_ms = None
        # ⭐ 세션 시작에 한 줄 — 이 통화의 소리가 어느 엔진 것인지 여기서 확정된다.
        logger.info(
            "cascade 엔진 선택: %s (%s) speaking_rate=%s 선행버퍼=%dms 묶음=%d자 style=%r",
            self._tts_engine or tts.CHIRP3_ENGINE, source,
            self._tts_rate if self._tts_rate is not None else "서버값",
            self.beaver.lead_ms if self.beaver.lead_ms is not None
            else settings.CASCADE_TTS_LEAD_MS,
            self._batch_chars(),
            (self._tts_style if self._tts_style is not None else "서버값")[:40],
        )

    def _stt_language_codes(self) -> list[str]:
        """이 통화에서 **들을 언어들** — 학습 언어 + 모국어.

        ⛔ 학습자는 두 언어를 섞어 쓴다: 모국어로 묻고("What does that mean?") 한국어로 따라
          말한다. 한쪽만 들으면 다른 쪽은 **통째로 사라진다**(2026-08-08 실통화: 영어 발화
          5회 연속 36초가 text='' 로 닫혔다). 이건 데모 버그가 아니라 제품 결함이다 —
          우리 사용자는 외국인이다.
        ⚠ **지금은 데모 경로다.** 회원의 모국어(`member.language`)로 배선하는 건 계약을 정하고
          따로 간다. 여기서는 실험이 가능하도록 env 두 개에서 끌어온다.
        학습 언어를 먼저 적는다 — 문서가 순서에 의미를 부여하지는 않지만(REST 레퍼런스는
        "most likely language detected" 라고만 한다), 이 통화의 주 언어가 무엇인지 우리 의도를
        코드에 남긴다. 벤더 문서의 권고("bare minimum")대로 2개까지만 쓴다.
        """
        return [settings.CASCADE_TTS_TARGET_LANGUAGE, settings.CASCADE_TTS_LANGUAGE]

    def _apply_aec_hint(self, aec: Any) -> None:
        """start.aec 로 **세션별** barge-in 정책을 정한다 — 에너지 게이트를 켤지 끌지.

        ⭐ 2026-08-08 역할 재정의(사장님 판단): 에너지 관문은 '발화 감지기'가 아니라
          **에코 2차 방어**다. 클라가 AEC 를 선언했으면 막을 대상이 없다(같은 날 로그에서
          비버 재생 중 전사 0건으로 실증) — 그 세션에서 관문을 돌리면 잔여 에코가 아니라
          **진짜 발화만** 걸린다. 그래서 끈다. 선언이 없거나 모르는 값이면 **켠다**(안전 쪽).
          지금 플러터 앱이 이 상태다(필드를 안 보낸다 — 2026-08-07 확인).

        ⛔ 어떤 mode 가 "AEC 있음"인지는 **명시적 화이트리스트**(protocol.AEC_MODES_WITH_CANCEL)
          로만 판정한다. 모르는 값이 방어를 끄면 안 된다 — 데모가 보내는 `hw` 가 예전 분기
          어디에도 안 걸려 조용히 기본값으로 흐르던 것이 그 사고의 예고편이었다.

        ⛔ **소리만으로는 끊지 않는다**(사장님 판단, 2026-08-08). 그래서 여기서 확인 방식을
          `immediate` 로 올리지 않는다 — 이어폰이어도 전사를 기다린다. 이어폰에 에코가 없다는
          것은 "게이트가 필요 없다"는 뜻이지 "글자 없이 끊어도 된다"는 뜻이 아니다.
        """
        hint: AecHint | None = None
        if isinstance(aec, dict):
            try:
                hint = AecHint.model_validate(aec)
            except Exception as exc:  # noqa: BLE001 - 계약 위반은 거절이 아니라 안전 쪽 폴백(R5)
                logger.warning("cascade aec 해석 실패(%s) — unknown 으로 본다(게이트 켬): %r",
                               exc, aec)
        elif aec is not None:
            logger.warning("cascade aec 가 객체가 아니다(%s) — unknown 으로 본다(게이트 켬)",
                           type(aec).__name__)
        self._aec_mode = hint.mode if hint is not None else "미선언"
        self._energy_gate = not (hint is not None and hint.has_echo_cancel)
        if self._energy_gate:
            # ⛔ 에코 억제는 **음향 층(클라 AEC)의 일**이다. 서버는 말 내용으로 에코를 추측하지
            #   않는다(그 추측이 어학 앱에서 따라 말하기를 죽였다 — 2026-08-08).
            logger.warning(
                "cascade AEC 미선언/미상(mode=%s) — 에너지 게이트 ON(임계 %.4f). 에코가 나면"
                " **클라에서** 잡아야 한다(서버는 말 내용으로 에코를 판정하지 않는다)",
                self._aec_mode, settings.CASCADE_BARGEIN_RMS,
            )
        if self._bargein_confirm != "transcript":
            logger.warning(
                "cascade ⚠ bargein_confirm=%s — 글자 없이 **소리만으로** 비버를 끊는 모드다."
                " env 로 켠 것이면 끄는 게 맞다(잡음이 비버를 죽인다)", self._bargein_confirm,
            )
        logger.info(
            "cascade aec 힌트: %s mode=%s → bargein_confirm=%s 에너지게이트=%s(임계 %.4f)"
            " 안전망=%dms",
            self._sid, self._aec_mode, self._bargein_confirm,
            "on" if self._energy_gate else "off", settings.CASCADE_BARGEIN_RMS,
            settings.CASCADE_BARGEIN_SUSTAIN_MS,
        )

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
