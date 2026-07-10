"""P2 통화 시작 선별 회귀 테스트 — mastery_repository 선별 + load_call_setup (외부 의존 0).

검증 대상:
    - pick_study_items: L1 구성(청크 3+어휘 1, 문법 0) / 초급 구성(복습≤2+문법 1+어휘
      나머지, 본편 5+예비 5 전부 vocab) / SEL2·SEL3 제외(F 는 즉시 재출제) /
      미소화 introduced 이월 선두 / 브리지 믹스(복습 확대+이전 레벨 우선).
    - bridge_or_struggle_ratio: 진입 후 증거통화 <3 → 0.7(신규·승급 직후) / 기본 0.3.
      (D15 — "유효통화" 폐지: 통화 수 파생값은 item_evidence 의 distinct call 기반.)
    - promotion_pending: gate_promotion 직후 True → 증거통화 1회 후 False.
    - pick_chat_targets / known_grammar: 유도 5(practicing 3+mastered 최고령+최근
      introduced) / 아는 문법 = practicing·mastered grammar 만.
    - load_call_setup: 시드 있으면 study_items(persona 스키마)/known_items/candidates
      (injected 포함, 상한 30)/promotion_notice 채움. learning_item 0행이면 전부
      None(R5) / korean_level 미확정이면 needs_level_test=True + 재료 None.

기대값 소스: 스크래치패드 smoke_p2c2.py(수동 점검 — 30/30 PASS 확인 후 이식).
DB 는 인메모리 sqlite(BigInteger+Identity PK → Integer 치환 — 기존 테스트 컨벤션).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.item_evidence import ItemEvidence
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
from domains.learning.models.member_item_progress import MemberItemProgress
from domains.learning.models.member_level_history import MemberLevelHistory

import domains.learning.service.normalcall_service as svc
from domains.learning.repository import mastery_repository as repo

NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 인메모리 DB (BigInteger+Identity PK 는 sqlite 에서 autoincrement 안 되므로 Integer 로 치환)
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
    """smoke_p2c2 시드 이식 — 커리큘럼 + 회원 3명(A 신규 L1 / B L2 진행중 / C 승급 직후).

    items: L1 청크 4(c1~c4) + core 어휘 8(v1~v8) / L2 문법 3(g1~g3) + core 어휘
    8(w1~w8) + SEL2 대상(w_sel2, rank 0) + non-core(w_noncore).
    B: placement(10일 전) + 증거통화 3(각 통화에 증거 1+ — D15) + practicing(v2·w1·g3)
       + mastered(v3) + introduced 이월(g1 성공 쿨다운 / g2 직전 F) + SEL2(w_sel2).
    C: gate_promotion(1일 전) + 이후 증거통화 0.
    """
    db = session_factory()
    voice = Voice(name="Fenrir", gender="male")
    db.add(voice)
    db.flush()
    ch = Character(name="비비", role="친근한 선생님", personality="다정함",
                   voice_id=voice.voice_id, price=0)
    db.add(ch)
    db.add(Level(level_no=1, profile="생존 회화 프로파일"))
    db.add(Level(level_no=2, profile="초급 A 프로파일"))
    db.flush()

    items: dict[str, LearningItem] = {}

    def add_item(key, **kw):
        it = LearningItem(source_key=key, assign_rule="test_v1", **kw)
        db.add(it)
        db.flush()
        items[key] = it
        return it

    # L1: 청크 4 + core 어휘 8
    for i in range(1, 5):
        add_item(f"c{i}", kind="chunk", band=1, level_no=1, seq_no=i,
                 surface=f"청크{i} 주세요", explanation=f"청크{i} 설명")
    for i in range(1, 9):
        add_item(f"v{i}", kind="vocab", band=1, level_no=1, topik_grade=1, is_core=True,
                 priority_rank=i, surface=f"단어v{i}",
                 gen_examples=f'["단어v{i} 예문이에요."]',
                 meanings='{"en": "meaning-v%d"}' % i)
    # L2: 문법 3 + core 어휘 8 + SEL2 대상 + non-core 1
    for i in range(1, 4):
        add_item(f"g{i}", kind="grammar", band=1, level_no=2, textbook_code="basic_a",
                 seq_no=i, surface=f"-문법{i}",
                 examples=f'["문법{i} 예문을 봤어요."]', explanation=f"문법{i} 설명")
    for i in range(1, 9):
        add_item(f"w{i}", kind="vocab", band=1, level_no=2, topik_grade=1, is_core=True,
                 priority_rank=i, surface=f"단어w{i}",
                 gen_examples=f'["단어w{i} 예문이에요."]')
    add_item("w_sel2", kind="vocab", band=1, level_no=2, topik_grade=1, is_core=True,
             priority_rank=0, surface="선발화단어", gen_examples='["선발화단어 예문."]')
    add_item("w_noncore", kind="vocab", band=1, level_no=2, topik_grade=1, is_core=False,
             surface="논코어단어")

    # 회원
    mA = Member(language="en", korean_level=1, onboarding_completed=True,
                auth_user_id="auth-A")
    mB = Member(language="en", korean_level=2, onboarding_completed=True,
                auth_user_id="auth-B")
    mC = Member(language="en", korean_level=2, onboarding_completed=True,
                auth_user_id="auth-C")
    db.add_all([mA, mB, mC])
    db.flush()

    # B: 진입 10일 전 placement + 통화 3(아래에서 통화마다 증거 1+ 적립 → 증거통화 3)
    db.add(MemberLevelHistory(member_id=mB.member_id, from_level=None, to_level=2,
                              reason="placement", created_at=NOW - timedelta(days=10)))
    calls = []
    for d in (3, 2, 1):
        c = Call(member_id=mB.member_id, character_id=ch.character_id,
                 call_date=NOW - timedelta(days=d), status="done")
        db.add(c)
        db.flush()
        calls.append(c)
    last_call = calls[-1]

    def prog(member, key, status, **kw):
        p = MemberItemProgress(
            member_id=member.member_id, item_id=items[key].item_id,
            status=status, score=kw.pop("score", 1.0),
            first_seen_at=kw.pop("first_seen_at", NOW - timedelta(days=9)),
            last_seen_at=NOW - timedelta(days=1), **kw)
        db.add(p)
        return p

    # practicing 3(복습/유도 풀): v2(가장 오래됨) → w1 → g3
    prog(mB, "v2", "practicing", last_used_at=NOW - timedelta(days=8))
    prog(mB, "w1", "practicing", last_used_at=NOW - timedelta(days=5))
    prog(mB, "g3", "practicing", last_used_at=NOW - timedelta(days=2))
    # mastered 최고령(유도 리텐션 슬롯)
    prog(mB, "v3", "mastered", mastered_at=NOW - timedelta(days=9), score=3.5)
    # introduced 이월: g1(직전 통화 성공 → SEL3 쿨다운) / g2(직전 통화 F → 즉시 재출제)
    prog(mB, "g1", "introduced")
    prog(mB, "g2", "introduced")
    # SEL2: 자발 1+ && 오류 0 && 최근 14일 사용 introduced(최근 7일 내 유도 후보이기도)
    prog(mB, "w_sel2", "introduced", spontaneous_count=1, miss_count=0,
         last_used_at=NOW - timedelta(days=2), first_seen_at=NOW - timedelta(days=2))

    def ev(member, key, call, grade):
        db.add(ItemEvidence(member_id=member.member_id, item_id=items[key].item_id,
                            call_id=call.call_id, grade_raw=grade, grade_final=grade,
                            verified=True, created_at=call.call_date))

    # D15: 각 통화에 증거 1+ → B 의 증거통화 = 3 (브리지 창 탈출).
    # calls[0]·calls[1]은 L1 항목만 — 현재 레벨(L2) 증거가 없어 개별 버벅임 판정이지만,
    # last_call 의 w1 E2(현재 레벨 성공)가 "3연속"을 파탄 → 정상 믹스 0.3.
    ev(mB, "v2", calls[0], "E1")
    ev(mB, "v3", calls[1], "E2")
    ev(mB, "g1", last_call, "E2")   # SEL3 쿨다운 대상(성공·무F)
    ev(mB, "g2", last_call, "F")    # F → 즉시 재출제
    ev(mB, "w1", last_call, "E2")   # 현재 레벨 E2+ → 버벅임 3연속 파탄(정상 믹스)

    # C: 승급 직후(gate_promotion 최신, 이후 증거통화 0)
    db.add(MemberLevelHistory(member_id=mC.member_id, from_level=1, to_level=2,
                              reason="gate_promotion", created_at=NOW - timedelta(days=1)))
    db.commit()

    yield {"db": db, "items": items, "ch": ch, "mA": mA, "mB": mB, "mC": mC}
    db.close()


# --------------------------------------------------------------------------- #
# (8) bridge_or_struggle_ratio
# --------------------------------------------------------------------------- #
def test_bridge_ratio_under_three_evidence_calls_is_070(env):
    """진입 후 증거통화 <3 → 0.7 — 신규(A)와 승급 직후(C) 모두 (D15)."""
    db = env["db"]
    assert repo.bridge_or_struggle_ratio(db, env["mA"].member_id) == 0.7
    assert repo.bridge_or_struggle_ratio(db, env["mC"].member_id) == 0.7


def test_bridge_ratio_normal_mix_is_030(env):
    """증거통화 3 + 최근 통화에 현재 레벨 E2+(버벅임 파탄) → 정상 믹스 0.3."""
    assert repo.bridge_or_struggle_ratio(env["db"], env["mB"].member_id) == 0.3


# --------------------------------------------------------------------------- #
# (9) promotion_pending
# --------------------------------------------------------------------------- #
def test_promotion_pending_true_then_false_after_evidence_call(env):
    """gate_promotion 직후 True → 이후 증거통화 1회면 False. placement(B)는 항상 False."""
    db, mC = env["db"], env["mC"]
    assert repo.promotion_pending(db, mC.member_id) is True
    assert repo.promotion_pending(db, env["mB"].member_id) is False

    # 승급 후 첫 증거통화(증거 1+ 적립)가 쌓이면 멘트 대상에서 빠진다 (D15)
    c = Call(member_id=mC.member_id, character_id=env["ch"].character_id,
             call_date=NOW, status="done")
    db.add(c)
    db.flush()
    db.add(ItemEvidence(member_id=mC.member_id, item_id=env["items"]["w1"].item_id,
                        call_id=c.call_id, grade_raw="E1", grade_final="E1",
                        verified=True, created_at=NOW))
    db.commit()
    assert repo.promotion_pending(db, mC.member_id) is False


# --------------------------------------------------------------------------- #
# (7) pick_study_items
# --------------------------------------------------------------------------- #
def test_pick_study_items_survival_l1(env):
    """L1(A): 본편 = 청크 3(seq 순) + 어휘 1, 문법 0 / 예비 5 = 전부 어휘 다음 순번."""
    db = env["db"]
    pa = repo.pick_study_items(db, env["mA"].member_id, 1,
                               review_slots=3, bridge_prev_ratio=0.7)
    main = [e for e in pa if e["slot"] == "main"]
    reserve = [e for e in pa if e["slot"] == "reserve"]

    assert [e["study_kind"] for e in main] == ["chunk"] * 3 + ["vocab"], \
        [(e["slot"], e["study_kind"]) for e in pa]
    assert [e["item"].surface for e in main[:3]] == \
        ["청크1 주세요", "청크2 주세요", "청크3 주세요"]
    assert main[3]["item"].surface == "단어v1"
    assert [e["study_kind"] for e in reserve] == ["vocab"] * 5
    assert [e["item"].surface for e in reserve] == [f"단어v{i}" for i in range(2, 7)]


def test_pick_study_items_beginner_mix_and_sel_filters(env):
    """초급(B, 정상 믹스): 복습 2+문법 1+어휘 2 / SEL3(g1 성공 쿨다운)·SEL2(w_sel2)
    제외, F(g2)는 즉시 재출제 / 예비 5 = core 어휘 다음 순번(non-core 미출현)."""
    db = env["db"]
    pb = repo.pick_study_items(db, env["mB"].member_id, 2,
                               review_slots=2, bridge_prev_ratio=0.3)
    main = [e for e in pb if e["slot"] == "main"]
    reserve = [e for e in pb if e["slot"] == "reserve"]

    assert [e["study_kind"] for e in main] == \
        ["review", "review", "grammar", "vocab", "vocab"], \
        [(e["study_kind"], e["item"].surface) for e in main]
    # 복습 = last_used 오래된 순 v2 → w1
    assert [e["item"].surface for e in main[:2]] == ["단어v2", "단어w1"]
    # 문법 = g2 (SEL3: g1 은 직전 성공 쿨다운 제외 / g2 는 직전 F → 즉시 재출제·이월 선두)
    assert [e["item"].surface for e in main if e["study_kind"] == "grammar"] == ["-문법2"]
    # 어휘 = SEL2(선발화단어 rank 0) 제외, priority 순 w2 → w3
    assert [e["item"].surface for e in main if e["study_kind"] == "vocab"] == \
        ["단어w2", "단어w3"]
    # 예비 = w4~w8 (core 만 — non-core '논코어단어' 미출현)
    assert [e["item"].surface for e in reserve] == [f"단어w{i}" for i in range(4, 9)]
    assert all(e["item"].is_core for e in pb if e["item"].kind == "vocab")


def test_pick_study_items_bridge_mix_prefers_previous_level(env):
    """브리지 믹스(0.7·복습 3): 복습 확대 + 이전 레벨(v2) 선두."""
    db = env["db"]
    pb2 = repo.pick_study_items(db, env["mB"].member_id, 2,
                                review_slots=3, bridge_prev_ratio=0.7)
    main = [e for e in pb2 if e["slot"] == "main"]
    assert [e["study_kind"] for e in main[:3]] == ["review"] * 3, \
        [(e["study_kind"], e["item"].surface) for e in main]
    assert main[0]["item"].surface == "단어v2"  # 이전 레벨(L1) 우선
    assert {e["item"].surface for e in main[:3]} == {"단어v2", "단어w1", "-문법3"}


def test_carryover_introduced_leads_new_pool(env):
    """미소화 introduced 이월분이 신규 풀 선두 — priority 상위(v1)보다 먼저."""
    db, mA = env["db"], env["mA"]
    db.add(MemberItemProgress(
        member_id=mA.member_id, item_id=env["items"]["v3"].item_id,
        status="introduced", score=0.25, provenance="observed",
        first_seen_at=NOW - timedelta(days=1), last_seen_at=NOW - timedelta(days=1),
    ))
    db.commit()

    pa = repo.pick_study_items(db, mA.member_id, 1, review_slots=1, bridge_prev_ratio=0.3)
    main_vocab = [e["item"].surface for e in pa
                  if e["slot"] == "main" and e["study_kind"] == "vocab"]
    assert main_vocab == ["단어v3"], main_vocab  # 이월 선두(priority 3 이지만 최우선)
    reserve = [e["item"].surface for e in pa if e["slot"] == "reserve"]
    assert reserve[0] == "단어v1"  # 이월 소진 후엔 priority 순


# --------------------------------------------------------------------------- #
# 대화 유도 / 아는 문법 (load_call_setup known_items 재료)
# --------------------------------------------------------------------------- #
def test_pick_chat_targets_and_known_grammar(env):
    """유도 5 = practicing 3(오래된 순) + mastered 최고령 + 최근 7일 introduced /
    아는 문법 = practicing·mastered grammar 만."""
    db, mB = env["db"], env["mB"]
    targets = repo.pick_chat_targets(db, mB.member_id, 2)
    assert [t.surface for t in targets] == \
        ["단어v2", "단어w1", "-문법3", "단어v3", "선발화단어"], \
        [t.surface for t in targets]
    assert repo.known_grammar(db, mB.member_id) == ["-문법3"]


# --------------------------------------------------------------------------- #
# (10) load_call_setup
# --------------------------------------------------------------------------- #
def test_load_call_setup_full_materials(env):
    """시드 있으면 study_items/known_items/candidates(injected·상한 30)/promotion_notice."""
    db, mB, ch = env["db"], env["mB"], env["ch"]
    setup = svc.load_call_setup(db, mB.member_id, ch.character_id)

    # study_items — persona 스키마 {slot, kind, obj, ex, des}
    # + P2.5 확장 {item_id, roman}(teaching_plan 카드 재료 — persona 는 미사용 키 무시)
    si = setup["study_items"]
    assert si is not None and set(si[0]) == {
        "slot", "kind", "obj", "ex", "des", "item_id", "roman"
    }
    assert si[2]["kind"] == "grammar" and si[2]["obj"] == "-문법2"
    assert si[2]["ex"] == "문법2 예문을 봤어요." and si[2]["des"] == "문법2 설명"
    assert isinstance(si[2]["item_id"], int) and si[2]["roman"] is None  # 문법엔 roman 없음

    # known_items — {grammar, targets(obj/ex/hint)}
    ki = setup["known_items"]
    assert ki is not None and ki["grammar"] == ["-문법3"]
    assert len(ki["targets"]) == 5
    assert all(set(t) == {"obj", "ex", "hint"} for t in ki["targets"])
    assert ki["targets"][0]["obj"] == "단어v2" and ki["targets"][0]["hint"]

    # candidates — 주입(공부 10+유도 dedup)=injected True + 기본 후보 병합, 상한 30
    cands = setup["candidates"]
    assert cands is not None and len(cands) <= 30
    picked = repo.pick_study_items(db, mB.member_id, 2, review_slots=2,
                                   bridge_prev_ratio=0.3)
    targets = repo.pick_chat_targets(db, mB.member_id, 2)
    picked_ids = {e["item"].item_id for e in picked} | {t.item_id for t in targets}
    inj_ids = {c["item_id"] for c in cands if c["injected"]}
    assert inj_ids == picked_ids, (inj_ids, picked_ids)
    non_inj = [c for c in cands if not c["injected"]]
    assert non_inj and all(c["item_id"] not in inj_ids for c in non_inj)

    # 승급 직후 아님 + 통화 요약 없음
    assert setup["promotion_notice"] is False
    assert setup["recent_topics"] is None
    assert setup["needs_level_test"] is False


def test_load_call_setup_promotion_notice_for_promoted_member(env):
    """승급 직후(C): promotion_notice=True + 브리지여도 study_items 정상(신규만)."""
    setup = svc.load_call_setup(env["db"], env["mC"].member_id, env["ch"].character_id)
    assert setup["promotion_notice"] is True
    assert setup["study_items"] is not None


def test_load_call_setup_level_unset_returns_no_materials(env):
    """korean_level 미확정: needs_level_test=True + 재료 전부 None(레벨테스트 라우팅)."""
    db = env["db"]
    mD = Member(language="en", korean_level=None, onboarding_completed=True,
                auth_user_id="auth-D")
    db.add(mD)
    db.commit()
    setup = svc.load_call_setup(db, mD.member_id, env["ch"].character_id)
    assert setup["needs_level_test"] is True
    assert setup["study_items"] is None
    assert setup["known_items"] is None
    assert setup["candidates"] is None
    assert setup["promotion_notice"] is False


def test_load_call_setup_without_learning_items_returns_none(session_factory):
    """learning_item 0행(커리큘럼 미시드): 재료 전부 None — 기존 프롬프트 폴백(R5)."""
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="선생님", personality="다정",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.add(Level(level_no=2, profile="초급 A 프로파일"))
        member = Member(language="en", korean_level=2, onboarding_completed=True,
                        auth_user_id="auth-empty")
        db.add(member)
        db.commit()

        setup = svc.load_call_setup(db, member.member_id, ch.character_id)
        assert setup["study_items"] is None
        assert setup["known_items"] is None
        assert setup["candidates"] is None
        assert setup["promotion_notice"] is False
        assert setup["needs_level_test"] is False
        assert setup["level_profile"] == "초급 A 프로파일"  # 통화 자체는 정상 진행 재료
    finally:
        db.close()
