"""OpenAI Realtime 통화 엔진 — **격리 패키지**.

## ⛔⛔ 이 패키지의 단 하나의 규칙
**Gemini 자산을 import 하지 않는다.** 금지 목록:

    core.prompts.*        (locked · editable 전부)
    core.gemini_live
    core.persona_prompt

왜: 「새 파일」로만 가르면 기존 조립 결과를 복사해 두 줄 고치는 식으로 흔적이 새어
들어온다. Gemini 지시문·툴 선언·이벤트 모양은 **Gemini 의 한계 때문에 생긴 것**이고
(조각 분할·컨텍스트 압축·재개 쪽지·재접지·종료 시드 대기), 이 엔진엔 그 한계가 없다.
한 줄이라도 섞이면 「오지 않는 신호를 기다리는」 지시문이 다시 만들어진다.

⭐ 약속이 아니라 기계로 막는다: `tests/test_openai_isolation.py` 가 이 폴더의 모든
  `.py` 를 AST 로 파싱해 import 를 전수 검사한다. **끄지 마라.**

⚠ 반대로 **도메인 자산은 그대로 쓴다** — 프론트 계약(`protocol.py`)·DB 모델·커리큘럼·
  캐릭터·통화후 분석·원가 진입점(`estimate_call_cost_usd`)·`call_session` 의 2펌프와
  종료 규약. 가르는 기준은 「Gemini 의 한계 때문에 생긴 것만 버린다」다.
"""
