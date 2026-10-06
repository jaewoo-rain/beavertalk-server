"""OpenAI Realtime 세션 어댑터 — normalcall 통화 엔진 하나.

## 이 파일이 지켜야 하는 두 계약
1. **호출부 계약**: `domains/learning/realtime/call_session.py` 가 쓰는 세션 인터페이스
   6개(`send_audio`·`send_reground`·`send_text_turn`·`send_persona`·`send_tool_response`·
   `events`)와 이벤트 모양. 호출부는 **덕타이핑**으로 쓴다(`isinstance` 검사 0건 —
   `LiveEvent` 는 타입 주석 자리에만 쓰인다) ⇒ 같은 필드 이름을 가진 우리 이벤트를
   올리면 호출부가 **한 줄도 안 바뀐다**.
2. **벤더 계약**: OpenAI Realtime WS(GA, `gpt-realtime-2.1-*`).

## ⛔ Gemini 자산을 import 하지 않는다
`core.prompts.*` · `core.gemini_live` · `core.persona_prompt` 는 이 패키지에서 금지다
(`tests/test_openai_isolation.py` 가 AST 로 전수 검사한다). 그래서 이벤트 dataclass 와
usage shim 도 **여기서 새로 정의한다** — 베끼는 것이 아니라 «호출부가 읽는 필드»를
우리가 채우는 것이다. 필드가 어긋나면 `tests/test_openai_live.py` 가 잡는다.

## 도메인을 모른다
`call_id`·`member_id`·플랜·커리큘럼·DB 를 모른다. `system_instruction`·`voice`·«표정을
쓰나»는 호출부(realtime)가 조립해 넘긴다 — `core/gemini_live.py` 와 같은 어댑터 규율.

## 이 엔진에서 **일부러 안 하는 것**
- 세션 재개(`resume_handle`) · 조각 분할 · 컨텍스트 압축(truncation) · 재접지 자동 주입.
  OpenAI 는 128k 창에 15분 통화가 ~6% 로 끝나고, truncation 을 켜면 캐시 프리픽스가
  깨져 **캐시 할인(정가 $10 → $0.30/1M)을 전부 잃는다.**
- `go_away` · `resume_update` 이벤트: **영원히 올리지 않는다.** 그 두 분기(호출부)가
  죽은 코드로 남는 것이 조건분기를 더하는 것보다 싸고, `resume_handle` 이 영구 None
  이라 재개 kwarg 가 어댑터로 넘어오지도 않는다(연쇄가 저절로 맞는다).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

from core.openai.audio import AUDIO_FORMAT, Upsampler16kTo24k
from core.openai.tools import AUTO_ACK_TOOLS, set_face_tool

logger = logging.getLogger(__name__)

# ── 벤더 상수 ────────────────────────────────────────────────────────────────── #
OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime"
OPENAI_REALTIME_MODEL_DEFAULT = "gpt-realtime-2.1-mini"

# ⭐ 음색. **`marin` 을 고른 근거 한 줄**: 2026-10-03 단가 스파이크가 실제로 세션을 열어
#   오디오를 받아 본 유일한 음색이다(`scripts/spike_openai_realtime_cost.py`) — 다른 값은
#   이름만 알고 들어 본 적이 없다. ⚠ mini 는 커스텀 음색을 지원하지 않으므로 Baba 의
#   Gemini 음색(`Fenrir`)은 못 옮긴다. 바꾸려면 `OPENAI_REALTIME_VOICE` env 한 줄이다.
DEFAULT_VOICE = "marin"
# ⛔⛔ OpenAI 가 받는 음색은 **이 10개뿐**이다(2026-10-05 실측 — 벤더 오류 메시지가 집합을
#   그대로 돌려줬다: `Invalid value: 'Fenrir'. Supported values are: ...`).
#   ⚠ 호출부(`call_session`)는 **DB 의 캐릭터 음색**을 넘긴다 — 그건 Gemini 음색 이름이다
#     (Baba = `Fenrir`). 그 값이 그대로 가면 `session.update` 가 거부되고 통화가 **열리기도
#     전에** 죽는다(실측: call 1733·1734 → LIVE_START_FAILED).
#   ⇒ 음색 이름을 아는 것은 **이 어댑터뿐**이므로 경계에서 걸러야 한다. 호출부를 고치면
#     Gemini 경로가 같이 바뀌고, 캐릭터가 늘 때마다 두 곳을 맞춰야 한다.
#   ⚠ 이 집합을 손으로 늘리지 마라 — 벤더가 늘리면 거부 메시지로 알려 준다.
SUPPORTED_VOICES = frozenset({
    "alloy", "ash", "ballad", "coral", "echo",
    "sage", "shimmer", "verse", "marin", "cedar",
})

# 입력 전사 모델. ⚠ **미검증** — 10-03 스파이크는 전사를 아예 안 켰다(대본 구동이라
#   필요가 없었다). 세션이 이 이름을 거절하면 어댑터가 **전사 없이 한 번 더 시도**한다
#   (아래 `_apply_session`) — 통화가 열리는 것이 먼저고, 전사는 env 로 갈아끼운다.
DEFAULT_TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"

# ⭐⭐ **턴 경계는 `semantic_vad`**(2026-10-03 실측). 기본 `server_vad`
#   (`silence_duration_ms` 500)는 학습자의 쉼 3점을 **전부 턴으로 쪼갰다**.
#   `semantic_vad` 는 1.98초 쉼까지 버텼다(2.48초는 어느 설정으로도 못 막는다 — 한계 수용).
TURN_DETECTION: dict = {"type": "semantic_vad"}

# ⛔⛔ **동시 통화 1건**(2026-10-04 사장님 결정 1). org TPM 40,000 에서 표정 ON 통화 1건이
#   분당 ~30k 를 쓴다 — 2건이면 한도를 넘고, 넘으면 통화가 **조용히** 안 열린다.
#   상향은 사장님이 직접 한다. 그때까지 여기서 묶는다.
#   ⚠ 프로세스 단위다(Cloud Run 인스턴스 1개 = 1건). 인스턴스가 2개로 늘면 2건이 된다 —
#     전역 게이트는 DB 가 필요하므로 1차 범위 밖이고, 그 사실을 여기 적어 둔다.
MAX_CONCURRENT_SESSIONS = 1

_SESSION_OPEN_TIMEOUT_S = 30.0
_SESSION_ACK_TIMEOUT_S = 10.0

_active_sessions = 0


class OpenAIRealtimeBusy(RuntimeError):
    """동시 통화 상한에 걸렸다. 호출부는 세션 열기 실패로 다루면 된다(`LIVE_START_FAILED`)."""


class OpenAIRealtimeError(RuntimeError):
    """세션을 열 수 없다(키 없음·설정 거부 등). 호출부는 세션 열기 실패로 다룬다."""


# ── 호출부가 읽는 이벤트 모양 ────────────────────────────────────────────────── #
# ⛔ 필드 이름이 계약이다. `call_session` 이 읽는 것:
#     kind · audio · text · is_final · fn_name · fn_id · fn_args · auto_acked · usage
#   그리고 **안 올리는 두 종류**가 쓰는 칸(`time_left`·`resume_handle`·`resumable`)도
#   기본값으로 둔다 — 호출부가 `event.time_left` 를 무조건 읽는 분기가 있어도
#   AttributeError 가 나지 않게(그 분기엔 영영 들어가지 않지만, 방어는 공짜다).
@dataclass(slots=True)
class OpenAIRealtimeEvent:
    """OpenAI 서버 이벤트를 호출부가 다루기 쉽게 정규화한 단일 이벤트."""

    kind: str
    audio: Optional[bytes] = None      # kind=="audio": 출력 PCM24k(변환 없음 — 앱 재생과 같은 레이트)
    text: Optional[str] = None         # kind in {in_tr,out_tr}
    is_final: bool = False             # 입력 전사 확정 여부(.completed 에서 True)
    time_left: Optional[str] = None    # ⛔ 영영 안 채운다(go_away 를 안 올린다)
    fn_name: Optional[str] = None
    fn_id: Optional[str] = None
    fn_args: Optional[dict] = None
    auto_acked: bool = False
    usage: Optional[Any] = None        # kind=="usage": 아래 _UsageShim
    resume_handle: Optional[str] = None  # ⛔ 영영 안 채운다
    resumable: bool = False              # ⛔ 영영 False


# ── usage shim ───────────────────────────────────────────────────────────────── #
# 호출부(`_record_usage`·`_modality_pairs`)는 전부 `getattr` 로 읽는다 ⇒ 같은 속성 이름을
# 가진 가벼운 객체를 올리면 호출부가 **0줄**이다. 모달리티 이름은 "AUDIO"/"TEXT" —
# 그게 `save_call_usage` 가 컬럼으로 승격하는 키다(normalcall_service.py:2139).
@dataclass(slots=True, frozen=True)
class _Mod:
    modality: str
    token_count: int


@dataclass(slots=True, frozen=True)
class _UsageShim:
    prompt_token_count: int
    response_token_count: int
    total_token_count: int
    cached_content_token_count: Optional[int]
    prompt_tokens_details: tuple
    response_tokens_details: tuple
    # ⭐ 캐시의 **모달리티 분해**. 원가 계기판이 이 값을 필요로 한다 — in_audio 의 89.8%가
    #   캐시($0.30/1M)인데 전액 정가($10/1M)로 계산하면 33배 과대계상이다.
    #   ⚠ Gemini 쪽은 이 속성이 없다 ⇒ 호출부가 `getattr(um, ..., None)` 으로 읽어
    #     빈 리스트가 되고, 요약에서 키 자체가 빠진다(Gemini 행 바이트 동일).
    cached_tokens_details: tuple
    # Gemini 전용 칸 — 값이 없다는 뜻으로 None 을 둔다(0 이 아니다: "안 쟀다"와 "0" 은 다르다).
    thoughts_token_count: Optional[int] = None
    tool_use_prompt_token_count: Optional[int] = None


def _mods(details: dict | None) -> tuple:
    """OpenAI `{audio_tokens, text_tokens}` → `(_Mod("AUDIO", n), _Mod("TEXT", n))`."""
    d = details or {}
    out = []
    for key, name in (("audio_tokens", "AUDIO"), ("text_tokens", "TEXT")):
        try:
            n = int(d.get(key) or 0)
        except (TypeError, ValueError):
            n = 0
        if n:
            out.append(_Mod(name, n))
    return tuple(out)


def usage_shim(usage: dict | None) -> Optional[_UsageShim]:
    """`response.done` 의 `response.usage` → 호출부가 읽는 모양. 값이 없으면 None.

    매핑(10-03 실측 필드 — `scripts/spike_openai_realtime_cost.py:_usage_row` 와 같은 경로):
      `input_tokens`→prompt · `output_tokens`→response · `total_tokens`→total ·
      `input_token_details.cached_tokens`→cached_content_token_count ·
      `input_token_details.{audio,text}_tokens`→prompt_tokens_details ·
      `output_token_details.{audio,text}_tokens`→response_tokens_details ·
      `input_token_details.cached_tokens_details.{audio,text}_tokens`→cached_tokens_details
    """
    if not usage:
        return None
    itd = usage.get("input_token_details") or {}
    otd = usage.get("output_token_details") or {}
    ctd = itd.get("cached_tokens_details") or {}
    cached = itd.get("cached_tokens")
    return _UsageShim(
        prompt_token_count=int(usage.get("input_tokens") or 0),
        response_token_count=int(usage.get("output_tokens") or 0),
        total_token_count=int(usage.get("total_tokens") or 0),
        cached_content_token_count=None if cached is None else int(cached),
        prompt_tokens_details=_mods(itd),
        response_tokens_details=_mods(otd),
        cached_tokens_details=_mods(ctd),
    )


# ── 세션 설정 ────────────────────────────────────────────────────────────────── #
def _transcribe_language(input_language_codes: Optional[list[str]]) -> Optional[str]:
    """BCP-47 목록(["ko-KR","en-US"]) → 전사 힌트용 ISO-639-1 하나("ko").

    OpenAI 전사는 언어를 **하나만** 받는다. 첫 값은 호출부가 «학습 대상 언어» 를 먼저
    넣어 주므로(그게 측정 대상이다) 그걸 쓴다. 비면 힌트 없이(자동 감지) 간다.
    """
    for code in input_language_codes or []:
        head = str(code or "").strip().replace("_", "-").split("-")[0].lower()
        if head:
            return head
    return None


def build_session_config(
    *,
    system_instruction: str,
    voice: str = DEFAULT_VOICE,
    with_face_tool: bool = False,
    input_language_codes: Optional[list[str]] = None,
    transcribe_model: Optional[str] = DEFAULT_TRANSCRIBE_MODEL,
    max_output_tokens: int = 0,
) -> dict:
    """`session.update` 페이로드 1개.

    근거: developers.openai.com Realtime 가이드 + 2026-10-03 **실호출로 검증된**
    스파이크 페이로드(`type:"realtime"` · `output_modalities` · `audio.{input,output}`).
    ⛔ 입·출력 포맷이 같아야 한다(세션 중 변경 불가) — 둘 다 `audio/pcm` 24kHz.
    ⛔ truncation 을 켜지 않는다(기본 off 유지) — 켜면 캐시 프리픽스가 깨진다.
    """
    audio_in: dict = {"format": dict(AUDIO_FORMAT), "turn_detection": dict(TURN_DETECTION)}
    if transcribe_model:
        tr: dict = {"model": transcribe_model}
        lang = _transcribe_language(input_language_codes)
        if lang:
            tr["language"] = lang
        audio_in["transcription"] = tr
    session: dict = {
        "type": "realtime",
        "instructions": system_instruction,
        "output_modalities": ["audio"],
        "audio": {
            "input": audio_in,
            "output": {"format": dict(AUDIO_FORMAT), "voice": voice or DEFAULT_VOICE},
        },
    }
    if with_face_tool:
        session["tools"] = [set_face_tool()]
        session["tool_choice"] = "auto"
    else:
        # ⛔ 빈 배열을 **명시**한다. 표정 OFF 는 «선언을 안 싣는다» 가 아니라 «없다» 여야
        #   하고, 그게 턴당 입력 −54%(실측)의 근거다.
        session["tools"] = []
        session["tool_choice"] = "none"
    if max_output_tokens and int(max_output_tokens) > 0:
        session["max_output_tokens"] = int(max_output_tokens)
    return {"type": "session.update", "session": session}


# ── 세션 ─────────────────────────────────────────────────────────────────────── #
class OpenAIRealtimeSession:
    """OpenAI Realtime WS 1개의 비동기 래퍼(호출부 인터페이스 6개 구현)."""

    __slots__ = ("_ws", "_up", "_closed", "_pending_face", "_seen_calls", "_sent_items", "_log_prefix")

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._up = Upsampler16kTo24k()
        self._closed = False
        self._pending_face: set[str] = set()
        # ⛔ 같은 call_id 에 두 번 답하면 그 응답 자체가 새 입력이 된다 — 한 번 본 호출은 다시 안 본다.
        #   벤더가 같은 호출을 `function_call_arguments.done` 과 `response.done.output` **둘 다**로 준다(스파이크 관측).
        self._seen_calls: set[str] = set()
        self._sent_items = 0
        self._log_prefix = "normalcall OpenAI"

    # -- 보내기 ---------------------------------------------------------------- #
    async def _send(self, obj: dict) -> None:
        await self._ws.send(json.dumps(obj, ensure_ascii=False))

    async def send_audio(self, pcm16_16k: bytes) -> None:
        """업링크 PCM16/16k 청크 → **어댑터가 24k 로 올려** 버퍼에 append.

        ⭐ 인자 이름을 16k 로 **그대로** 둔다 — 호출부(`_pump_client_to_gemini`)가 무수정이다.
        ⛔ `input_audio_buffer.commit` 을 부르지 않는다. 턴 경계는 `semantic_vad` 가 정한다 —
          수동 commit 을 섞으면 VAD 와 둘이 턴을 끊어 응답이 두 번 난다.
        """
        if not pcm16_16k:
            return
        pcm24 = self._up.feed(pcm16_16k)
        if not pcm24:
            return
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm24).decode("ascii"),
        })

    async def _create_item(self, text: str, *, role: str = "user") -> None:
        """대화 끝에 텍스트 항목 하나를 **덧붙인다**(생성 트리거 없음).

        ⛔ `session.update` 로 `instructions` 를 갈아끼우지 마라 — 프리픽스가 바뀌면
          캐시가 전부 깨져 그 통화의 남은 입력이 전액 정가가 된다.

        ⭐⭐ `role` 이 복종률을 가른다(2026-10-06 하네스 83세션 실측):
          서버 쪽지(퀴즈 큐·세트 이탈·드릴 안내)를 `user` 로 넣으면 **오디오 턴 위에서**
          세션 지시문 `[진행]`(「1번부터 차례로」·「다음 번호를 설명하고 말해 보라」)에
          밀린다 — 되묻기 복종 **44%**(8/18). 같은 글자를 `system` 으로 넣으면
          **100%**(15/15), p=0.00051.
          ⚠ 텍스트 모달리티에서는 `user` 로도 23/24 가 따랐다 — **오디오에서만 터진다.**
            그래서 지금까지 코드 시험으로 안 보였다.
          ⚠ 실측으로 재현된 실패 경로가 실통화 1740·1741 과 **글자까지 같았다**
            (큐가 열린 뒤 항목4 → 항목5 로 전진).
          ⛔ `role` 기본값은 `user` 그대로 둔다 — 선톡·종료·무음 넛지 시드는 **안 쟀다**
            (`send_text_turn` 통로). 바꾸려면 재야 한다.
        """
        self._sent_items += 1
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": text}],
            },
        })

    async def _create_response(self) -> None:
        await self._send({"type": "response.create", "response": {"output_modalities": ["audio"]}})

    async def send_text_turn(self, text: str) -> None:
        """완결 user 텍스트 턴 1회 — 선톡 시드·넛지 시드·종료 시드·루프 차단 쪽지가 이 통로다."""
        await self._create_item(text)
        await self._create_response()

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        """쪽지 주입 — ⭐ `role="system"` 으로 넣는다.

        ⚠ **이 통로로 오는 것의 대부분은 재접지가 아니다** — 표현학습 **퀴즈 큐**와
          세트·드릴 안내가 `turn_complete=False` 로 여기 온다(`_attach_quiz_cue`).
          재접지는 OpenAI 경로에서 꺼져 있지만 **이 메서드는 꺼지면 안 된다.**
        - `turn_complete=False`: 항목만 덧붙인다 ⇒ 학습자 발화가 VAD 로 끝날 때 그
          응답에 **함께** 실린다(Gemini 의 미완결 병합과 같은 결과).
        - `turn_complete=True`: 지금 답하라는 뜻 ⇒ `response.create` 까지.

        ⭐⭐ **왜 `system` 인가**(2026-10-06 하네스 83세션 — `_create_item` 독스트링):
          `user` 로 넣으면 오디오 턴 위에서 세션 지시문에 밀려 되묻기 복종이 **44%** 였다.
          `system` 으로 바꾸면 **100%**(p=0.00051). 벤더가 `item.added role=system` 으로
          되돌려주고 `error` 0건이다(SDK 타입 `RealtimeConversationItemSystemMessage`).
        ⛔ `send_text_turn`·`send_persona` 는 `user` 를 **유지한다** — 그 통로(선톡·종료·
          무음 넛지 시드)는 측정하지 않았다. 안 쟀으면 안 바꾼다.
        """
        await self._create_item(text, role="system")
        if turn_complete:
            await self._create_response()

    async def send_persona(self, text: str) -> None:
        """페르소나 조각을 **컨텍스트에만** 적재한다(생성 트리거 없음).

        ⛔ `send_reground` 와 **일부러 몸통을 나눈다** — 합치면 재접지 회귀의 관측 채널이
          오염된다(가짜 세션이 두 통로를 따로 센다).
        """
        await self._create_item(text)

    async def send_tool_response(
        self, fn_id: Optional[str], fn_name: Optional[str],
        *, resume: bool = False, blocking: bool = False,
    ) -> None:
        """function-call 에 형식적 응답을 돌려주고 **생성을 재개시킨다**.

        OpenAI 의 함수콜은 블로킹이다 — 응답이 도착하는 것 자체가 재개 신호이고,
        그 뒤 `response.create` 로 «이제 말해라» 를 준다. 그래서 툴이 낀 턴은
        Response 가 **2개**다(첫 번째는 function_call 만, 두 번째가 실제 발화).
        ⚠ `resume`/`blocking` 은 Gemini scheduling 축의 인자다 — 여기선 받아서 무시한다
          (호출부가 `blocking=is_face` 로 넘긴다. 시그니처를 안 바꾸는 것이 싸다).
        """
        del resume, blocking
        if not fn_id:
            return
        await self._send({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": fn_id,
                "output": json.dumps({"result": "ok"}),
            },
        })
        await self._create_response()

    # -- 받기 ------------------------------------------------------------------ #
    async def events(self) -> AsyncIterator[OpenAIRealtimeEvent]:
        """OpenAI 서버 이벤트 → 정규화 이벤트. 스트림이 끝나면 루프를 끝낸다.

        ## turn_end 를 어디에 맞추나 — ⛔ `response.done` 이지만 **전부는 아니다**
        툴이 낀 턴은 Response 가 2개다. 첫 번째(출력이 function_call 뿐)의 done 을
        턴 끝으로 올리면 호출부가 **소리 없는 빈 턴**을 하나 만들고 자막·세그먼트 정렬이
        어긋난다. ⇒ 출력에 function_call 만 있는 응답은 turn_end 를 **건너뛴다**.
        ⚠ `usage` 는 두 응답 모두 올린다 — 둘 다 과금된다(그게 「툴콜 턴 2× 과금」의 실체다).
        """
        import websockets

        while True:
            try:
                raw = await self._ws.recv()
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as exc:
                # ⛔ 예외로 올리지 않는다. 올리면 호출부의 «저쪽 끊김 → 재연결» 판정에
                #   걸리는데, 이 엔진은 재연결을 **원하지 않는다**(히스토리 재주입이 캐시를
                #   처음부터 다시 쌓는다). 스트림 종료로 다룬다 — 호출부가 정상 종료 파이프를 탄다.
                logger.info("%s 수신 스트림 종료(%s) — events 루프 종료", self._log_prefix, exc)
                return
            try:
                ev = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else None
            except (ValueError, TypeError):
                logger.warning("%s: JSON 아닌 프레임 무시", self._log_prefix)
                continue
            if not isinstance(ev, dict):
                continue
            out_events = self._normalize(ev)
            # ⭐⭐ **툴 자동응답을 yield 보다 먼저 보낸다.** `yield` 는 소비측이 그 이벤트를
            #   처리할 때까지 이 코루틴을 멈춰 세운다 — 소비측은 그 사이 클라 WS 로 표정
            #   마커를 보내고 로그를 찍는다(await 가 여럿이다). 그 지연만큼 모델은 함수콜
            #   응답을 기다리며 **턴을 멈춘다.** Gemini 어댑터가 같은 자리에서 「두 번 말하기」
            #   사고를 겪고 「파싱 즉시 응답」으로 고친 그 지점이다. 순서를 뒤집지 마라.
            while self._pending_face:
                call_id = self._pending_face.pop()
                with contextlib.suppress(Exception):
                    await self.send_tool_response(call_id, None)
            for out in out_events:
                yield out

    def _normalize(self, ev: dict) -> list[OpenAIRealtimeEvent]:
        t = str(ev.get("type") or "")
        # 오디오 — GA 이름 우선, 베타 이름도 받는다(문서 전환기 방어).
        if t in ("response.output_audio.delta", "response.audio.delta"):
            delta = ev.get("delta")
            if delta:
                with contextlib.suppress(Exception):
                    return [OpenAIRealtimeEvent(kind="audio", audio=base64.b64decode(delta))]
            return []
        if t in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            txt = ev.get("delta") or ""
            return [OpenAIRealtimeEvent(kind="out_tr", text=txt)] if txt else []
        if t == "conversation.item.input_audio_transcription.delta":
            txt = ev.get("delta") or ""
            return [OpenAIRealtimeEvent(kind="in_tr", text=txt, is_final=False)] if txt else []
        if t == "conversation.item.input_audio_transcription.completed":
            txt = (ev.get("transcript") or "").strip()
            # ⛔⛔ **빈 전사는 올리지 않는다.** 올리면 두 가지가 같이 어긋난다:
            #   ① 호출부가 빈 `input_transcript` 프레임을 앱에 보낸다(자막이 깜빡인다).
            #   ② 호출부가 `learner_spoke=True` + 무음 시계 리셋을 한다 — 즉 «소리는 났지만
            #      말은 못 알아들은» 경우가 «학습자가 말했다» 로 집계된다.
            #   ⇒ 「전사가 있을 때만 올린다」로 둔다. 그러면 ASR 이 통째로 실패한 구간은
            #     무음 3단 넛지가 받는다(= 지금 운영과 같은 결과).
            if not txt:
                logger.info("%s 입력 전사 확정이 비었다(무음 워처에 맡긴다)", self._log_prefix)
                return []
            return [OpenAIRealtimeEvent(kind="in_tr", text=txt, is_final=True)]
        if t == "conversation.item.input_audio_transcription.failed":
            logger.warning("%s 입력 전사 실패: %s", self._log_prefix,
                           json.dumps(ev.get("error") or {}, ensure_ascii=False)[:300])
            return []
        if t == "response.function_call_arguments.done":
            return [self._tool_event(ev.get("call_id"), ev.get("name"), ev.get("arguments"))]
        if t == "response.done":
            return self._response_done(ev)
        if t == "error":
            logger.warning("%s 서버 error: %s", self._log_prefix,
                           json.dumps(ev.get("error") or ev, ensure_ascii=False)[:400])
            return []
        if t == "rate_limits.updated":
            # ⭐⭐ **계기판이다. 지우지 마라.** 이 엔진의 가장 큰 제약이 org TPM 40,000 이고
            #   (그래서 동시 통화를 1건으로 묶었다) 그 잔량을 알려 주는 유일한 신호가 이것이다.
            #   턴마다 한 줄 늘지만, 「TPM 때문에 통화가 조용히 안 열린다」를 사후에 가릴 수
            #   있는 값이라 그 값어치를 한다.
            limits = ev.get("rate_limits") or []
            logger.info("%s 한도: %s", self._log_prefix, " ".join(
                "%s=%s/%s(재충전 %ss)" % (
                    (l or {}).get("name"), (l or {}).get("remaining"),
                    (l or {}).get("limit"), (l or {}).get("reset_seconds"))
                for l in limits) or "(비었다)")
            return []
        if t in ("session.created", "session.updated"):
            logger.info("%s %s", self._log_prefix, t)
            return []
        return []

    def _tool_event(self, call_id, name, arguments) -> OpenAIRealtimeEvent:
        args: dict = {}
        if arguments:
            with contextlib.suppress(ValueError, TypeError):
                parsed = json.loads(arguments)
                if isinstance(parsed, dict):
                    args = parsed
        if call_id:
            self._seen_calls.add(str(call_id))
        acked = False
        if name in AUTO_ACK_TOOLS and call_id:
            # 여기선 **표시만** 한다(코루틴이 아닌 자리라 await 할 수 없다). 실제 전송은
            # `events()` 가 **yield 하기 전에** 한다 — 그 순서가 중요하다(위 주석 참조).
            # 소비측은 `auto_acked=True` 를 보고 또 답하지 않는다.
            self._pending_face.add(str(call_id))
            acked = True
        return OpenAIRealtimeEvent(
            kind="tool_call", fn_name=name, fn_id=call_id, fn_args=args, auto_acked=acked,
        )

    def _response_done(self, ev: dict) -> list[OpenAIRealtimeEvent]:
        out: list[OpenAIRealtimeEvent] = []
        resp = ev.get("response") or {}
        items = resp.get("output") or []
        status = str(resp.get("status") or "")
        # 툴 선언을 실었어도 `response.function_call_arguments.done` 이 안 오고 `response.done`
        # 의 output 에만 실려 오는 경우가 있다(스파이크에서 둘 다 관측) — 중복은 call_id 로 막는다.
        for item in items:
            if (item or {}).get("type") != "function_call":
                continue
            cid = str(item.get("call_id") or "")
            if cid and cid in self._seen_calls:
                continue
            out.append(self._tool_event(item.get("call_id"), item.get("name"), item.get("arguments")))
        if status and status not in ("completed", "cancelled"):
            # ⚠ `failed`·`incomplete` — 비버가 **말을 안 했는데** 턴이 끝난 상태다.
            #   사유는 `status_details` 에만 있다(필터·토큰 상한·서버 오류). 안 찍으면
            #   「왜 아무 말도 안 하지」를 사후에 가릴 수 없다.
            logger.warning("%s 응답 status=%s %s", self._log_prefix, status,
                           json.dumps(resp.get("status_details") or {}, ensure_ascii=False)[:300])
        shim = usage_shim(resp.get("usage"))
        if shim is not None:
            out.append(OpenAIRealtimeEvent(kind="usage", usage=shim))
        if status == "cancelled":
            out.append(OpenAIRealtimeEvent(kind="interrupted"))
            return out
        only_tool = bool(items) and all((i or {}).get("type") == "function_call" for i in items)
        if not only_tool:
            out.append(OpenAIRealtimeEvent(kind="turn_end"))
        else:
            logger.info("%s 툴 전용 응답 — turn_end 건너뜀(이어지는 응답이 진짜 턴 끝)",
                        self._log_prefix)
        return out


# ── 열기 ─────────────────────────────────────────────────────────────────────── #
def _api_key(settings: Any) -> str:
    """키를 가져온다. ⛔ 값을 로그·예외 메시지에 **마스킹해서도** 넣지 않는다.

    ⚠ 이름이 `OPENAI_*` 가 아니라 `GPT_API_KEY` 다(이 저장소 관례). env 도 같이 본다 —
      Cloud Run 은 env 로 주고, 로컬은 `.env` 가 `Settings` 로 들어온다.
    """
    key = (getattr(settings, "GPT_API_KEY", "") or os.environ.get("GPT_API_KEY") or "").strip()
    if not key:
        raise OpenAIRealtimeError("GPT_API_KEY 가 없어 OpenAI Realtime 세션을 열 수 없습니다.")
    return key


async def _apply_session(ws, config: dict) -> None:
    """`session.update` 를 보내고 `session.updated` 를 확인한다.

    ⭐ **왜 확인까지 하나**: 설정이 거부되면 벤더는 `error` 를 보내고 **세션은 열린 채로
      기본값으로 돈다** — 16kHz 로 말하는 통화, 전사 없는 통화가 조용히 시작된다.
      그 조용한 실패를 여기서 잡는다.
    ⭐ 거부가 **입력 전사 때문이면 전사를 빼고 한 번 더** 시도한다(R5 — 전사 모델 이름이
      미검증이라 그 한 칸으로 통화 전체를 죽이지 않는다). 그러면 `in_tr` 이 없는 통화가
      되는데, 그건 로그로 드러난다(무음 넛지가 바로 돈다).
    """
    import websockets

    await ws.send(json.dumps(config, ensure_ascii=False))
    retried = False
    deadline = asyncio.get_running_loop().time() + _SESSION_ACK_TIMEOUT_S
    while True:
        left = deadline - asyncio.get_running_loop().time()
        if left <= 0:
            logger.warning("normalcall OpenAI: session.updated 확인 못 함(계속 진행)")
            return
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=left)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("normalcall OpenAI: session.updated 대기 시간초과(계속 진행)")
            return
        except websockets.exceptions.ConnectionClosed as exc:
            raise OpenAIRealtimeError("세션 설정 중 연결이 닫혔습니다: %s" % exc) from exc
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            continue
        t = str((ev or {}).get("type") or "")
        if t == "session.updated":
            logger.info("normalcall OpenAI 세션 설정 적용됨(전사=%s)",
                        "on" if "transcription" in (config["session"]["audio"]["input"]) else "off")
            return
        if t == "error":
            err = (ev or {}).get("error") or {}
            logger.error("normalcall OpenAI 세션 설정 거부: %s",
                         json.dumps(err, ensure_ascii=False)[:400])
            blob = json.dumps(err, ensure_ascii=False)
            if not retried and "transcription" in blob:
                retried = True
                config = json.loads(json.dumps(config))     # 깊은 복사(호출부 dict 보호)
                config["session"]["audio"]["input"].pop("transcription", None)
                logger.warning("normalcall OpenAI: 입력 전사를 빼고 재시도한다(통화는 살린다)")
                await ws.send(json.dumps(config, ensure_ascii=False))
                deadline = asyncio.get_running_loop().time() + _SESSION_ACK_TIMEOUT_S
                continue
            raise OpenAIRealtimeError("OpenAI 세션 설정이 거부됐습니다.")


def _pick_voice(requested: str | None, settings: Any) -> str:
    """OpenAI 가 받는 음색으로 좁힌다 — 못 받는 이름이면 설정값으로 떨어뜨린다.

    ⛔ 호출부가 넘기는 값은 **DB 의 캐릭터 음색**(Gemini 이름)이다. 그대로 보내면
      `session.update` 가 거부되고 통화가 열리기도 전에 죽는다 — `SUPPORTED_VOICES` 주석.
    ⚠ **조용히 바꾸지 않는다.** 캐릭터 목소리가 달라지는 것은 사용자가 듣는 변화이므로
      WARNING 으로 남겨, 「왜 바바 목소리가 아니지」를 로그로 되짚을 수 있게 한다.
    """
    want = (requested or "").strip()
    if want in SUPPORTED_VOICES:
        return want
    fallback = (getattr(settings, "OPENAI_REALTIME_VOICE", "") or "").strip()
    if fallback not in SUPPORTED_VOICES:
        fallback = DEFAULT_VOICE
    if want:
        logger.warning(
            "normalcall OpenAI 음색 대체: %r 은 이 엔진이 받지 않는다 → %r "
            "(DB 음색은 Gemini 이름이다 — 지원 10종: %s)",
            want, fallback, ", ".join(sorted(SUPPORTED_VOICES)),
        )
    return fallback


@contextlib.asynccontextmanager
async def open_session(
    client: Any,
    settings: Any,
    *,
    system_instruction: str,
    voice: str = DEFAULT_VOICE,
    tools: Optional[list] = None,
    resume_handle: Optional[str] = None,
    input_language_codes: Optional[list[str]] = None,
    model: Optional[str] = None,
    vertex: Optional[bool] = None,
) -> AsyncIterator[OpenAIRealtimeSession]:
    """OpenAI Realtime 세션을 열고 래퍼를 yield 하는 async 컨텍스트 매니저.

    ⛔⛔ **시그니처를 Gemini 팩토리와 글자까지 같게 둔다.** 호출부가
      `live_session_factory(client, settings, **factory_kwargs)` 로 부르고 kwargs 는
      «값이 있을 때만 넣는» 규율로 조립되기 때문에, 안 쓰는 인자는 **받아서 조용히
      무시**하는 것이 조건분기를 더하는 것보다 싸고 안전하다.

    - `client`: 무시한다. OpenAI 는 공유 클라이언트가 필요 없다(통화마다 WS 1개).
      ⭐ 호출부는 이 `client`(genai)를 **사이드카**(힌트·판정·통화후 분석)에 계속 쓴다 —
        「OpenAI 통화 + Gemini 사이드카」가 정상 구성이다.
    - `tools`: **있나 없나만** 본다(표정 ON/OFF). ⛔ 리스트 **안을 들여다보지 않는다** —
      그 안은 google SDK 객체이고, 이 패키지는 Gemini 자산을 모른다. 선언은
      `core/openai/tools.py` 가 자기 스키마로 소유한다.
    - `resume_handle`·`vertex`: 무시한다(세션 재개·백엔드 분기가 없는 엔진이다).
    """
    global _active_sessions
    del client, resume_handle, vertex

    limit = int(getattr(settings, "OPENAI_REALTIME_MAX_CONCURRENT", 0) or MAX_CONCURRENT_SESSIONS)
    if _active_sessions >= limit:
        raise OpenAIRealtimeBusy(
            "OpenAI Realtime 동시 통화 상한(%d)에 걸렸습니다." % limit
        )

    key = _api_key(settings)
    live_model = (model or getattr(settings, "OPENAI_REALTIME_MODEL", "")
                  or OPENAI_REALTIME_MODEL_DEFAULT)
    picked_voice = _pick_voice(voice, settings)
    config = build_session_config(
        system_instruction=system_instruction,
        voice=picked_voice,
        with_face_tool=bool(tools),
        input_language_codes=input_language_codes,
        transcribe_model=(getattr(settings, "OPENAI_REALTIME_TRANSCRIBE_MODEL", None)
                          or DEFAULT_TRANSCRIBE_MODEL),
        max_output_tokens=int(getattr(settings, "OPENAI_REALTIME_MAX_OUTPUT_TOKENS", 0) or 0),
    )

    import websockets

    url = "%s?model=%s" % (OPENAI_REALTIME_URL, live_model)
    logger.info("normalcall OpenAI 연결 시도: model=%s voice=%s 표정=%s",
                live_model, picked_voice, "on" if tools else "off")
    _active_sessions += 1
    try:
        async with websockets.connect(
            url,
            additional_headers={"Authorization": "Bearer %s" % key},
            max_size=None,
            open_timeout=_SESSION_OPEN_TIMEOUT_S,
        ) as ws:
            await _apply_session(ws, config)
            logger.info("normalcall OpenAI 세션 연결됨")
            try:
                yield OpenAIRealtimeSession(ws)
            finally:
                logger.info("normalcall OpenAI 세션 종료")
    finally:
        _active_sessions -= 1


def active_session_count() -> int:
    """지금 열려 있는 세션 수(시험·관측용)."""
    return _active_sessions
