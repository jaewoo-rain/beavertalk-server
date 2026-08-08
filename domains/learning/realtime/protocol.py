"""normalcall WebSocket 텍스트(JSON) 제어 프로토콜 모델.

🧒 이 파일은 클라(휴대폰 앱)와 서버가 통화 중 주고받는 '말의 규칙(프로토콜)'을 정한다.
  한 소켓으로 두 종류가 흐른다: **바이너리 프레임 = 목소리(오디오)**, **텍스트 프레임 = 명령
  (JSON)**. 소리와 명령을 프레임 종류로 딱 갈라서, 서버는 받은 프레임이 bytes 면 오디오,
  text 면 제어 신호로 즉시 구분한다(섞이지 않는다).

🧒 왜 'discriminated union(구별 유니온)'인가: 텍스트 명령은 종류가 여러 개다 — 클라가 보내는
  start(시작)·ping(살아있니?)·playback_done(재생 끝)·hint_used, 서버가 보내는 turn_start·
  자막·turn_end·call_ended 등. 이걸 그냥 "아무 JSON"으로 받으면 어떤 종류인지 매번 손으로
  뒤져야 하고 오타·빠진 필드를 놓친다. 대신 모든 메시지에 `type` 이라는 이름표를 하나 붙이고
  (예: {"type":"ping", ...}), pydantic 이 그 이름표를 보고 **자동으로 알맞은 모델로 검증·변환**
  하게 한다. 이름표가 곧 '분별자(discriminator)'. 새 명령을 추가할 땐 여기 유니온에 모델을
  더하고 어댑터를 갱신하면, 클라(Flutter)와의 계약이 한곳에서 안전하게 관리된다.

바이너리 프레임 = raw PCM 오디오(클라→서버 16k, 서버→클라 24k). 텍스트 프레임 = 아래 JSON.
클라→서버: start, playback_done, ping, hint_used / 서버→클라: turn_start,
output_transcript, input_transcript, turn_end, call_ended, error, pong,
teaching_plan, hint.

P2.5(D16): teaching_plan(통화 시작 직후 1회 — L1 학습 카드), hint(비버 질문별 예시
답변 사이드카), hint_used(힌트 열람 신호 — 해당 턴 증거 E1 강등). 구버전 클라는
미지 타입을 무시하므로 무해(확인됨) — 데이터 없으면 미전송 = 기존 화면.

배운 표현은 통화중 보내지 않고, 종료 후 `GET /api/v1/calls/{call_id}/result`(기존) 폴링.
종료는 call_ended 1건으로 통지(분석은 비동기, status 폴링은 /calls/{id}/status).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator


# ── 클라이언트 → 서버 ──
class ClientStart(BaseModel):
    """통화 시작 신호(오디오 스트리밍 전에 1회). user/level/locale 은 서버가 DB 로 얻는다.

    Attributes:
        character_id: ⛔ **폐기 — 서버가 무시한다.** 통화 캐릭터의 단일 소스는 서버다:
            수신통화면 inbound_call_id 로 되짚은 알람의 캐릭터, 그 외에는
            member.character_id. 필드를 지우지 않는 이유는 target_language 와 같다 —
            이미 배포된 앱이 계속 보내는데 제거하면 pydantic 422 로 통화가 아예
            안 열린다. 받되 쓰지 않는다.

            왜 뺐나: 앱의 call_loading 이 `args is int ? args : 1` 로 폴백해, 인자를
            안 넘기는 진입점(마이페이지·기록·온보딩완료)에서 항상 1(BABA)을 보냈다.
            prod 통화 701 건 중 421 건(60%)이 사용자가 고른 캐릭터가 아닌 상대와
            연결됐다. 게다가 소유 검증이 없어 미구매 유료 캐릭터로도 126 건이
            진행됐다 — 앱을 고치면 얼마든지 부를 수 있었다. 둘 다 "캐릭터를 클라가
            정한다"가 뿌리라 통로 자체를 닫았다.
        inbound_call_id: (선택) **수신통화일 때만.** 푸시로 받은 통화 id(uuid4)를
            그대로 돌려준다. 서버가 push_dispatch_log 로 알람을 되짚어 그 알람의
            캐릭터를 쓴다 — 알람마다 캐릭터가 다를 수 있는데 member.character_id
            하나로는 그걸 표현할 수 없기 때문이다.

            앱이 **고른 값이 아니라** 서버가 내려준 불투명한 uuid 라서 캐릭터 선택
            통로가 되지 않는다. 남의 uuid 를 들고 와도 알람 주인이 아니면 거절된다.
            홈에서 직접 거는 통화는 보내지 않는다(= member.character_id 사용).
        locale: (선택) 모국어 override. 없으면 member.language 사용.
        target_language: ⛔ **폐기 — 서버가 무시한다.** 학습 대상 언어의 단일 소스는
            DB(member.target_language)이고, 바꾸는 통로는 PATCH /members/me 뿐이다.
            필드를 지우지 않는 이유: 이미 배포된 앱이 계속 보내는데 제거하면 pydantic
            검증에서 422 가 나 통화가 아예 안 열린다. 받되 쓰지 않는다.
            근거: docs/20260728_0125_학습언어-DB-단일소스화와-모국어-정규화.md
        call_type: (선택) 통화 종류. None(기본)이면 서버가 판단한다(D11 자동 라우팅:
            member.korean_level 미확정 → level_test). 명시하면 그 값이 우선 —
            미래 레벨 재측정 요청 통로(기존 클라는 이 필드를 안 보내므로 무영향).
        tz_offset_min: (선택) 클라 UTC 오프셋(분, 동쪽 +). KST=540. 일일 통화 한도의
            "하루" 경계를 클라 로컬 자정으로 잡는 데 쓴다 — 서버가 UTC 로 고정하면
            한국 사용자는 오전 9시에 날짜가 바뀐다. 미전송(구버전 앱)이면 UTC(0).
            GET /calls/daily-status 의 tz_offset 과 같은 의미·같은 값을 보내야 한다.
        duration_min: (선택) 통화 길이(분) override. **데모/dev 전용** — prod 에서는
            서버가 무시하고 기본값을 쓴다. 서버가 3~15분으로 클램프. 없으면 기본값.
    """

    type: Literal["start"] = "start"
    character_id: int | None = None
    inbound_call_id: str | None = None
    locale: str | None = None
    target_language: str | None = None
    call_type: Literal["normal", "level_test"] | None = None
    duration_min: int | None = None
    tz_offset_min: int | None = None


class ClientPlaybackDone(BaseModel):
    """클라이언트가 특정 턴 오디오 재생을 마쳤다는 ack."""

    type: Literal["playback_done"] = "playback_done"
    turn_id: str | None = None


class ClientPing(BaseModel):
    """keepalive 핑(서버는 pong 응답)."""

    type: Literal["ping"] = "ping"
    t: int | None = Field(default=None, description="클라 타임스탬프(ms, 선택)")


class ClientHintUsed(BaseModel):
    """힌트 열람 신호(P2.5·D16) — 클라가 힌트를 **연 순간** 1회 전송.

    서버는 저장만 하고 응답하지 않는다(유실 시 사용자에게 유리한 쪽 오차 — ack 불요).
    통화후 분석에서 해당 시점 직후의 첫 USER 턴 증거를 E1(모방) 수준으로 강등한다.

    Attributes:
        turn_id: 동적 힌트(hint)의 대상 비버 턴 id (동적 힌트 열람 시).
        item_id: 정적 학습 카드(teaching_plan) 항목 id (카드 힌트 열람 시).
        stage: 카드 힌트 단계(1=첫 음절, 2=전체 — mechanics ⑪).
    """

    type: Literal["hint_used"] = "hint_used"
    turn_id: str | None = None
    item_id: int | None = None
    stage: int | None = None


ClientMessage = Annotated[
    Union[ClientStart, ClientPlaybackDone, ClientPing, ClientHintUsed],
    Field(discriminator="type"),
]


# ── 서버 → 클라이언트 ──
class ServerTurnStart(BaseModel):
    type: Literal["turn_start"] = "turn_start"
    turn_id: str


class ServerOutputTranscript(BaseModel):
    type: Literal["output_transcript"] = "output_transcript"
    text: str
    turn_id: str


class ServerInputTranscript(BaseModel):
    type: Literal["input_transcript"] = "input_transcript"
    text: str


class ServerTurnEnd(BaseModel):
    type: Literal["turn_end"] = "turn_end"
    turn_id: str


class ServerCallEnded(BaseModel):
    """통화 종료 통지. 분석 결과는 비동기 → call_id 로 result/status 폴링."""

    type: Literal["call_ended"] = "call_ended"
    call_id: str
    reason: str = "done"


class ServerCallStarted(BaseModel):
    """서버가 **이 통화의 캐릭터를 정했다**는 통지 — 오디오가 흐르기 전 1회.

    캐릭터 결정이 서버로 넘어오면서 필요해졌다. 앱은 통화 화면 아바타를
    `myProfile.characterId`(대표 캐릭터)로 그리는데, 수신통화는 알람마다 캐릭터가
    다를 수 있어 **대화 상대와 화면 얼굴이 어긋난다**. 서버가 고른 값을 알려주면
    앱이 그걸로 그려 한 사람으로 맞춰진다.

    구버전 클라는 미지 타입을 무시하므로 무해(기존 동작 유지 = 대표 캐릭터 표시).
    """

    type: Literal["call_started"] = "call_started"
    character_id: int


class ServerError(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    recoverable: bool = True


# ── start.aec — 클라가 선언하는 에코 억제(AEC) 상태 ──
# ⛔ **이 값은 이제 서버 방어 관문을 끄는 스위치다**(2026-08-08). 그전까지는 barge-in 확인
#   방식만 고르는 느슨한 힌트라 생짜 dict(`ctrl.get("aec")`)로 읽어도 됐다. 방어를 끄는
#   값이 된 이상 **모르는 값이 조용히 통과하면 안 된다** — `mode:'HW'` 오타 하나로 관문이
#   꺼지는 걸 막으려고 타입을 박고 소문자로 정규화한다.
# ⚠ 보안 경계는 아니다: 이 WS 는 본인 통화이고, 게이트가 꺼져 봐야 손해는 본인 통화 품질뿐이며
#   barge-in 은 오히려 원가를 **줄인다**. 그래서 목적은 인증이 아니라 **오타 차단**이다.
#   그래서 파싱 실패도 거절이 아니라 `unknown`(= 게이트 켬, 안전 쪽) 으로 떨어뜨린다.
AEC_MODES_WITH_CANCEL = frozenset({
    "hw",          # 기기/OS 하드웨어 AEC (브라우저 데모가 보내는 값)
    "sw", "software",
    "system",      # OS 음성통화 모드(VoiceProcessingIO 등)
    "browser",     # WebRTC getUserMedia echoCancellation:true
    "headset",     # 이어폰 — 음향 결합 자체가 없다(에코가 물리적으로 불가능)
})


class AecHint(BaseModel):
    """`start.aec`. 모르는 값·미선언은 전부 `unknown` = **게이트 켜는 쪽**으로 떨어진다."""

    model_config = ConfigDict(extra="ignore")

    mode: str = "unknown"

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize(cls, v: Any) -> str:
        return str(v).strip().lower() if v is not None else "unknown"

    @property
    def has_echo_cancel(self) -> bool:
        return self.mode in AEC_MODES_WITH_CANCEL


class ServerPong(BaseModel):
    type: Literal["pong"] = "pong"
    t: int | None = None


class TeachingItem(BaseModel):
    """teaching_plan 항목 1건(P2.5 — L1 학습 카드 화면용).

    프롬프트 주입(study_items)과 단일 소스: ko=obj / example=ex / meaning=des /
    roman=학습항목 meanings JSON 의 "roman"(청크 RR 표기, 없으면 None).
    """

    item_id: int
    ko: str
    roman: str | None = None
    meaning: str | None = None
    example: str | None = None
    kind: str


class ServerTeachingPlan(BaseModel):
    """통화 시작 직후 1회 push 되는 오늘의 학습 계획(P2.5 — mechanics ⑪).

    normal 통화에서 study_items 가 있을 때만 전송. 없으면 미전송 = 기존 자막 화면.
    """

    type: Literal["teaching_plan"] = "teaching_plan"
    items: list[TeachingItem]


class HintExample(BaseModel):
    """예시 답변 1개(한국어 문장 + 로마자 + 모국어 뜻)."""

    korean: str
    roman: str | None = None
    native: str


class ServerHint(BaseModel):
    """비버 질문에 대한 예시 답변 힌트(D16 — 서버 사이드카 LLM 생성, 예시 3개).

    turn_id = 질문한 비버 턴의 id. 클라는 접힌 힌트 상자로 표시하고, 열람 시
    hint_used(turn_id)를 되보낸다(증거 E1 강등 재료). examples 는 난이도·표현이 조금씩
    다른 예시 답변 3개(학습자가 골라 참고).
    """

    type: Literal["hint"] = "hint"
    turn_id: str
    examples: list[HintExample]


ServerMessage = Annotated[
    Union[
        ServerTurnStart,
        ServerOutputTranscript,
        ServerInputTranscript,
        ServerTurnEnd,
        ServerCallEnded,
        ServerCallStarted,
        ServerError,
        ServerPong,
        ServerTeachingPlan,
        ServerHint,
    ],
    Field(discriminator="type"),
]


client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
