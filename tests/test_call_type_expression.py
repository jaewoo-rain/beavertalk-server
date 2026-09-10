# -*- coding: utf-8 -*-
"""새 콜타입 2종(expression·freetalk) 라우팅 + 진도 배선 회귀 (외부 의존 0).

무엇을 지키나:
  ① `call_type=expression|freetalk` 이 **DB 에 그대로 남는다**(원가·통계가 코스별로 갈린다)
  ② 지시문이 코스 대본으로 갈린다 — 표현학습엔 [오늘의 표현] 이 실리고, 프리토킹엔 안 실린다
  ③ 선톡 시드도 코스별로 갈린다(⛔ 시드는 지시문을 이긴다 — call 1087)
  ④ ⛔⛔ **표현학습의 검출 목록은 [:10] 으로 잘리지 않는다** — 자르면 11번부터가 영원히
     «안 가르친 것» 으로 남는다. `normal` 은 상한 10 그대로다
  ⑤ ⛔ 학습자 발화 대조는 **표현학습에서만** 돈다 — `normal` 은 비버만 본다(9638a26 규율)
  ⑥ 일일 한도가 콜타입별로 각자 센다(새 2종도 한도 표에 있다 — 없으면 무제한이 된다)

가짜 Live 세션 + 가짜 WS + 인메모리 DB. 네트워크 0.
"""

from __future__ import annotations

import contextlib
import json

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.account.models.member_reason import MemberReason
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level

from core.config import settings as app_settings
from core.gemini_live import LiveEvent

import domains.learning.realtime.call_session as cs
import domains.learning.service.normalcall_service as svc
from domains.learning.realtime.call_session import run_call
from domains.learning.service import call_service


# --------------------------------------------------------------------------- #
# 인메모리 DB + 시드
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


ITEM_COUNT = 14  # ⭐ 10 보다 커야 [:10] 절단이 드러난다(그게 이 파일의 요점 하나다)


@pytest.fixture()
def seeded(session_factory):
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="친근한 선생님", personality="다정함",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.add(Level(language="ko", level_no=1, profile="초급 학습자"))
        db.flush()
        for i in range(1, ITEM_COUNT + 1):
            db.add(LearningItem(
                source_key=f"c{i}", language="ko", assign_rule="test_v1",
                kind="chunk", band=1, level_no=1, seq_no=i,
                surface=f"표현{i:02d} 주세요",
            ))
        member = Member(language="en", korean_level=1, onboarding_completed=True,
                        auth_user_id="auth-member")
        db.add(member)
        db.flush()
        db.add(MemberReason(member_id=member.member_id, reason="travel"))
        db.commit()
        return {"member_id": member.member_id, "character_id": ch.character_id}
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _mock_external(monkeypatch):
    """Storage/TTS/분석 스텁 — 네트워크 0."""
    monkeypatch.setattr(svc.storage, "upload", lambda *a, **k: "stub-key")
    monkeypatch.setattr(svc.storage, "public_url", lambda *a, **k: "https://stub/url.mp3")

    async def _fake_tts(*_a, **_k):
        return None

    monkeypatch.setattr(svc.tts, "synthesize", _fake_tts)

    async def _fake_generate(*_a, **_k):
        return svc.CallAnalysis(summary="요약", detected_mode="chat", expressions=[])

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _fake_generate)


class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self._incoming = list(incoming)
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        from starlette.websockets import WebSocketState
        self._WS = WebSocketState
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        if self._incoming:
            return self._incoming.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int | None = None) -> None:
        self.client_state = self._WS.DISCONNECTED


class FakeLiveSession:
    def __init__(self):
        self.sent_text_turns: list[str] = []

    async def send_audio(self, pcm16_16k: bytes) -> None:  # pragma: no cover - 미사용
        pass

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)

    async def events(self):
        yield LiveEvent(kind="out_tr", text="안녕!")
        yield LiveEvent(kind="turn_end")


def _factory(holder):
    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **_kw):
        sess = FakeLiveSession()
        holder["session"] = sess
        holder["system_instruction"] = system_instruction
        yield sess

    return _f


async def _run(session_factory, seeded, call_type: str | None, holder: dict):
    start = {"type": "start", "character_id": seeded["character_id"]}
    if call_type is not None:
        start["call_type"] = call_type
    ws = FakeWebSocket([
        {"type": "websocket.receive", "text": json.dumps(start)},
    ])
    await run_call(
        ws, app_settings, object(), session_factory,
        member_id=seeded["member_id"], live_session_factory=_factory(holder),
    )
    for _ in range(300):
        if not cs._analysis_tasks:
            break
        import asyncio
        await asyncio.sleep(0.01)
    return holder


