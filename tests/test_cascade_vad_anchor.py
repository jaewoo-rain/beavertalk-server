"""턴 종료 시계를 **speech_end 에 건다**(VAD 앵커) — 임계값은 안 건드린다.

## 왜 (2026-08-13/14)
지금까지 시계는 **최종 전사가 도착한 시각**부터 800ms 를 셌다. 그 근거는 2026-08-07 주석이다:

    "최종 전사 지연 723~870ms > VAD 지연 291~348ms ⇒ VAD 로 빼면 턴이 빈 채 닫힌다"

⛔ 그런데 그 실측은 **Google STT v2** 것이다 — `core/openai_stt.py` 는 그 **사흘 뒤**에
  태어났다(`92d8c10`, 2026-08-10). 기본 엔진이 openai 로 뒤집힌 것도 그날(`7944fe8`).
  즉 **지금 엔진에서는 한 번도 검증된 적이 없는 부결**이었다.

## 새 실측 (사장님 실기기 408초, 15표본)
    전사확정(speech_end 도착 → 최종 전사 도착) = 중앙 320ms · **p95 430ms** · 최대 603ms
    pipeline_lag(순수 VAD 지연)              = 중앙 170ms (꼬리 512·528)
⇒ 바닥값을 **450ms**(p95 위)로 두면 앵커가 성립한다.

## 왜 임계값(800ms)을 안 내리나
⛔ 우리 사용자는 진짜 초보라 **문장 중간에 오래 쉰다.** 800→500 은 그 쉼을 문장 경계로
  오인해 말을 자른다. 앵커는 임계를 그대로 두고 **파이프라인 지연만** 걷어내므로
  자를 위험이 **0** 이다. 이 둘은 같은 300ms 가 아니다.

## 여기서 고정하는 성질
  ① 앵커가 켜진 엔진에서 speech_end 는 **자기 오프셋으로** 시계를 앞당긴다
  ② ⭐ **최종 전사도 같은 앵커를 쓴다** — 안 그러면 이득이 **0** 이다(전사에 오프셋이 없어
     시계가 거기서 다시 800ms 로 밀린다)
  ③ 바닥값은 **엔진 성질**에서 온다 — 전역 설정을 올려 구글까지 바꾸지 않는다
  ④ 바닥값은 **전사를 기다리는 동안만** 건다(글자가 손에 들어온 뒤엔 기다릴 이유가 없다)
  ⑤ ⛔ 모르는 엔진·구글은 **현행 그대로**(앵커 없음)
  ⑥ 성질은 **접두사**로 찾는다 — 모델 ID 가 바뀌어도 조용히 빠지지 않는다
  ⑦ 꼬리(전사 603ms)에서도 턴이 안 빈다
"""

import asyncio
import time

import pytest

import core.stt as stt_mod
import domains.learning.realtime.cascade_session as cs
from core.stt import SPEECH_BEGIN, SPEECH_END, TRANSCRIPT, SttV2Event

_OPENAI = "openai-gpt-4o-mini-transcribe"


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        return None

    def all(self, type_: str) -> list[dict]:
        return [e for e in self.events if e.get("type") == type_]


def _session(vendor: str = _OPENAI, silence_ms: int = 800) -> cs.CascadeSession:
    session = cs.CascadeSession(_Sink(), object())
    session._stt_vendor = vendor
    session._silence_ms = silence_ms
    session._audio_ms = 10_000.0
    session._turn_id = "u1"
    return session


def _wait_ms(session: cs.CascadeSession) -> int:
    """이번 arm 이 **얼마를 더 기다리기로 했나**(ms)."""
    return int(round((session._close_at - time.monotonic()) * 1000))


