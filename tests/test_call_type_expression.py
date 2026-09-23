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
from domains.commerce.models.subscribe import Subscribe
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
        # QA C3-③(2026-09-22): expression·freetalk 명시는 admin 전용이다 — 이 파일은 그
        # 라우팅 자체를 시험하므로 기본 회원을 admin 으로 둔다. 비admin 라우팅(→auto)은
        # test_non_admin_explicit_expression_falls_back_to_auto 가 따로 admin 을 내린다.
        member = Member(language="en", korean_level=1, onboarding_completed=True,
                        auth_user_id="auth-member", role="admin")
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
        holder["kw"] = dict(_kw)          # model·vertex·tools — 플랜 분기 관측용
        yield sess

    return _f


async def _run(session_factory, seeded, call_type: str | None, holder: dict, *, extra: dict | None = None):
    start = {"type": "start", "character_id": seeded["character_id"], **(extra or {})}
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
async def test_call_type_unset_now_defaults_to_auto_not_chat(session_factory, seeded) -> None:
    """C3(2026-09-22, D3): call_type 미전송(구버전 앱·알람) → 레벨 미확정이면 레벨테스트,
    아니면 **auto(학습)**. 옛 기본값 "normal"(→ 지금의 chat)은 더 이상 기본이 아니다.

    ⚠ 이 시드 DB 엔 cur_lesson 이 없어(CUR_ENABLED 시드 부재) auto 가 옛 표현학습 경로로
    폴백한다(call_session.py:3086) — 그래서 여기서는 "expression" 이 관측된다. 커리큘럼이
    실제로 있는 언어에서는 decide_course 가 expression/freetalk 중 하나를 고른다."""
    await _run(session_factory, seeded, None, {})
    db = session_factory()
    try:
        assert db.query(Call).one().call_type == "expression", \
            "미전송이 chat(옛 normal)으로 떨어졌다 — auto(학습) 이어야 한다"
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wire_call_type,expected_course",
    [
        (None, "expression"),          # 미전송, 레벨 확정 → auto(학습) → 폴백 expression(cur 시드 없음)
        ("auto", "expression"),        # 명시 auto — 미전송과 같은 결과
        ("chat", "chat"),              # 자유대화 — 그대로
        ("normal", "chat"),            # 구버전 앱 호환 — normal 은 chat 으로 흡수
        ("expression", "expression"),  # 명시(admin·개발자도구용으로 유지, 서버는 그대로 수용)
        ("freetalk", "freetalk"),      # 〃
    ],
)
async def test_call_type_combination_table_routes_to_the_right_course(
    session_factory, seeded, wire_call_type: str | None, expected_course: str,
) -> None:
    """C3(2026-09-22, D3) 요구 시험표 — call_type 조합(미전송·auto·chat·normal·expression·
    freetalk) → 실제 코스. level_test 조합은 test_level_test_call.py 가 따로 지킨다
    (이 시드는 korean_level 이 이미 확정돼 있어 그 갈래를 여기서 못 만든다)."""
    await _run(session_factory, seeded, wire_call_type, {})
    db = session_factory()
    try:
        assert db.query(Call).one().call_type == expected_course
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
async def test_chat_calls_keep_the_cap_of_ten(
    session_factory, seeded, monkeypatch,
) -> None:
    """⚠ 옛 `normal`(C3 로 chat 개명)은 한 글자도 안 바뀐다 — 상한 10 은 일반 통화의 계약이다."""
    seen: list[list[str]] = []
    orig = cs._reground_instruction
    monkeypatch.setattr(
        cs, "_reground_instruction",
        lambda items, target: (seen.append(list(items)), orig(items, target))[1],
    )
    await _run(session_factory, seeded, "chat", {})
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
async def test_expression_has_no_hints_but_freetalk_does(session_factory, seeded, monkeypatch) -> None:
    """⛔ D7 — 표현학습은 힌트 없음(퀴즈 정답을 그대로 보여주게 된다). ⭐ 2026-09-12 사장님: «프리토킹에는 힌트가 보여야 한다» —
    프리토킹(옛 경로 포함)은 다시 켠다. 서버가 push 안 하면 화면에 안 뜨므로 hint_ctx 로 잰다."""
    spawned: dict[str, list[str]] = {"expression": [], "freetalk": []}
    for ct in ("expression", "freetalk"):
        monkeypatch.setattr(cs, "_spawn_hint_task",
                            lambda ws, st, _ct=ct: spawned[_ct].append(st.hint_ctx and "on" or "off"))
        await _run(session_factory, seeded, ct, {})
    assert "on" not in spawned["expression"]
    assert spawned["freetalk"] and all(x == "on" for x in spawned["freetalk"])


