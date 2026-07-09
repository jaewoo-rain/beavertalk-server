"""04-ASGI 예제 (b): FastAPI(=Starlette 위)의 4대 요소를 in-process 로 관찰한다.

- Lifespan       : 앱 시작/종료 훅 (우리 main.py 의 엔진/세션팩토리/genai 준비)
- Middleware     : 요청/응답을 감싸는 계층 (우리 CORSMiddleware 자리)
- DI(Depends)    : 핸들러 인자로 의존성 주입 (우리 CurrentMember/DbSession)
- BackgroundTask : 응답을 보낸 뒤 실행되는 작업

각 요소가 '언제' 실행되는지 print 순서로 드러난다.

실행:
    uv run --with fastapi --with httpx python 04_starlette_features.py
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, Request


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """앱 수명 훅. yield 이전=startup, 이후=shutdown."""
    print("[lifespan] 시작: 공유 자원 준비 (엔진/세션팩토리 자리)")
    app.state.greeting = "안녕"
    yield
    print("[lifespan] 종료: 자원 정리")


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def logging_mw(request: Request, call_next):
    """요청을 감싸는 미들웨어. call_next 앞뒤로 응답을 가로챈다."""
    print(f"[middleware] --> 요청 진입 {request.url.path}")
    response = await call_next(request)
    print(f"[middleware] <-- 응답 나감 status={response.status_code}")
    return response


def get_greeting(request: Request) -> str:
    """의존성. 우리 core/deps.py 의 get_current_member 와 같은 자리다."""
    print("[DI] get_greeting 의존성 실행")
    return request.app.state.greeting


def after_response(name: str) -> None:
    """BackgroundTask. 응답을 보낸 뒤에 실행된다."""
    print(f"[background] 응답 후 실행: {name} 인사 기록")


@app.get("/hello")
async def hello(bg: BackgroundTasks, greeting: str = Depends(get_greeting)):
    print("[handler] hello 본문 실행")
    bg.add_task(after_response, "비버")
    return {"message": f"{greeting}, 비버"}


async def main() -> None:
    transport = httpx.ASGITransport(app=app)
    # ASGITransport 는 lifespan 이벤트를 보내지 않으므로 수동으로 감싼다.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            print("\n=== GET /hello ===")
            resp = await client.get("/hello")
            print(f"[client] 받은 응답: {resp.json()}\n")


if __name__ == "__main__":
    asyncio.run(main())
