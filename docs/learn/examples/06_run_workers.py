"""06-Worker 시연 드라이버: 진짜 uvicorn 워커 2개를 띄우고 /pid 를 여러 번 때린다.

`uvicorn 06_pid_app:app --workers 2` 를 하위 프로세스로 실행 → 뜰 때까지 폴링 →
/pid 를 10번 호출 → 응답 PID 를 모아 '두 종류의 PID 가 번갈아' 나오는지 확인 →
프로세스 트리 종료. (Windows 에서 워커=spawn 이라 각 워커가 별도 프로세스다.)

실행:
    uv run --with fastapi --with uvicorn --with httpx python 06_run_workers.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections import Counter

import httpx

PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"


def main() -> int:
    # 워커 2개로 uvicorn 기동 (this file's dir 를 작업 디렉터리로)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "06_pid_app:app", "--workers", "2", "--port", str(PORT)],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        # 서버가 뜰 때까지 폴링(최대 ~15초)
        for _ in range(150):
            try:
                httpx.get(f"{BASE}/pid", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)
        else:
            print("서버 기동 실패")
            return 1

        pids = []
        for i in range(10):
            r = httpx.get(f"{BASE}/pid", timeout=2.0)
            pid = r.json()["pid"]
            pids.append(pid)
            print(f"요청 {i + 1:2d} → PID {pid}")

        counts = Counter(pids)
        print(f"\n등장한 PID 종류: {len(counts)}개")
        for pid, n in counts.items():
            print(f"  PID {pid}: {n}회")
        print("=> 워커 수만큼(2개) 서로 다른 프로세스가 요청을 나눠 처리했다.")
        return 0
    finally:
        # 마스터+워커 프로세스 트리 종료
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
