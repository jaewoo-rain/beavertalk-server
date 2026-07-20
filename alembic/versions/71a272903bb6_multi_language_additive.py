"""multi_language_additive — 멀티랭귀지 골격 (가법 + 백필, 비파괴)

단일언어(한국어) 학습 스키마에 language 차원을 더한다. **전부 하위호환** —
기존 행은 전부 'ko' 로 백필되고, 한국어 동작·바이트는 불변이다.

이 리비전(A)은 **가법·백필만** 담당한다(제약 수술은 후속 B):
  1. member_language_level 테이블 생성 — **복합 FK(→level)는 B에서** 추가
     (타깃 uq_level_lang_no 가 아직 없음). member FK·UNIQUE·PK 만 여기서.
  2. language / reading / target_language 컬럼 add(nullable 로 먼저).
     - call.target_language 는 server_default 'ko' 라 NOT NULL 로 바로 추가(기존 행 자동 'ko').
  3. **백필 UPDATE** — learning_item/level/member_level_history/item_evidence.language = 'ko'.
     member.korean_level → member_language_level('ko', n) 로 이관 백필(korean_level NOT NULL 회원).
  4. NOT NULL 승격(백필 완료 후).
  5. item_evidence 신규 인덱스(member_id, language, call_id) — 언어별 증거통화 집계 커버.

- 제약 스왑(unique·FK·인덱스 교체)과 member_language_level 복합 FK 는 전부 B에서.
- 비파괴적·무중단(컬럼 add nullable → 백필 → NOT NULL 승격 순서). downgrade 는 컬럼·테이블
  drop(멀티랭귀지 데이터 존재 시 B downgrade 가 먼저 막으므로 A까지 오면 이미 ko 뿐).
- autogenerate 가 놓친 것(수동): 백필 UPDATE 3종 + member→member_language_level 이관,
  add-nullable→NOT NULL 2단계(autogenerate 는 NOT NULL 로 바로 add 해 기존 행에서 실패).

설계: docs/plans/2026-07-20-multi-language-platform.md §3 T1.

Revision ID: 71a272903bb6
Revises: f2cd7402bede
Create Date: 2026-07-20 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '71a272903bb6'
down_revision: Union[str, Sequence[str], None] = 'f2cd7402bede'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (가법 + 백필)."""
    # ── 1. member_language_level (복합 FK→level 은 B에서 — uq_level_lang_no 선행 필요) ──
    op.create_table(
        'member_language_level',
        sa.Column('language_level_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False, comment='회원'),
        sa.Column('language', sa.Text(), nullable=False, comment='학습 대상 언어(ISO 639-1)'),
        sa.Column('level_no', sa.Integer(), nullable=True,
                  comment='현재 레벨(1~13 → level.level_no, NULL=콜드스타트/레벨테스트 미실시)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='수정 시각'),
        sa.ForeignKeyConstraint(['member_id'], ['member.member_id'], name='fk_mll_member', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('language_level_id'),
        sa.UniqueConstraint('member_id', 'language', name='uq_member_language'),
    )

    # ── 2. 컬럼 add ──
    # 2a. language 컬럼은 nullable 로 먼저 add(기존 행 백필 후 NOT NULL 승격).
    op.add_column('learning_item', sa.Column('language', sa.Text(), nullable=True,
                  comment='커리큘럼 언어(ISO 639-1)'))
    op.add_column('learning_item', sa.Column('reading', sa.Text(), nullable=True,
                  comment='읽기/발음표기(일본어 かな·중국어 병음 등, 언어별 선택)'))
    op.add_column('level', sa.Column('language', sa.Text(), nullable=True,
                  comment='레벨 언어(ISO 639-1)'))
    op.add_column('member_level_history', sa.Column('language', sa.Text(), nullable=True,
                  comment='학습 대상 언어(ISO 639-1)'))
    op.add_column('item_evidence', sa.Column('language', sa.Text(), nullable=True,
                  comment='학습 대상 언어(ISO 639-1 — learning_item.language 와 일치)'))
    # 2b. call.target_language 는 server_default 'ko' — NOT NULL 로 바로 add(기존 행 자동 'ko').
    op.add_column('call', sa.Column('target_language', sa.Text(), server_default=sa.text("'ko'"),
                  nullable=False, comment='이 통화의 학습 대상 언어(ISO 639-1)'))

    # ── 3. 백필 (autogenerate 미생성 — 수동) ──
    op.execute("UPDATE learning_item SET language = 'ko' WHERE language IS NULL")
    op.execute("UPDATE level SET language = 'ko' WHERE language IS NULL")
    op.execute("UPDATE member_level_history SET language = 'ko' WHERE language IS NULL")
    op.execute("UPDATE item_evidence SET language = 'ko' WHERE language IS NULL")
    # member.korean_level → member_language_level('ko', n) 이관(레벨테스트 완료 회원만).
    op.execute(
        "INSERT INTO member_language_level (member_id, language, level_no, created_at, updated_at) "
        "SELECT member_id, 'ko', korean_level, now(), now() FROM member "
        "WHERE korean_level IS NOT NULL "
        "ON CONFLICT (member_id, language) DO NOTHING"
    )

    # ── 4. NOT NULL 승격(백필 완료 후) ──
    op.alter_column('learning_item', 'language', existing_type=sa.Text(), nullable=False)
    op.alter_column('level', 'language', existing_type=sa.Text(), nullable=False)
    op.alter_column('member_level_history', 'language', existing_type=sa.Text(), nullable=False)
    op.alter_column('item_evidence', 'language', existing_type=sa.Text(), nullable=False)

    # ── 5. item_evidence 신규 인덱스(언어별 증거통화 distinct 집계) ──
    op.create_index('ix_evidence_member_lang_call', 'item_evidence',
                    ['member_id', 'language', 'call_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema (컬럼·테이블 drop).

    ⚠ member_language_level 데이터·language 컬럼값 유실. 멀티랭귀지 데이터(language!=ko)
    존재 시엔 B downgrade 가 먼저 RuntimeError 로 막으므로, 여기 도달하면 ko 뿐.
    """
    op.drop_index('ix_evidence_member_lang_call', table_name='item_evidence')
    op.drop_column('call', 'target_language')
    op.drop_column('item_evidence', 'language')
    op.drop_column('member_level_history', 'language')
    op.drop_column('level', 'language')
    op.drop_column('learning_item', 'reading')
    op.drop_column('learning_item', 'language')
    op.drop_table('member_language_level')
