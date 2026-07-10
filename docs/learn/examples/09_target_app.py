"""09 부하 테스트 대상 — 로컬에서만 잠깐 띄우는 작은 FastAPI 앱.

⚠️ 이 앱은 '부하를 걸어 보기 위한 표적'이다. 절대 프로덕션/원격 서버에
   부하를 주지 말고, 반드시 127.0.0.1(로컬)에서만 띄운다.

엔드포인트(8장 지표를 일부러 서로 다르게 뽑기 위한 4종):
  GET /fast   즉답(async, 대기 없음)        → 지연 하한, RPS 상한을 본다
  GET /slow   await asyncio.sleep(0.05)     → I/O 대기. 겹쳐서 RPS 유지(2·8장)
  GET /work   파이썬 루프로 CPU 살짝 사용     → GIL 병목. 동시성 올려도 RPS 안 오름(1장)
  GET /block  동기 def + time.sleep(0.05)   → 스레드풀로 오프로드되는 sync 경로(5장)

실행(로컬):
  uv run --with fastapi --with uvicorn uvicorn 09_target_app:app \
      --host 127.0.0.1 --port 8009
"""

from __future__ import annotations

import asyncio
import time

from fastapi import FastAPI

app = FastAPI(title="load-test target (local only)")


@app.get("/fast")
async def fast() -> dict[str, str]:
    """즉답. 아무 대기도 계산도 없다 — 순수 오버헤드 하한."""
    return {"ok": "fast"}


@app.get("/slow")
async def slow() -> dict[str, str]:
    """50ms I/O 대기(비동기). await 로 이벤트 루프에 양보한다 → 겹침 가능."""
    await asyncio.sleep(0.05)
    return {"ok": "slow"}


@app.get("/work")
async def work() -> dict[str, int]:
    """CPU 를 살짝 쓰는 순수 파이썬 루프. await 양보 지점이 없어 GIL 을 붙잡는다."""
    total = 0
    for i in range(200_000):
        total += i
    return {"sum": total}


@app.get("/block")
def block() -> dict[str, str]:
    """동기 def. FastAPI 가 스레드풀로 오프로드한다(5장). time.sleep 는 블로킹."""
    time.sleep(0.05)
    return {"ok": "block"}
