# -*- coding: utf-8 -*-
"""표현학습 문자열 정규화 회귀 — **판정 함수는 없다** (기획 §2-6 재설계, 2026-09-10).

## ⛔ 이 파일이 지키는 첫 번째 계약: «판정기가 돌아오지 않는다»

한때 여기 3갈래 판정기(정확일치→통과 / 공통 어절 0→오답 / 그 사이→LLM)가 있었다.
**설계째 걷어냈다**(사장님 결정) — 문자열에 통과 결정권을 주면:
  ① 포함 통과가 진도를 부풀린다(정답 `물` 에 "저는 물을 좋아해요" 가 통과)
  ② 드릴 복창과 퀴즈 정답을 못 가른다(1턴 창은 새고, 2턴 창은 정상 퀴즈를 막는다)
⇒ 판정은 전사를 읽는 LLM 이 하고, 파이썬은 그 결과를 검증해 쓴다.

그래서 이 파일은 이제 **정규화 하나**와 **모듈이 순수한가**만 본다.
"""

from __future__ import annotations

import unicodedata

import pytest

from domains.learning.service import quiz_judge as qj


# --------------------------------------------------------------------------- #
# ⛔ 판정 함수가 다시 생기지 않는다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("gone", ["judge", "PASS", "FAIL", "UNKNOWN", "_EXTRA_CHARS_MAX"])
def test_the_string_judge_is_gone_and_stays_gone(gone: str) -> None:
    """⛔⛔ 되살리지 마라 — 위 ①②가 그대로 돌아온다.

    ⚠ «작은 예외 하나만» 이 위험한 자리다. 정확일치만 통과시켜도 드릴 복창이 곧바로
      통과로 세어진다(복창은 정의상 정확일치다).
    """
    assert not hasattr(qj, gone), f"문자열 판정기가 돌아왔다: {gone}"


# --------------------------------------------------------------------------- #
# 정규화 — 대조가 전사 잡음에 안 흔들리게
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("안녕히 가세요", "안녕히가세요"),
        ("  안녕히  가세요 !! ", "안녕히가세요"),
        ("이거 얼마예요?", "이거얼마예요"),
        ("Hello, World!", "helloworld"),
    ],
)
def test_spacing_and_punctuation_are_stripped(raw: str, expected: str) -> None:
    assert qj.normalize(raw) == expected


def test_decomposed_hangul_is_folded() -> None:
    """⚠ 전사가 자모 분해형(NFD)으로 오면 눈에 같은 글자가 코드포인트로 다르다.

    NFC 를 안 걸면 대조가 **조용히** 실패한다 — 비버가 가르친 항목을 «안 다룬 것» 으로 센다.
    """
    nfd = unicodedata.normalize("NFD", "안녕히 가세요")
    assert nfd != "안녕히 가세요"                      # 실제로 다른 문자열이다(전제 확인)
    assert qj.normalize(nfd) == qj.normalize("안녕히 가세요")


def test_endings_are_never_stripped() -> None:
    """⛔ 조사·어미를 잘라내지 마라.

    `안녕히 가세요`(가는 사람에게) ↔ `안녕히 계세요`(남는 사람에게)는 두 글자 차이인데
    **뜻이 반대다.** 어미를 지우면 두 문장이 **같아지고**, 이 함수를 쓰는 대조가 곧바로 틀린다.
    """
    assert qj.normalize("안녕히 가세요") != qj.normalize("안녕히 계세요")


def test_empty_input_is_safe() -> None:
    for empty in ("", "   ", None):
        assert qj.normalize(empty) == ""


