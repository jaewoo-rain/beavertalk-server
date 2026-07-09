"""GIL 배경 (c): 바이트코드 · 레퍼런스 카운팅 · GC 를 눈으로.

1) dis.dis: 파이썬 함수는 '바이트코드'로 컴파일되고 인터프리터가 한 명령씩 실행한다.
   이 실행을 스레드가 동시에 못 하게 막는 자물쇠가 GIL.
2) sys.getrefcount: CPython 은 객체가 몇 번 참조되는지 세어(refcount), 0 이 되면 즉시 해제.
   이 카운터를 여러 스레드가 동시에 ++/-- 하면 깨지므로, 이를 지키는 싼 방법이 GIL 이었다.
3) gc: 서로를 참조하는 순환(cycle)은 refcount 가 0 이 안 돼 못 지운다 → 순환 수집기(gc)가 처리.
"""

from __future__ import annotations

import dis
import gc
import sys


def add(a, b):
    return a + b


print("=== 1) dis.dis(add): 파이썬 → 바이트코드 ===")
dis.dis(add)

print()
print("=== 2) reference counting (sys.getrefcount) ===")
obj = ["beaver"]
# getrefcount 는 '인자로 넘기는 순간의 임시 참조' 1을 항상 더해 보여준다.
print("refcount 처음        :", sys.getrefcount(obj), "(임시참조 +1 포함)")
alias = obj
print("refcount alias=obj 뒤 :", sys.getrefcount(obj))
del alias
print("refcount del alias 뒤 :", sys.getrefcount(obj))

print()
print("=== 3) 순환참조는 refcount 만으론 못 지운다 → gc 필요 ===")
gc.collect()      # 시작 전 청소
gc.disable()      # 자동 수집을 잠깐 끄고 순환을 쌓아본다


def make_cycle() -> None:
    a: dict = {}
    b: dict = {}
    a["b"] = b
    b["a"] = a  # 서로를 참조 → 함수를 나가도 refcount 가 서로 1로 남음


for _ in range(1000):
    make_cycle()

collected = gc.collect()  # 순환 수집기가 이제 회수
gc.enable()
print("gc.collect() 가 회수한 순환 객체 수:", collected, "(refcount 로는 절대 0이 안 됐다)")
