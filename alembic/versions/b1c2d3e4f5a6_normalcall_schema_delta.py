"""normalcall 스키마 델타

- voice 테이블 신규(Gemini Live 보이스 마스터)
- level 테이블 신규(한국어 12단계 레벨 마스터)
- character: 페르소나 분해(role/personality/rules) + voice_id FK, prompt 제거
- member: korean_level/interests/example_sentences + language comment 명확화
- call: status(분석 상태) + mode
- sentence: source_type(표현 출처)

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-06-25 08:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ── voice (Gemini Live 보이스 마스터) ──
    op.create_table(
        'voice',
        sa.Column('voice_id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False, comment='Gemini Live 프리빌트 보이스명(예: Charon, Aoede)'),
        sa.Column('description', sa.Text(), nullable=True, comment='음색 설명(예: 밝은/차분한)'),
        sa.Column('gender', sa.Text(), nullable=True, comment='성별 느낌(male/female/neutral)'),
        sa.Column('sample_url', sa.Text(), nullable=True, comment='미리듣기 샘플 URL'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='수정 시각'),
        sa.PrimaryKeyConstraint('voice_id'),
        sa.UniqueConstraint('name', name='uq_voice_name'),
    )

    # ── level (한국어 12단계 레벨 마스터) ──
    op.create_table(
        'level',
        sa.Column('level_id', sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column('level_no', sa.Integer(), nullable=False, comment='레벨 번호(1~12)'),
        sa.Column('band', sa.Text(), nullable=True, comment='밴드(초급/중급/고급)'),
        sa.Column('grade', sa.Text(), nullable=True, comment='어휘 등급(A/B/C)'),
        sa.Column('stage_name', sa.Text(), nullable=True, comment='단계명(초급 1 …)'),
        sa.Column('textbook', sa.Text(), nullable=True, comment='교재명(Basic Korean A …)'),
        sa.Column('grammar_count', sa.Integer(), nullable=True, comment='문법 포인트 수'),
        sa.Column('vocab_count', sa.Integer(), nullable=True, comment='어휘 수'),
        sa.Column('grammar_scope', sa.Text(), nullable=True, comment='핵심 문법(JSON 배열 문자열)'),
        sa.Column('vocab_sample', sa.Text(), nullable=True, comment='고빈도 대표 어휘(JSON 배열 문자열)'),
        sa.Column('profile', sa.Text(), nullable=True, comment='발화 프로파일(프롬프트 [학습자 수준] 슬롯 주입)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False, comment='수정 시각'),
        sa.PrimaryKeyConstraint('level_id'),
        sa.UniqueConstraint('level_no', name='uq_level_no'),
    )

    # ── character: 페르소나 분해 + voice 연결, prompt 제거 ──
    op.add_column('character', sa.Column('voice_id', sa.BigInteger(), nullable=True, comment='실시간 통화 음성(Gemini Live voice)'))
    op.add_column('character', sa.Column('role', sa.Text(), nullable=True, comment='역할/정체성'))
    op.add_column('character', sa.Column('personality', sa.Text(), nullable=True, comment='성격·말투·톤'))
    op.add_column('character', sa.Column('rules', sa.Text(), nullable=True, comment='캐릭터별 추가 규칙/금기'))
    op.create_index(op.f('ix_character_voice_id'), 'character', ['voice_id'], unique=False)
    op.create_foreign_key('fk_character_voice_id', 'character', 'voice', ['voice_id'], ['voice_id'], ondelete='SET NULL')
    op.drop_column('character', 'prompt')

    # ── member: 학습 프로파일 + language 의미 명확화 ──
    op.add_column('member', sa.Column('korean_level', sa.Integer(), nullable=True, comment='한국어 레벨(1~12 → level.level_no)'))
    op.add_column('member', sa.Column('interests', sa.Text(), nullable=True, comment='관심사(콤마구분 코드)'))
    op.add_column('member', sa.Column('example_sentences', sa.Text(), nullable=True, comment='통화 프롬프트용 예시 문장(개행 구분)'))
    op.alter_column('member', 'language',
                    existing_type=sa.Text(),
                    comment='모국어(번역 target locale)',
                    existing_comment='사용 언어',
                    existing_nullable=True)

    # ── call: 비동기 분석 상태 + 감지 모드 ──
    op.add_column('call', sa.Column('status', sa.Text(), server_default=sa.text("'ongoing'"), nullable=False, comment='분석 상태(ongoing/analyzing/done/failed)'))
    op.add_column('call', sa.Column('mode', sa.Text(), nullable=True, comment='감지된 통화 모드(conversation/study/unknown)'))

    # ── sentence: 표현 출처 ──
    op.add_column('sentence', sa.Column('source_type', sa.Text(), nullable=True, comment='표현 출처(asked/corrected/drilled)'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sentence', 'source_type')

    op.drop_column('call', 'mode')
    op.drop_column('call', 'status')

    op.alter_column('member', 'language',
                    existing_type=sa.Text(),
                    comment='사용 언어',
                    existing_comment='모국어(번역 target locale)',
                    existing_nullable=True)
    op.drop_column('member', 'example_sentences')
    op.drop_column('member', 'interests')
    op.drop_column('member', 'korean_level')

    op.add_column('character', sa.Column('prompt', sa.Text(), nullable=True, comment='생성용 프롬프트'))
    op.drop_constraint('fk_character_voice_id', 'character', type_='foreignkey')
    op.drop_index(op.f('ix_character_voice_id'), table_name='character')
    op.drop_column('character', 'rules')
    op.drop_column('character', 'personality')
    op.drop_column('character', 'role')
    op.drop_column('character', 'voice_id')

    op.drop_table('level')
    op.drop_table('voice')
