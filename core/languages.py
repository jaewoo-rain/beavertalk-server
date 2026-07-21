"""지원 학습 대상 언어 레지스트리(멀티랭귀지 Phase 1).

이 모듈은 "무엇을 가르치는가"(target language)의 단일 진실원이다. 통화·분석·체크판이
언어를 분기하지 않고 이 레지스트리의 `LanguageSpec` 한 행으로 동작을 결정한다.

- `code`           : ISO 639-1 언어코드(소문자). call.target_language·member_language_level·
                     커리큘럼 선별·증거/이력 집계 스코프의 키.
- `label`          : 프롬프트에 넣는 대상 언어 한국어 라벨(예: "한국어", "일본어").
- `level_count`    : 레벨 단계 수. 시드된 언어(ko·ja)는 13[생존 L1 + CEFR 12], 미시드는 12[CEFR A1~C4].
- `has_curriculum` : 커리큘럼 데이터(learning_item/level profile)가 시드돼 있나.
                     True(ko·ja)면 정식 코스(검출/증거/힌트/레벨 프로파일 주입). False 는 **회화 전용**.
                     시드(parse_<lang>.py + seed) 후 True 로 뒤집는다.
- `leveltest`      : 언어별 레벨테스트 대본·루브릭·사다리 앵커가 준비됐나(ko·ja True).

새 언어 = 여기 1행 + DB 시드 + 콘텐츠. 코드 분기는 추가하지 않는다(관통 원칙).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageSpec:
    """지원 대상 언어 1종의 능력 명세(불변)."""

    code: str            # ISO 639-1 (소문자)
    label: str           # 프롬프트용 한국어 라벨
    level_count: int     # 레벨 단계 수
    has_curriculum: bool # 커리큘럼 데이터 시드 여부(회화 전용 게이트)
    leveltest: bool      # 레벨테스트 대본·루브릭 준비 여부


# 지원 언어 레지스트리.
#   ko·ja : 커리큘럼·레벨테스트 활성(정식 코스). ja 는 도그푸딩용으로 T4b/T5 에서 시드·저작 완료
#           — level_count=13(생존 L1 + CEFR 12, 한국어와 동일 축), 문법 바둑판·체크판·레벨업 동작.
#   en/zh/fr/vi : 아직 회화 전용(커리큘럼·레벨테스트 데이터 미시드) — 시드·저작 후 True 로 뒤집는다.
SUPPORTED_LANGUAGES: dict[str, LanguageSpec] = {
    "ko": LanguageSpec("ko", "한국어", 13, True, True),
    "en": LanguageSpec("en", "영어", 13, True, True),
    "ja": LanguageSpec("ja", "일본어", 13, True, True),
    "zh": LanguageSpec("zh", "중국어", 12, False, False),
    "fr": LanguageSpec("fr", "프랑스어", 12, False, False),
    "vi": LanguageSpec("vi", "베트남어", 12, False, False),
}

# 대상 언어 기본값(오버라이드/미지원 시 폴백). settings.DEFAULT_TARGET_LANGUAGE 와 같은 값.
DEFAULT_LANGUAGE = "ko"

# 전환기 관용: 구 데모가 넘기던 **한국어 라벨**("프랑스어" 등)도 코드로 역해석한다.
# label → code 역인덱스(신규 코드 전달이 정식 — 라벨은 하위호환용).
_LABEL_ALIAS: dict[str, str] = {spec.label: spec.code for spec in SUPPORTED_LANGUAGES.values()}


def resolve_language(code: str | None) -> LanguageSpec | None:
    """언어코드(또는 구 데모 라벨)를 LanguageSpec 으로 해석한다. 미지원이면 None.

    - 코드는 소문자 정규화 후 조회("JA"→"ja").
    - 미지원 코드라도 구 데모 라벨("프랑스어" 등)이면 역인덱스로 구제(전환기).
    - 그 외(예: "스페인어", "xx")는 None → 호출부가 DEFAULT 로 폴백한다.
    """
    if not code:
        return None
    spec = SUPPORTED_LANGUAGES.get(code.strip().lower())
    if spec is not None:
        return spec
    alias = _LABEL_ALIAS.get(code.strip())
    if alias is not None:
        return SUPPORTED_LANGUAGES[alias]
    return None
