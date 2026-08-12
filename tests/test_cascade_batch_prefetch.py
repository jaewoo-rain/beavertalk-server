"""묶음 **선행 합성** — 다음 묶음을 앞 묶음이 재생되는 동안 미리 만든다.

## 왜 (2026-08-13, 설계 `docs/20260813_0430_…`)
클라 실측: `SERVER GAP 2435ms mid-utterance` · `starved! #5`(12.6초에 5회).
⇒ 비버가 한 문장 말하고 **2.4초 침묵한 뒤** 다음 문장. 지금 사용자가 듣는 가장 큰 결함이었다.

원인: `_flush_batch` 가 **송출 완료를 `await`** 해서 다음 묶음의 TTS 가 그 뒤에야 시작됐다
⇒ 벤더 왕복(실측 937~1098ms)이 통째로 침묵이 된다. 구간(언어) 선행 합성은 이미 있었지만
**묶음 사이에는 없었다.**
⛔ 배경: `_pace()` 에는 **필러가 없다** — 보낼 게 없으면 와이어가 그냥 조용하다.

## 여기서 고정하는 성질
  ① 두 번째 묶음의 **합성이 첫 묶음 송출 중에 시작**된다
  ② **순서**: 묶음은 만든 순서대로 나간다(뒤바뀌지 않는다)
  ③ **취소**: 앞 묶음이 취소되면 뒤 묶음도 **같이 죽고**, 미리 연 것도 **버려진다**(조건 B — 의도)
  ④ 첫 묶음은 **예전과 동일**(첫소리 경로 불변)
  ⑤ 한 묶음이 실패해도 **다음 묶음은 계속**(R5) — 그리고 **로그에 남는다**(조건 A)
  ⑥ 미리 만들었는데 못 쓴 글자가 `선행폐기=` 로 보인다(선행 합성의 **대가**)
"""

import asyncio
import logging

import pytest

import domains.learning.realtime.cascade_session as cs


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        return None


class _FakeChat:
    def __init__(self, pieces: list[str]) -> None:
        self._pieces = pieces
        self.text = ""
        self.usage_metadata = None
        self.failed = False
        self.truncated = False

    def chunks(self):
        chat = self

        async def _gen():
            for piece in chat._pieces:
                chat.text += piece
                yield piece

        return _gen()


def _rig(monkeypatch, pieces, *, send_delay=0.05, fail_on=None):
    """준비/송출 시각을 기록하는 리그 — **누가 언제 시작했나**가 이 시험의 전부다."""
    events: list[tuple[str, str]] = []

    async def _prepare(self, text):
        events.append(("prepare", text))
        return cs._PreparedBatch(text, [(text, "ko")], None, None)

    async def _send(self, prep):
        events.append(("send-start", prep.text))
        if fail_on and fail_on in prep.text:
            raise RuntimeError("합성 실패(시험)")
        await asyncio.sleep(send_delay)
        events.append(("send-done", prep.text))
        return len(prep.text) * 100

    monkeypatch.setattr(cs.CascadeSession, "_prepare_batch", _prepare)
    monkeypatch.setattr(cs.CascadeSession, "_speak_prepared", _send)
    monkeypatch.setattr(cs.CascadeSession, "_begin_beaver_turn",
                        lambda self: self.beaver.begin())
    # ⚠ `_batch_chars()` 는 **엔진별** 설정을 읽는다(성질 표) — 기본값 설정을 갈아도 안 먹는다.
    #   여기서는 "문장마다 한 묶음"으로 만들어 묶음 경계를 확실히 만든다.
    monkeypatch.setattr(cs.CascadeSession, "_batch_chars", lambda self: 1)
    monkeypatch.setattr(cs.gemini_chat, "open_chat_stream",
                        lambda *a, **kw: _FakeChat(pieces))
    session = cs.CascadeSession(_Sink(), object())
    return session, events


_PIECES = ["첫째 묶음입니다. ", "둘째 묶음입니다. ", "셋째 묶음입니다. "]


@pytest.mark.asyncio
async def test_the_next_batch_is_prepared_while_the_previous_one_is_sending(monkeypatch):
    """⭐⭐ ① 다음 묶음의 **합성이 앞 묶음 송출 중에** 시작된다 — 그게 이 변경의 전부다."""
    session, events = _rig(monkeypatch, _PIECES)

    await session._run_reply("안녕")

    order = [f"{kind}:{text.strip()[:2]}" for kind, text in events]
    # 둘째 묶음의 prepare 가 첫째 묶음의 send-done **앞**에 있어야 한다.
    prep2 = order.index("prepare:둘째")
    done1 = order.index("send-done:첫째")
    assert prep2 < done1, f"앞 묶음이 끝난 뒤에야 다음 합성을 걸었다 — {order}"