@pytest.mark.asyncio
async def test_chat_calls_still_get_hints(session_factory, seeded, monkeypatch) -> None:
    """⚠ 옛 `normal`(C3 로 chat 개명)·`level_test` 는 종전 그대로다(hint_used 강등 경로 포함)."""
    seen: list[bool] = []
    orig = cs._hint_instruction
    monkeypatch.setattr(cs, "_hint_instruction",
                        lambda *a, **k: (seen.append(True), orig(*a, **k))[1])
    await _run(session_factory, seeded, "chat", {})
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
# ⑥ 일일 한도 / 하루 통화 총량 예산
# --------------------------------------------------------------------------- #
def test_level_test_has_a_daily_limit() -> None:
    """⛔ 표에 없으면 `is_daily_limit_reached` 가 «막지 않는다» 로 떨어져 **무제한**이 된다.

    Live 는 통화당 원가가 나가므로 그건 조용한 비용 구멍이다.
    """
    assert call_service.DAILY_CALL_LIMIT.get("level_test")


@pytest.mark.parametrize("call_type", ["chat", "expression", "freetalk"])
def test_non_level_test_call_types_are_not_count_limited(call_type: str) -> None:
    """⭐⭐ C4(2026-09-23, D4): chat·expression·freetalk 는 **횟수 표에서 빠졌다** —
    하루 통화 총량(분) 예산(daily_budget_s·daily_budget_exceeded)이 대체했기 때문이다.
    이 셋이 다시 표에 들어오면 예산과 횟수가 이중으로 걸려 "예산은 남았는데 조각2가
    횟수로 거절"되는 회귀가 난다 — 그래서 표에 **없는 것**을 여기서 못박는다.
    """
    assert call_service.DAILY_CALL_LIMIT.get(call_type) is None


# --------------------------------------------------------------------------- #
# ⑧ 개발자도구 플랜 흉내 — plan_override(2026-09-13 사장님): admin 만 · 엔진 선택만 · 조각마다 재적용
# --------------------------------------------------------------------------- #
def _set_role(session_factory, member_id: int, role: str) -> None:
    db = session_factory()
    db.get(Member, member_id).role = role
    db.commit(); db.close()


