"""cur_* 2단계 B1 — 선별·저장·완료·해금·멱등 (sqlite 인메모리, **실제 시드 7MB** 그대로, test_cur_seed_load.py 관례).

계획 docs/plans/2026-09-12-cur-2단계-통화경로-이전.md §4 수용 기준을 하나씩 잠근다. 시드는 모듈에 한 번 적재하고
회원·통화는 시험마다 새로 만든다(회원 행이 시험 간 격리 단위).
"""
from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone

import pytest
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.curriculum import CurCall, CurItem, CurLesson, CurLessonItem, CurMemberItem, CurMemberLesson
from domains.learning.models.learning_item import LearningItem
from domains.learning.repository import curriculum_repository as repo
from domains.learning.service import curriculum_service as cur
from scripts.curriculum.load_cur_seed import load

SEED = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed.json")
pytestmark = pytest.mark.skipif(not os.path.exists(SEED), reason="cur_seed.json 없음")


@pytest.fixture(scope="module")
def factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sf() as s:
        for i in range(46):
            s.add(LearningItem(
                language="ko", kind="chunk", source_key=f"c:{i}", band=1, level_no=1, assign_rule="seed",
                surface=f"청크 문장 {i}", meanings=json.dumps({"en": f"chunk {i}"}), examples="[]",
            ))
        s.commit()
        load(s, json.load(io.open(SEED, encoding="utf-8")), dry_run=False)
        s.commit()
        v = Voice(name="Fenrir", gender="male")
        s.add(v); s.flush()
        s.add(Character(name="비비", role="선생님", personality="다정함", voice_id=v.voice_id, price=0))
        s.commit()
    return sf


@pytest.fixture()
def db(factory):
    s = factory()
    yield s
    s.rollback()
    s.close()


_counter = {"n": 0}


def _member(db: Session) -> int:
    _counter["n"] += 1
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id=f"auth-cur-{_counter['n']}")
    db.add(m); db.commit()
    return m.member_id


def _call(db: Session, member_id: int, call_type: str = "expression") -> int:
    ch = db.execute(text("SELECT character_id FROM character LIMIT 1")).scalar()
    c = Call(member_id=member_id, character_id=ch, call_type=call_type, call_date=datetime.now(timezone.utc))
    db.add(c); db.commit()
    return c.call_id


def _lesson(db: Session, code: str) -> CurLesson:
    lid = db.execute(text("SELECT lesson_id FROM cur_lesson WHERE code=:c"), {"c": code}).scalar()
    assert lid is not None, code
    return db.get(CurLesson, lid)


def _set_progress(db: Session, member_id: int, code: str) -> None:
    cur.ensure_progress(db, member_id)
    prog = repo.current_progress(db, member_id)
    prog.lesson_id = _lesson(db, code).lesson_id
    db.commit()


def _drill_all(db: Session, member_id: int, items: list[dict], call_id: int, *, passed=(), failed=()) -> dict:
    return cur.record_expression(db, call_id, items, drilled_ids=[d["item_id"] for d in items],
                                 passed_ids=list(passed), failed_ids=list(failed))


# --------------------------------------------------------------------------- #
# 선별
# --------------------------------------------------------------------------- #
def test_a_new_member_starts_at_lesson_1_with_15_chunks_and_no_review(db):
    m = _member(db)
    c = _call(db, m)
    opened = cur.open_call(db, m, c, "expression")
    assert opened.lesson.no == 1 and opened.lesson.code == "L1-S01-1" and opened.resumed is False
    assert len(opened.items) == 15 and all(d["review"] is False for d in opened.items)
    assert all(d["role"] == "chunk" and d["lesson_id"] == opened.lesson.lesson_id for d in opened.items)
    cc = repo.cur_call(db, c)
    assert cc is not None and cc.course == "expression" and cc.lesson_id == opened.lesson.lesson_id and cc.recorded_at is None
    assert cur.decide_course(db, m) == "expression"


