"""P1 레벨테스트 콜 회귀 테스트 (외부 의존 0).

검증 대상:
    - 콜타입 자동 라우팅(D11): korean_level=None → level_test 대본/call_type,
      보유자 → normal 대본, start(call_type="level_test") 명시 재측정(non-prod).
    - 명시 강등(P1): 데모(target_language 오버라이드) 명시 level_test → normal,
      prod 재측정(korean_level 보유) 명시 level_test → normal.
    - analyze_level_test_call: 판정 성공 단일 커밋 저장 / 빈 전사 스킵 / LLM 실패 /
      모순 출력(unknown+sufficient) failed / member 소실 failed(부분 저장 없음).
    - _clamp_assessed_level: band-level_no 불일치 재계산, unknown→1, sparse+low 하향,
      unknown+sufficient→None, sufficient+band 명시+level_no=1→재계산.
    - _user_char_total: 유니코드 letter/digit 만 계수(기호·이모지 제외).
    - get_status_detail 소유자 가드 + status 엔드포인트 응답 계약(call_type/assessed_level).
    - MemberRead.korean_level 직렬화(None/값).

헬퍼(FakeWebSocket/FakeLiveSession/세션팩토리)는 tests/test_normalcall_ws.py 의
패턴을 그대로 모방한다(해당 파일 수정 금지). 모든 외부(Gemini Live·Gemini 분석·
TTS·Storage·DB)는 인메모리/모킹.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# --- DB 시드용 모델 + 레지스트리(전 모델 등록 보장) ---
from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.account.models.member_reason import MemberReason
from domains.account.schemas.member import MemberRead
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.level import Level

from core.config import settings as app_settings
from core.supabase_auth import AuthUser

import core.deps as deps
import domains.learning.service.normalcall_service as svc
import domains.learning.realtime.call_session as cs
import domains.learning.realtime.ws_router as ws_router
from domains.learning.realtime.call_session import run_call
from core.gemini_live import LiveEvent


# 인증: Supabase 토큰 검증을 모킹 — "auth-*" 토큰만 유효(test_normalcall_ws 패턴).
def _fake_verify(token):
    if token and token.startswith("auth-"):
        return AuthUser(uid=token, email=f"{token}@test.io")
    return None


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(ws_router, "verify_token", _fake_verify)
    monkeypatch.setattr(deps, "verify_token", _fake_verify)


# --------------------------------------------------------------------------- #
# 인메모리 DB (BigInteger+Identity PK 는 sqlite 에서 autoincrement 안 되므로 Integer 로 치환)
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
    """Voice/Character/Level(1·2·3·5) + 회원 3명(레벨 None/3/5) 시드. ids 반환."""
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="친근한 선생님", personality="다정함",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.add(Level(level_no=1, profile="생존 회화"))
        db.add(Level(level_no=2, profile="초급 A 학습자"))
        db.add(Level(level_no=3, profile="초급 A 레벨3 학습자"))
        db.add(Level(level_no=5, profile="초급 A 레벨5 학습자"))
        db.flush()

        m_none = Member(language="en", korean_level=None, onboarding_completed=True,
                        auth_user_id="auth-none")
        m_l3 = Member(language="en", korean_level=3, onboarding_completed=True,
                      auth_user_id="auth-l3")
        m_l5 = Member(language="en", korean_level=5, onboarding_completed=True,
                      auth_user_id="auth-l5")
        db.add_all([m_none, m_l3, m_l5])
        db.flush()
        db.add(MemberReason(member_id=m_none.member_id, reason="travel"))
        db.commit()
        return {
            "member_none": m_none.member_id,
            "member_l3": m_l3.member_id,
            "member_l5": m_l5.member_id,
            "character_id": ch.character_id,
            "voice": voice.name,
        }
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _mock_external(monkeypatch):
    """Storage/TTS/Gemini 분석을 결정적 스텁으로 — 네트워크 0."""
    monkeypatch.setattr(svc.storage, "upload", lambda *a, **k: "stub-key")
    monkeypatch.setattr(svc.storage, "public_url", lambda *a, **k: "https://stub/url.mp3")

    async def _fake_tts(*_a, **_k):
        return None

    monkeypatch.setattr(svc.tts, "synthesize_korean", _fake_tts)

    # 기본 분석 스텁(normal 콜 경로용). 레벨테스트 판정 테스트는 각자 재모킹한다.
    async def _fake_generate(*_a, **_k):
        return svc.CallAnalysis(
            summary="짧은 통화 요약",
            detected_mode="chat",
            expressions=[],
        )

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _fake_generate)


# --------------------------------------------------------------------------- #
# 가짜 WebSocket / 가짜 Live 세션 (test_normalcall_ws 패턴 모방)
# --------------------------------------------------------------------------- #
class FakeWebSocket:
    """starlette WebSocket 인터페이스 일부를 흉내내는 가짜."""

    def __init__(self, incoming: list[dict], hang: bool = False):
        self._incoming = list(incoming)
        self._hang = hang
        self.sent_text: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed = False
        self.close_code = None
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

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def close(self, code: int | None = None) -> None:
        self.closed = True
        self.close_code = code
        self.client_state = self._WS.DISCONNECTED


class FakeLiveSession:
    """LiveSessionProtocol 구현 — 스크립트된 한 턴 후 events 소진(자연 종료)."""

    def __init__(self):
        self.sent_audio: list[bytes] = []
        self.sent_text_turns: list[str] = []

    async def send_audio(self, pcm16_16k: bytes) -> None:
        self.sent_audio.append(pcm16_16k)

    async def send_text_turn(self, text: str) -> None:
        self.sent_text_turns.append(text)

    async def events(self):
        yield LiveEvent(kind="out_tr", text="안녕, 편하게 얘기해요")
        yield LiveEvent(kind="audio", audio=b"\x00\x00" * 8)
        yield LiveEvent(kind="turn_end")
        # 제너레이터 종료 → _pump_gemini_to_client 가 _CallFinished 발생


def make_live_factory(session_holder):
    """run_call 에 주입할 live_session_factory(async CM 팩토리)."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _factory(client, settings, *, system_instruction, voice):
        sess = FakeLiveSession()
        session_holder["session"] = sess
        session_holder["system_instruction"] = system_instruction
        session_holder["voice"] = voice
        yield sess

    return _factory


