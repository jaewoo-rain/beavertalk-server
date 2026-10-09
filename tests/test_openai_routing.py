"""OpenAI Realtime 라우팅 배선 — `run_call` + **가짜 세션** + sqlite 실제 시드(실통화 0).

잠그는 것:
  ① env 가 비면 라우팅이 한 번도 안 돈다(종전 경로 그대로)
  ② 켜면 표현학습이 GPT 대본·GPT 시드로 간다(Gemini 대본 문장 0)
  ③ 통화 길이 = **플랜만** 본다(유료 900 / 무료 300) → 절대 백스톱 952 / 540
  ④ `call_started` 에 `remaining_s`·`max_fragments`·`fragment_index` 를 **안 싣는다**
     (클라가 자기 시간을 잰다). `course` 는 그대로 싣는다.
  ⑤ **재접지 워처가 안 뜬다** / Gemini 통화에선 뜬다
  ⑥ 조각 분할·압축·재개 핸들·통화 중 판정 사이드카가 안 돈다
  ⑦ `usage_engine` = `live:openai-realtime-2.1-mini` → 한 쿼리로 골라낸다
  ⑧ 표정: 무료 OFF / 유료 ON
  ⑨ 원가: `cached` 를 주면 값이 달라지고 **안 주면 기존과 바이트 동일**
  ⑩ 끊김에 재연결하지 않는다
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os

import pytest
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import settings as app_settings
from db.registry import Base
from domains.account.models.member import Member
from domains.account.models.member_reason import MemberReason
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
import domains.learning.realtime.call_session as cs
import domains.learning.service.normalcall_service as svc
from domains.learning.service import call_service
from domains.learning.realtime.call_session import run_call
from scripts.curriculum.load_cur_seed import load

SEED = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed.json")
pytestmark = pytest.mark.skipif(not os.path.exists(SEED), reason="cur_seed.json 없음")


# --------------------------------------------------------------------------- #
# 시드 (tests/test_cur_call_path.py 와 같은 방식 — 같은 재료가 필요하다)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = sf()
    try:
        for i in range(46):
            db.add(LearningItem(language="ko", kind="chunk", source_key=f"c:{i}", band=1,
                                level_no=1, assign_rule="seed", surface=f"청크 문장 {i}",
                                meanings=json.dumps({"en": f"chunk {i}"}), examples="[]"))
        db.commit()
        load(db, json.load(io.open(SEED, encoding="utf-8")), dry_run=False)
        db.commit()
        v = Voice(name="Fenrir", gender="male")
        db.add(v)
        db.flush()
        db.add(Character(name="비비", role="친근한 선생님", personality="다정함",
                         voice_id=v.voice_id, price=0))
        db.add(Level(language="ko", level_no=1, profile="초급 학습자다. 아직 문장을 못 만든다."))
        db.commit()
    finally:
        db.close()
    return sf


_n = {"i": 0}


@pytest.fixture()
def seeded(session_factory):
    _n["i"] += 1
    db = session_factory()
    try:
        ch = db.execute(text("SELECT character_id FROM character LIMIT 1")).scalar()
        # plan_override 는 admin 전용이다(개발자도구 흉내) — 플랜별 길이·표정을 시험하려면 필요.
        m = Member(language="en", korean_level=1, onboarding_completed=True,
                   auth_user_id=f"auth-openai-{_n['i']}", role="admin")
        db.add(m)
        db.flush()
        db.add(MemberReason(member_id=m.member_id, reason="travel"))
        db.commit()
        return {"member_id": m.member_id, "character_id": ch}
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _mock_external(monkeypatch):
    monkeypatch.setattr(svc.storage, "upload", lambda *a, **k: "stub-key")
    monkeypatch.setattr(svc.storage, "public_url", lambda *a, **k: "https://stub/url.mp3")

    async def _fake_tts(*_a, **_k):
        return None
    monkeypatch.setattr(svc.tts, "synthesize", _fake_tts)

    async def _fake_generate(*_a, **_k):
        return svc.CallAnalysis(summary="요약", detected_mode="chat", expressions=[])
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _fake_generate)
    # env 강제 길이가 있으면 플랜 분기를 안 탄다 — 시험에서는 끈다.
    monkeypatch.setattr(cs, "CALL_DURATION_S", None)


@pytest.fixture()
def openai_on(monkeypatch):
    """OpenAI 라우팅을 켠다(env 2개). ⛔ 실제 WS 는 안 연다 — 팩토리를 주입한다."""
    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "expression")
    monkeypatch.setattr(app_settings, "GPT_API_KEY", "test-key")
    return True


# --------------------------------------------------------------------------- #
# 가짜 WS / 가짜 세션
# --------------------------------------------------------------------------- #
class FakeWebSocket:
    def __init__(self, incoming: list[dict]):
        self._incoming = list(incoming)
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed_with: int | None = None
        from starlette.websockets import WebSocketState
        self._WS = WebSocketState
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        if self._incoming:
            item = self._incoming.pop(0)
            if callable(item):
                await item()
                return await self.receive()
            return item
        return {"type": "websocket.disconnect"}

    async def send_text(self, t: str) -> None:
        self.sent_text.append(t)

    async def send_bytes(self, b: bytes) -> None:
        self.sent_bytes.append(b)

    async def close(self, code: int | None = None) -> None:
        self.closed_with = code
        self.client_state = self._WS.DISCONNECTED


class FakeSession:
    """어댑터 자리에 끼우는 가짜. **이벤트 모양은 OpenAI 어댑터의 것**을 쓴다."""

    def __init__(self, script, raise_closed=False):
        self.script = script
        self.sent_text_turns: list[str] = []
        self.regrounds: list[str] = []
        self.personas: list[str] = []
        self.audio_in: list[bytes] = []
        self.raise_closed = raise_closed

    async def send_audio(self, pcm16_16k: bytes) -> None:
        self.audio_in.append(pcm16_16k)

    async def send_text_turn(self, t: str) -> None:
        self.sent_text_turns.append(t)

    async def send_reground(self, t: str, *, turn_complete: bool = True) -> None:
        self.regrounds.append(t)

    async def send_persona(self, t: str) -> None:
        self.personas.append(t)

    async def send_tool_response(self, fn_id, fn_name, **_kw) -> None:
        pass

    async def events(self):
        from core.openai.session import OpenAIRealtimeEvent as E
        for role, txt in self.script:
            if role == "B":
                yield E(kind="out_tr", text=txt)
                yield E(kind="turn_end")
            else:
                yield E(kind="in_tr", text=txt, is_final=True)
        if self.raise_closed:
            import websockets
            raise websockets.exceptions.ConnectionClosedError(None, None)


def _factory(holder, script=None, **sess_kw):
    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **kw):
        sess = FakeSession(script or [("B", "Hi! Let's start.")], **sess_kw)
        holder["session"] = sess
        holder["system_instruction"] = system_instruction
        holder["voice"] = voice
        holder["factory_kwargs"] = kw
        yield sess
    return _f


async def _run(session_factory, seeded, holder, *, plan=None, script=None,
               call_type="expression", factory=None, **sess_kw):
    start = {"type": "start", "character_id": seeded["character_id"], "call_type": call_type}
    if plan is not None:
        start["plan_override"] = plan
    ws = FakeWebSocket([{"type": "websocket.receive", "text": json.dumps(start)}])
    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"],
                   live_session_factory=factory or _factory(holder, script, **sess_kw))
    for _ in range(400):
        if not cs._analysis_tasks:
            break
        await asyncio.sleep(0.01)
    holder["ws"] = ws
    holder["frames"] = [json.loads(t) for t in ws.sent_text]
    return holder


def _started(holder):
    return next(f for f in holder["frames"] if f.get("type") == "call_started")


def _last_call(db, member_id):
    return (db.query(Call).filter(Call.member_id == member_id)
            .order_by(Call.call_id.desc()).first())


# --------------------------------------------------------------------------- #
# ① env 가 비면 라우팅이 안 돈다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_routing_is_off_by_default_and_keeps_the_previous_path(session_factory, seeded, monkeypatch):
    """⛔ 기본값은 꺼짐 — 되돌리기는 env 한 줄이고 코드 배포가 필요 없다."""
    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "")
    h = await _run(session_factory, seeded, {}, plan="premium")
    started = _started(h)
    assert started["course"] == "expression"
    # 종전 경로의 조각·예산 3값이 그대로 실린다
    assert started.get("fragment_index") == 1
    assert started.get("max_fragments") == 3
    assert started.get("remaining_s") == int(call_service.CALL_FRAGMENT_S)
    assert "[오늘의 표현" in h["system_instruction"], "Gemini 대본이 그대로여야 한다"


def test_live_openai_for_needs_both_course_and_key(monkeypatch):
    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "expression")
    monkeypatch.setattr(app_settings, "GPT_API_KEY", "")
    assert call_service.live_openai_for("expression") is False, "키 없으면 안 보낸다(R5)"
    monkeypatch.setattr(app_settings, "GPT_API_KEY", "k")
    assert call_service.live_openai_for("expression") is True
    assert call_service.live_openai_for("freetalk") is False
    assert call_service.live_openai_for("level_test") is False, "레벨테스트는 들어올 수 없다"
    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "expression,level_test")
    assert call_service.live_openai_for("level_test") is False, "목록에 적어도 막는다"


# --------------------------------------------------------------------------- #
# ②④ 대본·시드·프레임
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_openai_call_uses_the_gpt_script_and_omits_fragment_plumbing(
        session_factory, seeded, openai_on):
    h = await _run(session_factory, seeded, {}, plan="premium")
    started = _started(h)

    # ④ 조각·예산 3값을 **안 싣는다** — 클라가 자기 시간을 잰다.
    #   ⭐ `null` 이 아니라 **키 자체가 없다**(protocol.py:385-389 — None 이면 키를 뺀다)
    #     ⇒ 구버전 앱에도 조각이 생기기 **전과 같은 프레임**이 간다.
    for k in ("remaining_s", "max_fragments", "fragment_index"):
        assert k not in started, (k, started)
    # course 는 그대로 — 결과 화면 라우팅이 이 값을 쓴다.
    assert started["course"] == "expression"
    assert started["call_id"]

    # ② GPT 대본이다(Gemini 대본의 표식이 없다)
    si = h["system_instruction"]
    assert "[절대 금지" in si and si.index("입을 떼기 전") < 400
    assert "[오늘의 표현]" in si
    assert "[6번까지" in si or "번까지 다 돌았으면]" in si
    for gemini_mark in ("[퀴즈] 알림", "재접지", "이어서", "조각"):
        assert gemini_mark not in si, gemini_mark
    for close_word in ("마무리", "작별", "종료", "서버가 알린"):
        assert close_word not in si, close_word

    # 시드도 GPT 것 — 1번 항목의 표현이 시드에 없다(선공개 방지)
    seeds = h["session"].sent_text_turns
    assert seeds and "[지시]" in seeds[0]


@pytest.mark.asyncio
async def test_openai_engine_state_and_model(session_factory, seeded, openai_on, monkeypatch):
    seen = {}
    real = cs._run_session

    async def _spy(*a, **kw):
        seen["state"] = kw["state"]
        return await real(*a, **kw)
    monkeypatch.setattr(cs, "_run_session", _spy)
    await _run(session_factory, seeded, {}, plan="premium")
    st = seen["state"]
    assert st.live_engine == "openai"
    assert st.live_model == app_settings.OPENAI_REALTIME_MODEL
    assert st.live_vertex is None, "이 엔진엔 백엔드 축이 없다"
    # ⑥ 재개 핸들이 영영 None ⇒ 재개 kwarg 가 어댑터에 안 넘어간다
    assert st.resume_handle is None


# --------------------------------------------------------------------------- #
# ③ 통화 길이 = 플랜만 본다 → 백스톱이 저절로 따라 오른다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("plan,expect_s,expect_backstop", [
    ("premium", 900.0, 952.0),
    ("free", 300.0, 540.0),
])
async def test_call_duration_follows_the_plan_and_backstop_follows_the_duration(
        session_factory, seeded, openai_on, monkeypatch, plan, expect_s, expect_backstop):
    seen = {}
    real = cs._run_session

    async def _spy(*a, **kw):
        seen["state"] = kw["state"]
        return await real(*a, **kw)
    monkeypatch.setattr(cs, "_run_session", _spy)
    await _run(session_factory, seeded, {}, plan=plan)
    assert seen["state"].call_duration_s == expect_s
    # ⛔ 백스톱 상수·식은 안 건드렸다 — 입력만 바뀐다(R4).
    backstop = max(cs.ABSOLUTE_CALL_TIMEOUT_S,
                   seen["state"].call_duration_s + cs.SEED_TO_HANGUP_S + 30.0)
    assert backstop == expect_backstop


@pytest.mark.asyncio
async def test_server_does_not_cut_at_the_call_length(session_factory, seeded, openai_on, monkeypatch):
    """⛔ 서버가 900초에 **먼저** 끊으려 하지 않는다 — T23 이 그 경로를 지웠고 그대로다."""
    src = cs._watch_call_clock.__doc__ or ""
    assert "서버는 통화 길이로 종료하지 않는다" in src
    seen = {}
    real = cs._run_session

    async def _spy(*a, **kw):
        seen["state"] = kw["state"]
        return await real(*a, **kw)
    monkeypatch.setattr(cs, "_run_session", _spy)
    await _run(session_factory, seeded, {}, plan="premium")
    # ⭐ 예산 안전판의 입력은 **조각 상한(360)으로 깎이지 않은** 하루 잔여다.
    #   ⛔ 깎으면 15분 통화가 420초(=360+60)에 끊긴다 — 그게 접점 ① 의 실체였다.
    st = seen["state"]
    assert st.remaining_s == int(call_service.DAILY_BUDGET_S_BY_PLAN["premium"]), st.remaining_s
    assert st.remaining_s > int(call_service.CALL_FRAGMENT_S)
    # 안전판이 터지는 시각(= remaining + 60)이 통화 길이보다 **뒤**다 ⇒ 서버가 먼저 안 끊는다.
    assert st.remaining_s + 60 > st.call_duration_s


# --------------------------------------------------------------------------- #
# ⑤ 재접지 워처
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_reground_watcher_is_not_spawned_on_the_openai_call(
        session_factory, seeded, openai_on, monkeypatch):
    calls = {"n": 0}

    async def _spy(session, state):
        calls["n"] += 1
    monkeypatch.setattr(cs, "_reground_watch", _spy)
    await _run(session_factory, seeded, {}, plan="premium")
    assert calls["n"] == 0, "OpenAI 통화에 재접지 워처가 올라갔다"


@pytest.mark.asyncio
async def test_reground_watcher_still_spawned_when_routing_is_off(
        session_factory, seeded, monkeypatch):
    """⚠ 대조군 — 이 시험이 같이 통과해야 ①의 0 이 «라우팅 때문» 임이 증명된다."""
    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "")
    calls = {"n": 0}

    async def _spy(session, state):
        calls["n"] += 1
    monkeypatch.setattr(cs, "_reground_watch", _spy)
    await _run(session_factory, seeded, {}, plan="premium")
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_no_reground_note_is_ever_sent_on_the_openai_call(
        session_factory, seeded, openai_on):
    """수용 기준: 로그의 «재접지 얹기» 0건 ⇒ 세션이 받은 쪽지 0건."""
    h = await _run(session_factory, seeded, {}, plan="premium",
                   script=[("B", "Let's go."), ("U", "안녕하세요"), ("B", "Good.")])
    assert h["session"].regrounds == []
    assert h["session"].personas == []


# --------------------------------------------------------------------------- #
# ⑥ 안 만든 것들이 안 돈다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_fragment_split_and_no_fragment_saved_frame(session_factory, seeded, openai_on, monkeypatch):
    seen = {}
    real = cs._run_session

    async def _spy(*a, **kw):
        seen["state"] = kw["state"]
        return await real(*a, **kw)
    monkeypatch.setattr(cs, "_run_session", _spy)
    h = await _run(session_factory, seeded, {}, plan="premium")
    st = seen["state"]
    # 조각 상한 1 ⇒ 루프 차단기가 «조각 전환» 대신 종료 시드로 간다
    assert st.max_fragments == 1 and st.fragment_index == 1
    assert not st.fragment_end
    types = [f.get("type") for f in h["frames"]]
    assert "fragment_saved" not in types
    assert "call_ended" in types, "⛔ 생 close 금지 — _finish_call 이 정상 종료 신호를 보낸다"
    ended = next(f for f in h["frames"] if f["type"] == "call_ended")
    assert ended["reason"] == "done"


@pytest.mark.asyncio
async def test_no_compression_knobs_reach_the_adapter(session_factory, seeded, openai_on):
    h = await _run(session_factory, seeded, {}, plan="premium")
    kw = h["factory_kwargs"]
    # ⛔ 재개 핸들·백엔드는 넘기지 않는다(값이 None 이면 kwarg 자체를 안 넣는 규율).
    assert "resume_handle" not in kw
    assert "vertex" not in kw
    assert kw.get("model") == app_settings.OPENAI_REALTIME_MODEL


def test_reconnect_is_refused_for_the_openai_engine():
    """⛔ OpenAI 통화는 재연결하지 않는다 — 2세대는 캐시 프리픽스를 처음부터 다시 쌓는다."""
    import websockets

    state = cs._CallState()
    state.live_engine = "openai"
    state.call_start_ts = 0.0
    state.call_duration_s = 900.0
    eg = BaseExceptionGroup("x", [websockets.exceptions.ConnectionClosedError(None, None)])
    why = cs._reconnect_refusal(state, eg, 1.0)
    assert why and "OpenAI" in why
    # 대조군 — 같은 예외인데 Gemini 통화면 재연결한다(종전 동작 그대로)
    state.live_engine = ""
    assert cs._reconnect_refusal(state, eg, 1.0) == ""


# --------------------------------------------------------------------------- #
# ⑦ usage_engine 태그
# --------------------------------------------------------------------------- #
def test_openai_engine_tag_is_queryable_in_one_query():
    tag = svc.openai_engine_tag("gpt-realtime-2.1-mini")
    assert tag == "live:openai-realtime-2.1-mini"
    assert "openai" in tag, "DELETE … WHERE usage_engine LIKE '%openai%' 가 성립해야 한다"
    assert tag.startswith("live:"), "모드 자리는 live 다(build_engine_tag 계약)"
    # 모델이 바뀌면 태그가 갈린다(집계가 섞이지 않는다)
    assert svc.openai_engine_tag("gpt-realtime-2.1") == "live:openai-realtime-2.1"
    # 모르는 이름도 openai 로 표시된다
    assert svc.openai_engine_tag("weird-model") == "live:openai-weird-model"
    assert svc.openai_engine_tag(None) == svc.ENGINE_LIVE_OPENAI
    # Gemini 태그는 그대로
    assert svc.build_engine_tag("live", "gemini-3.1-flash-live-preview") == \
        "live:gemini-3.1-flash-live-preview"


@pytest.mark.asyncio
async def test_usage_row_is_tagged_with_the_openai_engine(session_factory, seeded, openai_on):
    from core.openai.session import OpenAIRealtimeEvent as E

    class _Usage(FakeSession):
        async def events(self):
            from core.openai.session import usage_shim
            yield E(kind="out_tr", text="Hi")
            yield E(kind="usage", usage=usage_shim({
                "input_tokens": 3200, "output_tokens": 180, "total_tokens": 3380,
                "input_token_details": {
                    "audio_tokens": 2900, "text_tokens": 300, "cached_tokens": 2560,
                    "cached_tokens_details": {"audio_tokens": 2500, "text_tokens": 60}},
                "output_token_details": {"audio_tokens": 160, "text_tokens": 20},
            }))
            yield E(kind="turn_end")

    holder: dict = {}

    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **kw):
        sess = _Usage([])
        holder["session"] = sess
        yield sess

    await _run(session_factory, seeded, holder, plan="premium", factory=_f)
    db = session_factory()
    try:
        call = _last_call(db, seeded["member_id"])
        assert call.usage_engine == "live:openai-realtime-2.1-mini"
        assert call.usage_in_audio == 2900 and call.usage_in_text == 300
        assert call.usage_out_audio == 160 and call.usage_out_text == 20
        assert call.usage_json["cached_mod"] == {"AUDIO": 2500, "TEXT": 60}
        # ⭐ 원가가 캐시 할인을 **실제로** 반영한다
        cost, unknown = svc.estimate_call_cost_usd(
            call.usage_engine, in_audio=call.usage_in_audio, in_text=call.usage_in_text,
            out_audio=call.usage_out_audio, out_text=call.usage_out_text,
            usage_json=call.usage_json)
        full, _ = svc.estimate_call_cost_usd(
            call.usage_engine, in_audio=call.usage_in_audio, in_text=call.usage_in_text,
            out_audio=call.usage_out_audio, out_text=call.usage_out_text, usage_json={})
        assert cost < full, "캐시 할인이 반영되지 않았다"
        assert not unknown
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# ⑧ 표정 — 무료 OFF / 유료 ON
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("plan,face_on", [("premium", True), ("free", False)])
async def test_face_follows_the_plan(session_factory, seeded, openai_on, monkeypatch, plan, face_on):
    monkeypatch.setattr(app_settings, "LIVE_FACE_SPIKE", True)
    h = await _run(session_factory, seeded, {}, plan=plan)
    si = h["system_instruction"]
    kw = h["factory_kwargs"]
    assert ("[표정]" in si) is face_on, si[-200:]
    assert ("tools" in kw) is face_on, kw.keys()
    if face_on:
        # 어댑터는 리스트 **안을 보지 않는다**(있나 없나만) — 자기 스키마로 선언한다.
        from core.openai.session import build_session_config
        cfg = build_session_config(system_instruction="x", with_face_tool=bool(kw["tools"]))
        assert cfg["session"]["tools"][0]["name"] == "set_face"


# --------------------------------------------------------------------------- #
# ⑨ 원가 — cached 기본 0 이면 **기존과 바이트 동일**
# --------------------------------------------------------------------------- #
def test_cost_is_byte_identical_without_cached_args():
    """⛔ 기존 호출부(Gemini·옛 캐스케이드 행)가 같은 값을 내야 한다."""
    kwargs = dict(in_audio=300628, in_text=245338, out_audio=10, out_text=5)
    p = svc.LIVE_TOKEN_PRICE_USD
    hand = (kwargs["in_audio"] * p["in_audio"] + kwargs["in_text"] * p["in_text"]
            + kwargs["out_audio"] * p["out_audio"] + kwargs["out_text"] * p["out_text"]) / 1_000_000
    assert svc.estimate_usage_cost_usd(**kwargs) == hand
    # cached 를 0 으로 **명시**해도 같다
    assert svc.estimate_usage_cost_usd(**kwargs, cached_in_audio=0, cached_in_text=0) == hand
    # Gemini 엔진을 명시해도 같다(그 표엔 캐시 칸이 없다 — 주더라도 깎이지 않는다)
    assert svc.estimate_usage_cost_usd(
        **kwargs, engine="live:gemini-native-audio", cached_in_audio=1000) == hand
    # estimate_call_cost_usd 경유도 같다
    assert svc.estimate_call_cost_usd(None, **kwargs)[0] == hand


def test_cost_cached_discount_uses_the_openai_table():
    eng = "live:openai-realtime-2.1-mini"
    base = svc.estimate_usage_cost_usd(in_audio=1_000_000, engine=eng)
    assert abs(base - 10.00) < 1e-9, "정가 $10/1M"
    # 전량 캐시 ⇒ $0.30/1M
    all_cached = svc.estimate_usage_cost_usd(
        in_audio=1_000_000, cached_in_audio=1_000_000, engine=eng)
    assert abs(all_cached - 0.30) < 1e-9
    assert all_cached < base
    # 실측 비율(89.8%)이면 그 사이 어딘가
    real = svc.estimate_usage_cost_usd(in_audio=1_000_000, cached_in_audio=898_000, engine=eng)
    assert 0.30 < real < 10.00
    # 텍스트도 같은 축
    assert svc.estimate_usage_cost_usd(in_text=1_000_000, engine=eng) > \
        svc.estimate_usage_cost_usd(in_text=1_000_000, cached_in_text=1_000_000, engine=eng)
    # 출력 단가(공식 $20 / $2.40)
    assert abs(svc.estimate_usage_cost_usd(out_audio=1_000_000, engine=eng) - 20.00) < 1e-9
    assert abs(svc.estimate_usage_cost_usd(out_text=1_000_000, engine=eng) - 2.40) < 1e-9


def test_cost_cached_cannot_go_negative():
    """필드 뜻이 바뀌어 cached > in 이 와도 음수 원가가 나오면 안 된다."""
    eng = "live:openai-realtime-2.1-mini"
    v = svc.estimate_usage_cost_usd(in_audio=100, cached_in_audio=10_000, engine=eng)
    assert v > 0


def test_cascade_rows_still_fall_back_to_unknown():
    """⛔ 캐스케이드 행이 같은 함수를 탄다 — 그 폴백이 안 깨졌다."""
    cost, unknown = svc.estimate_call_cost_usd("cascade:stt+llm+tts", in_text=1000)
    assert cost == 0.0 and unknown == ["engine:cascade:stt+llm+tts"]


# --------------------------------------------------------------------------- #
# 서버가 쥐는 것 ① 퀴즈 창 — **OpenAI 경로에서도 서버가 연다**
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_server_opens_the_quiz_window_on_the_openai_call(
        session_factory, seeded, openai_on, monkeypatch):
    """⭐ GPT 는 퀴즈를 **스스로 안 열었다**(0회·3라운드) ⇒ 서버 T16 상태기계가 큐를 얹는다.

    ⛔ 이 경로가 재접지 워처와 **다른 자리**에 있다는 것이 요점이다 — 워처를 안 올려도
      큐는 마이크 펌프(`_maybe_attach_reground_on_mic` → `_attach_quiz_cue`)가 얹는다.
      그 둘이 한 자리였다면 재접지를 끄는 순간 퀴즈가 통째로 죽었다.
    """
    import struct

    from domains.learning.repository import curriculum_repository as repo

    db = session_factory()
    lesson1 = repo.lesson_by_no(db, "ko", 1)
    surfaces = [it.surface for _li, it in repo.lesson_items(db, lesson1.lesson_id)]
    db.close()

    seen: dict = {}
    real = cs._run_session

    async def _spy(*a, **kw):
        seen["state"] = kw["state"]
        return await real(*a, **kw)
    monkeypatch.setattr(cs, "_run_session", _spy)

    holder: dict = {}

    class _Sess(FakeSession):
        async def events(self):
            from core.openai.session import OpenAIRealtimeEvent as E
            # 비버가 1·2·3번을 소개 → 미출제 3개 ⇒ 큐 arm. 4번 소개로 «다음 항목 소개» =
            # 큐 정리 조건 ③ 을 만족시킨다(안 하면 큐가 «항목 3 미정리» 로 보류된다 — 설계대로다).
            for s in surfaces[:4]:
                yield E(kind="out_tr", text='이렇게 말해요: "%s"' % s)
                yield E(kind="turn_end")
            await asyncio.sleep(0.6)            # 마이크 프레임이 도착할 시간을 준다

    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **kw):
        sess = _Sess([])
        holder["session"] = sess
        yield sess

    # 사람이 말하는 세기의 프레임(RMS 가 REGROUND_VOICE_RMS 를 넘어야 큐가 얹힌다)
    loud = struct.pack("<320h", *([8000, -8000] * 160))
    assert cs.frame_rms(loud) > cs.REGROUND_VOICE_RMS

    async def _wait():
        await asyncio.sleep(0.25)

    start = {"type": "start", "character_id": seeded["character_id"],
             "call_type": "expression", "plan_override": "premium"}
    ws = FakeWebSocket([
        {"type": "websocket.receive", "text": json.dumps(start)},
        _wait,
        {"type": "websocket.receive", "bytes": loud},
        _wait,
    ])
    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"], live_session_factory=_f)
    for _ in range(400):
        if not cs._analysis_tasks:
            break
        await asyncio.sleep(0.01)

    st = seen["state"]
    assert len(st.covered_nums) >= 3, st.covered_nums
    cues = holder["session"].regrounds
    assert cues, "서버가 퀴즈 큐를 얹지 않았다(마이크 훅이 안 돌았다는 뜻이다)"
    assert st.expr_quiz_cue_pending is None and st.expr_quiz_awaiting_open is True
    # ⭐ 큐는 **재접지 쪽지가 아니다** — 재접지는 0건이어야 한다(워처를 안 올렸으므로 arm 0).
    assert st.reground_count == 0


# --------------------------------------------------------------------------- #
# ⭐⭐ 주입 경로 격리 — **OpenAI 통화에 Gemini 문구가 한 글자도 안 들어간다**
#   (2026-10-05 사장님: 「잠금 지시문을 지피티가 사용하지마. 따로 작성해」)
# --------------------------------------------------------------------------- #
def _gemini_injected_strings() -> dict:
    """OpenAI 통화 경로가 **쓸 수 있었던** Gemini 주입 문구 전수.

    ⛔ 새 주입 문구가 생기면 여기에도 더해라 — 이 목록이 비면 아래 시험이 조용히 통과한다.
    """
    from core.prompts.expression import NUDGE_SEED_1_EXPRESSION, seed_expression_opening
    from core.prompts.locked import seeds as gem
    from domains.learning.realtime import seed_bundle as sb

    return {
        "loop_break": gem.LOOP_BREAK_NOTE,
        "drill_move_on": gem.EXPRESSION_DRILL_MOVE_ON,
        "resume_after_slip": sb.GEMINI_RESUME_SEED,
        "nudge_1_expression": NUDGE_SEED_1_EXPRESSION,
        "nudge_2_normal": gem.NUDGE_SEED_2_NORMAL,
        "close_seed": gem.close_seed_normal("TAG"),
        "quiz_cue": gem.expression_quiz_cue("<L>", 1, retry=False,
                                            locale_label="<N>", target="<T>"),
        "quiz_set_reminder": gem.expression_quiz_set_reminder("<L>"),
        "seed_opening": seed_expression_opening("한국어"),
    }


def _fingerprints(text: str, n: int = 24) -> set:
    """문구의 n-그램 지문 — 「조금 고쳐 섞어 넣기」까지 잡는다(완전일치만 보면 못 잡는다)."""
    t = "".join((text or "").split())
    return {t[i:i + n] for i in range(0, max(0, len(t) - n + 1))}


@pytest.mark.asyncio
async def test_no_gemini_seed_string_is_ever_injected_into_an_openai_call(
        session_factory, seeded, openai_on, monkeypatch):
    """⭐ 가짜 세션으로 통화를 돌려 **보내진 모든 텍스트**를 모으고 Gemini 문구와 대조한다.

    ⛔ 이 시험이 AST 격리 시험을 보완한다 — AST 는 `core/openai/` **안**만 본다. 주입은
      `call_session`(패키지 밖)이 하므로, 거기서 Gemini 상수를 꺼내 쓰면 AST 는 못 잡는다.
    """
    import struct

    from domains.learning.repository import curriculum_repository as repo

    db = session_factory()
    lesson1 = repo.lesson_by_no(db, "ko", 1)
    surfaces = [it.surface for _li, it in repo.lesson_items(db, lesson1.lesson_id)]
    db.close()

    seen = {}
    real = cs._run_session

    async def _spy(*a, **kw):
        seen["state"] = kw["state"]
        return await real(*a, **kw)
    monkeypatch.setattr(cs, "_run_session", _spy)

    holder = {}
    repeated = "이 표현은 이렇게 씁니다. 천천히 한번 해 보세요."   # LOOP_MIN_CHARS(15) 초과

    class _Sess(FakeSession):
        async def events(self):
            from core.openai.session import OpenAIRealtimeEvent as E
            for s in surfaces[:4]:          # 큐가 서고 정리까지(1·2·3 arm → 4 로 «다음 항목 소개»)
                yield E(kind="out_tr", text='이렇게 말해요: "%s"' % s)
                yield E(kind="turn_end")
            # ⚠ 루프 차단기는 `learner_spoke` 뒤에만 돈다(인사 구간은 보지 않는다) —
            #   학습자 전사 1건을 먼저 흘린다. 「네」는 어느 항목 표면형과도 안 겹친다.
            yield E(kind="in_tr", text="네", is_final=True)
            for _ in range(2):              # 같은 문장 2회 연속 → 루프 차단 ① 주입
                yield E(kind="out_tr", text=repeated)
                yield E(kind="turn_end")
            await asyncio.sleep(0.6)

    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **kw):
        sess = _Sess([])
        holder["session"] = sess
        holder["system_instruction"] = system_instruction
        yield sess

    loud = struct.pack("<320h", *([8000, -8000] * 160))

    async def _wait():
        await asyncio.sleep(0.25)

    start = {"type": "start", "character_id": seeded["character_id"],
             "call_type": "expression", "plan_override": "premium"}
    ws = FakeWebSocket([
        {"type": "websocket.receive", "text": json.dumps(start)},
        _wait,
        {"type": "websocket.receive", "bytes": loud},
        _wait,
    ])
    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"], live_session_factory=_f)
    for _ in range(400):
        if not cs._analysis_tasks:
            break
        await asyncio.sleep(0.01)

    sess = holder["session"]
    sent = list(sess.sent_text_turns) + list(sess.regrounds) + list(sess.personas)
    assert sent, "주입이 하나도 없었다 — 시험이 아무것도 안 봤다"

    # ① 보낸 것 중 어느 것도 Gemini 문구가 아니다(완전일치 + 24-그램 지문)
    gem = _gemini_injected_strings()
    for text in sent:
        fp = _fingerprints(text)
        for name, gem_text in gem.items():
            assert text != gem_text, (name, text[:60])
            overlap = fp & _fingerprints(gem_text)
            assert not overlap, (name, sorted(overlap)[:1], text[:60])

    # ② 실제로 GPT 문구가 나갔다 — 선톡 시드 · 루프 차단 · 퀴즈 큐
    from core.openai.prompts import seeds as gpt_seeds

    joined = "\n".join(sent)
    assert "[지시]" in joined, "GPT 선톡 시드가 없다"
    assert gpt_seeds.LOOP_BREAK in sent, "GPT 루프 차단 쪽지가 안 나갔다"
    assert any(t.startswith(gpt_seeds.NOTE_TAG) and "되묻는 차례" in t for t in sess.regrounds), \
        "GPT 퀴즈 큐가 안 나갔다"

    # ③ 무음 1·2·3단은 시계(60+10+12초)가 필요해 e2e 로 못 돌린다 ⇒ **주입 자리가 읽는
    #    state 값**을 본다. `_inject_nudge`/`_inject_close_seed` 는 이 값을 **그대로** 보낸다.
    st = seen["state"]
    assert st.nudge_seed_1 == gpt_seeds.NUDGE_1
    assert st.nudge_seed_2 == gpt_seeds.NUDGE_2
    assert st.close_seed == gpt_seeds.SILENCE_CLOSE
    assert st.seeds.name == "openai"
    assert st.seeds.drill_move_on == gpt_seeds.DRILL_MOVE_ON
    assert st.seeds.resume_after_slip == gpt_seeds.RESUME_AFTER_SLIP


@pytest.mark.asyncio
async def test_gemini_call_keeps_its_own_seeds_byte_identical(
        session_factory, seeded, monkeypatch):
    """⚠ 대조군 — 라우팅이 꺼지면 **종전 시드 그대로**다(묶음이 아무것도 덮지 않는다)."""
    from core.prompts.expression import NUDGE_SEED_1_EXPRESSION
    from core.prompts.locked import seeds as gem
    from domains.learning.realtime import seed_bundle as sb

    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "")
    seen = {}
    real = cs._run_session

    async def _spy(*a, **kw):
        seen["state"] = kw["state"]
        return await real(*a, **kw)
    monkeypatch.setattr(cs, "_run_session", _spy)
    await _run(session_factory, seeded, {}, plan="premium")
    st = seen["state"]
    assert st.seeds is sb.GEMINI and st.live_engine == ""
    assert st.nudge_seed_1 == NUDGE_SEED_1_EXPRESSION
    assert st.nudge_seed_2 == gem.NUDGE_SEED_2_NORMAL
    assert "통화종료" in st.close_seed, st.close_seed[:40]   # 종전 종료 태그가 그대로 박힌다
    assert st.seeds.loop_break == gem.LOOP_BREAK_NOTE
    assert st.seeds.drill_move_on == gem.EXPRESSION_DRILL_MOVE_ON
    assert st.seeds.resume_after_slip == cs._RESUME_SEED


def test_injection_sites_read_the_bundle_not_the_locked_constants():
    """⛔ 새 주입 자리가 Gemini 상수를 **직접** 쓰면 묶음이 무의미해진다 — 소스로 잠근다.

    ⚠ 소스 검사인 이유: 그 자리는 통화 중 특정 상태에서만 돌아 e2e 로 다 밟을 수 없다
      (무음 3단·드릴 상한·미끄러짐 복구는 시계·턴 수가 필요하다).
    """
    import inspect
    import re

    # ⚠ 앞에 `_` 나 글자가 붙은 것은 우리 함수 이름이다(`_expression_quiz_cue` = 재료 조립기).
    #   잠금 심볼을 **그대로** 부르는 자리만 잡는다.
    locked = [re.compile(r"(?<![\w])" + re.escape(n)) for n in (
        "LOOP_BREAK_NOTE", "EXPRESSION_DRILL_MOVE_ON",
        "expression_quiz_cue(", "expression_quiz_set_reminder(")]
    for fn in (cs._loop_breaker_on_turn_end, cs._inject_drill_move_on,
               cs._inject_quiz_set_reminder, cs._expression_quiz_cue,
               cs._inject_resume_seed, cs._inject_nudge, cs._inject_close_seed):
        src = inspect.getsource(fn)
        for pat in locked:
            assert not pat.search(src), (fn.__name__, pat.pattern)


# --------------------------------------------------------------------------- #
# OPENAI_SELF_QUIZ — ⛔ 플래그가 **실제로 돌아가는 state** 에 실리는가
#   실측(call 1747): 스냅샷 로그는 떴는데 «arm 생략» 은 0회였다. 원인은
#   call_session 이 `_CallState()` 를 **두 번** 만들고(:3704·:3945) 내가 첫 번째에
#   심었기 때문 — 두 번째가 그걸 덮어써서 플래그가 조용히 죽었다.
#   ⇒ 이 시험은 「심은 자리」가 아니라 **arm 가드가 보는 값**을 본다.
# --------------------------------------------------------------------------- #
def test_the_self_quiz_flag_reaches_the_state_that_arms_the_cue(monkeypatch):
    """⛔ 이게 깨지면 플래그가 지시문만 바꾸고 서버 큐는 그대로 얹힌다(조용한 실패)."""
    import inspect

    import domains.learning.realtime.call_session as cs

    src = inspect.getsource(cs.run_call)
    # ① state 가 몇 번 만들어지나 — 늘어나면 이 시험의 전제가 바뀐다
    n = src.count("state = _CallState()")
    assert n == 2, (
        "_CallState() 생성 횟수가 %d 로 바뀠다 — 플래그를 심는 자리를 다시 확인해라" % n)
    # ② 플래그 세팅이 **마지막** 생성 뒤에 있어야 한다
    last_new = src.rindex("state = _CallState()")
    set_at = src.rindex("state.expr_self_quiz =")
    assert set_at > last_new, (
        "expr_self_quiz 를 마지막 _CallState() **앞**에서 심고 있다 — 그 객체는 버려진다")
    # ③ arm 가드는 state 를 본다(settings 가 아니다 — 그 스코프엔 없다)
    arm = inspect.getsource(cs._arm_expression_quiz_cue)
    assert "state.expr_self_quiz" in arm
    assert "OPENAI_SELF_QUIZ" not in arm.replace("OPENAI_SELF_QUIZ)", "").replace(
        "(OPENAI_SELF_QUIZ", ""), "arm 가드가 settings 를 직접 읽으면 NameError 가 난다"


def test_the_prompt_switches_with_the_flag():
    """지시문 블록이 플래그로 갈린다 — 서버 큐 판([퀴즈]) vs 자가 개시판([되묻기])."""
    from core.openai.prompts import expression as ex

    items = [{"no": 1, "obj": "고마워요", "des": "thank you"}]
    kw = dict(role="R", personality="P", locale_label="영어(English)", items=items,
              quiz_group=3, target_language="한국어", name="S")
    off = ex.build_expression_instruction(**kw)
    on = ex.build_expression_instruction(self_quiz=True, **kw)
    assert "[퀴즈]" in off and "네가 정하지 않는다" in off
    assert "[되묻기 — 3개마다]" in on and "다 내고 나면" in on
    assert "네가 정하지 않는다" not in on, "자가 개시판에 «네가 정하지 않는다» 가 남았다"


# --------------------------------------------------------------------------- #
# 프리토킹 라우팅(2026-10-08) — 게이트 + **배선 구조**
#   ⚠ 통화 하네스(`_run`)는 expression 코스로 시드돼 있어 freetalk 실행 경로를 못 태운다.
#     그래서 ①게이트는 단위로, ②배선은 소스 구조로 못박는다(이 파일의 기존 방식).
# --------------------------------------------------------------------------- #
def test_live_openai_for_accepts_freetalk_when_listed(monkeypatch):
    monkeypatch.setattr(app_settings, "GPT_API_KEY", "k")
    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "expression")
    assert call_service.live_openai_for("freetalk") is False, "목록에 없으면 Gemini 다"
    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "expression,freetalk")
    assert call_service.live_openai_for("freetalk") is True
    assert call_service.live_openai_for("expression") is True
    assert call_service.live_openai_for("chat") is False, "chat 은 아직 배선이 없다"
    assert call_service.live_openai_for("level_test") is False


def test_freetalk_engine_branch_is_wired_with_gemini_in_the_else():
    """⛔ 엔진 분기가 **프리토킹 자리에도** 있고, Gemini 대본이 그 `else` 아래에 있다.

    env 가 비면 `live_openai_for` 가 False 라 else 가 종전 그대로 돈다(바이트 동일).
    이 구조가 깨지면 두 대본이 같이 조립되거나 Gemini 가 영구히 안 불린다.
    """
    import inspect
    src = inspect.getsource(cs.run_call)

    gpt = src.index("openai_freetalk.build_freetalk_instruction(")
    # ⚠ `build_freetalk_instruction(` 는 **옛 경로**(비-cur `elif call_type == "freetalk"`)에도
    #   있다. 여기서 보는 것은 cur 분기의 그것이니 **GPT 분기 뒤**에서 찾는다.
    gem = src.index("system_instruction = build_freetalk_instruction(", gpt)

    between = src[gpt:gem]
    assert "\n                else:\n" in between, \
        "GPT 분기와 Gemini 호출 사이에 else 가 없다 — 둘이 같이 조립된다"
    assert "use_openai = call_service.live_openai_for(call_type)" in src[:gpt], \
        "프리토킹 분기가 게이트를 안 본다"

    # 시드도 GPT 것으로 갈린다
    assert "openai_freetalk.seed_freetalk_opening(" in src
    assert "seed_freetalk_lesson_opening(" in src, "Gemini 시드가 else 쪽에 남아 있어야 한다"


def test_freetalk_gpt_branch_passes_the_chapter_brief():
    """⭐ 차시 브리프 네 칸이 **그대로** GPT 대본으로 간다(사장님 지시: 챕터만 참조)."""
    import inspect
    src = inspect.getsource(cs.run_call)
    i = src.index("openai_freetalk.build_freetalk_instruction(")
    call = src[i:i + 1400]
    for arg in ("situation=_brief.situation", "partner=_brief.partner",
                "items=_brief.items", "probes=_brief.probes"):
        assert arg in call, arg
    assert "max_sentences=FREETALK_MAX_SENTENCES" in call, "역할극 문장 수 상한이 빠졌다"


# --------------------------------------------------------------------------- #
# 자유대화 라우팅(2026-10-08) — ⛔ 분기 자리가 **프리토킹과 다르다**
# --------------------------------------------------------------------------- #
def test_live_openai_for_accepts_chat_when_listed(monkeypatch):
    monkeypatch.setattr(app_settings, "GPT_API_KEY", "k")
    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "expression,freetalk")
    assert call_service.live_openai_for("chat") is False, "목록에 없으면 Gemini 다"
    monkeypatch.setattr(app_settings, "OPENAI_REALTIME_COURSES", "expression,freetalk,chat")
    assert call_service.live_openai_for("chat") is True
    assert call_service.live_openai_for("level_test") is False, "레벨테스트는 영구 차단"


def test_chat_engine_branch_is_outside_the_cur_block():
    """⛔⛔ chat 은 **cur 라우트를 타지 않는다** — cur 가 내는 코스는 expression·freetalk 뿐.

    그래서 분기가 `elif call_type == "chat":` 쪽에 있어야 한다. cur 블록 안에 넣으면
    **닿지 않아** 영구히 Gemini 로 간다.
    """
    import inspect
    src = inspect.getsource(cs.run_call)

    chat_elif = src.index('elif call_type == "chat":')
    gpt = src.index("openai_chat.build_chat_instruction(")
    cur_block = src.index('if cur_open.course == "expression":')
    assert chat_elif < gpt < cur_block, \
        "GPT chat 분기가 `elif call_type == \"chat\"` 안이 아니다 — cur 블록은 chat 에 안 닿는다"

    # Gemini 대본은 그 else 아래에 남아 있다
    gem = src.index("system_instruction = build_chat_instruction(", gpt)
    assert "\n            else:\n" in src[gpt:gem], \
        "GPT 분기와 Gemini 호출 사이에 else 가 없다 — 둘이 같이 조립된다"


def test_chat_gpt_branch_passes_memory_and_interests():
    """⭐ 기억·관심사가 그대로 가고, 「아는 척」 시드가 **폴백을 갖는다**."""
    import inspect
    src = inspect.getsource(cs.run_call)
    i = src.index("openai_chat.build_chat_instruction(")
    call = src[i:i + 1400]
    assert "memory=chat_memory_dict" in call, "기억이 안 넘어간다"
    assert 'interests=setup["interests"]' in call, "관심사가 안 넘어간다"
    # 시드: 기억이 빈약하면 평범한 선톡으로 폴백해야 한다(빈 문자열이 그대로 나가면 안 된다)
    assert "openai_chat.seed_chat_opening(" in src and "seed_chat_plain_opening(" in src


def test_chat_branch_does_not_touch_call_id_before_it_exists():
    """⛔⛔ chat 분기는 **통화 행이 만들어지기 전**에 돈다 — `call_id` 를 쓰면 WS 가 죽는다.

    2026-10-08 실측: cur 블록(expression·freetalk)의 로그를 chat 분기에 베꼈더니
    `UnboundLocalError: cannot access local variable 'call_id'` 로 통화가 **열리자마자**
    끊겼고 통화 행도 안 남아 원인이 안 보였다(`call_session.py:3619`).
    ⚠ 구조 시험·통화 하네스가 둘 다 못 잡았다 — 하네스는 expression 만 태운다.
    """
    import inspect
    src = inspect.getsource(cs.run_call)

    where_created = src.index("call_id = await")
    chat_start = src.index('elif call_type == "chat":')
    chat_end = src.index("openai_chat.build_chat_instruction(")
    # chat 분기 본문(GPT·Gemini 양쪽)은 call_id 생성보다 **앞**에 있다 — 그 전제부터 못박는다.
    assert chat_start < where_created, "chat 분기가 call_id 생성 뒤로 옮겨졌다면 이 시험을 다시 설계해라"

    # 그 분기에서 call_id 를 **읽지 않는다**(생성 전이므로).
    # ⚠ 낱말 경계로 본다 — `continues_call_id`·`chain_call_id` 는 다른 변수다(인자로 들어와
    #   이미 바인딩돼 있어 안전하다).
    import re
    # ⚠ **주석은 걷어낸다** — 이 시험은 코드를 보는 것이고, 정작 「`call_id` 를 찍지
    #   말라」는 경고 주석이 자기 자신을 잡는 일이 없게 한다.
    src_lines = src[chat_start:where_created].split(chr(10))
    body = chr(10).join(l for l in src_lines if not l.strip().startswith(chr(35)))
    hit = re.search(r"(?<![\w])call_id(?![\w])", body)
    assert not hit, (
        "chat 분기가 call_id 를 쓴다 — 생성 전이라 UnboundLocalError 가 난다: %r"
        % (body[max(0, hit.start() - 60):hit.end() + 20] if hit else "")
    )
    assert chat_end < where_created, "GPT chat 대본 조립도 call_id 생성 전이다(전제 확인)"
