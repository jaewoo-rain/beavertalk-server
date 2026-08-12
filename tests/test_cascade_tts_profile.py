"""TTS **성질 표** — 엔진 이름으로 분기하지 않는다는 성질을 여기 한 곳에서 지킨다.

같은 사고가 **두 번** 났다(2026-08-11). 둘 다 "나중에 붙은 엔진이 앞 엔진의 전제를 물려받았다":
  ① 묶음 크기 — `"Gemini 면 400, 아니면 160"` → OpenAI 가 Chirp 의 160 을 탔다.
     Chirp 은 TTFB 165~212ms 라 요청이 많아도 견디는데 OpenAI 는 545~953ms 다.
  ② 첫 문장 단독 송출 — 조건이 `_gemini_realtime()` → OpenAI 가 Chirp 규칙을 탔다.
     실측: OpenAI 첫 배치 오디오 **800·1000·1450ms** vs 선행버퍼 **1500ms** → 재생이 버퍼보다
     먼저 바닥나 **끊긴다**. Gemini 는 첫 배치가 6440·8240ms 라 멀쩡했다.

⇒ 그래서 이 파일은 값이 아니라 **"새 엔진이 표에서 빠지면 먼저 실패한다"**를 고정한다.
  세 번째는 없어야 한다.
"""

import dataclasses

import pytest

import domains.learning.realtime.cascade_session as cs
from core.config import settings


# ── 재발 방지: 선택지 × 성질 전수 ──────────────────────────────────────────
def test_every_tts_choice_has_a_profile():
    """⛔ **여기가 이번 사고의 방지선이다.** 선택지에 엔진을 넣고 표에 안 넣으면 여기서 걸린다."""
    missing = [c for c in cs._TTS_CHOICES if c not in cs._TTS_PROFILES]
    assert not missing, f"성질 표에 없는 선택지: {missing} — 표에 한 줄 넣어라"


def test_every_profile_field_points_at_a_real_setting():
    """설정 **이름**을 담으므로 오타가 조용히 산다 — 전 필드가 실재하는지 본다."""
    for choice in cs._TTS_CHOICES:
        profile = cs._TTS_PROFILES[choice]
        for field in dataclasses.fields(profile):
            if not field.name.endswith("_setting"):
                continue
            name = getattr(profile, field.name)
            if name is None:
                continue                       # 선행버퍼 없음 = 서버 공통값(정상)
            assert hasattr(settings, name), f"{choice}.{field.name} → 없는 설정 {name!r}"


def test_every_profile_yields_a_vendor_name():
    """원가는 벤더 이름으로 갈린다 — 비면 통화 하나가 장부에서 사라진다."""
    for choice in cs._TTS_CHOICES:
        assert cs._TTS_PROFILES[choice].vendor(), choice


def test_unknown_engine_falls_back_loudly_and_conservatively(caplog):
    """모르는 엔진은 기본 성질로 가되 **조용히 가지 않는다**.

    ⛔ 그 기본이 **Chirp 이면 안 된다** — 첫 문장 단독 송출을 물려줘 이번 사고를 그대로
      재현한다. 왕복을 모르면 묶는 쪽이 안전하다.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        profile = cs._profile_for("some-new-tts")
    assert any("성질 미등록" in r.getMessage() for r in caplog.records), caplog.text
    assert profile.solo_first_sentence is False, "모르는 엔진에 단독 송출을 물려줬다"
    assert profile.takes_style is False
    assert cs._batch_chars_for("some-new-tts") == settings.CASCADE_TTS_BATCH_CHARS


def test_empty_engine_is_the_server_default_not_the_fallback():
    """빈 값은 '모르는 엔진'이 아니다 — 서버 기본(Chirp)이고 그 성질을 그대로 받는다."""
    assert cs._profile_for("") is cs._TTS_PROFILES[cs._CHIRP_CHOICE]


# ── 성질 ①: 첫 문장 단독 송출 ──────────────────────────────────────────────
def test_short_roundtrip_engine_keeps_solo_first_sentence():
    """⛔ Chirp 의 첫 문장 단독은 **유지된다** — 왕복이 165~212ms 라 그게 이득이다
    (첫 소리가 그만큼 빨라지고, 짧은 첫 배치라도 버퍼가 안 마른다)."""
    assert cs._TTS_PROFILES[cs._CHIRP_CHOICE].solo_first_sentence is True


@pytest.mark.parametrize("choice", [cs.tts.GEMINI_ENGINE, cs._GEMINI_BATCH_CHOICE,
                                    cs._OPENAI_TTS_CHOICE])
def test_slow_roundtrip_engines_do_not_send_the_first_sentence_alone(choice):
    """왕복이 긴 엔진은 묶어서 낸다 — 안 그러면 첫 배치가 버퍼보다 짧아 **끊긴다**."""
    assert cs._TTS_PROFILES[choice].solo_first_sentence is False


def test_solo_first_sentence_is_read_from_the_profile_not_the_engine_name():
    """⭐ **성질로 물어야** 새 엔진이 남의 규칙을 물려받지 않는다(이름 비교 금지).

    ⚠ 소스 문자열로 보면 **주석에 걸린다**(사고 경위를 주석에 적어 뒀다) — 코드만 본다.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cs.CascadeSession._run_reply)))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "solo_first_sentence" in names, "성질을 안 묻는다"
    assert "_gemini_realtime" not in names, "엔진 이름으로 되돌아갔다"


