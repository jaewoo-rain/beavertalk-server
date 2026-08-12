"""OpenAI STT 에 넘기는 언어 코드는 **ISO-639-1** 이다 — 2026-08-13 통화 사망 버그 회귀.

## 무슨 일이 있었나
에뮬 실통화가 **1.4초 만에** 죽었고 앱에 이 토스트가 떴다:

    Invalid value: 'en-US'. Supported values are: 'af','ar','az',…

원인 사슬(전부 로그·코드로 확인):
    ① `core/stt.py` 는 Google STT v2 용으로 **BCP-47**(`en-US`) 목록을 만든다.
       v2 가 지역을 요구하므로 지역 없는 `ja` 는 **폐기**된다("인식 언어 코드 폐기 ['ja']")
       ⇒ 남은 코드가 **1개**가 된다
    ② OpenAI 어댑터는 "코드가 정확히 1개면 그 값을 그대로" 넘겼다
    ③ ⇒ `en-US` 를 보냈다. OpenAI 는 **ISO-639-1**(`en`)만 받는다
    ④ 거절 → error 프레임 → 앱이 스낵바를 띄우고 **통화 종료**

⚠ **간헐적이라 더 나빴다**: 목록이 2개면 `None`(자동감지)이 나가 정상이다 — **1개로 좁아질
  때만** 터진다. 재현이 안 돼 원인 불명으로 남기 딱 좋은 모양이었다.

## 여기서 고정하는 성질
  ① 코드 1개 + 지역 있음 → **지역을 뗀 ISO-639-1** 로 나간다(`en-US` → `en`)
  ② 코드 2개 → `None`(자동감지) 유지 — OpenAI 는 언어를 하나만 받는다
  ③ 정규화 후 **중복이 합쳐져 1개**가 되면 그 하나를 보낸다(자동감지로 안 떨어뜨린다)
  ④ ⛔ **Google 경로는 BCP-47 그대로** — 두 벤더의 요구가 반대다. 그게 이 사고의 뿌리다
  ⑤ 벤더가 거절하면 **서버 로그에 WARNING** 이 남는다(지금까지 조용히 error 만 나갔다)
"""

import logging

from core.openai_stt import OpenAiRealtimeSttStream, openai_language_codes
from core.stt import normalize_language_codes


# ── ①②③ 변환 규칙 ────────────────────────────────────────────────────────
def test_a_single_regional_code_loses_its_region():
    """⭐ 이 한 줄이 통화를 죽였다 — `en-US` 를 그대로 보내면 OpenAI 가 거절한다."""
    assert openai_language_codes(["en-US"]) == ["en"]


def test_two_languages_stay_two():
    """② 두 언어면 그대로 둘이다(호출부가 그때 자동감지를 고른다)."""
    assert openai_language_codes(["en-US", "ko-KR"]) == ["en", "ko"]


def test_duplicates_after_normalizing_are_merged():
    """③ `en-US`+`en-GB` → `en` **하나**. 그건 자동감지를 잃는 게 아니라 사실 그대로다.

    두 항목이 가리키던 언어가 하나였을 뿐이고, OpenAI 에 `en` 을 주는 편이 자동감지보다 정확하다.
    """
    assert openai_language_codes(["en-US", "en-GB"]) == ["en"]


def test_already_iso_codes_pass_through():
    """지역 없는 코드는 그대로다 — Google 이 폐기하는 `ja` 도 OpenAI 에는 유효하다."""
    assert openai_language_codes(["ja"]) == ["ja"]
    assert openai_language_codes([]) == []
    assert openai_language_codes(None) == []


# ── 스트림이 실제로 무엇을 들고 가나 ──────────────────────────────────────
def test_the_stream_sends_iso_for_one_code():
    """① 코드 1개 → ISO-639-1 이 `language` 로 나간다."""
    assert OpenAiRealtimeSttStream(16_000, ["en-US"])._language == "en"


def test_the_stream_sends_none_for_two_codes():
    """② 코드 2개 → `None`(자동감지). OpenAI 는 언어를 하나만 받는다."""
    assert OpenAiRealtimeSttStream(16_000, ["en-US", "ko-KR"])._language is None


def test_the_stream_sends_the_merged_language():
    """③ 중복이 합쳐져 1개가 되면 **그 하나**를 보낸다(자동감지로 안 떨어진다)."""
    assert OpenAiRealtimeSttStream(16_000, ["en-US", "en-GB"])._language == "en"


def test_no_codes_means_auto_detect():
    assert OpenAiRealtimeSttStream(16_000, [])._language is None
    assert OpenAiRealtimeSttStream(16_000, None)._language is None


def test_the_normalization_is_logged(caplog):
    """⚠ 조용히 바꾸지 않는다 — 무엇을 받아 무엇으로 보냈는지 남는다."""
    with caplog.at_level(logging.INFO, logger="core.openai_stt"):
        OpenAiRealtimeSttStream(16_000, ["en-US"])
    assert [r for r in caplog.records if "언어 코드 정규화" in r.getMessage()], caplog.text


# ── ④ 두 벤더의 요구가 반대다 ─────────────────────────────────────────────
def test_the_google_path_keeps_bcp47():
    """⛔ Google 은 **지역까지** 요구한다 — 여기를 ISO 로 바꾸면 그쪽이 죽는다.

    변환은 **벤더 어댑터에서만** 한다. 공용 목록을 한쪽 취향으로 바꾸면 반대쪽이 깨진다.
    """
    assert normalize_language_codes(["en-US", "ko-KR"]) == ["en-US", "ko-KR"]
    # 지역 없는 코드는 Google 경로에서 폐기된다 — 그래서 목록이 1개로 좁아졌다(이 사고의 ①).
    assert "ja" not in normalize_language_codes(["ja", "en-US"])


# ── ⑤ 거절이 서버 로그에 남는다 ───────────────────────────────────────────
def test_a_vendor_rejection_is_logged_as_warning(caplog):
    """⛔⛔ **서버가 먼저 알아야 한다.** 지금까지 이 거절은 WARNING 한 줄 없이 나갔다.

    앱은 스낵바를 띄우고 통화를 끊는데 서버는 조용했다 — 그래서 이 버그를 못 봤다.
    """
    stream = OpenAiRealtimeSttStream(16_000, ["en-US"])
    with caplog.at_level(logging.INFO, logger="core.openai_stt"):
        events = stream._translate({"type": "error", "error": {"message": "Invalid value: 'en-US'"}})

    assert events and events[0].detail.startswith("Invalid value")
    warned = [r for r in caplog.records if "벤더 거절" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING, caplog.text
    assert "en-US" in warned[0].getMessage()
