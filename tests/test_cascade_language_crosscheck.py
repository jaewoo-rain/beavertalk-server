"""구간 언어를 **글자로 교차검증**한다 — 순번(홀짝)만 믿으면 발음이 뒤집힌다.

## 왜 (2026-08-14, 사장님 실기기 440초)
사장님: **"영어로만 말할 때 한국어 발음으로 말하는 때가 꽤 있었다."**

`split_by_language` 는 마커 경계로 자른 뒤 **순번**으로 언어를 정했다:

    out.append((piece, target_lang if i % 2 else base_lang))

⇒ 조각 안의 **글자를 안 본다.** 모델이 마커를 **반대로** 감싸면 그 영어가 ko 음성으로 나간다.

⭐ 그리고 그 자리는 **우리가 이미 적어 놨었다**(같은 파일 주석):
    "그 경우 문자 체계로 고르는 판정을 얹을 수 있는데(후보가 둘뿐이라 판정이 아니라
     고르기다), 지금은 그 자리만 열어 두고 단순 폴백을 쓴다."
설계는 있었고 구현이 없었다. 그 자리를 채운 것이다.

## 양방향으로 틀리고 있었다
실측 25턴 마커 상태: 없음 **5** · 있음 8 · 짝안맞음 1
· 마커 있음 + 반대로 감쌈 → **영어가 한국어 발음으로**(사장님이 말씀하신 것)
· 마커 없음 5건        → 통째로 base(en) → **한국어가 영어 발음으로**(아직 말씀 안 하셨을 뿐)

## ⛔ 새 언어감지기가 아니다
후보가 **둘뿐**이라 판정이 아니라 **고르기**다. 그래서 규칙이 셋뿐이고, **애매하면 손대지
않는다** — 고르기가 틀리면 지금보다 나빠진다.
"""

import domains.learning.realtime.cascade_reply as cr


def _split(text, base="en", target="ko"):
    stats: dict = {}
    return cr.split_by_language(text, base, target, stats), stats


# ── ① 마커가 반대로 감싸인 경우(사장님이 들으신 증상) ──────────────────────
def test_english_wrapped_as_target_is_read_in_english():
    """⭐⭐ 모델이 **영어를** `__ __` 로 감쌌다 — 순번대로면 ko 음성이 영어를 읽는다.

    ⚠ 이때 틀리는 건 감싼 조각 **하나가 아니다.** 순번이 통째로 뒤집혔으므로 바깥 한국어
      조각들도 en 으로 태깅돼 있다 — 셋 다 고쳐야 문장 전체가 옳은 발음으로 나간다.
      (내 첫 기대값은 1건이었다. 실제는 3건이고, **실제 쪽이 맞다.**)
    """
    out, stats = _split("자 그럼 __How are you today?__ 해볼까요?")
    langs = {text: lang for text, lang in out}
    assert langs["How are you today?"] == "en", out
    assert langs["자 그럼"] == "ko" and langs["해볼까요?"] == "ko", out
    assert stats["fixed"] == 3, stats


def test_korean_wrapped_correctly_is_untouched():
    """⛔ 제대로 감싼 것은 **안 건드린다** — 고치는 김에 멀쩡한 걸 뒤집으면 더 나쁘다."""
    out, stats = _split("Let's say __안녕하세요__ together!")
    langs = {text: lang for text, lang in out}
    assert langs["안녕하세요"] == "ko" and langs["Let's say"] == "en", out
    assert not stats.get("fixed"), stats


# ── ② 마커가 아예 없는 경우(실측 5건) ──────────────────────────────────────
def test_an_all_korean_reply_without_markers_is_read_in_korean():
    """⭐⭐ 마커 없음 = 통째로 base(en) 였다 — **한국어가 영어 발음으로** 읽히던 자리다."""
    out, stats = _split("오늘 날씨가 정말 좋네요. 산책 가실래요?")
    assert out == [("오늘 날씨가 정말 좋네요. 산책 가실래요?", "ko")], out
    assert stats["fixed"] == 1, stats


def test_an_all_english_reply_without_markers_stays_english():
    out, stats = _split("That sounds great! What did you do?")
    assert out[0][1] == "en", out
    assert not stats.get("fixed"), stats


# ── ③ 애매하면 손대지 않는다 ────────────────────────────────────────────────
def test_a_mixed_piece_is_left_alone():
    """⛔ 섞인 조각은 **고를 수 없다** — 둘 중 하나로 찍으면 반은 틀린다."""
    out, stats = _split("네 그건 coffee 라고 해요 in English")
    assert not stats.get("fixed"), out
    assert out[0][1] == "en", "애매한데 순번을 안 지켰다"


def test_digits_and_symbols_are_not_judged():
    """⛔ 숫자·기호뿐인 조각으로 언어를 바꾸지 않는다 — 판정 재료가 없다."""
    out, stats = _split("Say __2024__ please")
    langs = {text: lang for text, lang in out}
    assert langs["2024"] == "ko", "판정 불가인데 순번을 뒤집었다"
    assert not stats.get("fixed"), stats


def test_a_mostly_korean_piece_wins_even_with_a_loanword():
    """대부분 한글이면 라틴 몇 글자가 섞여도 한국어다(비율로 본다)."""
    out, stats = _split("저는 커피보다 tea 를 더 좋아해요")
    assert out[0][1] == "ko", out
    assert stats["fixed"] == 1


# ── ④ 두 후보가 한국어를 안 끼면 아예 안 돈다 ──────────────────────────────
def test_nothing_is_chosen_when_neither_side_is_korean():
    """⛔ 한글 유무로는 en 과 vi 를 못 가른다 — 근거가 없으면 **손대지 않는다**.

    ⚠ 이게 "새 감지기를 만들지 않는다"의 경계다. 후보에 한국어가 있을 때만 고르기가 성립한다.
    """
    out, stats = _split("Xin chào __hello there__ bạn", base="vi", target="en")
    assert not stats.get("fixed"), out


def test_nothing_is_chosen_when_both_sides_are_korean():
    out, stats = _split("안녕 __반가워__", base="ko", target="ko-KR")
    assert not stats.get("fixed"), out


# ── ⑤ 비율 함수의 성질 ──────────────────────────────────────────────────────
def test_the_ratio_ignores_punctuation_and_digits():
    assert cr._hangul_ratio("안녕!!! 123") == 1.0
    assert cr._hangul_ratio("hello?? 456") == 0.0
    assert cr._hangul_ratio("2024 !!!") == -1.0        # 글자가 없다 = 판정 불가
    assert 0.0 < cr._hangul_ratio("안녕 hello") < 1.0
