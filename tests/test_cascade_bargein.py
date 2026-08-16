"""barge-in 판정 회귀 — **말 끊기는 글자로 친다. 소리로는 안 친다.**

2026-08-08 실통화가 두 규칙을 동시에 깼다.

  ① 취소 3건이 **전부** `확정(음성 지속)` 이었다 — 전사가 한 글자도 없는데 "1.2초 이어졌으니
     잡음은 아니겠지"로 비버를 죽였다(들린글자 4·13·29자에서 잘렸다). 그런데 실측은 반대다:
         전사로 확정된 건들의 보류→확정   476 · 529 · 539 · 620ms
         파이프라인 지연                  723 ~ 914ms
     **진짜 말이면 전사가 항상 먼저 이긴다.** 1.2초까지 글자가 없었으면 그건 잡음이다.
     → 2026-08-10 그 우회로를 **통째로 없앴다**(사장님 지시). 남은 3.5초는 "끊는 시간"이
       아니라 **보류 유효기간**이다 — 그 안에 전사가 없으면 기각한다.

  ② 같은 통화에서 barge-in 기각 17건이 `에너지 < 임계` 였다. 임계를 발화 분포에 맞춰 올린
     결과 **에코 필터가 아니라 발화 필터**가 돼 있었다. 이 관문의 일은 "사용자가 말했나"가
     아니라 "이게 비버 자기 목소리인가"다(_bargein_allowed 독스트링의 원래 의도).
     → AEC 를 선언한 세션에서는 **아예 돌리지 않는다**. 미선언이면 에코 잔여(0.003) 위,
       실측 발화 하단(0.011) 아래에 긋는다.

여기서 고정하는 성질:
  ⓐ 전사가 오면 끊는다 / **소리만으로는 어떤 에너지에서도 안 끊는다**
  ⓑ STT 가 먹통이면 비버를 죽이는 게 아니라 **턴이 스스로 닫힌다**(dead air 방지는 그쪽 몫)
  ⓒ AEC 선언 = 에너지 게이트 off / 미선언·미상 = on(안전 쪽)
  ⓓ 임계는 **값이 아니라 위치**로 고정한다(잔여 에코 위, 발화 하단 아래)
  ⓔ 판정 로그가 사후 분석에 필요한 것을 들고 있다(에너지·대기 ms·분포 요약)
"""

import asyncio
import time

import pytest

import core.stt as stt_mod
from core.config import settings
from core.stt import SPEECH_BEGIN, TRANSCRIPT, SttV2Event
from domains.learning.realtime.cascade_session import CascadeInbound, CascadeSession, TurnState

# 실통화 실측 — 이 숫자들이 규칙의 근거다.
LIVE_TRANSCRIPT_CONFIRM_MS = 620      # 전사로 확정된 건들의 최댓값
LIVE_PIPELINE_LAG_MS = 914            # 파이프라인 지연 최댓값
LIVE_ECHO_MAX = 0.0030                # 비버 재생 중 잔여 에코 상단
LIVE_SPEECH_MIN = 0.0110              # 실제 발화 에너지 하단


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self) -> CascadeInbound:
        await asyncio.sleep(3600)
        raise AssertionError("이 테스트는 receive 를 쓰지 않는다")


def _session(monkeypatch, *, rms: float, aec=None) -> tuple[CascadeSession, list]:
    """비버가 말하는 중이고 사용자가 소리를 냈다 — barge-in 판정 직전 상태."""
    monkeypatch.setattr(settings, "CASCADE_MIC_ALWAYS_OPEN", True)
    # ⚠ 예전엔 여기서 최소지속 관문을 0 으로 껐다. 그 관문은 2026-08-14 에 **삭제**됐다 —
    #   끌 것이 없으니 이 줄도 사라졌다(사유는 `_bargein_allowed` 주석).
    session = CascadeSession(_Sink())
    session._apply_aec_hint(aec)
    session.state = TurnState.BEAVER_SPEAKING
    session._audio_ms = 10_000.0
    session._rms_at = lambda offset_ms: rms          # 그 오디오 지점의 에너지(실측 대역)
    session._audible_ms = lambda: 5_000              # 비버는 충분히 들렸다
    cuts: list = []

    async def _cut(event):
        cuts.append(event)

    session._on_barge_in = _cut
    return session, cuts


