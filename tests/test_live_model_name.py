# -*- coding: utf-8 -*-
"""Live 모델 이름 — **손으로 지은 이름이 배포로 나가는 것을 막는다.**

## ⛔ 왜 있나 (2026-09-07 실사고)

`LIVE_MODEL_VOICE` 기본값에 `gemini-live-2.5-flash-native-audio` 가 박혀 있었다.
**그런 모델은 없다.** 낱말 순서가 뒤집힌 오타였는데, 결과가 이랬다:

    Free·Pro  → VOICE 모델 → 1008 policy violation → 통화가 통째로 안 됨
    Max       → VIDEO(3.1)  → 정상

⇒ 사장님 계정이 Max 라 **2주 동안 아무도 몰랐다.** 「다른 사람들이 통화가 안 된다」는
제보로 겨우 드러났다.

## ⚠ 이 파일이 못 하는 것을 먼저 적는다

**모델이 실재하는지는 여기서 못 본다.** 테스트는 오프라인이고, 네트워크를 타면
CI 가 구글 사정에 흔들린다. 그래서 여기서는 **형태**만 잠근다 —
「우리가 아는 계열의 이름인가」.

⭐ **실재 확인은 배포 전에 사람이 한다.** 명령은 아래 [test_the_check_command_is_documented]
에 적어 뒀다. 형태가 맞아도 없는 모델일 수 있다(예: 09-2025 가 내려간 뒤).
"""

from __future__ import annotations

import re

import pytest

from core.config import Settings


# 2026-09-07 API 실측(bidiGenerateContent 지원). 이 목록 자체가 정본은 아니고,
# **형태를 읽는 근거**다 — 새 세대가 나오면 아래 정규식을 넓히면 된다.
_KNOWN_LIVE_MODELS = {
    "gemini-2.5-flash-native-audio-latest",
    "gemini-2.5-flash-native-audio-preview-09-2025",
    "gemini-2.5-flash-native-audio-preview-12-2025",
    "gemini-3.1-flash-live-preview",
    "gemini-3.5-transcribe-live",
    "gemini-3.5-live-translate-preview",
}

# 우리가 통화에 쓰는 계열의 이름 모양.
#   gemini-<버전>-flash-native-audio-<태그>   (2.5 네이티브 오디오 계열)
#   gemini-<버전>-flash-live-<태그>           (3.1 라이브 계열)
_SHAPE = re.compile(
    r"^gemini-\d+(?:\.\d+)?-flash-(?:native-audio|live)(?:-[a-z0-9-]+)?$"
)


def _defaults() -> Settings:
    """env 를 타지 않는 **기본값**을 본다.

    ⛔ `settings` 싱글턴을 쓰면 안 된다 — 그건 `.env` 가 덮은 값이라, 기본값이 틀려도
      통과해 버린다. 이번 사고가 정확히 그 모양이었다(운영 env 는 멀쩡했다).
    """
    return Settings.model_construct()


@pytest.mark.parametrize(
    "field", ["GEMINI_LIVE_MODEL", "LIVE_MODEL_VOICE", "LIVE_MODEL_VIDEO"]
)
def test_default_model_name_has_a_known_shape(field):
    """⭐ 기본값이 **우리가 아는 이름 모양**인가.

    `gemini-live-2.5-flash-native-audio` 같은 뒤집힌 오타를 여기서 잡는다.
    """
    value = getattr(Settings.model_fields[field], "default")
    assert isinstance(value, str) and value, f"{field} 기본값이 비었다"
    assert _SHAPE.match(value), (
        f"{field}={value!r} 가 아는 이름 모양이 아니다.\n"
        "  ⛔ 손으로 짓지 마라 — 아래 명령으로 실재하는 이름을 받아서 쓴다.\n"
        "  (새 세대가 나온 것이라면 이 파일의 _SHAPE 를 넓혀라)"
    )


@pytest.mark.parametrize(
    "field", ["GEMINI_LIVE_MODEL", "LIVE_MODEL_VOICE", "LIVE_MODEL_VIDEO"]
)
def test_default_model_is_one_we_actually_saw(field):
    """⚠ 2026-09-07 에 **API 가 실제로 준** 목록 안에 있는가.

    이 목록은 시점이 있는 사실이다. 구글이 새 모델을 내면 여기 없을 수 있는데,
    그때는 **API 로 확인한 뒤** 목록에 더해라 — 지우고 통과시키지 마라.
    """
    value = getattr(Settings.model_fields[field], "default")
    assert value in _KNOWN_LIVE_MODELS, (
        f"{field}={value!r} 는 2026-09-07 실측 목록에 없다.\n"
        "  새 모델이면 API 로 확인하고 _KNOWN_LIVE_MODELS 에 더해라.\n"
        "  오타면 고쳐라 — 이 시험이 막으려는 것이 그것이다."
    )


def test_voice_and_video_are_different_models():
    """⭐ 플랜 분기의 **존재 이유**를 못박는다.

    둘이 같아지면 원가 절감(Free·Pro 를 싼 모델로)이 조용히 사라진다.
    같게 만들 이유가 생기면 이 시험을 지우는 게 아니라 **결정을 문서에 적고** 지워라.
    """
    d = _defaults()
    assert Settings.model_fields["LIVE_MODEL_VOICE"].default != \
        Settings.model_fields["LIVE_MODEL_VIDEO"].default, (
        "VOICE 와 VIDEO 가 같다 — 플랜별 모델 분기가 무의미해졌다"
    )
    assert d is not None


def test_the_check_command_is_documented():
    """⛔ **실재 확인은 이 파일이 못 한다.** 그 방법을 코드 안에 남긴다.

    형태가 맞아도 없는 모델일 수 있다. 모델 이름을 바꾸면 배포 전에 이걸 돌려라:

        curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY&pageSize=200" \\
          | python -c "import json,sys; \\
              print('\\n'.join(m['name'].replace('models/','') \\
              for m in json.load(sys.stdin)['models'] \\
              if 'bidiGenerateContent' in m.get('supportedGenerationMethods',[])))"

    ⚠ AI Studio 기준이다(`USE_VERTEX=false`). Vertex 는 목록이 다르다.
    """
    assert __doc__ and "bidiGenerateContent" in test_the_check_command_is_documented.__doc__
