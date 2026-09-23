"""chat_memory (자유대화 기억 저장소) — learning 도메인.

C6(2026-09-23, docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md): 자유대화(chat)가
통화마다 "지난번 이야기를 아는 척" 할 수 있도록 회원·언어별로 누적하는 요약 저장소.

⚠ 문서 원문은 "PK (member_id, language)"이지만, 이 코드베이스의 모든 회원×축 테이블
  (member_item_progress·member_language_level 등)은 **대리 PK + UniqueConstraint**
  패턴이다(합성 PK 를 쓰는 모델이 하나도 없다) — 그 관례를 따른다. 뜻(회원·언어당
  정확히 한 행)은 UniqueConstraint 로 그대로 강제된다.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import JSON, BigInteger, ForeignKey, Identity, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class ChatMemory(Base, TimestampMixin):
    __tablename__ = "chat_memory"
    __table_args__ = (
        UniqueConstraint("member_id", "language", name="uq_chat_memory_member_language"),
    )

    chat_memory_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE", name="fk_chat_memory_member"),
        comment="회원",
    )
    # (멀티랭귀지) member_id 와 묶어 유니크 — 학습 대상 언어가 바뀌면 다른 기억이다.
    language: Mapped[str] = mapped_column(Text, comment="학습 대상 언어(ISO 639-1)")

    summary: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''"),
        comment="누적 압축 요약(자유 텍스트, 상한 1200자) — merge 때마다 옛 요약+이번 통화 요약을 재압축",
    )
    # ⛔ JSONB 아님(프로젝트 규약 — sqlite 테스트 호환). 전부 문자열 배열이고, 상한은
    #   코드(chat_memory_service._merge_capped)가 지킨다 — DB 제약으로 강제하지 않는다.
    topics: Mapped[list] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'"), comment="최근 화제(최신 우선, 최대 10)",
    )
    facts: Mapped[list] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'"),
        comment="학습자에 대해 알게 된 사실(최신 우선, 최대 15)",
    )
    interests: Mapped[list] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'"),
        comment="대화에서 드러난 관심사(최신 우선, 최대 10)",
    )
    next_topics: Mapped[list] = mapped_column(
        JSON, nullable=False, server_default=text("'[]'"),
        comment="다음 통화에서 이어 말할 거리(최신 우선, 최대 5)",
    )
    # ⭐ 멱등 키 — 같은 call_id 로 두 번 merge 하면 건너뛴다(fire-and-forget 통화후
    #   파이프라인이 재시도·중복 실행돼도 기억이 두 번 안 쌓이게).
    last_call_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("call.call_id", ondelete="SET NULL", name="fk_chat_memory_last_call"),
        comment="이 값으로 이미 merge 했으면 다시 안 한다(멱등)",
    )
