# -*- coding: utf-8 -*-
"""[dev] 표현학습 통화 흐름 자동 실험 — 실제 GPT 통화를 대본대로 돌려 본다(2026-10-11 사장님 지시).

⚠ **실통화다.** 배포된 gpt-api 에 WS 로 붙어 OpenAI Realtime 을 실제로 돌린다 — 전사·진도·원가가
  DB 에 기록된다(운영 DB 공유). 시험 계정(기본 test@naver.com)으로만 돌릴 것.

## 흐름(사장님 설계)
1. `POST /__dev/cur-reset` 로 그 계정의 학습 상태를 백지화 → 1차시 항목이 seq 순으로 나온다
2. 선톡: **영어만(예문 제외)** · **4문장 미만**인지
3. 1번 정답 → 반응이 영어인지
4. 2번에 **틀린 문장** → 틀렸다고 제대로 받는지
5. 다시 시키면 2번 정답
6. 3번 정답 → **되묻기(퀴즈)로 들어가는지**
7. (덤) 퀴즈 1 정답 · 퀴즈 2 「I don't know」 → 힌트가 아니라 정답을 주는지

## 학습자 목소리
OpenAI TTS(`/v1/audio/speech`, pcm 24k) → 16k 로 리샘플 → 20ms 조각으로 실시간 페이싱 전송 →
꼬리 무음 1.5초(semantic VAD 가 턴을 닫게). ⛔ 텍스트 입력 메시지는 프로토콜에 없다 — 소리뿐.

## 턴 규약
서버는 비버가 말하는 동안 마이크를 버린다(barge-in off, `state.turn_id is None` 게이트).
그래서 `turn_end` 를 받은 뒤 `playback_done` 을 보내고 나서야 학습자 소리를 보낸다.

사용법:
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/smoke_expression_flow.py --out flow.txt
    옵션: --base URL · --email · --pw · --no-reset · --turn-timeout 60
"""

from __future__ import annotations

import argparse
import asyncio
import audioop  # noqa: F401 — 3.11 에 있다(3.13 에서 빠진다)
import io
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets  # noqa: E402

from core.config import settings  # noqa: E402

HANGUL = re.compile(r"[가-힣]")
LATIN = re.compile(r"[A-Za-z]")
SENT_SPLIT = re.compile(r"(?<=[.!?。？！])\s+|\n+")


