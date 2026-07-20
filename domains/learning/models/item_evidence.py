"""item_evidence (항목 사용 증거 — append-only 감사 로그) — learning 도메인.

**UPDATE 금지 — 리플레이 재계산의 원본.** 통화후 분석의 검출 결과가 검증 게이트를
통과한 뒤 append 만 된다. member_item_progress 의 상태·점수·카운터는 전부 이 로그의
파생값 — 판정 규칙(점수 Δ·전이 조건)이 바뀌면 이 로그를 리플레이해 체크판 전체를
재계산할 수 있어야 한다. 행 수정·삭제는 그 재계산 가능성을 파괴한다.

- (멀티랭귀지) language(ISO 639-1) = 증거축. item_id 로 언어가 파생되긴 하나,
  member-only 집계(get_latest_history·증거통화 수 등)에서 조인 없이 언어 격리하려면
  증거 행에 language 를 박아야 한다(리스크 1 — 집계 오염 방지). learning_item.language
  와 항상 일치(앱 계층 보장). 기존 증거는 전부 'ko' 로 백필.
  인덱스 (member_id, language, call_id) — 언어별 증거통화 distinct 집계 커버.
- grade_raw: LLM 원판정(E1/E2/E3/F). grade_final: 서버 검증 게이트 후 최종 등급
  (E0/E1/E2/E3/F — E3→E1 앵무새 강등 등 반영). CHECK 없음 — call.status 컨벤션.
- verified: 서버 검증 게이트(인용 존재·에코·중복) 통과 여부.
- score_delta: 이 증거로 progress.score 에 실반영된 변화량(통화당 상한 적용 후) —
  리플레이 없이도 당시 반영값을 복원할 수 있게 기록.
- normalized_text_hash: 공백 정규화 인용문 해시 — 동일 인용 dedup·fast-track
  "서로 다른 문장" 판정(hash 상이 && Jaccard<0.5)용.
- created_at 단독(TimestampMixin 미사용) — append-only 라 updated_at 이 무의미.
- ix_evidence_member_item: 회원×항목 증거 시계열 조회(상태 전이 판정·리플레이) 커버.

설계: docs/20260709_1231_level-system-master-plan.md §3.3 / §5.2,
      docs/20260709_1346_level-system-detailed-mechanics.md ⑤~⑥,
      docs/plans/2026-07-20-multi-language-platform.md §3 T1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class ItemEvidence(Base):
    __tablename__ = "item_evidence"
    __table_args__ = (
        # 회원×항목 증거 시계열(상태 전이 판정·리플레이 재계산)
        Index("ix_evidence_member_item", "member_id", "item_id", "created_at"),
        # (멀티랭귀지) 회원×언어별 증거통화 distinct 집계(member-only 오염 방지)
        Index("ix_evidence_member_lang_call", "member_id", "language", "call_id"),
    )

    evidence_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE", name="fk_evidence_member"),
        comment="회원",
    )
    language: Mapped[str] = mapped_column(
        Text, comment="학습 대상 언어(ISO 639-1 — learning_item.language 와 일치)",
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("learning_item.item_id", ondelete="CASCADE", name="fk_evidence_item"),
        comment="학습 항목",
    )
    call_id: Mapped[int] = mapped_column(
        ForeignKey("call.call_id", ondelete="CASCADE", name="fk_evidence_call"),
        index=True, comment="증거가 나온 통화",
    )
    turn_index: Mapped[Optional[int]] = mapped_column(
        Integer, comment="USER 발화 턴 인덱스(전사 기준 — fast-track 선발화 판정용)",
    )

    grade_raw: Mapped[str] = mapped_column(
        Text, nullable=False, comment="LLM 원판정 등급(E1/E2/E3/F)",
    )
    grade_final: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="서버 검증 게이트 후 최종 등급(E0/E1/E2/E3/F — 강등 반영)",
    )
    learner_quote: Mapped[Optional[str]] = mapped_column(
        Text, comment="학습자 발화 원문 인용(검증 게이트 근거 — 전사 부분일치 검증)",
    )
    verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
        comment="서버 검증 게이트 통과 여부",
    )
    score_delta: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0"),
        comment="progress.score 실반영 변화량(통화당 상한 적용 후)",
    )
    normalized_text_hash: Mapped[Optional[str]] = mapped_column(
        Text, comment="정규화 인용문 해시(중복 dedup·fast-track 문장 상이 판정)",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="증거 기록 시각(append-only — updated_at 없음)",
    )
