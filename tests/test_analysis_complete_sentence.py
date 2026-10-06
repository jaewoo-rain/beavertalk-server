"""A6(2026-10-06 · PM-DEC-398) — 「새로 배운 표현」에 문형 표기가 저장되지 않는다.

결함: 분석 LLM 이 [검출 후보] 표의 「항목」 열(문법 = 문형 표기 `V-(으)면서`)을 korean 에 그대로
옮기면, 그 문자열이 Sentence.korean_sentence → 발음 평가 기준 문장이 되어 평가가 불가능했다.

시험 목록:
    ① 지시문에 「완성 문장」·「문형 표기 금지」·「예문 열」 규칙이 실린다
    ② 표기 판정 — 표기는 잡고 정상 문장은 놓아 준다
    ③ 표기 → 예문 치환(짝은 비운다 · translation 은 그대로)
    ④ 예문 없음 → 저장 제외
    ⑤ 정상 문장은 그대로(어휘 surface 와 같아도 바꾸지 않는다)
    ⑥ 후보 밖 표기는 learning_item 문법 행에서 예문을 찾는다
    ⑦ analyze_call 통합 — 저장된 Sentence 에 표기가 남지 않는다(치환 1 · 제외 1 · 정상 1)
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
    _lookup_grammar_examples,
)


def _expr(korean: str, **kw) -> LearnedExpression:
    return LearnedExpression(korean=korean, translation=kw.pop("translation", "t"), source_type="drilled", **kw)


# ① 지시문 ------------------------------------------------------------------- #
def test_instruction_requires_complete_sentences_not_pattern_notation():
    instruction = _analysis_instruction("en", "한국어")
    assert "완성 문장" in instruction
    assert "문형 표기" in instruction
    assert "예문 열" in instruction


# ② 표기 판정 ---------------------------------------------------------------- #
@pytest.mark.parametrize("text", [
    "V-(으)면서", "A-(으)면서(도)", "N이/가 아닙니다", "N은/는 N이에요/예요", "V-아/어요",
    "A/V-(으)ㄹ 때", "V-(스)ㅂ니다",
])
def test_pattern_notation_is_detected(text):
    assert _is_pattern_notation(text)


@pytest.mark.parametrize("text", [
    "커피 마시면서 얘기해요.", "음악을 들으면서 공부해요.", "TV를 보면서 밥을 먹어요.",
    "물이 있어요.", "배고파요", "CCTV가 있어요.",
])
def test_complete_sentence_is_not_flagged(text):
    assert not _is_pattern_notation(text)


# ③ 치환 --------------------------------------------------------------------- #
def test_pattern_is_replaced_with_the_item_example_and_pair_is_cleared():
    e = _expr("V-(으)면서", translation="while doing",
              native_expression="x", native_expression_translation="y", native_nuance="z")
    out = _complete_sentence_expressions([e], {"V-(으)면서": "음악을 들으면서 공부해요."})
    assert [x.korean for x in out] == ["음악을 들으면서 공부해요."]
    assert out[0].translation == "while doing"
    assert (out[0].native_expression, out[0].native_expression_translation, out[0].native_nuance) == (None, None, None)


# ④ 예문 없음 → 제외 -------------------------------------------------------- #
def test_pattern_without_example_is_dropped():
    out = _complete_sentence_expressions([_expr("N이/가 아닙니다")], {})
    assert out == []


def test_pattern_whose_example_is_itself_notation_is_dropped():
    out = _complete_sentence_expressions([_expr("V-(으)면서")], {"V-(으)면서": "V-(으)면서"})
    assert out == []


# ⑤ 정상 문장은 그대로 -------------------------------------------------------- #
def test_normal_sentences_pass_through_untouched():
    a = _expr("커피 마시면서 얘기해요.", native_expression="커피 한잔 때리면서 수다 떨어요")
    b = _expr("물")  # 어휘 표현 — 어휘 surface 와 같아도 바꾸지 않는다(grammar_examples 에 없다)
    out = _complete_sentence_expressions([a, b], {"V-(으)면서": "음악을 들으면서 공부해요."})
    assert out == [a, b]
    assert a.native_expression == "커피 한잔 때리면서 수다 떨어요"


def test_grammar_candidate_surface_match_is_replaced_even_without_latin_marker():
    # 표기 패턴이 아니어도 문법 후보 surface 와 정확히 같으면 대상이다
    out = _complete_sentence_expressions([_expr("-고 싶다")], {"-고 싶다": "커피를 마시고 싶어요."})
    assert [x.korean for x in out] == ["커피를 마시고 싶어요."]


# ⑥ 사전 구성 --------------------------------------------------------------- #
class _FakeDB:
    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    def scalars(self, _stmt):
        self.calls += 1
        return SimpleNamespace(all=lambda: self.rows)


def test_lookup_uses_grammar_candidates_first_and_db_for_the_rest():
    row = SimpleNamespace(surface="N이/가 아닙니다", examples=json.dumps(["저는 학생이 아닙니다."]), gen_examples=None)
    db = _FakeDB([row])
    cands = [
        {"item_id": 1, "kind": "grammar", "surface": "V-(으)면서", "example": "음악을 들으면서 공부해요."},
        {"item_id": 2, "kind": "vocab", "surface": "물", "example": "물이 있어요."},
    ]
    got = _lookup_grammar_examples(db, [_expr("V-(으)면서"), _expr("N이/가 아닙니다"), _expr("물")], cands, "ko")
    assert got == {"V-(으)면서": "음악을 들으면서 공부해요.", "N이/가 아닙니다": "저는 학생이 아닙니다."}
    assert db.calls == 1


def test_lookup_skips_the_db_when_every_pattern_is_a_candidate():
    db = _FakeDB([])
    cands = [{"item_id": 1, "kind": "grammar", "surface": "V-(으)면서", "example": "e"}]
    _lookup_grammar_examples(db, [_expr("V-(으)면서"), _expr("배고파요")], cands, "ko")
    assert db.calls == 0


# ⑦ analyze_call 통합 -------------------------------------------------------- #
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


def test_analyze_call_never_saves_pattern_notation(session_factory, call_ctx, monkeypatch):
    async def fake_generate(*_a, **kw):
        schema = kw["schema"]
        return schema(
            summary="커피", detected_mode="study", feedback="잘했어요.",
            expressions=[
                _expr("V-(으)면서", translation="while doing"),          # 후보 예문으로 치환
                _expr("N이/가 아닙니다", translation="is not N"),         # 후보·DB 둘 다 없음 → 제외
                _expr("커피 마시면서 얘기해요.", translation="Let's talk over coffee."),  # 그대로
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
    saved = sorted(s.korean_sentence for s in db.query(Sentence).filter(Sentence.call_id == call_ctx.call_id))
    assert saved == ["음악을 들으면서 공부해요.", "커피 마시면서 얘기해요."]
    assert not any(_is_pattern_notation(s) for s in saved)
    assert db.get(Call, call_ctx.call_id).status == "done"
