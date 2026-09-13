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
import scripts.curriculum.load_cur_seed as loader
from scripts.curriculum.load_cur_seed import CHUNK_LESSONS, load

SEED = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed.json")
SEED_JA = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed_ja.json")
pytestmark = pytest.mark.skipif(not (os.path.exists(SEED) and os.path.exists(SEED_JA)), reason="cur_seed*.json 없음")


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
        for i in range(46):   # ja 청크 46 — 운영 learning_item(kind=chunk, language=ja) 흉내: meanings {en,roman,ko} + reading 열(가나)
            s.add(LearningItem(
                language="ja", kind="chunk", source_key=f"jc:{i}", band=1, level_no=1, assign_rule="survival_v1",
                surface=f"チャンク{i}", reading=f"ちゃんく{i}", meanings=json.dumps({"en": f"chunk {i}", "roman": f"chanku{i}", "ko": f"청크 {i}"}), examples="[]",
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
              for t in ("cur_item", "cur_lesson", "cur_lesson_item", "cur_topic")}
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
    for l in trimmed["lessons"]:   # 시드 자체의 정합(item_count) — 불변식이 시드에서 세므로 시드는 스스로 맞아야 한다
        l["item_count"] = len(l["grammar_keys"]) + len(l["must_keys"]) + len(l["core_keys"]) + len(l["support_keys"])
    stats = load(db, trimmed, dry_run=False)
    db.commit()
    assert stats["retired"] == 1
    assert db.execute(text("SELECT COUNT(*) FROM cur_item WHERE kind='vocab'")).scalar() == 10636  # 삭제 안 함
    assert db.execute(text("SELECT retired_at IS NOT NULL FROM cur_item WHERE kind='vocab' AND key=:k"), {"k": victim}).scalar()
    # 2026-09-13 언어 일반화 — 불변식은 **시드에서 센다**: 시드에서 빠진 항목이 은퇴하면 현역 수 = 시드 수라 PASS 가 맞다(은퇴 = 삭제 아님).
    assert stats["check_item_key"] == "cur_item 현역 11,143" and stats["checks"][stats["check_item_key"]]
    assert all(stats["checks"].values())


# --------------------------------------------------------------------------- #
# 일본어(ja) — 청크 3차시(ko 와 동일, 사장님 2026-09-13) · no 1..348 · L1-S01-1 = 1 · A1-T01-1 = 4 · meanings {en, ko, kana} · ko 와 공존(주제 65 공유)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def seed_ja():
    return json.load(io.open(SEED_JA, encoding="utf-8"))


def test_ja_load_passes_invariants_and_is_idempotent(db: Session, seed_ja):
    assert seed_ja["meta"]["language"] == "ja" and "1" not in seed_ja["meta"]["levels"], "시드엔 레벨1 없음 — 청크는 로더가 learning_item 에서 복사(ko 와 같다)"
    stats = load(db, seed_ja, dry_run=False, language="ja")
    assert all(stats["checks"].values()), stats["checks"]
    assert stats["language"] == "ja" and stats["retired"] == 0 and stats["lessons"] == 348 and stats["renumbered"] == 0
    assert stats["check_item_key"] == "cur_item 현역 4,992"          # 어휘 4,328 + 문법 618 + 청크 46
    db.commit()
    before = {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in ("cur_item", "cur_lesson", "cur_lesson_item", "cur_topic")}
    stats2 = load(db, seed_ja, dry_run=False, language="ja")
    db.commit()
    assert {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in before} == before
    assert all(stats2["checks"].values()) and stats2["retired"] == 0
    assert before["cur_topic"] == 65 and before["cur_lesson"] == 348 and stats2["renumbered"] == 0


def test_ja_lesson_numbers_and_first_lesson_composition(db: Session, seed_ja):
    load(db, seed_ja, dry_run=False, language="ja")
    db.commit()
    nos = [r[0] for r in db.execute(text("SELECT no FROM cur_lesson WHERE language='ja' ORDER BY no"))]
    assert nos == list(range(1, 349))
    # 레벨1 청크 3차시 — ko 와 같은 코드·상황문·15/15/16
    rows = db.execute(text(
        "SELECT l.no, l.code, l.level_no, l.situation, COUNT(li.item_id) FROM cur_lesson l JOIN cur_lesson_item li ON li.lesson_id=l.lesson_id "
        "WHERE l.language='ja' AND l.no <= 3 GROUP BY l.no, l.code, l.level_no, l.situation ORDER BY l.no"
    )).all()
    assert [(r[1], r[2], r[3], r[4]) for r in rows] == [(c, 1, sit, n) for c, sit, n in CHUNK_LESSONS]
    chunk = db.execute(text("SELECT surface, meanings FROM cur_item WHERE language='ja' AND kind='chunk' ORDER BY item_id LIMIT 1")).one()
    assert chunk[0] == "チャンク0" and json.loads(chunk[1]) == {"en": "chunk 0", "roman": "chanku0", "ko": "청크 0", "kana": "ちゃんく0"}, "reading → kana"
    first = next(l for l in seed_ja["lessons"] if l["no"] == 1)
    assert first["code"] == "A1-T01-1"
    r = db.execute(text("SELECT lesson_id, no, level_no, item_count, situation FROM cur_lesson WHERE language='ja' AND code='A1-T01-1'")).one()
    assert (r[1], r[2], r[3]) == (4, 2, first["item_count"]) and r[4] == first["situation"], "A1-T01-1 은 청크 3차시 뒤 = 4"
    roles = [x[0] for x in db.execute(text("SELECT role FROM cur_lesson_item WHERE lesson_id=:l ORDER BY seq"), {"l": r[0]})]
    assert roles == (["grammar"] * len(first["grammar_keys"]) + ["must"] * len(first["must_keys"])
                     + ["core"] * len(first["core_keys"]) + ["support"] * len(first["support_keys"]))
    # 어휘 뜻 = {en, ko, kana} · 예문 1개 · 문법은 {en}
    v = seed_ja["vocab"][0]
    row = db.execute(text("SELECT surface, meanings, examples, level_no FROM cur_item WHERE language='ja' AND kind='vocab' AND key=:k"), {"k": v["key"]}).one()
    m = json.loads(row[1])
    assert row[0] == v["key"] and m == {"en": v["en"], "ko": v["ko"], "kana": v["kana"]} and len(json.loads(row[2])) == 1
    assert m["kana"] and all(ord(c) > 0x3000 for c in m["kana"]), "읽기 = 가나"
    g = seed_ja["grammar"][0]
    gm = db.execute(text("SELECT meanings FROM cur_item WHERE language='ja' AND kind='grammar' AND key=:k"), {"k": g["key"]}).scalar()
    assert json.loads(gm) == {"en": g["en"]}


def test_ko_and_ja_coexist_in_one_db_sharing_topics(db: Session, seed, seed_ja):
    s1 = load(db, seed, dry_run=False); db.commit()
    s2 = load(db, seed_ja, dry_run=False, language="ja"); db.commit()
    assert all(s1["checks"].values()) and all(s2["checks"].values())
    assert db.execute(text("SELECT COUNT(*) FROM cur_topic")).scalar() == 65
    assert db.execute(text("SELECT COUNT(*) FROM cur_lesson WHERE language='ko'")).scalar() == 491
    assert db.execute(text("SELECT COUNT(*) FROM cur_lesson WHERE language='ja'")).scalar() == 348
    # 다시 ko 적재 — ja 가 있어도 ko 불변식·멱등 그대로(언어별로 센다)
    s3 = load(db, seed, dry_run=False); db.commit()
    assert all(s3["checks"].values()) and s3["retired"] == 0
    assert db.execute(text("SELECT COUNT(*) FROM cur_item WHERE retired_at IS NULL")).scalar() == 11144 + 4992
    assert db.execute(text("SELECT COUNT(*) FROM cur_item WHERE kind='chunk'")).scalar() == 92, "언어별 청크 46 씩"


def test_ja_reload_renumbers_lessons_in_place_when_chunk_lessons_are_added(db: Session, seed_ja, monkeypatch):
    """운영 시나리오: 옛 로더(청크 ko 만)로 적재된 ja 345차시(A1-T01-1 = no 1) 위에 새 로더 → 청크 3차시가 앞에 들어와 no 가 3 씩 밀린다.
    uq_cur_lesson_no 충돌 없이(2단계) 갱신되고, lesson_id 로 묶인 회원 진도는 그대로다."""
    from domains.learning.models.curriculum import CurMemberProgress
    from domains.account.models.member import Member
    monkeypatch.setattr(loader, "CHUNK_LANGUAGES", frozenset({"ko"}))
    s1 = load(db, seed_ja, dry_run=False, language="ja"); db.commit()
    assert s1["lessons"] == 345 and all(s1["checks"].values())
    a1 = db.execute(text("SELECT lesson_id, no FROM cur_lesson WHERE language='ja' AND code='A1-T01-1'")).one()
    assert a1[1] == 1
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-renum", target_language="ja"); db.add(m); db.flush()
    db.add(CurMemberProgress(member_id=m.member_id, language="ja", lesson_id=a1[0])); db.commit()
    monkeypatch.setattr(loader, "CHUNK_LANGUAGES", frozenset({"ko", "ja"}))
    s2 = load(db, seed_ja, dry_run=False, language="ja"); db.commit()
    assert all(s2["checks"].values()) and s2["lessons"] == 348 and s2["renumbered"] == 345
    a1b = db.execute(text("SELECT lesson_id, no FROM cur_lesson WHERE language='ja' AND code='A1-T01-1'")).one()
    assert a1b[0] == a1[0] and a1b[1] == 4, "같은 행(lesson_id 유지)이 4 로 밀렸다"
    assert db.execute(text("SELECT no FROM cur_lesson WHERE language='ja' AND code='L1-S01-1'")).scalar() == 1
    assert [r[0] for r in db.execute(text("SELECT no FROM cur_lesson WHERE language='ja' ORDER BY no"))] == list(range(1, 349))
    prog = db.execute(text("SELECT lesson_id FROM cur_member_progress WHERE member_id=:m AND language='ja'"), {"m": m.member_id}).scalar()
    assert prog == a1[0], "회원 포인터는 lesson_id — 그대로 A1-T01-1 을 가리킨다"
    s3 = load(db, seed_ja, dry_run=False, language="ja"); db.commit()
    assert s3["renumbered"] == 0 and all(s3["checks"].values()), "재적재 멱등"


def test_language_mismatch_is_refused(db: Session, seed_ja):
    with pytest.raises(SystemExit):
        load(db, seed_ja, dry_run=False, language="ko")
