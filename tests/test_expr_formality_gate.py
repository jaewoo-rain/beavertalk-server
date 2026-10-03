# -*- coding: utf-8 -*-
"""2026-10-03 표현학습 **격식 판정 결함 2건** (docs/plans/2026-10-03-표현학습-격식판정-결함2건.md)

① LLM 판정의 `passed` 에 격식 게이트가 없었다 — 서버 검색(`_server_judge_quiz`)·STT 폴백(`_verify_stt_fallback`)·유예 서버
   대조(`_grace_off_set_server_pass`)는 전부 `keeps_formality` 를 통과시키는데 **LLM 경로 두 곳(세트 안·세트 밖)만** 안 봤다.
   ⇒ 반말이 passed 로 기록됐다(1397 «얼마야?»·«나는 미국 사람»).
② `keeps_formality` 가 표면형의 **마지막 표지 그 글자**를 요구했다 — `polite_marker` 가 사전형 어휘(「다니다」·「아니다」)와
   명사(「개요」)·ja 어휘(「クリスマス」)에 게이트를 잘못 켜는데, 거기에 종류 일치까지 요구해 **정중한 정답을 반말로 기각**했다.

⛔ 이 두 시험 묶음은 **같이** 읽어라 — ② 를 안 고치고 ① 만 걸면 `test_a_paraphrase_the_string_cannot_locate_still_passes`
  가 깨진다(게이트가 의역 정답을 전부 기각한다). ① 을 안 걸고 ② 만 고치면 결함① 시험 3개가 깨진다.
"""
from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

import domains.learning.realtime.call_session as cs
from domains.learning.service import quiz_judge

# ── ① 용 ko 항목. obj=표면형 · ex=예문(예문 OR 대조) — 실제 cur_seed.json 의 값을 그대로 썼다.
KO_ITEMS = [
    {"item_id": 201, "obj": "이거 얼마예요?", "des": "가격 묻기", "ex": None},
    {"item_id": 202, "obj": "고마워요", "des": "thank you", "ex": None},
    {"item_id": 203, "obj": "다니다", "des": "to attend/go around",          # cur_seed.json 어휘 — 사전형인데 «니다» 로 무장된다
     "ex": "여행을 자주 다니는 친구가 정말 부럽다."},
    {"item_id": 204, "obj": "물", "des": "water", "ex": None},               # 표지 없는 항목(게이트 꺼짐)
]


def _state(items=KO_ITEMS, lang="ko") -> cs._CallState:
    st = cs._CallState()
    st.expr_items = [dict(i) for i in items]
    st.reground_items = [i["obj"] for i in items]
    st.target_code = lang
    st.expr_ctx = {"client": object(), "model": "m", "locale_label": "영어(English)", "target_language": "한국어"}
    st.reground_persona = ("선생님", "다정함")
    st.expr_llm_judge = True
    return st


class FakeJudge:
    """tests/test_expr_llm_judge.FakeJudge 와 같은 모양 — schema 이름으로 갈라 답한다."""

    def __init__(self, taught_fn=None, verdict_fn=None):
        self.taught_fn = taught_fn or (lambda p, s: [])
        self.verdict_fn = verdict_fn or (lambda p, s: {"verdicts": []})
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, client, model, *, system_instruction, prompt, schema, **_kw):
        self.calls.append((schema.__name__, prompt))
        if schema.__name__ == "ExpressionTaughtOut":
            return schema(taught=self.taught_fn(prompt, system_instruction), retaught=[])
        return schema(**self.verdict_fn(prompt, system_instruction))


async def _drain(st):
    for _ in range(50):
        pending = [t for t in st.expr_tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _beaver(st, text):
    st.cur_beaver_text = [text]
    cs._flush_beaver_segment(st)


def _user(st, text):
    st.cur_user_text = [text]
    cs._flush_user_segment(st)


def _open_quiz(st, nums, seq=1):
    st.covered_nums = sorted(set(st.covered_nums) | set(nums))
    st.expr_quizzed = set(nums)
    st.expr_quiz_set = list(nums)
    st.expr_quiz_seq = seq
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)


