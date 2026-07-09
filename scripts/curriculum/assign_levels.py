"""어휘 레벨 배분 · core 선정 — 순수 함수 모음 (IO 없음).

scripts/curriculum/parse_xlsx.py 가 호출한다. 입력은 파싱된 dict 리스트이며,
각 함수가 level_no/assign_rule/priority_score/priority_rank/is_core 필드를 채운다.

규칙 (docs/20260709_1231_level-system-master-plan.md §2):
    - assign_rule="vocab_split_v1": TOPIK급 g → 레벨쌍 (2g, 2g+1).
      급 내 우선순위 점수 내림차순 정렬 후 지그재그(홀수 번째→앞 레벨, 짝수 번째→뒤 레벨).
      ※ P0-개정 이후 어휘 배분의 최종 정답은 CEFR 12단계 통합본(cefr_v1, parse_xlsx 의
      apply_cefr_vocab) — 지그재그는 CEFR 매칭 실패분의 폴백 베이스라인으로만 쓰인다.
    - 우선순위 점수 = 0.40*빈도 + 0.35*품사가중 + 0.25*교재등장보너스
        빈도     : legacy assets/level/vocab_12levels.json 순위를 surface+품사로 조인 → percentile(미등재 0.4)
        품사가중 : 동사 1.0 / 형용사 0.9 / 감탄사·대명사 0.8 / 부사 0.6 / 명사 0.5 / 기타 0.3
        교재등장 : 해당 레벨쌍 문법의 예문 + 같은 급 길잡이말(자기 자신 제외)에 surface 등장 시 1.0
    - is_core: 레벨당 core N(초급 100 / 중급 110 / 고급 120), 품사 쿼터
      (동사 30% / 형용사 15% / 명사 40% / 부사 10% / 기타 5%)로 상위 절단.
      접사·단독 의존명사·줄어든말은 core 금지. 쿼터 미달 카테고리는 잔여 점수순 백필.
    - grammar_core_cap_v1: 문법은 레벨(교재)별 단원 순(unit, seq_no) 상위 45개만
      is_core=True(게이트 대상), 초과분은 '선택 문법'(is_core=False).
"""

from __future__ import annotations

import re
from collections import defaultdict

ASSIGN_RULE = "vocab_split_v1"

# 문법 core 상한: 레벨(교재)별 단원 순 상위 45개만 게이트 대상('선택 문법' 분리)
GRAMMAR_CORE_RULE = "grammar_core_cap_v1"
GRAMMAR_CORE_CAP = 45

# 레벨당 core 목표 (2~5 초급 / 6~9 중급 / 10~13 고급)
CORE_TARGET: dict[int, int] = {
    **{lv: 100 for lv in (2, 3, 4, 5)},
    **{lv: 110 for lv in (6, 7, 8, 9)},
    **{lv: 120 for lv in (10, 11, 12, 13)},
}

# 품사 가중 (pos_primary 기준)
POS_WEIGHT: dict[str, float] = {
    "동사": 1.0, "형용사": 0.9, "감탄사": 0.8, "대명사": 0.8, "부사": 0.6, "명사": 0.5,
}
POS_WEIGHT_DEFAULT = 0.3

FREQ_MISSING_PERCENTILE = 0.4  # legacy 미등재 어휘의 빈도점수

# core 품사 쿼터 (카테고리, 비율) — 순서는 잔여석 배분 우선순위(largest remainder 동점 시)
CORE_POS_QUOTA: tuple[tuple[str, float], ...] = (
    ("동사", 0.30), ("형용사", 0.15), ("명사", 0.40), ("부사", 0.10), ("기타", 0.05),
)
_QUOTA_CATEGORIES = ("동사", "형용사", "명사", "부사")  # 그 외 pos_primary → "기타"

# legacy vocab_12levels.json 의 품사 약어 매핑 (정규 11종 → 약어). '보'(보조용언) 등은 조인 대상 아님.
_LEGACY_POS_ABBR: dict[str, str] = {
    "명사": "명", "대명사": "대", "의존명사": "의", "수사": "수", "동사": "동",
    "형용사": "형", "부사": "부", "관형사": "관", "감탄사": "감", "접사": "접",
    "줄어든말": "줄",
}

_TRAILING_DIGITS = re.compile(r"\d+$")
_UNIT_NUM_RE = re.compile(r"\d+")


def grade_level_pair(grade: int) -> tuple[int, int]:
    """TOPIK급 g → 레벨쌍 (앞 레벨 2g, 뒤 레벨 2g+1)."""
    return 2 * grade, 2 * grade + 1


