"""build_live_config / LIVE_CTX_* 검증 (B5 — 마스터플랜 §4).

여기까지 테스트가 **0건**이었다. 통화 원가의 81%를 결정하는 압축 창(trigger/target),
15분 통화의 전제인 session_resumption, R5(graceful degradation)를 지키는 transparent
Vertex 분기 — 전부 이 함수 한 곳에서 정해지는데 아무도 안 보고 있었다.

⚠ 이 파일이 특히 지키는 것: 마스터플랜 단계 4(압축 트리거 하향)는 `LIVE_CTX_TRIGGER_TOKENS`
  를 env 로 바꿔가며 관측하는 작업이다. "env 값이 실제로 config 까지 흘러가는가"와 "잘못된
  조합(target >= trigger)이 기동 시 막히는가"가 보장되지 않으면, 튜닝은 **관측한 값과 다른
  값으로 돌고 있을 수도 있는** 실험이 된다. 그래서 단계 4 착수 전에 이 파일을 먼저 채운다.

외부 의존 0 — 세션을 열지 않고 config 객체만 만들어 본다.
"""

from __future__ import annotations

import pytest
from google.genai import types

from core import gemini_live
from core.config import Settings
from core.gemini_live import DEFAULT_VOICE, LEVELTEST_DONE_TOOL, build_live_config


def _cfg(**kw) -> types.LiveConnectConfig:
    kw.setdefault("system_instruction", "너는 비버다")
    return build_live_config(**kw)


# --------------------------------------------------------------------------- #
# 1. 컨텍스트 압축 창 — 통화 원가를 직접 결정하는 값
# --------------------------------------------------------------------------- #
def test_compression_window_comes_from_settings():
    """trigger/target 은 상수가 아니라 settings 에서 온다(기본 16000/12000)."""
    cfg = _cfg()
    cwc = cfg.context_window_compression
    assert cwc is not None, "압축 설정이 빠지면 세션이 오디오 15분 한계에 그대로 걸린다"
    assert cwc.trigger_tokens == gemini_live.settings.LIVE_CTX_TRIGGER_TOKENS
    assert cwc.sliding_window.target_tokens == gemini_live.settings.LIVE_CTX_TARGET_TOKENS


def test_compression_window_defaults_are_16k_12k():
    """기본값 못박기 — 단계 0 계측이 이 값 위에서 수집됐다(실측의 기준선)."""
    s = Settings(DATABASE_URL_POOL="postgresql+psycopg2://x:x@127.0.0.1:5432/d")
    assert (s.LIVE_CTX_TRIGGER_TOKENS, s.LIVE_CTX_TARGET_TOKENS) == (16000, 12000)


def test_env_override_reaches_the_wire(monkeypatch):
    """env 로 낮춘 값이 실제 config 까지 흘러간다.

    ⭐ 단계 4(트리거 하향)의 전제. 이게 깨지면 "8k 로 낮췄다"고 믿으면서 16k 로 도는
    실험을 하게 되고, 관측한 원가·망각 여부가 전부 다른 설정의 결과가 된다.
    """
    monkeypatch.setattr(gemini_live.settings, "LIVE_CTX_TRIGGER_TOKENS", 8000)
    monkeypatch.setattr(gemini_live.settings, "LIVE_CTX_TARGET_TOKENS", 6000)
    cwc = _cfg().context_window_compression
    assert cwc.trigger_tokens == 8000
    assert cwc.sliding_window.target_tokens == 6000


def test_target_must_stay_below_trigger():
    """target >= trigger 는 기동 시점에 막힌다(런타임에 조용히 이상하게 돌지 않게)."""
    base = {"DATABASE_URL_POOL": "postgresql+psycopg2://x:x@127.0.0.1:5432/d"}
    with pytest.raises(ValueError):
        Settings(**base, LIVE_CTX_TRIGGER_TOKENS=8000, LIVE_CTX_TARGET_TOKENS=8000)
    with pytest.raises(ValueError):
        Settings(**base, LIVE_CTX_TRIGGER_TOKENS=8000, LIVE_CTX_TARGET_TOKENS=9000)
    with pytest.raises(ValueError):
        Settings(**base, LIVE_CTX_TRIGGER_TOKENS=8000, LIVE_CTX_TARGET_TOKENS=0)


