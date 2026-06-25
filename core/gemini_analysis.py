"""구조화 LLM 호출 메커니즘 (normalcall 통화후 분석용) — 외부 어댑터.

"무엇을 분석하는가"(프롬프트·출력 스키마)는 도메인 지식이라 learning 서비스가 소유하고,
이 모듈은 "이 시스템 지시문 + 이 JSON 스키마로 generateContent 돌려서 파싱된 객체를 줘"
라는 메커니즘만 담당한다(speechsuper.py 와 동일한 어댑터 규율 — 도메인 import 0).

호출/파싱 패턴은 beavertalk analysis._analyze 동형(Vertex generateContent +
response_schema). 네트워크/파싱/빈 입력 등 어떤 실패든 None 을 반환한다(graceful).
"""

from __future__ import annotations

import logging
from typing import Type, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


async def generate_structured(
    client: genai.Client,
    model: str,
    *,
    system_instruction: str,
    prompt: str,
    schema: Type[T],
    temperature: float = 0.2,
) -> T | None:
    """generateContent(response_schema=schema) 1콜로 구조화 출력을 받아 파싱한다.

    Args:
        client: lifespan 이 만든 genai.Client(app.state.genai_client).
        model: 분석 모델 식별자(settings.JUDGE_MODEL).
        system_instruction: 분석 지시문(도메인 서비스가 조립).
        prompt: 사용자 콘텐츠(예: 전사).
        schema: 출력 Pydantic 모델 타입.
        temperature: 생성 온도(기본 0.2).

    Returns:
        파싱된 schema 인스턴스, 또는 빈 입력/호출/파싱 실패 시 None.
    """
    if not prompt or not prompt.strip():
        return None

    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 호출 실패 graceful
        logger.warning("gemini_analysis: generate_content 실패(무시): %s", exc)
        return None

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema):
        return parsed

    raw = getattr(response, "text", None)
    if raw:
        try:
            return schema.model_validate_json(raw)
        except Exception as exc:  # noqa: BLE001 - 파싱 실패 graceful
            logger.warning("gemini_analysis: 결과 파싱 실패(무시): %s", exc)
    return None