# ── 성질 ②: 묶음 크기 ─────────────────────────────────────────────────────
def test_batch_size_grows_with_the_vendor_roundtrip():
    """요청당 고정 오버헤드가 큰 엔진일수록 크게 묶는다(실측 TTFB 순서와 같아야 한다)."""
    assert cs._batch_chars_for(cs._OPENAI_TTS_CHOICE) > cs._batch_chars_for(cs._CHIRP_CHOICE)
    assert (cs._batch_chars_for(cs._OPENAI_TTS_CHOICE)
            >= cs._batch_chars_for(cs.tts.GEMINI_ENGINE) * 0.5)
    for choice in cs._TTS_CHOICES:
        assert cs._batch_chars_for(choice) > 0


# ── 성질 ③: 스타일을 받는 엔진 ────────────────────────────────────────────
def test_style_capability_matches_the_vendor_api():
    """Chirp 만 스타일 프롬프트를 못 받는다 — 이게 틀리면 감정 로그가 거짓말을 한다."""
    assert cs._TTS_PROFILES[cs._CHIRP_CHOICE].takes_style is False
    for choice in (cs.tts.GEMINI_ENGINE, cs._GEMINI_BATCH_CHOICE, cs._OPENAI_TTS_CHOICE):
        assert cs._TTS_PROFILES[choice].takes_style is True


# ── 성질 ④: 쓸 수 있나(키) ────────────────────────────────────────────────
def test_key_check_is_declared_per_engine():
    """⛔ 키 검사가 `picked == "openai-tts"` 로 박혀 있었다 — 키가 필요한 새 엔진이 붙으면
    **검사 없이 통과**한다. 표가 판정하면 그 구멍이 안 생긴다."""
    import ast
    import inspect
    import textwrap

    for choice in cs._TTS_CHOICES:
        assert callable(cs._TTS_PROFILES[choice].is_configured), choice
    tree = ast.parse(textwrap.dedent(inspect.getsource(cs.CascadeSession._apply_tts_choice)))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "is_configured" in attrs, "키 검사가 표를 안 본다"


def test_unknown_engine_cannot_be_selected():
    """모르는 엔진은 **거절**이 안전하다(성질을 모르는 채로 소리를 내면 안 된다)."""
    assert cs._TTS_FALLBACK_PROFILE.is_configured() is False


# ── 성질 ⑤: core.tts 로 넘길 엔진 이름 ────────────────────────────────────
def test_google_engine_is_explicit_for_every_google_backed_choice():
    """⚠ 여기가 None 이면 `core.tts` 가 **서버 기본값**으로 되돌아간다 — 고른 것과 다른
    소리가 나면서 A/B 가 통째로 거짓말이 된다."""
    assert cs._TTS_PROFILES[cs._CHIRP_CHOICE].google_engine == cs._CHIRP_CHOICE
    assert cs._TTS_PROFILES[cs.tts.GEMINI_ENGINE].google_engine == cs.tts.GEMINI_ENGINE
    # 배치도 소리는 Gemini 가 낸다 — 모으는 방식만 다르다
    assert cs._TTS_PROFILES[cs._GEMINI_BATCH_CHOICE].google_engine == cs.tts.GEMINI_ENGINE
    # 구글을 안 타는 엔진만 None
    assert cs._TTS_PROFILES[cs._OPENAI_TTS_CHOICE].google_engine is None


# ── 성질 ⑥: 선행버퍼 ──────────────────────────────────────────────────────
def test_slow_engines_declare_a_lead_buffer():
    """왕복이 긴 엔진은 선행버퍼가 있어야 한다 — 없으면 첫 조각에서 바로 언더런이 난다."""
    for choice in (cs.tts.GEMINI_ENGINE, cs._GEMINI_BATCH_CHOICE, cs._OPENAI_TTS_CHOICE):
        assert cs._TTS_PROFILES[choice].lead_setting is not None, choice