# --------------------------------------------------------------------------- #
# 2. session_resumption — 15분 통화(세션 재연결)의 전제
# --------------------------------------------------------------------------- #
def test_resumption_absent_when_disabled(monkeypatch):
    """플래그가 꺼져 있으면 필드 자체를 넣지 않는다 = 종전과 바이트 동일(회귀 무영향)."""
    monkeypatch.setattr(gemini_live.settings, "LIVE_SESSION_RESUMPTION", False)
    assert _cfg(resume_handle="h-1").session_resumption is None


def test_resumption_new_session_asks_for_a_handle(monkeypatch):
    """켜져 있고 핸들이 없으면 handle=None — '새 세션이되 핸들은 발급해 달라'."""
    monkeypatch.setattr(gemini_live.settings, "LIVE_SESSION_RESUMPTION", True)
    sr = _cfg().session_resumption
    assert sr is not None and sr.handle is None


def test_resumption_replays_the_handle(monkeypatch):
    """재개 세대는 받은 핸들을 그대로 실어 보낸다(연결이 갈려도 대화가 이어지는 근거)."""
    monkeypatch.setattr(gemini_live.settings, "LIVE_SESSION_RESUMPTION", True)
    assert _cfg(resume_handle="handle-abc").session_resumption.handle == "handle-abc"


def test_transparent_only_on_vertex(monkeypatch):
    """⛔ transparent 는 Vertex 전용 — api_key(AI Studio) 폴백에 넘기면 SDK 가 ValueError
    를 던져 통화가 아예 안 열린다(R5 위반). 분기를 값으로 고정한다."""
    monkeypatch.setattr(gemini_live.settings, "LIVE_SESSION_RESUMPTION", True)
    monkeypatch.setattr(gemini_live.settings, "USE_VERTEX", True)
    assert _cfg().session_resumption.transparent is True
    monkeypatch.setattr(gemini_live.settings, "USE_VERTEX", False)
    assert _cfg().session_resumption.transparent is None


# --------------------------------------------------------------------------- #
# 3. 통화 본체 계약 — 오디오·전사·voice·페르소나
# --------------------------------------------------------------------------- #
def test_audio_and_transcription_contract():
    """출력=오디오, 입출력 전사 둘 다 켬. 전사가 빠지면 통화후 분석·문장추출이 통째로 죽는다."""
    cfg = _cfg()
    assert cfg.response_modalities == ["AUDIO"]
    assert cfg.input_audio_transcription is not None
    assert cfg.output_audio_transcription is not None
    # realtime_input_config 는 넣지 않는다(무음 버그 이력).
    assert getattr(cfg, "realtime_input_config", None) is None


def test_voice_and_system_instruction_are_passed_through():
    """system_instruction·voice 는 호출부(realtime)가 조립해 넘긴 값 그대로 — 어댑터는 안 만든다."""
    cfg = _cfg(system_instruction="지시문-XYZ", voice="Puck")
    assert cfg.speech_config.voice_config.prebuilt_voice_config.voice_name == "Puck"
    assert "지시문-XYZ" in str(cfg.system_instruction)
    assert _cfg().speech_config.voice_config.prebuilt_voice_config.voice_name == DEFAULT_VOICE


def test_temperature_is_not_zero():
    """native-audio 는 temperature=0 에서 반복·로봇처럼 된다 — 0 으로 내려가지 않게 못박는다."""
    assert _cfg().temperature == gemini_live.LIVE_TEMPERATURE > 0


def test_tools_default_none_keeps_general_call_bytes_identical():
    """일반 통화는 tools 미전달(None) — 하위호환. 필요할 때만 명시적으로 실린다."""
    assert _cfg().tools is None
    assert _cfg(tools=[LEVELTEST_DONE_TOOL]).tools == [LEVELTEST_DONE_TOOL]


def test_safety_relaxes_only_harassment():
    """거친 페르소나(트래시토커) 면박 허용은 HARASSMENT 만 — 혐오·성·위험은 엄격 유지."""
    by_cat = {s.category: s.threshold for s in _cfg().safety_settings}
    assert by_cat[types.HarmCategory.HARM_CATEGORY_HARASSMENT] == \
        types.HarmBlockThreshold.BLOCK_ONLY_HIGH
    for cat in (
        types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
        types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
        types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
    ):
        assert by_cat[cat] == types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE
