"""B2B 교사 콘솔 회귀 테스트 — 반·명단·과제·참여 (외부 의존 0).

검증 대상:
    - 참여코드: charset 에서 I·O·0·1 제외 / 길이 6 / 중복 회피.
    - 권한: `is_teacher` 가 아니면 403 / 남의 반은 404(403 아님 — 존재를 알려주지 않는다).
    - 참여: 동의 없으면 400 / 정원 초과 409 / 만료 코드 410 / 재참여는 행을 되살린다.
    - 과제: 챕터 = seq_no 순 40개 창 / 제외 반영 / 명단 전원에게 미수행 행 선깔기 /
      보관된 반 409 / 전량 제외 400.
    - 🔴 권리: `vocab_example()` 이 grammar 항목에는 None 을 준다
      (`examples` 가 kind 에 따라 교재 예문일 수 있다 — 07_데이터출처.md).
    - 집계: 제출이 없으면 취약 문장은 빈 목록이다(숫자를 지어내지 않는다).

DB 는 인메모리 sqlite(BigInteger+Identity PK → Integer 치환 — tests/test_mastery.py 컨벤션).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.classroom.models.classroom import Classroom
from domains.classroom.models.submission import Submission
from domains.classroom.schemas.classroom import (
    AssignmentCreate,
    ClassroomCreate,
    JoinIn,
    RosterMemberUpdate,
)
from domains.classroom.service.classroom_service import (
    JOIN_CODE_ALPHABET,
    ClassroomService,
    vocab_example,
)
from domains.classroom.service.conversation_goal import (
    CONVERSATION_TARGET_N,
    conversation_target_ids,
)
from domains.learning.models.learning_item import LearningItem

NOW = datetime.now(timezone.utc)
DUE = NOW + timedelta(days=1)


@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def env(session_factory):
    """교사 1 + 학습자 3 + 1급 어휘 80개(= 챕터 2개분) + 1·2단계 문법 6개."""
    db = session_factory()
    teacher = Member(name="김서연", is_teacher=True, auth_user_id="t-1")
    learners = [Member(name=f"L{i}", is_teacher=False, auth_user_id=f"l-{i}") for i in range(3)]
    db.add_all([teacher, *learners])
    db.flush()

    for i in range(1, 81):
        db.add(
            LearningItem(
                language="ko",
                kind="vocab",
                source_key=f"v:w{i}",
                band=1,
                topik_grade=1,
                level_no=2,
                assign_rule="vocab_split_v1",
                surface=f"단어{i}",
                seq_no=i,
                is_core=(i % 3 == 0),
                examples=json.dumps([f"단어{i}를 씁니다."], ensure_ascii=False),
            )
        )
    for i in range(1, 7):
        db.add(
            LearningItem(
                language="ko",
                kind="grammar",
                source_key=f"g:basic_a:g{i}",
                band=1,
                level_no=1 + (i % 2),
                assign_rule="textbook_v1",
                surface=f"문법{i}",
                textbook_code="basic_a",
                seq_no=i,
                # 🔴 교재 예문 — 화면에 나가면 안 된다
                examples=json.dumps(["교재예문A", "교재예문B"], ensure_ascii=False),
            )
        )
    db.commit()
    return db, teacher, learners


# ── 참여코드 ─────────────────────────────────────────────────────────────
def test_join_code_excludes_confusable_chars():
    """손글씨로 옮겨 적을 때 오인되는 I·O·0·1 은 charset 에 없다."""
    for ch in "IO01":
        assert ch not in JOIN_CODE_ALPHABET
    assert len(set(JOIN_CODE_ALPHABET)) == len(JOIN_CODE_ALPHABET)


def test_created_classroom_gets_six_char_code(env):
    db, teacher, _ = env
    room = ClassroomService(db).create_classroom(
        teacher, ClassroomCreate(name="TOPIK 1급 A반", target_grade=1)
    )
    assert len(room.join_code) == 6
    assert set(room.join_code) <= set(JOIN_CODE_ALPHABET)


# ── 권한 ─────────────────────────────────────────────────────────────────
def test_learner_cannot_use_console(env):
    db, _, learners = env
    with pytest.raises(HTTPException) as e:
        ClassroomService(db).require_teacher(learners[0])
    assert e.value.status_code == 403


def test_other_teachers_classroom_is_404_not_403(env):
    """403 은 '있긴 있다'를 알려준다. 남의 반은 없는 것으로 답한다."""
    db, teacher, _ = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    other = Member(name="다른교사", is_teacher=True, auth_user_id="t-2")
    db.add(other)
    db.commit()
    with pytest.raises(HTTPException) as e:
        svc.owned(room.classroom_id, other)
    assert e.value.status_code == 404


# ── 참여 ─────────────────────────────────────────────────────────────────
def _join(svc, learner, room, name="Nguyen Mai", consent=True):
    return svc.join(
        learner, JoinIn(join_code=room.join_code, roster_name=name, share_consent=consent)
    )


def test_join_requires_consent(env):
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    with pytest.raises(HTTPException) as e:
        _join(svc, learners[0], room, consent=False)
    assert e.value.status_code == 400


def test_join_respects_capacity(env):
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(
        teacher, ClassroomCreate(name="A반", target_grade=1, capacity=1)
    )
    _join(svc, learners[0], room)
    with pytest.raises(HTTPException) as e:
        _join(svc, learners[1], room)
    assert e.value.status_code == 409


def test_expired_code_is_gone(env):
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    room.code_expires_on = (NOW - timedelta(days=1)).date()
    db.commit()
    with pytest.raises(HTTPException) as e:
        _join(svc, learners[0], room)
    assert e.value.status_code == 410


def test_rejoin_does_not_inflate_the_learner_count(env):
    """나갔다 다시 들어와도 **정원 계산이 늘지 않는다.**

    🔴 2026-08-22 동작 변경 — 예전에는 같은 행을 되살렸다(`left_at = None`).
       이탈 시 익명화(`remove_from_class`)가 `member_id` 를 비우면서
       기존 행을 찾을 수 없게 됐고, 재참여는 **새 행**이 된다.
       고정할 불변식은 행 id 가 아니라 **활동 인원 수**다.
       행 동일성은 `test_rejoin_after_leaving_creates_a_new_row` 가 반대로 고정한다.
    """
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    cm = _join(svc, learners[0], room)
    svc.remove_from_class(cm)
    again = _join(svc, learners[0], room, name="새이름")
    assert again.left_at is None
    assert again.roster_name == "새이름"
    assert svc.learner_count(room.classroom_id) == 1


def test_roster_hides_removed_learner(env):
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    cm = _join(svc, learners[0], room)
    _join(svc, learners[1], room, name="Zhang")
    svc.remove_from_class(cm)
    assert [c.roster_name for c in svc.roster(room.classroom_id)] == ["Zhang"]


def test_confirm_toggle(env):
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    cm = _join(svc, learners[0], room)
    assert cm.confirmed_at is None
    svc.update_roster_member(cm, RosterMemberUpdate(confirmed=True))
    assert cm.confirmed_at is not None
    svc.update_roster_member(cm, RosterMemberUpdate(confirmed=False))
    assert cm.confirmed_at is None


# ── 챕터·과제 ─────────────────────────────────────────────────────────────
def test_chapter_is_a_fixed_40_window(env):
    db, _, _ = env
    svc = ClassroomService(db)
    c1 = svc.chapter_items(1, 1)
    c2 = svc.chapter_items(1, 2)
    assert len(c1) == 40 and len(c2) == 40
    assert c1[0].surface == "단어1" and c1[-1].surface == "단어40"
    assert c2[0].surface == "단어41"


def test_create_assignment_seeds_not_started_for_everyone(env):
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    for i, l in enumerate(learners):
        _join(svc, l, room, name=f"R{i}")

    a = svc.create_assignment(
        room,
        AssignmentCreate(grade=1, chapter=1, activities=["speaking", "conversation"], due_at=DUE),
    )
    subs = db.query(Submission).filter(Submission.assignment_id == a.assignment_id).all()
    assert len(subs) == 3
    assert {s.status for s in subs} == {"not_started"}
    assert {s.speaking_total for s in subs} == {40}


def test_excluded_items_are_dropped_from_snapshot(env):
    db, teacher, _ = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    items = svc.chapter_items(1, 1)
    drop = [items[0].item_id, items[1].item_id]
    a = svc.create_assignment(
        room, AssignmentCreate(grade=1, chapter=1, activities=["speaking"], due_at=DUE,
                               excluded_item_ids=drop)
    )
    kept = json.loads(a.target_item_ids)
    assert len(kept) == 38
    assert not set(drop) & set(kept)


def test_cannot_exclude_every_sentence(env):
    db, teacher, _ = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    all_ids = [i.item_id for i in svc.chapter_items(1, 1)]
    with pytest.raises(HTTPException) as e:
        svc.create_assignment(
            room, AssignmentCreate(grade=1, chapter=1, activities=["speaking"], due_at=DUE,
                                   excluded_item_ids=all_ids)
        )
    assert e.value.status_code == 400


def test_archived_classroom_rejects_new_assignment(env):
    db, teacher, _ = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    svc.archive(room)
    with pytest.raises(HTTPException) as e:
        svc.create_assignment(
            room, AssignmentCreate(grade=1, chapter=1, activities=["speaking"], due_at=DUE)
        )
    assert e.value.status_code == 409


def test_assignment_snapshot_holds_only_grammar_surfaces(env):
    """🔴 문법 스냅샷에 교재 예문이 섞이면 안 된다."""
    db, teacher, _ = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    a = svc.create_assignment(
        room, AssignmentCreate(grade=1, chapter=1, activities=["speaking"], due_at=DUE)
    )
    snapshot = json.loads(a.grammar_items or "[]")
    assert all(isinstance(s, str) for s in snapshot)
    assert not any("교재예문" in s for s in snapshot)


# ── 권리 ─────────────────────────────────────────────────────────────────
def test_vocab_example_refuses_grammar_items(env):
    """`examples` 는 kind 에 따라 권리가 갈린다 — grammar 는 서울대 교재 예문이다."""
    db, _, _ = env
    grammar = db.query(LearningItem).filter(LearningItem.kind == "grammar").first()
    vocab = db.query(LearningItem).filter(LearningItem.kind == "vocab").first()
    assert vocab_example(grammar) is None
    assert vocab_example(vocab) == "단어1를 씁니다."


# ── 집계 ─────────────────────────────────────────────────────────────────
def test_weak_items_are_empty_without_submissions(env):
    """제출이 없으면 취약 문장도 없다. 숫자를 지어내지 않는다."""
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    _join(svc, learners[0], room)
    a = svc.create_assignment(
        room, AssignmentCreate(grade=1, chapter=1, activities=["speaking"], due_at=DUE)
    )
    assert svc.weak_items(a) == []
    stats = svc.assignment_stats(a)
    assert stats["completed"] == 0 and stats["total"] == 1
    assert stats["avg_speaking"] is None


def test_weak_items_fold_failed_ids(env):
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    cms = [_join(svc, l, room, name=f"R{i}") for i, l in enumerate(learners)]
    a = svc.create_assignment(
        room, AssignmentCreate(grade=1, chapter=1, activities=["speaking"], due_at=DUE)
    )
    items = svc.chapter_items(1, 1)
    hard, easy = items[0].item_id, items[1].item_id
    for cm, failed in zip(cms, ([hard, easy], [hard], [hard])):
        s = (
            db.query(Submission)
            .filter(
                Submission.assignment_id == a.assignment_id,
                Submission.classroom_member_id == cm.classroom_member_id,
            )
            .one()
        )
        s.status = "done"
        s.speaking_passed = 40 - len(failed)
        s.failed_item_ids = json.dumps(failed)
    db.commit()

    weak = svc.weak_items(a)
    assert weak[0]["item_id"] == hard
    assert weak[0]["hit"] == 0 and weak[0]["of"] == 3   # 3명 전원 미통과
    assert weak[1]["item_id"] == easy and weak[1]["hit"] == 2
    assert svc.assignment_stats(a)["avg_speaking"] == pytest.approx(39.0, abs=0.4)


# ── 통화 요약의 교사 로케일 (10 §12.7) ─────────────────────────────────────
class _FakeGenai:
    """generate_content 를 흉내내는 최소 스텁. 외부 호출 0."""

    def __init__(self, out: str | None = "(translated) hello"):
        self.out = out
        self.calls = 0

        class _Models:
            def __init__(self, outer):
                self.outer = outer

            def generate_content(self, model, contents):  # noqa: ARG002
                self.outer.calls += 1
                if self.outer.out is None:
                    raise RuntimeError("boom")
                return type("R", (), {"text": self.outer.out})()

        self.models = _Models(self)


def _character(db):
    """call.character_id 가 NOT NULL 이라 캐릭터·음색을 먼저 만든다."""
    from domains.commerce.models.character import Character
    from domains.commerce.models.voice import Voice

    ch = db.query(Character).first()
    if ch is not None:
        return ch
    voice = Voice(name="Fenrir", gender="male")
    db.add(voice)
    db.flush()
    ch = Character(name="비버", voice_id=voice.voice_id, price=0)
    db.add(ch)
    db.flush()
    return ch


def _call_with_summary(db, member, summary: str, locale: str):
    from domains.learning.models.call import Call
    from domains.learning.models.sentence import Sentence

    ch = _character(db)
    call = Call(
        member_id=member.member_id,
        character_id=ch.character_id,
        summary=summary,
        status="done",
    )
    db.add(call)
    db.flush()
    db.add(Sentence(call_id=call.call_id, korean_sentence="안녕하세요.", locale=locale))
    db.commit()
    return call


def test_summary_returns_source_when_locale_matches(env):
    from domains.classroom.service.summary_service import SummaryService

    db, _, learners = env
    call = _call_with_summary(db, learners[0], "학교 근처 극장 길을 물었습니다.", "ko")
    g = _FakeGenai()
    out = SummaryService(db, g).get(call.call_id, "ko")
    assert out["text"] == "학교 근처 극장 길을 물었습니다."
    assert out["translated"] is False
    assert g.calls == 0  # 같은 언어면 번역하지 않는다


def test_summary_translates_once_then_caches(env):
    from domains.classroom.models.call_summary_translation import CallSummaryTranslation
    from domains.classroom.service.summary_service import SummaryService

    db, _, learners = env
    call = _call_with_summary(db, learners[0], "Đã hỏi đường đến rạp chiếu phim.", "vi")
    g = _FakeGenai("Asked the way to the cinema.")
    svc = SummaryService(db, g)

    first = svc.get(call.call_id, "en")
    assert first["translated"] is True
    assert first["text"] == "Asked the way to the cinema."
    assert first["source_locale"] == "vi"
    assert g.calls == 1

    second = svc.get(call.call_id, "en")
    assert second["text"] == first["text"]
    assert g.calls == 1  # 두 번째는 캐시 — LLM 을 다시 부르지 않는다
    assert db.query(CallSummaryTranslation).count() == 1


def test_summary_degrades_without_translator(env):
    """번역기가 없으면 원문을 준다. 요약을 숨기지 않는다(R5)."""
    from domains.classroom.service.summary_service import SummaryService

    db, _, learners = env
    call = _call_with_summary(db, learners[0], "Đã hỏi đường.", "vi")
    out = SummaryService(db, None).get(call.call_id, "en")
    assert out["text"] == "Đã hỏi đường."
    assert out["translated"] is False


def test_summary_survives_translator_failure(env):
    from domains.classroom.service.summary_service import SummaryService

    db, _, learners = env
    call = _call_with_summary(db, learners[0], "Đã hỏi đường.", "vi")
    out = SummaryService(db, _FakeGenai(None)).get(call.call_id, "en")
    assert out["text"] == "Đã hỏi đường."
    assert out["translated"] is False


def test_summary_never_overwrites_the_original(env):
    """⛔ call.summary 는 학습자 것이다. 번역이 원본을 덮으면 안 된다."""
    from domains.classroom.service.summary_service import SummaryService
    from domains.learning.models.call import Call

    db, _, learners = env
    original = "Đã hỏi đường đến rạp chiếu phim."
    call = _call_with_summary(db, learners[0], original, "vi")
    SummaryService(db, _FakeGenai("Asked the way.")).get(call.call_id, "en")
    assert db.get(Call, call.call_id).summary == original


def test_summary_locale_is_clamped_to_two(env):
    """콘솔은 ko + en 2종이다. 모르는 로케일은 기본값으로 접는다."""
    from domains.classroom.service.summary_service import SummaryService

    db, _, learners = env
    call = _call_with_summary(db, learners[0], "요약", "ko")
    out = SummaryService(db, _FakeGenai()).get(call.call_id, "vi")
    assert out["translated"] is False  # vi → ko 로 접혀 원문과 같아진다


# ── 이탈 시 익명화 (개인정보처리방침 §3.4 ⑤ · §3.5 ⑤) ────────────────────


def test_leaving_erases_identifying_fields(env):
    """나가면 식별 필드가 남지 않는다. `left_at` 만 세우는 소프트 삭제가 아니다."""
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    cm = _join(svc, learners[0], room, name="Nguyen Mai")
    cm.student_no = "20251234"
    cm.teacher_alias = "마이"
    db.commit()

    svc.remove_from_class(cm)

    assert cm.left_at is not None
    assert cm.roster_name is None
    assert cm.student_no is None
    assert cm.teacher_alias is None
    # 🔴 이것이 핵심이다 — 반 기록에서 사람으로 가는 유일한 링크
    assert cm.member_id is None


def test_leaving_keeps_class_aggregates(env):
    """§3.4 ⑤ 단서 — 반 단위 통계값은 유지된다. 분모가 소급 변동하면 안 된다."""
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    members = [_join(svc, l, room, name=f"R{i}") for i, l in enumerate(learners)]
    a = svc.create_assignment(
        room,
        AssignmentCreate(grade=1, chapter=1, activities=["speaking"], due_at=DUE),
    )
    before = svc.assignment_stats(a)["total"]

    svc.remove_from_class(members[0])

    assert svc.assignment_stats(a)["total"] == before == 3


def test_rejoin_after_leaving_creates_a_new_row(env):
    """익명화된 행은 되살아나지 않는다. 새 동의 = 새 행이다."""
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    first = _join(svc, learners[0], room, name="Mai")
    first_id = first.classroom_member_id
    svc.remove_from_class(first)

    again = _join(svc, learners[0], room, name="Mai")

    assert again.classroom_member_id != first_id
    assert again.roster_name == "Mai"


# ── 제출 배선 — 회화 (`06` §6.1 · D15) ───────────────────────────────────


def _evidence(db, member, call, item_id: int, grade: str):
    from domains.learning.models.item_evidence import ItemEvidence

    db.add(
        ItemEvidence(
            member_id=member.member_id,
            # 다국어축 — `item_evidence.language` 는 NOT NULL 이다(커리큘럼과 같은 축).
            language="ko",
            item_id=item_id,
            call_id=call.call_id,
            grade_raw=grade,
            grade_final=grade,
            verified=True,
            score_delta=1.0,
        )
    )
    db.commit()


def _call(db, member):
    from domains.learning.models.call import Call

    ch = _character(db)
    call = Call(member_id=member.member_id, character_id=ch.character_id, status="done")
    db.add(call)
    db.commit()
    return call


def _conversation_setup(env, activities=("conversation",)):
    db, teacher, learners = env
    svc = ClassroomService(db)
    room = svc.create_classroom(teacher, ClassroomCreate(name="A반", target_grade=1))
    cm = _join(svc, learners[0], room, name="Mai")
    a = svc.create_assignment(
        room,
        AssignmentCreate(grade=1, chapter=1, activities=list(activities), due_at=DUE),
    )
    return db, svc, learners[0], cm, a


def _targets(a):
    return json.loads(a.target_item_ids or "[]")


def test_call_evidence_marks_the_assignment_done(env):
    from domains.classroom.service import submission_service

    db, _, learner, cm, a = _conversation_setup(env)
    targets = _targets(a)
    call = _call(db, learner)
    _evidence(db, learner, call, targets[0], "E2")
    _evidence(db, learner, call, targets[1], "E3")

    linked = submission_service.link_call(db, learner.member_id, call.call_id)
    db.commit()

    assert len(linked) == 1
    sub = db.query(Submission).filter(Submission.assignment_id == a.assignment_id).one()
    assert sub.status == "done"
    assert sub.conversation_met == 2
    # ⛔ 예전에는 여기서 `len(targets)`(=40) 를 기대했다. 과제 생성이 넣는 값과 정의가
    #    갈려 첫 통화 뒤에 분모가 바뀌고 있었다. 지금은 양쪽 다 회화 목표 수다.
    assert sub.conversation_total == CONVERSATION_TARGET_N
    assert sub.call_id == call.call_id
    assert sub.completed_at is not None


def test_conversation_denominator_does_not_change_after_the_first_call(env):
    """생성 때 넣은 분모와 통화 뒤 분모가 같아야 한다.

    갈리면 교사 화면에서 `회화 2 / 10` 이 통화 한 번에 `2 / 40` 으로 튄다.
    """
    from domains.classroom.service import submission_service

    db, _, learner, cm, a = _conversation_setup(env)
    before = (
        db.query(Submission)
        .filter(Submission.assignment_id == a.assignment_id)
        .filter(Submission.classroom_member_id == cm.classroom_member_id)
        .one()
        .conversation_total
    )
    targets = _targets(a)
    call = _call(db, learner)
    _evidence(db, learner, call, targets[0], "E3")
    submission_service.link_call(db, learner.member_id, call.call_id)
    db.commit()

    sub = (
        db.query(Submission)
        .filter(Submission.assignment_id == a.assignment_id)
        .filter(Submission.classroom_member_id == cm.classroom_member_id)
        .one()
    )
    assert before == sub.conversation_total == CONVERSATION_TARGET_N


def test_conversation_goal_is_never_zero(env):
    """핵심 0 챕터가 사라진다 — 이 산정 변경의 목적이다.

    `is_core` 로 세던 시절에는 챕터당 0~20 으로 흔들리고 0 이 나왔다.
    그 챕터에서는 회화 과제가 `0 / 0` 이 됐다.
    """
    db, _teacher, _learners = env
    items = ClassroomService(db).chapter_items(1, 1)
    # 픽스처는 3의 배수만 is_core 다 — 옛 정의로는 챕터마다 수가 흔들린다.
    assert len(conversation_target_ids(items)) == CONVERSATION_TARGET_N
    assert conversation_target_ids([]) == []
    # 항목이 N 보다 적으면 있는 만큼만. 0 이 아니다.
    assert len(conversation_target_ids(items[:4])) == 4


def test_imitation_does_not_count_as_use(env):
    """E1 = 비버가 방금 한 말을 따라한 것. 사용이 아니다 (`06` §4)."""
    from domains.classroom.service import submission_service

    db, _, learner, cm, a = _conversation_setup(env)
    call = _call(db, learner)
    _evidence(db, learner, call, _targets(a)[0], "E1")

    linked = submission_service.link_call(db, learner.member_id, call.call_id)
    db.commit()

    assert linked == []
    sub = db.query(Submission).filter(Submission.assignment_id == a.assignment_id).one()
    assert sub.status == "not_started"


def test_call_without_target_items_leaves_assignment_alone(env):
    """목표와 무관한 통화는 수행이 아니다. 출석이 아니라 산출을 센다."""
    from domains.classroom.service import submission_service

    db, _, learner, cm, a = _conversation_setup(env)
    used = set(_targets(a))
    off_target = next(i for i in range(1, 81) if i not in used)
    call = _call(db, learner)
    _evidence(db, learner, call, off_target, "E3")

    linked = submission_service.link_call(db, learner.member_id, call.call_id)
    db.commit()

    assert linked == []
    sub = db.query(Submission).filter(Submission.assignment_id == a.assignment_id).one()
    assert sub.status == "not_started"


def test_speaking_only_assignment_is_not_linked_by_a_call(env):
    """활동에 회화가 없으면 통화로 수행 처리되지 않는다."""
    from domains.classroom.service import submission_service

    db, _, learner, cm, a = _conversation_setup(env, activities=("speaking",))
    call = _call(db, learner)
    _evidence(db, learner, call, _targets(a)[0], "E3")

    assert submission_service.link_call(db, learner.member_id, call.call_id) == []


def test_closed_assignment_is_not_updated_after_the_fact(env):
    """마감 처리된 과제가 뒤에서 바뀌면 교사가 이미 본 결과가 달라진다."""
    from domains.classroom.service import submission_service

    db, svc, learner, cm, a = _conversation_setup(env)
    a.closed_at = datetime.now(timezone.utc)
    db.commit()
    call = _call(db, learner)
    _evidence(db, learner, call, _targets(a)[0], "E3")

    assert submission_service.link_call(db, learner.member_id, call.call_id) == []


def test_left_learner_is_not_linked(env):
    """이탈하면 member_id 가 NULL 이라 애초에 조회되지 않는다."""
    from domains.classroom.service import submission_service

    db, svc, learner, cm, a = _conversation_setup(env)
    call = _call(db, learner)
    _evidence(db, learner, call, _targets(a)[0], "E3")
    svc.remove_from_class(cm)

    assert submission_service.link_call(db, learner.member_id, call.call_id) == []


def test_one_call_can_satisfy_two_classes(env):
    """반 두 곳에 속한 학습자 — 통화 1건을 한쪽에만 귀속시킬 근거가 없다."""
    from domains.classroom.service import submission_service

    db, teacher, learners = env
    svc = ClassroomService(db)
    rooms = [
        svc.create_classroom(teacher, ClassroomCreate(name=f"{i}반", target_grade=1))
        for i in range(2)
    ]
    for r in rooms:
        _join(svc, learners[0], r, name="Mai")
    assignments = [
        svc.create_assignment(
            r, AssignmentCreate(grade=1, chapter=1, activities=["conversation"], due_at=DUE)
        )
        for r in rooms
    ]
    call = _call(db, learners[0])
    _evidence(db, learners[0], call, _targets(assignments[0])[0], "E3")

    linked = submission_service.link_call(db, learners[0].member_id, call.call_id)
    db.commit()

    assert len(linked) == 2


# ── 제출 배선 — 발음 (앱이 보고해야 하는 쪽) ─────────────────────────────


def test_speaking_result_fills_weak_items(env):
    """`failed_item_ids` 가 「다시 가르칠 문장」의 유일한 재료다."""
    from domains.classroom.service import submission_service

    db, svc, learner, cm, a = _conversation_setup(env, activities=("speaking",))
    failed = svc.chapter_items(1, 1)[:2]

    sub = submission_service.record_speaking(
        db, a, cm, passed=38, total=40, failed_item_ids=[i.item_id for i in failed]
    )

    assert sub.status == "done"
    assert sub.speaking_passed == 38
    assert sub.speaking_total == 40
    weak = svc.weak_items(a)
    assert {w["surface"] for w in weak} == {i.surface for i in failed}
    assert all(w["of"] == 1 for w in weak)
