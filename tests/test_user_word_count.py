"""C11(2026-09-23) — 학습 달력용 사용자 단어 수 저장(call.user_word_count).

시험 목록(문서 그대로 + bt-back 경계조건):
    - ko 공백 분할
    - ja 글자수/2 반올림(문장부호·공백 제외 — count_target_script_chars)
    - 조각 누적(total_time 과 같은 방식, finalize_call)
    - 사용자 전사 없으면 NULL(0 아님) — 통화 내내 없으면 NULL 그대로, 나중 조각에
      전사가 없어도 이전에 쌓인 값을 지우지 않는다
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C11)
"""

from __future__ import annotations

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.service.normalcall_service import finalize_call, fragment_user_word_count


# --------------------------------------------------------------------------- #
# 1) fragment_user_word_count — 순수 함수(DB 없음)
# --------------------------------------------------------------------------- #
def test_ko_counts_by_space_split():
    segs = [{"role": "user", "text": "안녕 오늘 날씨 좋다"}]
    assert fragment_user_word_count(segs, "ko") == 4


def test_ja_counts_script_chars_over_two_rounded():
    """⭐⭐ bt-back 조건②: ja 는 공백·문장부호 뺀 글자수 ÷2 반올림(count_target_script_chars)."""
    # 「こんにちは、元気ですか」 — 한자·가나만 세면(문장부호·공백 제외) 10글자 → 10/2 = 5
    segs = [{"role": "user", "text": "こんにちは、元気ですか"}]
    assert fragment_user_word_count(segs, "ja") == 5


def test_zh_counts_script_chars_over_two_rounded():
    segs = [{"role": "user", "text": "你好，今天天气怎么样"}]  # 문장부호 제외 9자 → round(9/2)=4(반올림, banker's 아님 표준round)
    n = fragment_user_word_count(segs, "zh")
    assert n == round(9 / 2)


def test_only_user_role_segments_are_counted():
    segs = [
        {"role": "beaver", "text": "이것도 많이 말해요 무시해야 함"},
        {"role": "user", "text": "짧게 대답"},
    ]
    assert fragment_user_word_count(segs, "ko") == 2


def test_no_user_transcript_returns_none_not_zero():
    """⭐⭐ bt-back 조건③ — «집계 없음» 은 0 이 아니라 None 이다."""
    assert fragment_user_word_count([], "ko") is None
    assert fragment_user_word_count([{"role": "beaver", "text": "안녕"}], "ko") is None
    assert fragment_user_word_count([{"role": "user", "text": "   "}], "ko") is None


def test_multiple_user_turns_are_joined_before_counting():
    segs = [
        {"role": "user", "text": "안녕하세요"},
        {"role": "beaver", "text": "네 반가워요"},
        {"role": "user", "text": "오늘 날씨"},
    ]
    assert fragment_user_word_count(segs, "ko") == 3  # "안녕하세요 오늘 날씨" → 3어절


# --------------------------------------------------------------------------- #
# 2) finalize_call — 조각 누적 + None 이면 컬럼 안 건드림
# --------------------------------------------------------------------------- #
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
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-word-count")
    db.add(m); db.flush()
    c = Call(member_id=m.member_id, character_id=ch.character_id, status="ongoing", call_type="chat")
    db.add(c); db.commit()
    return {"db": db, "call": c}


def test_first_fragment_sets_the_count(ctx):
    finalize_call(ctx["db"], ctx["call"].call_id, total_time=60, status="analyzing",
                  accumulate=False, user_word_count=8)
    ctx["db"].refresh(ctx["call"])
    assert ctx["call"].user_word_count == 8


def test_second_fragment_accumulates_like_total_time(ctx):
    finalize_call(ctx["db"], ctx["call"].call_id, total_time=60, status="analyzing",
                  accumulate=False, user_word_count=8)
    finalize_call(ctx["db"], ctx["call"].call_id, total_time=60, status="analyzing",
                  accumulate=True, user_word_count=5)
    ctx["db"].refresh(ctx["call"])
    assert ctx["call"].user_word_count == 13
    assert ctx["call"].total_time == 120


def test_a_fragment_with_no_transcript_does_not_erase_the_accumulated_total(ctx):
    """⭐⭐ 나중 조각에 사용자 발화가 없어도(word_count=None) 이전 누적값을 지우지 않는다."""
    finalize_call(ctx["db"], ctx["call"].call_id, total_time=60, status="analyzing",
                  accumulate=False, user_word_count=8)
    finalize_call(ctx["db"], ctx["call"].call_id, total_time=60, status="analyzing",
                  accumulate=True, user_word_count=None)
    ctx["db"].refresh(ctx["call"])
    assert ctx["call"].user_word_count == 8


def test_a_call_with_no_transcript_at_all_stays_null(ctx):
    """⭐⭐ bt-back 조건③ — 통화 내내 사용자 전사가 없으면 NULL 그대로(0 아님)."""
    finalize_call(ctx["db"], ctx["call"].call_id, total_time=60, status="done",
                  accumulate=False, user_word_count=None)
    ctx["db"].refresh(ctx["call"])
    assert ctx["call"].user_word_count is None
