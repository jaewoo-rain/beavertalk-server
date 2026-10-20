"""PM-DEC-890 — 비비 자산이 바바와 같은 id·slot 으로 실리고 스위치로 켜진다."""
from types import SimpleNamespace

from domains.learning.realtime import clip_expr, script_read


def test_bibi_expr_bank_matches_baba_ids():
    b = clip_expr.load_bank("Baba")[0]
    n = clip_expr.load_bank("Bibi")[0]
    assert set(b) == set(n) and len(n) == 138


def test_bibi_script_bank_matches_baba_ids():
    b = script_read.load_bank("Baba")[0]
    n = script_read.load_bank("Bibi")[0]
    assert set(b) == set(n) and len(n) == 52


def test_switch_enables_bibi_by_leda_voice():
    s = SimpleNamespace(CLIP_EXPR="Baba,Bibi", SCRIPT_READ="Baba,Bibi")
    assert clip_expr.enabled(s, call_type="expression", voice="Leda", locale="en") == "Bibi"
    assert script_read.enabled(s, call_type="chat", voice="Leda", locale="en") == "Bibi"
    assert clip_expr.enabled(SimpleNamespace(CLIP_EXPR="Baba"), call_type="expression", voice="Leda", locale="en") is None


def test_bibi_persona_and_voice():
    assert "Bibi" in clip_expr.PERSONA["Bibi"]
    assert clip_expr.CHAR_VOICE == {"Baba": "Fenrir", "Bibi": "Leda"}
