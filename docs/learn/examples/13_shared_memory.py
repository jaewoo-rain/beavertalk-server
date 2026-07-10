"""13-Multiprocessing (c): 공유메모리로 '복사 없이' 큰 데이터를 나눠 쓴다.

(b)에서 큰 데이터를 인자로 넘기면 피클/복사가 폭발했다. multiprocessing.shared_memory
는 여러 프로세스가 '같은 물리 메모리 블록'을 매핑해 복사 없이 본다. 큰 배열을 자식마다
복사하는 대신, 이름(name)만 넘겨 각자 붙인다.

여기선 부모가 shared_memory 에 정수 배열을 만들고, 자식이 그 이름으로 붙어 값을 읽고
쓰는 것을 확인한다(복사 아님 — 자식이 쓴 값이 부모에서 그대로 보인다).

실행:
    uv run python 13_shared_memory.py
"""

from __future__ import annotations

from multiprocessing import Process
from multiprocessing import shared_memory


def child(name: str, n: int) -> None:
    """이름으로 기존 공유블록에 '붙어서'(create=False) 앞부분을 두 배로 덮어쓴다."""
    shm = shared_memory.SharedMemory(name=name)  # 새로 만들지 않고 기존 블록에 attach
    try:
        buf = shm.buf  # memoryview — 복사 아님, 같은 물리 메모리
        for i in range(n):
            buf[i] = (buf[i] * 2) % 256
    finally:
        shm.close()  # 이 프로세스의 매핑만 닫음(unlink 아님)


def main() -> None:
    n = 10
    shm = shared_memory.SharedMemory(create=True, size=n)  # n바이트 공유 블록
    try:
        for i in range(n):
            shm.buf[i] = i + 1  # 부모가 1..10 을 씀
        print(f"부모가 쓴 값       : {list(shm.buf[:n])}")

        p = Process(target=child, args=(shm.name, n))  # 데이터가 아니라 '이름'만 전달
        p.start()
        p.join()

        # 자식이 같은 메모리를 덮어썼다 → 복사였다면 부모엔 안 보였을 것
        print(f"자식이 덮어쓴 뒤 값 : {list(shm.buf[:n])}")
        print(f"공유블록 이름(name) : {shm.name}")
        print("=> 자식이 쓴 값이 부모에서 그대로 보인다 = 복사가 아니라 같은 메모리 공유")
    finally:
        shm.close()
        shm.unlink()  # 생성자(부모)가 OS 에서 블록을 실제로 해제


if __name__ == "__main__":
    main()