def test_a1_t01_1_first_call_is_18_in_role_order_second_is_12_plus_6_review(db):
    m = _member(db)
    _set_progress(db, m, "A1-T01-1")
    c1 = _call(db, m)
    o1 = cur.open_call(db, m, c1, "expression")
    assert len(o1.items) == 18
    roles = [d["role"] for d in o1.items]
    assert roles == ["grammar"] * 3 + ["must"] * 8 + ["core"] * 7, roles
    assert all(not d["review"] for d in o1.items)
    # 모두 드릴, 두 개 틀림(미통과)
    wrong = [o1.items[0]["item_id"], o1.items[5]["item_id"]]
    res = _drill_all(db, m, o1.items, c1, failed=wrong)
    assert res["lesson_completed"] is False and res["status"] == "learning"
    c2 = _call(db, m)
    o2 = cur.open_call(db, m, c2, "expression")
    fresh = [d for d in o2.items if not d["review"]]
    review = [d for d in o2.items if d["review"]]
    assert len(fresh) == 12 and len(review) == 6 and len(o2.items) == 18
    assert [d["role"] for d in fresh] == ["core"] * 2 + ["support"] * 10
    assert o2.items[:12] == fresh, "복습은 뒤에 붙는다"
    # 복습 순서 — 틀리고 미통과가 맨 앞(2개), 그다음 seen_count 같은 것들
    assert {d["item_id"] for d in review[:2]} == set(wrong)
    assert all(d["lesson_id"] == o1.lesson.lesson_id for d in review), "복습 항목도 제 차시(lesson_id)를 싣는다"
    assert len({d["item_id"] for d in o2.items}) == 18, "같은 item_id 가 두 번 안 실린다"


def test_one_call_never_mixes_two_lessons(db):
    m = _member(db)
    _set_progress(db, m, "A1-T01-1")
    c = _call(db, m)
    o = cur.open_call(db, m, c, "expression")
    _drill_all(db, m, o.items, c)
    c2 = _call(db, m)
    o2 = cur.open_call(db, m, c2, "expression")
    lids = {d["lesson_id"] for d in o2.items}
    assert lids == {o.lesson.lesson_id}, "다음 차시 항목을 끌어오지 않는다(복습도 이미 배운 것 = 같은 차시뿐)"


def test_examples_rotate_by_seen_count(db):
    m = _member(db)
    _set_progress(db, m, "A1-T01-1")
    c1 = _call(db, m)
    o1 = cur.open_call(db, m, c1, "expression")
    # 예문이 2개 이상인 항목 하나 고른다
    target = next(d for d in o1.items if len(json.loads(db.get(CurItem, d["item_id"]).examples or "[]")) >= 2)
    exs = json.loads(db.get(CurItem, target["item_id"]).examples)
    assert target["ex"] == exs[0], "첫 드릴 = 예문1"
    _drill_all(db, m, o1.items, c1)
    # 남은 12 + 복습 6 — 그 항목이 복습으로 다시 실리도록 다른 항목을 미리 더 본 것처럼 seen_count 를 올린다
    for d in o1.items:
        if d["item_id"] != target["item_id"]:
            row = repo.member_item(db, m, d["lesson_id"], d["item_id"]); row.seen_count = 5
    db.commit()
    c2 = _call(db, m)
    o2 = cur.open_call(db, m, c2, "expression")
    again = next(d for d in o2.items if d["item_id"] == target["item_id"])
    assert again["review"] is True and again["ex"] == exs[1 % len(exs)], "첫 복습 = 예문2"


