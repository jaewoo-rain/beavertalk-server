"""시험 태그 전용 MEMORY_IGNORE_TEST_EMAILS(PM-DEC-958) — 빈 값이면 늘 False(운영 기본)."""
from domains.learning.service import normalcall_service as svc


class _DB:
    def __init__(self, email):
        self.email = email
        self.calls = 0

    def scalar(self, _stmt):
        self.calls += 1
        return self.email


def test_member_email_listed():
    db = _DB("max09@test.com")
    assert svc.member_email_listed(db, 1, "") is False and db.calls == 0      # 빈 값이면 DB 도 안 본다
    assert svc.member_email_listed(db, 1, "max02@test.com, MAX09@test.com") is True
    assert svc.member_email_listed(db, 1, "max02@test.com") is False
    assert svc.member_email_listed(_DB(None), 1, "max09@test.com") is False
