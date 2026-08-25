"""`call_started` — 서버가 고른 캐릭터를 통화 시작에 알린다(캐스케이드).

⛔ 지금 안 깨지는데도 넣는 이유: 클라는 이 값으로 **어느 캐릭터 얼굴을 띄울지** 정하고,
없으면 앱이 고른 캐릭터로 폴백한다 — 그래서 정상처럼 보인다. 깨지는 조건은 **서버가 앱
선택과 다른 캐릭터를 고를 때**다. 캐릭터는 서버가 DB 에서 읽으므로(수신통화=알람 캐릭터,
그 외=member.character_id) 앱은 그걸 모른 채 자기 얼굴을 띄운다 ⇒ **목소리와 얼굴이 다른
캐릭터**가 되고, 에러도 안 난다.

여기서 고정하는 성질:
  ① 통화당 **정확히 1회** 나간다
  ② `character_id` 는 **DB 가 고른 그 값**이다(앱이 보낸 값도, 상수도 아니다)
  ③ 이름을 같이 싣는다 — 못 읽으면 **null 이고 통화는 계속된다**(R5)
  ④ Live 와 **같은 시점**이다: 캐릭터 확정 직후, `call_id` 확정 **전**
  ⑤ Live 의 `ServerCallStarted` 는 **안 건드린다**(필드를 더하면 Live 출력이 바뀐다)
"""

import ast
import inspect
import textwrap

import pytest

import domains.learning.realtime.cascade_session as cs
from domains.learning.realtime.cascade_protocol import CascadeCallStarted
from domains.learning.realtime.protocol import ServerCallStarted


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> None:
        self.events.append(event)

    async def send_audio(self, frame: bytes) -> None:
        pass


def _session(name_result):
    """DB 를 타지 않는 세션 — 이름 조회만 갈아 끼운다."""
    session = cs.CascadeSession(_Sink(), genai_client=object())
    session._character_id = 7
    session._session_factory = object()

    async def _run_db(_factory, fn):
        if callable(name_result):
            return name_result()
        return name_result

    session_svc_run_db = _run_db
    return session, session_svc_run_db


@pytest.mark.asyncio
async def test_announces_character_once_with_name(monkeypatch):
    """⭐ 통화당 1회 · **DB 가 고른 id** · 이름 동봉."""
    session, run_db = _session("Popo")
    monkeypatch.setattr(cs.svc, "run_db", run_db)

    await session._announce_character()

    started = [e for e in session.transport.events if e.get("type") == "call_started"]
    assert len(started) == 1, session.transport.events
    assert started[0]["character_id"] == 7, "DB 가 고른 값이 아니다"
    assert started[0]["name"] == "Popo"


@pytest.mark.asyncio
async def test_name_failure_still_sends_the_id(monkeypatch):
    """⚠ 이름을 못 읽어도 **id 는 반드시** 간다 — 얼굴을 맞추는 최소 정보다(R5)."""
    def _boom():
        raise RuntimeError("DB 죽음")

    session, run_db = _session(_boom)
    monkeypatch.setattr(cs.svc, "run_db", run_db)

    await session._announce_character()

    started = [e for e in session.transport.events if e.get("type") == "call_started"]
    assert len(started) == 1
    assert started[0]["character_id"] == 7
    assert started[0]["name"] is None, "실패했는데 이름이 실렸다"


@pytest.mark.asyncio
async def test_no_character_no_frame(monkeypatch):
    """캐릭터를 못 정했으면(데모·회원 없음) 아무것도 안 보낸다 — 거짓 id 를 지어내지 않는다."""
    session, run_db = _session("Popo")
    session._character_id = None
    monkeypatch.setattr(cs.svc, "run_db", run_db)

    await session._announce_character()

    assert [e for e in session.transport.events if e.get("type") == "call_started"] == []


