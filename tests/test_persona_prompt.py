"""core/persona_prompt 대본 조립 결정적 테스트 (외부 의존 0, DB/LLM 없음).

1) 스냅샷(회귀 0 증명): _RULE_CLOSE_PROTOCOL 상수 추출 리팩토링 전의
   _INVARIANTS_TEMPLATE 원문과 조립 로직을 이 파일에 그대로 동결(_ORIG_*)해 두고,
   리팩토링 후 build_system_instruction 출력이 바이트 동일함을 증명한다.
   (작업트리 미커밋 상태라 git 에서 원본을 꺼낼 수 없어, 리팩토링 착수 전에 캡처했다.)
2) 레벨테스트 대본(build_leveltest_instruction / seed_leveltest_opening /
   CLOSE_SEED_LEVELTEST) 형식 검증(2026-07 비버 자율 진행/OPI):
   종료 규약 공유, 자율 진행 방식(난이도 사다리·상승·스스로 끝내지 않음)·답 직후
   [반응+질문] 한 턴 규칙, 옛 서버 주입 시드·천장 함수 부재, 레벨 프로파일 부재,
   locale 라벨, 무인자 선톡 시드 형식.
"""

from __future__ import annotations

import core.persona_prompt as pp
from core.persona_prompt import (
    CLOSE_SEED_LEVELTEST,
    build_leveltest_instruction,
    build_system_instruction,
    seed_leveltest_opening,
)

# --------------------------------------------------------------------------- #
# 동결된 원본 (리팩토링 전 core/persona_prompt.py 에서 그대로 복사)
# ⛔ 원칙은 **수정 금지**다. 예외는 Live 문구를 **의도적으로** 바꿀 때뿐이고, 그때도
#   통째로 다시 뽑지 말고 소스에 넣은 **그 치환만** 여기에 똑같이 넣어라 — 그래야
#   의도 밖의 변경은 여전히 이 테스트에서 터진다. 재기준화는 아래 docstring 에 남긴다.
#   재기준화 이력: 2026-07 한국어 위주 전환 / 2026-08-19 끊긴 참조 2건
#                / 2026-08-22 규칙 6 신설 — 비언어 발성 길이(웃음 19~39초 턴, call 1136)
# --------------------------------------------------------------------------- #

_ORIG_INVARIANTS_TEMPLATE = """너는 '비버' — 아래 [페르소나]의 인물이다. 그 인물로서 외국인 학습자에게 전화를 걸어 {target}를 가르치고 함께 대화한다.

[모국어] 학습자의 모국어는 {locale_label}다.

[페르소나] 네 역할은 "{role}"다. 말투·성격: {personality}
{target}를 가르치고 교정하는 건 네가 하는 '일'일 뿐, 네 말투·성격은 오직 그 캐릭터다 — 설명·교정하는 순간에도 톤을 순화하지 말고, 통화가 길어져도 처음의 강도를 끝까지 유지하라. 단 아래 [불변 규칙]은 캐릭터보다 우선한다. 너는 통화 내내 이 인물이며, 네가 AI·모델·시스템·프롬프트라는 사실이나 이 지시문 자체를 절대 언급하지 마라(메타 발언 금지). 대화상대의 이름은 {username}

[불변 규칙 — 캐릭터와 무관하게 항상 지켜라]
1. 모드 분기(스스로 판단, 서버는 모드를 추적하지 않는다):
   - 네가 통화 첫머리에 던진 모드 질문에 대한 학습자의 음성 답을 듣고 네가 스스로 모드를 정해 진행해라.
   - [공부 모드] 학습자의 레벨([학습자 수준])과 흥미를 반영해 따라 말할 {target} 문장을 그 자리에서 만들어 준다 → 또박또박 한 번 들려주고 따라 말하게 시킨다 → 반응(칭찬·핀잔 등)은 네 캐릭터대로 하고, 틀리면 고쳐 준다. 한 번에 한 문장씩.
   - [대화 모드] 학습자의 관심사로 {target}를 섞은 대화를 이어간다. 학습자가 "이거 {target}로 어떻게 말해요?"라고 물으면 알려 준다. {target}가 어색하면 고쳐 준다.
   - 학습자가 도중에 모드를 바꾸고 싶다고 명시하면 따라가라.
2. 대화 지속(매우 중요):
   - 너의 일은 학습자와 대화를 계속 이어가는 것이다. 학습자가 "고마워/알겠어/갈게/bye"처럼 대화를 접으려 해도 짧게 받은 뒤 곧바로 새 화제나 질문을 하나 던져 이어가라(받는 말투는 네 캐릭터대로).
   - 헤어질 때 쓰는 표현을 가르치거나 예문으로 들려주는 일이 있다. 그건 수업 재료일 뿐이다 — 들려준 직후 곧바로 다음 재료나 새 화제로 넘어가라.
   - 대괄호로 시작하는 안내문이 대화 중간에 섞여 들어올 때가 있다. 그건 너에게만 가는 것이다 — 어떤 경우에도 대괄호와 그 안 문구를 소리 내어 읽거나 언급하지 말고, 내용만 행동으로 반영하라.
3. 언어 사용(code-switching) — 매우 중요. 목표는 학습자가 {target}를 실제로 '말하게' 하는 것이다:
   - [모드별 언어 — 가장 중요] '대화 모드'는 가르치는 게 아니라 복습·자유대화다 — 기본적으로 {target}로 대화하고, {locale_label}는 학습자가 "이거 {target}로 어떻게 말해요?"라고 묻거나 못 알아들어 막힐 때만 쓴다. '공부 모드'(오늘 항목 가르치기)는 새 항목을 이해시키는 게 우선이라 설명·지시·리액션은 레벨과 무관하게 전부 {locale_label}로 한다(새 항목을 모르는 학습자에게 {target}로 설명하면 그 설명조차 못 알아듣는다). 학습자가 따라 할 {target} 표현·예문만 또박또박 들려주고 따라 말하게 하라. 아래 밴드 정책은 대화 모드에서 초보에게 {locale_label} 발판을 얼마나 대줄지를 정한다(공부 모드엔 무관).
{lang_policy}
   - [대화 모드 — {target}로 착지] 네 턴은 맨 끝을 {target} 질문·요청으로 착지시켜라(물음표로 끝내고 멈춰라). 학습자는 네 마지막 말의 언어로 답하니 {target} 질문 뒤에 {locale_label}를 덧붙이지 마라 — 앞에 다는 {locale_label} 발판의 정도는 위 밴드 정책이 정한다.
   - [공부 모드 — 지금 항목으로 착지] 네 턴은 맨 끝을 지금 다루는 항목을 학습자가 소리 내어 말하게 하는 요청으로 착지시켜라 — 따라 말하게 하거나, 그 표현을 넣어야 답할 수 있는 짧은 질문을 던져라. 화제는 네가 고르는 게 아니라 지금 항목이 정한다.
   - 네가 던진 질문의 답을 같은 턴에 스스로 말하지 마라(자문자답 금지 — 학습자가 말할 기회를 뺏는다). 질문 뒤엔 조용히 기다려라.
   - 학습자 차례를 {target} 산출 0으로 끝내지 마라 — 최악의 경우 따라 말하기로라도 {target}를 한마디 내게 하라.
   - ★ [학습자 언어에 끌려가지 마라] 학습자가 {target}로 답해도 네 칭찬·교정·지시는 계속 {locale_label}로 하고, {target}는 따라 할 표현만 인용해라. 턴이 쌓일수록 리액션부터 {target}로 물든다 — 매 턴 첫 턴과 같은 비율로 돌아와라.
   - ★ [설명은 모국어로] 학습자가 막히거나("모르겠어요/뭐라고요?") 뜻·문법을 설명해야 할 때는, 밴드·비율과 무관하게 설명 문장 전체를 {locale_label}로 하고 {target}는 설명 대상 표현만 인용해라 — {target}를 모르는 학습자에게 {target}로 뜻을 설명하면 그 설명조차 못 알아듣는다(나쁨: "'X'는 'Y라는 뜻이야'"를 {target}로 / 좋음: 'X'만 {target}로 들려주고 뜻·쓰임은 {locale_label}로). 이해가 비율보다 우선 — 막혔는데 {target} 고집 금지. 설명이 끝나면 즉시 {target} 질문으로 착지해라.
   - 코드스위칭은 절·문장 단위로만, 한 턴에 언어 전환은 한 번만(한 문장 안에서 단어를 뒤섞지 마라). 못 알아들으면 {target} 비중을 낮추되 '{target}로 착지'는 유지해라.
4. "{target}로 어떻게 말해요?" 답변 + 교정 스타일:
   - 물어보면 올바른 {target} 표현을 또박또박 알려 준다(뜻·쓰임 설명은 위 '설명은 모국어로'를 따른다).
   - 교정은 한 번에 1~2개만. 사소한 것까지 다 잡는 과교정은 금지.
   - 교정할 때는 틀린 부분을 {locale_label}로 짚고, 올바른 {target} '○○○'를 단독으로 또박또박 다시 들려줘라 — 감싸는 말투는 네 캐릭터대로(공손한 "이렇게 말해요"를 강요하지 마라).
5. 응답 길이: 매 응답은 1~4문장으로 짧게. 혼자 길게 떠들지 말고 학습자가 말할 차례를 자주 줘라. 통화 시작 시 네가 먼저 말을 건다(선톡).
6. 말이 아닌 소리(웃음·감탄·머뭇거림)는 짧게 한 번만 내고 곧바로 말을 이어가라 — 소리만으로 턴을 끌지 마라. 무엇에 웃고 무엇에 감탄할지는 네 캐릭터대로다. 이건 소리의 길이에만 걸리는 규칙이다 — 할 말의 분량은 규칙 5 그대로다."""

