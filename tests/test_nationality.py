"""core.nationality 단위 테스트 (외부 호출 없음 — httpx.Client 전부 모킹).

검증 대상(실패모드표 — 2026-09-14 GPU 서버 계약):
- URL 미설정 → None (앱 무영향, R5).
- 정상 top5 응답 → 내부 형태({"predictions":[{country,iso,prob}], ...})로 정규화해 반환.
- 요청: multipart field `audio`, 쿼리파라미터 없음, 키가 있으면 X-API-Key.
- 400 no_speech 등 / 빈 top5 → None(재시도 없음).
- 빈 오디오 → None.
- 타임아웃·5xx → 최대 2회 재시도 후 None(총 3회 시도).
- 그 외 4xx(401·422) → 재시도 없이 None.
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

        def post(self, url, files=None, headers=None, **kwargs):
            calls.append({"url": url, "files": files, "headers": headers, **kwargs})
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


def _set_url(monkeypatch, url="http://nat.example:8000", api_key=None):
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_URL", url, raising=False)
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_KEY", api_key, raising=False)
    monkeypatch.setattr(natl.settings, "NATIONALITY_API_TIMEOUT_S", 20.0, raising=False)
    # 재시도 백오프를 0 으로 (테스트 속도) — 시도 횟수는 그대로 3회
    monkeypatch.setattr(natl, "_RETRY_BACKOFFS_S", (0.0, 0.0), raising=False)


def _ok_body(top5=None) -> dict:
    return {
        "top5": top5
        if top5 is not None
        else [
            {"label": "Korea", "prob": 0.9},
            {"label": "Japan", "prob": 0.07},
            {"label": "China", "prob": 0.03},
        ],
        "sec": 12.0,
        "encode_ms": 17,
        "total_ms": 89,
    }


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


def test_success_normalizes_to_internal_shape(monkeypatch):
    """top5 응답은 nationality_service 가 읽는 내부 형태로 정규화된다."""
    _set_url(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([_FakeResponse(200, _ok_body())], calls)
    )
    out = natl.predict_nationality(b"pcmpcm", audio_type="wav")
    assert out == {
        "predictions": [
            {"country": "Korea", "iso": "KR", "prob": 0.9},
            {"country": "Japan", "iso": "JP", "prob": 0.07},
            {"country": "China", "iso": "CN", "prob": 0.03},
        ],
        "top1": "Korea",
        "duration_sec": 12.0,
        "latency_ms": 89,
    }
    assert len(calls) == 1
    # multipart field 이름·쿼리파라미터 부재·헤더 검증
    assert set(calls[0]["files"]) == {"audio"}
    fname, fbytes, mime = calls[0]["files"]["audio"]
    assert fname == "audio.wav"
    assert fbytes == b"pcmpcm"
    assert mime == "audio/wav"
    assert "params" not in calls[0]
    assert calls[0]["headers"] is None
    assert calls[0]["url"].endswith("/predict")


def test_api_key_header_sent_and_stripped(monkeypatch):
    """NATIONALITY_API_KEY 가 있으면 X-API-Key 로 싣는다(개행·공백 제거)."""
    _set_url(monkeypatch, api_key="  s3cr3t\n")
    calls: list = []
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([_FakeResponse(200, _ok_body())], calls)
    )
    natl.predict_nationality(b"pcm")
    assert calls[0]["headers"] == {"X-API-Key": "s3cr3t"}


def test_labels_whose_names_differ_get_iso(monkeypatch):
    """Russia·United Kingdom·Hong Kong 은 iso 로 붙는다. 모르는 라벨은 iso=None 으로 남긴다."""
    _set_url(monkeypatch)
    body = _ok_body(
        [
            {"label": "Russia", "prob": 0.5},
            {"label": "United Kingdom", "prob": 0.3},
            {"label": "Hong Kong", "prob": 0.1},
            {"label": "Atlantis", "prob": 0.1},
        ]
    )
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([_FakeResponse(200, body)], [])
    )
    out = natl.predict_nationality(b"pcm")
    assert [p["iso"] for p in out["predictions"]] == ["RU", "GB", "HK", None]


def test_label_table_covers_all_41_classes():
    """서버 라벨은 41개다(nationality-api-guide.md §4). 표가 빠지면 iso 조회가 조용히 틀린다."""
    assert len(natl._LABEL_ISO) == 41
    assert len(set(natl._LABEL_ISO.values())) == 41


@pytest.mark.parametrize("code", ["no_speech", "too_short", "decode_failed"])
def test_400_audio_codes_return_none_without_retry(monkeypatch, code):
    """400 no_speech 등 → None (재시도 안 함)."""
    _set_url(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        natl.httpx,
        "Client",
        _make_client_factory([_FakeResponse(400, {"error": "x", "code": code})], calls),
    )
    assert natl.predict_nationality(b"pcm") is None
    assert len(calls) == 1


def test_empty_top5_returns_none(monkeypatch):
    """top5 가 빈 리스트여도 None."""
    _set_url(monkeypatch)
    monkeypatch.setattr(
        natl.httpx,
        "Client",
        _make_client_factory([_FakeResponse(200, _ok_body([]))], []),
    )
    assert natl.predict_nationality(b"pcm") is None


def test_timeout_retries_twice_then_none(monkeypatch):
    """타임아웃은 2회 재시도 후 None(총 3회 시도)."""
    _set_url(monkeypatch)
    calls: list = []
    to = httpx.TimeoutException("read timeout")
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([to, to, to], calls)
    )
    assert natl.predict_nationality(b"pcm") is None
    assert len(calls) == 3


def test_timeout_then_success(monkeypatch):
    """첫 시도 타임아웃 → 재시도 성공 시 정규화 결과 반환."""
    _set_url(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        natl.httpx,
        "Client",
        _make_client_factory([httpx.ConnectTimeout("boom"), _FakeResponse(200, _ok_body())], calls),
    )
    out = natl.predict_nationality(b"pcm")
    assert out is not None and out["top1"] == "Korea"
    assert len(calls) == 2


def test_5xx_retries_twice_then_none(monkeypatch):
    """5xx 는 2회 재시도 후 None(총 3회 시도)."""
    _set_url(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        natl.httpx,
        "Client",
        _make_client_factory([_FakeResponse(503), _FakeResponse(500), _FakeResponse(502)], calls),
    )
    assert natl.predict_nationality(b"pcm") is None
    assert len(calls) == 3


@pytest.mark.parametrize("status", [401, 422])
def test_other_4xx_no_retry_returns_none(monkeypatch, status):
    """401(키)·422(형식) 은 재시도하지 않고 None(총 1회 시도)."""
    _set_url(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([_FakeResponse(status)], calls)
    )
    assert natl.predict_nationality(b"pcm") is None
    assert len(calls) == 1


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
    calls: list = []
    monkeypatch.setattr(
        natl.httpx, "Client", _make_client_factory([_FakeResponse(200, _ok_body())], calls)
    )
    natl.predict_nationality(b"pcm", audio_type="mp3")
    _, _, mime = calls[0]["files"]["audio"]
    assert mime == "audio/mpeg"


# ──────────────────────────────────────────────────────────────────────────
# 헬퍼: 절대 호출돼선 안 되는 FakeClient (호출 시 테스트 실패)
# ──────────────────────────────────────────────────────────────────────────
def _boom_client():
    class _Boom:
        def __init__(self, *a, **k):
            raise AssertionError("httpx.Client 가 호출되면 안 됨")

    return _Boom
