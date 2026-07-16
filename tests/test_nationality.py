"""core.nationality 단위 테스트 (외부 호출 없음 — httpx.Client 전부 모킹).

검증 대상(실패모드표):
- URL 미설정 → None (앱 무영향, R5).
- 정상 predictions 응답 → body dict 그대로 반환.
- predictions=null(no_speech) / 빈 리스트 → None.
- 빈 오디오 → None.
- 타임아웃 → 1회 재시도 후 None(총 2회 시도).
- 5xx → 1회 재시도 후 None(총 2회 시도).
- 4xx → 재시도 없이 None(총 1회 시도).
- 임의 예외 미전파 → None.

⚠ 실제 외부 서버로 네트워크 호출하지 않는다. httpx.Client 를 FakeClient 로 교체한다.
"""

from __future__ import annotations

import httpx
import pytest

import core.nationality as natl


# ──────────────────────────────────────────────────────────────────────────
# 가짜 httpx 계층
# ──────────────────────────────────────────────────────────────────────────
class _FakeRequest:
    pass


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.request = _FakeRequest()

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=self.request, response=self
            )


def _make_client_factory(responses, calls):
    """지정한 응답/예외 시퀀스를 순서대로 내는 FakeClient 팩토리.

    responses 항목: _FakeResponse 인스턴스 또는 raise 할 예외 인스턴스.
    calls: 호출 횟수·인자 기록용 리스트.
    """
    seq = list(responses)

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, params=None, files=None):
            calls.append({"url": url, "params": params, "files": files})
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    return _FakeClient


@pytest.fixture(autouse=True)
def _reset_warned():
    """URL 미설정 warning 플래그를 테스트마다 초기화."""
    natl._warned_no_url = False
    yield
    natl._warned_no_url = False


def _set_url(monkeypatch, url="http://nat.example:8000"):
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_URL", url, raising=False)
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_TOP_K", 3, raising=False)
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_TIMEOUT_S", 20.0, raising=False)
    # 재시도 백오프를 0 으로 (테스트 속도)
    monkeypatch.setattr(natl, "_RETRY_BACKOFF_S", 0.0, raising=False)


# ──────────────────────────────────────────────────────────────────────────
# 테스트
# ──────────────────────────────────────────────────────────────────────────
def test_no_url_returns_none(monkeypatch):
    """URL 미설정이면 None (네트워크 호출 없음)."""
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_URL", None, raising=False)
    # httpx.Client 가 호출되면 실패로 간주(호출돼선 안 됨)
    monkeypatch.setattr(natl.httpx, "Client", _boom_client())
    assert natl.predict_nationality(b"xxxx") is None


def test_empty_audio_returns_none(monkeypatch):
    """빈 오디오면 URL 이 있어도 None."""
    _set_url(monkeypatch)
    monkeypatch.setattr(natl.httpx, "Client", _boom_client())
    assert natl.predict_nationality(b"") is None


def test_success_returns_body_as_is(monkeypatch):
    """정상 predictions 응답은 body dict 를 그대로 반환한다."""
    _set_url(monkeypatch)
    body = {
        "predictions": [
            {"country": "Korea", "iso": "KR", "prob": 0.9},
            {"country": "Japan", "iso": "JP", "prob": 0.07},
            {"country": "China", "iso": "CN", "prob": 0.03},
        ],
        "top1": "Korea",
        "duration_sec": 12.3,
        "latency_ms": 430,
    }
    calls: list = []
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([_FakeResponse(200, body)], calls)
    )
    out = natl.predict_nationality(b"pcmpcm", audio_type="wav")
    assert out == body
    assert len(calls) == 1
    # multipart field 이름과 top_k 쿼리 검증
    assert "file" in calls[0]["files"]
    fname, fbytes, mime = calls[0]["files"]["file"]
    assert fname == "audio.wav"
    assert fbytes == b"pcmpcm"
    assert mime == "audio/wav"
    assert calls[0]["params"] == {"top_k": 3}
    assert calls[0]["url"].endswith("/predict")


def test_no_speech_returns_none(monkeypatch):
    """predictions=null(no_speech_detected) → None (재시도 안 함)."""
    _set_url(monkeypatch)
    body = {"predictions": None, "top1": None, "reason": "no_speech_detected"}
    calls: list = []
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([_FakeResponse(200, body)], calls)
    )
    assert natl.predict_nationality(b"pcm") is None
    assert len(calls) == 1  # no_speech 는 재시도 없음


def test_empty_predictions_list_returns_none(monkeypatch):
    """predictions 가 빈 리스트여도 None."""
    _set_url(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        natl.httpx,
        "Client",
        _make_client_factory([_FakeResponse(200, {"predictions": []})], calls),
    )
    assert natl.predict_nationality(b"pcm") is None


def test_timeout_retries_then_none(monkeypatch):
    """타임아웃은 1회 재시도 후 None(총 2회 시도)."""
    _set_url(monkeypatch)
    calls: list = []
    to = httpx.TimeoutException("read timeout")
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([to, to], calls)
    )
    assert natl.predict_nationality(b"pcm") is None
    assert len(calls) == 2  # 재시도 1회


def test_timeout_then_success(monkeypatch):
    """첫 시도 타임아웃 → 재시도 성공 시 body 반환."""
    _set_url(monkeypatch)
    body = {"predictions": [{"country": "Korea", "iso": "KR", "prob": 1.0}], "top1": "Korea"}
    calls: list = []
    monkeypatch.setattr(
        natl.httpx,
        "Client",
        _make_client_factory([httpx.ConnectTimeout("boom"), _FakeResponse(200, body)], calls),
    )
    out = natl.predict_nationality(b"pcm")
    assert out == body
    assert len(calls) == 2


def test_5xx_retries_then_none(monkeypatch):
    """5xx 는 1회 재시도 후 None(총 2회 시도)."""
    _set_url(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        natl.httpx,
        "Client",
        _make_client_factory([_FakeResponse(503), _FakeResponse(500)], calls),
    )
    assert natl.predict_nationality(b"pcm") is None
    assert len(calls) == 2


def test_4xx_no_retry_returns_none(monkeypatch):
    """4xx 는 재시도하지 않고 None(총 1회 시도)."""
    _set_url(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([_FakeResponse(400)], calls)
    )
    assert natl.predict_nationality(b"pcm") is None
    assert len(calls) == 1  # 4xx 는 재시도 없음


def test_arbitrary_exception_not_propagated(monkeypatch):
    """어떤 예외든 전파되지 않고 None."""
    _set_url(monkeypatch)

    class _ExplodingClient:
        def __init__(self, *a, **k):
            raise RuntimeError("unexpected")

    monkeypatch.setattr(natl.httpx, "Client", _ExplodingClient)
    assert natl.predict_nationality(b"pcm") is None


def test_mp3_mime(monkeypatch):
    """audio_type=mp3 → audio/mpeg MIME."""
    _set_url(monkeypatch)
    body = {"predictions": [{"country": "Korea", "iso": "KR", "prob": 1.0}], "top1": "Korea"}
    calls: list = []
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([_FakeResponse(200, body)], calls)
    )
    natl.predict_nationality(b"pcm", audio_type="mp3")
    _, _, mime = calls[0]["files"]["file"]
    assert mime == "audio/mpeg"


# ──────────────────────────────────────────────────────────────────────────
# 헬퍼: 절대 호출돼선 안 되는 FakeClient (호출 시 테스트 실패)
# ──────────────────────────────────────────────────────────────────────────
def _boom_client():
    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("httpx.Client 가 호출되면 안 됨")

    return _Boom
