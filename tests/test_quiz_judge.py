# -*- coding: utf-8 -*-
"""표현학습 퀴즈 판정 회귀 — 순수 함수, LLM 0 (기획 §2-6 · §7).

무엇을 지키나:
  ① 정규화 정확일치 → PASS, **LLM 0회**
  ② 공통 어절 0 → FAIL, **LLM 0회**
  ③ ⛔ `안녕히 계세요` vs 정답 `안녕히 가세요` 는 ①도 ②도 **아니다** → UNKNOWN(뜻을 봐야 한다)
  ④ 조사·어미를 잘라내지 않는다 — 자르면 ③의 두 문장이 **같아진다**

⚠ 명세 §7 은 ③ 쌍의 **최종 판정이 오답**이길 요구한다. 그 최종 판정은 사이드카(LLM)가
  하므로, 여기서는 «①이 통과를 안 준다 + ②가 오답을 안 준다 + 그래서 LLM 에 간다» 세
  갈래로 나눠 못박는다. 사이드카가 오답이라 답하면 오답이 되는 배선은 호출부 시험이 본다.
"""

from __future__ import annotations

import pytest

from domains.learning.service import quiz_judge as qj


# --------------------------------------------------------------------------- #
# ① 정확일치 — LLM 0
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "answer,correct",
    [
        ("안녕히 가세요", "안녕히 가세요"),          # 그대로
        ("안녕히가세요", "안녕히 가세요"),            # 띄어쓰기 다름(전사가 자주 틀린다)
        ("안녕히 가세요.", "안녕히 가세요"),          # 문장부호
        ("  안녕히  가세요 !! ", "안녕히 가세요"),    # 공백·부호 범벅
        ("이거 얼마예요?", "이거 얼마예요?"),
    ],
)
def test_exact_match_after_normalization_passes(answer: str, correct: str) -> None:
    assert qj.judge(answer, correct) == qj.PASS


def test_the_answer_may_carry_filler_around_the_expression() -> None:
    """학습자는 "음... 안녕히 가세요!" 처럼 말한다 — 정답 전체가 들어 있으면 낸 것이다."""
    assert qj.judge("음... 안녕히 가세요!", "안녕히 가세요") == qj.PASS


def test_saying_only_a_part_is_not_a_pass() -> None:
    """⛔ 반대 방향은 통과가 아니다 — 정답이 답을 품는 건 **일부만** 말한 것이다."""
    assert qj.judge("안녕히", "안녕히 가세요") != qj.PASS


# --------------------------------------------------------------------------- #
# ② 공통 어절 0 — LLM 0
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "answer,correct",
    [
        ("모르겠어요", "안녕히 가세요"),
        ("I don't know", "이거 얼마예요?"),
        ("사과 주세요", "학교에 가요"),
    ],
)
def test_no_shared_word_is_a_fail(answer: str, correct: str) -> None:
    assert qj.judge(answer, correct) == qj.FAIL


def test_an_empty_answer_is_a_fail() -> None:
    """무음·전사 실패 — 산출이 0 이면 통과가 아니다."""
    for empty in ("", "   ", None):
        assert qj.judge(empty, "안녕히 가세요") == qj.FAIL


# --------------------------------------------------------------------------- #
# ③ ⛔ 이 파일의 핵심 — 뜻이 반대인 한 글자 차이
# --------------------------------------------------------------------------- #
def test_the_opposite_greeting_is_not_decided_by_code() -> None:
    """⛔⛔ `안녕히 계세요`(남는 사람에게) ↔ `안녕히 가세요`(가는 사람에게).

    두 글자 차이인데 **뜻이 반대다.** 유사도 잣대로는 «거의 맞음» 이 나오지만 오답이다.
    ⇒ 코드는 여기서 **판정하지 않는다.** 세 갈래를 각각 못박는다:
      · ①이 통과를 주면 안 된다     (정확일치가 아니다)
      · ②가 오답을 주면 안 된다     (`안녕히` 가 공통 어절이다)
      · 그래서 UNKNOWN 이어야 한다   (사이드카가 뜻을 본다)
    """
    v = qj.judge("안녕히 계세요", "안녕히 가세요")
    assert v != qj.PASS, "①이 통과를 줬다 — 뜻이 반대인 답이 정답이 된다"
    assert v != qj.FAIL, "②가 오답을 줬다 — 뜻을 볼 기회를 잃는다"
    assert v == qj.UNKNOWN


def test_stems_and_endings_are_not_stripped() -> None:
    """⛔ 어간 추출·조사 제거를 넣지 마라 — 위 두 문장의 차이가 **어미에 있다.**

    자르는 순간 `안녕히가` 와 `안녕히계` 가... 더 나쁘게는 `안녕히` 로 **같아지고**,
    정확일치가 통과를 준다. 정규화는 공백·문장부호까지다.
    """
    assert qj.normalize("안녕히 가세요") != qj.normalize("안녕히 계세요")


@pytest.mark.parametrize(
    "answer,correct",
    [
        ("학교에 갔어요", "학교에 가요"),      # 시제만 다르다
        ("이거 얼마예요", "이거 얼마에요"),    # 맞춤법 흔들림
        ("밥을 먹어요", "밥을 먹었어요"),
    ],
)
def test_close_but_not_equal_goes_to_the_sidecar(answer: str, correct: str) -> None:
    assert qj.judge(answer, correct) == qj.UNKNOWN


# --------------------------------------------------------------------------- #
# 서버 쪽 결손 — 학습자를 틀렸다고 하지 않는다
# --------------------------------------------------------------------------- #
def test_a_missing_correct_answer_is_unknown_not_fail() -> None:
    """⛔ 정답이 비어 있는 건 **우리 잘못**이다. 그걸로 학습자를 오답 처리하면 안 된다."""
    for empty in ("", "   ", None):
        assert qj.judge("안녕히 가세요", empty) == qj.UNKNOWN


def test_unicode_decomposed_input_still_matches() -> None:
    """⚠ 전사가 자모 분해형(NFD)으로 올 수 있다 — 눈에 같은데 코드포인트가 다르다.

    NFC 를 안 걸면 정확일치가 **조용히** 실패하고, 맞힌 답이 사이드카로 새어 비용이 난다.
    """
    import unicodedata

    nfd = unicodedata.normalize("NFD", "안녕히 가세요")
    assert nfd != "안녕히 가세요"          # 실제로 다른 문자열이다(전제 확인)
    assert qj.judge(nfd, "안녕히 가세요") == qj.PASS


def test_the_judge_never_calls_out(monkeypatch) -> None:
    """⛔ 이 모듈은 LLM·DB 를 **모른다**(어댑터 순수성). import 만으로도 그게 보여야 한다."""
    src = __import__("inspect").getsource(qj)
    for banned in ("genai", "gemini", "Session", "db.", "requests", "httpx"):
        assert banned not in src, f"판정기가 밖을 본다: {banned}"
