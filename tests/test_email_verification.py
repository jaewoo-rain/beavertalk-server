"""EmailVerificationService 단위 테스트 (sqlite + 코드/메일 모킹).

가입 인증은 제거됐고, 인증 엔진은 비밀번호 재설정(pwreset)에서만 쓰인다.
엔진 동작(발급→검증→소비, 오답/만료/시도초과)을 PWRESET 경로로 검증한다.
발급은 회원 존재 의존을 피하려 내부 `_issue` 를 직접 호출한다.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import domains.account.service.email_verification_service as evs
from core.config import settings
from db.registry import Base
from domains.account.models.email_verification import PURPOSE_PWRESET
from domains.account.service.email_verification_service import (
    EmailVerificationService,
)

EMAIL = "v@bt.io"
SUBJECT = "[BeaverTalk] 비밀번호 재설정 코드"


def _issue(svc: EmailVerificationService) -> None:
    """테스트용 코드 발급(회원 존재 의존 없이 엔진만 구동)."""
    svc._issue(EMAIL, PURPOSE_PWRESET, SUBJECT)


@pytest.fixture()
def db():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    monkeypatch.setattr(evs, "generate_code", lambda: "1234")
    monkeypatch.setattr(evs, "send_email", lambda *a, **k: None)
    # 재발송 간격은 테스트에서 방해되지 않게 0으로.
    monkeypatch.setattr(settings, "EMAIL_CODE_RESEND_SECONDS", 0)


def test_send_then_verify_then_consume(db):
    svc = EmailVerificationService(db)
    _issue(svc)
    svc.verify_code(EMAIL, PURPOSE_PWRESET, "1234")
    assert svc.consume_verified(EMAIL, PURPOSE_PWRESET) is True
    # 소비 후에는 다시 없음.
    assert svc.consume_verified(EMAIL, PURPOSE_PWRESET) is False


def test_wrong_code_increments_and_rejects(db):
    svc = EmailVerificationService(db)
    _issue(svc)
    with pytest.raises(Exception):
        svc.verify_code(EMAIL, PURPOSE_PWRESET, "0000")
    # 틀린 뒤 verified 가 아니므로 소비 불가.
    assert svc.consume_verified(EMAIL, PURPOSE_PWRESET) is False


def test_max_attempts_locks(db, monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_CODE_MAX_ATTEMPTS", 3)
    svc = EmailVerificationService(db)
    _issue(svc)
    for _ in range(3):
        with pytest.raises(Exception):
            svc.verify_code(EMAIL, PURPOSE_PWRESET, "0000")
    # 시도 초과 후엔 정답이어도 거부.
    with pytest.raises(Exception):
        svc.verify_code(EMAIL, PURPOSE_PWRESET, "1234")


def test_expired_code_rejected(db):
    svc = EmailVerificationService(db)
    _issue(svc)
    row = svc.repo.get(EMAIL, PURPOSE_PWRESET)
    row.expires_at = svc._now() - timedelta(minutes=1)  # 강제 만료
    db.commit()
    with pytest.raises(Exception):
        svc.verify_code(EMAIL, PURPOSE_PWRESET, "1234")


def test_unverified_cannot_be_consumed(db):
    svc = EmailVerificationService(db)
    _issue(svc)  # 발급만, 검증 안 함
    assert svc.consume_verified(EMAIL, PURPOSE_PWRESET) is False
