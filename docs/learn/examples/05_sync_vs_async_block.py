"""05-Uvicorn 예제(하이라이트): 핸들러 3종에 동시에 여러 요청을 던져 총 시간을 잰다.

- /sync-block  : `def` + time.sleep       → FastAPI 가 스레드풀로 돌려 서로 안 막음(겹침)
- /async-block : `async def` + time.sleep → 이벤트 루프를 막아 요청이 직렬화(합산)
- /async-ok    : `async def` + await asyncio.sleep → 논블로킹 양보(겹침)

in-process(httpx.ASGITransport) 라 포트/백그라운드 서버가 필요 없다.

실행:
    uv run --with fastapi --with httpx python 05_sync_vs_async_block.py
"""

from __future__ import annotations

import asyncio
import time

import httpx
from fastapi import FastAPI

app = FastAPI()
DELAY = 0.5  # 한 요청당 지연(초)
N = 5  # 동시에 던지는 요청 수


@app.get("/sync-block")
def sync_block():
    """동기 def → Starlette 가 run_in_threadpool 로 실행한다."""
    time.sleep(DELAY)
    return {"h": "sync"}


@app.get("/async-block")
async def async_block():
    """async def 안에서 블로킹 → 이벤트 루프가 멈춘다(안티패턴)."""
    time.sleep(DELAY)
    return {"h": "async-block"}


@app.get("/async-ok")
async def async_ok():
    """async def + await → 대기 중 루프에 양보한다."""
    await asyncio.sleep(DELAY)
    return {"h": "async-ok"}


async def hammer(client: httpx.AsyncClient, path: str) -> float:
    """path 로 N개 요청을 gather 로 동시에 던지고 총 소요 시간을 반환."""
    t0 = time.perf_counter()
    await asyncio.gather(*(client.get(path) for _ in range(N)))
    return time.perf_counter() - t0


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        print(f"각 엔드포인트에 {N}개 요청 동시에 (각 {DELAY}s 지연)\n")
        for path in ("/async-ok", "/sync-block", "/async-block"):
            dt = await hammer(client, path)
            print(f"{path:14s}: {dt:.3f} s")
    print(f"\n참고: 완전히 겹치면 ~{DELAY}s, 완전히 직렬이면 ~{DELAY * N}s")


if __name__ == "__main__":
    asyncio.run(main())
