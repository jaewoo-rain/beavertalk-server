"""취약 발음 학습 3테이블 — sound_lesson · member_sound_score · national_sound_stat

국적별/개인별 취약 발음 학습 기능(기획 2026-09-18)의 저장소.
domains/learning/models/{sound_lesson,member_sound_score,national_sound_stat}.py 와 1:1.

- **신규 테이블만 만든다. 기존 테이블 ALTER 0건** → 무중단·비파괴적. 롤백은 drop 3개.
- sound_lesson / national_sound_stat 은 **시드 전용 마스터**(런타임 쓰기 없음).
  적재는 `scripts/seed_sound_lessons.py` 가 assets/pronunciation/*.json 으로 upsert.
- member_sound_score 만 런타임 쓰기 대상(평가 단계 제출). UNIQUE(member_id, sound_key)
  가 멱등 upsert 의 근거다 — 없으면 평가를 두 번 내면 행이 둘 된다.
- payload 는 JSON 컬럼이다. learning_item 은 TEXT(JSON 문자열)를 쓰지만 이쪽은
  review.feedback 과 같은 sa.JSON 계열을 따른다(읽기 전용 덩어리·가공 없이 그대로 서빙).

Revision ID: b634396a56d4
Revises: a9e4d7c31f80
Create Date: 2026-09-20 20:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b634396a56d4"
down_revision: Union[str, None] = "a9e4d7c31f80"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sound_lesson",
        sa.Column("sound_lesson_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("sound_key", sa.Text(), nullable=False,
                  comment="소리 키 — onset_ㄱ · coda_ㄹ · rule_연음 (집계·점수와 같은 단위)"),
        sa.Column("type", sa.Text(), nullable=False, comment="sound(자모 소리) | rule(음운 규칙)"),
        sa.Column("label", sa.Text(), nullable=False, comment="표시 라벨 — 받침 ㄹ · 연음"),
        sa.Column("position", sa.Text(), nullable=True,
                  comment="onset | coda (소스 lessons.json 표기). 규칙 항목은 없음(NULL)"),
        sa.Column("jamo", sa.Text(), nullable=True, comment="자모 1자. 규칙 항목은 없음(NULL)"),
        sa.Column("diagram", sa.Text(), nullable=True,
                  comment="조음 도해 자산명 — Airflow / <이름>. 규칙 항목은 없음"),
        sa.Column("card_desc", sa.Text(), nullable=False,
                  comment="목록 카드의 한 줄 설명(소리 내는 법. 오류 서술 금지)"),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False,
                  comment="목록 정렬 순서"),
        sa.Column("payload", sa.JSON(), nullable=False,
                  comment="4단계 콘텐츠 전량 — how_to·words·sentence·test(+규칙은 formula·symbol)"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, comment="생성 시각"),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, comment="수정 시각"),
        sa.PrimaryKeyConstraint("sound_lesson_id"),
        sa.UniqueConstraint("sound_key", name="uq_sound_lesson_sound_key"),
    )

    op.create_table(
        "member_sound_score",
        sa.Column("member_sound_score_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("member_id", sa.BigInteger(), nullable=False),
        sa.Column("sound_key", sa.Text(), nullable=False,
                  comment="소리 키 — sound_lesson.sound_key 와 같은 값"),
        sa.Column("score", sa.Integer(), nullable=False, comment="최신 평가 점수 0~100"),
        sa.Column("best_score", sa.Integer(), nullable=False,
                  comment="역대 최고 점수 — 재도전으로 점수가 내려가도 성취는 남긴다"),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False,
                  comment="평가 제출 누적 횟수"),
        sa.Column("baseline_score", sa.Integer(), nullable=True,
                  comment="첫 학습 직전 점수(복습 집계값). 결과 화면의 「학습 전」 막대"),
        sa.Column("last_learned_at", sa.DateTime(timezone=True), nullable=True,
                  comment="마지막 평가 제출 시각"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, comment="생성 시각"),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, comment="수정 시각"),
        sa.ForeignKeyConstraint(["member_id"], ["member.member_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("member_sound_score_id"),
        sa.UniqueConstraint("member_id", "sound_key", name="uq_member_sound_score"),
    )
    op.create_index(
        op.f("ix_member_sound_score_member_id"), "member_sound_score", ["member_id"], unique=False,
    )

    op.create_table(
        "national_sound_stat",
        sa.Column("national_sound_stat_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("country_iso", sa.Text(), nullable=False, comment="ISO 3166-1 alpha-2 (예: VN)"),
        sa.Column("country_name", sa.Text(), nullable=False,
                  comment="영문 국가명 — speak_country.first_country 와 매칭하는 키"),
        sa.Column("sound_key", sa.Text(), nullable=False,
                  comment="소리 키 — sound_lesson.sound_key 와 같은 값"),
        sa.Column("share", sa.Integer(), nullable=False,
                  comment="그 나라 화자 중 이 소리를 틀리는 비율 %"),
        sa.Column("rank", sa.Integer(), nullable=False,
                  comment="그 나라 안에서의 순위(1부터). 목록 정렬·추천 기준"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, comment="생성 시각"),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False, comment="수정 시각"),
        sa.PrimaryKeyConstraint("national_sound_stat_id"),
        sa.UniqueConstraint("country_iso", "sound_key", name="uq_national_sound_stat"),
    )
    op.create_index(
        op.f("ix_national_sound_stat_country_iso"), "national_sound_stat", ["country_iso"], unique=False,
    )
    op.create_index(
        op.f("ix_national_sound_stat_country_name"), "national_sound_stat", ["country_name"], unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_national_sound_stat_country_name"), table_name="national_sound_stat")
    op.drop_index(op.f("ix_national_sound_stat_country_iso"), table_name="national_sound_stat")
    op.drop_table("national_sound_stat")
    op.drop_index(op.f("ix_member_sound_score_member_id"), table_name="member_sound_score")
    op.drop_table("member_sound_score")
    op.drop_table("sound_lesson")
