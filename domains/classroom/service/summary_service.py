"""통화 요약의 교사 로케일 제공 — 열 때 1회 번역, 이후 캐시.

`10_다국어_앱화면.md` §12.7 미결의 구현. 세 선택지 중 **교사가 열 때 1회 번역**을 골랐다.
통화 시점 비용이 0 이고, 실제로 읽는 요약만 번역하기 때문이다.

**graceful degradation (R5)** — genai 클라이언트가 없으면 원문을 그대로 주고
`translated=False` 로 표시한다. 콘솔이 "번역 불가"를 화면에 드러낸다.
번역 기능 부재로 요약 자체가 사라지면 안 된다.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from domains.classroom.models.call_summary_translation import CallSummaryTranslation
from domains.learning.models.call import Call
from domains.learning.models.sentence import Sentence

# 콘솔은 ko + en 2종이다(`10` §12). 여기를 30종으로 늘리지 마라.
SUPPORTED = ("ko", "en")

_PROMPT = (
    "다음은 한국어 학습자의 통화 요약이다. 교사가 읽을 수 있도록 {target} 로 옮겨라.\n"
    "- 의미를 더하거나 빼지 마라.\n"
    "- 예시로 인용된 한국어 표현은 한국어 그대로 둔다(학습 대상이다).\n"
    "- 결과만 출력한다.\n\n"
    "요약:\n{text}"
)
_LABEL = {"ko": "한국어", "en": "English"}


class SummaryService:
    def __init__(self, db: Session, genai: Any | None = None) -> None:
        self.db = db
        self.genai = genai

    def source_locale(self, call_id: int) -> Optional[str]:
        """요약이 어느 언어로 쓰였나 — 그 통화의 문장 로케일이 근거다."""
        return self.db.scalar(
            select(Sentence.locale).where(Sentence.call_id == call_id).limit(1)
        )

    def get(self, call_id: int, locale: str) -> dict:
        """교사 로케일 요약. `{text, translated, source_locale}`.

        ⛔ `call.summary` 를 덮어쓰지 않는다. 원본은 학습자 것이다.
        """
        if locale not in SUPPORTED:
            locale = "ko"

        call = self.db.get(Call, call_id)
        source = (call.summary or "").strip() if call else ""
        if not source:
            return {"text": "", "translated": False, "source_locale": None}

        src_locale = self.source_locale(call_id)
        if src_locale == locale:
            return {"text": source, "translated": False, "source_locale": src_locale}

        cached = self.db.scalar(
            select(CallSummaryTranslation).where(
                CallSummaryTranslation.call_id == call_id,
                CallSummaryTranslation.locale == locale,
            )
        )
        if cached is not None:
            return {"text": cached.text, "translated": True, "source_locale": cached.source_locale}

        translated = self._translate(source, locale)
        if translated is None:
            # 번역기가 없다 — 원문을 주고 그 사실을 알린다. 요약을 숨기지 않는다.
            return {"text": source, "translated": False, "source_locale": src_locale}

        row = CallSummaryTranslation(
            call_id=call_id, locale=locale, text=translated, source_locale=src_locale
        )
        self.db.add(row)
        self.db.commit()
        return {"text": translated, "translated": True, "source_locale": src_locale}

    def _translate(self, text: str, locale: str) -> Optional[str]:
        if self.genai is None:
            return None
        prompt = _PROMPT.format(target=_LABEL.get(locale, "English"), text=text)
        try:
            res = self.genai.models.generate_content(
                model="gemini-2.0-flash", contents=prompt
            )
            out = (getattr(res, "text", "") or "").strip()
            return out or None
        except Exception:
            # 외부 호출 실패로 콘솔이 죽으면 안 된다(R5).
            return None
