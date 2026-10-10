"""운영 DB 에는 있지만 모델에는 없는 열이 있어도 ORM 조회·쓰기가 깨지지 않는다(2026-10-10).

운영 DB 는 c1d2e3f40506(voice.openai_name) · d2e3f4051607(prompt_override · call.prompt_override_id)
까지 적용돼 있는데, main 에는 그 마이그레이션 파일만 들이고 모델·기능 코드는 넣지 않았다.
SQLAlchemy 는 선언한 열만 SELECT·INSERT 하므로 모르는 열은 무시된다 — 그 가정을 여기서 잠근다.
두 열 모두 NULL 허용이라 INSERT 에서 빠져도 된다.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from db.registry import Base
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call


def _engine_with_prod_extras():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    e = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    Base.metadata.create_all(e)
    with e.begin() as c:
        # 운영에만 있는 열·테이블(모델에 없음)
        c.execute(text("ALTER TABLE voice ADD COLUMN openai_name TEXT"))
        c.execute(text("ALTER TABLE call ADD COLUMN prompt_override_id BIGINT"))
        c.execute(text("CREATE TABLE prompt_override (override_id INTEGER PRIMARY KEY, name TEXT, body TEXT)"))
    return e


def test_orm_reads_and_writes_ignore_unmapped_prod_columns():
    e = _engine_with_prod_extras()
    with Session(e) as db:
        v = Voice(name="Fenrir", gender="male")
        db.add(v)
        db.flush()
        ch = Character(name="비비", role="선생님", personality="다정함", voice_id=v.voice_id, price=0)
        m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="u1",
                   actual_nationality="PH")
        db.add_all([ch, m])
        db.flush()
        db.add(Call(member_id=m.member_id, character_id=ch.character_id, call_type="expression",
                    call_date=datetime.now(timezone.utc), status="done"))
        db.commit()

    with Session(e) as db:
        assert db.scalar(select(Member.actual_nationality)) == "PH"
        call = db.scalars(select(Call)).one()
        assert call.status == "done"
        assert db.scalars(select(Voice)).one().name == "Fenrir"
        # 모르는 열은 NULL 로 남는다(쓰기에서 빠졌다)
        assert db.execute(text("select prompt_override_id from call")).scalar() is None
        assert db.execute(text("select openai_name from voice")).scalar() is None
