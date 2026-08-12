"""구간 마커 — 표정 + **자막**을 그 구간 오디오 앞에 인밴드로 끼운다(2026-08-12 프론트 합의).

⛔ 먼저: 캐스케이드는 지금까지 **비버 자막을 한 번도 안 보냈다**(Live 는 `output_transcript` 로
보낸다). 사용자가 비버 말을 글로 못 봤다 — Live 대비 기능 후퇴였다. 마커의 `text` 가 그걸
같이 메운다. ⭐ 프레임을 둘로 안 나눈 이유: **같은 사실의 출처가 둘이면 어긋난다.**

형식:
    {"type":"sentence","turn_id":"b7","seq":0,
     "emotion":"happy","text":"잘하셨어요!","server_bytes":48000}

여기서 고정하는 성질(프론트·bt-back 지정):
  ① `text` 가 **실제로 소리 나간 문장과 같다**(태그 제거·꼬리 버림 이후)
  ② `server_bytes` 가 **실제 보낸 누적 바이트와 일치**
  ③ 마커가 **그 구간 첫 오디오 프레임보다 먼저** 나간다(순서 역전 0건)
  ④ 태그 없는 구간이 **직전 감정을 이어받는다**(neutral 로 안 떨어진다)
  ⑤ 꼬리 미완성 태그가 **text 에도 소리에도** 안 남는다
"""

import pytest

import domains.learning.realtime.cascade_reply as cr
import domains.learning.realtime.cascade_session as cs


class _Wire:
    """WS 로 나간 **순서 그대로** 모은다 — 프레임과 오디오를 한 줄에 섞어 담는다."""

    def __init__(self) -> None:
        self.log: list[tuple] = []

    async def send_event(self, event: dict) -> None:
        self.log.append(("event", event))

    async def send_audio(self, frame: bytes) -> None:
        self.log.append(("audio", len(frame)))

    async def receive(self):
        raise AssertionError("쓰지 않는다")

    def markers(self) -> list[dict]:
        return [e for k, e in self.log if k == "event" and e.get("type") == "sentence"]


def _session(monkeypatch, chunks_by_text=None) -> cs.CascadeSession:
    """가짜 TTS — 구간 텍스트마다 정해진 오디오를 낸다(길이로 바이트를 셀 수 있게).

    ⚠ **0 으로 채우지 마라** — 그건 침묵이고 기본 엔진(Gemini)은 침묵 절단이 켜져 있어
      통째로 버린다. 그러면 이 시험이 재려는 것(마커·바이트)에 닿지도 못한다.
    """
    async def _stream(text, **kwargs):
        size = (chunks_by_text or {}).get(text.strip(), 480)

        async def _gen():
            yield bytes([40]) * size
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _stream)
    session = cs.CascadeSession(_Wire(), object())
    session.beaver.lead_ms = 100_000        # 페이싱 대기 없이(이 시험의 관심사가 아니다)
    return session


# ── ③ 순서: 마커 → 오디오 ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_marker_precedes_the_first_audio_frame(monkeypatch):
    """⛔ 순서가 **주 키**다(프론트가 마커를 오디오 큐에 위치로 꽂는다). 역전 0건."""
    session = _session(monkeypatch)
    await session.beaver.begin()
    await session._speak("<happy> 잘하셨어요!")

    kinds = [k for k, _ in session.transport.log]
    assert kinds, session.transport.log
    assert kinds[0] == "event", "오디오가 마커보다 먼저 나갔다"
    first_audio = kinds.index("audio")
    assert kinds[:first_audio].count("event") >= 1


@pytest.mark.asyncio
async def test_every_segment_gets_its_own_marker_in_order(monkeypatch):
    """⚠ `seq` 는 **구간 순번**이다(문장 순번이 아니다) — 코드스위칭 문장 하나가 구간 2개면
    마커도 2개다."""
    session = _session(monkeypatch)
    await session.beaver.begin()
    await session._speak("<happy> Say __안녕하세요__ now")

    markers = session.transport.markers()
    assert len(markers) >= 2, markers
    assert [m["seq"] for m in markers] == list(range(len(markers)))
    assert all(m["turn_id"] == "b1" for m in markers)


# ── ① 자막이 소리와 같다 ──────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_marker_text_is_what_is_actually_spoken(monkeypatch):
    """⛔ 자막이 소리와 다르면 안 된다 — 태그·마커를 걷어낸 **최종 문장**이어야 한다."""
    asked: list[str] = []

    async def _stream(text, **kwargs):
        asked.append(text)

        async def _gen():
            yield bytes([40]) * 480
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _stream)
    session = cs.CascadeSession(_Wire(), object())
    session.beaver.lead_ms = 100_000
    await session.beaver.begin()
    await session._speak("<surprised> 우와, 정말요?")

    markers = session.transport.markers()
    assert markers and markers[0]["text"] == "우와, 정말요?"
    assert asked and asked[0].strip() == markers[0]["text"], (asked, markers)


