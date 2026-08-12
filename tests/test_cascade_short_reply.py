"""짧은 응답 규칙은 **캐스케이드 전용**이다 — Live 는 바이트 불변.

2026-08-12. 사장님 증상 "말을 하다 만다"의 원인은 상한이 아니라 **프롬프트와 상한의 모순**이었다.

    core/persona_prompt.py   "5. 응답 길이: 매 응답은 1~4문장으로 짧게."
    core/config.py           CASCADE_LLM_MAX_OUTPUT_TOKENS = 40   ← 1~2문장이 한계

⇒ 프롬프트는 4문장까지 쓰라 하고, 상한은 그 전에 자른다. **모델은 지시를 따랐고 우리가 잘랐다.**
실통화 call 938: b1 글자=99(꼬리 59자 버림) · b2 글자=75(꼬리 85자) · b3 글자=27(**꼬리 99자**).
b3 은 말한 것의 4배를 버렸다.

⛔ 상한을 올려서 풀지 않는다 — 대답이 길어지면 첫소리·턴이 늦어진다(짧게 하라신 이유가 그것이다).
  상한은 **안전망**이지 길이 조절 수단이 아니다. 모델이 스스로 끝맺으면 자를 것이 없다.

여기서 고정하는 성질:
  ① `short_reply=True` 면 규칙 5가 짧은판으로 **교체**된다(추가가 아니라 교체 — 모순이 남으면 안 된다)
  ② 기본값(False)이면 **한 바이트도 안 바뀐다** — Live 가 그 경로다
  ③ Live 호출부는 이 인자를 **안 넘긴다**(넘기는 순간 사장님이 "딱 좋다"신 Live 길이를 잃는다)
  ④ 캐스케이드 호출부는 **넘긴다**
"""

import ast
import inspect
import textwrap
from pathlib import Path

import domains.learning.realtime.call_session as live
import domains.learning.realtime.cascade_session as cs
from core.persona_prompt import build_system_instruction

_BASE = dict(
    role="다정한 친구",
    personality="편안한 말투",
    level_profile="",
    locale="en",
    interests=["여행"],
    target_language="한국어",
)


def test_default_output_is_byte_identical():
    """⛔ **Live 바이트 불변** — 인자를 안 주든 False 로 주든 예전과 똑같아야 한다."""
    baseline = build_system_instruction(**_BASE)
    assert build_system_instruction(**_BASE, short_reply=False) == baseline
    assert "1~4문장으로 짧게" in baseline, "기본 규칙 5 가 사라졌다(Live 가 쓰는 문장이다)"


def test_short_reply_replaces_the_length_rule():
    """⭐ 짧은판은 **교체**다. 두 규칙이 같이 남으면 모델이 어느 쪽을 따를지 모른다."""
    short = build_system_instruction(**_BASE, short_reply=True)
    assert "1~2문장" in short and "반드시 문장을 끝맺어라" in short
    assert "1~4문장으로 짧게" not in short, (
        "긴 규칙이 남아 있다 — 프롬프트가 스스로 모순된다(이 결함의 원인 그 자체)"
    )


def test_short_reply_changes_only_the_length_rule():
    """⚠ 다른 곳은 안 건드린다 — 길이만 바꾸려다 페르소나·불변 규칙이 흔들리면 안 된다."""
    base = build_system_instruction(**_BASE)
    short = build_system_instruction(**_BASE, short_reply=True)
    base_lines = [ln for ln in base.split("\n") if not ln.startswith("5. 응답 길이")]
    short_lines = [ln for ln in short.split("\n") if not ln.startswith("5. 응답 길이")]
    assert base_lines == short_lines, "길이 규칙 말고 다른 줄이 바뀌었다"


def _calls(module_or_func, name="build_system_instruction"):
    src = (Path(inspect.getfile(module_or_func)).read_text(encoding="utf-8")
           if inspect.ismodule(module_or_func)
           else textwrap.dedent(inspect.getsource(module_or_func)))
    return [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")) == name
    ]


def test_live_never_opts_in():
    """⛔ Live 는 이 인자를 **넘기지 않는다**. 사장님이 Live 속도·길이는 "딱 좋다"고 하셨다."""
    calls = _calls(live)
    assert calls, "Live 에서 조립기 호출을 못 찾았다(경로가 바뀌었나)"
    for call in calls:
        assert not any(kw.arg == "short_reply" for kw in call.keywords), (
            f"Live 호출(line {call.lineno})이 짧은 규칙을 켰다 — Live 출력이 바뀐다"
        )


def test_cascade_opts_in():
    """⭐ 캐스케이드는 켠다 — 여기가 상한에 걸려 문장이 잘리던 경로다."""
    calls = _calls(cs.CascadeSession._system_instruction)
    assert calls, "캐스케이드에서 조립기 호출을 못 찾았다"
    assert any(
        any(kw.arg == "short_reply" and getattr(kw.value, "value", None) is True
            for kw in call.keywords)
        for call in calls
    ), "캐스케이드가 짧은 규칙을 안 켠다 — 대답이 계속 중간에서 잘린다"
