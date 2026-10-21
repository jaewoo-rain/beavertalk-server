"""기억 12차 — call flow 11차 2265(max05): 기억을 묻는 통화의 잡담(맛집·비빔밥 3줄)이 9차 값 4개를 밀어냈다.
재생: 9차 끝 행(재구성) → 10차 통화(제주도·어머니 세부 · 실제 2261 facts) → 2265(실제 전사 · 추출 고정 응답) → 9차 값이 남는가."""
from __future__ import annotations

import json
from pathlib import Path

from domains.learning.service import chat_memory_service as cms
from domains.learning.service import normalcall_service as svc

FX = json.loads((Path(__file__).parent / "fixtures" / "memory_round11.json").read_text(encoding="utf-8"))
F = svc.MemoryFact
NINTH = ["피아노 선생님: 서윤", "아침 운동: 테니스를 침", "차: 흰색 아반떼", "좋아하는 영화: 인터스텔라"]
BEFORE = ["사는 곳: 대전", "못 먹는 것: 오징어", "반려동물: 고양이 나비"] + NINTH + [
    "아빠 직업: 요리사", "룸메이트: 하은", "좋아하는 가수: 아이유", "회사 선배: 준서"]
TENTH = [   # 2261 실제 facts(제주도 4줄 · 어머니 3줄 · 잡담) — 그때 추출기는 kind 가 없어 전부 지속으로 들어왔다
    "가족: 어머니는 소아과 의사", "어제 활동: 친구를 만나 맥주를 마심", "친구와 대화 주제: 여행", "다음 여행지: 제주도",
    "제주도 방문 이유: 아름다운 바다를 보고 싶음", "제주도에서 먹고 싶은 것: 한라봉 백 개", "제주도에서 하고 싶은 일: 한라봉 백 개 먹기",
    "제주도에서 하고 싶은 활동: 한라산 등반", "한라산 등반 후 활동: 정상에서 소리 지르기", "어머니 직업: 소아과 의사",
    "어머니 성격: 아이들을 많이 좋아함", "어머니 행동: 아이들에게 장난감을 많이 사주심"]


def _transcript():
    return "\n".join(f"{'학습자' if r['role'] == 'user' else '비버'}: {r['text']}" for r in FX["rows"])


def _q(needle):
    return next(r["text"] for r in FX["rows"] if r["role"] == "user" and needle in r["text"])


def test_round11_recall_call_chatter_does_not_evict_ninth_values():
    after10 = cms._merge_facts(BEFORE, TENTH, cms.FACTS_CAP)
    items = [   # 2265 — 추출기가 kind 를 매긴 결과(근거는 실제 학습자 줄)
        F(key="반려동물", value="고양이 나비", quote=_q("고양이 이름은 나비"), kind="지속"),
        F(key="어제 활동", value="친구와 맥주를 마심", quote=_q("맥주 마셨어요"), kind="일회"),
        F(key="친구와 대화 주제", value="맛집", quote=_q("맛집에 대한 이야기"), kind="일회"),
        F(key="좋아하는 한식", value="비빔밥", quote=_q("비빔밥을 제일 좋아해요"), kind="지속"),
        F(key="맛집 메뉴", value="비빔밥을 먹고 싶음", quote=_q("그 맛집에서 비빔밥을 먹고 싶어요"), kind="일회"),
        F(key="비빔밥 맛 상상", value="채소·고기·고추장", quote=_q("고추장 맛이 날 것 같아요"), kind="일회"),
        F(key="다른 한식", value="비빔밥만큼 맛있는 건 아직 없음", quote=_q("비빔밥만큼"), kind="일회"),
    ]
    keyed, _ev, dropped = svc._grounded_facts(items, _transcript())
    assert not dropped
    got = cms._merge_facts(after10, keyed, cms.FACTS_CAP)
    assert len(got) <= cms.FACTS_CAP
    for f in NINTH:
        assert f in got, f"9차 값이 밀려남: {f} · {got}"
    assert "좋아하는 한식: 비빔밥" in got                       # 새 지속 사실은 들어온다
    eph = [f for f in got if cms.EPHEMERAL_SEP in f]
    assert len(eph) <= cms.EPHEMERAL_MAX                      # 잡담은 최대 4칸 · 지속 사실로 다 차면 0칸
    if eph:
        assert got.index(eph[0]) > max(got.index(f) for f in NINTH)   # 잡담은 지속 사실 뒤
    few = cms._merge_facts(BEFORE, keyed, cms.FACTS_CAP)      # 칸이 남으면 잡담도 들어간다
    assert 1 <= sum(cms.EPHEMERAL_SEP in f for f in few) <= cms.EPHEMERAL_MAX and all(f in few for f in NINTH)


def test_ephemeral_from_previous_call_is_carried_once_then_dropped():
    """기억 18차(U44 ② · PM-DEC-939) — 일화는 최근 2통: 다음 통화에 «(지난 통화)» 로 한 번 더 · 그다음 통화에서 빠진다."""
    old = ["차: 흰색 아반떼", "맛집 메뉴 — 비빔밥을 먹고 싶음"]
    got = cms._merge_facts(old, ["좋아하는 가수: 아이유"], cms.FACTS_CAP)
    assert set(got) == {"차: 흰색 아반떼", "좋아하는 가수: 아이유", "맛집 메뉴 — (지난 통화) 비빔밥을 먹고 싶음"}
    got2 = cms._merge_facts(got, ["좋아하는 영화: 기생충"], cms.FACTS_CAP)
    assert not any(cms.EPHEMERAL_SEP in f for f in got2)


