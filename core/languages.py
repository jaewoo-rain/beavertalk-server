"""지원 학습 대상 언어 레지스트리(멀티랭귀지 Phase 1).

이 모듈은 "무엇을 가르치는가"(target language)의 단일 진실원이다. 통화·분석·체크판이
언어를 분기하지 않고 이 레지스트리의 `LanguageSpec` 한 행으로 동작을 결정한다.

- `code`           : ISO 639-1 언어코드(소문자). call.target_language·member_language_level·
                     커리큘럼 선별·증거/이력 집계 스코프의 키.
- `label`          : 프롬프트에 넣는 대상 언어 한국어 라벨(예: "한국어", "일본어").
- `level_count`    : 레벨 단계 수. 시드된 언어는 13[생존 L1 + CEFR 12], 미시드는 12[CEFR A1~C4]
                     (**지금은 6개 언어 모두 13** — 아래 레지스트리 참고).
- `has_curriculum` : 커리큘럼 데이터(learning_item/level profile)가 시드돼 있나.
                     True 면 정식 코스(검출/증거/힌트/레벨 프로파일 주입). False 는 **회화 전용**.
                     시드(parse_<lang>.py + seed) 후 True 로 뒤집는다.
                     ⚠ **지금은 6개 언어 모두 True** 다 — "ko·ja 만"이라고 적혀 있던 게 낡은
                     설명이었다(2026-08-12 정정). 이 값은 실동작을 가른다(힌트 사이드카 등).
- `leveltest`      : 언어별 레벨테스트 대본·루브릭·사다리 앵커가 준비됐나(**6개 언어 모두 True**).

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
#   ⭐ **6개 언어 전부 정식 코스**다(커리큘럼·레벨테스트 활성). level_count=13(생존 L1 + CEFR 12,
#     한국어와 같은 축)이고 문법 바둑판·체크판·레벨업이 전 언어에서 돈다.
#     zh·fr·vi 는 `c7a7e5f`("중국어·프랑스어·베트남어 정식화 — 6개 언어 완성")에서 자산이
#     들어갔다(`curriculum_v2_{fr,vi,zh}/survival_chunks.json`, `level_profiles_*.json`).
#   ⚠ **이 주석은 2026-08-12 까지 낡아 있었다** — "en/zh/fr/vi 는 아직 회화 전용"이라고 적혀
#     있었지만 값은 이미 True 였다(`c7a7e5f` 가 값만 고치고 5줄 위 주석을 안 고쳤다).
#     ⛔ `has_curriculum` 은 **실동작을 가른다**(예: 힌트 사이드카는 커리큘럼 있는 언어에만
#       뜬다). 주석을 믿고 값을 되돌렸으면 **전 언어의 힌트가 조용히 죽었다.**
SUPPORTED_LANGUAGES: dict[str, LanguageSpec] = {
    "ko": LanguageSpec("ko", "한국어", 13, True, True),
    "en": LanguageSpec("en", "영어", 13, True, True),
    "ja": LanguageSpec("ja", "일본어", 13, True, True),
    "zh": LanguageSpec("zh", "중국어", 13, True, True),
    "fr": LanguageSpec("fr", "프랑스어", 13, True, True),
    "vi": LanguageSpec("vi", "베트남어", 13, True, True),
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


def normalize_locale(value: str | None) -> str | None:
    """로케일 문자열을 ISO 639-1 소문자 언어코드로 정규화한다("ko-KR"→"ko").

    앱 언어 피커는 BCP-47 형태의 id("ko-KR")를 쓰는데, 서버의 모국어 라벨 표
    (persona_prompt._LOCALE_LABEL)는 ISO 639-1 키("ko")만 갖는다. 변환 없이 저장되면
    조회가 미스나 **영어로 폴백**한다 — 실측: 모국어를 한국어로 고른 회원의 프롬프트에
    "학습자의 모국어는 영어(English)다"가 박혀 있었다(활성 23명 중 8명 영향).

    구분자는 '-'(BCP-47)와 '_'(POSIX) 둘 다 받는다. 빈 값·None 은 None 그대로
    (호출부의 "미상" 폴백을 가로채지 않는다).

    근거: docs/20260728_0125_학습언어-DB-단일소스화와-모국어-정규화.md §2-3
    """
    if not value:
        return None
    head = value.strip().replace("_", "-").split("-", 1)[0].lower()
    return head or None


# --------------------------------------------------------------------------- #
# 문자 체계(script) 판별 — 레벨테스트 표본 게이트용
# --------------------------------------------------------------------------- #
# 언어코드 → 그 언어를 "실제로 썼다"고 볼 수 있는 유니코드 구간.
#
# 왜 필요한가: 레벨테스트 표본 게이트가 USER 발화 글자수를 세는데, 예전엔 isalnum()
# 으로 **아무 문자나** 셌다. 그래서 일본어 레벨테스트에서 한국어로만 164자를 떠들어도
# 게이트를 통과했다(실측: 일본어는 21자뿐인데 통과 → A1 배정). 학습자가 모국어로
# 도망친 통화를 "표본 충분"으로 본 것이다.
_SCRIPT_RANGES: dict[str, tuple[tuple[int, int], ...]] = {
    # 한글 음절 + 자모(호환 자모 포함)
    "ko": ((0xAC00, 0xD7A3), (0x1100, 0x11FF), (0x3130, 0x318F)),
    # 히라가나 + 가타카나 + 한자(CJK 통합)
    "ja": ((0x3040, 0x309F), (0x30A0, 0x30FF), (0x4E00, 0x9FFF), (0x3400, 0x4DBF)),
    # 한자만
    "zh": ((0x4E00, 0x9FFF), (0x3400, 0x4DBF), (0xF900, 0xFAFF)),
}

# 라틴 문자를 쓰는 언어들 — 개별 구간 대신 "ASCII 라틴 + 라틴 확장"으로 묶는다.
# ⚠ 한계: en/fr/vi 는 서로 구별되지 않는다. 모국어도 라틴 문자인 학습자에겐 이 게이트가
#   무력하다. 지금 막으려는 실제 실패 모드는 **모국어(한글)로 도망치는 것**이고 그건
#   잡힌다. 라틴↔라틴 구별이 필요해지면 어휘 기반 판별이 따로 있어야 한다.
_LATIN_RANGES: tuple[tuple[int, int], ...] = (
    (0x0041, 0x005A), (0x0061, 0x007A),   # A-Z a-z
    (0x00C0, 0x024F),                     # 라틴-1 보충 + 확장 A/B (é, ñ, ơ, ư …)
    (0x1E00, 0x1EFF),                     # 라틴 확장 추가 (베트남어 성조 부호)
)


def is_target_script_char(ch: str, language: str) -> bool:
    """[ch] 가 [language] 를 실제로 산출했다고 볼 수 있는 문자인가.

    등록되지 않은 언어는 **보수적으로 True**(=예전처럼 전부 계수)를 돌려준다. 새 언어를
    추가하면서 이 표를 안 채웠을 때 레벨테스트가 통째로 막히는 편보다, 게이트가 느슨한
    편이 낫다(R5 — 데이터 부재로 기능이 죽지 않게).
    """
    ranges = _SCRIPT_RANGES.get(language)
    if ranges is None:
        if language in SUPPORTED_LANGUAGES:
            ranges = _LATIN_RANGES
        else:
            return ch.isalnum()
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in ranges)


def count_target_script_chars(text: str, language: str) -> int:
    """[text] 에서 [language] 문자 수를 센다(문장부호·공백·타 언어 제외)."""
    return sum(1 for ch in text if is_target_script_char(ch, language))
