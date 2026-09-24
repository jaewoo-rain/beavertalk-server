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
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.service.normalcall_service import (
    call_user_word_count,
    finalize_call,
    fragment_user_word_count,
)


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


# --------------------------------------------------------------------------- #
# 3) call_user_word_count — Q2(2026-09-24) DB 전체 재계산(실시간 경로의 신 산식)
# --------------------------------------------------------------------------- #
# 운영 실측: user_word_count NULL 1,570건 / 값 있음 3건. 옛 경로(_persist_remaining)는
# 조각의 in-memory 미저장 세그먼트(`new`)만 셌는데, `_periodic_flush`(60초마다)가
# 커서만 올리고 단어는 안 세서 5분 통화면 "마지막 flush tick 이후분"만 남았다.
def _row(ctx, *, turn_index: int, role: str, text: str) -> None:
    ctx["db"].add(CallRawData(
        call_id=ctx["call"].call_id, role=role, turn_index=turn_index, content=text,
    ))
    ctx["db"].commit()


def test_call_user_word_count_sums_the_whole_transcript_not_just_the_last_flush(ctx):
    """⛔⛔ 핵심 회귀 — 5분 통화(flush 여러 번)의 단어 수가 **전체**를 센다.

    옛 방식은 마지막 flush tick 이후의 세그먼트만 셌다(대부분 비거나 일부만) — 여기서는
    flush 4번을 흉내내 turn 을 나눠 커밋하고, 그래도 전체 합이 나오는지 본다.
    """
    # flush 1~4 를 흉내낸 것처럼 여러 번에 걸쳐 커밋(순서·시점은 call_user_word_count 가
    # 신경 쓰지 않는다 — DB 에 이미 있는 전체를 그때그때 다시 읽을 뿐이다).
    _row(ctx, turn_index=0, role="beaver", text="안녕하세요! 오늘도 화이팅")
    _row(ctx, turn_index=1, role="user", text="안녕 오늘 날씨 좋다")       # 4어절
    _row(ctx, turn_index=2, role="beaver", text="맞아요 산책하기 좋아요")
    _row(ctx, turn_index=3, role="user", text="네 저는 공원에 갈 거예요")   # 5어절
    _row(ctx, turn_index=4, role="beaver", text="좋은 생각이에요")
    _row(ctx, turn_index=5, role="user", text="같이 가실래요")            # 2어절

    n = call_user_word_count(ctx["db"], ctx["call"].call_id, "ko")
    assert n == 11, "마지막 flush 구간만이 아니라 전체 user 발화를 세어야 한다"  # 4+5+2


def test_call_user_word_count_is_not_none_right_after_a_flush_tick(ctx):
    """flush tick 직후(=이 조각에 새로 저장할 게 없는 시점)에 종료해도 NULL 이 아니다."""
    _row(ctx, turn_index=0, role="user", text="벌써 다 저장됐어요")  # 3어절
    # 이 시점 이후 새 세그먼트가 없어도(=옛 `new` 가 비어도) 전체 재계산은 그대로 값을 낸다.
    n = call_user_word_count(ctx["db"], ctx["call"].call_id, "ko")
    assert n == 3


def test_call_user_word_count_correct_for_a_short_under_a_minute_call(ctx):
    """60초 미만(flush 가 한 번도 안 돈) 통화도 정확하다 — 회귀 방지."""
    _row(ctx, turn_index=0, role="user", text="짧은 통화입니다")  # 2어절
    n = call_user_word_count(ctx["db"], ctx["call"].call_id, "ko")
    assert n == 2


def test_call_user_word_count_no_transcript_returns_none(ctx):
    """전사가 없으면(user 발화 0건) NULL 유지 — 0 으로 쓰지 않는다."""
    _row(ctx, turn_index=0, role="beaver", text="비버만 말했어요")
    assert call_user_word_count(ctx["db"], ctx["call"].call_id, "ko") is None
    assert call_user_word_count(ctx["db"], ctx["call"].call_id, "ko") is None  # 전사 자체가 없어도 동일


def test_call_user_word_count_ja_script_chars_path_still_works(ctx):
    """ja·zh 글자수÷2 환산이 DB 경로에서도 그대로 적용된다."""
    _row(ctx, turn_index=0, role="user", text="こんにちは、元気ですか")  # 10자 → 5
    assert call_user_word_count(ctx["db"], ctx["call"].call_id, "ja") == 5


def test_finalize_call_across_two_fragments_does_not_double_count(ctx):
    """⛔⛔ 조각 여럿에서 이중 계산 없음 — call_session.py 의 실제 호출 패턴
    (accumulate=False 로 매번 `call_user_word_count` 재계산·SET)을 그대로 흉내낸다.
    """
    # 조각1: user 발화 1건(4어절) → finalize.
    _row(ctx, turn_index=0, role="user", text="안녕 오늘 날씨 좋다")
    finalize_call(
        ctx["db"], ctx["call"].call_id, status="analyzing", accumulate=False,
        user_word_count=call_user_word_count(ctx["db"], ctx["call"].call_id, "ko"),
    )
    ctx["db"].refresh(ctx["call"])
    assert ctx["call"].user_word_count == 4

    # 조각2: 새 user 발화 1건(2어절) 추가 → finalize 를 다시 accumulate=False 로.
    _row(ctx, turn_index=1, role="user", text="같이 가실래요")
    finalize_call(
        ctx["db"], ctx["call"].call_id, status="analyzing", accumulate=False,
        user_word_count=call_user_word_count(ctx["db"], ctx["call"].call_id, "ko"),
    )
    ctx["db"].refresh(ctx["call"])
    assert ctx["call"].user_word_count == 6, "조각1 값에 조각1+2 합계를 또 더하면 10 이 된다 — SET 이어야 한다"