def _begin(session: CascadeSession) -> SttV2Event:
    return SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(session._audio_ms - 100))


# ── ⓐ 전사로만 끊는다 ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_transcript_cuts_beaver(monkeypatch):
    """글자가 오면 끊는다 — 이게 유일한 정상 경로다."""
    session, cuts = _session(monkeypatch, rms=0.02)
    await session._on_speech_begin(_begin(session))
    assert session._bargein_at is not None, "보류 상태로 들어가야 한다"
    assert not cuts, "보류 시점에는 아직 안 끊는다"
    await session._on_transcript(
        SttV2Event(kind=TRANSCRIPT, text="잠깐만요", offset_ms=int(session._audio_ms))
    )
    assert len(cuts) == 1
    assert ("전사확정", 0.02) in session._bargein_obs


@pytest.mark.asyncio
async def test_sound_alone_does_not_cut_beaver(monkeypatch):
    """⛔ **소리만으로는 안 끊는다.** 잡음이 비버를 죽이던 경로가 이것이다.

    보류가 걸린 뒤 실측 전사 지연(914ms)의 두 배가 지나도 글자가 없으면 그대로 보류다.
    """
    session, cuts = _session(monkeypatch, rms=0.02)
    await session._on_speech_begin(_begin(session))
    await asyncio.sleep(LIVE_PIPELINE_LAG_MS * 2 / 1000.0)
    assert not cuts, "글자 없이 소리만으로 끊겼다 — 08-08 결함 재발"
    assert session._bargein_at is not None, "보류는 유효기간까지 유지된다"


def test_pending_window_outlives_normal_transcript_delay():
    """⭐ 보류 유효기간은 **정상 전사가 도착할 시간**보다 넉넉해야 한다.

    짧으면 진짜 끼어들기가 기각된다(전사가 도착하기 전에 보류가 만료된다).
    ⛔ 길어도 이제 위험하지 않다 — 이 시간이 하는 일은 **기각**뿐이다(끊지 않는다).
    """
    assert settings.CASCADE_BARGEIN_PENDING_MS > LIVE_PIPELINE_LAG_MS * 3, (
        "전사가 도착하기 전에 보류가 만료된다 — 진짜 끼어들기가 죽는다"
    )
    assert settings.CASCADE_BARGEIN_PENDING_MS > LIVE_TRANSCRIPT_CONFIRM_MS * 4


# ── ⓐ-2 최소지속 관문을 지운 자리(2026-08-14) ──────────────────────────────
@pytest.mark.asyncio
async def test_a_very_recent_speech_start_still_reaches_the_transcript_gate(monkeypatch):
    """⭐⭐ **지운 관문이 뒤 관문을 죽이고 있었다** — 그게 이 삭제의 핵심 근거다.

    예전 ② 최소 지속 관문은 `audio_ms − offset < 400ms` 면 **`return False`** 했다. 그러면
    `_bargein_at` 이 안 잡히고, 나중에 두 글자가 와도 **끊을 대상이 없다** — 앞 관문이 뒤
    관문(전사 확인)을 무력화하는 구조였다. 증상은 「짧게 끼어들면 안 끊긴다」로 나타나
    원인 찾기가 어렵다.
    ⇒ 이제 speech_begin 이 방금 시작한 지점을 가리켜도 **보류로 넘어가고**, 전사가 결정한다.
    """
    session, cuts = _session(monkeypatch, rms=0.02)
    # 방금 말을 시작했다(오디오가 50ms 밖에 안 흘렀다) — 예전이라면 여기서 통째로 기각됐다.
    just_now = SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(session._audio_ms - 50))

    await session._on_speech_begin(just_now)

    assert session._bargein_at is not None, "짧게 시작했다고 전사 확인 기회조차 없앴다"
    assert not cuts, "보류 시점에는 아직 안 끊는다(전사가 결정한다)"
    assert not [o for o, _ in session._bargein_obs if o == "기각-지속"], session._bargein_obs

    await session._on_transcript(
        SttV2Event(kind=TRANSCRIPT, text="잠깐만요", offset_ms=int(session._audio_ms))
    )
    assert len(cuts) == 1, "두 글자가 왔는데도 못 끊었다 — 무력화가 남아 있다"