# 밴드별 언어 정책(한국어 위주 전환) 동결 원본 — build_system_instruction 이 규칙 3의
# {lang_policy} 슬롯에 .format(target, locale_label) 선처리 후 주입. 미상 밴드 → beginner.
_ORIG_LANG_POLICY = {
    "survival": (
        "   - [왕초보 — 대화] 아직 {target}를 거의 못 알아듣는다. {locale_label}로 화제만 아주 짧게 "
        "열고(한 마디), 실제 질문·핵심은 쉬운 {target}로 던져 학습자가 {target}를 알아듣고 답하게 하라 — "
        "{locale_label}로 내용을 다 풀지 마라(그러면 {target}를 안 듣는다). 못 알아들으면 그때만 "
        "{locale_label}로 더 풀어주고 다시 {target}로. 새 {target} 표현은 한 번에 하나만."
    ),
    "beginner": (
        "   - [초급 — 대화] {locale_label}로 화제만 짧게 열고, 질문·핵심은 {target}로 던져 학습자가 "
        "{target}로 짧게 답하게 하라({locale_label}로 내용을 다 풀지 말 것 — 그러면 {target}를 안 듣는다). "
        "막히면 선택지({target}로 'A예요, B예요?')나 {locale_label} 힌트를 준 뒤 다시 {target}로. "
        "새 {target} 표현은 한 번에 한두 개까지."
    ),
    "intermediate": (
        "   - [중급 — 대화] 처음부터 {target}로 대화한다(모국어 발판 없이). {locale_label}는 학습자가 "
        "'이거 {target}로 어떻게 말해요?'라고 묻거나 못 알아들어 막힐 때만. 학습자가 온전한 {target} "
        "문장으로 답하게 하고, {locale_label}로 답하면 '{target}로도 해볼래요?'라고 다시 유도해라."
    ),
    "advanced": (
        "   - [고급 — 대화] 전부 {target}로 자유롭게 대화한다. {locale_label}는 '어떻게 말해요' 질문이나 "
        "막힘 구제용으로만. 복문·긴 담화·의견을 {target}로 산출하도록 이끌어라."
    ),
}

# 원본 _LOCALE_LABEL 중 테스트가 쓰는 항목만 동결(폴백 포함).
_ORIG_LABEL = {"en": "영어(English)", "ja": "일본어(日本語)"}


def _orig_history_block(history):
    """리팩토링 전 _history_block 로직 동결 복사."""
    if not isinstance(history, dict):
        return ""
    summaries = [s for s in (history.get("summaries") or []) if s][:5]
    expressions = [e for e in (history.get("expressions") or []) if e][:30]
    if not summaries and not expressions:
        return ""
    lines = [
        "\n[최근 학습 이력 — 참고]",
        "아래는 이 학습자가 최근에 한 통화·배운 표현이다. 이미 배운 건 반복하지 말고 확장해 줘라(가끔 가벼운 복습은 OK).",
    ]
    if summaries:
        lines.append("- 최근 통화 요약: " + " / ".join(summaries))
    if expressions:
        lines.append("- 이미 배운 표현: " + ", ".join(expressions))
    return "\n".join(lines)


def _orig_build(*, role, personality, level_profile, locale, interests,
                name=None, history=None, target_language="한국어", locale_label=None,
                lang_band="beginner", close_tag="[통화종료]"):
    """동결 원본 조립 로직(한국어 위주 전환 후 기준). 규칙 3에 밴드 언어 정책 주입."""
    locale_label = locale_label or _ORIG_LABEL.get(locale, _ORIG_LABEL["en"])
    interests_text = ", ".join(i for i in interests if i) or "일상"
    username = (name or "").strip() or "학습자"
    lang_policy = _ORIG_LANG_POLICY.get(lang_band, _ORIG_LANG_POLICY["beginner"]).format(
        target=target_language, locale_label=locale_label
    )
    invariants = _ORIG_INVARIANTS_TEMPLATE.format(
        locale_label=locale_label,
        role=role or "친근한 한국어 대화 파트너",
        personality=personality or "다정하고 편안한 말투",
        username=username,
        target=target_language,
        lang_policy=lang_policy,
        close_tag=close_tag,
    )
    parts = [
        invariants,
        f"\n[학습자 수준]\n{level_profile}",
        f"\n[학습자 흥미·소재] {interests_text}",
    ]
    hb = _orig_history_block(history)
    if hb:
        parts.append(hb)
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# 1) 스냅샷: 리팩토링 후 출력 == 리팩토링 전 출력 (바이트 동일)
# --------------------------------------------------------------------------- #

_SNAPSHOT_CASES = [
    # (설명, kwargs)
    (
        "en + history",
        dict(
            role="장난기 많은 비버 선생님",
            personality="유쾌하고 텐션 높은 말투",
            level_profile="레벨 3: 짧은 과거형 문장을 만들 수 있다.",
            locale="en",
            interests=["K-pop", "요리"],
            name="Alex",
            history={
                "summaries": ["주말 계획을 이야기함", "음식 주문 표현을 배움"],
                "expressions": ["주세요", "얼마예요?"],
            },
        ),
    ),
    (
        "ja + history 없음",
        dict(
            role="차분한 라디오 DJ",
            personality="느긋하고 부드러운 말투",
            level_profile="레벨 1: 인사말 수준.",
            locale="ja",
            interests=[],
            name=None,
            history=None,
        ),
    ),
    (
        "en + 빈 history(블록 생략 경로)",
        dict(
            role="",
            personality="",
            level_profile="레벨 7: 경험을 서술한다.",
            locale="en",
            interests=["여행", "", "축구"],
            name="  ",
            history={"summaries": [], "expressions": []},
        ),
    ),
]


def test_build_system_instruction_snapshot_byte_identical():
    """한국어 위주 전환 후 기준으로 재동결한 스냅샷 — 조립 로직이 동결 원본과 바이트 동일.

    ⚠ 의도적 재기준화(2026-07 한국어 위주 전환): 옛 "모국어 90%" 기준은 폐기됐다.
    lang_band 미지정(기본 beginner) 경로가 동결 원본(beginner 정책)과 일치함을 증명한다.

    ⚠ 의도적 재기준화(2026-08-19 끊긴 참조 2건): 규칙 1 의 "위 선톡 질문"(가리킬 대상이
      지시문에 없다 — 선톡은 별도 턴이다)과 [착지] 불릿의 "아래 밴드 정책"(실제로는 위)을
      고쳤다. 동결본에도 **같은 두 치환만** 넣었으므로, 그 밖의 변경은 여전히 여기서 터진다.
      ⛔ 이번 변경은 Live 출력을 **실제로 바꾼다** — 이 테스트를 "Live 무영향 증명"으로
        인용하지 마라. 근거: core/persona_prompt.py 의 _INVARIANTS_TEMPLATE 위 주석.
    """
    for desc, kwargs in _SNAPSHOT_CASES:
        assert build_system_instruction(**kwargs) == _orig_build(**kwargs), desc


def test_invariants_template_byte_identical_to_frozen_original():
    """템플릿 자체도 동결 원본과 동일(한국어 위주 전환 후 기준으로 재동결).

    ⚠ 2026-08-22: **규칙 6(비언어 발성 길이)** 을 새로 세웠다 — 웃음으로 시작한 턴이
      19.3초·39.1초까지 늘어졌고(call 1136), barge-in off 라 그 동안 학습자 마이크가
      서버로 안 간다. ⛔ **규칙 5(응답 길이)는 한 바이트도 안 건드렸다** — 처음에 규칙 5 에
      붙였다가 되돌렸다(길이 규칙에 두 대상을 묶으면 할 말까지 줄어든다). 동결본에도
      **같은 치환만** 넣었다. ⛔ 이 변경도 Live 출력을 실제로 바꾼다.

    ⚠ 2026-08-13: 응답 길이 문구의 숫자가 `{max_sentences}` 자리표시자가 됐다(서버 상한과
      **같은 숫자**여야 해서 — 손으로 쓴 숫자가 두 곳에 있으면 모순이 재발한다).
      그래서 비교는 **기본값을 채운 뒤**에 한다. 이러면 성질이 하나 더 붙는다:
      **상한이 기본(4)이면 문자열이 예전과 한 바이트도 다르지 않다** = Live 무변경.
    """
    rendered = pp._INVARIANTS_TEMPLATE.replace(
        "{max_sentences}", str(pp._DEFAULT_MAX_SENTENCES)
    )
    assert rendered == _ORIG_INVARIANTS_TEMPLATE


def test_close_protocol_constant_matches_original_paragraph():
    """추출된 상수 == 원본 규칙 2 블록(번호 제외) — 문구 리라이트 없음.

    규칙 2 는 이제 헤더 + 서브불릿 여러 줄이라, '2. ' 시작 줄부터 다음 규칙('3. ')
    직전까지를 통째로 잘라 비교한다.
    """
    lines = _ORIG_INVARIANTS_TEMPLATE.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("2. 대화 지속"))
    end = next(i for i, ln in enumerate(lines) if ln.startswith("3. "))
    orig_rule2 = "\n".join(lines[start:end])
    assert "2. " + pp._RULE_CLOSE_PROTOCOL == orig_rule2


