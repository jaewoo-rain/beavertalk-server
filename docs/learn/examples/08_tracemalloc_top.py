"""08 (d) tracemalloc — 메모리를 '어느 줄'이 먹는지.

CPU 프로파일러가 '시간 먹는 함수'를 찾듯, tracemalloc 은 '메모리 먹는 줄'을 찾는다.
여기서는 세 가지 서로 다른 크기의 버퍼를 만들고,
할당 상위 라인(top allocations)을 줄 단위로 출력한다.

우리 통화 코드의 bytearray 누적 버퍼(_CallState 오디오)처럼,
"어디서 메모리가 쌓이나"를 코드 라인으로 지목하는 감을 잡는 예다.

실행: python 08_tracemalloc_top.py
"""

from __future__ import annotations

import tracemalloc


def make_big_list() -> list[int]:
    return [i for i in range(500_000)]          # 큰 int 리스트


def make_pcm_buffer() -> bytearray:
    buf = bytearray()
    for _ in range(2000):
        buf += b"\x00" * 640                     # 20ms PCM16/16k 청크 흉내
    return buf


def make_small_dicts() -> list[dict]:
    return [{"seq": i, "text": "x" * 8} for i in range(50_000)]


def main() -> None:
    tracemalloc.start()

    a = make_big_list()
    b = make_pcm_buffer()
    c = make_small_dicts()

    snap = tracemalloc.take_snapshot()
    stats = snap.statistics("lineno")  # 줄(line) 단위 집계, 큰 순

    print("메모리 할당 상위 5개 라인:")
    for stat in stats[:5]:
        frame = stat.traceback[0]
        # 파일명은 basename 만 짧게
        fname = frame.filename.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        print(f"  {stat.size/1024:8.1f} KiB  ({stat.count:>6} blocks)  {fname}:{frame.lineno}")

    cur, peak = tracemalloc.get_traced_memory()
    print()
    print(f"현재 추적 메모리: {cur/1024/1024:6.2f} MiB   피크: {peak/1024/1024:6.2f} MiB")
    tracemalloc.stop()

    # a,b,c 를 살려둬 GC 가 위 측정 전에 회수하지 않게 함
    print(f"(살아있는 객체: list={len(a)}, buf={len(b)}B, dicts={len(c)})")


if __name__ == "__main__":
    main()
