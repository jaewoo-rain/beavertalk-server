"""편집 가능한 프롬프트 문구 로더 — `core/prompts/editable/<name>.md` 를 import 시 읽어 검사·캐시한다(LLM 생성 0).

## 왜 (2026-09-12 사장님 — «잠금 / 편집 분리»)
사람들이 톤·설명을 고칠 때 표정·진도·판정 배관이 깨지지 않게, 프롬프트를 두 층으로 갈랐다:
  · `core/prompts/locked/`  — 기계가 기대는 문장(파이썬 상수·해시 시험). 고치려면 bt-back 과 시험을 같이.
  · `core/prompts/editable/*.md` — 사람이 고치는 문장(페르소나 톤·코스 소개·언어 비율 서술·교정 문구·선톡 말투). 이 로더가 읽는다.

## 검사 (하나라도 어기면 그 파일은 버리고 `<name>.default.md` 로 폴백 — 기동은 한다, 로그 ERROR)
  1. 섹션 집합이 기본판과 같다(추가·삭제·개명 금지).
  2. 섹션마다 `{슬롯}` 집합이 기본판과 같다(누락·미지 슬롯 금지 — .format 이 깨지거나 값이 안 들어간다).
  3. 금지어 출현 수가 기본판을 넘지 않는다(종료 어휘·«퀴즈» 등 — README §4 지뢰밭).
  4. `[` 로 시작하는 줄의 라벨이 기본판에 없는 새 것이면 금지(대괄호 = 서버 태그·구획 라벨 문법).
  5. 토큰 예산(자수 × 0.6 근사 — count_tokens 실측은 EDITING.md) 을 넘지 않는다.
`<name>.default.md` 는 저장소에 함께 둔 **현재 문구 스냅샷**이다 — 기본판 자체가 깨져 있으면 그건 배포 오류라 즉시 예외를 낸다(시험이 지킨다).

## 형식
HTML 주석(`<!-- … -->`)은 편집 규칙 안내 — 파싱 전에 지운다. `## 이름` 줄이 섹션 시작, 다음 `## ` 까지가 본문(앞뒤 빈 줄만 걷는다 —
줄 안의 들여쓰기·공백은 그대로 살린다: 규칙 불릿의 앞 공백 3칸이 조립 결과의 일부다).
"""
from __future__ import annotations

import logging
import math
import os
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

EDITABLE_DIR = os.path.join(os.path.dirname(__file__), "editable")

#: 기본판을 넘어 **늘리면** 안 되는 낱말(출현 수 비교). 종료 어휘는 비버가 그대로 뱉어 통화를 스스로 끊고(call 706·782·870),
#: «퀴즈» 는 서버 상태기계(T16)가 여는 것이라 대본에 늘리면 비버가 스스로 퀴즈를 시작한다.
BANNED_WORDS: tuple[str, ...] = (
    "마무리", "정리", "마지막", "여기까지", "종료", "작별", "퀴즈", "테스트", "채점", "통화종료", "[시스템]",
)

#: 파일별 토큰 예산(자수 × CHARS_TO_TOKENS 근사). 기본판 + 여유. 넘으면 폴백 — 한 줄 늘리면 토큰이 는다는 걸 여기서 잡는다.
TOKEN_BUDGET: dict[str, int] = {
    "normal": 3400,
    "expression": 900,
    "freetalk": 1900,
    "leveltest": 450,
}
#: 한국어 산문의 gemini-2.5-flash 토큰/자 근사(2026-09-12 실측: 프리토킹 차시판 2,911자 → 1,703 토큰 ≈ 0.585, 표현학습 3,890자 → 2,782 ≈ 0.715).
CHARS_TO_TOKENS = 0.6

_SLOT_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HEADER_RE = re.compile(r"^## (\S+)\s*$")


class EditablePromptError(ValueError):
    """편집 파일이 검사를 통과하지 못했다(메시지에 이유). 기본판 파일에서 나면 배포 오류다."""


