"""call 유효통화 컬럼 3개 + level 잉여 컬럼 4개 제거 (Rev5 — D15)

사장님 결정 D15(2026-07-09): 체류 게이트(G3) 폐지에 따른 잉여 컬럼 정리.

- call.is_valid_call/user_turn_count/user_char_count: G3(체류)·G4(창) 전용 파생 캐시였다.
  G3 폐지 후 통화 수 파생값(G4 창·브리지·버벅임·승급 멘트)은 전부 item_evidence 의
  "증거통화"(distinct call_id)로 직접 계산한다 — 관통 원칙 2(증거가 원본, 나머지는 파생).
- level.grammar_count/vocab_count/grammar_scope/vocab_sample: 런타임 사용처 0
  (전 코드 grep 확인 — 프롬프트는 level.profile 만 사용). 레벨별 문법·어휘의 단일
  소스는 learning_item 이라 이중 캐시였다.

downgrade 는 컬럼 구조만 복원하고 **데이터는 소실**된다 — 전부 파생 캐시(원본:
item_evidence / learning_item·level_profiles_13.json)라 무해. 복원 필요 시
call 컬럼은 재분석, level 컬럼은 구버전 seed_levels 재실행으로 재계산 가능했던 값이다.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-07-09 18:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — 파생 캐시 컬럼 제거(무중단: 코드가 더는 읽지 않는 컬럼)."""
    # ── 1. call 유효통화 컬럼 3개 (D15 — 증거통화 파생 계산으로 대체) ──
    op.drop_column('call', 'is_valid_call')
    op.drop_column('call', 'user_turn_count')
    op.drop_column('call', 'user_char_count')

    # ── 2. level 잉여 컬럼 4개 (런타임 사용처 0 — learning_item 이 단일 소스) ──
    op.drop_column('level', 'grammar_count')
    op.drop_column('level', 'vocab_count')
    op.drop_column('level', 'grammar_scope')
    op.drop_column('level', 'vocab_sample')


def downgrade() -> None:
    """Downgrade schema — 컬럼 구조만 복원. ⚠ 기존 값은 소실(파생 캐시라 무해)."""
    op.add_column(
        'level',
        sa.Column('vocab_sample', sa.Text(), nullable=True,
                  comment='고빈도 대표 어휘(JSON 배열 문자열)'),
    )
    op.add_column(
        'level',
        sa.Column('grammar_scope', sa.Text(), nullable=True,
                  comment='핵심 문법(JSON 배열 문자열)'),
    )
    op.add_column(
        'level',
        sa.Column('vocab_count', sa.Integer(), nullable=True, comment='어휘 수'),
    )
    op.add_column(
        'level',
        sa.Column('grammar_count', sa.Integer(), nullable=True, comment='문법 포인트 수'),
    )

    op.add_column(
        'call',
        sa.Column('user_char_count', sa.Integer(), nullable=True,
                  comment='USER 발화 글자 수(통화후 분석 산출 — 유효통화 판정 근거)'),
    )
    op.add_column(
        'call',
        sa.Column('user_turn_count', sa.Integer(), nullable=True,
                  comment='USER 발화 턴 수(통화후 분석 산출 — 유효통화 판정 근거)'),
    )
    op.add_column(
        'call',
        sa.Column('is_valid_call', sa.Boolean(), nullable=True,
                  comment='유효통화 여부(USER 턴 8+ && 200자+ && 증거 1+ — 분석 완료 시 계산, NULL=미판정)'),
    )
