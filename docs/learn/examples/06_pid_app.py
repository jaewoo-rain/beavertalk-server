"""06-Worker 예제: os.getpid() 를 반환하는 최소 FastAPI 앱.

여러 워커로 띄우면 요청마다 서로 다른 PID(프로세스)가 응답한다는 증거를 보여준다.

실행(워커 2개):
    uv run --with fastapi --with uvicorn uvicorn 06_pid_app:app --workers 2
그다음 다른 터미널에서:
    curl http://127.0.0.1:8000/pid   # 여러 번 → PID 두 종류가 번갈아 나옴
"""

from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI()


@app.get("/pid")
def pid() -> dict[str, int]:
    """이 요청을 처리한 프로세스의 PID."""
    return {"pid": os.getpid()}
