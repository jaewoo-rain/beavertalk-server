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
  IDLE → USER_SPEAKING → THINKING → BEAVER_SPEAKING → (barge-in) CANCELLING → IDLE
⭐ **상태 축과 턴 축은 다르다**(2026-08-11 QA 발견1). 비버가 말하는 중에도 사용자 턴은 열린다
  (barge-in 겹침) — 그래서 `_open_turn` 은 비버 상태를 뺏지 않고, 턴 타이머는 상태가 아니라
  **`_turn_id` 가 있는가**로 판단한다.
⛔ `CANCELLING` 은 반드시 풀린다(`_settle_reply_state` / `_settle_cancelling`). 안 풀리면
  `_open_turn` 이 그걸 보존해 이후 모든 턴이 USER_SPEAKING 이 못 된다 — 통화가 침묵으로 굳는다.
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

from core import audio
from core import gemini_analysis
from core import gemini_chat
from core import openai_tts
from core.audio import trim_silence_edges
from core import stt as stt_mod
from core import tts
from core.config import settings
from core.languages import normalize_locale, resolve_language
from core.persona_prompt import build_system_instruction, new_close_tag, seed_opening
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
    ClientCascadeTiming,
    ClientPlaybackProgress,
    ClientRouteChange,
    ClientTestBeaver,
    ServerAudioCancel,
    ServerCascadeReady,
    ServerTurnEnd,
    CascadeCallEnded,
    CascadeCallStarted,
    CascadeSentenceMarker,
    CascadeTurnStart,
    ServerUserTurnEnd,
    ServerUserTurnStart,
    ServerInputPartial,
    ServerSttRollover,
    ServerTestCancelReport,
    cascade_server_adapter,
)
from domains.learning.realtime.cascade_reply import (
    EMOTION_STYLES,
    MARKER,
    SentenceBuffer,
    detect_emotion,
    emotion_style,
    read_bare_label,
    read_stray_tag,
    sample_aligned,
    speak_stream,
    split_by_language,
    split_sentences as _split_sentences,
    strip_emotion_tags,
    strip_markers,
)
# ⛔ 힌트·작별 문구는 **Live 의 것을 그대로 쓴다**(새로 쓰면 두 경로가 갈린다).
#   ⚠ 다만 `_hint_sidecar` 자체는 못 쓴다 — 그 함수는 **원시 WebSocket** 을 받아
#     `client_state` 를 보고 `send_text` 한다. 캐스케이드는 transport 추상화를 지나므로
#     **생성·스키마·프레임은 재사용**하고 송신만 우리 경로로 한다(아래 `_run_hint`).
from domains.learning.realtime.call_session import (
    _LOCALE_LABEL,
    HintOut,
    _close_seed as live_close_seed,
    _hint_instruction,
)
from domains.learning.realtime.cascade_usage import CascadeUsage, log_usage_summary
from domains.learning.service import call_service
from domains.learning.service import normalcall_service as svc
from domains.learning.realtime.protocol import (
    AecHint,
    HintExample,
    ServerError,
    ServerHint,
    ServerPong,
)

logger = logging.getLogger(__name__)

_DEFAULT_SAMPLE_RATE = 16000
# 이어갈 값이 없을 때의 표정. ⚠ 폴백일 뿐 **정책이 아니다** — 태그가 없으면 직전 값을
# 이어간다(`_sentence_emotion`). 첫 구간에만 이 값으로 시작한다.
_DEFAULT_EMOTION = "neutral"
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
# ⭐ **오디오 시계 자기점검**(2026-08-13). 받은 바이트로 계산한 초가 통화 경과보다 이만큼 크면
#   우리가 가정한 규격(레이트·채널)이 틀렸다는 뜻이다 — 실시간 마이크는 실시간보다 빠를 수 없다.
#   ⛔ **위쪽만 본다.** 아래쪽(적게 옴)은 정상이다: 마이크 상시개방이 꺼져 있으면 비버가 말하는
#     동안 클라가 아예 안 보낸다. 양쪽을 다 경고하면 정상 통화가 매번 시끄러워진다.
#   1.5 근거: 규격이 틀리면 배수로 어긋난다(2배·3배). 1.5 는 그 아래 어디에도 안 걸린다.
_AUDIO_CLOCK_RATIO_MAX = 1.5
_AUDIO_CLOCK_MIN_MS = 10_000    # 표본이 이만큼 쌓인 뒤에 본다(개시 직후의 버스트는 무의미)
# 클라 계기와 조인하려고 들고 있는 **비버 턴별 서버 첫소리**의 개수 상한.
# 클라 메시지는 그 턴이 끝난 직후에 오므로 몇 개면 충분하다 — 15분치를 들고 있을 이유가 없다.
_FIRST_SOUND_HISTORY = 8
# 문장 마커 위치 추정의 **출발값**(글자당 바이트). 24kHz·16bit = 48,000 B/s 이고 실측 읽기
# 속도가 8~12자/초라 4,000~6,000 B/자 사이다. 첫 구간만 이 값을 쓰고, 그다음부터는 그 통화에서
# 실제로 들린 속도로 갈아탄다. ⚠ 상수 하나로 두 언어를 맞출 수 없어서 **배우는 값**으로 뒀다.
_DEFAULT_BYTES_PER_CHAR = 5_000.0
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
# (ElevenLabs 3종은 2026-08-10 제거했다 — 실측 전에 접었다. 사유는 그 커밋 메시지에 있다.)
# ⭐ OpenAI TTS — `/v1/audio/speech` HTTP 청크 스트리밍. `pcm`=24k/16bit/mono 라 변환이 없다.
#   `instructions` 가 스타일 프롬프트 자리라 **감정 태그가 그대로 붙는다.**
_OPENAI_TTS_CHOICE = "openai-tts"
# ⭐ **스타일(감정) 지시를 실제로 받는 엔진들.** ⛔ 한 곳에서만 정한다 — 2026-08-11 에 이 판정이
#   `_emotion_log` 에 하드코딩돼 있어서, OpenAI 로 돌면서 `감정=인사(미적용:cloud-tts-chirp3-hd)`
#   라고 **거짓 로그**가 찍혔다(감정은 실제로 들어가고 있었다). 로그가 거짓이면 사장님이
#   "감정이 안 걸리는구나"라고 잘못 판단하신다 — 잘못 잰 지표로 하루를 태운 것과 같은 계열이다.
@dataclass
class _OpenSegment:
    """**열려 있는** 합성 요청 하나 — 벤더가 보내는 오디오를 미리 받아 두는 자리.

    ⭐ 왜 미리 받나(2026-08-11 사장님: "언어 바뀔 때 살짝 끊긴다"): 언어 구간마다 벤더
      왕복이 **직렬로** 붙는다. 실측 로그에서 한국어 구간은 5자인데 1.0~1.35초였다(대부분
      TTFB). 그 동안 페이서에 줄 게 없어 소리가 빈다. 묶음 텍스트는 이미 다 손에 있으므로
      **앞 구간이 재생되는 동안** 뒤 구간을 열어 두면 그 공백만 사라진다.
    ⛔ 구간별 음성은 그대로다 — 한국어는 한국어 음성이 읽는다(발음이 학습 자료다).
    ⛔ 미리 **받아만** 둔다. 송출은 순서대로 `BeaverOutput` 이 하고, 실시간 페이싱(I3)도
      거기서 그대로 걸린다 — 미리 만들었다고 한꺼번에 밀어내지 않는다.
    """

    text: str
    language: str
    # ⭐ 이 구간의 표정(문장 단위). **이미 carry-forward 가 끝난 값**이다 — 클라에 규칙을
    #   넘기지 않는다(취소로 마커를 버릴 때 클라 상태가 어긋나기 때문).
    emotion: str
    queue: Any                 # asyncio.Queue[bytes | None] — None = 이 구간 끝
    task: Any                  # 벤더 → 큐 펌프(취소하면 선행분이 통째로 버려진다)
    report: dict
    align: dict
    trim: bool
    opened_at: float
    # ⭐⭐ 이 구간 안의 **문장들**(태그를 뗀 텍스트, 그 문장의 감정) — 2026-08-16.
    #   한 구간에 문장이 여럿이면 마커도 문장 수만큼 나간다. 비면 구간 전체가 마커 하나다
    #   (`_speak` 직접 호출·데모 훅처럼 문장을 안 쪼갠 경로).
    #   ⛔ TTS 요청은 **여전히 구간 단위**다 — 문장별로 쪼개면 짧은 문장 뒤에 벤더 왕복이
    #     통째로 공백이 된다(그래서 사장님이 그 안을 안 고르셨다).
    sentences: list[tuple[str, str]] = field(default_factory=list)


class _PreparedBatch:
    """송출 전에 **미리 준비한 묶음** — 언어 구간 목록 + 첫 구간의 열린 스트림.

    ⛔ `first` 는 **이미 벤더에 요청이 나간** 구간이다. 안 쓰게 되면(취소) **반드시 cancel** 해야
      한다 — 안 그러면 끊은 뒤에 소리가 더 나온다(I3).
    """

    __slots__ = ("text", "segments", "emotion", "first", "opened_at")

    def __init__(self, text: str, segments: list[tuple[str, str]],
                 emotion: str | None, first: "_OpenSegment | None",
                 opened_at: float = 0.0) -> None:
        self.text = text
        self.segments = segments
        self.emotion = emotion
        self.first = first
        # ⭐ **합성을 시작한 시각**(2026-08-13). 앞 묶음이 끝나기 **얼마나 전에** 시작했는지가
        #   곧 선행 합성의 성적표다 — 그 여유가 벤더 왕복보다 짧으면 차액이 그대로 공백이 된다.
        self.opened_at = opened_at

    def cancel(self) -> None:
        if self.first is not None:
            self.first.task.cancel()
            self.first = None


@dataclass(frozen=True)
class _TtsProfile:
    """엔진 하나의 **성질**. ⛔ 분기에서 엔진 이름을 비교하지 말고 여기를 봐라.

    같은 사고가 **두 번** 났다(2026-08-11):
      ① 묶음 크기 — OpenAI 가 Chirp 값(160)을 물려받았다. Chirp 은 TTFB 165~212ms 라 견디는데
         OpenAI 는 545~953ms 다.
      ② 첫 문장 단독 송출 — 조건이 `_gemini_realtime()` 이라 **OpenAI 가 Chirp 규칙을 탔다.**
         실측: OpenAI 첫 배치 오디오가 **800·1000·1450ms** 인데 선행버퍼가 1500ms 다 →
         버퍼를 못 채우고 바닥나서 **끊긴다.** Gemini 는 첫 배치가 6440·8240ms 라 안 끊긴다.
    ⇒ 두 번 다 "이름으로 비교"가 원인이다. 새 엔진은 **이 표에 한 줄**을 넣으면 되고,
      안 넣으면 회귀가 먼저 실패한다.

    Attributes:
        batch_setting: 문장을 얼마나 모아 한 요청으로 보낼지(설정 이름).
        lead_setting: 페이서 선행버퍼(설정 이름). None 이면 서버 공통값.
        rate_setting: 배속 기본값(설정 이름).
        solo_first_sentence: **첫 문장을 단독으로 즉시 쏘나.**
            ⭐ 왕복이 짧은 엔진에서만 이득이다(첫 소리가 그만큼 빨라진다). 왕복이 길면
            첫 배치가 짧아져 **재생이 버퍼보다 먼저 바닥나고 끊긴다** — 그게 위 ②다.
        takes_style: 감정/스타일 지시를 **실제로 받나**(안 받는 엔진은 로그에 미적용으로 적는다).
        vendor: 원가 벤더 문자열을 만드는 함수(모델마다 단가가 다르다).
        prefetch_depth: **동시에 열어 둘 합성 요청 수**(1 = 지금처럼 하나씩).
            ⛔ 벤더 쿼터가 상한을 정한다 — Gemini 는 분당 10회라 늘리면 429 를 앞당긴다.
        is_configured: 이 엔진을 **지금 쓸 수 있나**(키·자격증명). 화면에서 고를 때 검사한다.
        google_engine: `core.tts` 에 넘길 엔진 이름. 구글을 안 타는 엔진은 None.
            ⚠ Chirp 이 None 이면 안 된다 — 그러면 `core.tts` 가 서버 기본값으로 되돌아가
            **고른 것과 다른 소리**가 난다.
    """

    batch_setting: str
    lead_setting: str | None
    rate_setting: str
    solo_first_sentence: bool
    takes_style: bool
    prefetch_depth: int
    vendor: Any
    is_configured: Any
    google_engine: str | None


@dataclass(frozen=True)
class _SttProfile:
    """이 STT 엔진의 **전사 도착 성질**. ⛔ 분기에서 엔진 이름을 비교하지 말고 여기를 봐라.

    ⚠ 키가 **접두사**인 이유: 실제로 돈 벤더 문자열은 모델 ID 를 포함한다
      (`openai-gpt-4o-mini-transcribe`). 완전일치로 두면 **모델을 바꾸는 순간 조용히 표에서
      빠져** 앵커가 꺼지고, 아무도 모른 채 턴이 다시 느려진다.

    Attributes:
        prefix: 실제로 돈 벤더 문자열의 앞머리(빈 문자열 = 폴백 성질).
        anchor_on_speech_end: **턴 종료 시계를 speech_end 오프셋에 건다.**
            ⭐ 전사에 오프셋이 없는 엔진에서 유일한 위치 정보가 speech_end 다. 켜면 임계값
              (800ms)을 **안 건드리고** 파이프라인 지연만큼 앞당긴다 — 문장을 자를 위험이 없다.
            ⛔ 켜도 되는 조건은 하나다: **바닥값 ≥ 이 엔진의 전사 도착 지연(p95).**
              안 그러면 글자가 오기 전에 턴이 닫힌다(2026-08-07 그 결함).
        final_after_end_ms: speech_end 뒤 최종 전사가 오기까지(실측 **p95**) = 이 엔진의 바닥값.
    """

    prefix: str
    anchor_on_speech_end: bool
    final_after_end_ms: int


# ⭐ openai: 사장님 실기기 408초 통화 15표본 — 중앙값 320ms · **p95 430ms** · 최대 603ms.
#   바닥 450ms 는 그 p95 위다. ⚠ 최대 603ms 는 덮지 못한다 — 그때는 빈 턴으로 닫히고 늦은
#   전사가 새 턴을 연다(대답은 나간다. `_is_stale_tail` 의 빈 턴 수용 경로).
#   ⛔ 그 꼬리까지 덮으려고 바닥을 600 으로 올리면 **이득이 사라진다**(버는 게 320ms 다).
# ⛔ google: **앵커를 쓰지 않는다.** 2026-08-07 실측이 그 엔진 것이고(전사 723~870ms >
#   VAD 291~348ms), 지금도 그 성질이 유효한지 잰 적이 없다. R5 폴백으로 살아 있는 경로라
#   모르는 채로 켜면 그 통화만 조용히 깨진다.
_STT_PROFILES: tuple[_SttProfile, ...] = (
    _SttProfile("openai-", anchor_on_speech_end=True, final_after_end_ms=450),
)
_STT_FALLBACK_PROFILE = _SttProfile("", anchor_on_speech_end=False, final_after_end_ms=0)


def _stt_profile_for(vendor: str) -> _SttProfile:
    """벤더 문자열 → 성질. 모르는 엔진은 **앵커 없이**(현행 그대로) 간다."""
    name = (vendor or "").strip().lower()
    for profile in _STT_PROFILES:
        if profile.prefix and name.startswith(profile.prefix):
            return profile
    return _STT_FALLBACK_PROFILE


_TTS_PROFILES: dict[str, _TtsProfile] = {
    # Chirp: TTFB 165~212ms — 왕복이 짧아 첫 문장 단독이 **이득**이다(첫 소리가 빨라진다).
    _CHIRP_CHOICE: _TtsProfile(
        "CASCADE_TTS_BATCH_CHARS", None, "CASCADE_TTS_SPEAKING_RATE",
        solo_first_sentence=True, takes_style=False,
        # 왕복이 165~212ms 라 얻을 게 적다. 쿼터도 실측이 없어 2 까지만 연다(보수적).
        prefetch_depth=2,
        vendor=lambda: tts.CHIRP3_ENGINE,
        is_configured=lambda: True,        # 구글 자격증명은 서버 기본 경로다
        google_engine=_CHIRP_CHOICE,
    ),
    # Gemini 실시간: TTFB 805~1271ms · 합성이 재생보다 최대 1.5초 뒤처진다.
    tts.GEMINI_ENGINE: _TtsProfile(
        "CASCADE_TTS_BATCH_CHARS_GEMINI", "CASCADE_TTS_LEAD_MS_GEMINI",
        "CASCADE_TTS_SPEAKING_RATE_GEMINI",
        solo_first_sentence=False, takes_style=True,
        # ⚠ 2 로 올린다(2026-08-12 정정). 처음엔 "분당 10회 상한이라 미리 열면 상한을 빨리
        #   태운다"고 1로 뒀는데, **그 논리가 틀렸다**: 상한은 분당 **요청 수**이고 선행 합성은
        #   같은 구간을 1~2초 **일찍** 부를 뿐 요청 수를 늘리지 않는다. 분당 총량은 그대로다.
        #   ⛔ 그래서 1 로 둔 대가만 남았다 — 실통화에서 구간마다 0.83~1.49초씩 소리가 비었다
        #     (Gemini TTFB 805~1271ms × 구간 5~7개). 사장님이 "언어 바뀔 때 끊긴다"고 하신 그것이다.
        #   ⚠ 3 이 아니라 2 인 이유: **동시 요청** 한도는 1차 자료로 확인 못 했다. 모르는 값
        #     앞에서는 한 칸만 움직인다. 429 가 나면 세션 백오프가 Chirp 으로 내린다(안전망).
        prefetch_depth=2,
        vendor=lambda: (settings.CASCADE_TTS_GEMINI_MODEL or tts.GEMINI_ENGINE).strip(),
        is_configured=lambda: True,
        google_engine=tts.GEMINI_ENGINE,
    ),
    # Gemini 배치: 전체를 합성한 뒤 한 번에 낸다(첫 문장 규칙 자체가 안 탄다).
    _GEMINI_BATCH_CHOICE: _TtsProfile(
        "CASCADE_TTS_BATCH_CHARS_GEMINI", "CASCADE_TTS_LEAD_MS_GEMINI",
        "CASCADE_TTS_SPEAKING_RATE_GEMINI",
        solo_first_sentence=False, takes_style=True,
        prefetch_depth=1,                  # 같은 쿼터를 쓴다
        vendor=lambda: (settings.CASCADE_TTS_GEMINI_MODEL or tts.GEMINI_ENGINE).strip(),
        is_configured=lambda: True,
        google_engine=tts.GEMINI_ENGINE,   # 배치도 소리는 Gemini 가 낸다(모으는 방식만 다르다)
    ),
    # OpenAI: TTFB 545~953ms — **Chirp 이 아니라 Gemini 쪽 성질**이다(위 ②).
    _OPENAI_TTS_CHOICE: _TtsProfile(
        "CASCADE_TTS_BATCH_CHARS_OPENAI", "CASCADE_TTS_LEAD_MS_OPENAI",
        "CASCADE_TTS_SPEAKING_RATE",
        solo_first_sentence=False, takes_style=True,
        # ⭐ 분당 상한이 없다 → 구간 왕복(0.7~1.35초)을 앞 구간 재생 뒤로 숨긴다.
        #   3 인 이유: 실측 대답이 **구간 3~5개**라 3이면 대부분 첫 구간 재생 중에 나머지가
        #   다 열린다. 더 키워도 얻는 게 없고 메모리·동시요청만 는다.
        prefetch_depth=3,
        vendor=openai_tts.vendor_name,
        is_configured=openai_tts.is_configured,   # ⛔ 키 없으면 이 엔진만 거절(R5)
        google_engine=None,                       # 구글을 안 탄다(별도 어댑터)
    ),
}

# 표에 없는 엔진이 쓸 성질 — ⛔ **Chirp 을 그대로 주면 안 된다.** 그러면 첫 문장 단독 송출을
#   물려주는 셈이라 이번 사고를 그대로 재현한다. 왕복을 모르면 **묶는 쪽**이 안전하다
#   (단독 송출은 왕복이 짧다는 걸 알 때만 이득이고, 틀리면 소리가 끊긴다).
#   스타일도 안 받는 것으로 둔다 — 받는다고 가정했다가 틀리면 로그가 거짓말을 한다.
_TTS_FALLBACK_PROFILE = _TtsProfile(
    "CASCADE_TTS_BATCH_CHARS", None, "CASCADE_TTS_SPEAKING_RATE",
    solo_first_sentence=False, takes_style=False,
    prefetch_depth=1,                  # 쿼터를 모르면 늘리지 않는다
    vendor=lambda: tts.CHIRP3_ENGINE,
    is_configured=lambda: False,       # 모르는 엔진은 고를 수 없다(거절이 안전하다)
    google_engine=_CHIRP_CHOICE,
)


def _profile_for(engine: str) -> _TtsProfile:
    """엔진의 성질. ⛔ 표에 없으면 **경고를 찍고** 보수적인 기본 성질로 간다(조용히 안 떨어진다)."""
    name = (engine or _CHIRP_CHOICE).strip()
    profile = _TTS_PROFILES.get(name)
    if profile is None:
        logger.warning("cascade tts 성질 미등록 엔진(%r) — 표에 한 줄 넣어라", name[:24])
        return _TTS_FALLBACK_PROFILE
    return profile


# ⭐ **고를 수 있는 TTS 전부.** 엔진을 늘릴 때 손댈 자리를 한 곳으로 모은다 —
#   나열이 여러 곳에 흩어져 있으면 **어느 하나에서 빠진다**(2026-08-11 실제로 그랬다:
#   묶음 크기 분기에서 OpenAI 가 빠져 Chirp 값 160 을 물려받았다. Chirp 은 TTFB 165~212ms 라
#   요청이 많아도 견디는데, OpenAI 는 545~953ms 다 — **요청도 많고 왕복도 긴 최악 조합**).
_TTS_CHOICES = (_CHIRP_CHOICE, tts.GEMINI_ENGINE, _GEMINI_BATCH_CHOICE, _OPENAI_TTS_CHOICE)

# 이보다 빠르면 **말이 아니라 잘린 오디오**다. 실측 기준: Live 한국어 7.7자/초,
# 빠른 영어 16~18자/초. 여유를 크게 둬서 정상 발화가 걸리지 않게 한다.
_IMPOSSIBLE_CHARS_PER_S = 25.0


def _batch_chars_for(engine: str) -> int:
    """엔진별 묶음 크기 — **요청당 고정 오버헤드(TTFB)가 큰 엔진일수록 크게 묶는다.**"""
    return max(1, int(getattr(settings, _profile_for(engine).batch_setting)))
_STYLE_PROMPT_MAX = 200     # 스타일 문구 상한 — 길어지면 지연 비교가 오염된다
# 말하기 배속 허용 범위 — proto 원문 [0.25, 2.0]. 밖은 거절한다(요청이 통째로 거절되기 전에).
_RATE_MIN, _RATE_MAX = 0.25, 2.0
# (TTS 벤더 이름은 core.tts 가 소유한다 — 엔진 A/B 로 값이 바뀌므로 _tts_vendor() 로 읽는다)


def _parse_rate_map(raw: str) -> dict[str, float]:
    """`"en:1.4,ko:1.0"` → `{"en": 1.4, "ko": 1.0}`.

    ⛔ 범위 밖([0.25, 2.0] — proto 원문)·숫자 아님은 **버리고 경고**한다. 통화가 죽으면 안 되고
      (R5), 조용히 넣으면 요청이 통째로 거절된다.
    """
    out: dict[str, float] = {}
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        lang, _, value = item.partition(":")
        try:
            rate = float(value)
        except (TypeError, ValueError):
            logger.warning("cascade 언어별 배속 무시 — 숫자가 아니다: %r", item[:24])
            continue
        if not (_RATE_MIN <= rate <= _RATE_MAX):
            logger.warning("cascade 언어별 배속 무시 — 범위 [%.2f, %.2f] 밖: %r",
                           _RATE_MIN, _RATE_MAX, item[:24])
            continue
        key = lang.strip().lower()
        if key:
            out[key] = rate
    return out


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
# ⭐ Live 와 **같은 상수**를 쓴다 — 두 엔진의 말하기 속도를 비교하려면 잣대가 하나여야 한다.
BEAVER_BYTES_PER_MS = audio.OUTPUT_BYTES_PER_MS


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
    # 이 턴에서 내보낸 오디오 원본(통화 기록 저장용). 저장하면 비운다 — 안 비우면 15분
    # 통화의 비버 음성 전체가 통화 내내 RAM 에 남는다(Live 가 겪고 고친 자리다).
    pcm: bytearray = field(default_factory=bytearray)


