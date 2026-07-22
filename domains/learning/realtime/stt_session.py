"""발음 챌린지 STT 세션 — WS ↔ STT 스트림 브리지.

WS 연결 1개 = STT 세션 1건. normalcall 의 call_session 과 같은 구조(두 펌프를
asyncio.TaskGroup 으로 묶어 한쪽 종료 시 동반 취소)지만 방향이 단순하다:
  client → (audio)   → STT
  STT    → (partial/final transcript) → client

프로토콜(프론트와 합의 — 웹 발음챌린지와 동일):
  client→server: 첫 텍스트 {"type":"config","words":[...],"sampleRate":N}, 이후 바이너리 PCM,
                 선택 {"type":"stop"}. (테스트 훅: {"type":"__test_say","text":...})
  server→client: {"type":"ready"} / {"type":"partial","text"} / {"type":"final","text"} /
                 {"type":"error","error"}
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.websockets import WebSocket, WebSocketState

from core.stt import make_stt_stream

logger = logging.getLogger(__name__)

_DEFAULT_SAMPLE_RATE = 16000  # 앱 마이크 기본(PCM16/16k). config 로 덮어씀.
_MAX_WORDS = 200  # phrase hints 상한(악의적/과대 입력 방어)


@dataclass
class SttInbound:
    """WS 인바운드 1건: 바이너리=오디오, 텍스트=JSON 제어, 끊김=disconnect."""

    kind: str  # 'audio' | 'control' | 'disconnect'
    audio: bytes | None = None
    control: dict | None = None


class SttTransport(Protocol):
    """WS 어댑터 인터페이스(테스트는 스텁으로 대체)."""

    async def send_event(self, event: dict) -> None: ...
    async def receive(self) -> SttInbound: ...


class WsSttTransport:
    """starlette WebSocket → SttTransport 어댑터(bytes/text 분기)."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    async def send_event(self, event: dict) -> None:
        await self._ws.send_json(event)

    async def receive(self) -> SttInbound:
        msg = await self._ws.receive()
        if msg.get("type") == "websocket.disconnect":
            return SttInbound(kind="disconnect")
        if msg.get("bytes") is not None:
            return SttInbound(kind="audio", audio=msg["bytes"])
        text = msg.get("text")
        if text is not None:
            try:
                ctrl = json.loads(text)
            except (ValueError, TypeError):
                ctrl = {}
            return SttInbound(kind="control", control=ctrl)
        return SttInbound(kind="control", control={})


class _Stop(Exception):
    """펌프를 정상 종료시키는 내부 신호(에러 아님)."""


class PronSttSession:
    def __init__(self, transport: SttTransport) -> None:
        self.transport = transport

    async def run(self) -> None:
        # 1) 첫 메시지 — config 를 기대. 오디오가 먼저 오면 기본값으로 진행하고 그 청크를 버퍼링.
        try:
            first = await self.transport.receive()
        except Exception:  # noqa: BLE001 - 연결이 바로 끊긴 경우
            return
        if first.kind == "disconnect":
            return

        sample_rate = _DEFAULT_SAMPLE_RATE
        words: list[str] = []
        pending_audio: bytes | None = None
        if first.kind == "control" and (first.control or {}).get("type") == "config":
            ctrl = first.control or {}
            raw_words = ctrl.get("words") or []
            if isinstance(raw_words, list):
                words = [str(w) for w in raw_words if w][:_MAX_WORDS]
            try:
                sample_rate = int(ctrl.get("sampleRate") or _DEFAULT_SAMPLE_RATE)
            except (TypeError, ValueError):
                sample_rate = _DEFAULT_SAMPLE_RATE
        elif first.kind == "audio":
            pending_audio = first.audio

        # 2) STT 스트림 개시(키/크레덴셜 없으면 core.stt 가 페이크로 폴백).
        stream = make_stt_stream(sample_rate, words)
        try:
            await stream.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("STT 스트림 시작 실패")
            await self._safe_event({"type": "error", "error": str(exc)})
            return

        await self._safe_event({"type": "ready"})

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._pump_in(stream, pending_audio))
                tg.create_task(self._pump_out(stream))
        except* _Stop:
            pass  # 정상 종료(클라 stop / 스트림 끝 / disconnect)
        except* Exception as eg:  # noqa: BLE001
            self._log_pump_errors(eg)
            await self._safe_event({"type": "error", "error": "stt_stream_error"})
        finally:
            try:
                await stream.close()
            except Exception:  # noqa: BLE001
                pass

    # ── client → STT ──
    async def _pump_in(self, stream: Any, pending_audio: bytes | None) -> None:
        if pending_audio:
            await stream.push_audio(pending_audio)
        while True:
            inb = await self.transport.receive()
            if inb.kind == "disconnect":
                raise _Stop
            if inb.kind == "control":
                ctype = (inb.control or {}).get("type")
                if ctype == "stop":
                    raise _Stop
                if ctype == "__test_say":  # 테스트/페이크 구동 훅
                    stream.feed_test(str((inb.control or {}).get("text") or ""))
                continue
            if inb.kind == "audio" and inb.audio:
                await stream.push_audio(inb.audio)

    # ── STT → client ──
    async def _pump_out(self, stream: Any) -> None:
        async for text, is_final in stream.results():
            if not text:
                continue
            await self.transport.send_event(
                {"type": "final" if is_final else "partial", "text": text}
            )
        raise _Stop  # 스트림이 끝나면 세션 종료

    async def _safe_event(self, event: dict) -> None:
        try:
            await self.transport.send_event(event)
        except Exception:  # noqa: BLE001 - 이미 닫히는 중일 수 있음
            pass

    def _log_pump_errors(self, eg: BaseExceptionGroup) -> None:
        from starlette.websockets import WebSocketDisconnect

        real = eg.subgroup(
            lambda e: not isinstance(
                e, (WebSocketDisconnect, ConnectionError, asyncio.CancelledError)
            )
        )
        if real is not None:
            logger.error("STT pump 오류: %r", real.exceptions)


async def run_pron_stt(websocket: WebSocket) -> None:
    """WS 발음챌린지 STT 세션 구동(라우터에서 accept 후 위임). 소켓 정리까지 책임."""
    session = PronSttSession(WsSttTransport(websocket))
    try:
        await session.run()
    finally:
        if websocket.client_state != WebSocketState.DISCONNECTED:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass
