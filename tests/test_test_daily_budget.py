"""PM-DEC-887 — TEST_DAILY_BUDGET_S 스위치."""
from core.config import settings
from domains.learning.service import call_service


def test_default_keeps_plan_table(monkeypatch):
    monkeypatch.setattr(settings, "TEST_DAILY_BUDGET_S", 0)
    assert call_service.daily_budget_s("premium") == 900
    assert call_service.daily_budget_s(None) == 300


def test_override_on_test_env(monkeypatch):
    monkeypatch.setattr(settings, "TEST_DAILY_BUDGET_S", 36000)
    monkeypatch.setattr(settings, "ENV", "test")
    assert call_service.daily_budget_s("premium") == 36000
    assert call_service.daily_budget_s(None) == 36000


def test_override_ignored_on_prod(monkeypatch):
    monkeypatch.setattr(settings, "TEST_DAILY_BUDGET_S", 36000)
    monkeypatch.setattr(settings, "ENV", "prod")
    assert call_service.daily_budget_s("premium") == 900
