"""cur_* — 주제별 커리큘럼(차시) 체계. **새 학습 체계의 유일한 학습 데이터 원본.**

설계: docs/20260912_0330_cur-스키마-설계.md (v2, db-architect 검수 반영) · 배경 docs/20260912_0230_주제별-커리큘럼-분석.md
사장님 결정(2026-09-12) 1~13 이 이 파일의 근거다 — 특히:
  #1·#7  13단계 = 1 생존회화(청크 46, 차시 3) + 2~13 = A1~C4
  #2     표현학습은 문법·필수·핵심·지원어휘 전부를 가르친다(role 4종)
  #4     해금 = 그 차시 항목 전부 드릴 완료(퀴즈 정오 무관)
  #11    이어 연습 문법은 뒤 차시에서 또 가르친다 ⇒ 회원 항목 기록은 (회원, 차시, 항목) 별

⛔⛔ 원칙 — 통화·진도 코드는 **cur_* + member + character + call** 만 읽는다.
    learning_item · member_item_progress · item_evidence · member.korean_level · call.expression_result 는
    새 경로에서 읽지도 쓰지도 않는다(옛 체계는 그대로 돌고, 폐기 시점은 별건).
    유일한 예외는 시드 적재 때 청크 46 을 옛 learning_item 에서 **한 번 복사**하는 것(결정 #13).

컨벤션(기존 모델과 같다):
- JSON 류는 TEXT(JSON 문자열) — 테스트가 sqlite 인메모리라 JSONB 금지(learning_item.py 와 같은 이유).
- 상태 값 CHECK 는 영문 코드만(한글 CHECK 문자열은 인코딩 사고 지점 — 검수 P2).
- ⛔ 안 읽는 컬럼은 싣지 않는다(사장님 2026-09-12 «쓸데없는 컬럼 정리»): 시드의 등급·CEFR·빈도·출처·기능(F코드)·
  모범 대화·성공 조건·가드레일은 assets/curriculum_v3/cur_seed.json 에만 있다 — 필요해지면 컬럼을 추가하고 로더에 한 줄.
- Member 쪽 컬렉션 relationship 은 두지 않는다(회원당 수천 행 — member_item_progress 의 교훈 그대로).
- 비정규화는 `cur_lesson.item_count` 하나뿐(로더가 재계산). 드릴 수·레벨은 조인으로 센다(검수 P1-8).
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
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base

# ── 값 목록(모델과 마이그레이션이 같은 목록이어야 한다 — sqlite 는 create_all, 운영은 Alembic) ──
ITEM_KINDS = ("vocab", "grammar", "chunk")
LESSON_ITEM_ROLES = ("grammar", "must", "core", "support", "chunk")   # 표현학습 순서 = 이 순서
TOPIC_KINDS = ("conv", "support")                                     # 회화 / 지원
MEMBER_LESSON_STATUSES = ("learning", "expression_done", "freetalk_done")
CALL_COURSES = ("expression", "freetalk")
LEVEL_MIN, LEVEL_MAX = 1, 13


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class CurTopic(Base):
    """주제 65 (회화 56 + 지원 9). code T01~T65."""

    __tablename__ = "cur_topic"
    __table_args__ = (CheckConstraint(_in("kind", TOPIC_KINDS), name="ck_cur_topic_kind"),)

    topic_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True, nullable=False, comment="T01~T65")
    area: Mapped[str] = mapped_column(Text, nullable=False, comment="영역(나와 사람 …)")
    name: Mapped[str] = mapped_column(Text, nullable=False, comment="주제명")
    kind: Mapped[str] = mapped_column(Text, nullable=False, comment="conv(회화) / support(지원)")


class CurItem(Base):
    """학습 항목 — 어휘 10,636 + 문법 462 + 청크 46 을 **한 테이블**에.

    - key: 시드의 정체성 키(어휘 `형01`·`호00/호01`, 문법 이름, 청크 문장). UNIQUE(language, kind, key) = 재적재 멱등 키.
    - guide: 길잡이말 — 동형어 뜻을 가르는 유일한 단서(비버 설명 후보). description: 문법 설명.
    - surface: 판정·발화 표면형(어휘 = 첨자 뗀 표제어, 문법 = 주형, 청크 = 문장).
    - retired_at: 재적재로 빠진 항목은 **지우지 않고** 은퇴시킨다 — 회원 진도가 item_id 로 묶여 있어서(검수 P1-9).
    """

    __tablename__ = "cur_item"
    __table_args__ = (
        UniqueConstraint("language", "kind", "key", name="uq_cur_item_key"),
        CheckConstraint(_in("kind", ITEM_KINDS), name="ck_cur_item_kind"),
        CheckConstraint(f"level_no BETWEEN {LEVEL_MIN} AND {LEVEL_MAX}", name="ck_cur_item_level"),
        Index("ix_cur_item_topic", "topic_id"),
    )

    item_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ko'"), comment="학습 언어 코드")
    kind: Mapped[str] = mapped_column(Text, nullable=False, comment="vocab / grammar / chunk")
    key: Mapped[str] = mapped_column(Text, nullable=False, comment="시드 정체성 키(어휘 표제어+첨자 · 문법 이름 · 청크 문장)")
    surface: Mapped[str] = mapped_column(Text, nullable=False, comment="판정·발화 표면형")
    meanings: Mapped[Optional[str]] = mapped_column(Text, comment='로케일별 뜻 JSON 문자열 {"en": "..."}')
    pos: Mapped[Optional[str]] = mapped_column(Text, comment="품사(어휘)")
    guide: Mapped[Optional[str]] = mapped_column(Text, comment="길잡이말(동형어 뜻 고정)")
    level_no: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="1=청크 · 2~13 = 원 단계")
    topic_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cur_topic.topic_id", ondelete="RESTRICT", name="fk_cur_item_topic"), comment="어휘의 주제"
    )
    description: Mapped[Optional[str]] = mapped_column(Text, comment="문법 설명")
    examples: Mapped[Optional[str]] = mapped_column(Text, comment="예문 JSON 배열 문자열(최대 3)")
    retired_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="재적재로 빠진 항목의 은퇴 시각(NULL=현역). 지우지 않는다"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CurLesson(Base):
    """차시 491 = 레벨1 생존회화 3 + A1~C4 488. `no` 가 곧 진도 순서(1..491). probes = 프리토킹 유도 질문 후보(기록·후보)."""

    __tablename__ = "cur_lesson"
    __table_args__ = (
        UniqueConstraint("language", "no", name="uq_cur_lesson_no"),
        UniqueConstraint("language", "code", name="uq_cur_lesson_code"),
        CheckConstraint(f"level_no BETWEEN {LEVEL_MIN} AND {LEVEL_MAX}", name="ck_cur_lesson_level"),
        Index("ix_cur_lesson_level_no", "language", "level_no", "no"),
    )

    lesson_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ko'"))
    no: Mapped[int] = mapped_column(Integer, nullable=False, comment="진도 순서 1..491")
    code: Mapped[str] = mapped_column(Text, nullable=False, comment="L1-S01-1 · A1-T01-1 …")
    level_no: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="1~13")
    topic_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cur_topic.topic_id", ondelete="RESTRICT", name="fk_cur_lesson_topic"), comment="레벨1 은 NULL"
    )
    situation: Mapped[str] = mapped_column(Text, nullable=False, comment="프리토킹 상황명")
    partner: Mapped[Optional[str]] = mapped_column(Text, comment="상대역 — 상황 묘사용(비버가 되라는 뜻 아님)")
    probes: Mapped[Optional[str]] = mapped_column(Text, comment="유도 질문 JSON 배열 문자열")
    item_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"), comment="로더가 재계산")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class CurLessonItem(Base):
    """차시가 가르치는 항목. role 순(문법 → 필수 → 핵심 → 지원 / 청크)이 표현학습 순서, seq 가 차시 안 순번."""

    __tablename__ = "cur_lesson_item"
    __table_args__ = (
        UniqueConstraint("lesson_id", "seq", name="uq_cur_lesson_item_seq"),
        CheckConstraint(_in("role", LESSON_ITEM_ROLES), name="ck_cur_lesson_item_role"),
        Index("ix_cur_lesson_item_item", "item_id"),
    )

    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("cur_lesson.lesson_id", ondelete="CASCADE", name="fk_cur_li_lesson"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("cur_item.item_id", ondelete="RESTRICT", name="fk_cur_li_item"), primary_key=True
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, comment="grammar / must / core / support / chunk")
    seq: Mapped[int] = mapped_column(SmallInteger, nullable=False, comment="차시 안 순번(1부터)")


class CurMemberProgress(Base):
    """회원의 «지금 어디» — 언어당 1행. 레벨은 lesson 조인으로 센다(비정규화 없음)."""

    __tablename__ = "cur_member_progress"

    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE", name="fk_cur_mp_member"), primary_key=True
    )
    language: Mapped[str] = mapped_column(Text, primary_key=True, server_default=text("'ko'"))
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("cur_lesson.lesson_id", ondelete="RESTRICT", name="fk_cur_mp_lesson"), nullable=False,
        comment="지금 차시",
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CurMemberLesson(Base):
    """회원 × 차시 진도. status 는 단조(learning → expression_done → freetalk_done) — 항목이 늘어도 되돌리지 않는다."""

    __tablename__ = "cur_member_lesson"
    __table_args__ = (
        CheckConstraint(_in("status", MEMBER_LESSON_STATUSES), name="ck_cur_ml_status"),
        # 시각 ↔ 상태 정합(검수 P2-13): expression_done 이상이면 expression_done_at 이 있어야 한다
        CheckConstraint(
            "(status = 'learning') OR (expression_done_at IS NOT NULL)", name="ck_cur_ml_expression_ts"
        ),
        CheckConstraint(
            "(status <> 'freetalk_done') OR (freetalk_done_at IS NOT NULL)", name="ck_cur_ml_freetalk_ts"
        ),
    )

    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE", name="fk_cur_ml_member"), primary_key=True
    )
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("cur_lesson.lesson_id", ondelete="RESTRICT", name="fk_cur_ml_lesson"), primary_key=True
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'learning'"))
    expression_calls: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"), comment="표현학습 통화 횟수")
    expression_done_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="항목 전부 드릴 완료(결정 #4)")
    freetalk_call_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("call.call_id", ondelete="SET NULL", name="fk_cur_ml_freetalk_call"), comment="프리토킹 한 통화"
    )
    freetalk_done_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="한 번 하면 끝(결정 #3)")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CurMemberItem(Base):
    """회원 × 차시 × 항목 — 표현학습 판정 원본. 결정 #11: 같은 문법이라도 차시마다 따로 «배웠다/맞혔다».

    drilled_at·quiz_passed_at 은 단조(처음 찍힌 값 유지). 퀴즈 오답은 횟수만 센다 — 진도를 막지 않는다(결정 #4).
    """

    __tablename__ = "cur_member_item"
    __table_args__ = (Index("ix_cur_mi_member_lesson", "member_id", "lesson_id"),)

    member_id: Mapped[int] = mapped_column(
        ForeignKey("member.member_id", ondelete="CASCADE", name="fk_cur_mi_member"), primary_key=True
    )
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("cur_lesson.lesson_id", ondelete="RESTRICT", name="fk_cur_mi_lesson"), primary_key=True
    )
    item_id: Mapped[int] = mapped_column(
        ForeignKey("cur_item.item_id", ondelete="RESTRICT", name="fk_cur_mi_item"), primary_key=True
    )
    drilled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="배웠는가 — 처음 드릴 시각(단조)")
    drilled_call_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("call.call_id", ondelete="SET NULL", name="fk_cur_mi_drilled_call")
    )
    quiz_passed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="맞췄는가 — 단조")
    quiz_failed_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    last_quiz_call_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("call.call_id", ondelete="SET NULL", name="fk_cur_mi_last_quiz_call")
    )
    # 2단계(b2d3e4f5a6c7): 목록에 실린 횟수 — 예문 회전(§11: seen_count % len(examples))과 복습 정렬 키(P1-2).
    seen_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"), comment="목록에 실린 횟수 — 예문 회전·복습 정렬 키")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CurCall(Base):
    """통화 × 차시. **통화 시작 시점에 INSERT**(검수 P0-4) — 포인터가 넘어가도 «어느 차시 통화» 를 잃지 않는다.

    `course` 가 원본이고 `call.call_type` 은 옛 경로 호환용이다(검수 P1-11). `items` 는 결과 화면 전용 파생값.
    """

    __tablename__ = "cur_call"
    __table_args__ = (CheckConstraint(_in("course", CALL_COURSES), name="ck_cur_call_course"),)

    call_id: Mapped[int] = mapped_column(
        ForeignKey("call.call_id", ondelete="CASCADE", name="fk_cur_call_call"), primary_key=True
    )
    lesson_id: Mapped[int] = mapped_column(
        ForeignKey("cur_lesson.lesson_id", ondelete="RESTRICT", name="fk_cur_call_lesson"), nullable=False
    )
    course: Mapped[str] = mapped_column(Text, nullable=False, comment="expression / freetalk")
    items: Mapped[Optional[str]] = mapped_column(
        Text, comment='다룬 항목 JSON 배열 문자열 [{item_id, role, surface, meaning, drilled, passed, failed}]'
    )
    lesson_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # 2단계(b2d3e4f5a6c7): 종료 저장의 멱등 키 — record_expression 은 NULL 일 때만 쓰고 채운다(§6 ②).
    recorded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="종료 저장 완료 시각 — NULL 일 때만 record 가 쓴다(멱등 키)"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
