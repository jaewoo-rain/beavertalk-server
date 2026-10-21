"""PM-DEC-979(3차 후보) — 짧은 chat 통화도 «이렇게도 말해요» 1~2개."""
from domains.learning.service import normalcall_service as svc
from tests.test_chat_rephrased import _e, _rows


def test_short_chat_targets_one():
    assert svc._rephrase_target("chat", _rows(3)) == svc.REPHRASE_SHORT_MIN == 1      # 3줄(2385·2402 꼴)
    assert svc._rephrase_target("chat", _rows(4)) == 1                                  # 4줄(2382·2390)
    assert svc._rephrase_target("chat", _rows(2, "네")) == 0                            # 대답만 — 대상 아님
    assert svc._rephrase_target("chat", _rows(2, "응")) == 0
    assert svc._rephrase_target("chat", _rows(8)) == svc.REPHRASE_MIN                  # 긴 통화 종전 그대로
    assert svc._rephrase_target("expression", _rows(4)) == 0


def test_short_by_words_two_long_lines():
    rows = _rows(2, "주말에 친구랑 같이 한강에 가서 자전거를 타고 치킨을 먹었어요 정말 재미있었어요")   # 2줄 · 20단어↑
    assert svc._rephrase_target("chat", rows) == 1


def test_short_max_two():
    assert svc._rephrase_max(1) == svc.REPHRASE_SHORT_MAX == 2
    assert svc._rephrase_max(svc.REPHRASE_MIN) == svc.REPHRASE_MAX
    rows = _rows(4)
    exprs = [_e(f"오늘 동물원에서 웜뱃을 처음 봤어요 {i}", f"오늘 동물원에서 웜뱃을 처음 봤어요 {i}.", f"오늘 웜뱃 첫 영접 {i}") for i in range(4)]
    kept = svc._ground_rephrased(exprs, rows, svc._rephrase_max(1))
    assert len(kept) == 2


def test_rule_text_uses_short_max():
    t = svc._rephrase_rule(1, "한국어", svc._rephrase_max(1))
    assert "최소 1개, 최대 2개" in t
    assert "최대 8개" in svc._rephrase_rule(3, "한국어")


def _real_rows(lines):
    rows = []
    for i, ln in enumerate(lines):
        rows.append({"role": "beaver", "turn_index": 2 * i, "content": "그래서?"})
        rows.append({"role": "user", "turn_index": 2 * i + 1, "content": ln})
    return rows


def test_attempt_verbatim_substring_kept():
    rows = _real_rows(["제가 너무 바빠서 영화를 잘 안 봐요."])
    kept = svc._ground_rephrased([_e("제가 너무 바빠서 영화를 잘 안 봐요.", "제가 너무 바빠서 영화를 잘 안 봐요.", "제가 너무 바빠서 영화 볼 틈이 없어요.")], rows)
    assert len(kept) == 1 and kept[0].learner_attempt == "제가 너무 바빠서 영화를 잘 안 봐요."


def test_attempt_trimmed_substring_kept():
    rows = _real_rows(["네. bever 그 방법으로 스트레스 없어졌어요."])
    kept = svc._ground_rephrased([_e("그 방법으로 스트레스 없어졌어요", "그 방법으로 스트레스가 없어졌어요.", "그 방법으로 스트레스가 확 날아갔어요.")], rows)
    assert kept and kept[0].learner_attempt == "그 방법으로 스트레스 없어졌어요"


def test_tidied_attempt_replaced_with_real_line():
    # 2405 — 조사 추가·«저번에» 삭제한 판 → 실제 줄(앞머리 «네.» 만 뗌)로
    rows = _real_rows(["네. 영화에 효과음 넣는 거 저번에 계속 했어요 다."])
    kept = svc._ground_rephrased([_e("영화에 효과음을 넣는 것을 계속했어요.", "영화에 효과음을 넣는 것을 계속했어요.", "영화에 효과음 넣는 걸 꾸준히 했어요.")], rows)
    assert kept and kept[0].learner_attempt == "영화에 효과음 넣는 거 저번에 계속 했어요 다."


def test_invented_attempt_still_dropped():
    rows = _real_rows(["저는 비빔밥도 자주 먹어요."])
    assert svc._ground_rephrased([_e("저는 어제 우주에 갔어요", "저는 어제 우주에 갔어요.", "어제 우주 찍고 왔어요.")], rows) == []


def test_rule_text_requires_verbatim_and_same_meaning():
    t = svc._rephrase_rule(1, "한국어", 2)
    assert "글자 그대로" in t and "앞뒤를 잘라내는 것만" in t and "뜻은 korean 과 같아야" in t
