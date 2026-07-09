"""create_task vs await: 언제 '동시에' 도는가.

- 그냥 `await work(...)` 를 이어 쓰면 하나 끝나야 다음이 시작 → 순차.
- create_task 는 코루틴을 Task(=Future 의 일종)로 감싸 '즉시 스케줄'한다. 그래서 두 태스크가
  백그라운드에서 겹쳐 진행되고, 나중에 gather/await 로 결과만 모은다.

A=1초, B=2초를 create_task 로 겹치면 총 ~2초 (3초가 아님).
"""

from __future__ import annotations

import asyncio
import time


async def work(name: str, sec: float) -> str:
    print(f"  [{time.strftime('%S')}s] {name} 시작")
    await asyncio.sleep(sec)
    print(f"  [{time.strftime('%S')}s] {name} 끝 ({sec}s)")
    return name


async def main() -> None:
    start = time.perf_counter()
    t1 = asyncio.create_task(work("A", 1.0))  # 즉시 스케줄
    t2 = asyncio.create_task(work("B", 2.0))  # 즉시 스케줄
    print("두 태스크 생성 직후 (아직 gather 안 함) — 이미 백그라운드로 도는 중")
    results = await asyncio.gather(t1, t2)  # 완료를 기다려 결과 수집
    print("gather 결과:", results)
    print(f"총 시간: {time.perf_counter() - start:.3f} s   <- max(1,2)=2초, 합(3초) 아님")


if __name__ == "__main__":
    asyncio.run(main())
