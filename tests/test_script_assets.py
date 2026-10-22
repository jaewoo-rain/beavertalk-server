"""PM-DEC-1012 — 대본 읽기 대본 491차시 자산: 로더가 전부 읽고 · 비버 줄 녹음이 맞는 차시만 대본 읽기가 열린다."""
from __future__ import annotations

import json
from types import SimpleNamespace

import domains.learning.realtime.script_read as sr

LESSONS = sorted(p for p in sr.ROOT.glob("*.json"))


def test_491_lessons_present_and_well_formed():
    assert len(LESSONS) == 491
    total = 0
    for p in LESSONS:
        d = json.loads(p.read_text(encoding="utf-8"))
        assert d["lesson"] == p.stem and isinstance(d["no"], int)
        assert [x["n"] for x in d["lines"]] == list(range(1, len(d["lines"]) + 1)), p.stem
        for x in d["lines"]:
            for k in ("beaver", "beaver_en", "beaver_roman", "learner", "en", "roman"):
                assert isinstance(x.get(k), str) and x[k].strip(), (p.stem, x["n"], k)
        total += len(d["lines"])
    assert total == 4911          # 새 490차시 × 10줄 + 녹음과 맞춘 기존 A2-T11-1 11줄
    assert len({json.loads(p.read_text(encoding="utf-8"))["no"] for p in LESSONS}) == 491


def test_loader_reads_every_lesson():
    for p in LESSONS:
        clips, items, by_n = sr.load_bank("Baba", p.stem)
        assert items and len(items) == len(by_n), p.stem
        assert all(it.surface and it.en for it in items), p.stem


import io
import wave

import pytest


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    for c in (sr._SB_LRU, sr._SB_MISS, sr._BANK, sr._CLIPS_MATCH):
        c.clear()
    import core.storage as storage
    monkeypatch.setattr(storage, "download", lambda bucket, path: None)   # 기본: 버킷에 녹음 없음
    monkeypatch.setattr(sr.clip_expr, "_tts_sync", lambda text, lang, voice: None)   # 기본: TTS 없음(외부 호출 안 함)
    yield
    for c in (sr._SB_LRU, sr._SB_MISS, sr._BANK, sr._CLIPS_MATCH):
        c.clear()


def _wav(sec=0.5):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(bytes([1, 0]) * int(24000 * sec))
    return buf.getvalue()


def test_lesson_opens_with_bucket_recordings(monkeypatch):
    """버킷에 그 차시 비버 줄 녹음이 다 있으면 열리고, 그 녹음(해시 이름)으로 sb_N 이 채워진다 · 저장소 A2-T11-1 녹음은 섞이지 않는다."""
    import core.storage as storage
    code = "L1-S01-1"
    lines = json.loads((sr.ROOT / f"{code}.json").read_text(encoding="utf-8"))["lines"]
    want = {f"Baba/{sr.sb_hash(x['beaver'])}.wav" for x in lines}
    asked = []

    def fake(bucket, path):
        asked.append((bucket, path))
        return _wav() if bucket == sr.SB_PREFIX and path in want else None

    monkeypatch.setattr(storage, "download", fake)
    on = SimpleNamespace(SCRIPT_READ="Baba", SCRIPT_READ_CHAT_LESSON="")
    assert sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en", lesson=code) == "Baba"
    clips, items, by_n = sr.load_bank("Baba", code)
    assert all(clips[f"sb_{x['n']}"].text == x["beaver"] for x in lines)
    assert len(asked) == len(lines)                      # 차시 시작 때 한 번 · 두 번째(load_bank)는 캐시
    assert "sb_11" not in clips                          # A2-T11-1 녹음(11줄)이 남지 않는다


def test_partial_bucket_recordings_do_not_open(monkeypatch):
    import core.storage as storage
    code = "L1-S01-1"
    first = json.loads((sr.ROOT / f"{code}.json").read_text(encoding="utf-8"))["lines"][0]["beaver"]
    monkeypatch.setattr(storage, "download", lambda b, p: _wav() if p.endswith(sr.sb_hash(first) + ".wav") else None)
    on = SimpleNamespace(SCRIPT_READ="Baba", SCRIPT_READ_CHAT_LESSON="")
    assert sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en", lesson=code) is None


def test_sb_hash_matches_call_flow_rule():
    import hashlib
    assert sr.sb_hash("  안녕하세요. ") == hashlib.sha1("안녕하세요.".encode("utf-8")).hexdigest()[:12]


def test_only_lessons_with_matching_beaver_recordings_open():
    on = SimpleNamespace(SCRIPT_READ="Baba,Bibi", SCRIPT_READ_CHAT_LESSON="")
    assert sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en", lesson="A2-T11-1") == "Baba"
    opened = [p.stem for p in LESSONS
              if sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en", lesson=p.stem)]
    assert opened == ["A2-T11-1"]   # 나머지 490 은 비버 줄 녹음(sb_N)이 아직 없다 — 열면 A2-T11-1 소리가 나간다


def _one_missing(monkeypatch, code="L1-S01-1"):
    import core.storage as storage
    lines = json.loads((sr.ROOT / f"{code}.json").read_text(encoding="utf-8"))["lines"]
    gone = lines[-1]
    monkeypatch.setattr(storage, "download",
                        lambda b, p: None if p.endswith(sr.sb_hash(gone["beaver"]) + ".wav") else _wav())
    return code, lines, gone


def test_missing_recording_line_falls_back_to_tts(monkeypatch):
    """PM-DEC-1018 — 녹음이 끝내 안 된 줄은 그 줄만 TTS(캐릭터 목소리)로 채우고 차시를 연다."""
    code, lines, gone = _one_missing(monkeypatch)
    said = []

    def fake_tts(text, lang, voice):
        said.append((text, lang, voice))
        return _wav(0.8)

    monkeypatch.setattr(sr.clip_expr, "_tts_sync", fake_tts)
    on = SimpleNamespace(SCRIPT_READ="Baba", SCRIPT_READ_CHAT_LESSON="")
    assert sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en", lesson=code) == "Baba"
    assert said == [(gone["beaver"].strip(), "ko", "Fenrir")]
    clips, _, _ = sr.load_bank("Baba", code)
    c = clips[f"sb_{gone['n']}"]
    assert c.text == gone["beaver"] and abs(c.sec - 0.8) < 0.01      # RIFF 머리를 떼고 PCM 길이로
    assert all(f"sb_{x['n']}" in clips for x in lines)


def test_missing_recording_without_tts_keeps_lesson_closed(monkeypatch):
    code, _, _ = _one_missing(monkeypatch)
    on = SimpleNamespace(SCRIPT_READ="Baba", SCRIPT_READ_CHAT_LESSON="")
    assert sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en", lesson=code) is None


def test_too_many_missing_lines_do_not_use_tts(monkeypatch):
    import core.storage as storage
    code = "L1-S01-1"
    lines = json.loads((sr.ROOT / f"{code}.json").read_text(encoding="utf-8"))["lines"]
    gone = {sr.sb_hash(x["beaver"]) for x in lines[-(sr.SB_TTS_MAX_MISSING + 1):]}
    monkeypatch.setattr(storage, "download", lambda b, p: None if p.split("/")[-1][:-4] in gone else _wav())
    said = []
    monkeypatch.setattr(sr.clip_expr, "_tts_sync", lambda t, l, v: said.append(t) or _wav())
    on = SimpleNamespace(SCRIPT_READ="Baba", SCRIPT_READ_CHAT_LESSON="")
    assert sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en", lesson=code) is None
    assert said == []
