"""곁가지 원가 계기판 — 사이드카·통화후 분석(LLM)·복습 문장 TTS 의 수집과 원가 반영 (과금 0).

여기서 못박는 것:
  ① `generate_structured(usage=...)` 를 **안 넘기면 종전과 동일**하고, 넘기면 토큰이 쌓인다
  ② ⛔ 출력 토큰(+사고 토큰)이 원가에 **실제로 반영**된다 — in 만 세는 실수 방지
     (출력 단가가 입력의 8배라 빠뜨리면 크게 틀린다)
  ③ ⛔ 하위호환 — 곁가지 키가 없는 **과거 통화**는 원가가 예전 값 그대로다
  ④ 사이드카가 0회면 usage_json 에 키 자체가 안 생긴다(0 과 "안 돌았다"를 구별)
  ⑤ ⛔ R5 — 수집이 망가져도(응답 이형·usage 없음) 호출은 그대로 결과를 돌려준다
  ⑥ 통화후 몫은 **이미 저장된 usage 행에 UPDATE** 로 얹힌다(시점이 달라 유실되던 자리)
  ⑦ ⛔ 복습 문장 TTS 는 **단위가 다르다**(문자 과금) — LLM 키와 섞지 않고, 토큰 과금
     엔진이면 audio_s 없이는 **미상으로 드러낸다**(조용한 0 금지)

⛔ 실 API 를 부르지 않는다 — 페이크 응답으로 계약만 고정한다.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from core import gemini_analysis
from domains.learning.service import normalcall_service as svc


class _Out(BaseModel):
    ok: bool = True


def _um(prompt=0, cand=0, thoughts=0):
    return SimpleNamespace(
        prompt_token_count=prompt, candidates_token_count=cand, thoughts_token_count=thoughts
    )


def _client(response):
    async def _gen(**_kw):
        return response

    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=_gen)))


def _call(client, usage=None):
    return asyncio.run(
        gemini_analysis.generate_structured(
            client, "gemini-2.5-flash",
            system_instruction="s", prompt="p", schema=_Out, usage=usage,
        )
    )


# --------------------------------------------------------------------------- #
# ① 수집기 주입 — 안 넘기면 종전 동일, 넘기면 쌓인다
# --------------------------------------------------------------------------- #
def test_without_a_collector_nothing_changes():
    """⛔ 호출부를 안 고쳐도 되는 것이 이 설계의 전제다 — 인자 없이 부르면 종전 그대로."""
    out = _call(_client(SimpleNamespace(parsed=_Out(), usage_metadata=_um(10, 20))))
    assert isinstance(out, _Out)


def test_a_collector_accumulates_tokens_across_calls():
    client = _client(SimpleNamespace(parsed=_Out(), usage_metadata=_um(100, 30, 7)))
    u = gemini_analysis.LlmUsage()
    _call(client, u)
    _call(client, u)
    assert (u.calls, u.in_text, u.out_text, u.thoughts) == (2, 200, 60, 14)
    assert u.as_dict() == {
        "vendor": "gemini-2.5-flash", "calls": 2,
        "in_text": 200, "out_text": 60, "thoughts": 14,
    }


def test_a_failed_call_is_still_counted():
    """⚠ 응답이 왔는데 파싱만 실패한 콜도 **과금은 끝났다** — 안 세면 구멍이 다시 생긴다."""
    u = gemini_analysis.LlmUsage()
    out = _call(_client(SimpleNamespace(parsed=None, text=None, usage_metadata=_um(50, 5))), u)
    assert out is None
    assert (u.calls, u.in_text, u.out_text) == (1, 50, 5)


def test_a_dead_call_is_marked_but_costs_nothing():
    """호출 자체가 터진 콜은 토큰 0 이지만 **몇 번 터졌는지**는 남는다."""
    async def _boom(**_kw):
        raise RuntimeError("network")

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=_boom)))
    u = gemini_analysis.LlmUsage()
    assert _call(client, u) is None
    assert u.as_dict() == {
        "vendor": "", "calls": 0, "in_text": 0, "out_text": 0, "thoughts": 0, "failures": 1,
    }
    assert svc.estimate_side_cost_usd({"sidecars": u.as_dict()}) == (0.0, [])


def test_collection_never_breaks_the_call(caplog):
    """⛔ R5 — usage_metadata 가 이상해도 결과는 그대로 나온다(계기판이 기능을 죽이면 안 된다)."""
    weird = SimpleNamespace(parsed=_Out(), usage_metadata="not-an-object")
    u = gemini_analysis.LlmUsage()
    assert isinstance(_call(_client(weird), u), _Out)
    assert u.calls == 1 and u.in_text == 0


# --------------------------------------------------------------------------- #
# ②③ 원가 반영 — 출력 토큰이 실제로 실리나 / 과거 통화는 안 변하나
# --------------------------------------------------------------------------- #
def test_output_tokens_actually_move_the_cost():
    """⭐ 출력 단가가 입력의 8배($2.50 vs $0.30) — in 만 세면 크게 틀린다."""
    in_only, _ = svc.estimate_side_cost_usd(
        {"analysis": {"vendor": "gemini-2.5-flash", "in_text": 1_000_000, "out_text": 0}}
    )
    out_only, _ = svc.estimate_side_cost_usd(
        {"analysis": {"vendor": "gemini-2.5-flash", "in_text": 0, "out_text": 1_000_000}}
    )
    assert in_only == pytest.approx(0.30)
    assert out_only == pytest.approx(2.50)
    # 사고 토큰도 출력 단가다(응답 본문에 안 들어오지만 과금된다).
    with_thoughts, _ = svc.estimate_side_cost_usd(
        {"analysis": {"vendor": "gemini-2.5-flash", "out_text": 0, "thoughts": 1_000_000}}
    )
    assert with_thoughts == pytest.approx(2.50)


def test_sidecars_and_analysis_add_on_top_of_both_engines():
    """⚠ 곁가지는 **엔진 무관**이다 — Live 통화든 캐스케이드 통화든 똑같이 더해진다."""
    side = {
        "sidecars": {"vendor": "gemini-2.5-flash", "in_text": 1_000_000, "out_text": 0},
        "analysis": {"vendor": "gemini-2.5-flash", "in_text": 1_000_000, "out_text": 0},
    }
    live_base, _ = svc.estimate_call_cost_usd("live:gemini-native-audio", in_text=1000)
    live_with, unknown = svc.estimate_call_cost_usd(
        "live:gemini-native-audio", in_text=1000, usage_json=side
    )
    assert live_with == pytest.approx(live_base + 0.60)
    assert unknown == []

    casc_base, _ = svc.estimate_call_cost_usd(
        "cascade:stt+llm+tts", usage_json={"vendors": {}}
    )
    casc_with, _ = svc.estimate_call_cost_usd(
        "cascade:stt+llm+tts", usage_json={"vendors": {}, **side}
    )
    assert casc_with == pytest.approx(casc_base + 0.60)


def test_old_calls_without_the_new_keys_cost_exactly_what_they_did():
    """⛔ 하위호환 — 곁가지를 안 재던 통화의 원가는 **한 푼도 안 변한다**."""
    for engine, kwargs in (
        ("live:gemini-native-audio", dict(in_audio=1000, in_text=200, out_audio=3000)),
        (None, dict(in_audio=1000, out_audio=500)),
        ("cascade:stt+llm+tts", dict(usage_json={"vendors": {"llm": {
            "vendor": "gemini-2.5-flash", "in_text": 5000, "out_text": 900}}})),
    ):
        without = svc.estimate_call_cost_usd(engine, **kwargs)
        # usage_json 이 아예 None 이거나, 있어도 곁가지 키가 없으면 결과가 같아야 한다.
        with_empty = svc.estimate_call_cost_usd(
            engine, **{**kwargs, "usage_json": {**(kwargs.get("usage_json") or {})}}
        )
        assert without[0] == pytest.approx(with_empty[0]), engine
        assert without[1] == with_empty[1]


# --------------------------------------------------------------------------- #
# ⑦ 통화후 문장 TTS — 단위가 다르다(문자 vs 토큰)
# --------------------------------------------------------------------------- #
def test_review_sentence_tts_actually_costs_money():
    """⭐ 복습 문장 TTS 가 원가에 **실제로 반영**된다(실측 call 1046: 8문장이 0원이었다).

    이 경로는 Cloud TTS Chirp3-HD(MP3)라 **문자 과금**이다 — $30/1M 자.
    """
    from core import tts as tts_mod

    cost, unknown = svc.estimate_side_cost_usd(
        {"tts": {"vendor": tts_mod.CHIRP3_ENGINE, "calls": 8, "chars": 1_000_000}}
    )
    assert cost == pytest.approx(30.0) and unknown == []
    assert svc.estimate_side_cost_usd(
        {"tts": {"vendor": tts_mod.CHIRP3_ENGINE, "calls": 8, "chars": 400}}
    )[0] > 0


def test_a_token_billed_tts_engine_without_audio_seconds_is_unknown_not_zero():
    """⛔ 토큰 과금 엔진(Gemini-TTS)에 chars 를 쓰면 안 된다 — 못 재면 **드러낸다**.

    말하는 속도에 따라 문자→오디오초가 배로 틀리므로, chars 로 환산하면 그럴듯한
    거짓 숫자가 나온다. 조용한 0 도 금지다(캐스케이드 규율과 같은 자리).
    """
    vendor = next(iter(svc.TTS_TOKEN_PRICE_USD_PER_1M))
    cost, unknown = svc.estimate_side_cost_usd(
        {"tts": {"vendor": vendor, "calls": 3, "chars": 5000}}
    )
    assert cost == 0.0
    assert unknown and unknown[0].startswith(f"tts:{vendor}"), unknown
    # audio_s 가 있으면 정상 계산된다(같은 함수, 같은 규율).
    priced, none_unknown = svc.estimate_side_cost_usd(
        {"tts": {"vendor": vendor, "audio_s": 60}}
    )
    assert priced > 0 and none_unknown == []


def test_tts_rides_on_top_of_the_engine_cost_too():
    """곁가지 TTS 도 engine 분기 **위**에서 더해진다(Live·캐스케이드 공통)."""
    from core import tts as tts_mod

    side = {"tts": {"vendor": tts_mod.CHIRP3_ENGINE, "calls": 1, "chars": 1_000_000}}
    base, _ = svc.estimate_call_cost_usd("live:gemini-native-audio", in_text=1000)
    with_tts, unknown = svc.estimate_call_cost_usd(
        "live:gemini-native-audio", in_text=1000, usage_json=side
    )
    assert with_tts == pytest.approx(base + 30.0) and unknown == []


def test_the_cascade_tts_leg_still_prices_the_same_way():
    """⛔ 산식을 하나로 합쳤다 — 캐스케이드 TTS 다리의 값이 안 변해야 한다(회귀)."""
    from core import tts as tts_mod

    leg = {"vendors": {"tts": {"vendor": tts_mod.CHIRP3_ENGINE, "chars": 1_000_000}}}
    cost, unknown = svc.estimate_cascade_cost_usd(leg["vendors"])
    assert cost == pytest.approx(30.0) and unknown == []
    # 토큰 과금 엔진의 "audio_s 없음" 문구도 그대로 남아 있어야 한다.
    vendor = next(iter(svc.TTS_TOKEN_PRICE_USD_PER_1M))
    _, tok_unknown = svc.estimate_cascade_cost_usd({"tts": {"vendor": vendor, "chars": 10}})
    assert tok_unknown and "audio_s 없음" in tok_unknown[0]


def test_an_unknown_vendor_is_reported_not_swallowed():
    """모르는 벤더를 조용히 0원으로 먹으면 '곁가지가 공짜'라는 거짓말이 된다."""
    cost, unknown = svc.estimate_side_cost_usd(
        {"sidecars": {"vendor": "gpt-9", "in_text": 100, "out_text": 100}}
    )
    assert cost == 0.0 and unknown == ["sidecars:gpt-9"]


# --------------------------------------------------------------------------- #
# ④⑥ 영속화 — 0회면 키가 안 생기고, 통화후 몫은 UPDATE 로 얹힌다
# --------------------------------------------------------------------------- #
class _FakeCall:
    def __init__(self):
        self.usage_json = None
        self.usage_engine = None
        self.usage_msgs = 0
        self.usage_in_audio = self.usage_in_text = 0
        self.usage_out_audio = self.usage_out_text = 0
        self.usage_total = self.usage_peak_prompt = 0


class _FakeDb:
    def __init__(self, call):
        self._call = call
        self.commits = 0

    def get(self, _model, _pk):
        return self._call

    def commit(self):
        self.commits += 1


def test_no_sidecar_calls_means_no_key_at_all():
    """⚠ 0 과 '안 돌았다'는 다르다 — 한 번도 안 돌았으면 키 자체가 없어야 한다."""
    call, db = _FakeCall(), None
    db = _FakeDb(call)
    assert svc.save_call_usage(db, 1, {"msgs": 1, "in_mod": {}, "out_mod": {}}) is True
    assert "sidecars" not in call.usage_json
    assert gemini_analysis.LlmUsage().as_dict() is None


def test_post_call_analysis_is_added_after_the_row_already_exists():
    """⭐ 통화후 분석은 usage 가 **저장된 뒤** 돈다 — UPDATE 가 아니면 통째로 유실된다."""
    call = _FakeCall()
    db = _FakeDb(call)
    svc.save_call_usage(db, 1, {"msgs": 3, "in_mod": {"AUDIO": 10}, "out_mod": {}})
    saved_before = dict(call.usage_json)

    entry = {"vendor": "gemini-2.5-flash", "calls": 1, "in_text": 4000, "out_text": 800}
    assert svc.add_call_usage_extra(db, 1, "analysis", entry) is True
    assert call.usage_json["analysis"] == entry
    # 기존 키는 하나도 안 잃는다(덮어쓰기가 아니라 얹기).
    for k, v in saved_before.items():
        assert call.usage_json[k] == v
    # 재시도는 누적이 아니라 최신값으로 대체된다(중복 계상 방지).
    assert svc.add_call_usage_extra(db, 1, "analysis", entry) is True
    assert call.usage_json["analysis"] == entry


def test_adding_nothing_writes_nothing():
    """⛔ R5 — 빈 몫으로 부르면 조용히 False(커밋도 안 한다)."""
    db = _FakeDb(_FakeCall())
    assert svc.add_call_usage_extra(db, 1, "analysis", None) is False
    assert db.commits == 0
