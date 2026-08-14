"""캐스케이드가 **조립기에 인자를 안 넘겨** 틀린 내용을 주입하던 두 자리(2026-08-15).

⭐ 둘 다 결손이 아니라 **틀린 값의 주입**이다 — 안 넘기면 조립기의 기본값이 들어가고,
  그 기본값이 사용자에게 그대로 나간다. 값은 **이미 손에 있었다.**

## ① lang_band — 전 회원이 '초급' 언어정책을 받고 있었다
    persona_prompt.py  lang_band: str = "beginner"   ← 안 넘기면 이것
    persona_prompt.py  _LANG_POLICY.get(lang_band, _LANG_POLICY["beginner"])
    normalcall_service.py:296  "lang_band": mastery_repository.band_of(level_no, language)
⇒ `load_call_setup` 이 담아 주고 Live 는 넘기는데(`call_session.py:1088`) 캐스케이드만 빠졌다.
⇒ 왕초보도 고급자도 **전원 beginner 정책**. 이건 "기능이 없다"가 아니라 **틀린 걸 준다**이다.

## ② seed_opening — 다국어 학습자에게 "한국어 공부할래?"
    persona_prompt.py  def seed_opening(target_language: str = "한국어")
⇒ 캐스케이드는 인자를 안 넘겨 **항상 "한국어"** 로 첫인사했다(Live 는 넘긴다).

## ⛔ Live 무영향의 증명
조립기는 **두 엔진이 같이 쓰는 하나**다. 조립기를 안 건드리고 **호출부 인자만** 늘렸으므로
`tests/test_persona_prompt.py` 의 바이트 동일 스냅샷이 그대로 통과한다 —
**그 통과가 곧 Live 무영향의 자동 증명**이다. 깨지면 조립기를 건드린 것이다.
"""

import inspect
import textwrap

import domains.learning.realtime.cascade_session as cs


def _source(fn) -> str:
    return textwrap.dedent(inspect.getsource(fn))


# ── ① lang_band ────────────────────────────────────────────────────────────
def test_the_language_band_is_passed_through():
    """⭐⭐ 안 넘기면 **전원 초급 정책**이다 — 값이 있는데 안 쓰는 것이 제일 나쁘다."""
    src = _source(cs.CascadeSession._system_instruction)
    assert "lang_band=" in src, "조립기에 lang_band 를 안 넘긴다(전원 beginner 정책이 된다)"
    assert 'setup.get("lang_band"' in src, "설정에서 읽지 않고 상수를 넣었다"


def test_the_band_falls_back_to_beginner_when_absent():
    """⚠ R5: 설정에 없으면(데모·구버전) 예전과 같은 값으로 간다 — 통화가 죽으면 안 된다."""
    src = _source(cs.CascadeSession._system_instruction)
    assert '"lang_band", "beginner"' in src, "폴백이 없다 — 값이 없으면 KeyError 로 통화가 죽는다"


def test_the_band_matches_what_live_passes():
    """⛔ **두 엔진이 같은 값을 넘긴다.** 갈리면 같은 회원이 엔진에 따라 다른 정책을 받는다."""
    import domains.learning.realtime.call_session as live

    live_src = inspect.getsource(live)
    assert 'lang_band=setup.get("lang_band", "beginner")' in live_src, (
        "Live 쪽 표현이 바뀌었다 — 두 엔진이 같은 값을 넘기는지 다시 확인해라"
    )


# ── ② seed_opening ─────────────────────────────────────────────────────────
def test_the_greeting_uses_the_learner_target_language():
    """⭐ 인자를 안 넘기면 **항상 "한국어"** 다 — 다른 언어를 배우는 학습자에게 틀린 첫인사."""
    src = _source(cs.CascadeSession.run)
    assert "seed_opening(self._target_label)" in src, (
        "첫인사가 배우는 언어를 안 쓴다 — 전원에게 '한국어 공부할래?' 가 나간다"
    )


def test_the_greeting_label_is_the_one_the_prompt_uses():
    """⛔ 첫인사와 지시문이 **같은 라벨**을 써야 한다 — 갈리면 인사와 본문이 다른 언어를 말한다."""
    src = _source(cs.CascadeSession._system_instruction)
    assert "target_language=self._target_label" in src, src


# ── ⛔ 조립기는 안 건드렸다 ─────────────────────────────────────────────────
def test_the_builder_signature_is_untouched():
    """⛔ 호출부 인자만 늘렸다 — 조립기를 건드리면 **Live 출력 바이트가 바뀐다.**

    ⚠ 진짜 증명은 `tests/test_persona_prompt.py` 의 바이트 동일 스냅샷이다. 여기서는
      "그 두 인자가 원래부터 있던 자리"라는 것만 확인한다(새로 만든 통로가 아니다).
    """
    from core.persona_prompt import build_system_instruction, seed_opening

    params = inspect.signature(build_system_instruction).parameters
    assert params["lang_band"].default == "beginner"
    assert inspect.signature(seed_opening).parameters["target_language"].default == "한국어"