# --------------------------------------------------------------------------- #
# ⛔ 순수성 — **import 그래프로** 잰다 (소스 grep 이 아니다)
# --------------------------------------------------------------------------- #
def test_the_module_pulls_in_nothing_but_the_standard_library() -> None:
    """⛔⛔ **소스 문자열 grep 으로 재지 마라**(2026-09-10 codex QA).

    처음엔 `getsource` 에 "genai"·"Session" 같은 낱말이 없는지만 봤는데, 그건 **import
    그래프를 못 본다** — 한 단계만 건너뛰어도 무거운 의존이 들어와도 통과한다.
    ⇒ 실제로 **파일을 직접 로드해**(패키지 __init__ 우회) 무엇이 딸려 오는지 센다.

    ⚠ 알려진 사실: `domains/learning/service/__init__.py` 가 CallService 를 eager import 해서
      **`from domains.learning.service import quiz_judge` 로 부르면** SQLAlchemy·settings 가
      통째로 붙는다(실측 640 모듈, DATABASE_URL_POOL 없으면 ValidationError).
      그건 **패키지 진입점의 성질**이지 이 모듈의 성질이 아니다 — 이 시험이 그 둘을 가른다.
    """
    import subprocess
    import sys

    script = (
        "import importlib.util, sys, pathlib\n"
        "p = pathlib.Path('domains/learning/service/quiz_judge.py')\n"
        "spec = importlib.util.spec_from_file_location('qj_isolated', p)\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(m)\n"
        "heavy = [n for n in ('sqlalchemy','pydantic','fastapi','google','core.config')\n"
        "         if n in sys.modules]\n"
        "print('HEAVY=' + ','.join(heavy))\n"
        "print('NORM=' + m.normalize(' 가 세 요! '))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8",
    )
    assert out.returncode == 0, "정규화 모듈이 단독으로 뜨지 않는다:\n" + out.stderr[-800:]
    heavy = next(l for l in out.stdout.splitlines() if l.startswith("HEAVY="))
    assert heavy == "HEAVY=", "정규화 모듈이 무거운 의존을 끌어온다: " + heavy
    assert "NORM=가세요" in out.stdout


# --------------------------------------------------------------------------- #
# ⛔⛔ 대조(mentions) — 이 결함은 **세 번** 모양을 바꿔 돌아왔다
# --------------------------------------------------------------------------- #
# ① 통과(pass) 경로   정답 「물」에 "저는 물을 좋아해요" 가 통과 → 걷어냄
# ② 대조(covered) 경로 같은 결함이 남아 있었다("어제 선물을…")     → 낱말 경계 도입
# ③ 접두 결합어        `startswith` 만으로는 「개나리」가 통과했다   → 조사 꼬리 화이트리스트
# ⇒ 세 번째를 막는 건 시험뿐이다. **이 표를 지우지 마라.**
# ⚠ 규율: **미검출은 안전하고 오검출은 아니다** — 못 잡으면 한 번 더 가르치면 되지만,
#   잘못 잡으면 그 항목이 «완료» 로 처리돼 **가르칠 기회가 영영 사라진다.**
@pytest.mark.parametrize(
    "text,label,expected",
    [
        # ── 잡아야 한다: 정확 일치 + 조사가 붙은 꼴 ──
        ("물 주세요", "물", True),
        ("물을 마셔요", "물", True),
        ("물이 차가워요", "물", True),
        ("사과 한 개 주세요", "개", True),
        ("가방을 들다", "들다", True),          # 원형 그대로
        ("안녕히 가세요 조심히 들어가세요", "안녕히 가세요", True),   # 여러 어절
        # ── 잡으면 안 된다: 뒤에 붙은 결합어 ──
        ("어제 선물을 받았어요", "물", False),
        ("함박눈이 와요", "눈", False),
        # ── 잡으면 안 된다: **앞에 붙은** 결합어(③에서 새던 자리) ──
        ("개나리가 피었어요", "개", False),
        ("눈물이 났어요", "눈", False),
        ("물건을 샀어요", "물", False),
        ("말씀 감사합니다", "말", False),
    ],
)
def test_mentions_requires_a_word_boundary(text: str, label: str, expected: bool) -> None:
    assert qj.mentions(text, label) is expected


def test_a_conjugated_verb_is_a_miss_not_a_false_hit() -> None:
    """⚠ 활용은 어미를 **갈아치우므로** 여기서 안 잡힌다 — 그건 미검출이고 안전하다.

    ⛔ 활용형까지 잡으려 들지 마라(어간 추출). 그 욕심이 정확히 오검출을 부른다 —
      「들다」를 어간 「들」로 줄이는 순간 「들판」·「들것」이 딸려 온다.
    """
    assert qj.mentions("가방을 들었어요", "들다") is False


def test_the_particle_list_stays_small() -> None:
    """⛔ 이 목록이 길어질수록 접두 결합어가 다시 새어 들어온다.

    ⚠ 활용 어미(-었어요·-습니다…)는 **일부러 없다** — 넣으면 어간 추출이 되고, 그건 이
      모듈이 안 하기로 한 일이다.
    """
    assert "" in qj._PARTICLES                       # 정확 일치
    for banned in ("었어요", "습니다", "나리", "물", "씀"):
        assert banned not in qj._PARTICLES, f"오검출을 부르는 꼬리: {banned}"


def test_mentions_is_safe_on_empty_input() -> None:
    for empty in ("", "   ", None):
        assert qj.mentions(empty, "물") is False
        assert qj.mentions("물 주세요", empty) is False
