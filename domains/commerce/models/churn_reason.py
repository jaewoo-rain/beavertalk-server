"""churn_reason (해지 사유) — commerce 도메인. §17(2026-09-28, 앱요청 §17).

설계: docs/plans/2026-09-28-알람시간대-해지사유-5-17.md §17.

- 유니크 키는 **(member_id, subscribe_id)** — 「어느 구독이 끝났나」로 응답을 식별한다.
  앱이 subscribe_id 를 보내고(subscription_status_dto.dart 가 이미 파싱) 서버는
  그 행이 이 회원 것인지만 검증한다(위조 불가) — 서버가 resolve_status 로 스스로
  「지금 만료된 구독」을 추론하면, 답하기 전에 재구독한 회원의 직전 만료 응답을
  거절하거나 엉뚱한 행에 붙이는 구멍이 생긴다.
- ⛔ `subscribe` 테이블에 칸을 붙이지 않는다 — 구독 행은 **결제 상태 기계**이고
  subscription_status 가 읽는 표다. 설문 값을 섞으면 그 표가 넓어진다.
- 두 FK 모두 `ON DELETE SET NULL` + nullable — 해지 사유는 법적 보존 의무가 없지만,
  CASCADE 로 회원 삭제 때 같이 지우면 이탈 분석 자체가 사라진다. SET NULL 이면
  reason·offer_shown·created_at 은 남고 **사람과의 연결만 끊긴다**(결제 3표 —
  payment·subscribe·iap_receipt — 와 같은 패턴, S2). 그래서 탈퇴 회원의 행이
  여럿이면 유니크가 (member_id, subscribe_id) = (NULL, NULL) 로 전부 부딪힌다 —
  `NULLS NOT DISTINCT` 로 하나로 뭉친다(PG 17.6, postgresql_nulls_not_distinct).
- 중복 응답(재설치 후 다른 답)은 `ON CONFLICT DO UPDATE`(마지막 값이 최신 의사) —
  서비스 계층에서 upsert 한다.
- `offer_shown` 은 서버가 추론하지 않고 **앱이 보낸 값을 그대로 저장**한다 — 해지방어
  오퍼 시트가 실제로 떴는지는 앱만 안다(할인 이벤트가 없어 안 떴을 수도 있다).
- `end_date_snapshot` 은 응답 당시 그 구독의 만료 시각(참고용 — 판정에는 안 쓴다).
- 관리자 집계 API 는 만들지 않는다(YAGNI — `SELECT reason, count(*) GROUP BY 1` 한
  줄이면 충분하고, 필요해지면 이 컬럼들로 스키마 변경 없이 붙일 수 있다).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin

# 모델과 마이그레이션이 같은 목록이어야 한다(sqlite 는 create_all, 운영은 Alembic).
# 와이어 코드는 flutter WinbackReason(otherApp) 을 snake_case 로 정규화한 것 —
# 앱은 아직 이 API 를 호출하지 않는다(§17 순서 — 먼저 해도 깨질 게 없다).
REASONS = ("expensive", "unused", "missing", "other_app", "other")


class ChurnReason(Base, TimestampMixin):
    __tablename__ = "churn_reason"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('expensive','unused','missing','other_app','other')",
            name="ck_churn_reason_reason",
        ),
        UniqueConstraint(
            "member_id", "subscribe_id", name="uq_churn_reason_member_subscribe",
            postgresql_nulls_not_distinct=True,
        ),
    )

    churn_reason_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("member.member_id", ondelete="SET NULL"), index=True, comment="회원",
    )
    subscribe_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("subscribe.subscribe_id", ondelete="SET NULL"), comment="어느 구독이 끝났나",
    )
    reason: Mapped[str] = mapped_column(
        Text, nullable=False, comment="expensive|unused|missing|other_app|other",
    )
    offer_shown: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="해지방어 오퍼 시트가 실제로 떴는가(앱이 보낸 값 그대로)",
    )
    end_date_snapshot: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="응답 시점 그 구독의 만료 시각(참고용, 판정에 안 씀)",
    )
