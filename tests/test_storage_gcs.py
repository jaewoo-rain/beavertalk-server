"""core.storage (GCS 어댑터) 단위 테스트.

실제 GCS 없이 모듈 전역(_bucket/_ready/_signing_creds/_signer_email)을 fake 로 주입해
공개 API(upload/public_url/signed_url) 계약과 graceful 폴백을 검증한다.
호출부(normalcall/sentence/review)는 이 API 를 그대로 소비하므로 계약이 곧 회귀 방어선.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from core import storage


# --------------------------------------------------------------------------- #
# fake GCS blob/bucket
# --------------------------------------------------------------------------- #
class _FakeBlob:
    def __init__(self, name, require_token=False):
        self.name = name
        self.require_token = require_token  # True = private key 없음(signBlob 필요) 시뮬레이션
        self.uploaded = None
        self.content_type = None
        self.signed_kwargs = None

    def upload_from_string(self, data, content_type="application/octet-stream"):
        self.uploaded = data
        self.content_type = content_type

    def generate_signed_url(self, **kwargs):
        # 로컬 private key 서명 불가 상황: access_token 없이 부르면 라이브러리처럼 예외.
        if self.require_token and "access_token" not in kwargs:
            raise RuntimeError("you need a private key to sign credentials")
        self.signed_kwargs = kwargs
        return f"https://signed.example/{self.name}"


class _FakeBucket:
    def __init__(self, require_token=False):
        self.blobs = {}
        self.require_token = require_token

    def blob(self, name):
        b = self.blobs.get(name) or _FakeBlob(name, require_token=self.require_token)
        self.blobs[name] = b
        return b


@pytest.fixture
def fake_bucket(monkeypatch):
    """storage 를 '초기화 완료 + 버킷 주입' 상태로 — private key 로컬 서명 경로(키파일 SA)."""
    bucket = _FakeBucket(require_token=False)
    monkeypatch.setattr(storage, "_ready", True)
    monkeypatch.setattr(storage, "_bucket", bucket)
    monkeypatch.setattr(storage, "_client", object())
    monkeypatch.setattr(storage, "_signing_creds", None)
    monkeypatch.setattr(storage, "_signer_email", None)
    return bucket


# --------------------------------------------------------------------------- #
# _blob_name — prefix 합성
# --------------------------------------------------------------------------- #
def test_blob_name_composes_prefix_and_path():
    assert storage._blob_name("voice-recordings", "calls/4/279/0001_user.mp3") == \
        "voice-recordings/calls/4/279/0001_user.mp3"


def test_blob_name_strips_slashes():
    assert storage._blob_name("/voice-samples/", "/tts/1/1.mp3") == "voice-samples/tts/1/1.mp3"


def test_blob_name_no_prefix():
    assert storage._blob_name("", "a/b.mp3") == "a/b.mp3"


# --------------------------------------------------------------------------- #
# upload
# --------------------------------------------------------------------------- #
def test_upload_returns_path_and_writes_composed_blob(fake_bucket):
    key = storage.upload("voice-recordings", "calls/1/2/0003_beaver.mp3", b"\x00\x01", "audio/mpeg")
    # 반환값은 path(버킷 상대 key) — DB(voice_url)에 저장되는 값.
    assert key == "calls/1/2/0003_beaver.mp3"
    blob = fake_bucket.blobs["voice-recordings/calls/1/2/0003_beaver.mp3"]
    assert blob.uploaded == b"\x00\x01"
    assert blob.content_type == "audio/mpeg"


def test_upload_empty_data_returns_none(fake_bucket):
    assert storage.upload("voice-recordings", "x.mp3", b"") is None


def test_upload_graceful_none_when_unconfigured(monkeypatch):
    # _init 를 no-op 로 두고 _bucket=None → 자격증명/버킷 부재 시 graceful None(R5).
    monkeypatch.setattr(storage, "_ready", True)
    monkeypatch.setattr(storage, "_bucket", None)
    assert storage.upload("voice-recordings", "x.mp3", b"data") is None


def test_upload_swallows_exception(fake_bucket, monkeypatch):
    def boom(name):
        raise RuntimeError("gcs down")

    monkeypatch.setattr(fake_bucket, "blob", boom)
    assert storage.upload("voice-recordings", "x.mp3", b"data") is None


# --------------------------------------------------------------------------- #
# signed_url / public_url
# --------------------------------------------------------------------------- #
def test_signed_url_v4_get(fake_bucket):
    url = storage.signed_url("voice-recordings", "calls/1/2/0003_beaver.mp3", expires_in=600)
    assert url == "https://signed.example/voice-recordings/calls/1/2/0003_beaver.mp3"
    blob = fake_bucket.blobs["voice-recordings/calls/1/2/0003_beaver.mp3"]
    assert blob.signed_kwargs["version"] == "v4"
    assert blob.signed_kwargs["method"] == "GET"
    assert blob.signed_kwargs["expiration"] == timedelta(seconds=600)
    # signer 없으면 access_token/service_account_email 은 넘기지 않음(로컬 ADC 경로).
    assert "access_token" not in blob.signed_kwargs


def test_public_url_uses_long_ttl(fake_bucket):
    from core.config import settings

    url = storage.public_url("voice-samples", "tts/1/1.mp3")
    assert url == "https://signed.example/voice-samples/tts/1/1.mp3"
    blob = fake_bucket.blobs["voice-samples/tts/1/1.mp3"]
    assert blob.signed_kwargs["expiration"] == timedelta(seconds=settings.GCS_SIGNED_URL_PUBLIC_TTL)


def test_signed_url_none_path_returns_none(fake_bucket):
    assert storage.signed_url("voice-recordings", None) is None
    assert storage.public_url("voice-samples", None) is None


def test_signed_url_local_key_path_no_signblob(fake_bucket):
    # 키파일 SA(private key 보유) — 로컬 서명이 1순위, signBlob 인자 안 넘김.
    storage.signed_url("voice-recordings", "a.mp3")
    blob = fake_bucket.blobs["voice-recordings/a.mp3"]
    assert "access_token" not in blob.signed_kwargs
    assert "service_account_email" not in blob.signed_kwargs


def test_signed_url_falls_back_to_signblob_when_no_private_key(monkeypatch):
    # private key 없음(Cloud Run compute SA 등) → 1순위 실패 → signBlob 폴백(토큰 전달).
    bucket = _FakeBucket(require_token=True)
    monkeypatch.setattr(storage, "_ready", True)
    monkeypatch.setattr(storage, "_bucket", bucket)

    class _Creds:
        valid = True
        token = "ya29.fake"

    monkeypatch.setattr(storage, "_signer_email", "sa@proj.iam.gserviceaccount.com")
    monkeypatch.setattr(storage, "_signing_creds", _Creds())
    url = storage.signed_url("voice-recordings", "a.mp3")
    assert url == "https://signed.example/voice-recordings/a.mp3"
    blob = bucket.blobs["voice-recordings/a.mp3"]
    assert blob.signed_kwargs["service_account_email"] == "sa@proj.iam.gserviceaccount.com"
    assert blob.signed_kwargs["access_token"] == "ya29.fake"


def test_signed_url_no_private_key_no_creds_returns_none(monkeypatch):
    # private key 없고 signBlob 자격증명도 없으면 graceful None(로컬 사용자 ADC 상황).
    bucket = _FakeBucket(require_token=True)
    monkeypatch.setattr(storage, "_ready", True)
    monkeypatch.setattr(storage, "_bucket", bucket)
    monkeypatch.setattr(storage, "_signer_email", None)
    monkeypatch.setattr(storage, "_signing_creds", None)
    assert storage.signed_url("voice-recordings", "a.mp3") is None


def test_signed_url_swallows_exception(fake_bucket, monkeypatch):
    def boom(name):
        raise RuntimeError("sign fail")

    monkeypatch.setattr(fake_bucket, "blob", boom)
    assert storage.signed_url("voice-recordings", "a.mp3") is None


# --------------------------------------------------------------------------- #
# object_key / playback_url — 저장값 정규화 (2026-08-30 서명 만료 결함 대응)
# --------------------------------------------------------------------------- #
def test_object_key_passthrough_for_plain_key():
    # 정상 행은 이미 key 다 — 손대지 않는다.
    assert storage.object_key("voice-samples", "tts/992/820.mp3") == "tts/992/820.mp3"


def test_object_key_none_and_empty():
    assert storage.object_key("voice-samples", None) is None
    assert storage.object_key("voice-samples", "") is None


def test_object_key_extracts_from_gcs_signed_url():
    # 운영에서 실제로 저장돼 있던 형태(만료된 V4 서명 URL).
    url = (
        "https://storage.googleapis.com/beavertalk-app-audio"
        "/voice-samples/tts/992/820.mp3"
        "?X-Goog-Algorithm=GOOG4-RSA-SHA256&X-Goog-Expires=604800"
    )
    assert storage.object_key("voice-samples", url) == "tts/992/820.mp3"


def test_object_key_extracts_from_legacy_supabase_url():
    # GCS 이전(2026-07-15) 이전 세대의 URL 도 같은 규칙으로 풀린다.
    url = "https://proj.supabase.co/storage/v1/object/public/voice-samples/tts/1/2.mp3"
    assert storage.object_key("voice-samples", url) == "tts/1/2.mp3"


def test_object_key_without_prefix_strips_bucket_only():
    # prefix 가 경로에 없으면 버킷명만 걷어낸다(방어적 폴백).
    url = "https://storage.googleapis.com/beavertalk-app-audio/orphan/3.mp3"
    assert storage.object_key("voice-samples", url) == "orphan/3.mp3"


def test_playback_url_resigns_stored_url(fake_bucket):
    # ★ 회귀 방어선 — 저장된 URL 을 그대로 돌려주면 안 된다. 매번 새로 서명한다.
    stored = (
        "https://storage.googleapis.com/beavertalk-app-audio"
        "/voice-samples/tts/992/820.mp3?X-Goog-Expires=604800"
    )
    url = storage.playback_url("voice-samples", stored, 3600)
    assert url == "https://signed.example/voice-samples/tts/992/820.mp3"
    assert url != stored


def test_playback_url_none_when_no_value(fake_bucket):
    assert storage.playback_url("voice-samples", None) is None
