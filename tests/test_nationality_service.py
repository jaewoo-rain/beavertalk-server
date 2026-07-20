"""nationality_service.record_and_recompute 단위 테스트 (외부 호출 0, 인메모리 sqlite).

검증 대상:
    ① 1회 예측 → SpeakCountry top3·percent 정확(분모=보유 이력 1 → 희석 없음).
    ② 5회 초과 → FIFO 로 최근 5개만 유지(가장 오래된 것 삭제).
    ③ 나라별 prob 평균 — 없는 회차 0, 분모=보유 5(country 이름 기준 묶음).
    ④ 같은 call_id 재호출 → 이력 중복 없음(멱등) + 최신 predictions 로 재계산.
    ⑤ speak_country 기존 존재 → 그 행 UPDATE + 링크(speak_country_id) 유지.
    ⑥ predictions 빈/이상 dict → no-op(이력·억양 무변화).

DB 는 인메모리 sqlite(BigInteger+Identity PK → Integer 치환 — tests/test_mastery.py
컨벤션). call FK 는 sqlite 기본 미강제라 Call 행 없이 임의 call_id 사용.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.account.models.nationality_prediction import NationalityPrediction
from domains.account.models.speak_country import SpeakCountry
from domains.account.service import nationality_service as svc


# --------------------------------------------------------------------------- #
# 인메모리 DB (BigInteger+Identity PK 는 sqlite autoincrement 불가 → Integer 치환)
# --------------------------------------------------------------------------- #
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
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _member(db) -> Member:
    m = Member(email="a@b.c")
    db.add(m)
    db.commit()
    return m


def _preds(*pairs: tuple[str, float], top1: str | None = None) -> dict:
    """{'predictions':[{country,iso,prob}], 'top1':...} 원응답 형태 조립."""
    body = {
        "predictions": [
            {"country": c, "iso": c[:2].upper(), "prob": p} for c, p in pairs
        ]
    }
    if top1 is not None:
        body["top1"] = top1
    return body


def _count(db, member_id: int) -> int:
    return db.scalar(
        select(func.count())
        .select_from(NationalityPrediction)
        .where(NationalityPrediction.member_id == member_id)
    )


def _speak_country(db, member_id: int) -> SpeakCountry | None:
    m = db.get(Member, member_id)
    return m.speak_country


# --------------------------------------------------------------------------- #
# ① 1회 예측 → top3·percent (분모=보유 이력 1)
# --------------------------------------------------------------------------- #
def test_single_prediction_sets_speak_country(db):
    m = _member(db)
    svc.record_and_recompute(
        db, m.member_id, 1, _preds(("South Korea", 0.8), ("Japan", 0.2))
    )

    sc = _speak_country(db, m.member_id)
    assert sc is not None
    assert m.speak_country_id == sc.speak_country_id  # 링크됨
    # 분모=보유 이력 1: 0.8/1 → 80, 0.2/1 → 20 (초기 희석 없음)
    assert (sc.first_country, sc.first_percent) == ("South Korea", 80)
    assert (sc.second_country, sc.second_percent) == ("Japan", 20)
    assert sc.third_country is None and sc.third_percent is None
    assert _count(db, m.member_id) == 1


# --------------------------------------------------------------------------- #
# ② FIFO 5-cap
# --------------------------------------------------------------------------- #
def test_fifo_keeps_latest_five(db):
    m = _member(db)
    for call_id in range(1, 8):  # 7회
        svc.record_and_recompute(
            db, m.member_id, call_id, _preds(("South Korea", 0.5))
        )

    assert _count(db, m.member_id) == 5  # 최근 5개만
    remaining = set(
        db.scalars(
            select(NationalityPrediction.call_id).where(
                NationalityPrediction.member_id == m.member_id
            )
        ).all()
    )
    assert remaining == {3, 4, 5, 6, 7}  # 가장 오래된 1·2 삭제


# --------------------------------------------------------------------------- #
# ③ 나라별 평균 — 없는 회차 0, 분모 5
# --------------------------------------------------------------------------- #
def test_average_absent_rounds_count_as_zero(db):
    m = _member(db)
    rows = [
        _preds(("A", 0.5), ("C", 0.9)),  # C 는 이 회차만
        _preds(("A", 0.5), ("B", 0.4)),
        _preds(("A", 0.5), ("B", 0.4)),
        _preds(("A", 0.5)),
        _preds(("A", 0.5)),
    ]
    for i, body in enumerate(rows, start=1):
        svc.record_and_recompute(db, m.member_id, i, body)

    sc = _speak_country(db, m.member_id)
    # A: 0.5*5/5=0.5→50 | C: 0.9/5=0.18→18 | B: (0.4+0.4)/5=0.16→16
    assert (sc.first_country, sc.first_percent) == ("A", 50)
    assert (sc.second_country, sc.second_percent) == ("C", 18)
    assert (sc.third_country, sc.third_percent) == ("B", 16)


# --------------------------------------------------------------------------- #
# ④ 같은 call_id 재호출 → 멱등(중복 없음) + 최신값 재계산
# --------------------------------------------------------------------------- #
def test_same_call_id_is_idempotent(db):
    m = _member(db)
    svc.record_and_recompute(db, m.member_id, 100, _preds(("Japan", 0.9)))
    svc.record_and_recompute(db, m.member_id, 100, _preds(("Vietnam", 0.7)))

    assert _count(db, m.member_id) == 1  # 이력 중복 없음
    row = db.scalar(
        select(NationalityPrediction).where(
            NationalityPrediction.call_id == 100
        )
    )
    assert row.predictions[0]["country"] == "Vietnam"  # 최신값으로 update
    sc = _speak_country(db, m.member_id)
    assert sc.first_country == "Vietnam"  # 재계산 반영
    assert sc.first_percent == 70  # 0.7/1=0.70 (보유 이력 1)


# --------------------------------------------------------------------------- #
# ⑤ 기존 speak_country → UPDATE + 링크 유지
# --------------------------------------------------------------------------- #
def test_existing_speak_country_updated_in_place(db):
    m = _member(db)
    sc0 = SpeakCountry(first_country="Old", first_percent=99)
    db.add(sc0)
    db.flush()
    m.speak_country = sc0
    db.commit()
    original_id = sc0.speak_country_id

    svc.record_and_recompute(db, m.member_id, 1, _preds(("Thailand", 0.5)))

    m2 = db.get(Member, m.member_id)
    assert m2.speak_country_id == original_id  # 링크 유지(교체 아님)
    assert m2.speak_country.first_country == "Thailand"  # 같은 행 UPDATE
    assert m2.speak_country.first_percent == 50  # 0.5/1=0.50 (보유 이력 1)
    # speak_country 행이 새로 생기지 않았다
    assert db.scalar(select(func.count()).select_from(SpeakCountry)) == 1


# --------------------------------------------------------------------------- #
# ⑥ 빈/이상 predictions → no-op
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [{}, {"predictions": []}, {"predictions": None}, None])
def test_empty_predictions_noop(db, bad):
    m = _member(db)
    svc.record_and_recompute(db, m.member_id, 1, bad)

    assert _count(db, m.member_id) == 0
    assert db.get(Member, m.member_id).speak_country_id is None
