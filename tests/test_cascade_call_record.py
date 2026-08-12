"""캐스케이드 통화 기록 — **Live 와 같은 형식**으로 남는다(설계 §2 증명).

사장님: "통화내용 저장하던 방식도 **그대로 저장**하면 돼."
⛔ 두 경로의 기록이 갈리면 **복습·발음평가·분석이 한쪽만 돈다** — 그게 이 설계의 유일한 목적이다.

여기서 고정하는 성질:
  ① 통화 행은 **같은 함수**(`create_call`)가 만든다 — 인자까지 Live 와 같은 모양
  ② 세그먼트 계약이 같다: `{turn_index, role, text, pcm}` · role 은 user|beaver
  ③ **점진 저장**이 돈다(크래시 내성) + 저장한 오디오는 메모리에서 놓아준다
  ④ 원가 요약은 **한 번만 만들어** 로그와 DB 가 같은 숫자를 본다
  ⑤ DB 가 실패해도 통화는 살고, 커서가 남아 **다음에 재시도**된다(R5)
  ⑥ ⛔ 안 들린 비버 발화는 **들린 데까지만** 기록된다(설계 §5 와 같은 규율)
"""

import asyncio

import pytest

import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


class _Db:
    """`svc.run_db` 대역 — 어떤 서비스 함수가 어떤 인자로 불렸는지 그대로 모은다."""

    def __init__(self, fail: str = "") -> None:
        self.calls: list[tuple] = []
        self.fail = fail
        self.saved_segments: list[dict] = []

    async def run_db(self, factory, fn):
        # 서비스 모듈을 가짜로 바꿔 두고 fn(db) 를 그대로 실행한다 — **호출부의 인자**가 관심사다.
        return fn(self)


def _session(monkeypatch, db: _Db, *, call_id=11) -> cs.CascadeSession:
    monkeypatch.setattr(cs.svc, "run_db", db.run_db)
    monkeypatch.setattr(cs.svc, "resolve_call_character", lambda _db, m, i: 3)
    monkeypatch.setattr(cs.svc, "load_call_setup", lambda _db, m, c, lang: {
        "role": "비버", "personality": "밝다", "level_profile": "A1",
        "voice": "Fenrir", "locale": "en", "interests": [],
    })

    def _create_call(_db, member_id, character_id, call_type, *, target_language):
        db.calls.append(("create_call", member_id, character_id, call_type, target_language))
        if db.fail == "create_call":
            raise RuntimeError("DB 다운")
        return call_id

    def _save_segments(_db, cid, segs, member_id, *, upload_audio=True):
        db.calls.append(("save_segments", cid, len(segs), member_id, upload_audio))
        if db.fail == "save_segments":
            raise RuntimeError("DB 다운")
        db.saved_segments.extend([dict(s) for s in segs])
        return len(segs)

    def _save_usage(_db, cid, summary, *, engine=None):
        db.calls.append(("save_call_usage", cid, engine, summary))
        return True

    def _finalize(_db, cid, *, total_time, status):
        db.calls.append(("finalize_call", cid, total_time, status))

    monkeypatch.setattr(cs.svc, "create_call", _create_call)
    monkeypatch.setattr(cs.svc, "save_segments", _save_segments)
    monkeypatch.setattr(cs.svc, "save_call_usage", _save_usage)
    monkeypatch.setattr(cs.svc, "finalize_call", _finalize)
    return cs.CascadeSession(_Sink(), object(), session_factory=object(),
                             member_id=42, member_target_language="ko")


# ── ① 통화 행 ──────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_the_call_row_is_created_the_same_way_as_live(monkeypatch):
    """⛔ `create_call` **같은 함수**를, Live 와 **같은 인자 모양**으로 부른다."""
    db = _Db()
    session = _session(monkeypatch, db)
    await session._load_call_context()

    created = [c for c in db.calls if c[0] == "create_call"]
    assert created == [("create_call", 42, 3, "normal", "ko")], db.calls
    assert session._call_id == 11


@pytest.mark.asyncio
async def test_level_test_is_never_routed_from_cascade(monkeypatch):
    """⛔ 레벨 미확정이어도 **normal 고정**(사장님: 레벨테스트는 나중)."""
    db = _Db()
    monkeypatch.setattr(cs.svc, "load_call_setup", lambda *a, **k: {
        "role": "비버", "personality": "밝다", "voice": "Kore", "locale": "en",
        "needs_level_test": True,
    })
    session = _session(monkeypatch, db)
    await session._load_call_context()
    assert [c for c in db.calls if c[0] == "create_call"][0][3] == "normal"


# ── ②⑥ 세그먼트 계약 ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_segments_follow_the_live_contract(monkeypatch):
    """`{turn_index, role, text, pcm}` — `save_segments` 가 그대로 받는 모양이어야 한다."""
    db = _Db()
    session = _session(monkeypatch, db)
    session._call_id = 11
    session._add_segment("user", "안녕하세요", b"\x01\x02")
    session._add_segment("beaver", "반가워요", b"\x03\x04")
    await session._persist_segments()

    assert [s["role"] for s in db.saved_segments] == ["user", "beaver"]
    assert [s["turn_index"] for s in db.saved_segments] == [1, 2]
    assert db.saved_segments[0]["text"] == "안녕하세요"
    assert set(db.saved_segments[0]) == {"turn_index", "role", "text", "pcm"}


@pytest.mark.asyncio
async def test_an_empty_turn_is_not_recorded(monkeypatch):
    """⛔ 전사도 오디오도 없으면 안 남긴다 — 빈 행은 분석에 잡음만 준다."""
    db = _Db()
    session = _session(monkeypatch, db)
    session._call_id = 11
    session._add_segment("user", "   ", b"")
    await session._persist_segments()
    assert db.saved_segments == []


