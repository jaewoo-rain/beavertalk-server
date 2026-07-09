"""08 (b) 평균의 함정 — mean/median vs P95/P99.

"대부분 빠르고 가끔 느린" 현실적 지연 분포를 만든다.
  - 95%: 20ms 근처 (빠른 정상 응답)
  - 5% : 300~800ms (느린 꼬리 — 캐시 미스/GC/네트워크 재전송/멀티콜)

평균만 보면 "괜찮아 보이지만" P99 는 처참하다 — 100명 중 1명이 겪는 최악.
statistics 로 계산하고 ASCII 히스토그램으로 분포를 그린다.

실행: python 08_latency_percentiles.py
"""

from __future__ import annotations

import random
import statistics


def make_samples(n: int = 1000) -> list[float]:
    """지연(ms) 샘플. 시드 고정으로 재현 가능."""
    rng = random.Random(42)
    out: list[float] = []
    for _ in range(n):
        if rng.random() < 0.95:
            out.append(rng.gauss(20, 4))       # 빠른 95%
        else:
            out.append(rng.uniform(300, 800))  # 느린 꼬리 5%
    return [max(1.0, x) for x in out]


def percentile(sorted_vals: list[float], q: float) -> float:
    """q 분위(0~100)를 nearest-rank 로. P95 = 상위 5% 경계."""
    if not sorted_vals:
        return 0.0
    k = max(0, min(len(sorted_vals) - 1, round(q / 100 * len(sorted_vals)) - 1))
    return sorted_vals[k]


def histogram(vals: list[float], buckets: int = 10, width: int = 50) -> None:
    lo, hi = min(vals), max(vals)
    step = (hi - lo) / buckets or 1.0
    counts = [0] * buckets
    for v in vals:
        idx = min(buckets - 1, int((v - lo) / step))
        counts[idx] += 1
    peak = max(counts) or 1
    for i, c in enumerate(counts):
        left = lo + i * step
        right = left + step
        bar = "#" * round(c / peak * width)
        print(f"{left:6.0f}~{right:6.0f}ms | {bar} {c}")


def main() -> None:
    s = make_samples()
    s_sorted = sorted(s)

    mean = statistics.mean(s)
    median = statistics.median(s)
    p95 = percentile(s_sorted, 95)
    p99 = percentile(s_sorted, 99)
    mx = s_sorted[-1]

    print(f"샘플 수        : {len(s)}")
    print(f"mean (평균)    : {mean:7.1f} ms   <- '좋아 보인다'")
    print(f"median (중앙)  : {median:7.1f} ms   <- 절반은 이보다 빠르다")
    print(f"P95            : {p95:7.1f} ms")
    print(f"P99            : {p99:7.1f} ms   <- 100명 중 1명이 겪는 최악")
    print(f"max            : {mx:7.1f} ms")
    print(f"P99 / median   : {p99 / median:7.1f} x  (꼬리가 중앙의 몇 배인가)")
    print()
    print("분포 (ASCII 히스토그램):")
    histogram(s)


if __name__ == "__main__":
    main()