# --------------------------------------------------------------------------- #
# ① 콜타입이 DB 에 남는다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("call_type", ["expression", "freetalk"])
async def test_the_new_call_type_is_stored_on_the_call_row(
    session_factory, seeded, call_type: str,
) -> None:
    """원가·통계·복습 쿼리가 코스별로 갈리려면 이 값이 남아야 한다(기획 R6)."""
    await _run(session_factory, seeded, call_type, {})
    db = session_factory()
    try:
        rows = db.query(Call).all()
        assert len(rows) == 1 and rows[0].call_type == call_type
    finally:
        db.close()


@pytest.mark.asyncio
async def test_the_default_routing_is_untouched(session_factory, seeded) -> None:
    """⛔ 자동 라우팅은 한 글자도 안 바뀌었다 — 두 코스는 **명시로만** 들어온다."""
    await _run(session_factory, seeded, None, {})
    db = session_factory()
    try:
        assert db.query(Call).one().call_type == "normal"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# ②③ 지시문·시드가 코스별로 갈린다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_expression_call_carries_the_expression_script(session_factory, seeded) -> None:
    holder = await _run(session_factory, seeded, "expression", {})
    out = holder["system_instruction"]
    assert "[오늘의 표현" in out
    assert "[진행 절차]" in out
    # 선별된 표현이 실제로 실렸다(청크 14개 중 18개 요청 → 14개 전량)
    assert out.count("주세요") >= ITEM_COUNT
    # ⛔ 일반 통화의 모드 분기는 없다 — 통화 종류가 이미 답이다
    assert "[대화 모드]" not in out and "공부할래" not in out


@pytest.mark.asyncio
async def test_freetalk_call_carries_no_learning_items(session_factory, seeded) -> None:
    holder = await _run(session_factory, seeded, "freetalk", {})
    out = holder["system_instruction"]
    assert "[오늘의 표현" not in out and "[진행 절차]" not in out
    assert "표현01" not in out
    assert "처음부터 끝까지 한국어로 한다" in out


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call_type,anchor",
    [("expression", "[오늘의 표현] 1번"), ("freetalk", "**한국어로** 짧게 인사")],
)
async def test_the_opening_seed_is_the_courses_own(
    session_factory, seeded, call_type: str, anchor: str,
) -> None:
    """⛔ 시드는 지시문을 **이긴다**(call 1087) — 그래서 시드 자체가 갈려야 한다."""
    holder = await _run(session_factory, seeded, call_type, {})
    seeds = holder["session"].sent_text_turns
    assert seeds and anchor in seeds[0]
    # 일반 통화의 모드 질문이 새어 들어오면 안 된다
    assert "공부할지" not in seeds[0]


# --------------------------------------------------------------------------- #
# ④ ⛔⛔ 검출 목록이 잘리지 않는다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_expression_detection_list_is_not_truncated_at_ten(
    session_factory, seeded, monkeypatch,
) -> None:
    """⛔⛔ [:10] 로 자르면 11번부터가 «비버가 다뤄도 서버는 모르는» 항목이 된다.

    그러면 그 항목은 영원히 «안 가르친 것» 으로 남아 다음 통화에 또 나오고, 재접지 쪽지의
    «이미 다룬 것» 에서도 빠져 되감기를 부른다(통화 1360 이 정확히 부분 목록 사고였다).
    """
    seen: list[list[str]] = []
    orig = cs._reground_instruction
    monkeypatch.setattr(
        cs, "_reground_instruction",
        lambda items, target: (seen.append(list(items)), orig(items, target))[1],
    )
    await _run(session_factory, seeded, "expression", {})
    assert seen, "재접지 재료 조립이 안 돌았다"
    assert len(seen[0]) == ITEM_COUNT, f"검출 목록이 잘렸다: {len(seen[0])}"


@pytest.mark.asyncio
async def test_normal_calls_keep_the_cap_of_ten(
    session_factory, seeded, monkeypatch,
) -> None:
    """⚠ `normal` 은 한 글자도 안 바뀐다 — 상한 10 은 일반 통화의 계약이다."""
    seen: list[list[str]] = []
    orig = cs._reground_instruction
    monkeypatch.setattr(
        cs, "_reground_instruction",
        lambda items, target: (seen.append(list(items)), orig(items, target))[1],
    )
    await _run(session_factory, seeded, None, {})
    assert seen and len(seen[0]) <= 10


# --------------------------------------------------------------------------- #
# ⑤ 학습자 발화 대조는 표현학습에서만
# --------------------------------------------------------------------------- #
def _state(items: list[dict] | None) -> cs._CallState:
    st = cs._CallState()
    st.expr_items = items or []
    st.reground_items = ["안녕히 가세요", "이거 얼마예요?"]
    return st


def test_the_learner_line_counts_only_in_the_expression_course() -> None:
    """⭐ 표현학습은 비버가 모국어로 묻고 **정답을 말하지 않는다** — 라벨은 학습자 입에서만
    나온다. 비버만 보면 그 항목이 영원히 «안 다룬 것» 이 된다.
    """
    st = _state([{"item_id": 1, "obj": "안녕히 가세요"}])
    cs._note_covered_items(st, "안녕히 가세요", source="user")
    assert st.covered_nums == [1]


