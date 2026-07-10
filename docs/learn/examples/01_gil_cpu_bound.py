"""GIL 실험 (a): CPU 바운드 작업은 스레드로 병렬화되지 않는다.

같은 순수 파이썬 계산(countdown)을
  - 순차로 2번
  - 스레드 2개로 동시에
  - 프로세스 2개로 동시에
실행해 시간을 비교한다.

기대: 스레드는 GIL 때문에 순차와 비슷하거나 더 느리고, 프로세스는 코어를 실제로
나눠 쓰므로 빨라진다.

Windows 에서 multiprocessing 은 반드시 `if __name__ == "__main__":` 가드가 필요하다
(안 그러면 자식이 부모 모듈을 import 하며 무한 스폰).
"""

from __future__ import annotations

import time
from multiprocessing import Process
from threading import Thread

N = 50_000_000  # 이 정도면 한 번에 대략 1~2초


def countdown(n: int) -> None:
    """순수 파이썬 루프 = 바이트코드 실행 = GIL 을 잡아야 도는 CPU 작업."""
    while n > 0:
        n -= 1


def run_sequential() -> float:
    start = time.perf_counter()
    countdown(N)
    countdown(N)
    return time.perf_counter() - start


def run_threads() -> float:
    start = time.perf_counter()
    t1 = Thread(target=countdown, args=(N,))
    t2 = Thread(target=countdown, args=(N,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    return time.perf_counter() - start


def run_processes() -> float:
    start = time.perf_counter()
    p1 = Process(target=countdown, args=(N,))
    p2 = Process(target=countdown, args=(N,))
    p1.start()
    p2.start()
    p1.join()
    p2.join()
    return time.perf_counter() - start


if __name__ == "__main__":
    print(f"작업: countdown({N:,}) 를 총 2번")
    print(f"순차 (2번)   : {run_sequential():6.3f} s")
    print(f"스레드 2개   : {run_threads():6.3f} s   <- GIL: 안 빨라짐")
    print(f"프로세스 2개 : {run_processes():6.3f} s   <- 진짜 병렬")
