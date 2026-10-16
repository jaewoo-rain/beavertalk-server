"""가입 동의 기록·판정(PM-DEC-735·736 · 앱 빌드 72 동의 화면).

검증:
  A. 동의 기록이 없는 회원(신규·기존)은 /members/me 의 consent_required=True · consent_versions=현행.
  B. 필수 4항목을 현행 버전으로 보내면 항목별 한 행(버전·시각·앱 버전) · consent_required=False · 재전송은 새 행 0.
  C. 누락·모르는 항목 422 · 옛 버전 409(현행 버전 동봉) — 아무것도 안 쓴다.
  D. 필수 문서 버전이 바뀌면 다시 True · 다시 동의하면 바뀐 항목만 새 행(추가 전용 · 옛 행 보존).
  E. 온보딩·PATCH 응답도 동의 판정을 싣는다(어느 응답으로 프로필을 갱신해도 거짓으로 안 덮인다).

외부 의존 0(인메모리 sqlite).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import db.registry  # noqa: F401 - 모든 모델 등록
from db.base import Base
from domains.account.models.member import Member
from domains.account.models.member_consent import MemberConsent
from domains.account.routers import member as member_router
from domains.account.schemas.member import ConsentIn, ConsentItemIn, MemberUpdate
from domains.account.service import consent_service


@pytest.fixture()
def db():
    for t in Base.metadata.tables.values():
        if len(t.primary_key.columns) == 1:
            for pk in t.primary_key.columns:
                pk.type = Integer()
    e = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s


def _member(db: Session) -> Member:
    now = datetime.now(timezone.utc)
    m = Member(email="a@b.c", language="en", onboarding_completed=True, created_at=now, updated_at=now)
    db.add(m)
    db.commit()
    return m


def _all(versions: dict[str, str] | None = None, **kw) -> ConsentIn:
    v = versions or consent_service.CURRENT_VERSIONS
    return ConsentIn(items=[ConsentItemIn(item=i, version=x) for i, x in v.items()], **kw)


def _rows(db: Session, member_id: int) -> list[MemberConsent]:
    return list(db.scalars(select(MemberConsent).where(MemberConsent.member_id == member_id)
                           .order_by(MemberConsent.member_consent_id)))


def test_a_member_without_records_must_consent(db):
    m = _member(db)
    out = member_router.get_me(m, db)
    assert out.consent_required is True
    assert out.consent_versions == consent_service.CURRENT_VERSIONS
    assert set(out.consent_versions) == {"age14", "terms", "privacy_collect", "overseas_transfer"}


def test_b_all_four_current_items_record_one_row_each_and_resend_is_a_no_op(db):
    m = _member(db)
    out = member_router.record_consents(_all(app_version="1.0.0+72", platform="android", locale="ko"), m, db)
    assert out.consent_required is False
    rows = _rows(db, m.member_id)
    assert sorted(r.item for r in rows) == sorted(consent_service.REQUIRED_ITEMS)
    assert all(r.doc_version == consent_service.CURRENT_VERSIONS[r.item] for r in rows)
    assert all(r.app_version == "1.0.0+72" and r.platform == "android" and r.locale == "ko" for r in rows)
    assert all(r.agreed_at is not None for r in rows)
    assert member_router.get_me(m, db).consent_required is False
    member_router.record_consents(_all(), m, db)
    assert len(_rows(db, m.member_id)) == 4, "같은 버전 재전송은 새 행을 안 쓴다"


@pytest.mark.parametrize("drop", list(consent_service.CURRENT_VERSIONS))
def test_c_missing_item_is_422_and_writes_nothing(db, drop):
    m = _member(db)
    v = {k: x for k, x in consent_service.CURRENT_VERSIONS.items() if k != drop}
    with pytest.raises(HTTPException) as e:
        member_router.record_consents(_all(v), m, db)
    assert e.value.status_code == 422
    assert _rows(db, m.member_id) == []


def test_c_unknown_item_422_and_stale_version_409(db):
    m = _member(db)
    v = dict(consent_service.CURRENT_VERSIONS, marketing="v1")
    with pytest.raises(HTTPException) as e:
        member_router.record_consents(_all(v), m, db)
    assert e.value.status_code == 422
    v = dict(consent_service.CURRENT_VERSIONS, terms="2020-01-01")
    with pytest.raises(HTTPException) as e:
        member_router.record_consents(_all(v), m, db)
    assert e.value.status_code == 409
    assert e.value.detail["current_versions"] == consent_service.CURRENT_VERSIONS
    assert _rows(db, m.member_id) == []


def test_d_required_document_version_bump_asks_again_and_appends_only_changed_items(db, monkeypatch):
    m = _member(db)
    member_router.record_consents(_all(), m, db)
    bumped = dict(consent_service.CURRENT_VERSIONS, terms="2027-01-01")
    monkeypatch.setattr(consent_service, "CURRENT_VERSIONS", bumped)
    assert member_router.get_me(m, db).consent_required is True
    member_router.record_consents(_all(), m, db)
    rows = _rows(db, m.member_id)
    assert len(rows) == 5 and rows[-1].item == "terms" and rows[-1].doc_version == "2027-01-01"
    assert any(r.item == "terms" and r.doc_version == "2026-10-16" for r in rows), "옛 행은 지우지 않는다"
    assert member_router.get_me(m, db).consent_required is False


def test_e_onboarding_and_patch_responses_carry_the_consent_flag(db):
    m = _member(db)
    out = member_router.update_me(MemberUpdate(), m, db)
    assert out.consent_required is True and out.consent_versions
    member_router.record_consents(_all(), m, db)
    assert member_router.update_me(MemberUpdate(), m, db).consent_required is False
