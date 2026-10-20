"""기억 6차 — call flow 기억 6차(통화 사이) 남은 원인: 끝 추출 실패 무로그 · 비슷한 사실 중복 · replaced 글자 일치만 인정."""
import pytest

from core.config import settings as app_settings
from domains.learning.service import chat_memory_service as c
from domains.learning.service import normalcall_service as svc

MAX08 = ['고양이 나비를 키움', '어제 친구와 맥주를 마심', '어제 친구와 고양이 이야기를 함', '부산국제영화제에 가고 싶어 함',
         '사촌 이름은 도윤임', '사촌 도윤과 요리를 배움', '비빔밥을 만들고 싶어 함', '햄스터 이름이 호두임', '버섯을 못 먹음',
         '고양이 이름은 나비임', '어제 친구와 영화 이야기를 함', '친구와 부산 바다에 갔음', '친구 수빈과 운동함',
         '친구 수빈과 밥을 먹음', '친구 수빈은 춤을 잘 춤']


def test_near_duplicates_keep_only_newest():
    assert c._near_dup('고양이 나비를 키움', '고양이 이름은 나비임')
    assert c._near_dup('햄스터 이름이 호두임', '햄스터 이름은 호두')
    assert not c._near_dup('사촌 이름은 도윤임', '사촌 도윤과 요리를 배움')
    assert not c._near_dup('친구 수빈과 운동함', '친구 수빈과 밥을 먹음')
    assert not c._near_dup('다음 달에 강릉에 갈 예정', '강릉에서 바다 수영을 할 예정')


def test_max08_birthday_and_trip_survive_the_cap():
    """max08 실제 15칸 + 이번 통화 생일·강릉 → 생일·강릉이 남고 고양이·햄스터는 한 줄씩."""
    new = ['생일은 8월 30일', '다음 달에 강릉에 갈 예정', '햄스터 이름은 호두']
    got = c._merge_facts(MAX08, new, 15)
    assert len(got) <= 15
    assert '생일은 8월 30일' in got and '다음 달에 강릉에 갈 예정' in got and '버섯을 못 먹음' in got
    assert sum('나비' in f for f in got) == 1 and sum('호두' in f for f in got) == 1
    assert got[0] == '생일은 8월 30일'                                   # 최신 순서는 그대로
    assert '사촌 이름은 도윤임' in got


def test_tier_drops_chitchat_before_family_and_single_values():
    old = ['친구 수빈과 운동함', '언니 이름은 서연', '삼겹살을 좋아함']
    got = c._merge_facts(old, ['생일은 8월 30일'], 3)
    assert got == ['생일은 8월 30일', '친구 수빈과 운동함', '언니 이름은 서연']   # 잡담 중 오래된 것(삼겹살)부터 빠짐


def test_replaced_matches_paraphrased_old_fact():
    olds = ['새우를 먹으면 배가 아픔', '다음 주 오사카에 갈 예정', '고양이 나비를 키움']
    assert c.match_replaced(['새우 먹으면 배 아픔', '오사카에 갈 예정'], olds) == ['새우를 먹으면 배가 아픔', '다음 주 오사카에 갈 예정']
    assert c.match_replaced(['고양이 키움'], ['강아지 콩을 키움']) == []
    assert c.match_replaced(['생일은 3월 7일'], ['생일은 3월 7일']) == ['생일은 3월 7일']


@pytest.mark.asyncio
async def test_recompress_always_gets_fact_list(monkeypatch):
    """replaced 가 비어도 합친 사실 목록을 넘겨 요약의 옛 값(새우)을 고치게 한다."""
    seen = {}

    async def fake_gen(client, model, **kw):
        seen["prompt"] = kw["prompt"]
        return c._RecompressOut(summary="합친 요약")

    monkeypatch.setattr(c.gemini_analysis, "generate_structured", fake_gen)
    await c.recompress_summary(object(), "m", "새우를 먹으면 배가 아프다", "새 요약", replaced=[], facts=["버섯을 못 먹음"])
    assert "[지금 사실 목록]" in seen["prompt"] and "버섯을 못 먹음" in seen["prompt"] and "[바뀐 사실]" not in seen["prompt"]


@pytest.mark.asyncio
async def test_end_extraction_retries_then_logs(monkeypatch, caplog):
    """2230 — 통화 끝 요약기 호출이 실패하면 두 번 더 시도하고, 끝내 없으면 경고를 남긴다(예전엔 조용히 return)."""
    calls = []

    async def _summ(client, model, tail, old_facts=None):
        calls.append(1)
        return None

    async def _no_sleep(_s):
        return None

    async def _run_db(_f, fn):
        return "학습자: 안녕" if not calls else []

    monkeypatch.setattr(svc, "summarize_for_memory", _summ)
    monkeypatch.setattr(svc.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(svc, "run_db", _run_db)
    with caplog.at_level("WARNING"):
        await svc.extract_and_merge_chat_memory(1, 1, "ko", object(), app_settings, None)
    assert len(calls) == 3
    assert "요약기 응답 없음" in caplog.text


@pytest.mark.asyncio
async def test_end_extraction_retry_recovers(monkeypatch):
    calls = []
    merged = {}

    async def _summ(client, model, tail, old_facts=None):
        calls.append(1)
        return None if len(calls) == 1 else {"topic": "여행", "learner_facts": ["생일은 8월 30일"], "pending": "",
                                              "interests": [], "replaced": []}

    async def _no_sleep(_s):
        return None

    state = {"n": 0}

    async def _run_db(_f, fn):
        state["n"] += 1
        if state["n"] == 1:
            return "학습자: 생일은 8월 30일이에요"
        if state["n"] == 2:
            return []
        if state["n"] == 3:
            return ""
        merged["done"] = True
        return None

    async def _recompress(*_a, **_k):
        return "요약"

    monkeypatch.setattr(svc, "summarize_for_memory", _summ)
    monkeypatch.setattr(svc.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(svc, "run_db", _run_db)
    monkeypatch.setattr(c, "recompress_summary", _recompress)
    await svc.extract_and_merge_chat_memory(1, 1, "ko", object(), app_settings, None)
    assert len(calls) == 2 and merged.get("done")
