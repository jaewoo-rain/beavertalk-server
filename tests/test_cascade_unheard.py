"""아무도 안 들은 대답은 버린다 — 2026-08-08 사장님 증상 회귀.

증상: "음성이 끊겼으면 삭제돼야 하는데 계속 나온다."
실통화(00133) 로그가 원인을 그대로 보여줬다.

    08:15:23.6  turn u14 → 발화 대기열 — 비버가 말하는 중이라 대답 뒤로 미룬다
    08:15:24.4  barge-in 기각 — 비버가 아직 안 들린다(들린 0ms < 300ms)
    08:15:25.9  barge-in 기각 — 비버가 아직 안 들린다(들린 0ms < 300ms)
    08:15:34.9  대답 b9  첫소리=6485ms            ← 11초 뒤에야 소리가 난다

`_audible_ms()` 는 비버 턴이 아직 없으면(THINKING = LLM 생성 중) **항상 0** 이다. 실측 첫소리가
3.5~8초라 그동안은 ①barge-in 이 "안 들림"으로 기각되고 ②발화는 대기열로 밀리고 ③비버는
아무도 안 듣는 대답을 끝까지 하고 ④그 뒤에 **낡은 말**에 답했다.

⛔ 300ms 관문 자체는 옳다(소리도 나기 전에 죽이면 dead air 가 난다 — 45분 통화에서 취소
  14건 중 7건이 그랬고 그 뒤가 전부 빈 턴이었다). 틀린 건 **그다음 처리**다:
    지금    안 들림 → 대답 살림 → 대기열 → 낡은 대답 먼저
    맞는것  안 들림 → 아무도 못 들었다 → **버리고** 새 말에 답한다(손해 0)

여기서 고정하는 성질:
  ① 안 들린 대답은 버려지고 새 발화가 곧바로 답을 받는다
  ② **이미 들린 대답은 대기열 그대로**(2026-08-07 근거를 깨지 않는다)
  ③ 버릴 때 **클라 버퍼도 지운다** — 안 지우면 버린 대답이 그대로 재생된다(증상 자체)
  ④ 배치 합성 중에는 버리지 않는다(그 모드는 20초를 통째로 합성한 뒤 소리를 낸다)
  ⑤ 확정된 barge-in 을 '안 들림'으로 **다시** 재지 않는다
"""

import asyncio

import pytest

import domains.learning.realtime.cascade_session as cs
from core.config import settings
from core.stt import SPEECH_BEGIN, SttV2Event
from domains.learning.realtime.cascade_session import CascadeInbound, CascadeSession, TurnState


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.audio = 0

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        self.audio += len(frame)

    async def receive(self) -> CascadeInbound:
        await asyncio.sleep(3600)
        raise AssertionError("이 테스트는 receive 를 쓰지 않는다")

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]


class _StubGroup:
    """TaskGroup 대역 — 새 대답이 **시작됐는지**만 본다."""

    def __init__(self) -> None:
        self.started = 0

    def create_task(self, coro):
        self.started += 1
        coro.close()
        return None


async def _rig(audible_ms: int) -> tuple[CascadeSession, _StubGroup, asyncio.Task]:
    """대답 하나가 흐르는 중인 세션 — '얼마나 들렸나'만 바꿔 끼운다."""
    session = CascadeSession(_Sink(), genai_client=object())
    session._tg = _StubGroup()
    session._audible_ms = lambda: audible_ms

    async def _forever() -> None:
        await asyncio.sleep(10)

    running = asyncio.get_running_loop().create_task(_forever())
    session._reply_task = running
    session.state = TurnState.THINKING
    return session, session._tg, running


@pytest.mark.asyncio
async def test_unheard_reply_is_discarded_and_new_utterance_answered():
    """⭐ 아직 한 조각도 안 들린 대답은 **버리고** 새 발화에 답한다."""
    session, group, running = await _rig(audible_ms=0)
    await session._start_reply("지금 몇 시야")
    assert running.cancelled() or running.done(), "죽은 대답이 계속 돈다"
    assert group.started == 1, "새 발화가 답을 못 받았다"
    assert session._pending_user_text == "", "버렸는데 대기열에도 남겼다(두 번 답한다)"
    assert session.state == TurnState.THINKING


@pytest.mark.asyncio
async def test_heard_reply_keeps_the_queue():
    """⛔ **이미 들린 대답은 안 버린다** — 2026-08-07 대기열 도입 근거를 깨면 안 된다.

    "대답 다 해도 내가 중간에 말한 거에 답을 안 해" 가 그 근거다. 버리는 건 **아무도 안
    들었을 때만**이다.
    """
    session, group, running = await _rig(audible_ms=5_000)
    await session._start_reply("지금 몇 시야")
    assert not running.cancelled(), "듣고 있는 대답을 죽였다"
    assert group.started == 0
    assert session._pending_user_text == "지금 몇 시야", "발화를 통째로 버렸다"
    running.cancel()


