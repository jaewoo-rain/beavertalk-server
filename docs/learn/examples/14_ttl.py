"""14 (b) TTL(Time To Live) — 캐시에 유효기간을 준다.

캐시의 가장 큰 위험은 '낡은 데이터(stale)'다. 원본이 바뀌었는데 캐시가 옛날 값을
계속 돌려주면 버그다. 가장 단순하고 강력한 방어가 TTL: "이 키는 N초 뒤 알아서
사라진다". 만료되면 다음 읽기는 다시 miss → 원본에서 최신값을 다시 채운다.

  set(key, value, ex=N): 값을 넣으면서 N초 TTL 을 함께 지정(권장 최신 idiom)
  setex(key, N, value) : 위와 동일(구식). expire(key, N): 기존 키에 TTL 추가
  ttl(key)             : 남은 수명(초). -2=키 없음, -1=TTL 없음(영구)

fakeredis 는 TTL 을 실제로 구현하므로 만료를 그대로 관찰할 수 있다.
"""

from __future__ import annotations

import time

import fakeredis


def build_redis():
    # 실서버: return redis.Redis(host="localhost", port=6379, db=0)
    return fakeredis.FakeStrictRedis()


if __name__ == "__main__":
    r = build_redis()
    r.flushall()

    TTL_S = 2
    print(f"작업: set(ex={TTL_S}) 로 TTL {TTL_S}초 부여 후 시간에 따른 상태 관찰\n")

    r.set("level:1", "Level(1, 초급)", ex=TTL_S)   # 값 + 2초 수명

    def probe(label: str):
        val = r.get("level:1")
        ttl = r.ttl("level:1")
        state = "HIT " if val is not None else "MISS"
        shown = val.decode() if val is not None else "(사라짐)"
        print(f"  {label:>10} → {state}  ttl={ttl:>2}s  value={shown}")

    probe("0.0s")
    time.sleep(1.0)
    probe("1.0s")
    time.sleep(1.2)                                # 누적 2.2s > TTL 2s
    probe("2.2s")

    print("\n  → 만료 후엔 MISS. Cache-Aside 라면 여기서 다시 DB 를 쳐 최신값으로 재적재한다.")
    print("  (ttl=-2 는 '키 없음' = 완전히 증발했다는 뜻)")
