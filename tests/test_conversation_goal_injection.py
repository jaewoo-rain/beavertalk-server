"""숙제 통화 재료 — **교사가 낸 챕터만 본다. 평소 통화는 손대지 않는다.**

지키려는 것 넷.
1) 숙제 통화는 **학습자 축을 하나도 읽지 않는다.** 숙련도 기반 공부 선별·아는 문법·
   승급 대기·기본 검출 후보 넷이 그것이다. 하나라도 새면 「반의 숙제」가 학습자마다
   달라지고 교사 화면의 수치끼리 비교가 성립하지 않는다.
2) **저쪽이 정한 순서를 지킨다.** 우선순위 앞선 것이 먼저 유도돼야 한다.
3) 과제를 못 받으면 **주입 없는 평범한 통화**가 된다. ⛔ 평소 선별로 되돌리지 않는다 —
   되돌리면 숙제가 개인 커리큘럼에 다시 붙는다.
4) **평소 통화는 B2B 를 부르지 않는다.** 숙제를 안 쓰는 사용자에게 변화가 0 이어야 하고,
   B2B 장애가 평소 통화의 시작을 늦춰서도 안 된다.
"""

from __future__ import annotations

import httpx
import pytest

from core import b2b_client
from domains.learning.service import normalcall_service


class _FakeItem:
    """`LearningItem` 대역 — 재료 조립이 읽는 칸만 갖춘다."""

    def __init__(self, item_id: int, surface: str = "표현") -> None:
        self.item_id = item_id
        self.surface = surface
        self.kind = "vocab"
        self.examples = None
        self.gen_examples = None
        self.explanation = None
        self.meanings = None
        self.reading = None


def _stub_db(rows, *, seeded: bool = True):
    """`db.scalars(...).all()` 과 `db.scalar(...)` 만 답하는 최소 대역."""

    class _Result:
        def all(self):
            return rows

    class _Db:
        def scalars(self, _stmt):
            return _Result()

        def scalar(self, _stmt):
            # 커리큘럼 미시드 방어를 통과시키는 값.
            return 1 if seeded else None

    return _Db()


@pytest.fixture()
def no_learner_axis(monkeypatch):
    """학습자 축을 전부 폭탄으로 바꾼다 — 하나라도 불리면 검사가 실패한다."""

    def _boom(name):
        def _f(*a, **k):  # pragma: no cover - 불리면 즉시 실패한다
            raise AssertionError(f"숙제 통화가 학습자 축을 읽었다: {name}")

        return _f

    for name in (
        "pick_study_items",
        "known_grammar",
        "pick_chat_targets",
        "promotion_pending",
        "load_default_candidates",
        "bridge_or_struggle_ratio",
    ):
        monkeypatch.setattr(normalcall_service.mastery_repository, name, _boom(name))


def test_숙제_재료는_과제_항목만으로_채워진다(monkeypatch, no_learner_axis):
    monkeypatch.setattr(
        b2b_client, "conversation_goal_item_ids", lambda *a, **k: [11, 12]
    )
    rows = [_FakeItem(12, "나중"), _FakeItem(11, "먼저")]  # DB 는 순서를 보장하지 않는다

    got = normalcall_service._assignment_materials(
        _stub_db(rows), member_id=1, locale="en", language="ko", assignment_id=41
    )

    # 저쪽이 정한 순서(11, 12)를 지킨다 — DB 반환 순서를 그대로 쓰지 않는다.
    assert [s["item_id"] for s in got["study_items"]] == [11, 12]
    assert [t["obj"] for t in got["known_items"]["targets"]] == ["먼저", "나중"]
    assert [c["item_id"] for c in got["candidates"]] == [11, 12]


def test_본편은_다섯_개까지고_나머지는_예비다(monkeypatch, no_learner_axis):
    """slot 은 번호가 아니라 "main"|"reserve" 다 — 전부 main 이면 한 통화가 비대해진다."""
    ids = list(range(1, 11))
    monkeypatch.setattr(b2b_client, "conversation_goal_item_ids", lambda *a, **k: ids)
    rows = [_FakeItem(i) for i in ids]

    got = normalcall_service._assignment_materials(
        _stub_db(rows), member_id=1, locale="en", language="ko", assignment_id=41
    )

    slots = [s["slot"] for s in got["study_items"]]
    assert slots.count("main") == normalcall_service.mastery_repository.STUDY_MAIN_TOTAL
    assert slots.count("reserve") == len(ids) - slots.count("main")
    # 유도 표현은 전부 싣는다 — 교사 화면의 「n / 10」이 이 수와 같아야 한다.
    assert len(got["known_items"]["targets"]) == len(ids)


