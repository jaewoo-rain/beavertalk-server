"""11 (a) 이 OS 는 어떤 I/O 멀티플렉싱 백엔드를 쓰나.

파이썬 표준 `selectors` 모듈은 '지금 이 OS 에서 제일 좋은 것'을 자동으로 고른다.
  - 리눅스   → EpollSelector   (진짜 epoll)
  - macOS/BSD → KqueueSelector (kqueue)
  - 그 외/폴백 → SelectSelector (오래된 select, O(n))
Windows 에는 epoll 자체가 없어서 select 로 떨어진다. 우리 서버가 실제로 도는
Cloud Run(리눅스)에선 이 자리에 EpollSelector 가 온다.

실행: python 11_selector_backend.py
"""

from __future__ import annotations

import selectors
import sys


def main() -> None:
    print(f"platform             : {sys.platform}")
    print(f"DefaultSelector 클래스: {selectors.DefaultSelector}")

    sel = selectors.DefaultSelector()
    print(f"실제 인스턴스 타입    : {type(sel).__name__}")
    sel.close()

    print()
    print("이 파이썬 빌드에 존재하는 selector 백엔드:")
    for name in ("EpollSelector", "KqueueSelector", "DevpollSelector",
                 "PollSelector", "SelectSelector"):
        exists = hasattr(selectors, name)
        mark = "있음" if exists else "없음(이 OS에 없음)"
        print(f"  {name:16s}: {mark}")

    print()
    print("OS 별 기본 백엔드(개념):")
    print("  Linux   → EpollSelector   (epoll,  준비된 fd만 O(1)에 가깝게)")
    print("  macOS   → KqueueSelector  (kqueue, 준비된 이벤트만)")
    print("  Windows → SelectSelector  (select, 매번 전체 fd 스캔 O(n))")


if __name__ == "__main__":
    main()
