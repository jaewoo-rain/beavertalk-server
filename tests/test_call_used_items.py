"""통화 결과의 「이번 통화에서 쓴 표현」 — `CallResult.used_items`.

⭐ 왜 생겼나(2026-09-04). 「학습한 표현」(`sentences`)은 분석 지시문이 asked·corrected·
  drilled 셋으로만 정의한다. 자유대화를 매끄럽게 하면 셋 다 해당이 없어 결과 화면이
  통째로 빈다 — 실측 call 1294 는 학습자가 한국어로 잘 말했는데 표현 0개였다.
  그런데 같은 통화에서 체크판은 `가다`(E3)·`좋다`(E2) 를 잡아 두고 있었다.
  **데이터는 있고 화면이 안 쓰던 것**이다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과 — FK 가 성립해야 한다)
from domains.learning.models.call import Call
from domains.learning.models.item_evidence import ItemEvidence
from domains.learning.models.learning_item import LearningItem
from domains.learning.service.call_service import CallService


@pytest.fixture
def db():
    # BigInteger PK 는 sqlite 에서 자동증가가 안 된다 — 다른 검사들과 같은 처리를 쓴다.
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


def _seed(db, grades_and_quotes, *, member_id: int = 1, call_id: int = 1):
    db.add(Call(call_id=call_id, member_id=member_id, character_id=1, status="done"))
    seeded: set[int] = set()
    for n, (item_id, grade, quote, verified) in enumerate(grades_and_quotes):
        if item_id in seeded:
            _add_evidence(db, n + 1, member_id, item_id, call_id, grade, quote, verified)
            continue
        seeded.add(item_id)
        db.add(
            LearningItem(
                item_id=item_id,
                language="ko",
                kind="vocab",
                surface=f"낱말{item_id}",
                topik_grade=1,
                source_key=f"v:낱말{item_id}00",
                band="A",
                level_no=1,
                assign_rule="seed",
            )
        )
        _add_evidence(db, n + 1, member_id, item_id, call_id, grade, quote, verified)
    db.commit()


def _add_evidence(db, evidence_id, member_id, item_id, call_id, grade, quote, verified):
    db.add(
        ItemEvidence(
            evidence_id=evidence_id,
            member_id=member_id,
            language="ko",
            item_id=item_id,
            call_id=call_id,
            grade_raw=grade,
            grade_final=grade,
            learner_quote=quote,
            verified=verified,
        )
    )


def test_스스로_쓴_항목만_담는다(db):
    """E1(모방)·F(실패)·미검증은 빼고 E2·E3 만 남는다."""
    _seed(
        db,
        [
            (10, "E3", "오늘 학교에 갔어요.", True),
            (11, "E2", "어 좋아해?", True),
            (12, "E1", "예, 친구 만났어요.", True),   # 따라 말한 것
            (13, "F", "틀린 말", True),
            (14, "E3", "검증 안 됨", False),
        ],
    )

    got = CallService(db)._used_items(member_id=1, call_id=1)

    assert [i.item_id for i in got] == [10, 11]
    assert [i.surface for i in got] == ["낱말10", "낱말11"]
    assert got[0].quote == "오늘 학교에 갔어요."


def test_같은_항목은_한_번만_나온다(db):
    """E3 는 통화당 2건까지 허용된다 — 화면에 같은 낱말이 두 번 뜨면 세어 보게 된다."""
    _seed(db, [(10, "E3", "첫 번째", True), (10, "E3", "두 번째", True)])

    got = CallService(db)._used_items(member_id=1, call_id=1)

    assert len(got) == 1
    assert got[0].quote == "첫 번째"   # 처음 것을 남긴다


def test_남의_통화_증거는_안_섞인다(db):
    _seed(db, [(10, "E3", "내 말", True)])
    db.add(
        ItemEvidence(
            evidence_id=99, member_id=2, language="ko", item_id=10,
            call_id=1, grade_raw="E3", grade_final="E3",
            learner_quote="남의 말", verified=True,
        )
    )
    db.commit()

    got = CallService(db)._used_items(member_id=1, call_id=1)

    assert [i.quote for i in got] == ["내 말"]


def test_인용이_없으면_비운다(db):
    """지어내지 않는다."""
    _seed(db, [(10, "E3", None, True)])

    got = CallService(db)._used_items(member_id=1, call_id=1)

    assert got[0].quote is None
