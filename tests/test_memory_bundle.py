"""기억 오프라인 회귀 묶음 — 7~14차 실제 전사 · 실제 추출기 원 응답(fixtures/memory_bundle/extract) 재생 → call flow 체크포인트 14개.
command center 2026-10-21: 하나라도 깨지면 m3ops 리비전 금지. 채점 기준은 bundle.json(call flow 77_공유_기억오프라인회귀기준)."""
import pytest

import memory_bundle_lib as lib


@pytest.fixture(scope="module")
def results():
    return lib.run_all()


def test_bundle_has_all_extractions(results):
    missing = [(a, r, c) for a, r, c, bad in results if bad == ["추출 캐시 없음"]]
    assert not missing, f"추출 캐시 없는 체크포인트: {missing}"


def test_all_checkpoints_pass(results):
    failed = [(a, r, c, bad) for a, r, c, bad in results if bad]
    total = len(results)
    assert not failed, f"묶음 {total - len(failed)}/{total} — 실패: " + " | ".join(f"{a} {r}({c}): {bad}" for a, r, c, bad in failed)
