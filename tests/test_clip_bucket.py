"""PM-DEC-1044 — 표현학습 클립 차시를 버킷에서: 업로드만 하면 열리고, 필수 항목 클립이 manifest 에 없으면 Live.
PM-DEC-1052 — 클립은 통화 시작 전 EB_WAIT_S 만 기다리고 나머지는 통화 중에 받는다(못 받으면 그 차례만 TTS)."""
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


def _objects(items=("1", "2"), drop=(), drop_man=()):
    doc = {"lesson": LESSON, "no": 1, "items": [{"seq": s, "item_id": "9" + s, "surface": f"표현{s}", "en": f"e{s}"} for s in items]}
    man, objs = [], {}
    for s in items:
        for cid in (f"ask_{s}_1", f"model_{s}", f"quiz_{s}"):
            if cid in drop_man:
                continue
            man.append({"id": cid, "slot": "x", "text": cid, "item": f"표현{s}", "sec": 0.4, "face": "neutral"})
            if cid not in drop:
                objs[f"Baba_en/{LESSON}/{cid}.wav"] = _wav()
    objs[f"{LESSON}.json"] = json.dumps(doc).encode()
    objs[f"Baba_en/{LESSON}/manifest.json"] = json.dumps(man).encode()
    return objs


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for c in (ce._EB_LRU, ce._EB_MISS, ce._BANK_CACHE, ce._EB_FUT):
        c.clear()
    yield
    for c in (ce._EB_LRU, ce._EB_MISS, ce._BANK_CACHE, ce._EB_FUT):
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


def test_required_clip_missing_from_manifest_falls_back_to_live(monkeypatch):
    _serve(monkeypatch, _objects(drop_man=("model_2",)))
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) is None


def test_missing_wav_opens_and_that_turn_reads_with_tts(monkeypatch):
    """manifest 에 있는데 wav 를 못 받으면 차시는 열고, 그 클립 차례만 같은 목소리 TTS 로 읽는다."""
    import asyncio
    _serve(monkeypatch, _objects(drop=("model_2",)))
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) == "Baba"
    clips, _ = ce.load_bank("Baba", lesson=LESSON)
    assert clips["model_1"].pcm and not clips["model_2"].pcm
    said = []

    async def fake_tts(text, *, lang="en", voice="Fenrir"):
        said.append((text, lang, voice))
        return b"\x01\x00" * 4800

    monkeypatch.setattr(ce, "tts_pcm", fake_tts)
    sess = SimpleNamespace(char="Baba", tts_chars=0)
    got = asyncio.run(ce.ClipExprSession._fill_or_tts(sess, clips["model_2"]))
    assert got is not None and got.pcm and said == [("model_2", "en", "Fenrir")]
    assert asyncio.run(ce.ClipExprSession._fill_or_tts(sess, clips["model_1"])) is clips["model_1"]


def test_slow_bucket_starts_within_budget_and_fills_during_call(monkeypatch):
    """call 2874 — 189 클립 첫 받기 15.3초 동안 무음. 통화 시작은 EB_WAIT_S 만 기다린다."""
    import asyncio
    import threading
    import time
    objs = _objects(items=tuple(str(i) for i in range(1, 21)))
    gate = threading.Event()
    import core.storage as storage

    def slow(prefix, path):
        if path.endswith(".wav"):
            gate.wait(5)
        return objs.get(path)

    monkeypatch.setattr(storage, "download", slow)
    monkeypatch.setattr(ce, "EB_WAIT_S", 0.2)
    t0 = time.monotonic()
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) == "Baba"
    assert time.monotonic() - t0 < 1.5
    clips, items = ce.load_bank("Baba", lesson=LESSON)
    assert len(items) == 20 and not clips["ask_1_1"].pcm
    gate.set()
    assert asyncio.run(ce.ensure_pcm(clips["ask_1_1"])) is True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not all(c.pcm for c in clips.values()):
        time.sleep(0.05)
    assert all(c.pcm for c in clips.values()) and not ce._EB_FUT


def test_missing_lesson_falls_back_to_live(monkeypatch):
    _serve(monkeypatch, {})
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) is None


def test_repo_lesson_unchanged_and_does_not_touch_bucket(monkeypatch):
    asked = _serve(monkeypatch, {})
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson="L1-S01-1") == "Baba"
    assert asked == []


@pytest.mark.parametrize("heard,once", [
    ("다시 말해 주세요 다시 말해 주세요", "다시 말해 주세요"),
    ("잘 안 들렸어요. 잘 안 들렸어요.", "잘 안 들렸어요"),
    ("네 네 네", "네"),
    ("뭐 뭐라고요", "뭐 뭐라고요"),                  # 더듬기는 되풀이가 아니다
    ("천천히 말해 주세요", "천천히 말해 주세요"),
])
def test_collapse_repeats(heard, once):
    assert ce.collapse_repeats(heard) == once


def test_repeated_correct_answer_is_correct_not_close():
    """clipb 2866 퀴즈: 정답을 두 번 말하면 close 였다."""
    assert ce.rule_judge("다시 말해 주세요 다시 말해 주세요", ["다시 말해 주세요"], blank=False)[0] == "correct"
    assert ce.rule_judge("감사합니다 감사합니다", ["다시 말해 주세요"], blank=False)[0] is None   # 틀린 말은 접어도 틀림



def test_needed_clip_jumps_the_queue(monkeypatch):
    """인사 재생 중 첫 질문 클립은 줄(189개) 뒤에서 기다리지 않고 바로 받는다(기다림 0 · PM-DEC-1052 다음 판)."""
    import asyncio
    import threading
    objs = _objects(items=tuple(str(i) for i in range(1, 21)))
    hold = threading.Event()
    import core.storage as storage

    def slow(prefix, path):
        if path.endswith(".wav") and not path.endswith("ask_7_1.wav"):
            hold.wait(5)                       # 줄의 나머지는 막혀 있다
        return objs.get(path)

    monkeypatch.setattr(storage, "download", slow)
    monkeypatch.setattr(ce, "EB_WAIT_S", 0.0)
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) == "Baba"
    clips, _ = ce.load_bank("Baba", lesson=LESSON)
    try:
        assert asyncio.run(ce.ensure_pcm(clips["ask_7_1"])) is True
    finally:
        hold.set()


@pytest.mark.parametrize("heard,target,want", [
    ("몰라요", "몰라요", "correct"),
    ("잘 모르겠어요", "잘 모르겠어요.", "correct"),
    ("몰라요", "다시 말해 주세요", "dunno"),          # 정답이 아니면 모름 그대로
    ("잘 모르겠어요", "천천히 말해 주세요", "dunno"),
])
def test_dunno_phrase_as_target_is_judged_by_match(heard, target, want):
    """2889 — 정답이 «몰라요» 인 문항에서 «몰라요» 가 dunno 로 처리됐다."""
    assert ce.rule_judge(heard, [target], blank=False)[0] == want
