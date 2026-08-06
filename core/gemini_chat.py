"""캐스케이드 대화 LLM — **텍스트 스트리밍** 어댑터(도메인·DB 무지).

`gemini_analysis.generate_structured` 는 통화후 분석용 **단발·구조화** 호출이다. 통화 중에는
성질이 정반대라 따로 둔다: 첫 토큰이 빠를수록 좋고(첫 소리 지연), 문장이 완성되는 대로
TTS 로 흘려야 하며, 출력은 자유 텍스트다.

⭐ `thinking_budget=0` 이 기본이다. 음성 대화에 사고 토큰은 대부분 불필요한데 **출력 단가로
  과금되고 첫 소리를 늦춘다** — 원가·체감속도 둘 다 손해다(bt-back 지시, 2026-08-07).

graceful(R5): 클라이언트가 없거나 호출이 실패하면 **예외를 밖으로 내지 않고** 빈 스트림으로
끝난다. 캐스케이드 세션은 비버가 말을 못 해도 턴 감지·통화 자체는 계속 굴러야 한다.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


class ChatStream:
    """LLM 응답 1건의 스트리밍 핸들 — 텍스트 조각을 흘리고, 끝나면 사용량을 남긴다.

    사용량을 여기서 들고 있는 이유: 원가 수집(`CascadeUsage.record_llm`)은 **응답 객체를
    본 자리**에서만 정확하다. 호출부가 토큰을 세지 않는다(세면 벤더와 어긋난다).
    """

    __slots__ = ("text", "usage_metadata", "failed", "_client", "_model", "_kwargs")

    def __init__(self, client: Any, model: str, kwargs: dict) -> None:
        self._client = client
        self._model = model
        self._kwargs = kwargs
        self.text = ""                 # 지금까지 생성된 전체(감사·이력용 원본)
        self.usage_metadata: Any = None
        self.failed = False

    async def chunks(self) -> AsyncIterator[str]:
        """생성 텍스트 조각을 도착하는 대로 흘린다(실패는 흡수하고 조용히 끝낸다)."""
        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=self._model, **self._kwargs
            )
        except Exception as exc:  # noqa: BLE001 - 호출 실패 graceful(R5)
            self.failed = True
            logger.warning("cascade llm: 스트림 개시 실패(무시) — %s", exc)
            return
        try:
            async for response in stream:
                # usage_metadata 는 조각마다 갱신돼 오기도 한다 — **마지막 값**이 그 응답의 총합이다.
                usage = getattr(response, "usage_metadata", None)
                if usage is not None:
                    self.usage_metadata = usage
                piece = getattr(response, "text", None)
                if not piece:
                    continue
                self.text += piece
                yield piece
        except Exception as exc:  # noqa: BLE001 - 중도 실패도 흡수(이미 말한 데까지는 유효)
            self.failed = True
            logger.warning("cascade llm: 스트림 중단(무시) — %s", exc)


def open_chat_stream(
    client: Any,
    model: str,
    *,
    system_instruction: str,
    history: list[dict],
    user_text: str,
    thinking_budget: int | None = 0,
    temperature: float = 0.9,
) -> ChatStream | None:
    """대화 1턴을 스트리밍으로 연다. 클라이언트·SDK 가 없으면 None(호출부는 비버를 건너뛴다).

    Args:
        history: [{"role": "user"|"model", "text": str}] — **실제로 들린 대사**만 넣는다
            (끊긴 비버 발화는 생성 전체가 아니라 들린 데까지. 설계 §5).
        user_text: 이번 사용자 발화(STT 최종 전사).
        thinking_budget: 0 = 추론 끄기(기본). None 이면 모델 기본값.
    """
    if client is None or not (user_text or "").strip():
        return None
    try:
        from google.genai import types
    except Exception as exc:  # noqa: BLE001 - 미설치 환경 graceful
        logger.warning("cascade llm: google-genai 미설치(무시) — %s", exc)
        return None

    contents = []
    for item in history:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        role = "model" if item.get("role") == "model" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
    contents.append(types.Content(role="user", parts=[types.Part(text=user_text.strip())]))

    config_kwargs: dict = {}
    if thinking_budget is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
        **config_kwargs,
    )
    return ChatStream(client, model, {"contents": contents, "config": config})
