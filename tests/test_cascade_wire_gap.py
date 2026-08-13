"""`와이어공백` 은 **턴 안의 공백만** 센다 — 그리고 왜 남았는지까지 남긴다.

## 왜 (2026-08-13)
00172 로그에서 이렇게 찍혔다:

    와이어공백=[293.09s, 0.52s, 1.37s]
    와이어공백=[293.49s, 0.73s, 1.36s, 1.22s, 1.24s]

293초는 **조용한 통화의 유휴**다 — 사용자가 말하지 않는 동안 비버도 조용한 게 맞다.
그걸 공백으로 세면 **지표를 읽을 수 없다**(유휴와 결함이 한 통에 들어간다).
⛔ 프론트가 `fed/elapsed` 에서 겪은 함정과 같은 종류다.

## 그리고 원인 계측
선행 합성을 넣고도 1.2~1.4초가 남았다. 결과(공백)만 있고 **왜 늦었는지**가 없어서
후보를 못 갈랐다:
    (a) 선행 시작이 늦다  (b) 벤더 왕복이 선행보다 길다  (c) 묶을 문장이 아직 없다
⇒ **선행**(송출보다 얼마나 먼저 걸었나)을 같이 남긴다. 대략 `대기 ≈ 벤더왕복 − 선행` 이라,
  선행이 작으면 (a)/(c) 고 선행이 큰데도 대기가 크면 (b) 다.

여기서 고정하는 성질:
  ① 턴이 시작되면 공백 계산은 **처음부터** 다시 센다(앞 턴과 이어 재지 않는다)
  ② 턴 **안**의 공백은 그대로 잡힌다(진짜 결함은 놓치지 않는다)
  ③ 요청별 `선행` 이 로그에 남는다
  ④ 묶음 경계의 `선행여유`(선행 − 벤더왕복)가 음수면 그만큼이 곧 공백이다
"""

import pytest

import domains.learning.realtime.cascade_session as cs


class _Transport:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        self.frames.append(frame)


class _Clock:
    """수동 시계 — 공백은 **시간**이라 진짜로 재우면 시험이 느리고 흔들린다."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    async def sleep(self, sec: float) -> None:
        self.t += sec


# ── ①② 유휴는 공백이 아니다 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_idle_between_turns_is_not_a_gap():
    """⭐⭐ ① 앞 턴의 마지막 소리부터 이 턴의 첫 소리까지는 **사용자가 말하던 시간**이다.

    293초가 찍히던 자리다. 유휴를 공백으로 세면 중앙값도 최댓값도 못 쓴다.
    """
    clock = _Clock()
    beaver = cs.BeaverOutput(_Transport(), now=clock.now, sleep=clock.sleep)
    frame = b"\x00" * 960

    await beaver.begin()
    await beaver.send(frame)
    await beaver.end()

    clock.t += 293.0                      # 조용한 통화 — 사용자가 한참 말이 없다

    await beaver.begin()
    await beaver.send(frame)

    assert beaver.wire_gaps == [], f"턴 밖의 유휴를 공백으로 셌다 — {beaver.wire_gaps}"


@pytest.mark.asyncio
async def test_a_gap_inside_a_turn_is_still_caught():
    """⛔ ② 유휴를 뺀다고 **진짜 결함까지 놓치면** 지표를 없앤 것과 같다."""
    clock = _Clock()
    beaver = cs.BeaverOutput(_Transport(), now=clock.now, sleep=clock.sleep)
    frame = b"\x00" * 960

    await beaver.begin()
    await beaver.send(frame)
    clock.t += 1.37                       # 비버가 말하는 **중에** 소리가 끊겼다
    await beaver.send(frame)

    assert [round(g, 2) for g in beaver.wire_gaps] == [1.37], beaver.wire_gaps


@pytest.mark.asyncio
async def test_normal_pacing_is_not_a_gap():
    """판정 창(250ms) 아래는 정상 페이싱이다 — 세면 신호가 잡음에 묻힌다."""
    clock = _Clock()
    beaver = cs.BeaverOutput(_Transport(), now=clock.now, sleep=clock.sleep)
    frame = b"\x00" * 960

    await beaver.begin()
    await beaver.send(frame)
    clock.t += 0.02
    await beaver.send(frame)

    assert beaver.wire_gaps == []


# ── ③ 요청별 선행 ───────────────────────────────────────────────────────────
def test_the_request_log_shows_the_head_start():
    """③ `대기` 가 결과라면 `선행` 은 원인의 절반이다 — 둘이 같이 있어야 원인이 갈린다."""
    session = cs.CascadeSession(_Transport(), object())
    session._reply_spans = [("ko", 5, 48_000)]
    session._tts_waits = [1.20]
    session._tts_leads = [0.02]           # 거의 못 앞섰다 = 우리가 늦게 걸었다
    line = session._tts_request_log()
    assert "대기1.20s" in line and "선행0.02s" not in line, line

    session._tts_leads = [0.95]           # 앞섰는데도 기다렸다 = 벤더가 느리다
    assert "선행0.95s" in session._tts_request_log()


# ── ④ 묶음 경계의 여유 ──────────────────────────────────────────────────────
def test_a_negative_margin_is_exactly_the_gap():
    """⭐ ④ **선행 − 벤더왕복**. 음수면 그만큼 소리가 빈다 — 그게 남은 1.2~1.4초의 정체다."""
    session = cs.CascadeSession(_Transport(), object())
    session._batch_leads = [(200, 1_400)]         # 0.2초 앞섰는데 왕복이 1.4초다
    line = session._lead_log()
    assert "-1.20s" in line, line
    assert "선행0.20" in line and "벤더1.40" in line, line


def test_a_positive_margin_means_the_prefetch_covered_the_round_trip():
    """양수면 왕복이 앞 묶음 재생 뒤로 **완전히 숨었다**(공백 0)."""
    session = cs.CascadeSession(_Transport(), object())
    session._batch_leads = [(2_000, 900)]
    assert "+1.10s" in session._lead_log()


def test_no_second_batch_means_no_margin():
    """⚠ 묶음이 하나면 잴 대상이 없다 — 0 이 아니라 `-` 다(0 은 '여유가 없었다'로 읽힌다)."""
    session = cs.CascadeSession(_Transport(), object())
    assert session._lead_log() == "선행여유=-"


def test_a_batch_without_a_vendor_number_is_dropped():
    """⛔ 왕복을 못 받은 회차는 **버린다** — 0 으로 세면 원인이 선행 쪽으로 잘못 기운다."""
    session = cs.CascadeSession(_Transport(), object())
    prep = cs._PreparedBatch("문장", [("문장", "ko")], None, None, 100.0)
    seg = cs._OpenSegment("문장", "ko", "neutral", None, None, {}, {}, False, 100.0)

    session._note_batch_lead(prep, 101.0, seg)          # report 에 ttfb 가 없다
    assert session._batch_leads == []

    seg.report["ttfb_ms"] = 900
    session._note_batch_lead(prep, 101.0, seg)
    assert session._batch_leads == [(1000, 900)]


def test_the_first_batch_is_not_measured():
    """첫 묶음은 앞이 없다 — 그 지연은 `첫소리` 가 담당한다(두 지표가 겹치면 못 읽는다)."""
    session = cs.CascadeSession(_Transport(), object())
    prep = cs._PreparedBatch("문장", [("문장", "ko")], None, None, 100.0)
    seg = cs._OpenSegment("문장", "ko", "neutral", None, None, {"ttfb_ms": 900}, {}, False, 100.0)

    session._note_batch_lead(prep, 0.0, seg)            # prev 가 없다
    assert session._batch_leads == []
