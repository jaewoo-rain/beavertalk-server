"""L7 — 재측정으로 진도가 «이미 완료된 차시」로 되돌아가면 영구히 갇히던 결함 수정.

배경(fable-qa 재현, bt-back 확인): L2(`_move_progress_to_new_level`)가 재측정 시 진도
포인터를 그 레벨 첫 차시로 되돌리는데, 그 차시가 **이미 완료**(`expression_done`·
`freetalk_done`)돼 있으면 `decide_course`·`open_call`(freetalk 잠금)·`record_expression`·
`complete_freetalk` 이 전부 "이미 끝났다"를 전제해 되돌아온 진도를 다시 안 받아준다 —
표현학습 무한반복·프리토킹 안 열림·포인터 정지·레벨 정지.

수정: L2 가 포인터를 옮기는 그 자리에서, 되돌아간 지점(`no >= 목표 no`)의 완료 표시를
"학습 중"으로 되돌린다(`curriculum_service.reset_lessons_from`). `cur_member_item`(드릴
기록)은 안 건드린다 — 다시 하면 `review=True`(복습)로 나온다.

무엇을 지키나(전부 외부 의존 0, 인메모리 sqlite):
  ① 핵심 — 완료된 차시로 되돌아간 뒤 «표현학습 → 프리토킹 → 다음 차시 → 레벨업» 이 돈다
  ② 되돌린 범위는 목표 차시 **이후만**(이전 차시 완료 표시 무변경)
  ③ cur_member_item 무변경 — 다시 할 때 review=True 로 나온다
  ④ 레벨이 내려가는 재측정에서도 같다
  ⑤ ja/ko 독립
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
from domains.learning.models.curriculum import (
    CurItem,
    CurLesson,
    CurLessonItem,
    CurMemberItem,
    CurMemberLesson,
    CurMemberProgress,
)
from domains.learning.models.level import Level
from domains.learning.repository import curriculum_repository as repo
from domains.learning.repository import mastery_repository
from domains.learning.service import curriculum_service as cur
import domains.learning.service.normalcall_service as svc


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

    # ko: 레벨1(no=1) · 레벨2(no=4,5, 항목 1개씩) · 레벨3(no=6)
    s.add(Level(language="ko", level_no=1, profile="생존 회화"))
    s.add(Level(language="ko", level_no=2, profile="초급 A1"))
    s.add(Level(language="ko", level_no=3, profile="초급 A2"))
    lessons = {
        "l1": CurLesson(language="ko", no=1, code="L1-S01-1", level_no=1, situation="테스트"),
        "l4": CurLesson(language="ko", no=4, code="A1-T01-1", level_no=2, situation="테스트"),
        "l5": CurLesson(language="ko", no=5, code="A1-T02-1", level_no=2, situation="테스트"),
        "l6": CurLesson(language="ko", no=6, code="A2-T01-1", level_no=3, situation="테스트"),
    }
    s.add_all(lessons.values())
    s.flush()
    item4 = CurItem(language="ko", kind="vocab", key="w4", surface="단어4", level_no=2)
    item5 = CurItem(language="ko", kind="vocab", key="w5", surface="단어5", level_no=2)
    s.add_all([item4, item5])
    s.flush()
    s.add(CurLessonItem(lesson_id=lessons["l4"].lesson_id, item_id=item4.item_id, role="must", seq=1))
    s.add(CurLessonItem(lesson_id=lessons["l5"].lesson_id, item_id=item5.item_id, role="must", seq=1))
    # ja: 레벨1(no=1) 뿐 — ⑤ 언어 독립 시험용
    s.add(Level(language="ja", level_no=1, profile="일본어 입문"))
    s.add(CurLesson(language="ja", no=1, code="JL1-S01-1", level_no=1, situation="테스트"))
    s.commit()
    return s, lessons, item4, item5


def _member(db) -> int:
    m = Member(language="en", onboarding_completed=True,
               auth_user_id=f"a{db.query(Member).count() + 1}")
    db.add(m)
    db.commit()
    return m.member_id


def _now():
    return datetime.now(timezone.utc)


def _mark_lesson_done(db, member_id: int, lesson_id: int, item_id: int, *, freetalk: bool) -> None:
    """no 를 «완료」 상태로 만든다 — 드릴·통과 기록(CurMemberItem) + 차시 상태(CurMemberLesson)."""
    now = _now()
    db.add(CurMemberItem(member_id=member_id, lesson_id=lesson_id, item_id=item_id,
                         drilled_at=now, quiz_passed_at=now))
    ml = CurMemberLesson(
        member_id=member_id, lesson_id=lesson_id,
        status=("freetalk_done" if freetalk else "expression_done"),
        expression_done_at=now, freetalk_done_at=(now if freetalk else None),
    )
    db.add(ml)
    db.commit()


def _assessment(**kw) -> svc.LevelAssessment:
    base = dict(
        evidence=["안녕하세요"], reasoning="문형 사용", distinct_structures=3,
        band="a1", confidence="high", sample_quality="sufficient",
        summary="자기소개", feedback_for_learner="잘했어요!",
    )
    base.update(kw)
    return svc.LevelAssessment(**base)


def _call(db, member_id: int, target_language: str = "ko") -> int:
    c = Call(member_id=member_id, character_id=1, call_type="level_test",
             status="analyzing", target_language=target_language)
    db.add(c)
    db.commit()
    return c.call_id


# --------------------------------------------------------------------------- #
# ① 핵심 — 완료된 차시로 되돌아간 뒤 전체 사이클이 돈다(fable 재현 시나리오 그대로)
# --------------------------------------------------------------------------- #
def test_retest_into_a_completed_lesson_does_not_get_stuck(db):
    db, lessons, item4, item5 = db
    mid = _member(db)

    # 레벨2 회원이 no=4·5 를 freetalk_done 으로 끝내고 no=6 에 있다(fable 시나리오).
    _mark_lesson_done(db, mid, lessons["l4"].lesson_id, item4.item_id, freetalk=True)
    _mark_lesson_done(db, mid, lessons["l5"].lesson_id, item5.item_id, freetalk=True)
    mastery_repository.upsert_language_level(db, mid, "ko", 2)
    db.add(CurMemberProgress(member_id=mid, language="ko", lesson_id=lessons["l6"].lesson_id))
    db.commit()

    # 재측정 — 레벨2(같음). L2 가 포인터를 no=4 로 되돌린다.
    call_id = _call(db, mid, "ko")
    assert svc._save_level_assessment(db, call_id, mid, 2, _assessment()) is True
    db.expire_all()
    prog = repo.current_progress(db, mid, "ko")
    assert db.get(CurLesson, prog.lesson_id).no == 4

    # ⛔⛔ 결함이 있었다면 여기서부터 전부 막혀야 정상(재현) — 수정 후에는 전진해야 한다.
    assert cur.decide_course(db, mid, "ko") == "expression", "되돌아온 차시가 여전히 expression_done 으로 보인다 — 잠겨 있다"

    # no=4 표현학습 재통과(review 로 나오지만 결과는 같다) → freetalk 잠금 풀림 → 통과 → no=5.
    for lesson_key, item in (("l4", item4), ("l5", item5)):
        lesson = lessons[lesson_key]
        ec = _call(db, mid, "ko")
        o = cur.open_call(db, mid, ec, "expression", language="ko")
        assert o.lesson.lesson_id == lesson.lesson_id
        assert all(d["review"] for d in o.items), "이미 드릴된 항목이 review=True 로 안 나온다"
        cur.record_expression(db, ec, o.items, drilled_ids=[item.item_id],
                              passed_ids=[item.item_id], failed_ids=[])
        db.expire_all()
        assert cur.decide_course(db, mid, "ko") == "freetalk", f"{lesson_key} 표현학습 재통과 후 freetalk 가 안 열린다"

        fc = _call(db, mid, "ko")
        of = cur.open_call(db, mid, fc, "freetalk", language="ko")
        assert of.forced is False, "정상적으로 잠금 없이 열려야 한다"
        res = cur.complete_freetalk(db, fc, duration_s=200, normal_end=True)
        assert res is not None and res["moved"] is True, f"{lesson_key} 프리토킹 완료 후 포인터가 안 움직인다"
        db.expire_all()

    # 최종: no=6(레벨3)로 전진 + 레벨업까지 확인.
    prog = repo.current_progress(db, mid, "ko")
    assert db.get(CurLesson, prog.lesson_id).no == 6
    assert mastery_repository.get_language_level(db, mid, "ko") == 3, "레벨 경계(no=5→6)를 넘었는데 레벨이 안 올랐다"


# --------------------------------------------------------------------------- #
# ② 되돌린 범위는 목표 차시 이후만 — 이전 차시(no=1) 완료 표시는 무변경
# --------------------------------------------------------------------------- #
def test_only_lessons_at_or_after_the_target_are_reset(db):
    db, lessons, item4, item5 = db
    mid = _member(db)
    item1 = CurItem(language="ko", kind="chunk", key="c1", surface="청크1", level_no=1)
    db.add(item1)
    db.flush()
    db.add(CurLessonItem(lesson_id=lessons["l1"].lesson_id, item_id=item1.item_id, role="chunk", seq=1))
    _mark_lesson_done(db, mid, lessons["l1"].lesson_id, item1.item_id, freetalk=True)   # no=1 도 완료돼 있다
    _mark_lesson_done(db, mid, lessons["l4"].lesson_id, item4.item_id, freetalk=True)
    mastery_repository.upsert_language_level(db, mid, "ko", 2)
    db.add(CurMemberProgress(member_id=mid, language="ko", lesson_id=lessons["l5"].lesson_id))
    db.commit()

    call_id = _call(db, mid, "ko")
    svc._save_level_assessment(db, call_id, mid, 2, _assessment())  # 목표 = no=4
    db.expire_all()

    assert repo.lesson_status(db, mid, lessons["l1"].lesson_id).status == "freetalk_done", "목표 이전(no=1) 은 건드리면 안 된다"
    assert repo.lesson_status(db, mid, lessons["l4"].lesson_id).status == "learning", "목표(no=4) 는 되돌아가야 한다"


# --------------------------------------------------------------------------- #
# ③ cur_member_item 은 무변경 — review=True 확인은 ①에서 이미 겸함(추가 단정)
# --------------------------------------------------------------------------- #
def test_drilled_records_are_untouched_by_the_reset(db):
    db, lessons, item4, item5 = db
    mid = _member(db)
    _mark_lesson_done(db, mid, lessons["l4"].lesson_id, item4.item_id, freetalk=True)
    before = repo.member_item(db, mid, lessons["l4"].lesson_id, item4.item_id)
    before_drilled_at = before.drilled_at
    mastery_repository.upsert_language_level(db, mid, "ko", 2)
    db.add(CurMemberProgress(member_id=mid, language="ko", lesson_id=lessons["l5"].lesson_id))
    db.commit()

    call_id = _call(db, mid, "ko")
    svc._save_level_assessment(db, call_id, mid, 2, _assessment())
    db.expire_all()

    after = repo.member_item(db, mid, lessons["l4"].lesson_id, item4.item_id)
    assert after.drilled_at == before_drilled_at, "드릴 기록 시각이 바뀌었다 — 건드리면 안 된다"
    assert after.quiz_passed_at is not None, "통과 기록이 지워졌다"


# --------------------------------------------------------------------------- #
# ④ 레벨이 내려가는 재측정에서도 같은 방어가 걸린다
# --------------------------------------------------------------------------- #
def test_retest_down_into_a_completed_lesson_also_unlocks(db):
    db, lessons, item4, item5 = db
    mid = _member(db)
    _mark_lesson_done(db, mid, lessons["l4"].lesson_id, item4.item_id, freetalk=True)
    mastery_repository.upsert_language_level(db, mid, "ko", 3)  # 원래 레벨3
    db.add(CurMemberProgress(member_id=mid, language="ko", lesson_id=lessons["l6"].lesson_id))
    db.commit()

    call_id = _call(db, mid, "ko")
    svc._save_level_assessment(db, call_id, mid, 2, _assessment())  # 재측정 결과 레벨2 로 하락
    db.expire_all()

    prog = repo.current_progress(db, mid, "ko")
    assert db.get(CurLesson, prog.lesson_id).no == 4
    assert repo.lesson_status(db, mid, lessons["l4"].lesson_id).status == "learning"
    assert cur.decide_course(db, mid, "ko") == "expression"


# --------------------------------------------------------------------------- #
# ⑤ ja/ko 독립 — ko 재측정이 ja 차시 완료 표시를 안 건드린다
# --------------------------------------------------------------------------- #
def test_ko_retest_does_not_touch_ja_lesson_status(db):
    db, lessons, item4, item5 = db
    mid = _member(db)
    ja_lesson = repo.lesson_by_no(db, "ja", 1)
    ja_item = CurItem(language="ja", kind="chunk", key="jc1", surface="チャンク1", level_no=1)
    db.add(ja_item)
    db.flush()
    db.add(CurLessonItem(lesson_id=ja_lesson.lesson_id, item_id=ja_item.item_id, role="chunk", seq=1))
    _mark_lesson_done(db, mid, ja_lesson.lesson_id, ja_item.item_id, freetalk=True)
    _mark_lesson_done(db, mid, lessons["l4"].lesson_id, item4.item_id, freetalk=True)
    mastery_repository.upsert_language_level(db, mid, "ko", 2)
    db.add(CurMemberProgress(member_id=mid, language="ko", lesson_id=lessons["l5"].lesson_id))
    db.add(CurMemberProgress(member_id=mid, language="ja", lesson_id=ja_lesson.lesson_id))
    db.commit()

    call_id = _call(db, mid, "ko")
    svc._save_level_assessment(db, call_id, mid, 2, _assessment())
    db.expire_all()

    assert repo.lesson_status(db, mid, lessons["l4"].lesson_id).status == "learning", "ko 목표 차시는 되돌아가야 한다"
    assert repo.lesson_status(db, mid, ja_lesson.lesson_id).status == "freetalk_done", "ja 차시는 ko 재측정과 무관해야 한다"