async def _wait_analysis_tasks():
    """run_call 이 띄운 백그라운드 분석 task 가 끝날 때까지 대기."""
    for _ in range(200):
        if not cs._analysis_tasks:
            return
        await asyncio.sleep(0.01)


def _start_ws(character_id: int, call_type: str | None = None,
              target_language: str | None = None) -> FakeWebSocket:
    """start 메시지 1건으로 통화를 시작하는 가짜 WS(옵션 call_type/target_language 명시)."""
    start: dict = {"type": "start", "character_id": character_id}
    if call_type is not None:
        start["call_type"] = call_type
    if target_language is not None:
        start["target_language"] = target_language
    return FakeWebSocket([{"type": "websocket.receive", "text": json.dumps(start)}])


async def _run_one_call(session_factory, member_id: int, character_id: int,
                        call_type: str | None = None,
                        target_language: str | None = None) -> dict:
    """run_call 1회 실행 + 분석 task 대기 → holder(system_instruction 등) 반환."""
    holder: dict = {}
    ws = _start_ws(character_id, call_type, target_language)
    await run_call(
        ws, app_settings, object(), session_factory,
        member_id=member_id,
        live_session_factory=make_live_factory(holder),
    )
    await _wait_analysis_tasks()
    holder["ws"] = ws
    return holder


