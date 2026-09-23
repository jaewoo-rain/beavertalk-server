"""STT 다중 언어 회귀 — **모국어로 말하면 전사가 통째로 사라지던 결함**.

2026-08-08 실통화(09:45~09:47):
    u11 speech_ms=1194 text=''   u12 946 text=''   u13 1025 text=''
    u14 1206 text=''             u15 1200 text=''
5회 연속·약 36초 동안 전사 0. VAD 는 소리를 들었다(speech_ms 가 1초씩 있다). 사장님 확인:
**"11~15는 영어로 말했다."** 같은 통화의 u10 은 영어를 '베이킹 센' 으로 억지 음차했고,
한국어 발화(u3~u7)는 정확했다.

원인: 언어 코드 필드는 리스트인데 우리가 **한 개(ko-KR)** 만 넣었다. 우리 사용자는
외국인 학습자다. 그들은 모국어로 묻고 한국어로 따라 말한다. **한쪽만 들으면 다른 쪽은
사라진다.**

⛔⛔ C14-b(2026-09-23) — 이 결함을 처음 고친 곳(캐스케이드 STT v2, `RollingSttV2Stream`)은
  캐스케이드 엔진 삭제로 죽어서 지웠다. 아래 ②~③ 정규화 테스트와 ④ 라이브 입력 힌트
  테스트는 `normalize_language_codes`(core/stt.py 로 이전됨, 본문 불변)가 여전히 라이브
  통화에서 실사용 중이라 그대로 남긴다 — 이 파일이 지키는 결함 자체는 살아있다.

1차 자료(https://cloud.google.com/speech-to-text/v2/docs/multiple-languages, 2026-08-08 확인):
  · "You can only use the alternative languages feature with the long, short, and telephony
     models."                                   → 우리 모델 `long` = 지원
  · "You can list up to three languages for automatic language recognition."
  · "Specifying multiple languages is only available in the ... global region and the us and
     eu multi-regions."                          → 우리 위치 `global` = 지원
  · "constrain the language list to the bare minimum needed as a best practice"

여기서 고정하는 성질:
  ① 데모 통화는 **학습 언어 + 모국어**를 같이 듣는다
  ② 짧은 코드(en)는 STT 코드가 아니다 — 지역까지 붙여 보낸다(en-US)
  ③ 문서 상한 3개를 넘기지 않는다
  ④ 이상한 코드가 통화를 죽이지 않는다(R5) — 걸러내고, 그래도 실패하면 한 언어로 강등
"""

import core.stt as stt_mod


# ── ② 정규화 ────────────────────────────────────────────────────────────────
def test_short_codes_become_full_bcp47():
    """`en` 은 STT 코드가 아니다 — 지역까지 있어야 한다(1차 자료: v2 supported-languages)."""
    assert stt_mod.normalize_language_codes(["ko", "en"]) == ["ko-KR", "en-US"]


def test_case_and_separator_are_normalized():
    """클라·env 가 `KO_kr` 처럼 보내도 같은 언어로 본다(중복도 지운다)."""
    assert stt_mod.normalize_language_codes(["ko-KR", "ko", "KO_kr", "en-us"]) \
        == ["ko-KR", "en-US"]


def test_unverified_short_codes_are_dropped_not_guessed():
    """⛔ **모르는 짧은 코드는 추측하지 않는다.**

    틀린 코드를 넣으면 그 언어는 조용히 안 들린다(지금 결함과 같은 실패). 확인한 매핑만
    쓰고 나머지는 버린다 — 버려도 동작은 지금과 같다(학습 언어는 그대로 들린다).
    """
    assert stt_mod.normalize_language_codes(["ko", "th", "english", ""]) == ["ko-KR"]     # th: 표에서 안 확인 → 버림(vi 는 2026-09-14 부터 매핑)


def test_full_tags_pass_through():
    """이미 완전한 태그는 그대로 벤더에 넘긴다(우리가 아는 언어만 지원하지 않는다)."""
    assert stt_mod.normalize_language_codes(["ja-JP", "cmn-Hans-CN"]) == ["ja-JP", "cmn-Hans-CN"]


def test_vendor_limit_of_three_is_enforced():
    """문서 상한 3개. 넘겨 보내면 요청 자체가 거절될 수 있다."""
    codes = stt_mod.normalize_language_codes(["ko-KR", "en-US", "ja-JP", "fr-FR"])
    assert codes == ["ko-KR", "en-US", "ja-JP"]
    assert len(codes) <= stt_mod.STT_V2_MAX_LANGUAGES