def test_review_prefers_failed_then_least_seen(db):
    m = _member(db)
    _set_progress(db, m, "A1-T01-1")
    c1 = _call(db, m)
    o1 = cur.open_call(db, m, c1, "expression")
    ids = [d["item_id"] for d in o1.items]
    _drill_all(db, m, o1.items, c1, failed=[ids[3]], passed=[ids[4]])
    # seen_count 를 벌려 둔다: ids[7] 을 많이 본 것으로
    repo.member_item(db, m, o1.lesson.lesson_id, ids[7]).seen_count = 9
    db.commit()
    pool = repo.review_pool(db, m, frozenset(), 18)
    order = [mi.item_id for mi, _ in pool]
    assert order[0] == ids[3], "틀리고 미통과가 1순위"
    assert order.index(ids[7]) == len(order) - 1, "많이 본 것은 맨 뒤(seen_count ASC)"
    assert ids[4] in order, "통과한 것도 복습 풀에는 있다(우선순위만 뒤)"


def test_retired_items_are_never_selected(db):
    m = _member(db)
    _set_progress(db, m, "A1-T01-1")
    lesson = _lesson(db, "A1-T01-1")
    first_item = repo.lesson_items(db, lesson.lesson_id)[0][1]
    first_item.retired_at = datetime.now(timezone.utc); db.commit()
    try:
        o = cur.open_call(db, m, _call(db, m), "expression")
        assert first_item.item_id not in {d["item_id"] for d in o.items}
    finally:
        first_item.retired_at = None; db.commit()


# --------------------------------------------------------------------------- #
# 저장·완료·해금
# --------------------------------------------------------------------------- #
def test_drilling_all_30_completes_the_lesson_and_opens_freetalk(db):
    m = _member(db)
    _set_progress(db, m, "A1-T01-1")
    lesson = _lesson(db, "A1-T01-1")
    # 그 전엔 프리토킹 잠금
    with pytest.raises(cur.CourseLocked):
        cur.open_call(db, m, _call(db, m, "freetalk"), "freetalk")
    c1 = _call(db, m); o1 = cur.open_call(db, m, c1, "expression"); r1 = _drill_all(db, m, o1.items, c1)
    assert r1["lesson_completed"] is False
    c2 = _call(db, m); o2 = cur.open_call(db, m, c2, "expression"); r2 = _drill_all(db, m, o2.items, c2)
    assert r2["lesson_completed"] is True and r2["status"] == "expression_done"
    ml = repo.lesson_status(db, m, lesson.lesson_id)
    assert ml.status == "expression_done" and ml.expression_done_at is not None and ml.expression_calls == 2
    assert repo.cur_call(db, c2).lesson_completed is True
    assert cur.decide_course(db, m) == "freetalk"
    assert cur.me(db, m)["open"] == {"expression": True, "freetalk": True}
    assert cur.me(db, m)["items_drilled"] == 30 and cur.me(db, m)["items_total"] == 30
    # 프리토킹 열림 → 브리프 · 종료 → 다음 차시(no 5)
    c3 = _call(db, m, "freetalk")
    o3 = cur.open_call(db, m, c3, "auto")
    assert o3.course == "freetalk" and o3.items == [] and o3.brief is not None
    assert o3.brief.situation == lesson.situation and len(o3.brief.surfaces) == 30 == len(o3.brief.items)   # 상한 18 폐기(프리토킹 v1)
    assert cur.complete_freetalk(db, c3, duration_s=200, normal_end=True) == {"freetalk_done": True, "moved": True}
    assert repo.current_progress(db, m).lesson_id == _lesson(db, "A1-T02-1").lesson_id
    assert repo.lesson_status(db, m, lesson.lesson_id).status == "freetalk_done"
    assert cur.decide_course(db, m) == "expression"
    # 멱등 — 조각2 가 다시 불러도 포인터는 한 번만 움직인다
    assert cur.complete_freetalk(db, c3, duration_s=200, normal_end=True) == {"freetalk_done": True, "moved": False}
    assert repo.current_progress(db, m).lesson_id == _lesson(db, "A1-T02-1").lesson_id


