"""캐스케이드 DB 연결 — **Live 와 같은 함수로 같은 기록**을 만든다(설계 20260812_1620).

사장님: "라이브처럼 db에서 캐릭터 목소리 받아서 … 통화내용 저장하던 방식도 그대로 저장".
그리고 별건 지시: "stt는 자동으로 되지만 **통화는 어떤 언어로 해야 할지 알아야 하니까
locale 언어랑 target language 받아와야 해**."

⛔ 지금까지는 캐릭터·언어가 **env 전역 고정**이라 **누가 통화해도 같은 캐릭터·같은 설명 언어**였다.

여기서 고정하는 성질:
  ① 회원의 캐릭터가 바뀌면 **음색과 페르소나가 바뀐다**
  ② 회원의 locale/target 이 바뀌면 **페르소나 언어가 바뀐다**
  ③ ⛔ 그때도 **STT 는 자동 감지 그대로**다(언어 지정과 얽히면 안 된다 — 실측 39/41)
  ④ DB 가 실패해도 **통화는 산다**(R5)
  ⑤ dev 덮어쓰기(env)가 DB 를 이긴다 — ⛔ 단 **전용 override 설정**으로만
"""

import pytest

import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


def _setup(**over) -> dict:
    base = {
        "role": "다정한 비버 선생님", "personality": "밝고 친근하다",
        "level_profile": "초급(A1)", "voice": "Fenrir", "locale": "vi",
        "interests": ["여행"], "name": "비버",
    }
    base.update(over)
    return base


def _session(member_id=7, target="ko", setup=None) -> cs.CascadeSession:
    session = cs.CascadeSession(_Sink(), object(), session_factory=object(),
                                member_id=member_id, member_target_language=target)
    session._setup = setup
    return session


# ── ①③ 캐릭터·언어가 DB 에서 온다 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_character_voice_and_persona_come_from_the_db(monkeypatch):
    """⭐ 캐릭터가 바뀌면 **음색도 페르소나도** 바뀐다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_VOICE_OVERRIDE", "")
    a = _session(setup=_setup(voice="Fenrir", role="다정한 비버"))
    b = _session(setup=_setup(voice="Kore", role="장난꾸러기 수달"))
    a._resolve_languages(); a._voice = a._setup["voice"]
    b._resolve_languages(); b._voice = b._setup["voice"]

    assert a._tts_voice() == "Fenrir" and b._tts_voice() == "Kore"
    assert a._system_instruction() != b._system_instruction()
    assert "다정한 비버" in a._system_instruction()
    assert "장난꾸러기 수달" in b._system_instruction()


@pytest.mark.asyncio
async def test_the_persona_language_follows_the_member(monkeypatch):
    """② 회원의 모국어·학습 대상이 페르소나에 실린다 — 지금까지는 **전원 같은 언어**였다."""
    vi = _session(target="ko", setup=_setup(locale="vi"))
    en = _session(target="ja", setup=_setup(locale="en"))
    vi._resolve_languages()
    en._resolve_languages()

    assert (vi._locale, vi._target_code) == ("vi", "ko")
    assert (en._locale, en._target_code) == ("en", "ja")
    assert vi._target_label != en._target_label, "학습 대상 라벨이 안 따라간다"
    assert vi._system_instruction() != en._system_instruction()


def test_stt_stays_on_auto_detect_regardless_of_the_member(monkeypatch):
    """⛔⛔ **언어를 DB 에서 받는 것과 STT 언어 지정은 다른 이야기다.**

    실측: OpenAI 전사는 **자동 감지**로 41개 언어 중 39개를 맞혔고, `language` 를
    미지정/en/ko 로 바꿔 같은 오디오로 재봤을 때 **결과가 동일**했다. 넣어서 좋아진다는
    증거가 없으므로 넣지 않는다. 이 시험은 그 결정이 조용히 뒤집히는 걸 막는다.
    """
    from core.openai_stt import OpenAiRealtimeSttStream

    session = _session(target="ja", setup=_setup(locale="vi"))
    session._resolve_languages()
    codes = session._stt_language_codes()
    assert codes == [session._target_code, session._locale] == ["ja", "vi"]

    # ⭐ **행동으로 본다**: 코드를 둘 넘기면 어댑터가 `language` 를 안 싣는다 = 자동 감지.
    #   (문자열 검사로는 주석만 보고 통과해 버린다 — 이 프로젝트에서 이미 겪었다.)
    assert OpenAiRealtimeSttStream(24000, codes)._language is None, "STT 에 언어가 강제됐다"
    # 하나만 넘기면 지정된다 — 그래서 **둘을 넘기는 것 자체가** 자동 감지를 지키는 장치다.
    assert OpenAiRealtimeSttStream(24000, ["ko"])._language == "ko"


# ── ⑤ dev 덮어쓰기 ─────────────────────────────────────────────────────────
def test_dev_override_beats_the_db(monkeypatch):
    """사장님이 화면·env 로 실험하신다 — 덮어쓰기는 남긴다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_VOICE_OVERRIDE", "Puck")
    monkeypatch.setattr(cs.settings, "CASCADE_LOCALE_OVERRIDE", "ja")
    monkeypatch.setattr(cs.settings, "CASCADE_TARGET_LANGUAGE_OVERRIDE", "en")
    session = _session(target="ko", setup=_setup(voice="Fenrir", locale="vi"))
    session._voice = session._setup["voice"]
    session._resolve_languages()

    assert session._tts_voice() == "Puck"
    assert (session._locale, session._target_code) == ("ja", "en")