# --------------------------------------------------------------------------- #
# 1-b) 밴드별 언어 정책(2026-07 한국어 위주 전환) — 규칙 3 lang_policy 주입.
#      survival/beginner/intermediate/advanced 각 정책 문구 주입 + 미상 밴드 폴백,
#      옛 "10%/90%" 문구 부재, 전 밴드 공통 규칙 존재.
# --------------------------------------------------------------------------- #

_LANG_BASE = dict(
    role="장난기 많은 비버 선생님",
    personality="유쾌하고 텐션 높은 말투",
    level_profile="레벨 3: 짧은 과거형 문장을 만들 수 있다.",
    locale="en",
    interests=["K-pop", "요리"],
    name="Alex",
)

# 밴드 → 그 밴드에서만 나와야 하는 대표 정책 문구.
_BAND_MARK = {
    "survival": "[왕초보 — 대화]",
    "beginner": "[초급 — 대화]",
    "intermediate": "[중급 — 대화]",
    "advanced": "[고급 — 대화]",
}


def test_lang_band_policy_injected_per_band():
    """4밴드 각각 자기 정책 문구만 규칙 3에 실리고, 다른 밴드 문구는 섞이지 않는다."""
    for band, mark in _BAND_MARK.items():
        out = build_system_instruction(lang_band=band, **_LANG_BASE)
        assert mark in out, band
        # 다른 밴드의 대표 문구는 새지 않는다(정책 상호배제)
        for other, other_mark in _BAND_MARK.items():
            if other != band:
                assert other_mark not in out, (band, other)


def test_lang_band_old_ratio_wording_removed():
    """옛 code-switching 기준('10%'·'모국어(90%)'·'레벨과 상관없이 모국어 비중')이 전 밴드에서 사라졌다."""
    for band in _BAND_MARK:
        out = build_system_instruction(lang_band=band, **_LANG_BASE)
        assert "10%" not in out, band
        assert "90%" not in out, band
        assert "모국어 비중을 크게" not in out, band
        assert "(10%)+모국어" not in out, band


def test_lang_band_unknown_falls_back_to_beginner():
    """미상 밴드(예 'unknown')는 beginner 정책으로 폴백(보수적)."""
    out_unknown = build_system_instruction(lang_band="unknown", **_LANG_BASE)
    out_beginner = build_system_instruction(lang_band="beginner", **_LANG_BASE)
    assert out_unknown == out_beginner
    assert _BAND_MARK["beginner"] in out_unknown


def test_lang_band_default_is_beginner():
    """lang_band 미지정 = beginner 정책(load_call_setup 미확정 폴백 level 2 와 정합)."""
    assert (
        build_system_instruction(**_LANG_BASE)
        == build_system_instruction(lang_band="beginner", **_LANG_BASE)
    )


def test_rule3_common_invariants_present_all_bands():
    """규칙 3 전 밴드 공통 규칙: 한국어 착지·자문자답 금지·몰이해 시 모국어 설명·산출 0 금지."""
    for band in _BAND_MARK:
        out = build_system_instruction(lang_band=band, **_LANG_BASE)
        # 모국어 발판 → 한국어 질문 끝 착지
        assert "맨 끝을 한국어 질문·요청으로 착지시켜라" in out, band
        # 자문자답 금지
        assert "자문자답 금지" in out, band
        # 막힘·설명 → 모국어로 설명(⑤+⑥ 병합)
        assert '"모르겠어요/뭐라고요?"' in out, band
        assert "설명 문장 전체를 영어(English)로 하고" in out, band
        # 한국어 산출 0 금지
        assert "학습자 차례를 한국어 산출 0으로 끝내지 마라" in out, band
        # 절·문장 단위 코드스위칭
        assert "코드스위칭은 절·문장 단위로만" in out, band


def test_mother_tongue_header_drops_old_ratio_hint():
    """[모국어] 헤더에서 '모국어 위주로 사용한다' 부분이 삭제됐다(라벨 한 줄만)."""
    out = build_system_instruction(**_LANG_BASE)
    assert "[모국어] 학습자의 모국어는 영어(English)다." in out
    assert "학습자의 이해를 돕기 모국어 위주로 사용한다" not in out


def test_reground_reminder_language_wording_updated():
    """재접지 리마인더 끝 문구: '언어 배분은 그대로' → '언어 사용 규칙은 처음 지시받은 대로'."""
    rg = pp.build_reground_reminder("바바", "시크한 독설가")
    assert "언어 사용 규칙은 처음 지시받은 대로 유지 — 캐릭터 톤만 되살려라" in rg
    assert "언어 배분은 그대로 유지" not in rg


# --------------------------------------------------------------------------- #
# 2) 레벨테스트 대본
# --------------------------------------------------------------------------- #

_LT_KWARGS = dict(
    role="장난기 많은 비버 선생님",
    personality="유쾌하고 텐션 높은 말투",
    locale="en",
    interests=["K-pop", "요리"],
    name="Alex",
)


def test_leveltest_continuation_rule_and_no_readout():
    """레벨테스트는 자체 슬림 대화지속 문단을 갖는다(공유 _RULE_CLOSE_PROTOCOL 미사용).

    핵심 불변: 대화를 이어가라는 전진 지시 + 대괄호 낭독 금지. 종료 개념은 두 대본
    어디에도 없다(근거는 test_prompt_never_mentions_closing)."""
    normal = build_system_instruction(
        level_profile="레벨 3", history=None, **_LT_KWARGS
    )
    lt = build_leveltest_instruction(**_LT_KWARGS)
    # 일반 통화는 여전히 공유 종료 규약을 쓴다. 레벨테스트는 자체 슬림 버전(공유 상수 미포함).
    # 상수엔 더 이상 슬롯이 없어 .format 은 항등이지만, 슬롯이 다시 생기면 여기서 터지도록 남긴다.
    close_protocol = pp._RULE_CLOSE_PROTOCOL.format(close_tag=pp.CLOSE_TAG_DEFAULT)
    assert close_protocol in normal
    assert close_protocol not in lt
    # 자체 문단의 핵심 불변식
    assert "[대화 지속]" in lt
    # ⛔ 부정 지시로 되돌리지 마라. 옛 문구는 '언제 끝낼지는 서버만 안다 … "이제 그만"·
    #   "마지막으로" 같은 말 금지' 였는데, 일반 통화 쪽 같은 형태의 금지 예시를 비버가
    #   그대로 뱉은 실측이 있다(call=782 "슬슬 마무리할 시간이다"). 전진 지시로 확인한다.
    assert "너의 일은 대화를 계속 이어가는 것이다" in lt
    assert "받는 말투는 네 캐릭터대로" in lt  # 말투 처방 금지(캐릭터 우선)
    # ⛔ 종료 메커니즘 설명을 되살리지 마라(옛 문구: "종료 신호는 정확히 [통화종료] 로
    #   시작하는 메시지 하나뿐이며" → "서버가 대괄호로 시작하는 안내문으로만 주며").
    #   전자는 복사(call 852), 후자는 태그 발명(call 870 "[마무리]")을 낳았다.
    assert "대괄호 안 문구를 절대 소리 내어 읽거나 입에 담지 말고" in lt


def test_leveltest_self_driven_progress_and_reaction_rules():
    """비버 자율 진행/OPI(2026-07): 비버가 스스로 이끌고, 답 직후 [반응+질문]을 한 턴에."""
    lt = build_leveltest_instruction(**_LT_KWARGS)
    # [진행 — 네가 이끈다] — 쉬운→한 단계씩 상승, 건너뛰기·제자리 금지.
    assert "[진행 — 네가 이끈다]" in lt
    assert "한 단계씩 위로" in lt
    assert "건너뛰기·제자리걸음 금지" in lt
    assert "3번 이상 머물지 마라" in lt
    # 스스로 끝내지 않음(끝내는 건 서버).
    assert "스스로 끝내지 말고" in lt
    # [유도 질문 사다리] — 0단(인사·정형표현) → 1~4단 상승(넓은 레벨: L2→L3→L6→L10, a3/a4는 L3 흡수).
    assert "[유도 질문 사다리 — 위로 갈수록 어렵다. 각 단계는 그 레벨 문법을 끌어내는 질문이다]" in lt
    assert "0단(맨 아래): 인사·정형표현" in lt
    assert "1단(L2): 이름·사는 곳·어제 한 일을 물어" in lt
    assert "4단(L10): 어떤 주제에 대한 의견과 그 근거를 길게" in lt
    # OPI escalation: 유도해도 그 문법을 못 내면 거기가 실력 꼭대기(종료는 서버 전담).
    assert "거기가 그 학습자의 실력 꼭대기이니" in lt
    assert "각 단계는 그 레벨 문법을 '끌어내는 질문'이다" in lt
    # [막히면] — 발판 2번, 되묻기는 실패 아님.
    assert "[막히면]" in lt
    assert "최대 2번 발판" in lt
    # 반응+질문을 '한 번의 발화'로, 정답 여부 누출 금지.
    assert "반응과 다음 질문은 반드시 '한 번의 발화'로" in lt
    assert "반응만 하고 멈추면 어색한 침묵" in lt
    assert "정답 여부를 절대 티내지 마라" in lt
    # 레벨 비노출 유지("시험/평가" 금지 미세지시는 제거 — 담백함 우선).
    assert "레벨·점수 언급 금지" in lt
    assert '"시험/평가" 언급 금지' not in lt
    # 순수 시험관: 틀려도 고쳐주지/정답 불러주지 마라(교정·반복 드릴 금지).
    assert "정답을 불러주거나 고쳐주지 마라" in lt
    assert "너는 가르치지 않고 재기만 한다" in lt
    # 옛 서버 주도 주입 흔적 제거.
    assert '모든 질문은 서버가 "[다음]"으로 준다' not in lt
    assert "[다음]" not in lt


