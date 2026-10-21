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