def test_the_server_default_is_a_fallback_not_an_override(monkeypatch):
    """⛔ **배포 env 에 이미 값이 있다**(demo-api: CASCADE_TTS_VOICE=Sulafat).

    그걸 덮어쓰기로 쓰면 DB 캐릭터 음색이 **영영 안 먹는다** — 사장님 지시의 핵심이 죽는다.
    그래서 그 값은 **DB 가 없을 때의 기본값**이고, 덮어쓰기는 전용 설정으로만 한다.
    """
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_VOICE_OVERRIDE", "")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_VOICE", "Sulafat")
    with_db = _session(setup=_setup(voice="Fenrir"))
    with_db._voice = "Fenrir"
    without_db = _session(setup=None)

    assert with_db._tts_voice() == "Fenrir", "DB 음색이 서버 기본값에 눌렸다"
    assert without_db._tts_voice() == "Sulafat", "DB 가 없을 때 기본값이 안 쓰인다"


def test_openai_engine_falls_back_to_the_server_voice_loudly(monkeypatch, caplog):
    """⛔ OpenAI 는 음성 로스터가 **아예 다르다**(nova·alloy…). 대응표 근거가 없다.

    조용히 틀린 음성을 쓰면 "캐릭터 목소리가 이상하다"를 아무도 설명 못 한다.
    """
    import logging

    monkeypatch.setattr(cs.settings, "CASCADE_TTS_VOICE_OVERRIDE", "")
    session = _session(setup=_setup(voice="Fenrir"))
    session._voice = "Fenrir"
    session._tts_engine = cs._OPENAI_TTS_CHOICE
    with caplog.at_level(logging.WARNING):
        assert session._tts_voice() is None
    assert any("음색 미적용" in r.getMessage() for r in caplog.records), caplog.text


# ── ④ DB 가 죽어도 통화는 산다 ────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_db_failure_does_not_kill_the_call(monkeypatch, caplog):
    """⛔ "설명 언어가 틀린 통화"가 "통화 불가"보다 낫다(R5 — Live 와 같은 규율)."""
    import logging

    async def _boom(factory, fn):
        raise RuntimeError("DB 다운")

    monkeypatch.setattr(cs.svc, "run_db", _boom)
    session = _session(setup=None)
    with caplog.at_level(logging.WARNING):
        await session._load_call_context()

    assert session._setup is None
    assert session._system_instruction(), "페르소나가 아예 못 만들어졌다"
    assert any("설정 조회 실패" in r.getMessage() for r in caplog.records), caplog.text


@pytest.mark.asyncio
async def test_without_db_context_it_runs_like_the_demo():
    """세션 팩토리가 없으면(데모·테스트) 예전처럼 env 기본값으로 돈다."""
    session = cs.CascadeSession(_Sink(), object())
    await session._load_call_context()
    assert session._setup is None
    assert session._locale == cs.settings.CASCADE_TTS_LANGUAGE
    assert session._system_instruction()


# ── 언어가 **한 소스**에서 흐른다 ─────────────────────────────────────────
def test_every_language_use_reads_the_session_not_the_settings():
    """⛔ 같은 뜻의 값이 여러 곳에 있으면 **반드시 갈린다**(실제로 갈려 있었다).

    TTS 구간 분할·페르소나·배속·STT 힌트가 전부 세션의 `_locale`/`_target_code` 를 본다.
    """
    import inspect

    for name in ("_speak", "_system_instruction", "_stt_language_codes", "_run_batch_reply"):
        src = inspect.getsource(getattr(cs.CascadeSession, name))
        assert "settings.CASCADE_TTS_LANGUAGE" not in src, name
        assert "settings.CASCADE_TTS_TARGET_LANGUAGE" not in src, name
        assert "settings.CASCADE_PERSONA_LOCALE" not in src, name
