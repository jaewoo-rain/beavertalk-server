# -*- coding: utf-8 -*-
"""표현학습 퀴즈 판정 — **순수 함수. LLM 0.** (기획 §2-6)

퀴즈의 정답은 «배운 항목의 표면형 그 자체» 다(D15) — 새 문제를 만드는 게 아니라 방금
다룬 표현을 다시 말하게 하는 것이라, 판정은 «학습자가 그 표현을 냈는가» 하나다.

## ⛔ 유사도만으로는 안 된다 — 그래서 3분기다

    정답 `안녕히 가세요`  ↔  학습자 `안녕히 계세요`

두 글자만 다른데 **뜻이 반대다**(가는 사람에게 / 남는 사람에게). 편집거리·자카드 어느
잣대로도 «거의 맞음» 이 나오지만 이건 **오답**이다. 뜻을 봐야 하고, 그건 LLM 몫이다.
그렇다고 매번 LLM 을 부르면 통화당 수십 번이 되므로, **확실한 양 끝만 코드가 끊는다**:

    ① 정규화 후 정확일치        → PASS     (LLM 0 · 비용 0)
    ② 공통 어절 0               → FAIL     (LLM 0)
    ③ 그 사이                   → UNKNOWN  → 사이드카 1회

⭐ 위 «안녕히 계세요» 는 ①도 ②도 아니다(`안녕히` 가 공통 어절이다) ⇒ **③으로 간다.**
  그게 이 설계의 요점이다 — 코드는 «모르겠다» 를 말할 수 있어야 한다.

## ⚠ 정규화는 **보수적으로** 한다
띄어쓰기·문장부호만 걷어낸다. 조사·어미까지 잘라내면 위 사고의 반대 방향이 난다 —
`안녕히 가세요` 와 `안녕히 계세요` 의 차이가 어미에 있으므로, 어미를 지우면 두 문장이
**같아진다.** ⇒ 어간 추출·조사 제거를 여기에 넣지 마라.

⛔ 이 모듈은 DB·LLM·도메인 모델을 모른다(어댑터 순수성). 호출부가 판정을 받아 쓴다.

설계: docs/plans/2026-09-10-표현학습-프리토킹-통화-분리.md §2-6 · D15
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

Verdict = Literal["pass", "fail", "unknown"]

PASS: Verdict = "pass"
FAIL: Verdict = "fail"
UNKNOWN: Verdict = "unknown"

# 걷어낼 것: 공백류 + 문장부호. ⛔ 한글 자모를 건드리지 않는다.
# ⚠ `\w` 를 쓰지 않는다 — 로케일에 따라 한글 포함 여부가 흔들린다. 버릴 것을 직접 적는다.
_PUNCT_RE = re.compile(r"[\s.,!?;:~…·\"'“”‘’()\[\]{}<>«»\-–—/\\|]+")


def normalize(text: str | None) -> str:
    """비교용 정규화 — **공백·문장부호만** 걷어낸다(NFC 정규화 포함).

    ⚠ NFC 를 거는 이유: 전사가 자모 분해형(NFD)으로 올 수 있고, 그러면 눈에 같은 글자가
      코드포인트로 달라 정확일치가 조용히 실패한다. macOS 계열 입력·일부 STT 의 실측 함정이다.
    ⛔ 소문자화는 한다(라틴 문자 답변 대비). 조사·어미 절단은 **하지 않는다** — 모듈
      독스트링의 «안녕히 가세요/계세요» 참조.
    """
    s = unicodedata.normalize("NFC", (text or "").strip())
    return _PUNCT_RE.sub("", s).lower()


def _tokens(text: str | None) -> list[str]:
    """어절 목록 — 공백으로 자르고 각 어절을 정규화한다(빈 어절 제거)."""
    s = unicodedata.normalize("NFC", (text or "").strip())
    return [t for t in (normalize(w) for w in s.split()) if t]


def judge(answer: str | None, correct: str | None) -> Verdict:
    """학습자 답 ↔ 정답(항목 표면형) 판정.

    Returns:
        PASS    정규화 후 정확일치 — LLM 을 부르지 않는다.
        FAIL    공통 어절이 하나도 없다 — LLM 을 부르지 않는다.
        UNKNOWN 그 사이 — 호출부가 사이드카 1회로 뜻을 본다.

    ⚠ 정답이 비어 있으면 판정할 게 없다 → UNKNOWN(모른다고 말한다).
      ⛔ FAIL 로 떨어뜨리지 마라 — 서버 쪽 결손 때문에 학습자를 틀렸다고 하면 안 된다.
    ⚠ 답이 비어 있으면(무음·전사 실패) FAIL 이다. 산출이 0 이면 통과가 아니다.
    """
    c_norm = normalize(correct)
    if not c_norm:
        return UNKNOWN
    a_norm = normalize(answer)
    if not a_norm:
        return FAIL

    # ① 정확일치 — 띄어쓰기가 달라도 통과한다(전사는 띄어쓰기를 자주 틀린다).
    if a_norm == c_norm:
        return PASS

    # ⭐ 답이 정답을 **품고 있으면** 통과다. 학습자는 "음... 안녕히 가세요!" 처럼 말하고,
    #   전사에 군말이 섞인다. 정답 전체가 그 안에 있으면 그 표현을 낸 것이 맞다.
    #   ⛔ 반대 방향(정답이 답을 품는 것)은 통과가 아니다 — 그건 **일부만** 말한 것이다.
    if c_norm in a_norm:
        return PASS

    # ② 공통 어절 0 → 오답. 완전히 다른 말을 했다는 뜻이다.
    #   ⚠ 정답이 한 어절뿐이면 이 관문은 사실상 «부분 문자열도 아니다» 와 같다.
    a_set, c_set = set(_tokens(answer)), set(_tokens(correct))
    if c_set and not (a_set & c_set):
        return FAIL

    # ③ 겹치긴 하는데 같지 않다 — 뜻을 봐야 한다.
    return UNKNOWN
