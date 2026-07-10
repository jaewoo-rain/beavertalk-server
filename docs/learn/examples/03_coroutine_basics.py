"""코루틴 기본: 코루틴 '호출'은 즉시 실행이 아니다.

async 함수를 f() 로 부르면 '코루틴 객체'만 생긴다 — 몸통 코드는 아직 한 줄도 안 돈다.
await 하거나 asyncio.run / create_task 로 스케줄해야 비로소 실행된다.
await 를 잊으면 파이썬이 종료 때 RuntimeWarning 을 낸다.
"""

from __future__ import annotations

import asyncio
import gc


async def greet(name: str) -> str:
    await asyncio.sleep(0.1)
    return f"안녕, {name}"


async def main() -> None:
    coro = greet("비버")  # 아직 실행 안 됨 — 코루틴 객체일 뿐
    print("greet('비버') 반환 타입:", type(coro).__name__)
    print("아직 몸통은 안 돌았다. 이제 await 한다 ->")
    result = await coro  # 여기서 실제 실행
    print("await 결과:", result)


if __name__ == "__main__":
    asyncio.run(main())

    # 일부러 await 하지 않은 코루틴 → GC 시 경고가 뜬다.
    forgotten = greet("잊힌사람")
    del forgotten
    gc.collect()
    print("(위/아래에 'coroutine ... was never awaited' 경고가 보이면 정상)")