# --------------------------------------------------------------------------- #
# 결함① — LLM passed 에 격식 게이트
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_llm_passed_is_gated_when_the_whole_window_is_casual(monkeypatch):
    """1397 실측 모양: 정중형 항목에 학습자가 반말만 했는데 판정기가 passed 를 냈다. 코드가 기각한다(미판정)."""
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 1, "verdict": "passed", "why": "말했다"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1])
    _beaver(st, "퀴즈! 가격을 물을 때는 한국어로 어떻게 말해요?")
    await _drain(st)
    _user(st, "이거 얼마야?")
    await _drain(st)
    assert 201 not in st.expr_quiz_pass, "반말은 passed 가 아니다"


@pytest.mark.asyncio
async def test_the_gated_item_stays_undecided_not_failed(monkeypatch):
    """⛔ 기각은 **미판정**이다(quiz_judge:239-241 «침묵 ≠ 오답»). failed 로 적지도, 확정 집합에 넣지도 않는다 —
    그래야 다음 턴 판정·닫힘 판정·서버 폴백이 그 자리를 다시 본다."""
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 1, "verdict": "passed"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1])
    _user(st, "나는 미국 사람")
    await _drain(st)
    assert 201 not in st.expr_quiz_pass
    assert 201 not in st.expr_quiz_fail, "오답으로 적지 않는다"
    assert st.expr_quiz_llm_decided == set(), "확정이 아니다 — 다시 볼 수 있게 남긴다"
    assert st.expr_quiz_open is True, "미확정이면 «세트 전부 확정» 즉시 닫힘이 돌지 않는다"


@pytest.mark.asyncio
async def test_llm_passed_is_gated_on_the_located_answer_segment(monkeypatch):
    """2단 게이트 ①단 — 그 항목이 **문자열로 찾히는** U 가 있으면 그것만 본다. 창 다른 데서 공손했다고 통과시키지 않는다.
    (항목 「다니다」 = cur_seed.json 어휘의 사전형 오무장 모양. 예문 OR 로 U1 이 찾히고 그 U1 은 반말이다.)"""
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 3, "verdict": "passed"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [3])
    _user(st, "여행을 자주 다니는 친구가 정말 부럽다")        # 예문 그대로 — 찾히지만 반말
    _user(st, "네, 알겠어요")                                 # 공손하지만 그 항목의 답이 아니다
    await _drain(st)
    assert 203 not in st.expr_quiz_pass


@pytest.mark.asyncio
async def test_a_paraphrase_the_string_cannot_locate_still_passes(monkeypatch):
    """2단 게이트 ②단 — 문자열이 답을 못 집어내면(의역·음차·STT 변형) **창 안 U 전체**를 본다. LLM 의 의미 판정을 죽이지 않는다.
    ⛔ 이 시험이 ① 게이트의 상한이다 — 여기서 엄격하게 굴면 「감사합니다」·「도모」 류 정답이 전부 기각된다."""
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 2, "verdict": "passed", "why": "같은 뜻 정중형"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [2])
    _user(st, "감사합니다")                                   # 표면형 「고마워요」 와 글자가 겹치지 않는다
    await _drain(st)
    assert 202 in st.expr_quiz_pass
    assert st.expr_quiz_llm_decided == {2}


@pytest.mark.asyncio
async def test_an_item_without_a_polite_marker_is_not_gated(monkeypatch):
    """표면형이 명사·반말이면 볼 표지가 없다 — 게이트는 항상 참(종전과 같다)."""
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 4, "verdict": "passed"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [4])
    _user(st, "물")
    await _drain(st)
    assert 204 in st.expr_quiz_pass


