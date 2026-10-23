"""PM-DEC-1044 — 표현학습 클립 차시를 버킷에서: 업로드만 하면 열리고, 하나라도 없으면 Live."""
from __future__ import annotations

import io
import json
import wave
from types import SimpleNamespace

import pytest

import domains.learning.realtime.clip_expr as ce

LESSON = "L9-T01-1"
ON = SimpleNamespace(CLIP_EXPR="Baba")


def _wav(sec=0.4):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(bytes([1, 0]) * int(24000 * sec))
    return buf.getvalue()


def _objects(items=("1", "2"), drop=()):
    doc = {"lesson": LESSON, "no": 1, "items": [{"seq": s, "item_id": "9" + s, "surface": f"표현{s}", "en": f"e{s}"} for s in items]}
    man, objs = [], {}
    for s in items:
        for cid in (f"ask_{s}_1", f"model_{s}", f"quiz_{s}"):
            man.append({"id": cid, "slot": "x", "text": cid, "item": f"표현{s}", "sec": 0.4, "face": "neutral"})
            if cid not in drop:
                objs[f"Baba_en/{LESSON}/{cid}.wav"] = _wav()
    objs[f"{LESSON}.json"] = json.dumps(doc).encode()
    objs[f"Baba_en/{LESSON}/manifest.json"] = json.dumps(man).encode()
    return objs


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for c in (ce._EB_LRU, ce._EB_MISS, ce._BANK_CACHE):
        c.clear()
    yield
    for c in (ce._EB_LRU, ce._EB_MISS, ce._BANK_CACHE):
        c.clear()


def _serve(monkeypatch, objs):
    import core.storage as storage
    asked = []

    def fake(prefix, path):
        asked.append((prefix, path))
        return objs.get(path) if prefix == ce.EB_PREFIX else None

    monkeypatch.setattr(storage, "download", fake)
    return asked


def test_uploaded_lesson_opens_and_bank_merges_generic_clips(monkeypatch):
    asked = _serve(monkeypatch, _objects())
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) == "Baba"
    clips, items = ce.load_bank("Baba", lesson=LESSON)
    assert [i.seq for i in items] == ["1", "2"]
    assert {"ask_1_1", "model_1", "quiz_2"} <= set(clips)
    assert any(k.startswith("g_") for k in clips)          # 공통 클립은 저장소 것
    assert not any(k.startswith("native_") for k in clips) # 저장소 L1-S01-1 항목 클립은 안 섞인다
    assert all(p == ce.EB_PREFIX for p, _ in asked)


def test_missing_clip_falls_back_to_live(monkeypatch):
    _serve(monkeypatch, _objects(drop=("model_2",)))
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) is None


def test_missing_lesson_falls_back_to_live(monkeypatch):
    _serve(monkeypatch, {})
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) is None


def test_repo_lesson_unchanged_and_does_not_touch_bucket(monkeypatch):
    asked = _serve(monkeypatch, {})
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson="L1-S01-1") == "Baba"
    assert asked == []
