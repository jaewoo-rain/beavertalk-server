"""Live 토큰 만료 방어 회귀 테스트.

버그: genai.Client 는 lifespan 이 한 번 만들어 공유하고 그 SA access token 은
~1시간 만료다. REST 는 요청마다 갱신되지만 Live 의 WS connect 는 만료 토큰을
그대로 보내 1008(invalid auth)로 죽었다. 수정: open_session 이 connect 직전에
공유 creds 가 만료됐으면 강제 갱신한다(_ensure_fresh_credentials).

이 테스트는 1시간을 기다리지 않고, 만료된 creds 를 강제로 만들어 (a) 갱신이
호출되는지, (b) connect 시점엔 이미 유효 토큰인지(=connect 전에 갱신), (c) 유효할
땐 갱신하지 않는지, (d) api_key 클라이언트/갱신 실패에도 안전한지 검증한다.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core import gemini_live


class _FakeCreds:
    """google-auth Credentials 흉내: valid 플래그 + refresh 카운터."""

    def __init__(self, *, valid: bool, raises: bool = False) -> None:
        self.valid = valid
        self.refreshed = 0
        self._raises = raises

    def refresh(self, request: object) -> None:  # noqa: D401 - 인터페이스 흉내
        self.refreshed += 1
        if self._raises:
            raise RuntimeError("boom")
        self.valid = True  # 갱신되면 유효해진다


def _client(creds: object | None) -> object:
    """genai.Client 의 creds 접근 경로(_api_client._credentials)만 흉내."""
    return SimpleNamespace(_api_client=SimpleNamespace(_credentials=creds))


# ── _ensure_fresh_credentials 단위 ──────────────────────────────────────────
def test_expired_creds_are_refreshed() -> None:
    creds = _FakeCreds(valid=False)
    asyncio.run(gemini_live._ensure_fresh_credentials(_client(creds)))
    assert creds.refreshed == 1
    assert creds.valid is True


def test_valid_creds_are_not_refreshed() -> None:
    creds = _FakeCreds(valid=True)
    asyncio.run(gemini_live._ensure_fresh_credentials(_client(creds)))
    assert creds.refreshed == 0


def test_api_key_client_without_credentials_is_noop() -> None:
    # AI Studio(api_key) 클라이언트엔 _credentials 가 없다 → 예외 없이 스킵.
    asyncio.run(gemini_live._ensure_fresh_credentials(_client(None)))
    asyncio.run(gemini_live._ensure_fresh_credentials(SimpleNamespace()))


def test_refresh_failure_is_swallowed() -> None:
    # 갱신이 실패해도 여기서 죽지 않는다(connect 가 authoritative).
    creds = _FakeCreds(valid=False, raises=True)
    asyncio.run(gemini_live._ensure_fresh_credentials(_client(creds)))
    assert creds.refreshed == 1  # 시도는 했다


# ── open_session 통합: connect 전에 갱신되는가 ───────────────────────────────
class _FakeLiveCM:
    """aio.live.connect 가 돌려주는 async 컨텍스트매니저. __aenter__ 시점의
    creds.valid 를 기록해 '갱신이 connect 보다 먼저 일어났는지' 증명한다."""

    def __init__(self, creds: _FakeCreds, record: dict) -> None:
        self._creds = creds
        self._record = record

    async def __aenter__(self) -> object:
        self._record["valid_at_connect"] = self._creds.valid
        return object()  # GeminiLiveSession 은 저장만 하므로 아무 객체나 OK

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _full_client(creds: _FakeCreds, record: dict) -> object:
    live = SimpleNamespace(connect=lambda *, model, config: _FakeLiveCM(creds, record))
    return SimpleNamespace(
        _api_client=SimpleNamespace(_credentials=creds),
        aio=SimpleNamespace(live=live),
    )


def test_open_session_refreshes_before_connect() -> None:
    creds = _FakeCreds(valid=False)
    record: dict = {}
    client = _full_client(creds, record)
    settings = SimpleNamespace(GEMINI_LIVE_MODEL="fake-model")

    async def run() -> None:
        async with gemini_live.open_session(
            client, settings, system_instruction="안녕", voice="Fenrir"
        ):
            pass

    asyncio.run(run())
    assert creds.refreshed == 1
    # connect 진입 시점에 이미 유효 → 만료 토큰이 나가지 않는다(1008 방지의 핵심).
    assert record["valid_at_connect"] is True