class _ReplyTiming:
    """첫소리 분해 — **어디서 늦는지**를 로그 한 줄로 가른다(2026-08-08).

    첫소리가 06:07 ~3.1초 → 07:03 ~2.7초 → 08:12 ~5.9초로 2배가 됐는데, 한 숫자로 뭉쳐
    있어서 원인을 못 짚었다. 타임스탬프 역산은 변수가 셋인데 방정식이 하나라 신뢰할 수 없다
    ("여러 지표가 같은 방향을 가리켜도 원인이 확인된 게 아니다").

        첫소리 = ①LLM 첫 조각 + ②첫 문장 완성 + ③벤더 TTFB + ④첫 바이트 송출
    ⚠ **2026-08-09 에 뜻이 바뀌었다.** 예전에는 "첫 배치가 **전량** 송출될 때까지"였고, 그 안에
      페이서(실시간 송출)가 통째로 들어 있어 **이미 소리가 나가는 시간**을 지연으로 셌다.
      지금은 **첫 바이트가 클라로 나간 시각**까지다 — 그 뒤(첫 배치 전량)는 `첫배치=` 로 따로 낸다.
      ⛔ 08-09 이전 로그의 숫자와 직접 비교하지 마라.
    ⚠ **대기열 대기는 첫소리 밖이다**(`began` 이 _run_reply 안에서 찍힌다). 사용자가 체감하는
      지연은 둘의 합이라 따로 싣는다.
    ⛔ '출력 대기'는 항목에 없다 — 페이서는 **그 비버 턴의 시작 시각** 기준으로 재우므로
      (`_pace`: elapsed = now − _cur.started_at) 새 턴의 첫 조각은 절대 안 기다린다.
      앞 대답이 흐르는 동안은 애초에 `_run_reply` 가 시작되지 않는다(대기열).
    """

    __slots__ = ("began", "queued_ms", "chunk_at", "sentence_at", "request_at", "audio_at",
                 "batch_at", "vendor_ms", "batch_audio_ms", "notified_at", "closed_at")

    def __init__(self, began: float, queued_ms: int = 0) -> None:
        self.began = began
        self.queued_ms = max(0, queued_ms)
        self.chunk_at = 0.0
        self.sentence_at = 0.0
        # ⭐ **첫 TTS 요청을 건 시각**(2026-08-13). 이게 없으면 "첫 문장이 준비된 뒤 요청까지"가
        #   `송출` 안에 뭉쳐 보인다 — 지금 그 자리는 묶음 정책이 먹는 시간이다(짧은 대답은
        #   묶음이 안 차서 **LLM 스트림이 끝난 뒤에야** 첫 요청이 나간다). 벤더 탓이 아닌데
        #   벤더 옆에 붙어 있으면 엉뚱한 곳을 고치게 된다.
        self.request_at = 0.0
        self.audio_at = 0.0        # 첫 바이트가 **클라로 나간** 시각(사용자가 듣기 시작한 때)
        self.batch_at = 0.0        # 첫 배치가 **전량** 나간 시각(페이서가 실시간으로 흘린다)
        self.vendor_ms = -1        # 벤더가 첫 오디오를 주기까지(report["ttfb_ms"])
        self.batch_audio_ms = 0    # 첫 배치의 오디오 길이
        # ⭐ `LLM첫조각` 안에 **우리 WS 전송 1건**이 들어 있다(`beaver_preparing`). 크기를
        #   몰라서 844ms 를 통째로 "LLM 이 느리다"로 읽고 있었다.
        self.notified_at = 0.0
        # ⭐ 문장 상한에서 `stream.aclose()` 로 벤더 스트림을 닫는 데 걸린 시간(`묶음대기` 안).
        self.closed_at = 0.0

    def mark_chunk(self) -> None:
        self.chunk_at = self.chunk_at or time.monotonic()

    def mark_sentence(self) -> None:
        self.mark_chunk()      # 첫 문장이 먼저 보이는 구현이어도 순서가 뒤집히지 않게
        self.sentence_at = self.sentence_at or time.monotonic()

    def mark_audio(self, at: float = 0.0) -> None:
        """첫 바이트가 나간 시각. 인자로 **원장이 기록한 실제 시각**을 받는다."""
        self.mark_sentence()
        self.audio_at = self.audio_at or at or time.monotonic()

    def mark_notified(self) -> None:
        """`beaver_preparing(llm)` WS 전송이 **끝난** 시각 — 이 전송은 `LLM첫조각` 안에 있다."""
        self.notified_at = self.notified_at or time.monotonic()

    def mark_closed(self) -> None:
        """문장 상한에서 벤더 스트림을 **닫은** 시각(`묶음대기` 안에 들어 있다)."""
        self.closed_at = self.closed_at or time.monotonic()

    def mark_request(self, at: float) -> None:
        """첫 합성 요청을 **건 시각** — 벤더 왕복의 출발점이다(도착점은 `vendor_ms`)."""
        if at and not self.request_at:
            self.request_at = at

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
        # ⭐ **묶음대기** = 첫 문장이 준비된 뒤 첫 요청을 걸기까지. 벤더도 송출도 아닌 **우리 정책**이다.
        #   ⚠ 못 잰 회차는 0 이 아니라 `?` 다 — 모르는 값을 0 으로 적으면 그 항목이 없는 것처럼 보인다.
        asked = ms(self.request_at, self.sentence_at) if self.request_at else 0
        line = (
            "첫소리=%dms(대기열 %d + LLM첫조각 %d + 문장완성 %d + 묶음대기 %s + 벤더 %d + 송출 %d)"
            % (self.first_sound_ms, self.queued_ms,
               ms(self.chunk_at, self.began),
               ms(self.sentence_at, self.chunk_at),
               str(asked) if self.request_at else "?",
               vendor,
               max(0, ms(self.audio_at, self.sentence_at) - asked - max(0, vendor)))
        )
        if self.batch_at:
            line += " 첫배치=%dms(오디오 %dms 페이서 %dms)" % (
                ms(self.batch_at, self.began), self.batch_audio_ms,
                ms(self.batch_at, self.audio_at),
            )
        # ⭐ **큰 항목 안에 숨은 우리 몫**(2026-08-15). 둘 다 다른 칸에 이미 포함돼 있다 —
        #   빼서 세지 마라. 여기 값이 크면 원인이 벤더가 아니라 **우리 배관**이라는 뜻이다.
        #   ⚠ 못 잰 회차는 아예 안 찍는다(0 으로 찍으면 "없었다"로 읽힌다).
        inner = []
        if self.notified_at:
            inner.append("WS알림 %dms" % ms(self.notified_at, self.began))
        if self.closed_at and self.sentence_at:
            inner.append("스트림닫기 %dms" % ms(self.closed_at, self.sentence_at))
        if inner:
            line += " [내부: %s]" % " · ".join(inner)
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
      I6. 클라로 나가는 오디오 바이너리는 **항상 2의 배수 바이트**다(PCM16 표본 경계)

    I3 이 서버 책임인 이유: 실시간보다 빨리 밀어내면 클라 버퍼가 무한히 부푼다 →
    barge-in 취소가 늦게 먹히고(사용자가 끊었는데 계속 들린다) 이력 절단도 같이 틀어진다.
    클라의 백로그 계측은 **안전망이지 대책이 아니다.**

    I6 이 여기 있는 이유(2026-08-11): OpenAI TTS 가 홀수 길이 조각을 흘렸고, 클라는
    `new Int16Array(buf)` 에서 예외가 나 **그 조각을 통째로, 아무 흔적 없이 버렸다.**
    정렬 자체는 요청 경계(`speak_stream`)가 한다 — 거기만 조각의 연속성을 안다. 여기서는
    **못 지나가게만** 한다: 어떤 경로로 들어와도 클라가 받는 바이너리는 짝수다.
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
        # ⭐ **와이어 공백** — 프레임을 안 내보낸 구간(250ms 이상)만 모은다. 클라의
        #   `SERVER GAP … mid-utterance` 와 같은 것을 서버 쪽에서 재는 값이다.
        #   대답마다 비우고(대답 줄에 찍는다), 여기 값이 크면 **클라가 굶는다**.
        self.wire_gaps: list[float] = []
        # ⭐⭐ **페이서가 일부러 붙든 시간**(2026-08-14). 위와 **전혀 다른 것**이다:
        #   와이어공백 = 보낼 게 없어서 빈 시간(**클라가 굶는다**)
        #   페이서보류 = 보낼 게 있는데 우리가 붙든 시간(**클라 버퍼가 이미 찼다** = 정상)
        #   ⛔ 예전엔 둘을 합쳐 `와이어공백` 하나로 쟀다(시각을 `_pace()` **뒤**에 찍었다).
        #     벤더 조각이 크면 페이서가 그 길이만큼 자는데, 그게 통째로 "공백"으로 잡혔다 —
        #     5.5초짜리 값이 결함인지 정상 페이싱인지 **구분할 수 없었다.**
        self.paced_holds: list[float] = []
        self._last_send_at = 0.0
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

    def first_audio_at_of(self, turn_id: str) -> float:
        """**그 턴**의 첫 오디오가 나간 시각(0 = 아직·모르는 턴).

        ⚠ `first_audio_at`(현재 턴)과 다르다 — 클라 계기는 그 턴이 **아직 재생 중일 때** 오고,
          지난 턴을 물을 수도 있다. 원장이 턴별로 살아 있으니 그걸 그대로 쓴다.
        """
        record = self._records.get(turn_id)
        if record is None or record.first_audio_at < 0:
            return 0.0
        return record.first_audio_at

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
    async def begin(self, emotion: str | None = None) -> str:
        """비버 턴 시작 — **오디오보다 먼저** turn_start 를 낸다(I2).

        ⭐ `emotion` 은 클라 아바타 표정이다(프론트 요구: **turn_start 한 칸**). 첫 소리보다
          먼저 나가므로 클라가 표정을 미리 바꿀 여유가 있다(그쪽 선행버퍼 900ms).
        ⛔ 값을 검사하지 않는다 — 클라가 모르는 값을 neutral 로 떨어뜨린다(프론트 계약).
          서버가 집합을 늘릴 때 **클라 배포를 기다리지 않기 위해서**다.
        """
        if self._cur is not None:
            raise InvariantError("이미 열린 비버 턴이 있다(중첩 금지)")
        self._turn_seq += 1
        turn_id = f"b{self._turn_seq}"
        # ⛔⛔ **턴 밖의 조용함은 공백이 아니다**(2026-08-13). 안 지우면 앞 턴의 마지막
        #   프레임부터 이 턴의 첫 프레임까지 — 즉 **사용자가 말하고 생각하던 시간**이 통째로
        #   공백으로 잡힌다. 실제로 `293.09s` 가 찍혔다: 조용한 통화의 유휴였다.
        #   ⚠ 지표가 유휴와 결함을 못 가르면 **그 지표는 못 읽는다.** 우리가 재려는 것은
        #     "비버가 말하는 중에 소리가 끊겼나"이고, 그건 **턴 안에서만** 뜻이 있다.
        self._last_send_at = 0.0
        self._cur = _TurnRecord(turn_id=turn_id, started_at=self._now())
        self._records[turn_id] = self._cur
        self._order.append(turn_id)
        while len(self._order) > self._HISTORY_MAX:
            self._records.pop(self._order.pop(0), None)
        await self._transport.send_event(
            json.loads(cascade_server_adapter.dump_json(
                CascadeTurnStart(turn_id=turn_id, emotion=emotion)
            ).decode())
        )
        return turn_id

    def take_pcm(self, turn_id: str | None = None) -> bytes:
        """이 턴에서 **실제로 내보낸 오디오**를 통째로 꺼낸다(꺼내면 비운다 — 메모리).

        ⭐ 통화 기록에 남길 비버 음성이다. 원장이 이미 바이트를 세고 있으므로 **같은 자리**에서
          모은다 — 따로 모으면 두 숫자가 갈린다(이 프로젝트에서 여러 번 겪었다).
        """
        record = self._record(turn_id)
        if record is None:
            return b""
        pcm, record.pcm = bytes(record.pcm), bytearray()
        return pcm

    async def send(self, pcm: bytes, text: str = "") -> None:
        """오디오 청크 1개 송출 + 원장 기록 + 페이싱(I1·I3).

        text 는 이 청크가 실제로 발음하는 대사(무음 패딩이면 빈 문자열).
        """
        if self._cur is None:
            raise InvariantError("비버 턴 밖에서 오디오를 보내려 했다(I1 위반)")
        if not pcm:
            return
        if len(pcm) % 2:
            # ⛔ I6 — 여기까지 홀수가 왔다는 건 **정렬 경로를 안 탄 조각**이 있다는 뜻이다.
            #   반 표본을 그냥 보내면 클라가 조용히 버리므로, 잘라서라도 짝수로 내보내고
            #   **사실을 크게 남긴다**(조용히 고치면 다음에 또 못 찾는다).
            logger.warning(
                "cascade ⚠ 홀수 바이트 오디오(%d) — I6 위반. 표본 경계로 잘라 보낸다. "
                "정렬은 speak_stream 이 해야 한다(어느 경로가 건너뛰었나)", len(pcm),
            )
            pcm = pcm[:-1]
            if not pcm:
                return
        # ⭐ **페이서에 들어가기 전 시각**. 여기까지가 "보낼 게 없던 시간"이고, 여기부터가
        #   "있는데 우리가 붙든 시간"이다. 이 한 줄이 굶김과 정상 페이싱을 가른다.
        arrived = self._now()
        await self._pace()
        # ⛔ **여기서 다시 본다**(2026-08-11 QA 발견4). 위 `_pace()` 는 최대 lead_ms 만큼 자고,
        #   그 사이 다른 태스크의 `cancel()` 이 `_cur` 를 None 으로 만들 수 있다. 재확인이 없으면
        #   AttributeError 가 나고, 호출부는 `InvariantError` 만 잡으므로 **TaskGroup 으로 올라가
        #   세션 전체가 죽는다.** 지금 안 터지는 건 호출부 3곳이 태스크를 먼저 cancel 하기
        #   때문이다 — 즉 **클래스가 아니라 호출 관례가 안전을 지키고 있었다.**
        if self._cur is None:
            raise InvariantError("송출 중 턴이 취소됐다(I1 — 정상 경로)")
        if self._cur.first_audio_at < 0:
            self._cur.first_audio_at = self._now()
        start = self._cur.sent_bytes
        self._cur.sent_bytes += len(pcm)
        self._cur.ledger.append(
            SpokenChunk(start_byte=start, end_byte=self._cur.sent_bytes, text=text)
        )
        # 통화 기록용 원본(저장하면 비운다 — `take_pcm`). ⛔ 여기 말고 다른 데서 모으면
        # 원장 바이트와 갈린다.
        self._cur.pcm.extend(pcm)
        # ⭐⭐ **와이어 공백**(2026-08-13) — 클라의 `SERVER GAP … mid-utterance` 와 같은 것을
        #   서버에서 잰다. 앞 프레임을 보낸 뒤 여기까지가 **아무것도 안 나간 시간**이다.
        #   ⛔ 여기서 재는 이유: 상위(묶음 경계)에서 재면 **선행 합성을 넣은 뒤 0 이 된다** —
        #     합성은 미리 시작했는데 소리가 아직 안 나가는 구간을 못 본다. 즉 값이 좋아진 척
        #     한다. 굶는 쪽은 클라이고, 클라가 보는 건 **프레임 간격**이다.
        #   ⚠ 판정 창은 클라와 같은 250ms(그 아래는 정상 페이싱이라 안 센다).
        now = self._now()
        if self._last_send_at > 0:
            # ⛔ **둘을 나눠 센다.** 합치면 5.5초가 결함인지 정상인지 영영 못 가른다.
            starve = arrived - self._last_send_at      # 보낼 게 없었다 = 클라가 굶는다
            paced = now - arrived                      # 있는데 붙들었다 = 클라 버퍼가 찼다
            if starve >= 0.25:
                self.wire_gaps.append(starve)
            if paced >= 0.25:
                self.paced_holds.append(paced)
        self._last_send_at = now
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
    """WS 1개 = 캐스케이드 세션 1건 — 턴 감지 → LLM → TTS → 송출까지 한 바퀴를 돈다.

    ⚠ LLM 클라이언트가 없으면 **턴 감지까지만** 돈다(비버가 말하지 않는다 — R5).
    """

    def __init__(
        self,
        transport: CascadeTransport,
        genai_client: Any = None,
        *,
        llm_client: Any = None,
        llm_location: str = "",
        session_factory: Any = None,
        member_id: int | None = None,
        member_target_language: str | None = None,
    ) -> None:
        self.transport = transport
        # ⭐ **대답 LLM 만** 다른 리전을 쓸 수 있다(2026-08-13 사장님 지시: "LLM 을 서울로").
        #   ⛔ 힌트·통화후 분석은 **기본 클라이언트를 그대로 쓴다.** 근거 셋:
        #     ① 실시간이 아니다 — 사용자가 그 왕복을 기다리지 않는다(리전 이득이 없다).
        #     ② 다른 모델을 쓴다(`JUDGE_MODEL`) — **그 모델의 서울 가용성은 확인 못 했다.**
        #        확인 안 된 채 옮기면 분석이 조용히 실패한다.
        #     ③ 곁다리가 실패해도 대답은 살아야 한다 — 배관을 나눠 두면 그게 자동으로 된다.
        #   ⚠ 안 넘기면 `genai_client` 를 그대로 쓴다 ⇒ 기본 동작이 지금과 **완전히 같다**.
        self._llm_client = llm_client or genai_client
        # ⭐ **어느 리전으로 돌았나** — 통화 로그에 찍는다(2026-08-13).
        #   ⛔ 부팅 로그로는 답이 안 된다: 인스턴스가 재활용되면 그 줄은 한참 전 것이고,
        #     통화 로그와 짝을 못 짓는다. 원가·지연을 통화 단위로 비교하려면 **그 통화
        #     안에서** 리전이 닫혀야 한다.
        #   ⚠ 설정값이 아니라 **실제로 만들어진 리전**을 받는다(폴백이 일어나면 다르다).
        self._llm_location = (llm_location or "").strip()
        # ── DB 연결(설계 20260812_1620) ──────────────────────────────────────
        # ⛔ 셋 다 **없어도 돈다**(데모·테스트). 없으면 env 기본값으로 예전처럼 간다 — DB 가
        #   빠졌다고 통화가 죽으면 안 된다(R5). 있으면 Live 와 **같은 함수**로 같은 기록을 남긴다.
        self._session_factory = session_factory
        self._member_id = member_id
        self._member_target_language = member_target_language
        self._character_id: int | None = None
        self._call_id: int | None = None
        # 통화 기록(Live 와 같은 계약: {turn_index, role, text, pcm}). 점진 저장 커서까지.
        self._segments: list[dict] = []
        self._persisted = 0          # 이미 DB 에 저장한 세그먼트 수
        self._next_turn_index = 1
        self._cur_user_pcm = bytearray()   # 열린 사용자 턴의 마이크 오디오(저장용)
        # ⭐⭐ **이 통화의 길이**(초) — 구독 플랜이 정한다(Free 5분 / Pro·Max 15분).
        #   ⛔ 절대 백스톱(`CASCADE_SESSION_MAX_S`)과 **다른 층**이다: 이건 정상 종료(작별)이고
        #     백스톱은 "펌프가 멈췄을 때 죽이는" 마지막 방어선이다. 겹치면 안 되므로
        #     `_backstop_s()` 가 백스톱을 통화 길이보다 항상 뒤로 잡는다.
        self._call_duration_s = call_service.FREE_CALL_DURATION_S
        self._farewell_started = False   # 작별을 이미 시작했다(두 번 하지 않는다)
        # 문장 단위 감정: 직전 구간의 값(태그가 없으면 이어간다) + 이 턴에서 보낸 구간 순번.
        self._last_emotion = _DEFAULT_EMOTION
        self._segment_seq = 0
        # 이 대답에서 **실제로 내보낸 자막(구간 마커) 수**. ⛔ `_segment_seq` 를 쓰면 안 된다 —
        # 그건 턴 안에서 계속 커지는 **클라 계약값**이라 대답별 개수가 안 나온다.
        self._sentence_markers = 0
        # 이 세션이 **왜** 끝났나 — 종료 통지의 reason 이 된다(기본은 사용자 종료).
        self._end_reason = "client"
        self._odd_frames_warned = False   # I6 경고는 통화당 한 번(도배 방지)
        # ⭐ 힌트 사이드카(D16) — **곁다리**다. 메인 파이프는 이걸 기다리지 않는다.
        #   세션당 동시 1개: 새 질문이 오면 이전 미완 힌트를 취소한다(낡은 힌트가 다음 턴에
        #   뜨면 학습자가 엉뚱한 예시를 본다).
        self._hint_task: Any = None
        self._hint_tasks: set = set()      # 강참조(GC 방지) — 세션 종료 때 전량 취소
        # 종료 태그 — 지시문과 시드가 **같은 값**을 써야 모델이 그 문구를 시스템 지시로 읽는다.
        self._close_tag = new_close_tag()
        # DB 에서 읽은 통화 설정(캐릭터 role·personality·voice·locale·레벨 프로파일·흥미).
        # 조회 실패면 None → env 기본값 경로 그대로(로그로 드러낸다).
        self._setup: dict | None = None
        # ⭐⭐ **이 통화의 언어는 여기 두 값이 전부다**(2026-08-12 사장님 지시).
        #   `_locale`  = 비버가 **설명·리액션**에 쓸 언어(학습자 모국어)
        #   `_target_*`= 비버가 **가르칠** 언어(학습 대상)
        #   ⛔ 예전엔 같은 뜻의 값이 **네 곳**에 흩어져 있었다(페르소나 locale·페르소나 대상
        #     라벨·TTS 구간 언어·TTS 대상 언어). 흩어져 있으면 **반드시 갈린다** — 실제로
        #     "설명은 영어인데 TTS 는 다른 언어로 읽는" 조합이 만들어질 수 있었다.
        #   ⇒ 세션 시작에 **한 번** 정하고(`_resolve_languages`), 이후 전부 이 값을 읽는다.
        #   ⚠ STT 는 여기서 안 정한다 — **자동 감지가 실측상 최선**이다(41개 언어 39/41).
        self._locale = (settings.CASCADE_TTS_LANGUAGE or "en").strip()
        self._target_code = (settings.CASCADE_TTS_TARGET_LANGUAGE or "ko").strip()
        self._target_label = (settings.CASCADE_TTS_TARGET_LANGUAGE_LABEL or "한국어").strip()
        self._voice: str | None = None      # 캐릭터 음색(로스터 이름). None = 서버 기본값
        self._voice_warned = False
        # 음색 폴백 경고는 통화당 한 번(도배 방지) — 구간마다 부르는 자리다.
        self._voice_fallback_warned = False          # OpenAI 음색 미적용 경고는 통화당 한 번
        # 비버가 말하려면 LLM 클라이언트가 있어야 한다. 없으면 **턴 감지까지만** 돈다 —
        # 키가 없다고 통화가 죽으면 안 된다(R5).
        self._genai_client = genai_client
        self._history: list[dict] = []
        # 상한을 넘어 이력에서 잘라낸 줄들. 지금은 안 쓰지만 **버리지 않고 들고 있는다** —
        # 나중에 요약해 되먹이려면 원문이 우리 손에 있어야 한다(Live 는 이게 불가능했다).
        self._history_dropped: list[dict] = []
        self._reply_task: asyncio.Task | None = None
        self._reply_cancelled = False
        # ⭐ 대답 세대 번호. 끝나는 태스크가 **자기 것일 때만** 상태를 되돌린다 —
        #   안 그러면 늦게 죽은 옛 태스크가 새 대답의 THINKING 을 IDLE 로 덮는다.
        self._reply_seq = 0
        self._system_cache: str | None = None
        self._tts_engines: set[str] = set()   # **이 대답에서** 실제로 소리를 낸 엔진(A/B 로그용)
        # TTS 선택은 **세션 값**이다(예전엔 매 문장 settings 를 읽었다). 클라가 start 에서
        # 고르면 그 값으로, 안 고르면 서버 설정으로 통화 내내 일관되게 간다.
        self._tts_engine = (settings.CASCADE_TTS_ENGINE or "").strip()
        self._tts_rate: float | None = None
        # ⭐ **언어별 배속.** 구간이 어느 언어인지는 이미 안다(`__마커__` 분할) — 거기에 값을
        #   붙인다. ⛔ 새 분류기를 만들지 않는다. 비면 예전과 같다(엔진 기본값 → 1.0).
        self._tts_rate_by_lang: dict[str, float] = _parse_rate_map(
            settings.CASCADE_TTS_SPEAKING_RATE_BY_LANG
        )
        # 이 대답에서 **실제로 나간** 언어별 배속(로그용 — 세션 값만 찍으면 구간별 차이를 못 본다)
        self._reply_rates: dict[str, float] = {}
        self._tts_style: str | None = None
        self._batch_synthesizing = False   # 배치 합성 중(소리가 아직 안 나갔다)
        self._batch_synth_s: float | None = None   # 배치 합성 소요(대답 줄의 합성배속 재료)
        self._marker_seen: dict[str, int] = {}   # 언어 마커 상태별 문장 수(실험 성립 판정)
        # 대답별 **읽기 속도 실측**용. (언어, 들린 글자, 오디오 바이트) — 소리가 실제로 나간 것만.
        # ⛔ 원가용 tts_chars 와 다르다: 그건 'API 에 넘긴 글자'(끊겨도 돈은 나간다)라
        #   분모(오디오)와 모집단이 어긋나 **읽기 속도로 쓰면 28자/초 같은 값이 나온다.**
        self._reply_spans: list[tuple[str, int, int]] = []
        # 대사 맨 앞에 온 **집합 밖 태그**(있으면 소리로 안 내보내고 로그로 드러낸다)
        self._dropped_tag = ""
        # 배치 경로가 **실제로 말한 텍스트**와 상한에 걸려 버린 꼬리(이력·로그가 쓴다)
        self._batch_spoken = ""
        self._batch_dropped = ""
        # 요청별 홀수 조각 수 — 0 이 아니면 벤더가 PCM16 표본 경계를 안 지킨다는 증거다.
        self._tts_odd_chunks: list[int] = []
        # ⭐ 구간마다 **앞뒤 침묵을 몇 ms 걷어냈나**(2026-08-13). 로컬에선 TTS 키가 없어
        #   벤더 오디오를 못 재므로, **실통화가 이 값을 답한다.** 0 이 계속 나오면 절단이
        #   안 도는 것이고, 큰 값이 나오면 벤더 패딩이 그만큼 있었다는 뜻이다.
        self._tts_trims: list[tuple[int, int]] = []
        # 요청별 **첫 소리를 기다린 시간** — 구간 사이에 소리가 빈 시간이다(선행 합성의 성적표).
        self._tts_waits: list[float] = []
        # 요청별 **선행**(송출보다 얼마나 먼저 걸었나) — `_tts_waits`(결과)와 짝이다.
        self._tts_leads: list[float] = []
        # ⭐ 문장 마커를 꽂을 위치를 추정하는 자 — 언어별 **글자당 바이트**(이 통화 실측).
        #   첫 구간만 기본값을 쓰고 그다음부터는 방금 들린 속도로 스스로 고친다.
        self._bytes_per_char: dict[str, float] = {}
        self._marker_drifts: list[int] = []      # 그 추정이 얼마나 틀렸나(%)
        self._marker_emotions: list[str] = []    # 이 대답에서 실제로 나간 표정들(순서대로)
        # 글자 교차검증이 순번을 고친 횟수(이 대답에서). 0 = 마커가 지켜졌다.
        self._lang_fixes = 0
        # 묶음별 (선행 여유 ms, 벤더 왕복 ms) — 남은 공백의 **원인**을 가르는 짝이다.
        self._batch_leads: list[tuple[int, int]] = []
        # 429 백오프는 **세션 단위**다(프로세스 전역이면 쿼터가 회복돼도 영영 Chirp 이다).
        self._tts_gemini_off = False
        self._tts_gemini_calls = 0
        self._tts_ttfb_ms = -1
        self._tts_asked_at = 0.0    # 이 대답의 **첫** 합성 요청을 건 시각(첫소리 분해용)
        self._reply_emotion: str | None = None
        # barge-in 보류 상태(전사 확인 대기 마감 시각) / 끊겨서 못 들려준 대답
        self._bargein_at: float | None = None
        self._interrupted: dict | None = None
        # 비버가 말하는 동안 들어온 발화(대답이 끝나면 답한다) / 지금 비버가 하는 말(에코 판정)
        # ⭐ 대기열은 **목록**이다(2026-08-12). 예전엔 문자열 하나여서 두 가지가 동시에 틀렸다:
        #   ① **뒤에 온 발화가 앞을 덮어썼다** — 두 마디를 하면 앞 마디가 조용히 사라진다.
        #   ② 하나씩 소비해서 **비버가 연속으로 두 번 답했다**(사장님 실통화 call 937 의 b4·b5).
        #   사람이라면 밀린 두 마디를 한 번에 받고 **한 번** 답한다.
        self._pending_user_texts: list[str] = []
        self._pending_since = 0.0     # 대기열에 들어간 시각
        self._reply_queued_ms = 0     # 이번 대답이 대기열에서 기다린 시간(첫소리 분해용)
        self._rms_log: deque[tuple[float, float]] = deque()
        self.state = TurnState.IDLE
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._t0 = time.monotonic()
        self._sid = "s%d" % next(_session_seq)
        self._sample_rate = _DEFAULT_SAMPLE_RATE
        # ⭐ **클라가 선언했나, 우리가 가정했나**(2026-08-13). 이 구분이 없어서 반나절을 태웠다:
        #   `stt_audio_s/dur_s` 가 2.00배로 나와 "에뮬 마이크가 2배"라는 결론까지 갔는데
        #   프론트가 HAL 에서 재니 1.002배였다. **그 사이를 가를 값이 서버 로그에 없었다.**
        self._rate_declared = False
        self._channels = 1          # ⛔ 서버는 모노만 처리한다(다운믹스 없음)
        self._audio_ms = 0.0        # 클라에서 받아 STT 로 흘린 오디오 총량(오디오 타임라인)
        self._audio_clock_warned = False   # 오디오 시계 경고는 통화당 한 번(도배 금지)
        # ⭐⭐ **speech_end 도착 → 최종 전사 도착**(2026-08-13). 앵커를 VAD 로 옮길 수 있는지는
        #   오직 이 값이 답한다: 옮기면 남는 안전망이 바닥값(CASCADE_TURN_MIN_WAIT_MS) 하나뿐이라
        #   **바닥 ≥ 이 지연**이어야 성립한다. ⛔ 중앙값이 아니라 **p95** 로 봐야 한다 —
        #   중앙값으로 정하면 꼬리에서 턴이 말한 채로 빈 채 닫힌다(2026-08-07 그 결함이다).
        #   ⚠ 지금 근거로 쓰이는 723~870ms 는 **Google STT v2** 시절 값이다(openai 어댑터는
        #     사흘 뒤에 태어났다 — 92d8c10). 지금 엔진에서는 잰 적이 없다.
        self._speech_end_at = 0.0
        self._final_lag_ms = -1     # -1 = 이 턴에서 못 쟀다(0 으로 먹지 않는다)
        # ⭐ **이 턴의 '말 끝난 지점'**(오디오 시각). 앵커가 이 값을 쓴다 — 세션 전역
        #   `_last_speech_end_offset_ms`(이어붙임 판정용)와 **다른 것**이다: 그건 턴을 넘어
        #   살아남고, 이건 턴마다 지워진다. 섞으면 앞 턴의 지점으로 이번 턴을 재게 된다.
        self._turn_end_offset_ms = -1
        self._anchor_saved_ms = 0   # 앵커가 이 턴에서 실제로 앞당긴 시간(0 = 못 벌었다)
        # 실제로 돈 STT 벤더(스트림이 말해 준다 — 설정이 아니라 **결과**다. 폴백이 있다).
        self._stt_vendor = ""
        # ⭐ 비버 턴 id → 서버가 잰 **첫소리 ms**. 클라 계기(`client_timing`)와 조인할 키다.
        #   ⛔ 로그로만 찍고 버리면 조인이 불가능하다 — 클라 메시지는 그 뒤에 온다.
        self._first_sound_ms: dict[str, int] = {}
        # ⭐⭐ 비버 턴 id → **그 대답이 시작된 시각**. 위 값은 묶음을 다 보낸 뒤에야 채워지는데
        #   클라 계기는 **첫 소리에** 온다 ⇒ 그 시점엔 위가 비어 있어 조인이 100% 실패했다
        #   (2026-08-15 실통화 전 턴 `짝없음`). 이 값이 있으면 물어본 자리에서 바로 계산된다.
        self._reply_began_at: dict[str, float] = {}
        self._reply_began = 0.0     # 지금 만들고 있는 대답의 시작 시각(턴이 열릴 때 위로 묶인다)
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
        # ⭐ **STT 무활동 감시.** 턴이 열리는 순간 걸고, STT 이벤트가 올 때마다 갱신한다.
        #   이게 없으면 STT 가 speech_begin 하나만 보내고 조용해졌을 때 **아무 시계도 안 걸려**
        #   턴이 상한까지 굳는다(2026-08-10: 30초 침묵 뒤 사장님이 통화를 끊으셨다).
        self._turn_idle_at: float | None = None
        self._speech_active = False           # STT 가 "발화 중"이라고 보는가(BEGIN..END)
        self._last_voice_offset_ms = -1       # 마지막 음성 활동의 오디오 시각
        self._last_voice_at = 0.0             # 동 — 도착 시각(오프셋 미상일 때 폴백)
        self._pipeline_lag_ms = 0             # audio_ms_sent − offset (리전 왕복 + 인식 지연)
        self._lag_warned = False              # 비정상 지연은 **한 번만** 크게 알린다
        # 방금 닫은 턴(유령 턴 차단 — 같은 발화가 턴 2개가 되는 것을 막는다)
        self._closed_turn_id: str | None = None
        self._closed_end_offset_ms = -1
        # 앞 조각(벤더 VAD 가 쪼갠 같은 발화의 앞부분) — 이 턴 텍스트 앞에 붙는다.
        self._turn_carry = ""
        # ⛔ **speech_end 로만** 갱신한다. 이어붙임 판정의 기준점이라, 아무 이벤트나 찍는
        #   `_last_voice_offset_ms` 를 쓰면 **잡음 speech_begin 이 자기 자신을 기준으로 삼아**
        #   매번 '이어짐'이 되고 카운트다운이 영영 안 걸린다(차 안에서 턴이 안 닫힌다).
        self._last_speech_end_offset_ms = -1
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
        # 비버 출력(TTS 송출·원장·페이서). LLM 이 없어 소리를 안 내는 세션에서도, 클라가
        # 되보내는 playback_progress 를 **턴별 원장에 대조**하려면 지금부터 있어야 한다.
        self.beaver = BeaverOutput(transport)
        self._spoken_by_turn: dict[str, str] = {}   # turn_id → 실제로 들린 대사(이력용)
        # 원가 계측 — 캐스케이드의 **유일한 동기**가 원가라 세션이 끝나면 반드시 한 줄 남긴다.
        # 구성요소(STT·LLM·TTS)마다 수집 지점이 따로 있다 — cascade_usage 가 모아 한 줄로 낸다.
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
            self._rate_declared = raw is not None
            if sample_rate != _DEFAULT_SAMPLE_RATE:
                logger.warning(
                    "cascade start: 클라 sample_rate=%dHz 가 서버 기대(%dHz)와 다르다 — STT 는 "
                    "이 값으로 설정하지만 오디오 타임라인·지연 계측이 이 값에 의존한다",
                    sample_rate, _DEFAULT_SAMPLE_RATE,
                )
            # ⛔ 채널은 **1 만 처리한다.** 스테레오가 오면 바이트가 2배라 오디오 타임라인이
            #   정확히 2배로 늘고, 그러면 턴 타이머·barge-in 최소지속·원가 초가 **전부 같이**
            #   틀어진다. 다운믹스를 안 하므로 STT 도 잡음을 듣는다. 조용히 두면 안 되는 값이다.
            try:
                self._channels = max(1, int(ctrl.get("channels") or 1))
            except (TypeError, ValueError):
                logger.warning("cascade start: channels 해석 실패(%r) → 1 로 진행",
                               ctrl.get("channels"))
                self._channels = 1
            if self._channels != 1:
                logger.warning(
                    "cascade start: 클라 channels=%d — 서버는 **모노만** 처리한다(다운믹스 없다). "
                    "오디오 타임라인이 %d배로 늘어 턴 타이머·barge-in·원가 초가 같이 틀어진다",
                    self._channels, self._channels,
                )
            self._apply_tts_choice(ctrl)
            self._apply_aec_hint(ctrl.get("aec"))
        elif first.kind == "audio":
            # start 없이 오디오부터 온 세션도 **어느 체제로 도는지**는 로그에 남아야 한다.
            self._apply_aec_hint(None)
            pending_audio = first.audio
        self._sample_rate = sample_rate

        # ⭐ **DB 조회는 STT 개시보다 먼저**다. 페르소나(설명 언어·캐릭터)와 학습 대상 언어가
        #   정해져야 선톡 첫 문장이 옳은 언어로 나간다 — 뒤에 두면 첫 발화가 env 기본값으로
        #   나가고, 그건 "모든 학습자에게 같은 언어로 인사하는" 지금 결함 그대로다.
        #   ⚠ Live 도 같은 순서다(run_call: 캐릭터·setup → 통화 행 → 세션 open).
        await self._load_call_context()

        stream = stt_mod.make_stt_v2_stream(sample_rate, self._stt_language_codes())
        # ⭐ **무슨 언어로 듣고 있는지 로그만 보고 알 수 있어야 한다**(2026-08-08). 지금까지는
        #   코드를 읽고 env 를 조회해야 알 수 있었고, 그래서 "영어가 안 들린다"를 실통화 5건이
        #   빈 턴으로 닫힌 뒤에야 찾았다.
        logger.info(
            "cascade stt: %s 인식언어=%s 모델=%s 위치=%s",
            self._sid, getattr(stream, "language_codes", None) or self._stt_language_codes(),
            settings.STT_V2_MODEL, settings.STT_V2_LOCATION,
        )
        self._log_config_snapshot()
        try:
            await stream.start()
        except Exception as exc:  # noqa: BLE001 - 개시 실패는 이 세션만 실패(R5)
            logger.exception("캐스케이드 STT v2 개시 실패")
            await self._safe(ServerError(code="stt_start_failed", message=str(exc), recoverable=False))
            return

        # ⭐ **실제로 돈 엔진을 여기서 굳힌다**(설정이 아니라 `start()` 의 결과다 — 키가 없거나
        #   개시가 실패하면 스트림이 조용히 google 로 폴백한다). 턴 종료 앵커를 켤지가 이 값에
        #   달렸으므로, 설정으로 판단하면 **폴백된 통화에서 잘못된 앵커**가 켜진다.
        self._stt_vendor = getattr(stream, "vendor", "") or ""
        profile = self._stt_profile()
        logger.info(
            "cascade stt 엔진: %s 벤더=%s 턴앵커=%s%s",
            self._sid, self._stt_vendor or "미상",
            "speech_end" if profile.anchor_on_speech_end else "전사(현행)",
            " 바닥=%dms" % profile.final_after_end_ms if profile.anchor_on_speech_end else "",
        )

        self._t0 = time.monotonic()
        await self._safe(
            ServerCascadeReady(
                engine=stt_mod.stt_v2_engine_name(),
                turn_silence_ms=self._silence_ms,
                sample_rate=sample_rate,
                language=settings.STT_V2_LANGUAGE or settings.STT_LANGUAGE,
                bargein_confirm=self._bargein_confirm,
                mic_always_open=settings.CASCADE_MIC_ALWAYS_OPEN,
            )
        )

        # ⭐ **세션 절대 백스톱**(Live 불변식의 대응물). 턴 시계 넷은 전부 "이벤트가 정상적으로
        #   흐른다"를 전제하므로, 그 전제가 깨지면 아무것도 안 걸린다. 여기가 마지막 방어선이다.
        #   ⛔ 하드 킬이 아니다 — 아래 `finally` 가 그대로 돌아 **원가 한 줄이 남는다.**
        #   ⚠ 감싸는 구간은 **STT 스트림이 열린 뒤**다. 그 앞(첫 메시지 대기)은 STT·LLM 이 아직
        #     안 돌아 **과금이 0** 이고, 유휴 소켓은 플랫폼 타임아웃이 맡는다.
        # 바닥은 0·음수 방어용이다(정책이 아니다) — 값 자체는 설정이 정한다.
        backstop_s = self._backstop_s()
        try:
            async with asyncio.timeout(backstop_s), asyncio.TaskGroup() as tg:
                self._tg = tg  # [dev 훅] 가짜 비버 태스크를 같은 그룹에 붙이기 위해
                # ⭐ 통화 시계 — 플랜 시간이 되면 작별하고 닫는다(정상 종료).
                tg.create_task(self._watch_call_clock(), name="cascade-clock")
                if self._call_id is not None:
                    # ⭐ 통화중 점진 저장 — 통화가 죽어도 그때까지의 기록은 남는다(Live 와 같다).
                    tg.create_task(self._flush_loop(), name="cascade-flush")
                tg.create_task(self._pump_in(stream, pending_audio))
                tg.create_task(self._pump_stt(stream))
                tg.create_task(self._pump_turn())
                # ⭐ 선톡 — 비버가 먼저 인사한다(Live 와 같은 규약: call_session.py:1574).
                #   안 하면 둘 다 서로 말하기를 기다려 통화가 조용히 멈춘다. 덤으로 콜드
                #   스타트를 흡수한다: 실측에서 첫 대답만 9971ms 였고 그다음은 2.6~3.0초였다.
                #   사용자가 마이크를 허용하고 자세를 잡는 사이에 그 10초가 인사말에 실린다.
                if settings.CASCADE_GREETING:
                    # ⭐ **배우는 언어를 넘긴다**(2026-08-15). 인자를 안 넘기면 `seed_opening`
                    #   기본값이 "한국어"라, 다른 언어를 배우는 학습자에게도 "한국어 공부할래?"
                    #   로 첫인사를 했다. Live 는 넘기고 있었다(`call_session.py:1091`).
                    await self._start_reply(
                        seed_opening(self._target_label), is_greeting=True
                    )
        except* _Stop:
            pass  # 정상 종료(클라 stop / 스트림 끝 / disconnect)
        except* TimeoutError:
            # ⛔ **백스톱으로 닫혔다는 사실이 보여야 한다** — 정상 종료와 구분이 안 되면
            #   "왜 끊겼지"를 로그로 못 가른다. 그리고 이 줄이 뜨는 것 자체가 결함 신호다
            #   (정상 통화는 여기 절대 안 닿는다).
            logger.warning(
                "cascade ⚠ 세션 절대 백스톱 발동(%.0f초) — 펌프가 멈췄거나 종료 신호를 못 받았다."
                " 정상 통화는 여기 안 닿는다. 턴=%d",
                backstop_s, self._turn_seq,
            )
            self._end_reason = "backstop"
            await self._safe(ServerError(code="cascade_session_timeout",
                                         message="session_backstop", recoverable=False))
        except* Exception as eg:  # noqa: BLE001
            self._log_pump_errors(eg)
            await self._safe(ServerError(code="cascade_error", message="cascade_stream_error"))
        finally:
            self._tg = None
            # ⛔ 남은 힌트 태스크를 전부 취소한다 — 통화가 끝났는데 힌트가 늦게 나가면
            #   닫힌 소켓에 쓰거나(무해하지만) 다음 통화와 헷갈린다.
            for task in list(self._hint_tasks):
                task.cancel()
            try:
                await stream.close()
            except Exception:  # noqa: BLE001
                pass
            # ⭐ 원가 한 줄. **stream.close() 뒤**에 걷는다 — 마지막 스트림이 닫히면서
            # 그 스트림의 과금 계측이 세션 누계로 넘어오기 때문이다(core/stt.py _absorb_usage).
            # 계측 전 구간이 예외를 흡수하므로 여기서 통화가 죽을 일은 없다(R5).
            self.usage.record_stt(stream, stt_mod.stt_v2_engine_name())
            duration_s = time.monotonic() - self._t0
            summary = log_usage_summary(
                self.usage, duration_s=duration_s, turns=self._turn_seq,
            )
            self._log_bargein_summary()
            # ⭐ 통화 기록 마무리 — 남은 세그먼트 + 원가 + 상태 전환(Live 와 같은 함수).
            #   ⛔ **원가 요약은 한 번만 만든다**: 로그로 나간 그 객체를 그대로 저장한다.
            #     두 번 계산하면 로그와 DB 의 숫자가 갈린다(그러면 어느 쪽도 못 믿는다).
            await self._finalize_call(summary, duration_s)
            # ⭐⭐ **모든 정상 종료 경로**가 여기를 지난다(사용자 종료·시간 만료·백스톱).
            #   ⛔ 순서가 중요하다 — `call_id` 는 위 마감이 끝나야 확정이다. 먼저 보내면
            #     빈 값이 나가고, 클라는 그걸로 결과 화면을 못 연다.
            #   ⚠ 예전엔 **시간 만료 때만** 나갔다. 사용자가 끊으면 클라가 call_id 를 몰라
            #     `GET /calls` 를 5회×600ms 폴링해 되짚고 있었다(3초 헛돌고 오탐 위험).
            await self._safe(CascadeCallEnded(
                call_id=str(self._call_id) if self._call_id is not None else None,
                reason=self._end_reason,
            ))

    # ── ① client → STT ──
    async def _pump_in(self, stream: Any, pending_audio: bytes | None) -> None:
        if pending_audio:
            await stream.push_audio(pending_audio)
        while True:
            inb = await self.transport.receive()
            if inb.kind == "disconnect":
                # 소켓이 이미 끊겼다 — 통지는 못 가지만(그래서 _safe 가 삼킨다) 사유는 남긴다.
                self._end_reason = "disconnect"
                raise _Stop
            if inb.kind == "control":
                ctrl = inb.control or {}
                ctype = ctrl.get("type")
                if ctype == "stop":
                    # ⭐ **사용자가 종료 버튼을 눌렀다** — 실사용의 대부분이 이 경로다.
                    self._end_reason = "client"
                    raise _Stop
                if ctype == "ping":
                    await self._safe(ServerPong(t=ctrl.get("t")))
                elif ctype == "route_change":
                    self._on_route_change(ctrl)
                elif ctype == "playback_progress":
                    await self._on_playback_progress(ctrl)
                elif ctype == "client_timing":
                    self._on_client_timing(ctrl)
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
                self._check_audio_clock()
                if self._turn_id is not None and self._call_id is not None:
                    # ⭐ 턴이 열려 있는 동안만 모은다 — 통화 내내 모으면 침묵까지 저장한다.
                    #   ⚠ 통화 기록을 남길 때만(call_id 있음) 모은다: 데모는 메모리만 먹는다.
                    self._cur_user_pcm.extend(inb.audio)
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
                d for d in (self._close_at, self._turn_deadline, self._turn_idle_at,
                            self._bargein_at)
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
                # 보류 유효기간이 끝났다 = **전사가 끝내 안 왔다** → 무조건 기각.
                # ⛔ 예전엔 여기서 `_speech_active` 면 "안전망"으로 **끊었다.** 없앴다 —
                #   전사 없이 끊은 그 판단이 2026-08-10 통화를 죽였다(rms 0.0077 회색지대).
                #   사장님 규칙 그대로다: **글자로 인식할 때만 끊는다.**
                if self._bargein_at is not None and woke >= self._bargein_at - _DEADLINE_EPS_S:
                    self._bargein_at = None
                    self._note_bargein("보류만료", self._bargein_pending_rms)
                    logger.info(
                        "cascade barge-in 기각 — 전사가 안 왔다(잡음 추정, rms=%.4f)",
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
                # STT 가 통째로 조용하다 — 침묵/전사 타이머는 **걸릴 기회조차 없었다.**
                if self._turn_idle_at is not None and woke >= self._turn_idle_at - _DEADLINE_EPS_S:
                    await self._close_turn("stt_idle")
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
                self._end_reason = "stream_end"
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
          ③ barge-in 에너지 게이트(`_rms_at` 이 그 오프셋 부근에서 에너지를 찾는다 — 엉뚱한
             지점을 보면 진짜 발화가 조용한 것으로 잡힌다)
        ⚠ 예전엔 ③ 자리에 최소 지속 게이트가 있었다. 그 관문은 2026-08-14 에 삭제했다.
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
            self._end_reason = "error"
            raise _Stop

    # ── 전이 ──
    async def _on_speech_begin(self, event: SttV2Event) -> None:
        # ⚠ `_mark_voice` 가 오프셋을 덮으므로 **직전 값을 먼저** 집는다(이어붙임 판정 재료).
        prev_offset_ms = self._last_speech_end_offset_ms
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
            elif self._is_same_utterance(event, prev_offset_ms):
                # ⭐ 벤더 VAD 가 **한 발화를 쪼갠 것**이다(실측 간격 84ms). 여기서 카운트다운을
                #   안 풀면 앞 조각만으로 턴이 닫히고, 뒤 조각이 새 턴이 되어 **만들던 대답을
                #   버리고 다시 만든다** — 그게 "응답이 느리다"의 정체였다.
                self._close_at = None
                logger.info(
                    "cascade 같은 발화 이어짐(간격 %dms) — 턴 %s 를 계속 연다",
                    max(0, event.offset_ms - prev_offset_ms), self._turn_id,
                )
            return
        carry = self._carry_over_text(event)
        await self._open_turn(event.at)
        self._turn_carry = carry

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
            if self._speech_end_at:
                # ⭐ **speech_end 뒤 첫 최종 전사**만 잰다 — 우리가 기다리는 게 그것이다.
                #   재고 나면 출발점을 지운다(뒤따르는 최종 전사가 낡은 기준으로 재지 않게).
                self._final_lag_ms = int(max(0.0, event.at - self._speech_end_at) * 1000)
                self._speech_end_at = 0.0
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
        # 전사 확정 지연의 **출발점**. 여기부터 최종 전사가 도착하기까지가 그 값이다.
        self._speech_end_at = event.at
        if event.offset_ms >= 0:
            # 이어붙임의 기준점 — 벤더가 **말이 끝났다고 선언한** 오디오 시각.
            self._last_speech_end_offset_ms = event.offset_ms
            # ⭐ 턴 종료 시계의 **앵커**. 위와 값은 같아도 수명이 다르다(이건 턴과 함께 죽는다).
            if self._turn_id is not None:
                self._turn_end_offset_ms = event.offset_ms
        self._mark_voice(event)
        # ⛔ **턴이 열려 있나만 본다.** 예전엔 `state == USER_SPEAKING` 도 요구했는데,
        #   `_open_turn` 은 비버가 말하는 중이면 **일부러 상태를 안 뺏는다**(barge-in 겹침 허용).
        #   그래서 barge-in 이 켜진 상태에서 열린 턴 — 즉 **주 경로** — 은 여기서 조기 반환돼
        #   `_close_at` 이 한 번도 안 걸렸다. 글자가 끝내 안 나온 턴은 30초 상한까지 갔다.
        #   ⭐ 이 함수가 묻는 것은 "누가 말하는가"가 아니라 **"닫을 턴이 있는가"** 다.
        if self._turn_id is None:
            return  # 열린 턴이 없으면 무시(스트림 시작 직후의 잔여 이벤트 등)
        self._arm_close_timer(event)

    def _is_same_utterance(self, event: SttV2Event, prev_offset_ms: int) -> bool:
        """이 발화 시작이 **직전 발화 끝에 이어지는가**(= 벤더 VAD 가 쪼갠 한 문장인가).

        ⛔ 기준점은 **speech_end 오프셋뿐**이다(호출부가 넘긴다). 진짜 갈림은 벤더가
          `speech_stopped → speech_started` 쌍을 낸 자리이고, **잡음은 그 짝이 없다** —
          전사만 온 뒤 튀는 speech_begin 을 이어짐으로 보면 카운트다운이 매번 풀려
          차 안에서 턴이 영영 안 닫힌다(그 회귀가 이 조건을 지킨다).
        ⛔ 판정은 **오디오 시각**으로만 한다. 도착 시각(벽시계)은 벤더 지연이 섞여 못 쓴다 —
          실측에서 두 번째 조각의 speech_started 가 오디오상 84ms 뒤인데 **벽시계로는 453ms**
          뒤에 왔다. 그걸로 재면 잡음과 구분이 안 된다.
        ⛔ 오프셋이 없으면(페이크·구 엔진) **잇지 않는다** — 모르는 값으로 발화를 합치면
          진짜 두 번째 발화를 삼킨다(그게 더 나쁘다).
        """
        gap_max = max(0, settings.CASCADE_SPEECH_MERGE_GAP_MS)
        if gap_max <= 0 or event.offset_ms < 0 or prev_offset_ms < 0:
            return False
        gap = event.offset_ms - prev_offset_ms
        return 0 <= gap <= gap_max

    def _carry_over_text(self, event: SttV2Event) -> str:
        """방금 닫힌 턴이 **같은 발화의 앞부분**이면 그 텍스트를 새 턴으로 넘긴다.

        ⛔ 안 넘기면 비버가 **문장의 뒤 절반에만** 답한다("영 한국어 공부하자" 만 보고 답).
          실통화에서 만들던 대답을 버리고 새로 만든 그 대답이 정확히 그 상태였다.
        ⚠ 조건은 셋 다 만족해야 한다: 방금 닫혔다(유예 창) · 텍스트가 있었다 ·
          오디오 간격이 이어붙임 범위 안이다.
        """
        if not self._closed_text or self._closed_at <= 0.0:
            return ""
        if (time.monotonic() - self._closed_at) * 1000.0 > max(0, settings.CASCADE_STALE_FINAL_MS):
            return ""
        if not self._is_same_utterance(event, self._last_speech_end_offset_ms):
            return ""
        logger.info(
            "cascade 같은 발화 이어붙임(간격 %dms) — 직전 턴 %s 의 말을 새 턴 앞에 붙인다",
            max(0, event.offset_ms - self._last_speech_end_offset_ms), self._closed_turn_id,
        )
        return self._closed_text

    def _is_stale_tail(self, event: SttV2Event) -> bool:
        """방금 닫은 턴의 **꼬리**인가 — 그렇다면 새 턴을 열지 않는다(유령 턴 차단).

        왜 필요한가: 턴은 서버 타이머가 닫는데, 그 발화의 최종 전사가 **닫힌 뒤에** 도착할
        수 있다(파이프라인 지연·롤오버 재인식). 그걸 그대로 받으면 IDLE 상태라 새 턴이
        열리고, 같은 말이 턴 2개가 된다 — 비버가 같은 말에 두 번 대답한다.

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
        """마지막 음성 활동 지점을 오디오 시각으로 기록 + 파이프라인 지연 계측.

        ⭐ **STT 무활동 감시도 여기서 갱신한다** — 이벤트 종류를 가리지 않는다(begin/전사/end).
          우리가 알고 싶은 건 "STT 가 살아 있나"이지 "무슨 이벤트인가"가 아니다.
        """
        self._arm_idle_watchdog()
        self._last_voice_at = event.at
        if event.offset_ms >= 0:
            self._last_voice_offset_ms = event.offset_ms
            # 여기 도착하는 오프셋은 _sanitize_offset 을 이미 통과했다(상식 밖 값은 -1 로
            # 걸러져 이 분기에 들어오지 않는다) — 그래서 이 지연값은 계측으로 믿을 수 있다.
            self._pipeline_lag_ms = int(max(0.0, self._audio_ms - event.offset_ms))

    def _check_audio_clock(self) -> None:
        """⭐⭐ **우리가 가정한 오디오 규격이 맞나** — 통화당 한 번, 틀렸을 때만 시끄럽다.

        ⛔ 왜 필요한가(2026-08-13, 반나절): `stt_audio_s/dur_s` 가 2.00배로 나와서 "에뮬
          마이크가 2배"라는 결론까지 갔는데, 프론트가 HAL 에서 재니 1.002배였다. 규격이
          틀리면 **오디오 초·턴 타이머·barge-in 최소지속·원가 초가 전부 같이** 틀어지는데,
          서버 로그만 보면 영원히 모른다. 판정은 간단하다: **실시간 마이크는 실시간보다
          빠를 수 없다.** 넘으면 우리 자(레이트·채널)가 틀린 것이다.
        ⚠ 이 검사는 값을 **고치지 않는다** — 고칠 쪽은 클라이고, 서버는 드러내기만 한다.
        """
        if self._audio_clock_warned or self._audio_ms < _AUDIO_CLOCK_MIN_MS:
            return
        elapsed_ms = (time.monotonic() - self._t0) * 1000.0
        if elapsed_ms <= 0:
            return
        ratio = self._audio_ms / elapsed_ms
        if ratio <= _AUDIO_CLOCK_RATIO_MAX:
            return
        self._audio_clock_warned = True
        logger.warning(
            "cascade ⚠ 오디오 시계가 안 맞는다: 받은 오디오 %.1f초 / 통화 경과 %.1f초 = %.2f배 "
            "(가정 %dHz/%dch %s). 실시간보다 빠를 수 없다 — 규격 가정이 틀렸을 가능성이 크다. "
            "오디오 초·턴 타이머·barge-in·원가가 **같이** 틀어진다",
            self._audio_ms / 1000.0, elapsed_ms / 1000.0, ratio,
            self._sample_rate, self._channels,
            "클라 선언" if self._rate_declared else "서버 가정(클라 미전송)",
        )

    def _arm_idle_watchdog(self) -> None:
        """열린 턴의 **무활동 마감**을 지금부터 다시 센다(턴이 없으면 아무것도 안 한다)."""
        if self._turn_id is None:
            self._turn_idle_at = None
            return
        self._turn_idle_at = time.monotonic() + max(0.5, settings.CASCADE_TURN_IDLE_S)

    def _note_first_sound(self, turn_id: str | None, first_sound_ms: int) -> None:
        """이 비버 턴의 **서버 첫소리**를 보관한다 — 클라 계기와 조인할 유일한 키다.

        ⛔ 로그로만 찍고 버리면 조인이 **불가능**하다. 클라 메시지는 대답이 끝난 **뒤에**
          오므로, 그때 서버 값이 남아 있어야 뺄셈이 성립한다.
        ⚠ 상한을 둔다 — 15분 통화의 모든 턴을 들고 있을 이유가 없다(클라 계기는 곧 온다).
        """
        if not turn_id or first_sound_ms < 0:
            return
        self._first_sound_ms[turn_id] = first_sound_ms
        while len(self._first_sound_ms) > _FIRST_SOUND_HISTORY:
            self._first_sound_ms.pop(next(iter(self._first_sound_ms)))

    def _server_first_sound(self, turn_id: str) -> int:
        """그 턴의 **서버 첫소리 ms** — 없으면 **지금 계산해서라도** 낸다.

        ⛔⛔ 이 폴백이 이 기능의 전부다(2026-08-15). 원래는 `_note_first_sound` 가 채워 두길
          기대했는데, 그건 **묶음을 다 보낸 뒤**에야 돈다(`_speak_prepared` 가 실시간 페이싱으로
          재생 길이만큼 걸린다). 클라 계기는 **첫 소리에** 오므로 그때는 항상 비어 있었다 —
          실통화에서 짝없음이 **100%** 였던 이유다. 값이 없어서가 아니라 **아직 안 적혔을 뿐**이다.
        ⇒ 대답 시작 시각과 원장의 첫 오디오 시각이 둘 다 있으면 그 자리에서 뺀다.
        """
        cached = self._first_sound_ms.get(turn_id, -1)
        if cached >= 0:
            return cached
        began = self._reply_began_at.get(turn_id, 0.0)
        first_at = self.beaver.first_audio_at_of(turn_id)
        if began <= 0.0 or first_at <= 0.0:
            return -1
        return int(max(0.0, first_at - began) * 1000)

    def _on_client_timing(self, ctrl: dict) -> None:
        """클라가 **실제로 들린 시각**을 보내 왔다 — 서버 값과 빼서 **한 줄로** 남긴다.

        ⭐⭐ 이 뺄셈이 목적이다: `클라몫 = 들림 − 서버첫소리`. 오늘까지 이 값을 추정만 하고
          **한 번도 못 쟀다**(표본이 사장님 손에 달려 있었다). 이제 통화마다 자동으로 쌓인다.
        ⛔ 뺄셈을 **사람이 하게 두지 않는다.** 두 숫자만 찍으면 로그를 읽을 때마다 손으로
          빼야 하고, 그러면 아무도 안 본다.
        ⚠ R5: 여기서 나는 어떤 실패도 통화를 죽이지 않는다. **계측이 통화를 죽이면 안 된다.**
        """
        try:
            payload = ClientCascadeTiming.model_validate(ctrl)
        except Exception as exc:  # noqa: BLE001 - 구버전·깨진 메시지도 통화는 산다(R5)
            logger.info("cascade 클라계기 무시(해석 실패) — %s", str(exc)[:120])
            return
        turn_id = payload.turn_id.strip()
        server_ms = self._server_first_sound(turn_id) if turn_id else -1
        # ⛔ **조용히 버리지 않는다.** 짝을 못 찾은 것도 사실이고, 그 사실이 안 보이면
        #   "값이 안 쌓인다"를 원인 없이 겪는다(오늘 여러 번 밟은 계열이다).
        if server_ms < 0:
            logger.info(
                "cascade 클라계기 짝없음: turn=%s 들림=%dms — 그 턴의 서버 첫소리가 없다"
                "(취소된 턴이거나 소리가 안 나갔거나 id 가 어긋났다)",
                turn_id or "미상", payload.audible_ms,
            )
            return
        if payload.audible_ms < 0:
            logger.info("cascade 클라계기 무시: turn=%s 들림 값이 없다(구버전 클라)", turn_id)
            return
        logger.info(
            "cascade 클라계기: %s 들림=%dms%s 서버첫소리=%dms 클라몫=%dms "
            "(쿠션 %s · turn_start %s · %s)",
            turn_id, payload.audible_ms,
            # ⭐ **사용자가 입을 연 순간부터** 잰 값 — 사장님이 실제로 기다리는 시간이다.
            #   ⚠ 못 쟀으면(-1) 이 칸을 **아예 안 찍는다**. 없음과 0 은 다르다.
            " 말시작→첫소리=%dms" % payload.speech_to_sound_ms
            if payload.speech_to_sound_ms >= 0 else "",
            server_ms, payload.audible_ms - server_ms,
            "%d" % payload.cushion_ms if payload.cushion_ms >= 0 else "-",
            "%d" % payload.turn_start_ms if payload.turn_start_ms >= 0 else "-",
            # ⚠ 추정치가 실측과 같은 표에 섞이면 안 된다 — 줄에서 바로 갈리게 한다.
            "⚠추정" if payload.estimated else "실측",
        )

    def _stt_profile(self) -> _SttProfile:
        """이 통화에서 **실제로 돈** STT 엔진의 성질(설정이 아니라 스트림이 말한 값)."""
        return _stt_profile_for(self._stt_vendor)

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
        그러면 비버가 같은 말에 두 번 대답한다.

        그래서 바닥(CASCADE_TURN_MIN_WAIT_MS)을 둔다 — 지연이 임계를 넘어도 **최종 전사가
        도착할 시간은 항상 남긴다.** 지연이 임계보다 작을 때의 동작은 예전과 같다.

        ⭐⭐ **2026-08-13: 그 부결의 근거가 다른 벤더 것이었다.** 위 723~870ms 는 Google STT v2
        실측이고, `core/openai_stt.py` 는 그 **사흘 뒤**에 태어났다(92d8c10). 지금 기본 엔진에서
        다시 재니(408초 실통화 15표본) **중앙값 320ms · p95 430ms** 였다 — VAD 지연(중앙 170ms)
        보다 크지만 **바닥값으로 덮을 수 있는 크기**다. ⇒ 그 엔진에서만 앵커를 켠다.
        ⛔ 켜는 판단은 **엔진 성질 표**(`_SttProfile`)가 한다. 이름으로 분기하지 않는다.

        ⚠ **전사 이벤트도 같은 앵커를 쓴다.** 안 그러면 이득이 0 이다: 이 엔진의 전사에는
          오프셋이 없어서, 최종 전사가 도착할 때 시계가 **거기서 다시 800ms** 로 밀린다 —
          speech_end 에서 아무리 앞당겨도 뒤에 온 전사가 도로 늦춘다. 우리가 기다리는 침묵은
          **말이 끝난 지점**부터지 전사가 도착한 시각부터가 아니다.
        """
        profile = self._stt_profile()
        already_ms = 0.0
        anchor_ms, by_vad = -1, False
        if event.offset_ms >= 0 and event.kind == TRANSCRIPT:
            anchor_ms = event.offset_ms
        elif profile.anchor_on_speech_end:
            if event.kind == SPEECH_END and event.offset_ms >= 0:
                anchor_ms, by_vad = event.offset_ms, True
            elif self._turn_end_offset_ms >= 0:
                # 이 턴에서 벤더가 알려준 **말 끝난 지점**(전사 이벤트가 여기로 온다).
                anchor_ms, by_vad = self._turn_end_offset_ms, True
        if anchor_ms >= 0:
            already_ms = max(0.0, self._audio_ms - anchor_ms)
        threshold_ms = self._silence_ms if silence_ms is None else max(0, silence_ms)
        remain_s = max(0.0, (threshold_ms - already_ms) / 1000.0)
        floor_ms = max(0, settings.CASCADE_TURN_MIN_WAIT_MS)
        # ⛔ 엔진 바닥값은 **최종 전사를 아직 기다리는 동안만** 건다. 글자가 이미 손에 들어온
        #   뒤에는 더 기다릴 이유가 없다 — 거기서도 걸면 앵커로 번 시간을 바닥이 도로 먹는다.
        if profile.anchor_on_speech_end and not (event.kind == TRANSCRIPT and event.is_final):
            floor_ms = max(floor_ms, profile.final_after_end_ms)
        wait_s = max(remain_s, floor_ms / 1000.0)
        # ⭐ **앵커가 실제로 번 시간.** 예측(산수 320ms)을 적어 두고 검증하지 않으면 오늘 우리가
        #   반복한 그 실수가 된다 — 값이 0 이면 앵커는 켜졌는데 아무것도 못 벌고 있는 것이다.
        self._anchor_saved_ms = (
            int(max(0.0, threshold_ms / 1000.0 - wait_s) * 1000) if by_vad else 0
        )
        self._close_at = time.monotonic() + wait_s

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
        self._turn_carry = ""        # 호출부가 이어붙임이라고 판단하면 연 뒤에 채운다
        self._turn_began_at = at
        self._speech_end_at = 0.0
        self._final_lag_ms = -1      # 턴마다 새로 잰다(앞 턴 값이 새 턴에 묻어가면 안 된다)
        self._turn_end_offset_ms = -1
        self._anchor_saved_ms = 0
        self._finals = []
        self._partial = ""
        self._close_at = None
        self._turn_deadline = at + max(5, settings.CASCADE_TURN_MAX_S)
        # ⛔ **여는 순간 마감 시계를 건다.** 침묵·전사 타이머는 각각 speech_end·전사가 와야
        #   걸리는데, STT 가 조용해지면 **둘 다 안 온다**. 그때 유일하게 남는 시계다.
        self._turn_idle_at = time.monotonic() + max(0.5, settings.CASCADE_TURN_IDLE_S)
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
        if self._turn_carry:
            # 벤더 VAD 가 쪼갠 앞 조각을 도로 붙인다 — 비버는 **문장 전체**에 답해야 한다.
            text = f"{self._turn_carry} {text}".strip()
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
        # ⭐ `전사확정=` 은 **speech_end 도착 → 최종 전사 도착**이다(2026-08-13). 앵커를 VAD 로
        #   옮길 수 있는지가 이 값 하나에 달렸다 — 옮기면 안전망이 바닥값 하나뿐이라
        #   **바닥 ≥ 이 지연(p95)** 이어야 한다. -1 은 못 쟀다는 뜻이다(0 이 아니다).
        #   ⚠ 같은 줄의 `pipeline_lag_ms` 와 짝이다: 지금 STT(openai)는 전사에 offset 을 안
        #     실으므로 그 값은 **순수 VAD 지연** = 앵커를 옮겼을 때 뺄 몫이다.
        logger.info(
            "cascade turn: %s/%s reason=%s speech_ms=%d silence_ms=%d pipeline_lag_ms=%d "
            "전사확정=%dms 앵커절약=%dms 열림=%.1f초전 마지막음성=%.1f초전 미완=%s text=%r",
            self._sid, self._turn_id, reason, speech_ms, self._silence_ms, self._pipeline_lag_ms,
            self._final_lag_ms, self._anchor_saved_ms,
            max(0.0, now - self._turn_began_at),
            max(0.0, now - self._last_voice_at) if self._last_voice_at else -1.0,
            "yes" if _looks_unfinished(text) else "no", text,
        )
        # ⭐ 통화 기록(사용자) — Live 와 같은 계약. 텍스트가 **확정된 자리**에서 남긴다.
        #   ⛔ 빈 턴은 안 남긴다(위 로그가 '빈 턴'으로 따로 남는다). 오디오는 여기서 비운다 —
        #     안 비우면 다음 턴에 남의 소리가 붙는다.
        user_pcm, self._cur_user_pcm = bytes(self._cur_user_pcm), bytearray()
        if text:
            self._add_segment("user", text, user_pcm)
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
        self._turn_idle_at = None
        self._finals = []
        self._partial = ""
        # 비버가 말하는 중이었다면 그 상태를 그대로 둔다(위 _open_turn 과 같은 이유).
        if self.state == TurnState.USER_SPEAKING:
            self.state = TurnState.IDLE
        # ⭐ 여기서 비버가 대답한다(P1). ⛔ 빈 텍스트면 부르지 않는다 — 빈 입력 LLM 호출은
        # 원가만 나가고 헛대답을 만든다(결함 C 판단, 2026-08-07).
        self._settle_cancelling()
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

        살아 있는 관문(설계 §3 + 이후 실측):
          ⓪ 비버가 실제로 들렸나(CASCADE_BARGEIN_MIN_AUDIBLE_MS)
          ① 에너지 — 잔여 에코 2차 방어(AEC 를 선언한 세션에서는 안 돈다)
          ② confirm=transcript — 비어있지 않은 전사가 최소 글자수 이상 나와야 인정
        ⛔ 예전 "최소 지속" 관문은 **삭제했다**(2026-08-14) — 아래 그 자리의 주석을 봐라.
        세션 단위 값이라 `start.aec` 힌트로 기기·라우트마다 다르게 잡는다(이어폰이면 immediate).
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
            #   (_pending_user_texts 대기열).
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
        # ⛔⛔ **② 최소 지속 관문은 2026-08-14 에 삭제했다. 0 으로 끈 게 아니라 지웠다.**
        #   되살리고 싶어지면 아래 셋을 먼저 읽어라 — 셋 다 그때도 유효하다:
        #   1. **논리적 잉여다.** 아래 ③ 전사 2글자 확인이 "사람이 말했다"를 더 직접 증명한다.
        #      에코는 소리는 내도 **글자를 못 만든다.**
        #   2. 🔴 **그 확인을 무력화했다.** 이 관문이 ③보다 **먼저** 돌고 `return False` 하면
        #      `_bargein_at` 이 안 잡힌다 ⇒ 나중에 두 글자가 와도 **끊을 대상이 없다.**
        #      앞 관문이 뒤 관문을 죽이는 구조였다.
        #   3. **이름과 실제가 달랐다.** `audio_ms − offset < min_ms` 는 "사용자가 400ms
        #      말했다"가 아니라 **"speech_begin 이후 오디오가 400ms 흘렀다"** 다 — 발화 길이가
        #      아니라 파이프라인 흐름을 재고 있었다.
        #   ⭐ 코드가 이미 절반은 인정하고 있었다: 못 잴 때는 통과시키고 전사 확인에 맡기면서,
        #     잴 수 있을 때만 기각했다 — 논리가 반쪽만 적용돼 있었다.
        #   ⚠ 실측 12시간 `기각-지속` **0건**. 해를 끼치기 전에 지운 것이다.
        #   ⇒ **되살리려면 ③ 전사 확인 뒤로 옮겨라.** 앞에 두면 같은 무력화가 재발한다.
        # ③ 전사 확인 — **설계에 있다고 적어 두고 P1 에서 안 붙였던 관문**이다(2026-08-07).
        #   그동안 barge-in 은 에너지+지속만으로 발동했고, 기침·키보드·숨소리가 전부 통과했다.
        #   여기서 True 를 돌려주면 즉시 취소되므로, transcript 모드는 **판정을 미룬다**:
        #   전사가 오거나(_on_transcript) 음성이 길게 이어지면(_pump_turn 타이머) 그때 친다.
        if self._bargein_confirm == "transcript":
            self._bargein_pending_at = time.monotonic()
            self._bargein_pending_rms = rms
            self._bargein_at = (
                self._bargein_pending_at + max(0, settings.CASCADE_BARGEIN_PENDING_MS) / 1000.0
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
        """보류해 둔 barge-in 을 확정한다 — **전사가 왔을 때만 여기 온다.**

        ⭐ 확정 줄에 **보류→확정 ms 와 그때 에너지**를 같이 남긴다(2026-08-08). 없을 때는
          두 줄의 타임스탬프를 사람이 손으로 빼서 "전사가 얼마나 빨리 이기나"를 증명해야
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
        self._note_bargein("전사확정", rms)
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
                if not self._pending_user_texts:
                    # ⚠ 대기 시간은 **첫 발화 기준**이다 — 뒤에 온 것으로 갱신하면
                    #   "얼마나 기다렸나"가 짧게 나와 지연을 못 본다.
                    self._pending_since = time.monotonic()
                self._pending_user_texts.append(user_text)
                logger.info(
                    "cascade 발화 대기열(%d건) — 비버가 말하는 중이라 대답 뒤로 미룬다: %r",
                    len(self._pending_user_texts), user_text[:40],
                )
                return
        self.state = TurnState.THINKING
        self._reply_seq += 1
        self._reply_task = self._tg.create_task(
            self._run_reply(user_text, is_greeting, self._reply_seq)
        )

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
        self._pending_user_texts.clear()

    async def _run_reply(self, user_text: str, is_greeting: bool = False,
                         seq: int = 0) -> None:
        """사용자 발화 1건에 대한 비버의 대답 — LLM 스트리밍 → 문장 분할 → TTS → 송출."""
        self._reply_cancelled = False
        # ⛔ **대답마다 비운다.** 안 비우면 이전 턴의 엔진·마커 집계가 누적돼 로그가
        #   `tts=chirp+gemini` 처럼 섞여 찍히고, **어느 엔진이 낸 소리인지 못 가린다**
        #   (A/B 판정이 오염된다 — 2026-08-07 실측 로그에서 그 증상이 나왔다).
        self._tts_engines.clear()
        self._marker_seen.clear()
        self._reply_spans.clear()
        self._sentence_markers = 0
        self._dropped_tag = ""
        self._tts_odd_chunks.clear()
        self._tts_trims.clear()
        self._tts_waits.clear()
        self._tts_leads.clear()
        self._marker_drifts.clear()
        self._marker_emotions.clear()
        self._batch_leads.clear()
        self._lang_fixes = 0
        self._tts_ttfb_ms = -1      # 이 대답의 **첫** 합성 요청이 첫 오디오를 받기까지
        self._tts_asked_at = 0.0    # 그 요청을 **건** 시각(둘 사이가 벤더 왕복이다)
        # ⭐ 이 대답의 감정(대답 1건당 **하나**). 문장마다 바꾸면 구간이 쪼개져 TTS 호출이
        #   늘고, 분당 상한이 10 인데 대답 하나가 이미 3~6회다(429 가 1순위 제약이다).
        self._reply_emotion = None
        self._reply_rates = {}
        chat = gemini_chat.open_chat_stream(
            self._llm_client,
            settings.CASCADE_LLM_MODEL,
            system_instruction=self._system_instruction(),
            history=self._history,
            user_text=user_text,
            thinking_budget=settings.CASCADE_LLM_THINKING_BUDGET,
            max_output_tokens=settings.CASCADE_LLM_MAX_OUTPUT_TOKENS,
        )
        if chat is None:
            self.state = TurnState.IDLE
            return
        self._history.append({"role": "user", "text": user_text})
        buffer = SentenceBuffer()
        turn_id: str | None = None
        spoken_chars = 0
        began = time.monotonic()
        self._reply_began = began     # 비버 턴이 열릴 때 그 턴에 묶인다(클라 계기 조인용)
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
            # ⭐ 단계 전환을 **한 번씩만** 알린다(아래 _flush_batch·llm 프레임).
            tts_announced = False
            # ⭐⭐ **배치 경계의 와이어 공백**(2026-08-13). 클라가 "SERVER GAP 2435ms
            #   mid-utterance" 를 보고했는데 **서버에 이 값을 재는 계측이 없었다.**
            #   구간 사이 공백은 `대기N.NNs` 로 보이지만, **묶음과 묶음 사이**(다음 문장들의
            #   LLM 생성 + TTS 왕복)는 아무 데도 안 남았다. 페이서엔 필러가 없어서 그 시간
            #   동안 **와이어가 그냥 조용하다** — 클라 큐가 그만큼 굶는다.
            self.beaver.wire_gaps.clear()
            self.beaver.paced_holds.clear()
            # ⭐⭐ **묶음 선행 합성**(2026-08-13, 설계 `docs/20260813_0430_…`).
            #   송출을 태스크로 돌리고 **체인으로 순서를 지킨다**. 그러면 이 루프가 LLM 을
            #   계속 읽고, 다음 묶음이 준비되는 즉시 **TTS 요청을 건다** — 앞 묶음이 재생되는
            #   동안 벤더 왕복이 끝나 있다(그 왕복이 곧 침묵이었다).
            send_task: asyncio.Task | None = None
            wasted_chars = 0        # 미리 만들었는데 못 쓴 글자(원가만 나간 몫)
            # ⛔ **"첫 묶음을 이미 보냈나"는 동기 플래그여야 한다**(2026-08-13). 예전엔
            #   `first_audio_ms < 0` 으로 판정했는데, 송출이 태스크로 빠지면서 그 값은 소리가
            #   실제로 나갈 때까지 -1 로 남는다 ⇒ **모든 문장이 "첫 문장 단독" 경로**를 타서
            #   요청이 문장 수만큼 늘어난다(429 를 부른 그 상태로 되돌아간다).
            first_batch_out = False

            async def _send_chain(prev: asyncio.Task | None, prep: _PreparedBatch) -> None:
                """앞 묶음이 끝난 뒤 이 묶음을 보낸다 — **순서는 이 체인이 지킨다**.

                ⛔ 조건 B(2026-08-13 합의): 앞 묶음이 **취소**되면 뒤 묶음도 **같이 죽는다.**
                  사용자가 끊었는데 뒤 문장이 나가면 안 된다(I3 와 같은 취지). 우연이 아니라
                  **의도**이므로 여기서 명시적으로 취소하고, 미리 연 것도 함께 버린다.
                ⛔ 조건 A: 태스크 예외는 **아무도 await 하지 않으면 조용히 사라진다.** 이건
                  곁다리가 아니라 **대답 경로**다 — 소리가 안 나가는데 로그가 조용하면 오늘
                  우리가 당한 그 유형이 또 생긴다. 반드시 남기고, **다음 묶음은 계속 간다**(R5).
                """
                nonlocal turn_id, first_audio_ms, spoken_chars, wasted_chars
                prev_done = 0.0
                if prev is not None:
                    try:
                        await prev
                    except asyncio.CancelledError:
                        prep.cancel()
                        raise                      # 뒤 묶음도 같이 죽는다(의도)
                    # ⭐ 앞 묶음이 **다 나간 시각**. 여기부터 이 묶음의 첫 소리까지가 공백이다.
                    prev_done = time.monotonic()
                try:
                    turn_id = turn_id or await self._begin_beaver_turn()
                    first_audio_at = self.beaver.first_audio_at
                    opened = prep.first          # 취소가 지우기 전에 잡아 둔다(계측용)
                    sent = await self._speak_prepared(prep)
                    self._note_batch_lead(prep, prev_done, opened)
                    if sent and first_audio_ms < 0:
                        # ⭐ 첫 바이트가 **실제로 나간 시각**을 원장에서 받는다.
                        timing.mark_audio(first_audio_at or self.beaver.first_audio_at)
                        timing.mark_request(self._tts_asked_at)
                        timing.vendor_ms = self._tts_ttfb_ms
                        timing.mark_batch(int(sent / BEAVER_BYTES_PER_MS))
                        first_audio_ms = timing.first_sound_ms
                        # ⭐ 클라 계기와 조인할 값 — **여기서 보관하지 않으면 조인이 불가능하다.**
                        self._note_first_sound(turn_id, first_audio_ms)
                    spoken_chars += len(prep.text)
                except asyncio.CancelledError:
                    prep.cancel()
                    wasted_chars += len(prep.text)
                    raise
                except InvariantError:
                    prep.cancel()
                    wasted_chars += len(prep.text)
                    logger.info("cascade 묶음 송출 중단(턴이 이미 닫힘) turn=%s", turn_id)
                except Exception as exc:  # noqa: BLE001 - R5: 이 묶음만 실패, 다음은 계속
                    prep.cancel()
                    wasted_chars += len(prep.text)
                    logger.exception("cascade 묶음 송출 실패(다음 묶음은 계속) — %s", exc)

            async def _flush_batch() -> None:
                nonlocal pending, tts_announced, send_task
                nonlocal first_batch_out
                if not pending:
                    return
                text_batch, pending = " ".join(pending), []
                first_batch_out = True
                if not tts_announced:
                    # ⭐ **LLM → TTS 전환**. 프론트가 요청한 것: 클라가 가진 값은
                    #   `mic OPEN → 다음 발화`뿐인데 거기엔 사용자 발화 시간이 섞여 있어
                    #   순수 서버 지연이 아니다. 단계가 갈려야 LLM 이 느린지 TTS 가 느린지
                    #   답할 수 있다. ⛔ 도배 금지 — 구간마다가 아니라 **전환에서 한 번**.
                    tts_announced = True
                    await self._safe(ServerBeaverPreparing(
                        stage="tts", index=1, total=0,
                        elapsed_ms=int((time.monotonic() - began) * 1000),
                    ))
                # ⭐ **여기서 TTS 요청을 건다**(앞 묶음이 아직 재생 중일 수 있다).
                prep = await self._prepare_batch(text_batch)
                prev, send_task = send_task, None
                send_task = asyncio.create_task(_send_chain(prev, prep))

            # ✓ 이건 모으는 **방식**(전체 합성 후 한 번에 낸다)이지 조절값이 아니다.
            if self._tts_engine == _GEMINI_BATCH_CHOICE:
                turn_id, first_audio_ms, spoken_chars = await self._run_batch_reply(chat, timing)
                # ⚠ 이력에는 **실제로 말한 것만**(버린 꼬리 제외).
                self._remember_beaver(turn_id, strip_emotion_tags(self._batch_spoken))
                logger.info(
                    "cascade 대답%s(배치): turn=%s %s %s %s %s 글자=%d%s 자막=%d개 "
                    "tts=%s %s %s",
                    "(선톡)" if is_greeting else "", turn_id, timing.summary(),
                    self._llm_tokens_log(chat.usage_metadata),
                    self._emotion_log(), self._rate_log(), spoken_chars,
                    "(상한잘림 꼬리%d자버림)" % len(self._batch_dropped)
                    if self._batch_dropped else ("(상한잘림)" if chat.truncated else ""),
                    self._sentence_markers,
                    "+".join(sorted(self._tts_engines)) or self._tts_vendor(),
                    self._tts_request_log(),
                    self._reading_summary(self._batch_synth_s),
                )
                return
            # ⭐ **생성 시작**을 알린다(스트리밍 경로). 예전엔 이 프레임이 배치 경로에만
            #   있어서 폰(gemini-tts=스트리밍)에는 **한 번도 안 갔다** — 프론트 실기기 0건.
            #   ⚠ 클라 변경 0: 모델·필드가 배치 때와 같다(프론트가 이미 파싱한다).
            await self._safe(ServerBeaverPreparing(
                stage="llm", elapsed_ms=int((time.monotonic() - began) * 1000),
            ))
            # ⭐ **이 WS 전송이 `LLM첫조각` 안에 들어 있다**(2026-08-15). 844ms 중 얼마인지
            #   몰라서 "LLM 이 느리다"로 뭉뚱그려 읽고 있었다. 크면 벤더가 아니라 우리 소켓이다.
            timing.mark_notified()
            # ⭐⭐ **문장 개수가 길이를 정한다**(2026-08-12 사장님 "응답은 1~4문장이야").
            #   토큰으로 자르면 문장 중간에서 끊겨 꼬리를 버렸다(call 938: 99자 = 말한 것의 4배).
            #   ⛔ 새 파서를 만들지 않는다 — **이미 쓰는 `SentenceBuffer` 를 종료 조건으로도**
            #     쓴다. 그래야 "무엇이 한 문장인가"의 출처가 하나로 남는다.
            cap = self._max_sentences()
            sentences_done, capped = 0, False
            stream = chat.chunks()
            async for piece in stream:
                timing.mark_chunk()
                # ⭐ 감정은 **대답 맨 앞 태그 하나**다. 조각 경계로 태그가 쪼개져도 누적
                #   텍스트(chat.text)에서 읽으므로 놓치지 않는다.
                if self._reply_emotion is None:
                    self._reply_emotion = detect_emotion(chat.text)
                    self._note_stray_tag(chat.text)
                for sentence in buffer.push(piece):
                    sentences_done += 1
                    timing.mark_sentence()
                    # ⚠ **첫 문장 단독**은 왕복이 짧은 엔진의 규칙이다(성질 표가 판정한다).
                    #   왕복이 길면 손해만 본다: 첫 요청에 고정 오버헤드가 통째로 붙고,
                    #   나온 오디오가 **선행버퍼보다 짧아** 재생이 먼저 바닥나 끊긴다.
                    #   ⛔ 여기가 `_gemini_realtime()` 이라 OpenAI 가 Chirp 규칙을 탔다 —
                    #     첫 배치 800·1000·1450ms 인데 버퍼는 1500ms 였다. 그 끊김이다.
                    if (not first_batch_out and not pending
                            and self._profile().solo_first_sentence):
                        pending.append(sentence)
                        await _flush_batch()        # 첫 문장 = 단독 즉시 송출
                        continue
                    pending.append(sentence)
                    if sum(len(x) for x in pending) >= self._batch_chars():
                        await _flush_batch()
                if cap and sentences_done >= cap:
                    # ⭐ 상한을 채웠다 — **남은 생성을 받지 않는다.** 원가·지연이 같이 준다.
                    #   ⚠ 이건 **생성 종료 조건**이지 발화 삭제 규칙이 아니다. 같은 조각에
                    #     N+1번째 **완결** 문장이 함께 실려 오면 그것까지 말한다 — 이미 만들어
                    #     졌고(값을 이미 냈다) 완결이라 말이 안 잘린다. 버리면 질문 하나가
                    #     통째로 사라진다.
                    capped = True
                    await stream.aclose()
                    # ⭐ 상한에서 스트림을 닫는 데 걸린 시간 — `묶음대기`(82ms) 안에 이게 얼마나
                    #   들어 있는지 몰랐다. 벤더 스트림 종료가 느리면 여기가 곧 첫소리 지연이다.
                    timing.mark_closed()
                    break
            # ⭐ **LLM 이 끝난 시각**(프론트 요청 A, 2026-08-13). 지금까지는 `llm`(시작)과
            #   `tts`(첫 합성)뿐이라 클라가 "LLM 이 얼마나 걸렸나"를 못 봤다 — LLM·TTS·STT
            #   확정 지연이 한 덩어리로 보였다. 같은 프레임·같은 필드에 **단계 이름만** 더한다
            #   (프론트는 모르는 값을 무시하므로 클라 변경 0).
            #   ⚠ 이 시각은 첫소리보다 **뒤일 수 있다** — 우리는 첫 문장이 나오는 즉시 말을
            #     시작하고 LLM 은 그 뒤로도 계속 생성한다. 그게 정상이고, 그 사실이 이 두 값의
            #     차이로 처음 보인다.
            await self._safe(ServerBeaverPreparing(
                stage="llm_done", elapsed_ms=int((time.monotonic() - began) * 1000),
            ))
            tail = buffer.flush()
            dropped = ""
            if tail and capped:
                # 상한에서 끊었을 때 남은 것은 **N+1번째 문장의 앞부분**이다 — 말하지 않는다.
                #   ⚠ 토큰 상한 때와 달리 여기서 버리는 양은 문장 시작 몇 글자다(그게 요점이다).
                dropped, tail = tail.strip(), ""
            if tail and chat.truncated and (spoken_chars or pending):
                # ⛔ 상한에 걸린 대답의 꼬리는 **미완성 문장**이다 — 말하지 않는다.
                #   (아직 아무것도 못 말했으면 그 꼬리라도 낸다 — 침묵보다 낫다.)
                dropped, tail = tail.strip(), ""
            if tail:
                pending.append(tail)
            await _flush_batch()
            # ⛔ **마지막 묶음이 다 나갈 때까지 기다린다.** 안 기다리면 아래 `turn_end` 가
            #   소리보다 먼저 나가고(I5 위반), 이력·기록도 덜 찬 값으로 남는다.
            if send_task is not None:
                try:
                    await send_task
                except asyncio.CancelledError:
                    # 체인이 취소로 끝났다 = barge-in. 아래 except 절이 처리한다.
                    if not self._reply_cancelled:
                        raise
            if turn_id is not None:
                await self.beaver.end()
            # ⚠ 이력에는 **실제로 말한 것만** 남긴다 — 버린 꼬리를 넣으면 다음 턴의 모델이
            #   자기가 하지도 않은 말을 했다고 믿는다.
            spoken_reply = strip_emotion_tags(chat.text)
            if dropped and dropped in spoken_reply:
                spoken_reply = spoken_reply[: spoken_reply.rfind(dropped)]
            self._remember_beaver(turn_id, spoken_reply)
            # ⭐ TTS 엔진을 같이 찍는다 — 이 줄만 보고 A/B(첫소리 지연)를 가를 수 있어야 한다.
            #   폴백이 일어나면 실제로 소리를 낸 엔진이 여기 남는다(의도한 엔진이 아니라).
            logger.info(
                # ⚠ `문장모델=` 은 뺐다(2026-08-13) — 설정 echo 라 **실측 가짓수 1** 이고
                #   통화 시작의 `cascade 설정:` 줄에 이미 있다. ⛔ `tts=` 는 남긴다:
                #   가짓수 1 이지만 **폴백하면 바뀐다**("지금 안 변한다"와 "영영 안 변한다"는
                #   다르다 — 폴백이 일어난 날 아무것도 안 남으면 안 된다).
                "cascade 대답%s: turn=%s %s %s %s %s 글자=%d%s 자막=%d개 tts=%s "
                "언어분할=%s gemini호출=%d %s %s %s %s %s %s %s %s %s",
                "(선톡)" if is_greeting else "", turn_id, timing.summary(),
                # ⭐ prefill 이 `LLM첫조각` 의 얼마인지는 이 값 없이는 못 본다.
                self._llm_tokens_log(chat.usage_metadata),
                self._emotion_log(), self._rate_log(), spoken_chars,
                # ⭐ **잘렸다는 사실이 보여야 한다.** 조용히 잘리면 "왜 말이 이상하지"를 못 찾는다.
                # ⭐ **끝난 이유를 구분한다.** 문장 상한은 정상 종료고, 토큰 상한은 병리다
                #   (종결부호를 영영 안 찍었다는 뜻) — 같은 표시로 찍으면 신호가 죽는다.
                ("(문장상한%d 꼬리%d자버림)" % (cap, len(dropped)) if capped else
                 "(상한잘림 꼬리%d자버림)" % len(dropped)) if dropped else
                ("(문장상한%d)" % cap if capped else
                 ("⚠토큰상한잘림" if chat.truncated else "")),
                # ⭐ **자막이 실제로 나갔나**(구간 마커 수). 0 이면 클라가 자막을 못 받는다 —
                #   지금까지 이 값이 없어 "자막이 안 뜬다"를 서버 로그로 못 갈랐다.
                #   ⚠ 아래 `언어분할=` 과 다른 것이다(그건 `__마커__` 언어 구간 상태).
                self._sentence_markers,
                "+".join(sorted(self._tts_engines)) or self._tts_vendor(),
                ",".join(f"{k}{v}" for k, v in sorted(self._marker_seen.items())) or "-",
                self._tts_gemini_calls, "고정" if self._tts_gemini_off else "-",
                self._tts_request_log(),
                self._reading_summary(None),
                # ⭐ **와이어 공백** — 프레임을 안 내보낸 구간(클라 판정과 같은 250ms 창).
                #   ⚠ 예전의 `묶음공백=` 을 이걸로 **바꿨다**: 선행 합성을 넣으면 묶음 경계의
                #     대기가 0 이 되는데 **소리는 여전히 안 나갈 수 있다** — 그러면 값만
                #     좋아진 척한다. 굶는 쪽은 클라이고 클라가 보는 건 **프레임 간격**이다.
                self._batch_gap_log(self.beaver.wire_gaps),
                # ⭐ **짝으로 읽는 값** — 여기가 크고 위가 작으면 끊긴 게 아니라 앞서 보낸 것이다.
                self._paced_log(self.beaver.paced_holds),
                # ⭐ **왜 남았나** — 위가 결과(공백)면 이건 원인이다(선행 여유 vs 벤더 왕복).
                self._lead_log(),
                # ⭐ 글자가 순번을 고친 횟수. 0 이면 마커가 지켜진 것이고, 크면 프롬프트를 봐야 한다.
                "언어보정=%d건" % self._lang_fixes if self._lang_fixes else "언어보정=-",
                # ⭐ 문장 마커 위치는 **추정**이다 — 얼마나 틀렸는지 같이 보여야 읽을 수 있다.
                self._marker_drift_log(),
                # ⚠ 미리 만들었는데 못 쓴 글자 — 선행 합성의 **대가**다(원가만 나갔다).
                "선행폐기=%d자" % wasted_chars if wasted_chars else "선행폐기=-",
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
            # ⭐ 생성 글자수를 같이 넘긴다(2026-08-13). 문장 상한에서 `stream.aclose()` 로
            #   끊은 회차는 벤더 토큰이 **영영 안 온다**(usage 는 마지막 조각에만 실린다).
            #   ⇒ 토큰은 못 받아도 **글자는 우리가 받았다** — 요약이 그걸로 메운다.
            self.usage.record_llm(
                chat.usage_metadata, vendor=settings.CASCADE_LLM_MODEL,
                out_chars=len(chat.text or ""),
            )
            self._settle_reply_state(seq)
            await self._drain_pending_user_text()

    def _settle_reply_state(self, seq: int) -> None:
        """대답이 끝났다 — **자기 세대일 때만** 상태를 IDLE 로 되돌린다.

        ⭐ **CANCELLING 도 여기서 풀린다**(2026-08-11 QA 발견2). barge-in 이 세운 그 상태를 푸는
          전이가 **하나도 없었다** — 굳으면 `_open_turn` 이 그걸 보존 목록에 두므로 이후 모든 턴이
          USER_SPEAKING 이 못 되고 발견1(침묵 타이머 부재)이 **영구화**된다.
        ⚠ 세대 번호를 보는 이유: 늦게 죽은 옛 태스크가 **새 대답의 THINKING 을 IDLE 로 덮으면**
          그것도 같은 종류의 굳음이다(비버가 말하는데 상태는 IDLE).
        """
        if seq != self._reply_seq:
            return
        if self.state in (TurnState.THINKING, TurnState.BEAVER_SPEAKING,
                          TurnState.CANCELLING):
            self.state = TurnState.IDLE

    def _settle_cancelling(self) -> None:
        """**끝난 취소**를 IDLE 로 정리한다(2층 안전망 — QA 발견2).

        1층은 대답 태스크의 finally 다. 그런데 취소할 대답이 애초에 없었거나 태스크가 다른
        경로로 사라지면 그 층이 안 돈다. 그때 CANCELLING 이 남으면:
          `_open_turn` 이 CANCELLING 을 **보존 목록**에 두므로 이후 턴이 USER_SPEAKING 이 못 되고,
          발견1(침묵 타이머 부재)이 통화 끝까지 영구화된다.
        ⛔ 조건은 **"정말 끝났나"** 다 — 대답 태스크가 죽었고 비버 턴도 없을 때만 푼다.
          아직 도는 대답이 있는데 풀면 취소 배관이 자기 상태를 잃는다(`_open_turn` 주석의 의도).
        """
        if self.state != TurnState.CANCELLING:
            return
        if self._reply_task is not None and not self._reply_task.done():
            return
        if self.beaver.turn_id is not None:
            return
        logger.info("cascade 상태 정리: CANCELLING → IDLE(취소가 끝났다)")
        self.state = TurnState.IDLE

    async def _drain_pending_user_text(self) -> None:
        """비버가 말하는 동안 들어온 발화에 **이제** 답한다 — **모아서 한 번**.

        ⛔ 여기서 안 부르면 그 발화는 영영 답을 못 받는다 — 사장님이 겪으신 그 증상이다.
        ⭐ **둘 이상이면 합쳐서 한 번만 답한다**(2026-08-12 사장님 결정 "A"). 하나씩 소비하면
          비버가 **연속 두 번** 말해 대화가 어긋난다(call 937: "안녕하세요."→b4, "여보세요?"→b5).
        ⚠ 잇는 방식은 **공백 한 칸**이다. 근거: 한 턴 안의 여러 최종 전사를 합칠 때
          `_close_turn` 이 이미 `" ".join(...)` 을 쓴다 — **같은 층의 같은 규칙**이므로
          새 규칙을 만들지 않는다. 전사에는 종결부호가 붙어 오므로("안녕하세요." "여보세요?")
          이어 붙이면 사람이 두 마디를 연달아 한 것과 같은 모양이 된다.
        ⚠ 대기열은 **비버가 말하는 동안**에만 쌓인다(대답 ~10초) — 시간이 벌어진 발화가 섞일
          여지가 구조적으로 작아, 간격 기준 분할은 두지 않았다.
        ⛔ 꺼내기는 **원자적**이다(리스트를 통째로 교체) — 남겨 두면 다음 드레인이 같은 말에
          다시 답한다(중복 응답). 두 호출 자리(_run_reply·_run_resume) 모두 이 함수를 지난다.
        """
        pending_list, self._pending_user_texts = self._pending_user_texts, []
        waited_ms, self._pending_since = self._pending_wait_ms(), 0.0
        pending = " ".join(t.strip() for t in pending_list if t.strip())
        if not pending:
            return
        self._reply_task = None      # 방금 끝난 태스크 참조를 비워야 새 대답이 시작된다
        logger.info("cascade 대기열 발화에 답한다(%d건 합쳐서, %dms 기다렸다): %r",
                    len(pending_list), waited_ms, pending[:60])
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
        self._reply_emotion = detect_emotion(chat.text)
        self._note_stray_tag(chat.text)
        timing.mark_sentence()   # 배치는 '전체 텍스트 완성'이 곧 문장 완성 시점이다
        text = strip_markers(chat.text).strip() and chat.text.strip()
        self._batch_dropped = ""
        if text and chat.truncated:
            # 배치도 같은 규칙이다 — 미완성 문장은 말하지 않는다.
            text, self._batch_dropped = self._drop_incomplete_tail(text)
        self._batch_spoken = text
        if not text:
            self._batch_synthesizing = False
            return None, -1, 0

        segments = split_by_language(
            text, self._locale, self._target_code
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
                timing.mark_request(self._tts_asked_at)
                timing.vendor_ms = self._tts_ttfb_ms
                timing.mark_batch(int(sent / BEAVER_BYTES_PER_MS))
                first_audio_ms = timing.first_sound_ms
                self._note_first_sound(turn_id, first_audio_ms)
            if sent:
                self._reply_spans.append((language, len(label), sent))
            spoken += len(seg_text)
        await self.beaver.end()
        return turn_id, first_audio_ms, spoken

    async def _synthesize_all(self, text: str, language: str, budget_s: float) -> bytes:
        """구간 하나를 **끝까지** 합성해 PCM 을 모은다(스트리밍 송출 없음)."""
        text = strip_emotion_tags(text).strip()      # ⛔ 태그는 소리로 안 나간다
        if not text:
            return b""
        chunks: list[bytes] = []
        self._tts_asked_at = self._tts_asked_at or time.monotonic()   # 배치 경로도 같은 기준자
        try:
            async with asyncio.timeout(max(1.0, budget_s)):
                stream = await tts.synthesize_stream(
                    text,
                    language=language,
                    voice=self._tts_voice(),
                    engine=tts.GEMINI_ENGINE,
                    speaking_rate=self._note_rate(language),
                    style_prompt=self._style_prompt(),
                    allow_gemini=not self._tts_gemini_off,
                )
                async for chunk in stream:
                    chunks.append(chunk)
        except asyncio.TimeoutError:
            logger.warning("cascade 배치 합성: 구간 시간 초과 — 받은 %d조각만 쓴다", len(chunks))
        self.usage.record_tts(text, vendor=self._tts_vendor())
        pcm = b"".join(chunks)
        if pcm and self._trim_silence():
            # 배치는 전량을 손에 들고 있으니 **앞뒤 다** 자른다(지연 트레이드오프가 없다).
            before = len(pcm)
            pcm = trim_silence_edges(
                pcm, keep_head_ms=max(0, settings.CASCADE_TTS_TRIM_KEEP_MS),
                keep_tail_ms=max(0, settings.CASCADE_TTS_TRIM_KEEP_MS),
            )
            if len(pcm) != before:
                logger.info("cascade 침묵 정리: %.0fms → %.0fms (구간 %d자)",
                            before / BEAVER_BYTES_PER_MS, len(pcm) / BEAVER_BYTES_PER_MS,
                            len(text))
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

    def _note_tts_request(self, language: str, chars: int, sent: int,
                          align: dict | None = None, wait_s: float = 0.0,
                          report: dict | None = None, lead_s: float = 0.0) -> None:
        """합성 요청 1건의 결과를 남긴다 — **잘렸으면 여기서 드러난다.**

        ⛔ 어댑터엔 완결성 검사가 없다. 실제로 status 200 인데 14.5초짜리 문장이 **0.30초만
          오고 조용히 끝난** 회차가 있었다(2026-08-11, 8회 중 1회, 재현 안 됨). 그런 절단이
          로그에 안 남으면 "가끔 이상하다"로 끝나고 원인을 영영 못 찾는다.
        ⚠ 판정 문턱(자/초)은 **물리적으로 불가능한 선**이다 — 실측 목표가 7.7자/초(Live 한국어),
          빠른 영어가 16~18자/초다. 여기 걸리는 건 속도가 아니라 **잘린 오디오**다.
        ⚠ core 어댑터의 로그는 Cloud Logging 에 안 남는다 — 그래서 판정도 기록도 여기서 한다.
        """
        self._reply_spans.append((language, chars, sent))
        self._tts_odd_chunks.append(int((align or {}).get("odd", 0)))
        self._tts_trims.append((int((report or {}).get("trim_head_ms", 0)),
                                int((report or {}).get("trim_tail_ms", 0))))
        self._tts_waits.append(max(0.0, wait_s))
        # ⭐ **이 요청이 얼마나 미리 나갔나**(2026-08-13). `대기` 가 결과(공백)라면 이건 원인의
        #   절반이다 — 나머지 절반은 벤더 왕복이고, 대략 `대기 ≈ 왕복 − 선행` 이다.
        #   선행이 0 에 가까우면 우리가 늦게 건 것이고, 선행이 큰데도 대기가 크면 벤더가 느린 것이다.
        self._tts_leads.append(max(0.0, lead_s))
        audio_s = audio.output_audio_s(sent)
        if chars and audio_s > 0 and chars / audio_s > _IMPOSSIBLE_CHARS_PER_S:
            logger.warning(
                "cascade ⚠ tts 응답이 너무 짧다: %d자를 %.2f초(%.0f자/초)로 받았다 — "
                "벤더가 도중에 끊었을 수 있다. 엔진=%s",
                chars, audio_s, chars / audio_s, self._tts_vendor(),
            )

    def _note_batch_lead(self, prep: "_PreparedBatch", prev_done: float,
                         opened: "_OpenSegment | None") -> None:
        """⭐⭐ **선행이 실제로 얼마나 앞섰나** — 남은 공백의 원인을 여기서 가른다(2026-08-13).

        선행 합성을 넣고도 `와이어공백` 이 1.2~1.4초 남았다. 결과(공백)만 있고 **왜 늦었는지**가
        없어서 후보 셋을 못 갈랐다:
            (a) 선행 시작이 늦다   (b) 벤더 왕복이 선행보다 길다   (c) 묶을 문장이 아직 없다
        그래서 두 숫자를 남긴다:
            여유 = (앞 묶음이 다 나간 시각) − (이 묶음 합성을 시작한 시각)
            그 여유에서 **벤더 왕복을 빼면** 곧 공백이다 ⇒ `여유 − 벤더` 가 음수면 그만큼 빈다.
        ⛔ 첫 묶음은 대상이 아니다(앞이 없다 — 그건 `첫소리` 가 담당한다).
        ⚠ 벤더 왕복을 못 받은 회차는 **버린다**(0 으로 세면 "벤더가 즉시 줬다"가 되어 원인이
          선행 쪽으로 잘못 기운다).
        """
        if prev_done <= 0.0 or prep.opened_at <= 0.0 or opened is None:
            return
        ttfb = (opened.report or {}).get("ttfb_ms")
        if ttfb is None:
            return
        lead_ms = int((prev_done - prep.opened_at) * 1000)
        self._batch_leads.append((lead_ms, int(ttfb)))

    @staticmethod
    def _llm_tokens_log(usage: Any) -> str:
        """이 대답의 **입력·캐시 토큰** — prefill 이 첫조각 지연의 얼마인지 보려면 이게 있어야 한다.

        ⛔ **없는 회차는 `-` 다.** 0 으로 찍으면 "입력이 0 이었다"는 거짓말이 되고, 그 표로
          "프롬프트는 범인이 아니다"라는 결론을 내리게 된다. 문장 상한에서 스트림을 일찍
          닫은 회차는 벤더 토큰이 **영영 안 온다**(usage 는 마지막 조각에만 실린다).
        """
        def _pick(name: str) -> str:
            value = getattr(usage, name, None) if usage is not None else None
            return str(int(value)) if isinstance(value, (int, float)) else "-"

        return "입력=%s토큰 캐시=%s토큰" % (
            _pick("prompt_token_count"), _pick("cached_content_token_count"),
        )

    def _lead_log(self) -> str:
        """묶음별 `여유−벤더`(초). 음수 = **그만큼 소리가 빈다**(선행이 왕복을 못 덮었다)."""
        if not self._batch_leads:
            return "선행여유=-"
        return "선행여유=[%s]" % ", ".join(
            "%+.2fs(선행%.2f/벤더%.2f)" % ((lead - ttfb) / 1000.0, lead / 1000.0, ttfb / 1000.0)
            for lead, ttfb in self._batch_leads
        )

    @staticmethod
    def _paced_log(holds: list[float]) -> str:
        """페이서가 **일부러** 붙든 시간. ⛔ 이건 결함이 아니다 — 클라 버퍼가 찼다는 뜻이다.

        ⚠ 옆의 `와이어공백` 과 **짝으로만** 읽어라. 큰 값이 여기 있고 와이어공백이 작으면
          "끊긴 게 아니라 앞서 보낸 것"이고, 반대면 진짜로 굶은 것이다.
        """
        big = [h for h in holds if h >= 0.25]
        if not big:
            return "페이서보류=-"
        return "페이서보류=[%s]" % ", ".join(f"{h:.2f}s" for h in big)

    @staticmethod
    def _batch_gap_log(gaps: list[float]) -> str:
        """**아무것도 안 나간 시간** — 클라의 `SERVER GAP mid-utterance` 와 짝이다.

        ⛔ 페이서에는 **필러가 없다**(`_pace` 는 앞설 때 재우기만 한다). 그래서 우리가 보낼
          것이 없는 동안 와이어는 **그냥 조용하다** — 선행버퍼(1.5초)를 넘기면 클라가 굶는다.
        ⚠ 구간 사이 공백은 `대기N.NNs` 로 이미 보인다. 이 값은 **그것과 다른 자리**다:
          다음 문장 묶음을 만드는 시간(LLM 생성 + TTS 왕복)이라 계측이 아예 없었다.
        """
        big = [g for g in gaps if g >= 0.25]     # 클라 판정 창(250ms)과 같은 기준
        if not big:
            return "와이어공백=-"
        return "와이어공백=[%s]" % ", ".join(f"{g:.2f}s" for g in big)

    def _tts_request_log(self) -> str:
        """이 대답의 **요청별** (글자·오디오 초). 요청 수와 절단이 한 줄에서 보인다."""
        if not self._reply_spans:
            return "요청=-"
        odd, waits, leads = self._tts_odd_chunks, self._tts_waits, self._tts_leads
        return "요청=[%s]" % ", ".join(
            "%d자·%.2fs%s%s%s%s" % (
                chars, audio.output_audio_s(sent),
                # ⭐ **구간 간 공백** — 이 구간의 첫 소리를 기다린 시간이다. 언어가 바뀔 때
                #   들리던 그 끊김이 이 값이고, 선행 합성이 먹히면 0 에 가까워진다.
                "·대기%.2fs" % waits[i] if i < len(waits) and waits[i] >= 0.05 else "",
                # ⭐ **원인의 절반** — 이 요청을 송출보다 얼마나 먼저 걸었나. 위 `대기` 가 큰데
                #   이 값이 작으면 우리가 늦게 건 것이고, 이 값이 큰데도 대기가 크면 벤더가 느리다.
                "·선행%.2fs" % leads[i] if i < len(leads) and leads[i] >= 0.05 else "",
                "·홀수%d" % odd[i] if i < len(odd) and odd[i] else "",
                # ⭐ 걷어낸 침묵(앞/뒤 ms) — 0 이면 안 걷어냈다는 뜻이다.
                "·침묵-%d/%dms" % self._tts_trims[i]
                if i < len(self._tts_trims) and any(self._tts_trims[i]) else "",
            )
            for i, (_, chars, sent) in enumerate(self._reply_spans)
        )

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
        # ⚠ `합성배속=측정불가(...)` 는 **뺐다**(2026-08-13). 스트리밍 경로에서는 합성과 재생이
        #   겹쳐 원리상 못 재므로 **모든 줄에 똑같은 문구**가 붙었다 — 정보가 0 이고 줄만 길어진다.
        #   배치 모드에서만 값이 있어 거기서만 붙인다(그 모드는 전량 합성 후 재생이라 잴 수 있다).
        synth = f" 합성배속={(total_s / synth_s):.2f}x" if synth_s and synth_s > 0 else ""
        return (
            f"들린글자={total_chars} 오디오={total_s:.1f}초 읽기={speed}자per초 "
            f"[{per_lang}]{synth}"
        )

    async def _begin_beaver_turn(self) -> str:
        # ⚠ 감정은 **대답 맨 앞 태그**라 첫 조각이 오면 이미 정해져 있다(`detect_emotion`).
        #   아직 없으면 None 으로 보낸다 — 클라가 neutral 로 그린다.
        self.state = TurnState.BEAVER_SPEAKING
        turn_id = await self.beaver.begin(self._reply_emotion)
        # ⭐⭐ **이 턴의 대답이 언제 시작됐나**를 여기서 묶는다(2026-08-15). 클라 계기는 첫
        #   소리에 오는데, 그때 `첫소리` 는 **아직 계산되지 않았다**(그건 묶음을 다 보낸 뒤에
        #   나온다 — 그게 짝없음 100% 의 원인이었다). 이 값이 있으면 클라가 물어본 순간에
        #   **그 자리에서** 계산할 수 있다.
        self._reply_began_at[turn_id] = self._reply_began
        while len(self._reply_began_at) > _FIRST_SOUND_HISTORY:
            self._reply_began_at.pop(next(iter(self._reply_began_at)))
        return turn_id

    def _sentence_emotion(self, sentence: str) -> str:
        """이 문장의 표정 — 태그가 있으면 그 값, **없으면 직전 값을 이어간다**.

        ⭐ 실측 근거(2026-08-12): 모델은 **문장마다** 태그를 붙이지 않는다. **감정이 바뀌는
          지점에만** 붙인다 — `<happy> 안녕! 비버예요. 반가워요! <neutral> 준비 됐나요?`
          처럼 run(연속 구간) 단위로 준다. 그래서 태그 없는 문장은 **누락이 아니라 연속**이다.
        ⛔ 여기서 `neutral` 로 떨어뜨리면 위 예의 표정이 happy→neutral→neutral 로 **문장마다
          튄다.** 모델 의도는 happy 3문장 유지다.
        ⚠ 이어갈 값이 없는 첫 구간에서만 기본값(neutral)으로 시작한다.
        """
        tag = detect_emotion(sentence)
        if tag:
            self._last_emotion = tag
        return self._last_emotion

    async def _prepare_batch(self, sentence: str) -> _PreparedBatch:
        """묶음을 **송출 없이 준비**한다 — 언어 분할 + **첫 구간 합성 시작**.

        ⭐⭐ 이게 묶음 사이 공백(실측 2.44s)을 닫는 자리다. 예전엔 앞 묶음을 **다 보낸 뒤에야**
          다음 묶음의 TTS 를 걸어서 **벤더 왕복(실측 937~1098ms)이 통째로 침묵**이 됐다.
          이제 앞 묶음이 재생되는 동안 다음 묶음의 요청이 이미 나가 있다.
        ⛔ **깊이 1 이다**(다음 묶음의 첫 구간만). 동시 벤더 요청은 최대 `prefetch_depth + 1` —
          429 를 이미 겪은 자리라 늘리지 않는다.
        ⚠ 여기서 하는 일은 **요청을 거는 것**이지 오디오를 받는 게 아니다(빠르다).
        """
        opened_at = time.monotonic()
        segments, emotion = self._split_for_speak(sentence)
        first = None
        if segments:
            text, language = segments[0]
            first = await self._open_segment(text, language, emotion)
        return _PreparedBatch(sentence, segments, emotion, first, opened_at)

    async def _speak_prepared(self, prep: _PreparedBatch) -> int:
        """준비된 묶음을 송출한다(첫 구간은 이미 열려 있다)."""
        if not prep.segments:
            return 0
        if len(prep.segments) == 1:
            return await self._send_segment(prep.first) if prep.first is not None else 0
        return await self._speak_segments(prep.segments, prep.emotion, preopened=prep.first)

    def _split_for_speak(self, sentence: str) -> tuple[list[tuple[str, str]], str | None]:
        """언어 분할 + 마커 로그 + 감정 판정 — `_speak`/`_prepare_batch` 의 공통 앞부분."""
        # ⭐ 글자 교차검증이 **몇 번 고쳤나**를 걷는다(2026-08-14). 0 이면 마커가 잘 지켜지고
        #   있다는 뜻이고, 크면 프롬프트 쪽도 손봐야 한다는 신호다 — 세지 않으면 못 가른다.
        lang_stats: dict = {}
        segments = split_by_language(sentence, self._locale, self._target_code, lang_stats)
        marker_state = _marker_state(sentence)
        self._marker_seen[marker_state] = self._marker_seen.get(marker_state, 0) + 1
        # ⚠ **여기서는 상태를 안 바꾼다**(2026-08-16). 예전엔 `_sentence_emotion(묶음전체)` 를
        #   불러 carry-forward 를 묶음의 **마지막 태그**까지 밀어 버렸다. 문장별 감정을 붙인
        #   지금 그러면 첫 문장이 **뒤 문장의 표정**을 물려받는다. 이 값은 turn_start 아바타용
        #   미리보기일 뿐이고, 진짜 이어붙임은 `_open_segment` 가 문장 순서대로 돌린다.
        emotion = detect_emotion(sentence) or self._last_emotion
        if self._single_voice():
            # ⚠ 여기서는 교차검증 결과를 **안 쓴다**(음성이 하나뿐이라 고를 게 없다). 그래서
            #   `언어보정` 도 안 센다 — 안 쓴 보정을 세면 그 숫자가 거짓말이 된다.
            logger.info("cascade 언어구간: 분할 안 함(단일 음성 엔진 %s) 언어마커=%s",
                        self._tts_engine, marker_state)
            text = strip_markers(sentence).strip()
            return ([(text, self._locale)] if text else []), emotion
        self._lang_fixes += int(lang_stats.get("fixed", 0))
        logger.info(
            # ⚠ **`언어마커=`** 다(`자막=`·문장 마커와 다른 것이다).
            "cascade 언어구간: %d개 %s 언어마커=%s%s",
            len(segments), "/".join(lang for _, lang in segments) or "-", marker_state,
            # ⭐ 순번이 틀려서 글자가 고친 자리 — 여기가 "영어를 한국어 발음으로 읽던" 그 지점이다.
            " 언어보정=%d건" % lang_stats["fixed"] if lang_stats.get("fixed") else "",
        )
        if len(segments) <= 1:
            # ⛔⛔ **여기가 ③ 의 자리였다**(2026-08-14). 구간이 하나면 `split_by_language` 가
            #   정한 언어를 **버리고** `self._locale`(모국어)로 덮어쓰고 있었다. 그래서 마커가
            #   없는 대답 — 실측 25턴 중 5건 — 은 통째로 en 으로 나갔고, **그 안의 한국어가
            #   영어 발음으로** 읽혔다. 교차검증을 넣어도 이 줄이 도로 지웠을 것이다.
            #   ⚠ 텍스트는 예전 그대로 쓴다(구두점만 남은 문장의 동작을 안 바꾼다).
            text = strip_markers(sentence).strip()
            lang = segments[0][1] if segments else self._locale
            return ([(text, lang)] if text else []), emotion
        return segments, emotion

    async def _speak(self, sentence: str) -> int:
        """문장 하나를 **언어 구간별로** 합성해 송출한다.

        비버는 타깃 언어 부분을 __이렇게__ 감싸서 낸다. 그 경계로 잘라 구간마다 그 언어로
        읽는다 — 감싼 부분을 모국어 발음으로 읽으면 학습에 방해가 되기 때문이다.
        마커가 없으면(또는 짝이 안 맞으면) 통째로 기본 언어로 나간다(설계 폴백).
        """
        # ⚠ **준비/송출을 가른 뒤의 얇은 껍데기**다(2026-08-13). 데모 훅·회귀가 이 이름을
        #   쓰므로 남긴다 — 지우면 회귀가 대거 깨진다(오늘 정리에서 배운 것).
        return await self._speak_prepared(await self._prepare_batch(sentence))

    async def _speak_segments(self, segments: list[tuple[str, str]],
                              emotion: str | None = None,
                              preopened: _OpenSegment | None = None) -> int:
        """구간들을 **순서대로** 송출하되, 뒤 구간의 합성은 **미리 시작**한다.

        ⛔ 순서는 절대 유지된다 — 합성이 먼저 끝났다고 먼저 내보내면 말이 뒤섞인다.
          앞 구간을 다 보낸 뒤에야 다음 구간의 큐를 읽는다.
        ⭐ 얻는 것: 뒤 구간의 벤더 왕복(실측 0.7~1.35초)이 **앞 구간 재생 시간 뒤로 숨는다.**
        ⚠ 깊이는 엔진 성질이다(`prefetch_depth`) — Gemini 는 분당 10회 상한이라 1(직렬)이다.
        """
        depth = max(1, int(self._profile().prefetch_depth))
        opening: deque[_OpenSegment] = deque()
        index = 0
        sent = 0
        if preopened is not None:
            # ⭐ 앞 묶음이 재생되는 동안 **미리 연** 첫 구간이다(묶음 선행 합성).
            opening.append(preopened)
            index = 1
        try:
            while index < len(segments) or opening:
                # 상한까지 미리 연다. ⛔ 이 수가 곧 **동시 벤더 요청 수**다(쿼터가 정한다).
                while index < len(segments) and len(opening) < depth:
                    text, language = segments[index]
                    index += 1
                    segment = await self._open_segment(text, language, emotion)
                    if segment is not None:
                        opening.append(segment)
                if not opening:
                    break
                sent += await self._send_segment(opening.popleft())
        finally:
            # ⛔ **미리 연 것도 같이 버린다.** barge-in 으로 여기서 취소돼도 남은 큐가 나중에
            #   흘러나가면 "끊었는데 소리가 더 난다"가 된다(오늘 고친 그 계열).
            for segment in opening:
                segment.task.cancel()
        return sent

    async def _speak_one(self, sentence: str, language: str,
                         emotion: str | None = None) -> int:
        """구간 하나를 합성해 송출한다(열기 → 보내기). 보낸 바이트 수를 돌려준다."""
        segment = await self._open_segment(sentence, language, emotion)
        if segment is None:
            return 0
        return await self._send_segment(segment)

    async def _pump_segment(self, stream: Any, queue: Any) -> None:
        """벤더 오디오를 **큐로 미리 옮긴다**(송출은 하지 않는다).

        ⛔ 여기서 예외를 밖으로 올리지 않는다 — 한 구간이 실패해도 **나머지 구간은 나가야**
          한다(R5). 큐에 끝 표시만 넣고 조용히 끝내되, **사유는 로그로 남긴다.**
        """
        try:
            async for chunk in stream:
                queue.put_nowait(chunk)
        except asyncio.CancelledError:
            raise                      # 취소는 그대로 — 선행분은 큐째로 버려진다
        except Exception as exc:  # noqa: BLE001
            logger.warning("cascade tts 구간 합성 중단(나머지 구간은 계속) — %s", str(exc)[:200])
        finally:
            queue.put_nowait(None)

    async def _open_segment(self, sentence: str, language: str,
                            emotion: str | None = None) -> _OpenSegment | None:
        """구간 하나의 **합성을 시작**한다(아직 소리는 안 나간다). 빈 구간이면 None.

        원가의 문자 수는 **여기서** 센다 — 과금은 우리가 텍스트를 넘긴 순간 일어나므로,
        barge-in 으로 뒤에 안 나가더라도 그 값은 이미 나갔다(다 나온 뒤에 세면 장부에서 사라진다).
        """
        # ⛔ **감정 태그는 절대 소리로 나가지 않는다.** 여기가 벤더로 나가기 직전의 마지막
        #   길목이다(`__마커__` 와 같은 급의 요구 — `?` 를 "쿼스천마크"로 읽던 사고 계열이다).
        # ⭐⭐ **문장별 감정**(2026-08-16 사장님: "문장별 감정 전달해줘서 영상통화때 쓰려고").
        #   ⛔ 태그를 떼기 **전에** 쪼갠다 — 떼고 나면 어느 문장이 어떤 표정이었는지 사라진다.
        #   ⛔ 새 분할기를 만들지 않는다: "무엇이 한 문장인가"의 출처는 `SentenceBuffer` 하나다.
        #   ⚠ carry-forward 는 여기서 **순서대로** 돈다(구간은 index 순으로 열리고 그 안은
        #     문장 순이다) — 규칙 자체는 안 바꿨다(실측 근거가 있는 규칙이다).
        sentences: list[tuple[str, str]] = []
        for raw in _split_sentences(sentence):
            spoken = strip_emotion_tags(raw).strip()
            if spoken:
                sentences.append((spoken, self._sentence_emotion(raw)))
        sentence = strip_emotion_tags(sentence).strip()
        if not sentence:
            return None
        self.usage.record_tts(sentence, vendor=self._tts_vendor())
        report: dict = {}
        align: dict = {}          # 이 요청에서 홀수 조각이 몇 개였나(벤더가 격자를 지키나)
        # ⭐ **요청을 거는 순간**을 이 대답에서 한 번만 남긴다 — 첫소리의 `묶음대기` 가 이 값으로
        #   갈린다(첫 문장 준비 → 여기까지가 우리 정책, 여기부터 첫 오디오까지가 벤더).
        self._tts_asked_at = self._tts_asked_at or time.monotonic()
        stream = await self._open_vendor_stream(sentence, language, report, emotion)
        # ⛔ 정렬이 **침묵 절단보다 먼저**다. `_trim_edges` 는 조각을 통째로 버리고
        #   `trim_silence_edges` 는 표본 단위로 자르는데, 들어온 조각이 홀수면 그 순간
        #   **뒤따르는 바이트가 반 표본씩 밀린다**(소리가 통째로 잡음이 된다).
        stream = sample_aligned(stream, align)
        trim = self._trim_silence()
        if trim:
            stream = self._trim_edges(stream, report)
        queue: asyncio.Queue = asyncio.Queue()
        # ⚠ 세션 TaskGroup 에 붙이지 않는다 — 여기서 난 실패가 **통화 전체를 무너뜨리면** 안
        #   된다(R5). 대신 소유자(`_speak`)가 finally 에서 반드시 취소한다.
        task = asyncio.create_task(self._pump_segment(stream, queue))
        # ⚠ 구간 감정이 안 넘어오면(직접 호출·데모 훅) **이 대답의 감정**을 쓴다 —
        #   하드코딩 기본값으로 덮으면 예전 동작(대답 1건당 하나)이 조용히 바뀐다.
        emotion = emotion or self._reply_emotion or _DEFAULT_EMOTION
        return _OpenSegment(sentence, language, emotion, queue, task, report, align, trim,
                            time.monotonic(), sentences)

    async def _open_vendor_stream(self, sentence: str, language: str, report: dict,
                                  emotion: str | None = None) -> Any:
        """벤더 호출 — ✓ 이건 성질이 아니라 **어느 어댑터를 부르느냐**다(구글 SDK vs HTTP,
        인자 자체가 다르다). 조절값이 아니므로 성질 표로 안 옮긴다."""
        if self._tts_engine == _OPENAI_TTS_CHOICE:
            # ⛔ 구글이 아니다 — 별도 어댑터를 탄다. 폴백도 하지 않는다(엔진을 골라 듣는 중인데
            #   조용히 다른 소리가 나면 A/B 가 거짓말이 된다).
            return openai_tts.synthesize_stream(
                sentence,
                instructions=self._style_prompt(emotion),   # ⭐ **그 구간의** 감정이 들어간다
                report=report,
            )
        # ⭐ 이 통화에서 이미 429 를 맞았으면 **Gemini 를 다시 찌르지 않는다**(세션 단위 백오프).
        #   실측: 한도가 분당 10회인데 수요가 평균 19.2 / 피크 27 이었다. 소진된 상태에서
        #   문장마다 찔러봐야 **실패해도 요청은 나가고**(회복이 늦어진다) 첫소리만 늘어난다.
        #   ⛔ 프로세스 전역으로 고정하면 쿼터가 회복돼도 영영 Chirp 이다 — 세션 단위여야 한다.
        allow_gemini = not self._tts_gemini_off
        # ✓ 이건 Gemini **쿼터** 고유라 성질 표로 안 옮긴다(다른 벤더엔 이 백오프가 없다).
        if allow_gemini and self._tts_vendor() != tts.CHIRP3_ENGINE:
            # 백오프 전까지의 Gemini 호출 수 — ①(요청 수 줄이기)의 효과를 재는 유일한 값이다
            # (백오프가 호출 자체를 막으므로 tts_calls 로는 못 잰다).
            self._tts_gemini_calls += 1
        return await tts.synthesize_stream(
            sentence,
            language=language,
            voice=self._tts_voice(),
            report=report,
            allow_gemini=allow_gemini,
            engine=self._profile().google_engine,
            speaking_rate=self._note_rate(language),
            style_prompt=self._style_prompt(emotion),
        )

    async def _send_segment(self, segment: _OpenSegment) -> int:
        """열어 둔 구간을 **순서대로** 송출하고 정산한다. 보낸 바이트 수를 돌려준다."""
        wait_at = time.monotonic()
        first_at = -1.0

        # ⭐⭐ **문장 경계에서 마커를 끼워 넣는다**(2026-08-16). 미리 몰아 보내지 않는다 —
        #   프론트는 마커가 **도착한 순간의 큐 위치**에 꽂아 두고 재생이 거기 닿을 때 얼굴을
        #   바꾼다. 그래서 "그 문장 차례에" 보내야 표정이 그 문장에서 바뀐다.
        #   ⛔ 정확한 경계 바이트는 **알 수 없다** — TTS 는 구간 하나에 오디오 하나를 주고
        #     정렬 정보를 안 준다. 쪼개서 물어보는 길(문장별 요청)은 사장님이 안 고르셨다.
        #     ⇒ 글자 비율로 **추정**하고, 그 추정이 얼마나 틀렸는지 로그로 드러낸다.
        plan = self._marker_plan(segment)
        marker_idx = 0
        base_bytes = self.beaver.sent_bytes

        async def _buffered():
            nonlocal first_at, marker_idx
            while True:
                item = await segment.queue.get()
                if item is None:
                    return
                if first_at < 0:
                    first_at = time.monotonic()
                # ⭐ 첫 문장은 **첫 오디오 직전**(예전과 같은 자리), 뒤 문장은 그 차례에.
                sent_here = self.beaver.sent_bytes - base_bytes
                while marker_idx < len(plan) and plan[marker_idx][0] <= sent_here:
                    text, emotion = plan[marker_idx][1], plan[marker_idx][2]
                    marker_idx += 1
                    await self._send_sentence_marker(segment, text, emotion)
                yield item

        try:
            sent = await speak_stream(self.beaver, _buffered(), segment.text,
                                      trim_tail=segment.trim)
            # ⛔ 추정이 길게 잡혀 남은 문장이 있으면 **여기서라도 보낸다.** 자막·표정이
            #   사라지는 것보다 조금 늦는 편이 낫다(프론트는 순서대로 꽂는다).
            while marker_idx < len(plan):
                await self._send_sentence_marker(segment, plan[marker_idx][1],
                                                 plan[marker_idx][2])
                marker_idx += 1
            self._note_marker_drift(segment, sent, plan)
        finally:
            # ⛔ 취소로 빠져나가도 **미리 받아 둔 것까지 같이 버린다** — 안 그러면 끊은 뒤에
            #   소리가 더 난다(오늘 고친 "삭제돼야 하는데 계속 나온다"가 그 종류다).
            segment.task.cancel()
        report, align = segment.report, segment.align
        # 이 구간이 소리를 낼 때까지 **페이서에 줄 게 없던 시간** — 언어가 바뀔 때 들리던
        # 그 공백이 이 값이다(선행 합성이 먹히면 0 에 가까워진다).
        wait_s = max(0.0, (first_at - wait_at)) if first_at > 0 else 0.0
        # ⭐ 이 구간을 **송출보다 얼마나 먼저 열었나**(= 선행 합성이 실제로 번 시간).
        lead_s = max(0.0, wait_at - segment.opened_at) if segment.opened_at else 0.0
        self.usage.record_tts_audio(sent)
        if sent:
            self._tts_engines.add(report.get("engine") or self._tts_vendor())
            self._note_tts_request(segment.language, len(segment.text), sent, align, wait_s,
                                   report, lead_s)
        else:
            # 오디오가 한 조각도 안 나왔다 = 합성 실패. 건수만 따로 센다(문자는 열 때 이미).
            self.usage.record_tts("", vendor=self._tts_vendor(), failed=True)
        if self._tts_ttfb_ms < 0 and report.get("ttfb_ms") is not None:
            self._tts_ttfb_ms = int(report["ttfb_ms"])
        if report.get("quota") and not self._tts_gemini_off:
            # ⛔ 엔진이 통화 중간에 바뀐 사실을 반드시 남긴다 — A/B 판정이 이 줄에 걸린다.
            self._tts_gemini_off = True
            logger.warning(
                "cascade tts 엔진 고정: gemini 호출 %d회 만에 429(분당 쿼터) → 이 통화 동안 %s "
                "로 고정한다(다음 통화는 다시 gemini 로 시작)",
                self._tts_gemini_calls, tts.CHIRP3_ENGINE,
            )
        if report.get("fallback_from"):
            self._note_fallback(report)
        return sent

    def _note_fallback(self, report: dict) -> None:
        """폴백이 일어났다 — **어느 엔진이 실제로 소리를 냈는지** 장부와 로그를 맞춘다.

        폴백은 A/B 비교를 오염시킨다(이 통화의 '첫소리'가 어느 엔진 것인지 흐려진다).
        ⛔ **문자를 다시 세지 않는다**(2026-08-10 수정). 여기서 `record_tts(문장, ...)` 을
          부르면 열 때 이미 센 문장을 **한 번 더** 세서 원가가 두 배가 된다. 여기서 필요한
          건 재계량이 아니라 **벤더 이름 정정**이다.
        """
        self.usage.record_tts("", vendor=report["fallback_from"], failed=True)
        self.usage.record_tts_fallback()
        engine = report.get("engine")
        if engine:
            self.usage.retag_tts(engine)


    def _note_rate(self, language: str) -> float | None:
        """이 구간에 쓸 배속을 고르고 **실제로 나간 값을 기록한다**(로그용).

        ⛔ 세션 값만 찍으면 구간별로 달라진 걸 확인할 방법이 없다 — 그게 이 기능의 요점이다.
        """
        rate = self._speaking_rate(language)
        if rate is not None:
            self._reply_rates[(language or "?").strip().lower()] = rate
        return rate

    def _rate_log(self) -> str:
        """대답 줄에 실을 언어별 배속 표시."""
        if not self._reply_rates:
            return "배속=서버값"
        return "배속=[%s]" % " ".join(
            f"{lang}:{rate:.2f}" for lang, rate in sorted(self._reply_rates.items())
        )

    def _style_prompt(self, emotion: str | None = None) -> str | None:
        """이 대답에 쓸 스타일 문구 — **감정 태그 → 고정 문구**(없으면 서버 기본값).

        ⛔ 문구는 `EMOTION_STYLES` 표에서만 나온다. LLM 이 스타일 문장을 지어내면 같은 감정도
          통화마다 다르게 들린다(사장님: "프롬프트가 일정해야 감정 표현도 일정하다").
        ⚠ 집합 밖 값·태그 누락은 조용히 기본 스타일로 떨어진다(R5 — 통화가 죽지 않는다).
        ⚠ Chirp 세션에서는 `core/tts` 가 스타일을 아예 안 넘긴다 — 태그는 걷어내지고 소리에는
          영향이 없다. 그 사실은 대답 로그의 `감정=…(미적용)` 으로 보인다.
        """
        if self._tts_style is not None:
            return self._tts_style              # 데모 화면이 직접 고른 값이 이긴다
        # ⭐ **그 구간의 감정**이 온다(문장 단위, 2026-08-12). 안 오면 이 대답의 첫 감정 —
        #   예전(대답 1건당 하나) 동작이라 호출부가 안 넘겨도 그대로 돈다.
        return emotion_style(emotion or self._reply_emotion)

    def _note_stray_tag(self, text: str) -> None:
        """대사 맨 앞에 **집합 밖 태그**가 왔으면 그 사실을 남긴다(소리로는 이미 안 나간다).

        ⛔ 조용히 지우면 **프롬프트가 이상한 걸 뱉고 있다는 사실을 다시는 못 본다.**
          실통화 00147 이 그랬다 — `[대화]` 가 소리로 새고 감정은 전부 '없음'이었는데,
          로그에는 아무 흔적이 없어 원인을 길이 상한 쪽으로 잘못 짚었다.
        ⚠ 대답 하나에 **한 번만** 찍는다(조각마다 찍으면 로그가 도배된다).
        """
        if self._dropped_tag or self._reply_emotion:
            return
        tag = read_stray_tag(text)
        if not tag:
            # ⚠ 대괄호가 **없는** 경우도 갈라 둔다. 우리는 대사 원문을 안 찍으므로, 모델이
            #   `[대화]` 를 뱉었는지 `대화.` 를 뱉었는지는 지금 추론이다 — 후자면 위 제거가
            #   안 먹고 증상이 그대로 남는다. **지우지는 않고**(정상 낱말이다) 사실만 남겨
            #   다음 통화 로그에서 판별되게 한다.
            bare = read_bare_label(text)
            if bare:
                self._dropped_tag = "?" + bare       # 버린 게 아니라 **의심**이라는 표시
                logger.warning(
                    "cascade ⚠ 대사가 라벨 낱말 %r 로 시작한다(대괄호 없음) — 지우지 않았다. "
                    "소리로 그대로 나간다면 프롬프트 쪽 문제다(persona_prompt 의 구획 라벨)",
                    bare,
                )
            return
        self._dropped_tag = tag
        logger.warning(
            "cascade ⚠ 대사 맨 앞에 집합 밖 태그 %r — 소리로 안 내보내고 감정도 없음으로 둔다. "
            "프롬프트가 대괄호를 구획 라벨로 쓰는 것과 태그 규약이 충돌한다(persona_prompt)",
            tag[:20],
        )

    def _emotion_log(self) -> str:
        """대답 줄에 실을 감정 표시 — **적용됐는지까지** 적는다.

        ⛔ Chirp 은 스타일을 안 받는다. 그 사실이 안 보이면 사장님이 Chirp 으로 들으시고
          "감정이 안 되네"라고 하시게 된다.
        ⛔⛔ **판정은 성질 표 한 곳에서만 한다**(`_TtsProfile.takes_style`). 여기 하드코딩돼 있던 탓에 OpenAI 로
          돌면서 `감정=인사(미적용:cloud-tts-chirp3-hd)` 라는 **거짓 로그**가 찍혔다 —
          감정은 실제로 들어가고 있었는데(instructions), 로그만 아니라고 말했다.
          그리고 엔진 이름도 하드코딩이라 **돌지도 않은 엔진 이름**을 찍었다.
        """
        if not self._reply_emotion:
            # ⭐ **무엇이 왔는지**까지 적는다 — '없음'만 보고는 태그가 안 온 건지
            #   집합 밖이 온 건지 못 가른다(그 구분이 이번 사고의 실마리였다).
            if self._dropped_tag:
                return "감정=없음(버린태그:%s)" % self._dropped_tag[:12]
            return "감정=없음"
        # ⭐ **문장별 감정이 실제로 도는지**는 목록으로만 보인다(2026-08-16). 턴당 하나만
        #   찍으면 같은 값 3개인지 서로 다른 3개인지 구분이 안 된다 — 그게 이 기능의 전부다.
        detail = ("=[%s]" % ",".join(self._marker_emotions)
                  if len(self._marker_emotions) > 1 else "")
        if self._profile().takes_style:
            return "감정=%s%s" % (self._reply_emotion, detail)
        # 미적용이면 **실제로 도는 엔진 이름**을 적는다(빈 값이면 서버 기본값 = Chirp).
        return "감정=%s(미적용:%s)" % (self._reply_emotion, self._tts_vendor())

    def _speaking_rate(self, language: str | None = None) -> float | None:
        """이 **구간**에 적용할 말하기 배속 — 언어·엔진마다 다르다.

        고르는 순서(위가 이긴다):
          ① **언어별 값**(데모 화면·env). 하나의 값으로는 둘을 못 맞춘다 — 실측에서 영어는
             1.6배 느린데 한국어는 이미 맞았다. 한국어를 같이 올리면 **학습자가 따라 말하는
             부분이 Live 보다 빨라진다.**
          ② 클라가 고른 공통 값(데모 화면의 기존 통로 — 그대로 둔다)
          ③ 엔진 기본값(Gemini 만 따로) → 없으면 서버 공통값
        ⛔ 새 분류기를 만들지 않는다 — 구간의 언어는 `__마커__` 분할이 이미 알려 준다.
        ⚠ `자per초` 는 언어 간 직접 비교가 안 된다(한국어 1글자 ≈ 영어 3~4글자). 값을 정할 땐
          반드시 **같은 언어끼리** 비교해라.
        """
        lang = (language or "").strip().lower()
        if lang and lang in self._tts_rate_by_lang:
            return self._tts_rate_by_lang[lang]
        if self._tts_rate is not None:
            return self._tts_rate
        return getattr(settings, self._profile().rate_setting)

    def _single_voice(self) -> bool:
        """이 엔진은 **한 음성으로 두 언어를 다 읽나**(=마커 분할을 건너뛰나).

        ⛔ 엔진 이름으로 명시한다(설정). 기본은 비어 있어 지금처럼 나눈다.
        """
        engine = (self._tts_engine or tts.CHIRP3_ENGINE).strip()
        names = {n.strip() for n in (settings.CASCADE_TTS_SINGLE_VOICE_ENGINES or "").split(",")}
        return bool(engine) and engine in names

    def _trim_silence(self) -> bool:
        """이 엔진의 출력에서 **구간 앞뒤 침묵을 잘라낼 것인가.**

        ⛔ 엔진 이름으로 명시한다(설정). Chirp 은 빠져 있다 — 지금 잘 나오는 걸 건드리지 않는다.
        """
        engine = (self._tts_engine or tts.CHIRP3_ENGINE).strip()
        names = {n.strip() for n in (settings.CASCADE_TTS_TRIM_ENGINES or "").split(",")}
        return bool(engine) and engine in names

    async def _trim_edges(self, stream: Any, report: dict | None = None) -> Any:
        """스트림 **앞뒤** 침묵을 흘려보내지 않고 걷어낸다.

        ⭐ 지연이 늘지 않는다 — 붙들고 있는 것이 **침묵뿐**이라 사용자가 듣는 시점은 그대로다
          (오히려 첫 소리가 빨라진다).

        ⭐⭐ **꼬리도 여기서 잡는다**(2026-08-13). 예전엔 `speak_stream` 이 **마지막 조각**
          하나만 잘랐다 — 꼬리 침묵이 그 조각보다 길면 나머지는 그대로 나갔다. 우리는 문장×언어로
          잘게 쪼개므로 그 잔여가 **구간마다** 붙는다(짧은 구간일수록 자per초가 나빠지는 모양과
          맞는다: 39자 5.7자per초 vs 5자 3.4자per초).
          방법은 머리와 같다 — **침묵 조각은 붙들고 있다가**, 소리가 다시 오면 **그대로 흘려보내고**
          (말 사이 쉼이므로 지워선 안 된다), 스트림이 끝나면 그때 버린다. 붙드는 건 침묵이라
          **오디오 지연은 0** 이다.
        ⚠ `keep_tail_ms` 만큼은 남긴다 — 구간이 딱 붙으면 기계처럼 들린다(고치려는 게 "AI 티"다).
        ⚠ 판정은 `has_audible_signal` 이다. 아주 작은 소리(끝의 여린 음절)를 침묵으로 볼 수
          있는데, 그때도 **뒤에 소리가 오면 그대로 나간다**. 위험은 맨 끝 한 조각뿐이고 그건
          남기는 keep_tail 이 덮는다.
        ⛔ `trim_silence_edges` 의 반환값으로 판별하면 안 된다 — 전부 침묵이면 **그대로**
          돌려주는 규약이라(멀쩡한 오디오 보호) 침묵을 소리로 오인한다.
        """
        keep_ms = max(0, settings.CASCADE_TTS_TRIM_KEEP_MS)
        keep_bytes = int(keep_ms * BEAVER_BYTES_PER_MS) // 2 * 2   # I6: 항상 짝수
        started = False
        held: list[bytes] = []          # 끝이면 버릴 수도 있는 **침묵 조각**들
        dropped_head = dropped_tail = 0
        async for chunk in stream:
            if not chunk:
                continue
            if not started:
                if not audio.has_audible_signal(chunk):
                    dropped_head += len(chunk)
                    continue             # 통째로 침묵인 조각은 버린다
                started = True           # 소리가 시작됐다
                cut = trim_silence_edges(chunk, keep_head_ms=keep_ms, tail=False)
                dropped_head += len(chunk) - len(cut)
                yield cut
                continue
            if not audio.has_audible_signal(chunk):
                held.append(chunk)       # 말 사이일 수도 있다 — 아직 판단하지 않는다
                continue
            for silent in held:          # 말 사이였다 ⇒ 지연 0 으로 그대로 흘린다
                yield silent
            held.clear()
            yield chunk
        if held:
            # 스트림이 끝났다 ⇒ 붙들고 있던 것은 **꼬리 침묵**이다. 자연스러운 틈만 남긴다.
            tail = b"".join(held)
            keep = tail[:keep_bytes]
            dropped_tail += len(tail) - len(keep)
            if keep:
                yield keep
        if report is not None:
            report["trim_head_ms"] = round(dropped_head / BEAVER_BYTES_PER_MS)
            report["trim_tail_ms"] = round(dropped_tail / BEAVER_BYTES_PER_MS)

    def _max_sentences(self) -> int:
        """이 통화의 문장 상한 — **프롬프트 문구와 강제가 여기서 같이 나온다**.

        ⚠ 지금은 설정 하나지만 **언어별로 갈릴 수 있다**(실측: 영어 4문장 = 197자·19.4초,
          한국어는 훨씬 짧다). 그때 고칠 자리가 여기 하나가 되도록 접근을 이 함수로 모아 뒀다
          — 값을 읽는 곳이 흩어지면 언어별 규칙을 넣을 때 또 갈린다.
        ⚠ 0 이하는 "상한 없음"이다(되돌릴 길).
        """
        return max(0, settings.CASCADE_LLM_MAX_SENTENCES)

    def _profile(self) -> _TtsProfile:
        """이 세션 엔진의 성질(표 한 곳)."""
        return _profile_for(self._tts_engine)

    def _batch_chars(self) -> int:
        """문장을 얼마나 모아 한 번에 합성할지 — **요청당 오버헤드가 큰 엔진일수록 크게.**

        요청마다 고정 오버헤드(TTFB)가 붙으므로 짧은 요청이 많을수록 손해다. 반대로 TTFB 는
        길이와 거의 무관하므로(Gemini 실측 49자 1,328ms / 196자 1,188ms) 크게 묶어도 첫 소리가
        그만큼 늦지 않는다. 요청 수가 줄면 분당 쿼터에도 유리하다.
        ⚠ 이 값은 **문장 사이**만 묶는다 — `__마커__` 로 갈린 **언어 구간**은 어차피 요청이
          따로 나간다(그쪽은 `CASCADE_TTS_SINGLE_VOICE_ENGINES` 가 다루고, 한국어 발음이
          걸려 있어 귀로 판단할 문제다).
        """
        return _batch_chars_for(self._tts_engine)

    def _tts_vendor(self) -> str:
        """원가 벤더 문자열 = **의도한 엔진**. 실제로 다른 엔진이 냈으면 위에서 보정한다.

        ⚠ 모델별로 단가가 다르다 — 모델 ID 까지 남겨야 원가를 가를 수 있다.
        """
        return self._profile().vendor()

    def _drop_incomplete_tail(self, text: str) -> tuple[str, str]:
        """상한에 걸려 잘린 대답에서 **마지막 미완성 문장**을 떼어낸다 → (말할 것, 버릴 것).

        ⛔ 잘린 말을 그대로 읽으면 상한을 안 두느니만 못하다("…그리고 저는" 하고 끝난다).
        ⚠ **완성된 문장이 하나도 없으면 버리지 않는다** — 침묵보다는 미완성이 낫다.
          그 경우는 드물고(상한이 첫 문장도 못 담았다는 뜻이다) 로그로 드러낸다.
        """
        buffer = SentenceBuffer()
        sentences = buffer.push(text)
        tail = buffer.flush().strip()
        kept = "".join(sentences).strip()
        if not kept:
            logger.warning(
                "cascade ⚠ 대답이 상한(%d토큰)에 걸렸는데 **완성된 문장이 하나도 없다** — "
                "미완성인 채로 내보낸다. 상한이 너무 낮다는 신호다",
                settings.CASCADE_LLM_MAX_OUTPUT_TOKENS,
            )
            return text, ""
        return kept, tail

    def _add_segment(self, role: str, text: str, pcm: bytes) -> None:
        """통화 기록 한 줄 — **Live 와 같은 계약**(`save_segments` 가 그대로 받는다).

        ⛔ 전사도 오디오도 없으면 넣지 않는다(빈 행은 분석에 잡음만 준다).
        """
        if self._call_id is None:
            return
        text = (text or "").strip()
        if not text and not pcm:
            return
        self._segments.append(
            {"turn_index": self._next_turn_index, "role": role, "text": text, "pcm": pcm}
        )
        self._next_turn_index += 1

    def _record_beaver_segment(self, turn_id: str | None, text: str) -> None:
        """비버 발화 1건을 기록에 넣는다 — **들린 데까지**의 텍스트 + 그 턴의 오디오."""
        pcm = self.beaver.take_pcm(turn_id) if turn_id else b""
        self._add_segment("beaver", strip_markers(strip_emotion_tags(text or "")), pcm)

    async def _persist_segments(self, *, final: bool = False) -> None:
        """아직 저장 안 한 세그먼트를 저장한다(Live 의 점진 flush 와 같은 규약).

        ⛔ 실패해도 통화는 계속된다 — 다음 주기나 종료 때 **커서가 그대로라 재시도**된다(R5).
        ⚠ 종료 저장은 `upload_audio=False` 다: 전사 행을 먼저 커밋해 분석이 오디오 변환·업로드를
          기다리지 않는다(Live P2.6 과 같은 이유).
        """
        if self._call_id is None or self._session_factory is None or self._member_id is None:
            return
        new = self._segments[self._persisted:]
        if not new:
            return
        target = self._persisted + len(new)
        try:
            await svc.run_db(
                self._session_factory,
                lambda db: svc.save_segments(
                    db, self._call_id, new, self._member_id, upload_audio=not final
                ),
            )
        except Exception as exc:  # noqa: BLE001 - R5
            logger.warning("cascade: 세그먼트 저장 실패(다음에 재시도) — %s", str(exc)[:200])
            return
        self._persisted = target
        freed = 0
        for seg in self._segments[:target]:
            freed += len(seg.get("pcm") or b"")
            seg["pcm"] = b""       # ⭐ 저장했으니 놓아준다(안 놓으면 통화 내내 RAM 을 문다)
        logger.info(
            "cascade 기록: %s %d개(누적 %d) call_id=%s pcm해제=%dKB",
            "최종 저장" if final else "점진 flush", len(new), target, self._call_id, freed // 1024,
        )

    async def _finalize_call(self, summary: dict | None, duration_s: float) -> None:
        """통화 종료 저장 — 세그먼트 마무리 → 원가 → 상태(Live 의 종료 경로와 같은 순서).

        ⛔ 전부 R5 다 — 하나가 실패해도 나머지를 시도하고, 실패는 로그로만 남긴다.
          통화는 이미 끝났으므로 여기서 예외를 올려 봐야 아무도 못 살린다.
        """
        if self._call_id is None:
            return
        await self._persist_segments(final=True)
        if summary is not None:
            try:
                saved = await svc.run_db(
                    self._session_factory,
                    lambda db: svc.save_call_usage(
                        db, self._call_id, summary, engine=self.usage.engine()
                    ),
                )
                if not saved:
                    logger.warning("cascade: 원가 저장 대상 통화 없음 call_id=%s", self._call_id)
            except Exception as exc:  # noqa: BLE001 - R5
                logger.warning("cascade: 원가 저장 실패(무시) — %s", str(exc)[:200])
        try:
            await svc.run_db(
                self._session_factory,
                lambda db: svc.finalize_call(
                    db, self._call_id, total_time=int(max(0.0, duration_s)), status="analyzing"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - R5
            logger.warning("cascade: 통화 종료 처리 실패(무시) — %s", str(exc)[:200])
        logger.info(
            "cascade 통화 저장 완료: call_id=%s 세그먼트=%d 길이=%d초",
            self._call_id, self._persisted, int(max(0.0, duration_s)),
        )

    async def _flush_loop(self) -> None:
        """통화중 주기적 점진 저장 — 긴 통화·크래시 내성(Live 와 같은 주기)."""
        while True:
            await asyncio.sleep(max(5.0, settings.CASCADE_SEGMENT_FLUSH_S))
            await self._persist_segments()

    def _spawn_hint(self, turn_id: str | None, question: str) -> None:
        """비버 **질문** 턴 뒤에 예시 답변을 만들어 보낸다(D16 — Live 와 같은 사이드카).

        ⛔⛔ **메인 파이프를 1ms 도 늦추지 않는다.** 여기서 하는 일은 `create_task` 하나뿐이고
          LLM 콜·전송은 전부 백그라운드다. 실패·지연은 **힌트 미표시**로 끝난다(R5).
          ⚠ 세션 TaskGroup 에 붙이지 않는다 — 거기 붙이면 힌트가 터질 때 **통화가 죽는다**.
        ⭐ 질문 판정은 **물음표**다(Live 와 같은 규칙). 근거 셋:
          ① 이미 D16 이 그 규칙으로 돈다 — 갈리면 두 경로의 힌트가 달라진다(이번 작업 내내
             지킨 규율과 같다).
          ② 매 턴 LLM 에 물어보는 방식(선택지 b)은 **설명 턴에서도 호출**이 나가 원가가 는다.
          ③ "한국어 의문 어미를 놓친다"는 우려는 **우리 대사에는 해당이 적다** — 프롬프트가
             비버에게 "물음표로 끝내고 멈춰라"를 지시한다(persona_prompt 착지 규칙).
        ⚠ 커리큘럼이 있는 언어(ko)만 — 회화 전용 언어는 예시 생성 프롬프트가 그 언어에
          맞춰져 있지 않아 무의미하다(Live 와 같은 조건).
        """
        if not settings.CASCADE_HINT_ENABLED:
            # ⭐ **순정 모드** — 사이드카를 아예 안 띄운다(만들지도, 보내지도 않는다).
            #   화면에서 안 그리는 것과 다르다: 여기서 막으면 턴당 LLM 호출 1건이 줄고
            #   로그가 조용해져 **통화 자체를 판정할 수 있다.**
            return
        if self._genai_client is None or not turn_id:
            return
        spec = resolve_language(self._target_code)
        if spec is None or not spec.has_curriculum:
            return
        question = (question or "").strip()
        if "?" not in question:
            return          # 설명·안내 턴에 힌트를 띄우면 소음이다(mechanics ⑬)
        prev = self._hint_task
        if prev is not None and not prev.done():
            prev.cancel()   # 낡은 질문의 힌트가 늦게 뜨는 혼선 방지
        ctx = {
            "client": self._genai_client,
            "model": settings.JUDGE_MODEL,
            "instruction": _hint_instruction(
                _LOCALE_LABEL.get(self._locale) or _LOCALE_LABEL["en"], self._target_label
            ),
        }
        task = asyncio.create_task(
            self._run_hint(ctx, turn_id, question), name=f"cascade-hint-{turn_id}",
        )
        self._hint_task = task
        self._hint_tasks.add(task)
        task.add_done_callback(self._hint_tasks.discard)

    async def _run_hint(self, ctx: dict, turn_id: str, question: str) -> None:
        """힌트 1건 생성 → 전송(백그라운드 본문 — **예외 전량 흡수**, R5).

        ⚠ Live 의 `_hint_sidecar` 와 같은 일을 한다. 함수를 그대로 못 쓴 이유는 하나 —
          그쪽은 **원시 WebSocket** 을 받아 `send_text` 한다. 프레임(`ServerHint`)·스키마
          (`HintOut`)·지시문(`_hint_instruction`)은 **그대로 재사용**하므로 클라 변경은 0 이다.
        ⛔ 이 안에서 세션 상태를 건드리지 않는다 — 메인 턴과 경합하면 사이드카의 의미가 없다.
        """
        try:
            result = await gemini_analysis.generate_structured(
                ctx["client"], ctx["model"],
                system_instruction=ctx["instruction"], prompt=question,
                schema=HintOut, temperature=0.3, thinking_budget=0,
            )
            raw = getattr(result, "examples", None) if result is not None else None
            examples = [
                HintExample(
                    korean=k,
                    roman=getattr(e, "roman", None),
                    native=getattr(e, "native", "") or "",
                )
                for e in (raw or [])
                if (k := (getattr(e, "korean", None) or "").strip())
            ][:3]   # 최대 3개, `korean` 없는 예시는 버린다(클라 계약)
            if not examples:
                return
            if turn_id != self.beaver.turn_id and self._hint_task is not asyncio.current_task():
                # ⛔ 이미 다음 턴이 시작됐고 나는 낡은 태스크다 — 늦은 힌트는 **버린다**.
                logger.info("cascade 힌트 폐기(턴이 지났다) turn=%s", turn_id)
                return
            await self._safe(ServerHint(turn_id=turn_id, examples=examples))
            # ⛔ 대사 원문은 안 찍는다 — 개수와 턴만 남긴다.
            logger.info("cascade 💡 hint[turn=%s]: %d개", turn_id, len(examples))
        except asyncio.CancelledError:
            raise       # 취소(새 질문·통화 종료)는 정상 경로다
        except Exception as exc:  # noqa: BLE001 - 힌트 실패는 미표시일 뿐 통화 무영향
            logger.warning("cascade 힌트 사이드카 실패(무시 — 힌트 미표시): %s", str(exc)[:200])

    def _remember_beaver(self, turn_id: str | None, generated: str) -> None:
        """이력에는 **실제로 들린 데까지**만 남긴다(설계 §5).

        끊기지 않은 턴은 생성 전체가 곧 들린 말이다. 끊긴 턴은 원장이 답을 안다.
        """
        if turn_id is None or not generated.strip():
            # ⚠ 이력엔 안 남겨도 **오디오는 나갔을 수 있다** — 그 턴의 PCM 은 비워 둔다
            #   (안 비우면 다음 턴 기록에 남의 소리가 붙는다).
            if turn_id is not None:
                self.beaver.take_pcm(turn_id)
            return
        self._history.append({"role": "model", "text": generated.strip()})
        self._trim_history()
        # ⭐ 통화 기록도 **같은 자리**에서 남긴다 — 이력과 기록이 갈리면 분석이 다른 말을 본다.
        self._record_beaver_segment(turn_id, generated)
        # 힌트는 **여기서 태스크만** 띄운다(대답은 이미 다 나갔다 — 지연 0).
        self._spawn_hint(turn_id, generated)

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
            # ⭐ 기록에도 **들린 데까지만**. 안 들린 뒤쪽을 저장하면 분석이 사용자가 못 들은
            #   말을 학습 문장으로 삼는다(설계 §5 와 같은 규율).
            self._record_beaver_segment(turn_id, spoken)
        elif turn_id is not None:
            # 하나도 안 들렸다 — 그 오디오는 기록에도 안 남긴다(아무도 못 들은 소리다).
            self.beaver.take_pcm(turn_id)
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
        self._reply_seq += 1
        self._reply_task = self._tg.create_task(
            self._run_resume(pending["text"], self._reply_seq)
        )
        return True

    async def _run_resume(self, text: str, seq: int = 0) -> None:
        """끊겼던 말의 나머지를 그대로 소리로 낸다. ⛔ LLM 호출 없음(새 입력이 없으니 새 말도 없다).

        ⛔ **이것도 '대답 경로'다.** `_run_reply` 에만 붙였던 세 가지가 여기 빠져 있었다
          (2026-08-11 QA R1): ①대기열 배수 ②CANCELLING 해제 ③세대 가드.
          ①이 없으면 이어가기 중에 한 말이 **영영 답을 못 받고**, 사용자가 참다 다시 말하면
          그 새 대답의 finally 가 낡은 발화를 배수해 **침묵 → 새 말 답 → 낡은 말 답** 순으로 들린다
          (`32616b9` 가 `_run_reply` 에서 없앤 증상 그대로다).
        """
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
            self._settle_reply_state(seq)
            await self._drain_pending_user_text()

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

    def _resolve_languages(self) -> None:
        """이 통화의 **모국어·학습 대상 언어**를 정한다 — 우선순위 **env > DB > 기본값**.

        ⭐ 사장님 지시(2026-08-12): "stt는 자동으로 되지만 통화는 어떤 언어로 해야 할지
          알아야 하니까 locale 언어랑 target language 받아와야 해."
        ⛔ env 를 **먼저** 보는 이유: 사장님이 데모 화면에서 언어를 바꿔 실험하신다. dev
          override 를 남기라는 지시가 있었다(음색과 같은 우선순위).
        ⚠ 값이 하나뿐인 곳(라벨)은 대상 코드에서 **파생**시킨다 — 코드와 라벨을 따로 받으면
          그 둘이 갈린다(같은 계열의 사고를 이 프로젝트에서 이미 여러 번 겪었다).
        """
        # 학습 대상: 라우터가 DB(member.target_language)에서 꺼내 넘겨준 값 — Live 와 같은 경로.
        override_target = (settings.CASCADE_TARGET_LANGUAGE_OVERRIDE or '').strip()
        if self._member_target_language and not override_target:
            spec = resolve_language(self._member_target_language)
            if spec is not None:
                self._target_code, self._target_label = spec.code, spec.label
            else:
                logger.warning(
                    "cascade: 미지원 target_language(%s) → 서버 기본(%s) 폴백",
                    self._member_target_language, self._target_code,
                )
        # 모국어: 캐릭터 setup 의 locale(회원 언어). 없으면 env 기본값 그대로.
        if override_target:
            spec = resolve_language(override_target)
            if spec is not None:
                self._target_code, self._target_label = spec.code, spec.label
        setup_locale = (self._setup or {}).get("locale")
        override_locale = (settings.CASCADE_LOCALE_OVERRIDE or '').strip()
        if override_locale:
            self._locale = normalize_locale(override_locale) or override_locale
        elif setup_locale:
            self._locale = normalize_locale(setup_locale) or setup_locale
        # ⭐⭐ **조용한 폴백을 시끄럽게 한다**(2026-08-13). 오늘 우리가 당한 사고가 전부
        #   조용한 폴백이었다(자막 미전송·beaver_preparing 미전송·힌트 로그 부재).
        #   ⛔ **소리 없이 기본값으로 도는 게 제일 나쁘다** — 통화는 멀쩡해 보이는데 학습자는
        #     자기 모국어가 아닌 언어로 설명을 듣는다. 지금 영어권 사용자라 `en` 기본값이
        #     **우연히 맞을** 뿐이고, 그 우연이 깨지는 날 아무 신호도 없다.
        #   ⚠ env 는 안 비운다(R5 폴백이 필요하다). 대신 **발동하면 반드시 보인다.**
        overridden = bool(override_target or override_locale)
        from_db = bool(self._setup or self._member_target_language)
        source = "override(env)" if overridden else ("DB" if from_db else "env 기본값")
        line = ("cascade 언어: 모국어=%s 학습대상=%s(%s) 출처=%s · STT=자동감지(고정)",
                self._locale, self._target_code, self._target_label, source)
        if from_db and not overridden:
            logger.info(*line)
        else:
            # 덮어쓰기(실험)와 기본값(폴백) 둘 다 **의도된 운영 상태가 아니다**.
            logger.warning(*line)

    def _log_config_snapshot(self) -> None:
        """⭐⭐ **이 통화가 어떤 조건으로 돌았나 — 한 줄로**(2026-08-13).

        ⛔ 왜 필요한가: 오늘 우리는 **서로 다른 설정으로 돌아간 통화들을 나란히 비교했다.**
          임계 0.007 통화와 0.04 통화를 같은 표에 놓고 "에코가 안 잡힌다"고 판단할 뻔했고,
          문장상한 4 와 3 의 `글자=` 를 섞어 봤다. 조건은 env 에 있는데 **env 는 그 사이에
          바뀌므로**, 나중에 보면 과거 로그는 **영영 해석이 안 된다.**
        ⛔ **반드시 한 줄**이다. 여러 줄로 흩으면 grep 으로 한 통화를 못 묶는다.
        ⚠ 값이 확정된 뒤에 부른다 — 클라 `start` 가 엔진·배속을 바꿀 수 있어서, 그 전에 찍으면
          **찍은 값과 실제가 다르다**(그게 이 줄을 넣는 이유와 정반대가 된다).
        """
        lead = self.beaver.lead_ms
        # ⭐ **추론·토큰상한도 싣는다**(2026-08-13). 값이 안 보이면 "정말 0 으로 돌았나"를
        #   아무도 답할 수 없다 — env 가 덮을 수 있고, 실제로 덮고 있는 값이 있다(bargein).
        #   이건 A유형(안 보내는데 아무도 몰랐다)의 설정판이다.
        budget = settings.CASCADE_LLM_THINKING_BUDGET
        logger.info(
            "cascade 설정: %s 오디오=%dHz/%dch(%s) llm=%s@%s 추론=%s 토큰상한=%s tts=%s "
            # ⚠ `들림=` 은 예전 `min=`(최소 지속) 자리다 — 그 관문은 삭제했고(2026-08-14),
            #   지금 남은 시간 관문은 **"비버가 얼마나 들렸나"** 하나뿐이다. 이름을 그대로
            #   두면 사라진 관문이 아직 있는 것처럼 읽힌다.
            "문장상한=%s 힌트=%s 마이크상시=%s bargein(rms=%.3f 들림=%dms confirm=%s) "
            "침묵=%dms+벤더%s 병합gap=%dms 선행버퍼=%s 세션상한=%ds",
            self._sid,
            # ⭐ **가정인지 선언인지까지** 적는다 — 값만 적으면 "16000" 이 클라가 말한 건지
            #   우리가 찍은 건지 알 수 없고, 그 구분이 없어서 오늘 반나절을 태웠다.
            self._sample_rate, self._channels,
            "선언" if self._rate_declared else "가정",
            settings.CASCADE_LLM_MODEL, self._llm_location or "미상",
            "off" if budget == 0 else ("모델기본" if budget is None else budget),
            settings.CASCADE_LLM_MAX_OUTPUT_TOKENS or "없음",
            self._tts_engine or tts.CHIRP3_ENGINE,
            self._max_sentences() or "없음",
            "on" if settings.CASCADE_HINT_ENABLED else "off",
            "on" if settings.CASCADE_MIC_ALWAYS_OPEN else "off",
            settings.CASCADE_BARGEIN_RMS, settings.CASCADE_BARGEIN_MIN_AUDIBLE_MS,
            self._bargein_confirm,
            # ⭐ **벤더 침묵창은 우리 임계 앞에 얹혀 있다** — 둘을 붙여 찍는다. 따로 찍으면
            #   다음 사람이 또 800ms 만 보고 "왜 1.3초를 기다리지?"를 처음부터 파게 된다.
            #   ⚠ 병합 gap 도 같이 싣는다: 이 셋은 **한 묶음으로만 읽을 수 있다**
            #     (허용 쉼 = 병합gap + 벤더침묵, 그리고 그것이 임계를 넘으면 안 된다).
            self._silence_ms,
            "%dms" % settings.OPENAI_STT_SILENCE_MS if settings.OPENAI_STT_SILENCE_MS
            else "기본(미지정)",
            settings.CASCADE_SPEECH_MERGE_GAP_MS,
            "%dms" % lead if lead is not None else "서버공통",
            int(settings.CASCADE_SESSION_MAX_S),
        )
        # ⭐ **자기 점검** — 의도와 실제가 어긋난 것만 시끄럽게(정상이면 조용하다).
        #   ⚠ 정상까지 경고하면 아무도 안 본다. 여기 걸리는 건 "설정은 했는데 안 먹었다"뿐이다.
        wanted = (settings.CASCADE_LLM_LOCATION or "").strip()
        if wanted and wanted != self._llm_location:
            logger.warning(
                "cascade ⚠ LLM 리전 불일치 — 설정은 %s 인데 실제는 %s 다(클라이언트 생성 실패 "
                "폴백이었을 수 있다). 이 통화의 지연·원가를 서울 것으로 읽으면 안 된다",
                wanted, self._llm_location or "미상",
            )

    async def _resolve_call_duration(self) -> None:
        """이 통화의 길이 — **env 강제값 > 구독 플랜 > Free**(Live 와 같은 우선순위).

        ⛔ 새 규칙을 만들지 않는다. Live 도 `NORMAL_CALL_DURATION_S`(전 회원 강제)를 먼저 보고
          없으면 `call_service.call_duration_s_for_member`(플랜)를 부른다 — 같은 순서·같은 함수.
        ⚠ R5: 플랜 조회가 실패하면 **Free(5분)로 떨어진다.** 모르면 짧은 쪽이 안전하다 —
          길게 줬다 원가가 새는 것보다 낫다(`call_duration_s_for_member` 자신도 같은 방침).
        """
        forced = settings.NORMAL_CALL_DURATION_S
        if forced is not None:
            self._call_duration_s = float(forced)
            logger.info("cascade 통화 길이: %.0f초(env 강제값)", self._call_duration_s)
            return
        if self._session_factory is None or self._member_id is None:
            logger.info("cascade 통화 길이: %.0f초(Free — 회원 정보 없음)", self._call_duration_s)
            return
        try:
            self._call_duration_s = float(await svc.run_db(
                self._session_factory,
                lambda db: call_service.call_duration_s_for_member(db, self._member_id),
            ))
        except Exception as exc:  # noqa: BLE001 - R5
            self._call_duration_s = call_service.FREE_CALL_DURATION_S
            logger.warning(
                "cascade 통화 길이: 플랜 조회 실패 → Free(%.0f초)로 간다: %s",
                self._call_duration_s, str(exc)[:200],
            )
            return
        logger.info("cascade 통화 길이: %.0f초(구독 플랜)", self._call_duration_s)

    def _backstop_s(self) -> float:
        """세션 절대 백스톱 — ⛔ **정상 종료보다 항상 뒤**여야 한다.

        둘은 다른 층이다(정상 작별 vs 펌프 정지 방어). 백스톱이 먼저 오면 작별을 못 하고
        통화가 뚝 끊긴다 — 이 시계를 넣은 목적 자체가 깨진다.
        ⚠ env 로 통화 길이를 20분 넘게 강제할 수 있으므로 **상수로 두면 안 된다.**
        """
        # ⛔ 여기에 숨은 바닥값을 넣지 마라 — 상수가 코드에 박히면 그게 정책처럼 보이고
        #   테스트도 못 줄인다(이 프로젝트에서 이미 겪었다). 여유는 설정 하나가 정한다.
        floor = max(1.0, float(settings.CASCADE_SESSION_MAX_S))
        return max(floor, self._call_duration_s + max(0.0, settings.CASCADE_FAREWELL_GRACE_S))

    def _close_seed(self) -> str:
        """작별 지시문 — ⛔ **Live 와 같은 문구**(persona_prompt 가 소유). 새로 쓰지 않는다.

        새로 쓰면 두 경로의 마무리가 갈리고, "Live 는 자연스러운데 캐스케이드는 로봇 같다"가 된다.
        """
        return live_close_seed(self._close_tag)

    async def _watch_call_clock(self) -> None:
        """통화 시계 — 시간이 되면 **작별을 시킨다**(뚝 끊지 않는다).

        ⭐ 주입 방식(캐스케이드 설계): 선톡과 **같은 파이프**로 대답 하나를 만든다.
          Live 는 살아 있는 Live 세션에 시드를 밀어 넣지만, 캐스케이드는 매 턴 LLM 을 새로
          부르므로 "시드를 사용자 발화 자리에 넣어 대답을 만들게" 하는 게 기존 배관 그대로다
          (`seed_opening()` 이 이미 그 방식으로 선톡을 만든다 — **새 파이프가 아니다**).
        ⚠ 비버가 말하는 중이면 그 턴이 끝난 뒤에 낸다 — 겹쳐 말하면 불변식 I1 위반이다.
        ⛔ 마감(usage·통화행)은 여기서 하지 않는다. `run()` 의 finally 가 이미 그 일을 한다 —
          두 곳에서 마감하면 한 통화가 두 번 저장된다.
        """
        await asyncio.sleep(max(1.0, self._call_duration_s))
        logger.info("cascade 통화 시간 종료(%.0f초) → 작별 시드 주입", self._call_duration_s)
        grace = max(0.0, float(settings.CASCADE_FAREWELL_GRACE_S))
        deadline = time.monotonic() + grace
        while self._reply_busy() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        if not self._farewell_started:
            self._farewell_started = True
            await self._start_reply(self._close_seed(), is_greeting=True)
        # 작별이 다 나갈 때까지 기다렸다가 닫는다(중간에 끊으면 뚝 끊기는 것과 같다).
        deadline = time.monotonic() + grace
        while self._reply_busy() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        # ⛔ 여기서 통지하지 않는다. `call_id` 는 종료 마감(`_finalize_call`) 뒤에 확정되고,
        #   통지가 두 곳이면 **0회 또는 2회**가 되기 쉽다. 사유만 남기고 끝낸다.
        self._end_reason = "duration"
        raise _Stop

    def _marker_plan(self, segment: _OpenSegment) -> list[tuple[int, str, str]]:
        """이 구간에서 **언제(누적 바이트) 어떤 마커를 낼지** — (경계, 자막, 감정) 목록.

        ⛔ 경계는 **추정**이다. TTS 는 구간 하나에 오디오 하나를 주고 문장 정렬을 안 준다 —
          정확히 알려면 문장별로 따로 요청해야 하는데(그 안은 채택되지 않았다), 그러면 짧은
          문장 뒤에 벤더 왕복이 통째로 공백이 된다.
        ⚠ 첫 문장의 경계는 **항상 0** 이다 ⇒ 구간이 문장 하나면 예전과 **완전히 같은 동작**이다
          (첫 오디오 직전에 마커 하나). 추정이 끼어드는 건 두 번째 문장부터다.
        ⚠ 글자당 바이트는 **이 통화에서 실측한 값**을 언어별로 쓴다(없으면 기본값). 같은 언어·
          같은 음성·같은 배속이라 구간 안에서는 거의 선형이다.
        """
        sentences = segment.sentences or [(segment.text, segment.emotion)]
        if len(sentences) <= 1:
            return [(0, sentences[0][0], sentences[0][1])] if sentences else []
        bpc = self._bytes_per_char.get(segment.language, _DEFAULT_BYTES_PER_CHAR)
        plan: list[tuple[int, str, str]] = []
        chars = 0
        for text, emotion in sentences:
            plan.append((int(chars * bpc), text, emotion))
            chars += len(text)
        return plan

    def _note_marker_drift(self, segment: _OpenSegment, sent: int,
                           plan: list[tuple[int, str, str]]) -> None:
        """추정이 얼마나 틀렸나 — 그리고 **다음 구간을 위해 배운다**.

        ⛔ 추정으로 위치를 잡았으면 그 오차가 보여야 한다. 안 보이면 "자막이 좀 늦네"를
          원인 없이 겪는다(오늘 하루 우리가 반복해 밟은 자리다).
        ⚠ 참값(진짜 문장 경계)은 **모른다** — 그래서 재는 것은 "구간 전체 길이를 얼마나 맞췄나"다.
          그게 틀린 만큼 문장 경계도 같은 비율로 틀어진다.
        """
        chars = sum(len(t) for t, _ in (segment.sentences or []))
        if sent <= 0 or chars <= 0:
            return
        actual = sent / chars
        if len(plan) > 1:
            before = self._bytes_per_char.get(segment.language, _DEFAULT_BYTES_PER_CHAR)
            self._marker_drifts.append(int(round((before / actual - 1.0) * 100)))
        # ⭐ 다음 구간은 이 통화에서 **실제로 들린 속도**로 추정한다(첫 구간만 기본값이다).
        self._bytes_per_char[segment.language] = actual

    def _marker_drift_log(self) -> str:
        """`자막오차=[+12%, -8%]` — 양수면 **늦게** 꽂았다는 뜻이다(추정이 길었다)."""
        if not self._marker_drifts:
            return "자막오차=-"
        return "자막오차=[%s]" % ", ".join("%+d%%" % d for d in self._marker_drifts)

    async def _send_sentence_marker(self, segment: _OpenSegment,
                                    text: str | None = None,
                                    emotion: str | None = None) -> None:
        """구간 마커 — 표정 + **자막**을 그 구간 오디오 앞에 끼운다.

        ⛔ 캐스케이드는 지금까지 **비버 자막을 한 번도 안 보냈다**(Live 는 보낸다) —
          사용자가 비버 말을 글로 못 봤다. 프레임을 둘로 나누지 않고 여기서 같이 낸다:
          같은 사실(어느 문장을 지금 말하나)의 출처가 둘이면 반드시 어긋난다.
        ⚠ `text` 는 **실제로 소리 나가는 문장**이다 — 태그 제거·꼬리 버림이 끝난 값
          (`_open_segment` 가 그렇게 만들어 둔다). 자막이 소리와 다르면 안 된다.
        ⚠ `server_bytes` 는 이 구간 **직전까지** 그 턴에서 보낸 누적이다 — 프론트 원장과
          정수로 대조하는 교차검증용이고, 순서가 주 키다.
        """
        await self._safe(CascadeSentenceMarker(
            turn_id=self.beaver.turn_id or "",
            seq=self._segment_seq,
            emotion=emotion or segment.emotion,
            text=segment.text if text is None else text,
            server_bytes=self.beaver.sent_bytes,
        ))
        self._segment_seq += 1
        self._sentence_markers += 1
        # ⭐ 이 대답에서 **실제로 나간 표정들** — 턴당 하나만 찍던 `감정=` 으로는 문장별 감정이
        #   도는지 안 도는지 알 수가 없다(같은 값이 3개여도 보이지 않는다).
        self._marker_emotions.append(emotion or segment.emotion)

    def _reply_busy(self) -> bool:
        """비버가 아직 말하는(또는 만드는) 중인가."""
        return self._reply_task is not None and not self._reply_task.done()

    async def _load_call_context(self) -> None:
        """캐릭터·프롬프트 입력·음색을 **Live 와 같은 함수**로 읽는다(설계 §1).

        ⛔ 실패해도 통화는 계속된다(R5) — env 기본 페르소나로 가고 그 사실을 로그로 남긴다.
          "설명 언어가 틀린 통화"가 "통화 불가"보다 낫다.
        """
        # ⛔ 통화 길이는 **DB 유무와 무관하게** 정한다 — env 강제값(NORMAL_CALL_DURATION_S)은
        #   회원이 없어도 유효하고, 없으면 Free 로 떨어진다. 이걸 DB 가지 안에 두면
        #   데모·테스트에서 시계가 영영 20분(백스톱)이 된다.
        await self._resolve_call_duration()
        if self._session_factory is None or self._member_id is None:
            self._resolve_languages()      # 데모 경로 — env 기본값으로 확정만 한다
            return
        try:
            self._character_id = await svc.run_db(
                self._session_factory,
                lambda db: svc.resolve_call_character(db, self._member_id, None),
            )
            # ⭐ **캐릭터를 정한 그 자리에서 알린다** — Live 도 같은 시점이다
            #   (call_session.py: resolve_call_character 직후, `call_id` 확정 **전**).
            #   이건 통화 시작 알림이라 call_id 를 기다릴 이유가 없고, 기다리면 앱이 그만큼
            #   오래 엉뚱한 얼굴을 띄운다.
            await self._announce_character()
            # ⚠ 언어 스코프가 필요하다 — 학습 대상 언어로 레벨·커리큘럼을 고른다.
            spec = resolve_language(self._member_target_language or self._target_code)
            code = spec.code if spec else self._target_code
            self._setup = await svc.run_db(
                self._session_factory,
                lambda db: svc.load_call_setup(db, self._member_id, self._character_id, code),
            )
        except Exception as exc:  # noqa: BLE001 - R5
            logger.warning(
                "cascade: 통화 설정 조회 실패 — env 기본 페르소나로 계속한다: %s", str(exc)[:200]
            )
            self._setup = None
        self._resolve_languages()
        self._voice = (self._setup or {}).get("voice") or None
        # ⭐ 통화 행 — **Live 와 같은 함수**. call_type 은 항상 normal 이다(사장님: 레벨테스트는
        #   나중에). ⛔ 실패해도 통화는 계속된다(call_id=None → 기록만 못 남긴다 — R5).
        try:
            self._call_id = await svc.run_db(
                self._session_factory,
                lambda db: svc.create_call(
                    db, self._member_id, self._character_id, "normal",
                    target_language=self._target_code,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - R5
            logger.warning("cascade: 통화 행 생성 실패 — 기록 없이 계속한다: %s", str(exc)[:200])
            self._call_id = None
        if (self._setup or {}).get("needs_level_test"):
            # ⚠ 레벨 미확정이지만 캐스케이드는 **레벨테스트를 안 돌린다**(사장님 결정).
            #   Live 라면 여기서 라우팅이 갈린다 — 그 차이를 로그로 드러낸다.
            logger.info("cascade: 레벨 미확정이지만 call_type=normal 로 진행(레벨테스트 미지원)")
        logger.info(
            "cascade 캐릭터: member=%s character=%s 음색=%s(출처=%s) 레벨프로파일=%s",
            self._member_id, self._character_id,
            self._voice or (settings.CASCADE_TTS_VOICE or "").strip() or "언어 기본",
            # ⭐ 값만 찍으면 **어디서 온 값인지 모른다** — `음색=Sulafat` 이 DB 인지 env 인지
            #   구별이 안 돼 오늘 조사에서 시간을 썼다. 출처를 같이 박는다.
            "DB" if self._voice else ("env" if (settings.CASCADE_TTS_VOICE or "").strip() else "기본"),
            "있음" if (self._setup or {}).get("level_profile") else "없음",
        )

    async def _announce_character(self) -> None:
        """이 통화의 캐릭터를 클라에 알린다 — 통화당 **1회**, 오디오가 흐르기 전.

        ⛔ 안 보내면 앱은 **자기가 고른 캐릭터 얼굴**로 폴백한다. 서버가 다른 캐릭터를
          고르면(수신통화=알람 캐릭터, 그 외=member.character_id) **목소리와 얼굴이
          어긋난다** — 에러 없이 조용히.
        ⚠ 이름은 **더 있으면 좋은 것**이지 필수가 아니다(프론트는 환경마다 다른 id 대신
          이름으로 자산을 고른다). 조회가 실패해도 id 는 반드시 보낸다 — R5.
        """
        if self._character_id is None:
            return
        name = None
        try:
            name = await svc.run_db(
                self._session_factory,
                lambda db: svc.character_display_name(db, self._character_id),
            )
        except Exception as exc:  # noqa: BLE001 - R5
            logger.warning("cascade: 캐릭터 이름 조회 실패 — id 만 보낸다: %s", str(exc)[:200])
        await self._safe(CascadeCallStarted(character_id=self._character_id, name=name))
        logger.info("cascade 통화 시작 통지: character=%s(%s)", self._character_id, name or "이름없음")

    def _tts_voice(self) -> str | None:
        """이 통화의 TTS 음색 — **env override > DB 캐릭터 > 언어 기본**.

        ⭐ 로스터(프리빌트 30종)는 **Gemini Live 캐릭터 voice 와 같다**(`core/tts.py`), 그래서
          DB 값이 Gemini-TTS·Chirp 에 그대로 먹는다(문자열 형식은 `_resolve_voice` 가 만든다).
        ⛔ OpenAI TTS 는 로스터가 **아예 다르다**(nova·alloy…). 대응표의 근거가 없어
          **서버 기본값으로 가고 그 사실을 로그로 남긴다** — 조용히 틀린 음성을 쓰면
          "캐릭터 목소리가 이상하다"를 아무도 설명 못 한다(사장님 결정: OpenAI 는 보류).
        """
        override = (settings.CASCADE_TTS_VOICE_OVERRIDE or "").strip()
        if override:
            return override
        if self._voice and self._tts_engine == _OPENAI_TTS_CHOICE:
            if not self._voice_warned:
                self._voice_warned = True
                logger.warning(
                    "cascade 캐릭터 음색 미적용(%s) — OpenAI 는 음성 로스터가 달라 대응표가 없다. "
                    "서버 기본값으로 간다", self._voice,
                )
            return None
        # DB 캐릭터 음색이 없으면 서버 기본값(env). ⚠ 이 값은 **덮어쓰기가 아니다** —
        #   배포 env 에 값이 들어 있어 덮어쓰기로 쓰면 DB 가 영영 안 먹는다(config 주석).
        if self._voice:
            return self._voice
        # ⚠ 여기까지 왔다 = **DB 캐릭터에 음색이 없다**(`character.voice_id` NULL 등).
        #   그러면 이 통화는 그 캐릭터의 목소리가 아니다 — 그런데 에러도 없다. 조용히
        #   서버 기본값(env)이나 언어 기본 음성이 나간다. **그 사실이 보여야 한다.**
        fallback = (settings.CASCADE_TTS_VOICE or "").strip() or None
        if not self._voice_fallback_warned:
            self._voice_fallback_warned = True
            logger.warning(
                "cascade 음색 폴백 — DB 캐릭터에 음색이 없다(character=%s) → %s. "
                "이 통화는 **그 캐릭터 목소리가 아니다**",
                self._character_id, ("env %s" % fallback) if fallback else "언어 기본 음성",
            )
        return fallback

    def _system_instruction(self) -> str:
        """페르소나는 **새로 만들지 않는다** — normalcall 과 같은 조립기를 쓴다(설계 §1-2).

        두 엔진이 같은 지시문을 써야 품질 비교가 성립한다. 캐스케이드 데모는 DB 가 없어
        캐릭터·레벨을 못 읽으므로 데모용 기본값으로 채운다(P1 이후 통화 기록에 올릴 때
        normalcall 과 같은 값으로 바꾼다).
        """
        if self._system_cache is None:
            setup = self._setup or {}
            # ⭐ **DB 값이 있으면 그게 이긴다.** 없으면 예전 데모 기본값 그대로(R5).
            self._system_cache = build_system_instruction(
                role=setup.get("role") or settings.CASCADE_PERSONA_ROLE,
                personality=setup.get("personality") or settings.CASCADE_PERSONA_PERSONALITY,
                level_profile=setup.get("level_profile") or settings.CASCADE_PERSONA_LEVEL,
                locale=self._locale,
                interests=setup.get("interests") or [],
                # ⭐ 학습자 이름 — **DB(member.name)** 에서 온다(2026-08-12). Live 는 이미
                #   `name=setup["name"]` 을 넘기고 있었고 캐스케이드만 빠져 있었다. 안 넘기면
                #   프롬프트의 이름 자리에 폴백 문자열 "학습자"가 들어가 비버가 사람을 그렇게
                #   부른다. ⚠ 이름이 없을 수 있다(소셜 가입·미입력) — `None` 이면 조립기가
                #   예전과 **바이트 동일한** 문자열을 만든다(폴백이 원래 그 값이다).
                name=setup.get("name"),
                # ⭐⭐ **이건 결손이 아니라 틀린 내용의 주입이었다**(2026-08-15). 안 넘기면
                #   조립기 기본값 `"beginner"` 가 들어가 `_LANG_POLICY` 가 초급 언어정책을
                #   고른다 — 왕초보도 고급자도 **전원 초급 정책**을 받고 있었다.
                #   ⚠ 값은 이미 손에 있었다: `load_call_setup` 이 `lang_band` 를 담아 준다
                #     (`normalcall_service.py:296` = `mastery_repository.band_of(...)`).
                #     Live 는 넘기고 있었고(`call_session.py:1088`) 캐스케이드만 빠져 있었다.
                lang_band=setup.get("lang_band", "beginner"),
                target_language=self._target_label,
                # ⛔ 시드와 **같은 태그**여야 모델이 그 문구를 시스템 지시로 읽는다.
                close_tag=self._close_tag,
                # ⭐ 마커 표기 규칙을 켠다(캐스케이드 전용). normalcall 은 기본값 False 라
                #   출력이 바이트 동일하게 유지된다.
                # ⭐ 문구의 숫자를 **우리 상한에서** 만든다 — 손으로 쓴 숫자가 두 곳에 있으면
                #   서버는 3에서 끊는데 프롬프트는 4를 시키는 모순이 다시 생긴다.
                max_sentences=self._max_sentences(),
                language_marker=True,
                # ⭐ 감정 태그도 **캐스케이드 전용**이다. ⛔ Live 에 켜면 모델이 태그를 그대로
                #   읽어 버린다 — 서버가 걷어낼 자리가 없다(Live 는 모델이 직접 소리를 낸다).
                emotion_tags=tuple(EMOTION_STYLES),
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
            # ⚠ 진짜 대답이 돌고 있으면 **건드리지 않는다.** dev 훅과 대답 경로는 다른
            #   태스크 축이라, 여기서 IDLE 로 덮으면 말하는 중인 비버의 상태가 사라진다.
            if self.state == TurnState.BEAVER_SPEAKING and (
                self._reply_task is None or self._reply_task.done()
            ):
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
        # ⛔ **I6 위반의 유일한 외부 증인**(2026-08-12). 우리가 짝수 바이트를 보장하는데
        #   클라가 홀수를 셌다면 그 사이 어딘가에서 정렬이 깨진 것이다. 클라 큐는 홀수가
        #   와도 이어붙어 재생이 안 깨지므로 **자연 신호가 없다** — 이 숫자가 유일하다.
        #   ⚠ 구버전 클라는 안 보낸다(기본 0) → 조용히 넘어간다(R5).
        if progress.odd_frames and not self._odd_frames_warned:
            self._odd_frames_warned = True
            logger.warning(
                "cascade ⚠⚠ 클라가 **홀수 길이 오디오 프레임 %d개**를 받았다(불변식 I6 위반). "
                "서버 정렬(sample_aligned)을 안 타는 경로가 있다 — turn=%s",
                progress.odd_frames, progress.turn_id,
            )
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
            if picked in _TTS_CHOICES and not _TTS_PROFILES[picked].is_configured():
                # ⛔ 키가 없으면 **명확히 거절한다.** 조용히 다른 엔진으로 바꾸면 사장님이
                #   "OpenAI 소리"로 착각하신다(ElevenLabs 에서 했던 그대로).
                #   ⭐ 판정은 성질 표가 한다 — 새 엔진의 키 검사가 빠지는 걸 막는다.
                logger.warning("cascade tts 엔진 거절: %s — API 키 미설정", picked)
            elif picked in _TTS_CHOICES:
                self._tts_engine, source = picked, "클라 지정"
            else:
                logger.warning("cascade tts 엔진 값 거절: %r — 서버 기본값으로 진행", picked[:40])
        raw_rate = ctrl.get("speakingRate", ctrl.get("speaking_rate"))
        if raw_rate is not None:
            try:
                self._tts_rate = float(raw_rate)
            except (TypeError, ValueError):
                logger.warning("cascade speaking_rate 값 거절: %r", raw_rate)
        # ⭐ **언어별 배속**(데모 화면). 화면은 서버의 언어 코드를 모르므로 '설명/한국어'로
        #   보내고, 여기서 이 통화의 실제 코드에 얹는다. 범위 밖·숫자 아님은 거절한다.
        for key, language in (
            ("speakingRateNative", self._locale),
            ("speakingRateTarget", self._target_code),
        ):
            raw = ctrl.get(key)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                logger.warning("cascade %s 값 거절: %r", key, raw)
                continue
            if not (_RATE_MIN <= value <= _RATE_MAX):
                logger.warning("cascade %s 범위 밖 거절: %r", key, raw)
                continue
            self._tts_rate_by_lang[(language or "").strip().lower()] = value
        style = ctrl.get("stylePrompt", ctrl.get("style_prompt"))
        if isinstance(style, str):
            self._tts_style = style.strip()[:_STYLE_PROMPT_MAX]
        # ⭐ 엔진에 맞는 선행 버퍼를 잡는다. Gemini 는 합성이 재생보다 최대 1.5초 뒤처져서
        #   200ms 만 모으고 시작하면 **반드시 언더런이 난다**(그게 '끊긴다'의 정체였다).
        #   배속 자체는 1.7~1.9x 라 초반만 견디면 격차가 벌어져 안 끊긴다.
        lead = self._profile().lead_setting
        self.beaver.lead_ms = None if lead is None else max(0, int(getattr(settings, lead)))
        # ⭐ 세션 시작에 한 줄 — 이 통화의 소리가 어느 엔진 것인지 여기서 확정된다.
        logger.info(
            "cascade 엔진 선택: %s (%s) speaking_rate=%.2f(%s) 언어별=%s "
            "선행버퍼=%dms 묶음=%d자 style=%r",
            self._tts_engine or tts.CHIRP3_ENGINE, source,
            self._speaking_rate() or 1.0,
            "클라 지정" if self._tts_rate is not None else "엔진 기본값",
            self._tts_rate_by_lang or "없음",
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
        ⭐ 2026-08-12: **회원 DB 에서 온다**(세션의 `_target_code`/`_locale`). 예전엔 env 두 개라
          모든 학습자가 같은 언어로 취급됐다.
        ⛔⛔ 그렇다고 **STT 에 언어를 지정하는 건 아니다.** 코드를 **둘** 넘기면 OpenAI 어댑터는
          `language` 를 **안 싣는다**(= 자동 감지). 실측이 그 편을 지지한다 — 자동 감지가
          41개 언어 중 39개를 맞혔고, `language` 를 미지정/en/ko 로 바꿔 재도 결과가 같았다.
          여기 값은 **우리 의도의 기록**이고 벤더에 강제되는 값이 아니다.
        학습 언어를 먼저 적는다 — 문서가 순서에 의미를 부여하지는 않지만(REST 레퍼런스는
        "most likely language detected" 라고만 한다), 이 통화의 주 언어가 무엇인지 우리 의도를
        코드에 남긴다. 벤더 문서의 권고("bare minimum")대로 2개까지만 쓴다.
        """
        return [self._target_code, self._locale]

    def _on_route_change(self, ctrl: dict) -> None:
        """통화 **도중** 출력 장치가 바뀌었다 — AEC 정책을 다시 태운다.

        ⛔ **같은 함수를 다시 부른다**(`_apply_aec_hint`). 새 판정을 만들면 시작(`start.aec`)과
          도중이 갈리고, 그때부터 "어느 쪽이 진짜인지" 아무도 못 말한다.
        ⛔ **진행 중인 턴을 깨지 않는다.** 정책은 다음 판정부터 적용된다 — 말하는 중에
          게이트를 갈아끼우면 그 턴의 판정 근거가 중간에 바뀌어 barge-in 이 반쯤 적용된다.
          (불변식 영역이라 깨야 할 이유가 생기면 근거를 올리고 바꾼다.)
        ⚠ 이 프레임은 **안 올 수 있다**(클라 콜백 등록 실패 시 조용히 없다). 보조 신호일 뿐이라
          안 와도 동작은 예전 그대로여야 한다.
        """
        try:
            change = ClientRouteChange.model_validate(ctrl)
        except ValidationError as exc:
            # ⛔ 거절이 아니라 무시다 — 진단 프레임 하나 때문에 통화가 흔들리면 안 된다(R5).
            logger.warning("cascade route_change 형식 오류(무시) — %s", exc)
            return
        before_mode, before_gate = self._aec_mode, self._energy_gate
        self._apply_aec_hint(change.aec.model_dump() if change.aec is not None else None)
        route = (change.aec.route if change.aec is not None else "") or "미상"
        logger.info(
            "cascade 라우트 변경: %s → %s · 게이트 %s → %s · 라우트=%s · 업링크=%d바이트(%.1f초)"
            " — 진행 중인 턴은 그대로, 다음 판정부터 적용",
            before_mode, self._aec_mode,
            "on" if before_gate else "off", "on" if self._energy_gate else "off",
            route, change.uplink_bytes, change.uplink_bytes / 32000.0,
        )

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
            " 전사대기=%dms",
            self._sid, self._aec_mode, self._bargein_confirm,
            "on" if self._energy_gate else "off", settings.CASCADE_BARGEIN_RMS,
            settings.CASCADE_BARGEIN_PENDING_MS,
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


async def run_cascade(
    websocket: WebSocket,
    genai_client: Any = None,
    *,
    llm_client: Any = None,
    llm_location: str = "",
    session_factory: Any = None,
    member_id: int | None = None,
    member_target_language: str | None = None,
) -> None:
    """WS 캐스케이드 세션 구동(라우터에서 accept 후 위임). 소켓 정리까지 책임.

    genai_client 가 None 이면 비버는 말하지 않고 턴 감지만 돈다(P0 동작 — R5).

    ⭐ DB 인자 3종은 **Live 의 `run_call` 과 같은 모양**이다(설계 §1). 없으면(데모·테스트)
      예전처럼 env 기본값으로 돈다 — 통화가 죽지 않는다(R5).
    ⛔ `session_factory` 를 전역에서 import 하지 않고 **인자로 받는다**: Live 가 그렇고,
      테스트가 가짜 팩토리를 넣을 수 있어야 한다.
    """
    session = CascadeSession(
        WsCascadeTransport(websocket), genai_client,
        llm_client=llm_client,
        llm_location=llm_location,
        session_factory=session_factory,
        member_id=member_id,
        member_target_language=member_target_language,
    )
    try:
        await session.run()
    finally:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass
