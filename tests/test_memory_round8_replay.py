"""기억 9차 — call flow 8차(max08 2245 → 2247 · max06 2246) 고정 칸 밖 불합격을 실제 전사로 재생한다(일반화 확인).

재생: 추출(고정 응답 · 근거 quote 는 전사에서 그대로) → 근거 검사 → 주제 키 덮어쓰기 → 재압축(최악 — 옛 문장 그대로) → [기억] 블록.
⚠ 추출·재압축 LLM 은 고정 응답. 근거 검사·덮어쓰기·요약 문장 빼기는 실제 코드가 돈다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import settings as app_settings
from core.prompts import chat as chat_prompt
from db.registry import Base  # noqa: F401
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.service import chat_memory_service as cms
from domains.learning.service import normalcall_service as svc

FX = json.loads((Path(__file__).parent / "fixtures" / "memory_round8.json").read_text(encoding="utf-8"))
F = svc.MemoryFact


def _learner(cid: str, needle: str) -> str:
    """전사의 학습자 줄에서 그대로 베낀 근거(추출기가 해야 할 일)."""
    return next(r["text"] for r in FX["calls"][cid]["rows"] if r["role"] == "user" and needle in r["text"])


def _beaver(cid: str, needle: str) -> str:
    return next(r["text"] for r in FX["calls"][cid]["rows"] if r["role"] != "user" and needle in r["text"])


@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _seed(db, who, before_facts, before_summary):
    voice = Voice(name="V", gender="male"); db.add(voice); db.flush()
    ch = Character(name="바바", role="선생님", personality="시크", voice_id=voice.voice_id, price=0); db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id=f"auth9-{who}"); db.add(m); db.flush()
    prev = svc.create_call(db, m.member_id, ch.character_id, "chat")
    cms.merge_sync(db, m.member_id, "ko", prev, {"facts": before_facts}, before_summary)
    return m, ch


def _add_call(db, m, ch, cid):
    call_id = svc.create_call(db, m.member_id, ch.character_id, "chat")
    for i, r in enumerate(FX["calls"][cid]["rows"]):
        db.add(CallRawData(call_id=call_id, role="user" if r["role"] == "user" else "beaver", turn_index=i, content=r["text"]))
    db.commit()
    return call_id


def _worst_recompress(client, model, **kw):
    p = kw["prompt"]
    old = p.split("[옛 요약]\n", 1)[1].split("\n\n[이번 통화 요약]", 1)[0]
    new = p.split("[이번 통화 요약]\n", 1)[1].split("\n\n[", 1)[0]
    return cms._RecompressOut(summary=(old + " " + new).strip())


def _real_extract_with(items, replaced=()):
    """summarize_for_memory 를 실제로 돌리되 LLM 응답만 고정한다(근거 검사·주제 키 처리는 실제 코드)."""
    async def gen(client, model, **kw):
        if kw.get("schema") is svc.MemoryOut:
            return svc.MemoryOut(topic="일상", facts=items, replaced=list(replaced))
        return await _worst_recompress(client, model, **kw)
    return gen


@pytest.mark.asyncio
async def test_round8_max08_keyed_overwrite_and_no_stale(session_factory, monkeypatch):
    """2245: 요가 → 볼링(같은 통화 안에서 바꿈) · 옛 클라이밍(2244) · 선배 준서 · 아이유 · 다음 통화(2247) [기억]에 옛 값 0."""
    db = session_factory()
    before = ["다음 여행: 다음 달 경주", "반려동물: 고양이 나비, 햄스터 망고", "주말마다 클라이밍을 배움", "룸메이트 이름은 하은"]
    summary = ("주말마다 클라이밍을 즐겨 한다. 룸메이트 하은과 산 지 1년이 되었다. 건망증이 있다. "
               "다음 달에 경주에 가서 불국사에 가고 싶어 한다.")
    m, ch = _seed(db, "max08", before, summary)
    call_id = _add_call(db, m, ch, "2245")
    items = [
        F(key="회사 선배", value="준서, 많이 도와줌", quote=_learner("2245", "준서예요")),
        F(key="좋아하는 가수", value="아이유", quote=_learner("2245", "IU")),
        F(key="다음 여행", value="다음 달 전주", quote=_learner("2245", "전주에 가요")),
        F(key="주말 취미", value="요가", quote=_learner("2245", "요가를 해요")),          # 앞부분 값
        F(key="주말 취미", value="볼링을 배움", quote=_learner("2245", "볼링을 배워요")),  # 같은 통화에서 바꿈
        F(key="반려동물", value="고양이 나비, 햄스터 찹쌀", quote=_learner("2245", "찹쌀이에요")),
        F(key="못 먹는 것", value="양파", quote=_learner("2245", "양파를 못 먹어요")),
        F(key="생일", value="4월 9일", quote=_learner("2245", "사월 구일")),
        F(key="성격", value="건망증이 심함", quote=_beaver("2247", "건망증")),           # 비버 말 — 버려야 한다
    ]
    monkeypatch.setattr(cms.gemini_analysis, "generate_structured", _real_extract_with(items, ["주말마다 클라이밍을 배움"]))
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _real_extract_with(items, ["주말마다 클라이밍을 배움"]))
    await svc.extract_and_merge_chat_memory(call_id, m.member_id, "ko", object(), app_settings, session_factory)
    row = cms.load(db, m.member_id, "ko"); db.refresh(row)
    facts = row.facts
    assert "회사 선배: 준서, 많이 도와줌" in facts and "좋아하는 가수: 아이유" in facts
    assert [f for f in facts if f.startswith("주말 취미")] == ["주말 취미: 볼링을 배움"]   # 같은 통화 안 뒤 값
    assert not any("건망증" in f for f in facts), "비버 말(건망증)이 사실로 들어감"
    block = chat_prompt._memory_block(cms.to_dict(row))
    for stale in ("클라이밍", "경주", "망고"):
        assert stale not in block, f"[기억]에 옛 값 {stale}"
    assert "전주" in block and "찹쌀" in block and "4월 9일" in block


@pytest.mark.asyncio
async def test_same_call_change_keeps_latest_value(session_factory, monkeypatch):
    """추출기가 같은 통화 안의 앞 값(요가)과 뒤 값(볼링)을 순서대로 주면 뒤 값이 남아야 한다 — 추출기는 지금 맞는 값을 주라고 지시받는다."""
    db = session_factory()
    m, ch = _seed(db, "max08b", ["주말 취미: 클라이밍"], "주말마다 클라이밍을 즐겨 한다. 룸메이트 하은과 산다.")
    call_id = _add_call(db, m, ch, "2245")
    items = [F(key="주말 취미", value="볼링을 배움", quote=_learner("2245", "볼링을 배워요"))]
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _real_extract_with(items))
    monkeypatch.setattr(cms.gemini_analysis, "generate_structured", _real_extract_with(items))
    await svc.extract_and_merge_chat_memory(call_id, m.member_id, "ko", object(), app_settings, session_factory)
    row = cms.load(db, m.member_id, "ko"); db.refresh(row)
    assert "주말 취미: 볼링을 배움" in row.facts and "주말 취미: 클라이밍" not in row.facts
    assert "클라이밍" not in row.summary and "하은" in row.summary      # replaced 없이 주제 키만으로 옛 값 문장 제거


@pytest.mark.asyncio
async def test_unseen_topic_overwrite_generalizes(session_factory, monkeypatch):
    """측정 세트에 없던 주제(좋아하는 음료)도 같은 키면 덮어쓰고 옛 값 문장을 뺀다(과적합 확인)."""
    db = session_factory()
    m, ch = _seed(db, "gen", ["좋아하는 음료: 녹차"], "아침마다 녹차를 마신다. 고양이를 키운다.")
    call_id = _add_call(db, m, ch, "2246")
    rows = FX["calls"]["2246"]["rows"]
    quote = next(r["text"] for r in rows if r["role"] == "user" and r["text"].strip())   # 아무 학습자 줄(근거 통과용)
    items = [F(key="좋아하는 음료", value="커피", quote=quote)]
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _real_extract_with(items))
    monkeypatch.setattr(cms.gemini_analysis, "generate_structured", _real_extract_with(items))
    await svc.extract_and_merge_chat_memory(call_id, m.member_id, "ko", object(), app_settings, session_factory)
    row = cms.load(db, m.member_id, "ko"); db.refresh(row)
    assert row.facts[0] == "좋아하는 음료: 커피" and "녹차" not in row.summary and "고양이" in row.summary


def test_quote_must_come_from_learner_lines():
    t = "\n".join(f"{'학습자' if r['role'] == 'user' else '비버'}: {r['text']}" for r in FX["calls"]["2247"]["rows"])
    items = [F(key="성격", value="건망증", quote=_beaver("2247", "건망증")),
             F(key="관심 역사", value="한국전쟁", quote=_learner("2247", "한국전쟁")),
             F(key="빈 근거", value="x", quote="")]
    keyed, evidence, dropped = svc._grounded_facts(items, t)
    assert keyed == ["관심 역사: 한국전쟁"]          # 한국전쟁은 학습자가 실제로 말함(2247 t21)
    assert "성격: 건망증" in dropped and "빈 근거: x" in dropped


def test_in_call_facts_overwrite_by_topic():
    """8차 바바 t61: 같은 통화 t40 에 볼링으로 바꿨는데 «요가» 로 답함 — 통화 안 목록도 주제 키로 덮어쓴다."""
    old = ["주말 취미: 요가를 함", "좋아하는 가수: 아이유"]
    got = cms.merge_keyed_lines(["주말 취미: 볼링을 배움"], old, 15)
    assert got == ["주말 취미: 볼링을 배움", "좋아하는 가수: 아이유"]


def test_round9_dependent_fact_of_overwritten_value_is_dropped():
    """9차 2252: «아침 활동: 수영 → 테니스» 뒤에도 «수영 후 기분: 상쾌하고 기분 좋음» 이 남았다."""
    old = ["아침 활동: 수영", "수영 후 기분: 상쾌하고 기분 좋음", "자동차: 빨간색 모닝"]
    new = ["아침 활동: 테니스를 침", "수영 후 기분: 상쾌하고 기분 좋음", "좋아하는 가수: 아이유"]
    got = cms._merge_facts(old, new, 15)
    assert "아침 활동: 테니스를 침" in got and not any("수영" in f for f in got)
    assert "자동차: 빨간색 모닝" in got and "좋아하는 가수: 아이유" in got
    summary = cms.scrub_summary("아침마다 수영을 하고 상쾌해 한다. 빨간색 모닝을 탄다.", cms.profile_of(got),
                                cms.stale_values(old, got), got)
    assert "수영" not in summary and "모닝" in summary


def test_round9_profile_slot_without_quote_is_not_saved():
    """9차 max05: 근거 로그 없는 «반려동물: 없음» — profile 칸은 근거 검사를 안 거쳤다 → 쓰지 않는다."""
    import asyncio

    async def gen(client, model, **kw):
        return svc.MemoryOut(topic="일상", profile=[svc.MemoryProfileItem(key="반려동물", value="없음")],
                             facts=[F(key="아침 활동", value="테니스", quote="요즘 아침에는 테니스를 쳐요")])

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(svc.gemini_analysis, "generate_structured", gen)
    try:
        out = asyncio.run(svc.summarize_for_memory(object(), "m", "학습자: 수영은 그만뒀어요. 요즘 아침에는 테니스를 쳐요.", []))
    finally:
        mp.undo()
    assert out["profile"] == {} and "아침 활동: 테니스" in out["learner_facts"]
    assert not any("반려동물" in f for f in out["learner_facts"])