@pytest.mark.asyncio
async def test_a_short_blip_without_words_still_never_cuts(monkeypatch):
    """⛔ 지운 관문이 지키려던 성질은 **그대로 살아 있다** — 다만 지키는 주체가 다르다.

    「순간 튐으로 비버를 끊지 않는다」는 이제 **에너지 게이트 + 전사 확인**이 지킨다.
    소리만 짧게 나고 글자가 안 오면 보류는 유효기간에 **기각**으로 끝난다.
    ⚠ 이 시험이 있는 한 "에코 방어가 약해졌다"는 걱정에 코드로 답할 수 있다.
    """
    session, cuts = _session(monkeypatch, rms=0.02)
    await session._on_speech_begin(
        SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(session._audio_ms - 50))
    )
    # 글자는 끝내 안 온다(기침·키보드·잔여 에코).
    await asyncio.sleep(LIVE_PIPELINE_LAG_MS * 2 / 1000.0)

    assert not cuts, "글자 없이 짧은 소리로 끊겼다"


def test_the_min_duration_gate_is_gone_not_disabled(monkeypatch):
    """⛔ **0 으로 끈 게 아니라 지웠다.** 설정이 남아 있으면 다음 사람이 이유도 모르고 되올린다.

    ⚠ 이 시험의 주제는 값이 아니라 **부재**다 — 오늘 우리가 `silence_duration_ms` 에서
      당한 게 정확히 그 계열이다(아무도 설정한 적 없는 값을 아무도 모르고 있었다).
    """
    assert not hasattr(settings, "CASCADE_BARGEIN_MIN_MS"), (
        "삭제한 관문의 설정이 되살아났다 — 되살리려면 전사 확인 **뒤로** 옮겨라"
    )
    # 살아 있는 방어 셋은 그대로다.
    assert settings.CASCADE_BARGEIN_MIN_AUDIBLE_MS > 0
    assert settings.CASCADE_BARGEIN_RMS > 0
    assert settings.CASCADE_BARGEIN_MIN_CHARS >= 1


# ── ⓑ STT 장애 안전망 ────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("rms", [0.0077, 0.02, 0.44])
async def test_no_transcript_never_cuts_at_any_energy(monkeypatch, rms):
    """⛔⛔ **전사가 없으면 어떤 에너지에서도 안 끊는다**(2026-08-10 사장님 지시).

    예전엔 음성이 이어지면 "안전망"으로 끊었다. 그 판단이 통화를 죽였다 —
    rms=**0.0077**(침묵 0.0000~0.0030 과 발화 0.011~0.44 사이 회색지대)에서 비버를 죽였고,
    그 뒤 턴까지 굳어 30초 완전 침묵이 됐다. 막으려던 것보다 나쁜 결과였다.
    ⭐ STT 먹통 대응은 이제 **턴이 스스로 닫히는 것**이 맡는다(_turn_idle_at).
    """
    monkeypatch.setattr(settings, "CASCADE_BARGEIN_PENDING_MS", 120)
    session, cuts = _session(monkeypatch, rms=rms)
    await session._on_speech_begin(_begin(session))
    session._speech_active = True                     # 음성은 계속 이어지는데 글자가 없다
    pump = asyncio.create_task(session._pump_turn())
    await asyncio.sleep(0.3)
    pump.cancel()
    assert not cuts, "전사 없이 끊었다 — 08-10 결함 재발"
    assert [o for o, _ in session._bargein_obs] == ["보류만료"]


@pytest.mark.asyncio
async def test_pending_expires_as_noise_when_voice_stops(monkeypatch):
    """소리가 멎고 글자도 안 나왔다 = 잡음. 끊지 않고 표본으로만 남는다."""
    monkeypatch.setattr(settings, "CASCADE_BARGEIN_PENDING_MS", 120)
    session, cuts = _session(monkeypatch, rms=0.02)
    await session._on_speech_begin(_begin(session))
    session._speech_active = False
    pump = asyncio.create_task(session._pump_turn())
    await asyncio.sleep(0.3)
    pump.cancel()
    assert not cuts
    assert [o for o, _ in session._bargein_obs] == ["보류만료"]