def level_to_grade(level_no: int) -> int:
    """레벨 → 소속 TOPIK급 (2,3→1급 … 12,13→6급)."""
    return level_no // 2


# ---------------------------------------------------------------- 빈도점수

def build_freq_percentile(legacy: dict) -> dict[tuple[str, str], float]:
    """legacy vocab_12levels.json → (surface, 품사약어) → percentile(1.0=최빈, 0.0=최저빈).

    단어의 접미 번호('것01'→'것')를 제거해 surface 로 맞추고, 중복 키는 최빈(최소 순위)만 남긴다.
    """
    best_rank: dict[tuple[str, str], int] = {}
    for sheet in legacy.values():
        for rec in sheet.get("records", []):
            word, pos, rank = rec.get("단어"), rec.get("품사"), rec.get("순위")
            if not word or not pos or not isinstance(rank, int):
                continue
            key = (_TRAILING_DIGITS.sub("", str(word).strip()), str(pos).strip())
            if key not in best_rank or rank < best_rank[key]:
                best_rank[key] = rank
    ordered = sorted(best_rank, key=lambda k: best_rank[k])
    n = len(ordered)
    if n <= 1:
        return {k: 1.0 for k in ordered}
    return {key: 1.0 - idx / (n - 1) for idx, key in enumerate(ordered)}


def freq_score(item: dict, freq_pct: dict[tuple[str, str], float]) -> float:
    """빈도점수: surface(접미 제거된 형태) + pos_primary 약어로 legacy 순위 조인."""
    abbr = _LEGACY_POS_ABBR.get(item["pos_primary"])
    if abbr is None:
        return FREQ_MISSING_PERCENTILE
    return freq_pct.get((item["surface"], abbr), FREQ_MISSING_PERCENTILE)


# ---------------------------------------------------------------- 교재등장보너스

def build_grade_corpora(
    grammar_items: list[dict], vocab_items: list[dict],
) -> tuple[dict[int, str], dict[int, str]]:
    """급별 코퍼스 2종: (레벨쌍 문법 예문 텍스트, 같은 급 길잡이말 텍스트)."""
    grammar_texts: dict[int, list[str]] = defaultdict(list)
    for gi in grammar_items:
        grammar_texts[level_to_grade(gi["level_no"])].extend(gi["examples"])
    guide_texts: dict[int, list[str]] = defaultdict(list)
    for vi in vocab_items:
        if vi.get("guide_phrase"):
            guide_texts[vi["topik_grade"]].append(vi["guide_phrase"])
    return (
        {g: "\n".join(t) for g, t in grammar_texts.items()},
        {g: "\n".join(t) for g, t in guide_texts.items()},
    )


def textbook_bonus(item: dict, grammar_corpus: str, guide_corpus: str) -> float:
    """교재등장보너스: 레벨쌍 문법 예문 or 같은 급 길잡이말(자기 길잡이말 제외)에 surface 등장 시 1.0."""
    surface = item["surface"]
    if surface in grammar_corpus:
        return 1.0
    own = (item.get("guide_phrase") or "").count(surface)
    return 1.0 if guide_corpus.count(surface) > own else 0.0


# ---------------------------------------------------------------- 점수·배분·core

def compute_priority_scores(
    vocab_items: list[dict],
    grammar_items: list[dict],
    freq_pct: dict[tuple[str, str], float],
) -> None:
    """우선순위 점수(0.40 빈도 + 0.35 품사 + 0.25 교재등장)를 각 항목에 기록."""
    grammar_corpora, guide_corpora = build_grade_corpora(grammar_items, vocab_items)
    for item in vocab_items:
        f = freq_score(item, freq_pct)
        p = POS_WEIGHT.get(item["pos_primary"], POS_WEIGHT_DEFAULT)
        b = textbook_bonus(
            item,
            grammar_corpora.get(item["topik_grade"], ""),
            guide_corpora.get(item["topik_grade"], ""),
        )
        item["priority_score"] = round(0.40 * f + 0.35 * p + 0.25 * b, 6)


def assign_vocab_levels(vocab_items: list[dict]) -> None:
    """급 내 점수 내림차순 → 지그재그 배분(1,3,5…번째→앞 레벨 / 2,4,6…번째→뒤 레벨)."""
    by_grade: dict[int, list[dict]] = defaultdict(list)
    for item in vocab_items:
        by_grade[item["topik_grade"]].append(item)
    for grade, items in by_grade.items():
        front, back = grade_level_pair(grade)
        items.sort(key=lambda it: (-it["priority_score"], it["seq_no"]))
        for position, item in enumerate(items, start=1):
            item["level_no"] = front if position % 2 == 1 else back
            item["assign_rule"] = ASSIGN_RULE


