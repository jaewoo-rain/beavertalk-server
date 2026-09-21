"""취약 발음 학습 — 콘텐츠 번역 + 미리 구운 TTS

신규 테이블 2개만 만든다. 기존 테이블·컬럼은 건드리지 않는다(ALTER 0건).

- sound_lesson_i18n : 학습 콘텐츠 번역. 언어 추가가 **적재**가 되게 하는 표.
- sound_audio       : 미리 구운 TTS 의 **object key**(서명 URL 아님).

⛔ sound_audio.object_key 에 서명 URL 을 넣지 마라. 7일 뒤 만료되고 그 과는 영구히
  소리가 죽는다(2026-08-31 실사고). 읽을 때마다 core/storage.playback_url 로 새로 서명한다.

되돌리기는 테이블 2개 DROP 이다. 둘 다 파생 데이터(번역·오디오)라 원본 손실은 없다 —
sound_lesson 과 lessons.json 이 원본이고, 오디오는 다시 구우면 된다.

Revision ID: c19efa26e9c8
Revises: b634396a56d4
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa


revision = "c19efa26e9c8"
down_revision = "b634396a56d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sound_lesson_i18n",
        sa.Column("sound_lesson_i18n_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("sound_key", sa.Text(), nullable=False,
                  comment="sound_lesson.sound_key 와 같은 값"),
        sa.Column("locale", sa.Text(), nullable=False,
                  comment="언어 코드 — ko · en · es …(member.language 와 같은 축)"),
        sa.Column("label", sa.Text(), nullable=True, comment="표시 라벨 번역. 없으면 원본"),
        sa.Column("card_desc", sa.Text(), nullable=True,
                  comment="카드 한 줄 설명 번역. 없으면 원본"),
        sa.Column("payload", sa.JSON(), nullable=False,
                  comment="번역되는 부분만 — how_to[] · words[].meaning · sentence.translation · test.translation"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("sound_lesson_i18n_id"),
        sa.UniqueConstraint("sound_key", "locale", name="uq_sound_lesson_i18n"),
    )
    op.create_index(
        "ix_sound_lesson_i18n_sound_key", "sound_lesson_i18n", ["sound_key"]
    )

    op.create_table(
        "sound_audio",
        sa.Column("sound_audio_id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("text_hash", sa.Text(), nullable=False,
                  comment="sha256(text) 앞 32자 — 조회 키"),
        sa.Column("text", sa.Text(), nullable=False, comment="합성한 원문 — 재생성·대조용"),
        sa.Column("voice", sa.Text(), nullable=True,
                  comment="음색 이름. None 이면 언어 기본 음색(발음 학습은 기본 음색 고정)"),
        sa.Column("engine", sa.Text(), nullable=True,
                  comment="TTS 엔진(gemini-tts 등). None 이면 기본 엔진"),
        sa.Column("object_key", sa.Text(), nullable=False,
                  comment="⛔ 버킷 안 경로다. 서명 URL 을 넣지 마라 — 7일 뒤 죽는다"),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("sound_audio_id"),
        sa.UniqueConstraint("text_hash", "voice", "engine", name="uq_sound_audio"),
    )
    op.create_index("ix_sound_audio_text_hash", "sound_audio", ["text_hash"])


def downgrade() -> None:
    op.drop_index("ix_sound_audio_text_hash", table_name="sound_audio")
    op.drop_table("sound_audio")
    op.drop_index("ix_sound_lesson_i18n_sound_key", table_name="sound_lesson_i18n")
    op.drop_table("sound_lesson_i18n")
