"""13-Multiprocessing (b): 프로세스풀이 '항상' 이득은 아니다 — 피클링/복사 비용.

프로세스는 메모리를 공유하지 않는다. 인자와 반환값은 pickle 로 직렬화돼 파이프로
자식에 복사된다. 작업이 가볍고 데이터가 크면, 이 '전달 비용'이 계산 이득을 잡아먹어
오히려 순차보다 느려진다.

두 시나리오를 실측한다:
  1) 무거운 계산 + 작은 데이터  → 프로세스풀 이득 (계산이 전달비용을 압도)
  2) 가벼운 계산 + 큰 데이터    → 프로세스풀 손해 (전달비용이 계산을 압도)

실행:
    uv run python 13_pickle_overhead.py
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ProcessPoolExecutor


def heavy_small(_: int) -> int:
    """작은 입력(int) → 무거운 계산. 전달할 데이터는 int 하나뿐."""
    total = 0
    for i in range(3_000_000):
        total += i * i % 7
    return total


def light_big(chunk: bytes) -> int:
    """큰 입력(수 MB bytes) → 가벼운 계산(앞부분 합). 전달 비용이 지배한다."""
    return sum(chunk[:200_000])  # 앞 200KB 만 훑음 — 8MB 전달에 비하면 계산은 미미


def bench(label: str, fn, args, workers: int) -> None:
    t0 = time.perf_counter()
    seq = [fn(a) for a in args]
    seq_dt = time.perf_counter() - t0

    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as ex:
        par = list(ex.map(fn, args))
    par_dt = time.perf_counter() - t0

    print(f"[{label}]")
    print(f"  순차       : {seq_dt:6.3f} s")
    if par_dt < seq_dt:
        print(f"  프로세스풀 : {par_dt:6.3f} s   ({seq_dt / par_dt:.2f}x 빠름)  → 프로세스풀 이득 ✅")
    else:
        print(
            f"  프로세스풀 : {par_dt:6.3f} s   ({par_dt / seq_dt:.1f}x 느림)  "
            "→ 프로세스풀 손해 ❌ (피클/복사·spawn 비용 > 계산)"
        )
    assert seq == par  # 결과 동일성 확인
    print()


def main() -> None:
    workers = os.cpu_count() or 1
    print(f"os.cpu_count() = {workers}\n")

    # 1) 무거운 계산 + 작은 데이터: 전달할 건 int 하나 → 계산이 압도 → 이득
    bench("무거운 계산 + 작은 데이터", heavy_small, list(range(workers)), workers)

    # 2) 가벼운 계산 + 큰 데이터: 8MB 짜리 bytes 를 워커마다 넘김 → 피클/복사 폭발 → 손해
    big = [os.urandom(8 * 1024 * 1024) for _ in range(workers)]  # 각 8MB
    bench("가벼운 계산 + 큰 데이터(8MB×N)", light_big, big, workers)


if __name__ == "__main__":
    main()
