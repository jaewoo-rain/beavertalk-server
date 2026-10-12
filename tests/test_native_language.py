"""모국어 칸 분리(member.native_language) — 2026-10-12 PM-DEC-513·514 · F066.

- 온보딩: native_language 가 오면 그 값 · 없고 language 만 오면(빌드 58 이하) language 로 채움.
- PATCH language(앱 언어)는 모국어를 건드리지 않는다 — F066 회귀 잠금.
- PATCH native_language 는 모국어만 바꾼다 · null·빈 값은 「변경 없음」.
- 두 칸 모두 ISO 639-1 로 정규화("ko-KR"→"ko").
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Integer, create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from db.registry import Base
from domains.account.models.member import Member
from domains.account.schemas.member import MemberRead, MemberUpdate, MyPageOut, OnboardingIn


class _FakeMember:
    def __init__(self) -> None:
        self.language = None
        self.native_language = None
        self.target_language = None
        self.actual_nationality = None
        self.name = None
        self.onboarding_completed = False
        self.reasons: list = []


class _FakeDb:
    def commit(self) -> None: ...
    def refresh(self, _obj) -> None: ...


def _service_with(member):
    from domains.account.service.member_service import MemberService

    svc = MemberService.__new__(MemberService)
    svc.db = _FakeDb()
    svc.get = lambda _mid: member  # type: ignore[method-assign]
    return svc


def test_dtos_expose_native_language():
    for model in (MemberRead, MemberUpdate, MyPageOut, OnboardingIn):
        assert "native_language" in model.model_fields, model.__name__


def test_onboarding_language_only_fills_native_too():
    """빌드 58 이하 — 온보딩 언어 화면(「모국어가 무엇인가요?」)의 값이 language 로만 온다."""
    m = _FakeMember()
    _service_with(m).onboarding(1, "Ana", None, "vi")
    assert m.language == "vi"
    assert m.native_language == "vi"


def test_onboarding_both_fields_keep_their_own_values_normalized():
    m = _FakeMember()
    _service_with(m).onboarding(1, "Ana", None, "en", native_language="ko-KR")
    assert m.language == "en"
    assert m.native_language == "ko"


def test_onboarding_name_only_keeps_native():
    """이름 변경(updateName)도 온보딩 엔드포인트를 탄다 — 모국어를 지우거나 바꾸면 안 된다."""
    m = _FakeMember()
    m.language, m.native_language = "en", "vi"
    _service_with(m).onboarding(1, "Bao", None, None)
    assert (m.language, m.native_language) == ("en", "vi")


def test_onboarding_empty_native_is_no_change():
    m = _FakeMember()
    m.native_language = "vi"
    _service_with(m).onboarding(1, None, None, "en", native_language="")
    assert m.language == "en"
    assert m.native_language == "vi"


def test_patch_language_keeps_native_f066():
    """F066 — 설정 「사용자 언어」를 영어로 바꿔도 모국어는 그대로."""
    m = _FakeMember()
    m.language, m.native_language = "vi", "vi"
    _service_with(m).update(1, MemberUpdate(language="en"))
    assert m.language == "en"
    assert m.native_language == "vi"


def test_patch_native_changes_only_native_and_null_is_no_change():
    m = _FakeMember()
    m.language, m.native_language = "en", "en"
    svc = _service_with(m)
    svc.update(1, MemberUpdate(native_language="mn-MN"))
    assert (m.language, m.native_language) == ("en", "mn")
    svc.update(1, MemberUpdate(native_language=None))
    svc.update(1, MemberUpdate(native_language=""))
    assert m.native_language == "mn"


def test_orm_column_and_read_dto():
    """모델에 칸이 매핑되고 /members/me 응답(MemberRead)에 실린다."""
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    e = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    Base.metadata.create_all(e)
    assert "native_language" in {c["name"] for c in inspect(e).get_columns("member")}
    now = datetime.now(timezone.utc)
    with Session(e) as db:
        db.add(Member(email="a@b.c", language="en", native_language="vi",
                      onboarding_completed=True, created_at=now, updated_at=now))
        db.commit()
        m = db.scalar(select(Member))
        dto = MemberRead.model_validate(m)
    assert (dto.language, dto.native_language) == ("en", "vi")
