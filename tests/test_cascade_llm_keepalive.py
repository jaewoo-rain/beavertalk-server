"""대답 LLM 커넥션을 **살려 둔다** — 턴마다 TLS 를 새로 맺던 ~185ms 를 없앤다.

## 근거 (2026-08-15)
말끝 → 앱에서 소리 2,354ms 중 서버가 2,026ms. `첫소리` 분해 중앙값:
    LLM첫조각 844 · 문장완성 206 · 묶음대기 82 · 벤더TTS 819 · 송출 16
⇒ 벤더 왕복 2개(LLM 844 + TTS 819 = 1,663ms)가 전체의 71%. 그중 커넥션 재수립이 ~185ms
  (페어드 A/B 8쌍 **전부** 개선).

## ⛔ 이 기능의 진짜 위험은 성능이 아니라 **먹통**이다
유휴 커넥션을 GFE 가 먼저 끊으면 그 턴은 **빈 스트림**이 된다 — 개시도 종료도 정상인데
텍스트가 0 자다. 예외가 아니라서 `failed` 도 안 서고, 호출부는 그냥 말을 안 한다.
사용자에겐 **먹통**이고 로그에는 아무 흔적이 없었다.
⇒ 그래서 ①창을 30초로 짧게 잡고 ②`gemini_chat` 이 빈 스트림을 **시끄럽게** 남긴다.

## 그리고 조용히 무효화되는 길
google-genai 2.10 은 `async_client_args` 를 `httpx.AsyncClient` 로 넘긴다(설치된 소스로
확인). **그런데 aiohttp 가 설치돼 있으면 async 경로가 aiohttp 로 바뀌고 이 인자는 무시된다.**
⇒ 그 경우 **부팅 로그로 경고**한다. 오늘 하루 우리를 가장 많이 태운 게 "설정했는데 안 먹는
   값"이라, 같은 걸 우리 손으로 만들지 않는다.
"""

import logging

import pytest

import main as app_main


def test_keepalive_is_off_when_the_setting_is_zero():
    """⚠ 0 = 예전 동작. 되돌아가는 길을 막지 않는다."""
    assert app_main._keepalive_http_options(0) is None
    assert app_main._keepalive_http_options(-1) is None


def test_the_limits_reach_the_async_client_args():
    """⭐ 값이 **실제로 실리는 자리**를 못박는다 — 이름만 맞고 안 실리면 아무 효과가 없다."""
    options = app_main._keepalive_http_options(30.0)
    assert options is not None
    limits = options.async_client_args["limits"]
    assert limits.keepalive_expiry == 30.0


def test_aiohttp_silently_disables_it_and_we_say_so(monkeypatch, caplog):
    """⛔⛔ **조용히 안 먹는 상태를 만들지 않는다.**

    aiohttp 가 (전이 의존으로라도) 들어오면 `_use_aiohttp` 가 켜지고 httpx limits 는
    통째로 무시된다. 그때 아무 말도 안 하면 "keepalive 를 켰는데 왜 안 빨라지지"를
    처음부터 파게 된다 — 오늘 `silence_duration_ms` 에서 당한 그 유형이다.
    """
    from google.genai import _api_client as genai_api_client

    monkeypatch.setattr(genai_api_client, "has_aiohttp", True, raising=False)
    with caplog.at_level(logging.WARNING, logger="main"):
        assert app_main._keepalive_http_options(30.0) is None

    assert [r for r in caplog.records if "안 먹는다" in r.getMessage()], caplog.text


def test_the_window_is_short_enough_to_be_safe():
    """⛔ 30초를 넘기지 마라 — 유휴 커넥션이 끊기면 그 턴이 **빈 스트림**(먹통)이 된다.

    ⚠ 이 시험의 주제는 성능이 아니라 **안전**이다. 값을 올리려는 사람은 먼저
      `cascade llm: ⚠ 빈 스트림` 건수를 봐야 한다.
    """
    from core.config import settings

    assert 0 < settings.CASCADE_LLM_KEEPALIVE_S <= 30.0, settings.CASCADE_LLM_KEEPALIVE_S


@pytest.mark.asyncio
async def test_an_empty_stream_is_loud(caplog):
    """⛔ **개시·종료 모두 정상인데 텍스트가 0 자**인 회차를 시끄럽게 남긴다.

    예외가 아니므로 `failed` 는 안 선다 — 그래서 이 줄이 없으면 흔적이 0 이다.
    """
    from core import gemini_chat

    class _Client:
        class aio:
            class models:
                @staticmethod
                async def generate_content_stream(**kwargs):
                    async def _gen():
                        return
                        yield None      # pragma: no cover - 타입만 맞춘다
                    return _gen()

    chat = gemini_chat.ChatStream(_Client(), "m", {})
    with caplog.at_level(logging.INFO, logger="core.gemini_chat"):
        assert [piece async for piece in chat.chunks()] == []

    assert not chat.failed, "예외가 아닌데 실패로 표시했다"
    warned = [r for r in caplog.records if "빈 스트림" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING, caplog.text


@pytest.mark.asyncio
async def test_a_normal_stream_stays_quiet(caplog):
    """⛔ 정상이면 조용하다 — 정상까지 경고하면 아무도 경고를 안 본다."""
    from core import gemini_chat

    class _Resp:
        text = "안녕하세요"
        usage_metadata = None
        candidates = []

    class _Client:
        class aio:
            class models:
                @staticmethod
                async def generate_content_stream(**kwargs):
                    async def _gen():
                        yield _Resp()
                    return _gen()

    chat = gemini_chat.ChatStream(_Client(), "m", {})
    with caplog.at_level(logging.INFO, logger="core.gemini_chat"):
        assert [piece async for piece in chat.chunks()] == ["안녕하세요"]

    assert not [r for r in caplog.records if "빈 스트림" in r.getMessage()], caplog.text
