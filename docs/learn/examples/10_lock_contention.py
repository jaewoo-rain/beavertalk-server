"""10 (b) Lock Contention — 하나의 자물쇠를 여럿이 다투면 느려진다.

여러 스레드가 '공유 카운터 하나'를 lock 으로 보호하며 올리면, 매 증가마다
lock 획득/해제 + GIL 넘김 + 문맥교환이 끼어들어 직렬화된다(경합). 반대로
스레드마다 '자기 로컬 카운터'로 세고 마지막에 한 번 합치면 다툴 게 없다.

결과 합계는 두 방식이 똑같다(4,000,000). 오직 '경합 유무'만 다르다.

주의: 파이썬은 GIL 때문에 어차피 한 번에 한 스레드만 바이트코드를 돈다.
그래서 이 데모가 재는 것은 '멀티코어 병렬 이득'이 아니라, lock 을 잡고 놓는
행위 자체 + GIL 핸드오프 + 문맥교환의 순수 오버헤드다.

실행: python 10_lock_contention.py
"""

from __future__ import annotations

import threading
import time

N_THREADS = 4
ITERS = 1_000_000


def run_shared() -> tuple[float, int]:
    """공유 카운터 하나를 lock 으로 다툰다."""
    lock = threading.Lock()
    box = {"n": 0}

    def worker() -> None:
        for _ in range(ITERS):
            with lock:
                box["n"] += 1

    ts = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return time.perf_counter() - t0, box["n"]


def run_local() -> tuple[float, int]:
    """스레드마다 로컬 카운터 → 마지막에 합산(경합 없음)."""
    totals = [0] * N_THREADS

    def worker(idx: int) -> None:
        c = 0
        for _ in range(ITERS):
            c += 1
        totals[idx] = c

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return time.perf_counter() - t0, sum(totals)


def main() -> None:
    ta, sa = run_shared()
    tb, sb = run_local()
    print(f"스레드 {N_THREADS}개, 각 {ITERS:,}회 증가 (합계 목표 {N_THREADS * ITERS:,})")
    print(f"공유 Lock 경합   : {ta * 1000:8.1f} ms   결과={sa:,}")
    print(f"스레드로컬 합산  : {tb * 1000:8.1f} ms   결과={sb:,}")
    print(f"경합이 느린 배수 : {ta / tb:6.1f}x")


if __name__ == "__main__":
    main()
