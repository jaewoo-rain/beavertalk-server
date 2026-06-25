"""normalcall WebSocket 텍스트(JSON) 제어 프로토콜 모델.

바이너리 프레임 = raw PCM 오디오(클라→서버 16k, 서버→클라 24k). 텍스트 프레임 = 아래 JSON.
클라→서버: start, playback_done, ping / 서버→클라: turn_start, output_transcript,
input_transcript, turn_end, call_ended, error, pong.

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
    """

    type: Literal["start"] = "start"
    character_id: int
    locale: str | None = None


class ClientPlaybackDone(BaseModel):
    """클라이언트가 특정 턴 오디오 재생을 마쳤다는 ack."""

    type: Literal["playback_done"] = "playback_done"
    turn_id: str | None = None


class ClientPing(BaseModel):
    """keepalive 핑(서버는 pong 응답)."""

    type: Literal["ping"] = "ping"
    t: int | None = Field(default=None, description="클라 타임스탬프(ms, 선택)")


ClientMessage = Annotated[
    Union[ClientStart, ClientPlaybackDone, ClientPing],
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


ServerMessage = Annotated[
    Union[
        ServerTurnStart,
        ServerOutputTranscript,
        ServerInputTranscript,
        ServerTurnEnd,
        ServerCallEnded,
        ServerError,
        ServerPong,
    ],
    Field(discriminator="type"),
]


client_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)
server_adapter: TypeAdapter[ServerMessage] = TypeAdapter(ServerMessage)
