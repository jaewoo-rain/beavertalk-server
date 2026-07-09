"""이벤트 루프 (b) — 함정 데모: async 안에서 time.sleep 을 쓰면 동시성이 죽는다.

같은 '1초짜리 일'을 5개 동시에 시키는데,
  - BEFORE: time.sleep(1.0)   <- 블로킹. 루프를 통째로 멈춤 → 5개가 줄서서 ~5초
  - AFTER : asyncio.sleep(1.0) <- 논블로킹. await 에서 양보 → 겹쳐서 ~1초

코드는 딱 한 줄(sleep 종류) 차이인데 결과가 5배 다르다.
'협력적 멀티태스킹' = 코루틴이 await 로 스스로 양보해야 루프가 남을 돌본다.
time.sleep 은 양보하지 않으므로 루프가 굶는다.
"""

from __future__ import annotations

import asyncio
import time


async def blocking_task() -> None:
    time.sleep(1.0)  # 나쁜 예: 이벤트 루프 스레드를 1초간 정지시킨다


async def async_task() -> None:
    await asyncio.sleep(1.0)  # 좋은 예: await 지점에서 루프에 제어를 넘긴다


async def run_batch(factory, n: int, label: str) -> None:
    start = time.perf_counter()
    await asyncio.gather(*(factory() for _ in range(n)))
    print(f"{label:<34}: {time.perf_counter() - start:.3f} s")


async def main() -> None:
    n = 5
    print(f"같은 '1초 일' {n}개를 gather 로 동시에 시도\n")
    await run_batch(blocking_task, n, f"BEFORE  time.sleep(1)  x{n} (블로킹)")
    await run_batch(async_task, n, f"AFTER   asyncio.sleep(1) x{n} (논블로킹)")


if __name__ == "__main__":
    asyncio.run(main())
