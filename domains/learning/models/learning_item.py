"""learning_item (학습 항목) — learning 도메인. 커리큘럼 마스터 데이터(문법+어휘 단일 테이블).

`한국어_4단계_통합.xlsx` → curriculum_v2 JSON → 시드로 적재되는 학습 항목
(문법 459 + 어휘 10,636 + L1 생존 청크 46). kind 로 문법/어휘/청크를 구분하고,
canonical 좌표(band/topik_grade/textbook_code/seq_no)와 파생 배정(level_no + assign_rule)을
병기한다 — 배분 규칙이 바뀌면 level_no 를 UPDATE 재계산만 하면 된다.

- kind='chunk' : L1 생존 청크(정형 표현 통째 학습, 체크판 게이트 대상).
  교재/TOPIK 좌표가 없으므로 textbook_code·topik_grade 는 NULL — 조건부 CHECK 2종은
  kind 별 조건이라 chunk 에 무해. band=1(생존도 초급 밴드로 묶음), level_no=1 고정.
- source_key : 시드 멱등 upsert 조인 키.
  `g:{교재코드}:{surface}` / `v:{surface}{접미|00}` / `c:{no:02d}:{ko}`.
- JSON 류(pos_list/examples/meanings/gen_examples) : 프로젝트 컨벤션대로 TEXT(JSON 문자열)
  — 테스트가 sqlite 인메모리라 JSONB 금지.
- is_core/priority_rank : P0-A 파이프라인이 계산한 core 어휘 선정 결과(게이트 산입 여부).
- 자모(40행)는 이 테이블에 넣지 않는다(정적 자산 — assets/level/curriculum_v2/jamo.json).

설계: docs/20260709_1231_level-system-master-plan.md §3.3~3.4,
      docs/20260709_1346_level-system-detailed-mechanics.md.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class LearningItem(Base, TimestampMixin):
    __tablename__ = "learning_item"
    __table_args__ = (
        UniqueConstraint("source_key", name="uq_learning_item_source_key"),
        CheckConstraint(
            "kind IN ('grammar', 'vocab', 'chunk')",
            name="ck_learning_item_kind",
        ),
        # 문법은 교재 좌표 필수, 어휘는 TOPIK 등급 필수 (kind 별 조건부 NOT NULL)
        CheckConstraint(
            "kind != 'grammar' OR textbook_code IS NOT NULL",
            name="ck_learning_item_grammar_textbook",
        ),
        CheckConstraint(
            "kind != 'vocab' OR topik_grade IS NOT NULL",
            name="ck_learning_item_vocab_grade",
        ),
        # 통화 재료 선별(레벨×종류×단원 순서) — partial index 는 sqlite 미지원이라 일반 인덱스
        Index("ix_learning_item_level_kind", "level_no", "kind", "seq_no"),
        # 어휘 재배분(assign_rule 변경 시 TOPIK 등급 기준 재계산)
        Index("ix_learning_item_vocab_grade", "topik_grade", "seq_no"),
    )

    item_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)

    # ── 항목 식별 ──
    kind: Mapped[str] = mapped_column(Text, comment="항목 종류(grammar/vocab/chunk)")
    source_key: Mapped[str] = mapped_column(
        Text,
        comment="시드 멱등 조인 키(g:{교재코드}:{surface} / v:{surface}{접미|00} / c:{no:02d}:{ko})",
    )

    # ── canonical 좌표 (소스 데이터의 불변 위치 — 재배분에도 안 바뀜) ──
    band: Mapped[int] = mapped_column(
        SmallInteger, comment="밴드(1=초급/2=중급/3=고급 — 원본 단계, chunk 는 1)",
    )
    topik_grade: Mapped[Optional[int]] = mapped_column(
        SmallInteger, comment="TOPIK 등급(1~6, 어휘 필수 — CHECK)",
    )
    textbook_code: Mapped[Optional[str]] = mapped_column(
        Text, comment="교재 코드(basic_a … advanced_d, 문법 필수 — CHECK)",
    )
    seq_no: Mapped[Optional[int]] = mapped_column(
        Integer, comment="소스 내 순번(교재 단원 순서/어휘 우선순위 정렬용)",
    )

    # ── 파생 배정 (assign_rule 버전으로 재계산 가능) ──
    level_no: Mapped[int] = mapped_column(
        Integer,  # level.level_no(Integer)와 타입 통일 — FK 양단 동일 타입
        ForeignKey(
            "level.level_no", ondelete="RESTRICT", name="fk_learning_item_level_no",
        ),
        comment="배정 레벨(1~13 → level.level_no)",
    )
    assign_rule: Mapped[str] = mapped_column(
        Text, comment="배정 규칙 버전(textbook_v1/vocab_split_v1 …)",
    )

    # ── 표기 ──
    surface: Mapped[str] = mapped_column(Text, comment="표면형(전사 판정 대상 표기)")
    homograph_refs: Mapped[Optional[str]] = mapped_column(
        Text, comment="동형이의어 원표기 참조(병합 전 '사01/사02' 류 보존)",
    )

    # ── 어휘 속성 ──
    pos_primary: Mapped[Optional[str]] = mapped_column(Text, comment="대표 품사(정규 11종)")
    pos_list: Mapped[Optional[str]] = mapped_column(
        Text, comment="품사 목록(JSON 배열 문자열, 정규 11종)",
    )
    pos_raw: Mapped[Optional[str]] = mapped_column(Text, comment="원본 품사 표기(무손실 보존)")
    guide_phrase: Mapped[Optional[str]] = mapped_column(
        Text, comment="길잡이말(sense 힌트 — LLM 생성 시 필수 주입)",
    )
    is_verb_priority: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
        comment="동사 시트 우선 신호(동사 1,468 = 어휘 부분집합, 플래그만)",
    )
    is_core: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
        comment="core 어휘 여부(게이트 산입 — P0-A 선정 결과)",
    )
    priority_rank: Mapped[Optional[int]] = mapped_column(
        Integer, comment="core 선정 우선순위(점수 내림차순 순위 — P0-A 계산)",
    )

    # ── 문법 속성 ──
    unit: Mapped[Optional[str]] = mapped_column(Text, comment="교재 단원 번호")
    unit_title: Mapped[Optional[str]] = mapped_column(Text, comment="교재 단원 제목")
    grammar_type: Mapped[Optional[str]] = mapped_column(Text, comment="문법 유형(어미/조사/문형 …)")
    examples: Mapped[Optional[str]] = mapped_column(
        Text, comment="교재 예문(JSON 배열 문자열)",
    )
    explanation: Mapped[Optional[str]] = mapped_column(Text, comment="문법 설명")
    caution: Mapped[Optional[str]] = mapped_column(Text, comment="주의사항")

    # ── LLM 생성물 (generated/vocab_gen — 결손 시 NULL graceful) ──
    meanings: Mapped[Optional[str]] = mapped_column(
        Text, comment="다국어 뜻(JSON 객체 문자열, krdict/LLM 생성)",
    )
    gen_examples: Mapped[Optional[str]] = mapped_column(
        Text, comment="생성 예문(JSON 배열 문자열, 레벨 문법 scope 제약)",
    )
    gen_version: Mapped[Optional[str]] = mapped_column(Text, comment="생성물 버전(vocab_gen_v1 …)")
