"""클라가 **실제로 들린 시각**을 보내온다 — 서버가 빼서 **한 줄로** 남긴다.

## 왜 (2026-08-15)
오늘까지 표본이 **사장님 손에 달려 있었다.** USB 를 꽂거나 사진을 찍어 주셔야 숫자가 나왔고,
그래서 하루 종일 "표본이 6건뿐이라 단정 못 한다"를 반복했다. 서버로 보내면 **모든 통화가
자동으로 쌓인다.**

## ⭐⭐ 목적은 평균이 아니라 **뺄셈**이다
서버는 첫 소리를 **언제 보냈는지** 안다(`첫소리=2270ms`). 클라는 그게 **언제 났는지** 안다.

    클라 재생 몫 = 들림(audible_ms) − 서버 첫소리

이 값을 오늘 내내 추정만 하고 **한 번도 못 쟀다.** 그래서 **턴 단위**여야 한다 — 통화 끝에
평균만 받으면 짝을 못 맞춰 뺄셈이 성립하지 않는다.

## 여기서 고정하는 성질
  ① 서버가 **뺄셈을 해서** 찍는다(사람이 손으로 빼게 두지 않는다)
  ② 조인 키는 **비버 턴 id** 다 — 서버가 `첫소리` 를 잰 턴이 그것이다
  ③ ⛔ 짝을 못 찾으면 **조용히 버리지 않는다**(그 사실을 찍는다)
  ④ `estimated=true` 는 **`⚠추정`** 으로 갈린다(추정치가 실측 표에 섞이면 안 된다)
  ⑤ ⛔ 메시지가 없거나·필드가 없거나·깨져도 **통화는 그대로 돈다**(R5, 구버전 클라)
"""

import logging

import pytest

import domains.learning.realtime.cascade_session as cs
from domains.learning.realtime.cascade_protocol import (
    CascadeClientMessage,
    ClientCascadeTiming,
)
from pydantic import TypeAdapter

_LOGGER = "domains.learning.realtime.cascade_session"


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None


def _session() -> cs.CascadeSession:
    return cs.CascadeSession(_Sink(), object())


def _line(caplog, head: str = "cascade 클라계기") -> str:
    hits = [r.getMessage() for r in caplog.records if r.getMessage().startswith(head)]
    assert len(hits) == 1, f"{head} 줄이 {len(hits)}개다 — {caplog.text}"
    return hits[0]


def _feed(session, caplog, **ctrl) -> str:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        session._on_client_timing({"type": "client_timing", **ctrl})
    return caplog.text


# ── ①② 뺄셈 ────────────────────────────────────────────────────────────────
def test_the_server_does_the_subtraction(caplog):
    """⭐⭐ ① **서버가 빼서 찍는다.** 두 숫자만 찍으면 읽을 때마다 손으로 빼야 하고, 그러면 안 본다."""
    session = _session()
    session._note_first_sound("b58", 2270)

    _feed(session, caplog, turn_id="b58", audible_ms=3370,
          turn_start_ms=1500, cushion_ms=300, estimated=False)

    line = _line(caplog)
    assert "클라몫=1100ms" in line, line
    assert "들림=3370ms" in line and "서버첫소리=2270ms" in line, line
    assert "쿠션 300" in line and "turn_start 1500" in line, line
    assert "실측" in line and "추정" not in line, line


def test_the_join_key_is_the_beaver_turn(caplog):
    """② 조인 키는 **비버 턴**이다 — 사용자 턴 id 로 오면 짝이 안 맞는다(그리고 그게 보여야 한다)."""
    session = _session()
    session._note_first_sound("b58", 2270)

    _feed(session, caplog, turn_id="u12", audible_ms=3370)

    assert "짝없음" in _line(caplog, "cascade 클라계기 짝없음")


