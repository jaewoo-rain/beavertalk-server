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

from core.audio import trim_silence_edges

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


# 언어 마커 — 비버가 **타깃 언어로 말하는 부분**을 감싼다: "오늘은 __How are you?__ 를 배울까?"
# 따옴표를 안 쓰는 이유는 persona_prompt 가 이미 따옴표를 두 용도로 쓰기 때문이다(대사 인용 /
# 특정 표현만 타깃으로 들려주기). __ 는 자연 문장에 안 나오고 마크다운 굵게 문법이라 모델이 잘 지킨다.
MARKER = "__"


def split_by_language(text: str, base_lang: str, target_lang: str) -> list[tuple[str, str]]:
    """마커 경계로 잘라 [(문자열, 언어)] 로 만든다. 마커는 **여기서 사라진다**.

    폴백이 중요하다: 모델이 마커를 안 쓰면(또는 짝이 안 맞으면) 잘리지 않고 통째로
    base_lang 으로 나간다 — **마커 준수에 전부를 걸지 않는다.** 그 경우 문자 체계로
    고르는 판정을 얹을 수 있는데(후보가 둘뿐이라 판정이 아니라 고르기다), 지금은 그
    자리만 열어 두고 단순 폴백을 쓴다.
    """
    if not text:
        return []
    chunks = text.split(MARKER)
    if len(chunks) % 2 == 0:
        # 짝이 안 맞는다 = 모델이 규칙을 반만 지켰다. 자르지 않고 통째로 낸다(말이 사라지는
        # 것보다 낫다). 마커만 지운다.
        return [(text.replace(MARKER, "").strip(), base_lang)]
    out: list[tuple[str, str]] = []
    orphan = ""      # 아직 붙일 앞 구간이 없는 조각(첫 조각이 구두점일 때)
    for i, chunk in enumerate(chunks):
        piece = (orphan + chunk).strip()
        orphan = ""
        if not piece:
            continue
        if not _has_speech(piece):
            # ⛔ **구두점만 남은 조각을 단독 구간으로 만들지 않는다**(2026-08-08 실통화).
            #   "That's right! __맞아요__?" 를 쪼개면 마지막 조각이 "?" 하나가 되는데,
            #   그것만 TTS 에 보내면 문맥이 없어 **기호를 단어로 읽는다**("쿼스천 마크").
            #   앞 구간에 붙인다 — 앞이 없으면 다음 조각 앞에 붙인다.
            if out:
                prev_text, prev_lang = out[-1]
                # ⚠ 이러면 그 구두점은 **앞 구간의 언어로** 읽힌다. 구두점은 언어색이 옅어
                #   대개 무해하고, 단독으로 읽히는 것보다는 확실히 낫다.
                out[-1] = (f"{prev_text}{piece}", prev_lang)
            else:
                orphan = piece
            continue
        out.append((piece, target_lang if i % 2 else base_lang))
    if orphan and out:
        # 끝까지 붙일 곳을 못 찾은 조각(입력이 구두점으로 시작해 그 뒤가 전부 비었던 경우)
        out[0] = (f"{orphan}{out[0][0]}", out[0][1])
    return out


def _has_speech(text: str) -> bool:
    """소리 내어 읽을 **말**이 들어 있나 — 글자·숫자가 하나라도 있으면 발화다.

    ⚠ 길이로 자르면 안 된다. "네", "응", "Oh" 는 짧지만 **진짜 발화**다. 반대로 "?" 는
    길이가 같아도 발화가 아니다. 그래서 **문자 종류**로 가른다(isalnum 은 한글·라틴·숫자를
    모두 참으로 본다).
    """
    return any(ch.isalnum() for ch in text)


def strip_markers(text: str) -> str:
    """⭐ **마커를 지우는 유일한 지점.**

    비버 대사는 TTS 로만 가지 않는다:
      TTS      마커로 언어를 정하고 **지우고** 합성한다(split_by_language 가 한다)
      _history **남긴다** — LLM 이 자기 형식을 봐야 관행이 유지된다
      그 외    전사 저장·문장 추출·복습·화면 등 **전부 지운다**
    지금 캐스케이드는 DB 를 안 타지만 곧 탄다. 영속화를 붙이는 사람은 **이 함수를 쓰면 된다** —
    찾아 헤매지 않게 한 곳에 모아 둔다.
    """
    return (text or "").replace(MARKER, "")


async def speak_stream(beaver: Any, pcm_stream: AsyncIterator[bytes], text: str,
                       trim_tail: bool = False) -> int:
    """한 문장의 오디오를 송출한다. **이름표는 마지막 조각에** 붙는다. 보낸 바이트 수 반환.

    한 조각 앞서 보내는 이유: 마지막 조각이 어느 것인지는 다음 조각이 와 봐야 안다.

    ⭐ `trim_tail` — **마지막 조각의 뒤쪽 침묵**을 잘라낸다(2026-08-10). 어차피 그 조각은
      위 이유로 이미 손에 들고 있으므로 **지연이 0** 이다. 새로 버퍼를 만들지 않는다.
      ⚠ 한계: 꼬리 침묵이 마지막 조각보다 길면 그 앞 조각의 몫은 못 자른다. 더 자르려면
        조각을 더 붙들어야 하는데 그건 **첫소리를 늦추는 대가**라 하지 않는다.
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
        if trim_tail:
            pending = trim_silence_edges(pending, head=False)
        await beaver.send(pending, text)   # 이 문장을 끝까지 들었으면 이력에 남는다
        sent += len(pending)
    return sent
