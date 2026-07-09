"""07 (c) 풀 고갈 시연 — pool_size=2, max_overflow=0, 동시 3요청.

풀에 연결이 2개뿐인데 3개 스레드가 동시에 연결을 빌리려 한다.
- 앞의 2개: 즉시 체크아웃 성공, 일을 하는 동안(0.5s) 연결을 '쥐고' 있다
- 3번째   : 빈 연결이 없어 pool_timeout 만큼 '대기'했다가, 앞이 반납하면 얻는다

원격 DB 에 붙지 않는다(로컬 SQLite). 연결 반납을 늦추려 일부러 0.5s 잡는다.
"""

from __future__ import annotations

import threading
import time

from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

eng = create_engine(
    "sqlite:///:memory:",
    poolclass=QueuePool,
    pool_size=2,        # 상시 유지 연결 2개
    max_overflow=0,     # 초과 임시연결 금지 → 최대 동시 2개
    pool_timeout=5,     # 빈 연결 없으면 최대 5초 대기 후 예외
    # SQLite 는 연결을 만든 스레드에서만 쓸 수 있는 게 기본. 풀이 연결을 스레드
    # 간에 돌려쓰는 이 데모에서만 완화한다(실 DB 드라이버엔 필요 없음).
    connect_args={"check_same_thread": False},
)

start = time.perf_counter()


def worker(i):
    t_req = time.perf_counter() - start
    with eng.connect() as conn:            # ← 여기서 체크아웃(빌리기). 없으면 대기
        t_got = time.perf_counter() - start
        conn.execute(text("SELECT 1")).one()
        waited = t_got - t_req
        print(f"  요청 {i}: 체크아웃 {t_got:5.2f}s (대기 {waited:4.2f}s) → 0.5s 점유")
        time.sleep(0.5)                    # 일하는 척 = 연결을 쥐고 있음


if __name__ == "__main__":
    print("pool_size=2, max_overflow=0 → 동시에 최대 2연결. 3요청 동시 투입\n")
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print("\n관찰: 요청 1·2 는 대기 0. 요청 3 은 앞이 반납할 때까지 ~0.5s 대기.")
    eng.dispose()