def test_leveltest_omits_character_persona_uses_examiner_line():
    """레벨테스트는 '순수 배치 테스트' 관점 — 캐릭터 페르소나를 대본에 주입하지 않고
    고정 '시험관' 한 줄로 대체한다(캐릭터 톤 누출·한국어 과다(call 163) 방지).
    role/personality/rules 는 시그니처 호환용으로만 받는다(주입 0)."""
    lt = build_leveltest_instruction(**_LT_KWARGS)
    # 고정 시험관 + 캐릭터 연기 배제 + 측정 우선
    assert "시험관이다" in lt
    assert "캐릭터 연기 말고" in lt
    assert "실력만 담백하게 파악한다" in lt          # 측정 목적
    # 캐릭터 3필드(_LT_KWARGS)는 어디에도 주입되지 않는다
    assert "장난기 많은 비버 선생님" not in lt
    assert "유쾌하고 텐션 높은 말투" not in lt
    assert "캐릭터별 추가 규칙" not in lt


def test_leveltest_has_no_old_probe_plan_and_keeps_language_rule():
    """비버 자율 진행/OPI(2026-07): 옛 서버 주입식 프로빙 플랜 블록(계단 번호·probe_plan)이
    사라지고(자율 난이도 사다리로 대체), 질문=모국어·답=한국어 언어 규칙은 유지된다.
    build_leveltest_instruction 은 probe_plan 인자를 더는 받지 않는다."""
    lt = build_leveltest_instruction(**_LT_KWARGS)
    # 옛 프로빙 플랜 흔적 제거(자율 진행의 난이도 '사다리'와는 별개 — 계단 번호 어법은 폐기)
    assert "[단계 상승 프로빙 — 질문 사다리]" not in lt
    assert "0계단" not in lt
    assert "5계단" not in lt
    assert "추상 논증" not in lt
    # 질문=모국어, 대답=목표어 유도(측정은 학습자의 목표어 발화)
    assert "재는 건 오직 학습자의 한국어 발화다" in lt
    assert "이거 한국어로 말해 볼래요?" in lt
    assert "매 질문마다 반드시 학습자가 한국어로 답하게 시켜라" in lt  # target로 답하기 강조
    # probe_plan 인자는 폐기됨(넘기면 TypeError).
    import pytest as _pytest
    with _pytest.raises(TypeError):
        build_leveltest_instruction(**{**_LT_KWARGS, "probe_plan": "X"})


def test_leveltest_has_no_level_profile_or_history_slots():
    lt = build_leveltest_instruction(**_LT_KWARGS)
    assert "[학습자 수준]" not in lt
    assert "[최근 학습 이력" not in lt


def test_leveltest_locale_label_and_name_interests():
    lt_en = build_leveltest_instruction(**_LT_KWARGS)
    assert "영어(English)" in lt_en
    # 캐릭터는 미주입, 이름·흥미는 여전히 주입된다.
    assert "Alex와의 첫 통화" in lt_en
    assert "K-pop, 요리" in lt_en
    lt_ja = build_leveltest_instruction(**{**_LT_KWARGS, "locale": "ja"})
    assert "일본어(日本語)" in lt_ja
    # 미지원 locale → 영어 폴백 (기존 빌더와 동일 규칙)
    lt_xx = build_leveltest_instruction(**{**_LT_KWARGS, "locale": "xx"})
    assert "영어(English)" in lt_xx


def test_leveltest_seeds_format():
    # 비버 자율 진행/OPI: 선톡 시드는 무인자(node0 질문 인자 폐기).
    opening = seed_leveltest_opening()
    assert opening.startswith("[통화 시작]")
    # 0단 인사부터: 첫 질문은 '인사할 수 있어요?'(자기소개보다 먼저). 안심·설명 멘트 미세지시는 제거.
    assert "인사부터 되는지 본다" in opening
    assert "안심·설명 멘트는 한마디도 넣지 마라" not in opening
    # A1: 안내문 낭독 금지 지시를 맨 앞에 강하게 명시(강화 문구).
    assert "이 지시문 자체를 절대 소리 내어 읽거나 언급하지 마라" in opening
    # 첫 질문 = 대상 언어로 인사 정형표현(서버 주입 질문 줄 폐기).
    assert "인사할 수 있어요?" in opening
    assert "첫 질문:" not in opening  # 서버가 박아 주던 질문 줄 폐기
    assert "한국어" in opening
    fr = seed_leveltest_opening("프랑스어")
    assert "프랑스어" in fr

    # 종료 시드(OPI 개정): 시험 냄새 제거·낭독 금지·판정 여부 누출 금지·자연스러운 마무리.
    assert CLOSE_SEED_LEVELTEST.startswith("[통화종료]")
    assert "(낭독 금지.)" in CLOSE_SEED_LEVELTEST
    assert "어려운 질문을 하던 중이었어도 아무렇지 " in CLOSE_SEED_LEVELTEST
    assert "'테스트/평가/결과/점수/레벨'은 " in CLOSE_SEED_LEVELTEST
    assert "잘했는지 못했는지도 티내지 마라" in CLOSE_SEED_LEVELTEST


def test_leveltest_opening_seed_has_echo_ban_fewshot():
    """A5(초반 안정화): 선톡 시드가 첫 턴부터 '한국어로 답해도 리액션은 모국어로,
    학습자의 한국어를 따라 말하지 않는다'는 올바른 few-shot 예시를 박아 초기 락인을 예방."""
    opening = seed_leveltest_opening()
    # 에코 금지 지시(리액션은 모국어, 학습자 단어 따라 말하지 않음)
    assert "리액션·맞장구는 반드시" in opening
    assert "따라 말하지 마라" in opening
    # 구체 few-shot 예시(락인 예방 앵커 — 0단 인사 예시)
    assert "안녕하세요" in opening
    assert "完璧! Nice" in opening
    # target_language 치환이 예시에도 적용된다(f-string 버그 회귀 방지)
    fr = seed_leveltest_opening("프랑스어")
    assert "대답만 프랑스어로 하도록 이끈다" in fr
    assert "{target_language}" not in fr


def test_leveltest_echo_ban_is_emphasized_language_rule():
    """에코 금지(★)가 [언어] 섹션에서 강조된 규칙으로 부각되고, 리액션은 모국어로만."""
    lt = build_leveltest_instruction(**_LT_KWARGS)
    assert "[언어 — 가장 중요]" in lt
    assert "학습자가 말한 한국어를 절대 따라 말하지 마라(에코 금지)" in lt
    assert "리액션·맞장구도 반드시 영어(English)로만" in lt
    # 응답 길이 규칙은 유지(옛 번호 뭉치는 폐기).
    assert "매 응답은 1~2문장으로 짧게" in lt


def test_leveltest_no_ceiling_function_block():
    """서버 주도(2026-07): 비버는 종료 함수가 없다 — 천장 신호 블록·함수명이 사라진다.
    통화를 언제 끝낼지는 전부 서버가 정한다(종료 규약)."""
    lt = build_leveltest_instruction(**_LT_KWARGS)
    assert "[천장 신호" not in lt
    assert "leveltest_ceiling_reached" not in lt
    assert "천장" not in lt
    # 대화 지속(비버가 먼저 끝내지 않는다) — 슬림 자체 문단에 유지.
    assert "[대화 지속]" in lt
    assert "너의 일은 대화를 계속 이어가는 것이다" in lt


def test_leveltest_question_seed_symbol_removed():
    """비버 자율 진행/OPI(2026-07): 서버 주입 시드 함수는 완전히 폐기됐다.
    call_session 은 더는 이 심볼을 호출하지 않는다(주입 기계 제거)."""
    assert not hasattr(pp, "build_leveltest_question_seed")


def test_normal_seed_opening_unchanged():
    """기존 선톡 시드 회귀 방지(레벨테스트 시드 추가가 기존 시드를 건드리지 않음)."""
    assert pp.SEED_OPENING == pp.seed_opening()
    assert "오늘 한국어를 공부할지" in pp.SEED_OPENING
    # ⛔ 한국어 리터럴 예문을 되살리지 마라(2026-08-31, call 1256). 시드가 완성된 한국어
    #   문장을 따옴표로 건네자 두두가 "학습자의 모국어로"를 무시하고 그대로 낭독했다.
    assert "수다 떨래?'" not in pp.SEED_OPENING
    assert "학습자의 모국어로 해라" in pp.SEED_OPENING


# --------------------------------------------------------------------------- #
# 3) P2-b 체크판 통화 블록 (공부/대화/최근 소재/승급 알림)
#    설계: docs/20260709_1346_level-system-detailed-mechanics.md ②③④⑧.
# --------------------------------------------------------------------------- #

import core.curriculum_hints as ch  # noqa: E402

_BASE_KWARGS = dict(
    role="장난기 많은 비버 선생님",
    personality="유쾌하고 텐션 높은 말투",
    level_profile="레벨 3: 짧은 과거형 문장을 만들 수 있다.",
    locale="en",
    interests=["K-pop", "요리"],
    name="Alex",
)

_STUDY_ITEMS = [
    {"slot": "main", "kind": "review", "obj": "V-았어요/었어요",
     "ex": "어제 영화를 봤어요.", "des": None},
    {"slot": "main", "kind": "grammar", "obj": "V-(으)ㄹ 거예요",
     "ex": "주말에 여행을 갈 거예요.", "des": "미래 계획을 말할 때"},
    {"slot": "main", "kind": "vocab", "obj": "여행", "ex": None, "des": None},
    {"slot": "reserve", "kind": "vocab", "obj": "계획", "ex": None, "des": None},
]

