"""Gemini Live 세션 래퍼 (normalcall 실시간 음성통화) — 외부 어댑터.

beavertalk 서버의 검증된 live.py 를 이 프로젝트로 포팅. Vertex native-audio +
컨텍스트 윈도우 압축(세션 한계: 오디오 15분/연결 ~10분 — 압축은 드리프트 완화·장기
통화 대비, S2) + 입출력 전사 + 단일 prebuilt voice.
도메인/DB/프롬프트를 모른다(speechsuper.py 와 동일한 어댑터 규율). system_instruction
과 voice 는 호출부(realtime)가 조립해 넘긴다.

LiveSessionProtocol 로 모킹 가능 — 테스트는 동일 인터페이스의 가짜 세션을 주입.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Optional, Protocol, runtime_checkable

from google import genai
from google.genai import types

from core.audio import INPUT_MIME_TYPE
from core.config import Settings, settings

logger = logging.getLogger(__name__)

# 톤 일관성: 비버 음성 기본값(캐릭터가 voice 를 주면 그걸 사용).
DEFAULT_VOICE = "Fenrir"
# native-audio 모델은 temperature=0 에서 반복·로봇처럼 되므로 0 을 쓰지 않는다.
LIVE_TEMPERATURE = 0.6

# 레벨테스트 조기종료용 function-call 선언(인자 없음).
# native-audio 에선 out_tr sentinel 이 낭독돼 못 쓰므로 tool-call 로 천장 신호를 받는다.
# NON_BLOCKING: 서버는 응답을 기다리지 않고 대화를 이어간다 → 호출 자체가 발화를 끊지
# 않는다. 실제 종료는 call_session 소비측이 tool_call 이벤트를 감지해 종료 파이프에 합류
# 시켜 수행한다(이 어댑터는 "호출 가능"만 선언, "언제 호출"은 프롬프트가 지시 — 어댑터 규율).
# behavior/Behavior 경로는 google-genai types 로 검증됨(types.Behavior.NON_BLOCKING,
# FunctionDeclaration.behavior 필드).
LEVELTEST_DONE_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="leveltest_ceiling_reached",
            description="레벨 천장이 확정되어 측정 표본이 충분할 때 호출. 인자 없음.",
            behavior=types.Behavior.NON_BLOCKING,
        )
    ]
)

# ⭐ 표정 신호용 function-call(2026-08-18 계측 스파이크).
# native-audio 는 모델 출력이 **곧 소리**라 텍스트 태그를 쓰면 그대로 낭독한다
# (persona_prompt.py 가 emotion_tags 를 Live 에 금지한 이유가 그것이다).
# tool-call 은 소리가 아닌 유일한 통로다 — 레벨테스트가 같은 벽에서 먼저 쓴 수법이다.
# ⚠ 값 집합은 **클라 아바타 어휘 5종**과 같다(커밋 d1139d8 "매핑 계층을 만들지 않는다").
#
# ⛔⛔ **`behavior` 를 지정하지 않는다 = 기본 블로킹**(2026-08-20 A안). 공식문서:
#     "Function calling executes **sequentially by default**, meaning execution pauses
#      until the results of each function call are available."
#   ⇒ 기본(블로킹)이 표준 경로다. 모델은 우리 응답을 **기다렸다가** 이어서 말한다.
#
#   ⭐ 이 한 줄이 지금까지의 실패 셋을 전부 설명한다. 여기 있던 `NON_BLOCKING` 은
#     **쓰이지도 않는 LEVELTEST_DONE_TOOL 에서 복사해 온 것**이었다:
#       · SILENT   → "생성하지 마" 로 답한 셈 ⇒ 4턴 내내 대답 0건
#       · WHEN_IDLE→ 턴 사이엔 할 일이 없어 즉시 재개 ⇒ 32초에 89회 폭주
#       · INTERRUPT→ 하던 말을 자른다(미시도)
#     세 값 전부 "모델은 기다리는데 우리는 안 기다린다고 선언한 상태"의 증상이다.
#   ⛔ 되돌리려거든 위 문서 인용부터 반박해라. `NON_BLOCKING` 을 다시 붙이면
#     send_tool_response 의 scheduling 분기도 같이 살려야 한다 — 한쪽만 고치면
#     증상이 위 셋 중 하나로 정확히 재발한다.
SET_FACE_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="set_face",
            description=(
                "네 표정이 바뀔 때 호출한다. 그 말을 하기 **직전에** 부른다. "
                "표정이 그대로면 부르지 않는다."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "emotion": types.Schema(
                        type=types.Type.STRING,
                        enum=["neutral", "happy", "surprised", "sad", "angry"],
                        description="지금부터의 표정.",
                    )
                },
                required=["emotion"],
            ),
        )
    ]
)

LiveEventKind = Literal[
    "audio", "in_tr", "out_tr", "interrupted", "turn_end", "go_away", "tool_call",
    "usage", "resume_update",
]


@dataclass(slots=True)
class LiveEvent:
    """Gemini Live 응답을 호출부가 다루기 쉽게 정규화한 단일 이벤트."""

    kind: LiveEventKind
    audio: Optional[bytes] = None      # kind=="audio": 출력 PCM24k
    text: Optional[str] = None         # kind in {in_tr,out_tr}: 전사
    is_final: bool = False             # 입력 전사 확정 여부
    time_left: Optional[str] = None    # kind=="go_away": 서버 종료 예고 timeLeft(있으면)
    fn_name: Optional[str] = None      # kind=="tool_call": 호출된 function 이름
    fn_id: Optional[str] = None        # kind=="tool_call": function_call id(send_tool_response 매칭용)
    # ⭐ kind=="tool_call": 인자(2026-08-18). 지금까지 tool 이 **인자 없는 것 하나**뿐이라
    #   이 칸이 없었다. set_face 는 emotion 을 받으므로 필요하다.
    #   ⚠ 기본 None → 기존 소비측(없다) · 회귀 무영향.
    fn_args: Optional[dict] = None
    usage: Optional[Any] = None        # kind=="usage": SDK UsageMetadata 원본(어댑터는 해석 안 함)
    # kind=="resume_update": 세션 재개 핸들. resumable=False 면 지금 시점 상태로는 재개할 수
    # 없다는 뜻이라(모델 생성 중·tool 실행 중) **핸들을 덮어쓰면 안 된다**.
    resume_handle: Optional[str] = None
    resumable: bool = False


@runtime_checkable
class LiveSessionProtocol(Protocol):
    """realtime 브리지가 의존하는 Live 세션 인터페이스(모킹 확장점)."""

    async def send_audio(self, pcm16_16k: bytes) -> None: ...
    async def send_text_turn(self, text: str) -> None: ...
    async def send_reground(self, text: str) -> None: ...
    async def send_tool_response(self, fn_id: Optional[str], fn_name: Optional[str]) -> None: ...
    def events(self) -> AsyncIterator[LiveEvent]: ...


def build_live_config(
    *,
    system_instruction: str,
    voice: str = DEFAULT_VOICE,
    tools: Optional[list[types.Tool]] = None,
    resume_handle: Optional[str] = None,
) -> types.LiveConnectConfig:
    """normalcall 용 LiveConnectConfig 구성.

    오디오 출력 + 입출력 전사 + 단일 prebuilt voice + 컨텍스트 압축(슬라이딩 윈도우).
    safety_settings 는 거친 페르소나(트래시토커) 면박·욕설 허용을 위해 HARASSMENT 만
    완화하고 혐오·성·위험은 엄격 유지한다. realtime_input_config 는 넣지 않는다(무음 버그).

    tools: 기본 None → 일반 통화 config 는 바이트 동일(하위호환). 레벨테스트 조기종료
    같은 function-call 이 필요할 때만 [LEVELTEST_DONE_TOOL] 등을 넘긴다. LiveConnectConfig.tools
    의 pydantic 기본값도 None 이라 None 전달 시 미전달과 동일 직렬화(회귀 무영향).

    resume_handle: 세션 재개용 핸들. LIVE_SESSION_RESUMPTION 이 꺼져 있으면 session_resumption
    필드 자체를 넣지 않아 **종전과 바이트 동일**이다(스냅샷 회귀 무영향). 켜져 있고 핸들이
    None 이면 "새 세션이되 핸들을 발급해 달라"는 뜻이고, 핸들이 있으면 그 시점 상태를 복원한다.

    ⚠ transparent 는 Vertex 전용이라 USE_VERTEX 일 때만 켠다 — api_key 폴백 경로에 그대로
      넘기면 SDK 가 ValueError 를 던져 통화가 아예 안 열린다(R5 위반).
    """
    session_resumption = None
    if settings.LIVE_SESSION_RESUMPTION:
        session_resumption = types.SessionResumptionConfig(
            handle=resume_handle,
            # transparent=True 면 서버가 last_consumed_client_message_index 를 같이 준다
            # (재연결 시 미소비 오디오만 골라 재전송하기 위한 값). 지금은 재연결을 안 하므로
            # 관측 목적으로만 켠다 — 인덱스가 실제로 오는지 봐야 재연결 설계를 확정한다.
            transparent=True if settings.USE_VERTEX else None,
        )
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        tools=tools,
        session_resumption=session_resumption,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=system_instruction,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        temperature=LIVE_TEMPERATURE,
        # ⭐ 세션 한계(압축 無): 오디오 15분 / 연결 자체 ~10분(S2). 압축은 세션을
        # 무제한으로 늘리는 동시에, 오래된 오디오 토큰을 밀어내 5분 통화의 드리프트를
        # 완화하는 역할이다. 블랙박스 기본값 대신 명시값을 박는다: trigger_tokens 에서
        # 압축이 발동해 target_tokens 만큼 유지.
        #
        # 값은 env 로 뺐다(core.config 참조) — 통화 원가의 81%가 이 상한에 걸린 입력이라
        # 튜닝 대상인데, 상수였으면 값 하나 바꿀 때마다 재빌드·재배포(5~6분)가 필요했다.
        # 기본값은 종전과 동일(16000/12000)이라 이 변경만으로는 동작이 바뀌지 않는다.
        context_window_compression=types.ContextWindowCompressionConfig(
            trigger_tokens=settings.LIVE_CTX_TRIGGER_TOKENS,
            sliding_window=types.SlidingWindow(
                target_tokens=settings.LIVE_CTX_TARGET_TOKENS
            ),
        ),
        safety_settings=[
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            ),
        ],
    )


class GeminiLiveSession:
    """google-genai live.connect 세션의 비동기 래퍼."""

    def __init__(self, raw_session: Any) -> None:
        self._session = raw_session

    async def send_audio(self, pcm16_16k: bytes) -> None:
        """입력 PCM(16bit/16k/mono) 청크를 즉시 모델로 전송(버퍼링 없음)."""
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm16_16k, mime_type=INPUT_MIME_TYPE)
        )

    async def send_text_turn(self, text: str) -> None:
        """초기 선톡 시드/종료 시드용 user 텍스트 턴 1회 전송.

        receive 루프가 이미 돌고 있어야 첫 청크를 놓치지 않는다.
        """
        await self._session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=True,
        )

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        """재접지: 캐릭터 리마인더를 user 텍스트로 주입한다.

        turn_complete 의미(두 재접지 모드가 갈린다):
        - True(legacy_idle): 완결 턴 → 비버가 즉시 캐릭터답게 응답(별도 턴 → 이중 발화).
        - False(on_user_turn): 미완결 텍스트를 **진행 중 유저 발화 턴에 얹어** 유저 발화가
          완결하게 한다 → 비버가 [유저발화+리마인더]에 한 번만 응답(이중발화·잔류 제거 목표).
          ⚠ 오디오 VAD 턴 + client_content 병합은 Gemini 공식 보장 아님(미정의) → 실측 검증 필요.
          반드시 종료 구간 밖(should_close/close_seed_sent 아님)에서만 호출(늦은 얹기=작별 오염).
        """
        await self._session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=turn_complete,
        )

    async def send_tool_response(
        self, fn_id: Optional[str], fn_name: Optional[str],
        *, resume: bool = False, blocking: bool = False,
    ) -> None:
        """function-call 에 대한 형식적 응답을 되돌린다.

        ⛔⛔ **blocking=True 면 `scheduling` 을 아예 안 붙인다**(2026-08-20 A안).
        `scheduling` 은 NON_BLOCKING 호출의 응답을 "언제 맥락에 넣고 생성을 촉구할지"
        고르는 칸이다. 기본(블로킹) 호출에서는 모델이 이미 이 응답을 **기다리고 있으므로**
        응답이 도착하는 것 자체가 재개다 — 여기에 SILENT/WHEN_IDLE 을 얹으면 기다리는
        모델에게 "생성하지 마"/"할 일 없으면 또 해"를 덧대는 꼴이 된다. 그게 표정 tool 이
        세 번 실패한 이유다(SET_FACE_TOOL 위 주석의 증상 3종).
        ⚠ blocking=False 경로(레벨테스트)는 **바이트 동일**하다 — 회귀가 지킨다.


        레벨테스트 조기종료 tool(leveltest_ceiling_reached) 처럼 서버 판단만 필요하고
        결과 payload 가 없는 호출에 쓴다. scheduling=SILENT 로 이 응답이 추가 발화를
        유발하지 않게 한다(맥락에만 반영, 생성 트리거·인터럽트 없음) — 종료 파이프는
        call_session 이 별도로 몰아간다. id 는 수신한 function_call.id 와 매칭.
        SDK: session.send_tool_response(function_responses=[FunctionResponse(...)]),
        FunctionResponse.scheduling=types.FunctionResponseScheduling.SILENT (검증됨).
        """
        await self._session.send_tool_response(
            function_responses=[
                types.FunctionResponse(
                    id=fn_id,
                    name=fn_name,
                    response={"result": "ok"},
                    # ⭐⭐ **`resume` 가 없으면 모델이 다시 말하지 않는다**(2026-08-18 실측).
                    #   SILENT = "맥락에만 넣고 **생성을 트리거하지 않는다**". 레벨테스트는
                    #   그게 맞았다(그 호출의 목적이 **통화를 끝내는 것**이라 이어 말할 필요가
                    #   없었다). 그런데 표정 tool 은 부르고 **계속 말해야** 한다.
                    #   ⛔ 실측: set_face 를 4턴 내내 불렀는데 **첫 턴 말고는 대답이 0건**이었다
                    #     (사장님: "AI가 대답을 안 하는데?"). 모델은 호출 뒤 응답을 기다렸고,
                    #     우리가 SILENT 로 "생성하지 마"라고 답한 셈이다.
                    #   ⇒ WHEN_IDLE = "맥락에 넣고, **진행 중 생성을 끊지 않으면서** 생성을
                    #     촉구한다". INTERRUPT 는 하던 말을 자르므로 쓰지 않는다.
                    #   ⚠ 기본은 SILENT 그대로 — 레벨테스트 경로의 바이트가 안 바뀐다.
                    **({} if blocking else {"scheduling": (
                        types.FunctionResponseScheduling.WHEN_IDLE if resume
                        else types.FunctionResponseScheduling.SILENT
                    )}),
                )
            ]
        )

    async def events(self) -> AsyncIterator[LiveEvent]:
        """SDK 응답 스트림을 LiveEvent 로 정규화해 yield.

        session.receive() 는 한 턴까지만 yield 하므로 바깥 루프에서 매 턴 재호출.
        수신 0건이면 세션 종료로 보고 루프를 끝낸다. 모든 필드 접근은 None-safe.
        """
        while True:
            received_any = False
            async for response in self._session.receive():
                received_any = True

                data = getattr(response, "data", None)
                if data:
                    yield LiveEvent(kind="audio", audio=data)

                # GoAway: 서버가 곧 연결을 닫겠다는 예고(연결 ~10분 한계, S2). server_content 와
                # 무관한 최상위 필드라 None-가드보다 먼저 본다. SDK 필드명은 방어적으로(getattr) —
                # timeLeft 로 우아한 마무리를 유도. 종료 시드 파이프로 합류.
                go_away = getattr(response, "go_away", None)
                if go_away is not None:
                    yield LiveEvent(
                        kind="go_away",
                        time_left=getattr(go_away, "time_left", None),
                    )

                # usage: 과금 계측(server_content 와 무관한 최상위 필드 → go_away 와 같은 층위).
                # Live 는 모델이 턴을 만들 때마다 세션 컨텍스트 전체를 입력으로 재과금하는데,
                # 그 지배 항이 지금껏 관측되지 않았다(원가가 추정치뿐이었던 이유). SDK 는
                # 서버의 usageMetadata 를 가공 없이 통과시키므로(_live_converters), 이 값이
                # "메시지별 증분"인지 "세션 누적"인지는 서버가 정한다 — 어댑터는 판단하지 않고
                # 원본 그대로 넘긴다. 의미 판별·집계·원가 산식은 소비측(call_session)의 몫.
                # ⚠ total_token_count 로 거르지 않는다: 0/None 이어도 모달리티 detail 은 올 수
                # 있고, "언제·얼마나 오는가" 자체가 관측 대상이라 표본을 미리 버리면 안 된다.
                um = getattr(response, "usage_metadata", None)
                if um is not None:
                    yield LiveEvent(kind="usage", usage=um)

                # tool_call: 모델의 function-call 요청(server_content 와 무관한 최상위
                # 필드 → go_away 처럼 None-가드보다 먼저). 레벨테스트 조기종료 신호가
                # 여기로 온다. function_calls 마다 정규화해 방출 — 소비측(call_session)이
                # fn_name 으로 분기하고 send_tool_response(fn_id, fn_name) 로 응답한다.
                # session_resumption_update: 서버가 주기적으로 밀어주는 재개 핸들.
                # resumable=False 는 "지금 상태로는 재개 불가"(모델이 생성 중이거나 tool
                # 실행 중)라는 뜻이므로 소비측이 **핸들을 덮어쓰면 안 된다** — 그 상태로
                # 재개하면 데이터가 유실된다(SDK proto 주석).
                resume = getattr(response, "session_resumption_update", None)
                if resume is not None:
                    yield LiveEvent(
                        kind="resume_update",
                        resume_handle=getattr(resume, "new_handle", None),
                        resumable=bool(getattr(resume, "resumable", False)),
                    )

                tool_call = getattr(response, "tool_call", None)
                if tool_call is not None:
                    for fc in getattr(tool_call, "function_calls", None) or []:
                        yield LiveEvent(
                            kind="tool_call",
                            fn_name=getattr(fc, "name", None),
                            fn_id=getattr(fc, "id", None),
                            # ⚠ SDK 가 dict 로 준다. 어댑터는 **해석하지 않는다**(도메인 몫).
                            fn_args=dict(getattr(fc, "args", None) or {}),
                        )

                server_content = getattr(response, "server_content", None)
                if server_content is None:
                    continue

                in_tr = getattr(server_content, "input_transcription", None)
                in_text = getattr(in_tr, "text", None) if in_tr is not None else None
                if in_text:
                    is_final = bool(getattr(in_tr, "finished", False))
                    yield LiveEvent(kind="in_tr", text=in_text, is_final=is_final)

                out_tr = getattr(server_content, "output_transcription", None)
                out_text = getattr(out_tr, "text", None) if out_tr is not None else None
                if out_text:
                    yield LiveEvent(kind="out_tr", text=out_text)

                if getattr(server_content, "interrupted", False):
                    yield LiveEvent(kind="interrupted")

                if getattr(server_content, "turn_complete", False):
                    yield LiveEvent(kind="turn_end")

            if not received_any:
                logger.info("Gemini 수신 스트림 종료 — events 루프 종료")
                break


async def _ensure_fresh_credentials(client: genai.Client) -> None:
    """공유 Vertex creds 의 access token 이 만료됐으면 connect 전에 갱신한다.

    genai 내부(_api_client._credentials)의 SA 자격증명을 들여다본다. api_key 클라이언트나
    구조가 다른 버전에선 creds 가 None 이라 조용히 건너뛴다(라이브러리 기본 동작 유지).
    refresh 는 블로킹 네트워크 호출이므로 to_thread 로 이벤트 루프를 막지 않는다. 실패해도
    여기서 죽이지 않고(경고만) connect 로 진행 — 진짜 원인은 connect 에러가 말하게 둔다.
    """
    creds = getattr(getattr(client, "_api_client", None), "_credentials", None)
    if creds is None or getattr(creds, "valid", False):
        return
    try:
        import google.auth.transport.requests as greq

        await asyncio.to_thread(creds.refresh, greq.Request())
        logger.info("normalcall Live: 만료된 Vertex 토큰 갱신 완료")
    except Exception as exc:  # noqa: BLE001 - 갱신 실패는 connect 가 authoritative
        logger.warning("normalcall Live: Vertex 토큰 갱신 실패(연결 계속 시도): %s", exc)


@contextlib.asynccontextmanager
async def open_session(
    client: genai.Client,
    settings: Settings,
    *,
    system_instruction: str,
    voice: str = DEFAULT_VOICE,
    tools: Optional[list[types.Tool]] = None,
    resume_handle: Optional[str] = None,
) -> AsyncIterator[GeminiLiveSession]:
    """normalcall Gemini Live 세션을 열고 래퍼를 yield 하는 async 컨텍스트 매니저.

    config 는 build_live_config 가 구성, system_instruction/voice 는 호출부(realtime)가 조립.
    tools 기본 None → 일반 통화 무영향. 레벨테스트만 [LEVELTEST_DONE_TOOL] 을 넘긴다.

    ⚠ 통화 1건 = 세션 1개가 **아니다**(2026-08-04부터). Gemini 연결 수명이 ~10분이라
      15분 통화는 연결을 갈아끼워야 한다. call_session 의 세대 루프가 이 컨텍스트 매니저를
      여러 번 연다. resume_handle 을 넘기면 서버가 그 시점 대화 상태를 복원하므로,
      모델 입장에서는 대화가 끊긴 적이 없다.
    """
    config = build_live_config(
        system_instruction=system_instruction,
        voice=voice,
        tools=tools,
        resume_handle=resume_handle,
    )
    # ⭐ Live 토큰 만료 방어: genai.Client 는 lifespan 이 한 번 만들어 인스턴스 수명 내내
    # 공유한다. 그 SA access token 은 ~1시간 만료인데, REST(분석·TTS)는 요청마다 갱신돼
    # 멀쩡하지만 Live 의 WS connect 는 만료 토큰을 그대로 보내 1008(invalid auth)로 죽는다
    # ("인스턴스 뜨고 1시간 뒤 통화만 갑자기 안 됨"). 라이브러리 버전에 의존하지 않도록,
    # connect 직전에 공유 creds 가 만료됐으면 강제로 새 토큰을 발급한다. api_key(AI Studio)
    # 클라이언트엔 _credentials 가 없어 자동으로 건너뛴다(graceful).
    await _ensure_fresh_credentials(client)
    logger.info("normalcall Live 연결 시도: model=%s voice=%s", settings.GEMINI_LIVE_MODEL, voice)
    async with client.aio.live.connect(
        model=settings.GEMINI_LIVE_MODEL,
        config=config,
    ) as raw_session:
        logger.info("normalcall Live 세션 연결됨")
        try:
            yield GeminiLiveSession(raw_session)
        finally:
            logger.info("normalcall Live 세션 종료")