def test_short_or_abnormal_freetalk_does_not_complete(db):
    m = _member(db)
    _set_progress(db, m, "L1-S01-1")
    c1 = _call(db, m); o1 = cur.open_call(db, m, c1, "expression"); _drill_all(db, m, o1.items, c1)
    assert cur.decide_course(db, m) == "freetalk"
    c2 = _call(db, m, "freetalk"); cur.open_call(db, m, c2, "freetalk")
    assert cur.complete_freetalk(db, c2, duration_s=30, normal_end=True) == {"freetalk_done": False, "moved": False}
    assert cur.complete_freetalk(db, c2, duration_s=300, normal_end=False) == {"freetalk_done": False, "moved": False}
    assert repo.current_progress(db, m).lesson_id == _lesson(db, "L1-S01-1").lesson_id
    assert cur.decide_course(db, m) == "freetalk", "여전히 열려 있다 — 다음 프리토킹이 그 차시의 것"


def test_grammar_shared_by_two_lessons_is_drilled_again_later(db):
    """결정 11 — 「날짜와 요일」 은 A1-T02-1 과 A3-T02-1 둘 다에 속한다. 앞에서 배웠어도 뒤 차시 목록에 다시 실린다."""
    m = _member(db)
    gi = db.execute(text("SELECT item_id FROM cur_item WHERE kind='grammar' AND key='날짜와 요일'")).scalar()
    assert gi is not None
    a1, a3 = _lesson(db, "A1-T02-1"), _lesson(db, "A3-T02-1")
    assert {li.item_id for li, _ in repo.lesson_items(db, a1.lesson_id)} & {gi} and \
           {li.item_id for li, _ in repo.lesson_items(db, a3.lesson_id)} & {gi}
    _set_progress(db, m, "A1-T02-1")
    c1 = _call(db, m); o1 = cur.open_call(db, m, c1, "expression")
    assert gi in {d["item_id"] for d in o1.items}
    _drill_all(db, m, o1.items, c1, passed=[gi])
    assert repo.member_item(db, m, a1.lesson_id, gi).drilled_at is not None
    _set_progress(db, m, "A3-T02-1")
    c2 = _call(db, m); o2 = cur.open_call(db, m, c2, "expression")
    again = next(d for d in o2.items if d["item_id"] == gi)
    assert again["review"] is False and again["lesson_id"] == a3.lesson_id, "새 차시에선 «안 배운 것» 으로 다시 실린다"
    assert repo.member_item(db, m, a3.lesson_id, gi) is None
    _drill_all(db, m, o2.items, c2)
    assert repo.member_item(db, m, a3.lesson_id, gi).drilled_at is not None, "판정은 (회원, 차시, 항목) 행 — 차시별로 따로"


def test_review_verdicts_land_on_the_items_own_lesson_row(db):
    """복습으로 실린 다른 차시 항목의 passed/failed 는 그 항목의 차시 행에 적힌다(§6 ①)."""
    m = _member(db)
    _set_progress(db, m, "L1-S01-1")
    c1 = _call(db, m); o1 = cur.open_call(db, m, c1, "expression"); _drill_all(db, m, o1.items, c1)
    l1 = o1.lesson
    _set_progress(db, m, "A1-T01-1")                       # 포인터를 옮겼다(프리토킹은 생략 — 시험용)
    c2 = _call(db, m); o2 = cur.open_call(db, m, c2, "expression")
    assert all(not d["review"] for d in o2.items) and len(o2.items) == 18, "새 항목 18개가 차 있어 복습 0"
    _drill_all(db, m, o2.items, c2)
    c3 = _call(db, m); o3 = cur.open_call(db, m, c3, "expression")
    review = [d for d in o3.items if d["review"]]
    assert len(review) == 6 and all(d["lesson_id"] == l1.lesson_id for d in review), "복습 = 차시1 청크(제 차시 lesson_id)"
    target = review[0]
    cur.record_expression(db, c3, o3.items, drilled_ids=[d["item_id"] for d in o3.items],
                          passed_ids=[target["item_id"]], failed_ids=[review[1]["item_id"]])
    row = repo.member_item(db, m, l1.lesson_id, target["item_id"])
    assert row.quiz_passed_at is not None and row.last_quiz_call_id == c3 and row.seen_count == 2
    assert repo.member_item(db, m, o3.lesson.lesson_id, target["item_id"]) is None, "현재 차시 행에 잘못 적히지 않았다"
    assert repo.member_item(db, m, l1.lesson_id, review[1]["item_id"]).quiz_failed_count == 1


