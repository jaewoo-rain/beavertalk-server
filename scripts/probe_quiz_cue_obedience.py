# -*- coding: utf-8 -*-
"""[dev·조사] 퀴즈 큐 **복종률** 측정 하네스 — 「GPT 가 큐를 받고도 되묻지 않는다」의 원인 가리기.

## 무엇을 재나
실통화(call 1740·1741)의 **기하(geometry)를 그대로** 텍스트 모달리티로 재연한다.
같은 모델(`gpt-realtime-2.1-mini`) · 같은 세션 지시문
(`core.openai.prompts.expression.build_expression_instruction`) · 같은 큐 문구
(`core.openai.prompts.seeds.quiz_cue`) · 같은 주입 통로(`conversation.item.create`).

재연 기하 — 1740·1741 이 둘 다 이 상태에서 큐를 받았다:
  · 비버가 항목 1·2·3 을 가르쳤고 학습자가 전부 맞혔다(covered=[1,2,3])
  · 비버가 **항목 4 를 막 소개**했다
  · 거기서 서버가 큐를 얹는다(세트=[1,2,3], 큐 꼬리의 첫 줄 = 「4. ... = 좋아요」)
  · 학습자가 항목 4 의 정답을 말한다 → **그 다음 비버 2턴**을 센다
⇒ 실측 실패 모드는 「세트(1·2·3)를 안 묻고 꼬리 첫 줄(항목 4)·그 다음 번호로 전진」이다.

## ⛔ 이 하네스가 재지 **못하는** 것
- 오디오. 학습자 턴이 텍스트 항목이라 「오디오 더미에 끼인 텍스트 1개」 효과는 안 잡힌다.
  ⚠ 방향은 **보수적**이다 — 프로덕션에선 큐만 텍스트라 오히려 더 두드러진다.
- 서버 타이밍(RMS 관문·settle 지연 45~57초). 그건 Cloud Run 로그로 따로 잰다.
- `semantic_vad` 자동 응답. 여기선 `response.create` 를 직접 쏜다.

## 조건
  A  현행          role=user  · 현행 큐(824자, 꼬리 포함) · 큐 뒤 학습자 항목 1개
  B  role=system   role=system· 현행 큐                     ← 가설 5(역할)
  C  짧은 큐       role=user  · 2문장(128자)                ← 가설 1(길이)
  E  행동 먼저     role=user  · 「읽지 마라」를 맨 뒤로       ← 가설 2(순서)
  F  꼬리 없음     role=user  · remaining_rows 만 제거(453자) ← 가설 7(꼬리가 전진을 지시)
  G  생짜          role=user  · done+remaining 둘 다 제거(398자)
  H  꼬리없음+sys  role=system· F 와 같은 문구
  Z  즉시 응답     role=user  · 현행 큐 직후 response.create(학습자 항목 없음) ← 가설 4(자리)

## 사용
    GPT_API_KEY=... PYTHONIOENCODING=utf-8 python scripts/probe_quiz_cue_obedience.py \
        --conditions A,B,C,E,F,G,H,Z --n 5 --out <산출.json>
    (--face 로 표정 툴 ON 세션 — 운영이 켜고 돌았다)
⛔ 운영 코드·프롬프트 무변경. 이 파일은 조사 산출물이고 import 만 한다.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.openai.prompts import expression as expr      # noqa: E402
from core.openai.prompts import seeds                   # noqa: E402
from core.openai.tools import set_face_tool             # noqa: E402

MODEL = "gpt-realtime-2.1-mini"
URL = "wss://api.openai.com/v1/realtime"
JUDGE_MODEL = "gpt-4.1-mini"

# ── 실통화 1741 재료 (서버 로그 2026-10-05T23:02:54 «표현학습 목록» 그대로) ──────── #
ITEMS = [
    {"obj": "고마워요", "des": "thank you"},
    {"obj": "아니에요", "des": "not at all / you are welcome"},
    {"obj": "네", "des": "yes"},
    {"obj": "좋아요", "des": "good / okay"},
    {"obj": "맞아요", "des": "that is right"},
    {"obj": "진짜요?", "des": "really?"},
    {"obj": "처음 뵙겠습니다", "des": "how do you do (first meeting)"},
    {"obj": "잘 부탁드립니다", "des": "please treat me well"},
    {"obj": "저는 ◯◯ 사람이에요", "des": "I am from ◯◯"},
    {"obj": "이름이 뭐예요?", "des": "what is your name?"},
    {"obj": "저는 ◯◯이에요", "des": "I am ◯◯"},
    {"obj": "안녕하세요?", "des": "hello"},
    {"obj": "만나서 반갑습니다", "des": "nice to meet you"},
    {"obj": "잘 지냈어요?", "des": "how have you been?"},
    {"obj": "감사합니다", "des": "thank you (formal)"},
]
# 세트 = 방금 다룬 3개. covered = 같은 3개. ⇒ 꼬리 첫 줄이 「4. ... = 좋아요」가 된다(1740·1741 과 동형).
QUIZ_SET = [1, 2, 3]
QUIZ_LABELS = " ".join("«%s»" % ITEMS[n - 1]["obj"] for n in QUIZ_SET)
COVERED_AT_ATTACH = [1, 2, 3]
DONE_LABELS = [ITEMS[n - 1]["obj"] for n in COVERED_AT_ATTACH]
REMAINING_ROWS = ["%d. %s = %s" % (n, ITEMS[n - 1]["des"], ITEMS[n - 1]["obj"])
                  for n in range(1, 16) if n not in COVERED_AT_ATTACH][:12]
SET_SURFACES = [ITEMS[n - 1]["obj"] for n in QUIZ_SET]
TAIL_HEAD_SURFACE = ITEMS[3]["obj"]        # 좋아요 — 꼬리 목록의 첫 줄

# 학습자 대본 — 항목 1·2·3 을 전부 맞혀 비버를 항목 4 까지 밀어 올린다.
LEARNER_TURNS = ["고마워요.", "아니에요.", "네."]
LEARNER_AFTER_CUE = "좋아요."              # 항목 4 정답(1741 U12 와 같은 글자)
LEARNER_NEXT = "좋아요."

FULL_CUE = seeds.quiz_cue(
    QUIZ_LABELS, len(QUIZ_SET), retry=False, locale_label="English", target="한국어",
    done_labels=DONE_LABELS, remaining_rows=REMAINING_ROWS,
)
NO_TAIL_CUE = seeds.quiz_cue(
    QUIZ_LABELS, len(QUIZ_SET), retry=False, locale_label="English", target="한국어",
    done_labels=DONE_LABELS, remaining_rows=None,
)
BARE_CUE = seeds.quiz_cue(
    QUIZ_LABELS, len(QUIZ_SET), retry=False, locale_label="English", target="한국어",
)
SHORT_CUE = (
    "%s 지금 되묻는 차례다. %s 3개를 한 번에 하나씩 물어라. "
    "뜻과 상황만 English 로 주고 답은 네가 말하지 마라. 이 3개를 다 치운 뒤에 새 항목으로 간다. "
    "이 쪽지는 읽지 마라." % (seeds.NOTE_TAG, QUIZ_LABELS)
)
_HEAD = ("%s 이 문장은 읽지 마라. 이 쪽지의 언어를 따라가지 마라. "
         "네가 말하는 언어는 [절대 금지] 3 이 정한 그대로다. " % seeds.NOTE_TAG)
assert FULL_CUE.startswith(_HEAD), "큐 머리말이 바뀌었다 — _HEAD 를 맞춰라"
ACTION_FIRST_CUE = (
    "%s %s 이 쪽지는 읽지 마라. 쪽지의 언어를 따라가지 마라 — "
    "네가 말하는 언어는 [절대 금지] 3 이 정한 그대로다."
    % (seeds.NOTE_TAG, FULL_CUE[len(_HEAD):])
)

CONDITIONS = {
    "A": {"role": "user",   "cue": "full",   "immediate": False},
    "B": {"role": "system", "cue": "full",   "immediate": False},
    "C": {"role": "user",   "cue": "short",  "immediate": False},
    "E": {"role": "user",   "cue": "action", "immediate": False},
    "F": {"role": "user",   "cue": "notail", "immediate": False},
    "G": {"role": "user",   "cue": "bare",   "immediate": False},
    "H": {"role": "system", "cue": "notail", "immediate": False},
    "Z": {"role": "user",   "cue": "full",   "immediate": True},
}
CUES = {"full": FULL_CUE, "short": SHORT_CUE, "action": ACTION_FIRST_CUE,
        "notail": NO_TAIL_CUE, "bare": BARE_CUE}


def instruction() -> str:
    return expr.build_expression_instruction(
        role="Baba, a friendly beaver Korean tutor",
        personality="warm, playful, a little cheeky",
        locale_label="English",
        items=ITEMS,
        quiz_group=3,
        name="the learner",
        level_note="Absolute beginner. Cannot read Hangul yet.",
        self_quiz=False,
    )


class Probe:
    def __init__(self, ws, *, audio_out: bool = False):
        self.ws = ws
        self.audio_out = audio_out
        self.errors: list[dict] = []
        self.items_created: list[str] = []
        self.tool_calls = 0
        self.usage = {"in": 0, "out": 0}

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj, ensure_ascii=False))

    async def item(self, text: str, role: str = "user") -> None:
        await self.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": role,
            "content": [{"type": "input_text", "text": text}]}})

    def _absorb(self, ev: dict) -> None:
        t = ev.get("type") or ""
        if t == "conversation.item.created":
            it = (ev.get("item") or {})
            self.items_created.append("%s/%s" % (it.get("role"), it.get("type")))
        elif t == "error":
            self.errors.append(ev.get("error") or ev)

    @property
    def _mods(self) -> list:
        return ["audio"] if self.audio_out else ["text"]

    async def turn(self, timeout: float = 120.0) -> str:
        """response.create → 완결 대사 1개(오디오면 출력 전사). 툴콜 턴(응답 2개)을 처리한다."""
        await self.send({"type": "response.create",
                         "response": {"output_modalities": self._mods}})
        buf: list[str] = []
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                return "".join(buf) or "(TIMEOUT)"
            raw = await asyncio.wait_for(self.ws.recv(), timeout=left)
            ev = json.loads(raw)
            t = ev.get("type") or ""
            if t in ("response.output_text.delta", "response.text.delta",
                     "response.output_audio_transcript.delta",
                     "response.audio_transcript.delta"):
                buf.append(ev.get("delta") or "")
            elif t in ("response.output_audio.delta", "response.audio.delta"):
                pass          # 바이트는 버린다(전사만 센다)
            elif t == "response.done":
                resp = ev.get("response") or {}
                u = resp.get("usage") or {}
                self.usage["in"] += int(u.get("input_tokens") or 0)
                self.usage["out"] += int(u.get("output_tokens") or 0)
                items = resp.get("output") or []
                if not buf:
                    for it in items:
                        for c in (it or {}).get("content") or []:
                            if c.get("text"):
                                buf.append(c["text"])
                            elif c.get("transcript"):
                                buf.append(c["transcript"])
                only_tool = bool(items) and all(
                    (i or {}).get("type") == "function_call" for i in items)
                if only_tool:
                    # 운영 어댑터와 같은 규율: 형식 응답 → response.create 로 재개
                    for it in items:
                        self.tool_calls += 1
                        await self.send({"type": "conversation.item.create", "item": {
                            "type": "function_call_output",
                            "call_id": it.get("call_id"),
                            "output": json.dumps({"result": "ok"})}})
                    await self.send({"type": "response.create",
                                     "response": {"output_modalities": self._mods}})
                    continue
                return "".join(buf)
            else:
                self._absorb(ev)

    async def drain(self, seconds: float = 0.6) -> None:
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=left)
            except (asyncio.TimeoutError, TimeoutError):
                return
            self._absorb(json.loads(raw))


async def one_trial(cond: str, key: str, instr: str, *, face: bool,
                    audio_out: bool = False) -> dict:
    import websockets
    spec = CONDITIONS[cond]
    cue = CUES[spec["cue"]]
    async with websockets.connect(
        "%s?model=%s" % (URL, MODEL),
        additional_headers={"Authorization": "Bearer %s" % key},
        max_size=None, open_timeout=30,
    ) as ws:
        p = Probe(ws, audio_out=audio_out)
        sess: dict = {
            "type": "realtime", "instructions": instr,
            "output_modalities": ["audio"] if audio_out else ["text"],
            "audio": {"input": {"turn_detection": None}},
        }
        if audio_out:
            # ⭐ 운영과 같게 — PCM24k + marin(`core/openai/session.py:AUDIO_FORMAT`·DEFAULT_VOICE)
            from core.openai.audio import AUDIO_FORMAT
            from core.openai.session import DEFAULT_VOICE
            sess["audio"]["output"] = {"format": dict(AUDIO_FORMAT), "voice": DEFAULT_VOICE}
        if face:
            sess["tools"] = [set_face_tool()]
            sess["tool_choice"] = "auto"
        else:
            sess["tools"] = []
            sess["tool_choice"] = "none"
        await p.send({"type": "session.update", "session": sess})
        await p.drain(1.5)
        turns: list[dict] = []
        await p.item(expr.seed_opening("English"))
        turns.append({"who": "beaver", "tag": "open", "text": await p.turn()})
        for i, u in enumerate(LEARNER_TURNS):
            await p.item(u)
            turns.append({"who": "learner", "text": u})
            turns.append({"who": "beaver", "tag": "pre%d" % i, "text": await p.turn()})
        # ── 큐 주입 ─────────────────────────────────────────────────────────── #
        before = len(p.errors)
        await p.item(cue, role=spec["role"])
        await p.drain(1.0)
        cue_rejected = len(p.errors) > before
        turns.append({"who": "cue", "role": spec["role"], "rejected": cue_rejected,
                      "chars": len(cue)})
        if spec["immediate"]:
            turns.append({"who": "beaver", "tag": "post0", "text": await p.turn()})
            await p.item(LEARNER_NEXT)
            turns.append({"who": "learner", "text": LEARNER_NEXT})
            turns.append({"who": "beaver", "tag": "post1", "text": await p.turn()})
        else:
            await p.item(LEARNER_AFTER_CUE)
            turns.append({"who": "learner", "text": LEARNER_AFTER_CUE})
            turns.append({"who": "beaver", "tag": "post0", "text": await p.turn()})
            await p.item(LEARNER_NEXT)
            turns.append({"who": "learner", "text": LEARNER_NEXT})
            turns.append({"who": "beaver", "tag": "post1", "text": await p.turn()})
        return {"cond": cond, "face": face, "audio_out": audio_out,
                "turns": turns, "errors": p.errors,
                "items": p.items_created, "usage": p.usage, "tool_calls": p.tool_calls,
                "cue_rejected": cue_rejected, "cue_chars": len(cue)}


JUDGE_PROMPT = """You grade ONE turn of a Korean tutor talking to an English-speaking beginner.