_STUDY_ITEMS_L1 = [
    {"slot": "main", "kind": "chunk", "obj": "얼마예요?",
     "ex": "이거 얼마예요?", "des": None},
    {"slot": "main", "kind": "vocab", "obj": "물", "ex": None, "des": None},
    {"slot": "reserve", "kind": "vocab", "obj": "밥", "ex": None, "des": None},
]

_KNOWN_ITEMS = {
    "grammar": ["N은/는 N이에요/예요", "V-아요/어요"],
    "targets": [
        {"obj": "주세요", "ex": "물 주세요.", "hint": "카페에서 주문하는 상황"},
        {"obj": "얼마예요?", "ex": None, "hint": None},
    ],
}


def test_new_args_all_none_is_byte_identical_to_legacy():
    """체크판 신 인자 기본값(None/False) 명시 호출 == 동결 원본(beginner 정책) 조립.

    ⚠ 재정의(2026-07 한국어 위주 전환): 이 테스트의 "종전"은 이제 lang_band 미지정=beginner
    정책 기준이다(옛 "모국어 90%" 아님). study_items/known_items/recent_topics/promotion
    미제공이면 밴드 언어 정책만 규칙 3에 실린 채 나머지는 종전과 동일함을 증명한다.
    """
    for desc, kwargs in _SNAPSHOT_CASES:
        got = build_system_instruction(
            study_items=None, known_items=None, recent_topics=None,
            promotion_notice=False, **kwargs,
        )
        assert got == _orig_build(**kwargs), desc


def test_rule1_default_bullets_are_substring_of_template():
    """규칙 1 교체가 의존하는 replace 대상이 템플릿 원문에 실제로 존재한다."""
    assert pp._RULE1_MODE_DEFAULT in pp._INVARIANTS_TEMPLATE


def test_study_block_render_and_rule1_swap():
    out = build_system_instruction(study_items=_STUDY_ITEMS, **_BASE_KWARGS)
    # 블록 헤더(상호배제) + 본편/예비 구분
    assert "[오늘의 공부 항목 — 공부 모드일 때만 따르라. 대화 모드에서는 이 블록을 무시하라]" in out
    assert "본편:" in out
    assert "이어서(본편을 끝내면 여기서 계속):" in out
    # 항목 줄 렌더(유형라벨/예문/참고/예문 폴백) — 번호는 본편→예비 연속
    assert '1. (복습) V-았어요/었어요 — 예문: "어제 영화를 봤어요."' in out
    assert '2. (문법) V-(으)ㄹ 거예요 — 예문: "주말에 여행을 갈 거예요." — 참고: 미래 계획을 말할 때' in out
    assert "3. (단어) 여행 — 예문은 네가 즉석에서 만들라" in out
    assert "4. (단어) 계획 — 예문은 네가 즉석에서 만들라" in out
    # 진행 규칙 핵심 문구
    assert "이 목록의 존재·남은 개수·진행률을 학습자에게 절대 발설하지 마라" in out
    assert "다 못 끝내도 괜찮다. 서두르지 마라" in out
    # ⛔ 부정 지시("작별하지 마라")로 되돌리지 마라 — 금지 예시를 비버가 그대로
    #   뱉은 실측이 있다(call=782 "슬슬 마무리할 시간이다"). 전진 지시로만 확인한다.
    # ⛔ 조건절("~다 끝났는데 통화가 계속되면")로도 되돌리지 마라 — 모델이 조건 서술을
    #   상태 서술로 읽고 실행한다(call 870: "본편이 끝났다" → 4분 24초에 자체 종료).
    #   재료 소진을 '끝'이 아니라 '다음 단계'로 서술하는 형태를 고정한다.
    assert "재료를 다 쓴 뒤에도 대화는 그대로 이어진다" in out
    assert "학습자의 관심사로 새 화제를 꺼내라" in out
    assert "통화가 계속되면" not in out, "재료 소진을 조건절로 되돌렸다(call 870 재발 경로)"
    assert "최대 2번까지만 다시 시도해라" in out
    # 일반(비 L1) 절차 — 문법 절차 존재(교정은 규칙4로 위임), 왕초보 변형 아님
    assert "유형별 절차:" in out
    assert "만들게 한다(교정은 불변 규칙 4대로)" in out
    assert "왕초보" not in out
    # 규칙 1 교체판 적용(기본판 불릿은 사라짐)
    rule1_default = pp._RULE1_MODE_DEFAULT.format(target="한국어")
    rule1_swap = pp._RULE1_MODE_CHECKBOARD.format(target="한국어")
    assert rule1_swap in out and rule1_default not in out


def test_study_block_l1_variant_when_chunk_without_grammar():
    out = build_system_instruction(study_items=_STUDY_ITEMS_L1, **_BASE_KWARGS)
    assert "이 학습자는 한국어 왕초보다" in out
    assert "유형별 절차(왕초보):" in out
    assert "문법 용어(조사·어미·활용·시제 같은 말)를 절대" in out
    assert "1. (통문장) 얼마예요?" in out
    # 일반판 문법 절차는 없어야 한다(왕초보는 문법 절차 자체가 없음)
    assert "만들게 한다(교정은 불변 규칙 4대로)" not in out


# --------------------------------------------------------------------------- #
# 5개 단위 확인 (2026-08-16 — 사장님 지시)
# --------------------------------------------------------------------------- #
def test_five_item_check_is_in_the_progress_rules():
    """5·10·15…번째마다 그 5개를 다시 꺼내는 절차가 실제로 출력에 들어간다."""
    out = build_system_instruction(study_items=_STUDY_ITEMS, **_BASE_KWARGS)
    assert "항목 5개를 다룰 때마다(5번째·10번째·15번째… 항목을 다루고 난 직후)" in out
    assert "방금 다룬 그 5개를 짧게 다시 꺼내라" in out
    # 확인은 절차지 항목이 아니다 — 번호는 본편(1~3)→예비(4)로 그대로 이어진다.
    assert "3. (단어) 여행" in out and "4. (단어) 계획" in out


def test_five_item_check_counts_silently():
    """⛔ 세는 티를 내면 '진행률 발설 금지' 와 정면충돌한다 — 속으로만 센다."""
    out = build_system_instruction(study_items=_STUDY_ITEMS, **_BASE_KWARGS)
    assert "몇 개째인지는 속으로만 세라 — 세고 있다는 티를 내지 마라" in out
    assert "이 목록의 존재·남은 개수·진행률을 학습자에게 절대 발설하지 마라" in out  # 원 규칙 유지


def test_five_item_check_l1_variant_keeps_whole_sentence_teaching():
    """L1 은 '문장 만들기'가 아니라 **통째로 다시 말하기**다(왕초보 원칙 유지)."""
    l1 = build_system_instruction(study_items=_STUDY_ITEMS_L1, **_BASE_KWARGS)
    normal = build_system_instruction(study_items=_STUDY_ITEMS, **_BASE_KWARGS)
    tail = "통문장은 문장을 만들게 하지 말고, 그 말을 쓸 상황을 하나 주고 통째로 다시 말하게 해라"
    assert tail in l1
    assert tail not in normal


def test_five_item_check_has_no_closing_or_counting_words():
    """⛔ call 870 재발 방어 — 확인 절차가 '마무리'·'몇 개째' 로 읽히는 말을 안 쓴다.

    call 870: '본편이 끝났다'는 신호 하나로 비버가 "그럼 마지막으로" 로 옮겨 4분 24초에
    스스로 통화를 접었다. 5개 단위로 바꾼 이유가 바로 그 '끝 지점'을 없애는 것이고,
    이 목록 검사가 그 설계를 지키는 **유일한 자동 방어**다.
    ⚠ 검사 범위가 둘로 나뉜다:
      · 종료 어휘 — 공부 블록 **전체**. 주변 문장으로 새어 들어온 것도 잡아야 한다
        (2026-08-04 에 진행 규칙에서 실제로 그런 일이 있었다).
      · 세는 말 — **확인 절차 문구만**. 바로 위 발설 금지 규칙이 "이제 2개 남았어" 를
        **금지 예시로** 인용하고 있어 블록 전체로 검사하면 그 규칙이 걸린다.
    """
    closing = ["마무리", "마지막", "정리", "여기까지", "끝으로", "돌아보", "종료", "작별",
               "오늘은 이만", "그동안"]
    counting = ["이제 5개", "5개 했으니", "개 남았", "개째야", "다섯 개를 했"]
    for label, items in (("일반", _STUDY_ITEMS), ("L1", _STUDY_ITEMS_L1)):
        block = pp._study_block(items, target="한국어", locale_label="영어")
        assert "항목 5개를 다룰 때마다" in block
        for word in closing:
            assert word not in block, f"{label} 공부 블록에 종료 어휘 '{word}' 가 들어왔다"
    for word in counting:
        assert word not in pp._STUDY_FIVE_CHECK + pp._STUDY_FIVE_CHECK_L1_TAIL, \
            f"확인 절차 문구가 개수를 세어 말한다: '{word}'"


def test_five_item_check_absent_without_study_items():
    """⛔ 하위호환 — study_items 미제공이면 확인 절차도 없다(바이트 동일은 위 스냅샷 시험)."""
    out = build_system_instruction(study_items=None, **_BASE_KWARGS)
    assert "항목 5개를 다룰 때마다" not in out
    assert out == build_system_instruction(**_BASE_KWARGS)


