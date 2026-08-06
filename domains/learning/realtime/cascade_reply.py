"""캐스케이드 P1 — LLM 응답을 **문장 단위로** TTS 에 흘려 비버 턴으로 송출한다.

    사용자 최종 전사 → [LLM 스트리밍] → [문장 분할] → [TTS 스트리밍] → BeaverOutput(페이서·원장)

왜 문장 단위인가: 첫 소리까지의 지연이 캐스케이드의 약점이다(설계 §4 — 1.5~2.5초). 문장이
완성되는 즉시 합성해 흘리면 LLM 이 아직 뒷문장을 쓰는 동안 앞문장이 이미 들린다. 반대로 너무
잘게 쪼개면 TTS 요청 수가 늘고 운율이 끊긴다 — 그래서 최소 길이를 둔다.

⛔ 불변식은 BeaverOutput 이 지킨다(I1~I5). 여기서는 **문장 텍스트를 조각의 마지막에 붙이는
  것**만 지키면 된다: 원장 절단이 "그 문장을 끝까지 들었을 때만 이력에 남긴다"이므로, 문장
  이름표는 그 문장의 **마지막 오디오 조각**에 달려야 걸친 문장이 자동으로 버려진다.

⛔ 취소(barge-in)는 이 태스크를 **밖에서 취소**해 이뤄진다. 여기서 CancelledError 를 잡아
  삼키지 않는다 — 세션이 잡아 audio_cancel 을 낸다(설계 §5).
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# 문장 분할 규칙(설계 §1-4). 종결부호·줄바꿈에서 끊고, 너무 짧으면 붙이고, 너무 길면 자른다.
_SENTENCE_END = "?!.。！？…"
# ⚠ 설계 초안은 12자였는데 **한국어 회화에는 길다** — "안녕하세요 반가워요."(11자) 같은
# 정상 문장이 다음 문장과 묶여 첫 소리가 그만큼 늦어진다. 초보 학습자에게 하는 말은 원래
# 짧으므로 8자로 낮춘다(그보다 짧은 "네." 류만 뒤와 붙인다).
_MIN_SENTENCE_CHARS = 8
_MAX_SENTENCE_CHARS = 120


class SentenceBuffer:
    """스트리밍 텍스트를 문장으로 끊어 내보낸다(순수 — LLM/TTS 를 모른다)."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = ""

    def push(self, piece: str) -> list[str]:
        """조각을 넣고, 끊긴 문장들을 순서대로 돌려준다."""
        out: list[str] = []
        self._buf += piece or ""
        while True:
            cut = self._find_cut()
            if cut <= 0:
                break
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:].lstrip()
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> str:
        """남은 것(마지막 문장). 종결부호가 없어도 내보낸다 — 안 내보내면 말이 잘린다."""
        rest, self._buf = self._buf.strip(), ""
        return rest

    def _find_cut(self) -> int:
        for i, ch in enumerate(self._buf):
            if i + 1 < _MIN_SENTENCE_CHARS:
                continue
            if ch in _SENTENCE_END or ch == "\n":
                return i + 1
        if len(self._buf) >= _MAX_SENTENCE_CHARS:
            # 폭주 출력 방어 — 공백에서 끊어 운율이 덜 깨지게.
            space = self._buf.rfind(" ", 0, _MAX_SENTENCE_CHARS)
            return space + 1 if space >= _MIN_SENTENCE_CHARS else _MAX_SENTENCE_CHARS
        return 0


async def speak_stream(beaver: Any, pcm_stream: AsyncIterator[bytes], text: str) -> int:
    """한 문장의 오디오를 송출한다. **이름표는 마지막 조각에** 붙는다. 보낸 바이트 수 반환.

    한 조각 앞서 보내는 이유: 마지막 조각이 어느 것인지는 다음 조각이 와 봐야 안다.
    """
    sent = 0
    pending: bytes | None = None
    async for chunk in pcm_stream:
        if not chunk:
            continue
        if pending is not None:
            await beaver.send(pending, "")
            sent += len(pending)
        pending = chunk
    if pending is not None:
        await beaver.send(pending, text)   # 이 문장을 끝까지 들었으면 이력에 남는다
        sent += len(pending)
    return sent
