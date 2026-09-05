"""온디맨드 TTS — 임의 문장을 받아 MP3 바이트를 돌려준다(저장 안 함).

## ⚠ 기존 `/sentences/{id}/tts` 와 다르다 — 헷갈리지 마라

    sentences/{id}/tts   저장된 문장 · Storage 업로드 · JSON {"voice_url"}
    tts/speech (여기)     임의 문장   · 저장 안 함      · MP3 바이너리

학습 문장 재생은 계속 위쪽을 쓴다. 여기는 힌트 읽어주기처럼 **저장할 이유가 없는** 문장용이다.

## ⛔ 저장을 안 하면 캐시가 없다 — 그래서 ETag 가 유일한 절약이다

기존 API 는 `voice_url` 이 있으면 재합성을 건너뛴다. 여기는 그 방어가 없어서, 같은 문장을
열 번 요청하면 **열 번 합성하고 열 번 과금**된다(Cloud TTS 는 문자 과금).
⇒ `If-None-Match` 가 맞으면 **304 를 주고 합성을 아예 안 한다**(원가 0). 서버가 가진 유일한
  절약 장치이고, 이게 깨지면 조용히 매번 재합성한다 — 로그에도 안 남는다.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core import tts
from domains.account.models.member import Member
from domains.commerce.models.character import Character

logger = logging.getLogger(__name__)


class TtsResult:
    """합성 결과 또는 «캐시 적중(304)» 신호.

    ⚠ 라우터가 응답을 만든다(바이너리/304 는 HTTP 층의 일이다). service 는 재료만 준다.
    """

    __slots__ = ("audio", "content_type", "etag", "not_modified")

    def __init__(
        self,
        *,
        audio: Optional[bytes],
        content_type: str,
        etag: str,
        not_modified: bool = False,
    ) -> None:
        self.audio = audio
        self.content_type = content_type
        self.etag = etag
        self.not_modified = not_modified


class TtsService:
    def __init__(self, db: Session) -> None:
        self.db = db

    async def synthesize(
        self,
        member_id: int,
        text: str,
        character_id: Optional[int],
        if_none_match: Optional[str] = None,
    ) -> TtsResult:
        """문장 → MP3. 캐시 적중이면 합성 없이 `not_modified=True`.

        ⛔ 잠금 캐릭터 검사를 **안 한다**(사장님 결정 2026-09-04). 무료 사용자가 유료
          캐릭터 음색을 들을 수 있다 — 알고 가는 위험이다. 나중에 막으려면 판정기가
          이미 있다(`character_service._unlock` — 소유 먼저, 그다음 구독).
        """
        text = (text or "").strip()
        if not text:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "합성할 문장이 없습니다."
            )

        member = self.db.get(Member, member_id)
        # ⚠ 회원이 없을 리 없다(JWT 를 통과했다). 그래도 None 이면 폴백으로 진행한다 —
        #   여기서 500 을 내면 «음성이 안 나온다» 가 «앱이 깨진다» 가 된다.
        language = (getattr(member, "target_language", None) or "ko") if member else "ko"

        # ── 캐릭터 해석 ────────────────────────────────────────────────────
        # ⭐ 준 값 → 그대로 / 안 줬으면 대표 캐릭터(member.character_id).
        #   통화의 폴백 사슬(`resolve_call_character`)과 **같은 규칙**이다 — 두 곳이
        #   어긋나면 «통화에선 A 목소리인데 TTS 는 B 목소리» 가 된다.
        wanted_id = character_id if character_id is not None else (
            getattr(member, "character_id", None) if member else None
        )
        char: Optional[Character] = None
        if wanted_id is not None:
            char = self.db.get(Character, wanted_id)
            # ⛔ **클라가 준 id 가 없으면 404 다.** 조용히 대표 캐릭터로 떨어뜨리면
            #   프론트는 «내가 고른 목소리로 나왔다» 고 믿는다 — 틀린 성공이 제일 나쁘다.
            #   ⚠ 대표 캐릭터가 없어서 None 인 경우는 여기 안 온다(character_id 가 None).
            if char is None and character_id is not None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND, "캐릭터를 찾을 수 없습니다."
                )
        voice = char.voice.name if (char and char.voice and char.voice.name) else None

        # ── ETag: 저장 없이 재요청을 막는 유일한 장치 ──────────────────────
        # ⚠ 키에 **셋 다** 넣는다. 같은 문장이라도 캐릭터·언어가 바뀌면 다른 소리다.
        #   voice 대신 character_id 를 쓰지 않는 이유: 캐릭터의 음색이 나중에 바뀌면
        #   같은 id 라도 소리가 달라진다. 소리를 정하는 것은 voice 다.
        etag = hashlib.sha256(
            f"{text}|{voice or '-'}|{language}".encode("utf-8")
        ).hexdigest()[:32]
        if if_none_match and if_none_match.strip('"') == etag:
            logger.info(
                "tts: 캐시 적중(합성 안 함) member=%s 글자=%d 언어=%s",
                member_id, len(text), language,
            )
            return TtsResult(
                audio=None, content_type="audio/mpeg", etag=etag, not_modified=True
            )

        # ── 합성 ──────────────────────────────────────────────────────────
        # ⛔ 키 부재·합성 실패는 **503** 이다(R5 graceful degradation). 서버가 죽지 않고
        #   기능만 꺼진다 — 앱은 «음성 없이 텍스트만» 폴백이 있어야 한다.
        synthesized = await tts.synthesize(text, language, voice=voice)
        if not synthesized:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "오디오를 생성할 수 없습니다(TTS 비활성 또는 합성 실패).",
            )
        audio, content_type = synthesized

        # ⚠ 이 API 는 `call` 행이 없어 **원가 계기판에 안 잡힌다.** 쓰기 시작하면
        #   «왜 Cloud TTS 청구가 늘었지?» 를 되짚을 근거가 이 로그뿐이다.
        logger.info(
            "tts: 합성 member=%s 글자=%d 언어=%s 음성=%s 바이트=%d",
            member_id, len(text), language, voice or "(언어기본)", len(audio),
        )
        return TtsResult(audio=audio, content_type=content_type, etag=etag)