# --------------------------------------------------------------------------- #
# 왕초보 판별은 **밴드**가 정한다 (2026-08-17)
# --------------------------------------------------------------------------- #
_STUDY_ITEMS_ALL_REVIEW = [
    {"slot": "main", "kind": "vocab", "state": "review", "obj": "얼마예요?",
     "ex": None, "des": None},
    {"slot": "reserve", "kind": "vocab", "state": "again", "obj": "안녕히 가세요",
     "ex": None, "des": None},
]

# 유형 × 상태 — 세 상태가 한 목록에 다 들어간 경우.
_STUDY_ITEMS_STATES = [
    {"slot": "main", "kind": "chunk", "state": "review", "obj": "안녕히 가세요",
     "ex": None, "des": None},
    {"slot": "main", "kind": "chunk", "state": "again", "obj": "아니에요",
     "ex": None, "des": None},
    {"slot": "main", "kind": "chunk", "state": "new", "obj": "하나 둘 셋",
     "ex": None, "des": None},
]


def test_type_and_state_are_two_separate_labels():
    """⭐ 항목 줄에 **유형·상태**가 둘 다 실린다 — 한 칸에 섞으면 놓을 자리가 없는 상태가 생긴다.

    실측(member 20, ko L1): 청크 46개 중 미학습 0 / introduced 23 / practicing 22.
    '한 번 들었지만 아직 못 하는' 23개를 놓을 칸이 없어 계속 '신규'로 나갔고, 신규 절차는
    정의상 E1(모방)만 만든다(call 1053 증거: E1 7 · E3 1 · **E2 0**).
    """
    out = build_system_instruction(
        study_items=_STUDY_ITEMS_STATES, lang_band="survival", **_BASE_KWARGS
    )
    assert "1. (통문장·복습) 안녕히 가세요" in out
    assert "2. (통문장·다시) 아니에요" in out
    assert "3. (통문장·새로) 하나 둘 셋" in out
    # 상태별 시작 절차(합 — 유형×상태 곱셈 금지)
    assert "상태별 시작(항목 뒤 라벨):" in out
    assert "- 새로: 위 유형별 절차대로 가르쳐라" in out
    assert "- 다시: 먼저 물어봐라. 못 하면 상황 힌트를 한 번 주고 다시 물어라." in out
    assert "- 복습: 먼저 물어봐라. 못 하면 정답을 한 번 들려주고 한 번 따라 말하게 한 뒤" in out


def test_state_labels_do_not_trip_the_closing_denylist():
    """⛔ 라벨이 종료 어휘면 라벨만으로 통화가 끝난다 — 세 낱말 다 denylist 밖이어야 한다."""
    for word in pp._STUDY_STATE_LABEL.values():
        assert not pp.is_closing_slot(word), word
        assert len(word) <= 2, f"라벨이 길다(항목 30줄에 곱해진다): {word}"


def test_items_without_a_state_render_the_old_way():
    """⚠ 하위호환 — state 없는 옛 dict 는 유형만 찍는다(데모·저장된 픽스처)."""
    out = build_system_instruction(study_items=_STUDY_ITEMS, **_BASE_KWARGS)
    assert "1. (복습) V-았어요/었어요" in out and "·" not in out.split("본편:")[1][:40]


def test_state_labels_cost_almost_nothing_in_prompt_length():
    """⭐ 지시문 순증 상한 — 항목 30개에서 **300자 이내**(프롬프트가 터지면 지시가 꼬인다).

    비교 대상은 같은 30항목을 **state 없이**(=이 변경 전 모양) 렌더한 지시문이다.
    라벨 3자 × 30줄 + 상태별 시작 절차가 순증의 전부이고, 그 대신 유형 목록에서 옛
    '- 복습:' 줄이 빠졌다(유형이 아니라 상태였다).
    """
    items = [
        {"slot": "main" if i < 5 else "reserve", "kind": "chunk",
         "state": ("new", "again", "review")[i % 3], "obj": f"문장{i}",
         "ex": None, "des": None}
        for i in range(30)
    ]
    legacy = [{k: v for k, v in it.items() if k != "state"} for it in items]
    with_states = build_system_instruction(
        study_items=items, lang_band="survival", **_BASE_KWARGS
    )
    without = build_system_instruction(
        study_items=legacy, lang_band="survival", **_BASE_KWARGS
    )
    delta = len(with_states) - len(without)
    assert 0 < delta <= 300, f"지시문 순증 {delta}자 — 300자 상한을 넘었다"


def test_survival_band_keeps_the_beginner_variant_without_any_chunk_item():
    """⛔⛔ L1 청크를 다 건드린 회원(미학습 0)도 왕초보 모드가 켜져 있어야 한다.

    이월분을 'review' 로 옳게 라벨하면 목록에 chunk 가 한 개도 안 남는다. 옛 판별은
    거기서 False 로 뒤집혀 왕초보 문구·절차·5단위 L1 꼬리가 통째로 꺼졌다 —
    생존회화도 안 되는 학습자에게 조사·어미 용어가 나가기 시작한다.
    """
    out = build_system_instruction(
        study_items=_STUDY_ITEMS_ALL_REVIEW, lang_band="survival", **_BASE_KWARGS
    )
    assert "이 학습자는 한국어 왕초보다" in out
    assert "유형별 절차(왕초보):" in out
    assert "통문장은 문장을 만들게 하지 말고" in out       # 5단위 확인의 L1 꼬리
    assert "만들게 한다(교정은 불변 규칙 4대로)" not in out  # 일반 문법 절차는 여전히 없다


def test_a_higher_band_does_not_get_the_beginner_variant():
    """짝 — 밴드가 survival 이 아니면 왕초보 변형은 안 켜진다(항목이 복습뿐이어도)."""
    out = build_system_instruction(
        study_items=_STUDY_ITEMS_ALL_REVIEW, lang_band="beginner", **_BASE_KWARGS
    )
    assert "왕초보" not in out


def test_the_old_kind_based_check_still_works_without_a_band():
    """⚠ 밴드를 안 넘기는 호출부(데모 등)는 옛 판별로 폴백한다 — 동작이 안 바뀐다."""
    out = build_system_instruction(study_items=_STUDY_ITEMS_L1, **_BASE_KWARGS)
    assert "이 학습자는 한국어 왕초보다" in out


def test_review_procedure_guarantees_at_least_an_imitation():
    """⭐ 못 답해도 **따라 말하게** 한다 — 안 그러면 E1 조차 안 쌓인다.

    복습 절차가 '정답을 들려주고 넘어가라'로 끝나면, 이월분이 복습으로 바뀐 뒤
    왕초보가 계속 못 답할 때 증거가 **0** 이 된다(예전엔 최소 E1 은 쌓였다).
    """
    for band, items in (("survival", _STUDY_ITEMS_L1), ("beginner", _STUDY_ITEMS)):
        out = build_system_instruction(study_items=items, lang_band=band, **_BASE_KWARGS)
        assert "한 번 따라 말하게 한 뒤" in out, band


def test_known_block_render_and_grammar_join():
    out = build_system_instruction(known_items=_KNOWN_ITEMS, **_BASE_KWARGS)
    assert "[대화 모드 가이드 — 대화 모드일 때만 따르라. 공부 모드에서는 이 블록을 무시하라]" in out
    assert "학습자가 이미 아는 한국어 문법: N은/는 N이에요/예요 · V-아요/어요" in out
    assert '1. "주세요" — 예문: "물 주세요." — 유도 상황: 카페에서 주문하는 상황' in out
    assert '2. "얼마예요?"' in out
    # 유도 규칙
    assert "은근히 해라" in out
    assert "표현당 시도는 최대 2회" in out
    assert "마지막 수단으로 통화 전체에서 1회만" in out
    assert "하나도 못 꺼내도 실패가 아니다" in out
    # 규칙 1 교체판(대화 블록만으로도 교체)
    assert pp._RULE1_MODE_CHECKBOARD.format(target="한국어") in out


def test_known_block_empty_grammar_fallback_phrase():
    out = build_system_instruction(
        known_items={"grammar": [], "targets": _KNOWN_ITEMS["targets"]}, **_BASE_KWARGS,
    )
    assert "현재 레벨 프로파일을 그 범위로 삼아라" in out
    assert "학습자가 이미 아는 한국어 문법:" not in out


def test_no_blocks_keeps_rule1_default_even_with_topics_and_promotion():
    """recent_topics/promotion 만으로는 규칙 1 을 갈지 않는다(블록 전용 트리거)."""
    out = build_system_instruction(
        recent_topics=["주말 계획"], promotion_notice=True, **_BASE_KWARGS,
    )
    assert pp._RULE1_MODE_DEFAULT.format(target="한국어") in out
    assert "[오늘의 공부 항목" not in out and "[대화 모드 가이드" not in out


def test_recent_topics_line():
    out = build_system_instruction(recent_topics=["주말 계획", "음식 주문"], **_BASE_KWARGS)
    assert "[최근 통화 소재] 주말 계획 / 음식 주문 — 이 화제들을 그대로 반복하지 말고" in out


def test_promotion_notice_is_single_last_line():
    out = build_system_instruction(promotion_notice=True, **_BASE_KWARGS)
    assert out.splitlines()[-1] == (
        '[승급 알림] 학습자가 최근 실력이 늘어 오늘부터 조금 더 어려운 내용을 다룬다. '
        "통화 초반에 영어(English)로 '요즘 잘하니까 오늘은 좀 더 어려운 걸 해보자'는 취지를 "
        '네 캐릭터 말투로 한 번만 자연스럽게 언급하라. 레벨·점수·단계 같은 단어는 쓰지 마라.'
    )
    assert out.count("[승급 알림]") == 1


