"""PM-DEC-1018 — storage 첫 초기화를 여러 작업 스레드가 동시에 불러도 모두 버킷을 본다(scriptsb call 2646 · 10줄 중 1줄)."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import core.storage as storage


def test_concurrent_first_init_all_see_bucket(monkeypatch):
    monkeypatch.setattr(storage, "_ready", False)
    monkeypatch.setattr(storage, "_bucket", None)
    calls = []
    lock = threading.Lock()

    def slow_init_once():
        with lock:
            calls.append(1)
        time.sleep(0.2)                       # 자격증명·클라이언트 만들기에 걸리는 시간
        storage._bucket = object()

    monkeypatch.setattr(storage, "_init_once", slow_init_once)

    def first_download():
        storage._init()
        return storage._bucket is not None

    with ThreadPoolExecutor(max_workers=10) as pool:
        seen = list(pool.map(lambda _: first_download(), range(10)))
    assert seen == [True] * 10
    assert len(calls) == 1                    # 초기화는 한 번만


def test_failed_init_is_not_retried_per_call(monkeypatch):
    monkeypatch.setattr(storage, "_ready", False)
    monkeypatch.setattr(storage, "_bucket", None)
    calls = []

    def boom():
        calls.append(1)
        raise RuntimeError("no creds")

    monkeypatch.setattr(storage, "_init_once", boom)
    for _ in range(3):
        try:
            storage._init()
        except RuntimeError:
            pass
    assert len(calls) == 1                    # 종전처럼 한 번 실패하면 비활성으로 남는다
