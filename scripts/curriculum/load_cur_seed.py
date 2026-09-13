"""cur_seed*.json → cur_* 테이블 적재 (멱등 · 추가만 · 회원 진도 무손상). 언어 일반화(2026-09-13: ko · ja).

설계 docs/20260912_0330_cur-스키마-설계.md §4·§8(P1-9). 사장님 결정 #7·#12·#13:
  레벨1 = 생존회화 청크 46 → 차시 3개(15·15·16) — 청크는 옛 learning_item(kind=chunk, ko) 에서 **한 번 복사**
  (새 경로가 옛 테이블을 읽는 유일한 자리다. 그 뒤로는 안 본다).

⛔ 안 읽는 컬럼은 싣지 않는다(사장님 2026-09-12): 등급·CEFR·빈도·출처·기능(F코드)·모범 대화·성공 조건·가드레일은 시드 JSON 에만 남는다.

규칙
  · cur_item 은 DELETE 하지 않는다 — 시드에서 빠진 항목은 retired_at 을 찍는다(회원 진도가 item_id 로 묶여 있다)
  · cur_lesson_item 은 차시 단위로 diff(삭제+삽입) — 한 트랜잭션
  · 두 번 돌려도 같다(UPSERT by 멱등 키: topic/function=code · item=(language,kind,key) · lesson=(language,code))
  · 끝에 불변식을 세고 하나라도 깨지면 롤백·exit 1

언어(2026-09-13): `--language ko|ja`(기본 ko; 시드 meta.language 와 어긋나면 중단).
  · 청크 복사(learning_item)·CHUNK_LESSONS(레벨1 3차시)는 **ko 만** — ja 는 A1(level_no 2) 부터, 차시 no 는 언어별 1..N(ja 의 A1-T01-1 = 1).
  · meanings JSON = {"en", ("ko", "kana" 가 시드에 있으면 함께)} — 로케일 아닌 키(kana)도 청크의 "roman" 처럼 같은 JSON 에 싣는다(새 컬럼 금지).
  · 불변식은 시드에서 센다(차시 수·항목 수·어휘 전건 정확히 1차시) — ko 는 종전 값(491·11,144·11,307)과 같다.
  · 주제(cur_topic)는 언어 무관 자산(코드 65 공통) — 두 언어를 같은 DB 에 적재해도 65 다.

사용:  PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/curriculum/load_cur_seed.py [--language ko|ja] [--seed …] [--dry-run]
       DATABASE_URL_DIRECT(5432) 로 붙는다 — 마이그레이션과 같은 규율. ⚠ 운영 DB 는 «적재» 지시 뒤에만.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core.config import settings  # noqa: E402
import db.registry  # noqa: E402,F401  — 전 모델 등록(관계 매퍼가 Member·Call 을 찾는다)
from domains.learning.models.curriculum import (  # noqa: E402
    CurItem, CurLesson, CurLessonItem, CurTopic,
)

SEED_BY_LANGUAGE = {"ko": "cur_seed.json", "ja": "cur_seed_ja.json"}
CHUNK_LESSONS = [  # 결정 #12 — 레벨1 생존회화 3차시(**ko 만**). 청크 46 = 15·15·16 (시드 순)
    ("L1-S01-1", "처음 만난 사람과 인사하기", 15),
    ("L1-S02-1", "가게·식당에서 부탁하기", 15),
    ("L1-S03-1", "못 알아들었을 때 되묻기", 16),
]
ROLE_ORDER = ("grammar", "must", "core", "support")
TOPIC_KIND = {"회화": "conv", "지원": "support"}


def _j(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def load(session: Session, seed: dict, *, dry_run: bool, language: str | None = None) -> dict:
    lang = language or (seed.get("meta") or {}).get("language") or "ko"
    seed_lang = (seed.get("meta") or {}).get("language") or "ko"
    if seed_lang != lang:
        raise SystemExit(f"⛔ 시드 언어({seed_lang}) 와 --language({lang}) 가 다르다")
    has_chunks = lang == "ko"
    now = datetime.now(timezone.utc)
    stats: dict = {"language": lang}

    # 1. topic · function ────────────────────────────────────────────────
    topic_id: dict[str, int] = {}
    for t in seed["topics"]:
        row = session.scalar(select(CurTopic).where(CurTopic.code == t["code"]))
        if row is None:
            row = CurTopic(code=t["code"])
            session.add(row)
        row.area, row.name, row.kind = t["area"], t["name"], TOPIC_KIND[t["kind"]]
        session.flush()
        topic_id[t["code"]] = row.topic_id
    stats["topics"] = len(topic_id)

    # 2. item — 어휘·문법(시드) + 청크(옛 learning_item 에서 한 번 복사) ─────────────
    existing = {(r.kind, r.key): r for r in session.scalars(select(CurItem).where(CurItem.language == lang))}
    seen: set[tuple[str, str]] = set()
    item_id: dict[tuple[str, str], int] = {}

    def upsert(kind: str, key: str, **fields) -> int:
        row = existing.get((kind, key))
        if row is None:
            row = CurItem(language=lang, kind=kind, key=key)
            session.add(row)
            existing[(kind, key)] = row
        for k, v in fields.items():
            setattr(row, k, v)
        row.retired_at = None
        seen.add((kind, key))
        session.flush()
        item_id[(kind, key)] = row.item_id
        return row.item_id

    stage_level = {s: i + 2 for i, s in enumerate(["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4", "C1", "C2", "C3", "C4"])}
    def _meanings(v: dict) -> str | None:
        """{"en": …} + 시드에 있으면 "ko"(한국어뜻)·"kana"(읽기) — ko 시드엔 없어 종전과 같은 JSON."""
        m = {}
        if v.get("en"):
            m["en"] = v["en"]
        for k in ("ko", "kana"):
            if v.get(k):
                m[k] = v[k]
        return _j(m) if m else None

    for v in seed["vocab"]:
        upsert(
            "vocab", v["key"], surface=v["headword"],
            meanings=_meanings(v), pos=v.get("pos") or None,
            guide=v.get("guide") or None, level_no=stage_level.get(v.get("stage"), 2),
            topic_id=topic_id.get(v.get("topic_code")), examples=_j(v.get("examples") or []),
        )
    for g in seed["grammar"]:
        upsert(
            "grammar", g["key"], surface=g["key"], meanings=_j({"en": g["en"]}) if g.get("en") else None,
            level_no=stage_level.get(g.get("stage"), 2), description=g.get("desc") or None,
            examples=_j(g.get("examples") or []),
        )
    # 청크 — 옛 테이블에서 복사(결정 #13, **ko 만**). 순서 = item_id(원 시드 순)
    chunk_ids: list[int] = []
    if has_chunks:
        chunks = session.execute(text(
            "SELECT surface, meanings, examples FROM learning_item "
            "WHERE language = :lang AND kind = 'chunk' ORDER BY item_id"
        ), {"lang": lang}).all()
        if len(chunks) != 46:
            raise SystemExit(f"⛔ 옛 learning_item 청크가 46개가 아니다: {len(chunks)}")
        chunk_ids = [
            upsert("chunk", c.surface, surface=c.surface, meanings=c.meanings, examples=c.examples, level_no=1)
            for c in chunks
        ]
    # 시드에서 빠진 현역 항목 → 은퇴(삭제 금지)
    retired = 0
    for (kind, key), row in existing.items():
        if (kind, key) not in seen and row.retired_at is None:
            row.retired_at = now
            retired += 1
    stats["items"], stats["retired"] = len(seen), retired

    # 3. lesson + lesson_item + lesson_function ───────────────────────────
    lesson_rows: list[tuple[dict, list[tuple[str, int]]]] = []  # (lesson fields, [(role, item_id)...])
    chunk_lessons = CHUNK_LESSONS if has_chunks else []
    pos = 0
    for i, (code, situation, n) in enumerate(chunk_lessons, start=1):
        ids = chunk_ids[pos:pos + n]
        pos += n
        lesson_rows.append(({
            "no": i, "code": code, "level_no": 1, "topic_id": None,
            "situation": situation, "partner": None, "probes": None,
        }, [("chunk", iid) for iid in ids]))
    assert pos == (46 if has_chunks else 0)
    for l in seed["lessons"]:
        items: list[tuple[str, int]] = []
        for role, keys, kind in (("grammar", l["grammar_keys"], "grammar"), ("must", l["must_keys"], "vocab"),
                                 ("core", l["core_keys"], "vocab"), ("support", l["support_keys"], "vocab")):
            for k in keys:
                items.append((role, item_id[(kind, k)]))
        lesson_rows.append(({
            "no": l["no"] + len(chunk_lessons), "code": l["code"], "level_no": l["level_no"],
            "topic_id": topic_id[l["topic_code"]], "situation": l["situation"],
            "partner": l.get("partner"), "probes": _j(l.get("probes") or []),
        }, items))

    existing_lessons = {r.code: r for r in session.scalars(select(CurLesson).where(CurLesson.language == lang))}
    for fields, items in lesson_rows:
        row = existing_lessons.get(fields["code"])
        if row is None:
            row = CurLesson(language=lang, code=fields["code"])
            session.add(row)
            existing_lessons[fields["code"]] = row
        for k, v in fields.items():
            setattr(row, k, v)
        row.item_count = len(items)
        session.flush()
        # 차시 단위 diff — 기존 행 전부 지우고 다시(한 트랜잭션 안)
        session.query(CurLessonItem).filter(CurLessonItem.lesson_id == row.lesson_id).delete()
        seq = 0
        for role, iid in items:
            seq += 1
            session.add(CurLessonItem(lesson_id=row.lesson_id, item_id=iid, role=role, seq=seq))
    session.flush()
    stats["lessons"] = len(lesson_rows)

    # 4. 불변식 — **시드에서 센다**(언어별). ko 는 종전 고정값(65 · 11,144 · 491 · 11,307 · 10,636)과 같다.
    def n(sql: str) -> int:
        return int(session.execute(text(sql), {"lang": lang}).scalar())
    n_chunks = 46 if has_chunks else 0
    exp_items = len(seed["vocab"]) + len(seed["grammar"]) + n_chunks
    exp_lessons = len(seed["lessons"]) + len(chunk_lessons)
    exp_li = sum(l["item_count"] for l in seed["lessons"]) + n_chunks
    exp_vocab = len(seed["vocab"])
    LI = ("FROM cur_lesson_item li JOIN cur_item i ON i.item_id=li.item_id JOIN cur_lesson l ON l.lesson_id=li.lesson_id "
          "WHERE l.language=:lang")
    checks = {
        "cur_topic 65": n("SELECT COUNT(*) FROM cur_topic") == 65,
        f"cur_item 현역 {exp_items:,}": n("SELECT COUNT(*) FROM cur_item WHERE retired_at IS NULL AND language=:lang") == exp_items,
        f"cur_lesson {exp_lessons}": n("SELECT COUNT(*) FROM cur_lesson WHERE language=:lang") == exp_lessons,
        f"no 1..{exp_lessons} 연속": n(f"SELECT COUNT(*) FROM cur_lesson WHERE language=:lang AND no BETWEEN 1 AND {exp_lessons}") == exp_lessons
                          and n("SELECT COUNT(DISTINCT no) FROM cur_lesson WHERE language=:lang") == exp_lessons,
        f"lesson_item = 시드 {exp_li - n_chunks:,} + 청크 {n_chunks}": n(f"SELECT COUNT(*) {LI}") == exp_li,
        "어휘 전건 정확히 1차시": n(f"SELECT COUNT(*) {LI} AND i.kind='vocab'") == exp_vocab
                          and n(f"SELECT COUNT(DISTINCT li.item_id) {LI} AND i.kind='vocab'") == exp_vocab,
        "item_count = 실제": n("SELECT COUNT(*) FROM cur_lesson l WHERE l.language=:lang AND l.item_count <> (SELECT COUNT(*) FROM cur_lesson_item li WHERE li.lesson_id=l.lesson_id)") == 0,
    }
    stats["checks"] = checks
    stats["check_item_key"] = f"cur_item 현역 {exp_items:,}"
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--language", default="ko", choices=sorted(SEED_BY_LANGUAGE))
    ap.add_argument("--seed", default=None, help="기본: assets/curriculum_v3/<언어별 시드>")
    ap.add_argument("--dry-run", action="store_true", help="적재 후 롤백(불변식만 본다)")
    a = ap.parse_args()
    seed_path = a.seed or os.path.join("assets", "curriculum_v3", SEED_BY_LANGUAGE[a.language])
    seed = json.load(io.open(seed_path, encoding="utf-8"))
    engine = create_engine(settings.direct_url)
    with Session(engine) as session:
        try:
            stats = load(session, seed, dry_run=a.dry_run, language=a.language)
            for k, ok in stats["checks"].items():
                print(("PASS " if ok else "FAIL ") + k)
            print({k: v for k, v in stats.items() if k != "checks"})
            if not all(stats["checks"].values()):
                session.rollback()
                print("⛔ 불변식 실패 — 롤백")
                return 1
            if a.dry_run:
                session.rollback()
                print("dry-run — 롤백")
            else:
                session.commit()
                print("커밋 완료")
        except Exception:
            session.rollback()
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
