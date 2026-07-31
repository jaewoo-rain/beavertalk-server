"""이미 쓰고 있던 캐릭터의 소유권을 소급 부여한다(1회성 정리).

왜 필요한가
-----------
통화 캐릭터를 서버가 정하도록 바꾸면서 **소유 검증**이 생겼다. 그전엔 검증이 전혀
없어서(`db.get(Character, id)` 만 했다) 사지 않은 캐릭터로도 통화가 됐고, prod 에서
미구매 Bibi($10)로 126 건이 진행됐다.

검증을 그냥 켜면 그 사용자들은 다음 통화부터 쓰던 캐릭터를 잃는다. 지금은 결제가
실제로 돌지 않아(무료 지급 = is_test_grant) 그들이 규칙을 어긴 것도 아니다.
**쓰던 캐릭터가 말없이 바뀌는 게 더 큰 사고**라, 검증을 켜기 전에 소유권을 준다.

무엇을 하나
-----------
"통화한 적 있는데 소유 기록이 없는 (member, character)" 조합에 member_character
행을 만든다. purchase_price=0 — 실제로 돈을 받지 않았으므로 매출로 잡히면 안 된다.

사용법
------
    # 미리보기(기본) — 아무것도 쓰지 않는다
    conda run -n beavertalk-server python scripts/grandfather_character_ownership.py

    # 실제 적용
    conda run -n beavertalk-server python scripts/grandfather_character_ownership.py --apply

⚠ 접속 DB 는 core.config 규칙을 따른다(.env → .env.local, 뒤가 우선).
  이 저장소에서 **.env = production, .env.local = dev** 다(뒤집혀 있으니 주의).
  실행 전 출력 첫 줄의 호스트를 눈으로 확인할 것.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# scripts/ 에서 바로 실행해도 프로젝트 모듈을 찾게 한다(다른 dev 스크립트와 동일).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 통화한 적은 있는데 소유 기록이 없는 (member, character) 조합.
_ORPHANS = text(
    """
    SELECT DISTINCT c.member_id, c.character_id, ch.name, ch.price
    FROM call c
    JOIN character ch ON ch.character_id = c.character_id
    LEFT JOIN member_character mc
      ON mc.member_id = c.member_id AND mc.character_id = c.character_id
    WHERE mc.member_id IS NULL
    ORDER BY c.member_id, c.character_id
    """
)

_GRANT = text(
    """
    INSERT INTO member_character (member_id, character_id, purchase_price, purchase_date)
    VALUES (:member_id, :character_id, 0, :now)
    ON CONFLICT (member_id, character_id) DO NOTHING
    """
)


def _read_pool_url(env_file: str) -> str:
    """지정한 env 파일에서 DATABASE_URL_POOL 만 뽑는다(병합·override 없이)."""
    path = Path(env_file)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / env_file
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL_POOL="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit(f"{env_file} 에 DATABASE_URL_POOL 이 없다")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="실제로 INSERT (없으면 미리보기)")
    # ⚠ 어느 DB 인지 **반드시 명시**하게 한다. core.config 는 .env → .env.local 을
    #   병합해서 뒤가 이기는데, 이 저장소는 .env=실서비스 / .env.local=dev 로
    #   뒤집혀 있다. 기본값에 맡기면 고치려던 DB 가 아닌 곳을 친다(실제로 겪었다).
    ap.add_argument("--env-file", required=True,
                    help="DATABASE_URL_POOL 을 읽을 파일 (.env=실서비스 / .env.local=dev)")
    args = ap.parse_args()

    url = _read_pool_url(args.env_file)
    host = url.split("@")[-1].split("/")[0] if "@" in url else "?"
    print(f"DB: {host}")
    print(f"모드: {'APPLY (쓰기)' if args.apply else 'DRY-RUN (미리보기)'}\n")

    engine = create_engine(url)
    with sessionmaker(bind=engine)() as db:
        rows = db.execute(_ORPHANS).all()
        if not rows:
            print("소급 부여할 대상 없음.")
            return 0

        print(f"대상 {len(rows)} 건:")
        for r in rows:
            print(f"  member={r.member_id} character={r.character_id} {r.name} (정가 {r.price})")

        if not args.apply:
            print("\n미리보기만 했다. 실제 적용하려면 --apply 를 붙여라.")
            return 0

        now = datetime.now(timezone.utc)
        granted = 0
        for r in rows:
            res = db.execute(
                _GRANT,
                {"member_id": r.member_id, "character_id": r.character_id, "now": now},
            )
            granted += res.rowcount or 0
        db.commit()  # 스크립트도 명시적 커밋(프로젝트 컨벤션)
        print(f"\n부여 완료: {granted} 건 (purchase_price=0 — 매출 아님)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
