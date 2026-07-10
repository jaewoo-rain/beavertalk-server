"""07 (b) N+1 문제 실측 — lazy 로딩 vs selectinload.

부모(Call) N개 + 각자 자식(Sentence) 을 조회한다.
- lazy(기본)  : 부모 1쿼리 + 자식 접근 때마다 1쿼리 = 1 + N 쿼리
- selectinload: 부모 1쿼리 + 자식 IN 묶음 1쿼리 = 2 쿼리

우리 코드(alarm_repository / call_repository)의 selectinload 패턴과 같은 원리.
쿼리 수는 SQLAlchemy 의 before_cursor_execute 이벤트로 실제로 센다.
"""

from __future__ import annotations

import time

from sqlalchemy import ForeignKey, create_engine, event
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, Session, mapped_column, relationship, selectinload,
)

N_PARENTS = 50
CHILDREN_EACH = 4


class Base(DeclarativeBase):
    pass


class Call(Base):
    __tablename__ = "call"
    id: Mapped[int] = mapped_column(primary_key=True)
    sentences: Mapped[list["Sentence"]] = relationship(back_populates="call")


class Sentence(Base):
    __tablename__ = "sentence"
    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[int] = mapped_column(ForeignKey("call.id"))
    call: Mapped["Call"] = relationship(back_populates="sentences")


def make_engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        for _ in range(N_PARENTS):
            c = Call()
            c.sentences = [Sentence() for _ in range(CHILDREN_EACH)]
            s.add(c)
        s.commit()
    return eng


def count_queries(eng):
    box = {"n": 0}

    @event.listens_for(eng, "before_cursor_execute")
    def _c(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            box["n"] += 1
    return box


def run_lazy(eng):
    box = count_queries(eng)
    t0 = time.perf_counter()
    with Session(eng) as s:
        calls = s.query(Call).all()            # 1쿼리
        total = sum(len(c.sentences) for c in calls)  # 접근마다 +1쿼리 → N번
    dt = time.perf_counter() - t0
    print(f"  lazy (기본)      : {box['n']:3d} 쿼리, {dt*1000:6.2f} ms  (문장 {total}개)")


def run_selectin(eng):
    box = count_queries(eng)
    t0 = time.perf_counter()
    with Session(eng) as s:
        calls = s.query(Call).options(selectinload(Call.sentences)).all()  # 2쿼리
        total = sum(len(c.sentences) for c in calls)   # 이미 로드됨 → 추가쿼리 0
    dt = time.perf_counter() - t0
    print(f"  selectinload     : {box['n']:3d} 쿼리, {dt*1000:6.2f} ms  (문장 {total}개)")


if __name__ == "__main__":
    print(f"부모(Call) {N_PARENTS}개, 각자 자식(Sentence) {CHILDREN_EACH}개\n")
    run_lazy(make_engine())       # 이벤트가 엔진별이라 엔진을 새로 만든다
    run_selectin(make_engine())
    print(f"\n이론값: lazy = 1 + {N_PARENTS} = {1+N_PARENTS} 쿼리 / selectin = 2 쿼리")
