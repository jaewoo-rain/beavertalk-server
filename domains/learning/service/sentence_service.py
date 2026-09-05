"""SentenceService — 북마크 토글 + 북마크 목록. 소유는 call 경유 검증."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from core import storage, tts
from core.config import settings
from domains.learning.models.call import Call
from domains.learning.models.sentence import Sentence
from domains.learning.repository.sentence_repository import SentenceRepository
from domains.learning.schemas.call import EvaluationOut, SentenceOut
from domains.learning.schemas.sentence import SentenceTtsOut

logger = logging.getLogger(__name__)


class SentenceService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repo = SentenceRepository(db)

    def set_bookmark(self, member_id: int, sentence_id: int, value: bool) -> SentenceOut:
        sentence = self._get_owned(member_id, sentence_id)
        sentence.is_bookmarked = value
        self.db.commit()
        self.db.refresh(sentence)
        return self._to_out(sentence)

    def save_from_hint(
        self, member_id: int, call_id: int, korean: str, native: str
    ) -> SentenceOut:
        """통화 중 힌트를 즐겨찾기에 담는다 — 🔖 를 누른 그 순간 1행을 만든다.

        ## ⭐ 저장하면 기존 즐겨찾기와 **완전히 같은 행**이 된다
        `Sentence` 는 `call_id` 만 필수고 나머지는 선택이다. 통화 분석이 만드는 행과
        다른 것은 `source_type` 값 하나뿐이다(기존 asked/corrected/drilled + hint).
        ⇒ 목록·해제·문장 TTS·복습·발음평가가 **특별 취급 없이** 그대로 붙는다.

        ## ⛔ 중복은 에러가 아니라 재사용이다
        같은 힌트를 두 번 담아도 행은 하나여야 한다. 연타·재진입이 흔한 UI(🔖 버튼)라
        막지 않으면 목록이 같은 문장으로 더러워진다. 두 번째 호출도 **200 에 같은
        sentence_id** 를 돌려준다 — 프론트가 실패로 다루면 안 된다.

        ⚠ 소유 검증은 **404 로** 낸다(403 아님) — `_get_owned` 와 같은 규율이다.
          "남의 것"이라고 알려 주면 그 통화의 존재가 새어 나간다.
        """
        # ── 1) 소유 검증: 남의 통화에 문장을 심을 수 없다 ──────────────────
        call = self.db.get(Call, call_id)
        if call is None or call.member_id != member_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "통화를 찾을 수 없습니다.")

        korean = (korean or "").strip()
        native = (native or "").strip()
        if not korean:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "담을 문장이 없습니다."
            )

        # ── 2) 중복이면 그 행을 담고 끝낸다(새로 만들지 않는다) ────────────
        # ⚠ 키가 (call_id, korean) 이다. 같은 통화에서 같은 한국어 문장이면 같은 힌트로 본다.
        #   ⛔ source_type 도 함께 건다 — 분석이 만든 같은 문장이 있어도 그건 별개 행이다
        #     (출처가 다르고, 그쪽은 사용자가 실제로 말한 것이다).
        dup = (
            self.db.query(Sentence)
            .filter(
                Sentence.call_id == call_id,
                Sentence.korean_sentence == korean,
                Sentence.source_type == "hint",
                Sentence.deleted_at.is_(None),
            )
            .first()
        )
        if dup is not None:
            if not dup.is_bookmarked:
                dup.is_bookmarked = True
                self.db.commit()
                self.db.refresh(dup)
            return self._to_out(dup)

        # ── 3) 저장 ────────────────────────────────────────────────────────
        # ⭐ locale 은 **회원에서** 뽑는다. 통화 분석과 같은 함수를 써야 표기가 안 갈린다
        #   (normalcall_service.py:203 `_base_locale(member.language)`).
        from domains.account.models.member import Member as _Member
        from domains.learning.service.normalcall_service import _base_locale

        member = self.db.get(_Member, member_id)
        sentence = Sentence(
            call_id=call_id,
            korean_sentence=korean,
            native_sentence=native or None,
            locale=_base_locale(member.language if member else None),
            source_type="hint",
            is_bookmarked=True,
        )
        self.db.add(sentence)
        self.db.commit()   # ⚠ 쓰기는 service 가 커밋한다(R3)
        self.db.refresh(sentence)
        logger.info(
            "sentence: 힌트 담기 member=%s call=%s sentence=%s",
            member_id, call_id, sentence.sentence_id,
        )
        return self._to_out(sentence)

    def list_bookmarks(self, member_id: int) -> list[SentenceOut]:
        return [self._to_out(s) for s in self.repo.list_bookmarked(member_id)]

    async def synthesize_tts(
        self, member_id: int, sentence_id: int, client: Any | None
    ) -> SentenceTtsOut:
        """문장 단건 온디맨드 TTS — 이미 있으면 재사용, 없으면 합성→저장→URL 반환.

        idempotent: voice_url 이 이미 있으면 재합성 없이 그대로 반환. 핸들러(async)에서
        호출한다 — 합성(await)은 DB 세션 밖에서 하고, 저장만 동기 세션으로 처리한다.

        에러:
            404 — 없거나 타인 소유 문장(_get_owned).
            422 — korean_sentence 가 비어있음.
            503 — genai 클라이언트 None / 합성 실패 / 업로드 실패(오디오 생성 불가).
        """
        sentence = self._get_owned(member_id, sentence_id)

        # 1) idempotent: 이미 음성이 있으면 재합성 없이 그대로.
        if sentence.voice_url:
            return SentenceTtsOut(sentence_id=sentence_id, voice_url=sentence.voice_url)

        # 2) 합성 대상 텍스트 검증.
        korean = (sentence.korean_sentence or "").strip()
        if not korean:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "합성할 한국어 문장이 없습니다.",
            )

        call_id = sentence.call_id
        # (멀티랭귀지 + 캐릭터 음색) 이 통화의 학습 대상 언어로, 통화 캐릭터 목소리로 합성.
        # 언어·음색 두 축 — 없으면 ko / 언어 기본 음성으로 폴백.
        call = sentence.call or self.db.get(Call, call_id)
        language = (getattr(call, "target_language", None) or "ko")
        char = call.character if call else None
        char_voice = char.voice.name if (char and char.voice and char.voice.name) else None

        # 3) genai 미구성이면 합성 불가 → 503.
        if client is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "오디오를 생성할 수 없습니다(TTS 비활성).",
            )

        # 4) Cloud TTS 합성(await) — DB 세션 밖에서(대상 언어 Chirp3-HD, 캐릭터 음색).
        synthesized = await tts.synthesize(korean, language, voice=char_voice)
        if not synthesized:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "오디오를 생성할 수 없습니다.",
            )
        audio, content_type = synthesized

        # 5) public 버킷 업로드(기존 분석 파이프라인과 동일한 key 규칙).
        ext = "mp3" if content_type == "audio/mpeg" else "wav"
        path = f"tts/{call_id}/{sentence_id}.{ext}"
        key = storage.upload(settings.SUPABASE_BUCKET_SAMPLES, path, audio, content_type)
        url = storage.public_url(settings.SUPABASE_BUCKET_SAMPLES, key) if key else None
        if not url:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "오디오 저장에 실패했습니다.",
            )

        # 6) voice_url 저장(동기 세션). 재조회로 stale 회피 후 커밋.
        fresh = self.db.get(Sentence, sentence_id)
        if fresh is not None:
            fresh.voice_url = url
            self.db.commit()
        logger.info("on-demand TTS: sentence_id=%s 합성 완료 → %s", sentence_id, path)
        return SentenceTtsOut(sentence_id=sentence_id, voice_url=url)

    def soft_delete(self, member_id: int, sentence_id: int) -> None:
        """문장 소프트 삭제 — 행은 남기고 deleted_at 만 기록(읽기에서 제외됨)."""
        sentence = self._get_owned(member_id, sentence_id)
        sentence.deleted_at = datetime.now(timezone.utc)
        self.db.commit()

    # ── 내부 ──
    def _get_owned(self, member_id: int, sentence_id: int) -> Sentence:
        sentence = self.repo.get(sentence_id)
        # 발화의 소유는 그 발화가 속한 통화(call)의 회원으로 판단.
        # 이미 소프트 삭제된 발화는 없는 것으로 취급(404).
        if (
            sentence is None
            or sentence.deleted_at is not None
            or sentence.call.member_id != member_id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "발화를 찾을 수 없습니다.")
        return sentence

    def _to_out(self, s: Sentence) -> SentenceOut:
        return SentenceOut(
            sentence_id=s.sentence_id,
            korean_sentence=s.korean_sentence,
            native_sentence=s.native_sentence,
            locale=s.locale,
            voice_url=s.voice_url,
            is_bookmarked=s.is_bookmarked,
            evaluation=EvaluationOut.model_validate(s.evaluation) if s.evaluation else None,
        )
