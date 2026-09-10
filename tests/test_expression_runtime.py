# -*- coding: utf-8 -*-
"""표현학습 런타임 회귀 — 퀴즈 판정 배선 · 진도 커밋 · 결과 화면 (외부 의존 0).

무엇을 지키나:
  ① ⛔⛔ **앵무새 관문** — 방금 비버가 말한 걸 따라 한 것은 퀴즈 통과가 **아니다**
     (이게 없으면 «따라 말하기» 가 전부 통과가 되어 아무도 퀴즈를 안 풀고 레벨이 오른다)
  ② 스스로 꺼낸 답은 통과다 · 애매한 답만 사이드카로 간다 · 완전 불일치는 LLM 0
  ③ 사이드카 결과 반영 — 서버 목록 밖 번호는 버린다 · 오답은 오답퀴즈 재료가 된다
  ④ 진도 커밋이 **단조**다(통과 시각을 덮어쓰지 않는다) · 드릴 시각은 매번 갱신
  ⑤ 결과 화면 quiz_items — 이 통화에서 드릴한 것만, 다른 콜타입은 빈 배열
  ⑥ 이어하기 관문이 expression 을 통과시키고 level_test 는 계속 막는다
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
from domains.learning.models.member_item_progress import MemberItemProgress

import domains.learning.realtime.call_session as cs
import domains.learning.service.normalcall_service as svc
from domains.learning.service.call_service import CallService

NOW = datetime.now(timezone.utc)

BYE = "안녕히 가세요"
PRICE = "이거 얼마예요?"


def _state(items: list[tuple[int, str]]) -> cs._CallState:
    st = cs._CallState()
    st.expr_items = [{"item_id": i, "obj": s, "des": None, "ex": None} for i, s in items]
    st.reground_items = [s for _, s in items]
    return st


# --------------------------------------------------------------------------- #
# ①⛔⛔ 앵무새 관문
# --------------------------------------------------------------------------- #
def test_repeating_what_the_beaver_just_said_is_not_a_pass() -> None:
    """⛔⛔ 드릴은 «또박또박 들려주고 따라 말하게» 다 — 학습자가 항목을 그대로 말하는
    순간이 통화에 수없이 많다. 그걸 통과로 세면 **아무도 퀴즈를 안 풀고 레벨이 오른다.**
    """
    st = _state([(1, BYE)])
    unknown = cs._judge_quiz_answer(st, BYE, prior_beaver=f'따라 해봐: "{BYE}"')
    assert st.expr_quiz_pass == set(), "따라 말하기가 퀴즈 통과로 셌다"
    assert unknown == []


def test_saying_it_on_your_own_is_a_pass() -> None:
    """비버가 모국어로 묻고 정답을 말하지 않았다 = 학습자가 스스로 꺼낸 것이다."""
    st = _state([(1, BYE)])
    cs._judge_quiz_answer(st, BYE, prior_beaver="How do you say goodbye to someone leaving?")
    assert st.expr_quiz_pass == {1}


def test_the_parrot_gate_looks_at_the_whole_prior_turn() -> None:
    """⚠ 비버가 문장 속에 섞어 말해도 앵무새다 — 부분 문자열로 본다."""
    st = _state([(1, BYE)])
    cs._judge_quiz_answer(st, BYE, prior_beaver=f"자, {BYE} 라고 말해 볼까? 준비됐어?")
    assert st.expr_quiz_pass == set()


def test_a_pass_is_not_re_judged(monkeypatch) -> None:
    """이미 통과한 항목은 다시 보지 않는다 — 사이드카 비용도 안 낸다."""
    st = _state([(1, BYE)])
    st.expr_quiz_pass.add(1)
    assert cs._judge_quiz_answer(st, "안녕히 계세요", prior_beaver="") == []


# --------------------------------------------------------------------------- #
# ② 세 갈래
# --------------------------------------------------------------------------- #
def test_an_unrelated_answer_costs_no_llm() -> None:
    st = _state([(1, BYE), (2, PRICE)])
    assert cs._judge_quiz_answer(st, "모르겠어요", prior_beaver="") == []
    assert st.expr_quiz_pass == set()


def test_a_near_miss_goes_to_the_sidecar() -> None:
    """⛔ `안녕히 계세요` 는 뜻이 반대다 — 코드가 판정하지 않고 넘긴다."""
    st = _state([(1, BYE), (2, PRICE)])
    unknown = cs._judge_quiz_answer(st, "안녕히 계세요", prior_beaver="")
    assert unknown == [1], "애매한 항목만 사이드카로 가야 한다"
    assert st.expr_quiz_pass == set()


def test_nothing_is_judged_outside_the_expression_course() -> None:
    """⚠ 다른 콜타입의 펌프 비용은 불린 검사 하나다(R4)."""
    st = cs._CallState()          # expr_items 가 비어 있다
    st.cur_user_text = [BYE]
    cs._judge_and_spawn_quiz(st)  # 예외 없이 즉시 되돌아간다
    assert st.expr_quiz_pass == set() and st.expr_quiz_fail == set()


# --------------------------------------------------------------------------- #
# ③ 사이드카 결과 반영
# --------------------------------------------------------------------------- #
def test_a_sidecar_pass_marks_the_item() -> None:
    st = _state([(1, BYE)])
    cs._apply_quiz_verdict(st, 1, correct=True, prior_beaver="")
    assert st.expr_quiz_pass == {1} and st.expr_quiz_fail == set()


def test_a_sidecar_fail_feeds_the_retry_quiz() -> None:
    """⭐ 오답은 **이 통화 안에서** 다시 내는 재료다(기획 ⑥)."""
    st = _state([(1, BYE)])
    cs._apply_quiz_verdict(st, 1, correct=False, prior_beaver="")
    assert st.expr_quiz_fail == {1} and st.expr_quiz_pass == set()


def test_the_parrot_gate_also_guards_the_sidecar_result() -> None:
    """⛔ 사이드카는 뜻만 본다 — «방금 들은 걸 따라 했다» 는 사정을 모른다.

    관문이 한 곳에만 있으면 다른 경로로 새어 들어온다.
    """
    st = _state([(1, BYE)])
    cs._apply_quiz_verdict(st, 1, correct=True, prior_beaver=f'"{BYE}" 따라 해봐')
    assert st.expr_quiz_pass == set()


def test_a_pass_is_never_demoted_by_a_later_fail() -> None:
    """통과가 이긴다 — 강등은 없다(D12)."""
    st = _state([(1, BYE)])
    cs._apply_quiz_verdict(st, 1, correct=True, prior_beaver="")
    cs._apply_quiz_verdict(st, 1, correct=False, prior_beaver="")
    assert st.expr_quiz_pass == {1} and st.expr_quiz_fail == set()


def test_an_unknown_item_id_is_dropped() -> None:
    """⛔ 서버 목록 밖 번호는 버린다(환각 방어 — 재접지 covered 와 같은 규율)."""
    st = _state([(1, BYE)])
    cs._apply_quiz_verdict(st, 99, correct=True, prior_beaver="")
    assert st.expr_quiz_pass == set() and st.expr_quiz_fail == set()


# --------------------------------------------------------------------------- #
# 재접지 쪽지 — 표현학습판이 나가고 일반 사이드카가 그걸 덮지 않는다
# --------------------------------------------------------------------------- #
def test_the_reground_note_carries_the_three_way_progress() -> None:
    st = _state([(1, BYE), (2, PRICE), (3, "학교에 가요")])
    st.reground_persona = ("선생님", "다정함")
    st.covered_nums = [1, 2]
    st.expr_quiz_pass.add(1)
    st.expr_quiz_fail.add(2)
    cs._arm_reground(st, "time")
    note = st.reground_reminder
    assert "이미 다룬 표현" in note and BYE in note
    assert "이미 맞힌 표현" in note
    assert "아직 틀린 표현" in note and PRICE in note, "오답 재출제 재료가 빠졌다"
    assert "다음에 다룰 표현: 학교에 가요" in note
    assert st.reground_pending is True


def test_the_generic_sidecar_never_overwrites_the_expression_note() -> None:
    """⛔ 일반 사이드카가 돌면 `build_reground_brief`(일반 판)로 쪽지를 **다시** 조립해
    표현학습 쪽지가 통째로 사라진다 — 오답 재출제 재료가 없어진다.
    """
    st = _state([(1, BYE)])
    st.reground_ctx = {"client": object(), "model": "m", "instruction": "i"}
    cs._spawn_reground_sidecar(st)          # 표현학습이면 아무 태스크도 안 뜬다
    assert not st.reground_tasks


# --------------------------------------------------------------------------- #
# ④⑤ 진도 커밋 + 결과 화면
# --------------------------------------------------------------------------- #
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
    db = session_factory()
    voice = Voice(name="Fenrir", gender="male")
    db.add(voice)
    db.flush()
    ch = Character(name="비비", role="선생님", personality="다정함",
                   voice_id=voice.voice_id, price=0)
    db.add(ch)
    db.add(Level(language="ko", level_no=1, profile="생존 회화"))
    db.flush()
    items = []
    for i, surface in enumerate((BYE, PRICE, "학교에 가요"), start=1):
        it = LearningItem(source_key=f"c{i}", language="ko", assign_rule="t",
                          kind="chunk", band=1, level_no=1, seq_no=i, surface=surface)
        db.add(it)
        db.flush()
        items.append(it)
    m = Member(language="en", korean_level=1, onboarding_completed=True,
               auth_user_id="auth-R")
    db.add(m)
    db.flush()
    call = Call(member_id=m.member_id, character_id=ch.character_id,
                call_date=NOW, status="done", call_type="expression")
    db.add(call)
    db.flush()
    db.commit()
    return {"db": db, "member_id": m.member_id, "call_id": call.call_id,
            "items": items, "call": call, "character_id": ch.character_id}


def test_progress_is_written_for_drilled_and_passed(env) -> None:
    db, ids = env["db"], [i.item_id for i in env["items"]]
    stats = svc.save_expression_progress(
        db, env["member_id"], env["call_id"],
        drilled_ids=ids[:2], passed_ids=[ids[0]],
    )
    assert stats == {"drilled": 2, "passed": 1}
    rows = {r.item_id: r for r in db.query(MemberItemProgress).all()}
    assert set(rows) == set(ids[:2]), "드릴한 것만 행이 생겨야 한다"
    assert rows[ids[0]].quiz_passed_at is not None
    assert rows[ids[1]].quiz_passed_at is None, "드릴만 한 항목은 미통과다"
    assert all(r.drilled_call_id == env["call_id"] for r in rows.values())


def test_a_pass_timestamp_is_never_overwritten(env) -> None:
    """⛔ 통과는 취소되는 사건이 아니다 — 덮어쓰면 «언제 뗐나» 를 잃는다."""
    db, iid = env["db"], env["items"][0].item_id
    svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                 drilled_ids=[iid], passed_ids=[iid])
    first = db.query(MemberItemProgress).one().quiz_passed_at
    stats = svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                         drilled_ids=[iid], passed_ids=[iid])
    db.expire_all()
    assert db.query(MemberItemProgress).one().quiz_passed_at == first
    assert stats["passed"] == 0, "이미 통과한 항목을 다시 세면 안 된다"


def test_drilled_at_is_refreshed_every_call(env) -> None:
    """⚠ 드릴 시각은 «마지막으로 꺼낸 때» 다 — 선별 정렬이 그걸 읽는다."""
    db, iid = env["db"], env["items"][0].item_id
    row = MemberItemProgress(
        member_id=env["member_id"], item_id=iid, status="introduced", score=0.0,
        drilled_at=NOW - timedelta(days=3), drilled_call_id=None,
    )
    db.add(row)
    db.commit()
    svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                 drilled_ids=[iid], passed_ids=[])
    db.expire_all()
    fresh = db.query(MemberItemProgress).one()
    # ⚠ sqlite 는 tzinfo 를 버린다(운영 Postgres 는 timestamptz 라 유지된다). 시험은 그
    #   차이를 흡수하고 «옛 시각이 아니다» 만 본다 — 여기서 재려는 것은 갱신 여부다.
    assert fresh.drilled_at.replace(tzinfo=timezone.utc) > NOW - timedelta(minutes=1)
    assert fresh.drilled_call_id == env["call_id"]


def test_the_mastery_chain_columns_are_left_alone(env) -> None:
    """⛔ 표현학습은 등급·카운터 사슬을 **쓰지 않는다**(D12) — 만지면 normal 과 섞인다."""
    db, iid = env["db"], env["items"][0].item_id
    db.add(MemberItemProgress(member_id=env["member_id"], item_id=iid,
                              status="practicing", score=2.5, repeat_count=4))
    db.commit()
    svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                 drilled_ids=[iid], passed_ids=[iid])
    db.expire_all()
    row = db.query(MemberItemProgress).one()
    assert (row.status, row.score, row.repeat_count) == ("practicing", 2.5, 4)


def test_an_empty_progress_write_is_a_no_op(env) -> None:
    svc.save_expression_progress(env["db"], env["member_id"], env["call_id"],
                                 drilled_ids=[], passed_ids=[])
    assert env["db"].query(MemberItemProgress).count() == 0


def test_the_result_screen_shows_the_quiz_outcome(env) -> None:
    db, ids = env["db"], [i.item_id for i in env["items"]]
    svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                 drilled_ids=ids[:2], passed_ids=[ids[0]])
    result = CallService(db).get_call_result(env["member_id"], env["call_id"])
    got = {q.surface: q.passed for q in result.quiz_items}
    assert got == {BYE: True, PRICE: False}
    assert result.used_items == [], "표현학습에서는 이 칸이 빈다(옛 증거 사슬을 안 쓴다)"


def test_other_call_types_have_no_quiz_items(env) -> None:
    """⛔ 다른 콜타입에서는 빈 배열이다 — 화면이 칸을 안 그린다."""
    db = env["db"]
    env["call"].call_type = "normal"
    db.commit()
    result = CallService(db).get_call_result(env["member_id"], env["call_id"])
    assert result.quiz_items == []


def test_quiz_items_only_cover_this_call(env) -> None:
    """⚠ 지난 통화에서 뗀 항목은 이번 결과가 아니다 — 기준은 «이 통화에서 드릴했나» 다."""
    db, ids = env["db"], [i.item_id for i in env["items"]]
    db.add(MemberItemProgress(
        member_id=env["member_id"], item_id=ids[2], status="introduced", score=0.0,
        quiz_passed_at=NOW - timedelta(days=2), drilled_call_id=None,
    ))
    db.commit()
    svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                 drilled_ids=[ids[0]], passed_ids=[ids[0]])
    result = CallService(db).get_call_result(env["member_id"], env["call_id"])
    assert [q.surface for q in result.quiz_items] == [BYE]


# --------------------------------------------------------------------------- #
# ⑥ 이어하기 관문
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("call_type,ok", [
    ("normal", True), ("expression", True), ("freetalk", True), ("level_test", False),
])
def test_the_resume_gate_is_a_whitelist(env, call_type: str, ok: bool) -> None:
    """⛔ 화이트리스트로 쓴다 — 새 콜타입이 생겼을 때 **기본이 «막힘»** 이어야 안전하다.

    ⚠ 레벨테스트는 계속 막는다: 조각 개념이 없다(3분 하드캡은 측정 설계다).
    """
    db = env["db"]
    env["call"].call_type = call_type
    env["call"].call_date = NOW
    db.commit()
    call_id, reason = svc.resume_call(db, env["member_id"], env["call_id"], max_fragments=3)
    assert (call_id is not None) is ok, reason
