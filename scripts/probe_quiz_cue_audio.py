# -*- coding: utf-8 -*-
"""[dev·조사] 퀴즈 큐 — **운영과 같은 오디오 경로**로 재연한다(텍스트 하네스의 빈 칸 메우기).

## 왜 이 두 번째 하네스가 필요한가
`probe_quiz_cue_obedience.py`(텍스트 모달리티)에서는 모델이 큐를 **지켰다**. 그런데
운영(call 1740·1741)은 안 지켰다. 남은 차이는 **오디오**다:
  ① 학습자 턴이 오디오 항목이다 — 큐만 텍스트 1개로 끼인다.
  ② 큐가 **학습자 발화 중간**에 들어간다(`_maybe_attach_reground_on_mic` RMS 관문) —
     `semantic_vad` 가 커밋하기 **전**이다. 항목 순서가 측정된 바 없다.
  ③ 운영 어댑터는 `conversation.item.added/created` 를 **안 읽는다**(`core/openai/session.py`
     `_normalize` 에 그 분기가 없다) ⇒ 큐가 받아들여졌는지 확인한 적이 없다.

## 운영 불변식을 지킨다
- **barge-in off**: 비버 응답이 완전히 끝날 때까지 마이크를 **안 보낸다**(클라 `_micGated`).
- 수동 `commit` 없음 — 턴 경계는 `semantic_vad` 가 정한다.
- 출력 오디오 PCM24k · 음색 marin · 표정 툴은 `--face` 로.
- ⛔ 받기와 보내기를 **섞지 않는다**. 섞었던 첫 판은 비버 턴이 잘려 측정이 오염됐다.

## ⛔ 비용
오디오 입·출력이 누적 재청구된다. 1 시행 ~$0.05~0.2. **n 을 작게** 돌려라.

## 사용
    GPT_API_KEY=... TTS_CACHE=<dir> python scripts/probe_quiz_cue_audio.py \
        --conditions A,F --n 2 --out <json>
⛔ 운영 코드·프롬프트 무변경.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import probe_quiz_cue_obedience as T        # noqa: E402  (재료·조건·판정기 재사용)
from core.openai.audio import AUDIO_FORMAT  # noqa: E402
from core.openai.prompts import expression as expr   # noqa: E402
from core.openai.session import DEFAULT_VOICE        # noqa: E402
from core.openai.tools import set_face_tool          # noqa: E402

TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICE = "alloy"
SR = 24000
CHUNK_MS = 100
CHUNK_BYTES = SR * 2 * CHUNK_MS // 1000     # PCM16 mono
SILENCE_TAIL_CHUNKS = 26                    # 2.6s — semantic_vad 가 1.98초 쉼까지 버틴다
ITEM_EVENTS = ("conversation.item.created", "conversation.item.added",
               "conversation.item.done")


def tts_pcm(text: str, key: str, cache: Path) -> bytes:
    """학습자 대사 → PCM16/24k. 같은 글자는 파일로 캐시한다(재실행 비용 0)."""
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / ("%s.pcm" % "".join("%04x" % ord(c) for c in text)[:60])
    if f.exists():
        return f.read_bytes()
    body = json.dumps({"model": TTS_MODEL, "voice": TTS_VOICE, "input": text,
                       "response_format": "pcm"}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech", data=body,
        headers={"Authorization": "Bearer %s" % key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        pcm = r.read()
    f.write_bytes(pcm)
    return pcm


class AudioProbe:
    def __init__(self, ws):
        self.ws = ws
        self.events: list[dict] = []
        self.errors: list[dict] = []
        self.usage = {"in": 0, "out": 0, "in_audio": 0, "in_text": 0,
                      "in_cached": 0, "out_audio": 0}
        self.t0 = time.time()

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj, ensure_ascii=False))

    def _note(self, kind: str, **kw) -> None:
        self.events.append(dict(t=round(time.time() - self.t0, 2), kind=kind, **kw))

    def _absorb(self, ev: dict) -> str:
        t = ev.get("type") or ""
        if t in ITEM_EVENTS:
            it = ev.get("item") or {}
            head = ""
            for c in it.get("content") or []:
                head = (c.get("text") or c.get("transcript") or "")[:36]
                if head:
                    break
            self._note(t.split(".", 1)[1], id=(it.get("id") or "")[-6:],
                       role=it.get("role"), itype=it.get("type"),
                       prev=(ev.get("previous_item_id") or "")[-6:], head=head)
        elif t.startswith("input_audio_buffer."):
            self._note(t.split(".", 1)[1], item=(ev.get("item_id") or "")[-6:])
        elif t == "error":
            self.errors.append(ev.get("error") or ev)
            self._note("ERROR", detail=json.dumps(ev.get("error") or {},
                                                  ensure_ascii=False)[:250])
        elif t == "conversation.item.input_audio_transcription.completed":
            self._note("in_tr", item=(ev.get("item_id") or "")[-6:],
                       text=(ev.get("transcript") or "")[:60])
        elif t == "response.created":
            self._note("resp.created", id=((ev.get("response") or {}).get("id") or "")[-6:])
        return t

    def _usage(self, resp: dict) -> None:
        u = resp.get("usage") or {}
        self.usage["in"] += int(u.get("input_tokens") or 0)
        self.usage["out"] += int(u.get("output_tokens") or 0)
        d = u.get("input_token_details") or {}
        self.usage["in_audio"] += int(d.get("audio_tokens") or 0)
        self.usage["in_text"] += int(d.get("text_tokens") or 0)
        self.usage["in_cached"] += int(d.get("cached_tokens") or 0)
        od = u.get("output_token_details") or {}
        self.usage["out_audio"] += int(od.get("audio_tokens") or 0)

    async def push(self, pcm: bytes, *, inject: str | None = None,
                   inject_role: str = "user", inject_at: float = 0.5) -> None:
        """학습자 오디오 + 무음 꼬리를 **실시간 속도로 보낸다**(받지 않는다).
        `inject` 가 있으면 오디오 중간(= 운영의 RMS 관문 자리)에 쪽지를 끼운다."""
        n = max(1, (len(pcm) + CHUNK_BYTES - 1) // CHUNK_BYTES)
        cut = int(n * inject_at)
        for i in range(n):
            chunk = pcm[i * CHUNK_BYTES:(i + 1) * CHUNK_BYTES]
            if chunk:
                await self.send({"type": "input_audio_buffer.append",
                                 "audio": base64.b64encode(chunk).decode("ascii")})
            if inject is not None and i == cut:
                self._note("CUE.send", role=inject_role, chars=len(inject))
                await self.send({"type": "conversation.item.create", "item": {
                    "type": "message", "role": inject_role,
                    "content": [{"type": "input_text", "text": inject}]}})
            await asyncio.sleep(CHUNK_MS / 1000.0)
        sil = b"\x00" * CHUNK_BYTES
        for _ in range(SILENCE_TAIL_CHUNKS):
            await self.send({"type": "input_audio_buffer.append",
                             "audio": base64.b64encode(sil).decode("ascii")})
            await asyncio.sleep(CHUNK_MS / 1000.0)

    async def wait_turn(self, timeout: float = 30.0) -> str:
        """⭐ **대사가 있는** `response.done` 까지 받는다 — 툴 전용 응답은 건너뛴다.

        ⛔ 두 가지를 첫 판에서 배웠다(둘 다 하네스 결함이었다):
        ① `status=failed`(출력 0)인 응답이 온다 — 그걸 안 받으면 90초를 헛기다리고
           그 사이 VAD 가 다음 발화를 **같은 턴으로 합친다**(전사 「네, 좋아요.」).
        ② 기다리는 동안 **프레임을 하나도 안 보내면** VAD 가 발화를 못 닫는다(95초 열림).
           운영 클라는 비버 침묵 구간에 무음 프레임을 계속 보낸다 ⇒ 여기서도 보낸다.
        """
        buf: list[str] = []
        deadline = time.time() + timeout
        sil = bytes(CHUNK_BYTES)
        while True:
            left = deadline - time.time()
            if left <= 0:
                return "".join(buf) or "(TIMEOUT)"
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=min(0.4, left))
            except (asyncio.TimeoutError, TimeoutError):
                await self.send({"type": "input_audio_buffer.append",
                                 "audio": base64.b64encode(sil).decode("ascii")})
                continue
            ev = json.loads(raw)
            t = self._absorb(ev)
            if t in ("response.output_audio_transcript.delta",
                     "response.audio_transcript.delta",
                     "response.output_text.delta", "response.text.delta"):
                buf.append(ev.get("delta") or "")
            elif t == "response.done":
                resp = ev.get("response") or {}
                self._usage(resp)
                items = resp.get("output") or []
                status = str(resp.get("status") or "")
                self._note("resp.done", status=status,
                           kinds=",".join(str((i or {}).get("type")) for i in items))
                only_tool = bool(items) and all(
                    (i or {}).get("type") == "function_call" for i in items)
                if only_tool:
                    for it in items:
                        await self.send({"type": "conversation.item.create", "item": {
                            "type": "function_call_output", "call_id": it.get("call_id"),
                            "output": json.dumps({"result": "ok"})}})
                    await self.send({"type": "response.create",
                                     "response": {"output_modalities": ["audio"]}})
                    continue
                if not buf:
                    for it in items:
                        for c in (it or {}).get("content") or []:
                            if c.get("transcript") or c.get("text"):
                                buf.append(c.get("transcript") or c.get("text"))
                if status in ("failed", "incomplete"):
                    self._note("resp.FAILED", detail=json.dumps(
                        resp.get("status_details") or {}, ensure_ascii=False)[:250])
                    return "".join(buf) or "(FAILED:%s)" % status
                if not buf and status == "cancelled":
                    continue        # 끼어들기로 취소 — 다음 응답을 기다린다
                return "".join(buf)


async def one_trial(cond: str, key: str, instr: str, *, face: bool, cache: Path) -> dict:
    import websockets
    spec = T.CONDITIONS[cond]
    cue = T.CUES[spec["cue"]]
    lines = list(T.LEARNER_TURNS) + [T.LEARNER_AFTER_CUE, T.LEARNER_NEXT]
    pcms = {s: tts_pcm(s, key, cache) for s in set(lines)}

    async with websockets.connect(
        "%s?model=%s" % (T.URL, T.MODEL),
        additional_headers={"Authorization": "Bearer %s" % key},
        max_size=None, open_timeout=30,
    ) as ws:
        p = AudioProbe(ws)
        sess: dict = {
            "type": "realtime", "instructions": instr,
            "output_modalities": ["audio"],
            "audio": {
                "input": {"format": dict(AUDIO_FORMAT),
                          "turn_detection": {"type": "semantic_vad"},
                          "transcription": {"model": "gpt-4o-mini-transcribe"}},
                "output": {"format": dict(AUDIO_FORMAT), "voice": DEFAULT_VOICE},
            },
        }
        sess["tools"] = [set_face_tool()] if face else []
        sess["tool_choice"] = "auto" if face else "none"
        await p.send({"type": "session.update", "session": sess})
        await asyncio.sleep(1.5)

        turns: list[dict] = []
        await p.send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": expr.seed_opening("English")}]}})
        await p.send({"type": "response.create",
                      "response": {"output_modalities": ["audio"]}})
        turns.append({"who": "beaver", "tag": "open", "text": await p.wait_turn()})
        await asyncio.sleep(0.8)        # barge-in off — 비버 꼬리가 끝날 틈

        for i, line in enumerate(T.LEARNER_TURNS):
            await p.push(pcms[line])
            turns.append({"who": "learner", "text": line, "mode": "audio"})
            turns.append({"who": "beaver", "tag": "pre%d" % i, "text": await p.wait_turn()})
            await asyncio.sleep(0.8)

        # ── 큐: 학습자 발화 **중간**에 끼운다(운영의 자리) ───────────────────── #
        turns.append({"who": "cue", "role": spec["role"], "chars": len(cue),
                      "where": "mid-utterance"})
        await p.push(pcms[T.LEARNER_AFTER_CUE], inject=cue, inject_role=spec["role"])
        turns.append({"who": "learner", "text": T.LEARNER_AFTER_CUE, "mode": "audio"})
        turns.append({"who": "beaver", "tag": "post0", "text": await p.wait_turn()})
        await asyncio.sleep(0.8)

        await p.push(pcms[T.LEARNER_NEXT])
        turns.append({"who": "learner", "text": T.LEARNER_NEXT, "mode": "audio"})
        turns.append({"who": "beaver", "tag": "post1", "text": await p.wait_turn()})

        return {"cond": cond, "face": face, "path": "audio", "turns": turns,
                "errors": p.errors, "usage": p.usage, "events": p.events,
                "cue_chars": len(cue)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conditions", default="A")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--gap", type=float, default=15.0)
    ap.add_argument("--face", action="store_true")
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()

    key = (os.environ.get("GPT_API_KEY") or "").strip()
    if not key:
        raise SystemExit("GPT_API_KEY 가 없다")
    cache = Path(os.environ.get("TTS_CACHE") or (Path.cwd() / ".tts_cache"))
    instr = T.instruction()
    results = []
    for rep in range(args.n):
        for c in [x.strip() for x in args.conditions.split(",") if x.strip()]:
            t0 = time.time()
            try:
                r = await one_trial(c, key, instr, face=args.face, cache=cache)
            except Exception as exc:        # noqa: BLE001
                import traceback
                r = {"cond": c, "fatal": "%s: %s" % (type(exc).__name__, exc),
                     "tb": traceback.format_exc()[-1500:]}
            r["rep"] = rep
            r["secs"] = round(time.time() - t0, 1)
            for t in r.get("turns", []):
                if t.get("who") == "beaver":
                    t["rule"] = T.rule_label(t.get("text") or "")
                    if not args.no_judge and str(t.get("tag", "")).startswith("post"):
                        try:
                            t["judge"] = T.judge(t["text"], key)
                        except Exception as exc:    # noqa: BLE001
                            t["judge"] = {"error": str(exc)}
            results.append(r)
            v = [(t.get("judge") or {}).get("verdict") for t in r.get("turns", [])
                 if str(t.get("tag", "")).startswith("post")]
            tn = [(t.get("judge") or {}).get("target_number") for t in r.get("turns", [])
                  if str(t.get("tag", "")).startswith("post")]
            print("[AUDIO %s%s rep%d] %.0fs usage=%s %s%s %s" % (
                c, "+face" if args.face else "", rep, r.get("secs", 0), r.get("usage"),
                v, tn, r.get("fatal", "")), flush=True)
            Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
            await asyncio.sleep(args.gap)
    print("done ->", args.out)


if __name__ == "__main__":
    asyncio.run(main())
