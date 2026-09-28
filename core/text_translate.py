"""짧은 표시용 문장 묶음 번역 — 외부 어댑터(도메인·DB 를 모른다).

쓰는 곳(2026-09-29): 차시 상황 한 줄(`GET /cur/me` situation_translation) · 통화 기록 카드
제목(`GET /calls` summary). 둘 다 «요청된 항목만 번역하고 캐시»라 캐시는 도메인이 하고,
여기는 「이 문장들을 이 언어로」 1콜만 한다.

⛔ 통화 대본·분석 지시문과 **별개 경로**다(잠금 프롬프트 core/prompts/locked/* 무관).
⛔ 동기 함수다 — 읽기 API(sync 라우터, 스레드풀)에서 부른다. 응답을 막지 않게 HTTP
   타임아웃을 건다. 어떤 실패든 None(호출부가 원문/null 로 떨어진다 — R5).
"""

from __future__ import annotations

import logging

from google.genai import types
from pydantic import BaseModel

from core.prompts.common import LOCALE_LABEL

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You translate short UI strings for a Korean-learning app.\n"
    "Translate every item into {lang}. Return exactly {n} items in the same order.\n"
    "- Keep the meaning; do not add or drop information. Keep it short (it is a title/one-liner).\n"
    "- If an item is already written in {lang}, return it unchanged.\n"
    "- Output only the translations."
)


class _Out(BaseModel):
    items: list[str]


def language_name(locale: str) -> str:
    """번역 대상 언어 이름. 라벨 표(한국어 라벨 «영어(English)»)에 있으면 괄호 속 자국어 이름을,
    없으면(앱 30로케일 중 표 밖 — ne·my 등) ISO 639-1 코드로 지목한다."""
    label = LOCALE_LABEL.get(locale)
    if label and "(" in label and label.endswith(")"):
        return label[label.index("(") + 1:-1]
    return label or f"the language with ISO 639-1 code '{locale}'"


def translate_texts(
    client, model: str, texts: list[str], locale: str, *, timeout_s: float,
) -> list[str] | None:
    """texts 를 locale 언어로 번역해 **같은 순서·같은 개수**로 돌려준다. 실패하면 None.

    - client: genai.Client(없으면 None → 곧바로 None).
    - 개수가 어긋나면 어느 번역이 어느 원문인지 모르므로 전부 버린다(None).
    - 빈 번역 항목은 None 으로 둔다(호출부가 원문 유지·캐시 안 함).
    """
    if client is None or not texts:
        return None
    lang = language_name(locale)
    prompt = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    try:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM.format(lang=lang, n=len(texts)),
                response_mime_type="application/json",
                response_schema=_Out,
                temperature=0.0,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
            ),
        )
        parsed = getattr(response, "parsed", None)
        out = parsed if isinstance(parsed, _Out) else _Out.model_validate_json(response.text or "")
    except Exception as exc:  # noqa: BLE001 - 번역 실패가 조회를 막으면 안 된다(R5)
        logger.warning("text_translate: 번역 실패(원문 유지) locale=%s n=%d: %s", locale, len(texts), exc)
        return None
    if len(out.items) != len(texts):
        logger.warning(
            "text_translate: 개수 불일치(원문 유지) locale=%s 요청=%d 응답=%d",
            locale, len(texts), len(out.items),
        )
        return None
    return [(s or "").strip() or None for s in out.items]
