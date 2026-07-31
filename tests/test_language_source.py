"""학습 언어 DB 단일 소스화 + 모국어(locale) 정규화 회귀.

근거: docs/20260728_0125_학습언어-DB-단일소스화와-모국어-정규화.md

배경(실측): 활성 24명 중 8명이 잘못된 모국어로 통화 중이었다 — 앱이 BCP-47 id("ko-KR")를
그대로 PATCH 하는데 서버의 모국어 라벨 표는 ISO 639-1 키만 갖고 있어 **영어로 폴백**했다.
학습 언어는 앱 SharedPreferences 가 원본이라 복원이 끝나기 전에 통화가 시작되면 저장값
대신 기본 'ko' 가 실려 나갔다(잠금화면 수신통화가 그 구간).
"""

from __future__ import annotations

import core.persona_prompt as pp
from core.languages import normalize_locale
from domains.account.schemas.member import MemberRead, MemberUpdate, MyPageOut


# --------------------------------------------------------------------------- #
# 1) normalize_locale — BCP-47/POSIX/대문자를 ISO 639-1 로
# --------------------------------------------------------------------------- #
def test_normalize_locale_strips_region_and_lowercases():
    assert normalize_locale("ko-KR") == "ko"
    assert normalize_locale("en_US") == "en"
    assert normalize_locale("JA") == "ja"
    assert normalize_locale("  zh  ") == "zh"
    assert normalize_locale("ko") == "ko"


def test_normalize_locale_keeps_missing_as_none():
    """빈 값은 None 그대로 — 호출부의 '미상' 폴백을 가로채면 안 된다."""
    assert normalize_locale(None) is None
    assert normalize_locale("") is None
    assert normalize_locale("   ") is None


def test_normalized_locale_resolves_to_korean_label():
    """정규화 전에는 'ko-KR' 이 영어로 폴백했다 — 그게 실서비스 버그였다."""
    fallback = pp._LOCALE_LABEL[pp._DEFAULT_LOCALE]
    assert pp._LOCALE_LABEL.get("ko-KR", fallback) == fallback  # 정규화 없으면 영어
    assert pp._LOCALE_LABEL[normalize_locale("ko-KR")] == "한국어"  # 정규화 후 한국어


# --------------------------------------------------------------------------- #
# 2) DTO 노출 — 프론트가 DB 값을 읽어 마이페이지에 표시할 수 있어야 한다
# --------------------------------------------------------------------------- #
def test_member_dtos_expose_target_language():
    for model in (MemberRead, MemberUpdate, MyPageOut):
        assert "target_language" in model.model_fields, model.__name__


def test_member_update_target_language_is_optional():
    """부분 수정 DTO — 안 보내면 미설정(기존 값 보존)."""
    data = MemberUpdate(language="ko")
    assert "target_language" not in data.model_dump(exclude_unset=True)


# --------------------------------------------------------------------------- #
# 3) 저장 시 정규화 — 오염을 입구에서 막는다
# --------------------------------------------------------------------------- #
class _FakeMember:
    def __init__(self) -> None:
        self.language = None
        self.target_language = None
        self.name = None
        self.onboarding_completed = False
        self.reasons: list = []


class _FakeDb:
    def commit(self) -> None: ...
    def refresh(self, _obj) -> None: ...


def _service_with(member: _FakeMember):
    from domains.account.service.member_service import MemberService

    svc = MemberService.__new__(MemberService)
    svc.db = _FakeDb()
    svc.get = lambda _mid: member  # type: ignore[method-assign]
    return svc


def test_update_normalizes_both_language_fields():
    member = _FakeMember()
    _service_with(member).update(1, MemberUpdate(language="ko-KR", target_language="JA"))
    assert member.language == "ko"
    assert member.target_language == "ja"


def test_onboarding_normalizes_language():
    member = _FakeMember()
    _service_with(member).onboarding(1, "Alex", None, "ko-KR")
    assert member.language == "ko"