# --------------------------------------------------------------------------- #
# ⑨ QA C3-③(2026-09-22) — expression·freetalk 명시는 admin 전용
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("call_type", ["expression", "freetalk"])
async def test_non_admin_explicit_expression_or_freetalk_falls_back_to_auto(
    session_factory, seeded, call_type: str, caplog,
) -> None:
    """비admin 이 call_type=expression|freetalk 를 명시해도 auto(학습)로 되돌린다 —
    홈 화면에 그 버튼이 없어진 지금(D3), 명시는 admin 개발자 도구·하네스만 쓴다.

    ⚠ QA C3 재검-③(2026-09-22): 이 시드 DB 엔 cur_lesson 이 없어 auto 도 결국 expression
    으로 떨어진다 — "게이트가 auto 로 되돌렸다" 와 "게이트가 아예 없어서 명시가 그대로
    통과했다" 가 **최종 코스만으로는 구분이 안 된다**. 게이트가 실제로 탄 로그 줄로
    직접 확인한다(게이트가 없으면 이 줄 자체가 안 찍힌다)."""
    import logging
    _set_role(session_factory, seeded["member_id"], "user")
    with caplog.at_level(logging.INFO, logger=cs.logger.name):
        await _run(session_factory, seeded, call_type, {})
    assert any(
        f"call_type={call_type} 명시 — admin 아님 → auto 취급" in r.getMessage()
        for r in caplog.records
    ), f"비admin 명시 {call_type} 에 admin 게이트 로그가 안 찍혔다"
    db = session_factory()
    try:
        # 게이트가 정말 auto 로 넘겼다면 그 뒤는 auto 의 정상 폴백(이 DB 엔 cur_lesson 이
        # 없어 항상 expression) — 위 로그 단언과 합쳐야 "게이트 없이 그대로 통과"와 갈린다.
        assert db.query(Call).one().call_type == "expression"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_admin_plan_override_premium_picks_video_engine_and_free_picks_voice(session_factory, seeded, monkeypatch, caplog) -> None:
    """admin + override=premium → 영상(3.1·표정 도구) / admin + override=free → 음성(2.5). 한도·조각은 건드리지 않는다."""
    import logging
    monkeypatch.setattr(app_settings, "LIVE_FACE_SPIKE", True)
    _set_role(session_factory, seeded["member_id"], "admin")
    with caplog.at_level(logging.INFO, logger=cs.logger.name):
        h = await _run(session_factory, seeded, "normal", {}, extra={"plan_override": "premium"})
    assert h["kw"].get("model") == app_settings.LIVE_MODEL_VIDEO, "Premium 흉내 → 영상 모델(3.1)"
    assert h["kw"].get("tools"), "영상 통화 = 표정 도구 선언"
    assert "[표정]" in h["system_instruction"]
    assert any("플랜분기" in r.getMessage() and "(override=premium, admin)" in r.getMessage() for r in caplog.records)

    h2 = await _run(session_factory, seeded, "normal", {}, extra={"plan_override": "free"})
    assert h2["kw"].get("model") == app_settings.LIVE_MODEL_VOICE, "Free 흉내 → 음성 모델(2.5)"
    assert not h2["kw"].get("tools") and "[표정]" not in h2["system_instruction"]
    # 조각 수(한도 축)는 플랜 흉내를 모른다 — Free 회원 그대로 1
    db = session_factory()
    try:
        assert call_service.call_fragments_for_member(db, seeded["member_id"]) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_user_plan_override_is_ignored_and_absent_override_is_byte_identical(session_factory, seeded, monkeypatch, caplog) -> None:
    import logging
    # QA C3-③: 이 시험은 **비admin** 전제다 — seeded 기본은 이제 admin(⑨절)이라 내려야 한다.
    _set_role(session_factory, seeded["member_id"], "user")
    monkeypatch.setattr(app_settings, "LIVE_FACE_SPIKE", True)
    with caplog.at_level(logging.INFO, logger=cs.logger.name):
        base = await _run(session_factory, seeded, "normal", {})
    # override 없음 = 종전 경로(plan None → effective_plan) — 분기 로그에 override 표기 없음. 대본 바이트 동일은 각 코스 스냅샷 시험이 지킨다.
    plain = [r.getMessage() for r in caplog.records if "플랜분기: 영상=" in r.getMessage()]
    assert plain and all("override" not in m for m in plain)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=cs.logger.name):
        h = await _run(session_factory, seeded, "normal", {}, extra={"plan_override": "premium"})
    assert h["kw"].get("model") == base["kw"].get("model") == app_settings.LIVE_MODEL_VOICE, "user 는 본인 플랜(Free → 음성)"
    assert not h["kw"].get("tools") and not base["kw"].get("tools") and "[표정]" not in h["system_instruction"]
    assert any("override=premium 무시 — admin 아님" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_plan_override_is_reapplied_on_a_resumed_fragment(session_factory, seeded, monkeypatch) -> None:
    """이어하기 조각 2 도 start 에 같은 값을 다시 보내면 다시 적용된다(서버는 조각마다 판정).

    ⚠ C3(2026-09-22): call_type 은 "expression" 을 쓴다 — chat(옛 normal)은 아직
    이어하기 허용 목록에 없다(C7 이 붙인다). 여기서 보는 건 plan_override 재적용이지
    코스별 이어하기 자격이 아니므로, 지금 이어하기가 되는 코스로 시험한다."""
    monkeypatch.setattr(app_settings, "LIVE_FACE_SPIKE", True)
    monkeypatch.setattr(cs.call_service, "call_fragments_for_member", lambda db, m: 3)
    _set_role(session_factory, seeded["member_id"], "admin")
    h = await _run(session_factory, seeded, "expression", {}, extra={"plan_override": "premium"})
    db = session_factory()
    call = db.query(Call).filter(Call.member_id == seeded["member_id"]).order_by(Call.call_id.desc()).first()
    db.close()
    h2 = await _run(session_factory, seeded, "expression", {}, extra={"plan_override": "premium", "continues_call_id": str(call.call_id)})
    assert h2["kw"].get("model") == app_settings.LIVE_MODEL_VIDEO
    h3 = await _run(session_factory, seeded, "expression", {}, extra={"continues_call_id": str(call.call_id)})
    assert h3["kw"].get("model") == app_settings.LIVE_MODEL_VOICE, "값을 안 보낸 조각은 본인 플랜으로 — 서버는 기억하지 않는다"


# --------------------------------------------------------------------------- #
# QA C2-③(2026-09-22): admin 흉내가 아니라 **진짜 구독 행**으로 — 실제 세션 팩토리
# 인자(holder["kw"]["tools"])로 Free/Premium 을 검증한다. LIVE_FACE_SPIKE 기본값(False)은
# 그대로 두고(운영 env 가 true), 시험에서만 True 로 켠다.
# --------------------------------------------------------------------------- #
def _subscribe_premium(session_factory, member_id: int) -> None:
    from datetime import datetime, timedelta, timezone

    db = session_factory()
    db.add(Subscribe(
        member_id=member_id, plan="premium", start_date=datetime.now(timezone.utc),
        end_date=datetime.now(timezone.utc) + timedelta(days=30),
        is_activate=True, billing_state="ok", is_trial=False, source="manual",
    ))
    db.commit(); db.close()


@pytest.mark.asyncio
async def test_real_subscription_row_picks_tools_not_admin_override(session_factory, seeded, monkeypatch) -> None:
    """Free 회원(구독 행 없음)은 tools 없음 · 진짜 premium 구독 행이 있는 회원은 set_face 가 tools 에 실린다."""
    monkeypatch.setattr(app_settings, "LIVE_FACE_SPIKE", True)
    free = await _run(session_factory, seeded, "normal", {})
    assert not free["kw"].get("tools"), "구독 없음(Free) 인데 tools 가 실렸다"

    _subscribe_premium(session_factory, seeded["member_id"])
    premium = await _run(session_factory, seeded, "normal", {})
    assert premium["kw"].get("tools"), "진짜 premium 구독 행인데 tools 에 set_face 가 안 실렸다"
    assert "[표정]" in premium["system_instruction"]


# --------------------------------------------------------------------------- #
# ⑩ QA C3 재검-①(2026-09-22) — 런타임 코드에 call_type="normal" 이 남지 않는다
# --------------------------------------------------------------------------- #
def test_no_runtime_code_still_uses_normal_as_a_call_type_value():
    """옛 "normal" 은 죽은 값이다(마이그레이션 e0a404f9e6c0 가 기존 데이터도 전부 chat 으로
    전환했다). 딱 두 자리만 예외다:
      · call_session.py 의 구버전 앱 호환 감지 한 줄(`if call_type == "normal":` — 받으면
        즉시 chat 으로 바꾼다. 이 줄 자체가 없어지면 안 된다)
      · protocol.py 의 와이어 Literal — 위 감지가 파싱하려면 "normal" 을 계속 허용해야 한다.
    나머지는 전부 주석(역사 기록)이거나 chat 이어야 한다."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    pattern = re.compile(r"""(['"])normal\1""")
    scan_files = (
        "domains/learning/realtime/call_session.py",
        "domains/learning/realtime/cascade_session.py",
        "domains/learning/realtime/protocol.py",
        "domains/learning/routers/call.py",
        "domains/learning/service/call_service.py",
        "domains/learning/service/normalcall_service.py",
        "domains/learning/models/call.py",
        "main.py",
    )
    # (파일, 그 파일 안에서 허용하는 줄의 부분 문자열) — 코드 그 자체인 두 자리만.
    allowed = {
        "domains/learning/realtime/call_session.py": ('if call_type == "normal":',),
        "domains/learning/realtime/protocol.py": (
            'call_type: Literal["normal", "level_test", "expression", "freetalk", "auto", "chat"]',
        ),
    }
    offenders: list[str] = []
    for rel in scan_files:
        text = (root / rel).read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            if line.lstrip().startswith("#"):
                continue  # 주석(역사 기록) — 코드가 아니다
            if any(marker in line for marker in allowed.get(rel, ())):
                continue
            offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "call_type 에 'normal' 을 쓰는 런타임 코드가 남아 있다:\n" + "\n".join(offenders)
