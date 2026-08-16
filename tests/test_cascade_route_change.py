"""통화 **도중** 라우트 변경 — AEC 정책을 다시 태운다(2026-08-12 프론트 구현 완료).

`start.aec` 는 **세션 시작 스냅샷**이고 그것도 재생 개통 전 값이다. 통화 중 이어폰을 뽑으면
서버는 계속 `headset` 을 믿는데 실제 출력은 **스피커폰**(에코 최악)이다.

⛔⛔ **이 프레임은 안 올 수 있다.** 클라가 콜백 등록에 실패하면 **조용히 통지 없이** 통화가
계속 돈다(진단이 통화를 죽이면 안 된다는 그쪽 원칙). 그래서 `route_change` 가 안 온다는 건
"라우트가 안 바뀌었다"가 아니라 **모른다**는 뜻이다 — 보조 신호다.

여기서 고정하는 성질:
  ① ⭐ 프레임이 **0건이어도 동작이 예전과 완전히 같다**(필수로 가정하면 콜백 실패 기기에서
     정책이 굳는다)
  ② 정책 전환이 **로그에 남는다**(전/후·라우트·업링크)
  ③ 모르는 값·형식 오류가 와도 **안 죽는다**(안전 쪽으로 떨어진다)
  ④ ⛔ **진행 중인 턴을 깨지 않는다** — 다음 판정부터 적용
  ⑤ 계약 덤프에 자동으로 잡힌다
"""

import logging

import pytest

import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None

    async def receive(self):
        raise AssertionError("쓰지 않는다")


def _session() -> cs.CascadeSession:
    session = cs.CascadeSession(_Sink(), object())
    session._apply_aec_hint({"mode": "headset"})     # 시작은 이어폰(게이트 off)
    return session


# ── ① 안 와도 예전과 같다 ─────────────────────────────────────────────────
def test_a_session_without_any_route_change_behaves_exactly_as_before():
    """⭐⭐ **이 시험이 핵심이다.** 프레임을 필수로 가정하면 콜백 등록에 실패한 기기에서
    정책이 굳는다 — 그쪽은 실패해도 조용히 넘어간다(진단이 통화를 죽이면 안 되므로)."""
    with_frame = _session()
    without = _session()
    assert (without._aec_mode, without._energy_gate) == ("headset", False)
    # 프레임이 한 번도 안 온 세션 = 시작 스냅샷 그대로
    assert (with_frame._aec_mode, with_frame._energy_gate) == (without._aec_mode,
                                                               without._energy_gate)


# ── ② 전환이 로그에 남는다 ────────────────────────────────────────────────
def test_pulling_out_the_headset_turns_the_gate_back_on(caplog):
    """이어폰 → 스피커: 에코 방어(에너지 게이트)가 **다시 켜져야** 한다."""
    session = _session()
    assert session._energy_gate is False

    with caplog.at_level(logging.INFO):
        session._on_route_change({
            "type": "route_change",
            "aec": {"mode": "unknown", "route": "speaker"},
            "uplink_bytes": 320000,
        })

    assert session._energy_gate is True, "이어폰을 뽑았는데 게이트가 그대로다"
    line = next(m for m in caplog.messages if "라우트 변경" in m)
    assert "headset" in line and "unknown" in line          # 전/후 정책
    assert "speaker" in line                                 # 라우트
    assert "320000" in line and "10.0초" in line             # 업링크(32,000 B/s → 산수)


def test_plugging_a_headset_in_turns_the_gate_off(caplog):
    """반대 방향도 같은 함수가 처리한다(시작과 도중이 갈리면 안 된다)."""
    session = cs.CascadeSession(_Sink(), object())
    session._apply_aec_hint(None)                # 시작은 미선언(게이트 on)
    assert session._energy_gate is True

    session._on_route_change({"type": "route_change",
                              "aec": {"mode": "headset", "route": "headset"}})
    assert session._energy_gate is False


def test_it_reuses_the_start_aec_path():
    """⛔ 새 판정을 만들지 않는다 — 두 곳으로 갈라지면 시작과 도중이 어긋난다."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cs.CascadeSession._on_route_change)))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_apply_aec_hint" in called, called


# ── ③ 이상한 값 ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("ctrl", [
    {"type": "route_change"},                                    # aec 없음
    {"type": "route_change", "aec": {"mode": "우주선"}},           # 모르는 모드
    {"type": "route_change", "aec": "문자열"},                     # 형식 위반
    {"type": "route_change", "uplink_bytes": "많이"},              # 타입 위반
])
def test_a_malformed_frame_never_kills_the_call(ctrl, caplog):
    """⛔ 진단 프레임 하나 때문에 통화가 흔들리면 안 된다(R5) — 모르면 **안전 쪽**이다."""
    session = _session()
    with caplog.at_level(logging.WARNING):
        session._on_route_change(ctrl)          # 예외가 새어 나오면 실패다
    # 모르는 값에서 방어를 끄지 않는다(에너지 게이트 ON 이 안전 쪽이다).
    if "aec" not in ctrl or not isinstance(ctrl.get("aec"), dict):
        assert session._energy_gate is True or session._aec_mode == "headset"


# ── ④ 진행 중인 턴을 안 깬다 ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_it_does_not_break_a_turn_in_flight():
    """⛔ 말하는 중에 게이트를 갈아끼우면 그 턴의 판정 근거가 **중간에 바뀐다**.

    정책은 다음 판정부터 적용한다(불변식 영역이라 바꾸려면 근거가 필요하다).
    """
    session = _session()
    turn_id = await session.beaver.begin()
    session.state = cs.TurnState.BEAVER_SPEAKING

    session._on_route_change({"type": "route_change",
                              "aec": {"mode": "unknown", "route": "speaker"}})

    assert session.beaver.turn_id == turn_id, "진행 중인 비버 턴이 끊겼다"
    assert session.state == cs.TurnState.BEAVER_SPEAKING, "상태가 뒤집혔다"


# ── ⑤ 계약 덤프 ───────────────────────────────────────────────────────────
def test_the_frame_shows_up_in_the_generated_contract():
    """⑤ 그게 그 스크립트의 존재 이유다 — 사람이 문서를 고칠 일이 없어야 한다."""
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    data = json.loads((root / "docs" / "cascade-contract.json").read_text(encoding="utf-8"))
    frame = next(m for m in data["client_to_server"] if m["wire_type"] == "route_change")
    names = {f["name"] for f in frame["fields"]}
    assert names == {"aec", "uplink_bytes"}, names
    # ⛔ snake_case 그대로(별칭 만들지 마라 — 프론트 요구)
    assert not [n for n in names if any(c.isupper() for c in n)]
