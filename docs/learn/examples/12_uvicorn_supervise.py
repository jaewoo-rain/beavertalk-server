"""12-Gunicorn 의 '마스터-워커 감독 트리'를 uvicorn 으로 실제로 보여준다.

Gunicorn 은 Linux 전용이라 이 머신에선 못 돈다. 하지만 개념(하나의 마스터가 여러
워커 프로세스를 낳아 감독)은 uvicorn --workers N 도 동일한 구조를 만든다. 이 드라이버는:
  1) uvicorn 12_pidtree_app:app --workers 2 를 하위 프로세스로 띄우고
  2) /who 를 여러 번 호출해 각 워커의 pid·ppid 를 수집한 뒤
  3) 'ppid 하나 아래 워커 여럿'인 감독 트리를 ASCII 로 그린다.

Gunicorn 이라면 이 트리의 '마스터' 자리에 gunicorn 마스터가, '워커' 자리에
UvicornWorker 들이 앉는다 — 죽은 워커를 마스터가 재시작하고, graceful restart 로
워커를 하나씩 교체하는 것도 이 트리 위에서 일어난다.

실행:
    uv run --with fastapi --with uvicorn --with httpx python 12_uvicorn_supervise.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections import Counter

import httpx

PORT = 8124
BASE = f"http://127.0.0.1:{PORT}"
WORKERS = 2


def main() -> int:
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "12_pidtree_app:app",
            "--workers", str(WORKERS), "--port", str(PORT),
        ],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    try:
        # 뜰 때까지 폴링(최대 ~15초)
        for _ in range(150):
            try:
                httpx.get(f"{BASE}/who", timeout=0.5)
                break
            except Exception:
                time.sleep(0.1)
        else:
            print("서버 기동 실패")
            return 1

        # 워커별 (pid, ppid) 수집
        seen: dict[int, int] = {}  # pid -> ppid
        hits: Counter[int] = Counter()
        for _ in range(20):
            r = httpx.get(f"{BASE}/who", timeout=2.0)
            d = r.json()
            seen[d["pid"]] = d["ppid"]
            hits[d["pid"]] += 1

        # ppid 는 모든 워커가 공통(=마스터). 그 값으로 트리를 그린다.
        masters = set(seen.values())
        print("\n=== 감독 트리(마스터 → 워커) ===")
        for master in sorted(masters):
            print(f"master PID {master}   (요청을 직접 처리하지 않고 워커를 감독)")
            for pid in sorted(p for p, pp in seen.items() if pp == master):
                print(f"  └─ worker PID {pid}   ({hits[pid]}개 요청 처리)")
        print(f"\n워커 수: {len(seen)}개, 공통 부모(마스터) 수: {len(masters)}개")
        print("=> 워커 여럿의 부모(ppid)가 '하나의 마스터'로 모인다 = 감독 트리.")
        print("   Gunicorn 이라면 이 마스터 자리에 gunicorn, 워커 자리에 UvicornWorker 가 앉는다.")
        return 0
    finally:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
