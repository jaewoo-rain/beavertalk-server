"""E2E 표기 내성 리플레이 — 서버 판정 경로 구동 배선(가짜 generate_structured, 통화·네트워크 없이). 서버 단위시험 test_expr_llm_judge 와 같은 구동."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import e2e_replay_transcript as rp  # noqa: E402
import domains.learning.realtime.call_session as cs  # noqa: E402

ITEMS = [
    {"item_id": 101, "obj": "どうも", "des": "고마워요(가볍게)", "ex": None, "answer": "どうも"},
    {"item_id": 102, "obj": "名前", "des": "이름", "ex": None, "answer": "名前です"},
]


class FakeJudge:
    """정답 판정: 마지막 U 턴이 공개(B «It's») 뒤면 failed, 오답(다른 항목 표면형)이면 failed, 아니면 passed."""

    def __init__(self):
        self.calls = []

    async def __call__(self, client, model, *, system_instruction, prompt, schema, **_kw):
        self.calls.append((schema.__name__, prompt))
        if schema.__name__ == "ExpressionTaughtOut":
            return schema(taught=[])
        tail = prompt.split("[퀴즈 전사]")[-1]
        if "It's" in tail:
            return schema(verdicts=[{"num": 1, "verdict": "failed", "why": "공개 뒤 복창"}])
        if "I don't know" in tail:
            return schema(verdicts=[])                       # 모른다 — 아직 미정(공개 뒤 복창에서 failed 로 확정된다)
        if "名前" in prompt.split("[퀴즈 전사]")[-1] and "どうも" not in prompt.split("[퀴즈 전사]")[-1]:
            return schema(verdicts=[{"num": 1, "verdict": "failed", "why": "다른 항목"}])
        return schema(verdicts=[{"num": 1, "verdict": "passed", "why": "음차 포함 정답"}])


def test_build_cases_ja_has_native_styles_reveal_and_wrong(monkeypatch):
    monkeypatch.setattr(rp.h, "LANGUAGE", "ja")        # ⚠ 전역 — 직접 대입하면 뒤 시험(template_identify)이 ja 로 돈다
    cases = rp.build_cases(ITEMS, "ja", "ko")
    names = [c["case"] for c in cases if c["item_id"] == 102]
    assert names == ["원형", "kana", "roman", "hangul", "공개 뒤 복창", "오답(다른 항목)"]
    kana = next(c for c in cases if c["item_id"] == 102 and c["case"] == "kana")
    assert kana["answer"] == "なまえです" and kana["expect"] == "passed" and kana["steps"][0] == {"open_quiz": [2]}
    assert next(c for c in cases if c["item_id"] == 101 and c["case"] == "오답(다른 항목)")["answer"] == "名前です"
    assert "«고마워요(가볍게)» 는 일본어로" in cases[0]["steps"][1]["text"]


def test_run_cases_drives_real_server_judge_path_with_fake_llm(monkeypatch):
    fake = FakeJudge()
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", fake)
    monkeypatch.setattr(rp.h, "LANGUAGE", "ja")
    cases = [c for c in rp.build_cases(ITEMS, "ja", "ko") if c["item_id"] == 101]
    grab = rp.LogGrab()
    old_level = cs.logger.level
    cs.logger.addHandler(grab)
    cs.logger.setLevel(logging.INFO)                 # pytest 아래선 유효 레벨이 WARNING — 판정 INFO 줄이 안 온다
    try:
        rows = asyncio.run(rp.run_cases(cs, ITEMS, cases, language="ja", locale="ko", client=object(), model="m", grab=grab))
    finally:
        cs.logger.removeHandler(grab)
        cs.logger.setLevel(old_level)
    by = {r["case"]: r for r in rows}
    assert by["원형"]["got"] == "passed" and by["공개 뒤 복창"]["got"] == "failed" and by["오답(다른 항목)"]["got"] == "failed"
    assert all(r["ok"] for r in rows), [(r["case"], r["got"]) for r in rows]
    assert any(n == "ExpressionVerdictOut" for n, _ in fake.calls)
    assert by["원형"]["why"] and "퀴즈 판정(LLM)" in by["원형"]["why"]
