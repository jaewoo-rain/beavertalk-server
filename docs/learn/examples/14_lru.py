"""14 (c) LRU(Least Recently Used) eviction — 캐시가 꽉 차면 뭘 버릴까.

메모리는 유한하다. 캐시가 꽉 차면 새 항목을 넣기 위해 하나를 '방출(evict)'해야
한다. 가장 흔한 정책이 LRU: **가장 오래 안 쓴 것**부터 버린다(자주 쓰는 건 남긴다).

Redis 에서는 서버 설정으로 켠다:
    maxmemory 256mb
    maxmemory-policy allkeys-lru   # 메모리 초과 시 전체 키 중 LRU 를 방출
    # (volatile-lru = TTL 있는 키 중에서만 / allkeys-lfu = 빈도 기반 등도 있음)

fakeredis 로는 maxmemory eviction 재현이 제한적이라, 여기선 파이썬 표준
functools.lru_cache 로 '오래 안 쓴 것이 방출된다'는 개념 자체를 정직하게 실측한다.
동작 원리(꽉 차면 LRU 를 버린다)는 Redis 의 allkeys-lru 와 같다.
"""

from __future__ import annotations

from functools import lru_cache

calls = []


@lru_cache(maxsize=3)          # 최대 3개만 기억. 초과하면 LRU 를 버린다.
def compute(x: int) -> int:
    calls.append(x)            # 캐시 miss 로 '진짜 계산'이 일어난 순간만 기록
    return x * x


if __name__ == "__main__":
    print("작업: lru_cache(maxsize=3) 에 값을 넣고, 넘칠 때 무엇이 방출되는지 관찰\n")

    for x in [1, 2, 3]:        # 캐시 채우기 (전부 miss = 진짜 계산)
        compute(x)
    print(f"  1,2,3 계산      → cache_info: {compute.cache_info()}")

    compute(1)                 # 1 을 '최근 사용'으로 갱신 → 이제 2 가 가장 오래됨
    print("  1 을 다시 조회   → hit. LRU 순서: 2(오래됨) < 3 < 1(최신)")

    compute(4)                 # 4 삽입 → 자리 없음 → 가장 오래된 2 를 방출
    print("  4 삽입          → 꽉 참. 가장 오래 안 쓴 '2' 를 방출\n")

    before = compute.cache_info().misses
    compute(2)                 # 2 는 방출됐으므로 다시 miss = 재계산
    after = compute.cache_info().misses
    result = "재계산됨(miss)" if after > before else "캐시에 남아있음(hit)"
    print(f"  2 를 다시 조회   → {result}  ← 방출됐다는 증거")

    before = compute.cache_info().misses
    compute(1)                 # 1 은 아직 살아있음 = hit
    after = compute.cache_info().misses
    result = "재계산됨(miss)" if after > before else "캐시에 남아있음(hit)"
    print(f"  1 을 다시 조회   → {result}  ← 자주 쓴 건 살아남음\n")

    print(f"  최종 cache_info : {compute.cache_info()}")
    print(f"  '진짜 계산' 실행된 순서 : {calls}")
    print("  → 1,2,3,4 채운 뒤 2 만 다시 계산됨. 방출된 건 딱 '가장 오래 안 쓴 2'.")
