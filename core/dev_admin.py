"""[dev 전용] 제네릭 DB 어드민 백엔드.

Base.metadata 에 등록된 테이블만 화이트리스트로 허용해 조회/수정/삭제한다(임의 SQL 금지).
운영(prod)에서는 라우터 자체가 노출되지 않는다(main.py 가 ENV!=prod 에서만 등록).
민감정보(예: member.password 해시)도 그대로 노출되므로 절대 공개망에 띄우지 말 것.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from db.registry import Base


def _table(name: str):
    t = Base.metadata.tables.get(name)
    if t is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"알 수 없는 테이블: {name}")
    return t


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    return v


def _coerce(col, v: Any) -> Any:
    """문자열 입력을 컬럼 파이썬 타입으로 변환(빈 문자열 → None)."""
    if v is None or v == "":
        return None
    try:
        pt = col.type.python_type
        if pt is bool:
            return str(v).lower() in ("1", "true", "t", "yes", "y")
        if pt is int:
            return int(v)
        if pt is float:
            return float(v)
        if pt is Decimal:
            return Decimal(str(v))
    except Exception:  # noqa: BLE001 - 변환 실패 시 원본 그대로
        return v
    return v


def meta(db: Session) -> list[dict]:
    """전체 테이블 메타(컬럼·PK·행 수)."""
    out: list[dict] = []
    for name, t in sorted(Base.metadata.tables.items()):
        cnt = db.scalar(select(func.count()).select_from(t))
        cols = [
            {"name": c.name, "type": str(c.type), "pk": bool(c.primary_key)}
            for c in t.columns
        ]
        out.append({
            "name": name,
            "count": int(cnt or 0),
            "columns": cols,
            "pk": [c.name for c in t.primary_key.columns],
        })
    return out


def rows(db: Session, table: str, limit: int, offset: int) -> dict:
    """한 테이블의 행을 PK 순으로 페이지네이션 조회."""
    t = _table(table)
    order = list(t.primary_key.columns) or list(t.columns)[:1]
    stmt = select(t).order_by(*order).limit(limit).offset(offset)
    data = [
        {k: _jsonable(v) for k, v in r._mapping.items()}
        for r in db.execute(stmt)
    ]
    total = db.scalar(select(func.count()).select_from(t))
    return {
        "rows": data,
        "total": int(total or 0),
        "columns": [c.name for c in t.columns],
        "pk": [c.name for c in t.primary_key.columns],
    }


def _pk_conditions(t, pk: dict):
    conds = [t.c[k] == v for k, v in (pk or {}).items() if k in t.c]
    if not conds:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PK 조건이 필요합니다.")
    return conds


def delete_row(db: Session, table: str, pk: dict) -> int:
    """PK 로 한 행 삭제. 삭제된 행 수 반환."""
    t = _table(table)
    res = db.execute(delete(t).where(*_pk_conditions(t, pk)))
    db.commit()
    return res.rowcount or 0


def update_row(db: Session, table: str, pk: dict, changes: dict) -> int:
    """PK 로 한 행의 일부 컬럼 수정(PK 컬럼은 변경 불가). 변경 행 수 반환."""
    t = _table(table)
    pk_names = {c.name for c in t.primary_key.columns}
    vals = {
        k: _coerce(t.c[k], v)
        for k, v in (changes or {}).items()
        if k in t.c and k not in pk_names
    }
    if not vals:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "변경할 컬럼이 없습니다.")
    res = db.execute(update(t).where(*_pk_conditions(t, pk)).values(**vals))
    db.commit()
    return res.rowcount or 0
