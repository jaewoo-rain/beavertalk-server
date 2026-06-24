"""현재 Supabase DB 스키마를 읽기 전용으로 점검(아무것도 변경하지 않음).

public 스키마의 테이블 목록 + 각 테이블 행 수 + 내 모델과의 대조.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.config import settings
from db.engine import build_engine
from db.registry import Base

engine = build_engine(settings)
insp = inspect(engine)
existing = sorted(insp.get_table_names(schema="public"))
model_tables = sorted(Base.metadata.tables.keys())

print(f"=== public 스키마 기존 테이블: {len(existing)}개 ===")
with engine.connect() as conn:
    for t in existing:
        try:
            n = conn.execute(text(f'SELECT count(*) FROM public."{t}"')).scalar()
        except Exception as e:  # noqa: BLE001
            n = f"(count 실패: {e})"
        cols = [c["name"] for c in insp.get_columns(t, schema="public")]
        print(f"  - {t:18s} rows={n}  cols={cols}")

print(f"\n=== 내 모델 테이블: {len(model_tables)}개 ===")
for t in model_tables:
    mark = "있음" if t in existing else "없음(신규)"
    print(f"  - {t:18s} [{mark}]")

only_db = set(existing) - set(model_tables)
if only_db:
    print(f"\n=== 모델에 없는 기존 테이블(건드리면 안 됨): {sorted(only_db)} ===")
