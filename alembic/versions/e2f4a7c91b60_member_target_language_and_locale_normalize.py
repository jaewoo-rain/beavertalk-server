"""member.target_language 추가 + 모국어(language) 정규화·백필

학습 대상 언어의 단일 소스를 앱 SharedPreferences → DB 로 옮긴다. 함께, ISO 639-1 이
아닌 값("ko-KR")과 NULL 때문에 모국어 라벨 조회가 미스나 **영어로 폴백**하던 데이터를
정리한다(활성 23명 중 8명 영향 — 한국어 모국어인데 프롬프트엔 "영어(English)").

백필 4종:
  1. target_language NULL → 'ko'   (server_default 는 신규 행만 커버)
  2. language NULL        → 'ko'   (사장님 지시 — §2-4 의미상 주의 참조)
  3. language 'ko-KR'     → 'ko'   (BCP-47 → ISO 639-1: 첫 서브태그만)
  4. language 대문자      → 소문자

⚠ downgrade 는 컬럼만 되돌린다. language 백필의 **원래 값은 복원할 수 없다**
(마이그레이션이 원본을 보존하지 않는다). prod 적용 전 대상 행을 덤프해 둘 것 —
scripts 없이도 아래 SELECT 로 뜬다:
    SELECT member_id, language FROM member WHERE language IS NULL OR language <> lower(split_part(language,'-',1));

근거: docs/20260728_0125_학습언어-DB-단일소스화와-모국어-정규화.md
"""

from alembic import op
import sqlalchemy as sa


revision = "e2f4a7c91b60"
down_revision = "d8b2f1a3c6e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "member",
        sa.Column(
            "target_language",
            sa.Text(),
            nullable=True,
            server_default=sa.text("'ko'"),
            comment="학습 대상 언어(ISO 639-1). NULL=기본 ko",
        ),
    )
    # 1) 기존 행 백필 — server_default 는 INSERT 시점에만 적용되므로 직접 채운다.
    op.execute("UPDATE member SET target_language = 'ko' WHERE target_language IS NULL")

    # 2) 모국어 NULL → 'ko'
    op.execute("UPDATE member SET language = 'ko' WHERE language IS NULL")
    # 3) BCP-47/POSIX 구분자 제거: 'ko-KR'→'ko', 'en_US'→'en'
    op.execute(
        "UPDATE member SET language = split_part(replace(language, '_', '-'), '-', 1) "
        "WHERE language LIKE '%-%' OR language LIKE '%\\_%'"
    )
    # 4) 소문자 정규화 ('KO'→'ko')
    op.execute("UPDATE member SET language = lower(language) WHERE language <> lower(language)")


def downgrade() -> None:
    # language 백필은 되돌리지 않는다(원본 미보존). 컬럼만 제거한다.
    op.drop_column("member", "target_language")
