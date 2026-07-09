"""체크판·레벨업 테이블 3종 + call 유효통화 컬럼 3개 (Rev4)

P2 체크판·레벨업 파이프라인의 저장 계층. 모델 3개
(domains/learning/models/{member_item_progress,item_evidence,member_level_history}.py)
+ call.py 확장과 1:1.

- member_item_progress: 체크판(희소 — 행 부재=UNSEEN). UNIQUE(member_id,item_id).
  status/provenance 는 CHECK 없음(call.status 컨벤션 — 화이트리스트는 앱 계층).
- item_evidence: append-only 감사 로그(UPDATE 금지 — 리플레이 재계산 원본).
  created_at 단독(updated_at 없음).
- member_level_history: 레벨 변경 이력. reason CHECK(마스터성 5값) +
  trigger_call_id UNIQUE(멱등 키 — NULL 다중 허용은 Postgres UNIQUE 기본).
  created_at = 레벨 진입 시각 단일 소스.
- call 컬럼 3개(is_valid_call/user_turn_count/user_char_count): 전부 NULL 허용
  (NULL=미판정 — 분석 완료 시 계산) → 백필·server_default 불필요, 무중단 추가.
- JSON 류(gate_snapshot)는 TEXT(JSON 문자열) — 프로젝트 컨벤션(sqlite 테스트 호환).
- upgrade 는 신규 테이블 3 + ALTER 3 → 무중단·비파괴적.
  downgrade 는 테이블·데이터 전체 유실 — 적용 후 되돌림은 파괴적(R6 확인 대상).
  drop 순서: evidence/progress(말단 FK) → history → call 컬럼.

설계: docs/20260709_1231_level-system-master-plan.md §3.3 / §5,
      docs/20260709_1346_level-system-detailed-mechanics.md ⑤~⑦.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-09 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, Sequence[str], None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ── 1. member_item_progress (체크판 — 희소, 행 부재=UNSEEN) ──
    op.create_table(
        'member_item_progress',
        sa.Column('progress_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False, comment='회원'),
        sa.Column('item_id', sa.BigInteger(), nullable=False, comment='학습 항목'),
        # 상태 (CHECK 없음 — call.status 컨벤션, unseen 은 행 부재)
        sa.Column('status', sa.Text(), server_default=sa.text("'introduced'"), nullable=False,
                  comment='숙달 상태(introduced/practicing/mastered — unseen 은 행 부재)'),
        sa.Column('score', sa.Float(), server_default=sa.text('0'), nullable=False,
                  comment='숙달 점수(0~6 앱 클램프 — E0+0.25/E1+0.5/E2+1.0/E3+1.5/F-1.0)'),
        sa.Column('provenance', sa.Text(), server_default=sa.text("'observed'"), nullable=False,
                  comment='상태 출처(observed/placement/fast_track)'),
        sa.Column('fast_track_confirmed_at', sa.DateTime(timezone=True), nullable=True,
                  comment='fast-track 확정 시각(NULL=미확정 — 미확정분은 G2 게이트 미산입)'),
        # 증거 카운터
        sa.Column('repeat_count', sa.Integer(), server_default=sa.text('0'), nullable=False,
                  comment='모방(E1) 누적 횟수'),
        sa.Column('prompted_count', sa.Integer(), server_default=sa.text('0'), nullable=False,
                  comment='유도 성공(E2) 누적 횟수'),
        sa.Column('spontaneous_count', sa.Integer(), server_default=sa.text('0'), nullable=False,
                  comment='자발 사용(E3) 누적 횟수'),
        sa.Column('miss_count', sa.Integer(), server_default=sa.text('0'), nullable=False,
                  comment='오류(F) 누적 횟수'),
        # 시각
        sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='최초 노출/학습 시각'),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='최근 노출 시각(주입 포함)'),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True,
                  comment='학습자가 최근 실사용한 시각(E1+ 증거)'),
        sa.Column('mastered_at', sa.DateTime(timezone=True), nullable=True,
                  comment='MASTERED 도달 시각'),
        # 통화 귀속 (통화 삭제 시 진행은 보존 — SET NULL)
        sa.Column('first_call_id', sa.BigInteger(), nullable=True, comment='최초 증거 통화'),
        sa.Column('last_call_id', sa.BigInteger(), nullable=True, comment='최근 증거 통화'),
        # TimestampMixin
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='생성 시각'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='수정 시각'),
        sa.ForeignKeyConstraint(['member_id'], ['member.member_id'], name='fk_mip_member', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['item_id'], ['learning_item.item_id'], name='fk_mip_item', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['first_call_id'], ['call.call_id'], name='fk_mip_first_call', ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['last_call_id'], ['call.call_id'], name='fk_mip_last_call', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('progress_id'),
        sa.UniqueConstraint('member_id', 'item_id', name='uq_member_item'),
    )
    op.create_index('ix_member_item_progress_item_id', 'member_item_progress', ['item_id'], unique=False)
    op.create_index('ix_mip_member_status_used', 'member_item_progress',
                    ['member_id', 'status', 'last_used_at'], unique=False)

    # ── 2. item_evidence (append-only 감사 로그 — UPDATE 금지, 리플레이 원본) ──
    op.create_table(
        'item_evidence',
        sa.Column('evidence_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False, comment='회원'),
        sa.Column('item_id', sa.BigInteger(), nullable=False, comment='학습 항목'),
        sa.Column('call_id', sa.BigInteger(), nullable=False, comment='증거가 나온 통화'),
        sa.Column('turn_index', sa.Integer(), nullable=True,
                  comment='USER 발화 턴 인덱스(전사 기준 — fast-track 선발화 판정용)'),
        sa.Column('grade_raw', sa.Text(), nullable=False, comment='LLM 원판정 등급(E1/E2/E3/F)'),
        sa.Column('grade_final', sa.Text(), nullable=False,
                  comment='서버 검증 게이트 후 최종 등급(E0/E1/E2/E3/F — 강등 반영)'),
        sa.Column('learner_quote', sa.Text(), nullable=True,
                  comment='학습자 발화 원문 인용(검증 게이트 근거 — 전사 부분일치 검증)'),
        sa.Column('verified', sa.Boolean(), server_default=sa.text('false'), nullable=False,
                  comment='서버 검증 게이트 통과 여부'),
        sa.Column('score_delta', sa.Float(), server_default=sa.text('0'), nullable=False,
                  comment='progress.score 실반영 변화량(통화당 상한 적용 후)'),
        sa.Column('normalized_text_hash', sa.Text(), nullable=True,
                  comment='정규화 인용문 해시(중복 dedup·fast-track 문장 상이 판정)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='증거 기록 시각(append-only — updated_at 없음)'),
        sa.ForeignKeyConstraint(['member_id'], ['member.member_id'], name='fk_evidence_member', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['item_id'], ['learning_item.item_id'], name='fk_evidence_item', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['call_id'], ['call.call_id'], name='fk_evidence_call', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('evidence_id'),
    )
    op.create_index('ix_item_evidence_call_id', 'item_evidence', ['call_id'], unique=False)
    op.create_index('ix_evidence_member_item', 'item_evidence',
                    ['member_id', 'item_id', 'created_at'], unique=False)

    # ── 3. member_level_history (레벨 변경 이력 — created_at = 레벨 진입 시각 단일 소스) ──
    op.create_table(
        'member_level_history',
        sa.Column('history_id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column('member_id', sa.BigInteger(), nullable=False, comment='회원'),
        sa.Column('from_level', sa.Integer(), nullable=True,
                  comment='변경 전 레벨(NULL=최초 배정 — placement)'),
        sa.Column('to_level', sa.Integer(), nullable=False, comment='변경 후 레벨(1~13)'),
        sa.Column('reason', sa.Text(), nullable=False,
                  comment='변경 사유(placement/gate_promotion/remeasure_up/remeasure_down/manual)'),
        sa.Column('trigger_call_id', sa.BigInteger(), nullable=True,
                  comment='승급을 유발한 통화(UNIQUE 멱등 키 — NULL 다중 허용)'),
        sa.Column('gate_snapshot', sa.Text(), nullable=True,
                  comment='판정 당시 게이트 집계 스냅샷(JSON 문자열 — 감사·튜닝용)'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
                  nullable=False, comment='레벨 진입 시각(단일 소스 — member.level_entered_at 금지)'),
        sa.CheckConstraint(
            "reason IN ('placement', 'gate_promotion', 'remeasure_up', 'remeasure_down', 'manual')",
            name='ck_mlh_reason',
        ),
        sa.ForeignKeyConstraint(['member_id'], ['member.member_id'], name='fk_mlh_member', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['trigger_call_id'], ['call.call_id'], name='fk_mlh_trigger_call', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('history_id'),
        sa.UniqueConstraint('trigger_call_id', name='uq_mlh_trigger_call'),
    )
    op.create_index('ix_mlh_member_created', 'member_level_history',
                    ['member_id', 'created_at'], unique=False)

    # ── 4. call 유효통화 컬럼 3개 (전부 NULL 허용 — 백필 불필요, 무중단) ──
    op.add_column(
        'call',
        sa.Column('is_valid_call', sa.Boolean(), nullable=True,
                  comment='유효통화 여부(USER 턴 8+ && 200자+ && 증거 1+ — 분석 완료 시 계산, NULL=미판정)'),
    )
    op.add_column(
        'call',
        sa.Column('user_turn_count', sa.Integer(), nullable=True,
                  comment='USER 발화 턴 수(통화후 분석 산출 — 유효통화 판정 근거)'),
    )
    op.add_column(
        'call',
        sa.Column('user_char_count', sa.Integer(), nullable=True,
                  comment='USER 발화 글자 수(통화후 분석 산출 — 유효통화 판정 근거)'),
    )


def downgrade() -> None:
    """Downgrade schema. ⚠ 체크판·증거·이력 데이터 전체 유실 — 파괴적(R6 확인 대상).

    drop 순서: 말단 FK 테이블(evidence/progress) → history → call 컬럼.
    """
    op.drop_index('ix_evidence_member_item', table_name='item_evidence')
    op.drop_index('ix_item_evidence_call_id', table_name='item_evidence')
    op.drop_table('item_evidence')

    op.drop_index('ix_mip_member_status_used', table_name='member_item_progress')
    op.drop_index('ix_member_item_progress_item_id', table_name='member_item_progress')
    op.drop_table('member_item_progress')

    op.drop_index('ix_mlh_member_created', table_name='member_level_history')
    op.drop_table('member_level_history')

    op.drop_column('call', 'user_char_count')
    op.drop_column('call', 'user_turn_count')
    op.drop_column('call', 'is_valid_call')
