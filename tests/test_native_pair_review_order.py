"""C10(2026-09-23) — 복습 목록 순서·필드(현지인 표현 짝).

bt-back 경계조건 그대로:
    ① CallResultSentence 에 kind·paired_sentence_id·nuance, 기본 문장은 세 키 생략
    ② 순서: 기본 → 그 짝 → 다음 기본 → 그 짝(order_sentences_with_pairs 헬퍼 하나)
    ③ 짝 없는 기본 · 기본이 없는 고아 짝(방어)에서도 순서가 안 깨진다
    ④ 발음 채점 — 현지인 문장도 점수가 실린다(추가 코드 없음, 확인만)
    ⑤ 북마크 목록 — kind·nuance 만 싣고(paired_sentence_id 없음) 순서는 종전(정렬 안 함)
근거: docs/plans/2026-09-22-프리미엄-자유대화-15분-달력.md(C10)
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
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.sentence import Sentence
from domains.learning.service.call_service import CallService
from domains.learning.service.pronunciation_service import build_sentence_scores
from domains.learning.service.sentence_service import SentenceService, order_sentences_with_pairs


# --------------------------------------------------------------------------- #
# ②③ order_sentences_with_pairs — 순수 함수(DB 없음, transient 객체)
# --------------------------------------------------------------------------- #
def _s(sentence_id: int, *, kind: str | None = None, paired_sentence_id: int | None = None) -> Sentence:
    s = Sentence(korean_sentence=f"s{sentence_id}", locale="en")
    s.sentence_id = sentence_id
    s.kind = kind
    s.paired_sentence_id = paired_sentence_id
    return s


def test_interleaves_base_and_its_pair_for_two_expressions():
    base1, pair1 = _s(1), _s(2, kind="native", paired_sentence_id=1)
    base2, pair2 = _s(3), _s(4, kind="native", paired_sentence_id=3)
    ordered = order_sentences_with_pairs([base1, pair1, base2, pair2])
    assert [s.sentence_id for s in ordered] == [1, 2, 3, 4]


def test_a_base_without_a_pair_stays_alone_and_order_holds():
    base1 = _s(1)
    base2, pair2 = _s(2), _s(3, kind="native", paired_sentence_id=2)
    ordered = order_sentences_with_pairs([base1, base2, pair2])
    assert [s.sentence_id for s in ordered] == [1, 2, 3]


def test_an_orphan_pair_whose_base_is_missing_goes_to_the_end_without_crashing():
    """⭐⭐ bt-back 조건③ 방어 — 기본이 소프트 삭제(active 목록에서 제외)돼도 짝이
    사라지거나 예외를 내지 않는다. 정상 경로로는 생기지 않는 모양이다."""
    base1, pair1 = _s(1), _s(2, kind="native", paired_sentence_id=1)
    orphan_pair = _s(99, kind="native", paired_sentence_id=1234)  # 1234 는 목록에 없음
    ordered = order_sentences_with_pairs([base1, pair1, orphan_pair])
    assert [s.sentence_id for s in ordered] == [1, 2, 99]


def test_only_a_pair_survives_when_its_base_was_excluded():
    """짝만 남은 경우(기본이 목록에서 빠짐)도 그대로 끝에 붙어 살아남는다."""
    pair_only = _s(5, kind="native", paired_sentence_id=4)
    ordered = order_sentences_with_pairs([pair_only])
    assert [s.sentence_id for s in ordered] == [5]


# --------------------------------------------------------------------------- #
# ①②④ 통합 — CallService.get_call_result
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
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id="auth-review-order")
    db.add(m); db.flush()
    c = Call(member_id=m.member_id, character_id=ch.character_id, status="done", call_type="chat")
    db.add(c); db.commit()
    return {"db": db, "member_id": m.member_id, "call_id": c.call_id}


def _add_pair(db, call_id: int, *, korean: str, native: str | None = None) -> tuple[Sentence, Sentence]:
    base = Sentence(call_id=call_id, korean_sentence=korean, native_sentence="x", locale="en",
                     source_type="asked", is_bookmarked=False, evaluation=Evaluation())
    db.add(base); db.flush()
    pair = Sentence(call_id=call_id, korean_sentence=native, native_sentence="y", locale="en",
                     source_type="asked", is_bookmarked=False, kind="native",
                     paired_sentence_id=base.sentence_id, nuance="캐주얼함", evaluation=Evaluation())
    db.add(pair); db.flush()
    return base, pair


def test_call_result_orders_base_then_pair_across_two_expressions(ctx):
    db, call_id = ctx["db"], ctx["call_id"]
    base1, pair1 = _add_pair(db, call_id, korean="감사합니다", native="고마워요")
    base2 = Sentence(call_id=call_id, korean_sentence="안녕", locale="en", source_type="asked",
                      is_bookmarked=False, evaluation=Evaluation())  # 짝 없음
    db.add(base2); db.flush()
    db.commit()

    result = CallService(db).get_call_result(ctx["member_id"], call_id)
    ids = [s.sentence_id for s in result.sentences]
    assert ids == [base1.sentence_id, pair1.sentence_id, base2.sentence_id]


def test_base_sentence_json_omits_the_three_keys_entirely(ctx):
    db, call_id = ctx["db"], ctx["call_id"]
    base = Sentence(call_id=call_id, korean_sentence="안녕", locale="en", source_type="asked",
                     is_bookmarked=False, evaluation=Evaluation())
    db.add(base); db.commit()

    result = CallService(db).get_call_result(ctx["member_id"], call_id)
    dumped = result.sentences[0].model_dump()
    assert "kind" not in dumped
    assert "paired_sentence_id" not in dumped
    assert "nuance" not in dumped


def test_pair_sentence_json_carries_kind_paired_id_and_nuance(ctx):
    db, call_id = ctx["db"], ctx["call_id"]
    base, pair = _add_pair(db, call_id, korean="감사합니다", native="고마워요")
    db.commit()

    result = CallService(db).get_call_result(ctx["member_id"], call_id)
    by_id = {s.sentence_id: s.model_dump() for s in result.sentences}
    assert by_id[pair.sentence_id]["kind"] == "native"
    assert by_id[pair.sentence_id]["paired_sentence_id"] == base.sentence_id
    assert by_id[pair.sentence_id]["nuance"] == "캐주얼함"


def test_native_pair_sentence_gets_scored_like_any_other(ctx):
    """⭐⭐ bt-back 조건④ — 발음 채점은 문장 단위라 추가 코드 없이 현지인 문장도
    build_sentence_scores 에 그대로 실린다(확인만)."""
    db, call_id = ctx["db"], ctx["call_id"]
    base, pair = _add_pair(db, call_id, korean="감사합니다", native="고마워요")
    pair.evaluation.total_score = 91
    pair.evaluation.pronunciation = 90
    pair.evaluation.fluency = 92
    pair.evaluation.rhythm = 88
    db.commit()

    ordered = order_sentences_with_pairs([base, pair])
    scores = build_sentence_scores(ordered)
    by_id = {s.sentence_id: s for s in scores}
    assert by_id[pair.sentence_id].total_score == 91
    assert by_id[pair.sentence_id].korean_sentence == "고마워요"


# --------------------------------------------------------------------------- #
# R5-b(2026-09-24, bt-back) — GET /calls/{call_id}(지난 통화 상세)도 /result 와
# 같은 헬퍼(order_sentences_with_pairs)로 같은 순서·필드를 준다
# --------------------------------------------------------------------------- #
def test_get_call_matches_get_call_result_order(ctx):
    """⛔⛔ 핵심 재현·수정 확인 — `/calls/{id}` 가 `/result` 와 **같은 순서**를 준다.

    ⚠ 두 기본을 **먼저** 넣고 그 짝을 **나중에** 넣는다(물리/관계 순서 =
    [기본1·기본2·짝1·짝2]) — `order_sentences_with_pairs` 없이는 이 물리 순서가
    그대로 나가 [기본1·짝1·기본2·짝2](정답)와 달라진다. 짝을 기본 바로 뒤에
    붙여 넣으면(관계 순서가 이미 정답과 같아) 정렬 누락이 있어도 우연히 통과해
    이 시험이 아무것도 못 잡는다."""
    db, call_id = ctx["db"], ctx["call_id"]
    base1 = Sentence(call_id=call_id, korean_sentence="감사합니다", locale="en", source_type="asked",
                      is_bookmarked=False, evaluation=Evaluation())
    base2 = Sentence(call_id=call_id, korean_sentence="안녕", locale="en", source_type="asked",
                      is_bookmarked=False, evaluation=Evaluation())
    db.add(base1); db.add(base2); db.flush()
    pair1 = Sentence(call_id=call_id, korean_sentence="고마워요", native_sentence="y", locale="en",
                      source_type="asked", is_bookmarked=False, kind="native",
                      paired_sentence_id=base1.sentence_id, nuance="캐주얼함", evaluation=Evaluation())
    db.add(pair1); db.commit()

    detail = CallService(db).get_call(ctx["member_id"], call_id)
    result = CallService(db).get_call_result(ctx["member_id"], call_id)
    detail_ids = [s.sentence_id for s in detail.sentences]
    result_ids = [s.sentence_id for s in result.sentences]
    assert detail_ids == [base1.sentence_id, pair1.sentence_id, base2.sentence_id], \
        "물리 순서([기본1·기본2·짝1])를 그대로 내보냈다 — order_sentences_with_pairs 미적용"
    assert detail_ids == result_ids, "/calls/{id} 와 /result 의 순서가 어긋난다"


def test_get_call_base_sentence_json_omits_the_three_keys_entirely(ctx):
    db, call_id = ctx["db"], ctx["call_id"]
    base = Sentence(call_id=call_id, korean_sentence="안녕", locale="en", source_type="asked",
                     is_bookmarked=False, evaluation=Evaluation())
    db.add(base); db.commit()

    detail = CallService(db).get_call(ctx["member_id"], call_id)
    dumped = detail.sentences[0].model_dump()
    assert "kind" not in dumped
    assert "paired_sentence_id" not in dumped
    assert "nuance" not in dumped


def test_get_call_pair_sentence_json_carries_kind_paired_id_and_nuance(ctx):
    db, call_id = ctx["db"], ctx["call_id"]
    base, pair = _add_pair(db, call_id, korean="감사합니다", native="고마워요")
    db.commit()

    detail = CallService(db).get_call(ctx["member_id"], call_id)
    by_id = {s.sentence_id: s.model_dump() for s in detail.sentences}
    assert by_id[pair.sentence_id]["kind"] == "native"
    assert by_id[pair.sentence_id]["paired_sentence_id"] == base.sentence_id
    assert by_id[pair.sentence_id]["nuance"] == "캐주얼함"


def test_get_call_without_any_pairs_is_unchanged(ctx):
    """짝 없는 통화(지금 운영 상태) 회귀 — 기본 문장만 있으면 순서·필드가 종전과 같다."""
    db, call_id = ctx["db"], ctx["call_id"]
    s1 = Sentence(call_id=call_id, korean_sentence="하나", locale="en", source_type="asked",
                  is_bookmarked=False, evaluation=Evaluation())
    s2 = Sentence(call_id=call_id, korean_sentence="둘", locale="en", source_type="asked",
                  is_bookmarked=False, evaluation=Evaluation())
    db.add(s1); db.add(s2); db.commit()

    detail = CallService(db).get_call(ctx["member_id"], call_id)
    assert [s.sentence_id for s in detail.sentences] == [s1.sentence_id, s2.sentence_id]
    for s in detail.sentences:
        dumped = s.model_dump()
        assert "kind" not in dumped and "paired_sentence_id" not in dumped and "nuance" not in dumped


# --------------------------------------------------------------------------- #
# ⑤ 북마크 목록 — kind·nuance 만, paired_sentence_id 없음, 순서는 종전
# --------------------------------------------------------------------------- #
def test_bookmark_list_exposes_kind_and_nuance_but_not_paired_sentence_id(ctx):
    db, call_id = ctx["db"], ctx["call_id"]
    base, pair = _add_pair(db, call_id, korean="감사합니다", native="고마워요")
    base.is_bookmarked = True
    pair.is_bookmarked = True
    db.commit()

    out = SentenceService(db).list_bookmarks(ctx["member_id"])
    by_id = {o.sentence_id: o for o in out}
    # ⛔⛔ R5-b(2026-09-24, bt-back) — `paired_sentence_id` 를 `SentenceOut`(이 목록이
    #   쓰는 스키마)에 넣지 않기로 한 결정 그대로다 — 통화 상세 전용 `CallDetail
    #   SentenceOut` 으로 분리했다(schemas/call.py 참조). 이 스키마엔 그 필드
    #   자체가 없어야 한다.
    assert "paired_sentence_id" not in type(out[0]).model_fields

    pair_dumped = by_id[pair.sentence_id].model_dump()
    assert pair_dumped["kind"] == "native"
    assert pair_dumped["nuance"] == "캐주얼함"

    base_dumped = by_id[base.sentence_id].model_dump()
    assert "kind" not in base_dumped
    assert "nuance" not in base_dumped


def test_bookmark_list_order_is_unchanged_by_pairing(ctx):
    """⛔ 북마크는 통화 무관 목록이라 order_sentences_with_pairs 를 타지 않는다 —
    기존 정렬(sentence_id 내림차순)이 그대로다."""
    db, call_id = ctx["db"], ctx["call_id"]
    base, pair = _add_pair(db, call_id, korean="감사합니다", native="고마워요")
    base.is_bookmarked = True
    pair.is_bookmarked = True
    db.commit()

    out = SentenceService(db).list_bookmarks(ctx["member_id"])
    assert [o.sentence_id for o in out] == [pair.sentence_id, base.sentence_id]  # desc, 종전 그대로
