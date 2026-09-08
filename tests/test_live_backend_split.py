# -*- coding: utf-8 -*-
"""플랜별 **백엔드** 분기 — Free·Pro=Vertex / Max=AI Studio (2026-09-08).

## 사장님 지시
    같은 2.5 가 Vertex 에서 2.5 배 빠르다(실측 1.15초 vs 2.82초). Free·Pro 를 Vertex 로
    되돌린다. Max 는 3.1 인데 **3.1 은 Vertex 에 없으므로**(실측 1008) AI Studio 에 남는다.
    레벨테스트는 AI Studio 3.1 로 고정한다.

## ⛔ 이 파일이 지키는 것 — 「이름과 백엔드는 한 묶음」

2026-09-06 demo-api 리비전 `00265-br2` 가 `USE_VERTEX` 를 true→false 로 뒤집으면서
**모델 이름을 안 바꿨다.** 그 이름(`gemini-live-2.5-flash-native-audio`)은 Vertex 전용이라
AI Studio 에서 1008 이 났고, **Free·Pro 통화가 2주 죽었다.** Max 는 3.1 이라 멀쩡해서
아무도 몰랐다.

⇒ 그래서 이 설계는 **둘을 한 함수에서 같이 고른다**(`live_engine_for`). 이 파일은
   그 «한 묶음» 계약이 깨지지 않는지를 잠근다.
"""
import pytest

from core import gemini_live as gl
from domains.learning.service import call_service as cs


# --------------------------------------------------------------------------- #
# 1. 표 — 사장님 지시가 값으로 박혀 있는가
# --------------------------------------------------------------------------- #

def _fix(monkeypatch, **kw):
    """settings 를 시험 값으로 고정한다(개발자 .env 에 흔들리지 않게)."""
    base = dict(
        USE_VERTEX=False,
        LIVE_VOICE_BACKEND="vertex", LIVE_VIDEO_BACKEND="studio",
        LIVE_MODEL_VOICE_VERTEX="M-VERTEX-2.5", LIVE_MODEL_VIDEO_VERTEX="",
        LIVE_MODEL_VOICE="M-STUDIO-2.5", LIVE_MODEL_VIDEO="M-STUDIO-3.1",
        GEMINI_LIVE_MODEL="M-LEGACY",
    )
    base.update(kw)
    for k, v in base.items():
        monkeypatch.setattr(cs.settings, k, v, raising=False)


@pytest.mark.parametrize("plan", [None, "pro"])
def test_free_and_pro_go_to_vertex(monkeypatch, plan):
    """⭐ 원가는 그대로 두고 응답만 3.0 배 빨라지는 자리다(실측 2.82초 → 1.15초)."""
    _fix(monkeypatch)
    monkeypatch.setattr(cs, "effective_plan", lambda db, m: plan)
    assert cs.live_engine_for(object(), 1) == ("vertex", "M-VERTEX-2.5")


def test_max_stays_on_ai_studio(monkeypatch):
    """⛔ **3.1 은 Vertex 에 없다**(2026-09-08 실측: us-central1 에서 1008).

    그래서 «전부 Vertex» 는 선택지가 아니다 — Max 의 영상통화를 포기해야 한다.
    혼합이 설계상 필수라는 사실을 여기서 못박는다.
    """
    _fix(monkeypatch)
    monkeypatch.setattr(cs, "effective_plan", lambda db, m: "max")
    assert cs.live_engine_for(object(), 1) == ("studio", "M-STUDIO-3.1")


def test_unknown_plan_falls_back_to_free(monkeypatch):
    """모르는 플랜은 Free 로 떨어진다(R5) — 백엔드 축도 같은 규율이다."""
    _fix(monkeypatch)
    monkeypatch.setattr(cs, "effective_plan", lambda db, m: "누가봐도-없는-플랜")
    assert cs.live_engine_for(object(), 1) == ("vertex", "M-VERTEX-2.5")


# --------------------------------------------------------------------------- #
# 2. ⛔⛔ 한 묶음 계약 — 어긋난 (백엔드, 모델) 조합이 나올 수 있는가
# --------------------------------------------------------------------------- #

