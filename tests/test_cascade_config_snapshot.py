"""통화 시작에 **설정 스냅샷 한 줄** — 그 통화의 조건이 그 통화 로그 안에서 닫힌다.

## 왜 (2026-08-13)
오늘 우리는 **서로 다른 설정으로 돌아간 통화들을 나란히 비교했다**:
· 임계 0.007 통화와 0.04 통화를 같은 표에 놓고 "에코가 안 잡힌다"고 판단할 뻔했다
· 문장상한 4 통화와 3 통화의 `글자=` 를 섞어 봤다
· 힌트 ON/OFF 가 로그로 구분되지 않아 "0건"이 **꺼진 건지 안 만든 건지** 몰랐다

조건은 env 에 있는데 **env 는 그 사이에 바뀐다.** 그래서 과거 로그는 영영 해석이 안 된다.

## 이 하네스가 잡는 것(오늘 사고 3유형)
    A. 안 보내는데 아무도 몰랐다   (output_transcript · beaver_preparing)
    B. 보냈는데 받는 쪽이 안 봤다   (힌트)
    C. 조용히 기본값으로 돌았다     (snake_case 무시 · 음색 폴백 · main 로거)
스냅샷은 **C** 를 잡는다 — "무엇으로 돌았는지"가 통화마다 박히면 조용한 기본값이 안 숨는다.

여기서 고정하는 성질:
  ① **한 줄**이다 — 흩으면 grep 으로 한 통화를 못 묶는다
  ② 조건이 바뀌면 **줄도 바뀐다**(값을 진짜로 읽는다 — 상수를 찍는 게 아니다)
  ③ **값이 확정된 뒤** 찍는다(클라 `start` 가 엔진을 바꾼 뒤여야 한다)
  ④ 자기 점검: **설정한 리전 ≠ 실제 리전**이면 WARNING. 일치하면 조용하다
"""

import logging

import pytest

import domains.learning.realtime.cascade_session as cs


class _Sink:
    async def send_event(self, event: dict) -> None:
        return None

    async def send_audio(self, frame: bytes) -> None:
        return None


def _line(caplog) -> str:
    hits = [r.getMessage() for r in caplog.records if r.getMessage().startswith("cascade 설정:")]
    assert len(hits) == 1, f"스냅샷이 {len(hits)}줄이다(한 줄이어야 한다) — {hits}"
    return hits[0]


def test_the_snapshot_is_one_line_with_every_condition(monkeypatch, caplog):
    """① 한 줄에 전부 — 이 통화가 무엇으로 돌았는지가 여기서 닫힌다."""
    monkeypatch.setattr(cs.settings, "CASCADE_HINT_ENABLED", False)
    monkeypatch.setattr(cs.settings, "CASCADE_MIC_ALWAYS_OPEN", True)
    monkeypatch.setattr(cs.settings, "CASCADE_LLM_MAX_SENTENCES", 3)
    session = cs.CascadeSession(_Sink(), object(), llm_location="asia-northeast3")

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._log_config_snapshot()

    line = _line(caplog)
    for token in ("llm=", "@asia-northeast3", "tts=", "문장상한=3", "힌트=off",
                  "마이크상시=on", "bargein(", "confirm=", "침묵=", "선행버퍼=", "세션상한="):
        assert token in line, (token, line)


def test_the_snapshot_reflects_changes(monkeypatch, caplog):
    """② 조건이 바뀌면 줄도 바뀐다 — 상수를 찍으면 이 하네스는 거짓말이 된다."""
    def _snap(**over):
        for k, v in over.items():
            monkeypatch.setattr(cs.settings, k, v)
        session = cs.CascadeSession(_Sink(), object())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
            session._log_config_snapshot()
        return _line(caplog)

    on = _snap(CASCADE_HINT_ENABLED=True, CASCADE_LLM_MAX_SENTENCES=4)
    off = _snap(CASCADE_HINT_ENABLED=False, CASCADE_LLM_MAX_SENTENCES=2)
    assert "힌트=on" in on and "문장상한=4" in on, on
    assert "힌트=off" in off and "문장상한=2" in off, off


def test_a_region_mismatch_warns(monkeypatch, caplog):
    """④ ⭐ **설정은 했는데 안 먹었다**를 잡는다 — 오늘 실제로 못 보던 자리다.

    `CASCADE_LLM_LOCATION=asia-northeast3` 을 켰는데 클라이언트 생성이 실패해 기본 리전으로
    떨어졌다면, 그 통화의 지연·원가를 **서울 것으로 읽으면 안 된다**.
    """
    monkeypatch.setattr(cs.settings, "CASCADE_LLM_LOCATION", "asia-northeast3")
    session = cs.CascadeSession(_Sink(), object(), llm_location="us-central1")

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._log_config_snapshot()

    warned = [r for r in caplog.records if "리전 불일치" in r.getMessage()]
    assert len(warned) == 1 and warned[0].levelno == logging.WARNING, caplog.text


def test_a_matching_region_is_quiet(monkeypatch, caplog):
    """⛔ **정상이면 조용하다.** 정상까지 경고하면 아무도 경고를 안 본다."""
    monkeypatch.setattr(cs.settings, "CASCADE_LLM_LOCATION", "asia-northeast3")
    session = cs.CascadeSession(_Sink(), object(), llm_location="asia-northeast3")

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._log_config_snapshot()

    assert not [r for r in caplog.records if "리전 불일치" in r.getMessage()]


def test_no_setting_no_warning(monkeypatch, caplog):
    """설정을 안 켰으면 불일치도 없다(빈 값은 "전역을 따른다"는 뜻이다)."""
    monkeypatch.setattr(cs.settings, "CASCADE_LLM_LOCATION", "")
    session = cs.CascadeSession(_Sink(), object(), llm_location="us-central1")

    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._log_config_snapshot()

    assert not [r for r in caplog.records if "리전 불일치" in r.getMessage()]


def test_the_snapshot_runs_after_the_client_start_is_applied():
    """③ **값이 확정된 뒤** 찍어야 한다 — 클라 `start` 가 엔진·배속을 바꿀 수 있다.

    그 전에 찍으면 찍은 값과 실제가 달라져, 이 줄을 넣은 이유와 정반대가 된다.
    """
    import ast
    import inspect
    import textwrap

    src = textwrap.dedent(inspect.getsource(cs.CascadeSession.run))
    tree = ast.parse(src)
    order = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (node.func.id if isinstance(node.func, ast.Name)
                    else getattr(node.func, "attr", ""))
            if name in ("_log_config_snapshot", "receive"):
                order.append((node.lineno, name))
    seq = [n for _, n in sorted(order)]
    assert "_log_config_snapshot" in seq, "스냅샷을 세션 시작에서 안 찍는다"
    assert seq.index("receive") < seq.index("_log_config_snapshot"), (
        "클라 start 를 받기 전에 찍는다 — 엔진이 바뀌면 찍은 값이 거짓이 된다"
    )