# ─────────────────────────── HTTP ─────────────────────────── #
def _http(base, path, payload=None, token=None, method=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


# ─────────────────────────── TTS ─────────────────────────── #
def tts_pcm16k(text: str, voice: str = "alloy") -> bytes:
    """OpenAI TTS → PCM16 mono 16k. 학습자 목소리."""
    req = urllib.request.Request(
        "https://api.openai.com/v1/audio/speech",
        data=json.dumps({"model": "gpt-4o-mini-tts", "voice": voice, "input": text,
                         "response_format": "pcm"}).encode("utf-8"),
        headers={"Authorization": "Bearer " + settings.GPT_API_KEY, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        pcm24 = r.read()
    pcm16, _ = audioop.ratecv(pcm24, 2, 1, 24000, 16000, None)
    return pcm16


# ─────────────────────────── 분석 ─────────────────────────── #
def analyze(text: str, ko_allowed: list[str]) -> dict:
    """비버 한 턴: 문장 수 · 허용된 한국어(예문) 밖의 한글 글자 수."""
    sents = [s for s in SENT_SPLIT.split(text.strip()) if s.strip()]
    stripped = text
    for ko in ko_allowed:
        stripped = stripped.replace(ko, "")
        stripped = stripped.replace(ko.rstrip("?.!"), "")
    leak = len(HANGUL.findall(stripped))
    return {"sentences": len(sents), "hangul_total": len(HANGUL.findall(text)),
            "hangul_leak": leak, "latin": len(LATIN.findall(text))}


def fill(ko: str) -> str:
    # ⚠ 표면형은 이름·나라 모두 ◯◯ 다(「저는 ◯◯ 사람이에요」). 1854: 나라 자리에 이름을 넣어
    #   「저는 민수 사람이에요」가 됐고 비버가 세 턴 내리 틀렸다고 했다 — «사람이에요» 가 있으면 나라다.
    who = "미국" if "사람이에요" in ko else "민수"
    return (ko.replace("(이름)", "민수").replace("(나라)", "미국")
              .replace("◯◯", who).replace("○○", who))


# ─────────────────────────── 본체 ─────────────────────────── #
async def run(args, log):
    base = args.base.rstrip("/")
    tok = _http(base, "/__dev/login", {"email": args.email, "password": args.pw})["access_token"]
    log("로그인 ok")

    if not args.no_reset:
        r = _http(base, "/__dev/cur-reset", {}, tok)
        log("cur-reset → %s" % json.dumps(r, ensure_ascii=False)[:200])

    # ⭐ 서버가 **실제로 고르는 함수**(`select_items`)를 그대로 불러 목록을 받는다.
    #   ⛔ `cur_lesson_item.seq` 로 직접 뽑으면 틀린다 — 첫 실행(1837)에서 그렇게 뽑아
    #     「안녕히 가세요」를 1번으로 믿었는데 비버는 「안녕하세요?」를 가르쳤다(정답이 전부 오답 처리).
    import db.registry  # noqa: F401
    from sqlalchemy import create_engine, text as sqltext
    from sqlalchemy.orm import Session
    from domains.learning.service import curriculum_service as cur_svc
    eng = create_engine(settings.direct_url, poolclass=None)
    with Session(eng) as db:
        member_id = int(r["member_id"]) if not args.no_reset else int(_http(base, "/__dev/members", None, tok)[0]["member_id"])
        code = r["lesson"]["code"] if not args.no_reset else "L1-S01-1"
        lesson_ids = [x[0] for x in db.execute(sqltext("select lesson_id from cur_lesson where code=:c order by lesson_id"), {"c": code})]
        chosen = None
        for lid in lesson_ids:
            dto = cur_svc.select_items(db, member_id, lid, locale="en")
            if dto and HANGUL.search(dto[0].get("obj") or ""):
                chosen, items_dto = lid, dto; break
        if chosen is None:
            log("⛔ 한국어 차시를 못 찾았다: %s" % lesson_ids); return
    items = [d["obj"] for d in items_dto]
    log("lesson_id=%s select_items → %s" % (chosen, items[:8]))
    if len(items) < 3:
        log("⛔ 항목이 3개 미만 — 중단"); return

    # ─────────────────── 학습자 응답기 — 비버가 **무엇을 묻는지** 보고 답한다 ─────────────────── #
    # ⛔ 대본을 순서대로 기계적으로 보내면 안 된다(1838): 비버가 1번을 되풀이하자 그 뒤가 전부
    #   한 칸씩 어긋나 「학습자가 자꾸 다음 문장을 먼저 말하는」 통화가 됐다.
    #   비버 발화에서 ①한국어 항목 문장이 있으면 그 항목을 시키는 것 → 정답(2번은 첫 시도만 오답)
    #   ②없으면 되묻기로 보고 **뜻(영어)** 으로 항목을 찾아 답한다(두 번째 되묻기는 「모른다」).
    WRONG = "감자 먹어요"
    DONT = "I don't know that one, sorry, I forgot it."
    # ⚠ 1847·1849: 「Sorry, I don't know this one.」 은 semantic VAD 가 턴을 안 닫는 일이 3/4 — 끝맺음이
    #   더 분명한 글자로 바꾸고, 재발화는 **다른 글자**로 한다(같은 말 반복도 안 닫혔다).
    #   ⛔ 「Hmm, …」 처럼 **문장 중간에 쉼**이 있으면 안 된다(1850: 「흠...」 에서 턴이 닫히고 나머지가
    #     다음 발화에 붙었다) — 한 호흡으로 이어지는 한 문장이어야 한다.
    DONT_ALT = "Sorry, I really don't remember that one."
    surfaces = [fill(x) for x in items]
    # 대조 키 — 이름을 채운 꼴과 **코드가 싣는 꼴**(«저는 (이름)이에요», `_fill_slots`) 둘 다 본다.
    #   1839 에서 비버가 「저는 (이름)이에요」라고 했는데 키가 「저는민수이에요」뿐이라 못 맞췄다.
    from core.openai.prompts.freetalk import _fill_slots
    _norm = lambda s: re.sub(r"[?.!\s“”\"'「」()]", "", s)  # noqa: E731
    keys = [[_norm(fill(x)), _norm(_fill_slots(x)), _norm(x)] for x in items]
    meanings = [(d.get("des") or "").strip().lower() for d in items_dto]
    # 허용 한국어 = 이름 채운 꼴 + 원형 + **코드가 싣는 꼴**(«저는 (이름)이에요»). 1841 t11·t12 가
    #   「(이름)」「(나라)」 때문에 누출로 잘못 잡혔다 — 비버는 코드가 준 문장을 그대로 읽은 것.
    allowed = surfaces + items + [_fill_slots(x) for x in items]
    wrong_done = False
    review_count = 0
    dont_item = None          # 「모른다」고 한 항목 — 다음 턴에 정답을 주는지 본다
    max_taught = -1           # 비버가 시킨 가장 큰 항목 번호
    first_review_turn = None
    answer_after_dont = None

    def asked_item(text):
        """비버 발화에 들어 있는 항목 문장 중 **마지막** 것의 번호. 없으면 None."""
        flat = _norm(text)
        best, pos = None, -1
        for i, ks in enumerate(keys):
            for k in ks:
                if k and (p := flat.rfind(k)) > pos:
                    best, pos = i, p
        return best

    def meaning_item(text):
        low = text.lower()
        best, pos = None, -1
        for i, m in enumerate(meanings):
            if not m:
                continue
            cands = [m] + [c.strip() for c in re.split(r"[/,;]", m) if len(c.strip()) >= 4]
            for cnd in cands:
                p = low.rfind(cnd.lower().rstrip(".?!"))
                if p > pos:
                    best, pos = i, p
        return best

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + "/api/v1/calls/stream?token=" + tok
    report = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "call_type": "expression", "duration_min": 5}))
        turn_text = []
        turn_id = None
        beaver_turns = 0
        audio_bytes = 0
        heard = []

        import random

        def _quiet(n_chunks, chunk):
            """⚠ 완전한 0 이 아니라 **아주 작은 잡음**을 보낸다 — 디지털 무음은 VAD 가 「소리 끊김」으로
            볼 수 있어 턴이 안 닫힌다는 의심(1840: 앞 몇 턴은 닫히고 뒤부터 한 박자씩 밀렸다)."""
            for _ in range(n_chunks):
                yield bytes().join(int(random.randint(-24, 24)).to_bytes(2, "little", signed=True)
                                   for _ in range(chunk // 2))

        async def send_learner(label, text):
            nonlocal last_say, awaiting, resent
            pcm = await asyncio.get_running_loop().run_in_executor(None, tts_pcm16k, text)
            log("→ 학습자(%s): %r  (%.1fs)" % (label, text, len(pcm) / 32000))
            chunk = 640  # 20ms @16k mono 16bit
            # 앞 무음 0.4s — 비버 턴 직후 첫 조각이 잘리면(1839 t1 뒤) VAD 가 턴을 못 닫는다.
            for q in _quiet(20, chunk):
                await ws.send(q); await asyncio.sleep(0.02)
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i:i + chunk]); await asyncio.sleep(0.02)
            # 꼬리 무음 4s — 짧은 영어가 턴으로 안 닫혀 다음 발화와 합쳐졌다(1837·1838·1849).
            for q in _quiet(200, chunk):
                await ws.send(q); await asyncio.sleep(0.02)
            if not label.endswith("·재"):
                last_say, resent = (label, text), False
            awaiting = True

        # ⚠ 학습자 발화가 턴으로 안 닫히는 일이 잦다(1839·1840: 무음 넛지 60초가 먼저 오고, 다음
        #   발화가 오면 그제야 **앞 발화**가 들린다 — 한 박자 밀림). 하네스는 보낸 뒤 8초 안에
        #   `input_transcript`/`turn_start` 가 없으면 **같은 말을 한 번 더** 한다(실제 학습자도 그런다).
        last_say = None
        awaiting = False
        resent = False
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=8 if awaiting else args.turn_timeout)
            except asyncio.TimeoutError:
                if awaiting and last_say and not resent:
                    resent = True
                    log("   (8초 무응답 — 같은 말 다시) ")
                    await send_learner(last_say[0] + "·재", DONT_ALT if last_say[1] == DONT else last_say[1])
                    continue
                log("⛔ %ds 동안 수신 없음 — 종료" % (8 if awaiting else args.turn_timeout)); break
            if isinstance(msg, (bytes, bytearray)):
                audio_bytes += len(msg); continue
            ev = json.loads(msg)
            t = ev.get("type")
            if t == "call_started":
                log("call_started call_id=%s course=%s" % (ev.get("call_id"), ev.get("course")))
            elif t == "turn_start":
                turn_id = ev.get("turn_id"); turn_text = []; awaiting = False
            elif t == "output_transcript":
                piece = ev.get("text") or ev.get("delta") or ""
                if ev.get("final") or ev.get("is_final"):
                    turn_text = [piece] if piece else turn_text
                elif piece:
                    turn_text.append(piece)
            elif t == "input_transcript":
                heard.append(ev.get("text") or ev.get("delta") or ""); awaiting = False
            elif t == "turn_end":
                beaver_turns += 1
                if heard:
                    log("   (서버가 들은 것) %r" % "".join(heard)); heard = []
                text = "".join(turn_text) if len(turn_text) > 1 else (turn_text[0] if turn_text else "")
                a = analyze(text, allowed)
                flags = []
                if a["sentences"] >= 4: flags.append("⚠4문장+")
                if a["hangul_leak"] > 0: flags.append("⚠예문밖 한글 %d자" % a["hangul_leak"])
                ai = asked_item(text)
                mi = meaning_item(text) if ai is None else None
                # 되묻기 판정은 **3개를 가르친 뒤**에만 — 그 전의 「한국어 없는 턴」은 무음 넛지·되풀이다
                #   (1839 t2: 넛지 힌트가 뜻 대조에 걸려 되묻기로 오인됐다).
                is_review = (ai is None and max_taught >= 2
                             and (mi is not None or re.search(r"review|quiz|recap|복습", text, re.I)))
                if dont_item is not None:
                    answer_after_dont = any(k and k in _norm(text) for k in keys[dont_item])
                    flags.append("정답줌 ✅" if answer_after_dont else "정답 안 줌 ❌")
                    if re.search(r"hint|nudge|clue", text, re.I): flags.append("⚠힌트")
                    dont_item = None
                if is_review:
                    review_count += 1
                    if first_review_turn is None:
                        first_review_turn = (beaver_turns, max_taught)
                    flags.append("되묻기(항목 %s)" % (mi + 1 if mi is not None else "?"))
                elif ai is not None:
                    max_taught = max(max_taught, ai)
                log("← 비버 t%d (%d문장, 한글 %d/누출 %d) %s\n     %s" % (
                    beaver_turns, a["sentences"], a["hangul_total"], a["hangul_leak"], " ".join(flags), text.replace("\n", " ⏎ ")))
                report.append((beaver_turns, a, text, flags))
                await ws.send(json.dumps({"type": "playback_done", "turn_id": turn_id}))
                await asyncio.sleep(1.5)   # 서버가 turn_id 를 비우고 마이크를 다시 받을 틈

                # ── 무엇을 말할까 ──
                if beaver_turns >= args.max_turns or review_count >= 4:
                    log("종료 조건(턴 %d · 되묻기 %d) — 소켓을 닫는다" % (beaver_turns, review_count)); break
                if ai is not None:
                    if ai == 1 and not wrong_done:
                        wrong_done = True; await send_learner("2번 오답", WRONG)
                    else:
                        await send_learner("%d번 정답" % (ai + 1), surfaces[ai])
                elif is_review and mi is not None:
                    if review_count == 2:
                        dont_item = mi; await send_learner("되묻기 %d 모름" % (mi + 1), DONT)
                    else:
                        await send_learner("되묻기 %d 답" % (mi + 1), surfaces[mi])
                elif max_taught >= 0:
                    # 한국어 문장 없는 되풀이·넛지 턴 — 지금 항목을 다시 말해 준다(흐름을 끊지 않는다).
                    await send_learner("%d번 다시" % (max_taught + 1), surfaces[max_taught])
                else:
                    await send_learner("뭘 묻는지 모름", DONT)
            elif t == "error":
                log("⛔ error: %s" % json.dumps(ev, ensure_ascii=False)); break
            elif t == "call_ended":
                log("call_ended: %s" % json.dumps(ev, ensure_ascii=False)); break
            elif t in ("pong", "sentence", "fragment_saved", "teaching_plan", "hint"):
                pass
            else:
                log("   [%s] %s" % (t, json.dumps(ev, ensure_ascii=False)[:160]))
    log("수신 오디오 %.1fs" % (audio_bytes / 48000))

    # ─────────────────── 판정 ─────────────────── #
    log("\n=== 판정 ===")
    t1 = report[0] if report else None
    ok_a = bool(t1) and t1[1]["sentences"] <= 3 and t1[1]["hangul_leak"] == 0
    ok_b = all(r[1]["hangul_leak"] == 0 for r in report)
    ok_c = first_review_turn is not None and first_review_turn[1] >= 2
    ok_d = answer_after_dont is True
    log("A 선톡 영어·3문장 이하 : %s (%s문장, 누출 %s)" % ("✅" if ok_a else "❌", t1[1]["sentences"] if t1 else "-", t1[1]["hangul_leak"] if t1 else "-"))
    log("B 전 턴 예문밖 한글 0  : %s (누출 턴 %s)" % ("✅" if ok_b else "❌", [r[0] for r in report if r[1]["hangul_leak"]]))
    log("C 3개 뒤 되묻기 시작   : %s (%s)" % ("✅" if ok_c else "❌", ("t%d, 그때까지 가르친 번호 %d" % (first_review_turn[0], first_review_turn[1] + 1)) if first_review_turn else "되묻기 없음"))
    log("D 모르면 정답 즉시     : %s" % ("✅" if ok_d else ("❌" if answer_after_dont is False else "— (못 봄)")))
    log("RESULT %s" % ("PASS" if (ok_a and ok_b and ok_c and ok_d) else "FAIL"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://beavertalk-app-gpt-api-333511894671.asia-northeast3.run.app")
    ap.add_argument("--email", default="test@naver.com")
    ap.add_argument("--pw", default="11111111")
    ap.add_argument("--no-reset", action="store_true")
    ap.add_argument("--turn-timeout", type=int, default=60)
    ap.add_argument("--max-turns", type=int, default=14)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    lines = []
    t0 = time.time()

    def log(s):
        line = "[%6.1fs] %s" % (time.time() - t0, s)
        lines.append(line)
        if args.out:
            io.open(args.out, "w", encoding="utf-8", newline="").write("\n".join(lines) + "\n")
        else:
            print(line)

    try:
        asyncio.run(run(args, log))
    except Exception as exc:  # noqa: BLE001
        log("⛔ 예외: %r" % exc)
        raise


if __name__ == "__main__":
    main()
