"""cur_* 시드 로더 — zh·en(2026-10-14 · 내부 시험 PM-DEC-612). 청크 차시 없음 · ko·ja 와 공존 · 동명 문법 유일화.

실제 시드(assets/curriculum_v3/cur_seed_{zh,en}.json)를 그대로 쓴다(test_cur_seed_load.py 와 같은 관례).
"""
from __future__ import annotations

import io
import json
import os

import pytest
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from db.registry import Base
from domains.learning.models.learning_item import LearningItem
from scripts.curriculum.load_cur_seed import load

ASSETS = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3")
SEEDS = {lang: os.path.join(ASSETS, f) for lang, f in
         {"ko": "cur_seed.json", "ja": "cur_seed_ja.json", "zh": "cur_seed_zh.json", "en": "cur_seed_en.json"}.items()}
pytestmark = pytest.mark.skipif(not all(os.path.exists(p) for p in SEEDS.values()), reason="cur_seed*.json 없음")


@pytest.fixture(scope="module")
def seeds():
    return {lang: json.load(io.open(p, encoding="utf-8")) for lang, p in SEEDS.items()}


@pytest.fixture()
def db():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for lang, pre in (("ko", "c"), ("ja", "jc")):   # ko·ja 청크 46(로더가 learning_item 에서 복사)
            for i in range(46):
                s.add(LearningItem(
                    language=lang, kind="chunk", source_key=f"{pre}:{i}", band=1, level_no=1, assign_rule="seed",
                    surface=f"{lang} chunk {i}", reading=(f"よみ{i}" if lang == "ja" else None),
                    meanings=json.dumps({"en": f"chunk {i}"}), examples="[]",
                ))
        s.commit()
        yield s


@pytest.mark.parametrize("lang,lessons,vocab,grammar", [("zh", 264, 2676, 429), ("en", 287, 2712, 705)])
def test_zh_en_load_passes_invariants_and_is_idempotent(db: Session, seeds, lang, lessons, vocab, grammar):
    seed = seeds[lang]
    assert seed["meta"]["language"] == lang and "1" not in seed["meta"]["levels"], "청크 차시 없음 — A1 부터"
    stats = load(db, seed, dry_run=False, language=lang)
    assert all(stats["checks"].values()), stats["checks"]
    assert stats["lessons"] == lessons and stats["retired"] == 0
    db.commit()
    assert db.execute(text("SELECT COUNT(*) FROM cur_item WHERE language=:l AND kind='vocab'"), {"l": lang}).scalar() == vocab
    assert db.execute(text("SELECT COUNT(*) FROM cur_item WHERE language=:l AND kind='grammar'"), {"l": lang}).scalar() == grammar
    assert db.execute(text("SELECT COUNT(*) FROM cur_item WHERE language=:l AND kind='chunk'"), {"l": lang}).scalar() == 0
    nos = [r[0] for r in db.execute(text("SELECT no FROM cur_lesson WHERE language=:l ORDER BY no"), {"l": lang})]
    assert nos == list(range(1, lessons + 1))
    first = db.execute(text("SELECT code, level_no FROM cur_lesson WHERE language=:l AND no=1"), {"l": lang}).one()
    assert first[0].startswith("A1-") and first[1] == 2
    before = {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in ("cur_item", "cur_lesson", "cur_lesson_item", "cur_topic")}
    stats2 = load(db, seed, dry_run=False, language=lang)
    db.commit()
    assert all(stats2["checks"].values()) and stats2["retired"] == 0 and stats2["renumbered"] == 0
    assert {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in before} == before


def test_zh_vocab_meanings_carry_ko_and_pinyin(db: Session, seeds):
    seed = seeds["zh"]
    load(db, seed, dry_run=False, language="zh"); db.commit()
    v = seed["vocab"][0]
    m = json.loads(db.execute(text("SELECT meanings FROM cur_item WHERE language='zh' AND kind='vocab' AND key=:k"), {"k": v["key"]}).scalar())
    assert m == {"en": v["en"], "ko": v["ko"], "pinyin": v["pinyin"]} and m["ko"] and m["pinyin"]


def test_en_vocab_meaning_ko_present(db: Session, seeds):
    seed = seeds["en"]
    load(db, seed, dry_run=False, language="en"); db.commit()
    blank = [v["key"] for v in seed["vocab"] if not v.get("ko")]
    assert blank == [], "en 은 en=표제어라 ko 뜻이 없으면 학습자에게 뜻이 안 보인다"


@pytest.mark.parametrize("lang,dups", [("zh", 6), ("en", 23)])
def test_same_name_grammar_kept_apart_with_plain_surface(db: Session, seeds, lang, dups):
    seed = seeds[lang]
    split = [g for g in seed["grammar"] if g.get("surface")]
    assert len({g["surface"] for g in split}) == dups
    assert all(g["key"] == f"{g['surface']} ({g['stage']})" and g["lesson_codes"] for g in split)
    load(db, seed, dry_run=False, language=lang); db.commit()
    g = split[0]
    assert db.execute(text("SELECT surface FROM cur_item WHERE language=:l AND kind='grammar' AND key=:k"),
                      {"l": lang, "k": g["key"]}).scalar() == g["surface"]


def test_four_languages_coexist_and_ko_ja_unchanged(db: Session, seeds):
    for lang in ("ko", "ja"):
        assert all(load(db, seeds[lang], dry_run=False, language=lang)["checks"].values()); db.commit()
    snap = lambda: [tuple(r) for r in db.execute(text(
        "SELECT l.language, COUNT(DISTINCT l.lesson_id), COUNT(li.item_id) FROM cur_lesson l "
        "LEFT JOIN cur_lesson_item li ON li.lesson_id=l.lesson_id WHERE l.language IN ('ko','ja') GROUP BY l.language ORDER BY 1"))]
    items = lambda: db.execute(text("SELECT COUNT(*) FROM cur_item WHERE language IN ('ko','ja') AND retired_at IS NULL")).scalar()
    before, before_items = snap(), items()
    for lang in ("zh", "en"):
        assert all(load(db, seeds[lang], dry_run=False, language=lang)["checks"].values()); db.commit()
    assert snap() == before and items() == before_items, "zh·en 적재가 ko·ja 행을 바꾸지 않는다"
    assert db.execute(text("SELECT COUNT(*) FROM cur_topic")).scalar() == 65
    assert [r[0] for r in db.execute(text("SELECT DISTINCT language FROM cur_lesson ORDER BY 1"))] == ["en", "ja", "ko", "zh"]