def test_a_normal_call_still_ignores_the_learner_line() -> None:
    """⛔ 9638a26 규율 그대로 — 일반 통화에서 학습자가 우연히 낸 말은 «가르쳤다» 가 아니다.

    그걸 세면 **아직 안 가르친 항목을 잃는다**(가르칠 기회가 영영 사라진다).
    """
    st = _state(None)  # expr_items 가 비어 있다 = 표현학습이 아니다
    cs._note_covered_items(st, "안녕히 가세요", source="user")
    assert st.covered_nums == []


def test_the_beaver_line_counts_in_both_courses() -> None:
    """비버 경로는 기본값이라 **호출 모양이 안 바뀐다**(기존 호출부 무손상)."""
    for items in ([{"item_id": 1, "obj": "x"}], None):
        st = _state(items)
        cs._note_covered_items(st, '따라 해봐: "이거 얼마예요?"')
        assert st.covered_nums == [2]


def test_detection_stays_append_only() -> None:
    """같은 항목을 두 번 말해도 번호는 한 번만 들어간다(증거가 원본, 나머지는 파생)."""
    st = _state([{"item_id": 1, "obj": "안녕히 가세요"}])
    cs._note_covered_items(st, "안녕히 가세요", source="user")
    cs._note_covered_items(st, "안녕히 가세요", source="beaver")
    assert st.covered_nums == [1]


# --------------------------------------------------------------------------- #
# ⑦ 힌트 제거(D7) · 통화후 분석 분기
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("call_type", ["expression", "freetalk"])
async def test_the_new_courses_have_no_hints(
    session_factory, seeded, monkeypatch, call_type: str,
) -> None:
    """⛔ D7 — 화면 UI 자체를 없앤다. 서버가 push 안 하면 화면에 안 뜬다.

    ⭐ 표현학습에서 특히 해롭다: 이 코스의 퀴즈는 «배운 표현 맞히기» 라 예시 답변을 띄우면
      **정답을 그대로 보여주는 것**이 되어 판정이 무의미해진다.
    """
    spawned: list[str] = []
    monkeypatch.setattr(cs, "_spawn_hint_task",
                        lambda ws, st: spawned.append(st.hint_ctx and "on" or "off"))
    await _run(session_factory, seeded, call_type, {})
    assert "on" not in spawned


@pytest.mark.asyncio
async def test_normal_calls_still_get_hints(session_factory, seeded, monkeypatch) -> None:
    """⚠ `normal`·`level_test` 는 종전 그대로다(hint_used 강등 경로 포함)."""
    seen: list[bool] = []
    orig = cs._hint_instruction
    monkeypatch.setattr(cs, "_hint_instruction",
                        lambda *a, **k: (seen.append(True), orig(*a, **k))[1])
    await _run(session_factory, seeded, None, {})
    assert seen, "일반 통화의 힌트가 같이 꺼졌다"


@pytest.mark.asyncio
@pytest.mark.parametrize("call_type", ["expression", "freetalk"])
async def test_item_detection_is_off_but_analysis_still_runs(
    session_factory, seeded, monkeypatch, call_type: str,
) -> None:
    """⛔⛔ 후보를 **빈 리스트**로 넘겨야 한다 — `None` 은 «안 준다» 가 아니라 «기본 후보를
    대신 뽑아라» 다. None 으로 두면 끄려던 검출이 그대로 돈다(정확히 반대 결과).

    ⭐ 그리고 분석 자체는 **반드시 돌아야 한다** — 안 돌면 결과 화면의 `sentences` 가
      통째로 빈다(기획 §5).
    """
    seen: dict = {}

    def _spy(*args, **kwargs):
        seen["candidates"] = kwargs.get("candidates")
        seen["hinted"] = kwargs.get("hinted_from_turn_index")
        seen["called"] = True

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(cs.svc, "analyze_call", _spy)
    await _run(session_factory, seeded, call_type, {})
    assert seen.get("called"), "통화후 분석이 아예 안 돌았다 — sentences 가 빈다"
    assert seen["candidates"] == [], "None 이면 기본 후보가 뽑혀 검출이 그대로 돈다"
    assert seen["hinted"] is None


# --------------------------------------------------------------------------- #
# ⑥ 일일 한도
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("call_type", ["normal", "level_test", "expression", "freetalk"])
def test_every_call_type_has_a_daily_limit(call_type: str) -> None:
    """⛔ 표에 없으면 `is_daily_limit_reached` 가 «막지 않는다» 로 떨어져 **무제한**이 된다.

    Live 는 통화당 원가가 나가므로 그건 조용한 비용 구멍이다.
    ⚠ 값 자체(Free 가 하루 3통화가 되는 것)는 사장님 확인 사항 — 여기서는 **누락**만 막는다.
    """
    assert call_service.DAILY_CALL_LIMIT.get(call_type)
