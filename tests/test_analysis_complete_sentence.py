"""A6(2026-10-06 · PM-DEC-398 · 보강 PM-DEC-402) — 발음 평가용 문장은 한국어 완성 문장만 저장한다.

결함 ①: 분석 LLM 이 [검출 후보] 표의 「항목」 열(문법 = 문형 표기 `V-(으)면서`)을 korean 에 그대로 옮겼다.
결함 ②: `제 이름은 John이에요` 처럼 한국어 외 문자가 섞인 문장은 발음 평가가 불가능하다.

판정(한국어 통화): 한글·공백·문장부호만 + 문형 표기 아님 + 끝이 종결 어미 글자 또는 문장부호(.?!…)
                    · 「~기」 명사형 끝은 미완성.

시험 목록:
    ① 지시문 — 완성 문장 · 문형 표기·조각·「~하기」 금지 · 예문 열 · 한글 표기(존·세 시) · 한국어 통화에만
    ② 판정 — 표기·조각·외국 문자는 잡고, 정상 문장 다수는 그대로 통과(과잉 제외 없음)
    ③ 치환 — 표기·조각 → 항목 예문(짝은 비움 · translation 유지)
    ④ 제외 — 대체 예문 없음 · 예문도 통과 못 함
    ⑤ 현지인 짝 — 한국어 외 문자·조각이면 짝만 비우고 기본 표현은 남긴다
    ⑥ 다른 학습 언어 — 한국어 규칙을 적용하지 않는다(문형 표기만)
    ⑦ 예문 사전 — 후보 먼저(문법 우선) · 후보 밖은 learning_item · 걸린 게 없으면 조회 안 함
    ⑧ analyze_call 통합 — 저장된 Sentence 에 발음 불가 문장이 남지 않는다
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import settings
from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.sentence import Sentence
from domains.learning.service import normalcall_service as svc
from domains.learning.service.normalcall_service import (
    LearnedExpression,
    _analysis_instruction,
    _complete_sentence_expressions,
    _is_pattern_notation,
    _is_speakable,
    _lookup_item_examples,
    _needs_sentence_guard,
)


def _expr(korean: str, **kw) -> LearnedExpression:
    return LearnedExpression(korean=korean, translation=kw.pop("translation", "t"), source_type="drilled", **kw)


# ① 지시문 ------------------------------------------------------------------- #
def test_instruction_requires_complete_sentences():
    instruction = _analysis_instruction("en", "한국어")
    for phrase in ("korean 과 native_expression 은 항상", "완성 문장", "문형 표기",
                   "단어 하나만 있는 조각", "'~하기'", "예문 열"):
        assert phrase in instruction


def test_instruction_requires_hangul_only_for_korean():
    instruction = _analysis_instruction("en", "한국어")
    assert "John → 존" in instruction
    assert "3시 → 세 시" in instruction
    assert "라틴 문자·숫자·한자·가나" in instruction


def test_hangul_rule_is_not_added_for_other_target_languages():
    assert "John → 존" not in _analysis_instruction("en", "일본어")


# ② 판정 --------------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "V-(으)면서", "A-(으)면서(도)", "N이/가 아닙니다", "N은/는 N이에요/예요", "V-아/어요", "-고 싶다",
    "제 이름은 John이에요", "3시에 만나요", "OK 알겠어요", "日本語 공부해요", "ㅋㅋ 웃겨요",
    "물", "대박", "공부하기", "커피 마시면서", "공부하기.",
])
def test_unspeakable_texts_are_caught(text):
    assert not _is_speakable(text, "ko")


NORMAL_SENTENCES = [
    "커피 마시면서 얘기해요.", "음악을 들으면서 공부해요.", "제 이름은 존이에요.", "세 시에 만나요",
    "안녕하세요", "감사합니다", "죄송합니다", "이거 주세요", "얼마예요?", "화장실이 어디예요?", "네", "아니요",
    "괜찮아요", "도와주세요", "잘 지냈어요?", "또 봐요", "처음 뵙겠습니다", "배고파요", "뱃가죽이 등에 붙을 것 같아요",
    "진짜 대박이다", "가자", "맛있겠다", "그래", "알겠어", "좋아", "몰라", "어떡해", "고마워", "미안해", "내일 봐",
    "빨리 와", "배불러", "졸려", "뭐라고", "그런가", "잘 먹겠습니다", "물론이죠", "그렇구나", "할게", "갈래",
    "먹을까?", "진짜?", "대박!", "티비를 보면서 밥을 먹어요.", "씨씨티비가 있어요.", "잘 부탁드립니다",
]


@pytest.mark.parametrize("text", NORMAL_SENTENCES)
def test_normal_sentences_are_not_over_excluded(text):
    assert _is_speakable(text, "ko")


def test_pattern_notation_detector_kept():
    assert _is_pattern_notation("V-(으)면서") and not _is_pattern_notation("커피 마시면서 얘기해요.")


# ③ 치환 --------------------------------------------------------------------- #
def test_pattern_is_replaced_with_the_item_example_and_pair_is_cleared():
    e = _expr("V-(으)면서", translation="while doing",
              native_expression="x", native_expression_translation="y", native_nuance="z")
    out = _complete_sentence_expressions([e], {"V-(으)면서": "음악을 들으면서 공부해요."})
    assert [x.korean for x in out] == ["음악을 들으면서 공부해요."]
    assert out[0].translation == "while doing"
    assert (out[0].native_expression, out[0].native_expression_translation, out[0].native_nuance) == (None, None, None)


def test_word_fragment_is_replaced_with_its_example():
    out = _complete_sentence_expressions([_expr("물")], {"물": "물이 있어요."})
    assert [x.korean for x in out] == ["물이 있어요."]


# ④ 제외 --------------------------------------------------------------------- #
@pytest.mark.parametrize("text", ["N이/가 아닙니다", "제 이름은 John이에요", "3시에 만나요", "공부하기"])
def test_unspeakable_without_example_is_dropped(text):
    assert _complete_sentence_expressions([_expr(text)], {}) == []


def test_example_that_is_itself_unspeakable_is_dropped():
    assert _complete_sentence_expressions([_expr("V-(으)면서")], {"V-(으)면서": "V-(으)면서"}) == []
    assert _complete_sentence_expressions([_expr("물")], {"물": "Water 있어요"}) == []


# ⑤ 정상 · 현지인 짝 ---------------------------------------------------------- #
def test_normal_sentences_pass_through_untouched_even_if_they_match_a_surface():
    exprs = [_expr(t, native_expression="뱃가죽이 등에 붙을 것 같아요") for t in NORMAL_SENTENCES]
    examples = {"안녕하세요": "안녕하세요, 저는 학생이에요."}  # 청크 surface 와 같아도 바꾸지 않는다
    out = _complete_sentence_expressions(exprs, examples)
    assert [e.korean for e in out] == NORMAL_SENTENCES
    assert all(e.native_expression == "뱃가죽이 등에 붙을 것 같아요" for e in out)


@pytest.mark.parametrize("native", ["Hungry 해요", "배고파 3초 전", "꼬르륵", "空腹이에요"])
def test_unspeakable_native_pair_is_cleared_but_base_kept(native):
    e = _expr("배고파요", native_expression=native, native_expression_translation="tr", native_nuance="nu")
    out = _complete_sentence_expressions([e], {})
    assert [x.korean for x in out] == ["배고파요"]
    assert (out[0].native_expression, out[0].native_expression_translation, out[0].native_nuance) == (None, None, None)


# ⑥ 다른 학습 언어 ------------------------------------------------------------ #
def test_other_languages_only_reject_pattern_notation():
    assert _is_speakable("私はジョンです。", "ja")
    assert _is_speakable("I'm John.", "en")
    assert not _is_speakable("V-(으)면서", "ja")
    out = _complete_sentence_expressions([_expr("私はジョンです。")], {}, language="ja")
    assert [e.korean for e in out] == ["私はジョンです。"]


# ⑦ 예문 사전 --------------------------------------------------------------- #
class _FakeDB:
    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    def scalars(self, _stmt):
        self.calls += 1
        return SimpleNamespace(all=lambda: self.rows)


def test_lookup_uses_candidates_first_grammar_preferred_and_db_for_the_rest():
    rows = [
        SimpleNamespace(surface="N이/가 아닙니다", kind="grammar",
                        examples=json.dumps(["저는 학생이 아닙니다."]), gen_examples=None),
    ]
    db = _FakeDB(rows)
    cands = [
        {"item_id": 2, "kind": "vocab", "surface": "물", "example": "물이 있어요."},
        {"item_id": 1, "kind": "grammar", "surface": "V-(으)면서", "example": "음악을 들으면서 공부해요."},
    ]
    exprs = [_expr("V-(으)면서"), _expr("N이/가 아닙니다"), _expr("물"), _expr("배고파요")]
    got = _lookup_item_examples(db, exprs, cands, "ko")
    assert got["V-(으)면서"] == "음악을 들으면서 공부해요."
    assert got["물"] == "물이 있어요."
    assert got["N이/가 아닙니다"] == "저는 학생이 아닙니다."
    assert db.calls == 1


def test_lookup_skips_the_db_when_everything_is_covered():
    db = _FakeDB([])
    cands = [{"item_id": 1, "kind": "grammar", "surface": "V-(으)면서", "example": "e"}]
    _lookup_item_examples(db, [_expr("V-(으)면서"), _expr("배고파요")], cands, "ko")
    assert db.calls == 0


def test_guard_is_skipped_when_everything_is_speakable():
    assert not _needs_sentence_guard([_expr(t) for t in NORMAL_SENTENCES], "ko")
    assert _needs_sentence_guard([_expr("배고파요", native_expression="Hungry")], "ko")


# ⑧ analyze_call 통합 -------------------------------------------------------- #
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
def call_ctx(session_factory):
    db = session_factory()
    voice = Voice(name="V", gender="male"); db.add(voice); db.flush()
    ch = Character(name="바바", role="선생님", personality="시크", voice_id=voice.voice_id, price=0)
    db.add(ch); db.flush()
    m = Member(language="en", korean_level=3, onboarding_completed=True, auth_user_id="auth-a6")
    db.add(m); db.flush()
    c = Call(member_id=m.member_id, character_id=ch.character_id, status="analyzing", call_type="chat")
    db.add(c); db.flush()
    db.add(CallRawData(call_id=c.call_id, turn_index=1, role="BEAVER", content="커피 마시면서 얘기해요."))
    db.add(CallRawData(call_id=c.call_id, turn_index=2, role="USER", content="커피 마시면서 얘기해요."))
    db.commit()
    return SimpleNamespace(call_id=c.call_id, member_id=m.member_id)


def test_analyze_call_never_saves_unspeakable_sentences(session_factory, call_ctx, monkeypatch):
    async def fake_generate(*_a, **kw):
        schema = kw["schema"]
        return schema(
            summary="커피", detected_mode="study", feedback="잘했어요.",
            expressions=[
                _expr("V-(으)면서", translation="while doing"),                 # 후보 예문으로 치환
                _expr("N이/가 아닙니다", translation="is not N"),                # 대체 예문 없음 → 제외
                _expr("제 이름은 John이에요", translation="My name is John."),   # 한국어 외 문자 → 제외
                _expr("커피 마시면서 얘기해요.", translation="Let's talk over coffee.",
                      native_expression="Coffee 때리면서 수다 떨어요"),           # 기본은 남고 짝만 비움
            ],
            **({"detections": []} if "detections" in schema.model_fields else {}),
        )

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", fake_generate)
    monkeypatch.setattr(svc.tts, "synthesize", lambda *a, **k: None, raising=False)
    cands = [{"item_id": 1, "kind": "grammar", "surface": "V-(으)면서",
              "example": "음악을 들으면서 공부해요.", "injected": True}]
    asyncio.run(svc.analyze_call(
        call_ctx.call_id, None, settings, session_factory,
        locale="en", member_id=call_ctx.member_id, candidates=cands,
    ))
    db = session_factory()
    rows = db.query(Sentence).filter(Sentence.call_id == call_ctx.call_id).all()
    saved = sorted(s.korean_sentence for s in rows)
    assert saved == ["음악을 들으면서 공부해요.", "커피 마시면서 얘기해요."]
    assert all(_is_speakable(s, "ko") for s in saved)
    assert not any(s.kind == "native" for s in rows)
    assert db.get(Call, call_ctx.call_id).status == "done"
