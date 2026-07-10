"""12-Gunicorn 대체 시연용 앱: 자기 PID 와 부모 PID(PPID)를 반환한다.

Gunicorn 은 이 머신(Windows)에서 못 돈다(Linux 전용). 그래서 '마스터가 워커를
감독하는 프로세스 트리'를 uvicorn --workers 2 로 대신 보여준다. 워커는 자기 pid 와
ppid(=마스터)를 반환하므로, 여러 워커의 ppid 가 '하나의 공통 마스터'로 모이는 것을
드라이버에서 트리로 그릴 수 있다.

실행(직접): uv run --with fastapi --with uvicorn uvicorn 12_pidtree_app:app --workers 2
드라이버로: 12_uvicorn_supervise.py 참고.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI()


@app.get("/who")
def who() -> dict[str, int]:
    """이 요청을 처리한 워커의 pid 와 그 부모(마스터)의 pid."""
    return {"pid": os.getpid(), "ppid": os.getppid()}
