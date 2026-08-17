"""P2.5 teaching_plan push + hint_used 수신 + 동적 힌트 사이드카(D16) 테스트 (외부 의존 0).

검증 대상:
    - protocol: hint_used(클라)·teaching_plan/hint(서버) round-trip — discriminated union 등록.
    - _verify_detections ④'(D16): 힌트 열람 마커 이후 첫 USER 턴의 E2/E3 → E1 강등
      (grade_raw 보존), 그 다음 USER 턴은 무강등 / 마커 없으면 종전과 동일.
    - teaching_plan 매핑: _study_item_dto(item_id/roman 확장, chunk meanings JSON "roman")
      → _teaching_plan_items(TeachingItem) / run_call 이 start 직후 1회 push.
    - 힌트 사이드카: 질문("?") 턴 종료 시 태스크 생성, 새 질문이 오면 이전 미완 태스크
      취소(세션당 동시 1개), 성공 시 ServerHint 가 해당 turn_id 로 push.
    - hint_used 마커가 run_call → analyze_call(hinted_from_turn_index)로 전달.

모든 외부(Gemini Live·generate_structured·TTS·Storage)는 모킹 — tests/test_normalcall_ws.py
의 FakeWebSocket/가짜 Live 세션 패턴 재사용(⛔ R4: 펌프 구조 자체는 무변경 검증 대상 아님,
기존 회귀는 test_normalcall_ws.py 가 지킨다).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level

from core.config import settings as app_settings
from core.gemini_live import LiveEvent

import domains.learning.realtime.call_session as cs
import domains.learning.service.normalcall_service as svc
from domains.learning.realtime.call_session import HintOut, run_call
from domains.learning.realtime.protocol import (
    HintExample,
    ServerHint,
    ServerTeachingPlan,
    TeachingItem,
    client_adapter,
    server_adapter,
)


# --------------------------------------------------------------------------- #
# 공용 픽스처 (test_normalcall_ws.py 컨벤션)
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


@pytest.fixture()
def seeded(session_factory):
    """Voice/Character/Level(1)/Member(korean_level=1 — 힌트 발동 조건) 시드."""
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="친근한 선생님", personality="다정함",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.add(Level(language="ko", level_no=1, profile="생존 회화 프로파일"))
        member = Member(language="en", korean_level=1, onboarding_completed=True,
                        auth_user_id="auth-member")
        db.add(member)
        db.commit()
        return {"member_id": member.member_id, "character_id": ch.character_id}
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _mock_external(monkeypatch):
    """Storage/TTS/generate_structured 를 결정적 스텁으로 — 네트워크 0.

    generate_structured 기본 스텁은 None(분석 → failed, 힌트 → 미표시) — 이 파일의
    관심사는 분석 결과가 아니라 push/마커/태스크 수명. 힌트 스모크는 테스트 안에서
    재패치한다(monkeypatch 후패치 우선).
    """
    monkeypatch.setattr(svc.storage, "upload", lambda *a, **k: "stub-key")
    monkeypatch.setattr(svc.storage, "public_url", lambda *a, **k: "https://stub/url.mp3")

    async def _fake_tts(*_a, **_k):
        return None

    monkeypatch.setattr(svc.tts, "synthesize", _fake_tts)

    async def _fake_generate(*_a, **_k):
        return None

    # svc.gemini_analysis 와 cs.gemini_analysis 는 같은 모듈(core.gemini_analysis).
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _fake_generate)


class FakeWebSocket:
    """starlette WebSocket 흉내(test_normalcall_ws.py 동형) + hint 수신 이벤트."""

    def __init__(self, incoming: list[dict], hang: bool = False):
        self._incoming = list(incoming)
        self._hang = hang
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self.hint_sent = asyncio.Event()  # ServerHint 수신 감지(결정적 대기용)
        from starlette.websockets import WebSocketState
        self._WS = WebSocketState
        self.client_state = WebSocketState.CONNECTED

    async def receive(self) -> dict:
        if self._incoming:
            return self._incoming.pop(0)
        if self._hang:
            await asyncio.Event().wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)
        if '"hint"' in text:
            self.hint_sent.set()

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int | None = None) -> None:
        self.closed = True
        self.client_state = self._WS.DISCONNECTED


async def _wait_analysis_tasks():
    for _ in range(200):
        if not cs._analysis_tasks:
            return
        await asyncio.sleep(0.01)


def _sent_types(ws: FakeWebSocket) -> list[str]:
    return [json.loads(t).get("type") for t in ws.sent_text]


# --------------------------------------------------------------------------- #
# (1) protocol round-trip — 신규 3 메시지
# --------------------------------------------------------------------------- #
def test_protocol_hint_used_roundtrip():
    m = client_adapter.validate_python(
        json.loads('{"type":"hint_used","turn_id":"abc123","item_id":7,"stage":2}')
    )
    assert m.type == "hint_used" and m.turn_id == "abc123"
    assert m.item_id == 7 and m.stage == 2
    # 전부 선택 필드 — 최소 페이로드도 유효(동적 힌트는 turn_id 만 보냄)
    m2 = client_adapter.validate_python({"type": "hint_used"})
    assert m2.turn_id is None and m2.item_id is None and m2.stage is None


def test_protocol_teaching_plan_roundtrip():
    plan = ServerTeachingPlan(items=[
        TeachingItem(item_id=1, ko="안녕하세요?", roman="annyeonghaseyo?",
                     meaning="Hello.", example=None, kind="chunk"),
        TeachingItem(item_id=2, ko="학교", kind="vocab"),  # roman/meaning/example 선택
    ])
    back = server_adapter.validate_json(server_adapter.dump_json(plan))
    assert back.type == "teaching_plan" and len(back.items) == 2
    assert back.items[0].roman == "annyeonghaseyo?" and back.items[0].meaning == "Hello."
    assert back.items[1].roman is None and back.items[1].kind == "vocab"


def test_protocol_hint_roundtrip():
    hint = ServerHint(turn_id="t9", examples=[
        HintExample(korean="화장실에 가요", roman="hwajangsire gayo", native="I'm going to the bathroom"),
        HintExample(korean="화장실 어디예요?", roman="hwajangsil eodiyeyo?", native="Where is the bathroom?"),
        HintExample(korean="잠깐 나갔다 올게요", roman="jamkkan nagatda olgeyo", native="I'll step out for a moment"),
    ])
    back = server_adapter.validate_json(server_adapter.dump_json(hint))
    assert back.type == "hint" and back.turn_id == "t9"
    assert len(back.examples) == 3
    assert back.examples[0].korean == "화장실에 가요" and back.examples[0].roman == "hwajangsire gayo"


# --------------------------------------------------------------------------- #
# (2) _verify_detections ④' — 힌트 열람 직후 USER 턴 E2/E3 → E1 강등
# --------------------------------------------------------------------------- #
_HINT_CANDS = [
    {"item_id": 11, "kind": "vocab", "surface": "화장실", "example": None, "injected": True},
    {"item_id": 12, "kind": "vocab", "surface": "학교", "example": None, "injected": True},
]
_HINT_ROWS = [
    {"turn_index": 0, "role": "beaver", "content": "지금 어디에 가고 있어요?"},
    {"turn_index": 1, "role": "user", "content": "저는 화장실에 가요 지금요"},
    {"turn_index": 2, "role": "beaver", "content": "잘했어요. 내일 계획도 말해 볼래요?"},
    {"turn_index": 3, "role": "user", "content": "내일은 학교에 가요"},
]


def _hint_detections():
    D = svc.ItemDetection
    return [
        D(item_id=11, evidence="E3", quote="저는 화장실에 가요 지금요"),
        D(item_id=12, evidence="E3", quote="내일은 학교에 가요"),
    ]


def test_verify_detections_demotes_first_user_turn_after_hint():
    """마커(next_turn_index=1 — 비버 t0 flush 직후 열람) → USER t1 만 E3→E1(raw 보존),
    다음 USER 턴(t3)은 무강등. db 는 현 구현 미사용(전사·후보만으로 판정)."""
    verified = svc._verify_detections(
        None, 1, 1, _hint_detections(), _HINT_CANDS, _HINT_ROWS,
        hinted_from_turn_index={1},
    )
    by = {v.item_id: v for v in verified}
    assert len(verified) == 2
    assert by[11].grade_final == "E1" and by[11].grade_raw == "E3"  # 힌트 강등 + 원판정 보존
    assert by[12].grade_final == "E3"  # 마커 이후 "첫" USER 턴 1개만 강등


def test_verify_detections_no_hint_keeps_grades():
    """마커 미전달(기본 None) — 종전 판정과 동일(하위호환)."""
    verified = svc._verify_detections(
        None, 1, 1, _hint_detections(), _HINT_CANDS, _HINT_ROWS,
    )
    by = {v.item_id: v for v in verified}
    assert by[11].grade_final == "E3" and by[12].grade_final == "E3"


# --------------------------------------------------------------------------- #
# (3) teaching_plan 매핑 — chunk roman 포함 + run_call push
# --------------------------------------------------------------------------- #
def test_study_item_dto_carries_item_id_and_chunk_roman():
    item = LearningItem(kind="chunk", source_key="c:01:안녕하세요?", band=1,
                        language="ko", level_no=1,
                        assign_rule="survival_v1", surface="안녕하세요?",
                        meanings='{"en": "Hello.", "roman": "annyeonghaseyo?"}')
    item.item_id = 77  # db 없이 PK 수동 지정(매핑 단위 테스트)
    dto = svc._study_item_dto({"slot": "main", "study_kind": "chunk", "item": item}, "en")
    assert dto["item_id"] == 77 and dto["roman"] == "annyeonghaseyo?"
    assert dto["obj"] == "안녕하세요?" and dto["des"] == "Hello."

    items = cs._teaching_plan_items([dto])
    assert len(items) == 1
    ti = items[0]
    assert ti.item_id == 77 and ti.ko == "안녕하세요?" and ti.kind == "chunk"
    assert ti.roman == "annyeonghaseyo?" and ti.meaning == "Hello." and ti.example is None


def test_teaching_plan_items_skips_legacy_dto_without_item_id():
    """구형 dto(item_id 없음)는 카드로 못 만든다 — hint_used(item_id) 상관 불가."""
    items = cs._teaching_plan_items([
        {"slot": "main", "kind": "vocab", "obj": "학교", "ex": None, "des": None},
        {"slot": "main", "kind": "vocab", "obj": "", "item_id": 5},  # obj 없음도 제외
    ])
    assert items == []


@pytest.mark.asyncio
async def test_run_call_pushes_teaching_plan_once(session_factory, seeded, monkeypatch):
    """normal 통화 + study_items 있으면 start 직후 teaching_plan 1회 push(핫패스 밖)."""
    real_setup = svc.load_call_setup

    def _setup_with_items(db, member_id, character_id, language="ko"):
        out = real_setup(db, member_id, character_id, language)
        out["study_items"] = [
            {"slot": "main", "kind": "chunk", "obj": "안녕하세요?", "ex": None,
             "des": "Hello.", "item_id": 77, "roman": "annyeonghaseyo?"},
        ]
        return out

    monkeypatch.setattr(svc, "load_call_setup", _setup_with_items)

    import contextlib as _cl

    class OneTurn:
        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []

        async def send_audio(self, b): self.sent_audio.append(b)
        async def send_text_turn(self, t): self.sent_text_turns.append(t)

        async def events(self):
            yield LiveEvent(kind="out_tr", text="시작해요.")
            yield LiveEvent(kind="turn_end")

    @_cl.asynccontextmanager
    async def factory(client, settings, *, system_instruction, voice):
        yield OneTurn()

    ws = FakeWebSocket([
        {"type": "websocket.receive",
         "text": json.dumps({"type": "start", "character_id": seeded["character_id"]})},
    ])
    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"], live_session_factory=factory)
    await _wait_analysis_tasks()

    plans = [json.loads(t) for t in ws.sent_text if json.loads(t).get("type") == "teaching_plan"]
    assert len(plans) == 1, f"teaching_plan 1회 push 기대: {_sent_types(ws)}"
    item = plans[0]["items"][0]
    assert item == {"item_id": 77, "ko": "안녕하세요?", "roman": "annyeonghaseyo?",
                    "meaning": "Hello.", "example": None, "kind": "chunk"}
    # 통화 본편 이전에 나간다 — 카드 화면이 통화 시작과 함께 뜬다.
    # (인덱스 0 으로 못 박지 않는다: 앞에 call_started 같은 세션 메타가 붙을 수 있고,
    #  중요한 건 "몇 번째냐"가 아니라 **첫 턴보다 먼저냐**다.)
    types = _sent_types(ws)
    assert "turn_start" in types, f"턴이 시작되지 않음: {types}"
    assert types.index("teaching_plan") < types.index("turn_start"), (
        f"teaching_plan 이 통화 본편보다 늦게 나감: {types}"
    )


# --------------------------------------------------------------------------- #
# (4) 힌트 사이드카 스모크 — 태스크 생성·취소·push + hint_used 마커 전달
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hint_task_created_cancelled_and_pushed(session_factory, seeded, monkeypatch):
    """질문 턴마다 힌트 태스크 생성 — 새 질문이 오면 이전 미완 태스크 취소(동시 1개),
    성공한 힌트는 ServerHint(해당 turn_id)로 push. hint_used 마커는 analyze_call 로 전달."""
    q1_started = asyncio.Event()
    q1_cancelled = asyncio.Event()

    async def _hint_generate(client, model, *, system_instruction, prompt, schema,
                             temperature=0.2, thinking_budget=None, usage=None):
        if schema is not HintOut:  # 통화후 분석 콜은 기본 스텁과 동일(None)
            return None
        assert thinking_budget == 0 and "한국어 학습 힌트" in system_instruction
        # ⭐ 힌트 사이드카도 **원가 계기판을 들고 나간다**(2026-08-17). 이 인자가 빠지면
        #   통화중 LLM 몫이 다시 통째로 안 세어진다 — 여기서 붙잡는다.
        assert usage is not None, "힌트 사이드카가 usage 수집기 없이 나갔다"
        if "이름" in prompt:  # 질문1 힌트 — 영원히 미완(취소 검증 대상)
            q1_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                q1_cancelled.set()
                raise
        return HintOut(examples=[
            HintExample(korean="화장실에 가요", roman="hwajangsire gayo",
                        native="I'm going to the bathroom"),
            HintExample(korean="화장실 어디예요?", roman="hwajangsil eodiyeyo?",
                        native="Where is the bathroom?"),
            HintExample(korean="잠깐만요", roman="jamkkanmanyo", native="One moment"),
        ])

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _hint_generate)

    analyze_kwargs: dict = {}
    real_analyze = svc.analyze_call

    async def _spy_analyze(*a, **k):
        analyze_kwargs.update(k)
        return await real_analyze(*a, **k)

    monkeypatch.setattr(svc, "analyze_call", _spy_analyze)

    ws = FakeWebSocket([
        {"type": "websocket.receive",
         "text": json.dumps({"type": "start", "character_id": seeded["character_id"]})},
        # 힌트 열람 신호(마커 기록) — 처리 시점의 next_turn_index=0 이 기록된다.
        {"type": "websocket.receive",
         "text": json.dumps({"type": "hint_used", "turn_id": "demo-turn", "item_id": 3})},
    ], hang=True)

    import contextlib as _cl

    class TwoQuestions:
        """질문 2턴 — 두 번째 힌트가 push 될 때까지 기다렸다 종료(결정적)."""

        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []

        async def send_audio(self, b): self.sent_audio.append(b)
        async def send_text_turn(self, t): self.sent_text_turns.append(t)

        async def events(self):
            yield LiveEvent(kind="out_tr", text="이름이 뭐예요?")
            yield LiveEvent(kind="turn_end")
            await q1_started.wait()  # 질문1 힌트 태스크가 실행에 들어간 뒤에 질문2
            yield LiveEvent(kind="out_tr", text="지금 어디에 가요?")
            yield LiveEvent(kind="turn_end")
            await asyncio.wait_for(ws.hint_sent.wait(), timeout=5.0)
            # 제너레이터 종료 → _CallFinished(정상 종료 경로)

    @_cl.asynccontextmanager
    async def factory(client, settings, *, system_instruction, voice):
        yield TwoQuestions()

    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"], live_session_factory=factory)
    await _wait_analysis_tasks()

    # 질문1 힌트 태스크: 실행 시작 후 질문2 turn_end 에서 취소됐다(세션당 동시 1개).
    for _ in range(100):
        if q1_cancelled.is_set():
            break
        await asyncio.sleep(0.01)
    assert q1_cancelled.is_set(), "이전 미완 힌트 태스크가 취소되지 않음"

    # 질문2 힌트가 해당 턴 id 로 push 됐다.
    msgs = [json.loads(t) for t in ws.sent_text]
    hints = [m for m in msgs if m.get("type") == "hint"]
    assert len(hints) == 1 and len(hints[0]["examples"]) == 3
    assert hints[0]["examples"][0]["korean"] == "화장실에 가요"
    turn_ends = [m["turn_id"] for m in msgs if m.get("type") == "turn_end"]
    assert len(turn_ends) >= 2 and hints[0]["turn_id"] == turn_ends[1]

    # hint_used 마커(수신 시점 next_turn_index=0)가 분석에 전달됐다(D16 강등 재료).
    assert analyze_kwargs.get("hinted_from_turn_index") == {0}


@pytest.mark.asyncio
async def test_no_hint_task_for_non_question_turn(session_factory, seeded, monkeypatch):
    """평서 턴('?' 없음)은 힌트 미생성(질문 턴만 힌트 생성).

    ※ 힌트는 이제 전 통화(레벨테스트·일반, 레벨 무관)에서 활성 — 사장님 결정으로 레벨
    게이팅 폐지. 따라서 '레벨2는 힌트 비활성' 검증은 제거(더 이상 그런 규칙 없음)."""
    calls: list[str] = []

    async def _spy_generate(client, model, *, system_instruction, prompt, schema,
                            temperature=0.2, thinking_budget=None):
        if schema is HintOut:
            calls.append(prompt)
        return None

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _spy_generate)

    import contextlib as _cl

    class Statement:
        def __init__(self):
            self.sent_audio: list[bytes] = []
            self.sent_text_turns: list[str] = []

        async def send_audio(self, b): self.sent_audio.append(b)
        async def send_text_turn(self, t): self.sent_text_turns.append(t)

        async def events(self):
            yield LiveEvent(kind="out_tr", text="오늘은 인사를 배워요.")  # 평서 — 힌트 없음
            yield LiveEvent(kind="turn_end")

    @_cl.asynccontextmanager
    async def factory(client, settings, *, system_instruction, voice):
        yield Statement()

    # (a) 레벨1: 사이드카 활성이지만 평서 턴 → 태스크 미생성
    # hang=True: 클라 침묵 — 종료를 이벤트 소진(_CallFinished)이 주도(조기 disconnect 레이스 방지)
    ws = FakeWebSocket([
        {"type": "websocket.receive",
         "text": json.dumps({"type": "start", "character_id": seeded["character_id"]})},
    ], hang=True)
    await run_call(ws, app_settings, object(), session_factory,
                   member_id=seeded["member_id"], live_session_factory=factory)
    await _wait_analysis_tasks()
    assert calls == [], "평서 턴에 힌트 태스크가 생성됨"
    assert not any(m.get("type") == "hint" for m in map(json.loads, ws.sent_text))
