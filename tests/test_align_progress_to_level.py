"""L4 — 기존 회원의 레벨↔진도 정합 스크립트 회귀. 외부 의존 0, 인메모리 sqlite.

docs/plans/2026-09-23-레벨-커리큘럼-연결.md L4. scripts/dev_align_progress_to_level.py
의 순수 결정 함수(_decide)/적용 함수(_apply)를 직접 부른다 — main() 은 실 DB 커넥션을
만들어 여기서 부르지 않는다(스크립트 자체는 bt-back 이 dry-run 출력을 읽고 판단한다).

무엇을 지키나:
  ① _decide 만 호출(=dry-run)하면 DB 에 쓰기가 없다
  ② 레벨1·진도가 레벨1 안(no=10 이어도)이면 유지
  ③ 레벨2·진도 no=1(레벨1 차시) → 진도를 레벨2 첫 차시로 이동
  ④ 레벨1·진도 no=4(레벨2 차시) → 레벨을 2 로 상향(진도는 무변경)
  ⑤ 레벨NULL·진도가 레벨2 차시 → 레벨 2 로 상향
  ⑥ 레벨NULL·진도가 레벨1 차시 → 유지(NULL 보존 — needs_level_test 게이트를 지킨다)
  ⑦ ja/ko 가 서로 독립
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
from domains.learning.models.member_level_history import MemberLevelHistory
from domains.learning.repository import curriculum_repository as repo
from domains.learning.repository import mastery_repository
from scripts.dev_align_progress_to_level import _apply, _decide


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

    # ko: 레벨1(no 1~10, 일부러 넓게 — «no 가 커도 같은 레벨이면 유지» 시험용) · 레벨2(no 11~12)
    s.add(Level(language="ko", level_no=1, profile="생존 회화"))
    s.add(Level(language="ko", level_no=2, profile="초급 A1"))
    lessons = [CurLesson(language="ko", no=n, code=f"L1-{n:02d}", level_no=1, situation="테스트")
               for n in range(1, 11)]
    lessons += [
        CurLesson(language="ko", no=11, code="A1-T01-1", level_no=2, situation="테스트"),
        CurLesson(language="ko", no=12, code="A1-T02-1", level_no=2, situation="테스트"),
    ]
    s.add_all(lessons)
    # ja: 레벨1(no 1) · 레벨2(no 2) — ko 와 완전히 독립된 번호 체계
    s.add(Level(language="ja", level_no=1, profile="일본어 입문"))
    s.add(Level(language="ja", level_no=2, profile="일본어 초급"))
    s.add_all([
        CurLesson(language="ja", no=1, code="JL1-01", level_no=1, situation="테스트"),
        CurLesson(language="ja", no=2, code="JA1-01", level_no=2, situation="테스트"),
    ])
    s.commit()
    return s


def _member(db) -> int:
    m = Member(language="en", onboarding_completed=True,
               auth_user_id=f"a{db.query(Member).count() + 1}")
    db.add(m)
    db.commit()
    return m.member_id


def _progress(db, member_id: int, language: str, no: int) -> CurMemberProgress:
    lesson = repo.lesson_by_no(db, language, no)
    prog = CurMemberProgress(member_id=member_id, language=language, lesson_id=lesson.lesson_id)
    db.add(prog)
    db.commit()
    return prog


def _set_level(db, member_id: int, language: str, level_no: int) -> None:
    db.add(MemberLanguageLevel(member_id=member_id, language=language, level_no=level_no))
    db.commit()


# --------------------------------------------------------------------------- #
# ① _decide 만 호출하면(=dry-run) 쓰기가 없다
# --------------------------------------------------------------------------- #
def test_decide_alone_writes_nothing(db):
    mid = _member(db)
    _set_level(db, mid, "ko", 2)
    prog = _progress(db, mid, "ko", 1)
    _decide(db, prog)
    db.expire_all()
    assert repo.current_progress(db, mid, "ko").lesson_id == prog.lesson_id, "진도가 건드려졌다"
    assert mastery_repository.get_language_level(db, mid, "ko") == 2, "레벨이 건드려졌다"
    assert db.query(MemberLevelHistory).count() == 0


# --------------------------------------------------------------------------- #
# ② 레벨1 · 진도가 레벨1 안(no=10) → 유지
# --------------------------------------------------------------------------- #
def test_level_1_with_progress_still_inside_level_1_is_kept(db):
    mid = _member(db)
    _set_level(db, mid, "ko", 1)
    prog = _progress(db, mid, "ko", 10)
    d = _decide(db, prog)
    assert d["action"] == "keep"


# --------------------------------------------------------------------------- #
# ③ 레벨2 · 진도 no=1(레벨1 차시) → 진도를 레벨2 첫 차시(no=11)로 이동
# --------------------------------------------------------------------------- #
def test_level_ahead_of_progress_moves_the_progress_pointer(db):
    mid = _member(db)
    _set_level(db, mid, "ko", 2)
    prog = _progress(db, mid, "ko", 1)
    d = _decide(db, prog)
    assert d == {"action": "progress_move", "level": 2, "from_no": 1, "to_no": 11,
                 "target_lesson_id": repo.lesson_by_no(db, "ko", 11).lesson_id}
    _apply(db, prog, d)
    db.commit()
    db.expire_all()
    assert db.get(CurLesson, repo.current_progress(db, mid, "ko").lesson_id).no == 11
    assert mastery_repository.get_language_level(db, mid, "ko") == 2, "레벨은 안 건드린다(이미 맞다)"
    assert db.query(MemberLevelHistory).filter_by(member_id=mid).count() == 0, "진도이동은 이력을 안 남긴다"


# --------------------------------------------------------------------------- #
# ④ 레벨1 · 진도 no=11(레벨2 차시) → 레벨을 2 로 상향(진도는 무변경)
# --------------------------------------------------------------------------- #
def test_progress_ahead_of_level_raises_the_level_only(db):
    mid = _member(db)
    _set_level(db, mid, "ko", 1)
    prog = _progress(db, mid, "ko", 11)
    d = _decide(db, prog)
    assert d == {"action": "level_up", "from_level": 1, "to_level": 2, "no": 11}
    _apply(db, prog, d)
    db.commit()
    db.expire_all()
    assert mastery_repository.get_language_level(db, mid, "ko") == 2
    assert db.get(CurLesson, repo.current_progress(db, mid, "ko").lesson_id).no == 11, "진도는 그대로"
    row = db.query(MemberLevelHistory).filter_by(member_id=mid).one()
    assert row.reason == "curriculum_advance" and row.from_level == 1 and row.to_level == 2
    assert row.trigger_call_id is None, "통화가 트리거가 아니다"


# --------------------------------------------------------------------------- #
# ⑤ 레벨NULL · 진도가 레벨2 차시 → 레벨 2 로 상향
# --------------------------------------------------------------------------- #
def test_null_level_with_progress_past_level_1_raises_the_level(db):
    mid = _member(db)
    prog = _progress(db, mid, "ko", 11)  # 레벨 설정 안 함(NULL)
    assert mastery_repository.get_language_level(db, mid, "ko") is None
    d = _decide(db, prog)
    assert d == {"action": "level_up", "from_level": None, "to_level": 2, "no": 11}
    _apply(db, prog, d)
    db.commit()
    db.expire_all()
    assert mastery_repository.get_language_level(db, mid, "ko") == 2
    row = db.query(MemberLevelHistory).filter_by(member_id=mid).one()
    assert row.from_level is None and row.to_level == 2


# --------------------------------------------------------------------------- #
# ⑥ 레벨NULL · 진도가 레벨1 차시 → 유지(NULL 보존)
# --------------------------------------------------------------------------- #
def test_null_level_with_progress_still_in_level_1_is_left_alone(db):
    """⛔⛔ 제일 틀리기 쉬운 지점 — NULL 을 1 로 채우면 needs_level_test 게이트를 잃는다."""
    mid = _member(db)
    prog = _progress(db, mid, "ko", 5)  # 레벨1 안(no=1~10), 레벨 미설정
    d = _decide(db, prog)
    assert d["action"] == "keep"
    _apply(db, prog, d)  # keep 은 아무 일도 안 해야 한다
    db.commit()
    db.expire_all()
    assert mastery_repository.get_language_level(db, mid, "ko") is None, "NULL 이 1 로 채워지면 안 된다"


# --------------------------------------------------------------------------- #
# ⑦ ja/ko 독립
# --------------------------------------------------------------------------- #
def test_ja_and_ko_are_aligned_independently(db):
    mid = _member(db)
    _set_level(db, mid, "ko", 2)
    ko_prog = _progress(db, mid, "ko", 1)     # ko: 레벨이 앞섬 → 진도이동
    ja_prog = _progress(db, mid, "ja", 2)     # ja: 레벨 NULL, 진도가 레벨2 차시 → 레벨상향
    d_ko = _decide(db, ko_prog)
    d_ja = _decide(db, ja_prog)
    assert d_ko["action"] == "progress_move" and d_ko["to_no"] == 11
    assert d_ja == {"action": "level_up", "from_level": None, "to_level": 2, "no": 2}
    _apply(db, ko_prog, d_ko)
    _apply(db, ja_prog, d_ja)
    db.commit()
    db.expire_all()
    assert db.get(CurLesson, repo.current_progress(db, mid, "ko").lesson_id).no == 11
    assert mastery_repository.get_language_level(db, mid, "ko") == 2, "ko 레벨은 원래도 2 였다(무변경)"
    assert mastery_repository.get_language_level(db, mid, "ja") == 2, "ja 는 이번에 새로 올랐다"
    assert db.get(CurLesson, repo.current_progress(db, mid, "ja").lesson_id).no == 2, "ja 진도는 그대로"
