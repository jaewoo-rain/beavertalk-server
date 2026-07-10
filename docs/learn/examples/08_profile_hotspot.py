"""08 (a) cProfile 로 병목(hotspot) 찾기.

일부러 두 가지 비효율을 심었다:
  1) 멤버십 검사를 list 로 (O(n)) — set 이면 O(1)
  2) 문자열을 += 로 누적 (매번 새 문자열 생성) — "".join 이면 O(n)

cProfile 로 돌려 pstats 로 tottime 상위 함수를 출력한다.
"측정 없이 추측하지 말고, 시간 먹는 함수를 눈으로 보라"가 요지다.

실행: python 08_profile_hotspot.py
"""

from __future__ import annotations

import cProfile
import io
import pstats

N = 20_000


def slow_membership(items: list[int], probes: list[int]) -> int:
    """list 로 in 검사 — 매 probe 마다 리스트를 처음부터 훑는다(O(n))."""
    hits = 0
    for p in probes:
        if p in items:  # <- 여기가 병목: list.__contains__
            hits += 1
    return hits


def slow_concat(words: list[str]) -> str:
    """+= 로 문자열 누적 — 매번 새 str 을 만들어 복사한다(O(n^2))."""
    s = ""
    for w in words:
        s += w  # <- 여기가 병목: 반복 재할당
    return s


def workload() -> None:
    items = list(range(N))
    probes = list(range(N // 2, N + N // 2))  # 절반은 miss (최악 근처까지 훑음)
    slow_membership(items, probes)
    slow_concat([str(i) for i in range(N)])


# --- 고친 버전 -------------------------------------------------------------
def fast_membership(items_set: set[int], probes: list[int]) -> int:
    hits = 0
    for p in probes:
        if p in items_set:  # set.__contains__ = 해시 O(1)
            hits += 1
    return hits


def fast_concat(words: list[str]) -> str:
    return "".join(words)  # 한 번에 크기 계산 후 복사


def workload_fixed() -> None:
    items = set(range(N))
    probes = list(range(N // 2, N + N // 2))
    fast_membership(items, probes)
    fast_concat([str(i) for i in range(N)])


def profile(fn, label: str) -> None:
    pr = cProfile.Profile()
    pr.enable()
    fn()
    pr.disable()
    buf = io.StringIO()
    st = pstats.Stats(pr, stream=buf).sort_stats("tottime")
    st.print_stats(6)  # tottime 상위 6개
    print(f"===== {label} =====")
    # 헤더 + 상위 몇 줄만 깔끔히 출력
    for line in buf.getvalue().splitlines():
        if line.strip():
            print(line)
    print()


if __name__ == "__main__":
    profile(workload, "BEFORE  (list in / str +=)")
    profile(workload_fixed, "AFTER   (set in / join)")