@pytest.mark.asyncio
async def test_discard_also_clears_the_client_buffer():
    """⭐ 소리가 아직 안 났어도 **바이트는 이미 나가 있을 수 있다**(선행 버퍼).

    안 지우면 버린 대답이 그대로 재생된다 — 사장님이 겪으신 "삭제돼야 하는데 계속 나온다".
    """
    session, group, running = await _rig(audible_ms=0)
    turn_id = await session.beaver.begin()
    await session._start_reply("그만")
    cancels = [e for e in session.transport.events if e.get("type") == "audio_cancel"]
    assert len(cancels) == 1, session.transport.types()
    assert cancels[0]["turn_id"] == turn_id
    assert group.started == 1


@pytest.mark.asyncio
async def test_batch_synthesis_is_not_discarded():
    """배치 모드는 예외다 — 20초를 통째로 합성해 놓고 소리를 낸다.

    그 구간 내내 '안 들림'이라 버리기 시작하면 **그 모드가 영영 완성되지 못한다**(끊김 없는
    소리를 들어보는 것이 목적인데 목적 자체가 배반된다). 대신 대기열로 간다 — 발화는 안 버린다.
    """
    session, group, running = await _rig(audible_ms=0)
    session._batch_synthesizing = True
    await session._start_reply("여보세요")
    assert not running.cancelled()
    assert session._pending_user_text == "여보세요"
    running.cancel()


def test_discard_predicate_matches_the_bargein_gate():
    """⛔ '안 들림'의 정의가 두 곳에서 갈리면 안 된다 — 같은 술어를 쓴다."""
    session = CascadeSession(_Sink())
    session._audible_ms = lambda: settings.CASCADE_BARGEIN_MIN_AUDIBLE_MS - 1
    assert session._beaver_unheard() is True
    session._audible_ms = lambda: settings.CASCADE_BARGEIN_MIN_AUDIBLE_MS
    assert session._beaver_unheard() is False


# ── ⑤ 확정 뒤 '안 들림' 재검사 제거 ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_confirmed_bargein_is_not_re_gated_by_audible(monkeypatch):
    """진입 관문을 통과한 확정을 **0.5초 뒤에 한 번 더** 재지 않는다.

    08-08 통화에서 그 재검사가 확정 3건을 죽였다(`확정취소-안들림`). 진입 때 들렸으면
    지금은 **더 들렸다** — 다시 물을 이유가 없다.
    """
    session = CascadeSession(_Sink())
    session.state = TurnState.BEAVER_SPEAKING
    session._audible_ms = lambda: 0        # 재검사가 있었다면 여기서 취소됐다
    cuts: list = []

    async def _cut(event):
        cuts.append(event)

    session._on_barge_in = _cut
    await session._confirm_bargein(SttV2Event(kind=SPEECH_BEGIN), "전사 확인")
    assert len(cuts) == 1, "확정을 또 재서 죽였다"
    assert not any(o.startswith("확정취소-안들림") for o, _ in session._bargein_obs)


@pytest.mark.asyncio
async def test_confirm_still_skips_when_there_is_nothing_to_cut():
    """⚠ 상태 검사는 남는다 — 이건 중복이 아니라 **끊을 대상이 없는** 경우다."""
    session = CascadeSession(_Sink())
    session.state = TurnState.IDLE        # 보류 사이에 대답이 끝났다
    cuts: list = []

    async def _cut(event):
        cuts.append(event)

    session._on_barge_in = _cut
    await session._confirm_bargein(None, "전사 확인")
    assert not cuts
    assert [o for o, _ in session._bargein_obs] == ["확정취소-상태"]


# ── 첫소리 분해 ─────────────────────────────────────────────────────────────
def test_first_sound_breakdown_sums_to_the_total():
    """⭐ 분해값의 합 = 첫소리. 합이 안 맞으면 어디서 늦는지 못 가린다."""
    # ⚠ 2진수로 정확히 떨어지는 값을 쓴다 — 0.32 같은 값은 int() 절삭으로 319 가 된다.
    t = cs._ReplyTiming(began=100.0, queued_ms=120)
    t.chunk_at, t.sentence_at, t.audio_at = 100.75, 101.0, 102.25
    t.vendor_ms = 1000
    line = t.summary()
    assert "첫소리=2250ms" in line, line
    assert "대기열 120" in line and "LLM첫조각 750" in line
    assert "문장완성 250" in line
    assert "벤더 1000" in line and "송출 250" in line, line
    assert 750 + 250 + 1000 + 250 == 2250    # 합 = 첫소리(대기열은 첫소리 **밖**이다)


