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
    assert "학습자에 대해 아는 사실: 앵무새 초코를 키움 · 남동생 이름은 민호" in ins
