"""asyncio.TaskGroup (3.11+): 구조적 동시성 — 하나가 죽으면 형제를 취소한다.

beavertalk 의 통화 본체(_run_session)가 쓰는 바로 그 패턴:
  두 개의 '펌프'를 TaskGroup 으로 묶어 돌리다가, 하나라도 예외를 던지면 TaskGroup 이
  남은 형제 태스크를 자동 취소하고, 모인 예외를 ExceptionGroup 으로 올린다.
  받는 쪽은 except* 로 종류별로 푼다.

이 데모: 펌프 2개 + '1초 뒤 실패하는' 태스크. 실패 순간 펌프 둘이 취소되는 걸 관찰.
"""

from __future__ import annotations

import asyncio


async def pump(name: str, tick: float) -> None:
    try:
        while True:
            await asyncio.sleep(tick)
            print(f"  {name} tick")
    except asyncio.CancelledError:
        print(f"  {name} <- 형제 실패로 취소됨(정리 실행 가능)")
        raise  # 취소는 다시 올려주는 게 예의(정리만 하고 삼키지 말 것)


async def failing(after: float) -> None:
    await asyncio.sleep(after)
    print("  failing: 이제 예외를 던진다")
    raise ValueError("펌프 하나가 죽음")


async def main() -> None:
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(pump("펌프1", 0.3))
            tg.create_task(pump("펌프2", 0.3))
            tg.create_task(failing(1.0))
    except* ValueError as eg:  # ExceptionGroup 을 종류별로 언패킹
        print("except* ValueError 로 잡음:", [str(e) for e in eg.exceptions])
    print("TaskGroup 블록을 벗어남 — 형제는 모두 정리되어 남는 태스크 없음")


if __name__ == "__main__":
    asyncio.run(main())
