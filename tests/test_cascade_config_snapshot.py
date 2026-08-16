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
    for token in ("llm=", "@asia-northeast3", "추론=", "토큰상한=", "tts=", "문장상한=3",
                  "힌트=off", "마이크상시=on", "bargein(", "confirm=", "침묵=", "선행버퍼=",
                  "세션상한="):
        assert token in line, (token, line)


# ── 추론·토큰상한 — **정말 0 으로 돌았나**(2026-08-13) ────────────────────────
def _snapshot(monkeypatch, caplog, **over) -> str:
    for k, v in over.items():
        monkeypatch.setattr(cs.settings, k, v)
    session = cs.CascadeSession(_Sink(), object())
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="domains.learning.realtime.cascade_session"):
        session._log_config_snapshot()
    return _line(caplog)


def test_thinking_off_is_visible(monkeypatch, caplog):
    """⭐ 값이 안 보이면 **0 인지 아무도 모른다.**

    ⛔ 이건 A유형(안 보내는데 아무도 몰랐다)의 설정판이다. 추론 토큰은 출력 단가로 과금되고
      첫 소리를 늦추는데, 우리는 코드 기본값만 보고 "0 이겠지"라고 말해 왔다 — env 가 덮을
      수 있고 실제로 덮고 있는 값이 있다(bargein 임계는 배포값이 코드 기본값과 다르다).
    """
    assert "추론=off" in _snapshot(monkeypatch, caplog, CASCADE_LLM_THINKING_BUDGET=0)


def test_a_thinking_budget_shows_the_number(monkeypatch, caplog):
    """켜져 있으면 **몇인지**가 보인다 — 켜짐/꺼짐만으로는 원가를 못 읽는다."""
    assert "추론=512" in _snapshot(monkeypatch, caplog, CASCADE_LLM_THINKING_BUDGET=512)


def test_the_model_default_is_not_disguised_as_off(monkeypatch, caplog):
    """⛔ None(모델 기본값)을 `off` 로 적으면 **거짓말**이다 — 그때 추론은 돌 수도 있다."""
    line = _snapshot(monkeypatch, caplog, CASCADE_LLM_THINKING_BUDGET=None)
    assert "추론=모델기본" in line and "추론=off" not in line, line


def test_the_token_cap_is_visible(monkeypatch, caplog):
    """토큰 상한은 이제 **안전망**이다 — 그 값이 보여야 `상한잘림` 을 해석할 수 있다."""
    assert "토큰상한=200" in _snapshot(monkeypatch, caplog, CASCADE_LLM_MAX_OUTPUT_TOKENS=200)
    assert "토큰상한=없음" in _snapshot(monkeypatch, caplog, CASCADE_LLM_MAX_OUTPUT_TOKENS=0)


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


# ── 와이어 공백 — 클라의 `SERVER GAP` 과 짝이 되는 서버 값 ────────────────
def test_the_batch_gap_uses_the_same_window_as_the_client():
    """⭐ 클라가 `SERVER GAP … mid-utterance` 로 판정하는 창(250ms)과 **같은 기준**이다.

    ⛔ 기준이 다르면 양쪽 로그를 맞대 볼 수 없다 — 그게 이 값을 넣는 유일한 이유다.
    ⚠ 작은 공백까지 다 찍으면 줄만 길어진다(정상 통화에도 수십 개가 있다).
    ⚠ 2026-08-13: 이름이 `묶음공백=` → **`와이어공백=`** 으로 바뀌었다. 선행 합성을 넣으면
      **묶음 경계의 대기는 0 이 되는데 소리는 여전히 안 나갈 수 있다** — 그러면 값만 좋아진
      척한다. 그래서 재는 자리를 **송출 지점(프레임 간격)** 으로 옮겼다. 굶는 쪽은 클라이고,
      클라가 보는 것도 프레임 간격이다.
    """
    assert cs.CascadeSession._batch_gap_log([]) == "와이어공백=-"
    assert cs.CascadeSession._batch_gap_log([0.05, 0.2, 0.24]) == "와이어공백=-", (
        "판정 창보다 작은 공백까지 찍었다 — 신호가 잡음에 묻힌다"
    )
    line = cs.CascadeSession._batch_gap_log([0.05, 2.435, 0.49])
    assert "2.44s" in line and "0.49s" in line and "0.05s" not in line, line
