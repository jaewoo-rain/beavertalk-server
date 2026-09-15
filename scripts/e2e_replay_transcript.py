"""표기 내성 리플레이(통화 0) — 서버 LLM 판정 경로에 **전사 텍스트를 직접** 넣어 본다.

왜: 4차 재검(1615·1616)에서 오디오로는 표기 변형을 못 찍었다 — STT 가 한글 음차·가나·로마자 발음을 대상 언어 표기로 되돌린다
(콘니치하→こんにちは · 잇테키마스→行って き ます). 그래서 «학습자 전사가 변형 표기일 때 LLM 판정기가 통과시키나» 는
전사 문자열을 판정 경로에 바로 넣어야만 볼 수 있다(bt-back 결정 ② 2026-09-15).

무엇을 부르나: 서버 `domains.learning.realtime.call_session` 의 **실제** 가르침·정답 사이드카(4차 A·B) — 통화 WS 없이 `_CallState` 를
서버 단위시험(tests/test_expr_llm_judge.py `_state`·`_beaver`·`_user`·`_open_quiz`)과 같은 방식으로 만들고, 가짜 대신 **진짜 genai 클라이언트 +
settings.JUDGE_MODEL** 을 넣는다. 항목은 DB 의 차시(cur) 목록. ⚠ 서버 사설 함수에 기대므로 expr-build 가 이름을 바꾸면 여기도 고친다.

사용(⛔ conda env · .env 루트):
    E2E="PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/e2e_replay_transcript.py --env-root <.env 루트>"
    $E2E --email testmax@gmail.com --language ja --items 6                  # 항목마다: 원형 · kana · roman · hangul(→passed) · 공개 뒤 복창 · 오답(→failed)
    $E2E --email testmax@gmail.com --language ko --items 6                  # ko: 원형 · roman(→passed) · 공개 뒤 복창 · 오답
    $E2E --language ja --from-json steps.json                               # {"items":[{item_id,obj,des,ex}], "steps":[{"open_quiz":[1]},{"role":"beaver","text":…},{"role":"user","text":…}]}
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import e2e_expression_call as h  # noqa: E402

LOG_KEYS = ("퀴즈 판정(LLM)", "가르침 판정(LLM)", "판정 실패", "폴백")
IDK = "I don't know."
TARGET_LABEL = {"ko": "한국어", "ja": "일본어"}
LOCALE_LABEL = {"en": "영어", "ko": "한국어", "ja": "일본어"}


class LogGrab(logging.Handler):
    """call_session 판정 로그 줄을 모은다(보고서의 why)."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if any(k in msg for k in LOG_KEYS):
            self.lines.append(msg)


# ── 서버 단위시험과 같은 구동(tests/test_expr_llm_judge.py) ─────────────────────── #
def make_state(cs, items: list[dict], *, target_code: str, client, model: str, target_label: str, locale_label: str):
    st = cs._CallState()
    st.expr_items = list(items)
    st.reground_items = [i["obj"] for i in items]
    st.target_code = target_code
    st.expr_ctx = {"client": client, "model": model, "locale_label": locale_label, "target_language": target_label}
    st.reground_persona = ("선생님", "다정함")
    st.expr_llm_judge = True
    return st