def test_vertex_without_a_vertex_model_reverts_the_backend_too(monkeypatch):
    """⛔⛔ **이 시험이 2026-09-06 장애를 잠근다.**

    Vertex 를 지정했는데 그쪽 모델 이름이 비어 있으면, 백엔드만 Vertex 로 두고 AI Studio
    이름을 보내는 것이 가장 나쁜 결과다 — **1008 로 통화가 통째로 죽는다.**
    ⇒ 이름이 없으면 **백엔드까지 같이 되돌린다.** 느린 게 죽는 것보다 낫다(R5).
    """
    _fix(monkeypatch, LIVE_MODEL_VOICE_VERTEX="")
    monkeypatch.setattr(cs, "effective_plan", lambda db, m: None)
    backend, model = cs.live_engine_for(object(), 1)
    assert (backend, model) == ("studio", "M-STUDIO-2.5"), \
        "Vertex 모델이 없으면 백엔드도 studio 로 돌아와야 한다 — 안 그러면 1008 이다"


def test_model_and_backend_come_from_one_call(monkeypatch):
    """⛔ `live_model_for` 는 `live_engine_for` 의 **파생**이어야 한다.

    두 함수가 각자 고르면 그 사이에 구독이 바뀔 때 어긋난 조합이 나온다.
    """
    _fix(monkeypatch)
    monkeypatch.setattr(cs, "effective_plan", lambda db, m: None)
    assert cs.live_model_for(object(), 1) == cs.live_engine_for(object(), 1)[1]


# --------------------------------------------------------------------------- #
# 3. 하위호환 — 새 설정이 비면 종전 그대로
# --------------------------------------------------------------------------- #

def test_empty_backend_settings_follow_the_global_flag(monkeypatch):
    """⭐ 되돌리기가 **코드 배포 없이** 되어야 한다 — env 두 줄을 지우면 종전 동작이다."""
    _fix(monkeypatch, LIVE_VOICE_BACKEND="", LIVE_VIDEO_BACKEND="", USE_VERTEX=False)
    monkeypatch.setattr(cs, "effective_plan", lambda db, m: None)
    assert cs.live_engine_for(object(), 1) == ("studio", "M-STUDIO-2.5")

    monkeypatch.setattr(cs.settings, "USE_VERTEX", True, raising=False)
    assert cs.live_engine_for(object(), 1) == ("vertex", "M-VERTEX-2.5")


def test_defaults_ship_with_vertex_voice_and_no_vertex_video():
    """설정 기본값 자체 — Vertex 음성 이름은 있고, Vertex 영상 이름은 **비어 있어야** 한다."""
    from core.config import Settings

    s = Settings(DATABASE_URL_POOL="postgresql://x/y")
    assert s.LIVE_MODEL_VOICE_VERTEX == "gemini-live-2.5-flash-native-audio"
    assert s.LIVE_MODEL_VIDEO_VERTEX == "", "3.1 은 Vertex 에 없다 — 이름을 지어내지 마라"


# --------------------------------------------------------------------------- #
# 4. ⛔ Vertex 전용 필드가 **이 통화의** 백엔드를 따르는가 (전역이 아니라)
# --------------------------------------------------------------------------- #

def _cfg(**kw):
    return gl.build_live_config(system_instruction="x", voice="Leda", **kw)


def test_safety_settings_follow_the_call_not_the_global(monkeypatch):
    """⛔⛔ **혼합 운영의 핵심 함정.**

    `safety_settings` 는 Vertex 전용이다 — AI Studio 에 보내면 세션이 **열리지도 않는다**
    (실측 2026-08-20: `1007 Unknown name "safetySettings"`, call 1115·1116, msgs=0).
    전역 `USE_VERTEX` 를 보면 **모든 통화에 같은 답**을 주므로, Vertex 통화에 맞추는 순간
    Max(AI Studio) 통화가 전부 죽는다. 그래서 **이 통화의 값**을 따라야 한다.
    """
    monkeypatch.setattr(gl.settings, "USE_VERTEX", True, raising=False)
    assert _cfg(vertex=False).safety_settings is None, \
        "전역이 Vertex 라도 studio 통화엔 safetySettings 가 실리면 안 된다(1007)"

    monkeypatch.setattr(gl.settings, "USE_VERTEX", False, raising=False)
    cfg = _cfg(vertex=True)
    assert cfg.safety_settings and len(cfg.safety_settings) == 4, \
        "전역이 AI Studio 라도 vertex 통화엔 실려야 한다(거친 페르소나가 여기 걸려 있다)"


