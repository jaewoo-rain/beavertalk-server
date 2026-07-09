"""14 (a) Cache-Aside 패턴 — 읽기 시 캐시 먼저, 없으면 원본 조회 후 캐시에 채운다.

가장 흔한 캐시 패턴. 애플리케이션이 캐시를 '옆에 두고(aside)' 직접 관리한다:
  1) 캐시에서 찾는다(GET)
  2) 있으면(hit) 즉시 반환 — 원본(DB)을 아예 안 친다
  3) 없으면(miss) 느린 원본을 조회하고, 그 결과를 캐시에 SET 해 둔다

여기선 Redis 서버가 없으니 fakeredis(순수 파이썬 인메모리, redis-py 와 API 동일)로
돌린다. 실서버는 아래 build_redis() 의 주석대로 redis.Redis(host=...) 로 바꾸기만 하면
코드는 그대로다. '느린 DB'는 time.sleep 으로 흉내 낸다.
"""

from __future__ import annotations

import time

import fakeredis

# --- 실서버로 바꿀 때는 이 함수만 교체 ---------------------------------
# import redis
# def build_redis():
#     return redis.Redis(host="localhost", port=6379, db=0)
def build_redis():
    # fakeredis 는 redis.Redis 와 완전 호환 API. 프로세스 메모리에만 산다.
    return fakeredis.FakeStrictRedis()
# ---------------------------------------------------------------------

DB_LATENCY_S = 0.30   # 300ms: 원격 DB 왕복 + 무거운 조인 흉내

stats = {"hit": 0, "miss": 0}


def load_character_from_db(character_id: int) -> str:
    """'느린 원본'. 실제로는 SELECT ... JOIN voice ... 같은 DB 왕복이라 치자."""
    time.sleep(DB_LATENCY_S)
    return f"Character(id={character_id}, name=Beaver, voice=Warm)"


def get_character(r, character_id: int) -> str:
    key = f"character:{character_id}"

    cached = r.get(key)          # 1) 캐시 확인
    if cached is not None:       # 2) hit → 즉시 반환 (DB 안 침)
        stats["hit"] += 1
        return cached.decode()

    stats["miss"] += 1           # 3) miss → 느린 원본 조회
    value = load_character_from_db(character_id)
    r.set(key, value)            #    캐시에 채워 둔다(다음엔 hit)
    return value


if __name__ == "__main__":
    r = build_redis()
    r.flushall()

    print(f"작업: character:7 조회. 원본(DB) 지연 = {DB_LATENCY_S*1000:.0f}ms\n")

    t0 = time.perf_counter()
    get_character(r, 7)                       # 첫 호출: miss → 느림
    first = time.perf_counter() - t0

    t0 = time.perf_counter()
    get_character(r, 7)                       # 두번째: hit → 빠름
    second = time.perf_counter() - t0

    print(f"  1번째(miss, DB 감) : {first*1000:8.2f} ms")
    print(f"  2번째(hit, 캐시)   : {second*1000:8.2f} ms")
    print(f"  → {first/second:,.0f}배 빠름\n")

    # 같은 키를 여러 번(현실의 조회 트래픽 흉내)
    for _ in range(1000):
        get_character(r, 7)

    total = stats["hit"] + stats["miss"]
    ratio = stats["hit"] / total * 100
    print(f"  총 조회 {total}회 → hit {stats['hit']}, miss {stats['miss']}")
    print(f"  hit ratio = {ratio:.1f}%  (miss 는 최초 1번뿐, 나머지는 전부 캐시)")
