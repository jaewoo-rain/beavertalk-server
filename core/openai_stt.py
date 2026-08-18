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

from core.languages import normalize_locale

from core.audio import upsample_16k_to_24k
from core.config import settings
from core.stt import SPEECH_BEGIN, SPEECH_END, STREAM_ERROR, TRANSCRIPT, SttV2Event

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"
# ⚠ `OpenAI-Beta: realtime=v1` 헤더는 **폐기됐다** — 붙이면 4000 으로 거절된다(2026-08-10 실측).
_SEND_RATE = 24_000          # 벤더가 받는 유일한 PCM 레이트(16000 은 거절된다)
# ⛔ **치명이 아닌 벤더 오류 코드**(2026-08-16 스파이크에서 실제로 받은 것). 커넥션이 살아 있고
#   다음 발화가 정상으로 도는데, 여기 없으면 우리가 통화를 죽인다.
#   · `input_audio_buffer_commit_empty` — "지금 커밋할 오디오가 없다". PTT 는 **릴리즈마다
#     commit 을 보내므로**, 눌렀다 100ms 안에 떼는 정상 조작에서 이게 난다.
# ⚠ **좁게 연다.** 벤더 error 를 통째로 비치명으로 뒤집으면 진짜 스트림 death 를 놓친다 —
#   새 사례가 나오면 **실제로 받은 코드만** 하나씩 더해라(추측으로 넣지 마라).
_NON_FATAL_CODES = frozenset({"input_audio_buffer_commit_empty"})
_VENDOR_PREFIX = "openai-"


def is_configured() -> bool:
    """키가 있나. ⛔ 키 값은 로그·예외 어디에도 싣지 않는다."""
    return bool((settings.GPT_API_KEY or "").strip())


def openai_language_codes(codes: list[str] | None) -> list[str]:
    """⭐⭐ **벤더별 언어 코드 변환은 여기 한 곳이다**(2026-08-13 통화 사망 버그).

    두 벤더가 **정반대를 요구한다** — 그게 이 사고의 뿌리였다:
        Google STT v2   **BCP-47**(`en-US`). 지역이 없으면 거절 → `core/stt.py` 가 지역 없는
                        코드를 **폐기**한다("인식 언어 코드 폐기 ['ja']").
        OpenAI Realtime **ISO-639-1**(`en`). 지역이 붙으면 거절한다:
                        "Invalid value: 'en-US'. Supported values are: 'af','ar',…"

    🔴 실제로 통화가 죽었다(에뮬, **1.4초 만에**): `ja` 가 폐기돼 목록이 `['en-US']` **1개**가
      됐고, 아래 규칙이 그 값을 **그대로** OpenAI 에 넘겼다 → 거절 → error 프레임 → 앱이
      스낵바를 띄우고 통화를 끊었다.
    ⚠ **간헐적이라 더 나쁘다**: 목록이 2개면 `None`(자동감지)이 나가 정상이다. **1개로
      좁아질 때만** 터진다 — 재현이 안 돼 원인 불명으로 남기 딱 좋은 모양이었다.

    ⛔ 새 변환 함수를 만들지 않았다 — `normalize_locale`("ko-KR"→"ko")이 **이미 있다**.
    ⚠ 정규화하면 **중복이 생긴다**(`['en-US','en-GB']` → `['en','en']`). 합친다.
      그러면 1개가 되는데, 그건 **자동감지를 잃는 것이 아니라 사실 그대로다** — 두 항목이
      가리키던 언어가 하나였을 뿐이다. OpenAI 에 `en` 을 주는 편이 자동감지보다 정확하다.
    """
    out: list[str] = []
    for raw in codes or []:
        code = normalize_locale(raw)
        if code and code not in out:
            out.append(code)
    return out


