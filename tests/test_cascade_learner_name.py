"""학습자 이름은 **DB(member.name)** 에서 온다 — 두 경로가 같은 자리에서.

2026-08-12 지시. 조사해 보니 절반은 이미 돼 있었다 — 기록해 둔다:
    · `build_system_instruction(name=...)` 은 **원래 있던 인자**다(`core/persona_prompt.py`).
    · Live(`call_session.py`)는 **이미** `name=setup["name"]` 을 넘기고 있었다(2곳: 일반·레벨테스트).
    · `load_call_setup()` 도 **이미** `"name": member.name` 을 내주고 있었다.
    ⇒ 빠진 곳은 **캐스케이드 한 곳**뿐이었고, 그래서 비버가 사람을 "학습자"라고 불렀다
      (프롬프트의 폴백 문자열이 그대로 이름 자리에 들어갔다).

여기서 고정하는 성질:
  ① 캐스케이드는 setup 의 `name` 을 **그대로** 조립기에 넘긴다(이름이 프롬프트에 실린다)
  ② **이름이 없으면**(None·빈칸·공백) 출력이 예전과 **바이트 동일**하다 — R5
  ③ 두 경로가 **같은 인자**를 쓴다(각자 다른 이름 배관을 만들지 않았다)
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


def test_name_reaches_the_prompt():
    """⭐ 이름을 넘기면 프롬프트에 그 이름이 실린다(폴백이 아니라)."""
    out = build_system_instruction(**_BASE, name="지우")
    assert "대화상대의 이름은 지우" in out
    assert "대화상대의 이름은 학습자" not in out


def test_missing_name_is_byte_identical():
    """⛔ **이름이 없을 수 있다.** 그때 출력은 예전과 **한 바이트도 다르면 안 된다**.

    소셜 가입·미입력 회원이 실제로 있다. 폴백 값이 예전 동작 그 자체이므로, 세 가지 '없음'
    표현이 모두 같은 바이트를 내야 한다 — 안 그러면 이름 없는 회원의 통화 품질이 조용히 바뀐다.
    """
    baseline = build_system_instruction(**_BASE)          # 인자를 아예 안 준 예전 호출
    for absent in (None, "", "   "):
        assert build_system_instruction(**_BASE, name=absent) == baseline, (
            f"이름이 {absent!r} 일 때 출력이 달라졌다 — 이름 없는 회원의 프롬프트가 바뀐다"
        )


def _passes_name(func) -> bool:
    """소스에서 `build_system_instruction(... name=...)` 을 넘기는지 AST 로 확인한다.

    ⚠ 문자열 검색은 **주석에도 걸린다**(이 저장소에서 이미 세 번 속았다). 호출 노드를 본다.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        fname = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if fname != "build_system_instruction":
            continue
        if any(kw.arg == "name" for kw in node.keywords):
            return True
    return False


def test_both_paths_pass_the_name():
    """⭐ **Live·캐스케이드 둘 다** 같은 인자로 이름을 넘긴다 — 배관이 갈리면 안 된다."""
    assert _passes_name(cs.CascadeSession._system_instruction), (
        "캐스케이드가 이름을 안 넘긴다 — 비버가 사람을 '학습자'라고 부른다"
    )
    src = Path(inspect.getfile(live)).read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", ""))
        == "build_system_instruction"
    ]
    assert calls, "Live 에서 조립기 호출을 못 찾았다(경로가 바뀌었나)"
    for call in calls:
        assert any(kw.arg == "name" for kw in call.keywords), (
            f"Live 호출(line {call.lineno})이 이름을 안 넘긴다"
        )


def test_cascade_reads_the_name_from_db_setup():
    """⛔ 이름의 출처는 **DB setup** 이다 — env·상수로 채우면 다른 사람 이름이 나간다."""
    src = textwrap.dedent(inspect.getsource(cs.CascadeSession._system_instruction))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name == "build_system_instruction":
            kw = next(k for k in node.keywords if k.arg == "name")
            assert isinstance(kw.value, ast.Call) and getattr(kw.value.func, "attr", "") == "get", (
                "setup.get('name') 이 아니다 — 출처가 DB 가 아닐 수 있다"
            )
            assert kw.value.args and getattr(kw.value.args[0], "value", None) == "name"
            return
    raise AssertionError("조립기 호출을 못 찾았다")
