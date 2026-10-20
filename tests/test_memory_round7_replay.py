"""기억 8차 — call flow 7차(max08 2235 · max06 2236) 통화 사이 불합격을 로컬에서 재생한다.

입력: 7차 직전 chat_memory 행(사실·요약) + 실제 통화 전사(tests/fixtures/memory_round7.json).
재생: 끝 말 받아쓰기 → 추출(고정 응답) → 15칸 자르기 → 재압축(최악 — 옛 문장을 하나도 안 지우는 가짜) → [기억] 블록.
⚠ LLM 두 단계(추출·재압축)는 로컬 키가 없어 고정 응답이다. 추출 응답은 전사에 실제로 나온 말만 옮겼다.
   재압축은 «옛 요약 + 새 요약을 그대로 붙이는» 최악의 모델로 둔다 — 옛 값 제거가 LLM 에 기대지 않는지 본다.
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
from domains.learning.realtime import call_session as cs
from domains.learning.service import chat_memory_service as cms
from domains.learning.service import normalcall_service as svc

FX = json.loads((Path(__file__).parent / "fixtures" / "memory_round7.json").read_text(encoding="utf-8"))

# 추출기 고정 응답 — 전사에 학습자가 실제로 한 말만(2235·2236 전사 줄 번호) + 끝 말(생일).
EXTRACT = {
    "max08": {"topic": "룸메이트와 경주 여행",
              "learner_facts": ["룸메이트 이름은 하은", "하은과 같이 산 지 1년", "하은이 방 청소를 해 줌", "하은이 나를 귀여워함",
                                "하은과 같이 요리함", "강릉 여행을 미룸", "다음 달에 경주에 감", "햄스터 이름을 망고로 바꿈",
                                "버섯은 이제 먹음", "가지를 못 먹음"],
              "pending": "", "interests": ["여행", "요리"], "replaced": ["햄스터 이름이 호두임", "버섯을 못 먹음"],
              "profile": {"생일": "12월 5일", "다음 여행": "다음 달 경주", "못 먹는 것": "가지",
                          "반려동물": "고양이 나비, 햄스터 망고"}},
    "max06": {"topic": "룸메이트와 경주·제주 여행",
              "learner_facts": ["친구와 PC방에서 카트라이더를 함", "룸메이트 이름은 하은", "하은과 같이 산 지 1년",
                                "하은과 요리하고 밥 먹음", "강릉 여행을 미룸", "다음 달에 경주에 감", "경주에서 황남빵을 먹을 예정",
                                "햄스터 이름을 망고로 바꿈", "가지와 굴을 못 먹음", "제주도에서 한라산에 갈 예정"],
              "pending": "", "interests": ["게임", "여행"], "replaced": [],      # 최악: 추출기가 replaced 를 못 잡음
              "profile": {"생일": "12월 5일", "다음 여행": "다음 달 경주", "못 먹는 것": "가지, 굴",
                          "반려동물": "고양이 나비, 햄스터 망고"}},
}
# 다음 통화 첫 인사·질문에서 나오면 불합격인 옛 값(7차 2237·2238 실측 오답 포함)
STALE = {
    "max08": ["3월 7일", "새우", "우유를 마시면", "호두", "다낭", "오사카에 가서", "부산국제영화제", "제주도에 가서", "강릉에 가서"],
    "max06": ["3월 7일", "땅콩", "호두", "다낭", "오사카에 가서", "강릉에 가서"],
}


@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["max08", "max06"])
async def test_last_utterance_recovered_when_client_cuts(who, monkeypatch):
    """끝 말이 전사 없이 끊기면(2235·2236 마지막 학습자 줄이 빈칸) 끝 저장 전에 받아쓴다."""
    c = FX["calls"][who]
    st = cs._CallState()
    st.segments = [{"role": "user" if r["role"] == "user" else "beaver", "text": r["text"], "pcm": b""} for r in c["rows"]]
    assert st.segments[-1]["role"] == "user" and not st.segments[-1]["text"]
    st.segments[-1]["pcm"] = b"\xb8\x0b" * 16000 * 2          # 2초 · 진폭 3000(말소리)

    async def fake_rec(pcm):
        return c["last_learner_line"]

    monkeypatch.setattr(cs, "_recognize_ko", fake_rec)
    await cs._recover_last_user_text(st, c["call_id"])
    assert st.segments[-1]["text"] == c["last_learner_line"]


@pytest.mark.asyncio
async def test_last_utterance_short_noise_is_not_transcribed(monkeypatch):
    st = cs._CallState()
    st.segments = [{"role": "user", "text": "", "pcm": b"\x01\x00" * 1600}]   # 0.1초
    called = []

    async def fake_rec(pcm):
        called.append(1)
        return "x"

    monkeypatch.setattr(cs, "_recognize_ko", fake_rec)
    await cs._recover_last_user_text(st, 1)
    assert called == [] and st.segments[-1]["text"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["max08", "max06"])
async def test_round7_replay_birthday_kept_and_no_stale_values(who, session_factory, monkeypatch):
    c = FX["calls"][who]
    db = session_factory()
    voice = Voice(name="V", gender="male"); db.add(voice); db.flush()
    ch = Character(name="바바", role="선생님", personality="시크", voice_id=voice.voice_id, price=0); db.add(ch); db.flush()
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id=f"auth-{who}"); db.add(m); db.flush()
    prev = svc.create_call(db, m.member_id, ch.character_id, "chat")
    cms.merge_sync(db, m.member_id, "ko", prev, {"facts": c["before"]["facts"]}, c["before"]["summary"])
    call_id = svc.create_call(db, m.member_id, ch.character_id, "chat")
    rows = [dict(r) for r in c["rows"]]
    rows[-1]["text"] = c["last_learner_line"]          # 위 시험이 채운 끝 말
    for i, r in enumerate(rows):
        db.add(CallRawData(call_id=call_id, role="user" if r["role"] == "user" else "beaver", turn_index=i, content=r["text"]))
    db.commit()
    seen = {}

    async def fake_extract(client, model, transcript, old_facts=None):
        seen["tail_has_birthday"] = "12월 5일" in transcript
        out = dict(EXTRACT[who])
        out["replaced"] = cms.match_replaced(out["replaced"], old_facts)
        return out

    async def worst_recompress(client, model, **kw):
        # 옛 문장을 하나도 안 지우는 최악의 재압축 — [옛 요약] + [이번 통화 요약] 을 그대로 붙인다
        p = kw["prompt"]
        old = p.split("[옛 요약]\n", 1)[1].split("\n\n[이번 통화 요약]", 1)[0]
        new = p.split("[이번 통화 요약]\n", 1)[1].split("\n\n[", 1)[0]
        return cms._RecompressOut(summary=(old + " " + new).strip())

    monkeypatch.setattr(svc, "summarize_for_memory", fake_extract)
    monkeypatch.setattr(cms.gemini_analysis, "generate_structured", worst_recompress)
    await svc.extract_and_merge_chat_memory(call_id, m.member_id, "ko", object(), app_settings, session_factory)

    row = cms.load(db, m.member_id, "ko")
    db.refresh(row)
    assert seen["tail_has_birthday"], "끝 말(생일)이 추출 입력에 없다"
    assert row.last_call_id == call_id
    assert len(row.facts) <= cms.FACTS_CAP
    assert row.facts[0] == "생일: 12월 5일"
    assert "다음 여행: 다음 달 경주" in row.facts
    assert any(f.startswith("못 먹는 것: 가지") for f in row.facts)
    assert "반려동물: 고양이 나비, 햄스터 망고" in row.facts
    block = chat_prompt._memory_block(cms.to_dict(row))
    bad = [s for s in STALE[who] if s in block]
    assert bad == [], f"[기억] 블록에 옛 값이 남음: {bad}"
    assert "12월 5일" in block and "경주" in block and "망고" in block


def test_ctx_facts_carry_into_next_fragment(monkeypatch):
    """2235: 조각 2가 빈 사실 목록으로 시작해 조각 1의 «다음 달에 경주에 감» 을 잃고 비버 말(부산국제영화제)로 채웠다."""
    store = {}
    from core import storage
    monkeypatch.setattr(storage, "upload", lambda b, p, data, content_type="": store.__setitem__((b, p), data) or p)
    monkeypatch.setattr(storage, "download", lambda b, p: store.get((b, p)), raising=False)
    monkeypatch.setattr(cs, "_cid", lambda st: 2235)
    one = cs._CallState()
    one.fragment_index = 1
    one.ctx_facts = ["학습자는 다음 달에 경주에 간다", "학습자의 룸메이트 이름은 하은이다"]
    cs._ctx_facts_store(one)
    two = cs._CallState()
    two.fragment_index = 2
    cs._ctx_facts_load(two)
    assert two.ctx_facts == one.ctx_facts
    first = cs._CallState()
    first.fragment_index = 1
    cs._ctx_facts_load(first)          # 첫 조각은 이어받지 않는다(같은 call_id 재사용 없음)
    assert first.ctx_facts == []


@pytest.mark.asyncio
async def test_last_utterance_silence_is_not_transcribed(monkeypatch):
    """2241·2242: 첫 인사 뒤 10초 무음으로 끊긴 통화 — 무음은 받아쓰기를 부르지 않는다(루프 정지 사고의 입력)."""
    st = cs._CallState()
    st.segments = [{"role": "user", "text": "", "pcm": b"\x05\x00" * 16000 * 10}]
    called = []

    async def fake_rec(pcm):
        called.append(1)
        return "x"

    monkeypatch.setattr(cs, "_recognize_ko", fake_rec)
    await cs._recover_last_user_text(st, 2242)
    assert called == []


@pytest.mark.asyncio
async def test_recognize_runs_off_the_event_loop(monkeypatch):
    """동기 받아쓰기가 오래 걸려도 이벤트 루프는 계속 돈다(00254 에서 30초 멈춤)."""
    import asyncio
    import time

    def slow(pcm):
        time.sleep(0.3)
        return "안녕"

    monkeypatch.setattr(cs, "_recognize_ko_sync", slow)
    ticks = []

    async def ticker():
        for _ in range(5):
            ticks.append(1)
            await asyncio.sleep(0.02)

    got, _ = await asyncio.gather(cs._recognize_ko(b"\x00\x00"), ticker())
    assert got == "안녕" and len(ticks) == 5
