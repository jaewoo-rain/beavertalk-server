"""L3 — 포인터가 레벨 경계를 넘으면 레벨을 올린다. 외부 의존 0, 인메모리 sqlite.

docs/plans/2026-09-23-레벨-커리큘럼-연결.md T2. `complete_freetalk` 의 포인터 전진과
같은 커밋에서, 넘어간 차시의 레벨이 더 높으면 레벨도 올린다(진도가 레벨을 민다).
지금까지 표현학습으로는 레벨이 영원히 안 올랐다 — 이게 그 사슬을 잇는 유일한 자리다.

무엇을 지키나:
  ① no=3→4 면 레벨 1→2
  ② 같은 레벨 안 이동(4→5)이면 레벨 무변경
  ③ ko 면 korean_level 도 갱신(dual-write, upsert_language_level 내부)
  ④ member_level_history 1행(reason='curriculum_advance')
  ⑤ 같은 통화로 두 번 불러도 1행(멱등)
  ⑥ 마지막 차시(nxt 없음)면 아무 일도 없음
  ⑦ 레벨이 작아지는 경우 올리지 않는다(방어 — 시드를 믿지 않는다)
"""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.learning.models.call import Call
from domains.learning.models.curriculum import CurCall, CurLesson, CurMemberProgress
from domains.learning.models.level import Level
from domains.learning.models.member_level_history import MemberLevelHistory
from domains.learning.repository import curriculum_repository as repo
from domains.learning.repository import mastery_repository
from domains.learning.service import curriculum_service as cur


@pytest.fixture()
def db():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()

    s.add(Level(language="ko", level_no=1, profile="생존 회화"))
    s.add(Level(language="ko", level_no=2, profile="초급 A1"))
    s.add(Level(language="ko", level_no=3, profile="초급 A2"))
    s.add(Level(language="ko", level_no=5, profile="중급"))
    s.add_all([
        CurLesson(language="ko", no=1, code="L1-S01-1", level_no=1, situation="테스트"),
        CurLesson(language="ko", no=2, code="L1-S02-1", level_no=1, situation="테스트"),
        CurLesson(language="ko", no=3, code="L1-S03-1", level_no=1, situation="테스트"),   # 레벨1 마지막
        CurLesson(language="ko", no=4, code="A1-T01-1", level_no=2, situation="테스트"),   # 레벨2 첫 차시 — ①
        CurLesson(language="ko", no=5, code="A1-T02-1", level_no=2, situation="테스트"),   # 같은 레벨 — ②
        CurLesson(language="ko", no=6, code="A2-T01-1", level_no=3, situation="테스트"),   # 레벨3 마지막(nxt 없음) — ⑥
        # 일부러 역순 시드(레벨이 내려가는 no 순서) — ⑦ 방어 시험 전용.
        CurLesson(language="ko", no=10, code="X-BAD-HIGH", level_no=5, situation="테스트"),
        CurLesson(language="ko", no=11, code="X-BAD-LOW", level_no=2, situation="테스트"),
    ])
    s.commit()
    return s


def _member(db) -> int:
    m = Member(language="en", onboarding_completed=True,
               auth_user_id=f"a{db.query(Member).count() + 1}")
    db.add(m)
    db.commit()
    return m.member_id


def _freetalk_call(db, member_id: int, lesson_no: int) -> int:
    """`lesson_no` 차시의 프리토킹 통화를 만들고, 진도 포인터도 그 차시에 놓는다."""
    lesson = repo.lesson_by_no(db, "ko", lesson_no)
    c = Call(member_id=member_id, character_id=1, call_type="freetalk", status="done")
    db.add(c)
    db.flush()
    db.add(CurCall(call_id=c.call_id, lesson_id=lesson.lesson_id, course="freetalk"))
    prog = repo.current_progress(db, member_id, "ko")
    if prog is None:
        db.add(CurMemberProgress(member_id=member_id, language="ko", lesson_id=lesson.lesson_id))
    else:
        prog.lesson_id = lesson.lesson_id
    db.commit()
    return c.call_id


# --------------------------------------------------------------------------- #
# ① no=3→4 면 레벨 1→2
# --------------------------------------------------------------------------- #
def test_crossing_a_level_boundary_raises_the_level(db):
    mid = _member(db)
    call_id = _freetalk_call(db, mid, 3)
    res = cur.complete_freetalk(db, call_id, duration_s=200, normal_end=True)
    assert res == {"freetalk_done": True, "moved": True}
    db.expire_all()
    assert mastery_repository.get_language_level(db, mid, "ko") == 2
    prog = repo.current_progress(db, mid, "ko")
    assert db.get(CurLesson, prog.lesson_id).no == 4


