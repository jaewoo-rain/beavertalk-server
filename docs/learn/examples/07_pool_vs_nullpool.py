"""07 (a) 커넥션 풀 있음(QueuePool) vs 없음(NullPool) — 실측 비교.

원격 DB 에 부하를 주지 않으려고 로컬 SQLite 를 쓴다. 단, SQLite 재연결은
너무 싸서 '연결 비용'이 안 보인다. 그래서 connect 이벤트에 5ms sleep 을 걸어
실제 DB 의 'TCP 핸드셰이크 + TLS + 인증' 왕복 비용을 인위적으로 흉내 낸다.

- NullPool : 매 체크아웃마다 새 물리 연결 → 매번 5ms 핸드셰이크를 냄
- QueuePool: 미리 만든 연결을 재사용 → 핸드셰이크는 처음 몇 번뿐
"""

from __future__ import annotations

import time

from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import NullPool, QueuePool

HANDSHAKE_S = 0.005   # 5ms: 원격 DB 연결 수립(TCP+TLS+auth) 비용 흉내
N = 100               # 쿼리 반복 횟수


def build(poolclass):
    eng = create_engine("sqlite:///:memory:", poolclass=poolclass)

    # 물리 연결이 '새로 수립'될 때마다 호출되는 이벤트 = 진짜 핸드셰이크 지점
    @event.listens_for(eng, "connect")
    def _simulate_handshake(dbapi_conn, conn_record):
        time.sleep(HANDSHAKE_S)

    return eng


def bench(poolclass, label):
    eng = build(poolclass)
    connects = {"n": 0}

    @event.listens_for(eng, "connect")
    def _count(dbapi_conn, conn_record):
        connects["n"] += 1

    t0 = time.perf_counter()
    for _ in range(N):
        # with 블록마다 풀에서 연결을 '체크아웃' 후 반납. 쿼리 1번.
        with eng.connect() as conn:
            conn.execute(text("SELECT 1")).one()
    dt = time.perf_counter() - t0
    print(f"{label:>28} : {dt*1000:7.1f} ms   (물리 connect {connects['n']}회)")
    eng.dispose()


if __name__ == "__main__":
    print(f"작업: SELECT 1 을 {N}회. 연결 수립 비용 = {HANDSHAKE_S*1000:.0f}ms 흉내\n")
    bench(NullPool, "NullPool (풀 없음)")
    bench(QueuePool, "QueuePool (풀 재사용)")
    print(f"\n이론값: 풀 없으면 ~{N*HANDSHAKE_S*1000:.0f}ms(매번 핸드셰이크), "
          f"풀 있으면 처음 1회만 ~{HANDSHAKE_S*1000:.0f}ms")