@pytest.mark.asyncio
async def test_batches_are_sent_in_order(monkeypatch):
    """⛔ ② 순서는 절대다 — 합성이 먼저 끝났다고 먼저 나가면 말이 뒤섞인다."""
    session, events = _rig(monkeypatch, _PIECES)

    await session._run_reply("안녕")

    starts = [t.strip() for k, t in events if k == "send-start"]
    assert starts == ["첫째 묶음입니다.", "둘째 묶음입니다.", "셋째 묶음입니다."], starts
    # 앞 묶음이 끝난 뒤에 다음이 시작된다(겹쳐 보내지 않는다 — I1: 비버 턴은 하나).
    seq = [f"{k}:{t.strip()[:2]}" for k, t in events if k.startswith("send")]
    assert seq.index("send-done:첫째") < seq.index("send-start:둘째"), seq


@pytest.mark.asyncio
async def test_a_cancelled_batch_kills_the_ones_behind_it(monkeypatch):
    """⛔⛔ ③ **의도해서** 같이 죽는다(조건 B) — 끊었으면 뒤 문장도 나가면 안 된다.

    우연히 그렇게 되는 것과 의도해서 그런 것은 다르다. 여기서 못박는다.
    """
    session, events = _rig(monkeypatch, _PIECES, send_delay=5.0)

    task = asyncio.get_running_loop().create_task(session._run_reply("안녕"))
    await asyncio.sleep(0.05)          # 첫 묶음이 송출 중인 시점
    session._reply_cancelled = True
    task.cancel()
    await asyncio.sleep(0.05)
    # ⚠ `_run_reply` 는 **우리가 건 취소를 흡수한다**(기존 계약) — 이 태스크는 세션
    #   TaskGroup 의 자식이라 취소를 다시 올리면 **세션 전체가 무너진다**. 그래서 여기서
    #   `CancelledError` 를 기대하지 않는다. 우리가 볼 것은 **소리가 더 안 나갔는가**다.
    assert task.done()

    done = [t for k, t in events if k == "send-done"]
    assert done == [], f"취소했는데 묶음이 끝까지 나갔다 — {events}"
    started = [t.strip()[:2] for k, t in events if k == "send-start"]
    assert started == ["첫째"], f"취소 뒤에 다음 묶음이 송출을 시작했다 — {started}"


@pytest.mark.asyncio
async def test_a_prepared_batch_is_cancelled_when_unused(monkeypatch):
    """③ 미리 연 구간은 **반드시 취소**된다 — 안 그러면 끊은 뒤에 소리가 더 나온다(I3)."""
    cancelled = {"n": 0}

    class _Task:
        def cancel(self):
            cancelled["n"] += 1

    class _Seg:
        task = _Task()

    prep = cs._PreparedBatch("문장", [("문장", "ko")], None, _Seg())
    prep.cancel()
    prep.cancel()          # 두 번 불러도 안전해야 한다(취소 경로가 겹친다)

    assert cancelled["n"] == 1
    assert prep.first is None


@pytest.mark.asyncio
async def test_one_failed_batch_does_not_stop_the_rest(monkeypatch, caplog):
    """⛔ ⑤ 조건 A — 태스크 예외는 **조용히 사라지면 안 된다**. 그리고 다음은 계속 간다(R5).

    이건 곁다리가 아니라 **대답 경로**다. 소리가 안 나가는데 로그가 조용하면 오늘 우리가
    당한 그 유형(A: 안 보내는데 아무도 몰랐다)이 그대로 재발한다.
    """
    session, events = _rig(monkeypatch, _PIECES, fail_on="둘째")

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        await session._run_reply("안녕")

    done = [t.strip()[:2] for k, t in events if k == "send-done"]
    assert "셋째" in done, f"한 묶음이 실패했다고 뒤가 멈췄다 — {events}"
    assert [r for r in caplog.records if "묶음 송출 실패" in r.getMessage()], (
        "실패가 조용히 사라졌다 — 소리가 안 나가는데 로그가 없다"
    )


@pytest.mark.asyncio
async def test_the_first_batch_path_is_unchanged(monkeypatch):
    """④ 첫 묶음은 예전과 같다 — **첫소리를 늦추지 않는다**(선행은 두 번째부터)."""
    session, events = _rig(monkeypatch, ["한 묶음뿐입니다. "])

    await session._run_reply("안녕")

    kinds = [k for k, _ in events]
    assert kinds == ["prepare", "send-start", "send-done"], kinds


def test_the_wire_gap_metric_measures_frame_intervals():
    """⚠ ⑥ 값은 **송출 지점**에서 잰다 — 묶음 경계에서 재면 선행 합성 뒤 0 이 되어 거짓말한다.

    선행 합성을 넣으면 "다음 묶음을 기다린 시간"은 0 이 되는데 **소리는 여전히 안 나갈 수
    있다.** 굶는 쪽은 클라이고, 클라가 보는 것도 프레임 간격이다.
    """
    assert cs.CascadeSession._batch_gap_log([]) == "와이어공백=-"
    assert cs.CascadeSession._batch_gap_log([0.1, 0.24]) == "와이어공백=-"
    line = cs.CascadeSession._batch_gap_log([0.05, 2.435])
    assert "2.44s" in line and "0.05s" not in line, line
