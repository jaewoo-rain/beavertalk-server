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


def test_only_lessons_with_matching_beaver_recordings_open():
    on = SimpleNamespace(SCRIPT_READ="Baba,Bibi", SCRIPT_READ_CHAT_LESSON="")
    assert sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en", lesson="A2-T11-1") == "Baba"
    opened = [p.stem for p in LESSONS
              if sr.enabled(on, call_type="freetalk", voice="Fenrir", locale="en", lesson=p.stem)]
    assert opened == ["A2-T11-1"]   # 나머지 490 은 비버 줄 녹음(sb_N)이 아직 없다 — 열면 A2-T11-1 소리가 나간다