def test_first_sound_is_honest_when_no_audio_went_out():
    """소리가 안 나갔으면 0 이 아니라 '없음'이다 — 0 은 '즉시 나갔다'로 읽힌다."""
    t = cs._ReplyTiming(began=100.0)
    assert t.first_sound_ms == -1
    assert "없음" in t.summary()


def test_queue_wait_is_outside_first_sound():
    """대기열 대기는 첫소리에 **안 들어간다**(began 이 _run_reply 안에서 찍힌다).

    사용자 체감 지연 = 대기열 + 첫소리라, 따로 싣지 않으면 로그로 못 가른다.
    """
    t = cs._ReplyTiming(began=100.0, queued_ms=9_000)
    t.chunk_at = t.sentence_at = t.audio_at = 100.5
    assert t.first_sound_ms == 500
    assert "대기열 9000" in t.summary()


# ── 타이머 조기 발화 허용오차(바쁜 대기 방지) ───────────────────────────────
def test_deadline_tolerance_is_small_enough_to_be_invisible():
    """허용오차는 **판정에 영향을 주지 않을 만큼** 작아야 한다."""
    assert cs._DEADLINE_EPS_S > 0
    assert cs._DEADLINE_EPS_S * 1000 <= settings.CASCADE_TURN_SILENCE_MS / 10
    assert cs._DEADLINE_EPS_S * 1000 <= settings.CASCADE_BARGEIN_SUSTAIN_MS / 100


def test_pump_sleeps_before_retrying_an_unexplained_wake():
    """⛔ 설명 안 되는 깨어남에서 곧장 continue 하면 **timeout=0 스핀**이 된다(cpu=1).

    소스에 최소 대기가 남아 있어야 한다 — 지우면 이 테스트가 잡는다.
    """
    import inspect
    src = inspect.getsource(CascadeSession._pump_turn)
    assert "await asyncio.sleep(_DEADLINE_EPS_S)" in src, src[-800:]


# ── 판정 시점: **말을 시작한 때**(2026-08-08 23:18 실통화) ──────────────────
# 사장님: "1·2·3을 물어봤으면 2번을 대답하는 것 같다."
#     23:18:26.379  turn u6 text='4K'                          ← 대답 b7 시작(오인식이다)
#     23:18:29.338  b7 첫 소리(첫소리=2959ms)
#     23:18:29.815  turn u7 "Let's go to the 한국어 공부하자" → **대기열**
#     23:18:36.938  b7 재생 완료 → 그제서야 u7 에 답한다(7124ms 늦게)
# 같은 술어를 두 시점에 쟀고 그 사이에 답이 바뀌었다 — 시작할 땐 너무 일러서 못 끊고,
# 닫힐 땐 이미 늦어서 못 버린다. 그 간격은 일상적으로 열린다(말 시작→닫힘 1.7~2초 < 첫소리 3초).
@pytest.mark.asyncio
async def test_unheard_at_speech_start_wins_over_audible_at_close():
    """⭐ **말을 시작한 순간** 안 들렸으면, 닫힐 때 들리더라도 버리고 새 발화에 답한다."""
    session, group, running = await _rig(audible_ms=0)
    session._turn_beaver_unheard = session._beaver_unheard()   # 턴이 열린 순간(= _open_turn)
    session._audible_ms = lambda: 1_400                        # 닫힐 때는 이미 소리가 나간다
    await session._start_reply("Let's go to the 한국어 공부하자")
    assert running.cancelled() or running.done(), "낡은 대답을 끝까지 재생한다"
    assert group.started == 1
    assert session._pending_user_text == ""


@pytest.mark.asyncio
async def test_audible_at_speech_start_still_queues():
    """⛔ 말을 시작할 때 **이미 들리고 있었으면** 대기열이다(2026-08-07 근거 보존).

    그건 사용자가 그 대답을 듣고 반응한 경우다 — 버리면 "듣고 있던 말이 사라진다".
    """
    session, group, running = await _rig(audible_ms=5_000)
    session._turn_beaver_unheard = session._beaver_unheard()
    await session._start_reply("잠깐만요")
    assert not running.cancelled()
    assert session._pending_user_text == "잠깐만요"
    running.cancel()