def test_monotonic_drilled_and_passed_and_failed_count_only_when_unpassed(db):
    m = _member(db)
    _set_progress(db, m, "L1-S01-1")
    c1 = _call(db, m); o1 = cur.open_call(db, m, c1, "expression")
    iid = o1.items[0]["item_id"]; lid = o1.lesson.lesson_id
    _drill_all(db, m, o1.items, c1, passed=[iid])
    row = repo.member_item(db, m, lid, iid)
    d_at, p_at, dcall = row.drilled_at, row.quiz_passed_at, row.drilled_call_id
    c2 = _call(db, m); o2 = cur.open_call(db, m, c2, "expression")
    cur.record_expression(db, c2, o2.items, drilled_ids=[iid], passed_ids=[], failed_ids=[iid])
    row = repo.member_item(db, m, lid, iid)
    assert (row.drilled_at, row.quiz_passed_at, row.drilled_call_id) == (d_at, p_at, dcall), "단조 — 되돌리지 않는다"
    assert row.quiz_failed_count == 0, "이미 통과한 항목의 오답은 세지 않는다"
    assert row.seen_count == 2 and row.last_quiz_call_id == c2


def test_recording_twice_is_a_noop(db):
    m = _member(db)
    _set_progress(db, m, "L1-S01-1")
    c1 = _call(db, m); o1 = cur.open_call(db, m, c1, "expression")
    r = _drill_all(db, m, o1.items, c1, failed=[o1.items[0]["item_id"]])
    assert r is not None
    ml = repo.lesson_status(db, m, o1.lesson.lesson_id)
    assert ml.expression_calls == 1
    assert cur.record_expression(db, c1, o1.items, [d["item_id"] for d in o1.items], [], [o1.items[0]["item_id"]]) is None
    row = repo.member_item(db, m, o1.lesson.lesson_id, o1.items[0]["item_id"])
    assert row.seen_count == 1 and row.quiz_failed_count == 1 and ml.expression_calls == 1, "카운터는 한 번만"
    assert repo.cur_call(db, c1).recorded_at is not None


def test_fragment_resume_reuses_the_cur_call_and_reselects(db):
    """§7 P0 — 조각2(같은 call_id 재접속): cur_call INSERT 0 · 그 차시로 재선별 · 잠금 검사 면제."""
    m = _member(db)
    _set_progress(db, m, "A1-T01-1")
    c1 = _call(db, m); o1 = cur.open_call(db, m, c1, "expression")
    n_before = db.execute(text("SELECT COUNT(*) FROM cur_call")).scalar()
    # 조각1 저장(전부 드릴) → 조각2 같은 call_id 로 다시 시작
    _drill_all(db, m, o1.items, c1)
    o2 = cur.open_call(db, m, c1, "expression")
    assert o2.resumed is True and db.execute(text("SELECT COUNT(*) FROM cur_call")).scalar() == n_before
    assert o2.lesson.lesson_id == o1.lesson.lesson_id
    assert len([d for d in o2.items if not d["review"]]) == 12, "남은 항목부터 이어 간다"
    # 프리토킹 조각2: 포인터가 다음 차시로 넘어갔어도 그 통화의 cur_call 차시로 열린다(COURSE_LOCKED 아님)
    c2 = _call(db, m); o3 = cur.open_call(db, m, c2, "expression"); _drill_all(db, m, o3.items, c2)
    c3 = _call(db, m, "freetalk"); cur.open_call(db, m, c3, "freetalk")
    cur.complete_freetalk(db, c3, 120, True)                       # 포인터 → A1-T02-1
    o4 = cur.open_call(db, m, c3, "freetalk")                      # 조각2
    assert o4.resumed and o4.course == "freetalk" and o4.lesson.code == "A1-T01-1"


