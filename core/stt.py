"""발음 챌린지 서버 STT 어댑터 — Google Cloud Speech-to-Text 스트리밍.

core 어댑터 규율(도메인/DB 무지, graceful degradation): 키 부재·미설치·인증실패·STT_FAKE
어느 경우든 죽지 않고 **페이크 스트림**으로 폴백한다(과금 0, 서버 정상 기동). tts.py 와 동일한
lru_cache + None 폴백 패턴.

세션(stt_session)은 프로바이더를 모른 채 이 인터페이스만 쓴다:
  start()            스트림 개시
  push_audio(bytes)  마이크 PCM(LINEAR16) 청크 투입
  feed_test(text)    테스트 훅 — 실제 스트림은 no-op, 페이크는 final 결과로 방출
  results()          (text, is_final) 를 yield 하는 async iterator
  close()            정리
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.config import settings

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_SENTINEL = object()  # 큐 종료 센티널


@lru_cache(maxsize=1)
def get_speech_client() -> "Any | None":
    """SpeechAsyncClient(프로세스당 1개). STT_FAKE·키부재·미설치·인증실패면 None(graceful).

    STT 전용 키(STT_SA_KEY_FILE)가 있으면 그걸, 없으면 통화/TTS 용 키(TTS_SA_KEY_FILE,
    bt-dev-web-01)를 재사용한다 — 같은 GCP 프로젝트라 Speech-to-Text API 활성화 + SA 에
    roles/speech.client 만 있으면 된다.
    """
    if settings.STT_FAKE:
        logger.warning("[stt] STT_FAKE 활성 — 실제 Speech 클라이언트 미생성(페이크 스트림 사용).")
        return None
    try:
        key_path = Path(settings.STT_SA_KEY_FILE or settings.TTS_SA_KEY_FILE)
        if not key_path.is_file():
            logger.warning("[stt] SA 키 없음(%s) → STT 비활성(페이크 폴백).", key_path)
            return None
        # 사용 시점에만 import(미설치 환경에서 모듈 로드만으로 죽지 않게 — 페이크 경로는 미호출).
        from google.cloud import speech_v1
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=_SCOPES
        )
        client = speech_v1.SpeechAsyncClient(credentials=creds)
        logger.info(
            "[stt] Speech-to-Text async client ready (project=%s)",
            getattr(creds, "project_id", None),
        )
        return client
    except Exception as exc:  # noqa: BLE001 - 미설치/인증/임의 예외 graceful
        get_speech_client.cache_clear()
        logger.warning("[stt] Speech 클라이언트 초기화 실패(무시, 페이크 폴백) — %s", exc)
        return None


class GoogleSttStream:
    """google-cloud-speech v1 비동기 스트리밍(streaming_recognize) 래퍼.

    첫 요청은 streaming_config, 이후 요청은 audio_content 청크. WS 바이너리 프레임을
    asyncio.Queue 로 받아 request 제너레이터로 흘리고, 응답을 순회하며 (transcript, is_final)
    를 방출한다.

    ※ Google 스트리밍은 스트림당 ~5분 한도가 있으나 발음 챌린지는 단발(~60s)이라 롤오버는
      두지 않는다(장문 필요 시 여기서 스트림 재시작).
    """

    def __init__(self, client: Any, sample_rate: int, words: list[str]) -> None:
        self._client = client
        self._sample_rate = sample_rate
        self._words = words
        self._audio_q: asyncio.Queue[Any] = asyncio.Queue()
        self._responses: AsyncIterator[Any] | None = None

    def _streaming_config(self) -> Any:
        from google.cloud import speech_v1 as speech

        cfg_kwargs: dict[str, Any] = dict(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=self._sample_rate,
            language_code=settings.STT_LANGUAGE,
            enable_automatic_punctuation=False,
        )
        if settings.STT_MODEL:
            cfg_kwargs["model"] = settings.STT_MODEL
        if self._words:
            cfg_kwargs["speech_contexts"] = [
                speech.SpeechContext(phrases=self._words, boost=settings.STT_PHRASE_BOOST)
            ]
        config = speech.RecognitionConfig(**cfg_kwargs)
        return speech.StreamingRecognitionConfig(
            config=config, interim_results=True, single_utterance=False
        )

    async def _requests(self) -> AsyncIterator[Any]:
        from google.cloud import speech_v1 as speech

        yield speech.StreamingRecognizeRequest(streaming_config=self._streaming_config())
        while True:
            chunk = await self._audio_q.get()
            if chunk is _SENTINEL:
                return
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    async def start(self) -> None:
        # 비동기 클라: streaming_recognize 는 응답 async iterator 로 resolve 되는 awaitable.
        self._responses = await self._client.streaming_recognize(requests=self._requests())

    async def push_audio(self, pcm: bytes) -> None:
        await self._audio_q.put(pcm)

    def feed_test(self, text: str) -> None:  # noqa: D401 - 실제 스트림은 테스트 훅 무시
        return None

    async def results(self) -> AsyncIterator[tuple[str, bool]]:
        if self._responses is None:
            return
        async for response in self._responses:
            for result in response.results:
                if not result.alternatives:
                    continue
                text = result.alternatives[0].transcript or ""
                yield text, bool(result.is_final)

    async def close(self) -> None:
        await self._audio_q.put(_SENTINEL)


class FakeSttStream:
    """크레덴셜 없이 세션/프론트 통합을 구동하는 페이크(STT_FAKE·키부재 시).

    push_audio 는 무시하고, feed_test(text) 로 넣은 텍스트를 {final} 결과로 방출한다.
    라우터가 control {"type":"__test_say","text":...} 를 받으면 세션이 feed_test 를 부른다.
    """

    def __init__(self) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue()

    async def start(self) -> None:
        return None

    async def push_audio(self, pcm: bytes) -> None:
        return None

    def feed_test(self, text: str) -> None:
        if text:
            self._q.put_nowait((text, True))

    async def results(self) -> AsyncIterator[tuple[str, bool]]:
        while True:
            item = await self._q.get()
            if item is _SENTINEL:
                return
            yield item  # (text, is_final)

    async def close(self) -> None:
        self._q.put_nowait(_SENTINEL)


def make_stt_stream(sample_rate: int, words: list[str]) -> Any:
    """설정/크레덴셜에 따라 실제(Google) 또는 페이크 STT 스트림을 만든다.

    STT_FAKE 거나 클라이언트를 못 만들면(키부재/미설치/인증실패) 페이크로 폴백한다 — 어떤
    경우든 세션은 정상 동작(graceful).
    """
    client = get_speech_client()
    if client is None:
        return FakeSttStream()
    return GoogleSttStream(client, sample_rate, words)
