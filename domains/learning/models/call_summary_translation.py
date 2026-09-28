"""call_summary_translation — 통화 요약(`call.summary`) 번역 캐시. §10(2026-09-29).

⛔⛔ **이 표의 DDL 은 앱 서버 소유가 아니다.** B2B 서버가 만들었다(B2B alembic
`e1f2a3b4c5d6_call_summary_translation`, 버전표 `alembic_version_b2b`). 앱·B2B·콘솔이 같은
Supabase DB 를 쓰고, 같은 개체(call_id → 그 언어 번역)라 표를 둘 두지 않고 **같이 쓴다**.
  ⇒ ⛔ 앱 서버에서 이 표의 마이그레이션을 만들지 마라(이미 있어 실패한다).
  ⇒ 그래서 앱 `Base.metadata` 에 **올리지 않는다** — `alembic/env.py` 가 `Base.metadata` 의
     표를 «우리 것»(`_OWNED_TABLES`)으로 보므로, 올리면 autogenerate 가 B2B 표의 주석·인덱스
     차이를 diff 로 내거나, 표가 없는 로컬 dev DB 에선 create_table 을 만든다.
     여기는 별도 MetaData 의 Core Table 이다(FK 는 실제 DB 에 있다 — call ON DELETE CASCADE).
  ⚠ B2B 가 이 표 스키마(컬럼 이름·유니크)를 바꾸면 앱 통화 목록 번역이 깨진다(실패는 원문으로
     떨어지므로 목록은 안 죽는다 — R5). 바꿀 땐 앱 서버도 같이 봐야 한다.

B2B 는 `locale` 을 ko·en(콘솔 언어)만 쓰고, 앱은 회원 모국어 30종을 쓴다 — 같은 (call_id,
locale) 은 누가 만들었든 같은 번역이라 서로 재사용해도 된다.
⛔ `call.summary` 원본은 덮어쓰지 않는다(원본은 학습자 것).
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Identity, MetaData, Table, Text, UniqueConstraint, func

#: 앱 alembic 이 보지 않는 별도 메타데이터(위 docstring). 시험(sqlite)은 이걸로 create 한다.
b2b_shared_metadata = MetaData()

call_summary_translation = Table(
    "call_summary_translation",
    b2b_shared_metadata,
    Column("call_summary_translation_id", BigInteger, Identity(), primary_key=True),
    Column("call_id", BigInteger, nullable=False),
    Column("locale", Text, nullable=False),
    Column("text", Text, nullable=False),
    Column("source_locale", Text),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("call_id", "locale", name="uq_call_summary_translation"),
)