def test_empty_falls_back_instead_of_sending_nothing():
    """⛔ 언어 코드가 비면 스트림이 400 으로 죽고, 그건 **통화가 죽는다**는 뜻이다(R5)."""
    assert stt_mod.normalize_language_codes([], fallback="ko-KR") == ["ko-KR"]
    assert stt_mod.normalize_language_codes(["english"], fallback="ko") == ["ko-KR"]


# ── ④ 라이브 통화 입력 전사 언어 힌트 (2026-08-20) ──────────────────────────
# ⛔⛔ **여기부터는 캐스케이드가 아니라 라이브 회귀다.** 파일 이름 때문에 캐스케이드
#   테스트로 보이지만 아니다 — 캐스케이드 코드를 정리할 때 **이 절은 지우지 마라**
#   (라이브 테스트 파일로 옮기는 건 좋다).
#   기록: docs/20260813_0040_캐스케이드-데모잔재-정리목록.md §2-b
# 같은 결함이 **라이브 쪽에도** 있었다. 캐스케이드는 2026-08-08 에 고쳤는데(위),
# Gemini Live 의 입력 전사는 여전히 언어 힌트 없이 열려 있었다.
# 실측 call_id=1097(ko 학습 / en 모국어) — 학습자가 한국어를 따라 말했는데:
#     "피우다"→`フィウダ`  "다"→`套`  "아주"→`और च`  짧은 응답→`für 10`·`kumite`·`Sí.`
# ⛔ 이 전사는 CallRawData 로 저장돼 이어하기 요약·통화후 문장 추출·증거 인용 검증이 읽는다.
def test_live_call_hints_both_target_and_native_language():
    """라이브도 학습 언어 + 모국어를 같이 듣는다 — 순서는 학습 언어 먼저."""
    from domains.learning.realtime.call_session import _input_language_codes

    assert _input_language_codes("ko", "en") == ["ko-KR", "en-US"]


def test_live_call_dedupes_when_target_equals_native():
    """학습 언어와 모국어가 같으면 한 개다(중복 힌트는 의미가 없다)."""
    from domains.learning.realtime.call_session import _input_language_codes

    assert _input_language_codes("ko", "ko") == ["ko-KR"]


def test_live_call_gives_up_entirely_when_any_code_is_unmapped():
    """⛔⛔ **부분 힌트는 무힌트보다 나쁘다** — 하나라도 못 만들면 통째로 포기한다.

    `th` 는 검증된 매핑이 없다(_STT_LANGUAGE_ALIASES). 여기서 모국어 `en-US` 만 남겨 보내면 **그 발화를 영어로 알아들으라고 시키는** 꼴이
    된다. 빈 목록은 "힌트 없음"이고 그건 종전 동작(자동 감지)이다 — 안전한 쪽으로 떨어진다.
    """
    from domains.learning.realtime.call_session import _input_language_codes

    assert _input_language_codes("th", "en") == []


def test_live_call_hints_every_registry_language_since_1607():
    """2026-09-14 실통화 1607(ja 학습·ko 모국어): ja 미매핑 → «생략» → 무힌트 → 일본어가 「保険ってですか」「도움어」 로 찍혀 판정 전부 미통과.
    레지스트리 언어 전부(ko·en·ja·zh·fr·vi)가 힌트를 받는다. ko/en 은 종전 동일."""
    from domains.learning.realtime.call_session import _input_language_codes
    from core.languages import SUPPORTED_LANGUAGES as LANGUAGES

    assert _input_language_codes("ja", "ko") == ["ja-JP", "ko-KR"]
    assert _input_language_codes("ja", "en") == ["ja-JP", "en-US"]
    assert _input_language_codes("zh", "en") == ["cmn-Hans-CN", "en-US"]
    assert _input_language_codes("fr", "ko") == ["fr-FR", "ko-KR"]
    assert _input_language_codes("vi", "en") == ["vi-VN", "en-US"]
    assert _input_language_codes("ko", "en") == ["ko-KR", "en-US"], "종전 동일"
    for code in LANGUAGES:
        assert _input_language_codes(code, "en"), f"레지스트리 언어 {code} 가 힌트를 못 받는다"
    assert _input_language_codes("ko", "th") == [], "미검증 모국어(th)는 종전대로 통째로 포기"