@pytest.mark.asyncio
async def test_open_turn_records_the_moment_speech_started():
    """`_open_turn` 이 그 순간을 굳힌다 — 나중에 소리가 나도 기억은 안 바뀐다."""
    session = CascadeSession(_Sink())
    session._audible_ms = lambda: 0
    await session._open_turn(0.0)
    assert session._turn_beaver_unheard is True
    session._audible_ms = lambda: 9_999          # 그새 소리가 나기 시작해도
    assert session._turn_beaver_unheard is True  # 판정 근거는 '말을 시작한 때'다


@pytest.mark.asyncio
async def test_queue_answers_only_the_last_utterance():
    """⭐ 밀린 발화가 2건 이상이면 **마지막 것만** 답한다(사장님 선택 ①).

    지금은 단순 대입이라 우연히 그렇다 — 누가 리스트로 바꾸면 조용히 "순서대로 다 답함"이
    되고, 사용자는 이미 지나간 질문의 답을 줄줄이 듣는다.
    """
    session, group, running = await _rig(audible_ms=5_000)
    session._turn_beaver_unheard = False
    await session._start_reply("첫 번째 질문")
    await session._start_reply("두 번째 질문")
    await session._start_reply("세 번째 질문")
    assert session._pending_user_text == "세 번째 질문"
    assert group.started == 0
    running.cancel()


# ── 첫소리 분해: **페이서는 첫소리가 아니다**(2026-08-09) ───────────────────
# 실측 12건에서 TTS 항목이 `1200`(3회)·`2160/2161`(2회)로 반복되고 글자 수와 무관했다.
# 정체는 벤더 지연이 아니라 **재던 구간**이었다 — 예전 `TTS` 는 "첫 문장 준비 → 첫 배치가
# **전량** 송출 완료"까지였고, 그 안에 페이서(실시간 송출, I3)가 통째로 들어 있었다.
def test_pacer_dominates_the_time_to_send_a_whole_batch():
    """⭐ 배치 전량 송출 시간 ≈ **오디오 길이 − 선행버퍼**. 벤더가 아무리 빨라도 그렇다.

    가짜 시계로 재므로 결정적이다(벤더도 네트워크도 없다). 이 성질이 "왜 같은 값이 반복되나"의
    답이다 — 그 숫자는 벤더 지연이 아니라 **그 배치의 오디오 길이**를 보고 있었다.
    """
    from domains.learning.realtime.cascade_reply import speak_stream
    from domains.learning.realtime.cascade_session import BEAVER_BYTES_PER_MS, BeaverOutput

    async def _run(audio_ms: int, lead_ms: int) -> tuple[float, float]:
        clock = {"t": 0.0}

        async def _sleep(sec: float) -> None:
            clock["t"] += sec

        beaver = BeaverOutput(_Sink(), now=lambda: clock["t"], sleep=_sleep)
        beaver.lead_ms = lead_ms
        await beaver.begin()

        async def _stream():
            for _ in range(audio_ms // 200):
                yield b"\x00" * int(200 * BEAVER_BYTES_PER_MS)

        await speak_stream(beaver, _stream(), "x")
        return clock["t"] * 1000.0, beaver.first_audio_at * 1000.0

    total, first_at = asyncio.run(_run(2400, 200))
    assert 1900 <= total <= 2300, total          # ≈ 오디오 2400 − 선행 200 (− 마지막 조각)
    assert first_at == 0.0, "첫 바이트는 기다리지 않는다 — 사용자는 여기서부터 듣는다"  # noqa: E501
    faster, _ = asyncio.run(_run(2400, 1200))    # 선행버퍼를 키우면 그만큼 줄어든다
    assert faster < total - 900, (faster, total)


def test_first_sound_measures_the_first_byte_not_the_whole_batch():
    """⛔ '첫소리'는 **사용자가 듣기 시작한 시각**이다. 배치 전량은 따로 싣는다.

    둘을 한 숫자로 뭉치면 "이미 소리가 나가고 있는 시간"을 지연으로 세게 되고, 벤더를 바꿔도
    그 값은 안 줄어든다(엉뚱한 데를 판다).
    """
    t = cs._ReplyTiming(began=100.0)
    t.chunk_at, t.sentence_at = 100.5, 100.75
    t.vendor_ms = 240
    t.mark_audio(101.0)                 # 첫 바이트가 나간 시각
    t.mark_batch(audio_ms=2400)         # 그 배치가 다 나간 건 한참 뒤
    t.batch_at = 103.2
    line = t.summary()
    assert t.first_sound_ms == 1000, line
    assert "벤더 240" in line and "첫배치=3200ms" in line and "페이서 2200ms" in line, line
