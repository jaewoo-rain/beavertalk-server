"""09 미니 부하 생성기 — Locust 없이 asyncio+httpx 로 직접 부하를 만든다.

Locust 가 하는 일(동시에 여러 요청을 쏘고, 응답시간을 모아 P95/P99/RPS 로
집계)을 최소한으로 재현한다. 8장의 08_latency_vs_throughput 을 '실제 HTTP
서버(로컬 09_target_app)'에 대고 돌리는 버전이다.

핵심:
  - asyncio.Semaphore 로 동시성(concurrency)을 고정한다.
  - keep-alive 를 쓰기 위해 httpx.AsyncClient 를 하나만 만들어 재사용한다
    (매 요청 새 연결 → 포트 고갈은 9장 함정에서 다룸).
  - 워밍업 요청 몇 개를 먼저 버린다(콜드스타트 제외).

⚠️ URL 은 반드시 로컬. 프로덕션에 걸지 말 것.

실행:
  uv run --with httpx python 09_mini_loadgen.py http://127.0.0.1:8009/slow
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time

import httpx

TOTAL = 3000        # 총 요청 수
CONCURRENCY = 50    # 동시 진행 수(=Locust 의 유저 수와 비슷한 역할)
WARMUP = 50         # 버릴 워밍업 요청 수


async def main(url: str) -> None:
    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(limits=limits, timeout=10.0) as client:
        # 워밍업(콜드스타트/연결수립 비용을 통계에서 제외)
        await asyncio.gather(*(client.get(url) for _ in range(WARMUP)))

        sem = asyncio.Semaphore(CONCURRENCY)
        latencies: list[float] = []
        errors = 0

        async def one() -> None:
            nonlocal errors
            async with sem:
                t0 = time.perf_counter()
                try:
                    r = await client.get(url)
                    if r.status_code != 200:
                        errors += 1
                except Exception:
                    errors += 1
                    return
                latencies.append((time.perf_counter() - t0) * 1000)

        t0 = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(TOTAL)))
        elapsed = time.perf_counter() - t0

    latencies.sort()
    n = len(latencies)

    def pct(p: float) -> float:
        # 최근접 순위법(nearest-rank): P 백분위의 값
        k = max(0, min(n - 1, int(round(p / 100 * n)) - 1))
        return latencies[k]

    print(f"대상          : {url}")
    print(f"총 요청       : {TOTAL}  (동시성 {CONCURRENCY}, 워밍업 {WARMUP} 제외)")
    print(f"실패          : {errors}")
    print(f"소요          : {elapsed:6.2f} s")
    print(f"RPS(처리량)   : {n / elapsed:8.1f}")
    print(f"median (P50)  : {statistics.median(latencies):7.2f} ms")
    print(f"mean          : {statistics.fmean(latencies):7.2f} ms")
    print(f"P95           : {pct(95):7.2f} ms")
    print(f"P99           : {pct(99):7.2f} ms")
    print(f"max           : {latencies[-1]:7.2f} ms")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8009/slow"
    if not target.startswith("http://127.0.0.1") and not target.startswith("http://localhost"):
        raise SystemExit("안전장치: 로컬(127.0.0.1/localhost) URL 만 허용한다.")
    asyncio.run(main(target))
