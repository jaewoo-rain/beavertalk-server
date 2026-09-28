"""core.speechsuper — 자체 NPU 발음평가 경로(2026-09-28).

- 매핑: NPU `sent.eval.kr` 응답(API 안내 §3-2 실제 예)을 도메인 계약으로 옮긴다.
- 순위: 토큰 있으면 NPU → 실패 시 SpeechSuper(키 있을 때) → 스텁.
- 토큰은 헤더로만 보낸다.

네트워크는 부르지 않는다(_call_npu·httpx 를 바꿔 끼운다).
"""

from __future__ import annotations

import pytest

import core.speechsuper as ss
from tests.test_speechsuper import _assert_contract

# API 안내 §3-2 의 실제 응답(외국인 학습자, 13글자 중 3글자).
_NPU_RESULT = {
    "overall": 84, "pronunciation": 89, "fluency": 70,
    "rhythm": None, "comprehensibility": None,
    "words": [
        {"word": "문", "readType": 2, "score": 96, "phonemes": [
            {"alpha": "mm", "alpha_jamo": "ㅁ", "position": "초성", "pronunciation": None,
             "actual": None, "actual_jamo": None, "errorType": "음운탈락"},
            {"alpha": "uu", "alpha_jamo": "ㅜ", "position": "중성", "pronunciation": 96,
             "actual": "uu", "actual_jamo": "ㅜ"},
            {"alpha": "nn", "alpha_jamo": "ㄴ", "position": "초성", "pronunciation": 96,
             "actual": "nn", "actual_jamo": "ㄴ"}]},
        {"word": "앞", "readType": 2, "score": 54, "phonemes": [
            {"alpha": "aa", "alpha_jamo": "ㅏ", "position": "중성", "pronunciation": 96,
             "actual": "aa", "actual_jamo": "ㅏ"},
            {"alpha": "ph", "alpha_jamo": "ㅍ", "position": "초성", "pronunciation": 12,
             "actual": "p0", "actual_jamo": "ㅂ", "errorType": "격음화"}]},
        {"word": "에", "readType": 0, "score": 99, "phonemes": [
            {"alpha": "ee", "alpha_jamo": "ㅔ", "position": "중성", "pronunciation": 99,
             "actual": "ee", "actual_jamo": "ㅔ"}]},
    ],
}


def test_map_npu_result_contract_and_values():
    out = ss._map_npu_result("문앞에", _NPU_RESULT)
    _assert_contract(out)
    assert out["is_stub"] is False
    # rhythm 은 문장 하나로는 null → overall 로 대체(기존 규칙)
    assert out["evaluation"] == {
        "total_score": 84, "pronunciation": 89, "fluency": 70, "rhythm": 84,
    }
    # 글자 점수는 words[].score
    assert [(c["char"], c["score"], c["grade"]) for c in out["char_scores"]] == [
        ("문", 96, "상"), ("앞", 54, "하"), ("에", 99, "상"),
    ]
    # 음소 alpha 는 자모(alpha_jamo), phoneme 은 KoG2P 기호, 위치는 서버가 준 것
    ph = out["phonemes"]
    assert [(p["phoneme"], p["alpha"], p["position"]) for p in ph[:3]] == [
        ("mm", "ㅁ", "초성"), ("uu", "ㅜ", "중성"), ("nn", "ㄴ", "초성"),
    ]
    # 빠뜨린 소리(pronunciation null)는 0점
    assert ph[0]["pronunciation"] == 0
    # 연음된 받침 ㅍ 은 서버 기준 초성 → onset 키
    assert ph[4]["alpha"] == "ㅍ" and ph[4]["sound_key"] == "onset_ㅍ"
    # 틀린 음소는 errorType 이 있는 것만 · 글자 인덱스는 char_scores 기준
    assert out["phoneme_misses"] == [
        {"char_index": 0, "expected": "ㅁ"},
        {"char_index": 1, "expected": "ㅍ"},
    ]


