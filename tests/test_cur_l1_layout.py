"""레벨1 청크 46 차시 배치(domains/learning/cur_l1_layout) — 전건·해소·슬롯 치환.

배경 docs/20260914_1800_프리토킹-L1-소재-오배치-진단.md · 계획 docs/20260916_1620_L1-청크-차시-재배치-플랜.md
여기서 죽으면 **DB 를 건드리기 전에** 죽은 것이다(재시드 경로는 resolve 를 통과해야 쓴다).
"""
from __future__ import annotations

import io
import json
import os

import pytest

from core.prompts.locked.freetalk import fill_slots, lesson_block
from domains.learning import cur_l1_layout as l1

SEED = os.path.join("assets", "level", "curriculum_v2", "survival_chunks.json")


def _seed_items() -> list[dict]:
    return json.load(io.open(SEED, encoding="utf-8"))["items"]


def test_chunks_match_korean_seed_file() -> None:
    """CHUNKS 가 1차 자료와 같은가 — 시드가 바뀌면 여기서 먼저 안다(드리프트 감지)."""
    want = {int(x["no"]): x["ko"] for x in _seed_items()}
    assert l1.CHUNKS == want
    assert len(l1.CHUNKS) == l1.TOTAL == 46


@pytest.mark.parametrize("name", sorted(l1.LAYOUTS))
def test_layout_covers_all_46_exactly_once(name: str) -> None:
    lessons = l1.layout(name)
    l1.validate(lessons)                       # 46 전건·중복 0
    assert [x.no for x in lessons] == [1, 2, 3]
    assert sum(len(x.nos) for x in lessons) == l1.TOTAL


def test_new_and_legacy_are_the_same_46_regrouped() -> None:
    """되돌리기가 성립하려면 두 배치의 **항목 집합**이 같아야 한다(차시만 다르다)."""
    new_set = {n for x in l1.NEW for n in x.nos}
    legacy_set = {n for x in l1.LEGACY for n in x.nos}
    assert new_set == legacy_set == set(range(1, 47))
    assert [x.code for x in l1.NEW] == [x.code for x in l1.LEGACY]


def test_new_layout_fixes_the_two_observed_bugs() -> None:
    """실통화 1549·1606 의 두 무늬 — 차시 1 의 작별말, 차시 2 의 자기소개."""
    by_code = {x.code: x for x in l1.NEW}
    farewells = {4, 5, 6, 7}                   # 안녕히 가세요·계세요·또 봐요·좋은 하루
    intro = {23, 24, 25, 26, 27}               # 자기소개 5종
    shop = {8, 28, 29, 30, 32, 33, 34, 35, 46}  # 가게·식당

    assert not (set(by_code["L1-S01-1"].nos) & farewells), "차시 1 에 작별말이 있으면 조기 작별이 돌아온다"
    assert intro <= set(by_code["L1-S01-1"].nos), "자기소개는 «처음 만난 사람» 차시에 있어야 한다"
    assert not (set(by_code["L1-S02-1"].nos) & intro), "가게 차시에 자기소개가 섞이면 1606 이 재현된다"
    assert len(shop & set(by_code["L1-S02-1"].nos)) >= 8
    assert farewells <= set(by_code["L1-S03-1"].nos)


def test_every_new_lesson_names_its_partner() -> None:
    """partner=None 이면 프롬프트가 «네가 정한다» 로 떨어져 비버가 점원 대신 선배를 고른다(1606 t7)."""
    assert all((x.partner or "").strip() for x in l1.NEW)


def _rows(order: list[int]) -> list[tuple[int, str]]:
    """(item_id, surface) — item_id 는 1000+no 로 두어 위치와 구분되게 한다."""
    return [(1000 + n, l1.CHUNKS[n]) for n in order]


def test_resolve_matches_by_surface_not_position() -> None:
    """DB 순서가 시드 순과 달라도 표면형으로 맞춘다 — 옛 위치 가정을 물려받지 않는다."""
    shuffled = list(range(46, 0, -1))
    assign, warnings = l1.resolve(l1.NEW, _rows(shuffled))
    assert warnings == []
    for lesson in l1.NEW:
        assert assign[lesson.code] == [1000 + n for n in lesson.nos]


def test_resolve_falls_back_to_position_and_warns() -> None:
    rows = _rows(list(range(1, 47)))
    rows[22] = (9999, "저는 ○○입니다")        # no 23 «저는 ◯◯이에요» 를 다른 문장으로
    assign, warnings = l1.resolve(l1.NEW, rows)
    assert len(warnings) == 1 and "no 23" in warnings[0]
    assert 9999 in assign["L1-S01-1"]          # 위치(23번째)로 폴백


def test_resolve_rejects_wrong_count() -> None:
    with pytest.raises(ValueError):
        l1.resolve(l1.NEW, _rows(list(range(1, 46))))


def test_validate_rejects_a_broken_layout() -> None:
    broken = (l1.L1Lesson(no=1, code="X", situation="s", partner="p", nos=(1, 1, 2)),)
    with pytest.raises(ValueError):
        l1.validate(broken)


# ── ◯◯ 슬롯 ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text, want",
    [
        ("저는 ◯◯이에요", "저는 (이름)이에요"),
        ("저는 ◯◯ 사람이에요", "저는 (나라) 사람이에요"),
        ("◯◯이/가 뭐예요?", "(그 단어)이/가 뭐예요?"),
        ("안녕하세요?", "안녕하세요?"),
        ("", ""),
    ],
)
def test_fill_slots(text: str, want: str) -> None:
    assert fill_slots(text) == want


def test_lesson_block_has_no_raw_slot_marker() -> None:
    """실통화 1606 t23 — 비버가 «◯◯» 를 소리 내어 읽었다. 프롬프트에 기호가 나가면 안 된다."""

    class _Brief:
        situation = "가게에서 물건 사기"
        partner = "가게 점원"
        items = [{"obj": "저는 ◯◯이에요", "ex": None, "role": "chunk"},
                 {"obj": "저는 ◯◯ 사람이에요", "ex": None, "role": "chunk"}]
        probes: list[str] = []

    out = lesson_block(_Brief, username="John", header="[H]", partner_line="- 상대: {partner}",
                       partner_fallback="F", material_line="- 소재:", probes_prefix="- P:")
    assert "◯" not in out
    assert "(이름)" in out and "(나라)" in out
    assert "가게 점원" in out
