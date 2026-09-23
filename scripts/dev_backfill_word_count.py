"""[dev/ops] call.user_word_count 백필 — C11(2026-09-23, 학습 달력·연속일 D8·D9).

이 컬럼은 새로 생긴 것이라 기존 통화는 전부 NULL 이다. `call_raw_data` 의
role='user' 행(전사)을 모아 `normalcall_service.fragment_user_word_count` 와
**같은 계산**(공백 분할, ja·zh 는 count_target_script_chars 로 글자수/2 반올림)으로
다시 채운다.

⛔ 전사가 하나도 없는 통화는 NULL 로 남긴다(0 으로 쓰지 않는다 — «집계 없음»과 «0»을
  가른다, C11 조건③).
⚠ 운영 실행은 bt-back 이 한다 — 여기서는 기본 dry-run(계산만 출력), --apply 로만 UPDATE.

사용법:
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_backfill_word_count.py --all
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_backfill_word_count.py 1602 1604 --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

import db.registry  # noqa: F401, E402
from core.config import settings  # noqa: E402
from db.engine import build_engine  # noqa: E402
from db.session import build_session_factory  # noqa: E402
from domains.learning.models.call import Call  # noqa: E402
from domains.learning.models.call_raw_data import CallRawData  # noqa: E402
from domains.learning.service.normalcall_service import fragment_user_word_count  # noqa: E402


def _user_segments(db, call_id: int) -> list[dict]:
    rows = db.scalars(
        select(CallRawData)
        .where(CallRawData.call_id == call_id, CallRawData.role == "user")
        .order_by(CallRawData.turn_index, CallRawData.call_raw_data_id)
    ).all()
    return [{"role": "user", "text": r.content} for r in rows]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("call_ids", nargs="*", type=int, help="지정하면 그 통화만(비우면 --all 필요)")
    ap.add_argument("--all", action="store_true", help="user_word_count 가 NULL 인 모든 통화")
    ap.add_argument("--apply", action="store_true", help="실제 UPDATE(기본은 dry-run)")
    args = ap.parse_args()
    if not args.call_ids and not args.all:
        raise SystemExit("call_id 를 지정하거나 --all 을 넘겨라.")

    engine = build_engine(settings)
    engine.echo = False
    db = build_session_factory(engine)()

    if args.all:
        calls = db.scalars(select(Call).where(Call.user_word_count.is_(None))).all()
    else:
        calls = db.scalars(select(Call).where(Call.call_id.in_(args.call_ids))).all()
    print(f"대상 통화 {len(calls)}건")

    changed = skipped = 0
    for call in calls:
        segments = _user_segments(db, call.call_id)
        count = fragment_user_word_count(segments, call.target_language or "ko")
        if count is None:
            print(f"[{call.call_id}] 사용자 전사 없음 — NULL 유지")
            skipped += 1
            continue
        print(f"[{call.call_id}] target={call.target_language} 전사 {len(segments)}행 → user_word_count={count}"
              f" (기존 {call.user_word_count})")
        if args.apply:
            call.user_word_count = count
        changed += 1

    if args.apply:
        db.commit()
        print(f"적용 끝 — {changed}건 갱신, {skipped}건 건너뜀(전사 없음)")
    else:
        print(f"dry-run 끝 — {changed}건 갱신 예정, {skipped}건 건너뜀 예정. 적용은 --apply")


if __name__ == "__main__":
    main()
