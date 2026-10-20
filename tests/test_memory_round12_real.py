"""기억 13차 — 실제 추출기 결과(12차 전사 · fixtures/memory_round12_real.json)로 심기 1통 → 잡담 3통 뒤 지속 값이 남는가."""
import json
from pathlib import Path

from domains.learning.service import chat_memory_service as cms

FX = json.loads((Path(__file__).parent / "fixtures" / "memory_round12_real.json").read_text(encoding="utf-8"))["calls"]
PLANTED = {"2269": ["민재", "세종", "필라테스", "어린 왕자", "땅콩버터"], "2270": ["세종", "두부", "필라테스", "어린 왕자", "땅콩버터"]}
CHAT = {"2269": "2271", "2270": "2272"}


def test_planted_durable_values_survive_three_chat_calls():
    for plant, values in PLANTED.items():
        facts = cms._merge_facts([], FX[plant]["facts"], cms.FACTS_CAP)
        for _ in range(3):                                   # 잡담 통화 3회(같은 잡담 결과를 세 번 — 최악)
            facts = cms._merge_facts(facts, FX[CHAT[plant]]["facts"], cms.FACTS_CAP)
        joined = " ".join(f for f in facts if cms.EPHEMERAL_SEP not in f)
        for v in values:
            if plant == "2270" and v == "두부":
                continue                                     # 2272 잡담이 «반려동물: 고양이 나비» 로 덮어씀(call flow 측정 문장 오염 — 덮어쓰기는 맞다)
            assert v in joined, f"{plant}: 지속 값 {v} 이 잡담 뒤 빠짐 · {facts}"
        assert sum(cms.EPHEMERAL_SEP in f for f in facts) <= cms.EPHEMERAL_MAX
        assert len(facts) <= cms.FACTS_CAP


def test_real_chat_call_is_mostly_ephemeral_after_demotion():
    f = FX["2272"]["facts"]
    eph = [x for x in f if cms.EPHEMERAL_SEP in x]
    assert len(eph) >= len(f) - len(eph)                     # 잡담 통화는 일회가 지속 이상(실측 지속 2 · 일회 4 · 전 9 · 1)
