"""워크북 뜻·로마자를 `learning_item.meanings` 로 병합한다.

앱 어휘의 다국어 뜻이 **0%** 다(`assets/level/curriculum_v2/vocab.json` 실측 0/10,636).
학습자는 외국인인데 단어 뜻을 못 본다. 워크북 JSON 에 이미 10,468건이 있다.

조인 키는 `surface` 가 아니라 **`source_key`**(`v:{surface}{접미|00}`)다.
시드 멱등 키이므로 재실행이 안전하고, 동형이의어를 뭉개지 않는다.

소비 형식은 `normalcall_service._study_des` · `_study_roman` 가 정한다 —
`{"en": "store, shop", "roman": "gage"}`.

기본은 **dry-run** 이다. `--apply` 를 줄 때만 DB 를 쓴다.

    python scripts/merge_vocab_meanings.py                 # 세기만
    python scripts/merge_vocab_meanings.py --out out.json  # 병합본 파일로
    python scripts/merge_vocab_meanings.py --apply         # DB 반영(.env 필요)

⛔ 한글은 콘솔에서 깨진다. 사람이 읽을 결과는 `--report` 로 UTF-8 파일에 쓴다.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
VOCAB = ROOT / "assets" / "level" / "curriculum_v2" / "vocab.json"
# 워크북은 인쇄물 하네스가 정본이다. 저장소에 사본을 두지 않는다.
WORKBOOK_DIR = ROOT.parents[1] / "06_인쇄물디자인_하네스" / "_workspace"


def load_json(path: Path) -> Any:
    with io.open(path, encoding="utf-8") as fp:
        return json.load(fp)


def build_index(vocab_items: list[dict]) -> tuple[dict, dict]:
    """(급수, 표면형) → source_key. 충돌은 따로 모은다.

    한 급수 안에 같은 표면형이 둘 이상이면 **동형이의어**다.
    워크북에는 뜻이 하나뿐이라 어느 쪽에 붙일지 알 수 없다 — 통째로 뺀다.
    """
    buckets: dict[tuple[int, str], list[str]] = defaultdict(list)
    for it in vocab_items:
        if it.get("kind") != "vocab":
            continue
        grade = it.get("topik_grade")
        surface = it.get("surface")
        key = it.get("source_key")
        if grade is None or not surface or not key:
            continue
        buckets[(grade, surface)].append(key)

    unique = {k: v[0] for k, v in buckets.items() if len(v) == 1}
    ambiguous = {k: v for k, v in buckets.items() if len(v) > 1}
    return unique, ambiguous


def clean(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def merge() -> dict:
    vocab = load_json(VOCAB)
    items = vocab["items"]
    unique, ambiguous = build_index(items)

    merged: dict[str, dict] = {}
    stats = {
        "workbook_rows": 0,
        "matched": 0,
        "skipped_ambiguous": 0,
        "skipped_no_match": 0,
        "skipped_empty": 0,
    }
    misses: list[dict] = []

    for grade in range(1, 7):
        path = WORKBOOK_DIR / f"topik{grade}.json"
        if not path.exists():
            raise SystemExit(f"워크북 부재: {path}")
        book = load_json(path)
        rows = book["words"] if isinstance(book, dict) else book
        for row in rows:
            stats["workbook_rows"] += 1
            term = clean(row.get("term"))
            if not term:
                stats["skipped_empty"] += 1
                continue

            meaning = clean(row.get("meaning"))
            roman = clean(row.get("rom"))
            if not meaning and not roman:
                stats["skipped_empty"] += 1
                continue

            pair = (grade, term)
            if pair in ambiguous:
                stats["skipped_ambiguous"] += 1
                misses.append({"grade": grade, "term": term, "why": "동형이의어"})
                continue
            key = unique.get(pair)
            if key is None:
                stats["skipped_no_match"] += 1
                misses.append({"grade": grade, "term": term, "why": "앱 어휘에 없음"})
                continue

            payload: dict[str, str] = {}
            if meaning:
                payload["en"] = meaning
            if roman:
                payload["roman"] = roman
            merged[key] = payload
            stats["matched"] += 1

    stats["app_vocab_rows"] = sum(1 for i in items if i.get("kind") == "vocab")
    stats["ambiguous_surfaces"] = len(ambiguous)
    return {"stats": stats, "merged": merged, "misses": misses}


def apply_to_db(merged: dict[str, dict]) -> dict:
    """`source_key` 로 찾아 `meanings` 를 채운다. 이미 있으면 건드리지 않는다."""
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import select  # noqa: PLC0415

    from db.session import SessionLocal  # noqa: PLC0415
    from domains.learning.models.learning_item import LearningItem  # noqa: PLC0415

    written = 0
    kept = 0
    absent = 0
    with SessionLocal() as db:
        rows = {
            r.source_key: r
            for r in db.scalars(
                select(LearningItem).where(LearningItem.source_key.in_(list(merged)))
            )
        }
        for key, payload in merged.items():
            row = rows.get(key)
            if row is None:
                absent += 1
                continue
            if row.meanings:  # 기존 값을 덮지 않는다
                kept += 1
                continue
            row.meanings = json.dumps(payload, ensure_ascii=False)
            written += 1
        db.commit()
    return {"written": written, "kept": kept, "absent": absent}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="DB 에 실제로 쓴다(.env 필요)")
    ap.add_argument("--out", help="병합본 JSON 을 이 경로에 쓴다")
    ap.add_argument("--report", help="사람이 읽을 요약을 이 경로에 UTF-8 로 쓴다")
    args = ap.parse_args()

    result = merge()
    stats = result["stats"]

    if args.out:
        with io.open(args.out, "w", encoding="utf-8") as fp:
            json.dump(result["merged"], fp, ensure_ascii=False, indent=1)

    if args.report:
        lines = [
            "# meanings 병합 — dry-run 결과",
            "",
            f"- 앱 어휘 행: {stats['app_vocab_rows']:,}",
            f"- 워크북 행: {stats['workbook_rows']:,}",
            f"- **병합 대상: {stats['matched']:,}**",
            f"- 건너뜀 · 동형이의어: {stats['skipped_ambiguous']:,}",
            f"- 건너뜀 · 앱에 없음: {stats['skipped_no_match']:,}",
            f"- 건너뜀 · 값 없음: {stats['skipped_empty']:,}",
            f"- 급수 안 표면형 충돌: {stats['ambiguous_surfaces']:,}",
            "",
            "## 미매칭 표본 (앞 40건)",
            "",
        ]
        for m in result["misses"][:40]:
            lines.append(f"- {m['grade']}급 `{m['term']}` — {m['why']}")
        with io.open(args.report, "w", encoding="utf-8") as fp:
            fp.write("\n".join(lines) + "\n")

    if args.apply:
        if not os.getenv("DATABASE_URL_DIRECT") and not os.getenv("DATABASE_URL_POOL"):
            raise SystemExit("DB 접속 정보 부재 — .env 를 채우고 다시 실행하라.")
        stats.update(apply_to_db(result["merged"]))

    # 콘솔에는 숫자만. 한글은 --report 로.
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