Lesson list (numbered): 1 고마워요 (thank you), 2 아니에요 (not at all), 3 네 (yes),
4 좋아요 (good/okay), 5 맞아요 (that is right), 6 진짜요? (really?),
7 처음 뵙겠습니다, 8 잘 부탁드립니다, 9 저는 ◯◯ 사람이에요, 10 이름이 뭐예요?,
11 저는 ◯◯이에요, 12 안녕하세요?, 13 만나서 반갑습니다, 14 잘 지냈어요?, 15 감사합니다.

State before this turn: items 1, 2, 3 were already taught and answered correctly.
The tutor had just introduced item 4. Then a server note ordered the tutor to STOP
advancing and RE-QUIZ items 1, 2, 3 (고마워요 / 아니에요 / 네), one per turn.

TUTOR TURN:
<<<%s>>>

Answer strict JSON only, no prose:
{"verdict": "quiz", "target_number": 0, "read_note_aloud": false,
 "korean_for_explanation": false}
Where:
- verdict "quiz"  = the tutor elicits one of items 1, 2, 3 from the learner (the re-quiz).
- verdict "new"   = the tutor elicits / introduces an item numbered 4 or higher.
- verdict "other" = neither (small talk, closing, pure praise, no elicitation).
- target_number = the lesson number the tutor is eliciting (0 if none).
- read_note_aloud = did the tutor speak the bracketed server note or its wording aloud?
- korean_for_explanation = did the tutor use Korean for explanation/instructions,
  beyond uttering the target expression itself?
