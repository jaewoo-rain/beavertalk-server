"""[dev] call_raw_data 중복 턴 정리 — (call_id, turn_index) 가 두 벌 이상인 행에서 **가장 이른 행만 남긴다.**

왜 있나: 12차(2026-09-18) 이전 서버는 점진 flush 커서를 저장이 끝난 뒤에 올렸다. `svc.run_db` 는 스레드풀이라
await 가 취소돼도(fragment_end 로 TaskGroup 이 내려가는 순간) 그 스레드의 commit 은 끝난다 ⇒ 커서가 안 올라간
채 종료 저장이 같은 구간을 다시 써서 행이 두 벌이 됐다(실측: call_id=1644, 75행 / 고유 turn 65개 — turn 45~54).
코드는 고쳤지만 **이미 들어간 행은 그대로 남는다.** 이 스크립트가 그걸 지운다.

✔ 2026-09-19 정리 완료 — bt-back 이 `--all --apply` 로 **통화 39건 · 잉여 400행**을 지웠다(1644 는 75→65행, 남은 중복 통화 0).
  그 뒤 13차 B 가 (call_id, turn_index) 유니크 제약을 걸어 재발을 DB 가 막는다. 이 스크립트는 옛 데이터 정리용으로 남긴다.

⛔ 기본은 dry-run 이다. 실제로 지우려면 `--apply` 를 명시해야 한다(사장님·bt-back 몫).
⚠ `.env` = 운영 / `.env.local` = 로컬이다(이름과 반대). 다른 체크아웃의 .env 를 쓰려면 `--env-root` 로 준다.

사용법:
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_dedupe_raw_data.py 1644
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_dedupe_raw_data.py 1644 --env-root <.env 루트> --out dedupe.txt
    ... --apply              # 실제 삭제(가장 이른 행만 남긴다)
    ... --all                # call_id 없이 전수 점검(dry-run 권장)
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("call_id", nargs="?", type=int, default=None, help="점검·정리할 통화(생략하면 --all 필요)")
    ap.add_argument("--all", action="store_true", help="통화 전수 점검(중복이 있는 통화만 출력)")
    ap.add_argument("--apply", action="store_true", help="실제로 지운다(기본은 dry-run)")
    ap.add_argument("--env-root", default=None, help=".env 가 있는 루트(기본: 이 저장소)")
    ap.add_argument("--out", default=None, help="결과를 UTF-8 파일로도 저장(콘솔 한글 깨짐 회피)")
    args = ap.parse_args()
    if args.call_id is None and not args.all:
        sys.exit("⛔ call_id 를 주거나 --all 을 줘라.")

    if args.env_root:
        root = Path(args.env_root).resolve()
        if not (root / ".env").is_file():
            sys.exit("⛔ %s/.env 가 없다." % root)
        os.chdir(root)          # core.config 가 CWD 의 .env / .env.local 을 읽는다

    from sqlalchemy import create_engine, text      # noqa: E402 — env 정리 뒤에 읽는다

    from core.config import settings                # noqa: E402

    lines: list[str] = []

    def say(s: str) -> None:
        lines.append(s)
        print(s)

    engine = create_engine(settings.direct_url, poolclass=None)
    where = "" if args.call_id is None else "WHERE call_id = :cid"
    params = {} if args.call_id is None else {"cid": args.call_id}
    with engine.connect() as c:
        dup_rows = c.execute(text("""
            SELECT call_id, turn_index, count(*) AS n, min(call_raw_data_id) AS keep_id,
                   array_agg(call_raw_data_id ORDER BY call_raw_data_id) AS ids
              FROM call_raw_data
              %s
             GROUP BY call_id, turn_index
            HAVING count(*) > 1
             ORDER BY call_id, turn_index
        """ % where), params).fetchall()

        if args.call_id is not None:
            total, uniq = c.execute(text("""
                SELECT count(*), count(DISTINCT turn_index) FROM call_raw_data WHERE call_id = :cid
            """), params).one()
            say("call_id=%d — 행 %d개 / 고유 turn %d개" % (args.call_id, total, uniq))

        if not dup_rows:
            say("중복 0 — 지울 것이 없다.")
        else:
            by_call: dict[int, list] = {}
            for r in dup_rows:
                by_call.setdefault(r.call_id, []).append(r)
            for cid, rows in sorted(by_call.items()):
                turns = [int(r.turn_index) if r.turn_index is not None else None for r in rows]
                extra = sum(int(r.n) - 1 for r in rows)
                say("call_id=%d — 중복 turn %d개 %s · 지울 행 %d개" % (cid, len(rows), turns[:20], extra))
                for r in rows[:5]:
                    say("    turn %s: id %s → 남길 id %s" % (r.turn_index, list(r.ids), r.keep_id))
                if len(rows) > 5:
                    say("    … 외 %d턴" % (len(rows) - 5))

    delete_ids = [int(i) for r in dup_rows for i in r.ids if int(i) != int(r.keep_id)]
    say("합계: 지울 행 %d개 (%s)" % (len(delete_ids), "적용" if args.apply else "dry-run — 지우지 않았다"))
    if delete_ids and args.apply:
        with engine.begin() as c:
            done = c.execute(text("DELETE FROM call_raw_data WHERE call_raw_data_id = ANY(:ids)"),
                             {"ids": delete_ids}).rowcount
        say("삭제 완료: %d행" % done)

    if args.out:
        io.open(args.out, "w", encoding="utf-8", newline="").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