# ── ② server_bytes ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_server_bytes_matches_what_was_actually_sent(monkeypatch):
    """프론트 원장(바이트)과 **정수로 대조**하는 값이다 — 어긋나면 조용히 틀어진다."""
    session = _session(monkeypatch, chunks_by_text={"첫 문장이에요": 4800,
                                                    "둘째 문장이에요": 960})
    await session.beaver.begin()
    await session._speak("<happy> 첫 문장이에요")
    await session._speak("<sad> 둘째 문장이에요")

    markers = session.transport.markers()
    assert [m["server_bytes"] for m in markers] == [0, 4800], markers
    assert session.beaver.sent_bytes == 5760


@pytest.mark.asyncio
async def test_server_bytes_is_monotonic_and_skips_nothing(monkeypatch):
    """⭐⭐ **프론트가 이 값의 차분으로 자막 속도를 정한다**(2026-08-13 확정).

        구간 N 의 길이 = marker[N+1].server_bytes − marker[N].server_bytes

    ⇒ 이 값이 틀리면 **자막이 소리와 어긋난다.** `audio_bytes` 필드를 따로 안 넣기로 한
      근거가 이 성질이므로, 성질 쪽을 대신 못박는다:
      ① 단조 **증가**(같거나 줄면 그 구간 길이가 0/음수가 된다)
      ② **건너뛰지 않는다** — 차분의 합 = 실제로 보낸 총 바이트(중간에 새는 바이트 0)
      ③ 각 차분 = **그 구간이 실제로 낸 오디오**(가짜 TTS 가 준 길이 그대로)
    """
    sizes = {"첫 구간이에요": 4800, "둘째 구간이에요": 960, "셋째 구간이에요": 2400}
    session = _session(monkeypatch, chunks_by_text=sizes)
    await session.beaver.begin()
    for text in sizes:
        await session._speak(f"<happy> {text}")

    markers = session.transport.markers()
    assert len(markers) == 3, markers
    values = [m["server_bytes"] for m in markers]
    assert values == sorted(values), f"단조 증가가 깨졌다 — 구간 길이가 음수가 된다: {values}"
    assert len(set(values)) == len(values), f"같은 값이 두 번 — 그 구간이 0바이트가 된다: {values}"

    total = session.beaver.sent_bytes
    diffs = [b - a for a, b in zip(values, values[1:])] + [total - values[-1]]
    assert sum(diffs) == total, f"차분 합({sum(diffs)}) != 실제 송출({total}) — 바이트가 샌다"
    assert diffs == [sizes[m["text"]] for m in markers], (diffs, markers)


@pytest.mark.asyncio
async def test_a_new_turn_starts_counting_from_zero(monkeypatch):
    """⛔ 취소·턴 교체 뒤에는 **0 부터 다시 센다** — 누적이 이어지면 프론트의 첫 차분이
    앞 턴의 잔량을 먹어 **첫 자막이 통째로 어긋난다**.

    (`sent_bytes` 는 턴 레코드에 달려 있어 새 턴이면 새 레코드다 — 그 성질을 고정한다.)
    """
    session = _session(monkeypatch, chunks_by_text={"앞 턴 문장": 4800, "새 턴 문장": 960})
    await session.beaver.begin()
    await session._speak("<happy> 앞 턴 문장")
    assert session.beaver.sent_bytes == 4800

    await session.beaver.cancel(reason="barge_in")     # 실제 취소 경로를 지난다
    await session.beaver.begin()
    await session._speak("<sad> 새 턴 문장")

    markers = session.transport.markers()
    new_turn = [m for m in markers if m["text"] == "새 턴 문장"]
    assert new_turn, markers
    assert new_turn[0]["server_bytes"] == 0, (
        f"새 턴인데 앞 턴 누적을 이어받았다({new_turn[0]['server_bytes']})"
    )
    assert new_turn[0]["turn_id"] != markers[0]["turn_id"], "턴 id 가 안 바뀌었다"


# ── ④ carry-forward ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_untagged_sentence_keeps_the_previous_emotion(monkeypatch):
    """⭐ 모델은 **감정이 바뀔 때만** 태그를 붙인다(실측). 태그 없음 = 누락이 아니라 **연속**이다.

    ⛔ 여기서 neutral 로 떨어뜨리면 표정이 문장마다 튄다 — 모델 의도(연속)를 깨는 쪽이다.
    """
    session = _session(monkeypatch)
    await session.beaver.begin()
    await session._speak("<happy> 잘하셨어요!")
    await session._speak("비버예요.")            # 태그 없음 → happy 를 이어간다
    await session._speak("<sad> 아쉽네요.")

    assert [m["emotion"] for m in session.transport.markers()] == ["happy", "happy", "sad"]


@pytest.mark.asyncio
async def test_the_first_segment_without_a_tag_starts_neutral(monkeypatch):
    """⚠ 이어갈 값이 없는 첫 구간에서만 기본값으로 시작한다."""
    session = _session(monkeypatch)
    await session.beaver.begin()
    await session._speak("안녕하세요.")
    assert session.transport.markers()[0]["emotion"] == "neutral"


