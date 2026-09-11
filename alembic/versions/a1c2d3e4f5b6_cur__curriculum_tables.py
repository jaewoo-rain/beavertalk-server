"""cur_* — 주제별 커리큘럼(차시) 체계 테이블 10개 — **추가만**(기존 테이블 ALTER 0)

설계: docs/20260912_0330_cur-스키마-설계.md (v2) · 모델 domains/learning/models/curriculum.py
사장님 결정 2026-09-12 #1~#13. 통화·진도는 cur_* + member + character + call 만 읽는 새 학습 체계.

⚠ autogenerate 결과에서 cur_* CREATE 만 남겼다 — 같이 나온 옛 테이블 alter_column(주석·nullable 드리프트)·
  character 인덱스 변경은 이 리비전의 몫이 아니라 **전부 버렸다.** 되돌리면 cur_* DROP 뿐이다.
⚠ 값 목록 CHECK(kind/role/status/course)와 level_no 범위는 모델과 **같은 목록**이어야 한다(sqlite 는 create_all).

Revision ID: a1c2d3e4f5b6
Revises: c5a8e206d41f
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision = "a1c2d3e4f5b6"
down_revision = "c5a8e206d41f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('cur_function',
    sa.Column('function_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('code', sa.Text(), nullable=False, comment='F01~F30'),
    sa.Column('name', sa.Text(), nullable=False, comment='기능명(서술·지정 …)'),
    sa.PrimaryKeyConstraint('function_id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('cur_topic',
    sa.Column('topic_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('code', sa.Text(), nullable=False, comment='T01~T65'),
    sa.Column('area', sa.Text(), nullable=False, comment='영역(나와 사람 …)'),
    sa.Column('name', sa.Text(), nullable=False, comment='주제명'),
    sa.Column('kind', sa.Text(), nullable=False, comment='conv(회화) / support(지원)'),
    sa.CheckConstraint("kind IN ('conv', 'support')", name='ck_cur_topic_kind'),
    sa.PrimaryKeyConstraint('topic_id'),
    sa.UniqueConstraint('code')
    )
    op.create_table('cur_item',
    sa.Column('item_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('language', sa.Text(), server_default=sa.text("'ko'"), nullable=False, comment='학습 언어 코드'),
    sa.Column('kind', sa.Text(), nullable=False, comment='vocab / grammar / chunk'),
    sa.Column('key', sa.Text(), nullable=False, comment='시드 정체성 키(어휘 표제어+첨자 · 문법 이름 · 청크 문장)'),
    sa.Column('surface', sa.Text(), nullable=False, comment='판정·발화 표면형'),
    sa.Column('headword_suffix', sa.Text(), nullable=True, comment='동형어 첨자(01 · 00/01) — 표시용'),
    sa.Column('meanings', sa.Text(), nullable=True, comment='로케일별 뜻 JSON 문자열 {"en": "..."}'),
    sa.Column('pos', sa.Text(), nullable=True, comment='품사(어휘)'),
    sa.Column('guide', sa.Text(), nullable=True, comment='길잡이말(동형어 뜻 고정)'),
    sa.Column('grade', sa.Text(), nullable=True, comment='등급(1급~)'),
    sa.Column('cefr6', sa.Text(), nullable=True, comment='CEFR 6단계'),
    sa.Column('stage', sa.Text(), nullable=True, comment='원 배정 단계 A1~C4 (차시 단계와 다를 수 있음)'),
    sa.Column('level_no', sa.SmallInteger(), nullable=False, comment='1=청크 · 2~13 = 원 단계'),
    sa.Column('topic_id', sa.BigInteger(), nullable=True, comment='어휘의 주제'),
    sa.Column('function_id', sa.BigInteger(), nullable=True, comment='문법의 기능'),
    sa.Column('description', sa.Text(), nullable=True, comment='문법 설명'),
    sa.Column('notes', sa.Text(), nullable=True, comment='문법 주의사항'),
    sa.Column('examples', sa.Text(), nullable=True, comment='예문 JSON 배열 문자열(최대 3)'),
    sa.Column('freq', sa.Float(), nullable=True, comment='문장빈도'),
    sa.Column('textbook', sa.Text(), nullable=True, comment='문법 출처 교재'),
    sa.Column('textbook_unit', sa.Text(), nullable=True, comment='문법 출처 단원'),
    sa.Column('task_title', sa.Text(), nullable=True, comment='문법 출처 과제목'),
    sa.Column('retired_at', sa.DateTime(timezone=True), nullable=True, comment='재적재로 빠진 항목의 은퇴 시각(NULL=현역). 지우지 않는다'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('vocab', 'grammar', 'chunk')", name='ck_cur_item_kind'),
    sa.CheckConstraint('level_no BETWEEN 1 AND 13', name='ck_cur_item_level'),
    sa.ForeignKeyConstraint(['function_id'], ['cur_function.function_id'], name='fk_cur_item_function', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['topic_id'], ['cur_topic.topic_id'], name='fk_cur_item_topic', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('item_id'),
    sa.UniqueConstraint('language', 'kind', 'key', name='uq_cur_item_key')
    )
    op.create_index('ix_cur_item_function', 'cur_item', ['function_id'], unique=False)
    op.create_index('ix_cur_item_topic', 'cur_item', ['topic_id'], unique=False)
    op.create_table('cur_lesson',
    sa.Column('lesson_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('language', sa.Text(), server_default=sa.text("'ko'"), nullable=False),
    sa.Column('no', sa.Integer(), nullable=False, comment='진도 순서 1..491'),
    sa.Column('code', sa.Text(), nullable=False, comment='L1-S01-1 · A1-T01-1 …'),
    sa.Column('level_no', sa.SmallInteger(), nullable=False, comment='1~13'),
    sa.Column('stage', sa.Text(), nullable=True, comment='A1~C4 (레벨1 은 NULL)'),
    sa.Column('topic_id', sa.BigInteger(), nullable=True, comment='레벨1 은 NULL'),
    sa.Column('part', sa.Text(), nullable=True, comment='분할 1/1 · 1/2 …'),
    sa.Column('situation', sa.Text(), nullable=False, comment='프리토킹 상황명'),
    sa.Column('partner', sa.Text(), nullable=True, comment='상대역 — 상황 묘사용(비버가 되라는 뜻 아님)'),
    sa.Column('grammar_kind', sa.Text(), nullable=True, comment='신규 / 이어 연습 (기록)'),
    sa.Column('opening', sa.Text(), nullable=True, comment='프리토킹 여는 말(시드)'),
    sa.Column('probes', sa.Text(), nullable=True, comment='유도 질문 JSON 배열 문자열'),
    sa.Column('success', sa.Text(), nullable=True, comment='ai_roles 성공 조건 JSON(기록용)'),
    sa.Column('guardrails', sa.Text(), nullable=True, comment='ai_roles 진행 규칙 JSON(기록용)'),
    sa.Column('dialogue', sa.Text(), nullable=True, comment='모범 대화 JSON(기록용)'),
    sa.Column('item_count', sa.SmallInteger(), server_default=sa.text('0'), nullable=False, comment='로더가 재계산'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('level_no BETWEEN 1 AND 13', name='ck_cur_lesson_level'),
    sa.ForeignKeyConstraint(['topic_id'], ['cur_topic.topic_id'], name='fk_cur_lesson_topic', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('lesson_id'),
    sa.UniqueConstraint('language', 'code', name='uq_cur_lesson_code'),
    sa.UniqueConstraint('language', 'no', name='uq_cur_lesson_no')
    )
    op.create_index('ix_cur_lesson_level_no', 'cur_lesson', ['language', 'level_no', 'no'], unique=False)
    op.create_table('cur_lesson_function',
    sa.Column('lesson_id', sa.BigInteger(), nullable=False),
    sa.Column('function_id', sa.BigInteger(), nullable=False),
    sa.ForeignKeyConstraint(['function_id'], ['cur_function.function_id'], name='fk_cur_lf_function', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['lesson_id'], ['cur_lesson.lesson_id'], name='fk_cur_lf_lesson', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('lesson_id', 'function_id')
    )
    op.create_table('cur_lesson_item',
    sa.Column('lesson_id', sa.BigInteger(), nullable=False),
    sa.Column('item_id', sa.BigInteger(), nullable=False),
    sa.Column('role', sa.Text(), nullable=False, comment='grammar / must / core / support / chunk'),
    sa.Column('seq', sa.SmallInteger(), nullable=False, comment='차시 안 순번(1부터)'),
    sa.CheckConstraint("role IN ('grammar', 'must', 'core', 'support', 'chunk')", name='ck_cur_lesson_item_role'),
    sa.ForeignKeyConstraint(['item_id'], ['cur_item.item_id'], name='fk_cur_li_item', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['lesson_id'], ['cur_lesson.lesson_id'], name='fk_cur_li_lesson', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('lesson_id', 'item_id'),
    sa.UniqueConstraint('lesson_id', 'seq', name='uq_cur_lesson_item_seq')
    )
    op.create_index('ix_cur_lesson_item_item', 'cur_lesson_item', ['item_id'], unique=False)
    op.create_table('cur_member_progress',
    sa.Column('member_id', sa.BigInteger(), nullable=False),
    sa.Column('language', sa.Text(), server_default=sa.text("'ko'"), nullable=False),
    sa.Column('lesson_id', sa.BigInteger(), nullable=False, comment='지금 차시'),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['lesson_id'], ['cur_lesson.lesson_id'], name='fk_cur_mp_lesson', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['member_id'], ['member.member_id'], name='fk_cur_mp_member', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('member_id', 'language')
    )
    op.create_table('cur_call',
    sa.Column('call_id', sa.BigInteger(), nullable=False),
    sa.Column('lesson_id', sa.BigInteger(), nullable=False),
    sa.Column('course', sa.Text(), nullable=False, comment='expression / freetalk'),
    sa.Column('items', sa.Text(), nullable=True, comment='다룬 항목 JSON 배열 문자열 [{item_id, role, surface, meaning, drilled, passed, failed}]'),
    sa.Column('lesson_completed', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("course IN ('expression', 'freetalk')", name='ck_cur_call_course'),
    sa.ForeignKeyConstraint(['call_id'], ['call.call_id'], name='fk_cur_call_call', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['lesson_id'], ['cur_lesson.lesson_id'], name='fk_cur_call_lesson', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('call_id')
    )
    op.create_table('cur_member_item',
    sa.Column('member_id', sa.BigInteger(), nullable=False),
    sa.Column('lesson_id', sa.BigInteger(), nullable=False),
    sa.Column('item_id', sa.BigInteger(), nullable=False),
    sa.Column('drilled_at', sa.DateTime(timezone=True), nullable=True, comment='배웠는가 — 처음 드릴 시각(단조)'),
    sa.Column('drilled_call_id', sa.BigInteger(), nullable=True),
    sa.Column('quiz_passed_at', sa.DateTime(timezone=True), nullable=True, comment='맞췄는가 — 단조'),
    sa.Column('quiz_failed_count', sa.SmallInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_quiz_call_id', sa.BigInteger(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['drilled_call_id'], ['call.call_id'], name='fk_cur_mi_drilled_call', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['item_id'], ['cur_item.item_id'], name='fk_cur_mi_item', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['last_quiz_call_id'], ['call.call_id'], name='fk_cur_mi_last_quiz_call', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['lesson_id'], ['cur_lesson.lesson_id'], name='fk_cur_mi_lesson', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['member_id'], ['member.member_id'], name='fk_cur_mi_member', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('member_id', 'lesson_id', 'item_id')
    )
    op.create_index('ix_cur_mi_member_lesson', 'cur_member_item', ['member_id', 'lesson_id'], unique=False)
    op.create_table('cur_member_lesson',
    sa.Column('member_id', sa.BigInteger(), nullable=False),
    sa.Column('lesson_id', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.Text(), server_default=sa.text("'learning'"), nullable=False),
    sa.Column('expression_calls', sa.SmallInteger(), server_default=sa.text('0'), nullable=False, comment='표현학습 통화 횟수'),
    sa.Column('expression_done_at', sa.DateTime(timezone=True), nullable=True, comment='항목 전부 드릴 완료(결정 #4)'),
    sa.Column('freetalk_call_id', sa.BigInteger(), nullable=True, comment='프리토킹 한 통화'),
    sa.Column('freetalk_done_at', sa.DateTime(timezone=True), nullable=True, comment='한 번 하면 끝(결정 #3)'),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(status <> 'freetalk_done') OR (freetalk_done_at IS NOT NULL)", name='ck_cur_ml_freetalk_ts'),
    sa.CheckConstraint("(status = 'learning') OR (expression_done_at IS NOT NULL)", name='ck_cur_ml_expression_ts'),
    sa.CheckConstraint("status IN ('learning', 'expression_done', 'freetalk_done')", name='ck_cur_ml_status'),
    sa.ForeignKeyConstraint(['freetalk_call_id'], ['call.call_id'], name='fk_cur_ml_freetalk_call', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['lesson_id'], ['cur_lesson.lesson_id'], name='fk_cur_ml_lesson', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['member_id'], ['member.member_id'], name='fk_cur_ml_member', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('member_id', 'lesson_id')
    )


def downgrade() -> None:
    op.drop_table('cur_member_lesson')
    op.drop_index('ix_cur_mi_member_lesson', table_name='cur_member_item')
    op.drop_table('cur_member_item')
    op.drop_table('cur_call')
    op.drop_table('cur_member_progress')
    op.drop_index('ix_cur_lesson_item_item', table_name='cur_lesson_item')
    op.drop_table('cur_lesson_item')
    op.drop_table('cur_lesson_function')
    op.drop_index('ix_cur_lesson_level_no', table_name='cur_lesson')
    op.drop_table('cur_lesson')
    op.drop_index('ix_cur_item_topic', table_name='cur_item')
    op.drop_index('ix_cur_item_function', table_name='cur_item')
    op.drop_table('cur_item')
    op.drop_table('cur_topic')
    op.drop_table('cur_function')
