"""⛔ 바이트 동일 잠금 — core/prompts/common.py 추출이 통화 지시문을 바꾸지 않았음을 증명한다.

## 무엇을 지키나
2026-09-10 표현학습·프리토킹 코스를 만들면서 `core/persona_prompt.py` 의 공유 자산
(`CONTROL_TAG`·`CLOSE_TAG_DEFAULT`·`new_close_tag`·`_LOCALE_LABEL`·`_RULE_CLOSE_PROTOCOL`·
불변 규칙 7)을 `core/prompts/common.py` 로 **옮겼다**. 재배치일 뿐이므로 조립 결과는
**바이트 동일**해야 한다. 아래 sha256 은 **옮기기 전 코드**로 뽑은 값이다.

## ⛔ 이 파일의 규율 (tests/test_persona_prompt.py 의 동결본과 같다)
해시를 **함부로 갱신하지 마라.** 여기가 터졌다는 것은 둘 중 하나다:
  ① 공유 자산을 옮기다 문자를 흘렸다        → **코드를 고쳐라.** 해시가 옳다.
  ② Live 문구를 **의도적으로** 바꿨다        → 그때만 재기준화하고, 무엇을 왜 바꿨는지
     docs/prompts/README.md §8 결정 로그에 적는다.
⛔ 통째로 다시 뽑지 마라 — 그러면 실수로 바꾼 곳까지 같이 얼어붙는다.

## 왜 해시인가(전문이 아니라)
전문 동결본은 이미 `tests/test_persona_prompt.py` 가 갖고 있다(그쪽은 템플릿 원문을 박아
두고 조립 로직까지 재현한다). 여기는 **추출 리팩토링 전용 그물**이라 조합을 넓게 깔고
(체크판·왕초보·표정·캐스케이드 옵트인·레벨테스트·시드 4종·재접지 4종) 값만 비교한다.
⇒ 두 시험이 겹치는 게 아니라 **축이 다르다**: 저기는 «문구가 무엇인가», 여기는
  «어떤 조합에서도 안 변했는가».
"""

from __future__ import annotations

import hashlib

import pytest

from core.persona_prompt import (
    CLOSE_SEED_LEVELTEST,
    build_continue_reminder,
    build_leveltest_instruction,
    build_reground_brief,
    build_reground_reminder,
    build_system_instruction,
    close_seed_leveltest,
    seed_leveltest_opening,
    seed_opening,
    seed_opening_lean,
    seed_resume,
)

# --------------------------------------------------------------------------- #
# 조립 입력 — ⛔ 바꾸지 마라(해시가 이 입력에 묶여 있다).
# --------------------------------------------------------------------------- #
_BASE = dict(
    role="한국어를 가르치는 다정한 비버 선생님",
    personality="친근하고 밝다. 짧게 말하고 자주 되묻는다.",
    level_profile="아주 쉬운 단어와 짧은 문장으로 말한다.",
    locale="en",
    interests=["여행", "K-pop"],
    name="Sam",
    lang_band="beginner",
    target_language="한국어",
)
# 유형 3종 × 상태 3종 + this_call 표식 + 본편/예비 — 렌더 분기를 최대한 넓게 친다.
_STUDY = [
    {"slot": "main", "kind": "chunk", "state": "new",
     "obj": "안녕히 가세요", "ex": None, "des": "헤어질 때"},
    {"slot": "main", "kind": "grammar", "state": "again",
     "obj": "-고 싶다", "ex": "먹고 싶어요", "des": "희망"},
    {"slot": "reserve", "kind": "vocab", "state": "review",
     "obj": "가다", "ex": "학교에 가요", "des": "to go", "this_call": True},
]
_KNOWN = {
    "grammar": ["-아요/어요", "-았/었-"],
    "targets": [{"obj": "가다", "ex": "학교에 가요", "hint": "이동"}],
}