# ── ①③ speech_end 앵커 ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_speech_end_pulls_the_deadline_in():
    """⭐ ① VAD 지연(170ms)만큼 앞당긴다 — 임계 800ms 는 그대로다."""
    session = _session()
    await session._on_speech_end(
        SttV2Event(kind=SPEECH_END, offset_ms=int(session._audio_ms - 170))
    )
    assert 620 <= _wait_ms(session) <= 640, _wait_ms(session)
    assert session._anchor_saved_ms == 170, session._anchor_saved_ms


@pytest.mark.asyncio
async def test_without_the_anchor_the_full_threshold_is_waited():
    """⑤ ⛔ 구글·미상 엔진은 **건드리지 않는다** — 그 부결의 근거가 그쪽 실측이다."""
    for vendor in ("", "google-v2", "fake"):
        session = _session(vendor=vendor)
        await session._on_speech_end(
            SttV2Event(kind=SPEECH_END, offset_ms=int(session._audio_ms - 170))
        )
        assert 790 <= _wait_ms(session) <= 810, (vendor, _wait_ms(session))
        assert session._anchor_saved_ms == 0, vendor


# ── ② 전사도 같은 앵커를 쓴다(이게 없으면 이득이 0) ────────────────────────
@pytest.mark.asyncio
async def test_the_final_transcript_does_not_push_the_deadline_back():
    """⭐⭐ ② **이 한 줄이 이득의 전부다.**

    이 엔진의 전사에는 오프셋이 없다. 앵커가 없으면 최종 전사가 도착하는 순간 시계가
    **거기서 다시 800ms** 로 밀려, speech_end 에서 앞당긴 만큼이 통째로 사라진다.
    우리가 기다리는 침묵은 **말이 끝난 지점**부터지 전사가 도착한 시각부터가 아니다.
    """
    session = _session()
    end_offset = int(session._audio_ms - 170)
    await session._on_speech_end(SttV2Event(kind=SPEECH_END, offset_ms=end_offset))

    # 320ms 뒤(중앙값) 최종 전사가 온다 — 그동안 오디오도 320ms 더 흘렀다.
    session._audio_ms += 320
    await session._on_transcript(SttV2Event(kind=TRANSCRIPT, text="안녕하세요", is_final=True))

    # 말 끝난 지점 기준 800ms 라면, 여기서 남는 건 800 − (170+320) = 310ms 다.
    assert 300 <= _wait_ms(session) <= 320, _wait_ms(session)
    assert session._anchor_saved_ms == 490, session._anchor_saved_ms


# ── ③④ 바닥값 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_floor_protects_the_late_transcript():
    """③ VAD 가 늦게 알려줘도 **전사가 올 시간은 남긴다**(바닥 450ms = 실측 p95 위)."""
    session = _session()
    await session._on_speech_end(
        SttV2Event(kind=SPEECH_END, offset_ms=int(session._audio_ms - 600))
    )
    assert 440 <= _wait_ms(session) <= 460, _wait_ms(session)


@pytest.mark.asyncio
async def test_the_engine_floor_is_dropped_once_the_text_is_in_hand():
    """④ 글자가 이미 왔으면 더 기다릴 이유가 없다 — 여기서도 걸면 바닥이 이득을 도로 먹는다.

    ⚠ 전역 바닥(CASCADE_TURN_MIN_WAIT_MS=250ms)은 그대로 남는다(유령 턴 방어).
    """
    session = _session()
    await session._on_speech_end(
        SttV2Event(kind=SPEECH_END, offset_ms=int(session._audio_ms - 700))
    )
    assert 440 <= _wait_ms(session) <= 460, "전사 전에는 엔진 바닥이 걸려야 한다"

    await session._on_transcript(SttV2Event(kind=TRANSCRIPT, text="네", is_final=True))
    assert 240 <= _wait_ms(session) <= 260, _wait_ms(session)


def test_the_engine_floor_does_not_leak_into_the_global_setting():
    """③ ⛔ 전역 `CASCADE_TURN_MIN_WAIT_MS` 를 450 으로 올려 **구글까지 바꾸지 않는다**."""
    assert cs.settings.CASCADE_TURN_MIN_WAIT_MS == 250
    assert cs._stt_profile_for("google-v2").final_after_end_ms == 0


