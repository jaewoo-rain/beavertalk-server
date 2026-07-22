"""core.speechsuper 단위 테스트.

- 폴백: audio_url 없음 / 키 없음일 때 결정적 스텁 결과 반환(반환 계약 유지).
- 매핑: SpeechSuper 응답(result)을 도메인 형태로 정확히 매핑.

실제 SpeechSuper 실호출은 키/오디오가 필요하므로, 매핑은 _map_result 를 직접 호출해
응답 스키마만 검증한다(네트워크 없음).
"""

from __future__ import annotations

import core.speechsuper as ss


def _assert_contract(out: dict) -> None:
    """반환 계약(키/타입) 검증."""
    assert set(out.keys()) == {"evaluation", "char_scores", "phonemes"}
    ev = out["evaluation"]
    assert set(ev.keys()) == {"total_score", "pronunciation", "fluency", "rhythm"}
    for v in ev.values():
        assert isinstance(v, int)
    for cs in out["char_scores"]:
        assert set(cs.keys()) == {"char", "score", "grade"}
        assert isinstance(cs["score"], int)
        assert cs["grade"] in ("상", "중", "하")
    # phonemes 키는 항상 존재(리스트), 각 항목은 phoneme/alpha/pronunciation
    assert isinstance(out["phonemes"], list)
    for p in out["phonemes"]:
        assert set(p.keys()) == {"phoneme", "alpha", "pronunciation"}
        assert isinstance(p["phoneme"], str)
        assert isinstance(p["alpha"], str)
        assert isinstance(p["pronunciation"], int)


def test_fallback_no_audio_url():
    """audio_url 없으면 스텁으로 폴백하고 계약을 지킨다."""
    out = ss.assess_pronunciation("안녕하세요", None)
    _assert_contract(out)
    # 공백 제외 글자 수만큼 char_scores
    assert len(out["char_scores"]) == 5
    assert [c["char"] for c in out["char_scores"]] == list("안녕하세요")


def test_fallback_excludes_whitespace():
    """공백은 char_scores 에서 제외된다."""
    out = ss.assess_pronunciation("가 나 다", None)
    assert [c["char"] for c in out["char_scores"]] == ["가", "나", "다"]


def test_fallback_no_keys(monkeypatch):
    """키가 없으면 audio_url 이 있어도 스텁 폴백."""
    monkeypatch.setattr(ss.settings, "SPEECH_SUPER_APP_KEY", None, raising=False)
    monkeypatch.setattr(ss.settings, "SPEECH_SUPER_SECRET_KEY", None, raising=False)
    out = ss.assess_pronunciation("테스트", "https://example.com/a.wav")
    _assert_contract(out)
    # 스텁의 결정적 점수: 60 + ord%41
    s = 60 + (ord("테") % 41)
    assert out["char_scores"][0]["score"] == s


def test_map_result_word_scores():
    """words[] 단어 점수가 있으면 글자에 분배된다."""
    result = {
        "overall": 88,
        "pronunciation": 90,
        "fluency": 80,
        "rhythm": 85,
        "words": [
            {"word": "안녕", "scores": {"overall": 95}},
            {"word": "하세요", "scores": {"overall": 70}},
        ],
    }
    out = ss._map_result("안녕 하세요", result)
    _assert_contract(out)
    assert out["evaluation"] == {
        "total_score": 88,
        "pronunciation": 90,
        "fluency": 80,
        "rhythm": 85,
    }
    chars = out["char_scores"]
    assert [c["char"] for c in chars] == list("안녕하세요")
    # 앞쪽 글자는 높은 단어 점수, 뒤쪽은 낮은 단어 점수 영역에 들어간다
    assert chars[0]["score"] == 95
    assert chars[-1]["score"] == 70


def test_map_result_rhythm_falls_back_to_integrity():
    """rhythm 없으면 integrity, 그것도 없으면 overall 로 대체."""
    out = ss._map_result("가", {"overall": 60, "integrity": 72})
    assert out["evaluation"]["rhythm"] == 72
    out2 = ss._map_result("가", {"overall": 60})
    assert out2["evaluation"]["rhythm"] == 60


def test_map_result_no_words_uses_overall():
    """단어 점수 없으면 overall 기준 ±소폭으로 채운다."""
    out = ss._map_result("가나다", {"overall": 80})
    for c in out["char_scores"]:
        assert 78 <= c["score"] <= 82


