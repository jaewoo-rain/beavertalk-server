"""push_dispatch_log (발송 멱등 로그) — push 도메인.

예약전화 디스패처가 (alarm, 의도된 벽분 버킷) 단위로 UNIQUE INSERT 를 시도해
중복 발송을 막는 멱등 클레임 테이블. 오래된 로그는 주기적으로 purge 한다.

발송한 `call_id` 도 여기 남긴다 — 통화가 열릴 때 **서버가 그 통화의 캐릭터를
알람에서 직접 꺼내기 위한** 되짚기 열쇠다(아래 call_id 주석 참고).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class PushDispatchLog(Base, TimestampMixin):
    __tablename__ = "push_dispatch_log"

    push_dispatch_log_id: Mapped[int] = mapped_column(
        BigInteger, Identity(), primary_key=True
    )
    alarm_id: Mapped[int] = mapped_column(
        ForeignKey("alarm.alarm_id", ondelete="CASCADE"), index=True, comment="알람",
    )
    intended_fire_minute: Mapped[str] = mapped_column(
        Text, nullable=False, comment="의도된 벽분 버킷, 예 2026-07-06 08:00",
    )
    # 이 발송이 만든 통화 id(uuid4). 푸시 페이로드로 단말에 나갔다가, 사용자가 전화를
    # 받으면 앱이 `start.inbound_call_id` 로 **그대로 되돌려준다**.
    #
    # 왜 이걸 저장하나 — 통화 캐릭터를 클라가 정하지 않게 하려고. 예전엔 앱이
    # `start.character_id` 로 캐릭터를 지정했는데, 그러면 (a) 앱 버그로 엉뚱한 값이
    # 가고 (b) 앱을 고치면 미구매 유료 캐릭터도 부를 수 있었다. 이제 앱은 **자기가
    # 고르지 않은 불투명한 uuid** 만 돌려주고, 서버가 call_id → 이 로그 → alarm →
    # character 로 되짚는다. 위조해도 얻는 게 없다(남의 uuid 는 모르고, 알아도 그
    # 알람 주인이 아니면 거절된다).
    #
    # nullable 인 이유: 이 컬럼 추가 이전에 쌓인 로그가 있고, purge 로 사라질 때까지
    # 공존한다. UNIQUE 는 되짚기가 한 행으로 확정되게 한다.
    call_id: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, unique=True, comment="발송한 통화 id(uuid4) — 캐릭터 되짚기 열쇠",
    )

    __table_args__ = (
        UniqueConstraint(
            "alarm_id", "intended_fire_minute", name="uq_push_dispatch_alarm_minute",
        ),
        Index("ix_push_dispatch_log_created_at", "created_at"),
    )