# ── ⓒ AEC 선언이 에너지 게이트를 가른다 ──────────────────────────────────────
@pytest.mark.asyncio
async def test_aec_declared_skips_energy_gate(monkeypatch):
    """AEC 선언 세션: **에너지가 낮아도** 전사가 오면 끊는다.

    AEC 가 막고 있으면 에코는 애초에 전사되지 않는다(08-08 로그: 재생 중 전사 0건).
    막을 대상이 없는 관문이라, 돌리면 진짜 발화만 걸린다.
    """
    session, cuts = _session(monkeypatch, rms=0.0005, aec={"mode": "hw"})
    assert session._energy_gate is False
    await session._on_speech_begin(_begin(session))
    assert session._bargein_at is not None, "에너지로 기각당했다 — 게이트가 안 꺼졌다"
    await session._on_transcript(
        SttV2Event(kind=TRANSCRIPT, text="아니요", offset_ms=int(session._audio_ms))
    )
    assert len(cuts) == 1


@pytest.mark.asyncio
async def test_no_aec_keeps_energy_gate(monkeypatch):
    """미선언 세션(=지금 플러터 앱): 에너지 게이트가 여전히 적용된다."""
    session, cuts = _session(monkeypatch, rms=LIVE_ECHO_MAX / 2, aec=None)
    assert session._energy_gate is True
    await session._on_speech_begin(_begin(session))
    assert session._bargein_at is None, "잔여 에코가 보류까지 갔다"
    assert not cuts
    assert [o for o, _ in session._bargein_obs] == ["기각-에너지"]


@pytest.mark.parametrize("aec", [
    {"mode": "moon-cannon"},   # 모르는 값
    {"mode": None},
    {},                        # mode 자체가 없다
    "hw",                      # dict 가 아니다
    None,                      # 미선언(지금 앱)
])
def test_unknown_aec_falls_back_to_gate_on(monkeypatch, aec):
    """⛔ **모르는 값이 방어를 끄면 안 된다** — 전부 안전 쪽(게이트 켬)으로 떨어진다."""
    session = CascadeSession(_Sink())
    session._apply_aec_hint(aec)
    assert session._energy_gate is True


@pytest.mark.parametrize("mode", ["hw", "HW", " Hw ", "headset", "browser"])
def test_declared_aec_modes_disable_gate(monkeypatch, mode):
    """화이트리스트는 **명시적**이고, 대소문자·공백은 정규화한다(`mode:'HW'` 오타 차단)."""
    session = CascadeSession(_Sink())
    session._apply_aec_hint({"mode": mode})
    assert session._energy_gate is False


def test_aec_hint_never_selects_immediate_mode():
    """⛔ 이어폰이어도 **글자 없이는 안 끊는다.**

    예전에는 `headset` 이 확인 방식을 `immediate`(speech_begin 하나로 즉시 취소)로 올렸다.
    "에코가 없다"는 "글자 없이 끊어도 된다"가 아니다 — 기침·문 닫는 소리도 에코가 아니다.
    """
    for mode in ("headset", "hw", "none", "unknown"):
        session = CascadeSession(_Sink())
        session._apply_aec_hint({"mode": mode})
        assert session._bargein_confirm == "transcript", mode


# ── ⓓ 임계·상한은 값이 아니라 위치로 고정한다 ───────────────────────────────
def test_energy_threshold_sits_between_echo_and_speech():
    """⭐ 잔여 에코 위, 실측 발화 하단 아래. **누가 0.05 로 되돌리면 여기서 잡힌다.**"""
    assert settings.CASCADE_BARGEIN_RMS > LIVE_ECHO_MAX, "잔여 에코가 통과한다"
    assert settings.CASCADE_BARGEIN_RMS < LIVE_SPEECH_MIN, (
        "에코 필터가 아니라 발화 필터가 됐다 — 08-08 기각 17건의 상태"
    )