def test_announced_before_call_id_like_live():
    """⭐ **Live 와 같은 시점**: 캐릭터 확정 직후, `create_call`(call_id) **전**.

    뒤에 두면 앱이 그만큼 오래 엉뚱한 얼굴을 띄운다. 순서를 소스에서 못박는다.
    """
    src = textwrap.dedent(inspect.getsource(cs.CascadeSession._load_call_context))
    tree = ast.parse(src)
    order = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = (node.func.id if isinstance(node.func, ast.Name)
                 else getattr(node.func, "attr", ""))
        if fname in ("resolve_call_character", "_announce_character", "create_call"):
            order.append((node.lineno, fname))
    seq = [n for _, n in sorted(order)]
    assert "resolve_call_character" in seq and "_announce_character" in seq, seq
    assert seq.index("resolve_call_character") < seq.index("_announce_character"), seq
    if "create_call" in seq:
        assert seq.index("_announce_character") < seq.index("create_call"), (
            "call_id 를 기다린 뒤 알린다 — 그동안 앱은 엉뚱한 얼굴을 띄운다"
        )


def test_live_model_is_not_changed_by_cascade_work():
    """⛔ **캐스케이드 작업이 Live 프레임을 건드리면 안 된다** — 그게 이 시험의 목적이다.

    ⚠ 2026-08-19: Live 에 `call_id` 가 **의도적으로** 추가됐다(이어하기). 캐스케이드가
      흘러들어온 게 아니라, 클라가 다음 조각에서 `continues_call_id` 로 돌려줄 번호가
      필요해서 Live 계약을 **일부러** 넓힌 것이다(커밋 cf5cdae).
      ⇒ 시험을 지우지 않고 **기대값을 갱신**한다. 지우면 "캐스케이드가 Live 를 오염시켰나"를
        묻는 감시가 통째로 사라진다 — 그 질문은 여전히 유효하다.
    ⚠ 2026-08-25: `diag` 가 **의도적으로** 추가됐다(클라 계측 레벨). 이것도 캐스케이드가
      흘러든 게 아니라 Live 쪽 필요다 — 계측을 **앱 재배포 없이 서버에서 끄는** 스위치가
      필요했다. 계측이 통화를 방해할 때 그것 말고는 탈출구가 없다.
      ⇒ 같은 규율: 시험을 지우지 않고 기대값을 갱신한다.
    ⛔ 다음에 이 시험이 깨지면 먼저 물어라: **의도한 Live 변경인가, 캐스케이드가 샌 것인가.**
      후자면 고쳐야 할 것은 시험이 아니라 코드다.
    """
    assert ServerCallStarted(character_id=3).model_dump() == {
        "type": "call_started", "character_id": 3, "call_id": None, "diag": None,
    }
    # ⚠ 캐스케이드 모델에는 `call_id` 가 없다 — 캐스케이드는 조각 개념이 없다.
    assert "call_id" not in CascadeCallStarted(character_id=3).model_dump()
    # 캐스케이드 전용 모델만 이름을 갖는다. wire type 은 같다(클라 변경 0).
    assert CascadeCallStarted(character_id=3).type == ServerCallStarted(character_id=3).type
    assert CascadeCallStarted(character_id=3, name="Popo").model_dump() == {
        "type": "call_started", "character_id": 3, "name": "Popo",
    }


def test_only_one_call_started_model_in_the_cascade_union():
    """⛔ 같은 `type` 이 union 에 둘이면 판별이 깨진다(call_ended 에서 겪었다)."""
    from domains.learning.realtime import cascade_protocol as cp

    import typing

    union = typing.get_args(cp.CascadeServerMessage)[0]      # Annotated[Union[...], Field]
    names = [getattr(m, "__name__", "") for m in typing.get_args(union)]
    assert "CascadeCallStarted" in names, names
    assert "ServerCallStarted" not in names, "Live 모델이 같이 들어 있다 — 판별이 깨진다"
    assert names.count("CascadeCallStarted") == 1, names
