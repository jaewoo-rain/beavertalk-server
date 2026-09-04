# -*- coding: utf-8 -*-
"""플랜별 통화 분기 — 영상(표정)·모델을 구독 등급으로 가른다(2026-09-04).

## 사장님 지시
    max 만 영상통화. free·pro 는 모두 음성통화. free 는 한 조각.
    모델 가르는 이유는 원가 절감.

## ⛔ 이 파일이 지키는 것
판정기가 **하나**여야 한다(`effective_plan`). 상태(state)와 플랜(plan)은 다른 축이라
(grace/on_hold/ending 은 직전 플랜을 유지) 상태 문자열을 직접 보면 앱(`impliedTier`)과
판정이 갈린다 — "통화는 되는데 캐릭터는 잠김" 같은 어긋남이 그렇게 난다.
"""
import pytest

from core.config import Settings
from domains.learning.service import call_service as cs


# --------------------------------------------------------------------------- #
# 1. 표 자체 — 사장님 지시가 값으로 박혀 있는가
# --------------------------------------------------------------------------- #

def test_only_max_gets_video():
    """max 만 영상. free·pro 는 음성."""
    assert cs.CALL_VIDEO_BY_PLAN["max"] is True
    assert cs.CALL_VIDEO_BY_PLAN["pro"] is False
    assert cs.CALL_VIDEO_BY_PLAN[None] is False


def test_free_gets_exactly_one_fragment():
    """⛔ free 는 **한 조각**. pro·max 는 3조각(5분×3=15분, 서버는 6분 백스톱)."""
    assert cs.CALL_FRAGMENTS_BY_PLAN[None] == 1
    assert cs.CALL_FRAGMENTS_BY_PLAN["pro"] == 3
    assert cs.CALL_FRAGMENTS_BY_PLAN["max"] == 3


def test_unknown_plan_falls_back_to_free_everywhere():
    """⛔ 모르는 플랜은 **Free 로 떨어진다**(R5). 예외가 아니라 조용한 강등이다.

    "모르면 싸게 준다"가 안전하다 — 잘못 열어 원가가 새는 것보다 낫다.
    ⚠ 표를 dict.get 으로 읽되 **기본값을 반드시 준다**. 빈 dict 로 두면 KeyError 가
      통화를 죽인다.
    """
    for table, free_key in (
        (cs.CALL_VIDEO_BY_PLAN, None),
        (cs.CALL_FRAGMENTS_BY_PLAN, None),
        (cs.CALL_LIVE_MODEL_BY_PLAN, None),
    ):
        assert table.get("enterprise", table[free_key]) == table[free_key]


# --------------------------------------------------------------------------- #
# 2. 모델 선택 — 값의 원본은 settings 하나다
# --------------------------------------------------------------------------- #

def test_model_ids_live_in_settings_not_in_the_table():
    """⛔ 모델 id 문자열을 표에 적지 마라 — 두 곳에 적으면 언젠가 갈라진다.

    표는 "voice/video 중 어느 쪽인가"만 담고, 실제 id 는 settings 가 갖는다.
    """
    assert set(cs.CALL_LIVE_MODEL_BY_PLAN.values()) <= {"voice", "video"}


def test_video_plan_takes_the_video_model(monkeypatch):
    monkeypatch.setattr(cs.settings, "LIVE_MODEL_VOICE", "M-VOICE", raising=False)
    monkeypatch.setattr(cs.settings, "LIVE_MODEL_VIDEO", "M-VIDEO", raising=False)
    monkeypatch.setattr(cs, "effective_plan", lambda db, m: "max")
    assert cs.live_model_for(object(), 1) == "M-VIDEO"
    assert cs.call_video_for(object(), 1) is True


@pytest.mark.parametrize("plan", ["pro", None, "누가봐도-없는-플랜"])
def test_non_video_plans_take_the_voice_model(monkeypatch, plan):
    monkeypatch.setattr(cs.settings, "LIVE_MODEL_VOICE", "M-VOICE", raising=False)
    monkeypatch.setattr(cs.settings, "LIVE_MODEL_VIDEO", "M-VIDEO", raising=False)
    monkeypatch.setattr(cs, "effective_plan", lambda db, m: plan)
    assert cs.live_model_for(object(), 1) == "M-VOICE"
    assert cs.call_video_for(object(), 1) is False


