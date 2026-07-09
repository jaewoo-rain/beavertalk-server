"""14 (d) 쓰기 전략 3종 — Write-Through vs Write-Back vs Cache-Aside(write).

읽기는 Cache-Aside 하나로 대개 충분하지만, '쓸 때 캐시를 어떻게 다루나'는 갈린다.

  Cache-Aside(무효화) : DB 에 쓰고, 캐시는 그냥 지운다(delete). 다음 읽기가 재적재.
  Write-Through       : 쓸 때 캐시 + DB 를 '동시에' 갱신. 항상 일관, 쓰기는 DB 만큼 느림.
  Write-Back          : 캐시만 먼저 갱신하고 즉시 반환. DB 는 나중에 몰아서 flush.
                        → 쓰기 폭발적으로 빠름. 대신 flush 전 크래시 = 데이터 유실.

여기선 캐시=fakeredis, DB=지연 있는 dict 로 흉내 내고 세 전략의 쓰기 지연과
일관성/유실을 실측한다.
"""

from __future__ import annotations

import time

import fakeredis

DB_WRITE_S = 0.05     # 50ms: DB write 왕복 흉내

db: dict[str, str] = {}          # '느린 원본'
dirty: dict[str, str] = {}       # write-back 이 아직 DB 에 못 내린 값들


def db_write(key: str, value: str):
    time.sleep(DB_WRITE_S)
    db[key] = value


def build_redis():
    # 실서버: return redis.Redis(host="localhost", port=6379, db=0)
    return fakeredis.FakeStrictRedis()


# --- 세 가지 쓰기 -----------------------------------------------------
def write_through(r, key, value):
    r.set(key, value)            # 캐시 갱신
    db_write(key, value)         # + DB 도 즉시 (둘 다 끝나야 반환)


def write_back(r, key, value):
    r.set(key, value)            # 캐시만 갱신하고
    dirty[key] = value           # DB 로 내릴 목록에 적어두고 즉시 반환(느린 DB 안 기다림)


def flush_dirty():               # 나중에(주기적으로/배치로) 몰아서 DB 반영
    for k, v in dirty.items():
        db_write(k, v)
    n = len(dirty)
    dirty.clear()
    return n
# ---------------------------------------------------------------------


if __name__ == "__main__":
    r = build_redis()
    r.flushall()
    print(f"작업: 같은 키에 10번 쓰기. DB write 지연 = {DB_WRITE_S*1000:.0f}ms\n")

    # Write-Through: 매 쓰기가 DB 를 기다린다
    t0 = time.perf_counter()
    for i in range(10):
        write_through(r, "member:1:name", f"Beaver-{i}")
    wt = time.perf_counter() - t0
    print(f"  Write-Through : {wt*1000:8.1f} ms  (매 쓰기가 DB 대기, 항상 일관)")
    print(f"                  캐시={r.get('member:1:name').decode()}  DB={db['member:1:name']}  ← 일치")

    # Write-Back: 캐시만 갱신, DB 는 마지막에 한 번 flush
    r.flushall(); db.clear(); dirty.clear()
    t0 = time.perf_counter()
    for i in range(10):
        write_back(r, "member:1:name", f"Beaver-{i}")
    wb = time.perf_counter() - t0
    print(f"\n  Write-Back    : {wb*1000:8.1f} ms  (DB 안 기다림 → 폭발적으로 빠름)")
    print(f"                  flush 전: 캐시={r.get('member:1:name').decode()}  DB={db.get('member:1:name')}  ← DB 아직 낡음/없음")
    n = flush_dirty()
    print(f"                  flush({n}건) 후: DB={db['member:1:name']}  ← 이제 반영. 하지만 flush 전 크래시였다면 유실")

    print(f"\n  → Write-Back 가 Write-Through 보다 약 {wt/wb:,.0f}배 빠른 쓰기. 대가는 유실 위험.")
