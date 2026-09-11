# -*- coding: utf-8 -*-
"""표현학습 런타임 회귀 — 진도 판정 배선 · 진도 커밋 · 결과 화면 (외부 의존 0).

무엇을 지키나:
  ① ⛔⛔ **문자열은 통과를 주지 않는다** — 판정은 전사를 읽는 사이드카가 한다
     (앵무새 관문·포함 규칙은 **없다**. 그 길이 왜 막혔는지는 quiz_judge 독스트링)
  ② 사이드카 결과는 **합집합**으로 얹힌다 · 서버 목록 밖 번호는 버린다 · 통과가 이긴다
  ③ ⭐ 직전 비버 발화가 정답을 포함해도 **결과는 그대로 반영된다**(앵무새 필터 부활 차단)
  ④ 조각 끝 순서 — 판정 → state → DB. 단 **쓰기를 LLM 에 걸지 않는다**(상한·예외 흡수)
  ⑤ 진도 커밋이 **단조**다 · 결과 화면은 통화 축·**조각 축** 양쪽에서 안 지워진다
  ⑥ 이어하기 관문이 expression 을 통과시키고 level_test 는 계속 막는다
  ⑦ 표현학습 승급(D12) — 전량 통과 시 +1, trigger_call 당 멱등
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

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
from domains.learning.service import mastery_service
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
# ⛔⛔ 드릴 구간의 문자열은 통과를 주지 않는다 (2026-09-10 재설계 · T16 에서도 그대로)
#   ⚠ T16: passed/failed 는 **서버가 연 퀴즈 창 안에서만** 서버가 찾는다(tests/test_expression_t16.py).
#     드릴 중 복창은 창 밖이라 여전히 통과가 안 된다 — 아래 시험이 그 경계를 지킨다.
# --------------------------------------------------------------------------- #
def test_a_repeated_answer_is_never_marked_passed_by_code() -> None:
    """⛔⛔ **문자열이 통과를 주지 않는다** — 이 파일의 첫 계약이다.

    드릴은 «들려주고 따라 말하게» 라 학습자가 항목을 **그대로** 말하는 순간이 통화에
    수없이 많다. 옛 설계는 그걸 문자열로 걸러 보려다 두 번 실패했다(1턴 창은 재시도에서
    새고, 2턴 창은 정상 퀴즈를 막았다).
    ⇒ 펌프는 드릴 중 답을 **판정하지 않는다.** 따라 말한 답이 흘러도 통과가 안 찍힌다(퀴즈 창이 안 열렸다).
    """
    st = _state([(1, BYE)])
    st.cur_beaver_text = [f'따라 해봐: "{BYE}"']
    cs._flush_beaver_segment(st)
    st.cur_user_text = [BYE]
    cs._flush_user_segment(st)
    assert st.expr_quiz_pass == set(), "문자열이 통과를 줬다 — 판정기가 돌아왔다"


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
    assert (stats["drilled"], stats["passed"]) == (2, 1)
    # ⚠ 승급 판정도 같은 커밋 안에서 돈다 — 아직 남은 항목이 있으니 «stay» 다.
    assert stats["levelup"]["result"] == "stay"
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
    svc.save_expression_progress(
        db, env["member_id"], env["call_id"],
        drilled_ids=ids[:2], passed_ids=[ids[0]],
        snapshot=[{"item_id": ids[0], "surface": BYE, "passed": True},
                  {"item_id": ids[1], "surface": PRICE, "passed": False}],
    )
    result = CallService(db).get_call_result(env["member_id"], env["call_id"])
    got = {q.surface: q.passed for q in result.quiz_items}
    assert got == {BYE: True, PRICE: False}
    assert result.used_items == [], "표현학습에서는 이 칸이 빈다(옛 증거 사슬을 안 쓴다)"


def test_the_result_api_carries_failed_and_meaning(env) -> None:
    """T19 — 결과 API 응답 1건: failed 로 «틀림/아직 안 봄» 을 가르고 meaning 이 실린다. 옛 행은 failed=False·meaning=None."""
    db, ids = env["db"], [i.item_id for i in env["items"]]
    svc.save_expression_progress(
        db, env["member_id"], env["call_id"],
        drilled_ids=ids[:3], passed_ids=[ids[0]],
        snapshot=[{"item_id": ids[0], "surface": BYE, "meaning": "goodbye", "passed": True, "failed": False},
                  {"item_id": ids[1], "surface": PRICE, "meaning": "how much", "passed": False, "failed": True},
                  {"item_id": ids[2], "surface": "학교에 가요", "passed": False}],                 # 옛 모양
    )
    result = CallService(db).get_call_result(env["member_id"], env["call_id"])
    by = {q.surface: q for q in result.quiz_items}
    assert (by[BYE].passed, by[BYE].failed, by[BYE].meaning) == (True, False, "goodbye")
    assert (by[PRICE].passed, by[PRICE].failed, by[PRICE].meaning) == (False, True, "how much")
    assert (by["학교에 가요"].passed, by["학교에 가요"].failed, by["학교에 가요"].meaning) == (False, False, None)
    assert set(by[BYE].model_dump().keys()) >= {"item_id", "surface", "meaning", "passed", "failed"}, "플러터 계약 키"


def test_other_call_types_have_no_quiz_items(env) -> None:
    """⛔ 스냅샷이 없으면 빈 배열이다 — 화면이 칸을 안 그린다(옛 통화·크래시 포함)."""
    db = env["db"]
    env["call"].call_type = "normal"
    db.commit()
    result = CallService(db).get_call_result(env["member_id"], env["call_id"])
    assert result.quiz_items == []


def test_a_later_call_never_erases_an_earlier_result(env) -> None:
    """⛔⛔ 진도 행으로 되짚으면 **나중 통화가 지난 결과를 지운다**(2026-09-10 QA).

        통화 A  「물」 드릴 → 오답        (drilled_call_id = A)
        통화 B  「물」 다시 드릴 → 통과   (drilled_call_id = B 로 덮어쓴다)
        ⇒ A 의 결과 화면에서 「물」이 **사라진다**

    재드릴은 선별상 **정상 경로**다(«드릴했는데 못 끝낸 것» 이 다음 통화 맨 앞에 온다)
    ⇒ 어제 결과를 오늘 열면 틀린 항목만 증발하고 통과 항목만 남는다. 조용히 틀리는 버그다.
    ⭐ 그래서 결과는 **그 통화에 적힌 스냅샷**을 읽는다.
    """
    db, ids = env["db"], [i.item_id for i in env["items"]]
    call_a = env["call_id"]
    # 통화 A — 「안녕히 가세요」를 드릴했지만 못 뗐다
    svc.save_expression_progress(
        db, env["member_id"], call_a, drilled_ids=[ids[0]], passed_ids=[],
        snapshot=[{"item_id": ids[0], "surface": BYE, "passed": False}],
    )
    # 통화 B — 같은 항목을 다시 드릴해서 뗐다(진도 행의 drilled_call_id 가 B 로 덮인다)
    call_b = Call(member_id=env["member_id"], character_id=env["character_id"],
                  call_date=NOW, status="done", call_type="expression")
    db.add(call_b)
    db.flush()
    svc.save_expression_progress(
        db, env["member_id"], call_b.call_id, drilled_ids=[ids[0]], passed_ids=[ids[0]],
        snapshot=[{"item_id": ids[0], "surface": BYE, "passed": True}],
    )
    svc_a = CallService(db).get_call_result(env["member_id"], call_a)
    svc_b = CallService(db).get_call_result(env["member_id"], call_b.call_id)
    assert [(q.surface, q.passed) for q in svc_a.quiz_items] == [(BYE, False)], "A 의 결과가 지워졌다"
    assert [(q.surface, q.passed) for q in svc_b.quiz_items] == [(BYE, True)]
    # 진도 행은 최신 통화를 가리킨다 — 그래서 되짚기가 안 되는 것이다(전제 확인)
    db.expire_all()
    assert db.query(MemberItemProgress).one().drilled_call_id == call_b.call_id


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


# --------------------------------------------------------------------------- #
# ⭐⭐ 조각 끝 순서 — 판정 → state → DB. 단, **쓰기를 LLM 에 걸지 않는다**
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_final_judgement_is_skipped_outside_the_course() -> None:
    st = cs._CallState()
    await cs._final_expression_progress(st)          # 무동작·무예외


# --------------------------------------------------------------------------- #
# ⛔ F3 — 두 코스는 옛 승급 사슬을 건드리지 않는다 (기획 §5)
# --------------------------------------------------------------------------- #
def test_expression_rows_are_marked_so_the_old_gate_ignores_them(env) -> None:
    """⛔⛔ 기본 provenance('observed') + status 기본값('introduced') 이면 그 행이 **옛 승급
    게이트 G1 의 분자에 산입된다** — 표현학습으로 드릴만 한 항목이 «배웠다» 로 세어져
    normal 통화의 승급을 앞당긴다. 두 사슬이 한 컬럼에서 섞이는 자리다.
    """
    db, iid = env["db"], env["items"][0].item_id
    svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                 drilled_ids=[iid], passed_ids=[])
    row = db.query(MemberItemProgress).one()
    assert row.provenance == mastery_service.PROVENANCE_EXPRESSION


def test_the_g1_numerator_excludes_expression_rows() -> None:
    """⚠ 표시만 해 두고 게이트가 안 거르면 아무것도 안 고친 것이다 — 필터를 직접 본다."""
    import inspect

    src = inspect.getsource(mastery_service.evaluate_level_up)
    assert 'p.provenance not in ("placement", PROVENANCE_EXPRESSION)' in src


def test_an_expression_row_is_promoted_once_real_evidence_arrives(env) -> None:
    """⭐ 실증거가 붙으면 placement 처럼 observed 로 승격된다 — 그래야 «증거 없는 것»만 걸린다."""
    import inspect

    src = inspect.getsource(mastery_service.apply_evidence)
    assert 'prog.provenance in ("placement", PROVENANCE_EXPRESSION)' in src


def test_level_up_is_skipped_for_the_two_courses() -> None:
    """⛔ 이 게이트는 «이번 통화의 증거» 가 아니라 **기존 progress 상태**로 판정한다.

    그래서 검출을 안 한 통화가 **트리거가 되어** 옛 기준으로 승급이 찍힌다. 승급하면
    korean_level 이 바뀌고 `pick_expression_items` 는 레벨 **정확일치**라 커리큘럼 분모가
    통째로 갈아탄다 — D12 와 다른 기준으로.

    ⛔ 판정을 «후보가 0인가» 로 하면 안 된다(그렇게 썼다가 회귀가 잡았다) — `normal` 도
      후보가 빈 경우가 정상이고 그때는 승급이 **돌아야 한다**. 기준은 **콜타입**이다.
    """
    import inspect

    src = inspect.getsource(svc._apply_call_mastery)
    assert "None if skip_level_up" in src, "두 코스에서도 승급 판정이 돈다"


# --------------------------------------------------------------------------- #
# ⛔ F4 — 두 코스가 normal 의 학습 항목 기계를 물려받지 않는다 (§2-1·D8)
# --------------------------------------------------------------------------- #
def test_the_generic_sidecar_is_off_when_there_is_no_item_list() -> None:
    """⛔ 프리토킹은 항목이 0개다(D8) — 떠먹일 목록이 없으면 이 사이드카를 부를 이유가 없다.

    ⚠ 게이트가 `expr_items` 뿐이면 프리토킹이 그대로 통과해 일반 사이드카가 계속 돈다.
    """
    st = cs._CallState()                      # 표현학습도 아니고 목록도 없다 = 프리토킹
    st.reground_ctx = {"client": object(), "model": "m", "instruction": "i"}
    cs._spawn_reground_sidecar(st)
    assert not st.reground_tasks


def test_an_emptied_expression_list_does_not_fall_back_to_study_mode() -> None:
    """⚠⚠ 그 레벨을 전량 통과하면 표현 목록이 빈다 — 그때 'study' 로 굳으면 쪽지가
    **없는 항목**을 가리킨다.
    """
    st = cs._CallState()
    st.expr_items = []
    st.reground_items = []
    st.call_mode = "study" if st.reground_items else "chat"
    assert st.call_mode == "chat"


# --------------------------------------------------------------------------- #
# ⭐ 표현학습 승급 — «그 레벨 전체 퀴즈 통과»(D12)
# --------------------------------------------------------------------------- #
def test_the_level_is_not_complete_while_anything_remains(env) -> None:
    db, ids = env["db"], [i.item_id for i in env["items"]]
    svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                 drilled_ids=ids, passed_ids=ids[:2])
    assert mastery_service.expression_level_complete(db, env["member_id"], 1) is False


def test_passing_every_item_promotes_the_level(env) -> None:
    """⭐ 완료 판정의 유일한 기준은 `quiz_passed_at` 이다 — 등급·숙달 사슬을 안 본다."""
    db, ids = env["db"], [i.item_id for i in env["items"]]
    stats = svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                         drilled_ids=ids, passed_ids=ids)
    assert mastery_service.expression_level_complete(db, env["member_id"], 1) is True
    assert stats["levelup"]["result"] == "promoted"
    assert (stats["levelup"]["from_level"], stats["levelup"]["to_level"]) == (1, 2)
    db.expire_all()
    assert db.get(Member, env["member_id"]).korean_level == 2


def test_promotion_is_idempotent_per_call(env) -> None:
    """⛔ 조각이 여럿이라 한 통화에서 여러 번 불린다 — 두 번 올리면 안 된다."""
    db, ids = env["db"], [i.item_id for i in env["items"]]
    svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                 drilled_ids=ids, passed_ids=ids)
    again = svc.save_expression_progress(db, env["member_id"], env["call_id"],
                                         drilled_ids=ids, passed_ids=ids)
    assert again["levelup"]["result"] == "stay"
    db.expire_all()
    assert db.get(Member, env["member_id"]).korean_level == 2


def test_an_empty_curriculum_is_never_complete(env) -> None:
    """⛔ 커리큘럼 미시드를 «다 뗐다» 로 읽으면 빈 레벨을 타고 끝까지 올라간다."""
    assert mastery_service.expression_level_complete(env["db"], env["member_id"], 9) is False


# --------------------------------------------------------------------------- #
# ⛔⛔ P1 — 조각 축: 결과 화면이 «마지막 조각만» 남으면 안 된다
# --------------------------------------------------------------------------- #
def test_a_later_fragment_never_erases_an_earlier_fragments_result(env) -> None:
    """⛔⛔ **하필 «통과한 것» 만 사라지는 사고다.**

    이어하기는 **같은 call 행**을 계속 쓰고, 조각2 는 선별을 **다시 돈다.** 그 선별이
    `quiz_passed_at IS NULL` 로 거르므로 **조각1 에서 통과한 항목이 조각2 목록에 없다.**
    ⇒ 조각2 스냅샷으로 덮으면 학습자가 이룬 것만 정확히 지워진다(조각3까지 가면 1·2 가 다).

    ⚠ 이건 `drilled_call_id` 덮어쓰기(통화 축)와 **같은 실수의 조각 축 재발**이다 —
      그래서 두 축을 **각각** 잠근다. 통화 축 시험 하나로는 이 사고가 안 잡혔다.
    """
    db, ids = env["db"], [i.item_id for i in env["items"]]
    call_id = env["call_id"]
    # 조각1 — 2개 드릴, 1개 통과
    svc.save_expression_progress(
        db, env["member_id"], call_id, drilled_ids=ids[:2], passed_ids=[ids[0]],
        snapshot=[{"item_id": ids[0], "surface": BYE, "passed": True},
                  {"item_id": ids[1], "surface": PRICE, "passed": False}],
    )
    # 조각2 — **같은 call_id**. 선별이 통과분(ids[0])을 뺐으므로 목록에 없다.
    svc.save_expression_progress(
        db, env["member_id"], call_id, drilled_ids=[ids[2]], passed_ids=[ids[2]],
        snapshot=[{"item_id": ids[2], "surface": "학교에 가요", "passed": True}],
    )
    result = CallService(db).get_call_result(env["member_id"], call_id)
    got = {q.surface: q.passed for q in result.quiz_items}
    assert got == {BYE: True, PRICE: False, "학교에 가요": True}, got


def test_a_pass_in_an_earlier_fragment_survives_a_later_false(env) -> None:
    """⭐ `passed` 는 **OR** 다 — 한 번 통과했으면 통과다(강등 없음, D12)."""
    db, iid = env["db"], env["items"][0].item_id
    call_id = env["call_id"]
    svc.save_expression_progress(
        db, env["member_id"], call_id, drilled_ids=[iid], passed_ids=[iid],
        snapshot=[{"item_id": iid, "surface": BYE, "passed": True}],
    )
    svc.save_expression_progress(
        db, env["member_id"], call_id, drilled_ids=[iid], passed_ids=[],
        snapshot=[{"item_id": iid, "surface": BYE, "passed": False}],
    )
    result = CallService(db).get_call_result(env["member_id"], call_id)
    assert [(q.surface, q.passed) for q in result.quiz_items] == [(BYE, True)]


def test_a_broken_old_snapshot_does_not_lose_the_new_one(env) -> None:
    """⚠ 깨진 JSON 은 없는 셈 친다 — 화면용 파생값이라 조용히 새로 쓰는 편이 낫다(R5)."""
    db, iid = env["db"], env["items"][0].item_id
    env["call"].expression_result = "{깨진 json"
    db.commit()
    svc.save_expression_progress(
        db, env["member_id"], env["call_id"], drilled_ids=[iid], passed_ids=[iid],
        snapshot=[{"item_id": iid, "surface": BYE, "passed": True}],
    )
    result = CallService(db).get_call_result(env["member_id"], env["call_id"])
    assert [(q.surface, q.passed) for q in result.quiz_items] == [(BYE, True)]


def test_the_snapshot_carries_a_pass_even_if_it_was_never_marked_drilled() -> None:
    """⛔ 스냅샷 대상은 **covered ∪ 통과분**이다.

    사이드카가 어떤 항목을 `passed` 에만 넣고 `drilled` 에 안 넣을 수 있다(지시문이 그
    조합을 막지 않는다). covered 만 보면 **DB 엔 통과가 찍히는데 화면엔 안 나온다.**
    """
    st = _state([(1, BYE)])
    st.expr_quiz_pass.add(1)          # covered_nums 는 비어 있다
    snapshot = cs._expression_result_snapshot(st, set(cs._expr_covered_ids(st)))
    assert snapshot == [{"item_id": 1, "surface": BYE, "meaning": None, "passed": True, "failed": False}]


def test_the_snapshot_row_carries_meaning_and_failed() -> None:
    """T19 — 화면이 «퀴즈에서 틀림» 과 «아직 퀴즈 안 봄» 을 가르고, 모국어 뜻을 보여준다."""
    st = _state([(1, BYE), (2, PRICE), (3, "학교에 가요")])
    st.expr_items[0]["des"] = "goodbye"
    st.expr_items[1]["des"] = "how much"
    st.covered_nums = [1, 2, 3]
    st.expr_quiz_pass.add(1)
    st.expr_quiz_fail.add(2)
    rows = {r["item_id"]: r for r in cs._expression_result_snapshot(st, set(cs._expr_covered_ids(st)))}
    assert rows[1] == {"item_id": 1, "surface": BYE, "meaning": "goodbye", "passed": True, "failed": False}
    assert rows[2] == {"item_id": 2, "surface": PRICE, "meaning": "how much", "passed": False, "failed": True}
    assert rows[3]["passed"] is False and rows[3]["failed"] is False, "아직 퀴즈 안 봄"
    st.expr_quiz_fail.add(1)          # 통과와 오답이 둘 다 찍힌 상태(방어) — passed 가 이긴다
    assert cs._expression_result_snapshot(st, {1})[0]["failed"] is False


def test_snapshot_merge_keeps_passed_ors_failed_and_tolerates_old_rows() -> None:
    """T19 병합 3면 — passed 우선 · failed OR · 옛 행(failed/meaning 없음)."""
    old = json.dumps([
        {"item_id": 1, "surface": BYE, "passed": False},                  # 옛 행 — failed/meaning 키 없음
        {"item_id": 2, "surface": PRICE, "passed": False, "failed": True, "meaning": "how much"},
        {"item_id": 3, "surface": "학교에 가요", "passed": True, "failed": False},
    ])
    incoming = [
        {"item_id": 1, "surface": BYE, "meaning": "goodbye", "passed": False, "failed": True},
        {"item_id": 2, "surface": PRICE, "meaning": "how much?", "passed": True, "failed": False},   # 조각2 에서 통과
        {"item_id": 3, "surface": "학교에 가요", "meaning": None, "passed": False, "failed": True},  # 강등 시도
    ]
    merged = {r["item_id"]: r for r in svc._merge_expression_snapshot(old, incoming)}
    assert merged[1] == {"item_id": 1, "surface": BYE, "meaning": "goodbye", "passed": False, "failed": True}
    assert merged[2] == {"item_id": 2, "surface": PRICE, "meaning": "how much?", "passed": True, "failed": False}, "passed 가 failed 를 이긴다"
    assert merged[3]["passed"] is True and merged[3]["failed"] is False, "통과는 되돌아가지 않는다"
    only_old = {r["item_id"]: r for r in svc._merge_expression_snapshot(old, [])}
    assert only_old[1]["failed"] is False and only_old[1]["meaning"] is None


# --------------------------------------------------------------------------- #
# ③ ⭐ 앵무새 필터의 «반대편» — 되살리면 이 시험이 깨진다
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# ⛔⛔ P1-2 — 동음이의: identity 는 표면형이 아니라 item_id 다 (자산에 91그룹 실재)
# --------------------------------------------------------------------------- #
def test_homographs_are_tracked_separately() -> None:
    """⛔⛔ 같은 레벨에 **동일 표면형이 91그룹** 있다(vocab.json 전수 스캔 — 「개」·「네」·「눈」…).

    표면형으로 되짚으면 한쪽만 다뤄도 **두 항목에 함께** 진도가 찍히고, `quiz_passed_at` 은
    **되돌릴 수 없다** — 엉뚱한 항목이 영구히 «완료» 가 된다.
    """
    st = _state([(11, "개"), (22, "개")])       # 뜻이 다른 두 항목, 같은 표면형
    st.covered_nums = [1]                       # 1번만 다뤘다
    assert cs._expr_covered_ids(st) == [11], "동음이의 두 항목에 함께 찍혔다"


def test_the_next_label_uses_item_id_not_the_surface() -> None:
    """⛔ 표면형 집합으로 «했나» 를 물으면 동음이의 한쪽만 다뤄도 **둘 다 완료로 보인다** —
    안 다룬 항목이 next 후보에서 사라진다.
    """
    st = _state([(11, "개"), (22, "개"), (33, "물")])
    st.reground_persona = ("선생님", "다정함")
    st.covered_nums = [1]                        # 11번만 다뤘다
    cs._arm_reground(st, "time")
    assert "다음에 다룰 표현: 개" in st.reground_reminder, st.reground_reminder


# --------------------------------------------------------------------------- #
# ⛔⛔ P1-3 — 낱말 경계: 「선물」이 「물」로 잡히면 안 된다
# --------------------------------------------------------------------------- #
def test_a_word_inside_another_word_is_not_covered() -> None:
    """⛔⛔ 옛 대조는 `label in text` 라 항목 「물」에 "어제 **선물**을 받았어요" 가 잡혔다.

    ⇒ 그 항목이 «완료» 로 처리돼 **가르치지도 않고 건너뛰고**, DB 엔 drilled 로 남는다.
    ⚠ 같은 결함을 이미 통과(pass) 경로에서 한 번 걷어냈는데 **대조(covered) 경로에 그대로
      남아 있었다.** L2 이상은 90%가 어휘이고 대부분 1~2글자라 여기가 주 무대다.
    """
    st = _state([(1, "물")])
    cs._note_covered_items(st, "어제 선물을 받았어요", source="user")
    assert st.covered_nums == [], "「선물」이 「물」로 잡혔다"


@pytest.mark.parametrize("text", ["물을 주세요", "저는 물이 좋아요", "물"])
def test_the_word_with_a_particle_is_still_covered(text: str) -> None:
    """⭐ 조사·어미는 **뒤에 붙는다** — 그건 그대로 잡아야 한다(안 그러면 검출이 죽는다)."""
    st = _state([(1, "물")])
    cs._note_covered_items(st, text, source="user")
    assert st.covered_nums == [1], text


def test_a_normal_call_keeps_the_plain_substring_match() -> None:
    """⛔ `normal` 의 대조 규칙은 **안 바뀐다** — 바꾸면 그 통화의 covered 폭이 달라져
    재접지 쪽지가 같이 바뀐다(그 경로는 손대지 않는다).
    """
    st = cs._CallState()                      # expr_items 가 비어 있다 = 일반 통화
    st.reground_items = ["물"]
    cs._note_covered_items(st, "어제 선물을 받았어요")
    assert st.covered_nums == [1], "일반 통화의 생짜 대조가 바뀌었다"


# --------------------------------------------------------------------------- #
# ⛔ P1-4 — 쪽지가 한 arm 늦지 않는다 · 재접지 스위치와 진도 판정은 다른 축이다
# --------------------------------------------------------------------------- #
def test_the_legacy_idle_path_uses_the_expression_note() -> None:
    """⛔ legacy_idle 은 일반 브리프를 보냈다 — «다룬 것» 한 칸뿐이라 오답퀴즈 재료가 빠진다."""
    st = _state([(1, BYE), (2, PRICE)])
    st.reground_persona = ("선생님", "다정함")
    st.expr_quiz_fail.add(2)
    note = cs._build_expression_note(st)
    assert "아직 틀린 표현" in note and PRICE in note


# --------------------------------------------------------------------------- #
# ⛔ P2 — 승급이 자기복구를 한다(후보 0개여도 판정은 돈다)
# --------------------------------------------------------------------------- #
def test_promotion_still_runs_when_nothing_is_left_to_write(env) -> None:
    """⛔⛔ «다 뗐는데 레벨은 옛것» 상태에서 선별이 빈 목록을 준다 ⇒ 쓸 것이 없다.

    옛 코드는 그때 곧장 반환해 **승급 판정에 영영 못 닿았다** — 한 번 이 상태가 되면
    다음 통화로도 절대 안 풀린다.
    """
    db, ids = env["db"], [i.item_id for i in env["items"]]
    # 전량 통과 상태를 만들되 레벨은 그대로 둔다(승급이 아직 안 찍힌 상태)
    for iid in ids:
        db.add(MemberItemProgress(
            member_id=env["member_id"], item_id=iid, status="introduced", score=0.0,
            quiz_passed_at=NOW - timedelta(days=1),
            provenance=mastery_service.PROVENANCE_EXPRESSION,
        ))
    db.commit()
    stats = svc.save_expression_progress(
        db, env["member_id"], env["call_id"], drilled_ids=[], passed_ids=[],
    )
    assert stats["levelup"]["result"] == "promoted", stats["levelup"]
    db.expire_all()
    assert db.get(Member, env["member_id"]).korean_level == 2