async def drain(st) -> None:
    for _ in range(50):
        pending = [t for t in st.expr_tasks if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def open_quiz(cs, st, nums: list[int], seq: int = 1) -> None:
    st.covered_nums = sorted(set(st.covered_nums) | set(nums))
    st.expr_quizzed = set(nums)
    st.expr_quiz_set = list(nums)
    st.expr_quiz_seq = seq
    st.expr_quiz_awaiting_open = True
    cs._expression_quiz_open_on_beaver_turn(st)


async def run_steps(cs, st, steps: list[dict]) -> dict:
    """steps: {"open_quiz":[n…], "seq"?} · {"role":"beaver"|"user","text":…}. 매 턴 뒤 사이드카를 다 기다린다."""
    for s in steps:
        if "open_quiz" in s:
            open_quiz(cs, st, list(s["open_quiz"]), int(s.get("seq", 1)))
        elif s.get("role") == "beaver":
            st.cur_beaver_text = [s["text"]]
            cs._flush_beaver_segment(st)
            await drain(st)
        elif s.get("role") in ("user", "learner"):
            st.cur_user_text = [s["text"]]
            cs._flush_user_segment(st)
            await drain(st)
    return {"passed": sorted(st.expr_quiz_pass), "failed": sorted(st.expr_quiz_fail)}


# ── 사례 만들기 ─────────────────────────────────────────────────────────────── #
def question_for(item: dict, language: str, locale: str) -> str:
    des = item.get("des") or item.get("obj")
    if locale == "ko":
        return f"퀴즈! «{des}» 는 {TARGET_LABEL.get(language, language)}로 뭐라고 해요?"
    return f'Quiz time! How do you say "{des}" in {"Japanese" if language == "ja" else "Korean"}?'


def build_cases(items: list[dict], language: str, locale: str, styles: tuple[str, ...] | None = None) -> list[dict]:
    """항목 n 마다 새 퀴즈 1개씩: 원형·표기 변형(→passed) · 공개 뒤 복창 · 다른 항목 답(→failed). 답 = 예문(문형) 또는 표면형."""
    styles = styles or (("roman",) if language == "ko" else ("kana", "roman", "hangul"))
    cases: list[dict] = []
    for n, it in enumerate(items, 1):
        ans = it.get("answer") or it["obj"]
        q = question_for(it, language, locale)
        variants = [("원형", ans)]
        for style in styles:
            v = h.styled_answer(ans, style, language)
            if v:
                variants.append((style, v[0]))
        for name, text in variants:
            cases.append({"n": n, "item_id": it["item_id"], "obj": it["obj"], "case": name, "answer": text, "expect": "passed",
                          "steps": [{"open_quiz": [n]}, {"role": "beaver", "text": q}, {"role": "user", "text": text}]})
        cases.append({"n": n, "item_id": it["item_id"], "obj": it["obj"], "case": "공개 뒤 복창", "answer": ans, "expect": "failed",
                      "steps": [{"open_quiz": [n]}, {"role": "beaver", "text": q}, {"role": "user", "text": IDK},
                                {"role": "beaver", "text": f"It's {ans}. Say it."}, {"role": "user", "text": ans}]})
        if len(items) > 1:
            other = items[n % len(items)]
            wrong = other.get("answer") or other["obj"]
            cases.append({"n": n, "item_id": it["item_id"], "obj": it["obj"], "case": "오답(다른 항목)", "answer": wrong, "expect": "failed",
                          "steps": [{"open_quiz": [n]}, {"role": "beaver", "text": q}, {"role": "user", "text": wrong}]})
    return cases


async def run_cases(cs, items: list[dict], cases: list[dict], *, language: str, locale: str, client, model: str,
                    grab: LogGrab | None = None) -> list[dict]:
    rows = []
    for c in cases:
        st = make_state(cs, items, target_code=language, client=client, model=model,
                        target_label=TARGET_LABEL.get(language, language), locale_label=LOCALE_LABEL.get(locale, locale))
        mark = len(grab.lines) if grab else 0
        v = await run_steps(cs, st, c["steps"])
        got = "passed" if c["item_id"] in v["passed"] else ("failed" if c["item_id"] in v["failed"] else "pending")
        why = [ln for ln in (grab.lines[mark:] if grab else []) if "퀴즈 판정(LLM)" in ln or "실패" in ln or "폴백" in ln]
        rows.append(dict(c, got=got, ok=(got == c["expect"]), why=why[-1] if why else "",
                         fallback=any("폴백" in ln or "실패" in ln for ln in why)))
    return rows


def report(rows: list[dict], *, language: str, locale: str, model: str, lesson: dict, out_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    kinds: dict[str, list[int]] = {}
    for r in rows:
        k = kinds.setdefault(r["case"], [0, 0])
        k[0] += 1
        k[1] += int(r["ok"])
    L = [f"# 표기 내성 리플레이 — {language} · 차시 {lesson.get('no')} {lesson.get('code')} ({stamp})", "",
         f"- 서버 LLM 판정 경로(call_session 가르침·정답 사이드카)에 전사 텍스트 직접 주입 · 모델 `{model}` · 학습자 모국어 {locale} · 통화 0",
         f"- 결과: {sum(r['ok'] for r in rows)}/{len(rows)} 기대 일치 · 폴백/실패 턴 {sum(r['fallback'] for r in rows)}",
         "- 사례별: " + " · ".join(f"{k} {v[1]}/{v[0]}" for k, v in kinds.items()), "",
         "| # | 항목 | 사례 | 학습자 전사 | 기대 | 서버 판정 | 일치 | why(판정 로그) |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        L.append(f"| {r['n']} | {r['obj']} | {r['case']} | {r['answer'][:40]} | {r['expect']} | {r['got']}{' (폴백)' if r['fallback'] else ''} | "
                 f"{'✔' if r['ok'] else '✖'} | {r['why'].split('why=')[-1][:120] if r['why'] else ''} |")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stamp}_{language}_replay_styles.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


def load_items(sf, member_id: int, lesson_no: int, *, locale: str, limit: int) -> tuple[list[dict], dict]:
    """차시 항목 → 서버 판정기 DTO {item_id, obj, des, ex} (+answer). des = meanings[locale] 없으면 en."""
    from sqlalchemy import text as sql
    ctx = h.load_cur_context(sf, member_id, lesson_no, n=h.CUR_ITEMS_PER_CALL)
    its = [it for it in ctx["items"].values() if not it.review][:limit]
    with sf() as db:
        rows = db.execute(sql("SELECT item_id, meanings FROM cur_item WHERE item_id = ANY(:ids)"), {"ids": [it.item_id for it in its]}).all()
    means = {}
    for iid, m in rows:
        try:
            means[int(iid)] = json.loads(m) if isinstance(m, str) else (m or {})
        except ValueError:
            means[int(iid)] = {}
    out = []
    for it in its:
        m = means.get(it.item_id) or {}
        des = m.get(locale) or m.get("en") or it.en
        if isinstance(des, list):
            des = des[0] if des else it.en
        out.append({"item_id": it.item_id, "obj": it.surface, "des": str(des),
                    "ex": (it.examples[0] if it.kind == "grammar" and it.examples else None), "answer": it.answer})
    return out, ctx["lesson"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-root", default=None)
    ap.add_argument("--email", default="testmax@gmail.com")
    ap.add_argument("--language", choices=("ko", "ja"), default="ja")
    ap.add_argument("--lesson", type=int, default=None)
    ap.add_argument("--locale", default=None, help="판정기에 주는 학습자 모국어(뜻 언어) — 기본 ko 대상=en · ja 대상=ko")
    ap.add_argument("--items", type=int, default=6)
    ap.add_argument("--styles", default=None, help="쉼표 목록(kana,roman,hangul) — 기본 ko=roman · ja=kana,roman,hangul")
    ap.add_argument("--from-json", default=None)
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "e2e"))
    args = ap.parse_args()
    with __import__("contextlib").suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    h.bootstrap_env(args.env_root)
    h.LANGUAGE = args.language
    locale = args.locale or ("en" if args.language == "ko" else "ko")
    from core.config import settings
    from domains.learning.realtime import call_session as cs
    client = h.Picker._make_client()
    if client is None:
        sys.exit("⛔ genai 클라이언트 없음 — .env(GEMINI_API_KEY 또는 gcp_key.json) 확인")
    grab = LogGrab()
    logging.getLogger(cs.__name__).addHandler(grab)
    logging.getLogger(cs.__name__).setLevel(logging.INFO)
    model = settings.JUDGE_MODEL
    if args.from_json:
        spec = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        st = make_state(cs, spec["items"], target_code=args.language, client=client, model=model,
                        target_label=TARGET_LABEL[args.language], locale_label=LOCALE_LABEL.get(locale, locale))
        v = asyncio.run(run_steps(cs, st, spec["steps"]))
        print(json.dumps(v, ensure_ascii=False))
        print("\n".join(grab.lines))
        return
    sf = h.db_session_factory()
    with sf() as db:
        member = h.resolve_member(db, args.email)
    lesson_no = args.lesson or (1 if args.language == "ja" else h.DEFAULT_LESSON_NO)
    items, lesson = load_items(sf, member, lesson_no, locale=locale, limit=args.items)
    styles = tuple(s.strip() for s in args.styles.split(",")) if args.styles else None
    cases = build_cases(items, args.language, locale, styles)
    print(f"리플레이: {args.language} 차시 {lesson.get('no')} 항목 {len(items)} · 사례 {len(cases)} · 모델 {model}")
    rows = asyncio.run(run_cases(cs, items, cases, language=args.language, locale=locale, client=client, model=model, grab=grab))
    path = report(rows, language=args.language, locale=locale, model=model, lesson=lesson, out_dir=Path(args.out_dir))
    print(f"일치 {sum(r['ok'] for r in rows)}/{len(rows)} · 보고서 {path}")


if __name__ == "__main__":
    main()
