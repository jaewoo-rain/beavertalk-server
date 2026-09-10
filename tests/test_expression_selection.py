# -*- coding: utf-8 -*-
"""표현학습 선별 회귀 — `pick_expression_items` + `load_expression_items` (외부 의존 0).

무엇을 지키나(기획 §2-4 · D4·D14):
  ① ⛔ **퀴즈를 통과한 항목은 절대 안 나온다** — 완료 판정의 유일한 기준이 quiz_passed_at 이다
  ② 드릴만 하고 못 끝낸 것이 **앞**에 온다(사장님: "못한 거 + 그다음 것까지 해서 18개")
  ③ 개수 n 이 **변수**다(18 을 바꾸면 그대로 따라온다)
  ④ 풀이 n 보다 적으면 **짧게** 준다(빈 리스트 아님 — R5)
  ⑤ 레벨은 **정확일치**다(<= 아님) — 승급 분모가 그 레벨 항목 집합이어야 하므로
  ⑥ 언어 스코프 · DTO 스키마(청크는 예문 없이도 실린다)

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
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
from domains.learning.models.member_item_progress import MemberItemProgress

import domains.learning.service.normalcall_service as svc
from domains.learning.repository import mastery_repository as repo

NOW = datetime.now(timezone.utc)


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
    """L1 청크 12 + L2 어휘 3 + 다른 언어 1. 회원 1명(L1).

    ⚠ 청크는 `examples` 가 **없다**(실측: L1 생존 청크는 전 언어에서 예문 0개).
      그 상태로도 목록에 실려야 한다 — 청크는 표면형 자체가 문장이다.
    """
    db = session_factory()
    db.add(Level(language="ko", level_no=1, profile="생존 회화"))
    db.add(Level(language="ko", level_no=2, profile="초급 A"))
    db.add(Level(language="ja", level_no=1, profile="ja L1"))
    db.flush()

    items: dict[str, LearningItem] = {}

    def add(key, **kw):
        it = LearningItem(source_key=key, language=kw.pop("language", "ko"),
                          assign_rule="test_v1", **kw)
        db.add(it)
        db.flush()
        items[key] = it
        return it

    for i in range(1, 13):
        add(f"c{i}", kind="chunk", band=1, level_no=1, seq_no=i,
            surface=f"청크{i} 주세요")
    for i in range(1, 4):
        add(f"w{i}", kind="vocab", band=1, level_no=2, topik_grade=1, is_core=True,
            priority_rank=i, surface=f"단어w{i}",
            gen_examples=f'["단어w{i} 예문이에요."]',
            meanings='{"en": "meaning-w%d"}' % i)
    add("ja1", kind="chunk", band=1, level_no=1, seq_no=1,
        surface="ja 청크", language="ja")

    m = Member(language="en", korean_level=1, onboarding_completed=True,
               auth_user_id="auth-E")
    db.add(m)
    db.flush()
    db.commit()
    return {"db": db, "items": items, "member_id": m.member_id}


def _prog(env, key, **kw):
    db, items = env["db"], env["items"]
    db.add(MemberItemProgress(
        member_id=env["member_id"], item_id=items[key].item_id,
        status=kw.pop("status", "introduced"), score=0.0, **kw,
    ))
    db.commit()


def _pick(env, **kw):
    return repo.pick_expression_items(env["db"], env["member_id"], 1, **kw)


# --------------------------------------------------------------------------- #
# ① 통과분 제외 — 완료의 유일한 기준
# --------------------------------------------------------------------------- #
def test_items_that_passed_the_quiz_never_come_back(env) -> None:
    """⛔ 이게 깨지면 학습자가 이미 맞힌 표현을 영원히 다시 본다."""
    _prog(env, "c1", quiz_passed_at=NOW - timedelta(days=1))
    _prog(env, "c2", quiz_passed_at=NOW)
    picked = {i.source_key for i in _pick(env, n=12)}
    assert "c1" not in picked and "c2" not in picked
    assert len(picked) == 10  # 남은 12 - 2


def test_a_drilled_but_unfinished_item_still_comes_back(env) -> None:
    """드릴만 하고 퀴즈를 못 넘긴 것은 **여전히 풀에 있다** — 완료가 아니다."""
    _prog(env, "c3", drilled_at=NOW - timedelta(days=1))
    assert "c3" in {i.source_key for i in _pick(env, n=12)}


# --------------------------------------------------------------------------- #
# ② 미완이 앞으로
# --------------------------------------------------------------------------- #
def test_unfinished_drills_are_ordered_first(env) -> None:
    """사장님: "못한 거 + 그다음 것까지 해서 18개" — 정렬 한 줄이 그걸 만든다.

    ⚠ 뒤쪽은 random() 이라 순서를 못 박는다. **앞의 3자리**만 본다 — 그게 계약이다.
    """
    for key in ("c5", "c9", "c11"):
        _prog(env, key, drilled_at=NOW - timedelta(days=1))
    picked = [i.source_key for i in _pick(env, n=6)]
    assert set(picked[:3]) == {"c5", "c9", "c11"}, picked


def test_a_passed_item_is_excluded_even_if_it_was_drilled(env) -> None:
    """⛔ 정렬(드릴)이 필터(통과)를 이기면 안 된다 — 통과분은 어떤 경우에도 안 나온다."""
    _prog(env, "c4", drilled_at=NOW - timedelta(days=1), quiz_passed_at=NOW)
    assert "c4" not in {i.source_key for i in _pick(env, n=12)}


# --------------------------------------------------------------------------- #
# ③④ 개수는 변수 · 풀이 모자라면 짧게
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n", [1, 3, 7, 12])
def test_the_count_is_a_variable(env, n: int) -> None:
    assert len(_pick(env, n=n)) == n


def test_the_default_count_is_the_module_constant(env) -> None:
    """⛔ 18 을 손으로 적지 마라 — 상수 하나가 소유한다."""
    assert repo.EXPRESSION_ITEMS_PER_CALL == 18
    assert repo.EXPRESSION_QUIZ_GROUP == 3
    # 풀(12) < 기본값(18) → 짧게 준다
    assert len(_pick(env)) == 12


def test_a_short_pool_gives_a_short_list_not_an_empty_one(env) -> None:
    """R5 — 재료가 모자라도 통화는 산다."""
    for i in range(1, 12):
        _prog(env, f"c{i}", quiz_passed_at=NOW)
    picked = _pick(env, n=18)
    assert len(picked) == 1 and picked[0].source_key == "c12"


def test_an_exhausted_pool_gives_an_empty_list(env) -> None:
    """전부 통과한 회원은 빈 리스트다 — 그게 «이 레벨을 다 했다» 의 신호다(레벨업 재료)."""
    for i in range(1, 13):
        _prog(env, f"c{i}", quiz_passed_at=NOW)
    assert _pick(env, n=18) == []


def test_zero_or_negative_count_short_circuits(env) -> None:
    assert _pick(env, n=0) == [] and _pick(env, n=-5) == []


# --------------------------------------------------------------------------- #
# ⑤⑥ 스코프
# --------------------------------------------------------------------------- #
def test_the_level_is_an_exact_match_not_a_range(env) -> None:
    """⛔ `<=` 로 바꾸지 마라. 표현학습의 승급은 «그 레벨 전체 통과»(D12)라, 이전 레벨을
    섞으면 분모가 흐려진다. 복습 선별(`_pick_review_items`)과 **반대 방향**이다.
    """
    picked = {i.source_key for i in _pick(env, n=18)}
    assert picked and not any(k.startswith("w") for k in picked)
    # 그리고 L2 로 부르면 L2 항목만 온다
    l2 = repo.pick_expression_items(env["db"], env["member_id"], 2, n=18)
    assert {i.source_key for i in l2} == {"w1", "w2", "w3"}


def test_other_languages_are_out_of_scope(env) -> None:
    assert "ja1" not in {i.source_key for i in _pick(env, n=18)}
    ja = repo.pick_expression_items(env["db"], env["member_id"], 1, language="ja", n=18)
    assert [i.source_key for i in ja] == ["ja1"]


def test_another_members_progress_does_not_leak(env) -> None:
    """⛔ 조인 조건에서 member_id 가 빠지면 남의 통과분이 내 풀을 깎는다."""
    db = env["db"]
    other = Member(language="en", korean_level=1, onboarding_completed=True,
                   auth_user_id="auth-other")
    db.add(other)
    db.flush()
    db.add(MemberItemProgress(
        member_id=other.member_id, item_id=env["items"]["c1"].item_id,
        status="introduced", score=0.0, quiz_passed_at=NOW,
    ))
    db.commit()
    assert "c1" in {i.source_key for i in _pick(env, n=18)}


# --------------------------------------------------------------------------- #
# ⑥ DTO — 프롬프트 스키마와 1:1
# --------------------------------------------------------------------------- #
def test_the_dto_matches_the_prompt_schema(env) -> None:
    rows = svc.load_expression_items(env["db"], env["member_id"], 1, "en", "ko", n=3)
    assert len(rows) == 3
    for r in rows:
        assert set(r) == {"item_id", "obj", "des", "ex", "quiz_passed"}
        assert r["quiz_passed"] is False, "선별이 이미 통과분을 뺐다 — 항상 False 로 나간다"
        assert r["obj"].startswith("청크")
        # ⚠ L1 청크는 예문이 없다. 그 상태로도 실려야 한다(프롬프트가 꼬리를 생략한다).
        assert r["ex"] is None


def test_the_dto_carries_meaning_and_example_when_the_item_has_them(env) -> None:
    rows = svc.load_expression_items(env["db"], env["member_id"], 2, "en", "ko", n=3)
    assert {r["obj"] for r in rows} == {"단어w1", "단어w2", "단어w3"}
    assert all(r["ex"] and r["des"] for r in rows)


def test_the_dto_is_empty_when_the_pool_is(env) -> None:
    """R5 — 호출부가 «빈 표현학습» 을 만나도 통화는 열린다."""
    for i in range(1, 13):
        _prog(env, f"c{i}", quiz_passed_at=NOW)
    assert svc.load_expression_items(env["db"], env["member_id"], 1, "en", "ko") == []