# ── ③ 짝을 못 찾아도 조용하지 않다 ─────────────────────────────────────────
def test_an_unmatched_turn_is_logged_not_swallowed(caplog):
    """⛔⛔ ③ **조용히 버리지 않는다.** 안 보이면 "값이 안 쌓인다"를 원인 없이 겪는다.

    턴이 취소됐거나(barge-in) 소리가 한 조각도 안 나갔으면 서버 첫소리가 없다 — 그것도 사실이다.
    """
    session = _session()

    _feed(session, caplog, turn_id="b99", audible_ms=3370)

    line = _line(caplog, "cascade 클라계기 짝없음")
    assert "b99" in line and "3370" in line, line


def test_a_cancelled_turn_has_no_server_value():
    """소리가 안 나간 턴은 애초에 보관되지 않는다(음수는 안 담는다)."""
    session = _session()
    session._note_first_sound("b7", -1)
    assert "b7" not in session._first_sound_ms


def test_the_history_is_bounded():
    """⚠ 15분 통화의 모든 턴을 들고 있을 이유가 없다 — 클라 계기는 그 턴 직후에 온다."""
    session = _session()
    for i in range(cs._FIRST_SOUND_HISTORY + 5):
        session._note_first_sound(f"b{i}", 1000 + i)
    assert len(session._first_sound_ms) == cs._FIRST_SOUND_HISTORY
    assert "b0" not in session._first_sound_ms, "오래된 것부터 밀려야 한다"
    assert f"b{cs._FIRST_SOUND_HISTORY + 4}" in session._first_sound_ms


# ── ④ 추정치는 섞이지 않는다 ────────────────────────────────────────────────
def test_an_estimated_sample_is_marked(caplog):
    """⚠ ④ 추정치가 실측과 **같은 표에 섞이면** 그 표로 내린 판단이 전부 흔들린다."""
    session = _session()
    session._note_first_sound("b3", 2000)

    _feed(session, caplog, turn_id="b3", audible_ms=2600, estimated=True)

    line = _line(caplog)
    assert "⚠추정" in line and "클라몫=600ms" in line, line


# ── ⑤ 계측이 통화를 죽이지 않는다 ───────────────────────────────────────────
def test_a_broken_message_does_not_raise(caplog):
    """⛔ ⑤ **계측이 통화를 죽이면 안 된다.** 구버전·깨진 메시지도 흡수한다."""
    session = _session()
    session._note_first_sound("b1", 1000)

    for bad in ({"turn_id": 5, "audible_ms": "많이"}, {"audible_ms": None}, {}):
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            session._on_client_timing({"type": "client_timing", **bad})   # 예외가 나면 실패다


def test_a_missing_field_is_not_read_as_zero(caplog):
    """⛔ 값이 없는 것과 0 은 다르다 — 0 으로 먹으면 "즉시 들렸다"는 **거짓 표본**이 된다."""
    session = _session()
    session._note_first_sound("b4", 2000)

    _feed(session, caplog, turn_id="b4")            # audible_ms 없음(구버전 클라)

    assert [r for r in caplog.records if "들림 값이 없다" in r.getMessage()], caplog.text
    assert not [r for r in caplog.records
                if r.getMessage().startswith("cascade 클라계기:")], "없는 값으로 뺄셈을 했다"


# ── 계약 ────────────────────────────────────────────────────────────────────
def test_the_message_is_in_the_client_union():
    """⛔ union 에 안 넣으면 소켓 층에서 **거절**된다 — 핸들러까지 오지도 않는다."""
    adapter: TypeAdapter = TypeAdapter(CascadeClientMessage)
    msg = adapter.validate_python(
        {"type": "client_timing", "turn_id": "b58", "audible_ms": 3370}
    )
    assert isinstance(msg, ClientCascadeTiming)
    assert msg.turn_id == "b58" and msg.audible_ms == 3370


def test_every_field_has_a_default():
    """⚠ R5: 구버전 클라가 일부만 보내도 성립해야 한다 — 전부 기본값이 있다."""
    msg = ClientCascadeTiming()
    assert msg.audible_ms == -1 and msg.turn_start_ms == -1 and msg.cushion_ms == -1
    assert msg.turn_id == "" and msg.estimated is False
