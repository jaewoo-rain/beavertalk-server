"""`beaver_preparing` 은 **스트리밍 경로에서도** 나간다 — 프론트 실기기 0건 회귀.

증상: 프론트가 실기기에서 이 프레임을 **한 번도 못 받았다**. 원인은 단순했다 — 송신 지점이
`_run_batch_reply` 안에만 둘 있었고, 폰은 `gemini-tts`(스트리밍)라 그 함수를 안 지난다.

왜 필요한가(프론트 근거, 타당하다): 클라가 가진 값은 `mic OPEN → 다음 발화 = 6.8초`뿐인데
**그 안에 사용자 발화 시간이 섞여 있어 순수 서버 지연이 아니다.** 단계가 갈려야 LLM 이 느린지
TTS 가 느린지 답한다.

여기서 고정하는 성질:
  ① 스트리밍 대답 하나에 `stage="llm"` **1회** · `stage="tts"` **1회**(전환에서만 — 도배 금지)
  ② 순서는 llm → tts (뒤집히면 단계 해석이 반대가 된다)
  ③ 클라 변경 0 — 배치 경로가 쓰던 **같은 모델·같은 필드**다
"""

import ast
import inspect
import textwrap

import domains.learning.realtime.cascade_session as cs
from domains.learning.realtime.cascade_protocol import ServerBeaverPreparing


def _preparing_calls(func):
    src = textwrap.dedent(inspect.getsource(func))
    return [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", ""))
        == "ServerBeaverPreparing"
    ]


def _stage_of(call) -> str:
    kw = next((k for k in call.keywords if k.arg == "stage"), None)
    return getattr(kw.value, "value", "") if kw else ""


def test_streaming_reply_announces_both_stages():
    """⭐ 스트리밍 경로(`_run_reply`)가 두 단계를 **각각 한 번씩** 낸다."""
    stages = [_stage_of(c) for c in _preparing_calls(cs.CascadeSession._run_reply)]
    assert stages.count("llm") == 1, f"llm 단계 알림이 {stages.count('llm')}회다 — {stages}"
    assert stages.count("tts") == 1, f"tts 단계 알림이 {stages.count('tts')}회다(도배) — {stages}"
    assert stages.index("llm") < stages.index("tts"), "순서가 뒤집혔다 — 단계 해석이 반대가 된다"


def test_tts_stage_is_guarded_so_it_fires_once_per_reply():
    """⛔ **도배 금지.** 배치가 여러 번 흘러도 tts 알림은 한 번이다(플래그로 막는다).

    구간마다 내면 한 대답에 5~6건이 나가고, 그 로그로는 아무것도 못 가른다.
    """
    src = textwrap.dedent(inspect.getsource(cs.CascadeSession._run_reply))
    tree = ast.parse(src)
    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        emits = [c for c in ast.walk(node) if c in _preparing_calls(cs.CascadeSession._run_reply)
                 or (isinstance(c, ast.Call)
                     and (c.func.id if isinstance(c.func, ast.Name)
                          else getattr(c.func, "attr", "")) == "ServerBeaverPreparing"
                     and _stage_of(c) == "tts")]
        if emits and any(isinstance(n, ast.Name) and n.id == "tts_announced"
                         for n in ast.walk(node.test)):
            guarded = True
    assert guarded, "tts 단계 알림이 1회 보장 없이 나간다(플러시마다 나갈 수 있다)"


def test_frame_shape_is_unchanged():
    """⚠ **클라 변경 0** — 배치 때와 같은 wire type·필드다."""
    frame = ServerBeaverPreparing(stage="llm", elapsed_ms=12)
    assert frame.type == "beaver_preparing"
    assert frame.model_dump() == {
        "type": "beaver_preparing", "stage": "llm", "index": 0, "total": 0, "elapsed_ms": 12,
    }