@pytest.mark.asyncio
async def test_off_set_llm_passed_is_gated_too(monkeypatch):
    """세트 **밖** 자발 정답 경로(`_apply_off_set_pass`)도 같은 게이트를 받는다 — 리드 지시가 놓친 두 번째 구멍.
    세트 밖은 passed 만 적용하므로 기각 = 기록 0(covered 도 안 늘어난다)."""
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 2, "verdict": "passed", "why": "자발"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1])                                       # 세트는 1번뿐 — 2번은 «세트 밖»
    _user(st, "이거 얼마야?")                                  # 반말만 있는 창
    await _drain(st)
    assert 202 not in st.expr_quiz_pass
    assert 2 not in st.covered_nums, "기각된 세트 밖 항목을 «가르쳤다» 로 적지 않는다"


@pytest.mark.asyncio
async def test_the_known_miss_of_the_second_tier_is_pinned_here(monkeypatch):
    """⚠⚠ 이 시험은 **바라는 동작이 아니라 알려진 한계**를 못으로 박는다(2차 독립 QA 지적).

    2단 게이트 ②단은 창 안 U **전체**를 본다 — 그 항목의 답을 문자열로 집어낼 수 없을 때(의역·음차) 다른 발화의 공손함이
    게이트를 열어 준다. 아래가 그 모양이다: 반말 답(「얼마야?」)과 무관한 공손 발화(「네, 알겠어요」)가 한 창에 있으면 통과한다.
    ⛔ 세그먼트 번호 없이는 원리적으로 못 닫는다(§1-3 안2 = 스키마 `answer_seg` + locked 판정기 지시문 → 사장님 결정).
    ⇒ 이 시험이 **깨지면** 누군가 그 구멍을 닫은 것이다. 그때는 이 시험을 지우고 «기각» 시험으로 바꿔라.
    """
    fake = FakeJudge(verdict_fn=lambda p, s: {"verdicts": [{"num": 1, "verdict": "passed"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1])
    _user(st, "이거 얼마야?")                                 # 1번 항목의 답 — 반말이고 문자열로는 안 찾힌다
    _user(st, "네, 알겠어요")                                 # 무관한 공손 발화
    await _drain(st)
    assert 201 in st.expr_quiz_pass, "알려진 미탐 — 창에 공손한 발화가 있으면 ②단이 열린다"


@pytest.mark.asyncio
async def test_a_repeatedly_gated_item_does_not_hold_the_window_open(monkeypatch):
    """⛔ 상태기계 안전판 — 게이트가 계속 기각하면 «세트 전부 확정» 즉시 닫힘(6차 A)이 안 돈다. 그래도 창은
    학습자 턴 상한(EXPR_QUIZ_OPEN_MAX_USER_TURNS=6)에서 강제로 닫혀야 한다 — 게이트가 창을 영구히 열어 두지 않는다."""
    fake = FakeJudge(taught_fn=lambda p, s: [], verdict_fn=lambda p, s: {"verdicts": [{"num": 1, "verdict": "passed"}]})
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    st = _state()
    _open_quiz(st, [1])
    for i in range(cs.EXPR_QUIZ_OPEN_MAX_USER_TURNS):
        _user(st, "얼마야? %d" % i)
        await _drain(st)
    assert st.expr_quiz_open is False, "강제 닫힘이 돌아야 한다"
    assert 201 not in st.expr_quiz_pass, "반말만 한 창은 끝까지 통과가 아니다"


# --------------------------------------------------------------------------- #
# 결함② — 격식 표지 «종류 일치» 요구 폐지
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,surface", [
    ("감사합니다", "고마워요"),                                 # 어휘가 다른 정중형(요 ↔ 니다)
    ("고마워요", "감사합니다"),
    ("저는 학생이 아니에요.", "N이/가 아닙니다"),                # cur_seed.json 문법 — 표면형 니다 · 예문 해요체
    ("여행을 자주 다녀요", "다니다"),                            # 사전형 오무장(니다) + 정중한 정답
    ("발표 개요는 아래와 같이 정리했습니다.", "개요"),            # 명사 오무장(요) + 합니다체 정답
    ("이거 얼마예요", "이거 얼마예요?"),                         # 종전에도 참이던 것(회귀)
    ("잘 부탁드립니 다. 오케이", "잘 부탁드립니다"),             # T18 어절 안 공백(회귀)
    ("이거 하나 주세요", "이거 주세요"),
])
def test_formality_accepts_any_polite_marker(text: str, surface: str) -> None:
    assert quiz_judge.keeps_formality(text, surface) is True


