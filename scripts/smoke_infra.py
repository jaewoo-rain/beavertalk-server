"""인프라 스모크 테스트 (DB 실제 연결 없음).

config 로드 → engine 생성 → session 팩토리까지 import 되는지만 확인.
실행: <env>/python.exe scripts/smoke_infra.py  (프로젝트 루트에서)
"""

import sys
from pathlib import Path

# 프로젝트 루트를 import 경로에 추가
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import settings  # noqa: E402
from db.engine import build_engine  # noqa: E402
from db.session import build_session_factory, get_db  # noqa: E402

engine = build_engine(settings)
session_factory = build_session_factory(engine)

print("ENV =", settings.ENV)
print("POOL set =", bool(settings.DATABASE_URL_POOL))
# DIRECT 는 선택값 — 없으면 POOL 로 폴백(direct_url)
print("DIRECT(폴백) set =", bool(settings.direct_url))
print("engine dialect =", engine.dialect.name)
print("pool class =", type(engine.pool).__name__)
print("session factory ready =", session_factory is not None and get_db is not None)
print("OK: 인프라 import/설정 로드 성공 (실제 DB 연결은 수행하지 않음)")
