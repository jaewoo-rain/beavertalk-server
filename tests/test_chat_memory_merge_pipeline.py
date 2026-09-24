"""Q3(2026-09-24, 프론트 실기기 QA) — 자유대화 「기억」 요약이 구조적으로 항상 실패하던
버그의 회귀 시험(normalcall_service.extract_and_merge_chat_memory 통합 경로).

원인: `run_db`(threadpool 워커) 안에서 `asyncio.run()` 으로 새 이벤트 루프를 만들어
`chat_memory_service.merge`(재압축 LLM 호출 포함)를 통째로 돌렸다. `client`(lifespan
공유 genai 클라이언트)는 메인 루프에 바인딩돼 있어 새 루프에서 쓰면
`RuntimeError: ... bound to a different event loop` 가 났고, `gemini_analysis` 의
포괄 except 가 그걸 삼켜 재압축이 **항상** 실패 폴백으로 떨어지고 있었다.

수정: 재압축(LLM 호출)은 메인 루프(extract_and_merge_chat_memory 자신, fire-and-forget
task)에서 먼저 끝내고, DB 읽기/쓰기만 threadpool(run_db)의 순수 동기 함수
(load_old_summary_for_merge·merge_sync)로 분리했다.

시험 목록(bt-back 지시 그대로):
    - 재압축이 실제로 메인 루프에서 도는가(가짜 압축 함수 주입, 루프 identity 확인)
    - 재압축 실패 시 최신 내용이 살아남는다(폴백 방향)
    - 멱등 — 같은 call_id 로 두 번 불려도 두 번째는 아무것도 안 한다
    - 실패 격리(R5) — 재압축 단계가 예외를 던져도 함수 밖으로 안 샌다
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C7)
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import settings as app_settings
from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.service import chat_memory_service
from domains.learning.service import normalcall_service as svc


@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def ctx(session_factory):
    db = session_factory()
    voice = Voice(name="V", gender="male"); db.add(voice); db.flush()
    ch = Character(name="바바", role="선생님", personality="시크", voice_id=voice.voice_id, price=0)
    db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-cm-pipe")
    db.add(m); db.flush()
    call_id = svc.create_call(db, m.member_id, ch.character_id, "chat")
    db.add(CallRawData(call_id=call_id, role="user", turn_index=0, content="오늘 요리 얘기를 했어요"))
    db.add(CallRawData(call_id=call_id, role="beaver", turn_index=1, content="재밌었겠네요"))
    call_id_2 = svc.create_call(db, m.member_id, ch.character_id, "chat")
    db.add(CallRawData(call_id=call_id_2, role="user", turn_index=0, content="여행 얘기도 했어요"))
    db.commit()
    return {
        "db": db, "member_id": m.member_id, "call_id": call_id, "call_id_2": call_id_2,
        "session_factory": session_factory,
    }


def _fake_slots(topic="요리"):
    async def _summarize(client, model, tail):
        return {
            "topic": topic, "learner_facts": ["채식주의자다"],
            "pending": "다음엔 여행 얘기", "interests": ["요리", "여행"],
        }
    return _summarize


# --------------------------------------------------------------------------- #
# 1) 재압축이 실제로 메인 루프에서 돈다 — 핵심 회귀
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_recompression_runs_on_the_main_event_loop(ctx, monkeypatch):
    """⛔⛔ 핵심 회귀 — 옛 코드는 threadpool 안에서 asyncio.run() 으로 새 루프를
    만들어 재압축을 돌렸다. 압축 함수를 가짜로 주입해 **호출된 루프**가 이 시험이
    도는 루프(=extract_and_merge_chat_memory 를 부른 메인 루프)와 같은지 본다.
    """
    this_loop = asyncio.get_running_loop()
    seen: dict = {}

    async def fake_recompress(client, model, old, new):
        seen["loop"] = asyncio.get_running_loop()
        return (new + " " + old).strip()

    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots())
    monkeypatch.setattr(chat_memory_service, "recompress_summary", fake_recompress)

    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
    )

    assert seen.get("loop") is this_loop, \
        "재압축이 다른 이벤트 루프에서 돌았다 — 옛 asyncio.run 함정이 재발했다"

    row = chat_memory_service.load(ctx["db"], ctx["member_id"], "ko")
    assert row is not None and row.last_call_id == ctx["call_id"]


# --------------------------------------------------------------------------- #
# 2) 재압축 실패 시 최신 내용이 살아남는다(폴백 방향)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fallback_keeps_the_newest_content_when_recompression_fails(ctx, monkeypatch):
    """⚠ 두 번째 통화(기존 old_summary 가 이미 있는 상태)에서 재압축이 실패해도,
    상한(1,200자)에 걸려 잘리는 쪽은 **옛 내용**이어야 한다(Q3 폴백 방향 수정) —
    첫 통화(old_summary="")만 쓰면 특수분기(early-return)라 이 경로를 못 본다."""
    async def boom(*a, **k):
        raise RuntimeError("gemini 장애 흉내")

    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots(topic="요리"))
    monkeypatch.setattr(chat_memory_service.gemini_analysis, "generate_structured", boom)

    # 1차: old_summary="" 특수분기(재압축 안 탐) — 기반을 만든다.
    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
    )
    row1 = chat_memory_service.load(ctx["db"], ctx["member_id"], "ko")
    assert row1 is not None and "화제: 요리" in row1.summary

    # 2차: old_summary 가 이미 있는 상태 — 재압축 실패 폴백이 실제로 도는 경로.
    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots(topic="여행"))
    await svc.extract_and_merge_chat_memory(
        ctx["call_id_2"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
    )
    # 실제 쓰기는 run_db(threadpool)의 별도 세션이 했다 — ctx["db"] 의 identity map 에
    # 캐시된 row1 인스턴스를 그대로 돌려주지 않도록 새로 읽는다.
    ctx["db"].expire_all()
    row2 = chat_memory_service.load(ctx["db"], ctx["member_id"], "ko")
    assert row2 is not None
    assert row2.summary.startswith("화제: 여행"), \
        "재압축 실패 폴백에서 이번(최신) 통화 내용이 앞에 오지 않았다 — 옛 폴백 순서로 돌아갔다"


# --------------------------------------------------------------------------- #
# 3) 멱등 — 같은 call_id 로 두 번 불려도 두 번째는 아무것도 안 한다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_idempotent_on_the_same_call_id(ctx, monkeypatch):
    calls = {"n": 0}

    async def counting_recompress(client, model, old, new):
        calls["n"] += 1
        return (new + " " + old).strip()

    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots())
    monkeypatch.setattr(chat_memory_service, "recompress_summary", counting_recompress)

    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
    )
    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
    )
    assert calls["n"] == 1, "같은 call_id 로 두 번째 호출인데 재압축이 또 돌았다(멱등 깨짐)"


# --------------------------------------------------------------------------- #
# 4) 실패 격리(R5) — 재압축 단계가 예외를 던져도 함수 밖으로 안 샌다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_recompression_exception_does_not_escape(ctx, monkeypatch):
    async def boom(client, model, old, new):
        raise RuntimeError("recompress 폭발 흉내 — R5 로 흡수돼야 한다")

    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots())
    monkeypatch.setattr(chat_memory_service, "recompress_summary", boom)

    # 예외가 여기서 다시 터지면 이 시험 자체가 실패한다.
    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
    )
    # 실패했으니 저장도 안 됐어야 한다(부분 저장 없음).
    row = chat_memory_service.load(ctx["db"], ctx["member_id"], "ko")
    assert row is None


# --------------------------------------------------------------------------- #
# 5) R4-c(2026-09-24, bt-back) — 중간 조각은 재압축 없이 슬롯만 싼값에 접는다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_intermediate_fragment_merges_slots_without_recompressing(ctx, monkeypatch):
    """⛔⛔ 핵심 재현·수정 확인 — `is_final=False`(중간 조각)면 `recompress_summary`
    (LLM)를 **안 부르고**, 그래도 topics/facts 는 chat_memory 에 실제로 남는다(조각1
    종료 후 재연결 안 해도 그 시점까지의 기억이 남는다, R4-c 원래 버그: 지금 0).

    ⚠ `summarize_for_resume_text`(슬롯 추출)는 중간 조각에도 여전히 부른다 — 슬롯 자체가
    없으면 접을 게 없다. 이건 `build_resume_context`(이어하기 브리프)가 매 조각 끝마다
    이미 부르는 것과 같은 호출이라 **증분 비용이 아니다**(그 함수 문서 참조). 여기서
    "LLM 0회"로 확인하는 건 **재압축**(recompress_summary, 사장님 경고가 겨눈 바로 그
    단계) 하나뿐이다.
    """
    recompress_calls = {"n": 0}

    async def counting_recompress(client, model, old, new):
        recompress_calls["n"] += 1
        return (new + " " + old).strip()

    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots(topic="요리"))
    monkeypatch.setattr(chat_memory_service, "recompress_summary", counting_recompress)

    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
        is_final=False,
    )

    assert recompress_calls["n"] == 0, "중간 조각인데 재압축(LLM)이 돌았다 — 사장님 경고를 어겼다"
    row = chat_memory_service.load(ctx["db"], ctx["member_id"], "ko")
    assert row is not None, "중간 조각 종료 후 재연결 안 해도 남아야 할 기억이 아예 없다"
    assert "요리" in row.topics
    assert "채식주의자다" in row.facts
    assert row.summary == "", "중간 조각이 재압축 없이 summary 를 건드렸다(비어 있어야 한다)"


@pytest.mark.asyncio
async def test_intermediate_fragment_merge_is_not_blocked_across_fragments(ctx, monkeypatch):
    """⛔⛔ bt-back 이 «제일 틀리기 쉽다» 고 짚은 자리 — 멱등 가드(`last_call_id`)가
    중간 조각 저장에도 걸리면 **같은 call_id 를 공유하는** 다음 조각이 무시된다
    (조각 전환은 새 call_id 를 안 만든다, `resume_call` 계약). 조각1의 화제와
    조각2의 화제가 **둘 다** 남아야 한다."""
    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots(topic="요리"))
    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
        is_final=False,
    )
    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots(topic="여행"))
    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
        is_final=False,
    )
    ctx["db"].expire_all()
    row = chat_memory_service.load(ctx["db"], ctx["member_id"], "ko")
    assert row is not None
    assert "요리" in row.topics and "여행" in row.topics, \
        "조각2 의 중간 merge 가 조각1 의 내용을 밀어냈거나 멱등 가드에 막혔다"


@pytest.mark.asyncio
async def test_final_fragment_recompresses_even_after_intermediate_merges(ctx, monkeypatch):
    """⛔⛔ 마지막 조각은 그 전에 중간 조각 merge 가 몇 번 있었어도 재압축이 **돌아야**
    한다 — `merge_slots_only` 가 `last_call_id` 를 안 건드리는 이유가 정확히 이것이다
    (건드렸다면 여기서 `load_old_summary_for_merge` 가 "이미 이 call_id 로 병합함"
    으로 오판해 재압축을 건너뛴다)."""
    recompress_calls = {"n": 0}

    async def counting_recompress(client, model, old, new):
        recompress_calls["n"] += 1
        return (new + " " + old).strip()

    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots(topic="요리"))
    monkeypatch.setattr(chat_memory_service, "recompress_summary", counting_recompress)

    # 조각1(중간) — 재압축 없이 슬롯만.
    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
        is_final=False,
    )
    assert recompress_calls["n"] == 0

    # 조각2(마지막, 같은 call_id) — 전체 병합이 돌아야 한다.
    monkeypatch.setattr(svc, "summarize_for_resume_text", _fake_slots(topic="여행"))
    await svc.extract_and_merge_chat_memory(
        ctx["call_id"], ctx["member_id"], "ko", client=object(),
        settings_obj=app_settings, session_factory=ctx["session_factory"],
        is_final=True,
    )
    assert recompress_calls["n"] == 1, "마지막 조각인데 중간 조각의 흔적 때문에 재압축이 건너뛰어졌다"
    ctx["db"].expire_all()
    row = chat_memory_service.load(ctx["db"], ctx["member_id"], "ko")
    assert row is not None and row.last_call_id == ctx["call_id"]
    assert row.summary != "", "마지막 조각인데 summary 가 여전히 비어 있다"
