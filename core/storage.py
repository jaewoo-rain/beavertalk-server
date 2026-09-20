"""GCS(Google Cloud Storage) 오디오 업로드/URL 어댑터 (normalcall) — graceful.

통화 원본 음성·표현 TTS·연습 녹음을 **단일 비공개 버킷**(settings.GCS_AUDIO_BUCKET)에
올리고 object key 를 반환한다. 자격증명 부재·패키지 미설치·임의 예외를 모두 흡수해
None 을 반환한다(speechsuper.py 규율) — 저장이 안 돼도 통화/분석은 죽지 않는다(R5).

설계(Supabase Storage → GCS 이전):
    - 버킷 1개(비공개, uniform bucket-level access, public 차단). 과거 Supabase 의
      voice-samples/voice-recordings 2버킷은 이제 **폴더 prefix** 로만 남는다
      (호출부는 그대로 그 이름을 `bucket` 인자로 넘긴다 → 최종 key = "prefix/path").
    - DB(voice_url 컬럼)에는 **object key(path, 버킷 상대 경로)만** 저장하고, 재생 URL 은
      V4 signed URL 로 그때그때 조립한다(만료 O — 매 요청 재발급). 공개/서명 구분 없이 전부 서명.
    - Cloud Run 서명: 런타임 SA 엔 private key 가 없어 IAM signBlob 로 서명한다
      (SA 에 roles/iam.serviceAccountTokenCreator 필요 — 자기 자신 대상). 자격증명은 ADC.

공개 API(upload/public_url/signed_url)의 시그니처는 이전 Supabase 버전과 동일 —
호출부(normalcall/sentence/review service) 및 테스트 스텁이 그대로 동작한다.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

_client: "Any | None" = None
_bucket: "Any | None" = None
_ready = False
_signing_creds: "Any | None" = None
_signer_email: str | None = None


def _init() -> None:
    """GCS 클라이언트·버킷·서명 자격증명을 lazy 초기화(실패 시 비활성, 1회 경고)."""
    global _client, _bucket, _ready, _signing_creds, _signer_email
    if _ready:
        return

    _ready = True
    bucket_name = settings.GCS_AUDIO_BUCKET
    if not bucket_name:
        logger.warning("storage(gcs): GCS_AUDIO_BUCKET 미설정 → 업로드 비활성(voice_url=None).")
        return
    try:
        import google.auth
        from google.cloud import storage as gcs

        # cloud-platform 스코프: Storage 업로드 + IAM signBlob(서명) 모두 필요.
        creds, adc_project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        project = settings.GCP_PROJECT or adc_project
        _client = gcs.Client(project=project, credentials=creds)
        _bucket = _client.bucket(bucket_name)
        _signing_creds = creds
        # SA 자격증명이면 service_account_email 존재 → signBlob 경유 V4 서명 가능.
        # 사용자 ADC(로컬)면 None → 서명 실패 시 graceful None(업로드 자체는 동작).
        _signer_email = getattr(creds, "service_account_email", None)
        logger.info(
            "storage(gcs): 초기화 완료 bucket=%s project=%s signer=%s",
            bucket_name, project, _signer_email,
        )
    except Exception as exc:  # noqa: BLE001 - 미설치/자격증명/임의 예외 graceful
        logger.warning("storage(gcs): 비활성(업로드 스킵) — %s", exc)
        _client = _bucket = None


def _blob_name(bucket: str, path: str) -> str:
    """과거 Supabase 버킷명을 폴더 prefix 로 합성 → 단일 버킷 내 object 이름."""
    prefix = (bucket or "").strip("/")
    p = (path or "").lstrip("/")
    return f"{prefix}/{p}" if prefix else p


def upload(bucket: str, path: str, data: bytes, content_type: str = "application/octet-stream") -> str | None:
    """data 를 (prefix=bucket)/path 로 업로드하고 object key(path)를 반환한다(graceful None).

    반환값은 이전 Supabase 버전과 동일하게 **path(버킷 상대 key)** — 호출부는 이 key 를
    DB(voice_url)에 저장하고, 재생 시 signed_url(bucket, key) 로 URL 을 조립한다.
    동일 경로 재업로드는 덮어쓰기(재시도 안전). 실패 시 None.
    """
    if not data:
        return None
    _init()
    if _bucket is None:
        return None
    try:
        name = _blob_name(bucket, path)
        blob = _bucket.blob(name)
        blob.upload_from_string(data, content_type=content_type)
        logger.info("storage(gcs): 업로드 성공 %s (%d bytes)", name, len(data))
        return path
    except Exception as exc:  # noqa: BLE001 - 업로드 실패 graceful
        logger.warning("storage(gcs): 업로드 실패(무시, None) %s/%s — %s", bucket, path, exc)
        return None


def _signed(bucket: str, path: str | None, expires_in: int) -> str | None:
    """(prefix=bucket)/path 객체의 V4 signed GET URL. path 없거나 실패 시 None.

    2단 서명 전략:
      1순위 — 자격증명에 private key 가 있으면 **로컬 서명**(키파일 SA, 예: Vertex 키 재사용).
              추가 IAM(signBlob) 불필요.
      2순위 — private key 가 없으면(Cloud Run compute SA/impersonated) **IAM signBlob** 로 서명.
              service_account_email + access_token 을 넘겨 iamcredentials.signBlob 을 태운다
              (SA 에 roles/iam.serviceAccountTokenCreator 필요, 토큰 ~1h 만료 시 refresh).
    """
    if not path:
        return None
    _init()
    if _bucket is None:
        return None
    try:
        blob = _bucket.blob(_blob_name(bucket, path))
        exp = timedelta(seconds=expires_in)
        try:
            # 1순위: 로컬 private key 서명(키파일 SA). 키가 없으면 라이브러리가 예외.
            return blob.generate_signed_url(version="v4", expiration=exp, method="GET")
        except Exception:  # noqa: BLE001 - private key 부재 → signBlob 폴백
            token, email = _signing_token(), _signer_email
            if not (token and email):
                raise
            return blob.generate_signed_url(
                version="v4", expiration=exp, method="GET",
                service_account_email=email, access_token=token,
            )
    except Exception as exc:  # noqa: BLE001 - 서명 실패 graceful
        logger.warning("storage(gcs): signed_url 실패 %s/%s — %s", bucket, path, exc)
        return None


def _signing_token() -> str | None:
    """signBlob 폴백용 access token(만료 시 refresh). 자격증명 없으면 None."""
    if _signing_creds is None:
        return None
    try:
        from google.auth.transport.requests import Request

        if not getattr(_signing_creds, "valid", False):
            _signing_creds.refresh(Request())
    except Exception:  # noqa: BLE001 - 갱신 실패는 token 유무로 흡수
        pass
    return getattr(_signing_creds, "token", None)


def public_url(bucket: str, path: str | None) -> str | None:
    """과거 public 버킷용 API — 단일 비공개 버킷이라 장기 signed URL 로 대체(기본 7일).

    캐릭터 프리뷰·표현 TTS 처럼 자주 재생되는 자산용. DB 엔 key 만 저장하므로 만료돼도
    다음 요청에서 재발급된다.
    """
    return _signed(bucket, path, settings.GCS_SIGNED_URL_PUBLIC_TTL)


def signed_url(bucket: str, path: str | None, expires_in: int = 3600) -> str | None:
    """private 자산(통화 원본·연습 녹음)의 서명 재생 URL(기본 1시간). path 없거나 실패 시 None."""
    return _signed(bucket, path, expires_in)


def object_key(bucket: str, stored: str | None) -> str | None:
    """DB 에 담긴 값을 object key(버킷 상대 경로)로 정규화한다.

    이 모듈의 계약은 「DB 엔 key 만 저장하고 URL 은 매 요청 조립」이다. 정상 행은 이미
    key 이므로 그대로 돌려준다. 다만 **전체 URL 을 저장해 버린 행**이 실재한다
    (2026-08-30 실측: sentence.voice_url — 만료된 서명 URL 이 영구 반환되고 있었다).
    그런 행은 경로를 되짚어 key 를 뽑아 **읽는 즉시 재서명되게** 한다.

    두 세대의 URL 을 모두 흡수한다.
      - GCS  : https://storage.googleapis.com/{버킷}/{prefix}/{key}?X-Goog-...
      - 과거 Supabase: {SUPABASE_URL}/storage/v1/object/public/{prefix}/{key}
    prefix 세그먼트를 찾아 그 뒤를 key 로 본다. 못 찾으면 버킷명만 걷어낸 경로를 쓴다.
    """
    if not stored:
        return None
    if not stored.startswith(("http://", "https://")):
        return stored  # 이미 key — 정상 경로

    from urllib.parse import unquote, urlparse

    segments = [seg for seg in unquote(urlparse(stored).path).split("/") if seg]
    prefix = (bucket or "").strip("/")
    if prefix and prefix in segments:
        # prefix 뒤가 곧 key. 두 세대 URL 이 같은 규칙으로 풀린다.
        segments = segments[segments.index(prefix) + 1:]
    else:
        gcs_bucket = (settings.GCS_AUDIO_BUCKET or "").strip("/")
        if gcs_bucket and segments and segments[0] == gcs_bucket:
            segments = segments[1:]
    return "/".join(segments) or None


def playback_url(bucket: str, stored: str | None, expires_in: int | None = None) -> str | None:
    """저장값(key 또는 과거 전체 URL) → **지금 서명한** 재생 URL. 실패 시 None.

    호출부는 DB 값을 그대로 내보내지 말고 반드시 이 함수를 거친다 — 저장된 URL 을
    그대로 돌려주면 서명이 만료된 뒤 영구히 재생 불가가 된다.
    """
    key = object_key(bucket, stored)
    if not key:
        return None
    if expires_in is None:
        return public_url(bucket, key)
    return signed_url(bucket, key, expires_in)
