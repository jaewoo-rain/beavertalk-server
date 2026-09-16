"""레벨1 생존회화 청크 46 → 차시 3개 배치 — **단일 원본**. 로더(scripts)와 dev 재시드(service)가 같이 읽는다.

배경: 2026-09-14 실통화 1606·1549 진단(docs/20260914_1800_프리토킹-L1-소재-오배치-진단.md).
옛 배치는 «시드 순서대로 15/15/16 자르기» 였는데 원본이 **상황 순이 아니라 카테고리 순**이라
차시 라벨과 소재가 어긋났다 — «가게·식당» 차시에 자기소개 소재 12개, «인사» 차시에 작별말 4개.
그래서 비버가 가게 상황에 «저는 ◯◯ 회원 아니에요» 를 지어내고(1606) 5턴 연속 작별했다(1549).

⛔ 두 배치를 **둘 다** 들고 있는 이유: 되돌리기다. `/__dev/cur-reseed-l1 {"layout":"legacy"}` 한 번이면
   옛 매핑으로 복귀한다(코드 롤백·재배포 없이). 새 배치가 이상하면 그 길로 간다.

⚠ 1차 자료는 `assets/level/curriculum_v2/survival_chunks.json`(한국어)이다. 영어 미러
   `curriculum_v2_en` 은 no 4 를 "Good morning" 으로 적는 등 어긋나 있어 근거로 쓰지 않는다.
   아래 CHUNKS 가 그 파일과 같은지는 tests/test_cur_l1_layout.py 가 지킨다(드리프트 감지).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

LANGUAGE = "ko"

# 시드 no(1~46) → 표면형. 순서·문장 모두 assets/level/curriculum_v2/survival_chunks.json 과 같아야 한다.
CHUNKS: dict[int, str] = {
    1: "안녕하세요?",           2: "만나서 반갑습니다",      3: "잘 지냈어요?",
    4: "안녕히 가세요",          5: "안녕히 계세요",          6: "또 봐요",
    7: "좋은 하루 보내세요",     8: "어서 오세요",            9: "감사합니다",
    10: "고마워요",             11: "아니에요",              12: "죄송합니다",
    13: "미안해요",             14: "괜찮아요",              15: "네",
    16: "아니요",               17: "좋아요",                18: "맞아요",
    19: "알겠어요",             20: "몰라요",                21: "진짜요?",
    22: "맛있어요",             23: "저는 ◯◯이에요",        24: "저는 ◯◯ 사람이에요",
    25: "이름이 뭐예요?",        26: "처음 뵙겠습니다",        27: "잘 부탁드립니다",
    28: "이거 주세요",           29: "얼마예요?",             30: "화장실이 어디예요?",
    31: "도와주세요",           32: "여기요!",               33: "잠시만요",
    34: "배고파요",             35: "물 주세요",             36: "다시 말해 주세요",
    37: "천천히 말해 주세요",     38: "잘 못 들었어요",         39: "뭐라고요?",
    40: "◯◯이/가 뭐예요?",      41: "한국어로 어떻게 말해요?",  42: "이해 못 했어요",
    43: "하나·둘·셋·넷·다섯",     44: "여섯·일곱·여덟·아홉·열",  45: "일·이·삼·사·오·육·칠·팔·구·십",
    46: "한 개 주세요·두 개 주세요",
}
TOTAL = 46


@dataclass(frozen=True)
class L1Lesson:
    """차시 하나. `nos` 는 시드 no 목록(= 소재 순서이자 cur_lesson_item.seq 순서)."""

    no: int
    code: str
    situation: str
    partner: str | None
    nos: tuple[int, ...]

    @property
    def surfaces(self) -> tuple[str, ...]:
        return tuple(CHUNKS[n] for n in self.nos)


# ── 새 배치(2026-09-16) — 한국어 원본의 situation 기준으로 상황별 재묶기 ────────────────
#   S01 자기소개 5종이 들어오고 작별말은 0. S02 는 가게 소재 + 숫자. S03 에 전략 7 + 작별말 4.
#   작별말을 마지막 차시로 몬 이유: 통화 종료는 서버 종료 시드가 주관하는데(«[시스템]» 전엔 작별 금지)
#   소재로 작별말을 쥐여주면 비버가 2분 만에 작별을 시작한다(1549·1606 t17).
NEW: tuple[L1Lesson, ...] = (
    L1Lesson(
        no=1, code="L1-S01-1", situation="처음 만난 사람과 인사하고 자기를 소개하기",
        partner="한국어 교실에서 처음 만난 한국인",
        nos=(1, 2, 3, 26, 27, 23, 24, 25, 9, 10, 11, 15, 17, 18, 21),
    ),
    L1Lesson(
        no=2, code="L1-S02-1", situation="가게·식당에서 주문하고 부탁하기",
        partner="가게 점원(식당이면 종업원)",
        nos=(8, 32, 28, 29, 35, 46, 34, 22, 30, 33, 43, 44, 45, 16, 19),
    ),
    L1Lesson(
        no=3, code="L1-S03-1", situation="못 알아들었을 때 되묻고 도움 청하기",
        partner="한국어로 말을 걸어 온 한국인 친구",
        nos=(36, 37, 38, 39, 40, 41, 42, 20, 31, 12, 13, 14, 4, 5, 6, 7),
    ),
)

# ── 옛 배치 — 되돌리기 전용. 시드 순서대로 15/15/16(옛 load_cur_seed.CHUNK_LESSONS) ─────────
LEGACY: tuple[L1Lesson, ...] = (
    L1Lesson(no=1, code="L1-S01-1", situation="처음 만난 사람과 인사하기", partner=None,
             nos=tuple(range(1, 16))),
    L1Lesson(no=2, code="L1-S02-1", situation="가게·식당에서 부탁하기", partner=None,
             nos=tuple(range(16, 31))),
    L1Lesson(no=3, code="L1-S03-1", situation="못 알아들었을 때 되묻기", partner=None,
             nos=tuple(range(31, 47))),
)

LAYOUTS: dict[str, tuple[L1Lesson, ...]] = {"new": NEW, "legacy": LEGACY}
DEFAULT_LAYOUT = "new"


def layout(name: str | None = None) -> tuple[L1Lesson, ...]:
    """이름으로 배치를 고른다. 모르는 이름이면 ValueError(dev 도구가 400 으로 돌려준다)."""
    key = (name or DEFAULT_LAYOUT).strip().lower()
    if key not in LAYOUTS:
        raise ValueError(f"layout 은 {sorted(LAYOUTS)} 중 하나여야 한다: {name!r}")
    return LAYOUTS[key]


def validate(lessons: Sequence[L1Lesson]) -> None:
    """46 을 정확히 한 번씩 쓰는가. 깨진 배치로 DB 를 건드리지 않게 하는 마지막 문."""
    seen: list[int] = [n for l in lessons for n in l.nos]
    if sorted(seen) != list(range(1, TOTAL + 1)):
        dup = sorted({n for n in seen if seen.count(n) > 1})
        missing = sorted(set(range(1, TOTAL + 1)) - set(seen))
        raise ValueError(f"청크 배치가 46 전건이 아니다 — 중복 {dup} · 누락 {missing} · 합계 {len(seen)}")


def resolve(
    lessons: Sequence[L1Lesson], rows: Iterable[tuple[int, str]]
) -> tuple[dict[str, list[int]], list[str]]:
    """배치 + DB 청크 행 → {차시코드: [item_id…]}. 두 번째 반환값은 경고 목록.

    rows 는 **시드 순서**(옛 learning_item 은 item_id 순, cur_item 은 적재 순)의 (item_id, surface).
    해소는 **표면형 매칭**을 먼저 한다 — 옛 코드는 «정렬하면 시드 순» 이라는 위치 가정에만 기댔는데,
    그 가정을 검증 없이 새 배치에 물려받지 않는다. 매칭이 안 되면 위치(no-1)로 폴백하고 경고를 남긴다
    (dry-run 응답에 그대로 실려 눈으로 본다).
    """
    validate(lessons)
    ordered = list(rows)
    if len(ordered) != TOTAL:
        raise ValueError(f"청크가 {TOTAL} 개가 아니다: {len(ordered)}")

    by_surface: dict[str, int] = {}
    for item_id, surface in ordered:
        by_surface.setdefault((surface or "").strip(), item_id)

    out: dict[str, list[int]] = {}
    warnings: list[str] = []
    for lesson in lessons:
        ids: list[int] = []
        for n in lesson.nos:
            want = CHUNKS[n]
            item_id = by_surface.get(want)
            if item_id is None:
                item_id = ordered[n - 1][0]
                warnings.append(
                    f"{lesson.code}: 표면형 «{want}»(no {n}) 를 DB 에서 못 찾아 위치로 폴백 "
                    f"→ item_id={item_id} «{ordered[n - 1][1]}»"
                )
            ids.append(item_id)
        out[lesson.code] = ids
    return out, warnings