def test_cur_call_items_merge_across_fragments_and_keep_passes(db):
    m = _member(db)
    _set_progress(db, m, "L1-S01-1")
    c1 = _call(db, m); o1 = cur.open_call(db, m, c1, "expression")
    ids = [d["item_id"] for d in o1.items]
    _drill_all(db, m, o1.items[:5], c1, passed=[ids[0]], failed=[ids[1]])
    # 조각2 는 같은 call — recorded_at 이 이미 있으니 record 는 no-op 이어야 하지만, 병합 규칙 자체는 순수 함수로 잠근다
    merged = cur.merge_call_items(repo.cur_call(db, c1).items, [
        {"item_id": ids[0], "surface": "새 표면형", "passed": False, "failed": True},   # 강등 시도
        {"item_id": ids[1], "surface": "x", "passed": True},                           # 조각2 통과
        {"item_id": ids[9], "surface": "y", "passed": False, "failed": False, "drilled": True},
    ])
    by = {r["item_id"]: r for r in merged}
    assert by[ids[0]]["passed"] is True and by[ids[0]]["failed"] is False and by[ids[0]]["surface"] == "새 표면형"
    assert by[ids[1]]["passed"] is True and by[ids[1]]["failed"] is False
    assert by[ids[9]]["drilled"] is True and len(by) == 6
    rows = json.loads(repo.cur_call(db, c1).items)
    assert {r["item_id"] for r in rows} == set(ids[:5])
    assert set(rows[0].keys()) >= {"item_id", "surface", "meaning", "passed", "failed", "role", "drilled"}


def test_me_lessons_and_reset(db):
    m = _member(db)
    me = cur.me(db, m)
    assert me["lesson"]["no"] == 1 and me["status"] == "learning" and me["items_total"] == 15 and me["items_drilled"] == 0
    assert me["open"] == {"expression": True, "freetalk": False}
    ls = cur.lessons(db, m, level=1)
    assert [l["no"] for l in ls] == [1, 2, 3] and ls[0]["status"] == "learning" and ls[1]["status"] is None
    c1 = _call(db, m); o1 = cur.open_call(db, m, c1, "expression"); _drill_all(db, m, o1.items, c1)
    assert cur.me(db, m)["status"] == "expression_done"
    out = cur.reset(db, m, lesson_no=4)
    assert out["lesson_code"] == "A1-T01-1" and out["deleted_calls"] == 1
    assert repo.cur_call(db, c1) is None and repo.member_item_map(db, m, o1.lesson.lesson_id) == {}
    assert cur.me(db, m)["lesson"]["code"] == "A1-T01-1" and cur.me(db, m)["status"] == "learning"


def test_record_without_cur_call_or_wrong_course_is_ignored(db):
    m = _member(db)
    c = _call(db, m)                                       # cur_call 없음(옛 경로 통화)
    assert cur.record_expression(db, c, [], [], [], []) is None
    assert cur.complete_freetalk(db, c, 300, True) is None


