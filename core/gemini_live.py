"""Gemini Live 세션 래퍼 (normalcall 실시간 음성통화) — 외부 어댑터.

beavertalk 서버의 검증된 live.py 를 이 프로젝트로 포팅. Vertex native-audio +
컨텍스트 윈도우 압축(5분+ 통화 유지의 핵심) + 입출력 전사 + 단일 prebuilt voice.
도메인/DB/프롬프트를 모른다(speechsuper.py 와 동일한 어댑터 규율). system_instruction
과 voice 는 호출부(realtime)가 조립해 넘긴다.

LiveSessionProtocol 로 모킹 가능 — 테스트는 동일 인터페이스의 가짜 세션을 주입.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal, Optional, Protocol, runtime_checkable

from google import genai
from google.genai import types

from core.audio import INPUT_MIME_TYPE
from core.config import Settings

logger = logging.getLogger(__name__)

# 톤 일관성: 비버 음성 기본값(캐릭터가 voice 를 주면 그걸 사용).
DEFAULT_VOICE = "Fenrir"
# native-audio 모델은 temperature=0 에서 반복·로봇처럼 되므로 0 을 쓰지 않는다.
LIVE_TEMPERATURE = 0.6

LiveEventKind = Literal["audio", "in_tr", "out_tr", "interrupted", "turn_end"]


@dataclass(slots=True)
class LiveEvent:
    """Gemini Live 응답을 호출부가 다루기 쉽게 정규화한 단일 이벤트."""

    kind: LiveEventKind
    audio: Optional[bytes] = None      # kind=="audio": 출력 PCM24k
    text: Optional[str] = None         # kind in {in_tr,out_tr}: 전사
    is_final: bool = False             # 입력 전사 확정 여부


@runtime_checkable
class LiveSessionProtocol(Protocol):
    """realtime 브리지가 의존하는 Live 세션 인터페이스(모킹 확장점)."""

    async def send_audio(self, pcm16_16k: bytes) -> None: ...
    async def send_text_turn(self, text: str) -> None: ...
    def events(self) -> AsyncIterator[LiveEvent]: ...


def build_live_config(
    *, system_instruction: str, voice: str = DEFAULT_VOICE
) -> types.LiveConnectConfig:
    """normalcall 용 LiveConnectConfig 구성.

    오디오 출력 + 입출력 전사 + 단일 prebuilt voice + 컨텍스트 압축(슬라이딩 윈도우).
    safety_settings 는 거친 페르소나(트래시토커) 면박·욕설 허용을 위해 HARASSMENT 만
    완화하고 혐오·성·위험은 엄격 유지한다. realtime_input_config 는 넣지 않는다(무음 버그).
    """
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        system_instruction=system_instruction,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
            )
        ),
        temperature=LIVE_TEMPERATURE,
        # ⭐ 5분+ 통화 유지의 핵심: 오디오는 토큰 소모가 커 압축 없이는 ~2분 만에
        # 컨텍스트 한계로 서버가 세션을 닫는다. 슬라이딩 윈도우로 길게 유지.
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        safety_settings=[
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                threshold=types.HarmBlockThreshold.BLOCK_ONLY_HIGH,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=types.HarmBlockThreshold.BLOCK_MEDIUM_AND_ABOVE,
            ),
        ],
    )


class GeminiLiveSession:
    """google-genai live.connect 세션의 비동기 래퍼."""

    def __init__(self, raw_session: Any) -> None:
        self._session = raw_session

    async def send_audio(self, pcm16_16k: bytes) -> None:
        """입력 PCM(16bit/16k/mono) 청크를 즉시 모델로 전송(버퍼링 없음)."""
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm16_16k, mime_type=INPUT_MIME_TYPE)
        )

    async def send_text_turn(self, text: str) -> None:
        """초기 선톡 시드/종료 시드용 user 텍스트 턴 1회 전송.

        receive 루프가 이미 돌고 있어야 첫 청크를 놓치지 않는다.
        """
        await self._session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=True,
        )

    async def events(self) -> AsyncIterator[LiveEvent]:
        """SDK 응답 스트림을 LiveEvent 로 정규화해 yield.

        session.receive() 는 한 턴까지만 yield 하므로 바깥 루프에서 매 턴 재호출.
        수신 0건이면 세션 종료로 보고 루프를 끝낸다. 모든 필드 접근은 None-safe.
        """
        while True:
            received_any = False
            async for response in self._session.receive():
                received_any = True

                data = getattr(response, "data", None)
                if data:
                    yield LiveEvent(kind="audio", audio=data)

                server_content = getattr(response, "server_content", None)
                if server_content is None:
                    continue

                in_tr = getattr(server_content, "input_transcription", None)
                in_text = getattr(in_tr, "text", None) if in_tr is not None else None
                if in_text:
                    is_final = bool(getattr(in_tr, "finished", False))
                    yield LiveEvent(kind="in_tr", text=in_text, is_final=is_final)

                out_tr = getattr(server_content, "output_transcription", None)
                out_text = getattr(out_tr, "text", None) if out_tr is not None else None
                if out_text:
                    yield LiveEvent(kind="out_tr", text=out_text)

                if getattr(server_content, "interrupted", False):
                    yield LiveEvent(kind="interrupted")

                if getattr(server_content, "turn_complete", False):
                    yield LiveEvent(kind="turn_end")

            if not received_any:
                logger.info("Gemini 수신 스트림 종료 — events 루프 종료")
                break


@contextlib.asynccontextmanager
async def open_session(
    client: genai.Client,
    settings: Settings,
    *,
    system_instruction: str,
    voice: str = DEFAULT_VOICE,
) -> AsyncIterator[GeminiLiveSession]:
    """normalcall Gemini Live 세션을 열고 래퍼를 yield 하는 async 컨텍스트 매니저.

    클라이언트 WS 수명 동안 단일 세션 유지(멀티턴 히스토리 보존). config 는
    build_live_config 가 구성, system_instruction/voice 는 호출부(realtime)가 조립.
    """
    config = build_live_config(system_instruction=system_instruction, voice=voice)
    logger.info("normalcall Live 연결 시도: model=%s voice=%s", settings.GEMINI_LIVE_MODEL, voice)
    async with client.aio.live.connect(
        model=settings.GEMINI_LIVE_MODEL,
        config=config,
    ) as raw_session:
        logger.info("normalcall Live 세션 연결됨")
        try:
            yield GeminiLiveSession(raw_session)
        finally:
            logger.info("normalcall Live 세션 종료")
