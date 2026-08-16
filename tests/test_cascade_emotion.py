"""감정 태그 — **고정 집합에서 골라 쓰게** 한다(2026-08-10 사장님 지시).

① "llm 이 감정을 태그로 뱉어서 그걸 tts 에 넣어야 하잖아"
② ⭐ "미리 감정들 프롬프트를 정해놓고 **골라 쓰게** 하는 게 어때? **프롬프트가 일정해야
   감정 표현도 그나마 일정할 거 아니야**"

②가 설계의 핵심 제약이다 — LLM 이 스타일 문장을 매번 지어내면 같은 감정도 통화마다 다르게
들린다. 그래서 LLM 은 **태그 이름만** 고르고, 문구는 서버 표에서만 나온다.

여기서 고정하는 성질:
  ① 태그는 **TTS 로 넘어가는 텍스트에 남지 않는다**(소리로 나가면 안 된다)
  ② 집합 밖·누락이면 **기본 스타일**로 조용히 떨어진다(R5)
  ③ 감정 문구 전량에 **속도 어휘가 없다**(오늘 "또박또박" 한 낱말이 절반을 먹었다)
  ④ Chirp 에서는 스타일이 전달되지 않는다(기존 성질 보존) — 대신 로그로 보인다
  ⑤ 대답 1건당 감정 **하나**(구간을 더 쪼개지 않는다 — 429 가 1순위 제약이다)
"""

import pytest

import domains.learning.realtime.cascade_reply as cr
import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


# ── ① 태그는 소리로 안 나간다 ──────────────────────────────────────────────
@pytest.mark.parametrize("emotion", list(cr.EMOTION_STYLES))
def test_tags_are_stripped_before_tts(emotion):
    """⛔ `__마커__` 와 같은 급이다 — `?` 를 '쿼스천마크'로 읽던 사고 계열."""
    text = f"[{emotion}] 우와, 정말 잘했어요!"
    out = cr.strip_emotion_tags(text)
    assert "[" not in out and emotion not in out.replace("잘했어요", "")
    assert "우와, 정말 잘했어요!" in out


def test_tags_are_stripped_anywhere_not_just_the_front():
    """규약은 '맨 앞'이지만 **어디 있든** 걷어낸다 — LLM 이 규약을 어겨도 소리는 안 나간다."""
    out = cr.strip_emotion_tags("잘했어요! <happy> 다음은 <surprised> 이거예요")
    assert "<happy>" not in out and "<surprised>" not in out


@pytest.mark.asyncio
async def test_speak_one_never_sends_a_tag_to_the_vendor(monkeypatch):
    """⭐ 벤더로 나가기 **직전 길목**에서 막는다(스트리밍 경로)."""
    asked: list[str] = []

    async def _tts(text, **kwargs):
        asked.append(text)

        async def _gen():
            yield b"\x00\x00" * 240
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    session = cs.CascadeSession(_Sink())
    await session.beaver.begin()
    await session._speak_one("<happy> 아주 좋아요", "en")
    assert asked and all("<happy>" not in t for t in asked), asked


# ── ② 집합 밖·누락 → 기본 스타일 ───────────────────────────────────────────
def test_unknown_or_missing_tag_falls_back_quietly():
    """⚠ 통화가 죽으면 안 된다(R5). 모르는 값은 **없는 것과 같게** 다룬다."""
    assert cr.detect_emotion("[화남] 뭐야") is None          # 집합 밖
    assert cr.detect_emotion("그냥 대사") is None            # 누락
    assert cr.emotion_style(None) is None
    assert cr.emotion_style("화남") is None
    assert cr.emotion_style("happy") == cr.EMOTION_STYLES["happy"]


def test_session_style_is_the_table_value_or_the_server_default():
    session = cs.CascadeSession(_Sink())
    assert session._style_prompt() is None                   # 감정 없음 → 서버 기본값
    session._reply_emotion = "happy"
    assert session._style_prompt() == cr.EMOTION_STYLES["happy"]
    session._tts_style = "직접 고른 문구"                     # 데모 화면 지정이 이긴다
    assert session._style_prompt() == "직접 고른 문구"


# ── ③ 속도 어휘 금지 ───────────────────────────────────────────────────────
def test_no_emotion_phrase_dictates_speed():
    """⛔ 오늘 "또박또박" 한 낱말이 한국어 속도의 절반을 먹었다(ko 1.3 → 6.2~7.4).

    속도는 `speaking_rate` 파라미터가 맡는다. 문구와 파라미터가 싸우면 어느 게 진짜인지
    못 가린다. 기존 회귀는 상수 하나만 지키므로 **새 문구 전량**에 같은 성질을 건다.
    """
    banned = ("천천히", "또박또박", "느리게", "느릿", "차분", "차근", "빠르게", "속도",
              "slow", "fast", "pace")
    for emotion, phrase in cr.EMOTION_STYLES.items():
        low = phrase.lower()
        for word in banned:
            assert word not in low, (emotion, phrase, word)


def test_the_set_stays_small_and_teacherly():
    """⛔ 개수를 늘리지 마라 — 많을수록 LLM 이 헷갈리고 표현이 흔들린다(②를 깬다)."""
    assert 4 <= len(cr.EMOTION_STYLES) <= 8, cr.EMOTION_STYLES
    assert all(v.strip() for v in cr.EMOTION_STYLES.values())
    assert len(set(cr.EMOTION_STYLES.values())) == len(cr.EMOTION_STYLES)   # 1:1


