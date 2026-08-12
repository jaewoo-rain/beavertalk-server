"""캐스케이드 **대답 LLM 만** 리전을 가른다 — Live 는 그대로.

사장님 지시(2026-08-13): "LLM 을 서울 리전으로." 그런데 env 한 줄로는 안 된다.

    core/config.py     GCP_LOCATION = "us-central1"     ← **전역** Vertex 리전
    main.py lifespan   app.state.genai_client            ← **하나뿐**이다

⇒ `GCP_LOCATION` 을 바꾸면 **Live 도 같이 옮겨간다**. Live 네이티브 오디오의 서울 지원 여부는
**확인하지 못했다**(bidi WebSocket 이라 단순 호출로 못 재고, 모델 GET 은 어느 리전에서든 404 라
가용성 판정에 못 쓴다). 확인 안 된 채 전역을 옮기면 **통화가 죽는다**.
⇒ 그래서 **교체가 아니라 추가**다: 대답용 클라이언트를 하나 더 만들고 Live 는 손대지 않는다.

여기서 고정하는 성질:
  ① 값이 비면 **기본 클라이언트를 그대로 재사용**한다(객체를 더 만들지 않는다 = 지금과 동일)
  ② 값이 있으면 그 리전으로 **새로** 만들고, `app.state.genai_client` 는 **안 바뀐다**
  ③ 생성이 실패하면 **기본 클라이언트로 떨어진다**(R5 — 리전 하나 때문에 통화가 죽으면 안 된다)
  ④ 세션은 **대답에만** 그 클라이언트를 쓴다 — 힌트·분석은 기본 클라이언트다
  ⑤ 아무것도 안 넘기면 세션 동작이 예전과 같다
"""

import inspect
import textwrap

import pytest

import domains.learning.realtime.cascade_session as cs
import main as app_main
from core.config import Settings


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None


def _settings(**kw) -> Settings:
    base = dict(DATABASE_URL_POOL="postgresql+psycopg2://u:p@localhost:5432/x")
    base.update(kw)
    return Settings(**base)


def test_empty_location_reuses_the_default_client():
    """① 비어 있으면 **같은 객체**다 — 기본 동작이 지금과 완전히 같아야 한다."""
    default = object()
    got = app_main._create_cascade_client(_settings(CASCADE_LLM_LOCATION=""), default)
    assert got is default, "값이 없는데 클라이언트를 새로 만들었다"


def test_same_location_reuses_the_default_client():
    """전역과 같은 리전을 적어도 새로 안 만든다(같은 것을 둘로 두면 헷갈린다)."""
    default = object()
    got = app_main._create_cascade_client(
        _settings(GCP_LOCATION="us-central1", CASCADE_LLM_LOCATION="us-central1"), default
    )
    assert got is default


def test_a_different_location_builds_a_separate_client(monkeypatch):
    """② 다른 리전이면 **그 리전으로 새로** 만든다. Live 것은 안 건드린다."""
    made: list[str | None] = []

    def _fake(settings, location=None):
        made.append(location)
        return f"client@{location}"

    monkeypatch.setattr(app_main, "_create_genai_client", _fake)
    default = object()
    got = app_main._create_cascade_client(
        _settings(GCP_LOCATION="us-central1", CASCADE_LLM_LOCATION="asia-northeast3"), default
    )
    assert got == "client@asia-northeast3"
    assert made == ["asia-northeast3"]
    assert got is not default, "기본 클라이언트를 덮어썼다 — Live 가 같이 옮겨간다"


def test_a_failed_regional_client_falls_back(monkeypatch, caplog):
    """③ 실패하면 **기본 리전으로 계속한다**(R5) — 리전 하나 때문에 통화가 죽으면 안 된다."""
    monkeypatch.setattr(app_main, "_create_genai_client", lambda settings, location=None: None)
    default = object()
    got = app_main._create_cascade_client(
        _settings(GCP_LOCATION="us-central1", CASCADE_LLM_LOCATION="asia-northeast3"), default
    )
    assert got is default


def test_the_session_uses_the_reply_client_only_for_replies():
    """④ **대답만** 그 클라이언트를 쓴다 — 힌트는 기본 클라이언트다.

    ⛔ 힌트·분석을 같이 옮기지 않는 이유: ①실시간이 아니라 리전 이득이 없고 ②다른 모델
      (`JUDGE_MODEL`)을 쓰는데 **그 모델의 서울 가용성을 확인 못 했다** ③곁다리가 실패해도
      대답은 살아야 한다.
    """
    reply_src = textwrap.dedent(inspect.getsource(cs.CascadeSession._run_reply))
    assert "self._llm_client" in reply_src, "대답이 대답용 클라이언트를 안 쓴다"

    hint_src = textwrap.dedent(inspect.getsource(cs.CascadeSession._run_hint))
    assert "self._llm_client" not in hint_src, (
        "힌트가 대답용 클라이언트를 쓴다 — 모델 가용성이 확인 안 된 리전으로 분석이 넘어간다"
    )


def test_the_session_defaults_to_the_shared_client():
    """⑤ 안 넘기면 예전과 같다 — 데모·테스트가 인자 하나로 계속 돈다."""
    shared = object()
    session = cs.CascadeSession(_Sink(), shared)
    assert session._llm_client is shared
    assert session._genai_client is shared

    other = object()
    session2 = cs.CascadeSession(_Sink(), shared, llm_client=other)
    assert session2._llm_client is other
    assert session2._genai_client is shared, "힌트용 클라이언트까지 갈아치웠다"


def test_live_never_reads_the_cascade_location():
    """⛔ **Live 는 손대지 않는다** — 이 설정을 Live 경로가 읽으면 전역 이동과 같아진다."""
    import domains.learning.realtime.call_session as live

    for mod in (live, __import__("core.gemini_live", fromlist=["x"])):
        src = inspect.getsource(mod)
        assert "CASCADE_LLM_LOCATION" not in src, mod.__name__
