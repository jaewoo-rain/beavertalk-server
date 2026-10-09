"""OpenAI Realtime 단가 스파이크 (독립 실행 — 앱에 통합되지 않음).

두 가지 모드:

  1) `model`  (키 불필요, 기본)
     공식 단가 + 공식 오디오 토큰율로 15분 통화 1건 원가를 계산한다.
     Realtime 은 매 턴 «지금까지의 대화 전체» 를 입력으로 재청구하므로,
     오디오 토큰 1개는 «처음 1회는 uncached, 이후 남은 턴 수만큼 cached» 로 청구된다.
     이 재청구 구조를 그대로 모델링한다.

  2) `live`   (OPENAI_API_KEY 필요)
     gpt-realtime-mini 와 실제 WS 세션을 열어 response.done.usage 를 누적하고
     cached_tokens_details.audio_tokens 로 «오디오 캐시 적중» 을 실측한다.

출처(공식):
  단가      https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini
            https://developers.openai.com/api/docs/pricing
  토큰율    https://developers.openai.com/api/docs/guides/voice-latency-cost
            입력 오디오 1토큰/100ms, 출력 오디오 1토큰/50ms
  재청구    https://developers.openai.com/api/reference/resources/realtime/server-events
            "output from previous turns (text and audio tokens) will become the input for later turns"
  캐시필드  같은 페이지. input_token_details.cached_tokens_details.audio_tokens

사용:
  PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/spike_openai_realtime_cost.py
  PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/spike_openai_realtime_cost.py --mode live --wav some16k.wav
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys

# ---------------------------------------------------------------- 공식 단가 (USD / 1M tokens)

@dataclasses.dataclass(frozen=True)
class Price:
    name: str
    text_in: float
    text_cached: float
    text_out: float
    audio_in: float
    audio_cached: float
    audio_out: float


# ⚠ gpt-realtime / gpt-realtime-mini 는 deprecated (2027-01-20 종료). 아래는 현행 모델.
PRICES = {
    # developers.openai.com/api/docs/models/gpt-realtime-2.1-mini (2026-10-03 확인)
    #   Text  in $0.6  / cached $0.06 / out $2.4
    #   Audio in $10   / cached $0.3  / out $20
    #   128,000 context / 32,000 max output
    "mini": Price("gpt-realtime-2.1-mini", 0.60, 0.06, 2.40, 10.00, 0.30, 20.00),
    # developers.openai.com/api/docs/models/gpt-realtime-2.1 (2026-10-03 확인)
    #   Text  in $4 / cached $0.4 / out $24   (구 gpt-realtime 은 out $16 이었다)
    #   Audio in $32 / cached $0.4 / out $64
    "full": Price("gpt-realtime-2.1", 4.00, 0.40, 24.00, 32.00, 0.40, 64.00),
}

# 비교축: Gemini Live 3.1 native audio (운영 DB call.usage_* 실측 기반 — 캐시 없음)
GEMINI_MEASURED_15MIN_USD = (0.87, 0.97)

# 공식 오디오 토큰율
AUDIO_IN_TOKENS_PER_SEC = 10.0   # 1 token / 100ms
AUDIO_OUT_TOKENS_PER_SEC = 20.0  # 1 token / 50ms


# ---------------------------------------------------------------- 실측에서 얻은 구조 상수
#
# 2026-10-03 gpt-realtime-2.1-mini 실측 12턴 (scratchpad/aud) 에서 직접 읽은 값.
#
#  · in_audio 는 «학습자 오디오만» 이다. 보낸 5초 조각당 정확히 50토큰 증가(=10 tok/s)하고,
#    비버 자신의 오디오 출력(out_audio 168~329tok)은 다음 턴 in_audio 에 **전혀 안 들어간다**.
#    비버 발화는 전사 «텍스트» 로 컨텍스트에 들어온다.  <- 추정 모델의 전제가 틀렸던 지점
#  · 컨텍스트 텍스트는 턴당 +72.4 토큰 증가(turn1 2,528 -> turn12 3,324, 11턴).
#    같은 구간 비버 발화 평균 254tok(=12.7초) -> 비버 발화 1초당 약 5.7 텍스트 토큰.
#  · 지시문 4,273자(normalcall_prompt.txt) = 약 2,500 입력 텍스트 토큰.
#  · set_face 툴콜이 있으면 한 턴이 Response 2개가 되고, 2번째도 프롬프트 전체를 청구한다
#    (실측 2,914 중 2,880 캐시 = 98.8%).
CTX_TEXT_TOK_PER_ASSISTANT_SEC = 5.7
MEASURED_INSTRUCTION_TOKENS = 2500
MEASURED_SEC_PER_TURN = 12.7          # 비버 발화 1턴 길이
TOOL_SECOND_RESPONSE_CACHE_RATE = 0.988


@dataclasses.dataclass
class CallShape:
    """통화 1건의 형태. 실측 구조 기반."""
    duration_s: float = 900.0            # 15분
    assistant_share: float = 0.45        # 비버 발화 비율
    user_share: float = 0.25             # 학습자 발화 비율
    instruction_tokens: int = MEASURED_INSTRUCTION_TOKENS
    user_transcript_tok_per_s: float = 5.0   # 입력 전사를 켜면 붙는 분 (통화후 분석에 필요)
    tool_every_turn: bool = True             # set_face

    @property
    def assistant_speech_s(self) -> float:
        return self.duration_s * self.assistant_share

    @property
    def user_speech_s(self) -> float:
        return self.duration_s * self.user_share

    @property
    def turns(self) -> int:
        return max(1, round(self.assistant_speech_s / MEASURED_SEC_PER_TURN))


def model_cost(p: Price, s: CallShape, *, caching: bool = True) -> dict:
    """실측 구조로 15분 통화 1건을 청구해 본다.

    누적 토큰은 생성 턴에서 1회 uncached, 그 뒤 남은 턴 동안 cached 로 재청구된다.
    선형 누적이면 평균 재청구 횟수 = 턴수/2.
    """
    a_rate = p.audio_cached if caching else p.audio_in
    t_rate = p.text_cached if caching else p.text_in
    n = s.turns
    reuse = n / 2.0
    full_reuse = n - 1

    # --- 입력 오디오: 학습자 발화만 (실측)
    user_audio_tok = AUDIO_IN_TOKENS_PER_SEC * s.user_speech_s
    in_audio = user_audio_tok * (p.audio_in + reuse * a_rate) / 1e6

    # --- 입력 텍스트: 지시문(전 턴 재청구) + 누적 전사
    instr = s.instruction_tokens * (p.text_in + full_reuse * t_rate) / 1e6
    assistant_tr_tok = CTX_TEXT_TOK_PER_ASSISTANT_SEC * s.assistant_speech_s
    user_tr_tok = s.user_transcript_tok_per_s * s.user_speech_s
    transcripts = (assistant_tr_tok + user_tr_tok) * (p.text_in + reuse * t_rate) / 1e6

    # --- 출력
    out_audio = AUDIO_OUT_TOKENS_PER_SEC * s.assistant_speech_s * p.audio_out / 1e6
    out_text = assistant_tr_tok * p.text_out / 1e6

    # --- 툴콜: 턴마다 Response 가 하나 더 생기고 프롬프트 전체를 다시 청구한다
    tool = 0.0
    if s.tool_every_turn:
        hit = TOOL_SECOND_RESPONSE_CACHE_RATE if caching else 0.0
        avg_audio_ctx = user_audio_tok / 2.0
        avg_text_ctx = s.instruction_tokens + (assistant_tr_tok + user_tr_tok) / 2.0
        tool = n * (
            avg_audio_ctx * ((1 - hit) * p.audio_in + hit * a_rate)
            + avg_text_ctx * ((1 - hit) * p.text_in + hit * t_rate)
        ) / 1e6

    parts = {
        "in_audio": in_audio,
        "in_text_instructions": instr,
        "in_text_transcripts": transcripts,
        "out_audio": out_audio,
        "out_text": out_text,
        "tool_2nd_response": tool,
    }
    total = sum(parts.values())
    return {
        "model": p.name,
        "caching": caching,
        "turns": n,
        "parts_usd": parts,
        "total_usd": total,
        "usd_per_min": total / (s.duration_s / 60.0),
    }


def crosscheck_from_gemini_measurement() -> None:
    """가정(턴수)에 기대지 않는 두 번째 경로.

    Gemini 실측 in_audio 원가를 역산해 «실제로 재청구된 오디오 초» 를 구하고,
    같은 대화 형태를 OpenAI 단가로 다시 청구해 본다.

    Gemini 오디오 토큰율 32 tok/s: https://ai.google.dev/gemini-api/docs/tokens
    """
    gemini_in_audio_usd = 0.5917   # call 1604 실측 (상위 에이전트가 운영 DB 에서)
    gemini_audio_price = 3.00      # USD / 1M
    gemini_tok_per_s = 32.0

    tokens = gemini_in_audio_usd / gemini_audio_price * 1e6
    billed_audio_s = tokens / gemini_tok_per_s

    s = CallShape()
    speech_s = s.assistant_speech_s + s.user_speech_s   # 컨텍스트에 들어가는 실발화

    print("=== 교차검증: Gemini 실측 역산 -> OpenAI 단가 재청구")
    print(f"  Gemini in_audio ${gemini_in_audio_usd:.4f} / ${gemini_audio_price}/1M")
    print(f"   -> in_audio 토큰 {tokens:,.0f} -> 청구된 오디오 {billed_audio_s:,.0f}초")
    print(f"   -> 실발화 {speech_s:.0f}초 대비 재청구 배수 {billed_audio_s/speech_s:.1f}x")
    print(f"      (통화길이 {s.duration_s:.0f}초 대비 {billed_audio_s/s.duration_s:.1f}x)")
    print("      ※ Gemini 는 sliding window 압축이 오래된 오디오를 밀어내 배수가 깎인 값이다.")
    print("         OpenAI mini 는 128k 안에 다 들어가 압축이 없으므로 배수는 이보다 크다.")

    p = PRICES["mini"]
    fresh_tok = AUDIO_IN_TOKENS_PER_SEC * speech_s
    reused_tok = AUDIO_IN_TOKENS_PER_SEC * (billed_audio_s - speech_s)
    usd = (fresh_tok * p.audio_in + reused_tok * p.audio_cached) / 1e6
    print(f"  같은 대화를 OpenAI mini 로: fresh {fresh_tok:,.0f} tok @ ${p.audio_in} + "
          f"reused {reused_tok:,.0f} tok @ ${p.audio_cached}")
    print(f"   -> in_audio = ${usd:.4f}   (가정기반 모델값 "
          f"${model_cost(p, s)['parts_usd']['in_audio']:.4f} 와 비교)")
    print(f"   -> 같은 토큰을 캐시 없이 받았다면 "
          f"${(fresh_tok + reused_tok) * p.audio_in / 1e6:.4f}")
    print()


def run_model_mode() -> None:
    lo, hi = GEMINI_MEASURED_15MIN_USD
    print("=== 15분 통화 1건 환산 — 실측 구조 기반 (gpt-realtime-2.1-mini)")
    print(f"    지시문 {MEASURED_INSTRUCTION_TOKENS} tok · 비버 1턴 {MEASURED_SEC_PER_TURN}초 ·"
          f" 비버발화 1초당 컨텍스트 텍스트 {CTX_TEXT_TOK_PER_ASSISTANT_SEC} tok · set_face 매 턴")
    print()

    results = {}
    for share, label in ((0.45, "비버 45% (405초)"), (0.65, "비버 65% (585초)")):
        s = CallShape(assistant_share=share, user_share=0.25 if share == 0.45 else 0.20)
        on = model_cost(PRICES["mini"], s, caching=True)
        off = model_cost(PRICES["mini"], s, caching=False)
        results[share] = on
        print(f"-- {label}  학습자 {s.user_speech_s:.0f}초 / {on['turns']}턴")
        for k, v in on["parts_usd"].items():
            print(f"     {k:<22} ${v:.4f}  ({v/on['total_usd']*100:4.1f}%)")
        print(f"     {'합계':<22} ${on['total_usd']:.4f}   (${on['usd_per_min']:.4f}/분)")
        print(f"     캐시 OFF 였다면      ${off['total_usd']:.4f}  "
              f"-> 캐시가 {(1-on['total_usd']/off['total_usd'])*100:.0f}% 를 깎는다")
        print(f"     목표 $0.40: {'달성' if on['total_usd'] <= 0.40 else '초과'}  / "
              f"Gemini 대비 {lo/on['total_usd']:.1f}~{hi/on['total_usd']:.1f}배 저렴")
        print()

    print("=== full(gpt-realtime-2.1) 비교 — 비버 45%")
    s45 = CallShape(assistant_share=0.45, user_share=0.25)
    f_on = model_cost(PRICES["full"], s45, caching=True)
    print(f"  합계 ${f_on['total_usd']:.4f}  (${f_on['usd_per_min']:.4f}/분) "
          f"-> Gemini(${lo:.2f}~${hi:.2f}) 동급, 원가 해법 아님")

    print()
    print("=== 민감도 (mini, 캐시 ON, 비버 45% 기준)")
    for label, kw in [
        ("set_face 끄면", dict(tool_every_turn=False)),
        ("입력 전사 끄면", dict(user_transcript_tok_per_s=0.0)),
        ("지시문 2배 (5k tok)", dict(instruction_tokens=5000)),
        ("비버가 100% 말함", dict(assistant_share=1.0, user_share=0.0)),
    ]:
        r = model_cost(PRICES["mini"], dataclasses.replace(s45, **kw), caching=True)
        print(f"  {label:<24} ${r['total_usd']:.4f}")


# ---------------------------------------------------------------- live 모드 (키 필요)

# ---------------------------------------------------------------- live 모드 (키 필요)
#
# 키는 env 로만 받는다. 호출측에서 OPENAI_API_KEY="$GPT_API_KEY" 로 매핑한다.
# ⛔ 키 값을 찍거나 파일에 쓰지 않는다.

LIVE_MODEL = "gpt-realtime-2.1-mini"
LIVE_URL = "wss://api.openai.com/v1/realtime?model=" + LIVE_MODEL

SET_FACE_TOOL = {
    "type": "function",
    "name": "set_face",
    "description": "비버의 표정을 바꾼다. 말을 시작할 때마다 반드시 먼저 호출한다.",
    "parameters": {
        "type": "object",
        "properties": {
            "face": {
                "type": "string",
                "enum": ["neutral", "smile", "surprised", "sad", "angry"],
                "description": "표정",
            }
        },
        "required": ["face"],
        "additionalProperties": False,
    },
}

# 학습자(영어 모국어) 발화 대본. 코드스위칭 관찰용으로 영어/한국어를 섞어 던진다.
LEARNER_LINES = [
    "Hi! I'm ready for today's lesson.",
    "Sorry, I didn't catch that. Can you explain in English?",
    "안녕하세요. 저는 마이크예요.",
    "How do I say 'I want to eat' in Korean?",
    "저는 밥을 먹고 싶어요?",
    "What's the difference between 은 and 는?",
    "오늘 날씨가 좋아요.",
    "Can you ask me a question in Korean?",
    "네, 저는 학생이에요.",
    "I don't understand. What does that mean?",
    "주말에 친구를 만났어요.",
    "Thanks! That was helpful.",
]


def _usage_row(turn: int, label: str, usage: dict) -> dict:
    itd = usage.get("input_token_details", {}) or {}
    ctd = itd.get("cached_tokens_details", {}) or {}
    otd = usage.get("output_token_details", {}) or {}
    return {
        "turn": turn,
        "label": label,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "in_text": itd.get("text_tokens", 0),
        "in_audio": itd.get("audio_tokens", 0),
        "cached_total": itd.get("cached_tokens", 0),
        "cached_text": ctd.get("text_tokens", 0),
        "cached_audio": ctd.get("audio_tokens", 0),
        "out_text": otd.get("text_tokens", 0),
        "out_audio": otd.get("audio_tokens", 0),
    }


async def _live_session(key: str, instructions: str, *, rate: int, turns: int,
                        collect_audio: bool, seed_audio: bytes | None,
                        user_audio_slices: list[bytes] | None = None,
                        use_tools: bool = True, pace_s: float = 0.0) -> dict:
    """WS 세션 1개를 열어 턴을 돌리고 턴별 usage 를 모은다."""
    import base64
    import websockets

    rows: list[dict] = []
    transcripts: list[str] = []
    audio_out = bytearray()
    errors: list[str] = []

    async with websockets.connect(
        LIVE_URL,
        additional_headers={"Authorization": f"Bearer {key}"},
        max_size=None,
        open_timeout=30,
    ) as ws:

        async def send(obj: dict) -> None:
            await ws.send(json.dumps(obj))

        await send({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": instructions,
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": rate},
                        "turn_detection": None,          # 수동 commit (대본 구동)
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": rate},
                        "voice": "marin",
                    },
                },
                "tools": [SET_FACE_TOOL] if use_tools else [],
                "tool_choice": "auto" if use_tools else "none",
                "max_output_tokens": 600,
            },
        })

        async def pump_until_done(turn: int, label: str) -> dict | None:
            """response.done 하나를 받을 때까지 읽는다. 툴콜이면 결과를 돌려준다."""
            pending_calls: list[dict] = []
            cur_tr: list[str] = []
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
                ev = json.loads(raw)
                t = ev.get("type", "")
                if t == "error":
                    errors.append(json.dumps(ev.get("error", ev), ensure_ascii=False)[:400])
                    return None
                if t in ("response.output_audio.delta", "response.audio.delta"):
                    if collect_audio and ev.get("delta"):
                        audio_out.extend(base64.b64decode(ev["delta"]))
                elif t in ("response.output_audio_transcript.delta",
                           "response.audio_transcript.delta"):
                    cur_tr.append(ev.get("delta", ""))
                elif t == "response.function_call_arguments.done":
                    pending_calls.append({
                        "call_id": ev.get("call_id"),
                        "name": ev.get("name"),
                        "arguments": ev.get("arguments", "{}"),
                    })
                elif t == "response.done":
                    resp = ev.get("response", {}) or {}
                    usage = resp.get("usage", {}) or {}
                    status = resp.get("status")
                    if status != "completed":
                        errors.append(
                            f"turn {turn} {label}: status={status} "
                            + json.dumps(resp.get("status_details"), ensure_ascii=False)[:300]
                        )
                    if cur_tr:
                        transcripts.append("".join(cur_tr).strip())
                    for item in resp.get("output", []) or []:
                        if item.get("type") == "function_call" and not any(
                            c["call_id"] == item.get("call_id") for c in pending_calls
                        ):
                            pending_calls.append({
                                "call_id": item.get("call_id"),
                                "name": item.get("name"),
                                "arguments": item.get("arguments", "{}"),
                            })
                    row = _usage_row(turn, label, usage)
                    row["tool_calls"] = len(pending_calls)
                    rows.append(row)
                    return {"calls": pending_calls}

        for i in range(turns):
            if pace_s and i:
                await asyncio.sleep(pace_s)   # TPM 40k/min 회피
            chunk = None
            if seed_audio is not None:
                chunk = seed_audio
            elif user_audio_slices:
                chunk = user_audio_slices[i % len(user_audio_slices)]
            if chunk is not None:
                await send({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                })
                await send({"type": "input_audio_buffer.commit"})
            else:
                await send({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text",
                                     "text": LEARNER_LINES[i % len(LEARNER_LINES)]}],
                    },
                })
            await send({"type": "response.create"})

            res = await pump_until_done(i + 1, "main")
            if res is None:
                break

            # 툴콜이 있었으면 결과를 돌려주고 response.create 를 한 번 더 -> 2번째 청구 측정
            if res["calls"]:
                for c in res["calls"]:
                    await send({
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": c["call_id"],
                            "output": json.dumps({"ok": True}),
                        },
                    })
                await send({"type": "response.create"})
                if await pump_until_done(i + 1, "after_tool") is None:
                    break

    return {
        "rows": rows,
        "transcripts": transcripts,
        "audio": bytes(audio_out),
        "errors": errors,
        "rate": rate,
    }


def _report_live(res: dict, out_dir: str) -> dict:
    rows = res["rows"]
    if not rows:
        print("usage 를 한 건도 못 받았다.", file=sys.stderr)
        for e in res["errors"]:
            print("  error:", e, file=sys.stderr)
        return {}

    print(f"=== 실측: {LIVE_MODEL} / rate {res['rate']} / Response {len(rows)}건")
    hdr = (f"{'#':>3} {'label':<11} {'in_tok':>7} {'in_txt':>7} {'in_aud':>7} "
           f"{'c_tot':>7} {'c_txt':>7} {'c_aud':>7} {'o_aud':>6} {'tool':>4}")
    print(hdr)
    for r in rows:
        print(f"{r['turn']:>3} {r['label']:<11} {r['input_tokens']:>7} {r['in_text']:>7} "
              f"{r['in_audio']:>7} {r['cached_total']:>7} {r['cached_text']:>7} "
              f"{r['cached_audio']:>7} {r['out_audio']:>6} {r['tool_calls']:>4}")

    tot_in_audio = sum(r["in_audio"] for r in rows)
    tot_c_audio = sum(r["cached_audio"] for r in rows)
    tot_in_text = sum(r["in_text"] for r in rows)
    tot_c_text = sum(r["cached_text"] for r in rows)
    tot_in = sum(r["input_tokens"] for r in rows)
    tot_c = sum(r["cached_total"] for r in rows)
    tot_o_audio = sum(r["out_audio"] for r in rows)
    tot_o_text = sum(r["out_text"] for r in rows)

    def pct(a, b):
        return (a / b * 100.0) if b else float("nan")

    print()
    print("=== 캐시 적중률 (실측)")
    print(f"  오디오 입력  {tot_c_audio:,}/{tot_in_audio:,} = {pct(tot_c_audio, tot_in_audio):.1f}%")
    print(f"  텍스트 입력  {tot_c_text:,}/{tot_in_text:,} = {pct(tot_c_text, tot_in_text):.1f}%")
    print(f"  전체 입력    {tot_c:,}/{tot_in:,} = {pct(tot_c, tot_in):.1f}%")

    p = PRICES["mini"]
    measured_usd = (
        (tot_in_audio - tot_c_audio) * p.audio_in
        + tot_c_audio * p.audio_cached
        + (tot_in_text - tot_c_text) * p.text_in
        + tot_c_text * p.text_cached
        + tot_o_audio * p.audio_out
        + tot_o_text * p.text_out
    ) / 1e6
    print(f"  이 세션 실비: ${measured_usd:.4f}  (출력 오디오 {tot_o_audio/20:.0f}초)")

    tools = sum(r["tool_calls"] for r in rows if r["label"] == "main")
    after = [r for r in rows if r["label"] == "after_tool"]
    print()
    print("=== 툴콜 과금")
    print(f"  main Response 의 툴콜 {tools}건 / after_tool Response {len(after)}건")
    if after:
        a = after[0]
        print(f"  after_tool 1건 예: input {a['input_tokens']:,} 중 캐시 {a['cached_total']:,} "
              f"({pct(a['cached_total'], a['input_tokens']):.1f}%)")

    if res["transcripts"]:
        tr_path = os.path.join(out_dir, "transcripts.txt")
        with open(tr_path, "w", encoding="utf-8") as fh:
            for i, t in enumerate(res["transcripts"], 1):
                fh.write(f"[{i}] {t}\n\n")
        print(f"\n전사 저장: {tr_path}")
    if res["audio"]:
        import wave
        wav_path = os.path.join(out_dir, f"beaver_{res['rate']}.wav")
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(res["rate"])
            wf.writeframes(res["audio"])
        print(f"음성 저장: {wav_path}  ({len(res['audio'])/2/res['rate']:.1f}초)")

    return {
        "rows": rows,
        "audio_hit_rate": pct(tot_c_audio, tot_in_audio),
        "text_hit_rate": pct(tot_c_text, tot_in_text),
        "total_hit_rate": pct(tot_c, tot_in),
        "session_usd": measured_usd,
        "out_audio_tokens": tot_o_audio,
    }


def run_live_mode(args: argparse.Namespace) -> int:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print('키가 없다. OPENAI_API_KEY="$GPT_API_KEY" 로 넘겨라.', file=sys.stderr)
        return 2
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("websockets 가 없다.", file=sys.stderr)
        return 2

    out_dir = args.out or "."
    os.makedirs(out_dir, exist_ok=True)

    src = args.instructions or "normalcall_prompt.txt"
    with open(src, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    instructions = "\n".join(lines[3:]).strip()   # 앞 3행은 덤프 머리글
    instructions += (
        "\n\n[표정] 말을 시작할 때마다 먼저 set_face 함수를 호출해 표정을 정해라."
    )
    print(f"지시문: {src} / {len(instructions):,}자")

    slices: list[bytes] | None = None
    if args.user_audio:
        import wave
        with wave.open(args.user_audio, "rb") as wf:
            src_rate = wf.getframerate()
            pcm = wf.readframes(wf.getnframes())
        if src_rate != args.rate:
            import audioop
            pcm, _ = audioop.ratecv(pcm, 2, 1, src_rate, args.rate, None)
        seg = int(args.rate * 2 * args.slice_s)
        slices = [pcm[o:o + seg] for o in range(0, len(pcm) - seg + 1, seg)]
        print(f"사용자 오디오: {args.user_audio} {src_rate}Hz -> {args.rate}Hz, "
              f"{len(slices)}조각 x {args.slice_s}초")

    res = asyncio.run(_live_session(
        key, instructions, rate=args.rate, turns=args.turns,
        collect_audio=True, seed_audio=None, user_audio_slices=slices,
        use_tools=not args.no_tools, pace_s=args.pace,
    ))
    summary = _report_live(res, out_dir)
    if res["errors"]:
        print("\n=== 에러")
        for e in res["errors"]:
            print(" ", e)

    # 16kHz 입력 수용 여부 probe: 위에서 받은 24k 음성을 16k 로 내려 실제로 보내 본다
    if args.probe16 and res.get("audio"):
        print("\n=== 16kHz 입력 수용 probe")
        import audioop
        pcm16k, _ = audioop.ratecv(res["audio"], 2, 1, res["rate"], 16000, None)
        clip = pcm16k[: 16000 * 2 * 4]     # 4초
        print(f"  24k->16k 리샘플 {len(res['audio'])} -> {len(pcm16k)} bytes, 4초 전송")
        try:
            r16 = asyncio.run(_live_session(
                key, instructions, rate=16000, turns=1,
                collect_audio=True, seed_audio=clip,
            ))
            if r16["rows"]:
                print("  -> 16kHz 수용됨. Response 생성 성공.")
                _report_live(r16, out_dir)
            else:
                print("  -> 16kHz 거부. 에러:")
                for e in r16["errors"]:
                    print("    ", e)
        except Exception as exc:                      # noqa: BLE001
            print(f"  -> 16kHz probe 실패: {type(exc).__name__}: {exc}")

    if summary:
        with open(os.path.join(out_dir, "live_usage.json"), "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, ensure_ascii=False)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["model", "live"], default="model")
    ap.add_argument("--json", action="store_true", help="모델 결과를 JSON 으로")
    ap.add_argument("--turns", type=int, default=12, help="live: 돌릴 턴 수")
    ap.add_argument("--rate", type=int, default=24000, help="live: 오디오 샘플레이트")
    ap.add_argument("--out", help="live: 산출물 디렉터리")
    ap.add_argument("--instructions", help="live: 지시문 파일 (기본 normalcall_prompt.txt)")
    ap.add_argument("--probe16", action="store_true",
                    help="live: 16kHz 입력 수용 여부를 실제로 보내 확인")
    ap.add_argument("--user-audio", help="live: 학습자 발화로 보낼 wav (조각내 턴마다 전송)")
    ap.add_argument("--slice-s", type=float, default=5.0, help="live: 조각 길이(초)")
    ap.add_argument("--no-tools", action="store_true", help="live: set_face 툴 비활성(TPM 절약)")
    ap.add_argument("--pace", type=float, default=0.0, help="live: 턴 사이 대기(초). TPM 회피")
    args = ap.parse_args()

    if args.mode == "live":
        return run_live_mode(args)

    if args.json:
        s = CallShape()
        out = {
            k: {
                "cached": model_cost(PRICES[k], s, caching=True),
                "uncached": model_cost(PRICES[k], s, caching=False),
            }
            for k in PRICES
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    run_model_mode()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