# ── ④ Chirp 에서는 스타일이 안 간다(기존 성질) + 로그로 보인다 ─────────────
@pytest.mark.parametrize("engine,applied", [
    ("chirp3-hd", False),          # 스타일을 안 받는다
    ("", False),                   # 서버 기본값(Chirp)
    ("gemini-tts", True),
    ("gemini-batch", True),
    ("openai-tts", True),          # ⛔ 2026-08-11 까지 여기가 빠져 **거짓 로그**를 찍었다
])
def test_emotion_log_tells_the_truth_for_every_engine(engine, applied):
    """⛔⛔ **로그가 거짓말을 하면 사람이 잘못 판단한다.**

    실통화 원문: `tts=openai-gpt-4o-mini-tts` 인데 `감정=happy(미적용:cloud-tts-chirp3-hd)`.
    감정은 실제로 들어가고 있었고(instructions) **로그만 틀렸다.** 그걸 보고 "감정이 안
    걸리는구나"라고 판단하면 멀쩡한 기능을 다시 만들게 된다.
    ⚠ 미적용일 때 찍는 엔진 이름도 **실제로 도는 엔진**이어야 한다(예전엔 하드코딩이었다).
    """
    session = cs.CascadeSession(_Sink())
    session._tts_engine = engine
    session._reply_emotion = "happy"
    line = session._emotion_log()
    if applied:
        assert line == "감정=happy", line
    else:
        assert "미적용" in line and session._tts_vendor() in line, line


def test_emotion_log_says_none_when_there_is_no_tag():
    session = cs.CascadeSession(_Sink())
    session._reply_emotion = None
    assert session._emotion_log() == "감정=없음"


def test_style_engine_set_is_defined_in_one_place():
    """⛔ 하드코딩이 이 사고의 원인이었다 — 판정은 성질 표 한 곳에서만.

    표 자체의 전수 점검은 `test_cascade_tts_profile.py` 가 한다. 여기서는 **로그가 표를
    본다**는 것만 잡는다.
    """
    import inspect

    assert cs._TTS_PROFILES[cs._OPENAI_TTS_CHOICE].takes_style is True
    assert cs._TTS_PROFILES[cs.tts.GEMINI_ENGINE].takes_style is True
    assert cs._TTS_PROFILES[cs._CHIRP_CHOICE].takes_style is False
    assert "takes_style" in inspect.getsource(cs.CascadeSession._emotion_log)


def test_the_old_bracket_form_is_still_accepted():
    """⚠ 모델이 규약을 어기고 `<happy>` 을 뱉을 때 **안 받으면 손해만 본다** —
    감정을 잃고 그 글자가 소리로 나간다. 어차피 걷어내므로 받는 쪽이 안전하다."""
    assert cr.detect_emotion("<happy> 잘했어요!") == "happy"
    assert cr.strip_emotion_tags("<happy> 잘했어요!").strip() == "잘했어요!"


def test_chirp_branch_still_passes_an_empty_style():
    """기존 성질 보존 — `core/tts` 의 Chirp 가지는 스타일을 넘기지 않는다."""
    import inspect

    import core.tts as tts_mod

    src = inspect.getsource(tts_mod.synthesize_stream)
    assert '_one(None, "", CHIRP3_ENGINE)' in src, src[-600:]


# ── ⑤ 프롬프트 규약: 캐스케이드 전용(옵트인) ───────────────────────────────
def test_emotion_rule_is_opt_in_and_live_output_is_unchanged():
    """⛔⛔ **Live 에 켜면 모델이 태그를 그대로 읽어 버린다** — 걷어낼 자리가 없다.

    그래서 기본값(빈 튜플)에서는 블록이 안 붙고 출력이 바이트 동일하다.
    """
    from core.persona_prompt import build_system_instruction

    common = {"role": "r", "personality": "p", "level_profile": "l",
              "locale": "en", "interests": []}
    base = build_system_instruction(**common)
    with_tags = build_system_instruction(**common, emotion_tags=tuple(cr.EMOTION_STYLES))
    assert "[감정 태그]" not in base and "감정 태그" not in base
    assert base != with_tags
    assert with_tags.startswith(base)                 # 뒤에 붙기만 한다
    # ⭐ **꺾쇠로 가르친다**(2026-08-12). 대괄호로 가르치면 이 프롬프트가 이미 구획 라벨로
    #   쓰는 `[공부 모드]` 와 자리가 겹쳐, 모델이 태그 자리에 모드 이름을 넣는다 —
    #   그러면 그 글자가 소리로 나가고 감정은 통화 내내 죽는다(실통화 00147/00148).
    for emotion in cr.EMOTION_STYLES:
        assert f"<{emotion}>" in with_tags
        assert f"[{emotion}]" not in with_tags, "대괄호로 가르치면 구획 라벨과 충돌한다"
        # ⛔ 프롬프트가 가르치는 표기를 **서버가 실제로 읽어야** 한다. 둘이 갈리면 감정이
        #   조용히 죽는다(표기만 바꾸고 정규식을 안 옮기는 실수를 여기서 막는다).
        assert cr.detect_emotion(f"<{emotion}> 잘했어요") == emotion
    assert "하나만" in with_tags, "대답당 하나라는 제약이 프롬프트에 없다"