def _session_summary(session: Any) -> str:
    """서버가 **확정한** 세션에서 우리가 알아야 할 셋만 뽑는다.

    ⭐ 셋인 이유:
      · `model` — 우리가 고른 전사 모델이 맞나
      · `turn_detection` — 타입 + `silence_duration_ms`. **이 값이 우리 지연에 그대로 붙는다**
        (안 먹으면 벤더 기본 500ms 로 돌고, 우리 800ms 위에 얹혀 총 1.3초가 된다)
      · `noise_reduction` — **우리는 안 보낸다.** 벤더 기본이 뭔지 문서에 없어서, 확정값을
        보는 것이 유일한 확인 수단이다(5분 지연 폭증 보고가 있는 기능이라 알아야 한다)
    ⚠ 전문을 찍지 않는다 — 길고 매 통화 나온다. 그리고 전사 텍스트가 섞일 여지를 안 만든다.
    """
    audio_in = ((session or {}).get("audio") or {}).get("input") or {}
    model = ((audio_in.get("transcription") or {}).get("model")
             or (session or {}).get("model") or "-")
    turn = audio_in.get("turn_detection")
    if isinstance(turn, dict):
        turn_txt = "%s(silence=%s)" % (
            turn.get("type") or "-",
            turn.get("silence_duration_ms", "미지정(벤더기본)"),
        )
    else:
        # ⛔ None 이면 **턴 감지가 아예 꺼진 것**이다 — 우리가 보낸 설정이 버려졌다는 신호다.
        turn_txt = "없음(⚠ 우리가 보낸 turn_detection 이 안 먹었다)"
    noise = audio_in.get("noise_reduction")
    if isinstance(noise, dict):
        noise_txt = noise.get("type") or "(타입없음)"
    else:
        noise_txt = "없음" if noise is None else str(noise)[:24]
    return "model=%s turn_detection=%s noise_reduction=%s" % (model, turn_txt, noise_txt)


def vendor_name() -> str:
    """원가 벤더 문자열 = **실제로 돈 모델 ID**(단가가 모델마다 다르다)."""
    return _VENDOR_PREFIX + (settings.OPENAI_STT_MODEL or "gpt-4o-mini-transcribe").strip()


