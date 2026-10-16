"""P5(2026-09-15, 실통화 1610 — 프리토킹 첫 비버 턴 오디오 0B, 재시드 1회에도 sum_resp=0): 벙어리 인사 재시드 상한 2 · 두 번째는 짧은 대체 시드(차시 프리토킹) ·
재시드 결과 로그 한 줄. 첫 번째 재시드·정상 인사 무재시드는 tests/test_normalcall_ws.py 가 그대로 지킨다."""
from __future__ import annotations

import logging

import pytest

import domains.learning.realtime.call_session as cs
from core.prompts.locked import seeds


class _Sess:
    def __init__(self):
        self.sent_text_turns: list[str] = []

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)


def _state(course: str = "freetalk") -> cs._CallState:
    st = cs._CallState()
    st.session_epoch = 1
    st.seed_text = "[통화 시작] 긴 선톡 시드"
    st.cur_course = course
    st.freetalk_brief = object() if course == "freetalk" else None
    st.freetalk_target = "한국어" if course == "freetalk" else ""
    return st


@pytest.mark.asyncio
async def test_second_mute_greeting_gets_the_short_freetalk_seed_and_the_third_is_not_reseeded():
    st, sess = _state(), _Sess()
    st.beaver_turns = 1
    assert cs._greeting_was_mute(st, 0) is True
    await cs._reseed_greeting(sess, st)
    st.beaver_turns = 2                                   # 재시드가 만든 턴도 벙어리로 끝났다
    assert cs._greeting_was_mute(st, 0) is True, "상한 2 — 두 번째 재시드"
    await cs._reseed_greeting(sess, st)
    assert sess.sent_text_turns == [st.seed_text, seeds.seed_freetalk_lesson_reseed_short("한국어")]
    st.beaver_turns = 3
    assert cs._greeting_was_mute(st, 0) is False, "세 번째는 없다"
    assert cs.GREETING_RESEED_MAX == 2 and st.greeting_reseeds == 2


@pytest.mark.asyncio
async def test_second_reseed_outside_lesson_freetalk_resends_the_same_seed():
    st, sess = _state(course="expression"), _Sess()
    for turns in (1, 2):
        st.beaver_turns = turns
        assert cs._greeting_was_mute(st, 0) is True
        await cs._reseed_greeting(sess, st)
    assert sess.sent_text_turns == [st.seed_text, st.seed_text]
    short = seeds.seed_freetalk_lesson_reseed_short("한국어")
    assert "한 문장만 인사하고" in short and "첫 질문 하나만" in short and len(short) < 120


def _mute_t1_state() -> cs._CallState:
    st = _state()
    st.segments = [{"turn_index": 0, "role": "user", "text": "", "pcm": b"\x01\x02"}]
    st.next_turn_index = 1
    st.cur_beaver_text = ["Hey Tester, long time no see!"]
    cs._flush_beaver_segment(st)                          # 오디오 0바이트 인사 턴(t1)
    return st


@pytest.mark.asyncio
async def test_mute_greeting_turn_is_dropped_from_saved_segments_and_turn_index_stays_continuous():
    """D4(PM-DEC-693 안 1 · call 1994) — 재시드한 무음 인사 t1 은 저장 대본에 남기지 않는다(학습자는 못 듣고 못 봤다)."""
    st, sess = _mute_t1_state(), _Sess()
    assert [s["turn_index"] for s in st.segments] == [0, 1] and st.beaver_turns == 1
    assert cs._greeting_was_mute(st, 0) is True
    cs._drop_mute_greeting_segment(st)
    await cs._reseed_greeting(sess, st)
    assert [s["role"] for s in st.segments] == ["user"] and st.next_turn_index == 1
    assert st.beaver_turns == 1, "beaver_turns 는 재시드 판정이 쓴다 — 그대로"
    st.cur_beaver_text = ["Hey Tester, long time no see!"]
    st.cur_beaver_pcm = bytearray(b"\x00\x01" * 8)
    cs._flush_beaver_segment(st)                          # 재시드가 만든 소리 난 인사
    assert [(s["turn_index"], s["role"]) for s in st.segments] == [(0, "user"), (1, "beaver")]
    assert [s["text"] for s in st.segments if s["role"] == "beaver"] == ["Hey Tester, long time no see!"]


def test_drop_keeps_a_persisted_or_voiced_segment():
    st = _mute_t1_state()
    st.persisted_count = 2                                # 이미 저장됐으면 손대지 않는다
    cs._drop_mute_greeting_segment(st)
    assert len(st.segments) == 2 and st.next_turn_index == 2
    st2 = _mute_t1_state()
    st2.segments[-1]["pcm"] = b"\x00\x01"               # 소리가 있으면 무음 인사가 아니다
    cs._drop_mute_greeting_segment(st2)
    assert len(st2.segments) == 2 and st2.next_turn_index == 2


@pytest.mark.asyncio
async def test_reseed_result_is_logged_once_on_the_next_turn_end(caplog):
    caplog.set_level(logging.INFO, logger=cs.logger.name)
    st, sess = _state(), _Sess()
    st.beaver_turns = 1
    await cs._reseed_greeting(sess, st)
    cs._log_reseed_result(st, 96000)
    cs._log_reseed_result(st, 96000)                     # 한 번만
    lines = [r.getMessage() for r in caplog.records if "재시드 결과" in r.getMessage()]
    assert lines == ["normalcall 재시드 결과 1/2: 다음 턴 오디오 96000B (소리 남)"]
    await cs._reseed_greeting(sess, st)
    cs._log_reseed_result(st, 0)
    assert "⛔또 벙어리" in [r.getMessage() for r in caplog.records if "재시드 결과" in r.getMessage()][-1]