def test_turn_max_bounds_noise_turns_without_cutting_real_speech():
    """턴 상한 = **글자가 한 번도 안 나온 잡음 턴**의 바운드다(전사 기준 종료가 못 닿는 자리).

    ⛔ 짧게 깎을 수 없다: 어학 앱이라 학습자가 진짜로 길게 말한다.
    """
    assert settings.CASCADE_TURN_MAX_S >= 15, "학습자가 길게 말하면 잘린다"
    assert settings.CASCADE_TURN_MAX_S <= 35, "잡음 턴이 이만큼 굳으면 통화가 죽은 것처럼 보인다"
    # 정상 종료(전사 정지)가 항상 먼저 닫아야 한다 — 상한은 마지막 안전망이다.
    assert settings.CASCADE_TURN_MAX_S * 1000 > settings.CASCADE_TURN_TRANSCRIPT_SILENCE_MS * 5


# ── ⓔ 로그가 사후 분석 재료를 들고 있다 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_judgment_logs_carry_energy_and_wait(monkeypatch, caplog):
    """보류·확정 줄에 **에너지와 보류→확정 ms** 가 있어야 한다.

    없을 때는 두 줄의 타임스탬프를 사람이 손으로 빼서 분석했다(620/476/539/529ms).
    같은 계산을 매번 손으로 하게 두지 않는다.
    """
    caplog.set_level("INFO")
    session, _ = _session(monkeypatch, rms=0.0123)
    await session._on_speech_begin(_begin(session))
    await session._on_transcript(
        SttV2Event(kind=TRANSCRIPT, text="잠깐", offset_ms=int(session._audio_ms))
    )
    hold = next(m for m in caplog.messages if "barge-in 보류" in m)
    confirm = next(m for m in caplog.messages if "barge-in 확정" in m)
    assert "rms=0.0123" in hold and "게이트=on" in hold
    assert "보류→확정" in confirm and "rms=0.0123" in confirm


@pytest.mark.asyncio
async def test_session_end_summarizes_energy_by_outcome(monkeypatch, caplog):
    """통화당 한 줄 요약 — 분류는 **에너지가 아니라 결과**로 한다.

    임계로 갈라낸 표본으로 임계를 정하면 순환논법이다(그렇게 모은 표본이 "발화 최저 0.0110"
    을 만들었는데, 그건 기각된 것들만 모인 값이었다).
    """
    caplog.set_level("INFO")
    session, _ = _session(monkeypatch, rms=0.02)
    session._bargein_obs = [("전사확정", 0.02), ("전사확정", 0.05), ("보류만료", 0.001)]
    session._log_bargein_summary()
    line = next(m for m in caplog.messages if "barge-in 요약" in m)
    assert "전사확정 2건 0.0200/0.0500/0.0500" in line
    assert "보류만료 1건" in line
    assert "게이트=on" in line


def test_summary_never_breaks_a_call(monkeypatch, caplog):
    """R5 — 계측이 통화를 죽이지 않는다."""
    caplog.set_level("INFO")
    session = CascadeSession(_Sink())
    session._bargein_obs = [("망가진표본", None)]   # type: ignore[list-item]
    session._log_bargein_summary()                  # 예외가 밖으로 나가면 안 된다
    assert any("요약 실패" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_turn_close_log_carries_open_age(monkeypatch, caplog):
    """턴 종료 줄에 **언제 열렸는지**가 있어야 한다.

    08-08 u7(`reason=max speech_ms=1151`)을 로그만으로 못 갈랐던 이유가 이것이다 — 턴이
    열린 시각이 어디에도 없어서 "30초를 열려 있었나, 1초 만에 죽었나"를 확정할 수 없었다.
    """
    caplog.set_level("INFO")
    session = CascadeSession(_Sink())
    session._turn_id = "u1"
    session._turn_began_at = time.monotonic() - 3.0
    session._last_voice_at = time.monotonic() - 1.0
    session._finals = ["안녕하세요"]
    await session._close_turn("max")
    line = next(m for m in caplog.messages if "cascade turn:" in m)
    assert "열림=3.0초전" in line and "마지막음성=1.0초전" in line
