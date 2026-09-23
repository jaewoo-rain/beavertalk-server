"""L1 — 진도 생성 시 레벨 반영(placement) 회귀. 외부 의존 0, 인메모리 sqlite.

docs/plans/2026-09-23-레벨-커리큘럼-연결.md T1-a. `ensure_progress` 가 진도 행을
**처음 만들 때** no=1 대신 그 언어 레벨의 첫 차시로 만든다.

무엇을 지키나:
  ① 레벨2 신규 회원 → 그 레벨 첫 차시(no=4)에서 시작
  ② 레벨 NULL(미실시) → no=1(종전 그대로)
  ③ 그 레벨 차시가 0건인 언어 → no=1 폴백, 예외 안 남(R5)
  ④ ja 진도를 만들 때 ko 레벨을 안 본다(언어 스코프, 함정 1)
  ⑤ 진도 행이 이미 있으면 절대 건드리지 않는다(이동은 L2 의 몫)
"""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.learning.models.curriculum import CurLesson, CurMemberProgress
from domains.learning.models.level import Level
from domains.learning.models.member_language_level import MemberLanguageLevel
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

    # ko: 레벨1(no 1~3) · 레벨2(no 4~5)
    s.add(Level(language="ko", level_no=1, profile="생존 회화"))
    s.add(Level(language="ko", level_no=2, profile="초급 A1"))
    s.add_all([
        CurLesson(language="ko", no=1, code="L1-S01-1", level_no=1, situation="테스트"),
        CurLesson(language="ko", no=2, code="L1-S02-1", level_no=1, situation="테스트"),
        CurLesson(language="ko", no=3, code="L1-S03-1", level_no=1, situation="테스트"),
        CurLesson(language="ko", no=4, code="A1-T01-1", level_no=2, situation="테스트"),
        CurLesson(language="ko", no=5, code="A1-T02-1", level_no=2, situation="테스트"),
    ])
    # ja: 레벨1(no 1) 하나뿐 — 레벨2 이상은 시드가 없다(③④ 시험용)
    s.add(Level(language="ja", level_no=1, profile="일본어 입문"))
    s.add(CurLesson(language="ja", no=1, code="JL1-S01-1", level_no=1, situation="테스트"))
    # fr: 레벨1(no 1) 하나뿐 — 회원은 레벨5 를 갖지만 그 레벨 차시가 0건(③ 시험용)
    s.add(Level(language="fr", level_no=1, profile="프랑스어 입문"))
    s.add(Level(language="fr", level_no=5, profile="프랑스어 중급"))
    s.add(CurLesson(language="fr", no=1, code="FL1-S01-1", level_no=1, situation="테스트"))
    s.commit()
    return s


def _member(db, korean_level: int | None = None) -> int:
    m = Member(language="en", korean_level=korean_level, onboarding_completed=True,
               auth_user_id=f"a{db.query(Member).count() + 1}")
    db.add(m)
    db.commit()
    return m.member_id


def _lesson_no(db, lesson_id: int) -> int:
    return db.get(CurLesson, lesson_id).no


# --------------------------------------------------------------------------- #
# ① 레벨2 신규 → 그 레벨 첫 차시
# --------------------------------------------------------------------------- #
def test_new_member_with_level_2_starts_at_the_levels_first_lesson(db):
    mid = _member(db, korean_level=2)
    prog = cur.ensure_progress(db, mid, "ko")
    assert _lesson_no(db, prog.lesson_id) == 4


# --------------------------------------------------------------------------- #
# ② 레벨 NULL(레벨테스트 미실시) → no=1 종전 그대로
# --------------------------------------------------------------------------- #
def test_no_level_falls_back_to_no_1(db):
    mid = _member(db, korean_level=None)
    prog = cur.ensure_progress(db, mid, "ko")
    assert _lesson_no(db, prog.lesson_id) == 1


# --------------------------------------------------------------------------- #
# ③ 그 레벨 차시가 0건 → no=1 폴백, 예외 없음
# --------------------------------------------------------------------------- #
def test_level_with_zero_lessons_falls_back_to_no_1(db):
    mid = _member(db)
    db.add(MemberLanguageLevel(member_id=mid, language="fr", level_no=5))
    db.commit()
    prog = cur.ensure_progress(db, mid, "fr")  # fr 레벨5 는 차시 0건 — fr 유일 차시(no=1)로
    assert _lesson_no(db, prog.lesson_id) == 1


def test_level_with_zero_lessons_logs_a_warning_not_an_exception(db, caplog):
    import logging

    mid = _member(db)
    db.add(MemberLanguageLevel(member_id=mid, language="fr", level_no=5))
    db.commit()
    with caplog.at_level(logging.WARNING, logger=cur.logger.name):
        cur.ensure_progress(db, mid, "fr")
    assert any("no=1 폴백" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# ④ 언어 스코프 — ja 진도를 만들 때 ko 레벨을 안 본다
# --------------------------------------------------------------------------- #
def test_ja_progress_ignores_the_korean_level(db):
    """★ 함정 1 — ko 레벨 2 인 회원이라도 ja 는 언어별 레벨(여기선 미실시=None)을 본다.
    ko 레벨을 봤다면 ja 레벨2 차시를 찾으려다 못 찾아 no=1 폴백이 (우연히) 같은
    결과를 낼 수 있으므로, ja 에 레벨2 차시를 안 만들어 두어 "ko 를 봤다면 여기로
    빠졌을 것"이라는 함정을 배제했다 — 유일한 ja 차시(no=1, 레벨1)만 있다."""
    mid = _member(db, korean_level=2)
    prog = cur.ensure_progress(db, mid, "ja")
    assert _lesson_no(db, prog.lesson_id) == 1


# --------------------------------------------------------------------------- #
# ⑤ 행이 이미 있으면 절대 건드리지 않는다
# --------------------------------------------------------------------------- #
def test_existing_progress_row_is_never_moved(db):
    mid = _member(db, korean_level=2)  # 새로 만들면 no=4 로 가야 할 레벨
    lesson2 = db.query(CurLesson).filter_by(language="ko", no=2).one()
    db.add(CurMemberProgress(member_id=mid, language="ko", lesson_id=lesson2.lesson_id))
    db.commit()

    prog = cur.ensure_progress(db, mid, "ko")
    assert _lesson_no(db, prog.lesson_id) == 2, "기존 진도가 레벨에 맞춰 옮겨졌다 — L1 은 생성만 해야 한다"


def test_existing_progress_row_is_returned_as_is_on_repeated_calls(db):
    mid = _member(db, korean_level=None)
    first = cur.ensure_progress(db, mid, "ko")
    second = cur.ensure_progress(db, mid, "ko")
    assert first.lesson_id == second.lesson_id
    assert db.query(CurMemberProgress).filter_by(member_id=mid, language="ko").count() == 1