def _quota_counts(total: int) -> dict[str, int]:
    """core N 을 품사 쿼터 비율로 정수 배분 (largest remainder, 동점은 쿼터 선언 순)."""
    floors = {cat: int(total * ratio) for cat, ratio in CORE_POS_QUOTA}
    remainders = {cat: total * ratio - floors[cat] for cat, ratio in CORE_POS_QUOTA}
    leftover = total - sum(floors.values())
    for cat, _ in sorted(
        CORE_POS_QUOTA, key=lambda cr: (-remainders[cr[0]], [c for c, _ in CORE_POS_QUOTA].index(cr[0])),
    ):
        if leftover <= 0:
            break
        floors[cat] += 1
        leftover -= 1
    return floors


def core_eligible(item: dict) -> bool:
    """core 금지 규칙: 접사·줄어든말 포함, 단독 의존명사는 제외."""
    pos_list = item["pos_list"]
    if "접사" in pos_list or "줄어든말" in pos_list:
        return False
    if pos_list == ["의존명사"]:
        return False
    return True


def _quota_category(item: dict) -> str:
    return item["pos_primary"] if item["pos_primary"] in _QUOTA_CATEGORIES else "기타"


def rank_and_select_core(vocab_items: list[dict]) -> dict[int, int]:
    """레벨 내 priority_rank 부여 + 품사 쿼터로 core 선정. 반환: 레벨→core 개수(검증용)."""
    by_level: dict[int, list[dict]] = defaultdict(list)
    for item in vocab_items:
        by_level[item["level_no"]].append(item)

    core_counts: dict[int, int] = {}
    for level_no, items in by_level.items():
        items.sort(key=lambda it: (-it["priority_score"], it["seq_no"]))
        for rank, item in enumerate(items, start=1):
            item["priority_rank"] = rank
            item["is_core"] = False

        target = CORE_TARGET[level_no]
        quotas = _quota_counts(target)
        eligible_by_cat: dict[str, list[dict]] = defaultdict(list)
        for item in items:  # 이미 점수순
            if core_eligible(item):
                eligible_by_cat[_quota_category(item)].append(item)

        selected: list[dict] = []
        for cat, _ in CORE_POS_QUOTA:
            selected.extend(eligible_by_cat[cat][: quotas[cat]])
        # 쿼터 미달 카테고리가 있으면 잔여 eligible 에서 점수순 백필
        if len(selected) < target:
            chosen = {id(it) for it in selected}
            rest = [it for it in items if core_eligible(it) and id(it) not in chosen]
            selected.extend(rest[: target - len(selected)])

        for item in selected:
            item["is_core"] = True
        core_counts[level_no] = len(selected)
    return core_counts


# ---------------------------------------------------------------- 문법 core 상한

def grammar_unit_sort_key(item: dict) -> tuple[int, int]:
    """문법 단원 순 정렬 키: (unit 숫자, seq_no). unit 결측·비숫자('01과'/'1과' 혼재)는 말미."""
    m = _UNIT_NUM_RE.search(str(item.get("unit") or ""))
    return (int(m.group()) if m else 10**9, item["seq_no"])


def cap_grammar_core(grammar_items: list[dict]) -> dict[int, int]:
    """grammar_core_cap_v1: 레벨(교재)별 단원 순 상위 45개만 is_core=True.

    초과분('선택 문법')은 is_core=False — 게이트 미산입, 데이터는 보존.
    assign_rule 에 '+grammar_core_cap_v1' 을 덧붙여 버전을 기록한다(멱등).
    반환: 레벨→core 개수(검증용). 45개 이하 레벨은 전건 core.
    """
    by_level: dict[int, list[dict]] = defaultdict(list)
    for item in grammar_items:
        by_level[item["level_no"]].append(item)

    core_counts: dict[int, int] = {}
    for level_no, items in by_level.items():
        items.sort(key=grammar_unit_sort_key)
        for pos, item in enumerate(items):
            item["is_core"] = pos < GRAMMAR_CORE_CAP
            if GRAMMAR_CORE_RULE not in item["assign_rule"]:
                item["assign_rule"] = f"{item['assign_rule']}+{GRAMMAR_CORE_RULE}"
        core_counts[level_no] = sum(1 for it in items if it["is_core"])
    return core_counts
