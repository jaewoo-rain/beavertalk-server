"""오디오 규격 — **가정인가 선언인가**, 그리고 그 가정이 맞나(자기점검).

## 왜 (2026-08-13, 반나절)
에뮬 통화의 `stt_audio_s / dur_s` 가 **2.00배**로 나왔다. "에뮬 마이크가 2배로 흘린다"는
결론 직전까지 갔는데, 프론트가 HAL 계층에서 재니 **1.002배**였다.
⇒ 서버가 가정한 규격이 맞는지 **가를 값이 서버 로그에 없었다.**

이 자리가 왜 위험한가 — 서버는 클라 선언(`sampleRate`)으로 오디오 타임라인을 만든다
(`_audio_ms += bytes / (rate*2)`). 그 자가 틀어지면 **한 곳이 아니라 전부** 틀어진다:
    · 턴 종료 타이머(이미 흘러간 침묵 빼기)
    · barge-in 에너지 게이트(그 오프셋 부근의 에너지를 찾는다)
    · 원가의 오디오 초
    · 파이프라인 지연 계측
그런데 **에러는 안 난다.** 소리도 그럭저럭 난다. 조용히 틀리는 것이 제일 나쁘다.

여기서 고정하는 성질:
  ① 설정 스냅샷이 **레이트·채널**을 싣는다
  ② 그 값이 **클라 선언인지 서버 가정인지** 구분된다(값만 적으면 못 가른다)
  ③ 받은 오디오가 통화 경과보다 **크게 앞서면** WARNING — 실시간보다 빠를 수 없다
  ④ ⛔ 정상은 조용하다. 적게 오는 쪽(마이크 게이팅)은 **경고하지 않는다**
  ⑤ 경고는 통화당 **한 번**(도배하면 아무도 안 본다)
"""

import logging
import time

import pytest

import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None


def _snapshot(session, caplog) -> str:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._log_config_snapshot()
    hits = [r.getMessage() for r in caplog.records if r.getMessage().startswith("cascade 설정:")]
    assert len(hits) == 1, hits
    return hits[0]


# ── ①② 무엇을 가정했나 ──────────────────────────────────────────────────────
def test_the_snapshot_carries_the_audio_spec(caplog):
    """① 레이트·채널이 그 통화 로그 안에 있다 — 나중에 보면 env 로도 못 복원한다."""
    session = cs.CascadeSession(_Sink(), object())
    assert "오디오=16000Hz/1ch" in _snapshot(session, caplog)


def test_a_declared_rate_is_marked_as_declared(caplog):
    """⭐ ② **선언과 가정을 가른다.** 이 구분이 없어서 반나절을 태웠다.

    같은 `16000` 이어도 클라가 말한 값과 우리가 찍은 값은 **증거로서 무게가 다르다**.
    """
    session = cs.CascadeSession(_Sink(), object())
    session._rate_declared = True
    assert "오디오=16000Hz/1ch(선언)" in _snapshot(session, caplog)

    session._rate_declared = False
    assert "오디오=16000Hz/1ch(가정)" in _snapshot(session, caplog)


def test_the_channel_count_shows_up(caplog):
    """채널도 같이 — 스테레오가 오면 타임라인이 정확히 2배가 된다(다운믹스 안 한다)."""
    session = cs.CascadeSession(_Sink(), object())
    session._channels = 2
    assert "/2ch" in _snapshot(session, caplog)


# ── ③④⑤ 자기점검 ───────────────────────────────────────────────────────────
def _warned(caplog) -> list:
    return [r for r in caplog.records if "오디오 시계가 안 맞는다" in r.getMessage()]


def test_audio_running_ahead_of_the_wall_clock_warns(caplog):
    """⭐⭐ ③ **실시간 마이크는 실시간보다 빠를 수 없다.** 넘으면 우리 자가 틀린 것이다."""
    session = cs.CascadeSession(_Sink(), object())
    session._t0 = time.monotonic() - 10.0      # 통화 10초 경과
    session._audio_ms = 20_000.0               # 그런데 받은 오디오는 20초 = 2.00배

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._check_audio_clock()

    warned = _warned(caplog)
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING, caplog.text
    assert "2.00배" in warned[0].getMessage(), warned[0].getMessage()


def test_a_normal_call_is_quiet(caplog):
    """⛔ ④ 정상까지 경고하면 아무도 경고를 안 본다."""
    session = cs.CascadeSession(_Sink(), object())
    session._t0 = time.monotonic() - 10.0
    session._audio_ms = 10_020.0               # 1.002배 — 프론트가 HAL 에서 잰 실제 값이다

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._check_audio_clock()

    assert not _warned(caplog), caplog.text


def test_less_audio_than_elapsed_is_not_an_error(caplog):
    """⛔ ④ **아래쪽은 정상이다** — 마이크 상시개방이 꺼져 있으면 비버가 말할 때 안 보낸다.

    양쪽을 다 경고하면 그 모드의 통화가 매번 시끄러워지고, 그러면 진짜 경고가 묻힌다.
    """
    session = cs.CascadeSession(_Sink(), object())
    session._t0 = time.monotonic() - 60.0
    session._audio_ms = 20_000.0               # 0.33배 — 절반 넘게 안 보냈다(정상)

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._check_audio_clock()

    assert not _warned(caplog), caplog.text


def test_the_warning_fires_once_per_call(caplog):
    """⑤ 프레임마다 찍으면 로그가 죽는다(오디오는 20ms 마다 온다)."""
    session = cs.CascadeSession(_Sink(), object())
    session._t0 = time.monotonic() - 10.0
    session._audio_ms = 30_000.0

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        for _ in range(5):
            session._check_audio_clock()

    assert len(_warned(caplog)) == 1, caplog.text


def test_a_short_sample_is_not_judged(caplog):
    """⚠ 개시 직후의 버스트로 판정하지 않는다 — 표본이 쌓인 뒤에 본다."""
    session = cs.CascadeSession(_Sink(), object())
    session._t0 = time.monotonic() - 0.1
    session._audio_ms = 1_000.0                # 10배지만 표본이 1초뿐이다

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._check_audio_clock()

    assert not _warned(caplog), caplog.text


# ── 클라가 채널을 **선언할 수 있어야** 한다 ──────────────────────────────────
def test_the_start_message_accepts_channels():
    """⛔ 선언할 방법이 없으면 "가정이 틀렸나"를 물을 수조차 없다.

    ⚠ 기본 1 이라 지금 클라의 바이트는 그대로다(클라 변경 0).
    """
    from domains.learning.realtime.cascade_protocol import ClientCascadeStart

    assert ClientCascadeStart().channels == 1
    assert ClientCascadeStart(channels=2).channels == 2