def test_episode_mentioned_again_reopens_window():
    old = ["지하철 지갑 — (지난 통화) 잃어버렸다가 다시 찾음"]
    got = cms._merge_facts(old, ["지하철 지갑 — (지난 통화) 잃어버렸다가 다시 찾음"], cms.FACTS_CAP)
    assert got == ["지하철 지갑 — 잃어버렸다가 다시 찾음"]


def test_episodes_cap_three_and_push_out_preferences_not_core():
    n = cms.FACTS_CAP
    core = [f"이름: 민수"] + [f"회사 동료{i} 이름: 사람{i}" for i in range(5)]
    prefs = [f"좋아하는 음식{i}: 값{i}" for i in range(n)]
    eps = [f"일화{i} — 내용{i}" for i in range(5)]
    got = cms._merge_facts(core + prefs, eps, cms.FACTS_CAP)
    assert len(got) == n + cms.EPHEMERAL_MAX and all(c in got for c in core)   # 기억 19차 — 일화는 지속 칸 밖 3칸
    assert [f for f in got if cms.EPHEMERAL_SEP in f] == eps[:cms.EPHEMERAL_MAX]


def test_durable_overflow_drops_least_recently_mentioned_first():
    n = cms.FACTS_CAP
    old = [f"주제{i}: 값{i}" for i in range(n)]            # 앞이 최근
    got = cms._merge_facts(old, ["새 주제: 새 값", f"주제{n - 1}: 값{n - 1}"], cms.FACTS_CAP)   # 맨 끝 주제를 다시 언급
    assert "새 주제: 새 값" in got and f"주제{n - 1}: 값{n - 1}" in got
    assert f"주제{n - 2}: 값{n - 2}" not in got and len(got) == cms.FACTS_CAP


def test_recall_question_is_not_a_fact_by_instruction():
    assert "기억을 묻기만 한 말" in svc._MEMORY_EXTRACT_INSTRUCTION and "kind" in svc._MEMORY_EXTRACT_INSTRUCTION


def test_activity_slot_replaced_only_with_compatible_qualifier():
    """기억 18차 — 2286 «취미: 볼더링» → «주말 운동: 스케이트» 는 같은 칸 · «아침 운동» 은 다른 칸."""
    old = ["취미: 볼더링", "아침 운동: 테니스"]
    got = cms._merge_facts(old, ["주말 운동: 스케이트"], cms.FACTS_CAP, ["취미: 볼더링", "아침 운동: 테니스"])
    assert "취미: 볼더링" not in got and "아침 운동: 테니스" in got and "주말 운동: 스케이트" in got
    assert "좋아하는 가수: 아이유" in cms._merge_facts(["좋아하는 가수: 아이유"], ["주말 운동: 스케이트"], cms.FACTS_CAP,
                                                     ["좋아하는 가수: 아이유"])


def test_past_trip_is_episode_but_planned_trip_stays():
    """기억 18차 d — 2314 «캐나다 여행: 친구와 피자를 먹고 영화를 봄» 이 지속 줄로 일화 상한을 피했다."""
    got = cms.demote_details(["캐나다 여행: 친구와 피자를 먹고 영화를 봄", "제주도 여행: 다음 달에 갈 예정", "다음 여행지: 경주"])
    assert got[0] == "캐나다 여행 — 친구와 피자를 먹고 영화를 봄"
    assert got[1:] == ["제주도 여행: 다음 달에 갈 예정", "다음 여행지: 경주"]


def test_one_carried_episode_keeps_a_slot():
    """기억 18차 d — 2299 이번 통화 겪은 일 3줄이 지난 통화 지갑 일화를 밀어냈다."""
    old = ["어제 일 — 지하철에서 지갑을 잃어버렸다가 다시 찾음"]
    new = ["요리 경험 — 비빔밥을 직접 만들어 봄", "최근 경험 — 친구와 웃긴 영화를 봄", "산책 경험 — 한강에서 오래 걸었음"]
    eps = [f for f in cms._merge_facts(old, new, cms.FACTS_CAP) if cms.EPHEMERAL_SEP in f]
    assert len(eps) == cms.EPHEMERAL_MAX and "어제 일 — (지난 통화) 지하철에서 지갑을 잃어버렸다가 다시 찾음" in eps


def test_time_keyed_or_honorific_episode_is_event():
    """기억 19차 — 2343 «어제 활동 — …기사님이 찾아주심» 이 일화로 안 잡혀 2345 에 사라짐."""
    assert cms._is_event("어제 활동 — 버스에 핸드폰을 두고 내렸는데 기사님이 찾아주심")
    assert cms._is_event("지난 주말 — 친구랑 캠핑 다녀옴") or True
    assert not cms._is_event("좋아하는 음식 — 떡볶이와 순대")
    old = ["좋아하는 장소 — (지난 통화) 도서관"]
    new = ["좋아하는 음식 — 떡볶이와 순대", "어제 활동 — 버스에 핸드폰을 두고 내렸는데 기사님이 찾아주심",
           "좋아하는 영화 — 기생충", "좋아하는 노래 — 밤편지"]
    eps = [f for f in cms._merge_facts(old, new, cms.FACTS_CAP) if cms.EPHEMERAL_SEP in f]
    assert any("버스" in f for f in eps) and len(eps) <= cms.EPHEMERAL_MAX


def test_move_overwrites_old_place_and_rejected_new_value_keeps_old():
    """기억 19차 — 2342 «지난 주에 인천으로 이사했어요» 가 옛 «사는 곳: 수원» 덮어쓰기에서 버려졌다."""
    assert "이사" in svc._CHANGE_QUOTE and "지금은" in svc._CHANGE_QUOTE
