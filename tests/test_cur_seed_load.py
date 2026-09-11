"""cur_* 시드 로더 — sqlite 인메모리로 «멱등 · 추가만 · 불변식» 을 잠근다.

실제 시드(assets/curriculum_v3/cur_seed.json, 7MB)를 그대로 쓴다 — 축약본을 만들면 불변식(10,636·462·491)이
시험에서 의미를 잃는다. 청크 46 은 옛 learning_item 에 가짜로 넣어 «한 번 복사» 경로를 그대로 탄다(결정 #13).
"""
from __future__ import annotations

import io
import json
import os

import pytest
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session

from db.registry import Base
from domains.learning.models.learning_item import LearningItem
from scripts.curriculum.load_cur_seed import CHUNK_LESSONS, load

SEED = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed.json")
pytestmark = pytest.mark.skipif(not os.path.exists(SEED), reason="cur_seed.json 없음")


@pytest.fixture(scope="module")
def seed():
    return json.load(io.open(SEED, encoding="utf-8"))


@pytest.fixture()
def db():
    # 기존 sqlite 시험 관례(test_call_type_expression.py) — BigInteger Identity PK 는 sqlite 에서 자동증가가 안 된다
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for i in range(46):
            s.add(LearningItem(
                language="ko", kind="chunk", source_key=f"c:{i}", band=1, level_no=1, assign_rule="seed",
                surface=f"청크 문장 {i}", meanings=json.dumps({"en": f"chunk {i}"}), examples="[]",
            ))
        s.commit()
        yield s


def test_load_passes_invariants_and_is_idempotent(db: Session, seed):
    stats = load(db, seed, dry_run=False)
    assert all(stats["checks"].values()), stats["checks"]
    assert stats["retired"] == 0
    db.commit()

    # 두 번째 적재 — 행 수가 늘지 않고, 은퇴 0
    before = {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
              for t in ("cur_item", "cur_lesson", "cur_lesson_item", "cur_topic", "cur_function", "cur_lesson_function")}
    stats2 = load(db, seed, dry_run=False)
    db.commit()
    after = {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in before}
    assert after == before
    assert all(stats2["checks"].values()) and stats2["retired"] == 0


def test_level1_chunk_lessons_and_order(db: Session, seed):
    load(db, seed, dry_run=False)
    db.commit()
    rows = db.execute(text(
        "SELECT l.no, l.code, l.level_no, COUNT(li.item_id) FROM cur_lesson l "
        "JOIN cur_lesson_item li ON li.lesson_id = l.lesson_id WHERE l.no <= 3 GROUP BY l.no, l.code, l.level_no ORDER BY l.no"
    )).all()
    assert [(r[1], r[2], r[3]) for r in rows] == [(c, 1, n) for c, _, n in CHUNK_LESSONS]
    # A1-T01-1 은 4번째, level_no 2, 항목 = 문법 3 + 필수 8 + 핵심 9 + 지원 10 = 30, 순번은 role 순
    r = db.execute(text("SELECT lesson_id, no, level_no, item_count FROM cur_lesson WHERE code='A1-T01-1'")).one()
    assert (r[1], r[2], r[3]) == (4, 2, 30)
    roles = [x[0] for x in db.execute(text("SELECT role FROM cur_lesson_item WHERE lesson_id=:l ORDER BY seq"), {"l": r[0]})]
    assert roles == ["grammar"] * 3 + ["must"] * 8 + ["core"] * 9 + ["support"] * 10


def test_retire_instead_of_delete(db: Session, seed):
    load(db, seed, dry_run=False)
    db.commit()
    # 시드에서 어휘 하나를 빼고 다시 적재 → 그 항목은 은퇴(행 유지), 차시 항목은 빠진다
    trimmed = dict(seed)
    victim = seed["vocab"][0]["key"]
    trimmed["vocab"] = seed["vocab"][1:]
    trimmed["lessons"] = [
        {**l, "must_keys": [k for k in l["must_keys"] if k != victim], "core_keys": [k for k in l["core_keys"] if k != victim],
         "support_keys": [k for k in l["support_keys"] if k != victim]}
        for l in seed["lessons"]
    ]
    stats = load(db, trimmed, dry_run=False)
    db.commit()
    assert stats["retired"] == 1
    assert db.execute(text("SELECT COUNT(*) FROM cur_item WHERE kind='vocab'")).scalar() == 10636  # 삭제 안 함
    assert db.execute(text("SELECT retired_at IS NOT NULL FROM cur_item WHERE kind='vocab' AND key=:k"), {"k": victim}).scalar()
    assert not stats["checks"]["cur_item 현역 11,144"]  # 불변식은 정직하게 실패를 알린다(운영 로더는 여기서 롤백)
