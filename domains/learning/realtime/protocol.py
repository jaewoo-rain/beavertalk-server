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

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter


# ── 클라이언트 → 서버 ──
class ClientStart(BaseModel):
    """통화 시작 신호(오디오 스트리밍 전에 1회). user/level/locale 은 서버가 DB 로 얻는다.

    Attributes:
        character_id: 통화할 캐릭터(페르소나) id.
        locale: (선택) 모국어 override. 없으면 member.language 사용.
        target_language: (선택) 가르치는 대상 언어 override. 데모 전용 — prod 에서는
            서버(run_call)가 무시하고 항상 "한국어". 없으면 None → 한국어.
        call_type: (선택) 통화 종류. None(기본)이면 서버가 판단한다(D11 자동 라우팅:
            member.korean_level 미확정 → level_test). 명시하면 그 값이 우선 —
            미래 레벨 재측정 요청 통로(기존 클라는 이 필드를 안 보내므로 무영향).
        duration_min: (선택) 통화 길이(분) override. **데모/dev 전용** — prod 에서는
            서버가 무시하고 기본값을 쓴다. 서버가 3~15분으로 클램프. 없으면 기본값.
    """

    type: Literal["start"] = "start"
    character_id: int
    locale: str | None = None
    target_language: str | None = None
    call_type: Literal["normal", "level_test"] | None = None
    duration_min: int | None = None


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


class ServerError(BaseModel):
    type: Literal["error"] = "error"
    code: str
    message: str
    recoverable: bool = True


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
        ServerError,
        ServerPong,
        ServerTeachingPlan,
        ServerHint,
    ],
    Field(discriminator="type"),
]


client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
