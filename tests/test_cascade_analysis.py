"""캐스케이드 **통화후 분석** — 이게 없어서 통화가 통째로 멈춰 있었다(2026-08-16).

## 실측 (공유 DB, SELECT 만)
```
analyzing 에 멈춘 통화(전체 기간)
  cascade:…+gemini-2.5-flash-tts   54건
  cascade:…+gemini-2.5-flash        7건
  live                              0건
캐스케이드 최근 20건: 전사행 0~29 / 문장 **0** / 증거 **0** / status 전부 analyzing
라이브 대조군 10건:   전사행 1~20 / 문장 0~1 / 증거 0~1 / status 전부 done
```
⇒ 전사만 쌓이고 **표현 추출·문장 저장·항목 검출·체크판 증거·레벨업이 전부 없었다.**
   상태를 `analyzing` 으로 바꿔 놓고 **분석하는 사람이 없었다.**

## 여기서 고정하는 성질
  ① 통화가 끝나면 분석이 **불린다**
  ② 인자가 Live 와 같다 — 특히 `target_language` 는 **라벨**이다(코드면 루브릭이 틀어진다)
  ③ 기록이 없는 통화(call_id·member_id 없음)에서는 **안 부른다**
  ④ ⛔ 분석 착수가 터져도 **통화 종료가 안 죽는다**(R5)
  ⑤ ⛔ **Live 의 것을 재사용한다** — 새로 쓰면 두 경로가 갈려 한쪽만 고쳐진다
"""

import inspect
import logging

import pytest

import domains.learning.realtime.call_session as live
import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None


def _session(monkeypatch, **over) -> cs.CascadeSession:
    """분석 직전 상태 — 통화가 끝났고 기록이 있다."""
    session = cs.CascadeSession(_Sink(), object())
    session._call_id = 1023
    session._member_id = 7
    session._session_factory = object()
    session._locale = "en"
    session._target_label = "한국어"
    session._target_code = "ko"
    session._setup = {"candidates": [{"item_id": 1}, {"item_id": 2}]}
    for k, v in over.items():
        setattr(session, k, v)
    return session


def _spy(monkeypatch) -> list[tuple[tuple, dict]]:
    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(live, "_trigger_analysis",
                        lambda *a, **kw: calls.append((a, kw)))
    return calls


# ── ①② 불린다 · 인자가 맞다 ────────────────────────────────────────────────
def test_the_analysis_is_triggered(monkeypatch):
    """⭐⭐ 61건이 멈춰 있던 이유 — **아무도 안 불렀다.**"""
    calls = _spy(monkeypatch)
    _session(monkeypatch)._trigger_analysis()

    assert len(calls) == 1, "분석이 안 불렸다"
    args, kw = calls[0]
    assert args[0] == 1023 and args[4] == "en"
    assert kw["member_id"] == 7 and kw["call_type"] == "normal"


def test_the_target_language_is_the_label_not_the_code(monkeypatch):
    """⛔⛔ **라벨이다.** 코드("ko")를 넘기면 분석 루브릭이 그 언어로 안 걸린다.

    Live 도 `spec.label` 을 넘긴다(`call_session.py:962`). 여기가 갈리면 같은 회원의
    통화가 엔진에 따라 다른 루브릭으로 분석된다.
    """
    calls = _spy(monkeypatch)
    _session(monkeypatch)._trigger_analysis()

    assert calls[0][1]["target_language"] == "한국어", calls[0][1]
    assert calls[0][1]["target_language"] != "ko"


def test_the_candidates_come_from_the_call_setup(monkeypatch):
    """⭐ 후보는 **이미 손에 있다** — `load_call_setup` 이 담아 준다. 새로 선별하지 않는다."""
    calls = _spy(monkeypatch)
    _session(monkeypatch)._trigger_analysis()

    assert calls[0][1]["candidates"] == [{"item_id": 1}, {"item_id": 2}]


def test_a_missing_setup_falls_back_to_none(monkeypatch):
    """⚠ R5: 설정 조회가 실패한 통화(setup=None)도 분석은 돈다 — 기본 후보로 대체된다."""
    calls = _spy(monkeypatch)
    _session(monkeypatch, _setup=None)._trigger_analysis()

    assert calls[0][1]["candidates"] is None