def test_map_result_extracts_phonemes():
    """words[].phonemes[] 에서 자모별 발음 점수를 추출한다("안녕하세요" 실측 구조)."""
    result = {
        "overall": 60,
        "words": [
            {
                "word": "안",
                "scores": {"overall": 97},
                "phonemes": [
                    {"span": {"start": 0, "end": 1}, "phoneme": "A", "alpha": "ㅏ", "pronunciation": 97},
                    {"span": {"start": 1, "end": 2}, "phoneme": "N", "alpha": "ㄴ", "pronunciation": 100},
                ],
            },
            {
                "word": "녕",
                "scores": {"overall": 0},
                "phonemes": [
                    {"phoneme": "L", "alpha": "ㄹ", "pronunciation": 0},
                    {"phoneme": "EO", "alpha": "ㅕ", "pronunciation": 1},
                    {"phoneme": "NG", "alpha": "ㅇ", "pronunciation": 0},
                ],
            },
        ],
    }
    out = ss._map_result("안녕", result)
    _assert_contract(out)
    phs = out["phonemes"]
    assert [p["alpha"] for p in phs] == ["ㅏ", "ㄴ", "ㄹ", "ㅕ", "ㅇ"]
    assert [p["pronunciation"] for p in phs] == [97, 100, 0, 1, 0]
    assert phs[0] == {"phoneme": "A", "alpha": "ㅏ", "pronunciation": 97}


def test_map_result_skips_words_without_phonemes():
    """phonemes 없는 word(readType 4 등)는 스킵하고, 있는 것만 모은다."""
    result = {
        "overall": 80,
        "words": [
            {"word": "가", "scores": {"overall": 80}},  # phonemes 없음 → 스킵
            {
                "word": "나",
                "scores": {"overall": 90},
                "phonemes": [{"phoneme": "N", "alpha": "ㄴ", "pronunciation": 88}],
            },
        ],
    }
    out = ss._map_result("가나", result)
    assert [p["alpha"] for p in out["phonemes"]] == ["ㄴ"]


def test_map_result_phonemes_defensive():
    """words 없음/타입 이상/필드 결손이어도 KeyError 없이 빈 리스트로 처리."""
    # words 자체 없음
    assert ss._map_result("가", {"overall": 70})["phonemes"] == []
    # phonemes 항목 타입/필드 이상 → 유효한 것만
    result = {
        "overall": 70,
        "words": [
            {"word": "가", "phonemes": "not-a-list"},  # 타입 이상 → 스킵
            {"word": "나", "phonemes": [
                "bad-item",                                   # dict 아님 → 스킵
                {"phoneme": "", "alpha": "ㄴ", "pronunciation": 5},  # phoneme 빈값 → 스킵
                {"phoneme": "A", "pronunciation": 5},                # alpha 없음 → 스킵
                {"phoneme": "N", "alpha": "ㄴ", "pronunciation": "88"},  # 문자열 점수 → 정규화
            ]},
        ],
    }
    phs = ss._map_result("가나", result)["phonemes"]
    assert phs == [{"phoneme": "N", "alpha": "ㄴ", "pronunciation": 88}]


def test_stub_generates_mock_phonemes():
    """스텁 폴백도 자모(phonemes)를 목으로 생성 — 소리별 정확도 리포트가 빈 채로
    나오지 않게. 각 항목은 {alpha, pronunciation} 을 갖고, 한글이면 자모가 나온다."""
    out = ss.assess_pronunciation("안녕하세요", None)
    phonemes = out["phonemes"]
    assert isinstance(phonemes, list) and len(phonemes) > 0
    assert all("alpha" in p and "pronunciation" in p for p in phonemes)
    # "안" → ㅇ·ㅏ·ㄴ 처럼 자모가 실제로 분해된다.
    assert {"ㅏ", "ㄴ"} <= {p["alpha"] for p in phonemes}


def test_call_failure_falls_back(monkeypatch):
    """실호출 경로에서 예외가 나면 스텁으로 폴백(예외 전파 안 됨)."""
    monkeypatch.setattr(ss.settings, "SPEECH_SUPER_APP_KEY", "x", raising=False)
    monkeypatch.setattr(ss.settings, "SPEECH_SUPER_SECRET_KEY", "y", raising=False)

    def boom(*_a, **_k):
        raise RuntimeError("network down")

    monkeypatch.setattr(ss, "_load_audio", boom)
    out = ss.assess_pronunciation("안녕", "https://example.com/a.wav")
    _assert_contract(out)  # 스텁 결과
