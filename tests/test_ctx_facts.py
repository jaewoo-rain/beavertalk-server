"""PM-DEC-914 — perturn 줄거리 사이드카의 학습자 사실 목록(누적 · 지우지 않음 · 새 세션 지시문에 따로)."""
import pytest

from domains.learning.realtime import call_session as cs


@pytest.mark.asyncio
async def test_facts_accumulate_and_reach_new_session_instruction(monkeypatch):
    st = cs._CallState()
    st.ctx_client = object()
    st.ctx_base = "BASE"
    st.ctx_facts = ["남동생 이름은 민호"]
    st.segments = [{"role": "user", "text": "앵무새 초코를 키워요"}, {"role": "beaver", "text": "와 귀엽다"}]
    seen = {}

    async def fake(client, model, **kw):
        seen["prompt"] = kw["prompt"]
        return cs.CtxSummaryOut(summary="반려동물 얘기", facts=["앵무새 초코를 키움"])   # 옛 사실을 빠뜨려도

    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    await cs._ctx_summary_sidecar(st)
    assert "남동생 이름은 민호" in seen["prompt"]
    assert st.ctx_facts == ["앵무새 초코를 키움", "남동생 이름은 민호"]   # 지우지 않는다
    ins = cs._perturn_instruction(st)
    assert "이번 통화에서 학습자가 말한 사실(지난 통화 [기억]과 다르면 이것이 맞다): 앵무새 초코를 키움 · 남동생 이름은 민호" in ins
    assert ins.index("이번 통화에서 학습자가 말한 사실") < ins.index("줄거리:")
    # PM-DEC-919 — 맨 끝([이어서] 바로 앞)에도 다시
    assert ins.index("이번 통화 사실(이게 맞다): 앵무새 초코를 키움") < ins.index("[이어서]")


def test_ctx_echo_tail_is_cut():
    """PM-DEC-916 — 비버가 지시문 형식을 이어 쓰면(call 2134 t53) 저장 전에 그 앞까지만 남긴다."""
    t = "거북이는 어떻게 조심해?\n\n너(비버): …\n학습자: (답변)\n지금 네 표정: neutral\n[이어서] 통화가 진행 중이다."
    assert cs._cut_ctx_echo(t) == "거북이는 어떻게 조심해?"
    assert cs._cut_ctx_echo("그냥 평범한 말이야.") == "그냥 평범한 말이야."


def test_call_token_echo_is_stripped():
    """PM-DEC-918 — call 2194 «…어디서 먹어?call:배가 터질 뻔했다니…» · 맨 앞 «call:괜찮긴…»."""
    assert cs._strip_face_echo("삼겹살은 보통 어디서 먹어?call:배가 터질 뻔했다니") == "삼겹살은 보통 어디서 먹어?배가 터질 뻔했다니"
    assert cs._strip_face_echo("call:괜찮긴 뭐가 괜찮아") == "괜찮긴 뭐가 괜찮아"
    assert cs._strip_face_echo("Do you recall: that day?") == "Do you recall: that day?"


def test_in_call_full_list_keeps_new_fact_appended_at_end():
    """기억 17차 — 00286 2296: 통화 안 목록이 15개로 찬 뒤 사이드카가 «옛 15개 + 좋아하는 계절: 가을» 을 돌려주면
    앞에서 15개를 잘라 «가을» 이 늘 빠졌다(3분 뒤 «아직 못 들었는데»)."""
    from domains.learning.service import chat_memory_service as cms
    old = ["박물관 방문: 친구와 감", "첼로 선생님 이름: 다온", "사는 곳: 수원"] + [f"잡담{i}: 값{i}" for i in range(12)]
    got = cms.cap_in_call_facts(old + ["좋아하는 계절: 가을"], old, 15)
    assert len(got) == 15 and "좋아하는 계절: 가을" in got
    assert "첼로 선생님 이름: 다온" in got and "사는 곳: 수원" in got        # 중요한 옛 사실은 남는다
    assert got[0] == "좋아하는 계절: 가을"                                  # 정말 새 항목이 앞


def test_in_call_same_key_new_value_wins():
    from domains.learning.service import chat_memory_service as cms
    got = cms.cap_in_call_facts(["좋아하는 음료: 커피", "좋아하는 음료: 녹차"], ["좋아하는 음료: 커피"], 15)
    assert got == ["좋아하는 음료: 녹차"]
