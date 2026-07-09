"""10 (d) Branch Prediction — C 에선 극적, 파이썬에선 흐릿한 이유.

CPU 는 'if 가 참일까 거짓일까'를 미리 찍어(분기 예측) 파이프라인을 채워 둔다.
정렬된 데이터는 분기가 한동안 계속 거짓 → 계속 참이라 예측이 잘 맞고,
무작위 데이터는 50:50 이라 자꾸 틀려(misprediction) 파이프라인을 버린다.
C 로 짠 타이트한 루프에선 이 차이가 수 배로 벌어진다(유명한 벤치마크).

파이썬에선? 실측해서 정직하게 본다. 결론부터: 재현되는 것과 안 되는 것이 있다.
  1) if 분기 버전: 정렬이 비정렬보다 확실히 빠르다(~30%). 이건 매 실행 재현된다.
  2) 브랜치리스 버전(데이터 의존 제어흐름 제거): '분기예측만' 떼어내려 만든
     대조군인데, 그 gap 이 실행마다 if 보다 크기도/작기도 하다 → 파이썬에선
     분기예측 몫을 깨끗이 분리 못 한다. (그 자체가 정직한 발견이다.)
  3) 반복당 시간이 ~15ns 로, 바이트코드 디스패치 오버헤드가 워낙 커서
     분기 mispredict(수 ns)가 그 안에 묻힌다 → C 의 5~6배 같은 극적 효과는 없다.

즉 "정렬이 빠르다"는 재현되지만 "그게 다 분기예측"이라는 순진한 귀속은
파이썬에선 못 세운다. rep 을 넉넉히(9회) 주고 trial 3번으로 재현성을 보인다.

numpy 마스크합도 곁들이는데, numpy 는 데이터 의존 분기가 없다(벡터화).
그래서 numpy 의 정렬/비정렬 차이는 '분기예측'이 아니라 '흩어진 gather = 캐시
미스'다 — 순진한 해석이 원인을 오인하기 쉽다는 걸 같이 보여준다.

실행: uv run --with numpy python 10_branch_prediction.py
"""

from __future__ import annotations

import random
import time

import numpy as np

N = 2_000_000


def best_ms(fn, rep: int = 9) -> float:
    best = 1e9
    for _ in range(rep):
        t0 = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t0)
    return best * 1000


def with_branch(arr) -> int:
    """데이터 의존 if 분기 있음."""
    s = 0
    for v in arr:
        if v >= 128:
            s += v
    return s


def branchless(arr) -> int:
    """같은 결과, 데이터 의존 제어흐름 제거(비트마스크)."""
    s = 0
    for v in arr:
        s += v & -(v >= 128)  # (v>=128)이 1이면 v, 0이면 0
    return s


def main() -> None:
    rng = random.Random(0)
    data = [rng.randint(0, 255) for _ in range(N)]
    srt = sorted(data)
    assert with_branch(data) == branchless(data) == with_branch(srt)

    print(f"원소 {N:,}개 (0~255), 조건: v >= 128  |  best-of-9, trial 3회")
    print("       | if:비정렬  정렬   gap  | 브랜치리스:비정렬  정렬   gap")
    for trial in range(3):
        bu = best_ms(lambda: with_branch(data))
        bs = best_ms(lambda: with_branch(srt))
        lu = best_ms(lambda: branchless(data))
        ls = best_ms(lambda: branchless(srt))
        print(
            f"trial{trial} | {bu:7.1f} {bs:6.1f} {bu - bs:5.1f} ms "
            f"| {lu:9.1f} {ls:6.1f} {lu - ls:5.1f} ms"
        )
    print(f"(if 비정렬 반복당 약 {bu / N * 1e6:.1f} ns — 디스패치 오버헤드가 지배적)")
    print("→ '정렬이 빠르다'는 재현된다. 하지만 브랜치리스 gap 이 if gap 보다")
    print("  크기도/작기도 해서, 파이썬에선 '분기예측 몫'을 깨끗이 못 떼어낸다.")
    print()

    print("=== numpy 마스크합 (분기 없음 = 벡터화) ===")
    na = np.array(data, dtype=np.int64)
    ns = np.sort(na)
    nu = best_ms(lambda: int(na[na >= 128].sum()))
    nsr = best_ms(lambda: int(ns[ns >= 128].sum()))
    print(f"  비정렬 {nu:7.3f} ms   정렬 {nsr:7.3f} ms")
    print("  → 이 차이는 '분기예측'이 아니라 흩어진 gather = 캐시 미스다.")


if __name__ == "__main__":
    main()
