"""[dev 전용] 승급 직전 계정 시딩 — 실통화 1~2회로 자동 레벨업을 눈으로 확인하기 위한 도구.

무엇을 하나 (레벨 1 → 2 승급이 게이트가 가장 작아 대상으로 삼는다):
    1. 대상 회원을 레벨 1 로 세팅 + placement 이력(5일 전 백데이트 — G5 무관하지만 여유).
    2. 기존 레벨 상태 초기화(progress/evidence/history 삭제 — /__dev/level-reset 과 동일).
    3. 생존 청크 46개 중:
       - 22개 → MASTERED(observed, score 3.0)      : G2 잘씀 22/46 = 47.8% (문턱 50% 직전)
       - 20개 → INTRODUCED                          : G1 배움 44/46 = 95.7% (문턱 90% 충족)
       - 2개  → PRACTICING + 어제 통화의 E2 증거 2건씩(score 2.0)
       - 2개  → UNSEEN(행 없음)
    4. 남은 것: PRACTICING 2개 중 **1개만** 통화에서 성공 산출(E2/E3)하면
       → 3회 산출·2통화·2일 분산 충족 → MASTERED 23/46 = 50% → G2 통과 → 자동 승급 1→2.

사용법:
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_levelup_seed.py <이메일>

이후 시나리오:
    통화 1: 스크립트가 출력한 "타깃 표현"을 대화 중 한 번 자연스럽게 말한다 → 통화 종료
            → 분석 완료 시 자동 승급(다음 통화에서 비버의 "난이도 올려볼까" 멘트 확인).
⚠ prod 금지 — dev DB 전용(ENV 가드 포함).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402

import db.registry  # noqa: F401, E402
from core.config import settings  # noqa: E402
from db.engine import build_engine  # noqa: E402
from db.session import build_session_factory  # noqa: E402
from domains.account.models.member import Member  # noqa: E402
from domains.learning.models.call import Call  # noqa: E402
from domains.learning.models.item_evidence import ItemEvidence  # noqa: E402
from domains.learning.models.learning_item import LearningItem  # noqa: E402
from domains.learning.models.member_item_progress import MemberItemProgress  # noqa: E402
from domains.learning.models.member_level_history import MemberLevelHistory  # noqa: E402
from domains.learning.service.mastery_service import normalize_text, text_hash  # noqa: E402

MASTERED_COUNT = 22   # G2(50%) 직전 — 통화에서 1개만 더 잘 쓰면 23/46 통과
PRACTICING_COUNT = 2  # 타깃(어제 E2 2건 시딩 — 오늘 1건이면 3회·2통화·2일 충족)
INTRODUCED_COUNT = 20  # G1 = (22+2+20)/46 = 95.7% ≥ 90%


def main() -> None:
    if settings.ENV == "prod":
        raise SystemExit("prod 금지 — dev 전용 도구입니다.")
    if len(sys.argv) < 2:
        raise SystemExit("사용법: python scripts/dev_levelup_seed.py <이메일>")
    email = sys.argv[1].strip()

    engine = build_engine(settings)
    engine.echo = False  # dev 의 SQL 로깅 억제(시딩 출력 가독성)
    db = build_session_factory(engine)()
    now = datetime.now(timezone.utc)

    member = db.scalar(select(Member).where(Member.email == email, Member.deleted_at.is_(None)))
    if member is None:
        raise SystemExit(f"회원 없음: {email} (데모 페이지에서 먼저 가입/로그인 1회 필요)")
    mid = member.member_id

    chunks = db.scalars(
        select(LearningItem).where(LearningItem.kind == "chunk").order_by(LearningItem.seq_no)
    ).all()
    if len(chunks) < MASTERED_COUNT + PRACTICING_COUNT + INTRODUCED_COUNT:
        raise SystemExit(f"청크 부족({len(chunks)}) — 시드(learning_item) 먼저 확인")
    # practicing 타깃은 고정 문장만(빈칸형 "◯" 청크는 검출 매칭이 불안정 — 통화 확인용으로 부적합).
    fixed = [c for c in chunks if "◯" not in (c.surface or "")]
    slotted = [c for c in chunks if "◯" in (c.surface or "")]
    chunks = fixed[:MASTERED_COUNT] + fixed[MASTERED_COUNT:MASTERED_COUNT + PRACTICING_COUNT] + (
        fixed[MASTERED_COUNT + PRACTICING_COUNT:] + slotted
    )

    # 1) 기존 레벨 상태 백지화
    db.execute(delete(ItemEvidence).where(ItemEvidence.member_id == mid))
    db.execute(delete(MemberItemProgress).where(MemberItemProgress.member_id == mid))
    db.execute(delete(MemberLevelHistory).where(MemberLevelHistory.member_id == mid))

    # 2) 레벨 1 + placement 이력(백데이트)
    member.korean_level = 1
    db.add(MemberLevelHistory(
        member_id=mid, from_level=None, to_level=1, reason="placement",
        trigger_call_id=None, gate_snapshot=None, created_at=now - timedelta(days=5),
    ))

    # 3) 어제 통화(가짜, done) — practicing 타깃의 "1번째 통화·1일 전" 증거 담체
    yesterday_call = Call(
        member_id=mid, character_id=member.character_id or 1,
        call_date=now - timedelta(days=1), status="done", call_type="normal",
    )
    db.add(yesterday_call)
    db.flush()

    targets: list[str] = []
    for i, item in enumerate(chunks[: MASTERED_COUNT + PRACTICING_COUNT + INTRODUCED_COUNT]):
        if i < MASTERED_COUNT:
            db.add(MemberItemProgress(
                member_id=mid, item_id=item.item_id, status="mastered",
                provenance="observed", score=3.0,
                prompted_count=2, spontaneous_count=1,
                first_seen_at=now - timedelta(days=4), last_seen_at=now - timedelta(days=1),
                last_used_at=now - timedelta(days=1), mastered_at=now - timedelta(days=1),
            ))
        elif i < MASTERED_COUNT + PRACTICING_COUNT:
            targets.append(item.surface)
            db.add(MemberItemProgress(
                member_id=mid, item_id=item.item_id, status="practicing",
                provenance="observed", score=2.0, prompted_count=2,
                first_seen_at=now - timedelta(days=1), last_seen_at=now - timedelta(days=1),
                last_used_at=now - timedelta(days=1),
                first_call_id=yesterday_call.call_id, last_call_id=yesterday_call.call_id,
            ))
            for n in range(2):  # 어제 E2 2건 — 오늘 1건이면 성공 산출 3회(D14) 충족
                quote = f"{item.surface} 연습 {n + 1}"
                db.add(ItemEvidence(
                    member_id=mid, item_id=item.item_id, call_id=yesterday_call.call_id,
                    turn_index=n, grade_raw="E2", grade_final="E2", learner_quote=quote,
                    verified=True, score_delta=1.0,
                    normalized_text_hash=text_hash(normalize_text(quote)),
                    created_at=now - timedelta(days=1),
                ))
        else:
            db.add(MemberItemProgress(
                member_id=mid, item_id=item.item_id, status="introduced",
                provenance="observed", score=0.5, repeat_count=1,
                first_seen_at=now - timedelta(days=2), last_seen_at=now - timedelta(days=1),
            ))
    db.commit()

    print(f"시딩 완료 — member {mid} ({email}) / 레벨 1, 승급(→2)까지 잘씀 1개 남음")
    print(f"체크판: mastered {MASTERED_COUNT} / practicing {PRACTICING_COUNT} / "
          f"introduced {INTRODUCED_COUNT} / unseen {len(chunks) - MASTERED_COUNT - PRACTICING_COUNT - INTRODUCED_COUNT}")
    print("\n▶ 다음 통화에서 이 표현 중 하나를 자연스럽게 말하면 승급됩니다:")
    for t in targets:
        print(f"   · {t}")
    print("\n(통화 종료 → 분석 완료 후 /__levelcalldemo 의 '현재 저장된 레벨'이 2로 바뀌고,")
    print(" 그 다음 통화에서 비버가 '저번에 잘했으니 조금 어려운 것도 해볼까' 멘트를 합니다)")


if __name__ == "__main__":
    main()