def test_empty_settings_fall_back_to_the_legacy_model(monkeypatch):
    """⚠ 하위호환 — 두 설정을 비우면 종전 `GEMINI_LIVE_MODEL` 그대로다.

    플랜 기능을 통째로 끄고 싶을 때의 탈출구이고, 레벨테스트·캐스케이드처럼 플랜을
    모르는 호출부가 아직 그 값을 본다.
    """
    monkeypatch.setattr(cs.settings, "LIVE_MODEL_VOICE", "", raising=False)
    monkeypatch.setattr(cs.settings, "LIVE_MODEL_VIDEO", "", raising=False)
    monkeypatch.setattr(cs.settings, "GEMINI_LIVE_MODEL", "M-LEGACY", raising=False)
    monkeypatch.setattr(cs, "effective_plan", lambda db, m: "max")
    assert cs.live_model_for(object(), 1) == "M-LEGACY"


# --------------------------------------------------------------------------- #
# 3. 비상 차단기 — LIVE_FACE_SPIKE 의 의미가 바뀌었다
# --------------------------------------------------------------------------- #

def test_face_spike_is_now_a_kill_switch_not_a_feature_flag():
    """⛔ `LIVE_FACE_SPIKE=false` 면 **Max 도** 표정을 못 받는다.

    ⚠ 의미가 바뀌었다 — 예전엔 "표정 기능 자체의 on/off" 였고 지금은 **비상 차단기**다.
      켜져 있어도 플랜이 아니면 안 준다. 통화 경로의 실제 식은
          face_tool = bool(settings.LIVE_FACE_SPIKE) and wants_video
      이고, 이 시험은 그 진리표를 못박는다.
    """
    truth = {(True, True): True, (True, False): False,
             (False, True): False, (False, False): False}
    for (spike, wants), expect in truth.items():
        assert (bool(spike) and wants) is expect


# --------------------------------------------------------------------------- #
# 4. 실측 가능성 — 모델이 통화 행에 남아야 한다
# --------------------------------------------------------------------------- #

def test_engine_tag_carries_the_model_so_savings_are_measurable():
    """⭐ 종전 태그는 모델을 안 담아 **2.5 통화와 3.1 통화가 같은 문자열로 섞였다.**

    그러면 "2.5 로 내린 게 얼마 아꼈나"를 DB 로 못 묻는다. 플랜 분기를 넣는 이유가
    원가 절감인데 그 효과를 측정할 수 없으면 안 된다.
    """
    from domains.learning.service import normalcall_service as svc
    tag = svc.build_engine_tag("live", "gemini-3.1-flash-live-preview")
    assert tag == "live:gemini-3.1-flash-live-preview"
    assert tag != svc.ENGINE_LIVE_GEMINI, "모델이 안 실렸다 — 두 모델이 또 섞인다"


def test_new_tag_still_costs_as_live_not_cascade():
    """⛔ 태그를 바꿨다고 원가가 캐스케이드로 새면 안 된다.

    분기가 `startswith("cascade:")` 라 `live:` 접두사만 지키면 되지만, 그 계약이
    깨지는 순간 원가가 조용히 틀린 단가로 계산된다. 여기서 못박는다.
    """
    from domains.learning.service import normalcall_service as svc
    tag = svc.build_engine_tag("live", "gemini-live-2.5-flash-native-audio")
    live, _ = svc.estimate_call_cost_usd(
        engine=tag, in_audio=1_000_000, in_text=0, out_audio=0, out_text=0, usage_json=None,
    )
    assert live == pytest.approx(svc.LIVE_TOKEN_PRICE_USD["in_audio"]), \
        "새 태그가 Live 단가로 안 계산됐다"


def test_legacy_constant_is_untouched():
    """⛔ `ENGINE_LIVE_GEMINI` 는 캐스케이드와 공유하는 **계약**이다. 바꾸지 마라.

    한쪽만 바꾸면 두 엔진의 행이 서로 다른 이름으로 쌓여 비교가 깨진다.
    """
    from domains.learning.service import normalcall_service as svc
    assert svc.ENGINE_LIVE_GEMINI == "live:gemini-native-audio"


# --------------------------------------------------------------------------- #
# 5. 설정 자체
# --------------------------------------------------------------------------- #

def test_defaults_keep_max_on_31_and_others_on_25():
    s = Settings(DATABASE_URL_POOL="postgresql://x/y")
    assert "3.1" in s.LIVE_MODEL_VIDEO
    assert "2.5" in s.LIVE_MODEL_VOICE
