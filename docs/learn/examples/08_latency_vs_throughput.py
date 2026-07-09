"""08 (c) Latency vs Throughput — 다른 축이다.

각 "요청"은 50ms 걸리는 I/O 대기(await asyncio.sleep)다.
  - 요청 하나의 지연(latency)은 동시성과 무관하게 ~50ms 로 그대로다.
  - 하지만 동시에 여러 개를 처리하면 초당 처리량(throughput=RPS)이 오른다.

Little's Law: 평균 동시처리 ≈ 도착률(RPS) × 평균 지연(s).
  => concurrency 를 올리면 지연 그대로여도 RPS 가 오른다(대기가 겹치므로).

모듈 1~2(GIL: I/O 는 대기중 양보 / event loop: 한 스레드로 겹침)의 연장이다.

실행: python 08_latency_vs_throughput.py
"""

from __future__ import annotations

import asyncio
import time

REQUESTS = 200
WORK_S = 0.05  # 요청당 I/O 지연 50ms


async def handle() -> float:
    """요청 하나: 50ms I/O 대기. 개별 지연을 ms 로 반환."""
    t0 = time.perf_counter()
    await asyncio.sleep(WORK_S)
    return (time.perf_counter() - t0) * 1000


async def run(concurrency: int) -> None:
    """concurrency 개씩 동시에 흘려보내며 총 시간·RPS·평균 지연 측정."""
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []

    async def guarded() -> None:
        async with sem:
            latencies.append(await handle())

    t0 = time.perf_counter()
    await asyncio.gather(*(guarded() for _ in range(REQUESTS)))
    elapsed = time.perf_counter() - t0

    rps = REQUESTS / elapsed
    mean_lat = sum(latencies) / len(latencies)
    print(
        f"concurrency={concurrency:>3} | "
        f"총 {elapsed:5.2f}s | RPS={rps:7.1f} | 요청당 지연 평균={mean_lat:5.1f}ms"
    )


async def main() -> None:
    print(f"작업: {WORK_S*1000:.0f}ms I/O 요청 {REQUESTS}개를, 동시성만 바꿔가며 처리")
    for c in (1, 10, 50, 200):
        await run(c)
    print()
    print("관찰: 요청당 지연은 ~50ms 로 거의 불변. 그런데 RPS 는 동시성에 비례해 상승.")
    print("      Little's Law: RPS ≈ concurrency / 지연(s) = concurrency / 0.05")


if __name__ == "__main__":
    asyncio.run(main())