def test_학습자_축_네_가지가_빠진다(monkeypatch, no_learner_axis):
    """빼는 것이 이 기능의 요점이다 — 값이 새면 숙제가 개인화된다."""
    monkeypatch.setattr(b2b_client, "conversation_goal_item_ids", lambda *a, **k: [11])

    got = normalcall_service._assignment_materials(
        _stub_db([_FakeItem(11)]), member_id=1, locale="en",
        language="ko", assignment_id=41,
    )

    # 「아는 문법」 soft 범위는 학습자 숙련도다.
    assert got["known_items"]["grammar"] is None
    # 승급은 개인 커리큘럼 축이다.
    assert got["promotion_notice"] is False
    # 검출 후보는 과제 항목뿐이다 — 기본 후보가 섞이지 않는다.
    assert [c["injected"] for c in got["candidates"]] == [True]


def test_과제를_못_받으면_주입_없는_통화가_된다(monkeypatch, no_learner_axis):
    """⛔ 평소 선별로 되돌리지 않는다 — 되돌리면 숙제가 개인 큐에 다시 붙는다."""
    monkeypatch.setattr(b2b_client, "conversation_goal_item_ids", lambda *a, **k: [])

    got = normalcall_service._assignment_materials(
        _stub_db([]), member_id=1, locale="en", language="ko", assignment_id=41
    )
    assert got == normalcall_service._EMPTY_MATERIALS


def test_받은_id_가_이_언어에_없으면_주입_없는_통화가_된다(monkeypatch, no_learner_axis):
    """언어축이 안 맞아 조회가 0건인 경우다. 남의 언어 표현을 유도하지 않는다."""
    monkeypatch.setattr(
        b2b_client, "conversation_goal_item_ids", lambda *a, **k: [11, 12]
    )

    got = normalcall_service._assignment_materials(
        _stub_db([]), member_id=1, locale="en", language="ja", assignment_id=41
    )
    assert got == normalcall_service._EMPTY_MATERIALS


def test_평소_통화는_B2B_를_부르지_않는다(monkeypatch):
    """숙제를 안 쓰는 사용자에게 변화가 0 이어야 한다. 시작이 늦어져도 안 된다."""

    def _boom(*a, **k):  # pragma: no cover - 불리면 검사가 실패한다
        raise AssertionError("평소 통화가 B2B 를 물었다")

    monkeypatch.setattr(b2b_client, "conversation_goal_item_ids", _boom)
    monkeypatch.setattr(normalcall_service, "_assignment_materials", _boom)

    repo = normalcall_service.mastery_repository
    monkeypatch.setattr(repo, "bridge_or_struggle_ratio", lambda *a, **k: 0.0)
    monkeypatch.setattr(repo, "band_of", lambda *a, **k: "survival")
    monkeypatch.setattr(repo, "pick_study_items", lambda *a, **k: [])
    monkeypatch.setattr(repo, "known_grammar", lambda *a, **k: [])
    monkeypatch.setattr(repo, "pick_chat_targets", lambda *a, **k: [])
    monkeypatch.setattr(repo, "load_default_candidates", lambda *a, **k: [])
    monkeypatch.setattr(repo, "promotion_pending", lambda *a, **k: False)

    got = normalcall_service._load_study_materials(
        _stub_db([]), member_id=1, level_no=2, locale="en",
        language="ko", assignment_id=None,
    )
    assert got["promotion_notice"] is False


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


# --------------------------------------------------------------------------- #
# 과제 통화의 언어 — 2026-09-04
#
# 09-04 실기기에서 회화 과제 통화가 **조용히 아무 일도 안 했다**(call_id=1290).
# 계정의 학습 언어가 일본어라 비버가 "오늘 일본어 공부할래…" 로 열었고, B2B 는
# 언어가 다르면 목표를 빈 배열로 돌려주므로 교사가 낸 표현 10개 중 0건이 실렸다.
# 교사 화면에는 영원히 「미수행」으로 남는다 — 로그도 안 남는다(호출은 성공했다).
# --------------------------------------------------------------------------- #


def test_과제_통화는_반_커리큘럼_언어로_건다():
    from domains.learning.realtime.call_session import _call_target_language

    assert _call_target_language("ja", 2) == "ko"
    assert _call_target_language(None, 2) == "ko"
    assert _call_target_language("ko", 2) == "ko"


def test_평소_통화는_학습자_언어_그대로다():
    """⛔ 이 예외는 과제 통화에만 걸린다. 평소 통화까지 ko 로 끌면 다국어가 죽는다."""
    from domains.learning.realtime.call_session import _call_target_language

    assert _call_target_language("ja", None) == "ja"
    assert _call_target_language("fr", None) == "fr"
    assert _call_target_language(None, None) is None
