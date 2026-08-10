"""OpenAI Realtime 전사 어댑터 — **`SttV2Event` 4종으로 정규화한다.**

왜 붙이나(2026-08-10 실측, 같은 오디오·같은 실시간 경로):
    `안녕하세요. + <상대언어> + 돈까스가 좋아요.` 6개 언어쌍
      Google long+[ko,X]        영어만 ⚠, 나머지 5개 음차/증발
      Google chirp+[ko]         영어·프랑스어만 ✅
      ElevenLabs scribe v2 실시간 영어·중국어만 ✅
      **OpenAI Realtime          6/6 ✅**
    ⭐ 대조 실험: 가운데 구간만 떼어 언어를 지정하면 **구글도 6/6** 이다.
      ⇒ 오디오 문제가 아니라 **순수한 code-switching 문제**이고 지금은 OpenAI 만 푼다.
    원가도 $0.003/분 = 구글의 1/5.3.
    (근거 문서: docs/20260810_2210_STT-TTS-Live-벤더-선정기준.md §4-2)

⛔ **새 이벤트 타입을 만들지 않는다.** 세션(`cascade_session`)은 이 어댑터를 몰라야 한다 —
  SPEECH_BEGIN / SPEECH_END / TRANSCRIPT(text, is_final) / STREAM_ERROR 로만 말한다.

⚠ 16kHz 가 거절된다(`integer_below_min_value`). 우리 클라는 16k 를 보내므로 **24k 로 올려서**
  보낸다(`core.audio.upsample_16k_to_24k` — 2:3 정수비라 부동소수 리샘플러가 필요 없다).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from typing import Any, AsyncIterator

from core.audio import upsample_16k_to_24k
from core.config import settings
from core.stt import SPEECH_BEGIN, SPEECH_END, STREAM_ERROR, TRANSCRIPT, SttV2Event

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
# ⚠ `OpenAI-Beta: realtime=v1` 헤더는 **폐기됐다** — 붙이면 4000 으로 거절된다(2026-08-10 실측).
_SEND_RATE = 24_000          # 벤더가 받는 유일한 PCM 레이트(16000 은 거절된다)
_VENDOR_PREFIX = "openai-"


def is_configured() -> bool:
    """키가 있나. ⛔ 키 값은 로그·예외 어디에도 싣지 않는다."""
    return bool((settings.GPT_API_KEY or "").strip())


def vendor_name() -> str:
    """원가 벤더 문자열 = **실제로 돈 모델 ID**(단가가 모델마다 다르다)."""
    return _VENDOR_PREFIX + (settings.OPENAI_STT_MODEL or "gpt-4o-mini-transcribe").strip()


class OpenAiRealtimeSttStream:
    """WS 1개 = 통화 1건. 롤오버가 없다(수명 한계가 문서에 없다 — 있으면 재연결로 다룬다).

    인터페이스는 `RollingSttV2Stream` 과 같다: start / push_audio / events / close / usage.
    """

    def __init__(self, sample_rate: int, language_codes: list[str] | None = None) -> None:
        self._sample_rate = sample_rate
        # ⚠ OpenAI 는 `language` 를 **하나만** 받는다 — 다국어는 "안 넣기"가 유일한 방법이다
        #   (선정기준 §4-2). 그래서 코드가 여러 개면 아예 지정하지 않는다.
        self._language = (language_codes or [None])[0] if len(language_codes or []) == 1 else None
        self._ws: Any = None
        self._closed = False
        self._sent_ms = 0.0
        self._partial: dict[str, str] = {}   # item_id → 지금까지 받은 부분 전사
        self._q: asyncio.Queue[SttV2Event | None] = asyncio.Queue()
        self._reader: asyncio.Task | None = None
        self.vendor = vendor_name()          # cascade_usage 가 이 값을 우선한다

    # ── 수명 ──
    async def start(self) -> None:
        import websockets

        key = (settings.GPT_API_KEY or "").strip()
        if not key:
            raise RuntimeError("GPT_API_KEY 미설정")
        # ⛔ 키는 헤더로만 나간다. 예외 메시지·로그에 절대 넣지 않는다.
        self._ws = await websockets.connect(
            REALTIME_URL, additional_headers={"Authorization": f"Bearer {key}"},
            max_size=None,
        )
        transcription: dict[str, Any] = {
            "model": (settings.OPENAI_STT_MODEL or "gpt-4o-mini-transcribe").strip()
        }
        if self._language:
            transcription["language"] = self._language
        await self._ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": {
                    "format": {"type": "audio/pcm", "rate": _SEND_RATE},
                    "transcription": transcription,
                    "turn_detection": {"type": "server_vad"},
                }},
            },
        }))
        self._reader = asyncio.create_task(self._read_loop())
        logger.info("[stt-openai] 연결 — 모델=%s 언어=%s (%dHz 로 올려 보낸다)",
                    transcription["model"], self._language or "자동감지", _SEND_RATE)

    async def close(self) -> None:
        # ⭐ **닫기 전에 무음을 조금 흘린다** — 안 그러면 마지막 발화가 통째로 사라진다.
        #   실측(2026-08-10): 같은 오디오를 그냥 끊으면 전사가 2건("안녕하세요." / "Today I
        #   want to study Korean.")인데, **꼬리 무음 1.5초**를 붙이면 3건째 "돈가스가 좋아요."
        #   가 온다. server VAD 가 발화 끝을 못 봐서 마지막 구간을 커밋하지 않는 것이다.
        #   ⚠ 통화 중에는 마이크가 상시 열려 무음이 계속 흘러 문제가 안 된다 — **통화 끝**에서만
        #     생긴다. 그래서 여기서만 메운다(원가 영향 1초 미만).
        await self._flush_tail_silence()
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        await self._q.put(None)

    async def _flush_tail_silence(self) -> None:
        """마지막 발화를 커밋시키기 위한 무음 꼬리(R5 — 실패해도 통화 종료를 막지 않는다)."""
        tail_ms = max(0, settings.OPENAI_STT_TAIL_SILENCE_MS)
        if self._closed or self._ws is None or not tail_ms:
            return
        silence = bytes(2 * int(_SEND_RATE * tail_ms / 1000))   # PCM16 무음
        try:
            await self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(silence).decode("ascii"),
            }))
            # 최종 전사가 도착할 틈을 준다 — 안 기다리면 보내 놓고 끊는 것과 같다.
            await asyncio.sleep(min(2.0, tail_ms / 1000.0))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[stt-openai] 꼬리 무음 전송 실패(무시) — %s", str(exc)[:120])

    # ── 입력 ──
    async def push_audio(self, pcm: bytes) -> None:
        if self._closed or not pcm or self._ws is None:
            return
        self._sent_ms += len(pcm) / (self._sample_rate * 2) * 1000.0
        out = upsample_16k_to_24k(pcm) if self._sample_rate != _SEND_RATE else pcm
        try:
            await self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(out).decode("ascii"),
            }))
        except Exception as exc:  # noqa: BLE001
            # ⛔ **조용히 버리지 않는다**(2026-08-11 QA 발견6). 예전엔 경고 한 줄만 남기고
            #   `_closed=True` 로 두어, 반열림 소켓(송신 실패 + 수신 무응답)에서 **모든 오디오를
            #   버렸다.** 수신이 안 끝나면 STREAM_ERROR 도 안 나가서 세션은 살아 있는데
            #   **사용자 말이 전부 사라진다.** websockets keepalive 가 대개 ~40초 안에 끊어
            #   주지만, 그 40초치 발화는 흔적 없이 없어진다.
            #   ⭐ 큐에 직접 넣으면 세션이 **이미 가진 경로**(에러 통지·종료)로 흘러간다.
            logger.warning("[stt-openai] 오디오 전송 실패 — 스트림을 끊는다: %s", str(exc)[:120])
            self._closed = True
            self._q.put_nowait(SttV2Event(kind=STREAM_ERROR,
                                          detail=f"push_audio 실패: {str(exc)[:160]}"))

    # ── 출력 ──
    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                for event in self._translate(json.loads(raw)):
                    await self._q.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await self._q.put(SttV2Event(kind=STREAM_ERROR, detail=str(exc)[:200]))
        finally:
            await self._q.put(None)

    def _translate(self, msg: dict) -> list[SttV2Event]:
        """벤더 이벤트 → 우리 4종. ⛔ 모르는 타입은 **조용히 버린다**(계약을 넓히지 않는다).

        ⚠ `delta` 는 **증분**이라 그대로 넘기면 자막이 한 글자씩 덮인다. 우리 계약의 부분 전사는
          '지금까지의 전체'라, item 별로 이어 붙여서 낸다.
        """
        kind = msg.get("type") or ""
        if kind == "input_audio_buffer.speech_started":
            return [SttV2Event(kind=SPEECH_BEGIN, offset_ms=int(msg.get("audio_start_ms", -1)))]
        if kind == "input_audio_buffer.speech_stopped":
            return [SttV2Event(kind=SPEECH_END, offset_ms=int(msg.get("audio_end_ms", -1)))]
        if kind.endswith("input_audio_transcription.delta"):
            item = str(msg.get("item_id") or "")
            text = self._partial.get(item, "") + (msg.get("delta") or "")
            self._partial[item] = text
            return [SttV2Event(kind=TRANSCRIPT, text=text, is_final=False)] if text else []
        if kind.endswith("input_audio_transcription.completed"):
            item = str(msg.get("item_id") or "")
            self._partial.pop(item, None)
            text = msg.get("transcript") or ""
            return [SttV2Event(kind=TRANSCRIPT, text=text, is_final=True)] if text else []
        if kind == "error":
            detail = (msg.get("error") or {}).get("message") or "unknown"
            # ⛔ 벤더 에러 본문에 키가 실릴 일은 없지만, 길이를 잘라 남긴다.
            return [SttV2Event(kind=STREAM_ERROR, detail=str(detail)[:200])]
        return []

    async def events(self) -> AsyncIterator[SttV2Event]:
        while True:
            item = await self._q.get()
            if item is None:
                return
            yield item

    # ── 페이크 훅(테스트 계약 호환) ──
    def feed_test(self, text: str) -> None:
        if text:
            self._q.put_nowait(SttV2Event(kind=TRANSCRIPT, text=text, is_final=True))

    def feed_test_event(self, kind: str) -> None:
        self._q.put_nowait(SttV2Event(kind=kind, at=time.monotonic()))

    def usage(self) -> dict:
        """⚠ 벤더가 과금 초를 안 준다 — **우리가 흘린 길이**를 낸다.

        `billed_msgs=0` 이므로 요약이 `audio_s_source="sent_audio"` 로 표시한다(계약 그대로).
        ⛔ 침묵 과금 여부는 **미확인**이다. 우리는 마이크 상시개방이라 침묵도 흘린다 —
          벤더가 발화만 과금한다면 이 값은 **과대**다. 과소보다 과대가 안전한 방향이라
          그대로 두고, 청구서로 확인되면 여기만 고친다.
        """
        return {"streams": 1, "sent_audio_ms": self._sent_ms, "replay_audio_ms": 0.0,
                "billed_sum_ms": 0.0, "billed_max_ms": 0.0, "billed_msgs": 0}