# --------------------------------------------------------------------------- #
# ② 같은 레벨 안 이동(4→5)이면 레벨 무변경
# --------------------------------------------------------------------------- #
def test_moving_within_the_same_level_does_not_change_the_level(db):
    mid = _member(db)
    call_id = _freetalk_call(db, mid, 4)
    res = cur.complete_freetalk(db, call_id, duration_s=200, normal_end=True)
    assert res == {"freetalk_done": True, "moved": True}
    db.expire_all()
    assert mastery_repository.get_language_level(db, mid, "ko") is None, "레벨 배정이 아예 없었는데 생기면 안 된다"
    prog = repo.current_progress(db, mid, "ko")
    assert db.get(CurLesson, prog.lesson_id).no == 5
    assert db.query(MemberLevelHistory).filter_by(member_id=mid).count() == 0


# --------------------------------------------------------------------------- #
# ③ ko 면 korean_level 도 갱신
# --------------------------------------------------------------------------- #
def test_ko_level_up_also_updates_the_legacy_korean_level_column(db):
    mid = _member(db)
    call_id = _freetalk_call(db, mid, 3)
    cur.complete_freetalk(db, call_id, duration_s=200, normal_end=True)
    db.expire_all()
    assert db.get(Member, mid).korean_level == 2


# --------------------------------------------------------------------------- #
# ④ history 1행(reason='curriculum_advance')
# --------------------------------------------------------------------------- #
def test_level_up_writes_one_history_row(db):
    mid = _member(db)
    call_id = _freetalk_call(db, mid, 3)
    cur.complete_freetalk(db, call_id, duration_s=200, normal_end=True)
    db.expire_all()
    rows = db.query(MemberLevelHistory).filter_by(member_id=mid).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.reason == "curriculum_advance"
    assert row.from_level == 1 and row.to_level == 2
    assert row.language == "ko"
    assert row.trigger_call_id == call_id


# --------------------------------------------------------------------------- #
# ⑤ 같은 통화로 두 번 불러도 1행(멱등)
# --------------------------------------------------------------------------- #
def test_calling_complete_freetalk_twice_with_the_same_call_writes_only_one_row(db):
    mid = _member(db)
    call_id = _freetalk_call(db, mid, 3)
    first = cur.complete_freetalk(db, call_id, duration_s=200, normal_end=True)
    second = cur.complete_freetalk(db, call_id, duration_s=200, normal_end=True)
    assert first == {"freetalk_done": True, "moved": True}
    assert second == {"freetalk_done": True, "moved": False}, "이미 done — no-op(조각2 재호출)"
    db.expire_all()
    assert db.query(MemberLevelHistory).filter_by(member_id=mid).count() == 1
    assert mastery_repository.get_language_level(db, mid, "ko") == 2


# --------------------------------------------------------------------------- #
# ⑥ 마지막 차시(nxt 없음)면 아무 일도 없음
# --------------------------------------------------------------------------- #
def test_the_last_lesson_has_no_next_lesson_so_nothing_happens(db):
    mid = _member(db)
    call_id = _freetalk_call(db, mid, 11)  # 시드 전체의 마지막 차시(no=11) — next_lesson 이 None
    res = cur.complete_freetalk(db, call_id, duration_s=200, normal_end=True)
    assert res == {"freetalk_done": True, "moved": False}
    db.expire_all()
    assert mastery_repository.get_language_level(db, mid, "ko") is None
    prog = repo.current_progress(db, mid, "ko")
    assert db.get(CurLesson, prog.lesson_id).no == 11, "포인터가 그대로 남는다(넘길 차시가 없다)"
    assert db.query(MemberLevelHistory).filter_by(member_id=mid).count() == 0


# --------------------------------------------------------------------------- #
# ⑦ 레벨이 작아지는 경우 올리지 않는다(방어 — 시드를 믿지 않는다)
# --------------------------------------------------------------------------- #
def test_a_lower_level_on_the_next_lesson_does_not_lower_the_level(db, caplog):
    import logging

    mid = _member(db)
    call_id = _freetalk_call(db, mid, 10)  # X-BAD-HIGH(level=5) → next 는 no=11(level=2, 역순 시드)
    with caplog.at_level(logging.WARNING, logger=cur.logger.name):
        res = cur.complete_freetalk(db, call_id, duration_s=200, normal_end=True)
    assert res == {"freetalk_done": True, "moved": True}, "포인터는 여전히 다음 차시로 간다(레벨만 방어)"
    db.expire_all()
    prog = repo.current_progress(db, mid, "ko")
    assert db.get(CurLesson, prog.lesson_id).no == 11, "차시 포인터 자체는 next_lesson 규칙대로 전진한다"
    assert mastery_repository.get_language_level(db, mid, "ko") is None, "레벨은 안 내려간다(그대로 미배정)"
    assert db.query(MemberLevelHistory).filter_by(member_id=mid).count() == 0
    assert any("레벨을 안 올린다" in r.getMessage() for r in caplog.records)
