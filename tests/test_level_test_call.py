"""P1 레벨테스트 콜 회귀 테스트 (외부 의존 0).

검증 대상:
    - 콜타입 자동 라우팅(D11): korean_level=None → level_test 대본/call_type,
      보유자 → normal 대본, start(call_type="level_test") 명시 재측정(non-prod).
    - 명시 강등(P1): 데모(target_language 오버라이드) 명시 level_test → normal,
      prod 재측정(korean_level 보유) 명시 level_test → normal.
    - analyze_level_test_call: 판정 성공 단일 커밋 저장 / 빈 전사 스킵 / LLM 실패 /
      모순 출력(unknown+sufficient) failed / member 소실 failed(부분 저장 없음).
    - _place_from_band: 4버킷 고정매핑(survival→1/beginner→2/intermediate→3/advanced→6),
      sparse 캡(min(level,2)), unknown+sufficient→None(모순), unknown|none→1.
    - _citation_coverage: 어절 토큰 부분일치 비율(0.0~1.0) — 인용검증 순수함수.
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
        db.add(Level(language="ko", level_no=1, profile="생존 회화"))
        db.add(Level(language="ko", level_no=2, profile="초급 A 학습자"))
        db.add(Level(language="ko", level_no=3, profile="초급 A 레벨3 학습자"))
        db.add(Level(language="ko", level_no=5, profile="초급 A 레벨5 학습자"))
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
    async def _factory(client, settings, *, system_instruction, voice, tools=None):
        sess = FakeLiveSession()
        session_holder["session"] = sess
        session_holder["system_instruction"] = system_instruction
        session_holder["voice"] = voice
        session_holder["tools"] = tools
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
    # 레벨테스트 대본(비버 자율 진행/OPI): 진행 방식·시험관 블록 포함, 일반 대본의 [학습자 수준] 없음.
    assert "[진행 방식 — 네가 스스로 이끈다]" in instr
    assert "표본 수집" in instr
    assert "[학습자 수준]" not in instr
    # 레벨테스트 선톡 시드가 주입됐다(무인자 — 비버가 첫 질문을 스스로 시작).
    assert holder["session"].sent_text_turns
    assert "가벼운 인사 한 마디 + 바로 질문" in holder["session"].sent_text_turns[0]

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
    assert "[진행 방식 — 네가 스스로 이끈다]" not in instr

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
    assert "[진행 방식 — 네가 스스로 이끈다]" in instr
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
    assert "[진행 방식 — 네가 스스로 이끈다]" not in instr  # 레벨테스트 대본 아님

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
    assert "[진행 방식 — 네가 스스로 이끈다]" not in instr

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

    assert "[진행 방식 — 네가 스스로 이끈다]" in holder["system_instruction"]

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
    # band=beginner + sufficient → 고정매핑 레벨 2(_BUCKET_LEVEL["beginner"]).
    result = _assessment(band="beginner",
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
        assert member.korean_level == svc._BUCKET_LEVEL["beginner"] == 2
        assert call.assessed_level == 2
        assert call.assessment_note == result.reasoning
        assert call.summary == result.summary
        assert call.status == "done"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# (5) _place_from_band 단위 — AI는 증인(밴드만), 코드가 심판(레벨 숫자 고정매핑)
# --------------------------------------------------------------------------- #
def test_place_from_band_bucket_mapping():
    """4버킷 고정매핑(sufficient 표본): survival→1 / beginner→2 / intermediate→3 / advanced→6."""
    assert svc._place_from_band(
        _assessment(band="survival", sample_quality="sufficient")) == 1
    assert svc._place_from_band(
        _assessment(band="beginner", sample_quality="sufficient")) == 2
    assert svc._place_from_band(
        _assessment(band="intermediate", sample_quality="sufficient")) == 3
    assert svc._place_from_band(
        _assessment(band="advanced", sample_quality="sufficient")) == 6
    # 매핑 상수 자체도 계약대로인지 고정.
    assert svc._BUCKET_LEVEL == {
        "survival": 1, "beginner": 2, "intermediate": 3, "advanced": 6
    }


def test_place_from_band_sparse_caps_at_two():
    """sparse 표본 → 중급+ 과배치 금지: min(level, 2)."""
    # advanced(6) 도 sparse 면 2 로 캡.
    assert svc._place_from_band(
        _assessment(band="advanced", sample_quality="sparse")) == 2
    # intermediate(3) → 2.
    assert svc._place_from_band(
        _assessment(band="intermediate", sample_quality="sparse")) == 2
    # beginner(2) 는 이미 2 이하 → 그대로 2.
    assert svc._place_from_band(
        _assessment(band="beginner", sample_quality="sparse")) == 2
    # survival(1) 은 이미 1 → 그대로 1.
    assert svc._place_from_band(
        _assessment(band="survival", sample_quality="sparse")) == 1


def test_place_from_band_unknown_with_sufficient_returns_none():
    """모순 출력: 표본 sufficient 인데 band=unknown → None(판정 신뢰 불가·미저장)."""
    assert svc._place_from_band(
        _assessment(band="unknown", confidence="low",
                    sample_quality="sufficient")) is None


def test_place_from_band_unknown_or_none_returns_one():
    """band=unknown(sufficient 아님) 또는 sample_quality=none → 생존 회화 1."""
    # unknown + sparse → 1.
    assert svc._place_from_band(
        _assessment(band="unknown", confidence="low",
                    sample_quality="sparse")) == 1
    # unknown + none → 1.
    assert svc._place_from_band(
        _assessment(band="unknown", sample_quality="none")) == 1
    # 밴드는 명시됐어도 sample_quality=none 이면 1(모국어뿐 전제).
    assert svc._place_from_band(
        _assessment(band="advanced", sample_quality="none")) == 1
    assert svc._place_from_band(
        _assessment(band="beginner", sample_quality="none")) == 1


# --------------------------------------------------------------------------- #
# (5.5) _citation_coverage 단위 — 인용검증 순수함수(어절 토큰 부분일치 비율)
# --------------------------------------------------------------------------- #
def test_citation_coverage_full_and_partial_and_absent():
    """0.0~1.0: 완전 실재=1.0 / ASR 드리프트 부분겹침=비율 / 통째 부재=0.0 / 빈문자열=0.0."""
    # 두 어절 모두 answer 에 부분문자열로 존재 → 1.0.
    assert svc._citation_coverage("김치를 좋아해요", "저는 김치를 좋아해요") == 1.0
    # ASR 드리프트: '김치를'은 있으나 '좋아해요'는 '좋아 해요'로 갈라져 부재 → 1/2.
    assert svc._citation_coverage("김치를 좋아해요", "저는 김치 좋아 해요") == 0.5
    # 어느 토큰도 answer 에 없음 → 0.0.
    assert svc._citation_coverage("완전히 다른 말", "김치를 먹어요") == 0.0
    # 빈 heard / 빈 answer → 0.0.
    assert svc._citation_coverage("", "김치를 먹어요") == 0.0
    assert svc._citation_coverage("김치를 좋아해요", "") == 0.0


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
    result = _assessment(band="unknown",
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

    assert result == (7, None, None, None, None)
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
