"""multi_language_composite_keys — 제약 수술(단일 → 언어 복합 키)

Rev A(가법+백필) 이후, 유일성·FK·인덱스를 (language, …) 복합 키로 교체한다.
A에서 모든 행이 language='ko' 로 채워졌으므로 복합 유일성 위반 없음(단일언어=ko 뿐).

수술 순서(참조 무결성 유지):
  1. learning_item 단일 FK(fk_learning_item_level_no) drop — level.uq_level_no 의존 제거
  2. level.uq_level_no drop
  3. level.uq_level_lang_no(language, level_no) create — 복합 FK 타깃
  4. learning_item 복합 FK fk_learning_item_level(language, level_no)→level create
  5. learning_item source_key uq 스왑(uq_learning_item_source_key → uq_learning_item_lang_source_key)
  6. member_language_level 복합 FK fk_mll_level(language, level_no)→level create (A에서 미생성분)
  7. 인덱스 스왑(learning_item 2종 + member_level_history — language 프리픽스)

- DDL 락(ACCESS EXCLUSIVE 잠깐) — dev/소규모라 무해. prod 는 백업·저트래픽 창.
- ⚠ downgrade 가드: **멀티랭귀지 데이터(language!=ko) 존재 시 RuntimeError** — 단일 ko
  좌표로 역-swap 하면 다국어 유일성이 붕괴(e5f6a7b8c9d0 패턴). ko-only 일 때만 역가능.

설계: docs/plans/2026-07-20-multi-language-platform.md §3 T1.

Revision ID: f10b3c05cf8e
Revises: 71a272903bb6
Create Date: 2026-07-20 18:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f10b3c05cf8e'
down_revision: Union[str, Sequence[str], None] = '71a272903bb6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (제약 수술)."""
    # 1. learning_item 단일 FK drop (level.uq_level_no 의존 해제)
    op.drop_constraint('fk_learning_item_level_no', 'learning_item', type_='foreignkey')

    # 2~3. level 유일성 스왑: uq_level_no → uq_level_lang_no
    op.drop_constraint('uq_level_no', 'level', type_='unique')
    op.create_unique_constraint('uq_level_lang_no', 'level', ['language', 'level_no'])

    # 4. learning_item 복합 FK create → level(language, level_no)
    op.create_foreign_key(
        'fk_learning_item_level', 'learning_item', 'level',
        ['language', 'level_no'], ['language', 'level_no'], ondelete='RESTRICT',
    )

    # 5. learning_item source_key 유일성 스왑
    op.drop_constraint('uq_learning_item_source_key', 'learning_item', type_='unique')
    op.create_unique_constraint(
        'uq_learning_item_lang_source_key', 'learning_item', ['language', 'source_key'],
    )

    # 6. member_language_level 복합 FK create(A에서 미생성 — 타깃 uq_level_lang_no 필요)
    op.create_foreign_key(
        'fk_mll_level', 'member_language_level', 'level',
        ['language', 'level_no'], ['language', 'level_no'], ondelete='RESTRICT',
    )

    # 7. 인덱스 스왑(language 프리픽스)
    op.drop_index('ix_learning_item_level_kind', table_name='learning_item')
    op.create_index('ix_learning_item_level_kind', 'learning_item',
                    ['language', 'level_no', 'kind', 'seq_no'], unique=False)
    op.drop_index('ix_learning_item_vocab_grade', table_name='learning_item')
    op.create_index('ix_learning_item_vocab_grade', 'learning_item',
                    ['language', 'topik_grade', 'seq_no'], unique=False)
    op.drop_index('ix_mlh_member_created', table_name='member_level_history')
    op.create_index('ix_mlh_member_created', 'member_level_history',
                    ['member_id', 'language', 'created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema (역-swap — ko-only 일 때만).

    ⚠ 가드: 멀티랭귀지 데이터(language!=ko / target_language!=ko)가 하나라도 있으면
    단일-ko 좌표로 되돌릴 수 없다(유일성 붕괴·데이터 유실). RuntimeError 로 차단
    (e5f6a7b8c9d0_level_13_shift 패턴). 되돌리려면 백업 복원으로만.
    """
    bind = op.get_bind()
    non_ko = bind.execute(sa.text(
        "SELECT "
        "(SELECT count(*) FROM learning_item WHERE language <> 'ko') "
        "+ (SELECT count(*) FROM level WHERE language <> 'ko') "
        "+ (SELECT count(*) FROM member_language_level WHERE language <> 'ko') "
        "+ (SELECT count(*) FROM member_level_history WHERE language <> 'ko') "
        "+ (SELECT count(*) FROM item_evidence WHERE language <> 'ko') "
        "+ (SELECT count(*) FROM call WHERE target_language <> 'ko')"
    )).scalar_one()
    if non_ko:
        raise RuntimeError(
            f"비가역: 멀티랭귀지 데이터 {non_ko}행(language!=ko) 존재 — 단일-ko 스키마로 "
            "다운그레이드 불가(유일성 붕괴). 되돌리려면 백업 복원으로만."
        )

    # 7. 인덱스 역-스왑
    op.drop_index('ix_mlh_member_created', table_name='member_level_history')
    op.create_index('ix_mlh_member_created', 'member_level_history',
                    ['member_id', 'created_at'], unique=False)
    op.drop_index('ix_learning_item_vocab_grade', table_name='learning_item')
    op.create_index('ix_learning_item_vocab_grade', 'learning_item',
                    ['topik_grade', 'seq_no'], unique=False)
    op.drop_index('ix_learning_item_level_kind', table_name='learning_item')
    op.create_index('ix_learning_item_level_kind', 'learning_item',
                    ['level_no', 'kind', 'seq_no'], unique=False)

    # 6. member_language_level 복합 FK drop
    op.drop_constraint('fk_mll_level', 'member_language_level', type_='foreignkey')

    # 5. learning_item source_key 유일성 역-스왑
    op.drop_constraint('uq_learning_item_lang_source_key', 'learning_item', type_='unique')
    op.create_unique_constraint(
        'uq_learning_item_source_key', 'learning_item', ['source_key'],
    )

    # 4. learning_item 복합 FK drop
    op.drop_constraint('fk_learning_item_level', 'learning_item', type_='foreignkey')

    # 2~3. level 유일성 역-스왑
    op.drop_constraint('uq_level_lang_no', 'level', type_='unique')
    op.create_unique_constraint('uq_level_no', 'level', ['level_no'])

    # 1. learning_item 단일 FK 복원 → level.level_no
    op.create_foreign_key(
        'fk_learning_item_level_no', 'learning_item', 'level',
        ['level_no'], ['level_no'], ondelete='RESTRICT',
    )
