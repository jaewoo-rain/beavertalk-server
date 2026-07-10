"""10 (a) Cache Locality — 같은 데이터, 접근 순서만 바꿔도 몇 배.

CPU 는 RAM 을 캐시라인(보통 64바이트) 단위로 통째로 읽어 온다. 메모리를
'연속으로' 훑으면 한 번 읽어온 라인을 알뜰히 다 쓰지만(캐시 히트), 'N칸씩
뜀뛰기'하면 매번 새 라인을 읽어야 해서(캐시 미스) 느리다.

numpy 는 C 로 짠 연속 메모리라 이 효과가 파이썬 for 루프보다 훨씬 선명하게
보인다. 여기서는 '같은 원소'를 두 방식으로 복사한다:
  - 연속 복사   copyto(dst, a)   : a 를 메모리 순서대로 읽는다 (캐시 친화)
  - 전치 복사   copyto(dst, a.T) : a.T 는 같은 데이터의 전치 '뷰'라, 실제로는
                                   원본을 세로로(스트라이드 N*8바이트) 뜀뛰기하며 읽는다

두 경우 총 읽는 바이트 수·원소 수는 완전히 같다. 오직 접근 순서만 다르다.

실행: uv run --with numpy python 10_cache_locality.py
"""

from __future__ import annotations

import time

import numpy as np


def best_ms(fn, rep: int = 7) -> float:
    """여러 번 재서 최소값(ms). 최소값은 OS 방해가 가장 적었던 '진짜 실력'."""
    best = 1e9
    for _ in range(rep):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


def main() -> None:
    n = 4000
    a = np.arange(n * n, dtype=np.float64).reshape(n, n)  # C-연속(행이 메모리에 붙어 있음)
    dst = np.empty_like(a)

    t_lin = best_ms(lambda: np.copyto(dst, a))     # 연속 읽기
    t_tr = best_ms(lambda: np.copyto(dst, a.T))    # 전치 = 뜀뛰기 읽기

    print(f"배열 {n}x{n} float64 ({a.nbytes / 1e6:.0f} MB), 원소 {n * n:,}개")
    print(f"연속 복사  copyto(dst, a)    : {t_lin:7.2f} ms  (메모리 순서대로 = 캐시 히트)")
    print(f"전치 복사  copyto(dst, a.T)  : {t_tr:7.2f} ms  (세로 뜀뛰기 = 캐시 미스)")
    print(f"느려진 배수                  : {t_tr / t_lin:5.2f}x")
    print()
    print("같은 원소, 같은 총 바이트, 같은 연산 — 오직 '접근 순서'만 달랐다.")


if __name__ == "__main__":
    main()
