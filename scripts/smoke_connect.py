"""실제 Supabase 연결 스모크 테스트 (비밀번호 필요).

.env 에 진짜 DATABASE_URL_POOL/DIRECT 를 넣은 뒤 실행:
    <env>/python.exe scripts/smoke_connect.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from db.engine import engine  # noqa: E402

with engine.connect() as conn:
    ver = conn.execute(text("select version()")).scalar()
    print("연결 성공 ✅")
    print("PostgreSQL:", ver)
