"""폴백은 **시끄러워야 한다** — 조용히 기본값으로 도는 게 제일 나쁘다(2026-08-13).

오늘 우리가 당한 사고가 전부 **조용한 폴백**이었다: 자막 미전송 · `beaver_preparing` 미전송 ·
snake_case 무시 · 힌트 로그 부재. 전부 "동작은 하는데 아무도 모른다"였다.

여기서 막는 것은 두 자리다.

## ① 언어 — 지금 **우연히 맞다**
`CASCADE_TTS_LANGUAGE=en` 이 배포 env 에 박혀 있고, DB locale 이 없으면 그 값으로 통화한다.
지금 사용자가 영어권이라 **우연히 맞을** 뿐이고, 그 우연이 깨지는 날 학습자는 자기 모국어가
아닌 언어로 설명을 듣는다 — **에러도 경고도 없이**.
⚠ env 를 비우지는 않는다(R5 폴백이 필요하다). 대신 **발동하면 반드시 보인다**.

## ② 음색 — `character.voice_id` 가 NULL 이면 **그 캐릭터 목소리가 아니다**
`_tts_voice()` 는 DB 음색이 없을 때 env(`CASCADE_TTS_VOICE=Sulafat`)로 떨어진다. 조사에서
"env 가 안 먹는다"로 보였지만 실제로는 **조건부로 먹는다** — 조용해서 안 보였을 뿐이다.

여기서 고정하는 성질:
  ① DB 에서 왔으면 **INFO**(정상 운영)
  ② env 기본값으로 떨어지면 **WARNING**
  ③ 실험용 덮어쓰기(`*_OVERRIDE`)가 켜져 있어도 **WARNING**(의도된 운영 상태가 아니다)
  ④ 음색 폴백은 **통화당 한 번만** 경고한다(구간마다 부르는 자리다 — 도배 금지)
  ⑤ 캐릭터 로그에 **출처**가 박힌다(값만 있으면 DB 인지 env 인지 모른다)
"""

import logging

import pytest

import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None


def _session(monkeypatch, *, setup=None, member_target=None,
             target_override="", locale_override=""):
    monkeypatch.setattr(cs.settings, "CASCADE_TARGET_LANGUAGE_OVERRIDE", target_override)
    monkeypatch.setattr(cs.settings, "CASCADE_LOCALE_OVERRIDE", locale_override)
    session = cs.CascadeSession(_Sink(), object())
    session._setup = setup
    session._member_target_language = member_target
    return session


def test_db_language_is_quiet(monkeypatch, caplog):
    """① 정상 운영은 조용하다 — 경고가 흔해지면 아무도 안 본다."""
    session = _session(monkeypatch, setup={"locale": "ja"}, member_target="ko")
    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._resolve_languages()

    lang = [r for r in caplog.records if "cascade 언어" in r.getMessage()]
    assert lang and lang[0].levelno == logging.INFO, [r.levelname for r in lang]
    assert "출처=DB" in lang[0].getMessage()


def test_env_default_language_warns(monkeypatch, caplog):
    """⭐⭐ ② DB 가 없으면 **경고**. 지금 `en` 이 우연히 맞는 상태이고, 그 우연은 언젠가 깨진다."""
    session = _session(monkeypatch, setup=None, member_target=None)
    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._resolve_languages()

    lang = [r for r in caplog.records if "cascade 언어" in r.getMessage()]
    assert lang, caplog.text
    assert lang[0].levelno == logging.WARNING, (
        "조용히 기본값으로 돈다 — 학습자가 자기 모국어가 아닌 언어로 설명을 듣는데 아무도 모른다"
    )
    assert "출처=env 기본값" in lang[0].getMessage()


def test_an_override_also_warns(monkeypatch, caplog):
    """③ 실험용 덮어쓰기도 **의도된 운영 상태가 아니다** — 켜져 있으면 보여야 한다.

    (env 를 켜 두고 잊으면 DB 값이 영영 안 먹는데, 로그가 조용하면 그 사실을 못 찾는다.)
    """
    session = _session(monkeypatch, setup={"locale": "ja"}, member_target="ko",
                       target_override="en")
    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._resolve_languages()

    lang = [r for r in caplog.records if "cascade 언어" in r.getMessage()]
    assert lang and lang[0].levelno == logging.WARNING, [r.levelname for r in lang]
    assert "출처=override(env)" in lang[0].getMessage()


def test_voice_fallback_warns_once(monkeypatch, caplog):
    """⭐⭐ ④ DB 음색이 없으면 **그 캐릭터 목소리가 아니다** — 한 번은 말해야 한다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_VOICE_OVERRIDE", "")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_VOICE", "Sulafat")
    session = cs.CascadeSession(_Sink(), object())
    session._voice = None                     # character.voice_id NULL 인 상태
    session._character_id = 7

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        assert session._tts_voice() == "Sulafat"
        assert session._tts_voice() == "Sulafat"      # 구간마다 불린다
        assert session._tts_voice() == "Sulafat"

    warned = [r for r in caplog.records if "음색 폴백" in r.getMessage()]
    assert len(warned) == 1, f"도배했다({len(warned)}회) — 구간마다 부르는 자리다"
    assert warned[0].levelno == logging.WARNING
    assert "character=7" in warned[0].getMessage()


def test_db_voice_is_quiet(monkeypatch, caplog):
    """DB 음색이 있으면 아무 말도 안 한다(그게 정상이다)."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_VOICE_OVERRIDE", "")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_VOICE", "Sulafat")
    session = cs.CascadeSession(_Sink(), object())
    session._voice = "Leda"

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        assert session._tts_voice() == "Leda", "env 가 DB 를 이겼다"

    assert not [r for r in caplog.records if "음색 폴백" in r.getMessage()]


def test_the_character_log_records_where_the_voice_came_from():
    """⑤ 값만 찍으면 **어디서 온 값인지 모른다** — 오늘 조사에서 그것 때문에 시간을 썼다."""
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(cs.CascadeSession._load_call_context))
    assert "출처=%s" in src, "캐릭터 로그에 음색 출처가 없다"
