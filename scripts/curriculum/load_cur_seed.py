"""cur_seed.json → cur_* 테이블 적재 (멱등 · 추가만 · 회원 진도 무손상).

설계 docs/20260912_0330_cur-스키마-설계.md §4·§8(P1-9). 사장님 결정 #7·#12·#13:
  레벨1 = 생존회화 청크 46 → 차시 3개(15·15·16) — 청크는 옛 learning_item(kind=chunk, ko) 에서 **한 번 복사**
  (새 경로가 옛 테이블을 읽는 유일한 자리다. 그 뒤로는 안 본다).

규칙
  · cur_item 은 DELETE 하지 않는다 — 시드에서 빠진 항목은 retired_at 을 찍는다(회원 진도가 item_id 로 묶여 있다)
  · cur_lesson_item 은 차시 단위로 diff(삭제+삽입) — 한 트랜잭션
  · 두 번 돌려도 같다(UPSERT by 멱등 키: topic/function=code · item=(language,kind,key) · lesson=(language,code))
  · 끝에 불변식을 세고 하나라도 깨지면 롤백·exit 1

사용:  PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/curriculum/load_cur_seed.py [--seed assets/curriculum_v3/cur_seed.json] [--dry-run]
       DATABASE_URL_DIRECT(5432) 로 붙는다 — 마이그레이션과 같은 규율. ⚠ 운영 DB 는 «돌려» 지시 뒤에만.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.getcwd())

from sqlalchemy import create_engine, select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from core.config import settings  # noqa: E402
import db.registry  # noqa: E402,F401  — 전 모델 등록(관계 매퍼가 Member·Call 을 찾는다)
from domains.learning.models.curriculum import (  # noqa: E402
    CurFunction, CurItem, CurLesson, CurLessonFunction, CurLessonItem, CurTopic,
)

LANG = "ko"
CHUNK_LESSONS = [  # 결정 #12 — 레벨1 생존회화 3차시. 청크 46 = 15·15·16 (시드 순)
    ("L1-S01-1", "처음 만난 사람과 인사하기", 15),
    ("L1-S02-1", "가게·식당에서 부탁하기", 15),
    ("L1-S03-1", "못 알아들었을 때 되묻기", 16),
]
ROLE_ORDER = ("grammar", "must", "core", "support")
TOPIC_KIND = {"회화": "conv", "지원": "support"}


def _j(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False)


def _headword_suffix(key: str) -> str | None:
    parts = [p for p in re.split(r"[/·]", key) if p]
    sufs = [m.group(1) for p in parts for m in [re.search(r"(\d{2})$", p)] if m]
    return "/".join(sufs) if sufs else None


def load(session: Session, seed: dict, *, dry_run: bool) -> dict:
    now = datetime.now(timezone.utc)
    stats: dict[str, int] = {}

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
    func_id: dict[str, int] = {}
    for f in seed["functions"]:
        row = session.scalar(select(CurFunction).where(CurFunction.code == f["code"]))
        if row is None:
            row = CurFunction(code=f["code"])
            session.add(row)
        row.name = f["name"]
        session.flush()
        func_id[f["code"]] = row.function_id
    stats["topics"], stats["functions"] = len(topic_id), len(func_id)

    # 2. item — 어휘·문법(시드) + 청크(옛 learning_item 에서 한 번 복사) ─────────────
    existing = {(r.kind, r.key): r for r in session.scalars(select(CurItem).where(CurItem.language == LANG))}
    seen: set[tuple[str, str]] = set()
    item_id: dict[tuple[str, str], int] = {}

    def upsert(kind: str, key: str, **fields) -> int:
        row = existing.get((kind, key))
        if row is None:
            row = CurItem(language=LANG, kind=kind, key=key)
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
    for v in seed["vocab"]:
        upsert(
            "vocab", v["key"], surface=v["headword"], headword_suffix=_headword_suffix(v["key"]),
            meanings=_j({"en": v["en"]}) if v.get("en") else None, pos=v.get("pos") or None,
            guide=v.get("guide") or None, grade=v.get("grade") or None, cefr6=v.get("cefr6") or None,
            stage=v.get("stage") or None, level_no=stage_level.get(v.get("stage"), 2),
            topic_id=topic_id.get(v.get("topic_code")), examples=_j(v.get("examples") or []),
            freq=v.get("freq"),
        )
    for g in seed["grammar"]:
        upsert(
            "grammar", g["key"], surface=g["key"], meanings=_j({"en": g["en"]}) if g.get("en") else None,
            stage=g.get("stage") or None, level_no=stage_level.get(g.get("stage"), 2),
            function_id=func_id.get(g.get("function_code")), description=g.get("desc") or None,
            notes=g.get("notes") or None, examples=_j(g.get("examples") or []),
            textbook=g.get("textbook") or None, textbook_unit=g.get("textbook_unit") or None,
            task_title=g.get("task_title") or None,
        )
    # 청크 — 옛 테이블에서 복사(결정 #13). 순서 = item_id(원 시드 순)
    chunks = session.execute(text(
        "SELECT surface, meanings, examples FROM learning_item "
        "WHERE language = :lang AND kind = 'chunk' ORDER BY item_id"
    ), {"lang": LANG}).all()
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
    pos = 0
    for i, (code, situation, n) in enumerate(CHUNK_LESSONS, start=1):
        ids = chunk_ids[pos:pos + n]
        pos += n
        lesson_rows.append(({
            "no": i, "code": code, "level_no": 1, "stage": None, "topic_id": None, "part": "1/1",
            "situation": situation, "partner": None, "grammar_kind": None, "opening": None, "probes": None,
            "success": None, "guardrails": None, "dialogue": None, "functions": [],
        }, [("chunk", iid) for iid in ids]))
    assert pos == 46
    for l in seed["lessons"]:
        items: list[tuple[str, int]] = []
        for role, keys, kind in (("grammar", l["grammar_keys"], "grammar"), ("must", l["must_keys"], "vocab"),
                                 ("core", l["core_keys"], "vocab"), ("support", l["support_keys"], "vocab")):
            for k in keys:
                items.append((role, item_id[(kind, k)]))
        lesson_rows.append(({
            "no": l["no"] + len(CHUNK_LESSONS), "code": l["code"], "level_no": l["level_no"], "stage": l["stage"],
            "topic_id": topic_id[l["topic_code"]], "part": l.get("part"), "situation": l["situation"],
            "partner": l.get("partner"), "grammar_kind": l.get("grammar_kind"), "opening": l.get("opening"),
            "probes": _j(l.get("probes") or []), "success": _j(l.get("success")), "guardrails": _j(l.get("guardrails") or []),
            "dialogue": _j(l.get("dialogue") or []), "functions": l.get("functions") or [],
        }, items))

    existing_lessons = {r.code: r for r in session.scalars(select(CurLesson).where(CurLesson.language == LANG))}
    for fields, items in lesson_rows:
        funcs = fields.pop("functions")
        row = existing_lessons.get(fields["code"])
        if row is None:
            row = CurLesson(language=LANG, code=fields["code"])
            session.add(row)
            existing_lessons[fields["code"]] = row
        for k, v in fields.items():
            setattr(row, k, v)
        row.item_count = len(items)
        session.flush()
        # 차시 단위 diff — 기존 행 전부 지우고 다시(한 트랜잭션 안)
        session.query(CurLessonItem).filter(CurLessonItem.lesson_id == row.lesson_id).delete()
        session.query(CurLessonFunction).filter(CurLessonFunction.lesson_id == row.lesson_id).delete()
        seq = 0
        for role, iid in items:
            seq += 1
            session.add(CurLessonItem(lesson_id=row.lesson_id, item_id=iid, role=role, seq=seq))
        for fc in funcs:
            session.add(CurLessonFunction(lesson_id=row.lesson_id, function_id=func_id[fc]))
    session.flush()
    stats["lessons"] = len(lesson_rows)

    # 4. 불변식 ──────────────────────────────────────────────────────────
    def n(sql: str) -> int:
        return int(session.execute(text(sql)).scalar())
    checks = {
        "cur_topic 65": n("SELECT COUNT(*) FROM cur_topic") == 65,
        "cur_function 30": n("SELECT COUNT(*) FROM cur_function") == 30,
        "cur_item 현역 11,144": n("SELECT COUNT(*) FROM cur_item WHERE retired_at IS NULL AND language='ko'") == 10636 + 462 + 46,
        "cur_lesson 491": n("SELECT COUNT(*) FROM cur_lesson WHERE language='ko'") == 491,
        "no 1..491 연속": n("SELECT COUNT(*) FROM cur_lesson WHERE language='ko' AND no BETWEEN 1 AND 491") == 491
                          and n("SELECT COUNT(DISTINCT no) FROM cur_lesson WHERE language='ko'") == 491,
        "lesson_item = 시드 11,261 + 청크 46": n("SELECT COUNT(*) FROM cur_lesson_item") == 11261 + 46,
        "어휘 전건 정확히 1차시": n("SELECT COUNT(*) FROM cur_lesson_item li JOIN cur_item i ON i.item_id=li.item_id WHERE i.kind='vocab'") == 10636
                          and n("SELECT COUNT(DISTINCT li.item_id) FROM cur_lesson_item li JOIN cur_item i ON i.item_id=li.item_id WHERE i.kind='vocab'") == 10636,
        "item_count = 실제": n("SELECT COUNT(*) FROM cur_lesson l WHERE l.item_count <> (SELECT COUNT(*) FROM cur_lesson_item li WHERE li.lesson_id=l.lesson_id)") == 0,
        "lesson_function 행 = 시드 기능 합": n("SELECT COUNT(*) FROM cur_lesson_function") == sum(len(l.get("functions") or []) for l in seed["lessons"]),
    }
    stats["checks"] = checks
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default=os.path.join("assets", "curriculum_v3", "cur_seed.json"))
    ap.add_argument("--dry-run", action="store_true", help="적재 후 롤백(불변식만 본다)")
    a = ap.parse_args()
    seed = json.load(io.open(a.seed, encoding="utf-8"))
    engine = create_engine(settings.direct_url)
    with Session(engine) as session:
        try:
            stats = load(session, seed, dry_run=a.dry_run)
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
