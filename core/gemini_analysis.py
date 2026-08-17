"""구조화 LLM 호출 메커니즘 (normalcall 통화후 분석용) — 외부 어댑터.

"무엇을 분석하는가"(프롬프트·출력 스키마)는 도메인 지식이라 learning 서비스가 소유하고,
이 모듈은 "이 시스템 지시문 + 이 JSON 스키마로 generateContent 돌려서 파싱된 객체를 줘"
라는 메커니즘만 담당한다(speechsuper.py 와 동일한 어댑터 규율 — 도메인 import 0).

호출/파싱 패턴은 beavertalk analysis._analyze 동형(Vertex generateContent +
response_schema). 네트워크/파싱/빈 입력 등 어떤 실패든 None 을 반환한다(graceful).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Type, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass
class LlmUsage:
    """이 어댑터를 통해 나간 LLM 콜의 토큰을 모으는 **수집기**(원가 계기판의 눈).

    🧒 왜 필요한가: `call.usage_*` 는 **Live 세션만** 센다. 그런데 통화 1건에는 Live 말고도
      LLM 이 여러 번 돈다 — 통화중(동적 힌트·재접지 브리프·레벨테스트 턴 판정)과
      통화후(문장 추출·검출·레벨 판정). 전부 이 파일을 타는데 여태 **한 토큰도 안 셌다.**
      그래서 "5분 $0.19" 같은 숫자가 Live 몫만 담은 채 전체 원가처럼 읽혔다.

    ⛔ **호출부를 고치지 않고** 붙일 수 있어야 한다 — 그래서 반환값을 바꾸지 않고
      선택 인자(`usage=`)로 주입받는 수집기 모양이다. 안 넘기면 종전과 100% 동일하다.
    ⛔ R5: 수집이 실패해도 통화·분석은 그대로 간다. add_response 는 예외를 전부 먹는다.
    ⚠ 필드 이름은 `usage_json.vendors.llm` 과 **같은 계약**이다(vendor/in_text/out_text/
      thoughts) — 원가 계산이 캐스케이드 LLM 다리와 **같은 코드**를 재사용하기 위해서다.
      ⭐ thoughts 를 따로 세는 이유: 사고 토큰은 **출력 단가로 과금**되는데 응답 본문에
      안 들어온다. 빼먹으면 낸 돈의 일부가 통계에서 사라진다.
    """

    vendor: str = ""
    calls: int = 0
    in_text: int = 0
    out_text: int = 0
    thoughts: int = 0
    failures: int = 0          # 응답 자체를 못 받은 콜(토큰 0 — 과금 없음)
    models: set[str] = field(default_factory=set)

    def add_response(self, model: str, response) -> None:
        """응답 1건의 usage_metadata 를 더한다(예외 전량 흡수 — 계기판이 기능을 죽이면 안 된다).

        ⚠ **파싱 실패한 응답도 센다.** 파싱은 우리 사정이고 과금은 이미 끝났다 —
          실패한 콜을 안 세면 "돈은 나갔는데 계기판엔 없는" 구멍이 다시 생긴다.
        """
        try:
            self.calls += 1
            self.models.add(model)
            if not self.vendor:
                self.vendor = model
            um = getattr(response, "usage_metadata", None)
            if um is None:
                return
            self.in_text += int(getattr(um, "prompt_token_count", 0) or 0)
            self.out_text += int(getattr(um, "candidates_token_count", 0) or 0)
            self.thoughts += int(getattr(um, "thoughts_token_count", 0) or 0)
        except Exception as exc:  # noqa: BLE001 - 계측 실패는 통화·분석과 무관(R5)
            logger.warning("gemini_analysis: usage 수집 실패(무시): %s", exc)

    def note_failure(self) -> None:
        """응답을 못 받은 콜(네트워크·예외). 토큰은 없지만 **몇 번 실패했는지**는 남긴다."""
        self.failures += 1

    def merge(self, other: "LlmUsage") -> None:
        """다른 수집기를 흡수한다(통화중 사이드카가 여러 태스크로 흩어질 때)."""
        if other is None:
            return
        self.calls += other.calls
        self.in_text += other.in_text
        self.out_text += other.out_text
        self.thoughts += other.thoughts
        self.failures += other.failures
        self.models |= other.models
        if not self.vendor:
            self.vendor = other.vendor

    def as_dict(self) -> dict | None:
        """usage_json 에 실을 모양. **한 번도 안 돌았으면 None** — 0 과 구별한다.

        ⚠ 모델이 섞였으면(`models` 2개 이상) vendor 하나로 값을 매기면 틀린다.
          그 사실을 `models` 로 남겨, 원가 계산이 아니라 **읽는 사람**이 알아채게 한다.
        """
        if not self.calls and not self.failures:
            return None
        out: dict = {
            "vendor": self.vendor,
            "calls": self.calls,
            "in_text": self.in_text,
            "out_text": self.out_text,
            "thoughts": self.thoughts,
        }
        if self.failures:
            out["failures"] = self.failures
        if len(self.models) > 1:
            out["models"] = sorted(self.models)
        return out


async def generate_structured(
    client: genai.Client,
    model: str,
    *,
    system_instruction: str,
    prompt: str,
    schema: Type[T],
    temperature: float = 0.2,
    thinking_budget: int | None = None,
    usage: LlmUsage | None = None,
) -> T | None:
    """generateContent(response_schema=schema) 1콜로 구조화 출력을 받아 파싱한다.

    Args:
        client: lifespan 이 만든 genai.Client(app.state.genai_client).
        model: 분석 모델 식별자(settings.JUDGE_MODEL).
        system_instruction: 분석 지시문(도메인 서비스가 조립).
        prompt: 사용자 콘텐츠(예: 전사).
        schema: 출력 Pydantic 모델 타입.
        temperature: 생성 온도(기본 0.2).
        thinking_budget: 추론 토큰 예산. None(기본)이면 config 에 아예 넣지 않아
            종전 호출과 동일(모델 기본값). 0 이면 추론 비활성 — 통화중 힌트
            사이드카(D16)처럼 지연이 중요한 단발 콜용.
        usage: 토큰 수집기(LlmUsage). 넘기면 이 콜의 usage_metadata 를 거기 더한다.
            ⛔ **넘기지 않으면 종전과 완전히 동일하다**(반환값·부수효과 무변화) —
            호출부를 한 줄도 안 고치고 계기판을 붙일 수 있게 한 이유다.

    Returns:
        파싱된 schema 인스턴스, 또는 빈 입력/호출/파싱 실패 시 None.
    """
    if not prompt or not prompt.strip():
        return None

    config_kwargs: dict = {}
    if thinking_budget is not None:
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)

    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json",
                response_schema=schema,
                temperature=temperature,
                **config_kwargs,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 호출 실패 graceful
        logger.warning("gemini_analysis: generate_content 실패(무시): %s", exc)
        if usage is not None:
            usage.note_failure()
        return None

    # ⚠ 파싱보다 **먼저** 센다. 응답이 온 시점에 과금은 이미 끝났고, 아래 파싱 실패
    #   경로로 빠져도 그 돈은 나갔다. 여기 두지 않으면 실패한 콜이 계기판에서 사라진다.
    if usage is not None:
        usage.add_response(model, response)

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