"""


def judge(text: str, key: str) -> dict:
    import urllib.request
    body = json.dumps({
        "model": JUDGE_MODEL, "temperature": 0,
        "messages": [{"role": "user", "content": JUDGE_PROMPT % text}],
        "response_format": {"type": "json_object"},
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions", data=body,
        headers={"Authorization": "Bearer %s" % key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    return json.loads(out["choices"][0]["message"]["content"])


def rule_label(text: str) -> dict:
    """⭐ 판정기와 **독립**인 문자열 라벨(판정기 편향 교차검증용).
    세트 표면형(1·2·3)이 나왔나 / 꼬리 첫 줄(좋아요)·그 뒤 번호가 나왔나."""
    t = text or ""
    set_hit = [s for s in SET_SURFACES if s in t]
    ahead = [ITEMS[n - 1]["obj"] for n in range(4, 16) if ITEMS[n - 1]["obj"] in t]
    return {"set_surface": set_hit, "ahead_surface": ahead,
            "tail_head": TAIL_HEAD_SURFACE in t}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conditions", default="A,B,C,E,F,G,H,Z")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gap", type=float, default=12.0, help="시행 사이 대기(TPM 40k 보호)")
    ap.add_argument("--face", action="store_true", help="표정 툴 ON(운영과 같게)")
    ap.add_argument("--audio-out", action="store_true",
                    help="출력 모달리티=audio(운영과 같게) — 전사만 센다")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--dump-prompt", default=None)
    args = ap.parse_args()

    instr = instruction()
    if args.dump_prompt:
        parts = ["=== INSTRUCTION (%d chars) ===\n%s" % (len(instr), instr)]
        for k, v in CUES.items():
            parts.append("=== CUE %s (%d chars) ===\n%s" % (k, len(v), v))
        Path(args.dump_prompt).write_text("\n\n".join(parts), encoding="utf-8")
        print("prompt dumped ->", args.dump_prompt)
        return

    key = (os.environ.get("GPT_API_KEY") or "").strip()
    if not key:
        raise SystemExit("GPT_API_KEY 가 없다")
    conds = [c.strip() for c in args.conditions.split(",") if c.strip()]
    results = []
    for rep in range(args.n):
        for c in conds:
            t0 = time.time()
            try:
                r = await one_trial(c, key, instr, face=args.face,
                                    audio_out=args.audio_out)
            except Exception as exc:          # noqa: BLE001
                r = {"cond": c, "fatal": "%s: %s" % (type(exc).__name__, exc)}
            r["rep"] = rep
            r["secs"] = round(time.time() - t0, 1)
            for t in r.get("turns", []):
                if t.get("who") == "beaver":
                    t["rule"] = rule_label(t.get("text") or "")
                    if not args.no_judge and str(t.get("tag", "")).startswith("post"):
                        try:
                            t["judge"] = judge(t["text"], key)
                        except Exception as exc:   # noqa: BLE001
                            t["judge"] = {"error": str(exc)}
            results.append(r)
            verdicts = [(t.get("judge") or {}).get("verdict") for t in r.get("turns", [])
                        if str(t.get("tag", "")).startswith("post")]
            targets = [(t.get("judge") or {}).get("target_number") for t in r.get("turns", [])
                       if str(t.get("tag", "")).startswith("post")]
            print("[%s%s%s rep%d] %.0fs usage=%s tool=%s rejected=%s %s%s %s" % (
                c, "+face" if args.face else "", "+audio" if args.audio_out else "",
                rep, r.get("secs", 0), r.get("usage"), r.get("tool_calls"),
                r.get("cue_rejected"), verdicts, targets, r.get("fatal", "")), flush=True)
            Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
            await asyncio.sleep(args.gap)
    print("done ->", args.out)


if __name__ == "__main__":
    asyncio.run(main())
