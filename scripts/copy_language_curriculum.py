"""언어별 커리큘럼(level·learning_item)을 다른 DB에서 그대로 복사한다.

왜 필요한가
-----------
멀티랭귀지(2026-07-22)를 붙일 때 ko 외 5개 언어(ja/en/zh/fr/vi)의 커리큘럼은
`scripts/curriculum/parse_lang.py` 로 **로컬에서 생성해 도그푸딩 DB에 직접 밀어넣었고,
생성물은 커밋되지 않았다** — `.gitignore` 가 `assets/level/curriculum_v2_*/grammar.json`
(+ vocab.json)을 제외한다. 그래서 `scripts/seed.py --language ja` 는 소스 파일이 없어
FileNotFoundError 로 죽는다.

그 결과 실서비스 DB 에는 ko 만 시드돼 있고, 비-ko 학습자가 레벨테스트를 마치면
member_language_level upsert 가 FK(`fk_mll_level` → level(language, level_no))
위반으로 터진다. 통화 5분을 다 하고 나서야 조용히 실패한다.

데이터 사본이 남아 있는 곳이 도그푸딩 DB 하나뿐이라, 재생성 대신 **실제로 돌던 그
데이터를 그대로 옮기는** 편이 안전하다(재생성물이 같다는 보장이 없다).

무엇을 하나
-----------
source DB 의 지정 언어 `level` + `learning_item` 행을 target DB 로 INSERT 한다.

  - **PK 는 옮기지 않는다.** 두 DB 의 id 공간이 달라(prod item 최대 11,141 /
    dogfood 29,105) 그대로 넣으면 충돌하거나 남의 id 를 덮는다. identity 가 새로
    부여하게 두고, 자연키로 중복만 막는다.
      level         : (language, level_no)
      learning_item : (language, source_key)
  - **ko 는 기본적으로 제외한다.** target 에 이미 있고, 건드리면 기존 학습 기록
    (member_item_progress.item_id / item_evidence.item_id)이 가리키는 행이 흔들린다.
  - 멱등하다 — ON CONFLICT DO NOTHING 이라 여러 번 돌려도 안전하다.

사용법
------
    # 미리보기(기본) — 아무것도 쓰지 않는다
    python scripts/copy_language_curriculum.py --source .env.local --target .env

    # 실제 복사
    python scripts/copy_language_curriculum.py --source .env.local --target .env --apply

    # 특정 언어만
    python scripts/copy_language_curriculum.py --source .env.local --target .env \
        --languages ja,en --apply

⚠ `--source`/`--target` 은 **필수**다. core.config 는 `.env` → `.env.local` 을 병합해
  뒤가 이기는데 이 저장소는 **`.env`=실서비스 / `.env.local`=dev** 로 뒤집혀 있다.
  기본값에 맡기면 옮기려던 방향이 뒤집힌다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parent.parent

# 복사 대상 테이블: (테이블, PK 컬럼, 자연키 컬럼들)
_TABLES = (
    ("level", "level_id", ("language", "level_no")),
    ("learning_item", "item_id", ("language", "source_key")),
)


def _url(env_file: str) -> str:
    path = Path(env_file)
    if not path.is_absolute():
        path = _REPO / env_file
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL_POOL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{env_file} 에 DATABASE_URL_POOL 이 없다")


def _ref(url: str) -> str:
    """로그용 DB 식별자(비밀 노출 없이 어느 DB 인지만)."""
    import re

    m = re.search(r"postgres\.([a-z0-9]+)", url)
    return m.group(1) if m else "?"


def _columns(engine: Engine, table: str) -> list[str]:
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t ORDER BY ordinal_position"
            ),
            {"t": table},
        )
        return [r[0] for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", required=True, help="가져올 DB 의 env 파일 (예: .env.local)")
    ap.add_argument("--target", required=True, help="넣을 DB 의 env 파일 (예: .env)")
    ap.add_argument("--languages", default="ja,en,zh,fr,vi",
                    help="쉼표 구분. 기본은 ko 를 뺀 5개 언어")
    ap.add_argument("--apply", action="store_true", help="실제로 INSERT (없으면 미리보기)")
    args = ap.parse_args()

    langs = [x.strip() for x in args.languages.split(",") if x.strip()]
    if "ko" in langs:
        # 실수로 ko 를 넣으면 기존 학습 기록이 가리키는 행과 뒤섞인다.
        raise SystemExit("ko 는 target 에 이미 있다 — 복사하면 기존 학습 기록이 위험하다")

    src_url, tgt_url = _url(args.source), _url(args.target)
    if src_url == tgt_url:
        raise SystemExit("source 와 target 이 같은 DB 다")

    print(f"source : {args.source}  ({_ref(src_url)})")
    print(f"target : {args.target}  ({_ref(tgt_url)})")
    print(f"언어   : {', '.join(langs)}")
    print(f"모드   : {'APPLY (쓰기)' if args.apply else 'DRY-RUN (미리보기)'}\n")

    src, tgt = create_engine(src_url), create_engine(tgt_url)

    for table, pk, natural in _TABLES:
        src_cols = _columns(src, table)
        tgt_cols = _columns(tgt, table)
        if src_cols != tgt_cols:
            raise SystemExit(
                f"[{table}] 두 DB 의 컬럼이 다르다 — 마이그레이션 상태를 먼저 맞춰라\n"
                f"  source: {src_cols}\n  target: {tgt_cols}"
            )
        cols = [c for c in src_cols if c != pk]  # PK 는 identity 가 새로 부여

        with src.connect() as sc:
            rows = sc.execute(
                text(f"SELECT {', '.join(cols)} FROM {table} "
                     f"WHERE language = ANY(:langs) ORDER BY {pk}"),
                {"langs": langs},
            ).mappings().all()

        with tgt.connect() as tc:
            before = tc.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE language = ANY(:langs)"),
                {"langs": langs},
            ).scalar()

        print(f"[{table}] source {len(rows)}행 / target 기존 {before}행")
        if not args.apply or not rows:
            continue

        placeholders = ", ".join(f":{c}" for c in cols)
        stmt = text(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT ({', '.join(natural)}) DO NOTHING"
        )
        inserted = 0
        with tgt.begin() as tc:  # 테이블 단위 트랜잭션 — 중간 실패 시 통째로 롤백
            for i in range(0, len(rows), 500):  # 배치 — 1.5만 행을 한 번에 보내지 않는다
                chunk = [dict(r) for r in rows[i:i + 500]]
                res = tc.execute(stmt, chunk)
                inserted += res.rowcount or 0
        print(f"  → 삽입 {inserted}행 (중복 스킵 {len(rows) - inserted})")

    print("\n완료 ✅" if args.apply else "\n미리보기만 했다. 실제 적용은 --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
