"""04-ASGI 예제 (a): 원시 ASGI 앱을 손으로 짜서 in-process 로 호출한다.

ASGI 앱은 결국 `async def app(scope, receive, send)` 하나다. FastAPI/Starlette 도
이 시그니처를 구현한 객체일 뿐이다. 여기서는 프레임워크 없이 직접 짜서
'ASGI 가 실제로 무엇을 주고받는지(scope/receive/send)'를 눈으로 본다.

실행:
    uv run --with httpx python 04_raw_asgi.py
"""

from __future__ import annotations

import asyncio

import httpx


async def app(scope, receive, send):
    """세 개의 인자만으로 동작하는 최소 ASGI 앱."""
    # 1) scope: 이 요청의 메타데이터(불변 dict) — 타입/경로/메서드 등
    print(f"[scope]   type={scope['type']!r} path={scope.get('path')!r} method={scope.get('method')!r}")

    # 2) receive: 클라이언트가 보낸 이벤트를 await 로 하나씩 당겨온다
    event = await receive()
    print(f"[receive] {event['type']!r} body={event.get('body')!r}")

    # 3) send: 응답을 '이벤트 여러 개'로 나눠 내보낸다 (헤더 먼저, 그다음 본문)
    print("[send]    http.response.start (status=200)")
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    print("[send]    http.response.body")
    await send({"type": "http.response.body", "body": "안녕, ASGI".encode()})


async def main() -> None:
    # httpx.ASGITransport: 실제 네트워크/포트 없이 앱 함수를 직접 호출한다
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://asgi.local") as client:
        print("=== in-process 로 GET / 요청 ===")
        resp = await client.get("/")
        print("--- 결과 ---")
        print(f"status={resp.status_code}")
        print(f"body={resp.text!r}")


if __name__ == "__main__":
    asyncio.run(main())
