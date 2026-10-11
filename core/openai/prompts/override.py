# -*- coding: utf-8 -*-
"""프롬프트 override 검사 — **순수 함수**(DB 를 모른다).

## 왜 여기인가
`core/openai/` 는 `domains.`·`db.` 를 import 할 수 없다(`test_openai_isolation.py` 가 AST
로 전수 검사). 그래서 **DB 읽기는 서비스층**이 하고, **글자 검사는 여기**가 한다 —
어댑터는 「이 글자를 써도 되나」만 판단한다.

## 무엇을 막나 — 넷
배포 없이 글자를 고치면 **시험이 못 지킨다.** 그 구멍을 런타임 검사로 메운다.
어기면 **코드 기본값으로 떨어지고 ERROR 를 남긴다** — 통화는 산다(R5).

1. **슬롯 집합 동일** — `%(locale_label)s` 오타 하나가 `.format`/`%` 를 깨뜨려 **통화가
   열리기도 전에 죽는다.** 기본값이 쓰는 슬롯과 **정확히 같아야** 한다(누락·추가 다 금지).
2. **대괄호 라벨을 새로 만들 수 없다** — 대본이 `[오늘의 표현]` 을 **이름으로** 가리키고
   그 블록은 코드가 만든다. 이름을 바꾸면 대본이 **없는 블록**을 가리킨다.
3. **금지어 수가 기본값을 넘지 않는다** — 종료 어휘를 가르친 과거 3건(call 706·852·870)이
   전부 모델이 스스로 통화를 끊는 사고로 끝났다. 「늘지 않았다」만 본다(기본값이 이미
   쓰는 만큼은 허용 — 예: 전문의 「서버가 종료를 알릴 때까지」).
4. **길이 상한** — 입력 토큰이 곧 원가다. 기본값의 3배까지 허용한다(넉넉하되 사고는 막는다).

## ⛔ 하지 않는 것
글자의 **뜻**은 보지 않는다. 「이 지시가 좋은가」는 통화로만 알 수 있다 — 검사는
**배관이 깨지는 것**만 막는다.
"""

from __future__ import annotations

import re

#: 슬롯 표기 둘을 다 본다 — 이 레포는 대본마다 표기법이 다르다(측정값 2026-10-10:
#:   expression `%(l)s` · freetalk 위치 인자 `%s` · leveltest `.format` 의 `{role}`).
_PCT_NAMED = re.compile(r"%\((\w+)\)[sd]")
_PCT_POS = re.compile(r"%[sd]")
_BRACE = re.compile(r"\{(\w+)\}")
#: 대괄호 라벨 — 대본이 이름으로 가리키는 구획. 한글·영문·숫자·중점만(문장은 제외).
_LABEL = re.compile(r"\[([^\[\]\n]{2,24})\]")

#: ⭐ **코드가 만드는 데이터 블록의 이름들**(2026-10-10). 대본이 이걸 이름으로 가리키는 건
#:   정당하다 — 검사 2가 막으려는 것은 **아무것도 가리키지 않는** 새 이름뿐이다.
#:   ⚠ 기본 대본에 그 이름이 안 적혀 있을 수 있다(표현학습 전문은 `[오늘의 표현]` 을
#:     「제공된 표현」이라고 **말로** 가리킨다). 그래서 기본값의 라벨 집합만 보면
#:     「`[오늘의 표현]` 이라고 또박또박 쓰는 것」이 거부된다 — 그게 더 분명한 지시인데도.
#:   ⛔ 여기에 이름을 더할 때는 **그 블록을 실제로 만드는 코드**가 있어야 한다.
DATA_LABELS = frozenset({
    "오늘의 표현",   # expression `_items_block`
    "이번 차시",     # freetalk   `_lesson_block`
    "관심사",        # chat       `_interests_block`
    "기억",          # chat       `_memory_block`
})

#: 종료 어휘 — 늘어나면 막는다(위 검사 3).
BANNED = ("마무리", "마지막", "여기까지", "종료", "작별", "통화를 끝", "끝내는 때")

#: 길이 상한 배수. ⚠ 3 → 5 (2026-10-11 토론2): 영어 본문은 같은 내용이 한국어보다 글자 수가 약 1.6배라
#:   기본값(503자) 3배에 걸렸다(A안 2,001자). 영어 2,000자 ≈ 500 토큰 — 지시문 1,600 토큰 중 원가 걱정 거리가 아니다.
MAX_LEN_FACTOR = 5


def _slots(text: str) -> tuple[frozenset[str], int]:
    """(이름 있는 슬롯 집합, 위치 인자 개수). 둘을 따로 세는 이유는 모듈 독스트링."""
    named = frozenset(_PCT_NAMED.findall(text)) | frozenset(_BRACE.findall(text))
    # `%(x)s` 도 `%[sd]` 에 걸리므로 이름 있는 것을 먼저 지우고 센다.
    pos = len(_PCT_POS.findall(_PCT_NAMED.sub("", text)))
    return named, pos


def _labels(text: str) -> frozenset[str]:
    return frozenset(m.strip() for m in _LABEL.findall(text))


def vet(name: str, body: str, default: str) -> tuple[bool, str]:
    """override 글자를 써도 되나. `(ok, 이유)` — ok 가 False 면 이유를 로그로 남긴다.

    ⚠ `default` 는 **같은 대본의 코드 기본값**이다. 대본끼리 비교하지 않는다 —
      네 대본이 공유하는 슬롯·라벨이 **0개**라(측정값) 공통 스키마가 없고, 필요도 없다.
    """
    body = (body or "").strip()
    if not body:
        return False, "빈 글자"

    d_named, d_pos = _slots(default)
    b_named, b_pos = _slots(body)
    if b_named != d_named:
        missing = sorted(d_named - b_named)
        unknown = sorted(b_named - d_named)
        return False, ("슬롯 집합이 다르다 — 빠진 것 %s · 모르는 것 %s "
                       "(오타 하나가 통화를 열기도 전에 죽인다)" % (missing, unknown))
    if b_pos != d_pos:
        return False, "위치 인자(%%s) 개수가 다르다 — 기본값 %d개 · override %d개" % (d_pos, b_pos)

    # ⭐ 기본값이 쓰는 라벨 + **코드가 만드는 데이터 블록 이름**은 허용한다. 막는 건
    #   «아무것도 가리키지 않는» 새 이름뿐이다.
    new_labels = sorted(_labels(body) - _labels(default) - DATA_LABELS)
    if new_labels:
        return False, ("기본값에 없는 대괄호 라벨 %s — 대본이 가리키는 블록은 코드가 "
                       "만든다. 새 이름을 쓰면 없는 블록을 가리킨다(쓸 수 있는 이름: %s)"
                       % (new_labels, sorted(_labels(default) | DATA_LABELS)))

    for word in BANNED:
        if body.count(word) > default.count(word):
            return False, ("종료 어휘 «%s» 가 늘었다(기본값 %d → override %d) — 종료 개념을 "
                           "가르친 과거 3건이 전부 모델 자력 종료로 끝났다"
                           % (word, default.count(word), body.count(word)))

    limit = max(len(default) * MAX_LEN_FACTOR, 400)
    if len(body) > limit:
        return False, "너무 길다 — %d자(상한 %d자). 입력 토큰이 곧 원가다" % (len(body), limit)

    return True, ""
