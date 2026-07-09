"""10 (c) Context Switch 비용 감 — 같은 총작업을 더 잘게 쪼갤수록 느려진다.

총 작업량(바이트코드 루프 6,000,000회)은 고정. 이걸 T개 스레드로 나눠 돌린다.
- T가 작을 때(1~8): GIL 로 어차피 직렬 실행 → 시간 거의 일정.
- T가 커질 때(128, 512): 스레드 생성 + OS 스케줄러가 코어에 번갈아 올리는
  문맥교환(레지스터·스택 저장/복원) 오버헤드가 쌓여 총 시간이 늘어난다.

파이썬/GIL 환경에서 '스레드를 많이 만들수록 손해'라는 추세를 체감하는 게 목적.
(정밀한 문맥교환 1회 비용은 OS 도구로만 정확히 잰다 — 여기선 추세만.)

실행: python 10_context_switch.py
"""

from __future__ import annotations

import threading
import time

TOTAL = 6_000_000


def spin(n: int) -> None:
    x = 0
    while x < n:
        x += 1


def run(nthreads: int) -> float:
    per = TOTAL // nthreads
    ts = [threading.Thread(target=spin, args=(per,)) for _ in range(nthreads)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return time.perf_counter() - t0


def main() -> None:
    print(f"총작업 {TOTAL:,}회를 T개 스레드로 분할 (일은 항상 같은 양)")
    base = None
    for n in (1, 2, 4, 8, 32, 128, 512):
        dt = run(n) * 1000
        if base is None:
            base = dt
        print(f"  threads={n:4d} : {dt:8.1f} ms   (1스레드 대비 {dt / base:4.2f}x)")
    print()
    print("일의 양은 그대로인데 스레드가 많아질수록 느려진다 = 문맥교환/생성 오버헤드.")


if __name__ == "__main__":
    main()
