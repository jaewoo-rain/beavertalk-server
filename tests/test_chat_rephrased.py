"""자유대화 «이렇게도 말해요»(rephrased) — 2311 대화 기록 0개 후속(command center 승인)."""
from domains.learning.service import normalcall_service as svc


def _rows(n_user, text="오늘 동물원에서 웜뱃을 처음 봤어요"):
    rows = []
    for i in range(n_user):
        rows.append({"role": "beaver", "turn_index": 2 * i, "content": "그래서 어땠어?"})
        rows.append({"role": "user", "turn_index": 2 * i + 1, "content": f"{text} {i}"})
    return rows


def _e(attempt, korean, native, kind="rephrased"):
    return svc.LearnedExpression(korean=korean, translation="t", source_type=kind, learner_attempt=attempt,
                                 native_expression=native)


def test_target_only_for_chat_freetalk_with_enough_learner_speech():
    assert svc._rephrase_target("chat", _rows(8)) == svc.REPHRASE_MIN
    assert svc._rephrase_target("freetalk", _rows(8)) == svc.REPHRASE_MIN
    assert svc._rephrase_target("expression", _rows(8)) == 0
    assert svc._rephrase_target("homework", _rows(8)) == 0
    assert svc._rephrase_target("chat", _rows(2, "네")) == 0


def test_grounding_keeps_only_learner_sentences_with_real_native_pair():
    rows = _rows(8)
    exprs = [
        _e("오늘 동물원에서 웜뱃을 처음 봤어요 1", "오늘 동물원에서 웜뱃을 처음 봤어요.", "오늘 동물원에서 웜뱃 첫 영접했어요."),   # 남음
        _e("저는 어제 우주에 갔어요", "저는 어제 우주에 갔어요.", "어제 우주 찍고 왔어요."),                               # 학습자 말 아님
        _e("오늘 동물원에서 웜뱃을 처음 봤어요 2", "오늘 동물원에서 웜뱃을 처음 봤어요.", None),                             # 짝 없음
        _e("오늘 동물원에서 웜뱃을 처음 봤어요 3", "웜뱃을 처음 봤어", "웜뱃을 처음 봤어요"),                                # 어미만 다름
        _e("오늘 동물원에서 웜뱃을 처음 봤어요 4", "질문", "질문", kind="asked"),                                           # 다른 종류는 그대로
    ]
    got = svc._ground_rephrased(exprs, rows)
    assert [e.native_expression for e in got] == ["오늘 동물원에서 웜뱃 첫 영접했어요.", "질문"]


def test_rule_mentions_min_and_native_pair():
    rule = svc._rephrase_rule(3, "한국어")
    assert "최소 3개" in rule and "native_expression" in rule and "«이렇게도 말해요»" in rule
