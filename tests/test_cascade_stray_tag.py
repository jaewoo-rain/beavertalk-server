"""⛔⛔ **집합 밖 대괄호 토큰이 소리로 새면 안 된다**(2026-08-12 실통화 00147).

사장님: "ai 가 대답할 때 **대화. ~~** 이렇게 시작할 때 **'대화'라고 읽는다**".
같은 통화의 로그는 b4~b9 전부 `감정=없음` 이었다 — **두 증상이 하나의 버그**다:
  · `_EMOTION_TAG_RE` 는 감정 6개만 안다 → `[대화]` 는 안 지워져 **TTS 가 읽었다**
  · `detect_emotion` 도 집합 밖이라 None → **감정이 통화 내내 죽었다**

왜 그런 게 오나(가설 — 프롬프트는 이번 범위 밖): `core/persona_prompt.py` 가
`[공부 모드]` `[대화 모드]` `[학습자 수준]` 처럼 **대괄호를 구획 라벨로** 쓴다. 같은
프롬프트가 "대사 맨 앞에 `<happy>` 을 붙여라"라고도 하니 모델이 그 자리에 모드 이름을 넣는다.

여기서 고정하는 성질:
  ① 집합 밖 토큰이 **TTS 로 넘어가는 텍스트에 안 남는다**
  ② 그때 감정은 없음으로 가되 **무엇이 왔는지 로그에 남는다**(조용히 지우지 않는다)
  ③ 집합 안 태그는 **지금처럼** 동작한다(기존 성질 유지)
  ④ **정상 대사의 대괄호는 안 지운다** — 로마자·영어 뜻 표기가 그 자리를 쓴다
"""

import logging

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


# ── ① 소리로 안 나간다 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("tag", ["대화", "공부 모드", "대화 모드", "시스템", "학습자 수준"])
def test_stray_bracket_tokens_never_reach_the_tts_text(tag):
    """⛔ 실제로 새던 그 모양들 — 맨 앞 한글 대괄호 토큰은 전부 걷어낸다."""
    out = cr.strip_emotion_tags(f"[{tag}] 안녕하세요, 오늘은 인사를 배워요.")
    assert "[" not in out and tag not in out, out
    assert out.startswith("안녕하세요")


def test_several_stray_tokens_in_a_row_are_all_removed():
    """프롬프트 라벨이 두 개 붙어 나오는 경우도 있다."""
    out = cr.strip_emotion_tags("[대화 모드] [공부 모드] 안녕하세요.")
    assert out == "안녕하세요."


def test_a_stray_token_next_to_a_real_emotion_tag():
    """③과 겹치는 자리 — 감정은 살고 라벨만 사라진다."""
    assert cr.strip_emotion_tags("[대화] <happy> 잘했어요!") == "잘했어요!"
    assert cr.detect_emotion("[대화] <happy> 잘했어요!") == "happy"


@pytest.mark.asyncio
async def test_the_vendor_never_sees_a_stray_tag(monkeypatch):
    """⭐ 벤더로 나가기 **직전 길목**에서 막힌다(실제 송출 경로)."""
    asked: list[str] = []

    async def _tts(text, **kwargs):
        asked.append(text)

        async def _gen():
            yield b"\x00\x00" * 240
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    session = cs.CascadeSession(_Sink())
    await session.beaver.begin()
    await session._speak_one("[대화] 안녕하세요", "ko")
    assert asked and all("대화" not in t for t in asked), asked


# ── ② 조용히 지우지 않는다 ─────────────────────────────────────────────────
def test_the_dropped_token_is_reported_in_the_reply_line(caplog):
    """⛔ '없음'만 보고는 태그가 **안 온 건지 집합 밖이 온 건지** 못 가른다."""
    session = cs.CascadeSession(_Sink())
    with caplog.at_level(logging.WARNING):
        session._note_stray_tag("[대화] 안녕하세요.")
    assert session._emotion_log() == "감정=없음(버린태그:대화)"
    assert any("집합 밖 태그" in r.getMessage() for r in caplog.records), caplog.text


def test_the_warning_is_logged_once_per_reply(caplog):
    """⚠ 조각마다 찍으면 로그가 도배된다 — 대답 하나에 한 번."""
    session = cs.CascadeSession(_Sink())
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            session._note_stray_tag("[대화] 안녕하세요.")
    assert len([r for r in caplog.records if "집합 밖 태그" in r.getMessage()]) == 1


def test_a_normal_reply_reports_plain_none():
    """태그가 아예 없으면 예전 그대로 '감정=없음'."""
    session = cs.CascadeSession(_Sink())
    session._note_stray_tag("안녕하세요. 오늘은 인사를 배워요.")
    assert session._emotion_log() == "감정=없음"
    assert session._dropped_tag == ""


def test_a_known_emotion_is_not_reported_as_dropped():
    """③ 집합 안 태그는 감정으로 쓰인다 — 버린 게 아니다."""
    session = cs.CascadeSession(_Sink())
    session._tts_engine = "gemini-tts"          # 스타일을 받는 엔진(미적용 꼬리표가 안 붙는다)
    session._reply_emotion = "happy"
    session._note_stray_tag("<happy> 잘했어요!")
    assert session._dropped_tag == ""
    assert session._emotion_log() == "감정=happy"


# ── ④ 정상 대사는 안 건드린다 ──────────────────────────────────────────────
def test_romanization_and_glosses_survive():
    """⛔ 이 앱에서 대괄호가 **정상적으로** 쓰이는 자리다 — 지우면 학습 내용이 사라진다."""
    for text in ("[annyeonghaseyo] means hello.",
                 "안녕하세요 [annyeonghaseyo] 라고 해요.",
                 "This means [I am a student]."):
        assert cr.strip_emotion_tags(text) == text, text


def test_brackets_in_the_middle_of_a_sentence_survive():
    """규약은 '맨 앞'이다 — 문장 중간의 대괄호는 대사의 일부일 가능성이 높다."""
    text = "다음 [   ] 안에 들어갈 말은 뭘까요?"
    assert cr.strip_emotion_tags(text) == text


def test_a_long_bracket_phrase_is_not_a_tag():
    """태그는 짧다 — 긴 대괄호 문구를 지우면 대사를 먹는다."""
    text = "[오늘은 정말 즐거운 하루였어요 그렇죠] 라고 말해 보세요."
    assert cr.strip_emotion_tags(text) == text


def test_read_stray_tag_only_answers_when_the_bracket_is_closed():
    """⚠ 스트리밍은 조각으로 온다 — `[대화` 까지만 왔을 때 성급히 판정하면 안 된다."""
    assert cr.read_stray_tag("[대화") is None
    assert cr.read_stray_tag("[대화] 안녕") == "대화"
    assert cr.read_stray_tag("<happy> 잘했어요") is None, "집합 안은 버린 태그가 아니다"


# ── ⚠ 대괄호가 없는 경우: **지우지 않고 세기만** ───────────────────────────
def test_a_bare_label_word_is_observed_but_never_removed(caplog):
    """⚠ 우리는 대사 원문을 안 찍는다 — `[대화]` 였는지 `대화.` 였는지는 **추론**이었다.

    후자면 위 제거가 안 먹고 증상이 그대로 남는다. 그래서 **지우지 않고**(정상 낱말이다)
    사실만 남겨 다음 통화 로그에서 갈리게 한다.
    """
    assert cr.strip_emotion_tags("대화. 안녕하세요.") == "대화. 안녕하세요."   # ⛔ 안 지운다
    session = cs.CascadeSession(_Sink())
    with caplog.at_level(logging.WARNING):
        session._note_stray_tag("대화. 안녕하세요.")
    assert session._emotion_log() == "감정=없음(버린태그:?대화)"
    assert any("대괄호 없음" in r.getMessage() for r in caplog.records), caplog.text


def test_a_normal_sentence_starting_with_the_same_word_is_not_flagged():
    """⛔ "대화 연습해 봐요" 는 정상 대사다 — 구두점이 뒤따를 때만 라벨로 본다."""
    assert cr.read_bare_label("대화 연습해 봐요.") is None
    assert cr.read_bare_label("공부 열심히 했어요!") is None
    assert cr.read_bare_label("대화: 안녕하세요") == "대화"
