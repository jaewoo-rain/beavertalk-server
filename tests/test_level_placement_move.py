"""L2 — 레벨테스트 재측정 시 진도 포인터 이동 회귀. 외부 의존 0, 인메모리 sqlite.

docs/plans/2026-09-23-레벨-커리큘럼-연결.md T1-b. `_save_level_assessment` 가 레벨을
확정하는 **그 자리, 같은 커밋**에서 그 언어 진도 포인터도 새 레벨 첫 차시로 옮긴다.

무엇을 지키나:
  ① 레벨↑ → 진도가 앞으로(새 레벨 첫 차시)
  ② 레벨↓ → 진도가 뒤로(새 레벨 첫 차시) — 자동 강등은 없지만 재측정은 예외(D3)
  ③ 값이 같아도(2→2) 이동한다 — 레벨 값 비교가 아니라 "재측정 이벤트" 자체가 트리거
  ④ 배운 기록(cur_member_lesson·cur_member_item)은 건드리지 않는다
  ⑤ 다른 언어 진도는 영향받지 않는다
  ⑥ 진도 행이 없으면 만들지 않는다(L1 이 다음 통화에 만든다)
  ⑦ 그 레벨 차시가 0건이면 조용히 넘어간다(예외로 레벨테스트 저장을 안 죽인다)
  ⑧ 레벨과 진도가 한 커밋에 같이 반영된다(중간 상태 없음)
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.learning.models.call import Call
from domains.learning.models.curriculum import CurItem, CurLesson, CurMemberItem, CurMemberLesson, CurMemberProgress
from domains.learning.models.level import Level
import domains.learning.service.normalcall_service as svc
from domains.learning.repository import curriculum_repository as repo
from domains.learning.repository import mastery_repository


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

    # ko: 레벨1(no 1~3) · 레벨2(no 4~5) · 레벨3(no 6~7)
    s.add(Level(language="ko", level_no=1, profile="생존 회화"))
    s.add(Level(language="ko", level_no=2, profile="초급 A1"))
    s.add(Level(language="ko", level_no=3, profile="초급 A2"))
    s.add_all([
        CurLesson(language="ko", no=1, code="L1-S01-1", level_no=1, situation="테스트"),
        CurLesson(language="ko", no=2, code="L1-S02-1", level_no=1, situation="테스트"),
        CurLesson(language="ko", no=3, code="L1-S03-1", level_no=1, situation="테스트"),
        CurLesson(language="ko", no=4, code="A1-T01-1", level_no=2, situation="테스트"),
        CurLesson(language="ko", no=5, code="A1-T02-1", level_no=2, situation="테스트"),
        CurLesson(language="ko", no=6, code="A2-T01-1", level_no=3, situation="테스트"),
        CurLesson(language="ko", no=7, code="A2-T02-1", level_no=3, situation="테스트"),
    ])
    # ja: 레벨1(no 1) 하나뿐 — "다른 언어 무영향" 시험용
    s.add(Level(language="ja", level_no=1, profile="일본어 입문"))
    s.add(CurLesson(language="ja", no=1, code="JL1-S01-1", level_no=1, situation="테스트"))
    # fr: 레벨1(no 1) 뿐 — "그 레벨 차시 0건" 시험용(레벨5 는 차시가 없다)
    s.add(Level(language="fr", level_no=1, profile="프랑스어 입문"))
    s.add(Level(language="fr", level_no=5, profile="프랑스어 중급"))
    s.add(CurLesson(language="fr", no=1, code="FL1-S01-1", level_no=1, situation="테스트"))
    s.commit()
    return s


def _member(db) -> int:
    m = Member(language="en", onboarding_completed=True,
               auth_user_id=f"a{db.query(Member).count() + 1}")
    db.add(m)
    db.commit()
    return m.member_id


def _call(db, member_id: int, target_language: str = "ko") -> int:
    c = Call(member_id=member_id, character_id=1, call_type="level_test",
             status="analyzing", target_language=target_language)
    db.add(c)
    db.commit()
    return c.call_id


def _lesson_no(db, lesson_id: int) -> int:
    return db.get(CurLesson, lesson_id).no


def _assessment(**kw) -> svc.LevelAssessment:
    base = dict(
        evidence=["안녕하세요"], reasoning="초급 문형 사용", distinct_structures=3,
        band="a1", confidence="high", sample_quality="sufficient",
        summary="자기소개", feedback_for_learner="잘했어요!",
    )
    base.update(kw)
    return svc.LevelAssessment(**base)


def _set_progress(db, member_id: int, language: str, no: int) -> None:
    lesson = repo.lesson_by_no(db, language, no)
    prog = repo.current_progress(db, member_id, language)
    if prog is None:
        db.add(CurMemberProgress(member_id=member_id, language=language, lesson_id=lesson.lesson_id))
    else:
        prog.lesson_id = lesson.lesson_id
    db.commit()


# --------------------------------------------------------------------------- #
# ① 레벨↑ → 진도가 앞으로
# --------------------------------------------------------------------------- #
def test_level_up_moves_progress_forward_to_the_new_levels_first_lesson(db):
    mid = _member(db)
    _set_progress(db, mid, "ko", 2)  # 레벨1 안에서 진행 중
    call_id = _call(db, mid, "ko")
    assert svc._save_level_assessment(db, call_id, mid, 3, _assessment()) is True
    prog = repo.current_progress(db, mid, "ko")
    assert _lesson_no(db, prog.lesson_id) == 6  # 레벨3 첫 차시


# --------------------------------------------------------------------------- #
# ② 레벨↓ → 진도가 뒤로
# --------------------------------------------------------------------------- #
def test_level_down_moves_progress_backward_to_the_new_levels_first_lesson(db):
    mid = _member(db)
    _set_progress(db, mid, "ko", 6)  # 레벨3 에서 진행 중
    call_id = _call(db, mid, "ko")
    assert svc._save_level_assessment(db, call_id, mid, 1, _assessment()) is True
    prog = repo.current_progress(db, mid, "ko")
    assert _lesson_no(db, prog.lesson_id) == 1  # 레벨1 첫 차시로 되돌아간다


# --------------------------------------------------------------------------- #
# ③ 값이 같아도(2→2) 이동한다 — 값 비교가 아니라 이벤트 트리거
# --------------------------------------------------------------------------- #
def test_same_level_retest_still_moves_progress_to_the_levels_first_lesson(db):
    mid = _member(db)
    _set_progress(db, mid, "ko", 5)  # 레벨2 안에서 이미 두 번째 차시까지 진행
    assert mastery_repository.get_language_level(db, mid, "ko") is None  # 최초 배정 전
    # 먼저 레벨2 로 배정(placement) — 이건 "최초" 라 from_level=None.
    call1 = _call(db, mid, "ko")
    svc._save_level_assessment(db, call1, mid, 2, _assessment())
    _set_progress(db, mid, "ko", 5)  # 배정 뒤 학습이 진행돼 no=5 까지 갔다고 가정
    # 재측정 — 결과가 똑같이 레벨2 다. 값은 안 바뀌었지만 "재측정 이벤트" 이므로 이동해야 한다.
    call2 = _call(db, mid, "ko")
    assert svc._save_level_assessment(db, call2, mid, 2, _assessment()) is True
    prog = repo.current_progress(db, mid, "ko")
    assert _lesson_no(db, prog.lesson_id) == 4, "2→2 인데도 그 레벨 첫 차시로 이동해야 한다"


# --------------------------------------------------------------------------- #
# ④ 배운 기록은 건드리지 않는다
# --------------------------------------------------------------------------- #
def test_learned_records_survive_the_move(db):
    mid = _member(db)
    lesson5 = repo.lesson_by_no(db, "ko", 5)
    item = CurItem(language="ko", kind="vocab", key="k1", surface="단어1", level_no=2)
    db.add(item)
    db.flush()
    _set_progress(db, mid, "ko", 5)
    now = datetime.now(timezone.utc)
    db.add(CurMemberLesson(member_id=mid, lesson_id=lesson5.lesson_id, status="expression_done", expression_done_at=now))
    db.add(CurMemberItem(member_id=mid, lesson_id=lesson5.lesson_id, item_id=item.item_id, drilled_at=now))
    db.commit()

    call_id = _call(db, mid, "ko")
    svc._save_level_assessment(db, call_id, mid, 1, _assessment())

    assert db.query(CurMemberLesson).filter_by(member_id=mid, lesson_id=lesson5.lesson_id).one().status == "expression_done"
    row = db.query(CurMemberItem).filter_by(member_id=mid, lesson_id=lesson5.lesson_id, item_id=item.item_id).one()
    assert row.drilled_at is not None


# --------------------------------------------------------------------------- #
# ⑤ 다른 언어 진도는 영향받지 않는다
# --------------------------------------------------------------------------- #
def test_other_language_progress_is_untouched(db):
    mid = _member(db)
    _set_progress(db, mid, "ko", 2)
    _set_progress(db, mid, "ja", 1)
    ja_before = repo.current_progress(db, mid, "ja").lesson_id

    call_id = _call(db, mid, "ko")
    svc._save_level_assessment(db, call_id, mid, 3, _assessment())

    assert repo.current_progress(db, mid, "ja").lesson_id == ja_before


# --------------------------------------------------------------------------- #
# ⑥ 진도 행이 없으면 만들지 않는다
# --------------------------------------------------------------------------- #
def test_no_progress_row_is_not_created_by_the_move(db):
    mid = _member(db)
    assert repo.current_progress(db, mid, "ko") is None
    call_id = _call(db, mid, "ko")
    assert svc._save_level_assessment(db, call_id, mid, 2, _assessment()) is True
    assert repo.current_progress(db, mid, "ko") is None, "L1(ensure_progress)의 몫이지 L2 가 만들면 안 된다"


# --------------------------------------------------------------------------- #
# ⑦ 그 레벨 차시가 0건이면 조용히 넘어간다(예외로 저장을 안 죽인다)
# --------------------------------------------------------------------------- #
def test_level_with_zero_lessons_does_not_crash_and_keeps_the_old_pointer(db):
    mid = _member(db)
    _set_progress(db, mid, "fr", 1)
    before = repo.current_progress(db, mid, "fr").lesson_id
    call_id = _call(db, mid, "fr")
    assert svc._save_level_assessment(db, call_id, mid, 5, _assessment()) is True, "차시 0건이라도 레벨테스트 저장은 죽으면 안 된다"
    assert repo.current_progress(db, mid, "fr").lesson_id == before, "옮길 차시가 없으니 그대로 남는다"


def test_level_with_zero_lessons_logs_a_warning(db, caplog):
    import logging

    mid = _member(db)
    _set_progress(db, mid, "fr", 1)
    call_id = _call(db, mid, "fr")
    with caplog.at_level(logging.WARNING, logger=svc.logger.name):
        svc._save_level_assessment(db, call_id, mid, 5, _assessment())
    assert any("진도 이동 생략" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# ⑧ 레벨과 진도가 한 커밋에 같이 반영된다
# --------------------------------------------------------------------------- #
def test_level_and_progress_land_together_in_the_same_call(db):
    mid = _member(db)
    _set_progress(db, mid, "ko", 2)
    call_id = _call(db, mid, "ko")
    svc._save_level_assessment(db, call_id, mid, 3, _assessment())
    # ⚠ QA 지적(2026-09-24) — 같은 세션의 identity map 이 in-memory 값을 돌려주면 "DB 에
    #   실렸다"를 증명하지 못한다. expire_all() 로 캐시를 비우고 실제로 다시 읽는다.
    db.expire_all()
    assert mastery_repository.get_language_level(db, mid, "ko") == 3
    prog = repo.current_progress(db, mid, "ko")
    assert _lesson_no(db, prog.lesson_id) == 6
