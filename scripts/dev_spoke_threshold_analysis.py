"""[dev] §12(2026-09-27, 앱 요청) — `_spoke_exists()` 를 전사 의존에서 뗄 때 쓸 임계값
후보(total_time >= N초)의 영향을 실측한다. **읽기 전용**(아무것도 안 바꾼다).

배경: `call_repository._spoke_exists()` 가 지금은 `call_raw_data.role='user' AND
content IS NOT NULL AND content <> ''` 만 본다 — 전사가 비면 통화가 아무리 길어도
「말 안 함」으로 잡혀 학습 달력·하루 한도·이어하기 판정(`has_call_in_window`)에서
빠진다. 설계(다) 채택 — 「user 전사 있음 **또는** total_time >= N초」. N 을 정하기
위해 이 스크립트가 두 표를 낸다:
  ① 지금 spoke=false 인 통화들의 total_time 분포(버킷)
  ② 후보 N 값별로 "몇 건이 새로 spoke=true 로 뒤집히는가" + "그 통화들을 가진
     (member, 로컬 날짜) 조합이 며칠이나 새로 '통화한 날'이 되는가"(달력·하루한도
     영향의 근사치 — 시간대는 Asia/Seoul 로 어림한다, 서비스 레이어의 실제 로컬
     날짜 계산과 약간 다를 수 있다).

⛔ 기본은 조회만(아무 것도 안 바꾼다) — 그래서 --apply 같은 옵션이 없다.
⚠ `.env` = 운영 / `.env.local` = 로컬이다(이름과 반대). 다른 체크아웃의 .env 를 쓰려면
`--env-root` 로 준다.

사용법:
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_spoke_threshold_analysis.py
    ... --env-root <.env 루트> --out spoke_threshold.txt
    ... --candidates 30,45,60,90,120,180,300   # 기본값과 같다(생략 가능)
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_DEFAULT_CANDIDATES = [30, 45, 60, 90, 120, 180, 300]
_BUCKETS_SQL = """
    CASE
        WHEN total_time IS NULL THEN 'null'
        WHEN total_time < 30 THEN '<30'
        WHEN total_time < 45 THEN '30-44'
        WHEN total_time < 60 THEN '45-59'
        WHEN total_time < 90 THEN '60-89'
        WHEN total_time < 120 THEN '90-119'
        WHEN total_time < 180 THEN '120-179'
        WHEN total_time < 300 THEN '180-299'
        ELSE '300+'
    END
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-root", default=None, help=".env 가 있는 루트(기본: 이 저장소)")
    ap.add_argument("--out", default=None, help="결과를 UTF-8 파일로도 저장(콘솔 한글 깨짐 회피)")
    ap.add_argument(
        "--candidates", default=",".join(map(str, _DEFAULT_CANDIDATES)),
        help="쉼표로 구분한 N(초) 후보 목록",
    )
    args = ap.parse_args()
    candidates = [int(x) for x in args.candidates.split(",") if x.strip()]

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
        say("=" * 70)
        say("① 지금 spoke=false 인 통화의 total_time 분포")
        say("=" * 70)
        rows = c.execute(text(f"""
            WITH spoke AS (
                SELECT c.call_id, c.total_time,
                       EXISTS (
                           SELECT 1 FROM call_raw_data r
                           WHERE r.call_id = c.call_id AND r.role = 'user'
                             AND r.content IS NOT NULL AND r.content <> ''
                       ) AS spoke_true
                FROM call c
            )
            SELECT {_BUCKETS_SQL} AS bucket, count(*) AS n
              FROM spoke
             WHERE spoke_true = false
             GROUP BY 1
             ORDER BY min(total_time) NULLS FIRST
        """)).fetchall()
        total_false = 0
        for r in rows:
            say("  %-8s %6d" % (r.bucket, r.n))
            total_false += r.n
        say("  합계(spoke=false): %d" % total_false)

        say("")
        say("=" * 70)
        say("② 후보 N(초)별 — 새로 spoke=true 로 뒤집히는 통화 수 · 새로 '말한 날'이 되는 (member,날짜) 조합 수")
        say("   (날짜는 Asia/Seoul 로 어림 — 서비스 레이어 실계산과 약간 다를 수 있음)")
        say("=" * 70)
        say("  %8s  %14s  %20s" % ("N(초)", "뒤집히는 통화", "새로 '말한 날'(근사)"))

        for n in candidates:
            flipped = c.execute(text("""
                SELECT count(*) FROM call c
                 WHERE c.total_time >= :n
                   AND NOT EXISTS (
                       SELECT 1 FROM call_raw_data r
                        WHERE r.call_id = c.call_id AND r.role = 'user'
                          AND r.content IS NOT NULL AND r.content <> ''
                   )
            """), {"n": n}).scalar_one()

            newly_spoken_days = c.execute(text("""
                WITH spoke AS (
                    SELECT c.call_id, c.member_id,
                           (c.call_date AT TIME ZONE 'Asia/Seoul')::date AS d,
                           c.total_time,
                           EXISTS (
                               SELECT 1 FROM call_raw_data r
                               WHERE r.call_id = c.call_id AND r.role = 'user'
                                 AND r.content IS NOT NULL AND r.content <> ''
                           ) AS spoke_true
                    FROM call c
                ),
                daily AS (
                    SELECT member_id, d,
                           bool_or(spoke_true) AS has_spoke_now,
                           bool_or(spoke_true OR total_time >= :n) AS has_spoke_at_n
                    FROM spoke GROUP BY member_id, d
                )
                SELECT count(*) FROM daily
                 WHERE NOT has_spoke_now AND has_spoke_at_n
            """), {"n": n}).scalar_one()

            say("  %8d  %14d  %20d" % (n, flipped, newly_spoken_days))

    if args.out:
        io.open(args.out, "w", encoding="utf-8", newline="").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