def test_transparent_follows_the_call_not_the_global(monkeypatch):
    """같은 함정 둘째 — `session_resumption.transparent` 도 Vertex 전용(ValueError)."""
    monkeypatch.setattr(gl.settings, "LIVE_SESSION_RESUMPTION", True, raising=False)
    monkeypatch.setattr(gl.settings, "USE_VERTEX", True, raising=False)
    assert _cfg(vertex=False).session_resumption.transparent is None
    monkeypatch.setattr(gl.settings, "USE_VERTEX", False, raising=False)
    assert _cfg(vertex=True).session_resumption.transparent is True


@pytest.mark.parametrize("global_flag", [True, False])
def test_none_means_follow_the_global(monkeypatch, global_flag):
    """⚠ 기본 None = 전역 그대로 — 기존 호출부·스냅샷이 **바이트 동일**이어야 한다."""
    monkeypatch.setattr(gl.settings, "USE_VERTEX", global_flag, raising=False)
    monkeypatch.setattr(gl.settings, "LIVE_SESSION_RESUMPTION", True, raising=False)
    assert bool(_cfg().safety_settings) is global_flag
    assert bool(_cfg(vertex=None).safety_settings) is global_flag


# --------------------------------------------------------------------------- #
# 5. ⛔ 소스를 읽는 시험 — 동어반복은 9/4 사고를 못 잡았다
# --------------------------------------------------------------------------- #

def _src(*parts) -> str:
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1].joinpath(*parts)
            ).read_text(encoding="utf-8")


def test_call_session_picks_client_and_model_together():
    """⛔ 통화 경로가 **한 번의 조회**로 둘을 같이 읽는가.

    ⚠ 위 §1~2 는 `call_service` 만 본다. 실제로 그 함수를 쓰는지는 **호출부를 읽어야**
      안다 — `test_plan_call_split.py:123` 이 같은 이유로 소스를 읽는다(진리표를 스스로
      만들어 검증하는 시험은 9/4 사고를 못 잡았다).
    """
    src = _src("domains", "learning", "realtime", "call_session.py")
    assert "call_service.live_engine_for(db, member_id)" in src, \
        "통화 경로가 live_engine_for 를 안 쓴다 — 백엔드와 모델이 따로 정해질 수 있다"
    assert 'factory_kwargs["vertex"] = state.live_vertex' in src, \
        "고른 백엔드가 세션 팩토리까지 안 흘러간다 — safety/transparent 가 전역을 따른다"


def test_level_test_backend_is_pinned_not_inherited():
    """⛔ 레벨테스트는 플랜 분기를 **안 탄다** — 명시가 없으면 폴백으로 흘러 조용히 죽는다.

    사장님 결정(2026-09-08): 레벨테스트는 AI Studio 3.1.
    """
    src = _src("domains", "learning", "realtime", "call_session.py")
    assert 'genai_client_studio' in src and 'client, live_vertex = _lt_client, False' in src, \
        "레벨테스트가 백엔드를 명시하지 않는다 — 전역이 바뀌는 날 1008 로 죽는다"


def test_default_client_meaning_is_unchanged():
    """⛔ 기본 `genai_client` 는 캐스케이드·통화후 분석이 공유한다 — 의미를 바꾸지 않는다.

    사장님 결정(2026-09-08): 분석·캐스케이드는 AI Studio 그대로. 그래서 **추가만** 한다.
    """
    src = _src("main.py")
    assert "app.state.genai_client = _create_genai_client(settings)" in src, \
        "기본 클라이언트 생성이 바뀌었다 — 캐스케이드·분석이 같이 움직인다"
    assert "app.state.genai_client_vertex" in src and "app.state.genai_client_studio" in src