def parse(text: str) -> dict[str, str]:
    """`## 이름` 섹션 → 본문. HTML 주석 제거 · 본문 앞뒤 빈 줄만 제거(줄 안 공백 보존)."""
    body = _COMMENT_RE.sub("", text.replace("\r\n", "\n"))
    sections: dict[str, str] = {}
    key: str | None = None
    buf: list[str] = []
    for line in body.split("\n"):
        m = _HEADER_RE.match(line)
        if m:
            if key is not None:
                sections[key] = "\n".join(buf).strip("\n")
            key, buf = m.group(1), []
            if key in sections:
                raise EditablePromptError(f"섹션 중복: {key}")
            continue
        if key is not None:
            buf.append(line)
    if key is not None:
        sections[key] = "\n".join(buf).strip("\n")
    return sections


def _slots(s: str) -> set[str]:
    return set(_SLOT_RE.findall(s))


def _bracket_labels(sections: dict[str, str]) -> set[str]:
    out: set[str] = set()
    for body in sections.values():
        for line in body.split("\n"):
            if line.startswith("["):
                out.add(line.split("]", 1)[0] + "]")
    return out


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) * CHARS_TO_TOKENS)


def validate(name: str, sections: dict[str, str], default: dict[str, str]) -> list[str]:
    """기본판 대비 검사 — 위반 목록(빈 리스트 = 통과)."""
    problems: list[str] = []
    if set(sections) != set(default):
        missing = sorted(set(default) - set(sections)); extra = sorted(set(sections) - set(default))
        problems.append(f"섹션 집합이 다르다 — 빠짐 {missing} · 추가 {extra}")
    for k in set(sections) & set(default):
        s, d = _slots(sections[k]), _slots(default[k])
        if s != d:
            problems.append(f"[{k}] 슬롯 — 누락 {sorted(d - s)} · 미지 {sorted(s - d)}")
        if sections[k].count("{") != sections[k].count("}") or sections[k].count("{") != len(_SLOT_RE.findall(sections[k])):
            problems.append(f"[{k}] 중괄호가 슬롯 밖에 있다(.format 충돌)")
    joined = "\n".join(sections.values()); joined_d = "\n".join(default.values())
    for w in BANNED_WORDS:
        if joined.count(w) > joined_d.count(w):
            problems.append(f"금지어 «{w}» 가 기본판({joined_d.count(w)})보다 많다({joined.count(w)})")
    new_labels = _bracket_labels(sections) - _bracket_labels(default)
    if new_labels:
        problems.append(f"새 대괄호 라벨 줄: {sorted(new_labels)}")
    budget = TOKEN_BUDGET.get(name)
    if budget is not None and estimate_tokens(joined) > budget:
        problems.append(f"토큰 예산 초과 — 근사 {estimate_tokens(joined)} > {budget}")
    return problems


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


@lru_cache(maxsize=None)
def load(name: str) -> dict[str, str]:
    """`editable/<name>.md` 를 검사해 섹션 dict 로. 실패·부재면 ERROR 로그 + `<name>.default.md`."""
    default_path = os.path.join(EDITABLE_DIR, f"{name}.default.md")
    default = parse(_read(default_path))
    if not default:
        raise EditablePromptError(f"기본판이 비었다: {default_path}")
    path = os.path.join(EDITABLE_DIR, f"{name}.md")
    if not os.path.exists(path):
        logger.error("편집 프롬프트 %s 없음 — 기본판으로 폴백", path)
        return default
    try:
        sections = parse(_read(path))
    except Exception as exc:  # noqa: BLE001 — 파싱 실패도 폴백 사유
        logger.error("편집 프롬프트 %s 파싱 실패(%s) — 기본판으로 폴백", path, exc)
        return default
    problems = validate(name, sections, default)
    if problems:
        for p in problems:
            logger.error("편집 프롬프트 %s.md 거절: %s", name, p)
        logger.error("편집 프롬프트 %s.md → 기본판(%s.default.md)으로 폴백. 고친 뒤 pytest tests/test_editable_prompts.py", name, name)
        return default
    return sections


def section(name: str, key: str) -> str:
    """섹션 본문 하나. 없는 키는 배포 오류(KeyError) — 코드가 아는 키만 부른다."""
    return load(name)[key]


def reset_cache() -> None:
    """시험용 — 캐시 비우기."""
    load.cache_clear()
