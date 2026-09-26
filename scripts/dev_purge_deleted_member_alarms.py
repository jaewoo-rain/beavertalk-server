"""[dev] 탈퇴 회원(member.deleted_at IS NOT NULL)에게 남은 alarm·device_token 일회성 정리.

왜 필요한가: S3(2026-09-26, Play 심사 대비) 이전에는 회원 탈퇴(소프트 삭제)가 alarm·
device_token 을 지우지 않아, 이미 탈퇴한 회원의 알람이 예약전화 발송 대상에 계속
남아 있었다(운영 실측: member 2, 2026-07-05 탈퇴, 알람 2개 보유). 코드는 이제 새로
탈퇴하는 회원부터 막지만(member_service.delete()), **이미 탈퇴한 회원의 잔존분**은
과거 데이터라 코드 배포만으로는 안 지워진다. 이 스크립트가 그 잔존분만 지운다.

⛔ 기본은 dry-run 이다. 실제로 지우려면 `--apply` 를 명시해야 한다(운영 실행은 bt-back 몫
— 빌더는 실행하지 않는다).
⚠ `.env` = 운영 / `.env.local` = 로컬이다(이름과 반대). 다른 체크아웃의 .env 를 쓰려면
`--env-root` 로 준다.

사용법:
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_purge_deleted_member_alarms.py
    ... --env-root <.env 루트> --out purge.txt
    ... --apply              # 실제 삭제
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
    ap.add_argument("--apply", action="store_true", help="실제로 지운다(기본은 dry-run)")
    ap.add_argument("--env-root", default=None, help=".env 가 있는 루트(기본: 이 저장소)")
    ap.add_argument("--out", default=None, help="결과를 UTF-8 파일로도 저장(콘솔 한글 깨짐 회피)")
    args = ap.parse_args()

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
    with engine.connect() as c:
        members = c.execute(text("""
            SELECT m.member_id, m.deleted_at,
                   (SELECT count(*) FROM alarm a WHERE a.member_id = m.member_id) AS alarm_n,
                   (SELECT count(*) FROM device_token d WHERE d.member_id = m.member_id) AS token_n
              FROM member m
             WHERE m.deleted_at IS NOT NULL
               AND (
                     EXISTS (SELECT 1 FROM alarm a WHERE a.member_id = m.member_id)
                  OR EXISTS (SELECT 1 FROM device_token d WHERE d.member_id = m.member_id)
               )
             ORDER BY m.member_id
        """)).fetchall()

        if not members:
            say("탈퇴 회원의 잔존 alarm·device_token 0 — 지울 것이 없다.")
        else:
            total_alarm = sum(r.alarm_n for r in members)
            total_token = sum(r.token_n for r in members)
            say("탈퇴 회원 %d명 — alarm %d개 · device_token %d개 잔존" % (
                len(members), total_alarm, total_token,
            ))
            for r in members:
                say("  member_id=%d (탈퇴 %s) — alarm %d개 · device_token %d개" % (
                    r.member_id, r.deleted_at, r.alarm_n, r.token_n,
                ))

    say("적용" if args.apply else "dry-run — 지우지 않았다")
    if members and args.apply:
        member_ids = [r.member_id for r in members]
        with engine.begin() as c:
            da = c.execute(text("DELETE FROM alarm WHERE member_id = ANY(:ids)"),
                           {"ids": member_ids}).rowcount
            dt = c.execute(text("DELETE FROM device_token WHERE member_id = ANY(:ids)"),
                           {"ids": member_ids}).rowcount
        say("삭제 완료: alarm %d개 · device_token %d개" % (da, dt))

    if args.out:
        io.open(args.out, "w", encoding="utf-8", newline="").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
