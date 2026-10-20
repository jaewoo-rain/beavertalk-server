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

import difflib
import json
import logging
import re
import wave
from typing import Any, Optional

from domains.learning.realtime import clip_expr
from domains.learning.realtime.clip_expr import Clip, ClipExprSession, Item, jamo

logger = logging.getLogger(__name__)

ROOT = clip_expr.ROOT.parent / "clips_script"
FACE_BY_GROUP = {"sg_good": "happy", "sg_open": "happy", "sg_close_call": "happy", "sg_angry": "angry"}
PASS_SIM, CLOSE_SIM = 0.6, 0.4
ANGRY_CAP = 5
EMPTY_MAX = 2          # 무음·빈 STT 를 이만큼 «다시» 로 받고 그다음엔 정답 들려주고 넘어간다

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

    # ── 진행 ─────────────────────────────────────────────────────────────── #
    def _ask_id(self) -> str:
        return f"sp_{self.item.seq}" if self.idx < len(self.items) else self._pick("sg_again")

    def _judged_label(self) -> str:
        return f"줄{self.item.seq}·시도{self.attempt}" if self.idx < len(self.items) else "-"

    async def _card(self, msg: dict) -> None:
        if self._notify is None:
            return
        try:
            await self._notify(msg)
        except Exception as exc:  # noqa: BLE001 - 카드 알림 실패는 통화 무영향(소리로 진행)
            logger.warning("normalcall script: %s 알림 실패(무시): %r", msg.get("type"), exc)

    async def _line_card(self) -> None:
        if self.idx >= len(self.items):
            return
        x = self.lines[self.item.seq]
        await self._card({"type": "script_line", "n": x["n"], "total": len(self.items), "ko": x["learner"],
                          "roman": x.get("roman") or "", "en": x.get("en") or "", "attempt": self.attempt})

    async def _result_card(self, result: str) -> None:
        if self.idx < len(self.items):
            await self._card({"type": "script_result", "n": self.item.seq, "result": result, "attempt": self.attempt})

    async def _on_open(self) -> None:
        if self.idx >= len(self.items):
            await self._turn([self._pick("sg_again")], result="resume")
            return
        n = self.item.seq
        await self._line_card()
        lead = [] if self._restored else [self._pick("sg_open")]
        await self._turn(lead + [f"sb_{n}", f"sp_{n}"], result="resume" if self._restored else "open")

    async def _analyze(self, pcm: bytes) -> tuple[str, str]:
        """받아쓰기만(규칙 대조는 _on_answer 에서). 미리 판정(spec)도 이 함수를 쓴다."""
        import time
        hint = self.item.surface if self.idx < len(self.items) else ""
        t0 = time.monotonic()
        try:
            if self._stt_fn is not None:
                heard = await self._stt_fn(pcm, hint)
            else:
                self.stt_s += len(pcm) / clip_expr._MIC_BPS
                heard, _lang = await clip_expr.recognize(pcm, [hint], alt_en=True)
        except Exception as exc:  # noqa: BLE001 - 받아쓰기 실패는 «빈 값» → 다시 읽기(화내지 않는다)
            logger.warning("normalcall script: STT 실패(빈 값으로): %r", exc)
            heard = ""
        self.judge_calls += 1
        self.judge_ms.append(int((time.monotonic() - t0) * 1000))
        return heard, "-"

    async def _on_answer(self, heard: str, _r: str, t_end: float) -> None:
        if self.idx >= len(self.items):
            await self._turn([self._pick("sg_again")], result="done", heard=heard, t_end=t_end)
            return
        n = self.item.seq
        r, sim = match(self.item.surface, heard)
        self.results.append((n, r, round(sim, 2)))
        logger.info("normalcall script judge: call_id=%s 조각=%s 줄=%d 시도=%d 판정=%s 자모=%.2f 내용어=%.2f 대본=«%s» 들음=«%s»",
                    self._cid(), self._frag(), n, self.attempt, r, sim, _content_hit(self.item.surface, heard),
                    self.item.surface, heard[:60])
        if r == "empty":
            self.empties += 1
            if self.empties > EMPTY_MAX:
                await self._result_card("skip")
                await self._turn([f"sm_{n}"] + await self._next_line(), result="skip", heard=heard, t_end=t_end)
                return
            await self._turn([self._pick("sg_again")], result="again", heard=heard, t_end=t_end)
            return
        if r in ("pass", "close"):
            await self._result_card(r)
            lead = self._pick("sg_good" if r == "pass" else "sg_close")
            await self._turn([lead] + await self._next_line(), result=r, heard=heard, t_end=t_end)
            return
        if self.attempt >= 2:
            await self._result_card("skip")
            await self._turn([f"sm_{n}"] + await self._next_line(), result="skip", heard=heard, t_end=t_end)
            return
        await self._result_card("fail")
        self.attempt += 1
        if not self.angry_line and self.angry_used < ANGRY_CAP:
            self.angry_line = True
            self.angry_used += 1
            lead = self._pick("sg_angry")
        else:
            lead = self._pick("sg_again")
        await self._line_card()
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
        await self._line_card()
        return [f"sb_{n}", f"sp_{n}"]

    async def __aexit__(self, *exc) -> None:
        await super().__aexit__(*exc)
        counts: dict[str, int] = {}
        for _, r, _ in self.results:
            counts[r] = counts.get(r, 0) + 1
        jm = sorted(self.judge_ms)
        logger.info("normalcall script 요약: call_id=%s 조각=%s 줄완료=%d/%d 판정=%s 화냄=%d stt_calls=%d stt_p50_ms=%s 결과=%s",
                    self._cid(), self._frag(), self.done_items, len(self.items), counts, self.angry_used, len(jm),
                    jm[len(jm) // 2] if jm else None, self.results)