class OpenAiRealtimeSttStream:
    """WS 1개 = 통화 1건. 롤오버가 없다(수명 한계가 문서에 없다 — 있으면 재연결로 다룬다).

    인터페이스는 `RollingSttV2Stream` 과 같다: start / push_audio / events / close / usage.
    """

    def __init__(self, sample_rate: int, language_codes: list[str] | None = None,
                 *, manual_commit: bool = False) -> None:
        self._sample_rate = sample_rate
        # ⭐⭐ **PTT 세션인가**(2026-08-18 사장님 결정). True 면 `turn_detection: null` 을 보내고
        #   턴 경계를 `input_audio_buffer.commit` 으로 **우리가** 정한다.
        #   ⛔ 기본 False — 마이크 상시개방 캐스케이드는 벤더 VAD 가 있어야 돈다. 상시로 켜지 않는다.
        #   ⚠ False 일 때 이 클래스가 벤더에 보내는 바이트는 **예전과 완전히 같다**(회귀가 지킨다).
        self._manual_commit = bool(manual_commit)
        # ⚠ OpenAI 는 `language` 를 **하나만** 받는다 — 다국어는 "안 넣기"가 유일한 방법이다
        #   (선정기준 §4-2). 그래서 코드가 여러 개면 아예 지정하지 않는다.
        # ⛔ **반드시 ISO-639-1 로 바꿔서** 넣는다. 호출부가 주는 목록은 Google 용 BCP-47
        #   (`en-US`)이고, 그대로 넘기면 OpenAI 가 거절해 **통화가 죽는다**(위 함수 주석).
        wanted = openai_language_codes(language_codes)
        self._language = wanted[0] if len(wanted) == 1 else None
        if language_codes and list(language_codes) != wanted:
            # 조용히 바꾸지 않는다 — 무엇을 받아 무엇으로 보냈는지 남긴다.
            logger.info("[stt-openai] 언어 코드 정규화 %s → %s", list(language_codes), wanted)
        self._ws: Any = None
        self._closed = False
        self._sent_ms = 0.0
        # ⭐⭐ **commit → transcription.completed 계측**(2026-08-18 사장님 지시). 지금까지 이
        #   구간은 800ms 침묵 타이머 **밑에 숨어** 같이 흘렀다. 침묵을 없애면 맨몸으로 드러나고,
        #   이건 대기가 아니라 **벤더가 글자를 만드는 시간**이라 우리가 못 없앤다.
        #   ⛔ 값 하나로 단정하지 마라 — **오디오 길이와의 상관**이 증거다:
        #     가설A 흘리며 전사 → 길이와 무관하게 짧다   ⇒ 목표 R 1.8초 달성
        #     가설B commit 뒤 일괄 → 길이에 비례한다     ⇒ 다음 표적은 LLM 이 아니라 STT 벤더 교체
        #   ⚠ 여기서 잰다(세션이 아니라) — 우리 큐 홉이 안 섞인 **순수 벤더 왕복**이다.
        self._commit_at = 0.0            # 마지막 commit 을 보낸 시각(0 = 대기 중 아님)
        self._commit_audio_ms = 0.0      # 그 commit 시점의 누적 오디오 ms
        self._prev_commit_audio_ms = 0.0 # 직전 commit 시점 — 둘의 차가 **이 턴 오디오 길이**
        self.last_commit_lag_ms = -1     # 세션이 턴 로그에 실을 수 있게 남긴다(-1 = 못 쟀다)
        self.last_commit_audio_ms = -1
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
        # ⭐⭐ **침묵창을 명시한다**(2026-08-14). 지금까지는 안 보내고 벤더 기본값에 맡겼는데,
        #   그 값이 우리 턴 침묵(800ms) **앞에 통째로 얹혀** 있었다(실측 근거는 config 주석).
        #   ⛔ 안 보내면 그 값이 얼마인지 **로그로도 알 수 없다** — 조용한 기본값의 전형이다.
        #   ⚠ 0 이면 예전처럼 안 보낸다(벤더 기본값으로 되돌아간다).
        # ⭐⭐ **PTT 는 벤더 VAD 를 통째로 끈다**(2026-08-18). `null` 이면 벤더는 **commit 을
        #   받을 때만** 전사한다 ⇒ 침묵창 300ms 가 소멸하고 턴 경계가 버튼이 된다.
        #   ⛔ 스파이크(20260816_1749 §2)에서 이 모델이 `null` + 수동 commit 을 **에러 0건**으로
        #     받는 것을 실제 응답으로 확인했다. 문서 인용이 아니다.
        turn_detection: dict[str, Any] | None
        silence_ms = 0
        if self._manual_commit:
            turn_detection = None
        else:
            turn_detection = {"type": "server_vad"}
            silence_ms = max(0, int(settings.OPENAI_STT_SILENCE_MS or 0))
            if silence_ms:
                turn_detection["silence_duration_ms"] = silence_ms
        await self._ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {"input": {
                    "format": {"type": "audio/pcm", "rate": _SEND_RATE},
                    "transcription": transcription,
                    "turn_detection": turn_detection,
                }},
            },
        }))
        self._reader = asyncio.create_task(self._read_loop())
        logger.info("[stt-openai] 연결 — 모델=%s 언어=%s 침묵창=%s (%dHz 로 올려 보낸다)",
                    transcription["model"], self._language or "자동감지",
                    "수동커밋(turn_detection=null)" if self._manual_commit
                    else ("%dms" % silence_ms if silence_ms else "벤더기본(미지정)"), _SEND_RATE)

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

    async def commit(self) -> bool:
        """⭐ 버퍼를 **지금 확정한다** — PTT 의 턴 경계(`turn_detection: null` 전용).

        ⛔ **VAD 세션에서는 아무것도 안 한다.** 거기서 보내면 벤더가 item 을 하나 더 만들어
          기존 경로의 전사 개수가 바뀐다 — "VAD 경로는 바이트 단위로 불변"을 깨는 자리다.
        ⚠ 반환값은 "보냈나"다. 세션은 이 값으로 **전사를 기다릴지**를 정한다 — 안 보냈는데
          기다리면 상한(1.5초)을 통째로 헛대기한다.
        ⚠ R5: 전송 실패가 통화를 죽이지 않는다. 못 보냈으면 False 로 알리고 세션이 턴을 닫는다.
        """
        if not self._manual_commit or self._closed or self._ws is None:
            return False
        try:
            await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[stt-openai] commit 전송 실패 — 이 턴은 전사를 못 받는다: %s",
                           str(exc)[:120])
            return False
        self._commit_at = time.monotonic()
        self._prev_commit_audio_ms, self._commit_audio_ms = self._commit_audio_ms, self._sent_ms
        return True

    def _note_commit_done(self, chars: int) -> None:
        """commit 한 건이 전사로 돌아왔다 — **왕복 ms 와 그 턴 오디오 길이를 같은 줄에** 남긴다.

        ⭐ 두 값이 한 줄에 있어야 가설 A/B 를 **로그만 보고** 가른다. 따로 찍으면 사람이 손으로
          짝을 맞춰야 하고, 그러면 아무도 안 본다(이 프로젝트가 여러 번 밟은 계열이다).
        """
        if not self._commit_at:
            return
        lag_ms = int(max(0.0, time.monotonic() - self._commit_at) * 1000)
        audio_ms = int(max(0.0, self._commit_audio_ms - self._prev_commit_audio_ms))
        self._commit_at = 0.0
        self.last_commit_lag_ms, self.last_commit_audio_ms = lag_ms, audio_ms
        logger.info(
            "[stt-openai] commit→전사 확정: %dms (이 턴 오디오 %dms, 글자 %d) — "
            "길이와 무관하면 가설A(흘리며 전사), 비례하면 가설B(commit 뒤 일괄)",
            lag_ms, audio_ms, chars,
        )

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
        if kind == "session.updated":
            # ⭐⭐ **우리가 보낸 설정이 실제로 먹었는지 확인하는 유일한 계기판**(2026-08-16).
            #   벤더는 미지원 필드가 하나라도 있으면 그 `session.update` 를 **통째로 버리는데
            #   커넥션은 안 죽는다** ⇒ 조용히 기본값으로 돈다. 그러면 `silence_duration_ms=300`
            #   이 안 먹고 벤더 기본 **500ms** 로 도는데(문서 확인) 우리는 **알 방법이 없었다.**
            #   ⇒ 지연이 200ms 늘어난 채로 며칠을 재고 있었을 수도 있다.
            logger.info("[stt-openai] 세션 확정 — %s", _session_summary(msg.get("session")))
            return []
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
            self._note_commit_done(len(text))
            # ⭐⭐ **PTT 에서는 빈 전사도 올린다**(2026-08-18). 아무 말 없이 눌렀다 떼면 벤더는
            #   빈 `completed` 를 주는데, 그걸 여기서 버리면 세션에 **이벤트가 0건**이라
            #   상한(1.5초)을 통째로 헛대기한 뒤에야 턴이 닫힌다 — 정상 조작에서 나는 지연이다.
            #   ⛔ VAD 경로는 예전 그대로 버린다(빈 final 이 새로 흐르면 턴 판정이 바뀐다).
            if text or self._manual_commit:
                return [SttV2Event(kind=TRANSCRIPT, text=text, is_final=True)]
            return []
        if kind == "error":
            err = msg.get("error") or {}
            detail = err.get("message") or "unknown"
            # ⛔⛔ **서버가 먼저 알아야 한다.** 지금까지 이 거절은 서버 로그에 WARNING 한 줄
            #   없이 error 프레임으로만 나갔다 — 앱은 스낵바를 띄우고 통화를 끊는데 서버는
            #   조용했다(2026-08-13 통화 사망 버그를 못 본 이유가 이것이다).
            #   ⛔ 벤더 에러 본문에 키가 실릴 일은 없지만, 길이를 잘라 남긴다.
            # ⚠ **`param`·`code` 를 같이 남긴다**(2026-08-16). 거절에는 두 종류가 있다:
            #     · 세션 설정 거절(`param="session.…"`) — 그 `session.update` 만 버려지고
            #       **커넥션은 산다**. 즉 우리는 **기본값으로 돌고 있는 것**이다.
            #     · 그 밖 — 스트림 자체가 못 쓰게 된 경우.
            #   ⛔ 예전 문구는 무조건 "이 통화는 여기서 끊긴다"라고 **단정**했다. 앞의 경우엔
            #     그게 사실이 아니다 — 로그가 단정하면 다음 사람이 엉뚱한 곳을 판다.
            param = str(err.get("param") or "")
            # ⛔⛔ **설정 거절로 통화를 죽이지 않는다**(2026-08-16). 지금까지는 벤더 `error` 를
            #   전부 `STREAM_ERROR` 로 올렸고, 세션은 그걸 받으면 **무조건 통화를 종료**한다
            #   (`cascade_session.py` 의 `raise _Stop`). 그런데 세션 설정 거절은 **커넥션을
            #   안 죽인다** — 그 `session.update` 만 버려지고 스트림은 그대로 산다.
            #   ⇒ 벤더는 계속 돌 수 있는데 **우리가 먼저 끊고 있었다.**
            #   ⚠ 설정이 안 먹은 채 도는 것은 "느려짐"이지 "통화 불가"가 아니다. 끊는 쪽이 훨씬 나쁘다.
            # ⛔ 그 밖의 오류는 **지금처럼 죽인다** — 전부 살리면 진짜 스트림 death 를 놓친다.
            # ⭐ **좁게 연다**(2026-08-16 결정 ⓐ). 새 사례가 나오면 그때 하나씩 더한다 —
            #   벤더 error 를 통째로 비치명으로 뒤집으면 **진짜 스트림 death 를 놓친다.**
            code = str(err.get("code") or "")
            config_reject = param.startswith("session.") or code in _NON_FATAL_CODES
            logger.warning(
                "[stt-openai] 벤더 거절 — %s (code=%s param=%s 언어=%s) %s",
                str(detail)[:200], err.get("code") or "-", param or "-",
                self._language or "자동감지",
                "⚠ 세션 설정이 통째로 미적용됐다 — 벤더 기본값으로 돌고 있다(통화는 계속한다)"
                if config_reject else "이 통화는 여기서 끊긴다",
            )
            if config_reject:
                # ⚠ 비치명으로 넘길 때도 **위 WARNING 은 이미 남겼다** — 조용히 삼키지 않는다.
                return []
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
