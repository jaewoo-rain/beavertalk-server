"""PM-DEC-1019 — 통화 중 「반응」 연출. 모델이 아니라 **서버가 판정 결과로** 발동한다.

계획서: 45 하네스 `_output/2026-10-22_계획_통화반응/01_계획.md` §3.

- 발동 순간 서버 → 앱 `{"type":"reaction","kind","duration_ms","id"}` 1개. 앱이 반응 영상+효과음을 그 길이만큼 재생한다.
- 반응 동안 비버 답은 나가지 않는다 — 클립 통화는 일꾼이 그만큼 기다렸다 클립을 내고, Live 통화는 답 보류(PM-DEC-993)를
  반응 끝까지 늘린다. 반응 중엔 마이크를 Live 로 안 보내고(효과음이 «학습자가 이어 말함» 으로 잡혀 답이 버려지지 않게),
  모델 set_face 표정 마커도 보내지 않는다.
- 빈도 상한은 통화(call_id) 하나에 판정기 하나로 센다 — 조각(이어하기)이 바뀌어도 이어 센다(react1 2654: 조각 2에서 상한이 0 부터 다시 셌다).
  프로세스 메모리라 다른 인스턴스로 간 조각은 새로 센다(시험 태그는 인스턴스 1~2개).
⛔ 끼어들기·들린 답 취소와 무관하다 — 이미 들리기 시작한 답 위에는 반응을 띄우지 않는다(발동 자리에서 거른다).
⛔ env LIVE_REACTION=1 일 때만. 꺼져 있으면 메시지·대기·마이크·표정 어디도 종전과 같다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

REACTION_ON = os.environ.get("LIVE_REACTION", "0") == "1"
TOTAL_CAP_S = float(os.environ.get("LIVE_REACTION_TOTAL_S", "40") or 40)   # 통화(조각)당 반응 시간 합 상한
MIN_GAP_S = float(os.environ.get("LIVE_REACTION_GAP_S", "6") or 6)        # 앞 반응 끝에서 다음 반응까지 최소 간격
WRONG_STREAK_MAX = 2     # 연속 오답 이만큼째부터는 웃지 않는다(놀림으로 읽힐 위험 · 계획서 §7)

# 종류 → (길이 ms, 연속일 때 짧은 판 ms 또는 None, 통화당 상한 또는 None). 계획서 §3 표.
SPEC: dict[str, tuple[int, Optional[int], Optional[int]]] = {
    "correct": (4000, 2000, None),   # 퀴즈·표현 연습 정답 — 연속 정답 2번째부터 짧은 판
    "wrong": (4000, None, 3),        # 오답 — 귀여운 웃음 · 통화당 3회
    "pron": (3000, None, 2),         # 발음(받아쓰기 유사도) 높음 — 통화당 2회
    "script": (4000, None, 1),       # 대본 읽기 완주
    "laugh": (3000, None, 2),        # 자유대화 웃긴 말(모델 set_face laugh) — 통화당 2회
}


class ReactionGate:
    """이 통화의 반응 빈도·겹침 판정. 시계는 이벤트 루프 시각(초)."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.total_ms = 0
        self.until = 0.0          # 이 시각까지 반응 재생 중(서버 추정)
        self.last_end = -1e9
        self.correct_streak = 0
        self.wrong_streak = 0
        self.seq = 0
        self.skipped: dict[str, int] = {}

    def active(self, now: float) -> bool:
        return now < self.until

    def _skip(self, kind: str, why: str) -> None:
        key = f"{kind}:{why}"
        self.skipped[key] = self.skipped.get(key, 0) + 1
        logger.info("normalcall 반응: 건너뜀 kind=%s 사유=%s", kind, why)

    def decide(self, kind: str, now: float) -> Optional[dict]:
        """발동하면 앱 메시지(dict), 아니면 None. 정답·오답 연속 수는 발동 여부와 무관하게 센다."""
        spec = SPEC.get(kind)
        if spec is None:
            return None
        ms, short_ms, cap = spec
        if kind == "correct":
            self.correct_streak += 1
            self.wrong_streak = 0
            if self.correct_streak >= 2 and short_ms:
                ms = short_ms
        elif kind == "wrong":
            self.wrong_streak += 1
            self.correct_streak = 0
            if self.wrong_streak >= WRONG_STREAK_MAX:
                self._skip(kind, "연속오답")
                return None
        if self.active(now):
            self._skip(kind, "반응중")
            return None
        if cap is not None and self.counts.get(kind, 0) >= cap:
            self._skip(kind, "상한")
            return None
        if kind != "script" and now - self.last_end < MIN_GAP_S:
            self._skip(kind, "간격")
            return None
        if self.total_ms + ms > TOTAL_CAP_S * 1000:
            self._skip(kind, "총시간")
            return None
        self.seq += 1
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.total_ms += ms
        self.until = now + ms / 1000.0
        self.last_end = self.until
        return {"type": "reaction", "kind": kind, "duration_ms": ms, "id": self.seq}

    def summary(self) -> str:
        return f"발동={self.counts} 합={self.total_ms}ms 건너뜀={self.skipped}"


_GATES: dict[Any, ReactionGate] = {}   # call_id → 판정기(조각을 넘어 상한을 잇는다)
_GATES_MAX = 300


def gate(state: Any) -> Optional[ReactionGate]:
    """켜져 있으면 이 통화의 판정기(처음 부를 때 만든다 · 같은 call_id 의 앞 조각 것을 잇는다). 꺼져 있으면 None."""
    if not REACTION_ON or state is None:
        return None
    g = getattr(state, "rx_gate", None)
    if g is None:
        cid = getattr(state, "call_id", None)
        g = _GATES.get(cid) if cid is not None else None
        if g is None:
            g = ReactionGate()
            if cid is not None:
                _GATES[cid] = g
                while len(_GATES) > _GATES_MAX:
                    _GATES.pop(next(iter(_GATES)))
        try:
            state.rx_gate = g
        except AttributeError:   # rx_gate 칸이 없는 상태 객체(시험용 가짜 등) — 끔과 같게
            return None
    return g


def active(state: Any) -> bool:
    """지금 반응 재생 중인가(마이크·표정·답 보류가 본다). 꺼져 있으면 늘 False."""
    g = getattr(state, "rx_gate", None) if REACTION_ON else None
    return g is not None and g.active(asyncio.get_running_loop().time())


async def fire(state: Any, kind: str, send: Any, *, where: str) -> int:
    """판정이 나면 반응을 보낸다. 보낸 길이(ms), 안 보냈으면 0. send = async (dict) -> None."""
    g = gate(state)
    if g is None or send is None:
        return 0
    msg = g.decide(kind, asyncio.get_running_loop().time())
    if msg is None:
        return 0
    try:
        await send(msg)
    except Exception as exc:  # noqa: BLE001 - 반응은 장식이다 — 못 보내면 기다리지도 않는다(통화 무영향)
        logger.warning("normalcall 반응: 전송 실패(무시) kind=%s: %r", kind, exc)
        g.until = 0.0
        return 0
    logger.info("normalcall 반응: 발동 kind=%s ms=%d id=%d 자리=%s call_id=%s", kind, msg["duration_ms"], msg["id"], where,
                getattr(state, "call_id", None))
    return int(msg["duration_ms"])


def ws_sender(client_ws: Any):
    """앱 WS 로 반응 dict 를 보내는 함수(클립 알림과 같은 길 — 프로토콜 스키마 밖 · 앱은 모르는 type 을 무시한다)."""
    async def _send(msg: dict) -> None:
        await client_ws.send_text(json.dumps(msg, ensure_ascii=False))
    return _send
