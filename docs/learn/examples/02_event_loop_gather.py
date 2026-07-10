"""이벤트 루프 (a): asyncio.gather 로 100개의 await 를 동시에.

asyncio.sleep(1.0) 을 100번 '동시에' 기다린다. 한 스레드/한 이벤트 루프인데도
총 시간은 100초가 아니라 약 1초다 — 각 코루틴이 await 에서 루프에 제어를 양보하고,
루프는 그동안 다른 코루틴을 진행시키기 때문.
"""

from __future__ import annotations

import asyncio
import time


async def one(i: int) -> int:
    await asyncio.sleep(1.0)  # 여기서 루프에 양보 → 루프는 다른 코루틴을 돌린다
    return i


async def main() -> None:
    n = 100
    start = time.perf_counter()
    results = await asyncio.gather(*(one(i) for i in range(n)))
    elapsed = time.perf_counter() - start
    print(f"asyncio.sleep(1.0) x{n} 동시 실행")
    print(f"  총 시간   : {elapsed:.3f} s   <- 합(100초)이 아님")
    print(f"  결과 개수 : {len(results)}")


if __name__ == "__main__":
    asyncio.run(main())