def test_map_npu_uncertain_not_flagged_and_misread_scores_zero():
    result = {
        "overall": 50, "pronunciation": 50, "fluency": 50,
        "words": [
            {"word": "가", "readType": 3, "score": None, "phonemes": [
                {"alpha": "k0", "alpha_jamo": "ㄱ", "position": "초성",
                 "pronunciation": 5, "errorType": "초성"}]},
            {"word": "나", "readType": 2, "score": 70, "phonemes": [
                {"alpha": "n0", "alpha_jamo": "ㄴ", "position": "초성",
                 "pronunciation": 65, "errorType": "초성", "uncertain": True}]},
            {"word": "다", "readType": 2, "score": None, "phonemes": []},
        ],
    }
    out = ss._map_npu_result("가나다", result)
    _assert_contract(out)
    # 오독(readType 3, score null) → 0점 「하」 · 판정 못 한 「다」 는 빠진다
    assert [(c["char"], c["score"]) for c in out["char_scores"]] == [("가", 0), ("나", 70)]
    # uncertain 은 지적하지 않는다
    assert out["phoneme_misses"] == [{"char_index": 0, "expected": "ㄱ"}]


def test_npu_first_when_token_set(monkeypatch):
    calls = {}

    def fake_call(**kw):
        calls.update(kw)
        return _NPU_RESULT

    monkeypatch.setattr(ss.settings, "PRON_NPU_TOKEN", "tkn", raising=False)
    monkeypatch.setattr(ss, "_load_audio", lambda url: (b"RIFF", "wav"))
    monkeypatch.setattr(ss, "_call_npu", fake_call)
    monkeypatch.setattr(
        ss, "_call_speechsuper", lambda **kw: pytest.fail("SpeechSuper 를 부르면 안 된다"))
    out = ss.assess_pronunciation("문앞에", "https://example.com/a.wav")
    assert out["is_stub"] is False
    assert out["evaluation"]["total_score"] == 84
    assert calls["token"] == "tkn" and calls["ref_text"] == "문앞에"


def test_npu_failure_falls_back_to_speechsuper_then_stub(monkeypatch):
    monkeypatch.setattr(ss.settings, "PRON_NPU_TOKEN", "tkn", raising=False)
    monkeypatch.setattr(ss, "_load_audio", lambda url: (b"RIFF", "wav"))

    def boom(**kw):
        raise RuntimeError("npu down")

    monkeypatch.setattr(ss, "_call_npu", boom)
    # SpeechSuper 키가 있으면 SpeechSuper
    monkeypatch.setattr(ss.settings, "SPEECH_SUPER_APP_KEY", "a", raising=False)
    monkeypatch.setattr(ss.settings, "SPEECH_SUPER_SECRET_KEY", "b", raising=False)
    monkeypatch.setattr(ss, "_call_speechsuper", lambda **kw: {
        "overall": 77, "words": [{"word": "가", "scores": {"overall": 77}}]})
    out = ss.assess_pronunciation("가", "https://example.com/a.wav")
    assert out["is_stub"] is False and out["evaluation"]["total_score"] == 77
    # 키도 없으면 스텁
    monkeypatch.setattr(ss.settings, "SPEECH_SUPER_APP_KEY", None, raising=False)
    out = ss.assess_pronunciation("가", "https://example.com/a.wav")
    assert out["is_stub"] is True


def test_no_token_keeps_old_path(monkeypatch):
    monkeypatch.setattr(ss.settings, "PRON_NPU_TOKEN", None, raising=False)
    monkeypatch.setattr(ss, "_call_npu", lambda **kw: pytest.fail("토큰 없으면 NPU 금지"))
    monkeypatch.setattr(ss.settings, "SPEECH_SUPER_APP_KEY", None, raising=False)
    out = ss.assess_pronunciation("가", "https://example.com/a.wav")
    assert out["is_stub"] is True


def test_call_npu_sends_token_in_header_only(monkeypatch):
    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"result": _NPU_RESULT}

    class _Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, data=None, files=None, headers=None):
            sent.update(url=url, data=data, files=files, headers=headers)
            return _Resp()

    monkeypatch.setattr(ss.httpx, "Client", _Client)
    out = ss._call_npu(ref_text="문앞에", audio_bytes=b"x", audio_type="wav",
                       base_url="https://npu.example:8443/", token="secret-t")
    assert out is _NPU_RESULT
    assert sent["url"] == "https://npu.example:8443/sent.eval.kr"
    assert sent["headers"] == {"Authorization": "Bearer secret-t"}
    assert "secret-t" not in sent["url"]
    assert "secret-t" not in str(sent["data"])
    assert sent["data"]["refText"] == "문앞에"
