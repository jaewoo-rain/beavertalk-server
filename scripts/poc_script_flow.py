# -*- coding: utf-8 -*-
"""[dev] 대본 구동 흐름 테스트 — 학습자 답을 **고정 수열**(correct/wrong)로 몰아 퀴즈 개시 시점을 잰다.

## 왜 (사장님 지시 2026-10-06)
「일부러 틀려보고 맞추기도 해서 조정 가능하잖아. 1.맞추고 2.틀리고 3.틀리고 4.맞추고 5.틀리고
 6.틀리고 7.틀리다 이렇게하면 다음 퀴즈가 나와야하는거잖아.」
⇒ 학습자를 대본으로 몰아 정답/오답을 우리가 정하고, 퀴즈가 제때 열리는지를 숫자로 본다.

## 종전 결론을 바로잡는 실험
「GPT 자발 퀴즈 개시 0회(3라운드)」는 그 세 번 다 지시문이 「스스로 퀴즈를 시작하지 마라」였다
(bare15/instruction_used.txt:53). 시키지 않은 일을 안 한 것을 센 것이라 근거가 아니다.
⇒ A: OPENAI_SELF_QUIZ=true 로 GPT 에게 기회를 준다. B: 기본(서버 큐)로 큐가 언제 얹히나 잰다.

## 설계 — 기존 하네스 재사용
운영 통화 경로(서버 WS → call_session → core/openai)를 그대로 타려고 `e2e_expression_call.py`
세션을 **그대로 쓰고** `decide_reply` 만 override 한다(adaptive 정책 대신 고정 대본).
- `correct` → 지금 비버가 **묻고 있는 항목**(전사에서 세션이 식별한 self.current)의 표면형/답을 말한다.
- `wrong`   → 주제 밖 영어(=모국어 locale). ⛔ 다른 항목의 표면형을 쓰지 않는다(오염 방지).
- 대본이 끝나면 말하지 않는다(침묵) → 서버 시계가 통화를 닫는다.

측정은 서버 로그(타임스탬프)에서 뽑는다 — arm / 보류 / 얹기(대기=Xs) / 열림 / 닫힘 / arm 생략.

## ⛔ 규율
운영 코드·프롬프트 무변경. 이 파일은 산출물 전용. 키는 env(GPT_API_KEY→서버가 읽음)·토큰만.
비밀번호는 E2E_PASSWORD env. 통화가 남기는 DB 행은 `usage_engine='live:openai-realtime-2.1-mini'` 로 지운다.

## 사용
    PY=/c/.../envs/beavertalk-server/python.exe
    PYTHONIOENCODING=utf-8 ENV=test E2E_PASSWORD=... "$PY" scripts/poc_script_flow.py \
        --env-root <.env 루트> --base http://127.0.0.1:8099 --email testfree@gmail.com \
        --lesson 1 --duration 5 --server-log <서버로그파일> --out <산출.md> --label A
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import e2e_expression_call as e2e  # noqa: E402

# 주제 밖 영어 — ⛔ 어느 항목의 표면형도 아니다. 「say again」류(복창 유도)는 피한다.
WRONG_POOL = [
    "Hmm, I am not really sure about that.",
    "I think the weather is nice today.",
    "I like drinking coffee in the morning.",
    "That sounds quite interesting to me.",
    "I went to the park yesterday afternoon.",
    "My favorite color is probably blue.",
]

# 사장님 수열: correct, wrong, wrong, wrong, 반복.
BASE_PATTERN = ["correct", "wrong", "wrong", "wrong"]


def build_script(reps: int) -> list[str]:
    return (BASE_PATTERN * reps)


class ScriptSession(e2e.Session):
    """decide_reply 만 대본으로 갈아끼운다. 나머지 상태기계(식별·mode·rounds)·발화(_speak_later)는 그대로."""

    def __init__(self, *a, script: list[str] | None = None, **k) -> None:
        super().__init__(*a, **k)
        self.script = list(script or [])
        self.script_i = 0
        self.wrong_i = 0
        self.script_log: list[dict] = []

    def decide_reply(self, text, mentioned, asked):  # type: ignore[override]
        rec = self.current
        # 항목 미식별 / 안 물은 턴 / 말 끊김 → 대본 소비 없이 원본 행동(여는 턴·수긍·대기)
        if rec is None or not asked:
            return super().decide_reply(text, mentioned, asked)
        if e2e.looks_cut_off(text):
            return None, "", "wait"
        if self.script_i >= len(self.script):
            return None, "", "wait"      # 대본 끝 — 침묵(서버 시계가 닫는다)
        label = self.script[self.script_i]
        self.script_i += 1
        item = rec.item
        entry = {"turn": len(self.turns), "step": self.script_i, "label": label,
                 "item_id": item.item_id, "surface": item.surface, "mode": self.mode,
                 "t": round(self.now(), 1)}
        if label == "correct":
            say = item.answer
            entry["said"] = say
            self.script_log.append(entry)
            return say, e2e.LANGUAGE, "correct"
        say = WRONG_POOL[self.wrong_i % len(WRONG_POOL)]
        self.wrong_i += 1
        entry["said"] = say
        self.script_log.append(entry)
        return say, "en", "idk"


# ─────────────────────────────────────────────────────────── 서버 로그 파싱

CUE_PREFIX = "normalcall 표현학습 퀴즈 큐"
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _ts(line: str) -> float | None:
    m = TS_RE.match(line)
    if not m:
        return None
    return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))


def parse_server_log(path: Path, call_id: int) -> dict:
    """그 call_id 의 퀴즈·드릴·판정 사건을 시계열로 뽑는다."""
    out: dict = {"arm": [], "hold": [], "attach": [], "open": [], "close": [],
                 "self_skip": [], "drill_nudge": [], "verdict": [], "forced_close": [], "lines": []}
    cid = f"call_id={call_id}"
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if cid not in raw:
            continue
        ts = _ts(raw)
        rec = {"ts": ts, "line": raw.strip()}
        if CUE_PREFIX in raw:
            if " arm 생략(OPENAI_SELF_QUIZ)" in raw:
                out["self_skip"].append(rec)
            elif " arm:" in raw:
                out["arm"].append(rec)
            elif " 보류:" in raw:
                out["hold"].append(rec)
            elif " 얹기:" in raw:
                out["attach"].append(rec)
            elif " 열림:" in raw:
                out["open"].append(rec)
            elif " 닫힘:" in raw:
                out["close"].append(rec)
            elif " 강제 닫힘:" in raw:
                out["forced_close"].append(rec)
            out["lines"].append(rec)
        elif "드릴 안내 주입" in raw:
            out["drill_nudge"].append(rec)
            out["lines"].append(rec)
        elif "퀴즈 판정" in raw:
            out["verdict"].append(rec)
            out["lines"].append(rec)
    return out


def _items_set(line: str) -> str:
    m = re.search(r"항목=(\[[^\]]*\])", line)
    return m.group(1) if m else "?"


def write_report(label: str, sess, srv: dict, script: list[str], out: Path) -> None:
    L: list[str] = []
    L.append(f"# 대본 구동 흐름 — {label} (call_id={sess.call_id})")
    L.append("")
    L.append(f"- 대본 수열: `{','.join(script)}`")
    L.append(f"- 드릴 순서(식별): {[sess.items[i].surface for i in sess.drilled_order]}")
    L.append(f"- 종료: {sess.end_reason} · 비버 턴 {sum(1 for t in sess.turns if t.role=='beaver')} · "
             f"학습자 턴 {sum(1 for t in sess.turns if t.role=='learner')}")
    L.append("")

    # ── 측정 1·2: 되묻기(퀴즈) 시작 지점 + 포함 항목
    L.append("## 퀴즈/되묻기 개시")
    if label.startswith("A"):
        L.append("- (A=OPENAI_SELF_QUIZ: 서버 큐 **안 얹음**. GPT 자발 개시를 전사로 본다 — 아래 전사 발췌)")
        for r in srv["self_skip"]:
            L.append(f"  - arm 생략: {r['line']}")
    else:
        for r in srv["arm"]:
            L.append(f"- arm: {_items_set(r['line'])}  @{r['ts']}")
        for r in srv["attach"]:
            m = re.search(r"대기=(\d+)s", r["line"])
            L.append(f"- 얹기: {_items_set(r['line'])}  대기={m.group(1) if m else '?'}s")
        for r in srv["open"]:
            L.append(f"- 열림: {_items_set(r['line'])}")
        for r in srv["close"]:
            L.append(f"- 닫힘: {r['line'][-120:]}")
    L.append("")

    # ── 측정 5: 큐 지연(arm→얹기)
    if srv["arm"] and srv["attach"]:
        for a, b in zip(srv["arm"], srv["attach"]):
            if a["ts"] and b["ts"]:
                L.append(f"- 큐 지연(arm→얹기): {b['ts']-a['ts']:.0f}s")
        if srv["hold"]:
            L.append(f"- 보류 로그 {len(srv['hold'])}건 (이유 예: {srv['hold'][0]['line'].split('이유=')[-1] if '이유=' in srv['hold'][0]['line'] else '?'})")
    L.append("")

    # ── 측정 3·4: 항목별 턴·수열
    L.append("## 대본 적용(항목별 학습자 턴)")
    L.append("| step | label | 향한 항목 | 말한 것 | mode | t(s) |")
    L.append("|---|---|---|---|---|---|")
    for e in sess.__dict__.get("script_log", []):
        L.append(f"| {e['step']} | {e['label']} | {e['surface']} | `{e.get('said','')[:40]}` | {e['mode']} | {e['t']} |")
    L.append("")

    # ── 드릴 넛지(재시도 한도)
    L.append(f"## 드릴 넛지(«다음 항목으로») {len(srv['drill_nudge'])}건")
    for r in srv["drill_nudge"]:
        L.append(f"- {r['line']}")
    L.append("")

    # ── 판정
    if srv["verdict"]:
        L.append(f"## 서버 판정 {len(srv['verdict'])}건")
        for r in srv["verdict"]:
            L.append(f"- {r['line']}")
        L.append("")

    # ── 전사
    L.append("## 전사")
    for t in sess.turns:
        if t.role == "beaver":
            L.append(f"- 🦫 {t.text}")
            if t.tags:
                L.append(f"    _{' '.join(t.tags)}_")
        else:
            stt = f"  (STT: `{t.stt}`)" if t.stt else ""
            L.append(f"- 👤[{t.kind}] {t.text}{stt}")
    L.append("")
    if sess.errors:
        L.append("## 에러")
        for e in sess.errors:
            L.append(f"- {e}")
    out.write_text("\n".join(L), encoding="utf-8")
    print(f"저장: {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-root", required=True)
    ap.add_argument("--base", default="http://127.0.0.1:8099")
    ap.add_argument("--email", default="testfree@gmail.com")
    ap.add_argument("--password", default=os.environ.get("E2E_PASSWORD"))
    ap.add_argument("--lesson", type=int, default=1)
    ap.add_argument("--duration", type=int, default=5)
    ap.add_argument("--reps", type=int, default=7, help="BASE_PATTERN(correct,wrong,wrong,wrong) 반복 수")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--server-log", required=True, help="로컬 uvicorn 로그 파일(타임스탬프)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", default="run")
    ap.add_argument("--no-llm", action="store_true")
    args = ap.parse_args()

    with __import__("contextlib").suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    e2e.LANGUAGE = "ko"
    e2e.bootstrap_env(args.env_root)
    from core.config import settings as _settings  # noqa: F401
    from domains.learning.repository import mastery_repository as mr
    e2e.QUIZ_GROUP = int(mr.EXPRESSION_QUIZ_GROUP)

    sf = e2e.db_session_factory()
    with sf() as db:
        e2e.MEMBER_ID = e2e.resolve_member(db, args.email)
    print(f"회원 {args.email} → member_id={e2e.MEMBER_ID} · QUIZ_GROUP={e2e.QUIZ_GROUP} · 서버 {args.base}")

    if not args.password:
        sys.exit("⛔ E2E_PASSWORD 가 없다")
    token = e2e.get_token(args.base, args.email, args.password)
    api = e2e.CurApi(args.base, token)

    if not args.no_reset:
        if not e2e.cur_reset(api, e2e.MEMBER_ID, args.lesson, sf=sf):
            sys.exit(2)

    ctx = e2e.load_cur_context(sf, e2e.MEMBER_ID, args.lesson, n=e2e.CUR_ITEMS_PER_CALL)
    print(f"차시 {ctx['lesson']['code']} · 예측 새 항목 {len(ctx['predicted_new'])} · 전체 {len(ctx['items'])}")

    script = build_script(args.reps)
    voice = e2e.Voice()
    picker = e2e.Picker(enabled=not args.no_llm)

    # Session 을 ScriptSession 으로 갈아끼운다(run_call 이 이 이름으로 만든다). script 를 클로저로 주입.
    _orig = e2e.Session
    def _factory(*a, **k):
        return ScriptSession(*a, script=script, **k)
    e2e.Session = _factory  # type: ignore[assignment]
    try:
        sess = asyncio.run(e2e.run_call(
            args.base, token, ctx["items"], voice, picker,
            duration_min=args.duration, probe=False, verbose=False,
            course="expression", lesson=ctx["lesson"]))
    finally:
        e2e.Session = _orig  # type: ignore[assignment]

    # 로그 flush 를 기다린다
    time.sleep(3)
    srv = parse_server_log(Path(args.server_log), sess.call_id or -1)
    write_report(args.label, sess, srv, script, Path(args.out))
    print(f"call_id={sess.call_id} · script_log {len(sess.script_log)}건 · "
          f"arm {len(srv['arm'])} 얹기 {len(srv['attach'])} 열림 {len(srv['open'])} "
          f"자발생략 {len(srv['self_skip'])} 드릴넛지 {len(srv['drill_nudge'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