@pytest.mark.parametrize("text,surface", [
    ("얼마입니까?", "이거 얼마예요?"),                           # 독립 QA 반례 — 합니다체 의문형(-ㅂ니까)
    ("회사원입니까?", "N입니까?, N입니다"),                       # cur_seed.json 문법 항목이 실제로 이 꼴을 가르친다
    ("갑니까", "가요"),
    ("같이 갑시다", "가요"),                                   # 청유 -ㅂ시다
    ("먹읍시다", "먹어요"),                                    # 청유 -읍시다
])
def test_formality_accepts_formal_question_and_propositive(text: str, surface: str) -> None:
    """⭐ 독립 QA(2026-10-03)가 잡은 구멍 — 「-ㅂ니까/습니까」·「-ㅂ시다/읍시다」가 표지 목록에 없어 **합니다체 의문·청유 정답이
    기각**됐다(수정 전에도 기각이었지만, ① 게이트가 LLM 경로에 처음 적용되므로 지금은 LLM 의 passed 를 뒤집는다)."""
    assert quiz_judge.keeps_formality(text, surface) is True


@pytest.mark.parametrize("text", [
    "배고프니까 밥 먹어",                  # 「-니까」는 이유 연결어미 — 반말이다
    "비 오니까 집에 가",
    "그러니까",
    "맛이 시다",                        # 「시다」는 형용사 — 반말이다
    "김치는 시다",
])
def test_the_coda_condition_keeps_casual_lookalikes_out(text: str) -> None:
    """⛔ 「니까」·「시다」를 **받침 조건 없이** 표지로 넣으면 이 반말들이 통과한다. 합니다체·청유형은 앞 음절에 **항상 ㅂ 받침**이 있다."""
    assert quiz_judge.keeps_formality(text, "고맙습니다") is False


@pytest.mark.parametrize("text,surface", [
    ("이거 얼마야?", "이거 얼마예요?"),                # 1397 ①
    ("나는 미국 사람", "저는 ◯◯ 사람이에요"),           # 1397 ②
    ("잘 못 들었다", "잘 못 들었어요"),                # 1398 t9
    ("안녕", "안녕하세요"),
    ("잘 부탁드려", "잘 부탁드립니다"),
    ("만나서 반가워", "만나서 반갑습니다"),
    ("고마워", "고맙습니다"),
    ("몰라", "모르겠어요"),
    ("나 학생이야", "저는 학생입니다"),
    ("이거 줘", "이거 주세요"),
    ("요리 좋아", "도와주세요"),                      # «요» 는 어절 **끝**만 — 느슨해져도 이건 기각이다
    ("여행을 자주 다니는 친구가 정말 부럽다.", "다니다"),  # 느슨해져도 반말 예문은 기각
])
def test_formality_still_rejects_casual_speech(text: str, surface: str) -> None:
    assert quiz_judge.keeps_formality(text, surface) is False


