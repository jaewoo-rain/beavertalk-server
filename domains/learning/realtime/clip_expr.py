"""PM-DEC-872 — 표현학습 클립 주도 시제품(구조 B · 바바 · 1차시 L1-S01-1 · 모국어 en · exp/clip-expr-872 전용).

요청서: 52 하네스 `_output/2026-10-14_통화흐름_공동설계/63_공유_표현학습클립시제품요청.md`.

구조(대리 세션 — PerTurnSession 과 같은 자리):
  - 이 객체가 LiveSessionProtocol 을 흉내 낸다 → 2펌프·워처·저장·분석·종료는 **종전 그대로** 돈다.
  - 기본 통화에는 Live 세션이 없다. 학습자 말 끝은 마이크 프레임 RMS 로 이 객체가 잡는다(서버 VAD).
  - 말 끝 → 판정 사이드카 1회(3.5-flash-lite · thinking minimal · 음성 입력 → {"heard","result"} JSON · 받아쓰기와 판정을 한 번에) →
    진행 틀(요청서 3-1)이 클립 순서열을 고른다 → 클립 PCM 을 Live 오디오처럼 내보낸다(out_tr·audio·turn_end).
  - 예외 턴(요청서 4절)만 그 턴 하나짜리 Live 세션을 새로 연다(지시문 = 바바 한 줄 + 지금 항목 + 직전 2턴 + 안내).
  - 클립마다 서버 로그 「normalcall clipexpr clip:」 1줄 + WS {"type":"clip", …} 알림(앱은 모르는 type 을 무시한다).
⛔ 판정은 학습 진도 기록과 별개다 — 진도는 종전 추적기(전사 기반)가 그대로 적는다(요청서 3-3 · 운영 DB 범위 동일).
⛔ 켜는 조건은 env CLIP_EXPR(캐릭터 이름) + 표현학습 + 그 캐릭터 voice + locale en 일 때만. 빈 값이면 아무것도 안 만든다.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import math
import random
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from pydantic import BaseModel

from core.gemini_live import LiveEvent

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3] / "assets" / "clips_expr"
VOICE_BANK = {"Fenrir": "Baba", "Leda": "Bibi"}
PERSONA = {
    "Baba": "너는 «바바»다. 학습자의 한국어 연습을 돕는 비꼬기 좋아하는 친구 같은 튜터 — 짧고 건조하고 살짝 잘난 척한다. 욕설·비하 없이.",
}
_CHUNK = 9600            # 0.2초(24kHz·16bit·mono)
_MIC_BPS = 32000         # 업링크 16kHz·16bit·mono 바이트/초
EXC_CAP = 8              # 예외 턴 통화당 상한(요청서 4절)
LAUGH_P = 0.3
# 클립 묶음 → 표정(set_face). 매니페스트 face 칸이 neutral 이 아니면 그것이 먼저다. 앱 표정 이름(neutral·happy·surprised·sad·angry·laugh)만.
FACE_BY_GROUP = {"g_ok": "happy", "g_rep_ok": "happy", "g_open": "happy", "g_close": "happy", "g_quiz_start": "happy",
                 "g_laugh": "laugh", "g_wrong": "surprised", "g_rep_again": "surprised", "g_dunno": "sad"}


@dataclass(slots=True)
class Clip:
    id: str
    text: str
    sec: float
    pcm: bytes
    face: str = "neutral"


@dataclass(slots=True)
class Item:
    seq: int
    surface: str
    en: str
    blank: bool


class JudgeOut(BaseModel):
    heard: str
    result: str     # correct|close|wrong|dunno|other|native_lang


_BANK_CACHE: dict[str, tuple[dict[str, Clip], list[Item]]] = {}


def load_bank(char: str, lang: str = "en", lesson: str = "L1-S01-1") -> tuple[dict[str, Clip], list[Item]]:
    key = f"{char}_{lang}_{lesson}"
    if key in _BANK_CACHE:
        return _BANK_CACHE[key]
    d = ROOT / f"{char}_{lang}"
    clips: dict[str, Clip] = {}
    for c in json.loads((d / "manifest.json").read_text(encoding="utf-8")):
        with wave.open(str(d / f"{c['id']}.wav"), "rb") as w:
            assert w.getframerate() == 24000 and w.getsampwidth() == 2 and w.getnchannels() == 1, c["id"]
            face = c.get("face") or "neutral"
            if face == "neutral":
                face = FACE_BY_GROUP.get(c["id"].rsplit("_", 1)[0], "neutral")
            clips[c["id"]] = Clip(c["id"], c["text"], float(c["sec"]), w.readframes(w.getnframes()), face)
    data = json.loads((ROOT / f"{lesson}.json").read_text(encoding="utf-8"))
    items = [Item(x["seq"], x["surface"], x.get("en") or "", "◯" in x["surface"]) for x in data["items"]]
    _BANK_CACHE[key] = (clips, items)
    return clips, items


def enabled(settings: Any, *, call_type: Optional[str], voice: Optional[str], locale: Optional[str]) -> Optional[str]:
    """켤 캐릭터 이름(없으면 None). CLIP_EXPR 은 쉼표로 여러 캐릭터를 받는다."""
    want = {x.strip() for x in str(getattr(settings, "CLIP_EXPR", "") or "").split(",") if x.strip()}
    char = VOICE_BANK.get(voice or "")
    if not want or char not in want or call_type != "expression" or (locale or "").split("-")[0] != "en":
        return None
    if not (ROOT / f"{char}_en" / "manifest.json").exists():
        logger.warning("normalcall clipexpr: %s 클립 자산 없음 — 종전 통화", char)
        return None
    return char


def _rms(pcm: bytes) -> float:
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0
    s = sum(x * x for x in a[::4])
    return math.sqrt(s / max(1, len(a[::4]))) / 32768.0


def _wav16k(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


_STAGE_RULE = {
    "ask": "학습자가 영어 뜻 «{en}» 을 한국어로 말해야 한다. 정답 표현: «{surface}».",
    "hint": "힌트를 들은 뒤 다시 답했다. 정답 표현: «{surface}».",
    "rep": "학습자가 «{surface}» 를 따라 말해야 한다.",
    "native": "학습자가 현지인 표현 «{native}» 를 따라 말해야 한다.",
    "quiz": "퀴즈: 영어 뜻 «{en}» 을 한국어로 말해야 한다. 정답 표현: «{surface}».",
}
_JUDGE_PROMPT = """너는 한국어 말하기 연습 판정기다. 첨부 음성은 학습자 한 마디다.
1) heard: 들린 그대로 받아쓴다(한국어는 한글로, 영어는 영어로). 말이 없으면 빈 문자열.
2) result: 아래 중 하나.
   - correct: 목표와 뜻·형태가 맞다(공백·문장부호·억양 차이 무시{blank_rule}).
   - close: 거의 맞다(낱말 하나 틀림 · 조사·어미만 다름 · 존댓말 어미 차이).
   - wrong: 한국어로 답했지만 목표와 다르다.
   - dunno: 「몰라요」「모르겠어요」「I don't know」처럼 모른다고 했다.
   - other: 학습과 무관한 말이나 질문을 했다.
   - native_lang: 한국어 없이 영어로 길게 말했다(짧은 「I don't know」는 dunno).
상황: {rule}
JSON 하나만 낸다."""


class ClipExprSession:
    """클립 주도 표현학습 대리 세션(LiveSessionProtocol 흉내 + async CM)."""

    def __init__(self, factory: Callable, client: Any, settings: Any, factory_kwargs: dict, *, char: str,
                 state: Any, notify: Optional[Callable] = None, judge: Optional[Callable] = None,
                 request_close: Optional[Callable] = None, rng: Optional[random.Random] = None) -> None:
        self._factory = factory
        self._client = client
        self._settings = settings
        self._kwargs = dict(factory_kwargs)
        self._kwargs.pop("resume_handle", None)
        self.char = char
        self._state = state
        self._notify = notify
        self._judge_fn = judge or self._judge_llm
        self._request_close = request_close
        self._rng = rng or random.Random()
        self.clips, self.items = load_bank(char)
        self._out: asyncio.Queue = asyncio.Queue()
        self._cmd: asyncio.Queue = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._live: Any = None
        self._opened = False
        self._closing = False
        # 듣기(VAD)
        self._listening = False
        self._speech = bytearray()
        self._voiced_s = 0.0
        self._silence_s = 0.0
        self._idle_s = 0.0
        self._t_last_voiced = 0.0
        self._vad_th = float(getattr(settings, "LIVE_REGROUND_VOICE_RMS", 0.02) or 0.02)
        self._end_s = float(getattr(settings, "CLIP_EXPR_END_SILENCE_S", 0.8) or 0.8)
        self._dunno_s = float(getattr(settings, "CLIP_EXPR_DUNNO_S", 6.0) or 6.0)
        # 진행
        self.idx = 0
        self.stage = "ask"
        self.item_fails = 0
        self.ask_n = 0
        self.quiz_q: list[int] = []
        self.done_items = 0
        self.exc_used = 0
        self._last_in_group: dict[str, str] = {}
        self._recent: list[str] = []
        # 계기
        self.judge_calls = 0
        self.judge_in = 0
        self.judge_out = 0
        self.judge_ms: list[int] = []
        self.latency_ms: list[int] = []
        self.clip_count = 0
        self.turns = 0
        self._face_n = 0

    # ── 수명 ──────────────────────────────────────────────────────────────── #
    async def __aenter__(self) -> "ClipExprSession":
        self._worker = asyncio.create_task(self._run(), name="nc-clipexpr")
        logger.info("normalcall clipexpr 켬: call_id=%s 캐릭터=%s 항목 %d · 클립 %d", self._cid(), self.char,
                    len(self.items), len(self.clips))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(BaseException):
                await self._worker
        lat = sorted(self.latency_ms)
        jm = sorted(self.judge_ms)
        logger.info("normalcall clipexpr 요약: call_id=%s turns=%d clips=%d 항목완료=%d/%d judge_calls=%d judge_in=%d "
                    "judge_out=%d judge_p50_ms=%s latency_p50_ms=%s latency_max_ms=%s live_exc=%d",
                    self._cid(), self.turns, self.clip_count, self.done_items, len(self.items), self.judge_calls,
                    self.judge_in, self.judge_out, jm[len(jm) // 2] if jm else None, lat[len(lat) // 2] if lat else None,
                    lat[-1] if lat else None, self.exc_used)

    def _cid(self):
        return getattr(self._state, "call_id", None)

    # ── LiveSessionProtocol ─────────────────────────────────────────────── #
    async def events(self) -> AsyncIterator:
        while True:
            ev = await self._out.get()
            if ev is None:
                return
            yield ev

    async def send_audio(self, pcm16_16k: bytes) -> None:
        if not self._listening or not pcm16_16k:
            return
        dur = len(pcm16_16k) / _MIC_BPS
        loud = _rms(pcm16_16k) >= self._vad_th
        if loud:
            self._voiced_s += dur
            self._silence_s = 0.0
            self._t_last_voiced = time.monotonic()
            self._speech.extend(pcm16_16k)
            return
        if self._voiced_s >= 0.2:
            self._speech.extend(pcm16_16k)
            self._silence_s += dur
            if self._silence_s >= self._end_s or len(self._speech) >= _MIC_BPS * 12:
                self._listening = False
                self._cmd.put_nowait(("utt", bytes(self._speech), self._t_last_voiced))
            return
        # 아직 말 시작 전 — 짧은 잡음은 버리고 무응답 시간만 센다
        self._speech.clear()
        self._voiced_s = 0.0
        self._idle_s += dur
        if self._idle_s >= self._dunno_s:
            self._listening = False
            self._cmd.put_nowait(("silent", b"", time.monotonic()))

    async def send_text_turn(self, text: str) -> None:
        if not self._opened:
            self._opened = True
            self._cmd.put_nowait(("open",))
        elif text == getattr(self._state, "close_seed", None):
            self._cmd.put_nowait(("close",))
        else:
            self._cmd.put_nowait(("nudge",))      # 무음 넛지·재개 시드 — 지금 문제를 다시 낸다

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        return None                               # 쪽지·안내는 Live 지시용이다 — 클립 통화는 쓰지 않는다

    async def send_persona(self, text: str) -> None:
        return None

    async def send_tool_response(self, fn_id, fn_name, **kw) -> None:
        if self._live is not None:
            await self._live.send_tool_response(fn_id, fn_name, **kw)

    # ── 일꾼 ──────────────────────────────────────────────────────────────── #
    async def _run(self) -> None:
        while True:
            cmd = await self._cmd.get()
            try:
                await self._handle(cmd)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 한 턴 실패로 통화를 죽이지 않는다 — 지금 문제를 다시 낸다
                logger.warning("normalcall clipexpr: 턴 처리 실패 — 다시 출제: %r", exc)
                await self._turn([self._ask_id()], result="error")

    async def _handle(self, cmd: tuple) -> None:
        kind = cmd[0]
        if kind == "open":
            await self._turn([self._pick("g_open"), self._ask_id()], result="open")
            return
        if kind == "close":
            self._closing = True
            await self._turn([self._pick("g_close")], result="close")
            return
        if kind == "nudge":
            await self._turn([self._ask_id()], result="nudge")
            return
        pcm, t_end = cmd[1], cmd[2]
        if kind == "silent":
            heard, result = "", "dunno"
        else:
            heard, result = await self._judge(pcm)
        await self._out.put(LiveEvent(kind="in_tr", text=heard or "…", is_final=True))
        await self._advance(heard, result, t_end)

    # ── 진행 틀(요청서 3-1) ──────────────────────────────────────────────── #
    @property
    def item(self) -> Item:
        return self.items[self.idx]

    def _ask_id(self) -> str:
        if self.stage == "quiz" and self.quiz_q:
            return f"quiz_{self.quiz_q[0]}"
        return f"ask_{self.item.seq}_{1 + self.ask_n % 2}"

    def _has(self, cid: str) -> bool:
        return cid in self.clips

    def _pick(self, group: str) -> str:
        ids = sorted(c for c in self.clips if c.rsplit("_", 1)[0] == group)
        last = self._last_in_group.get(group)
        pool = [c for c in ids if c != last] or ids
        cid = self._rng.choice(pool)
        self._last_in_group[group] = cid
        return cid

    def _wrong(self) -> list[str]:
        seq = [self._pick("g_laugh")] if self._rng.random() < LAUGH_P else []
        return seq + [self._pick("g_wrong")]

    def _next_item_clips(self) -> list[str]:
        """지금 항목을 끝내고 다음 출제(또는 퀴즈·마무리) 클립."""
        self.done_items += 1
        finished = self.items[self.idx].seq
        if self.done_items % 3 == 0:
            self.quiz_q = [it.seq for it in self.items[self.idx - 2:self.idx + 1]]
        self.idx += 1
        self.item_fails = 0
        self.ask_n += 1
        if self.quiz_q:
            self.stage = "quiz"
            return [self._pick("g_quiz_start"), f"quiz_{self.quiz_q[0]}"]
        if self.idx >= len(self.items):
            return self._finish()
        self.stage = "ask"
        logger.debug("normalcall clipexpr: 항목 %d 끝", finished)
        return [self._ask_id()]

    def _finish(self) -> list[str]:
        self.stage = "done"
        if self._request_close is not None:
            self._request_close()
        return []

    def _after_quiz(self) -> list[str]:
        self.quiz_q.pop(0)
        if self.quiz_q:
            return [f"quiz_{self.quiz_q[0]}"]
        if self.idx >= len(self.items):
            return self._finish()
        self.stage = "ask"
        return [self._ask_id()]

    async def _advance(self, heard: str, r: str, t_end: float) -> None:
        st = self.stage
        if st == "quiz" and self.quiz_q:
            # 퀴즈는 정답 판정만(요청서 3-1 ⑤) — 마지막 퀴즈는 idx 가 이미 항목 끝을 넘었다
            lead = [self._pick("g_ok")] if r == "correct" else [f"model_{self.quiz_q[0]}"]
            await self._turn(lead + self._after_quiz(), result=r, heard=heard, t_end=t_end)
            return
        it = self.item if self.idx < len(self.items) else None
        if st == "done" or it is None:
            await self._turn([self._pick("g_back")], result=r, heard=heard, t_end=t_end)
            return
        seq = it.seq
        if r in ("other", "native_lang"):
            guide = ("한 마디만 캐릭터대로 받고, 지금 연습 중인 표현으로 돌아오자고 영어로 말한다." if r == "other"
                     else "영어로 짧게 받아 주고, 한국어로 해 보라고 한다.")
            tail = [self._ask_id()] if r == "other" else []
            await self._exception_or_back(guide, heard, tail, r, t_end)
            return
        if r == "correct":
            if st in ("ask", "hint") and it.blank:
                await self._exception_or_back("학습자가 말한 문장을 따옴표로 한 번 되짚고 칭찬 한 마디를 영어로 한다.",
                                              heard, self._next_item_clips(), r, t_end, fallback=[self._pick("g_ok")])
                return
            if st in ("ask", "hint", "rep", "rep2") and self._has(f"native_{seq}"):
                lead = self._pick("g_ok") if st in ("ask", "hint") else self._pick("g_rep_ok")
                self.stage = "native"
                await self._turn([lead, f"native_{seq}"], result=r, heard=heard, t_end=t_end)
                return
            lead = self._pick("g_ok") if st in ("ask", "hint") else self._pick("g_rep_ok")
            await self._turn([lead] + self._next_item_clips(), result=r, heard=heard, t_end=t_end)
            return
        # 실패(close·wrong·dunno)
        if st in ("native", "native2"):
            if st == "native":
                self.stage = "native2"
                await self._turn([self._pick("g_rep_again"), f"native_{seq}"], result=r, heard=heard, t_end=t_end)
            else:
                await self._turn([self._pick("g_move")] + self._next_item_clips(), result=r, heard=heard, t_end=t_end)
            return
        self.item_fails += 1
        if self.item_fails >= 3:
            await self._exception_or_back("쉽게 다시 설명하고 정답 표현을 한 번 들려준 뒤, 다음으로 넘어가자고 한다.",
                                          heard, self._next_item_clips(), r, t_end, fallback=[self._pick("g_move")])
            return
        if st == "ask" and r == "close" and self._has(f"hint_{seq}"):
            self.stage = "hint"
            await self._turn(self._wrong() + [f"hint_{seq}"], result=r, heard=heard, t_end=t_end)
            return
        if st in ("ask", "hint"):
            lead = [self._pick("g_dunno")] if r == "dunno" else self._wrong()
            self.stage = "rep"
            await self._turn(lead + [f"model_{seq}"], result=r, heard=heard, t_end=t_end)
            return
        if st == "rep":
            self.stage = "rep2"
            again = f"rep_{seq}" if self._has(f"rep_{seq}") else f"model_{seq}"
            await self._turn([self._pick("g_rep_again"), again], result=r, heard=heard, t_end=t_end)
            return
        await self._turn([self._pick("g_move")] + self._next_item_clips(), result=r, heard=heard, t_end=t_end)

    async def _exception_or_back(self, guide: str, heard: str, tail: list[str], r: str, t_end: float,
                                 fallback: Optional[list[str]] = None) -> None:
        if self.exc_used >= EXC_CAP:
            lead = fallback if fallback is not None else [self._pick("g_back")]
            await self._turn(lead + tail, result=r, heard=heard, t_end=t_end, note="예외상한")
            return
        self.exc_used += 1
        await self._turn(tail, result=r, heard=heard, t_end=t_end, live_guide=guide)

    # ── 판정 사이드카 ─────────────────────────────────────────────────────── #
    async def _judge(self, pcm: bytes) -> tuple[str, str]:
        it = self.items[min(self.idx, len(self.items) - 1)]
        st = self.stage
        seq_q = self.quiz_q[0] if (st == "quiz" and self.quiz_q) else it.seq
        q = next(x for x in self.items if x.seq == seq_q)
        native = ""
        if st in ("native", "native2") and self._has(f"native_{q.seq}"):
            native = self.clips[f"native_{q.seq}"].text.split('"')[1] if '"' in self.clips[f"native_{q.seq}"].text else ""
        rule = _STAGE_RULE["native" if st.startswith("native") else ("rep" if st.startswith("rep") else st)]
        rule = rule.format(en=q.en, surface=q.surface, native=native)
        blank_rule = (" · 빈칸 항목: 「저는 + 명사 + 이에요/예요」 꼴이면 명사 내용과 무관하게 correct" if q.blank else "")
        t0 = time.monotonic()
        heard, result, tin, tout = await self._judge_fn(_wav16k(pcm), _JUDGE_PROMPT.format(rule=rule, blank_rule=blank_rule))
        self.judge_calls += 1
        self.judge_in += int(tin or 0)
        self.judge_out += int(tout or 0)
        self.judge_ms.append(int((time.monotonic() - t0) * 1000))
        if result not in ("correct", "close", "wrong", "dunno", "other", "native_lang"):
            result = "wrong"
        if st in ("rep", "rep2", "native", "native2") and result == "close":
            result = "correct"                    # 따라 말하기는 근접이면 통과
        return heard, result

    async def _judge_llm(self, wav: bytes, prompt: str) -> tuple[str, str, int, int]:
        from google.genai import types
        model = getattr(self._settings, "CLIP_EXPR_JUDGE_MODEL", "") or "gemini-3.5-flash-lite"
        resp = await self._client.aio.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=[types.Part.from_bytes(data=wav, mime_type="audio/wav"),
                                                        types.Part.from_text(text=prompt)])],
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json",
                                               response_schema=JudgeOut,
                                               # 3.x 는 thinking_budget 0 을 거절한다(400) — 단계 이름으로 최소화
                                               thinking_config=(types.ThinkingConfig(thinking_level="minimal") if model.startswith("gemini-3")
                                                                else types.ThinkingConfig(thinking_budget=0)),
                                               automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
        )
        um = getattr(resp, "usage_metadata", None)
        try:
            out = JudgeOut.model_validate_json(resp.text or "{}")
        except Exception:  # noqa: BLE001 - 판정 불가는 오답으로(통화는 이어간다)
            out = JudgeOut(heard="", result="wrong")
        return out.heard, out.result, getattr(um, "prompt_token_count", 0) or 0, getattr(um, "candidates_token_count", 0) or 0

    # ── 내보내기 ─────────────────────────────────────────────────────────── #
    async def _turn(self, clip_ids: list[str], *, result: str, heard: str = "", t_end: Optional[float] = None,
                    live_guide: Optional[str] = None, note: str = "") -> None:
        """비버 턴 하나 = [예외 Live 응답] + 클립 순서열 + turn_end. 끝나면 다시 듣는다."""
        self.turns += 1
        first_sound: Optional[float] = None
        live_text = ""
        face_now = "neutral"
        if live_guide:
            t_live = time.monotonic()
            live_text = await self._live_turn(live_guide, heard)
            first_sound = getattr(self, "_live_first_sound", None)
            logger.info("normalcall clipexpr live: call_id=%s 항목=%s 판정=%s 안내=«%s» 응답=«%s» 걸림=%.2fs",
                        self._cid(), self.item.seq if self.idx < len(self.items) else "-", result, live_guide[:40],
                        live_text[:80], time.monotonic() - t_live)
        for cid in clip_ids:
            c = self.clips.get(cid)
            if c is None:
                logger.warning("normalcall clipexpr: 클립 없음 %s(건너뜀)", cid)
                continue
            if c.face != face_now:
                # 영상통화 표정 — Live 의 set_face 와 같은 길(서버가 sentence 마커로 앱에 보낸다). 응답은 필요 없다(auto_acked).
                face_now = c.face
                self._face_n += 1
                await self._out.put(LiveEvent(kind="tool_call", fn_name="set_face", fn_id=f"clip-face-{self._face_n}",
                                              fn_args={"emotion": c.face}, auto_acked=True))
            await self._out.put(LiveEvent(kind="out_tr", text=(" " if first_sound or live_text else "") + c.text))
            for i in range(0, len(c.pcm), _CHUNK):
                await self._out.put(LiveEvent(kind="audio", audio=c.pcm[i:i + _CHUNK]))
                if first_sound is None:
                    first_sound = time.monotonic()
            self.clip_count += 1
            t_rel = self._t_rel()
            logger.info("normalcall clipexpr clip: call_id=%s t=%.1f id=%s 항목=%s 단계=%s 판정=%s 들음=«%s» 초=%.2f 표정=%s",
                        self._cid(), t_rel, cid, self.item.seq if self.idx < len(self.items) else "-", self.stage,
                        result, heard[:60], c.sec, c.face)
            if self._notify is not None:
                with contextlib.suppress(Exception):
                    await self._notify({"type": "clip", "id": cid, "result": result, "stage": self.stage, "face": c.face,
                                       "t": round(t_rel, 2)})
        lat = None
        if t_end is not None and first_sound is not None:
            lat = int((first_sound - t_end) * 1000)
            self.latency_ms.append(lat)
        logger.info("normalcall clipexpr turn: call_id=%s 판정=%s 단계→%s 클립=%s 말끝→첫소리_ms=%s 예외=%s%s",
                    self._cid(), result, self.stage, ",".join(clip_ids) or "-", lat, "live" if live_guide else "-",
                    f" ({note})" if note else "")
        self._recent = (self._recent + ([f"학습자: {heard}"] if heard else []) +
                        [f"비버: {' '.join(self.clips[c].text for c in clip_ids if c in self.clips) or live_text}"])[-4:]
        await self._out.put(LiveEvent(kind="turn_end"))
        if self._closing:
            return
        self._reset_listen()

    def _t_rel(self) -> float:
        t0 = getattr(self._state, "call_start_ts", None)
        return (asyncio.get_running_loop().time() - t0) if t0 else 0.0

    def _reset_listen(self) -> None:
        self._speech.clear()
        self._voiced_s = self._silence_s = self._idle_s = 0.0
        self._listening = True

    async def _live_turn(self, guide: str, heard: str) -> str:
        """예외 턴 — 그 턴 하나짜리 Live 세션(지시문 = 바바 한 줄 + 지금 항목 + 직전 2턴 + 안내)."""
        self._live_first_sound = None
        it = self.items[min(self.idx, len(self.items) - 1)]
        si = (f"{PERSONA.get(self.char, '')}\n학습자는 영어 화자이고 한국어를 배운다. 지금 연습 중인 표현: «{it.surface}»(뜻: {it.en}).\n"
              f"직전 대화:\n" + "\n".join(self._recent[-2:]) + f"\n[이번 턴] {guide}\n"
              "1~2문장으로 짧게 말하고 멈춘다. 다른 표현을 새로 가르치거나 질문을 두 개 하지 않는다.")
        kw = {**self._kwargs, "system_instruction": si}
        text: list[str] = []
        try:
            async with self._factory(self._client, self._settings, **kw) as s:
                self._live = s
                await s.send_text_turn(f"학습자가 방금 말했다: «{heard or '(말 없음)'}»")
                async for ev in s.events():
                    if ev.kind == "turn_end":
                        break
                    if ev.kind == "out_tr" and ev.text:
                        text.append(ev.text)
                    if ev.kind == "audio" and self._live_first_sound is None:
                        self._live_first_sound = time.monotonic()
                    if ev.kind in ("out_tr", "audio", "tool_call", "usage"):
                        await self._out.put(ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 예외 턴 실패는 g_back 클립으로 대신한다
            logger.warning("normalcall clipexpr: 예외 턴 Live 실패 — g_back 으로 대신: %r", exc)
            back = self.clips[self._pick("g_back")]
            await self._out.put(LiveEvent(kind="out_tr", text=back.text))
            for i in range(0, len(back.pcm), _CHUNK):
                await self._out.put(LiveEvent(kind="audio", audio=back.pcm[i:i + _CHUNK]))
        finally:
            self._live = None
        return "".join(text)