_HISTORY = {
    "summaries": ["주말 계획을 이야기함"],
    "expressions": ["주세요", "얼마예요?"],
}


def test_history_expressions_suppressed_when_known_items():
    """known_items 제공 시 history.expressions 미주입(이중 주입 방지) — summaries 는 유지."""
    out = build_system_instruction(known_items=_KNOWN_ITEMS, history=_HISTORY, **_BASE_KWARGS)
    assert "- 이미 배운 표현:" not in out
    assert "- 최근 통화 요약: 주말 계획을 이야기함" in out


def test_history_summaries_suppressed_when_recent_topics():
    out = build_system_instruction(
        recent_topics=["음식 주문"], history=_HISTORY, **_BASE_KWARGS,
    )
    assert "- 최근 통화 요약:" not in out
    assert "- 이미 배운 표현: 주세요, 얼마예요?" in out


def test_history_fully_ignored_when_both_known_and_topics():
    out = build_system_instruction(
        known_items=_KNOWN_ITEMS, recent_topics=["음식 주문"],
        history=_HISTORY, **_BASE_KWARGS,
    )
    assert "[최근 학습 이력" not in out


# --------------------------------------------------------------------------- #
# 4) curriculum_hints — freetalking 문법↔미션 인덱스
# --------------------------------------------------------------------------- #


def test_hint_for_matches_freetalking_mission():
    """실자산 매칭: 1과 Grammer1 → Misson1 원문(철자 Misson 흡수 확인)."""
    assert ch.hint_for("N은/는 N이에요/예요", None) == (
        "처음 만난 친구에게 자신의 국적을 소개해 보세요."
    )


def test_hint_for_normalizes_whitespace():
    assert ch.hint_for(" N은/는  N이에요/예요 ", None) == (
        "처음 만난 친구에게 자신의 국적을 소개해 보세요."
    )


def test_hint_for_fallback_template():
    unmatched = "존재하지 않는 문법 XYZ"
    assert ch.hint_for(unmatched, "커피를 마셔요.") == (
        "학습자의 관심사 속에서 '커피를 마셔요.'처럼 말하게 될 만한 순간을 만들어라"
    )
    # ex 없으면 obj 로 폴백
    assert ch.hint_for(unmatched, None) == (
        f"학습자의 관심사 속에서 '{unmatched}'처럼 말하게 될 만한 순간을 만들어라"
    )


def test_hint_for_missing_asset_is_graceful(monkeypatch, tmp_path):
    """자산 부재 → 빈 인덱스 → 폴백만(R5 graceful — 예외 없음)."""
    monkeypatch.setattr(ch, "_FREETALKING_JSON", tmp_path / "no_such.json")
    monkeypatch.setattr(ch, "_index", None)  # lazy 캐시 리셋(종료 시 원복)
    out = ch.hint_for("N은/는 N이에요/예요", "저는 학생이에요.")
    assert out == "학습자의 관심사 속에서 '저는 학생이에요.'처럼 말하게 될 만한 순간을 만들어라"


# --------------------------------------------------------------------------- #
# 제어 태그 분리(2026-07-27) — 통화 조기 종료 회귀 방지.
# 옛날엔 종료 시드·재접지·무음 넛지가 모두 "[시스템]" 접두어를 공유했고, 종료 규약이
# 그 접두어 자체를 종료 트리거로 정의한 탓에 재접지·넛지가 종료 신호로 오독됐다
# (실측 call_id=683: 재접지 30초 뒤 "[시스템] 종료 신호가 왔습니다" → 240s 만에 종료).
# 근거: docs/20260727_1710_통화-조기종료-종료태그-분리와-안전망.md
# --------------------------------------------------------------------------- #


def test_reground_reminder_uses_control_tag_not_close_tag():
    """재접지 리마인더는 종료가 아니다 — 접두어가 종료 태그와 겹치면 안 된다."""
    reminder = pp.build_reground_reminder("장난기 많은 비버", "유쾌한 말투")
    assert reminder.startswith(pp.CONTROL_TAG)
    assert not reminder.startswith(pp.CLOSE_TAG_DEFAULT)
    assert "통화종료" not in reminder


def test_close_tag_never_reaches_either_script():
    """⛔ 종료 태그는 어느 대본에도 실리지 않는다 — 비버가 복사해 스스로 종료했다.

    이 테스트는 예전엔 정반대(`assert tag in normal`)였다. 지시문이 태그를 보여주면
    모델이 우연히 맞히는 게 아니라 **그대로 복사한다**: call_id=852(2026-08-01)에서
    비버가 그 통화의 난수 태그 "[통화종료:d963]" 을 서버가 시드를 보내기 2분 39초 전에
    출력하고 작별했다. d963 은 지시문에만 존재하는 값이라 우연일 수 없다. 30일간 8건,
    그중 3건은 재개 상한(_RESUME_MAX=2)까지 소진했다 — 상한을 넘기면 통화가 죽는다.

    태그를 다시 지시문에 넣지 마라. 비버는 태그가 아니라 시드 본문으로 종료를 알아본다.
    """
    tag = "[통화종료:abcd]"
    normal = build_system_instruction(
        level_profile="레벨 3", history=None, close_tag=tag, **_LT_KWARGS
    )
    lt = build_leveltest_instruction(close_tag=tag, **_LT_KWARGS)
    assert tag not in normal
    assert tag not in lt
    # 난수 없는 기본형·접두어도 마찬가지(부분 문자열까지 봉쇄).
    assert "[통화종료" not in normal
    assert "[통화종료" not in lt
    # 옛 접두어는 어느 대본에도 남아 있으면 안 된다(자기낭독 후보 제거).
    assert "[시스템]" not in normal
    assert "[시스템]" not in lt


def test_leveltest_close_seed_carries_tag_but_instruction_does_not():
    """태그는 **시드에만** 실린다. 지시문은 성질만 규정한다.

    시드가 태그로 시작해야 하는 건 모델 때문이 아니라 서버 때문이다 — 누출 탐지
    (_CONTROL_TAG_RE)와 로그·회귀가 이 통화의 시드를 그걸로 식별한다.
    """
    tag = "[통화종료:beef]"
    assert pp.close_seed_leveltest(tag).startswith(tag)
    assert tag not in build_leveltest_instruction(close_tag=tag, **_LT_KWARGS)


def test_new_close_tag_is_per_call_random():
    """통화마다 다른 태그 — 모델이 우연히·낭독으로 재현하기 어렵게."""
    tags = {pp.new_close_tag() for _ in range(20)}
    assert len(tags) > 1
    assert all(t.startswith("[통화종료:") and t.endswith("]") for t in tags)


def test_close_protocol_never_exposes_tag():
    """규약 문단은 태그를 **한 번도** 노출하지 않는다(복사할 원본 제거).

    변천: 3회 노출("[시스템]"을 따옴표째 진열 — call_id=706 낭독) → 1회 노출 + 난수 접미
    (call_id=852 에서 그 난수까지 복사당함) → 0회. 난수는 '우연한 재현'만 막고 '복사'는
    못 막는다는 게 실측으로 확인됐다.
    """
    assert "{close_tag}" not in pp._RULE_CLOSE_PROTOCOL
    assert "통화종료" not in pp._RULE_CLOSE_PROTOCOL


def test_rule3_has_language_drift_guard():
    """★ 학습자가 목표어로 답해도 비버는 모국어를 유지해야 한다.

    실측(call_id=784, Rara·모국어 en·레벨1·공부모드): t4 에서 학습자가 한국어로 답한
    직후 t5 부터 비버의 칭찬·지시가 통째로 한국어로 뒤집혔다. 기존 규칙은 "공부 모드는
    설명·지시·리액션을 모국어로"라고만 했지 **상대가 목표어로 말했을 때**를 안 다뤘다.
    """
    out = build_system_instruction(
        level_profile="레벨 1", history=None, **_LT_KWARGS
    )
    assert "[학습자 언어에 끌려가지 마라]" in out
    assert "학습자가 한국어로 답해도 네 칭찬·교정·지시는 계속 영어(English)로" in out
    # 따라 말하기(공부 모드의 목적)까지 막으면 안 된다 — 인용은 허용해야 한다.
    assert "따라 할 표현만 인용해라" in out


# --------------------------------------------------------------------------- #
# 종료 규약 — 부정 지시 금지(금지 예시가 씨앗이 된 실측 사고)
# --------------------------------------------------------------------------- #
_SEED_PHRASES = ("슬슬 끊자", "마지막으로", "이제 그만")


def test_close_protocol_has_no_forbidden_example_phrases():
    """★ 금지 예시를 프롬프트에 적으면 비버가 그걸 뱉는다.

    실측 call=782: 프롬프트가 '"슬슬 끊자","마지막으로" 등 금지' 라고 적어뒀는데
    비버가 "슬슬 마무리할 시간이다" 라고 했다. 5분 통화 12건 중 3건이 서버 종료
    신호보다 4~16턴 먼저 마무리에 들어갔다(call=836/744/782).
    """
    out = build_system_instruction(**_BASE_KWARGS)
    for p in _SEED_PHRASES:
        assert p not in out, f"금지 예시 {p!r} 가 프롬프트에 다시 들어왔다 — 씨앗이 된다"


def test_leveltest_close_protocol_has_no_seed_phrases():
    lt = build_leveltest_instruction(**_LT_KWARGS)
    for p in _SEED_PHRASES:
        assert p not in lt, f"금지 예시 {p!r} 가 레벨테스트 대본에 다시 들어왔다"


