"""`전사확정=` — **speech_end 도착 → 최종 전사 도착**. 앵커 결정이 이 값 하나에 달렸다.

## 왜 (2026-08-13)
턴 종료 타이머는 지금 **전사 오프셋으로만** 이미 흘러간 침묵을 뺀다(`_arm_close_timer`).
그 결정의 근거는 2026-08-07 주석이다:

    "최종 전사 지연 723~870ms > VAD 이벤트 지연 291~348ms
     ⇒ VAD 기준으로 빼면 전사가 그 뒤에 도착해 턴이 **빈 채로** 닫힌다"

⛔ 그런데 그 실측은 **Google STT v2** 값이다 — `core/openai_stt.py` 의 최초 커밋은
  `92d8c10`(2026-08-10)로 그 사흘 **뒤**이고, 기본 엔진이 openai 로 뒤집힌 것도 같은 날
  (`7944fe8`)이다. 즉 **지금 엔진에서는 잰 적이 없다.**

⭐ 앵커를 VAD 로 옮기면 임계값(800ms)을 **안 건드리고** 대기가 줄어든다 — 문장 중간에서
  턴을 자를 위험이 없다(우리 사용자는 진짜 초보라 말하다 오래 쉰다). 다만 옮기는 순간
  남는 안전망은 바닥값(`CASCADE_TURN_MIN_WAIT_MS`) **하나뿐**이라
  **바닥 ≥ 전사확정 지연(p95)** 이어야 성립한다. 그 분포를 만들라고 넣는 값이다.
⚠ 중앙값으로 정하면 꼬리에서 진다 — 그게 2026-08-07 의 그 결함이다.

여기서 고정하는 성질:
  ① speech_end 뒤 **첫** 최종 전사까지를 잰다
  ② 못 쟀으면 **-1**(0 이 아니다 — 0 은 "즉시 왔다"로 읽힌다)
  ③ **턴마다** 새로 잰다(앞 턴 값이 새 턴에 묻어가지 않는다)
  ④ 뒤따르는 최종 전사가 **낡은 기준**으로 다시 재지 않는다
"""

import asyncio
import logging
import time

import pytest

import core.stt as stt_mod
from core.stt import SPEECH_BEGIN, SPEECH_END, TRANSCRIPT, SttV2Event
from domains.learning.realtime.cascade_session import CascadeSession

_LOGGER = "domains.learning.realtime.cascade_session"


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None


async def _drive(session: CascadeSession, script) -> None:
    """⚠ **도착 시각을 넣는 순간에 찍는다.**

    `SttV2Event.at` 은 기본값이 생성 시각인데, 대본은 한 번에 만들어진다 — 그대로 두면 모든
    이벤트의 `at` 이 같아서 **지연이 항상 0** 으로 나온다(내가 처음 이렇게 짰고, 회귀가
    "0ms" 를 통과시킬 뻔했다). 실제로는 어댑터가 **도착할 때** 이벤트를 만든다.
    """
    pump = asyncio.create_task(session._pump_turn())
    for item in script:
        if isinstance(item, float):
            await asyncio.sleep(item)
            continue
        item.at = time.monotonic()
        await session._q.put(item)
    await asyncio.sleep(0.05)
    pump.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await pump


def _session(monkeypatch, silence_ms=60, floor_ms=30) -> CascadeSession:
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_SILENCE_MS", silence_ms)
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_MIN_WAIT_MS", floor_ms)
    session = CascadeSession(_Sink())
    session._silence_ms = silence_ms
    session._audio_ms = 10_000.0
    return session


def _lags(caplog) -> list[int]:
    """턴 로그에서 `전사확정=` 값만 뽑는다 — 이 줄이 곧 분석 재료다."""
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if msg.startswith("cascade turn:") and "전사확정=" in msg:
            out.append(int(msg.split("전사확정=")[1].split("ms")[0]))
    return out


