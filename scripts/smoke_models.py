"""모델 매핑 검증 (DB 비밀번호 불필요).

1) 14개 모델 import → registry 등록
2) configure_mappers(): 모든 relationship/back_populates/foreign_keys 정합성 검사
3) 인메모리 SQLite 에 create_all: FK 타깃 해석 + DDL 생성 검증
4) 테이블/FK/관계 요약 출력
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import configure_mappers

from db.registry import Base  # noqa: F401  (전 도메인 모델 등록)

# 2) 매퍼 정합성
configure_mappers()
print("configure_mappers() OK — 관계/back_populates/foreign_keys 정합성 통과")

# 3) 인메모리 SQLite 에 전체 스키마 생성
engine = create_engine("sqlite://")
Base.metadata.create_all(engine)
print("create_all(sqlite) OK — FK 타깃 해석 + DDL 생성 성공")

# 4) 요약
tables = Base.metadata.sorted_tables
print(f"\n총 테이블: {len(tables)}")
for t in tables:
    fks = [f"{fk.parent.name}->{fk.column.table.name}" for fk in t.foreign_keys]
    pk = ",".join(c.name for c in t.primary_key.columns)
    print(f"  - {t.name:16s} PK({pk}){'  FK: ' + ', '.join(fks) if fks else ''}")

# 관계 요약
from sqlalchemy import inspect as sa_inspect  # noqa: E402

print("\n관계(relationship) 목록:")
for mapper in Base.registry.mappers:
    cls = mapper.class_
    rels = list(mapper.relationships)
    if rels:
        for r in rels:
            direction = r.direction.name
            bp = r.back_populates or "—(단방향)"
            print(f"  {cls.__name__}.{r.key:22s} {direction:14s} back_populates={bp}")
