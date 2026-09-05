"""온디맨드 TTS DTO — 문장을 주면 음성 바이트를 돌려준다.

⚠ 응답 DTO 가 **없다.** 이 API 는 JSON 이 아니라 MP3 바이너리를 돌려주기 때문이다
  (`fastapi.Response(media_type="audio/mpeg")`). 응답 스키마를 만들면 그걸 붙이고 싶어지고,
  붙이는 순간 JSON 직렬화가 끼어들어 바이너리가 깨진다.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# ⭐ 한 문장 기준. 문단을 읽어 줘야 하면 이 값을 올리는 게 아니라 **프론트가 문장 단위로
#   쪼개** 보내는 것이 맞다 — 그래야 캐시(ETag)도 문장 단위로 먹는다.
# ⚠ 원가가 글자 수에 비례한다(Cloud TTS 는 문자 과금). 이 상한이 1회 원가의 천장이다.
TTS_TEXT_MAX_CHARS = 200


class TtsSpeechIn(BaseModel):
    """`POST /tts/speech` 요청.

    ⭐ **목소리 이름도 언어 코드도 받지 않는다.** 서버가 정한다:
        목소리  character_id → character.voice.name
                안 주면 member.character_id(사용자가 고른 대표 캐릭터)
        언어    member.target_language

    ⛔ 왜 클라가 안 고르나 — 앱에 벤더 목소리 이름("Leda" 같은)이 굳으면 캐릭터 음색을
      바꿀 때 앱을 같이 고쳐야 한다. 기존 `/sentences/{id}/tts` 도 같은 규율이다.
    """

    text: str = Field(min_length=1, max_length=TTS_TEXT_MAX_CHARS)
    # ⚠ 선택이다. 안 주면 대표 캐릭터로 떨어진다 — 통화의 폴백 사슬과 같은 규칙이라
    #   «통화에선 A 목소리인데 TTS 는 B 목소리» 같은 어긋남이 안 생긴다.
    character_id: Optional[int] = None