@pytest.mark.asyncio
async def test_the_lag_from_speech_end_to_the_final_transcript_is_logged(monkeypatch, caplog):
    """⭐⭐ ① 이 값이 없으면 앵커를 옮길지 **판단할 재료가 없다**."""
    session = _session(monkeypatch, silence_ms=300, floor_ms=250)
    audio = session._audio_ms
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _drive(session, [
            SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 2000)),
            SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 500)),
            0.15,                                    # 벤더가 전사를 확정하기까지
            SttV2Event(kind=TRANSCRIPT, text="안녕하세요", is_final=True),
            0.5,
        ])

    lags = _lags(caplog)
    assert lags, caplog.text
    assert 100 <= lags[0] <= 400, f"speech_end→최종전사 를 못 재고 있다 — {lags}"


@pytest.mark.asyncio
async def test_an_unmeasured_lag_is_minus_one(monkeypatch, caplog):
    """⛔ ② 못 쟀으면 **-1**. 0 으로 적으면 "전사가 즉시 왔다"는 **거짓 근거**가 된다.

    VAD END 없이 전사만 오는 경로가 실제로 있다(엔진·잡음 환경). 그 턴을 0 으로 세면
    분포의 하단이 통째로 조작된다 — 그 분포로 바닥값을 정하면 통화가 깨진다.
    """
    session = _session(monkeypatch)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _drive(session, [
            SttV2Event(kind=TRANSCRIPT, text="안녕", is_final=True),
            0.3,
        ])

    assert _lags(caplog) == [-1], caplog.text


@pytest.mark.asyncio
async def test_each_turn_measures_its_own_lag(monkeypatch, caplog):
    """③ 앞 턴 값이 새 턴에 **묻어가면 안 된다** — 그러면 분포가 앞 턴으로 오염된다."""
    # ⚠ 임계가 전사 도착보다 짧으면 첫 턴이 **빈 채로** 닫힌다(그게 앵커 논쟁의 핵심이다).
    #   여기서 보려는 건 그 결함이 아니라 값의 격리라, 임계를 넉넉히 준다.
    session = _session(monkeypatch, silence_ms=400, floor_ms=300)
    audio = session._audio_ms
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _drive(session, [
            SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 2000)),
            SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 900)),
            0.1,
            SttV2Event(kind=TRANSCRIPT, text="첫 발화", is_final=True),
            0.6,                                     # 첫 턴이 닫힌다
            # 두 번째 턴은 speech_end 없이 전사만 온다 → 못 쟀다(-1)
            SttV2Event(kind=TRANSCRIPT, text="두 번째 발화", is_final=True,
                       offset_ms=int(audio + 500)),
            0.6,
        ])

    lags = _lags(caplog)
    assert len(lags) == 2, caplog.text
    assert lags[0] >= 50, lags
    assert lags[1] == -1, f"앞 턴의 값이 새 턴에 묻어갔다 — {lags}"


@pytest.mark.asyncio
async def test_a_later_final_does_not_remeasure_from_a_stale_anchor(monkeypatch, caplog):
    """④ 뒤따르는 최종 전사는 **다시 재지 않는다** — 재면 값이 부풀어 바닥값을 과대 산정한다.

    우리가 기다리는 것은 speech_end 뒤 **첫** 전사다. 두 번째 것까지 세면 "전사가 느리다"는
    잘못된 결론이 나오고, 그 결론은 앵커 이동을 **부당하게 부결**시킨다.
    """
    session = _session(monkeypatch, silence_ms=800, floor_ms=600)
    audio = session._audio_ms
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        await _drive(session, [
            SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 2000)),
            SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 900)),
            0.1,
            SttV2Event(kind=TRANSCRIPT, text="앞", is_final=True),
            0.3,
            SttV2Event(kind=TRANSCRIPT, text="앞 뒤", is_final=True),
            0.8,
        ])

    lags = _lags(caplog)
    assert lags and lags[0] < 300, f"두 번째 최종 전사로 다시 쟀다 — {lags}"
