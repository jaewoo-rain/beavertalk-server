"""[dev/ops] 레벨↔진도 정합 — L4(2026-09-24, docs/plans/2026-09-23-레벨-커리큘럼-연결.md).

L1(진도 생성)·L2(재측정 이동)·L3(포인터 경계 돌파 시 레벨업)은 **새 통화**에서만 돈다.
그 세 가지가 생기기 **전부터** 있던 회원은 레벨과 진도가 어긋난 채로 남는다 — 운영
실측(member 88·92): 레벨은 1 인데 진도는 no=4(레벨2 차시)다. L3 는 «앞으로 경계를
넘을 때» 만 돌기 때문에, 이미 넘어버린 이 사람들은 스크립트가 아니면 영원히 안 고쳐진다.

⛔⛔ 어긋남은 **양방향**이다 — 한쪽만 고치면 안 된다:
  레벨이 앞섬(레벨2·진도no=1)  → **진도**를 그 레벨 첫 차시로 옮긴다
  진도가 앞섬(레벨1·진도no=4)  → **레벨**을 그 차시의 level_no 로 올린다 ← 이게 빠지면 88·92 는
                                    영원히 레벨1 로 레벨2 차시를 배운다

⛔ **양쪽 다 "앞선 쪽으로" 만** 맞춘다(내리지 않는다, D3). 레벨 갱신은 반드시
  `mastery_repository.upsert_language_level`(L2·L3 과 같은 함수, ko dual-write 포함) —
  직접 UPDATE 하지 않는다. history 1행은 `reason='curriculum_advance'` 를 재사용한다
  (새 값을 또 만들면 마이그레이션이 하나 더 는다 — «진도에서 레벨을 파생했다» 는 뜻이
  L3 와 같다). `trigger_call_id` 는 NULL(통화가 트리거가 아니다 — UNIQUE 는 NULL 다중
  허용이라 문제없다).

⛔⛔ **레벨 NULL(레벨테스트 미실시)은 진도가 앞선 경우에만 올린다.** 진도가 레벨1
  차시(no=1~3)에 있으면 **아무것도 하지 않는다** — NULL 을 1 로 채우면 그 회원이
  `needs_level_test = korean_level is None` 게이트를 잃어 레벨테스트를 다시 못 받는다.
  이게 이 스크립트에서 제일 틀리기 쉬운 지점이다.

⛔ 배운 기록(`cur_member_item`·`cur_member_lesson`)은 건드리지 않는다 — 포인터/레벨만.
⚠ 운영 실행은 bt-back 이 한다 — 여기서는 기본 dry-run(계산만 출력), --apply 로만 쓴다.

사용법:
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_align_progress_to_level.py
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_align_progress_to_level.py --language ja
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_align_progress_to_level.py 88 92 --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import db.registry  # noqa: F401, E402
from core.config import settings  # noqa: E402
from db.engine import build_engine  # noqa: E402
from db.session import build_session_factory  # noqa: E402
from domains.learning.models.curriculum import CurMemberProgress  # noqa: E402
from domains.learning.models.member_level_history import MemberLevelHistory  # noqa: E402
from domains.learning.repository import curriculum_repository as repo  # noqa: E402
from domains.learning.repository import mastery_repository  # noqa: E402

REASON = "curriculum_advance"  # L3 와 같은 값 재사용 — «진도에서 레벨을 파생했다» 는 뜻이 같다


def _decide(db: Session, prog: CurMemberProgress) -> dict:
    """한 (member, language) 진도 행의 조치를 **결정만** 한다(쓰지 않는다). 호출부가 apply 여부로 쓴다."""
    member_id, language = prog.member_id, prog.language
    lesson = repo.lesson_by_id(db, prog.lesson_id)
    if lesson is None:
        return {"action": "skip", "reason": "진도가 가리키는 차시 행이 없다(정합 이상)"}
    level_no = mastery_repository.get_language_level(db, member_id, language)
    lesson_level = lesson.level_no

    if level_no is not None and level_no > lesson_level:
        # 레벨이 앞섬 — 진도를 그 레벨 첫 차시로.
        target = repo.first_lesson_of_level(db, language, level_no)
        if target is None:
            return {"action": "skip", "reason": f"레벨{level_no} 차시 0건 — 옮길 곳이 없다",
                    "level": level_no, "no": lesson.no}
        if target.lesson_id == lesson.lesson_id:
            return {"action": "keep", "level": level_no, "no": lesson.no}
        return {
            "action": "progress_move", "level": level_no,
            "from_no": lesson.no, "to_no": target.no, "target_lesson_id": target.lesson_id,
        }

    # ⛔ 레벨 NULL 은 기준선 1 로만 비교한다(레벨테스트를 대신 배정하지 않는다) — 진도가
    #   레벨1 안에 있으면(no 가 커도) 그대로 둔다. 레벨1 을 넘겼을 때만 "앞섰다".
    baseline = level_no if level_no is not None else 1
    if lesson_level > baseline:
        # 진도가 앞섬 — 레벨을 그 차시의 level_no 로.
        return {
            "action": "level_up", "from_level": level_no, "to_level": lesson_level,
            "no": lesson.no,
        }

    return {"action": "keep", "level": level_no, "no": lesson.no}


def _apply(db: Session, prog: CurMemberProgress, decision: dict) -> None:
    member_id, language = prog.member_id, prog.language
    if decision["action"] == "progress_move":
        prog.lesson_id = decision["target_lesson_id"]
    elif decision["action"] == "level_up":
        mastery_repository.upsert_language_level(db, member_id, language, decision["to_level"])
        db.add(MemberLevelHistory(
            member_id=member_id, language=language,
            from_level=decision["from_level"], to_level=decision["to_level"],
            reason=REASON, trigger_call_id=None,
            created_at=datetime.now(timezone.utc),
        ))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("member_ids", nargs="*", type=int, help="지정하면 그 회원만(비우면 전체)")
    ap.add_argument("--language", help="지정하면 그 언어만(비우면 전체 언어)")
    ap.add_argument("--apply", action="store_true", help="실제 UPDATE(기본은 dry-run)")
    args = ap.parse_args()

    engine = build_engine(settings)
    engine.echo = False
    db = build_session_factory(engine)()

    stmt = select(CurMemberProgress)
    if args.member_ids:
        stmt = stmt.where(CurMemberProgress.member_id.in_(args.member_ids))
    if args.language:
        stmt = stmt.where(CurMemberProgress.language == args.language)
    rows = db.scalars(stmt.order_by(CurMemberProgress.member_id, CurMemberProgress.language)).all()
    print(f"대상 진도 행 {len(rows)}건")

    moved = raised = kept = 0
    for prog in rows:
        d = _decide(db, prog)
        action = d["action"]
        if action == "progress_move":
            print(f"[{prog.member_id}] {prog.language}: 레벨{d['level']} · no={d['from_no']} → "
                  f"진도이동 no={d['to_no']} (레벨이 앞섬)")
            moved += 1
        elif action == "level_up":
            frm = d["from_level"] if d["from_level"] is not None else "NULL"
            print(f"[{prog.member_id}] {prog.language}: 레벨{frm} · no={d['no']} → "
                  f"레벨상향 {frm}→{d['to_level']} (진도가 앞섬)")
            raised += 1
        elif action == "keep":
            print(f"[{prog.member_id}] {prog.language}: 레벨{d.get('level')} · no={d.get('no')} → 유지")
            kept += 1
        else:  # skip
            print(f"[{prog.member_id}] {prog.language}: 건너뜀 — {d.get('reason')}")
        if args.apply and action in ("progress_move", "level_up"):
            _apply(db, prog, d)

    if args.apply:
        db.commit()
        print(f"적용 끝 — 진도이동 {moved}건, 레벨상향 {raised}건, 유지 {kept}건")
    else:
        print(f"dry-run 끝(쓰기 0) — 진도이동 예정 {moved}건, 레벨상향 예정 {raised}건, 유지 {kept}건. 적용은 --apply")


if __name__ == "__main__":
    main()
