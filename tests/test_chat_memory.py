"""C6(2026-09-23) — 자유대화 기억 저장소(chat_memory_service).

시험 목록(문서 그대로): 첫 저장 · 합치기(중복·상한) · 재압축 실패 폴백 · 멱등.
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md
"""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.learning.models.chat_memory import ChatMemory
from domains.learning.service import chat_memory_service as svc


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
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-cm")
    db.add(m); db.commit()
    return {"db": db, "member_id": m.member_id}


# --------------------------------------------------------------------------- #
# 1) 첫 저장 — 아직 기억이 없다
# --------------------------------------------------------------------------- #
def test_load_returns_none_before_any_memory(ctx):
    assert svc.load(ctx["db"], ctx["member_id"], "ko") is None


@pytest.mark.asyncio
async def test_first_merge_creates_the_row(ctx):
    row = await svc.merge(
        ctx["db"], ctx["member_id"], "ko", call_id=1,
        slots={
            "summary": "학습자는 요리를 좋아한다고 말했다.",
            "topics": ["요리"], "facts": ["채식주의자다"],
            "interests": ["요리", "여행"], "next_topics": ["다음엔 여행 이야기"],
        },
    )
    assert row.member_id == ctx["member_id"]
    assert row.language == "ko"
    assert row.last_call_id == 1
    assert row.summary == "학습자는 요리를 좋아한다고 말했다."
    assert row.topics == ["요리"]
    assert row.facts == ["채식주의자다"]
    assert row.interests == ["요리", "여행"]
    assert row.next_topics == ["다음엔 여행 이야기"]

    loaded = svc.load(ctx["db"], ctx["member_id"], "ko")
    assert loaded is not None and loaded.chat_memory_id == row.chat_memory_id


@pytest.mark.asyncio
async def test_different_language_gets_a_separate_row(ctx):
    """(멀티랭귀지) 같은 회원이라도 언어가 다르면 다른 기억 행이다."""
    await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1, slots={"topics": ["한국어 화제"]})
    await svc.merge(ctx["db"], ctx["member_id"], "ja", call_id=2, slots={"topics": ["日本語の話題"]})

    assert svc.load(ctx["db"], ctx["member_id"], "ko").topics == ["한국어 화제"]
    assert svc.load(ctx["db"], ctx["member_id"], "ja").topics == ["日本語の話題"]


# --------------------------------------------------------------------------- #
# 2) 합치기 — 중복 제거 · 최신 우선 · 상한
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_merge_dedupes_and_prefers_the_newest(ctx):
    await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1,
                     slots={"topics": ["요리", "영화"]})
    row = await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=2,
                           slots={"topics": ["여행", "요리"]})  # "요리" 중복
    # 최신 것(이번 통화)이 앞에 오고, 중복("요리")은 한 번만 남는다.
    assert row.topics == ["여행", "요리", "영화"]


@pytest.mark.asyncio
async def test_merge_caps_each_array_at_its_documented_limit(ctx):
    await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1, slots={
        "topics": [f"t{i}" for i in range(8)],
        "facts": [f"f{i}" for i in range(12)],
        "interests": [f"i{i}" for i in range(8)],
        "next_topics": [f"n{i}" for i in range(4)],
    })
    row = await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=2, slots={
        "topics": [f"t{i}" for i in range(8, 12)],       # 8+4=12 > 10 상한
        "facts": [f"f{i}" for i in range(12, 16)],        # 12+4=16 > 15 상한
        "interests": [f"i{i}" for i in range(8, 12)],     # 8+4=12 > 10 상한
        "next_topics": [f"n{i}" for i in range(4, 8)],    # 4+4=8 > 5 상한
    })
    assert len(row.topics) == svc.TOPICS_CAP == 10
    assert len(row.facts) == svc.FACTS_CAP == 15
    assert len(row.interests) == svc.INTERESTS_CAP == 10
    assert len(row.next_topics) == svc.NEXT_TOPICS_CAP == 5
    # 최신 우선이므로 이번 통화(t8~t11)가 전부 살아 있어야 한다.
    assert {"t8", "t9", "t10", "t11"} <= set(row.topics)


def test_merge_capped_helper_is_order_preserving_and_ignores_blanks(ctx):
    out = svc._merge_capped(["a", "b"], ["c", "", None, "a", 5], cap=10)
    assert out == ["c", "a", "b"], "새 항목이 앞, 중복 제거, 빈 값/비문자열 무시"


# --------------------------------------------------------------------------- #
# 3) 재압축 실패(또는 LLM 미제공) 폴백 — 옛 요약 유지 + 이번 요약을 뒤에 붙여 자르기
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_recompress_falls_back_when_no_client_is_given(ctx):
    await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1,
                     slots={"summary": "옛 요약 문장."})
    row = await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=2,
                           slots={"summary": "이번 통화 요약 문장."})
    assert row.summary == "옛 요약 문장. 이번 통화 요약 문장."


@pytest.mark.asyncio
async def test_recompress_falls_back_when_the_llm_call_raises(ctx, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("gemini 장애 흉내")

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", boom)

    await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1, slots={"summary": "옛 요약."})
    row = await svc.merge(
        ctx["db"], ctx["member_id"], "ko", call_id=2, slots={"summary": "이번 요약."},
        client=object(), model="gemini-2.5-flash",
    )
    assert row.summary == "옛 요약. 이번 요약."


@pytest.mark.asyncio
async def test_recompress_falls_back_when_the_llm_returns_empty(ctx, monkeypatch):
    async def empty(*a, **k):
        return svc._RecompressOut(summary="")

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", empty)

    await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1, slots={"summary": "옛 요약."})
    row = await svc.merge(
        ctx["db"], ctx["member_id"], "ko", call_id=2, slots={"summary": "이번 요약."},
        client=object(), model="gemini-2.5-flash",
    )
    assert row.summary == "옛 요약. 이번 요약."


@pytest.mark.asyncio
async def test_recompress_uses_the_llm_result_when_available(ctx, monkeypatch):
    async def fake(*a, **k):
        return svc._RecompressOut(summary="재압축된 요약.")

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", fake)

    await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1, slots={"summary": "옛 요약."})
    row = await svc.merge(
        ctx["db"], ctx["member_id"], "ko", call_id=2, slots={"summary": "이번 요약."},
        client=object(), model="gemini-2.5-flash",
    )
    assert row.summary == "재압축된 요약."


@pytest.mark.asyncio
async def test_summary_is_hard_capped_at_1200_chars(ctx):
    huge = "가" * 2000
    row = await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1, slots={"summary": huge})
    assert len(row.summary) == svc.SUMMARY_CHAR_CAP == 1200


# --------------------------------------------------------------------------- #
# 4) 멱등 — 같은 call_id 로 두 번 merge 하면 두 번째는 아무것도 안 한다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_merge_is_idempotent_on_the_same_call_id(ctx):
    first = await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1,
                             slots={"topics": ["요리"], "summary": "첫 요약."})
    again = await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1,
                             slots={"topics": ["다른 화제가 와도"], "summary": "다른 요약이 와도"})
    assert again.chat_memory_id == first.chat_memory_id
    assert again.topics == ["요리"], "같은 call_id 인데 두 번째 merge 내용이 반영됐다"
    assert again.summary == "첫 요약."


@pytest.mark.asyncio
async def test_merge_proceeds_for_a_genuinely_new_call_id(ctx):
    await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=1, slots={"topics": ["요리"]})
    row = await svc.merge(ctx["db"], ctx["member_id"], "ko", call_id=2, slots={"topics": ["여행"]})
    assert row.last_call_id == 2
    assert row.topics == ["여행", "요리"]
