"""GIL 실험 (b): I/O 바운드 작업은 스레드로 빨라진다.

time.sleep 은 "기다리는" 작업이다. 파이썬은 sleep 같은 블로킹 I/O 에 들어가면 GIL 을
놓아준다. 그래서 다른 스레드가 그동안 GIL 을 잡고 자기 sleep 에 들어갈 수 있어,
여러 대기가 겹쳐(overlap) 총 시간이 줄어든다.

기대: 스레드 4개 = 대략 1초 (4초가 아님).
"""

from __future__ import annotations

import time
from threading import Thread


def io_task() -> None:
    """실제 네트워크/DB 대기를 흉내: 1초 동안 '기다림'(그 사이 GIL 풀림)."""
    time.sleep(1.0)


def run_sequential(n: int) -> float:
    start = time.perf_counter()
    for _ in range(n):
        io_task()
    return time.perf_counter() - start


def run_threads(n: int) -> float:
    start = time.perf_counter()
    threads = [Thread(target=io_task) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return time.perf_counter() - start


if __name__ == "__main__":
    N = 4
    print(f"작업: time.sleep(1.0) 를 {N}번")
    print(f"순차       : {run_sequential(N):6.3f} s   <- 4번 = 약 4초")
    print(f"스레드 {N}개 : {run_threads(N):6.3f} s   <- 겹침 = 약 1초")
