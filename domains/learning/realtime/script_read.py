"""PM-DEC-877/878 — 회화학습 대본 읽기 시제품(바바 · 28차시 A2-T11-1 · 모국어 en · exp/clip-expr-872 전용).

요청서: 52 하네스 `_output/2026-10-14_통화흐름_공동설계/66_공유_대본읽기시제품요청.md`.

- 표현학습 클립 대리 세션(clip_expr.ClipExprSession)의 틀을 그대로 쓴다: 서버 VAD · 미리 판정 · 클립 내보내기 · 표정 · 로그 ·
  WS 알림 · 조각 이어가기 · 재생 끝 뒤부터 세는 무응답 시간.
- 다른 것: 비버 대사 전부 클립 · **Live 없음 · LLM 없음** · 판정은 규칙만(STT 문자열 ↔ 대본 줄 · 한글 자모 유사도).
- 받아쓰기: Google Speech-to-Text v1 recognize(ko-KR · 대본 줄을 phrase 힌트로) — 발음 챌린지와 같은 클라이언트(core.stt).
- 앱 메시지: script_line(학습자 차례 · 한글·로마자·영어·n/11) · script_result(n·result·attempt) · script_end.
⛔ 켜는 조건은 env SCRIPT_READ(캐릭터 이름) + 그 캐릭터 voice + locale en + 일반 통화(레벨테스트·숙제 아님). 빈 값이면 아무것도 안 만든다.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import time
import wave
from typing import Any, Optional

from domains.learning.realtime import clip_expr
from domains.learning.realtime.clip_expr import Clip, ClipExprSession, Item, jamo

logger = logging.getLogger(__name__)

ROOT = clip_expr.ROOT.parent / "clips_script"
FACE_BY_GROUP = {"sg_good": "happy", "sg_open": "happy", "sg_close_call": "happy", "sg_angry": "angry"}
PASS_SIM, CLOSE_SIM = 0.6, 0.4
READY_LEAD_S = 1.0      # PM-DEC-895 — 바바 말 끝나기 이만큼 전에 내 줄을 회색으로 미리 보인다
PARTIAL_RATIO = 0.7     # PM-DEC-912 — 들은 한글이 대본의 이만큼 못 되면 «앞 반쪽» 으로 보고 한 번 더 들어 본다
FAIL_MAX = 3           # PM-DEC-894 — 한 줄에 이만큼 틀리면 화낸 뒤 다음 비버 대사로(한 줄에 멈추지 않게)
_CLOSE_PCM = b"\x00" * 14400   # 마무리 클립 대신 무음 0.3초(24kHz·16bit) — 종료 절차(턴 시작·끝)만 밟는다

_KEEP = re.compile(r"[^0-9A-Za-z가-힣]")
_PARTICLES = ("은", "는", "이", "가", "을", "를", "도", "에", "요", "하고")


def _content_hit(line: str, heard: str) -> float:
    """대본 줄 내용어(조사 떼고 2음절 이상 어절의 앞 2음절)가 받아쓴 말에 든 비율."""
    h = _KEEP.sub("", heard or "")
    words = []
    for w in (line or "").split():
        w = _KEEP.sub("", w)
        for pa in _PARTICLES:
            if len(w) > len(pa) + 1 and w.endswith(pa):
                w = w[: -len(pa)]
                break
        if len(w) >= 2:
            words.append(w[:2])
    if not words:
        return 0.0
    return sum(1 for w in words if w in h) / len(words)


def _hangul_len(s: str) -> int:
    return len(re.findall(r"[가-힣]", s or ""))


def _is_first_part(line: str, heard: str) -> bool:
    """들은 말이 대본의 앞부분만(한글 70% 미만)이고 그 앞부분과 거의 같은가 — 두 문장 사이 쉼에서 끊긴 꼴(PM-DEC-912)."""
    hl, ll = _hangul_len(heard), _hangul_len(line)
    if not hl or hl >= PARTIAL_RATIO * ll:
        return False
    hj = jamo(heard)
    head = jamo("".join(re.findall(r"[가-힣]", line))[: hl + 2])
    return difflib.SequenceMatcher(None, head, hj).ratio() >= 0.75


def match(line: str, heard: str) -> tuple[str, float]:
    """(pass|close|fail|empty, 자모 유사도). 요청서 5절 — 느슨하게."""
    if not _KEEP.sub("", heard or ""):
        return "empty", 0.0
    if not re.search(r"[가-힣]", heard):
        return "fail", 0.0                       # 다른 언어로 말함
    sim = difflib.SequenceMatcher(None, jamo(line), jamo(heard)).ratio()
    if sim >= PASS_SIM or _content_hit(line, heard) >= 0.5:
        return "pass", sim
    if sim >= CLOSE_SIM:
        return "close", sim
    return "fail", sim


def enabled(settings: Any, *, call_type: Optional[str], voice: Optional[str], locale: Optional[str]) -> Optional[str]:
    want = {x.strip() for x in str(getattr(settings, "SCRIPT_READ", "") or "").split(",") if x.strip()}
    char = clip_expr.VOICE_BANK.get(voice or "")
    if not want or char not in want or call_type in ("level_test", "homework") or (locale or "").split("-")[0] != "en":
        return None
    if not (ROOT / f"{char}_en" / "manifest.json").exists():
        logger.warning("normalcall script: %s 대본 자산 없음 — 종전 통화", char)
        return None
    return char


_BANK: dict[str, tuple] = {}


def load_bank(char: str, lesson: str = "A2-T11-1") -> tuple[dict[str, Clip], list[Item], dict[int, dict]]:
    key = f"{char}_{lesson}"
    if key in _BANK:
        return _BANK[key]
    d = ROOT / f"{char}_en"
    clips: dict[str, Clip] = {}
    for c in json.loads((d / "manifest.json").read_text(encoding="utf-8")):
        with wave.open(str(d / f"{c['id']}.wav"), "rb") as w:
            assert w.getframerate() == 24000 and w.getsampwidth() == 2 and w.getnchannels() == 1, c["id"]
            face = c.get("face") or "neutral"
            if face == "neutral":
                face = FACE_BY_GROUP.get(c["id"].rsplit("_", 1)[0], "neutral")
            clips[c["id"]] = Clip(c["id"], c["text"], float(c["sec"]), w.readframes(w.getnframes()), face)
    lines = json.loads((ROOT / f"{lesson}.json").read_text(encoding="utf-8"))["lines"]
    items = [Item(x["n"], x["learner"], x.get("en") or "", False) for x in lines]
    _BANK[key] = (clips, items, {x["n"]: x for x in lines})
    return _BANK[key]


class ScriptReadSession(ClipExprSession):
    """대본 읽기 대리 세션 — 진행 틀만 다르다(요청서 3절)."""

    PERSIST = ("idx", "stage", "done_items", "_last_in_group", "_recent", "attempt", "empties", "angry_used", "angry_line")
    CLOSE_GROUP = "sg_close_call"

    def __init__(self, *a, stt: Optional[Any] = None, **kw) -> None:
        clips, items, lines = load_bank(kw["char"])
        super().__init__(*a, bank=(clips, items), **kw)
        self.lines = lines
        self._stt_fn = stt
        self.stage = "read"
        self.attempt = 1
        self.empties = 0
        self.angry_used = 0
        self.angry_line = False
        self.results: list[tuple[int, str, float]] = []
        self._band_tasks: set[asyncio.Task] = set()
        # PM-DEC-899 — 판정하는 동안(받아쓰기 ~1초) 들어온 마이크를 버리지 않는다(사용자 「마이크가 씹히는 느낌」 · call 2104 줄8 빈 판정 13회).
        self._held = bytearray()
        self._held_voiced = False
        self._last_pcm = b""
        self._min_voiced_s = 0.35       # 숨소리·잡음 한 토막이 «말 끝» 을 만들어 진짜 읽기를 놓치지 않게
        self._partial = False           # PM-DEC-912 — 방금 판정이 앞 반쪽이라 한 번 더 듣는 중
        self._line_after_turn = False
        # 두 문장 줄을 쉬어 가며 읽는다 — 말 끝을 길게(call 2040·2041 반쪽 판정)
        self._end_s = float(getattr(self._settings, "SCRIPT_READ_END_SILENCE_S", 1.2) or 1.2)

    # ── 진행 ─────────────────────────────────────────────────────────────── #
    def _ask_id(self) -> str:
        return f"sb_{self.item.seq}" if self.idx < len(self.items) else f"sb_{len(self.items)}"

    async def _wait(self, *, carry: bool = False, keep: bool = False) -> None:
        """반응 없이 같은 줄을 다시 기다린다(PM-DEC-894 — 무응답·빈 값·1회 틀림).

        PM-DEC-899 — 판정하는 동안 들어온 소리(_held)를 다시 흘려 넣는다. 학습자가 그새 이어 말했으면
        carry 일 때 방금 판정한 앞부분(말 끝 무음 뺌)을 붙여 한 덩어리로 다시 판정한다.
        """
        self._save()
        held, voiced = bytes(self._held), self._held_voiced
        self._held.clear()
        self._held_voiced = False
        self._reset_listen()
        if not voiced and not keep:
            return
        # keep(PM-DEC-912 앞 반쪽): 새 소리가 없어도 앞부분을 쥔다 — 말 끝 무음 뒤 같은 소리로 다시 판정된다
        if (carry or keep) and self._last_pcm:
            head = self._trim_tail(self._last_pcm)
            self._speech.extend(head)
            self._voiced_s = len(head) / clip_expr._MIC_BPS
        step = int(clip_expr._MIC_BPS * 0.1)
        for i in range(0, len(held), step):
            await super().send_audio(held[i:i + step])

    def _trim_tail(self, pcm: bytes) -> bytes:
        """말 끝 무음을 걷는다(100ms 단위 · 소리 문턱 아래)."""
        step = int(clip_expr._MIC_BPS * 0.1)
        end = len(pcm)
        while end > 0 and clip_expr._rms(pcm[max(0, end - step):end]) < self._vad_th:
            end -= step
        return pcm[:max(0, end)]

    async def send_audio(self, pcm16_16k: bytes) -> None:
        if (not self._listening and pcm16_16k and not self._closing and self.idx < len(self.items)
                and time.monotonic() >= self._play_until):
            # 판정 중 — 버리지 말고 쥐고 있다가 반응 없는 대기로 돌아가면 다시 흘려 넣는다(_wait)
            if len(self._held) < clip_expr._MIC_BPS * 12:
                self._held.extend(pcm16_16k)
                if clip_expr._rms(pcm16_16k) >= self._vad_th:
                    self._held_voiced = True
            return
        await super().send_audio(pcm16_16k)

    async def _handle(self, cmd: tuple) -> None:
        kind = cmd[0]
        if kind == "nudge":
            await self._wait()          # 무음 넛지에도 상황극 밖 말을 하지 않는다 — 자막을 둔 채 기다린다
            return
        if kind == "close":
            # 마무리 클립 없이 끝낸다 — 종료 절차는 비버 턴 하나(턴 시작·끝)를 기다리므로 무음 0.3초 턴을 낸다.
            self._closing = True
            await self._out.put(clip_expr.LiveEvent(kind="out_tr", text=" "))
            await self._out.put(clip_expr.LiveEvent(kind="audio", audio=_CLOSE_PCM))
            self._save()
            await self._out.put(clip_expr.LiveEvent(kind="turn_end"))
            return
        await super()._handle(cmd)

    def _judged_label(self) -> str:
        return f"줄{self.item.seq}·시도{self.attempt}" if self.idx < len(self.items) else "-"

    async def _card(self, msg: Optional[dict]) -> None:
        if self._notify is None or msg is None:
            return
        try:
            await self._notify(msg)
        except Exception as exc:  # noqa: BLE001 - 카드 알림 실패는 통화 무영향(소리로 진행)
            logger.warning("normalcall script: %s 알림 실패(무시): %r", msg.get("type"), exc)

    def _line_msg(self) -> Optional[dict]:
        if self.idx >= len(self.items):
            return None
        x = self.lines[self.item.seq]
        return {"type": "script_line", "n": x["n"], "total": len(self.items), "ko": x["learner"],
                "roman": x.get("roman") or "", "en": x.get("en") or "", "attempt": self.attempt}

    # ── 자막 띠(PM-DEC-893) — 클립이 앱에서 소리 나기 시작하는 시각에 맞춰 보낸다 ──────────── #
    _SAY_KIND = (("sg_close_call", "end"), ("sg_open", "open"), ("sg_angry", "angry"), ("sg_again", "again"),
                 ("sg_good", "good"), ("sg_close", "close"), ("sm_", "answer"))

    def _band_at(self, delay_s: float, msg: Optional[dict]) -> None:
        if msg is None or self._notify is None:
            return

        async def _later() -> None:
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            await self._card(msg)

        t = asyncio.create_task(_later())
        self._band_tasks.add(t)
        t.add_done_callback(self._band_tasks.discard)

    def _on_clip_start(self, cid: str, clip, delay_s: float) -> None:
        if cid.startswith("sb_"):
            n = int(cid[3:])
            x = self.lines.get(n) or {}
            self._band_at(delay_s, {"type": "script_beaver", "n": n, "total": len(self.items), "ko": x.get("beaver") or "",
                                    "gloss": x.get("beaver_en") or "", "roman": x.get("beaver_roman") or ""})
            # PM-DEC-895 — 말할 타이밍 색: 바바 말 끝나기 조금 전 내 줄 회색(script_line) → 끝나는 순간 빨강(script_turn).
            dur = len(clip.pcm) / clip_expr._OUT_BPS   # 재생 길이 — _play_until 과 같은 셈
            end = delay_s + dur
            self._band_at(max(delay_s + dur / 2, end - READY_LEAD_S), self._line_msg())
            self._band_at(end, self._turn_msg())
        elif cid.startswith("sp_"):
            self._band_at(delay_s, self._line_msg())
        else:
            kind = next((k for p, k in self._SAY_KIND if cid.startswith(p)), "say")
            self._band_at(delay_s, {"type": "script_say", "kind": kind, "text": clip.text, "sec": round(clip.sec, 2)})

    def _turn_msg(self) -> Optional[dict]:
        return None if self.idx >= len(self.items) else {"type": "script_turn", "n": self.item.seq}

    def _on_turn_audio_end(self, delay_s: float) -> None:
        if self._line_after_turn:
            self._line_after_turn = False
            # 화냄 클립 동안은 내 줄을 띄우지 않는다 — 끝나는 순간 다시 읽기 줄을 바로 빨강으로(요청서 §2)
            self._band_at(delay_s, self._line_msg())
            self._band_at(delay_s, self._turn_msg())

    async def _result_card(self, result: str) -> None:
        if self.idx < len(self.items):
            await self._card({"type": "script_result", "n": self.item.seq, "result": result, "attempt": self.attempt})

    async def _on_open(self) -> None:
        if self._finished_resume():
            return
        if self.idx >= len(self.items):
            await self._wait()
            return
        n = self.item.seq
        await self._turn([f"sb_{n}"], result="resume" if self._restored else "open")   # 내 줄은 sb 재생에 맞춰(_on_clip_start)

    async def _analyze(self, pcm: bytes) -> tuple[str, str]:
        """받아쓰기만(규칙 대조는 _on_answer 에서). 미리 판정(spec)도 이 함수를 쓴다."""
        import time
        hint = self.item.surface if self.idx < len(self.items) else ""
        t0 = time.monotonic()
        if self._stt_fn is None and getattr(self._settings, "SCRIPT_READ_STT_PROBE", False):
            # PM-DEC-898 — 긴 줄 지연 조사: 영어 대체 없이 한 번 더 받아쓰고 걸린 시간만 기록한다(판정에는 안 씀).
            t = asyncio.create_task(self._probe_stt(pcm, hint))
            self._band_tasks.add(t)
            t.add_done_callback(self._band_tasks.discard)
        try:
            if self._stt_fn is not None:
                heard = await self._stt_fn(pcm, hint)
            else:
                self.stt_s += len(pcm) / clip_expr._MIC_BPS
                # PM-DEC-898 — 영어 대체를 끈다: 한국어 대본 읽기라 필요 없고, 켜면 가끔 5초씩 걸렸다(call 2110 줄6 5033ms ↔ 끔 1429ms).
                heard, _lang = await clip_expr.recognize(pcm, [hint], alt_en=False)
        except Exception as exc:  # noqa: BLE001 - 받아쓰기 실패는 «빈 값» → 다시 읽기(화내지 않는다)
            logger.warning("normalcall script: STT 실패(빈 값으로): %r", exc)
            heard = ""
        self.judge_calls += 1
        self.judge_ms.append(int((time.monotonic() - t0) * 1000))
        self._last_stt = (len(pcm) / clip_expr._MIC_BPS, self.judge_ms[-1])
        self._last_pcm = pcm
        return heard, "-"

    async def _probe_stt(self, pcm: bytes, hint: str) -> None:
        import time
        t0 = time.monotonic()
        try:
            heard, _ = await clip_expr.recognize(pcm, [hint], alt_en=False)
        except Exception as exc:  # noqa: BLE001 - 조사용 — 실패는 기록만
            heard = f"ERR {exc!r}"[:40]
        logger.info("normalcall script stt조사: call_id=%s 말=%.1fs alt_en=False 걸림_ms=%d 들음=«%s»", self._cid(),
                    len(pcm) / clip_expr._MIC_BPS, int((time.monotonic() - t0) * 1000), heard[:40])

    async def _on_answer(self, heard: str, _r: str, t_end: float) -> None:
        if self.idx >= len(self.items):
            await self._wait()
            return
        n = self.item.seq
        r, sim = match(self.item.surface, heard)
        was_partial, self._partial = self._partial, False
        if r != "empty" and not was_partial and _is_first_part(self.item.surface, heard):
            # PM-DEC-912 — 두 문장 줄 사이 쉼에서 말 끝이 잡혀 앞 문장만 판정됐다(call 2121 · 「와 맛있겠네요」 pass 뒤
            #   다음 대사가 뒤 문장과 겹침). 판정을 미루고 앞부분을 쥔 채 더 듣는다 — 이어 말하면 합쳐서, 아니면
            #   말 끝 무음(1.2초) 뒤 같은 소리로 한 번 더 판정해 그대로 진행한다.
            logger.info("normalcall script partial: call_id=%s 줄=%d 들음=«%s» — 이어 말하나 더 듣는다", self._cid(), n, heard[:40])
            self._partial = True
            await self._wait(keep=True)
            return
        self.results.append((n, r, round(sim, 2)))
        import time
        audio_s, stt_ms = getattr(self, "_last_stt", (0.0, -1))
        logger.info("normalcall script judge: call_id=%s 조각=%s 줄=%d 시도=%d 판정=%s 자모=%.2f 내용어=%.2f 말=%.1fs stt_ms=%d "
                    "말끝→판정_ms=%d 대본=«%s» 들음=«%s»",
                    self._cid(), self._frag(), n, self.attempt, r, sim, _content_hit(self.item.surface, heard),
                    audio_s, stt_ms, int((time.monotonic() - t_end) * 1000), self.item.surface, heard[:60])
        if r == "empty":
            self.empties += 1
            await self._wait(carry=True)      # 실패로 치지 않는다 · 반응 없음 · 그새 이어 말했으면 붙여서 다시
            return
        if r in ("pass", "close"):
            await self._result_card(r)
            nxt = await self._next_line()
            if nxt:
                await self._turn(nxt, result=r, heard=heard, t_end=t_end)   # 반응 없이 다음 비버 대사
            else:
                await self._wait()            # 11번 끝 — 종료 요청이 들어가 있다
            return
        if self.attempt == 1:
            await self._result_card("fail")
            self.attempt += 1
            await self._card(self._line_msg())   # 같은 줄 다시 읽기(투명도 0.9) — 반응 없음 · 바로 말할 차례
            await self._card(self._turn_msg())
            await self._wait(carry=True)
            return
        self.angry_used += 1
        lead = self._pick("sg_angry")
        if self.attempt >= FAIL_MAX:
            await self._result_card("skip")
            await self._turn([lead] + await self._next_line(), result="skip", heard=heard, t_end=t_end)
            return
        await self._result_card("fail")
        self.attempt += 1
        self._line_after_turn = True          # 화냄 클립이 끝나면 같은 줄
        await self._turn([lead], result="fail", heard=heard, t_end=t_end)

    async def _next_line(self) -> list[str]:
        self.idx += 1
        self.done_items += 1
        self.attempt = 1
        self.empties = 0
        self.angry_line = False
        if self.idx >= len(self.items):
            self.stage = "done"
            await self._card({"type": "script_end", "total": len(self.items)})
            if self._request_close is not None:
                self._request_close()
            return []
        n = self.item.seq
        return [f"sb_{n}"]

    async def __aexit__(self, *exc) -> None:
        for t in list(self._band_tasks):
            t.cancel()
        await super().__aexit__(*exc)
        counts: dict[str, int] = {}
        for _, r, _ in self.results:
            counts[r] = counts.get(r, 0) + 1
        jm = sorted(self.judge_ms)
        logger.info("normalcall script 요약: call_id=%s 조각=%s 줄완료=%d/%d 판정=%s 화냄=%d stt_calls=%d stt_p50_ms=%s 결과=%s",
                    self._cid(), self._frag(), self.done_items, len(self.items), counts, self.angry_used, len(jm),
                    jm[len(jm) // 2] if jm else None, self.results)
