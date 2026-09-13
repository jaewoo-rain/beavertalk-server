"""E2E 하네스 --language ja 어댑터 — 시드 로드 · 판정 호출(언어 분기) · 계정 target_language 복구. 서버·DB 없이."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import e2e_expression_call as h  # noqa: E402


@pytest.fixture
def ja_mode(monkeypatch):
    monkeypatch.setattr(h, "LANGUAGE", "ja")
    yield
    monkeypatch.setattr(h, "LANGUAGE", "ko")


# ── ① 시드 ───────────────────────────────────────────────────────────── #
def test_ja_seed_lesson1_is_a1_t01_1_with_18_items():
    seed = json.loads((ROOT / "assets" / "curriculum_v3" / "cur_seed_ja.json").read_text(encoding="utf-8"))
    l1 = next(l for l in seed["lessons"] if l["no"] == 1)
    assert l1["code"] == "A1-T01-1"
    vocab = [v for v in seed["vocab"] if v.get("lesson_code") == "A1-T01-1"]
    grammar = [g for g in seed["grammar"] if "A1-T01-1" in (g.get("lesson_codes") or [])]
    assert len(vocab) + len(grammar) == 18
    assert all("kana" in v and "ko" in v and "en" in v for v in vocab)      # meanings {"en","ko","kana"}
    assert l1["success"]["speech_level"] == "です・ます체"


# ── ② 판정 호출(언어 분기) · 답/반말 ───────────────────────────────────── #
def test_ja_judge_calls_and_answer_forms(ja_mode):
    assert h.has_surface("私です", "私") is True                    # です 표지를 조사 꼬리로 받는다(0f1cfe5)
    assert h.has_surface("私はカーラです。", "～は～です") is True    # 문형 템플릿
    assert h.has_surface("私はカーラだ。", "～は～です") is False     # 보통형(だ)은 격식 없음
    name = h.Item(1, "名前", "name", "", ("name",), kind="vocab", example="名前は田中一郎です。")
    assert name.answer == "名前です"                                 # 두 글자 어휘는 「Xです」
    assert h.casual_ja("私はカーラです。") == "私はカーラだ。"
    assert h.casual_ja("私はお茶がいいです。") == "私はお茶がいい。"   # い형용사엔 だ 를 안 붙인다
    assert h.casual_ja("食べます。") == ""                            # ます 활용은 안 건드린다 → 오답 변형
    assert h.norm_ko("Ｔｅｓｔ　名前。") == "Test名前"                # NFKC + 일본어 문장부호 제거
    assert h.lang_of("私はカーラです。") == "ko" and h.lang_of("How do you say name?") == "native"


def test_ja_redrill_uses_language_matcher(ja_mode):
    items = {1: h.Item(1, "名前", "name", "", ("name",), kind="vocab", example="名前は田中一郎です。")}
    turns = [h.Turn(0, "beaver", 1.0, "もう一度、「名前」。"), h.Turn(1, "learner", 2.0, "はい"), h.Turn(2, "beaver", 3.0, "名前は田中一郎です。")]
    n, ev = h.count_redrills(turns, items, passed_before={1}, passed_times={})
    assert n == 2 and [e[0] for e in ev] == [0, 2]


# ── ③ 계정 복구 ─────────────────────────────────────────────────────── #
class _FakeApi:
    def __init__(self, orig="ko"):
        self.orig = orig
        self.calls: list = []

    def member_me(self):
        return {"target_language": self.orig}

    def patch_target_language(self, code):
        self.calls.append(code)
        return {"target_language": code}


def test_target_language_is_restored_even_when_body_fails():
    api = _FakeApi(orig="ko")
    with pytest.raises(RuntimeError):
        h.run_with_target_language(api, "ja", lambda orig: (_ for _ in ()).throw(RuntimeError("boom")))
    assert api.calls == ["ja", "ko"]


def test_target_language_restored_on_system_exit_and_none_original():
    api = _FakeApi(orig=None)
    with pytest.raises(SystemExit):
        h.run_with_target_language(api, "ja", lambda orig: sys.exit(2))
    assert api.calls == ["ja", None]


def test_ko_mode_does_not_touch_account():
    api = _FakeApi(orig="ko")
    seen = []
    h.run_with_target_language(api, "ko", lambda orig: seen.append(orig))
    assert api.calls == [] and seen == [None]
