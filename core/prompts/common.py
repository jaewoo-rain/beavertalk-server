"""두 통화 코스가 **공유하는** 불변 프롬프트 자산 — 순수 문자열, LLM 생성 0.

여기 있는 것은 `core/persona_prompt.py`(normal·level_test)와 `core/prompts/expression.py`·
`freetalk.py` 가 **같이 쓰는** 조각이다. ⛔ **복사하지 마라 — 이 모듈이 소유한다.**
같은 문구가 두 곳에 있으면 어느 게 진짜인지 아무도 모른다(docs/prompts/README.md 원칙 3).

## ⛔ 이 파일을 고치면 무엇이 같이 바뀌나
`RULE_CLOSE_PROTOCOL`·`RULE_OFF_TOPIC` 은 **일반 통화 지시문의 본문**이다(persona_prompt 의
`_INVARIANTS_TEMPLATE` 이 이걸 이어 붙여 만든다). 한 글자만 고쳐도 실서비스 통화가 바뀌고
`tests/test_persona_prompt.py` 의 동결 스냅샷과 `tests/test_prompt_common_snapshot.py` 가
동시에 터진다. **그게 정상이다** — 의도한 변경이면 그때 재기준화하고 결정 로그에 적는다.

## ⛔ 옮겨온 것이지 새로 쓴 것이 아니다 (2026-09-10)
표현학습·프리토킹 코스를 만들면서 `persona_prompt.py` 에서 **원문 그대로** 떼어 왔다.
재배치일 뿐이라 `build_system_instruction` 출력은 **바이트 동일**하다.

## 각 자산의 지뢰 (되돌리면 사고가 재발한다)
- `CONTROL_TAG` vs `CLOSE_TAG_DEFAULT` — ⛔ **절대 다시 합치지 마라.** 둘 다 "[시스템]" 이던
  시절 종료 규약이 그 접두어를 종료 트리거로 정의해, 재접지·무음 넛지가 **종료 신호로
  오독**돼 통화가 조기에 끊겼다(실측 call_id=683 — 재접지 30초 뒤 작별).
  근거: docs/20260727_1710_통화-조기종료-종료태그-분리와-안전망.md
- `RULE_CLOSE_PROTOCOL` — ⛔ 여기에 **종료를 다시 설명하지 마라.** 이 문단은 "대화를
  이어가라"와 "낭독 금지" 두 가지뿐이고 종료 신호·태그·마무리 절차가 한 글자도 없다.
  종료 메커니즘을 지시문에서 가르치면 모델이 그걸 **실행 수단으로 쓴다**(call 706·852·870).
  ⛔ 금지 예시를 쓰지 마라 — 부정 지시 + 예시 조합은 모델이 그 예시를 그대로 뱉게 만든다
  (call 836/744/782). 전진 지시로만 쓴다.
- `RULE_OFF_TOPIC` — 거절을 길게 설명하지 않게 하는 마지막 줄을 빼지 마라(응답 길이 규칙과
  정면충돌한다).

상세는 `docs/prompts/README.md` §4 지뢰밭.
"""

from __future__ import annotations

import uuid

# --------------------------------------------------------------------------- #
# 모국어 라벨 (멀티랭귀지)
# --------------------------------------------------------------------------- #
# locale → 모국어 한국어 라벨. (멀티랭귀지) ko = 한국어 모국어 학습자(예: 한국인이
# 일본어를 배우는 도그푸딩) 정식화. 기존 통화의 locale 은 en/zh/… 라 이 키는 무영향.

LOCALE_LABEL: dict[str, str] = {
    "ko": "한국어",
    "en": "영어(English)", "zh": "중국어(中文)", "ja": "일본어(日本語)",
    "vi": "베트남어(Tiếng Việt)", "th": "태국어(ภาษาไทย)", "id": "인도네시아어(Bahasa Indonesia)",
    "mn": "몽골어(Монгол хэл)", "uz": "우즈베크어(Oʻzbek)", "ru": "러시아어(Русский)",
    "es": "스페인어(Español)", "fr": "프랑스어(Français)", "pt": "포르투갈어(Português)",
    "de": "독일어(Deutsch)", "ar": "아랍어(العربية)",
}
DEFAULT_LOCALE = "en"


def locale_label(locale: str | None, override: str | None = None) -> str:
    """모국어 라벨 1개. 미지원 코드는 영어로 폴백(persona_prompt 와 같은 규약).

    ⚠ 키는 **2자 코드**다("ko"). "ko-KR" 처럼 지역이 붙어 오면 조회가 미스나 영어로
      떨어진다 — 호출부가 `core.languages.normalize_locale` 로 정규화한 뒤 넘겨야 한다
      (실측 3건이 그렇게 영어로 샜다).
    """
    return override or LOCALE_LABEL.get(locale or "", LOCALE_LABEL[DEFAULT_LOCALE])


# ⭐ 잠금 분리(2026-09-12): CONTROL_TAG, CLOSE_TAG_DEFAULT, PERSONA_TAIL, REGROUND_COVERED_CAP, RULE_CLOSE_PROTOCOL, RULE_RESPONSE_LENGTH, DEFAULT_MAX_SENTENCES, RULE_NONVERBAL_SOUND, RULE_OFF_TOPIC, _OFF_TOPIC_NUMBER, RULE_OFF_TOPIC_BODY → core/prompts/locked/rules.py 로 **이동**(복사 아님 — 바이트 그대로). common.py 는 재수출한다(호출부 import 무변경).
from core.prompts.locked.rules import (
    CLOSE_TAG_DEFAULT,
    CONTROL_TAG,
    DEFAULT_MAX_SENTENCES,
    PERSONA_TAIL,
    REGROUND_COVERED_CAP,
    RULE_CLOSE_PROTOCOL,
    RULE_NONVERBAL_SOUND,
    RULE_OFF_TOPIC,
    RULE_OFF_TOPIC_BODY,
    RULE_RESPONSE_LENGTH,
    _OFF_TOPIC_NUMBER,
)



def new_close_tag() -> str:
    """통화 1건 전용 종료 태그를 만든다 — 통화 간 신호가 섞이지 않게 난수 접미.

    비버가 종료 태그를 스스로 뱉으면 그 출력이 자기 컨텍스트에 남아 다음 턴에 종료
    신호로 읽힌다(자기충족 루프 — 실측 call_id=706).

    ⚠ 난수는 그 방어의 **주력이 아니다**. 지시문이 태그를 보여주는 한 모델은 우연히
    맞히는 게 아니라 그대로 복사한다(실측 call_id=852 — 난수 d963 을 서버가 보내기
    2분 39초 전에 비버가 출력). 주 방어는 RULE_CLOSE_PROTOCOL 이 태그 리터럴을 아예
    노출하지 않는 것이고, 난수는 그 위에 남은 안전장치다.

    난수는 호출자(run_call)가 주입한다 — build_* 는 순수 조립 함수로 남는다.
    """
    return f"[통화종료:{uuid.uuid4().hex[:4]}]"


# --------------------------------------------------------------------------- #
# 불변 규칙 — 통화 지속 / 종료 규약 (모든 코스 공유)
# --------------------------------------------------------------------------- #
# ⛔ 문구를 바꾸면 build_system_instruction 출력이 변해 통화 회귀가 난다.
#   {close_tag} 외의 {중괄호}를 넣지 마라(.format 충돌).