@pytest.mark.asyncio
async def test_only_the_heard_part_of_a_cut_reply_is_recorded(monkeypatch):
    """⑥ barge-in 으로 끊긴 대답은 **들린 데까지만** 기록한다.

    안 들린 뒤쪽을 저장하면 분석이 **사용자가 못 들은 말**을 학습 문장으로 삼는다.
    """
    db = _Db()
    session = _session(monkeypatch, db)
    session._call_id = 11
    session.beaver.lead_ms = 100_000        # 페이서 대기 없이(이 시험의 관심사가 아니다)
    turn_id = await session.beaver.begin()
    # ⚠ 서버 추정 재생점은 **보수적으로 짧은 쪽**이다(보낸 양 − 클라 버퍼 600ms). "들렸다"가
    #   되려면 그 지점 **앞에서 끝나는** 조각이어야 한다 — 1.0초 + 1.2초를 보내면 추정점이
    #   1.6초에 떨어져 첫 조각만 들린 것이 된다.
    await session.beaver.send(b"\x01\x02" * (48 * 1000 // 2), "들린 문장")
    await session.beaver.send(b"\x03\x04" * (48 * 1200 // 2), "안 들린 문장")
    session._reply_cancelled = True
    session._on_reply_cancelled(turn_id, "들린 문장 안 들린 문장")

    beaver = [s for s in session._segments if s["role"] == "beaver"]
    assert beaver, session._segments
    assert "들린 문장" in beaver[0]["text"]
    assert "안 들린 문장" not in beaver[0]["text"], beaver[0]["text"]


# ── ③ 점진 저장 + 메모리 해제 ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_incremental_flush_advances_the_cursor_and_frees_audio(monkeypatch):
    """⭐ 크래시 내성 — 통화 중에 저장한다. 저장한 오디오는 **놓아준다**."""
    db = _Db()
    session = _session(monkeypatch, db)
    session._call_id = 11
    session._add_segment("user", "첫 번째", b"\x01" * 4800)
    await session._persist_segments()
    assert session._persisted == 1
    assert session._segments[0]["pcm"] == b"", "저장한 오디오를 계속 물고 있다"

    session._add_segment("beaver", "두 번째", b"\x02" * 4800)
    await session._persist_segments()
    # 두 번째 저장은 **새 것만** 넘긴다(같은 걸 두 번 저장하면 기록이 중복된다)
    assert [c for c in db.calls if c[0] == "save_segments"][-1][2] == 1
    assert session._persisted == 2


@pytest.mark.asyncio
async def test_a_failed_flush_keeps_the_cursor_for_a_retry(monkeypatch):
    """⑤ 실패해도 커서가 안 움직인다 — 다음 주기·종료 때 **다시 시도**된다(R5)."""
    db = _Db(fail="save_segments")
    session = _session(monkeypatch, db)
    session._call_id = 11
    session._add_segment("user", "안녕", b"\x01" * 100)
    await session._persist_segments()
    assert session._persisted == 0
    assert session._segments[0]["pcm"], "실패했는데 오디오를 버렸다(재시도가 불가능해진다)"


# ── ④ 원가 · 종료 ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_usage_is_saved_once_with_the_same_object_as_the_log(monkeypatch):
    """⛔ 로그와 DB 가 **같은 요약 객체**를 본다 — 두 번 계산하면 숫자가 갈린다."""
    db = _Db()
    session = _session(monkeypatch, db)
    session._call_id = 11
    session.usage.record_tts("안녕하세요", vendor="gemini-2.5-flash-tts")
    session.usage.record_tts_audio(48000)
    summary = session.usage.summary(duration_s=30.0, turns=2)

    await session._finalize_call(summary, 30.0)
    saved = [c for c in db.calls if c[0] == "save_call_usage"]
    assert len(saved) == 1, db.calls
    assert saved[0][3] is summary, "로그와 다른 객체를 저장했다"
    assert saved[0][2] == session.usage.engine()
    assert ("finalize_call", 11, 30, "analyzing") in db.calls


@pytest.mark.asyncio
async def test_the_final_persist_writes_text_first(monkeypatch):
    """종료 저장은 `upload_audio=False` — 전사 행을 먼저 커밋해 분석이 안 기다린다(Live P2.6)."""
    db = _Db()
    session = _session(monkeypatch, db)
    session._call_id = 11
    session._add_segment("user", "안녕", b"\x01" * 100)
    await session._finalize_call(None, 12.0)
    seg_calls = [c for c in db.calls if c[0] == "save_segments"]
    assert seg_calls and seg_calls[-1][4] is False, seg_calls


@pytest.mark.asyncio
async def test_without_a_call_id_nothing_is_recorded(monkeypatch):
    """⛔ 통화 행이 없으면(생성 실패·데모) 기록을 안 만든다 — 주인 없는 행이 생기면 안 된다."""
    db = _Db(fail="create_call")
    session = _session(monkeypatch, db)
    await session._load_call_context()
    assert session._call_id is None
    session._add_segment("user", "안녕", b"\x01")
    assert session._segments == []
    await session._finalize_call(None, 5.0)
    assert not [c for c in db.calls if c[0] in ("save_segments", "finalize_call")]


def test_the_flush_interval_matches_live(monkeypatch):
    """⚠ 주기가 길면 그만큼 잃는다 — Live 와 같은 1분."""
    from core.config import settings
    from domains.learning.realtime.call_session import FLUSH_INTERVAL_S

    assert settings.CASCADE_SEGMENT_FLUSH_S == FLUSH_INTERVAL_S