def test_cur_files_never_mention_the_old_learning_tables():
    """계획 §4 «옛 테이블 미참조» — cur 경로 파일에 옛 이름이 0건."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("domains/learning/repository/curriculum_repository.py", "domains/learning/service/curriculum_service.py",
                "alembic/versions/b2d3e4f5a6c7_cur__seen_count_recorded_at.py"):
        src = (root / rel).read_text(encoding="utf-8")
        for banned in ("learning_item", "member_item_progress", "korean_level", "expression_result", "item_evidence"):
            assert banned not in src, f"{rel}: {banned} — 주석·독스트링까지 0건(계획 §4, grep 검수)"


# --------------------------------------------------------------------------- #
# 조각 단위 멱등(c3d4e5f6a7b8, 실통화 1550) — 조각 2 저장됨 · 같은 조각 재저장/재분석 no-op · 재개 목록은 통과 제외·오답 앞줄
# --------------------------------------------------------------------------- #
def _set_fragment(db: Session, call_id: int, n: int) -> None:
    c = db.get(Call, call_id)
    c.fragment_count = n
    db.commit()


def test_fragment_2_and_3_are_recorded_and_the_same_fragment_is_a_noop(db):
    m = _member(db)
    c = _call(db, m)
    o1 = cur.open_call(db, m, c, "expression")
    ids = [d["item_id"] for d in o1.items]
    # 조각 1: 3개 드릴, 2개 통과, 1개 오답
    r1 = cur.record_expression(db, c, o1.items, drilled_ids=ids[:3], passed_ids=ids[:2], failed_ids=[ids[2]])
    assert r1 is not None and r1["fragment"] == 1
    cc = repo.cur_call(db, c)
    first_recorded_at = cc.recorded_at
    assert cc.recorded_fragment == 1 and first_recorded_at is not None
    # 같은 조각의 중복 종료 / 재분석 → no-op
    assert cur.record_expression(db, c, o1.items, drilled_ids=ids[:3], passed_ids=ids[:2], failed_ids=[]) is None
    assert cur.record_expression(db, c, o1.items, drilled_ids=ids[:3], passed_ids=ids[:2], failed_ids=[], fragment_no=1) is None
    assert repo.lesson_status(db, m, o1.lesson.lesson_id).expression_calls == 1

    # 조각 2 재개(resume_call 이 fragment_count 를 2 로) — 목록: 통과 2개 제외, 오답 1개는 이미 드릴돼 fresh 밖(복습 풀에서 앞줄)
    _set_fragment(db, c, 2)
    o2 = cur.open_call(db, m, c, "expression")
    assert o2.resumed is True
    ids2 = [d["item_id"] for d in o2.items]
    assert not (set(ids[:2]) & set(ids2)), "조각 1 통과 항목은 조각 2 목록에 없다"
    assert ids2[0] == ids[2] and o2.items[0]["review"] is True, "조각 1 오답은 조각 2 맨 앞(복습 표시) — H6/1568"
    assert all(d["review"] is False for d in o2.items if d["item_id"] in ids[3:]), "남은 새 항목은 그대로"
    # 조각 2 판정 저장 — recorded_fragment 2 · 통과 2개 추가 · recorded_at 은 처음 시각 유지 · expression_calls 2
    r2 = cur.record_expression(db, c, o2.items, drilled_ids=ids2[:3], passed_ids=ids2[:2], failed_ids=[])
    assert r2 is not None and r2["fragment"] == 2
    cc = repo.cur_call(db, c)
    assert cc.recorded_fragment == 2 and cc.recorded_at == first_recorded_at
    mine = repo.member_item_map(db, m, o1.lesson.lesson_id)
    assert all(mine[i].quiz_passed_at is not None for i in ids[:2] + ids2[:2]), "조각 1·2 통과가 모두 남는다(1550 결함)"
    assert repo.lesson_status(db, m, o1.lesson.lesson_id).expression_calls == 2
    rows = {r["item_id"]: r for r in json.loads(cc.items)}
    assert all(rows[i]["passed"] for i in ids[:2] + ids2[:2]), "items 병합(P1-5) — 통과 OR"
    assert rows[ids[2]]["passed"] is True and rows[ids[2]]["failed"] is False, "조각1 오답(ids[2])이 조각2 앞줄에서 통과 → passed 가 failed 를 지운다(단조)"
    # 조각 2 재분석/중복 종료 → no-op, 조각 3 은 다시 저장된다
    assert cur.record_expression(db, c, o2.items, drilled_ids=ids2[:3], passed_ids=ids2[:2], failed_ids=[]) is None
    _set_fragment(db, c, 3)
    r3 = cur.record_expression(db, c, o2.items, drilled_ids=[], passed_ids=[ids2[2]], failed_ids=[])
    assert r3 is not None and r3["fragment"] == 3 and repo.cur_call(db, c).recorded_fragment == 3
    assert repo.member_item(db, m, o1.lesson.lesson_id, ids2[2]).quiz_passed_at is not None


def test_select_items_exclude_and_first_shape_the_resume_list(db):
    m = _member(db)
    lesson = _lesson(db, "L1-S01-1")
    base = cur.select_items(db, m, lesson.lesson_id)
    ids = [d["item_id"] for d in base]
    # 통과 제외(새 회원은 복습 풀이 비어 그만큼 줄어든다 — 있으면 복습 채움 규칙으로 채워진다), 오답은 앞줄
    out = cur.select_items(db, m, lesson.lesson_id, exclude=ids[:2], first=[ids[4], ids[3]])
    got = [d["item_id"] for d in out]
    assert not (set(ids[:2]) & set(got)) and len(out) == len(base) - 2
    assert got[:2] == [ids[4], ids[3]], "오답은 준 순서대로 앞줄"
    assert got[2:] == [i for i in ids if i not in ids[:2] + [ids[4], ids[3]]], "나머지는 seq 순"
    # 인자 없으면 종전과 같다
    assert [d["item_id"] for d in cur.select_items(db, m, lesson.lesson_id)] == ids


def test_resume_first_puts_drilled_wrong_items_at_the_front_as_review(db):
    """하네스 H6 / 실측 1568 — 조각1 오답은 드릴됐으니(drilled_at) 옛 코드에선 fresh 에서 빠져 «오답 앞줄» 이 죽은 코드였다.
    지금은 first 항목을 드릴 여부와 무관하게 앞줄에 싣고 review=True 로 표시한다. 순서 = first → fresh(seq) → 복습 채움."""
    m = _member(db)
    c = _call(db, m)
    o1 = cur.open_call(db, m, c, "expression")
    ids = [d["item_id"] for d in o1.items]
    # 조각 1: 5개 드릴 — 2개 통과, 3개 오답
    cur.record_expression(db, c, o1.items, drilled_ids=ids[:5], passed_ids=ids[:2], failed_ids=ids[2:5])
    _set_fragment(db, c, 2)
    o2 = cur.open_call(db, m, c, "expression")
    got = [d["item_id"] for d in o2.items]
    assert got[:3] == ids[2:5], "조각1 오답 3개가 조각2 목록 맨 앞(조각1 순서)"
    assert all(d["review"] is True for d in o2.items[:3]), "드릴됐던 항목이라 복습 취급"
    assert all(d["seen_count"] == 1 if "seen_count" in d else True for d in o2.items[:3])
    assert not (set(ids[:2]) & set(got)), "통과 2개는 없다"
    assert got[3:] == ids[5:], "나머지는 seq 순 그대로(fresh)"
    assert all(d["review"] is False for d in o2.items[3:])
    assert len(o2.items) == 13, "15 - 통과 2 = 13 (오답 3 포함), n=18 상한 안"
    # first 에 이미 통과(exclude)한 것이 섞여도 exclude 가 이긴다 · 새 통화(first 없음)는 종전과 같다
    lesson = o1.lesson
    a = cur.select_items(db, m, lesson.lesson_id, exclude=ids[:1], first=ids[:1])
    b = cur.select_items(db, m, lesson.lesson_id, exclude=ids[:1])
    assert ids[0] not in {d["item_id"] for d in a}
    assert [d["item_id"] for d in a if not d["review"]] == [d["item_id"] for d in b if not d["review"]]   # 복습 채움 꼬리는 random() 정렬
    m2 = _member(db)
    fresh_new = cur.select_items(db, m2, lesson.lesson_id)
    assert [d["item_id"] for d in fresh_new] == ids and all(d["review"] is False for d in fresh_new)