def test_the_hint_marker_is_none(monkeypatch):
    """힌트가 꺼져 있으면 열람 마커가 없다(D16 강등이 없을 뿐 나머지는 그대로 돈다)."""
    calls = _spy(monkeypatch)
    _session(monkeypatch)._trigger_analysis()

    assert calls[0][1]["hinted_from_turn_index"] is None


def test_the_default_client_is_used_not_the_regional_one(monkeypatch):
    """⛔ 분석은 **기본 클라이언트**다 — 서울 리전은 대답 전용이고 JUDGE_MODEL 가용성을 모른다."""
    calls = _spy(monkeypatch)
    shared, seoul = object(), object()
    session = _session(monkeypatch)
    session._genai_client, session._llm_client = shared, seoul
    session._trigger_analysis()

    assert calls[0][0][1] is shared and calls[0][0][1] is not seoul


# ── ③ 기록이 없으면 안 부른다 ───────────────────────────────────────────────
@pytest.mark.parametrize("missing", ["_call_id", "_member_id", "_session_factory"])
def test_no_record_no_analysis(monkeypatch, missing):
    """기록이 없는 통화(데모·행 생성 실패)에서는 분석할 대상이 없다."""
    calls = _spy(monkeypatch)
    _session(monkeypatch, **{missing: None})._trigger_analysis()

    assert calls == []


def test_no_client_no_analysis_but_a_warning(monkeypatch, caplog):
    """⚠ graceful degradation — 키가 없으면 **분석만** 못 한다. 그리고 조용하지 않다."""
    calls = _spy(monkeypatch)
    session = _session(monkeypatch)
    session._genai_client = None
    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._trigger_analysis()

    assert calls == []
    assert [r for r in caplog.records if "분석 생략" in r.getMessage()], caplog.text


# ── ④ 분석이 터져도 통화 종료는 산다 ───────────────────────────────────────
def test_a_failing_trigger_does_not_break_the_call(monkeypatch, caplog):
    """⛔ R5 — 통화는 **이미 끝났다.** 여기서 예외를 올려 봐야 아무도 못 살린다."""
    def _boom(*a, **kw):
        raise RuntimeError("분석 착수 실패(시험)")

    monkeypatch.setattr(live, "_trigger_analysis", _boom)
    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        _session(monkeypatch)._trigger_analysis()      # 예외가 나면 실패다

    assert [r for r in caplog.records if "분석 착수 실패" in r.getMessage()], caplog.text


# ── ⑤ 재사용 · 순서 ────────────────────────────────────────────────────────
def test_it_reuses_the_live_trigger():
    """⛔ **새로 쓰지 않는다** — GC 방지 집합·done 콜백·call_type 디스패치가 거기 있다.

    ⭐ 그리고 그 태스크는 **모듈 전역**이라 세션이 죽어도 산다. 세션 TaskGroup 에 붙이면
      통화가 끝나며 취소된다 — 그러면 이 기능이 조용히 아무것도 안 하게 된다.
    """
    src = inspect.getsource(cs.CascadeSession._trigger_analysis)
    assert "call_session" in src and "_trigger_analysis(" in src
    assert "create_task" not in src, "세션이 직접 태스크를 띄운다 — 통화 종료와 함께 죽는다"


def test_the_transcript_is_committed_before_the_analysis_runs():
    """⚠ 순서 — 분석은 `call_raw_data` 를 읽는다. 전사 커밋이 **먼저**여야 한다."""
    src = inspect.getsource(cs.CascadeSession._finalize_call)
    assert src.index("_persist_segments") < src.index("_trigger_analysis"), src


def test_the_terminal_status_string_matches_the_analysis_path():
    """⚠ 종말 상태는 **코드가 쓰는 값**이다 — 손으로 정하지 않는다."""
    import domains.learning.service.normalcall_service as svc

    assert 'set_status(db, call_id, "done")' in inspect.getsource(svc.analyze_call)
