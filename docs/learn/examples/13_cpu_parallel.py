"""13-Multiprocessing (a): CPU 바운드는 프로세스로 진짜 병렬이 된다.

1장 (a)의 수미상관. 같은 CPU 계산(소수 세기)을 순차 vs ProcessPoolExecutor
(max_workers=코어수)로 돌려 '스피드업'을 실측한다. 스레드로는 GIL 때문에 안
빨라졌지만(1장), 프로세스는 각자 자기 GIL·자기 코어라 코어수배에 근접한다.

Windows 는 프로세스 생성이 spawn 이라 반드시 `if __name__ == "__main__"` 가드가
필요하다(없으면 자식이 모듈을 다시 import 하며 무한 스폰).

실행:
    uv run python 13_cpu_parallel.py
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor


def count_primes(limit: int) -> int:
    """2..limit 범위의 소수 개수를 센다 — 순수 파이썬 CPU 바운드(바이트코드 루프)."""
    count = 0
    for n in range(2, limit):
        is_prime = True
        i = 2
        while i * i <= n:
            if n % i == 0:
                is_prime = False
                break
            i += 1
        if is_prime:
            count += 1
    return count


def main() -> None:
    cores = os.cpu_count() or 1
    workers = cores
    tasks = [500_000] * workers  # 워커 수만큼 동일한 무게의 덩어리

    print(f"os.cpu_count() = {cores}")
    print(f"작업: count_primes(500,000) 를 총 {workers}개 (= 워커 수)\n")

    # 1) 순차: 한 프로세스가 하나씩
    t0 = time.perf_counter()
    seq = [count_primes(n) for n in tasks]
    seq_dt = time.perf_counter() - t0
    print(f"순차 ({workers}개)          : {seq_dt:6.3f} s   (결과 예: {seq[0]})")

    # 2) 프로세스풀: 코어수만큼 진짜 병렬
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        par = list(ex.map(count_primes, tasks))
    par_dt = time.perf_counter() - t0
    print(f"프로세스풀 {workers}개        : {par_dt:6.3f} s   (결과 예: {par[0]})")

    print(f"\n스피드업: {seq_dt / par_dt:.2f}x  (이상적 상한 = 코어수 {cores})")


if __name__ == "__main__":
    main()