# ── ⑥ 성질 표 ───────────────────────────────────────────────────────────────
def test_the_profile_is_matched_by_prefix_not_by_exact_name():
    """⭐ ⑥ 모델 ID 가 바뀌어도 **조용히 빠지지 않는다**.

    ⛔ 완전일치로 두면 `OPENAI_STT_MODEL` 을 바꾸는 순간 표에서 빠져 앵커가 꺼지고,
      아무도 모른 채 턴이 다시 320ms 느려진다(우리가 두 번 데인 '이름으로 비교').
    """
    assert cs._stt_profile_for(_OPENAI).anchor_on_speech_end
    assert cs._stt_profile_for("openai-gpt-5-transcribe-next").anchor_on_speech_end
    assert not cs._stt_profile_for("").anchor_on_speech_end
    assert not cs._stt_profile_for("some-new-vendor").anchor_on_speech_end


def test_the_openai_floor_sits_above_the_measured_p95():
    """바닥값의 근거는 **실측 p95(430ms)** 다 — 중앙값(320)으로 잡으면 꼬리에서 진다."""
    assert cs._stt_profile_for(_OPENAI).final_after_end_ms >= 430


# ── ⑦ 실제 턴: 꼬리에서도 안 빈다 ──────────────────────────────────────────
async def _drive(session, script) -> None:
    """⚠ 자는 동안 **오디오 시계도 같이 흐르게** 한다.

    실통화에서는 마이크가 계속 흐르므로 `_audio_ms` 가 실시간으로 는다. 안 흐르게 두면
    "이미 지난 침묵"이 영영 안 자라서 앵커가 **실제보다 늦게** 닫는 것처럼 보인다 —
    시험 리그가 계측의 전제를 깨뜨리는 그 계열이다.
    """
    pump = asyncio.create_task(session._pump_turn())
    for item in script:
        if isinstance(item, float):
            await asyncio.sleep(item)
            session._audio_ms += item * 1000.0
            continue
        item.at = time.monotonic()
        await session._q.put(item)
    await asyncio.sleep(0.05)
    pump.cancel()
    with pytest.raises((asyncio.CancelledError, Exception)):
        await pump


@pytest.mark.asyncio
async def test_the_slowest_observed_transcript_still_lands_in_its_turn(monkeypatch):
    """⑦ 최악 표본(603ms)에서도 턴이 **빈 채로** 닫히지 않는다.

    ⚠ 성립 조건을 정직하게 적는다: 마감은 `max(800 − VAD지연, 450)` 이므로 603ms 전사를
      덮으려면 **VAD 지연 ≤ 197ms** 여야 한다(실측 중앙 170ms). 그 밖(꼬리 512·528ms)에서는
      빈 턴으로 닫히고 늦은 전사가 새 턴을 연다 — 대답은 나가지만 한 박자 늦는다.
      ⛔ 그 꼬리까지 덮으려고 바닥을 600 으로 올리면 **버는 것(320ms)이 사라진다.**
    """
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_SILENCE_MS", 800)
    session = cs.CascadeSession(_Sink(), object())
    session._stt_vendor = _OPENAI
    session._silence_ms = 800
    session._audio_ms = 10_000.0
    audio = session._audio_ms

    await _drive(session, [
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(audio - 2000)),
        SttV2Event(kind=SPEECH_END, offset_ms=int(audio - 170)),
        0.603,                                   # 최악 표본만큼 늦게 온 최종 전사
        SttV2Event(kind=TRANSCRIPT, text="안녕하세요", is_final=True),
        0.5,
    ])

    ends = session.transport.all("user_turn_end")
    assert len(ends) == 1, session.transport.events
    assert ends[0]["text"] == "안녕하세요", "꼬리 전사가 자기 턴에 못 실렸다(빈 턴)"
