"""온디맨드 TTS 라우터 — 문장을 주면 MP3 바이트를 돌려준다.

⚠ `sentence.py` 에 넣지 않았다. 경로가 `/tts/speech` 라 «문장 리소스»가 아니고,
  섞으면 다음 사람이 `/sentences/{id}/tts` 와 같은 것으로 읽는다(둘은 전혀 다르다).
"""

from __future__ import annotations

from fastapi import APIRouter, Header, Response, status
from typing import Optional

from core.deps import CurrentMember, DbSession
from domains.learning.schemas.tts import TtsSpeechIn
from domains.learning.service.tts_service import TtsService

router = APIRouter(prefix="/tts", tags=["tts"])


@router.post(
    "/speech",
    # ⛔⛔ `response_model` 을 붙이지 마라 — JSON 직렬화가 끼어들어 바이너리가 깨진다.
    #   그래서 아래 responses 는 **문서용**이다(OpenAPI 에 audio/mpeg 로 보이게).
    # ⚠ `response_class` 도 같이 준다. 안 주면 FastAPI 가 200 에 `application/json` 을
    #   **기본으로 끼워 넣어**, 스펙만 보는 프론트가 JSON 을 기대하게 된다(실제로 그랬다).
    response_class=Response,
    responses={
        200: {"content": {"audio/mpeg": {}}, "description": "MP3 바이트"},
        304: {"description": "ETag 일치 — 재합성 없음(원가 0)"},
        404: {"description": "준 character_id 가 없는 캐릭터"},
        422: {"description": "text 가 비었거나 200자 초과"},
        503: {"description": "TTS 비활성/합성 실패 — 앱은 텍스트만 보여줄 것"},
    },
)
async def synthesize_speech(
    data: TtsSpeechIn,
    member: CurrentMember,
    db: DbSession,
    if_none_match: Optional[str] = Header(default=None, alias="If-None-Match"),
) -> Response:
    """임의 문장 → MP3 바이너리. **저장하지 않는다.**

    ⛔ 응답 형식이 상태코드마다 다르다:
        200  audio/mpeg        바디 = MP3 바이트
        그외 application/json  바디 = {"detail": {"code","message"}}
      ⇒ 클라는 **바디를 읽기 전에 상태코드를 먼저** 봐야 한다. 이 API 의 유일한 함정이다.

    목소리·언어는 받지 않는다 — `character_id`(생략 시 대표 캐릭터)와
    `member.target_language` 로 서버가 정한다.
    """
    result = await TtsService(db).synthesize(
        member.member_id, data.text, data.character_id, if_none_match
    )
    # ⭐ 캐시 적중이면 **바디 없이 304**. 저장을 안 하는 이 API 의 유일한 절약 지점이다.
    if result.not_modified:
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": f'"{result.etag}"'},
        )
    return Response(
        content=result.audio,
        media_type=result.content_type,
        headers={
            "ETag": f'"{result.etag}"',
            # ⚠ private: 회원마다 목소리·언어가 다르므로 공용 캐시에 담기면 안 된다.
            "Cache-Control": "private, max-age=86400",
        },
    )