def _cases() -> dict[str, str]:
    return {
        "live_plain": build_system_instruction(**_BASE),
        "live_checkboard": build_system_instruction(
            **_BASE, study_items=_STUDY, known_items=_KNOWN,
            recent_topics=["여행", "음식"], promotion_notice=True,
        ),
        "live_survival": build_system_instruction(
            **{**_BASE, "lang_band": "survival"}, study_items=_STUDY[:1],
        ),
        "live_face_tool": build_system_instruction(**_BASE, face_tool=True),
        "cascade_optin": build_system_instruction(
            **_BASE, max_sentences=3, language_marker=True,
            emotion_tags=("neutral", "happy", "surprised", "sad", "angry"),
        ),
        "leveltest": build_leveltest_instruction(
            role=_BASE["role"], personality=_BASE["personality"], locale="en",
            interests=_BASE["interests"], name="Sam", target_language="한국어",
        ),
        "seed_opening": seed_opening("한국어"),
        "seed_opening_lean": seed_opening_lean("한국어"),
        "seed_resume": seed_resume("한국어"),
        "seed_leveltest_opening": seed_leveltest_opening("한국어"),
        "close_seed_leveltest": close_seed_leveltest(),
        "CLOSE_SEED_LEVELTEST": CLOSE_SEED_LEVELTEST,
        "reground_reminder": build_reground_reminder(
            _BASE["role"], _BASE["personality"]),
        "continue_reminder": build_continue_reminder(
            _BASE["role"], _BASE["personality"]),
        "brief_chat": build_reground_brief(
            _BASE["role"], _BASE["personality"], mode="chat",
            covered=["가다", "안녕히 가세요"], topic="여행"),
        "brief_study": build_reground_brief(
            _BASE["role"], _BASE["personality"], mode="study",
            covered=["가다", "안녕히 가세요"], topic="여행"),
    }


# --------------------------------------------------------------------------- #
# ⛔ 동결 해시 — core/prompts/common.py 추출 **전** 코드로 뽑았다(2026-09-10).
#    (sha256, 문자 수). 길이를 같이 박는 이유: 해시만 있으면 터졌을 때
#    "얼마나 달라졌나"를 못 본다. 길이 차이가 곧 첫 단서다.
# --------------------------------------------------------------------------- #
_FROZEN: dict[str, tuple[str, int]] = {
    "live_plain": ("0eb57542dcff74985b12057ecbe957281ad6d94d63704cc1ee5448386f59c691", 3769),
    "live_checkboard": ("87ecc3a8bce7689eca31eacd34ff3912c7208472004039caa1d1b663fe22d388", 7100),
    "live_survival": ("96e23311bd2d59a90b1eadca449c30231e7e4c6f0d7622dc68715b17cb2081e6", 6135),
    "live_face_tool": ("f718605d73d6121eb283a339ded6128a64264f4c3af4b4f1a9532db30bb7d115", 4405),   # 2026-09-12 [표정] 블록 qual 로 교체(사장님 결정, bt-back 승인) — 옛 dec3ed7d…/4827
    "cascade_optin": ("7aadb5781e94e6530737ce9a3425c2b4f58e27b9846df549186a9a844eab095f", 4713),
    "leveltest": ("019f4df8f448fe68dda443511330e59974e752662f5228a5897743a3a0ab98bb", 2117),
    "seed_opening": ("0b1d8dcb23e47f0669fffdf94738eb8c51039a82fc0cc5fe1b2c7a3a668f0fcf", 285),
    "seed_opening_lean": ("9489cfd0c65021bb0a058d30e20cc87e111bf010d765a752aa936a1b14c04c27", 129),
    "seed_resume": ("0843114577a4be222834feaa5b66cf2717801a8e47fc2393945a50337dbc8925", 242),
    "seed_leveltest_opening": ("5e1e20b34e110fe967aa98f21eb092fb4e333db256962af697e0d1e00ee8c6e6", 392),
    "close_seed_leveltest": ("f0b6d37c56592ff96fb6ae727f7c700ef3e3c474224efa8380a3b09b7f10201f", 262),
    "CLOSE_SEED_LEVELTEST": ("f0b6d37c56592ff96fb6ae727f7c700ef3e3c474224efa8380a3b09b7f10201f", 262),
    "reground_reminder": ("5caed02aab24577f0f3097b18692207012ecef07c190c8ad3919578d161a71d0", 307),
    "continue_reminder": ("942375799cbbd6c85bf08b2b2475f64850b40f2d26add897c4642cfb9a04101b", 224),
    "brief_chat": ("715664fb343404280a658b11b468ec643e692968eae4a6bf3486454cd08dd81a", 391),
    "brief_study": ("b27aa7366ba719ab66419388b991848326e7261365c59f4e1890837ab73643f8", 426),
}


