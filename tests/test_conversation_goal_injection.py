"""과제 통화의 회화 목표 주입 — B2B 서비스에 묻고, 못 받으면 평소대로.

지키려는 것 셋.
1) 목표를 받으면 **그것으로 갈아끼운다.** 안 그러면 학습자가 교사가 낸 표현을
   쓸 이유가 통화 안에 없어 `conversation_met` 이 구조적으로 낮게 나온다.
2) **저쪽이 정한 순서를 지킨다.** 우선순위 앞선 것이 먼저 유도돼야 한다.
3) 실패·빈 응답·설정 부재는 전부 **평소 선별로 폴백**한다. B2B 장애가 전화
   자체를 끊으면 안 된다 — 회화 목표는 통화의 성립 조건이 아니라 재료다.
"""

from __future__ import annotations

import httpx
import pytest

from core import b2b_client
from domains.learning.service import normalcall_service


class _FakeItem:
    """`LearningItem` 대역 — 이 검사는 id 와 순서만 본다."""

    def __init__(self, item_id: int) -> None:
        self.item_id = item_id


def _stub_db(rows):
    """`db.scalars(...).all()` 만 답하는 최소 대역."""

    class _Result:
        def all(self):
            return rows

    class _Db:
        def scalars(self, _stmt):
            return _Result()

    return _Db()


@pytest.fixture()
def fallback_marker(monkeypatch):
    """평소 선별이 불렸는지 알 수 있게 표식 하나를 돌려준다."""
    marker = [_FakeItem(999)]
    monkeypatch.setattr(
        normalcall_service.mastery_repository,
        "pick_chat_targets",
        lambda *a, **k: marker,
    )
    return marker


def test_과제_목표를_받으면_평소_선별을_대체한다(monkeypatch, fallback_marker):
    monkeypatch.setattr(
        b2b_client, "conversation_goal_item_ids", lambda *a, **k: [11, 12]
    )
    rows = [_FakeItem(12), _FakeItem(11)]  # DB 는 순서를 보장하지 않는다

    got = normalcall_service._conversation_targets(
        _stub_db(rows), member_id=1, level_no=2, language="ko", assignment_id=41
    )

    # 저쪽이 정한 순서(11, 12)를 지킨다 — DB 반환 순서를 그대로 쓰지 않는다.
    assert [i.item_id for i in got] == [11, 12]
    assert got is not fallback_marker


def test_목표가_비면_평소_선별로_되돌아간다(monkeypatch, fallback_marker):
    monkeypatch.setattr(b2b_client, "conversation_goal_item_ids", lambda *a, **k: [])

    got = normalcall_service._conversation_targets(
        _stub_db([]), member_id=1, level_no=2, language="ko", assignment_id=41
    )
    assert got is fallback_marker


def test_받은_id_가_이_언어에_없으면_평소_선별로_되돌아간다(monkeypatch, fallback_marker):
    """언어축이 안 맞아 조회가 0건이면 빈 유도로 두지 않는다."""
    monkeypatch.setattr(
        b2b_client, "conversation_goal_item_ids", lambda *a, **k: [11, 12]
    )

    got = normalcall_service._conversation_targets(
        _stub_db([]), member_id=1, level_no=2, language="ja", assignment_id=41
    )
    assert got is fallback_marker


def test_설정이_없으면_묻지도_않고_빈_목록이다(monkeypatch):
    monkeypatch.setattr(b2b_client.settings, "B2B_API_BASE_URL", None)
    monkeypatch.setattr(b2b_client.settings, "B2B_SERVICE_TOKEN", "t")

    def _boom(*a, **k):  # pragma: no cover - 불리면 검사가 실패한다
        raise AssertionError("설정이 없는데 HTTP 를 쳤다")

    monkeypatch.setattr(httpx, "Client", _boom)
    assert b2b_client.conversation_goal_item_ids(1, assignment_id=41) == []


def test_B2B_가_죽어도_통화를_막지_않는다(monkeypatch):
    """타임아웃·5xx·파싱 실패는 전부 빈 목록이다 — 예외가 위로 새면 전화가 끊긴다."""
    monkeypatch.setattr(b2b_client.settings, "B2B_API_BASE_URL", "http://b2b")
    monkeypatch.setattr(b2b_client.settings, "B2B_SERVICE_TOKEN", "t")

    class _Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise httpx.ConnectTimeout("b2b down")

    monkeypatch.setattr(httpx, "Client", lambda **k: _Boom())
    assert b2b_client.conversation_goal_item_ids(1, assignment_id=41) == []


def test_토큰을_헤더로_싣고_과제로_좁힌다(monkeypatch):
    monkeypatch.setattr(b2b_client.settings, "B2B_API_BASE_URL", "http://b2b/")
    monkeypatch.setattr(b2b_client.settings, "B2B_SERVICE_TOKEN", "s3cret")
    seen: dict = {}

    class _Ok:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            seen.update(url=url, params=params, headers=headers)
            return httpx.Response(
                200, json={"item_ids": [7, 8], "assignment_ids": [41]},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(httpx, "Client", lambda **k: _Ok())

    assert b2b_client.conversation_goal_item_ids(72, assignment_id=41) == [7, 8]
    # 후행 슬래시가 겹치지 않는다.
    assert seen["url"] == "http://b2b/api/v1/internal/members/72/conversation-goals"
    assert seen["params"]["assignment_id"] == 41
    assert seen["headers"]["X-Service-Token"] == "s3cret"
