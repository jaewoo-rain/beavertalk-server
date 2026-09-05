"""sentence 관련 DTO(북마크 토글 · 힌트 담기)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SentenceBookmarkUpdate(BaseModel):
    is_bookmarked: bool


class SentenceTtsOut(BaseModel):
    """문장 단건 온디맨드 TTS 응답 — 합성/재사용된 재생 URL."""

    sentence_id: int
    voice_url: str


class SentenceFromHintIn(BaseModel):
    """통화 중 힌트를 즐겨찾기에 담는다 — 🔖 를 누른 그 순간에만 문장이 생긴다.

    ⛔ **힌트가 뜰 때는 저장하지 않는다**(사장님 결정 2026-09-05):
      "즐겨찾기 안해도 DB저장되면 너무 낭비인데?"
      5분 통화에 힌트 5회면 15행이 쌓이는데 그 대부분을 아무도 안 담는다.

    ⚠ `korean`·`native` 를 **클라가 돌려보낸다.** 힌트는 서버 어디에도 저장되지 않기
      때문이다(사이드카가 만들어 WS 로 쏘고 끝 — hint 테이블도, 사이드카의 DB 쓰기도 없다).
      ⇒ 그래서 **이 문장이 진짜 그 힌트였는지 서버가 검증하지 못한다.** 알고 가는 것이고,
        심어 봐야 자기 즐겨찾기 목록에만 들어가 남에게 영향이 없다.
        ⛔ 다만 `call_id` 소유 검증은 별개다 — 그건 남의 통화에 심는 것이라 반드시 막는다.

    ⚠ `roman`(로마자)은 받지 않는다 — `Sentence` 에 대응 필드가 없다.
    ⚠ `locale` 도 받지 않는다 — 서버가 회원에서 뽑는다(`_base_locale(member.language)`).
      클라가 보내면 통화 분석이 넣는 값과 표기가 갈린다.
    """

    call_id: int
    korean: str = Field(min_length=1, max_length=500)
    native: str = Field(min_length=1, max_length=500)
