"""기억 14차 — call flow 13차(00282) 실제 추출 결과(fixtures/memory_round13_real.json · 근거 로그에서 통화별 마지막 추출)로
심기 1통 → 잡담 3통 뒤 심은 값이 남는가. 13차는 «가장 오래 언급 안 된 것부터» 축출이라 한 번만 말한 심은 값이 먼저 밀렸다
(max10 볼더링 · max05 하린)."""
import json
from pathlib import Path

from domains.learning.service import chat_memory_service as cms

FX = json.loads((Path(__file__).parent / "fixtures" / "memory_round13_real.json").read_text(encoding="utf-8"))
# 심은 값 6개 중 그 계정 심기 통화 추출에 실제로 들어온 것(추출기가 못 뽑은 값은 병합이 살릴 수 없다)
SEQ = {
    "max10": (["2276"], ["2278", "2280", "2282"], ["하린", "광주", "보통", "볼더링", "밤양갱", "복숭아"]),
    "max05": (["2277"], ["2279", "2281", "2283"], ["광주", "보통", "볼더링", "밤양갱"]),
}
MID_FRAGMENT = {"2277": ["수영 코치 이름: 하린"]}   # 2277 조각 1 중간 병합에만 있던 줄(조각 2 최종 추출은 근거 불일치로 버림)


def _call(cid):
    return cms.demote_details(MID_FRAGMENT.get(cid, []) + FX[cid]["facts"])


def test_planted_values_survive_three_chat_calls():
    for who, (plant, chats, values) in SEQ.items():
        facts: list[str] = []
        for cid in plant + chats:
            facts = cms._merge_facts(facts, _call(cid), cms.FACTS_CAP)
        durable = " ".join(f for f in facts if cms.EPHEMERAL_SEP not in f)
        if who == "max05":
            values = values + ["하린"]
        missing = [v for v in values if v not in durable]
        assert not missing, f"{who}: 심은 값 빠짐 {missing} · {facts}"
        assert len(facts) <= cms.FACTS_CAP


def test_old_attribute_fact_not_evicted_by_new_chatter():
    old = [f"좋아하는 것{i}: 값{i}" for i in range(cms.FACTS_CAP)]          # 속성다운 주제로 칸이 다 참
    new = ["요즘 근황: 한가함", "주로 하는 일: 영화 봄", "친구와 대화: 고민 상담"]   # 비속성 잡담
    got = cms._merge_facts(old, new, cms.FACTS_CAP)
    assert all(f in got for f in old)


def test_replaced_does_not_delete_keyed_fact():
    """13차 2278: 추출기가 바꾸지도 않은 «주말 취미: 볼더링» 을 replaced 로 내서 지워졌다."""
    got = cms._merge_facts(["주말 취미: 볼더링", "매운 음식을 잘 못 먹음"], ["요즘 근황: 한가함"], cms.FACTS_CAP,
                           replaced=["주말 취미: 볼더링", "매운 음식을 잘 못 먹음"])
    assert "주말 취미: 볼더링" in got and "매운 음식을 잘 못 먹음" not in got


def test_non_attribute_new_durable_capped_at_three():
    lines = [f"잡담{i}: 값{i}" for i in range(6)] + ["좋아하는 노래: 밤양갱", "강아지: 보통이"]
    got = cms.demote_details(lines)
    assert sum(1 for f in got if cms.EPHEMERAL_SEP not in f and f.startswith("잡담")) == 3
    assert "좋아하는 노래: 밤양갱" in got and "강아지: 보통이" in got