def test_the_server_resolves_carry_forward_not_the_client():
    """⛔ 클라에 규칙을 넘기지 않는다 — 취소로 마커를 버릴 때 클라 상태가 어긋난다.

    매 마커에 **이미 이어붙인 결과값**이 실린다(상태 없는 쪽이 안 깨진다).
    """
    session = cs.CascadeSession(_Wire(), object())
    assert session._sentence_emotion("<angry> 뭐야") == "angry"
    assert session._sentence_emotion("그리고 또") == "angry"      # 이어간다
    assert session._sentence_emotion("<happy> 좋아") == "happy"


# ── ⑤ 잘린 태그 ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_dangling_tag_reaches_neither_the_text_nor_the_audio(monkeypatch):
    """⛔ 길이 상한이 태그 중간을 자른 실측(`… today. <happy`)."""
    asked: list[str] = []

    async def _stream(text, **kwargs):
        asked.append(text)

        async def _gen():
            yield bytes([40]) * 480
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _stream)
    session = cs.CascadeSession(_Wire(), object())
    session.beaver.lead_ms = 100_000
    await session.beaver.begin()
    await session._speak("오늘 재미있었어요. <happy")

    markers = session.transport.markers()
    assert markers and "<happy" not in markers[0]["text"], markers
    assert all("<happy" not in t for t in asked), asked


# ── 계약 자체 ─────────────────────────────────────────────────────────────
def test_the_marker_is_a_declared_protocol_frame():
    """⛔ dict 를 손으로 만들지 않는다 — 계약이 코드에 있어야 다음 사람이 안 어긴다."""
    import json

    from domains.learning.realtime.cascade_protocol import (
        CascadeSentenceMarker,
        cascade_server_adapter,
    )

    frame = json.loads(cascade_server_adapter.dump_json(CascadeSentenceMarker(
        turn_id="b7", seq=0, emotion="happy", text="잘하셨어요!", server_bytes=48000,
    )).decode())
    assert frame == {"type": "sentence", "turn_id": "b7", "seq": 0,
                     "emotion": "happy", "text": "잘하셨어요!", "server_bytes": 48000}


def test_an_unknown_emotion_is_not_blocked():
    """⛔ 화이트리스트 금지(합의) — 구버전 앱이 안 죽고, 서버가 집합을 늘려도 된다."""
    from domains.learning.realtime.cascade_protocol import CascadeSentenceMarker

    assert CascadeSentenceMarker(turn_id="b1", seq=0, emotion="excited",
                                 text="야호", server_bytes=0).emotion == "excited"


# ── 🔴 자막이 **나갔는지 로그로** 알 수 있어야 한다(2026-08-12) ────────────
@pytest.mark.asyncio
async def test_the_reply_line_reports_how_many_subtitles_went_out(monkeypatch, caplog):
    """⛔ 사장님이 폰으로 통화하셨는데 **자막이 나갔는지 서버 로그로 못 갈랐다.**

    `_send_sentence_marker` 가 전송만 하고 아무 기록도 안 남겼기 때문이다. 대답 줄에 개수를
    싣는다 — 0 이면 클라가 자막을 못 받은 것이고, 그때 원인을 서버/클라로 가를 수 있다.
    ⚠ 구간별 줄을 새로 만들지 않는다(통화당 로그가 폭발한다) — **기존 요약 줄**에 한 칸이다.
    """
    import logging

    session = _session(monkeypatch)
    await session.beaver.begin()
    with caplog.at_level(logging.INFO):
        await session._speak("<happy> 안녕하세요!")
        await session._speak("<sad> 아쉽네요.")
    assert session._sentence_markers == 2


def test_the_subtitle_count_has_a_different_name_from_the_language_split():
    """⛔ 자막(문장 마커)과 **언어분할**(`__마커__`)은 **이름이 달라야 한다**.

    같은 "마커"라는 말이 두 뜻으로 쓰여 오늘 실제로 혼동이 났다(대답 줄의 `자막=N개` 를
    언어분할로 읽었다). 2026-08-13 에 언어 쪽을 `언어마커=`·`언어분할=` 로 갈랐다.
    ⚠ 세션 전체에서 **맨 `마커=`** 가 없어야 한다 — 한 곳만 고치면 다른 곳이 또 헷갈린다.
    """
    import inspect
    import re

    src = inspect.getsource(cs.CascadeSession._run_reply)
    assert "자막=%d개" in src, "자막 개수가 대답 줄에 없다"
    assert "언어분할=" in src, "언어분할 표시가 사라졌다"

    whole = inspect.getsource(cs)
    bare = [ln.strip() for ln in whole.splitlines()
            if re.search(r'(?<![가-힣])마커=', ln) and "언어마커=" not in ln
            and "문장마커" not in ln and ln.strip().startswith(('"', "'", "#", "f'", 'f"'))
            and not ln.strip().startswith("#")]
    assert not bare, f"맨 `마커=` 가 남아 자막과 헷갈린다: {bare}"
