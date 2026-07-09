"""create learning_item — 커리큘럼 마스터 데이터(문법+어휘 단일 테이블)

한국어_4단계_통합.xlsx → curriculum_v2 JSON 시드가 적재할 학습 항목 테이블
(문법 459 + 어휘 10,636 + L1 생존 청크 46 — kind='chunk', 체크판 게이트 대상).
domains/learning/models/learning_item.py 와 1:1.

- JSON 류(pos_list/examples/meanings/gen_examples)는 TEXT(JSON 문자열)
  — 프로젝트 컨벤션(level.grammar_scope 동일 패턴, sqlite 테스트 호환).
- FK 는 비-PK 유니크 컬럼 level.level_no(uq_level_no) 참조, ondelete RESTRICT
  — 레벨 마스터 행은 학습 항목이 매달린 채 못 지운다.
- CHECK 3종: kind 화이트리스트(grammar/vocab/chunk) / 문법=textbook_code 필수 /
  어휘=topik_grade 필수. 조건부 CHECK 2종은 kind 별 조건이라 chunk 에 무해
  (chunk 는 textbook_code·topik_grade 둘 다 NULL).
- 인덱스는 partial 이 아닌 일반 복합 인덱스(sqlite 미지원 + 1.1만 행 규모라 무해).
- 신규 테이블(기존 테이블 ALTER 없음) → 무중단·비파괴적. 단 Rev1(level_13_shift)
  이후에만 적용 — level_no 배정이 1~13 좌표 전제.

설계: docs/20260709_1231_level-system-master-plan.md §3.3~3.4 Rev2.

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-07-09 14:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'learning_item',
        sa.Column('item_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        # 항목 식별
        sa.Column('kind', sa.Text(), nullable=False, comment='항목 종류(grammar/vocab/chunk)'),
        sa.Column('source_key', sa.Text(), nullable=False, comment='시드 멱등 조인 키(g:{교재코드}:{surface} / v:{surface}{접미|00} / c:{no:02d}:{ko})'),
        # canonical 좌표
        sa.Column('band', sa.SmallInteger(), nullable=False, comment='밴드(1=초급/2=중급/3=고급 — 원본 단계, chunk 는 1)'),
        sa.Column('topik_grade', sa.SmallInteger(), nullable=True, comment='TOPIK 등급(1~6, 어휘 필수 — CHECK)'),
        sa.Column('textbook_code', sa.Text(), nullable=True, comment='교재 코드(basic_a … advanced_d, 문법 필수 — CHECK)'),
        sa.Column('seq_no', sa.Integer(), nullable=True, comment='소스 내 순번(교재 단원 순서/어휘 우선순위 정렬용)'),
        # 파생 배정
        sa.Column('level_no', sa.Integer(), nullable=False, comment='배정 레벨(1~13 → level.level_no)'),  # level.level_no 와 동일 타입
        sa.Column('assign_rule', sa.Text(), nullable=False, comment='배정 규칙 버전(textbook_v1/vocab_split_v1 …)'),
        # 표기
        sa.Column('surface', sa.Text(), nullable=False, comment='표면형(전사 판정 대상 표기)'),
        sa.Column('homograph_refs', sa.Text(), nullable=True, comment="동형이의어 원표기 참조(병합 전 '사01/사02' 류 보존)"),
        # 어휘 속성
        sa.Column('pos_primary', sa.Text(), nullable=True, comment='대표 품사(정규 11종)'),
        sa.Column('pos_list', sa.Text(), nullable=True, comment='품사 목록(JSON 배열 문자열, 정규 11종)'),
        sa.Column('pos_raw', sa.Text(), nullable=True, comment='원본 품사 표기(무손실 보존)'),
        sa.Column('guide_phrase', sa.Text(), nullable=True, comment='길잡이말(sense 힌트 — LLM 생성 시 필수 주입)'),
        sa.Column('is_verb_priority', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='동사 시트 우선 신호(동사 1,468 = 어휘 부분집합, 플래그만)'),
        sa.Column('is_core', sa.Boolean(), server_default=sa.text('false'), nullable=False, comment='core 어휘 여부(게이트 산입 — P0-A 선정 결과)'),
        sa.Column('priority_rank', sa.Integer(), nullable=True, comment='core 선정 우선순위(점수 내림차순 순위 — P0-A 계산)'),
        # 문법 속성
        sa.Column('unit', sa.Text(), nullable=True, comment='교재 단원 번호'),
        sa.Column('unit_title', sa.Text(), nullable=True, comment='교재 단원 제목'),
        sa.Column('grammar_type', sa.Text(), nullable=True, comment='문법 유형(어미/조사/문형 …)'),
        sa.Column('examples', sa.Text(), nullable=True, comment='교재 예문(JSON 배열 문자열)'),
        sa.Column('explanation', sa.Text(), nullable=True, comment='문법 설명'),
        sa.Column('caution', sa.Text(), nullable=True, comment='주의사항'),
        # LLM 생성물
        sa.Column('meanings', sa.Text(), nullable=True, comment='다국어 뜻(JSON 객체 문자열, krdict/LLM 생성)'),
        sa.Column('gen_examples', sa.Text(), nullable=True, comment='생성 예문(JSON 배열 문자열, 레벨 문법 scope 제약)'),
        sa.Column('gen_version', sa.Text(), nullable=True, comment='생성물 버전(vocab_gen_v1 …)'),
        # TimestampMixin
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='수정 시각'),
        sa.CheckConstraint("kind IN ('grammar', 'vocab', 'chunk')", name='ck_learning_item_kind'),
        sa.CheckConstraint("kind != 'grammar' OR textbook_code IS NOT NULL", name='ck_learning_item_grammar_textbook'),
        sa.CheckConstraint("kind != 'vocab' OR topik_grade IS NOT NULL", name='ck_learning_item_vocab_grade'),
        sa.ForeignKeyConstraint(['level_no'], ['level.level_no'], name='fk_learning_item_level_no', ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('item_id'),
        sa.UniqueConstraint('source_key', name='uq_learning_item_source_key'),
    )
    op.create_index('ix_learning_item_level_kind', 'learning_item', ['level_no', 'kind', 'seq_no'], unique=False)
    op.create_index('ix_learning_item_vocab_grade', 'learning_item', ['topik_grade', 'seq_no'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_learning_item_vocab_grade', table_name='learning_item')
    op.drop_index('ix_learning_item_level_kind', table_name='learning_item')
    op.drop_table('learning_item')