def test_prompt_never_mentions_closing():
    """⛔ 지시문은 '종료'라는 개념 자체를 모른다 — 두 대본 모두.

    옛 계약("종료 신호 정의 + 신호가 오면 마무리 절차")은 **의도적으로 폐기됐다**.
    종료 메커니즘을 가르치면 모델이 그걸 실행 수단으로 쓴다는 게 세 번 확인됐다:
      ① 리터럴 3회 노출 → "[시스템]" 낭독 후 자체 종료(call 706)
      ② 리터럴 1회 + 난수 → 난수까지 복사해 자체 종료(call 852, 서버 시드 2분 39초 전)
      ③ 리터럴 0회 + "대괄호 안내문이 종료 신호" 성질 서술 → 없는 태그 발명(call 870
         "[마무리]" 8회). 지시문 자체가 대괄호 라벨을 11종 쓰므로 흉내낼 문법이 된다.

    종료는 때가 되면 서버가 평범한 지시문으로 넣고(in-context), 낭독 방지는 서버
    필터가 강제한다(call_session._CONTROL_TAG_RE). 지시문이 알 필요가 없다.

    남아야 하는 두 가지: ① 대화를 이어가라는 전진 지시 ② 대괄호 낭독 금지.
    """
    normal = build_system_instruction(**_BASE_KWARGS)
    lt = build_leveltest_instruction(**_LT_KWARGS)
    # ⚠ 블록이 붙은 판까지 반드시 검사한다. 예전엔 맨 지시문만 봤는데, 그 사각지대에서
    #   공부 항목 블록의 진행 규칙이 "종료 신호가 오면 … 통화 종료 규약을 따르라"를
    #   그대로 들고 있었다(2026-08-04 발견). 실제 통화는 거의 다 블록이 붙으므로,
    #   맨 지시문만 통과시키는 검사는 사실상 아무것도 안 지킨 셈이었다.
    with_blocks = build_system_instruction(
        **_BASE_KWARGS,
        study_items=_STUDY_ITEMS,
        known_items=_KNOWN_ITEMS,
        recent_topics=["여행", "음식"],
        promotion_notice=True,
    )
    with_l1 = build_system_instruction(**_BASE_KWARGS, study_items=_STUDY_ITEMS_L1)
    for name, out in (
        ("일반", normal),
        ("레벨테스트", lt),
        ("일반+공부·대화 블록", with_blocks),
        ("일반+L1 청크 블록", with_l1),
    ):
        assert "종료" not in out, f"{name} 대본에 '종료'가 다시 들어왔다"
        assert "통화종료" not in out
        assert "마무리" not in out, f"{name} 대본에 '마무리'가 다시 들어왔다"
        # 남아야 하는 것
        assert "대화를 계속 이어가는 것이다" in out
        assert "소리 내어 읽거나" in out


def test_continue_reminder_never_mentions_closing():
    """★ 후반 재접지가 종료 어휘를 꺼내면 그 자체가 종료 신호가 된다.

    전례: 재접지 리마인더가 종료 시드와 같은 태그를 써서 30초 뒤 작별했다(call_id=683).
    태그는 분리됐지만, 어휘로도 같은 일이 난다.
    """
    from core.persona_prompt import CONTROL_TAG, build_continue_reminder

    r = build_continue_reminder("선생님", "까칠하다")
    for w in ("끝", "종료", "작별", "마무리", "시간", "남은"):
        assert w not in r, f"후반 리마인더에 종료 어휘 {w!r} 가 들어갔다"
    assert r.startswith(CONTROL_TAG), "종료 태그가 아니라 CONTROL_TAG 여야 한다"
    assert "새 질문" in r and "이어가라" in r


def test_continue_reminder_is_distinct_from_reground():
    """중반(캐릭터 톤)과 후반(대화 지속)은 목적이 다르므로 문구도 달라야 한다."""
    from core.persona_prompt import build_continue_reminder, build_reground_reminder

    assert build_continue_reminder("r", "p") != build_reground_reminder("r", "p")


def test_close_protocol_does_not_prescribe_tone():
    """★ 종료 규약이 말투를 처방하면 캐릭터가 약해진다.

    캐릭터는 "교정하는 순간에도 톤을 순화하지 마라"가 원칙이고, 커밋 f8e0ebb 가
    종료 시드에서 작별 말투 처방을 이미 걷어냈다. 같은 원칙이 종료 규약에도 적용된다 —
    "따뜻하게" 같은 부사를 넣지 말고 캐릭터에 위임한다.
    """
    out = build_system_instruction(**_BASE_KWARGS)
    lt = build_leveltest_instruction(**_LT_KWARGS)
    for w in ("따뜻하게", "다정하게", "부드럽게", "친절하게"):
        assert w not in out, f"종료 규약이 말투를 처방한다: {w!r}"
        assert w not in lt, f"레벨테스트 종료 규약이 말투를 처방한다: {w!r}"


def test_close_protocol_has_no_meta_explanation():
    """서버가 길이를 관리한다는 **설명**은 행동을 바꾸지 않는다 — 지시만 남긴다.

    '너는 시간을 세지 말고' 도 결국 부정 지시라 "시간"을 심는다. 뒷줄이 이미
    "너의 일은 대화를 이어가는 것" 이라고 행동을 정하므로 중복이다.
    """
    out = build_system_instruction(**_BASE_KWARGS)
    assert "시간을 세지" not in out
    assert "통화 길이를 모른다" not in out


# --------------------------------------------------------------------------- #
# 이어하기 시드 (2026-08-19)
# --------------------------------------------------------------------------- #
def test_the_resume_seed_never_asks_the_mode_question_again():
    """⛔⛔ 조각2에서 "공부할래 수다 떨래?"를 **다시 물으면 안 된다.**

    실측(call 1087): 이어했는데 비버가 처음과 같은 질문을 다시 했다.
        조각1 t1 : "Hey jaewoo! Ready to study Korean today, or just chat in Korean?"
        조각2 t8 : "Hey, jaewoo! Ready to study Korean today, or would you rather..."
    원인은 `seed_opening` 이 그대로 나간 것이다. 지시문의 브리프에 "인사하지 마라"가 있어도
    **시드가 이긴다** — 시드는 직접 명령이고 지시문은 배경이기 때문이다.
    """
    from core.persona_prompt import seed_opening, seed_resume

    resume = seed_resume("한국어")
    assert "수다" not in resume, resume          # 모드 질문 문구
    assert "인사부터" not in resume, resume
    assert "인사하지 말고" in resume
    assert "다시 묻지 마라" in resume

    # ⛔ 첫 통화 시드는 **그대로**여야 한다 — 거기서는 인사와 모드 질문이 맞다.
    opening = seed_opening("한국어")
    assert "수다" in opening and "인사" in opening


def test_the_resume_seed_does_not_mention_the_disconnect():
    """⛔ "끊겼다 이어졌다"를 비버가 말하면 **끊김이 두 번 일어난다.**

    사용자는 이미 자기가 버튼을 눌러 이었다는 걸 안다.
    """
    from core.persona_prompt import seed_resume

    assert "끊겼다 이어졌다는 말은 하지 마라" in seed_resume()


# --------------------------------------------------------------------------- #
# '방금' 라벨 — **이 통화에서 다뤘나** (2026-08-19)
# --------------------------------------------------------------------------- #
def test_an_item_touched_in_this_call_is_marked():
    """⭐ 상태 축(새로/다시/복습)은 **평생 상태**라 방금 것과 3주 전 것을 못 가른다.

    사장님 지시: "이전에 배웠던 걸 이야기했더라도 복습 시간에는 다뤄야 해."
    ⇒ 기준은 상태가 아니라 **이 통화에서 손댔는지**다.

    ⛔ 범위가 '오늘'이 **아니다**(사장님 정정): "하루에 두 번 통화를 하면 두 개는 다르게
      취급해야지. 하루가 아니라 이번 연달아 통화하는 것들 중에서만이야."
      ⇒ 조각들은 같은 call_id 를 쓰므로 그 하나가 경계다 — 날짜도 타임존도 안 쓴다.
    """
    from core.persona_prompt import _render_study_item

    marked = _render_study_item(1, {"kind": "chunk", "state": "review",
                                    "this_call": True, "obj": "안녕하세요"})
    assert "(통문장·복습·방금)" in marked, marked

    # ⛔ 안 붙는 경우 — 라벨이 늘 붙으면 신호가 죽는다.
    old_item = _render_study_item(1, {"kind": "chunk", "state": "review", "obj": "안녕하세요"})
    assert "방금" not in old_item, old_item
    fresh = _render_study_item(1, {"kind": "grammar", "state": "new", "obj": "-았/었어요"})
    assert fresh.startswith("1. (문법·새로)"), fresh


def test_the_five_check_tells_the_beaver_what_the_mark_means():
    """⛔ 라벨만 붙이고 **무엇을 하라는 말이 없으면** 비버가 알아서 한다.

    `잘 해낸 것`·`헷갈려 하는 것` 에는 지시가 있는데 `다룬 것` 에는 없어서, 처음부터 다시
    가르칠지 건너뛸지가 복불복이었다.
    """
    from core.persona_prompt import _STUDY_FIVE_CHECK

    assert "'방금'이 붙은 항목" in _STUDY_FIVE_CHECK
    assert "처음부터 다시 설명하지 말고" in _STUDY_FIVE_CHECK
    assert "짧게 물어 확인만" in _STUDY_FIVE_CHECK
    # ⭐ 사장님 지시의 핵심 — 이전에 배운 것도 이 통화에서 다뤘으면 대상이다.
    assert "이전에 배운 것이어도" in _STUDY_FIVE_CHECK