@pytest.mark.parametrize("name", sorted(_FROZEN))
def test_prompt_output_is_byte_identical_after_common_extraction(name: str) -> None:
    text = _cases()[name]
    want_sha, want_len = _FROZEN[name]
    got_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(text) == want_len, (
        f"{name}: 길이가 {want_len} → {len(text)} 로 변했다"
        f"({len(text) - want_len:+d}자). 공유 자산을 옮기다 흘렸는지 먼저 보라."
    )
    assert got_sha == want_sha, (
        f"{name}: 길이는 같은데 내용이 다르다 — 문자 치환이 있었다는 뜻이다."
    )


def test_every_case_is_frozen() -> None:
    """조합을 새로 추가했으면 해시도 같이 박아라(그물에 구멍을 내지 않는다)."""
    assert set(_cases()) == set(_FROZEN)


# --------------------------------------------------------------------------- #
# 소유권 — 공유 자산이 정말 common.py 에서 오는가(복사본이 다시 생기지 않게)
# --------------------------------------------------------------------------- #
def test_shared_assets_are_owned_by_common_not_copied() -> None:
    """persona_prompt 의 공유 심볼은 common 의 **같은 객체**여야 한다.

    ⛔ 여기가 터지면 누군가 문구를 **복사해** 되돌린 것이다. 같은 문구가 두 곳에 있으면
      어느 게 진짜인지 아무도 모른다(docs/prompts/README.md 원칙 3).
    """
    import core.persona_prompt as pp
    from core.prompts import common

    assert pp._RULE_CLOSE_PROTOCOL is common.RULE_CLOSE_PROTOCOL
    assert pp._LOCALE_LABEL is common.LOCALE_LABEL
    assert pp.CONTROL_TAG is common.CONTROL_TAG
    assert pp.CLOSE_TAG_DEFAULT is common.CLOSE_TAG_DEFAULT
    assert pp.new_close_tag is common.new_close_tag


def test_off_topic_rule_is_the_template_tail() -> None:
    """규칙 7 은 common 이 소유하고, 템플릿은 그것을 **끝에 이어붙인다**.

    ⚠ 이어붙이는 자리가 틀리면(예: 가운데 삽입) 길이는 같은데 순서가 달라진다 —
      해시 시험이 잡지만, 원인을 여기서 바로 가리키게 한 줄 더 둔다.
    """
    import core.persona_prompt as pp
    from core.prompts import common

    assert pp._INVARIANTS_TEMPLATE.endswith(common.RULE_OFF_TOPIC)
    assert pp._INVARIANTS_TEMPLATE.count(common.RULE_OFF_TOPIC) == 1


def test_control_tag_and_close_tag_are_never_merged() -> None:
    """⛔ 두 태그를 다시 합치면 재접지·넛지가 종료 신호로 오독된다(call_id=683).

    근거: docs/20260727_1710_통화-조기종료-종료태그-분리와-안전망.md
    """
    from core.prompts import common

    assert common.CONTROL_TAG != common.CLOSE_TAG_DEFAULT
    assert not common.CONTROL_TAG.startswith("[통화종료")
    for _ in range(20):
        tag = common.new_close_tag()
        assert tag.startswith("[통화종료:") and tag != common.CONTROL_TAG


def test_close_protocol_never_teaches_the_close_mechanism() -> None:
    """⛔ 종료 규약 문단에 종료 태그·종료 개념을 넣지 마라(call 706·852·870).

    지시문이 수단을 가르치면 모델이 그 수단으로 **혼자 통화를 끊는다.**
    (같은 계약을 tests/test_persona_prompt.py 도 본다 — 자산이 옮겨졌으니 여기도 지킨다.)
    """
    from core.prompts import common

    assert "{close_tag}" not in common.RULE_CLOSE_PROTOCOL
    assert "통화종료" not in common.RULE_CLOSE_PROTOCOL
    assert "종료 신호" not in common.RULE_CLOSE_PROTOCOL