@pytest.mark.parametrize("text,surface,exp", [
    ("元気ですか。", "～は～です", True),                 # 종조사 か 때문에 종전엔 기각됐다
    ("楽しかったですか。", "～かったです", True),
    ("家に帰りましょうか。", "～ましょう", True),
    ("昨日はすごい雨でしたね。", "～です／でした", True),
    ("ありがとうございました", "ありがとうございます", True),   # ました(텍스트쪽 표지)
    ("私はカーラです。", "～は～です", True),              # 회귀(test_cur_ja)
    ("水をください。それから…", "～をください", True),      # 회귀
    ("そうでしょう", "そうです", True),                   # 독립 QA 반례 — でしょう(보통형은 だろう)
    ("そうでしょうか", "そうです", True),
    ("食べますから", "食べます", True),                    # 독립 QA 반례 — 접속조사 から·けど·し
    ("行きますけど", "行きます", True),
    ("行きますし", "行きます", True),
    ("ありがとうございます、先生", "ありがとうございます", True),   # 독립 QA 반례 — 「、」 뒤 호칭(종전엔 공백이 있을 때만 통과)
    ("学生ですけれども", "～は～です", True),                # 2차 QA 반례 — 「けれども」가 「けれど」 뒤에 있어야 «も» 가 안 남는다
    ("私はカーラだ", "～は～です", False),                # 보통형은 기각(회귀)
    ("元気か", "～は～です", False),                      # 종조사를 떼도 표지가 없다
    ("行く", "いってきます", False),                      # 회귀
    ("行くから", "行きます", False),                      # ⛔ 절단은 «드러나는 쪽이 정중할 때만» 참 — 반말은 안 샌다
    ("食べるけど", "食べます", False),
    ("だめだし", "食べます", False),
])
def test_ja_polite_forms_are_not_judged_casual(text: str, surface: str, exp: bool) -> None:
    assert quiz_judge.keeps_formality(text, surface, "ja") is exp


def test_polite_marker_arming_is_unchanged() -> None:
    """⛔ 무장 스위치(표면형 쪽)는 **무변경**이다 — 게이트가 켜지는 항목 집합이 바뀌면 «새 기각» 통로가 생긴다."""
    assert quiz_judge.polite_marker("이거 얼마예요?") == "요"
    assert quiz_judge.polite_marker("잘 부탁드립니다") == "니다"
    assert quiz_judge.polite_marker("안녕히 가십시오") == "십시오"
    assert quiz_judge.polite_marker("네") is None and quiz_judge.polite_marker("가다") is None
    assert quiz_judge.polite_marker("～は～です", "ja") == "です"
    assert quiz_judge.polite_marker("～ができる", "ja") is None
    assert quiz_judge.polite_marker("お元気ですか", "ja") is None, "ja 의 «か» 미무장은 이번 수정 범위가 아니다(보고)"


_SEED = pathlib.Path(__file__).resolve().parents[1] / "assets" / "curriculum_v3" / "cur_seed.json"


@pytest.mark.parametrize("lang,fname", [("ko", "cur_seed.json"), ("ja", "cur_seed_ja.json")])
def test_curriculum_examples_are_not_judged_casual(lang: str, fname: str) -> None:
    """커리큘럼 전수 — 정중 표면형 항목의 **예문을 그대로 말한** 학습자가 기각되는 수. 수정 전 ko 26·ja 19 → 수정 후 ko 1·ja 4,
    남는 것은 전부 **진짜 반말 예문**이다(기각이 맞다). ⇒ 상한을 못으로 박아 둔다."""
    path = _SEED.parent / fname
    if not path.exists():
        pytest.skip("커리큘럼 시드가 없다(assets/curriculum_v3)")
    seed = json.loads(path.read_text(encoding="utf-8"))
    rejected = [
        (it["key"], ex)
        for kind in ("grammar", "vocab")
        for it in (seed.get(kind) or [])
        if quiz_judge.polite_marker(it["key"], lang) is not None
        for ex in (it.get("examples") or [])
        if not quiz_judge.keeps_formality(ex, it["key"], lang)
    ]
    cap = 1 if lang == "ko" else 4
    assert len(rejected) <= cap, rejected
