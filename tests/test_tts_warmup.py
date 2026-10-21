"""2차(2026-10-22) — 벙어리 첫 인사 TTS 대체 예열(warm_greeting_tts)."""
from domains.learning.realtime import call_session as cs


def test_warmup_skips_without_key_file(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cs._settings, "TTS_SA_KEY_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(cs, "_greeting_tts_sync", lambda *a: calls.append(a) or b"")
    cs.warm_greeting_tts()
    assert calls == []


def test_warmup_skips_when_switch_off(monkeypatch, tmp_path):
    key = tmp_path / "k.json"; key.write_text("{}")
    calls = []
    monkeypatch.setattr(cs._settings, "TTS_SA_KEY_FILE", str(key))
    monkeypatch.setattr(cs._settings, "GREETING_TTS_WARMUP", False)
    monkeypatch.setattr(cs, "_greeting_tts_sync", lambda *a: calls.append(a) or b"")
    cs.warm_greeting_tts()
    assert calls == []


def test_warmup_synthesizes_once(monkeypatch, tmp_path):
    key = tmp_path / "k.json"; key.write_text("{}")
    calls = []
    monkeypatch.setattr(cs._settings, "TTS_SA_KEY_FILE", str(key))
    monkeypatch.setattr(cs._settings, "GREETING_TTS_WARMUP", True)
    monkeypatch.setattr(cs, "_greeting_tts_sync", lambda text, voice: calls.append((text, voice)) or b"\x00" * 10)
    cs.warm_greeting_tts()
    assert len(calls) == 1 and calls[0][1] == cs.DEFAULT_VOICE


def test_warmup_failure_is_swallowed(monkeypatch, tmp_path):
    key = tmp_path / "k.json"; key.write_text("{}")
    monkeypatch.setattr(cs._settings, "TTS_SA_KEY_FILE", str(key))
    monkeypatch.setattr(cs._settings, "GREETING_TTS_WARMUP", True)

    def boom(*_a):
        raise RuntimeError("tts down")
    monkeypatch.setattr(cs, "_greeting_tts_sync", boom)
    cs.warm_greeting_tts()   # 예외가 새지 않는다
