"""C9(2026-09-23) — 현지인 표현 저장·TTS(_save_analysis 통합, 실제 DB 사용).

C8 은 스키마·지시문·정규화만 순수 시험했다(tests/test_native_expression_pair.py).
이 파일은 그 정규화된 값이 **실제로 어떻게 저장되는지**를 인메모리 sqlite 로 시험한다.

시험 목록(bt-back 경계조건 그대로):
    ① 짝 행 생성 + paired_sentence_id 가 기본 행을 가리킨다
    ② 짝이 없는 표현은 기본 행만(짝 행 0)
    ③ dedup(seen)으로 기본 행이 건너뛰어지면 짝도 안 만든다
    ④ TTS 대상(pending) 목록에 짝 행도 (sentence_id, korean) 로 들어간다
    ⑤ 기본 행의 kind·paired_sentence_id·nuance 는 전부 NULL
    ⑥ 기존 북마크·소프트삭제가 짝 행에서도 그대로 동작(확인만, 추가 코드 없음)
    ⑦ 재분석 중복 방지는 "삭제 후 재생성"이 아니라 status 게이트 + 단일 커밋 구조다
       (prepare_reanalysis 는 status=='failed' 일 때만 허용 — 그 시점엔 이 통화의
       Sentence 가 하나도 없다는 뜻이므로 짝 행도 두 배로 늘 수 없다).
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C9)
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
from domains.learning.models.sentence import Sentence
from domains.learning.service.normalcall_service import (
    LearnedExpression,
    _CallAnalysisBase,
    _save_analysis,
)
from domains.learning.service.sentence_service import SentenceService


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
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-native-pair")
    db.add(m); db.flush()
    c = Call(member_id=m.member_id, character_id=ch.character_id, status="analyzing", call_type="chat")
    db.add(c); db.commit()
    return {"db": db, "member_id": m.member_id, "call_id": c.call_id}


def _result(expressions: list[LearnedExpression]) -> _CallAnalysisBase:
    return _CallAnalysisBase(summary="요약", detected_mode="chat", expressions=expressions)


# --------------------------------------------------------------------------- #
# ① 짝 행 생성 + 연결
# --------------------------------------------------------------------------- #
def test_pair_row_is_created_and_linked_to_the_base_row(ctx):
    e = LearnedExpression(
        korean="감사합니다", translation="thank you", source_type="asked",
        native_expression="고마워요", native_expression_translation="thanks",
        native_nuance="더 캐주얼함",
    )
    pending = _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")

    rows = ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"]).order_by(Sentence.sentence_id).all()
    assert len(rows) == 2
    base, pair = rows
    assert base.kind is None
    assert pair.kind == "native"
    assert pair.paired_sentence_id == base.sentence_id
    assert pair.korean_sentence == "고마워요"
    assert pair.native_sentence == "thanks"
    assert pair.nuance == "더 캐주얼함"
    assert len(pending) == 2


# --------------------------------------------------------------------------- #
# ② 짝이 없으면 기본 행만
# --------------------------------------------------------------------------- #
def test_no_pair_row_when_expression_has_no_native_pair(ctx):
    e = LearnedExpression(korean="안녕하세요", translation="hello", source_type="asked")
    pending = _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")

    rows = ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"]).all()
    assert len(rows) == 1
    assert rows[0].kind is None
    assert len(pending) == 1


# --------------------------------------------------------------------------- #
# ③ dedup 으로 기본 행이 건너뛰어지면 짝도 안 만든다
# --------------------------------------------------------------------------- #
def test_dedup_skips_the_pair_too_when_the_base_expression_is_a_duplicate(ctx):
    e1 = LearnedExpression(
        korean="감사합니다", translation="thank you", source_type="asked",
        native_expression="고마워요", native_expression_translation="thanks",
    )
    e2 = LearnedExpression(
        korean="감사합니다", translation="thank you (dup)", source_type="corrected",
        native_expression="땡큐", native_expression_translation="thx",
    )
    pending = _save_analysis(ctx["db"], ctx["call_id"], _result([e1, e2]), "en")

    rows = ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"]).all()
    # 기본 1(e1) + 짝 1(e1) = 2. e2 는 seen 에 걸려 기본·짝 모두 생성되지 않는다.
    assert len(rows) == 2
    natives = [r for r in rows if r.kind == "native"]
    assert len(natives) == 1
    assert natives[0].korean_sentence == "고마워요"
    assert len(pending) == 2


# --------------------------------------------------------------------------- #
# ④ TTS pending 목록에 짝 행 포함
# --------------------------------------------------------------------------- #
def test_pending_tts_targets_include_the_pair_row(ctx):
    e = LearnedExpression(
        korean="화이팅", translation="fighting", source_type="drilled",
        native_expression="파이팅", native_expression_translation="fighting (casual)",
    )
    pending = _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")
    koreans = {korean for _sid, korean in pending}
    assert "화이팅" in koreans
    assert "파이팅" in koreans


# --------------------------------------------------------------------------- #
# ⑤ 기본 행은 세 칸 전부 NULL
# --------------------------------------------------------------------------- #
def test_base_row_has_all_three_new_columns_null(ctx):
    e = LearnedExpression(
        korean="반가워요", translation="nice to meet you", source_type="asked",
        native_expression="반가워", native_expression_translation="nice to meet you (casual)",
    )
    _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")
    base = ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"], kind=None).one()
    assert base.kind is None
    assert base.paired_sentence_id is None
    assert base.nuance is None


# --------------------------------------------------------------------------- #
# ⑥ 기존 북마크·소프트삭제가 짝 행에서도 그대로(확인만 — 추가 코드 없음)
# --------------------------------------------------------------------------- #
def test_existing_bookmark_and_soft_delete_flows_work_on_a_pair_row(ctx):
    e = LearnedExpression(
        korean="대박", translation="awesome", source_type="asked",
        native_expression="대박이다", native_expression_translation="that's awesome",
    )
    _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")
    pair = ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"], kind="native").one()

    svc = SentenceService(ctx["db"])
    out = svc.set_bookmark(ctx["member_id"], pair.sentence_id, True)
    assert out.is_bookmarked is True

    svc.soft_delete(ctx["member_id"], pair.sentence_id)
    ctx["db"].refresh(pair)
    assert pair.deleted_at is not None


# --------------------------------------------------------------------------- #
# ⑦ 재분석 중복 방지 — 삭제 로직이 아니라 status 게이트 + 단일 커밋 구조
# --------------------------------------------------------------------------- #
def test_reanalysis_never_doubles_pair_rows_because_failed_calls_have_zero_sentences(ctx):
    """⭐⭐ bt-back 조건⑤: 기존 재분석 경로에 "문장을 지우는" 코드는 없다(grep 확인,
    normalcall_service.py 에 Sentence 삭제 쿼리 0건). 보호막은 구조다 —
    `prepare_reanalysis` 는 call.status=='failed' 일 때만 재분석을 허용하고,
    `_save_analysis` 는 LLM 분석이 **성공**했을 때만(status="failed" 로 갈 일이 없을
    때만) 단일 커밋으로 호출된다. 즉 재분석이 열리는 시점엔 이 통화의 Sentence 가
    (기본이든 짝이든) 하나도 없다 — 이 시험은 그 전제를 직접 확인한다."""
    call = ctx["db"].get(Call, ctx["call_id"])
    call.status = "failed"
    ctx["db"].commit()

    # failed 상태 = _save_analysis 가 이 통화에 대해 한 번도 성공적으로 커밋된 적
    # 없다는 뜻 — 실제로 Sentence 가 0개인지 확인.
    assert ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"]).count() == 0

    e = LearnedExpression(
        korean="굿모닝", translation="good morning", source_type="asked",
        native_expression="좋은 아침", native_expression_translation="good morning (casual)",
    )
    _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")

    rows = ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"]).all()
    assert len(rows) == 2  # 기본 1 + 짝 1, 딱 그만큼 — 재분석이 두 번 겹쳐 쌓이지 않는다


# --------------------------------------------------------------------------- #
# ⑧ Q1(2026-09-24) — 조각 간(across-fragment) dedup: 저장 시 기존 활성 행과 대조
# --------------------------------------------------------------------------- #
# 운영 실측(call 1653, 910초·3조각): 활성 문장 34행인데 고유 표면형 14개 — 조각마다
# 같은 표현을 새 행으로 또 저장해 TTS 도 그만큼 다시 과금됐다. `_save_analysis` 를
# **두 번** 부르는(조각1→조각2 재현) 것이 이 회귀의 핵심 구조다.
def test_second_fragment_does_not_resave_an_already_stored_expression(ctx):
    """⛔⛔ 핵심 회귀 — 조각2 분석이 조각1과 같은 표현을 다시 저장하면 안 된다."""
    e = LearnedExpression(korean="안녕하세요", translation="hello", source_type="asked")
    pending1 = _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")
    assert len(pending1) == 1

    # 조각2: analyze_call 은 전사 전체를 다시 보므로 같은 표현이 다시 추출된다(재현).
    pending2 = _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")
    assert pending2 == [], "조각2가 이미 저장된 표현을 TTS 대상(pending)에 다시 올렸다 — 재합성 과금"

    rows = ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"]).all()
    assert len(rows) == 1, "조각2가 같은 표현을 중복 저장했다"


def test_second_fragment_still_saves_a_genuinely_new_expression(ctx):
    """조각2에서 **새로 나온** 표현은 정상 저장된다 — dedup 이 과도하게 막지 않는다."""
    e1 = LearnedExpression(korean="안녕하세요", translation="hello", source_type="asked")
    e2 = LearnedExpression(korean="반갑습니다", translation="nice to meet you", source_type="asked")
    _save_analysis(ctx["db"], ctx["call_id"], _result([e1]), "en")
    pending2 = _save_analysis(ctx["db"], ctx["call_id"], _result([e1, e2]), "en")

    assert {k for _sid, k in pending2} == {"반갑습니다"}
    rows = ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"]).all()
    assert len(rows) == 2, "새 표현 1개만 추가돼 총 2행이어야 한다(중복 1 + 신규 1)"


def test_second_fragment_does_not_reduplicate_the_native_pair(ctx):
    """짝(kind='native')도 같은 규칙을 탄다 — 짝이 중복되면 2배로 는다."""
    e = LearnedExpression(
        korean="감사합니다", translation="thank you", source_type="asked",
        native_expression="고마워요", native_expression_translation="thanks",
    )
    _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")
    pending2 = _save_analysis(ctx["db"], ctx["call_id"], _result([e]), "en")
    assert pending2 == []

    rows = ctx["db"].query(Sentence).filter_by(call_id=ctx["call_id"]).all()
    assert len(rows) == 2, "기본 1 + 짝 1 그대로여야 한다 — 조각2가 짝까지 중복시키면 안 된다"
    natives = [r for r in rows if r.kind == "native"]
    assert len(natives) == 1


def test_whitespace_variant_across_fragments_still_dedups(ctx):
    """정규화(normalize_text, 공백 전제거)는 검출 게이트 dedup 과 같은 기준이다 —
    조각 간 STT/모델 산출이 공백만 다르게 내도 같은 표현으로 잡혀야 한다."""
    e1 = LearnedExpression(korean="안녕 하세요", translation="hello", source_type="asked")
    e2 = LearnedExpression(korean="안녕하세요", translation="hello", source_type="asked")
    _save_analysis(ctx["db"], ctx["call_id"], _result([e1]), "en")
    pending2 = _save_analysis(ctx["db"], ctx["call_id"], _result([e2]), "en")
    assert pending2 == [], "공백만 다른 조각2 표현이 별개로 저장됐다"