# 판정 결과 헬퍼 — 필요한 필드만 덮어쓴다.
def _assessment(**kw) -> svc.LevelAssessment:
    base = dict(
        evidence=["안녕하세요 저는 학생이에요"],
        reasoning="초급 문형(현재형·조사)을 안정적으로 사용",
        band="beginner",
        level_in_band=2,
        level_no=3,
        confidence="high",
        sample_quality="sufficient",
        summary="자기소개와 취미",
        feedback_for_learner="아주 잘했어요!",
    )
    base.update(kw)
    return svc.LevelAssessment(**base)


def _seed_level_test_call(session_factory, member_id: int, character_id: int,
                          user_lines: list[str]) -> int:
    """level_test 통화 + USER 전사 행을 시드하고 call_id 반환."""
    db = session_factory()
    try:
        call = Call(member_id=member_id, character_id=character_id,
                    status="analyzing", call_type="level_test")
        db.add(call)
        db.flush()
        idx = 0
        db.add(CallRawData(call_id=call.call_id, role="beaver", turn_index=idx,
                           content="이름이 뭐예요?"))
        for line in user_lines:
            idx += 1
            db.add(CallRawData(call_id=call.call_id, role="user", turn_index=idx,
                               content=line))
        db.commit()
        return call.call_id
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# (1)~(3) 콜타입 라우팅 — run_call 대본/call_type
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_auto_routes_to_level_test_when_level_none(session_factory, seeded):
    """korean_level=None + call_type 미지정 → 레벨테스트 대본, call_type=level_test."""
    holder = await _run_one_call(
        session_factory, seeded["member_none"], seeded["character_id"]
    )

    instr = holder["system_instruction"]
    # 레벨테스트 대본: 프로빙 사다리 포함, 일반 대본의 [학습자 수준] 블록 없음.
    assert "[단계 상승 프로빙 — 질문 사다리]" in instr
    assert "1계단" in instr
    assert "[학습자 수준]" not in instr
    # 레벨테스트 선톡 시드가 주입됐다.
    assert holder["session"].sent_text_turns
    assert "실력" in holder["session"].sent_text_turns[0]

    db = session_factory()
    try:
        calls = db.query(Call).all()
        assert len(calls) == 1
        assert calls[0].call_type == "level_test"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_member_with_level_routes_to_normal(session_factory, seeded):
    """korean_level=3 보유자 → 기존 일반 대본([학습자 수준] 포함), call_type=normal."""
    holder = await _run_one_call(
        session_factory, seeded["member_l3"], seeded["character_id"]
    )

    instr = holder["system_instruction"]
    assert "[학습자 수준]" in instr
    assert "초급 A 레벨3 학습자" in instr  # level_no=3 프로파일이 주입됨
    assert "[단계 상승 프로빙 — 질문 사다리]" not in instr

    db = session_factory()
    try:
        calls = db.query(Call).all()
        assert len(calls) == 1
        assert calls[0].call_type == "normal"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_explicit_call_type_forces_level_test(session_factory, seeded, monkeypatch):
    """non-prod: korean_level=5 보유자라도 start(call_type='level_test') 명시 → 재측정 진입."""
    monkeypatch.setattr(app_settings, "ENV", "dev")
    holder = await _run_one_call(
        session_factory, seeded["member_l5"], seeded["character_id"],
        call_type="level_test",
    )

    instr = holder["system_instruction"]
    assert "[단계 상승 프로빙 — 질문 사다리]" in instr
    assert "[학습자 수준]" not in instr

    db = session_factory()
    try:
        calls = db.query(Call).all()
        assert len(calls) == 1
        assert calls[0].call_type == "level_test"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_demo_explicit_level_test_demoted_to_normal(
    session_factory, seeded, monkeypatch, caplog
):
    """F1: 데모(target_language 오버라이드) + 명시 call_type=level_test → normal 강등.

    비한국어 전사를 한국어 루브릭으로 판정하면 korean_level 이 오염되므로
    데모 통화에서는 명시 level_test 도 normal 로 강등 + warning."""
    monkeypatch.setattr(app_settings, "ENV", "dev")
    with caplog.at_level(logging.WARNING, logger="domains.learning.realtime.call_session"):
        holder = await _run_one_call(
            session_factory, seeded["member_none"], seeded["character_id"],
            call_type="level_test", target_language="스페인어",
        )

    instr = holder["system_instruction"]
    assert "[단계 상승 프로빙 — 질문 사다리]" not in instr  # 레벨테스트 대본 아님

    db = session_factory()
    try:
        calls = db.query(Call).all()
        assert len(calls) == 1
        assert calls[0].call_type == "normal"
    finally:
        db.close()
    assert any("강등" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_prod_remeasure_explicit_level_test_demoted_to_normal(
    session_factory, seeded, monkeypatch, caplog
):
    """F2: prod && korean_level 보유자 + 명시 call_type=level_test → normal 강등
    (재측정은 미지원 — 후속 기능) + warning."""
    monkeypatch.setattr(app_settings, "ENV", "prod")
    with caplog.at_level(logging.WARNING, logger="domains.learning.realtime.call_session"):
        holder = await _run_one_call(
            session_factory, seeded["member_l5"], seeded["character_id"],
            call_type="level_test",
        )

    instr = holder["system_instruction"]
    assert "[학습자 수준]" in instr  # 일반 대본(레벨 5 프로파일 주입)
    assert "[단계 상승 프로빙 — 질문 사다리]" not in instr

    db = session_factory()
    try:
        calls = db.query(Call).all()
        assert len(calls) == 1
        assert calls[0].call_type == "normal"
    finally:
        db.close()
    assert any("재측정" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_prod_explicit_level_test_allowed_when_level_unset(
    session_factory, seeded, monkeypatch
):
    """F2 경계: prod 라도 korean_level 미확정이면 명시 level_test 는 강등되지 않는다."""
    monkeypatch.setattr(app_settings, "ENV", "prod")
    holder = await _run_one_call(
        session_factory, seeded["member_none"], seeded["character_id"],
        call_type="level_test",
    )

    assert "[단계 상승 프로빙 — 질문 사다리]" in holder["system_instruction"]

    db = session_factory()
    try:
        calls = db.query(Call).all()
        assert len(calls) == 1
        assert calls[0].call_type == "level_test"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# (4) 판정 성공 — 단일 커밋 저장
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_level_assessment_success_saves_level_and_meta(
    session_factory, seeded, monkeypatch
):
    call_id = _seed_level_test_call(
        session_factory, seeded["member_none"], seeded["character_id"],
        user_lines=["안녕하세요 저는 미국에서 온 학생이에요",
                    "한국어 공부가 정말 재미있어요"],
    )
    result = _assessment(band="beginner", level_in_band=2, level_no=3,
                         confidence="high", sample_quality="sufficient")
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", mock)

    await svc.analyze_level_test_call(
        call_id, object(), app_settings, session_factory,
        member_id=seeded["member_none"], locale="en",
    )

    mock.assert_called_once()
    db = session_factory()
    try:
        member = db.get(Member, seeded["member_none"])
        call = db.get(Call, call_id)
        # 레벨 배정 + 판정 메타 + done 이 함께 반영됨(단일 커밋 결과).
        assert member.korean_level == 3
        assert call.assessed_level == 3
        assert call.assessment_note == result.reasoning
        assert call.summary == result.summary
        assert call.status == "done"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# (5) _clamp_assessed_level 단위 — AI는 증인, 코드가 심판
# --------------------------------------------------------------------------- #
def test_clamp_recomputes_when_band_level_mismatch():
    """band=intermediate 인데 level_no=2(범위 밖) → 밴드 기준 재계산(6~9)."""
    r = _assessment(band="intermediate", level_in_band=2, level_no=2)
    level = svc._clamp_assessed_level(r)
    assert 6 <= level <= 9
    assert level == 7  # lo(6) + level_in_band(2) - 1

    # level_in_band 도 불능이면 밴드 중앙값−1(=7)로.
    r2 = _assessment(band="intermediate", level_in_band=None, level_no=99)
    assert svc._clamp_assessed_level(r2) == 7


def test_clamp_unknown_band_with_speech_returns_one():
    """band=unknown(발화는 20자+ 존재 전제) → 생존 회화 1."""
    r = _assessment(band="unknown", level_in_band=None, level_no=None,
                    confidence="low", sample_quality="sparse")
    assert svc._clamp_assessed_level(r) == 1
    # sample_quality=none 도 동일하게 1.
    r2 = _assessment(band="beginner", level_in_band=2, level_no=3,
                     sample_quality="none")
    assert svc._clamp_assessed_level(r2) == 1


def test_clamp_sparse_low_confidence_downgrades_one_level():
    """표본 빈약(sparse) + 저신뢰(low) → 1단계 하향(하한 1)."""
    r = _assessment(band="beginner", level_in_band=2, level_no=3,
                    confidence="low", sample_quality="sparse")
    assert svc._clamp_assessed_level(r) == 2
    # 밴드 최하단(2)에서 하향해도 하한 1 아래로는 안 내려간다.
    r2 = _assessment(band="beginner", level_in_band=1, level_no=2,
                     confidence="low", sample_quality="sparse")
    assert svc._clamp_assessed_level(r2) == 1


def test_clamp_unknown_with_sufficient_sample_returns_none():
    """F3(a): 표본이 sufficient 인데 band=unknown = 모순 출력 → None(판정 신뢰 불가)."""
    r = _assessment(band="unknown", level_in_band=None, level_no=None,
                    confidence="low", sample_quality="sufficient")
    assert svc._clamp_assessed_level(r) is None


def test_clamp_level_one_with_sufficient_band_recomputes():
    """F3(b): sufficient + band 명시인데 level_no=1 → 생존 판정 대신 밴드 재계산."""
    r = _assessment(band="beginner", level_in_band=2, level_no=1,
                    confidence="high", sample_quality="sufficient")
    assert svc._clamp_assessed_level(r) == 3  # lo(2) + level_in_band(2) - 1
    # level_in_band 도 불능이면 밴드 중앙값−1(beginner → 3).
    r2 = _assessment(band="beginner", level_in_band=None, level_no=1,
                     sample_quality="sufficient")
    assert svc._clamp_assessed_level(r2) == 3
    # 표본이 sufficient 가 아니면(sparse) 명시적 생존 판정 1 을 존중한다.
    r3 = _assessment(band="beginner", level_in_band=1, level_no=1,
                     confidence="medium", sample_quality="sparse")
    assert svc._clamp_assessed_level(r3) == 1


def test_user_char_total_counts_letters_and_digits_only():
    """F4: 유니코드 letter/digit 만 계수 — 문장부호·기호·이모지는 제외."""
    dialog = "[USER] 안녕!! 123 🙂...\n[BEAVER] 네, 반가워요"
    assert svc._user_char_total(dialog) == 5  # 안녕(2) + 123(3)
    assert svc._user_char_total("[USER] !!!???...###") == 0


# --------------------------------------------------------------------------- #
# (6) 빈 전사 스킵 / (7) LLM 실패
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_skips_judge_when_user_speech_under_threshold(
    session_factory, seeded, monkeypatch
):
    """USER 발화 <20자(공백 제외) → LLM 미호출·미저장·done(다음 통화 재테스트)."""
    call_id = _seed_level_test_call(
        session_factory, seeded["member_none"], seeded["character_id"],
        user_lines=["안녕"],  # 2자 < 20자
    )
    mock = AsyncMock(return_value=_assessment())
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", mock)

    await svc.analyze_level_test_call(
        call_id, object(), app_settings, session_factory,
        member_id=seeded["member_none"], locale="en",
    )

    mock.assert_not_called()
    db = session_factory()
    try:
        assert db.get(Member, seeded["member_none"]).korean_level is None
        call = db.get(Call, call_id)
        assert call.status == "done"
        assert call.assessed_level is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_llm_failure_marks_failed_and_keeps_level_unset(
    session_factory, seeded, monkeypatch
):
    """generate_structured=None(LLM 실패) → status=failed, korean_level 미저장."""
    call_id = _seed_level_test_call(
        session_factory, seeded["member_none"], seeded["character_id"],
        user_lines=["안녕하세요 저는 미국에서 온 학생이에요",
                    "한국어 공부가 정말 재미있어요"],
    )
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", mock)

    await svc.analyze_level_test_call(
        call_id, object(), app_settings, session_factory,
        member_id=seeded["member_none"], locale="en",
    )

    mock.assert_called_once()
    db = session_factory()
    try:
        assert db.get(Member, seeded["member_none"]).korean_level is None
        call = db.get(Call, call_id)
        assert call.status == "failed"
        assert call.assessed_level is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_skips_judge_when_user_speech_is_symbols_only(
    session_factory, seeded, monkeypatch
):
    """F4: 기호·이모지만 20자+ → letter/digit 0자로 계수 → LLM 미호출·미저장·done."""
    call_id = _seed_level_test_call(
        session_factory, seeded["member_none"], seeded["character_id"],
        user_lines=["!!!???...,,,;;;###$$$%%%🙂🙂🙂"],  # 공백 제외 20자+ 이지만 전부 기호
    )
    mock = AsyncMock(return_value=_assessment())
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", mock)

    await svc.analyze_level_test_call(
        call_id, object(), app_settings, session_factory,
        member_id=seeded["member_none"], locale="en",
    )

    mock.assert_not_called()
    db = session_factory()
    try:
        assert db.get(Member, seeded["member_none"]).korean_level is None
        call = db.get(Call, call_id)
        assert call.status == "done"
        assert call.assessed_level is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_contradictory_llm_output_marks_failed(
    session_factory, seeded, monkeypatch
):
    """F3: band=unknown + sample_quality=sufficient(모순) → 클램프 None → failed·미저장."""
    call_id = _seed_level_test_call(
        session_factory, seeded["member_none"], seeded["character_id"],
        user_lines=["안녕하세요 저는 미국에서 온 학생이에요",
                    "한국어 공부가 정말 재미있어요"],
    )
    result = _assessment(band="unknown", level_in_band=None, level_no=None,
                         confidence="low", sample_quality="sufficient")
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", mock)

    await svc.analyze_level_test_call(
        call_id, object(), app_settings, session_factory,
        member_id=seeded["member_none"], locale="en",
    )

    mock.assert_called_once()
    db = session_factory()
    try:
        assert db.get(Member, seeded["member_none"]).korean_level is None
        call = db.get(Call, call_id)
        assert call.status == "failed"
        assert call.assessed_level is None
        assert call.summary is None  # 부분 저장 없음
    finally:
        db.close()


@pytest.mark.asyncio
async def test_missing_member_marks_failed_without_partial_save(
    session_factory, seeded, monkeypatch
):
    """F5: member 소실(탈퇴 등) → 아무것도 저장하지 않고 failed(부분 저장 창 제거)."""
    ghost_member_id = 999_999
    call_id = _seed_level_test_call(
        session_factory, ghost_member_id, seeded["character_id"],
        user_lines=["안녕하세요 저는 미국에서 온 학생이에요",
                    "한국어 공부가 정말 재미있어요"],
    )
    mock = AsyncMock(return_value=_assessment())
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", mock)

    await svc.analyze_level_test_call(
        call_id, object(), app_settings, session_factory,
        member_id=ghost_member_id, locale="en",
    )

    mock.assert_called_once()
    db = session_factory()
    try:
        call = db.get(Call, call_id)
        assert call.status == "failed"
        assert call.assessed_level is None
        assert call.summary is None  # member 없음 → call 쪽도 미저장(원자성)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# (7.5) start 메시지 검증 실패 warning — F6
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_invalid_start_candidate_logs_warning_then_parses_valid(caplog):
    """F6: 깨진 start 후보는 warning 1회 로그 후 폐기, 이후 정상 start 는 그대로 파싱."""
    ws = FakeWebSocket([
        {"type": "websocket.receive", "text": "{broken json"},
        {"type": "websocket.receive", "text": "{\"type\": \"unknown_kind\"}"},
        {"type": "websocket.receive",
         "text": json.dumps({"type": "start", "character_id": 7})},
    ])
    with caplog.at_level(logging.WARNING, logger="domains.learning.realtime.call_session"):
        result = await cs._read_initial_start(ws)

    assert result == (7, None, None, None)
    warnings = [r for r in caplog.records if "검증 실패" in r.getMessage()]
    assert len(warnings) == 1  # 통화당 1회만(스팸 방지)


# --------------------------------------------------------------------------- #
# (8) status 응답 계약 — get_status_detail + 엔드포인트
# --------------------------------------------------------------------------- #
def test_get_status_detail_owner_and_other(session_factory, seeded):
    db = session_factory()
    try:
        call = Call(member_id=seeded["member_none"],
                    character_id=seeded["character_id"],
                    status="done", call_type="level_test", assessed_level=3)
        db.add(call)
        db.commit()
        call_id = call.call_id

        detail = svc.get_status_detail(db, call_id, seeded["member_none"])
        assert detail == {"status": "done", "call_type": "level_test",
                          "assessed_level": 3}
        # 타인 통화 / 없는 통화 → None
        assert svc.get_status_detail(db, call_id, seeded["member_l3"]) is None
        assert svc.get_status_detail(db, 999999, seeded["member_none"]) is None
    finally:
        db.close()


def _build_app(session_factory):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = object()
    return app


def test_status_endpoint_returns_call_type_and_assessed_level(session_factory, seeded):
    """GET /calls/{id}/status — 소유자: call_type/assessed_level 포함,
    타인 통화: 'unknown' + None(기존 하위호환)."""
    from fastapi.testclient import TestClient

    db = session_factory()
    try:
        call = Call(member_id=seeded["member_none"],
                    character_id=seeded["character_id"],
                    status="done", call_type="level_test", assessed_level=3)
        db.add(call)
        db.commit()
        call_id = call.call_id
    finally:
        db.close()

    app = _build_app(session_factory)
    client = TestClient(app)

    r1 = client.get(f"/api/v1/calls/{call_id}/status",
                    headers={"Authorization": "Bearer auth-none"})
    assert r1.status_code == 200
    body = r1.json()
    assert body["status"] == "done"
    assert body["call_type"] == "level_test"
    assert body["assessed_level"] == 3

    r2 = client.get(f"/api/v1/calls/{call_id}/status",
                    headers={"Authorization": "Bearer auth-l3"})
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["status"] == "unknown"
    assert body2["call_type"] is None
    assert body2["assessed_level"] is None


# --------------------------------------------------------------------------- #
# (9) MemberRead.korean_level 직렬화
# --------------------------------------------------------------------------- #
def test_member_read_serializes_korean_level(session_factory, seeded):
    db = session_factory()
    try:
        m_none = db.get(Member, seeded["member_none"])
        m_l3 = db.get(Member, seeded["member_l3"])

        dto_none = MemberRead.model_validate(m_none)
        dto_l3 = MemberRead.model_validate(m_l3)

        assert dto_none.korean_level is None
        assert dto_l3.korean_level == 3
        # 직렬화 출력에도 필드가 항상 포함된다(None 포함).
        assert "korean_level" in dto_none.model_dump()
        assert dto_none.model_dump()["korean_level"] is None
        assert dto_l3.model_dump()["korean_level"] == 3
    finally:
        db.close()
