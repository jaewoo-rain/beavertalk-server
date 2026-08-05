"""call (전화/통화) — learning 도메인."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from domains.account.models.member import Member
    from domains.commerce.models.character import Character
    from domains.learning.models.call_raw_data import CallRawData
    from domains.learning.models.sentence import Sentence


class Call(Base, TimestampMixin):
    __tablename__ = "call"
    __table_args__ = (
        Index("ix_call_member_date", "member_id", "call_date"),  # 내 통화 최신순
    )

    call_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE"), index=True, comment="회원",
    )
    character_id: Mapped[int] = mapped_column(
        ForeignKey("character.character_id", ondelete="RESTRICT"), index=True, comment="캐릭터",
    )
    # (멀티랭귀지) 이 통화의 학습 대상 언어 — member_language_level·커리큘럼 선별·
    # 증거/이력 집계 스코프를 전부 결정. 기존 통화는 server_default 'ko'.
    target_language: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ko'"),
        comment="이 통화의 학습 대상 언어(ISO 639-1)",
    )
    call_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="전화 날짜")
    total_time: Mapped[Optional[int]] = mapped_column(Integer, comment="총 통화 시간(초)")
    summary: Mapped[Optional[str]] = mapped_column(Text, comment="대화 내용 한 줄 요약")
    feedback: Mapped[Optional[str]] = mapped_column(
        Text, comment="통화 코칭 한 문장(통화후 분석 생성)",
    )
    pron_feedback: Mapped[Optional[str]] = mapped_column(
        Text, comment="발음 LLM 한마디 캐시",
    )
    pron_feedback_n: Mapped[Optional[int]] = mapped_column(
        Integer, comment="발음 한마디 캐시 무효화키(생성 시점 counted 복습 수)",
    )
    rating: Mapped[Optional[int]] = mapped_column(Integer, comment="만족도(1~3점)")
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'ongoing'"),
        comment="분석 상태(ongoing/analyzing/done/failed)",
    )
    mode: Mapped[Optional[str]] = mapped_column(
        Text, comment="감지된 통화 모드(conversation/study/unknown)",
    )
    call_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'normal'"),
        comment="통화 종류(normal/level_test)",
    )
    assessed_level: Mapped[Optional[int]] = mapped_column(
        Integer, comment="레벨테스트 판정 결과(1~13, level_test 전용)",
    )
    assessment_note: Mapped[Optional[str]] = mapped_column(
        Text, comment="판정 근거(모델 reasoning — 감사·디버깅용)",
    )
    # (D15) 유효통화 컬럼 3종(is_valid_call/user_turn_count/user_char_count)은 폐지 —
    # 통화 수 파생값은 item_evidence 의 "증거통화"(distinct call)로 계산한다.

    # ── 원가 계기판 2단계: Live usage 영속화 (2026-08-05) ────────────────────
    # 🧒 왜 DB 에 남기나: 통화 원가는 지금까지 로그로만 봤는데 Cloud Logging 보존이 30일이라
    #   그 뒤엔 사라진다. 무제한 플랜을 열면 원가 추이를 계속 봐야 하므로 통화 행에 남긴다.
    #
    # ⛔ 전부 NULL 허용이다. NULL = "계측이 안 됐다"(구 통화·모킹 세션·Live 실패)이고
    #   0 = "정말 0 토큰"이다 — 둘을 구별할 수 있어야 표본을 신뢰할 수 있다.
    #
    # ⛔ 원가(달러) 컬럼은 **일부러 만들지 않았다.** 단가는 벤더가 바꾼다. 토큰은 사실이고
    #   원가는 파생 계산이라, 달러를 박아 두면 단가가 바뀐 순간 과거와 현재를 같은 잣대로
    #   못 본다. 산식은 normalcall_service.estimate_usage_cost_usd 한 곳에만 둔다.
    #   (레벨 시스템 관통 원칙 ② — 증거가 원본, 나머지는 파생 계산.)
    #
    # 왜 모달리티 4항이 각각 컬럼인가: 단가가 전부 다르다(입력 오디오 $3 / 입력 텍스트 $0.5 /
    # 출력 오디오 $12 / 출력 텍스트 $2). 합쳐 두면 원가를 계산할 수가 없다.
    usage_msgs: Mapped[Optional[int]] = mapped_column(
        Integer, comment="Live usage 관측 메시지 수(0/NULL = 미수신)",
    )
    usage_in_audio: Mapped[Optional[int]] = mapped_column(
        BigInteger, comment="입력 오디오 토큰 합($3.00/1M)",
    )
    usage_in_text: Mapped[Optional[int]] = mapped_column(
        BigInteger, comment="입력 텍스트 토큰 합($0.50/1M)",
    )
    usage_out_audio: Mapped[Optional[int]] = mapped_column(
        BigInteger, comment="출력 오디오 토큰 합($12.00/1M)",
    )
    usage_out_text: Mapped[Optional[int]] = mapped_column(
        BigInteger, comment="출력 텍스트 토큰 합($2.00/1M)",
    )
    usage_total: Mapped[Optional[int]] = mapped_column(
        BigInteger, comment="API 총 토큰 합(모달리티 4항의 합이 아니다 — thoughts·cached 포함)",
    )
    usage_peak_prompt: Mapped[Optional[int]] = mapped_column(
        Integer, comment="이 통화가 도달한 최대 컨텍스트(압축·트리거 튜닝의 핵심 지표)",
    )
    # 나머지 요약 원본. ⛔ JSONB 아님 — 프로젝트 규약(sqlite 테스트 호환). 집계는 위 컬럼이
    # 담당하므로 JSONB 의 인덱싱 이점이 필요 없다. ⛔ 턴별 시계열은 넣지 않는다(통화당
    # 최대 400엔트리 ≈ 48KB — 1만 통화면 480MB). 시계열이 필요한 건 조사 기간뿐이고 로그로 충분.
    usage_json: Mapped[Optional[dict]] = mapped_column(
        JSON, comment="usage 요약 원본(dropped/monotonic/last/thoughts/기타 모달리티/재연결·압축 수)",
    )

    member: Mapped["Member"] = relationship(back_populates="calls")
    character: Mapped["Character"] = relationship(lazy="select")  # 단방향(필요 시 쿼리에서 joinedload)
    raw_data: Mapped[list["CallRawData"]] = relationship(
        back_populates="call", cascade="all, delete-orphan", passive_deletes=True,
    )
    sentences: Mapped[list["Sentence"]] = relationship(
        back_populates="call", cascade="all, delete-orphan", passive_deletes=True,
    )
