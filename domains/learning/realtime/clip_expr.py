"""PM-DEC-872 — 표현학습 클립 주도 시제품(구조 B · 바바 · 1차시 L1-S01-1 · 모국어 en · exp/clip-expr-872 전용).

요청서: 52 하네스 `_output/2026-10-14_통화흐름_공동설계/63_공유_표현학습클립시제품요청.md`.

구조(대리 세션 — PerTurnSession 과 같은 자리):
  - 이 객체가 LiveSessionProtocol 을 흉내 낸다 → 2펌프·워처·저장·분석·종료는 **종전 그대로** 돈다.
  - 기본 통화에는 Live 세션이 없다. 학습자 말 끝은 마이크 프레임 RMS 로 이 객체가 잡는다(서버 VAD).
  - 판정(2차 · call 2034·2035 지연 불합격 뒤): Google STT recognize(ko-KR + en-US · 목표 표현을 힌트로) → 규칙(자모 유사도·모름·언어) →
    규칙이 못 정하면 그때만 텍스트 LLM(2.5-flash · thinking 0) 1회. 말 끝 무음 0.35초에 **미리** 시작하고, 말이 이어지면 버린다.
  - 진행 틀(요청서 3-1)이 클립 순서열을 고른다 → 클립 PCM 을 Live 오디오처럼 내보낸다(out_tr·audio·turn_end).
  - 예외 턴(요청서 4절): 텍스트 LLM 영어 한두 문장 + Cloud TTS(Chirp3-HD · 같은 Fenrir) — 그 턴짜리 Live 는 연결·첫 소리가 7~13초라 뺐다.
    한국어 정답은 TTS 로 읽지 않고 그 항목 클립(model_·ask_)을 뒤에 붙인다.
  - 조각(이어하기)이 바뀌어도 진행 위치를 call_id 로 이어 간다(프로세스 메모리 · 시험 태그는 인스턴스 1개로 띄운다).
  - 클립마다 서버 로그 「normalcall clipexpr clip:」 1줄 + WS {"type":"clip", …} 알림(앱은 모르는 type 을 무시한다).
⛔ 판정은 학습 진도 기록과 별개다 — 진도는 종전 추적기(전사 기반)가 그대로 적는다(요청서 3-3 · 운영 DB 범위 동일).
⛔ 켜는 조건은 env CLIP_EXPR(캐릭터 이름) + 표현학습 + 그 캐릭터 voice + locale en 일 때만. 빈 값이면 아무것도 안 만든다.
"""
from __future__ import annotations

import asyncio
import contextlib
import difflib
import json
import logging
import math
import random
import re
import time
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

from pydantic import BaseModel

from core.gemini_live import LiveEvent
from domains.learning.realtime import reaction as _reaction

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3] / "assets" / "clips_expr"
VOICE_BANK = {"Fenrir": "Baba", "Leda": "Bibi"}
CHAR_VOICE = {v: k for k, v in VOICE_BANK.items()}   # PM-DEC-890 — 예외 턴 TTS 목소리(Chirp3-HD 같은 이름)
PERSONA = {
    "Baba": "You are Baba, a snarky, teasing Korean-tutor friend: short, dry, a little smug. No insults, no slurs.",
    # PM-DEC-890 — call flow 비비 자산과 같이 받은 성격 한 줄(예외 턴 텍스트용).
    # PM-DEC-896 — 사용자 「비비는 엄청 오바하면서 해야해」 · call flow 교체안.
    "Bibi": ("You are Bibi — a hyper, over-the-top Korean tutor friend who reacts big to everything. Scream with excitement at "
             "right answers, and when the learner is wrong or stuck, dramatically and warmly cheer them on to try again — never "
             "mock or scold. Speak English; use Korean only for the expression itself."),
}
_CHUNK = 9600            # 0.2초(24kHz·16bit·mono)
_OUT_BPS = 48000         # 출력 24kHz·16bit·mono 바이트/초
_MIC_BPS = 32000         # 업링크 16kHz·16bit·mono 바이트/초
EXC_CAP = 8              # 예외 턴 통화당 상한(요청서 4절)
LAUGH_P = 0.3
PRE_GAP_S = 0.3          # 말 문턱 전 이만큼 짧은 틈은 말을 끊지 않는다(PM-DEC-913)
SPEC_SILENCE_S = 0.35    # 이만큼 무음이면 판정을 미리 시작한다(말이 이어지면 버린다)
PLAYBACK_SLACK_S = 0.3   # 앱 재생 끝 추정에 더하는 여유
# 클립 묶음 → 표정(set_face). 매니페스트 face 칸이 neutral 이 아니면 그것이 먼저다. 앱 표정 이름(neutral·happy·surprised·sad·angry·laugh)만.
FACE_BY_GROUP = {"g_ok": "happy", "g_rep_ok": "happy", "g_open": "happy", "g_close": "happy", "g_quiz_start": "happy",
                 "g_laugh": "laugh", "g_wrong": "surprised", "g_rep_again": "surprised", "g_dunno": "sad"}
RESULTS = ("correct", "close", "wrong", "dunno", "other", "native_lang")

# 조각을 넘어 진행을 잇는다(call_id → 상태). 시험 태그 전용 · 인스턴스 1개 전제.
_PROGRESS: dict[Any, dict] = {}
_PROGRESS_BUCKET = "clip-progress"   # PM-DEC-915 — GCS 에도 남긴다(다른 인스턴스로 간 조각 이어하기)


def _progress_load(call_id) -> Optional[dict]:
    from core import storage
    raw = storage.download(_PROGRESS_BUCKET, f"{call_id}.json")
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - 깨진 파일은 «없음»(처음부터)
        logger.warning("normalcall clipexpr: 진행 파일 깨짐 call_id=%s — 처음부터", call_id)
        return None


def _progress_store(call_id, snap: dict) -> None:
    from core import storage
    storage.upload(_PROGRESS_BUCKET, f"{call_id}.json", json.dumps(snap, ensure_ascii=False).encode("utf-8"),
                   content_type="application/json")


@dataclass(slots=True)
class Clip:
    id: str
    text: str
    sec: float
    pcm: bytes
    face: str = "neutral"


@dataclass(slots=True)
class Item:
    seq: int
    surface: str
    en: str
    blank: bool


class TextJudgeOut(BaseModel):
    result: str     # correct|close|wrong|other


# ── 문자열 규칙 ────────────────────────────────────────────────────────────── #
_CHO = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_JUNG = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_JONG = " ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"
_KEEP = re.compile(r"[^0-9A-Za-z가-힣]")
_HANGUL = re.compile(r"[가-힣]")
_DUNNO = ("몰라", "모르", "모름", "잘 모", "don't know", "dont know", "do not know", "no idea", "not sure")
_BLANK_RE = re.compile(r"저는?\s*\S+.*(이에요|예요|에요|입니다|이야|야)\W*$")


def jamo(text: str) -> str:
    out = []
    for ch in _KEEP.sub("", text or ""):
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out.append(_CHO[code // 588] + _JUNG[(code % 588) // 28] + _JONG[code % 28].strip())
        else:
            out.append(ch.lower())
    return "".join(out)


def jamo_sim(a: str, b: str) -> float:
    ja, jb = jamo(a), jamo(b)
    if not ja or not jb:
        return 0.0
    return difflib.SequenceMatcher(None, ja, jb).ratio()


def rule_judge(heard: str, targets: list[str], *, blank: bool) -> tuple[Optional[str], float]:
    """(결과 또는 None=LLM 에 묻기, 최고 자모 유사도)."""
    h = (heard or "").strip()
    if not _KEEP.sub("", h):
        return "dunno", 0.0
    low = h.lower()
    if any(k in low for k in _DUNNO):
        return "dunno", 0.0
    if not _HANGUL.search(h):
        return "native_lang", 0.0
    if blank and _BLANK_RE.search(h):
        return "correct", 1.0
    sim = max((jamo_sim(t, h) for t in targets if t), default=0.0)
    if sim >= 0.8:
        return "correct", sim
    if sim >= 0.6:
        return "close", sim
    return None, sim


# ── 자산 ──────────────────────────────────────────────────────────────────── #
_BANK_CACHE: dict[str, tuple[dict[str, Clip], list[Item]]] = {}


def load_bank(char: str, lang: str = "en", lesson: str = "L1-S01-1") -> tuple[dict[str, Clip], list[Item]]:
    key = f"{char}_{lang}_{lesson}"
    if key in _BANK_CACHE:
        return _BANK_CACHE[key]
    d = ROOT / f"{char}_{lang}"
    clips: dict[str, Clip] = {}
    for c in json.loads((d / "manifest.json").read_text(encoding="utf-8")):
        with wave.open(str(d / f"{c['id']}.wav"), "rb") as w:
            assert w.getframerate() == 24000 and w.getsampwidth() == 2 and w.getnchannels() == 1, c["id"]
            face = c.get("face") or "neutral"
            if face == "neutral":
                face = FACE_BY_GROUP.get(c["id"].rsplit("_", 1)[0], "neutral")
            clips[c["id"]] = Clip(c["id"], c["text"], float(c["sec"]), w.readframes(w.getnframes()), face)
    data = json.loads((ROOT / f"{lesson}.json").read_text(encoding="utf-8"))
    items = [Item(x["seq"], x["surface"], x.get("en") or "", "◯" in x["surface"]) for x in data["items"]]
    _BANK_CACHE[key] = (clips, items)
    return clips, items


def fallback(kind: str, reason: str, **kw: Any) -> None:
    """⑤-③ 폴백 사유 한 줄(스위치가 켜졌을 때만 부른다)."""
    logger.info("normalcall clip-fallback: %s → Live · 사유=%s %s", kind, reason,
                " ".join(f"{k}={v}" for k, v in kw.items()))


def lesson_missing(lesson: Optional[str], char: str, lang: str = "en") -> Optional[str]:
    """이 차시 클립 묶음에 빠진 것(없으면 None) — 차시 파일 · 캐릭터 manifest · 항목별 ask_/model_ 클립."""
    if not lesson:
        return "차시 없음"
    if not (ROOT / f"{lesson}.json").exists():
        return f"차시 파일 없음({lesson})"
    d = ROOT / f"{char}_{lang}"
    if not (d / "manifest.json").exists():
        return f"캐릭터 자산 없음({char}_{lang})"
    ids = [c["id"] for c in json.loads((d / "manifest.json").read_text(encoding="utf-8"))]

    def has(prefix: str) -> bool:      # «model_3» 그대로 · «ask_3_a» 처럼 변형 접미가 붙은 것도
        return any(i == prefix or i.startswith(prefix + "_") for i in ids)
    seqs = [x["seq"] for x in json.loads((ROOT / f"{lesson}.json").read_text(encoding="utf-8"))["items"]]
    miss = [s for s in seqs if not has(f"ask_{s}") or not has(f"model_{s}")]
    return f"항목 클립 없음(seq {miss[:5]})" if miss else None


def enabled(settings: Any, *, call_type: Optional[str], voice: Optional[str], locale: Optional[str],
            lesson: Optional[str] = None) -> Optional[str]:
    """켤 캐릭터 이름(없으면 None). CLIP_EXPR 은 쉼표로 여러 캐릭터를 받는다.

    ⑤(PM-DEC-945) — 차시 단위 판정: 그 차시 클립 묶음(차시 파일·캐릭터 manifest·항목별 ask_/model_)이 다 있을 때만.
    숙제(homework)·다른 코스는 늘 None. 스위치가 켜진 통화의 폴백은 사유를 한 줄 남긴다.
    """
    want = {x.strip() for x in str(getattr(settings, "CLIP_EXPR", "") or "").split(",") if x.strip()}
    if not want:
        return None
    char = VOICE_BANK.get(voice or "")
    lang = (locale or "").split("-")[0]
    if call_type != "expression":
        fallback("clipexpr", "코스", call_type=call_type)
        return None
    if char not in want:
        fallback("clipexpr", "캐릭터", voice=voice)
        return None
    if lang != "en":
        fallback("clipexpr", "모국어", locale=locale)
        return None
    miss = lesson_missing(lesson, char, lang)
    if miss:
        fallback("clipexpr", miss, char=char)
        return None
    return char


def _rms(pcm: bytes) -> float:
    a = array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    if not a:
        return 0.0
    s = sum(x * x for x in a[::4])
    return math.sqrt(s / max(1, len(a[::4]))) / 32768.0


# ── 외부 호출(받아쓰기 · 텍스트 LLM · TTS) ────────────────────────────────── #
_SYNC_SPEECH: list = []          # PM-DEC-923 — 동기 SpeechClient 프로세스당 1개(스레드 안전)
RECOGNIZE_TIMEOUT_S = 8.0


def _sync_speech_client():
    if _SYNC_SPEECH:
        return _SYNC_SPEECH[0]
    from google.cloud import speech_v1
    from google.oauth2 import service_account

    from core import stt as _stt
    from core.config import settings as _s
    key = _s.STT_SA_KEY_FILE or _s.TTS_SA_KEY_FILE
    if _s.STT_FAKE or not key or not Path(key).is_file():
        return None
    creds = service_account.Credentials.from_service_account_file(str(key), scopes=_stt._SCOPES)
    _SYNC_SPEECH.append(speech_v1.SpeechClient(credentials=creds))
    return _SYNC_SPEECH[0]


def _recognize_sync(pcm16k: bytes, hints: list[str], alt_en: bool):
    """⛔ 작업 스레드 전용 — 비동기 클라이언트의 첫 호출은 인증 갱신을 루프 안에서 돌려 통화 전체를 멈췄다(00254 · 약 30초)."""
    from google.cloud import speech_v1
    client = _sync_speech_client()
    if client is None:
        raise RuntimeError("Speech 클라이언트 없음")
    cfg = speech_v1.RecognitionConfig(
        encoding=speech_v1.RecognitionConfig.AudioEncoding.LINEAR16, sample_rate_hertz=16000, language_code="ko-KR",
        alternative_language_codes=["en-US"] if alt_en else [],
        speech_contexts=[speech_v1.SpeechContext(phrases=[h for h in hints if h][:5], boost=10.0)] if any(hints) else [],
    )
    return client.recognize(config=cfg, audio=speech_v1.RecognitionAudio(content=pcm16k), timeout=RECOGNIZE_TIMEOUT_S)


async def recognize(pcm16k: bytes, hints: list[str], *, alt_en: bool = True) -> tuple[str, str]:
    """Google STT v1 recognize → (받아쓴 말, 언어 코드). PM-DEC-923 — 동기 클라이언트를 스레드에서(루프 무정지)."""
    resp = await asyncio.to_thread(_recognize_sync, pcm16k, hints, alt_en)
    texts, lang = [], "ko-kr"
    for r in resp.results:
        if r.alternatives:
            texts.append(r.alternatives[0].transcript)
            lang = (getattr(r, "language_code", "") or lang).lower()
    return " ".join(texts).strip(), lang


_SYNC_TTS: list = []             # PM-DEC-924 — 동기 TextToSpeechClient 프로세스당 1개(스레드 안전)
TTS_TIMEOUT_S = 10.0


def _sync_tts_client():
    if _SYNC_TTS:
        return _SYNC_TTS[0]
    from google.cloud import texttospeech
    from google.oauth2 import service_account

    from core.config import settings as _s
    key = _s.TTS_SA_KEY_FILE
    if not key or not Path(key).is_file():
        return None
    creds = service_account.Credentials.from_service_account_file(
        str(key), scopes=["https://www.googleapis.com/auth/cloud-platform"])
    _SYNC_TTS.append(texttospeech.TextToSpeechClient(credentials=creds))
    return _SYNC_TTS[0]


def _tts_sync(text: str, lang: str, voice: str) -> Optional[bytes]:
    """⛔ 작업 스레드 전용 — 비동기 gRPC 첫 호출의 루프 정지를 피한다(PM-DEC-923·924)."""
    from google.cloud import texttospeech

    from core import tts as _tts
    cli = _sync_tts_client()
    if cli is None:
        return None
    code, name = _tts._resolve_voice(lang, voice)
    resp = cli.synthesize_speech(
        input=texttospeech.SynthesisInput(text=text),
        voice=texttospeech.VoiceSelectionParams(language_code=code, name=name),
        audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.LINEAR16, sample_rate_hertz=24000),
        timeout=TTS_TIMEOUT_S,
    )
    return resp.audio_content or b""


async def tts_pcm(text: str, *, lang: str = "en", voice: str = "Fenrir") -> Optional[bytes]:
    """Cloud TTS(Chirp3-HD) → PCM16 24kHz(헤더 뗌). 키·클라이언트 없으면 None. PM-DEC-924 — 스레드에서."""
    pcm = await asyncio.to_thread(_tts_sync, text, lang, voice)
    if pcm is None:
        return None
    if pcm[:4] == b"RIFF":
        pcm = pcm[44:]
    return pcm or None


_TEXT_JUDGE_SI = """You judge one answer in a Korean speaking drill. The learner's words come from speech recognition (may be noisy).
Return result:
- correct: same meaning and form as the target (ignore spacing/punctuation; polite-ending variants of the same phrase are correct)
- close: almost (one word off, particle/ending differs)
- wrong: answered in Korean but a different phrase
- other: unrelated talk or a question, not an attempt at the target"""


class ClipExprSession:
    """클립 주도 표현학습 대리 세션(LiveSessionProtocol 흉내 + async CM)."""

    PERSIST = ("idx", "stage", "item_fails", "ask_n", "quiz_q", "done_items", "exc_used", "_last_in_group", "_recent")

    def __init__(self, factory: Callable, client: Any, settings: Any, factory_kwargs: dict, *, char: str,
                 state: Any, notify: Optional[Callable] = None, judge: Optional[Callable] = None,
                 request_close: Optional[Callable] = None, rng: Optional[random.Random] = None,
                 bank: Optional[tuple] = None, say: Optional[Callable] = None, lesson: Optional[str] = None) -> None:
        self._factory = factory                    # 쓰지 않는다(예외 턴도 Live 없음) — 시그니처만 맞춘다
        self._client = client
        self._settings = settings
        self.char = char
        self._state = state
        self._notify = notify
        self._judge_fn = judge                     # 시험용: async (pcm, ctx) -> (heard, result)
        self._say_fn = say                         # 시험용: async (guide, heard) -> (text, pcm|None)
        self._request_close = request_close
        self._rng = rng or random.Random()
        self.clips, self.items = bank if bank is not None else load_bank(char, lesson=lesson or "L1-S01-1")
        self._out: asyncio.Queue = asyncio.Queue()
        self._cmd: asyncio.Queue = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None
        self._opened = False
        self._closing = False
        self._restored = False
        # 듣기(VAD)
        self._listening = False
        self._speech = bytearray()
        self._voiced_s = 0.0
        self._silence_s = 0.0
        self._idle_s = 0.0
        self._t_last_voiced = 0.0
        self._play_until = 0.0
        self._spec: Optional[asyncio.Task] = None
        self._vad_th = float(getattr(settings, "LIVE_REGROUND_VOICE_RMS", 0.02) or 0.02)
        self._end_s = float(getattr(settings, "CLIP_EXPR_END_SILENCE_S", 0.8) or 0.8)
        self._dunno_s = float(getattr(settings, "CLIP_EXPR_DUNNO_S", 8.0) or 8.0)
        self._min_voiced_s = 0.2        # 이만큼 소리가 나야 말로 친다(짧은 잡음은 버린다)
        self._pre_gap_s = 0.0           # 말 문턱 전 첫 음절 뒤 틈(PM-DEC-913)
        self._model = getattr(settings, "CLIP_EXPR_JUDGE_MODEL", "") or "gemini-2.5-flash"
        # 진행
        self.idx = 0
        self.stage = "ask"
        self.item_fails = 0
        self.ask_n = 0
        self.quiz_q: list[int] = []
        self.done_items = 0
        self.exc_used = 0
        self._last_in_group: dict[str, str] = {}
        self._recent: list[str] = []
        self._judged = "-"
        # 계기
        self.judge_calls = 0
        self.llm_calls = 0
        self.judge_in = 0
        self.judge_out = 0
        self.judge_ms: list[int] = []
        self.latency_ms: list[int] = []
        self.spec_hits = 0
        self.stt_s = 0.0
        self.tts_chars = 0
        self.clip_count = 0
        self.turns = 0
        self._face_n = 0

    # ── 수명 ──────────────────────────────────────────────────────────────── #
    def _frag(self):
        return getattr(self._state, "fragment_index", None)

    async def __aenter__(self) -> "ClipExprSession":
        snap = _PROGRESS.get(self._cid()) if self._cid() is not None else None
        if not snap and self._cid() is not None and getattr(self._state, "fragment_index", 1) not in (None, 1):
            # 이어하기 조각인데 이 인스턴스 메모리에 없다 — 다른 인스턴스가 쓴 진행을 GCS 에서 읽는다
            snap = await asyncio.to_thread(_progress_load, self._cid())
        if snap:
            for k in self.PERSIST:
                if k in snap:
                    setattr(self, k, snap[k])
            self._restored = True
        self._worker = asyncio.create_task(self._run(), name="nc-clipexpr")
        logger.info("normalcall clipexpr 켬: call_id=%s 조각=%s 캐릭터=%s 항목 %d · 클립 %d · %s", self._cid(), self._frag(),
                    self.char, len(self.items), len(self.clips),
                    f"이어감(항목 {self.idx + 1} · 단계 {self.stage})" if self._restored else "처음부터")
        return self

    def _save(self) -> None:
        if self._cid() is None:
            return
        snap = {k: json.loads(json.dumps(getattr(self, k))) for k in self.PERSIST}
        _PROGRESS[self._cid()] = snap
        while len(_PROGRESS) > 300:
            _PROGRESS.pop(next(iter(_PROGRESS)))
        self._last_snap = snap

    async def __aexit__(self, *exc) -> None:
        snap = getattr(self, "_last_snap", None)
        if snap is not None and self._cid() is not None:
            try:
                await asyncio.to_thread(_progress_store, self._cid(), snap)   # 조각 끝 — 다음 조각이 어느 인스턴스로 가도 이어지게
            except Exception as exc:  # noqa: BLE001 - 저장 실패는 이 인스턴스 메모리로만 이어진다(종전과 같음)
                logger.warning("normalcall clipexpr: 진행 저장 실패 call_id=%s: %r", self._cid(), exc)
        for t in (self._worker, self._spec):
            if t is not None:
                t.cancel()
                with contextlib.suppress(BaseException):
                    await t
        self._save()
        lat = sorted(self.latency_ms)
        jm = sorted(self.judge_ms)
        logger.info("normalcall clipexpr 요약: call_id=%s 조각=%s judge_model=%s turns=%d clips=%d 항목완료=%d/%d judge_calls=%d "
                    "llm_calls=%d judge_in=%d judge_out=%d judge_p50_ms=%s spec_hits=%d stt_s=%.1f latency_p50_ms=%s "
                    "latency_max_ms=%s exc=%d",
                    self._cid(), self._frag(), self._model, self.turns, self.clip_count, self.done_items, len(self.items),
                    self.judge_calls, self.llm_calls, self.judge_in, self.judge_out, jm[len(jm) // 2] if jm else None,
                    self.spec_hits, self.stt_s, lat[len(lat) // 2] if lat else None, lat[-1] if lat else None, self.exc_used)
        # 원가 — Live 0 이라 「normalcall usage」 줄(형식 고정 · Live 전용)이 비어 있다. 이 조각의 클립 경로 원가를 따로 한 줄로 낸다.
        #   텍스트 LLM(판정·예외 문장) 토큰 × 단가 + STT 초 × 단가 + TTS 글자 × 단가. 단가는 env 로 바꾼다(공시 단가 기준값).
        st = self._settings
        usd = (self.judge_in * float(getattr(st, "CLIP_USD_TEXT_IN_PER_M", 0.30)) / 1e6
               + self.judge_out * float(getattr(st, "CLIP_USD_TEXT_OUT_PER_M", 2.50)) / 1e6
               + self.stt_s / 60.0 * float(getattr(st, "CLIP_USD_STT_PER_MIN", 0.024))
               + self.tts_chars * float(getattr(st, "CLIP_USD_TTS_PER_M_CHARS", 30.0)) / 1e6)
        logger.info("normalcall clipexpr 원가: call_id=%s 조각=%s judge_model=%s text_in=%d text_out=%d stt_s=%.1f tts_chars=%d "
                    "clip_usd=%.5f", self._cid(), self._frag(), self._model, self.judge_in, self.judge_out, self.stt_s,
                    self.tts_chars, usd)

    def _cid(self):
        return getattr(self._state, "call_id", None)

    # ── LiveSessionProtocol ─────────────────────────────────────────────── #
    async def events(self) -> AsyncIterator:
        while True:
            ev = await self._out.get()
            if ev is None:
                return
            yield ev

    async def send_audio(self, pcm16_16k: bytes) -> None:
        if not self._listening or not pcm16_16k:
            return
        if not self._voiced_s and time.monotonic() < self._play_until:
            # 앱이 아직 비버 소리를 트는 중(추정) — 그 사이 말은 이전 시도의 꼬리다(call 2040·2041 반쪽 판정). 버린다.
            return
        dur = len(pcm16_16k) / _MIC_BPS
        loud = _rms(pcm16_16k) >= self._vad_th
        if loud:
            self._voiced_s += dur
            self._silence_s = 0.0
            self._pre_gap_s = 0.0
            self._t_last_voiced = time.monotonic()
            self._speech.extend(pcm16_16k)
            if self._spec is not None:          # 말이 이어졌다 — 미리 한 판정은 버린다
                self._spec.cancel()
                self._spec = None
            return
        if self._voiced_s >= self._min_voiced_s:
            self._speech.extend(pcm16_16k)
            self._silence_s += dur
            if self._spec is None and self._silence_s >= SPEC_SILENCE_S:
                self._spec = asyncio.create_task(self._analyze(bytes(self._speech)), name="nc-clipexpr-spec")
            if self._silence_s >= self._end_s or len(self._speech) >= _MIC_BPS * 12:
                self._listening = False
                spec, self._spec = self._spec, None
                self._cmd.put_nowait(("utt", bytes(self._speech), self._t_last_voiced, spec))
            return
        # 아직 말 시작 전 — 짧은 잡음은 버리고 무응답 시간을 센다(재생 중 프레임은 위에서 이미 버렸다)
        if self._voiced_s > 0 and self._pre_gap_s + dur < PRE_GAP_S:
            # PM-DEC-913 — 말 문턱 전 첫 음절 뒤 짧은 틈(0.3초 미만)은 버리지 않는다(call 2125 「채는…」「가 아주…」 머리 잘림).
            self._pre_gap_s += dur
            self._speech.extend(pcm16_16k)
            return
        self._pre_gap_s = 0.0
        self._speech.clear()
        self._voiced_s = 0.0
        self._idle_s += dur
        if self._idle_s >= self._dunno_s:
            self._listening = False
            self._cmd.put_nowait(("silent", b"", time.monotonic(), None))

    async def send_text_turn(self, text: str) -> None:
        if not self._opened:
            self._opened = True
            self._cmd.put_nowait(("open",))
        elif text == getattr(self._state, "close_seed", None):
            self._cmd.put_nowait(("close",))
        else:
            self._cmd.put_nowait(("nudge",))      # 무음 넛지·재개 시드 — 지금 문제를 다시 낸다

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        return None                               # 쪽지·안내는 Live 지시용이다 — 클립 통화는 쓰지 않는다

    async def send_persona(self, text: str) -> None:
        return None

    async def send_tool_response(self, fn_id, fn_name, **kw) -> None:
        return None                               # 표정은 auto_acked 로 보낸다

    # ── 일꾼 ──────────────────────────────────────────────────────────────── #
    async def _run(self) -> None:
        while True:
            cmd = await self._cmd.get()
            try:
                await self._handle(cmd)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 한 턴 실패로 통화를 죽이지 않는다 — 지금 문제를 다시 낸다
                logger.warning("normalcall clipexpr: 턴 처리 실패 — 다시 출제: %r", exc)
                await self._turn([self._ask_id()], result="error")

    async def _handle(self, cmd: tuple) -> None:
        kind = cmd[0]
        if kind == "open":
            await self._on_open()
            return
        if kind == "close":
            self._closing = True
            await self._turn([self._pick(self.CLOSE_GROUP)], result="close")
            return
        if kind == "nudge":
            await self._turn([self._ask_id()], result="nudge")
            return
        pcm, t_end, spec = cmd[1], cmd[2], cmd[3]
        if kind == "silent":
            heard, result = "", "dunno"
        elif spec is not None:
            try:
                heard, result = await spec
                self.spec_hits += 1
            except asyncio.CancelledError:
                heard, result = await self._analyze(pcm)
        else:
            heard, result = await self._analyze(pcm)
        await self._out.put(LiveEvent(kind="in_tr", text=heard or "…", is_final=True))
        self._judged = f"{self._judged_label()}"
        await self._on_answer(heard, result, t_end)

    CLOSE_GROUP = "g_close"

    def _judged_label(self) -> str:
        if self.stage == "quiz" and self.quiz_q:
            return f"퀴즈{self.quiz_q[0]}"
        return f"{self.item.seq}/{self.stage}" if self.idx < len(self.items) else "-"

    def _finished_resume(self) -> bool:
        """다 끝낸 통화에 조각 이어하기로 다시 붙었다 — 인사도 문제도 없이 바로 마무리한다(call 2040 조각2 «Again» 반복)."""
        if not (self._restored and self.stage == "done"):
            return False
        logger.info("normalcall clipexpr: 끝난 진행에 이어하기 — 바로 마무리 call_id=%s 조각=%s", self._cid(), self._frag())
        if self._state is not None and getattr(self._state, "call_start_ts", None) is None:
            # PM-DEC-892 — 비버 턴이 하나도 없으면 통화 시계가 안 서서 종료 워처가 작별 시드를 영영 안 넣었다(call 2075 조각3 · 112초 침묵).
            self._state.call_start_ts = asyncio.get_running_loop().time()
        if self._request_close is not None:
            self._request_close()
        return True

    async def _on_open(self) -> None:
        if self._finished_resume():
            return
        if self._restored:
            await self._turn([self._ask_id()], result="resume")       # 조각 이어하기 — 지금 문제를 그대로 다시
            return
        await self._turn([self._pick("g_open"), self._ask_id()], result="open")

    # ── 진행 틀(요청서 3-1) ──────────────────────────────────────────────── #
    @property
    def item(self) -> Item:
        return self.items[self.idx]

    def _ask_id(self) -> str:
        if self.stage == "quiz" and self.quiz_q:
            return f"quiz_{self.quiz_q[0]}"
        if self.idx >= len(self.items):
            return self._pick("g_back")
        seq = self.item.seq
        if self.stage == "rep" and self._has(f"rep_{seq}"):
            return f"model_{seq}"
        if self.stage == "rep2":
            return f"rep_{seq}" if self._has(f"rep_{seq}") else f"model_{seq}"
        if self.stage in ("native", "native2") and self._has(f"native_{seq}"):
            return f"native_{seq}"
        return f"ask_{seq}_{1 + self.ask_n % 2}"

    def _has(self, cid: str) -> bool:
        return cid in self.clips

    def _pick(self, group: str) -> str:
        ids = sorted(c for c in self.clips if c.rsplit("_", 1)[0] == group)
        last = self._last_in_group.get(group)
        pool = [c for c in ids if c != last] or ids
        cid = self._rng.choice(pool)
        self._last_in_group[group] = cid
        return cid

    def _wrong(self, *, laugh: bool = True) -> list[str]:
        # PM-DEC-1019 — 서버 반응(웃음)이 이미 나갔으면 웃음 클립을 또 붙이지 않는다.
        seq = [self._pick("g_laugh")] if laugh and self._rng.random() < LAUGH_P else []
        return seq + [self._pick("g_wrong")]

    def _next_item_clips(self) -> list[str]:
        """지금 항목을 끝내고 다음 출제(또는 퀴즈·마무리) 클립."""
        self.done_items += 1
        if self.done_items % 3 == 0:
            self.quiz_q = [it.seq for it in self.items[self.idx - 2:self.idx + 1]]
        self.idx += 1
        self.item_fails = 0
        self.ask_n += 1
        if self.quiz_q:
            self.stage = "quiz"
            return [self._pick("g_quiz_start"), f"quiz_{self.quiz_q[0]}"]
        if self.idx >= len(self.items):
            return self._finish()
        self.stage = "ask"
        return [self._ask_id()]

    def _finish(self) -> list[str]:
        self.stage = "done"
        if self._request_close is not None:
            self._request_close()
        return []

    def _after_quiz(self) -> list[str]:
        self.quiz_q.pop(0)
        if self.quiz_q:
            return [f"quiz_{self.quiz_q[0]}"]
        if self.idx >= len(self.items):
            return self._finish()
        self.stage = "ask"
        return [self._ask_id()]

    async def _on_answer(self, heard: str, r: str, t_end: float) -> None:
        st = self.stage
        if st == "quiz" and self.quiz_q:
            # 퀴즈는 정답 판정만(요청서 3-1 ⑤) — 마지막 퀴즈는 idx 가 이미 항목 끝을 넘었다
            lead = [self._pick("g_ok")] if r == "correct" else [f"model_{self.quiz_q[0]}"]
            rx = "correct" if r == "correct" else ("wrong" if r in ("close", "wrong") else None)
            await self._turn(lead + self._after_quiz(), result=r, heard=heard, t_end=t_end, react=rx)
            return
        it = self.item if self.idx < len(self.items) else None
        if st == "done" or it is None:
            await self._turn([self._pick("g_back")], result=r, heard=heard, t_end=t_end)
            return
        seq = it.seq
        if r in ("other", "native_lang"):
            # 딴말·영어 답 — 클립(g_back «Back to Korean» + 지금 문제)으로 바로 받는다. TTS 응답은 2.5~3.9초라(call 2043) 뺐다.
            await self._turn([self._pick("g_back"), self._ask_id()], result=r, heard=heard, t_end=t_end)
            return
        if r == "correct":
            if st in ("ask", "hint") and it.blank:
                # 빈칸 정답 — STT 를 믿기 어려워 되짚지 않는다(«오이에에요» 놀림 · call 2034). 칭찬 클립만.
                await self._turn([self._pick("g_ok")] + self._next_item_clips(), result=r, heard=heard, t_end=t_end,
                                 react="correct")
                return
            if st in ("ask", "hint", "rep", "rep2") and self._has(f"native_{seq}"):
                lead = self._pick("g_ok") if st in ("ask", "hint") else self._pick("g_rep_ok")
                self.stage = "native"
                await self._turn([lead, f"native_{seq}"], result=r, heard=heard, t_end=t_end, react="correct")
                return
            lead = self._pick("g_ok") if st in ("ask", "hint") else self._pick("g_rep_ok")
            await self._turn([lead] + self._next_item_clips(), result=r, heard=heard, t_end=t_end, react="correct")
            return
        # 실패(close·wrong·dunno)
        if st in ("native", "native2"):
            if st == "native":
                self.stage = "native2"
                await self._turn([self._pick("g_rep_again"), f"native_{seq}"], result=r, heard=heard, t_end=t_end)
            else:
                await self._turn([self._pick("g_move")] + self._next_item_clips(), result=r, heard=heard, t_end=t_end)
            return
        self.item_fails += 1
        if self.item_fails >= 3:
            model = [f"model_{seq}"]
            nxt = self._next_item_clips()
            await self._exception_or_back(
                "They failed this phrase three times. In one or two short, kind English sentences, explain the phrase "
                f"«{it.surface}» (meaning: {it.en}) simply — do not write any Korean; the answer will be played right after.",
                heard, model + nxt, r, t_end, fallback=[self._pick("g_move")])
            return
        if st == "ask" and r == "close" and self._has(f"hint_{seq}"):
            self.stage = "hint"
            rx = await self._react("wrong")
            await self._turn(self._wrong(laugh=not rx) + [f"hint_{seq}"], result=r, heard=heard, t_end=t_end)
            return
        if st in ("ask", "hint"):
            if r == "dunno":
                lead = [self._pick("g_dunno")]
            else:
                rx = await self._react("wrong")
                lead = self._wrong(laugh=not rx)
            self.stage = "rep"
            await self._turn(lead + [f"model_{seq}"], result=r, heard=heard, t_end=t_end)
            return
        if st == "rep":
            self.stage = "rep2"
            again = f"rep_{seq}" if self._has(f"rep_{seq}") else f"model_{seq}"
            await self._turn([self._pick("g_rep_again"), again], result=r, heard=heard, t_end=t_end)
            return
        await self._turn([self._pick("g_move")] + self._next_item_clips(), result=r, heard=heard, t_end=t_end)

    async def _exception_or_back(self, guide: str, heard: str, tail: list[str], r: str, t_end: float,
                                 fallback: Optional[list[str]] = None) -> None:
        if self.exc_used >= EXC_CAP:
            lead = fallback if fallback is not None else [self._pick("g_back")]
            await self._turn(lead + tail, result=r, heard=heard, t_end=t_end, note="예외상한")
            return
        self.exc_used += 1
        await self._turn(tail, result=r, heard=heard, t_end=t_end, say_guide=guide,
                         fallback=fallback if fallback is not None else [self._pick("g_back")])

    # ── 판정 ──────────────────────────────────────────────────────────────── #
    def _targets(self) -> tuple[list[str], bool, str]:
        st = self.stage
        if st == "quiz" and self.quiz_q:
            q = next(x for x in self.items if x.seq == self.quiz_q[0])
            return [q.surface], q.blank, st
        it = self.items[min(self.idx, len(self.items) - 1)]
        if st in ("native", "native2") and self._has(f"native_{it.seq}"):
            t = self.clips[f"native_{it.seq}"].text
            nat = t.split('"')[1] if t.count('"') >= 2 else ""
            return [nat or it.surface], False, st
        return [it.surface], it.blank, st

    async def _analyze(self, pcm: bytes) -> tuple[str, str]:
        """받아쓰기 → 규칙 → (필요하면) 텍스트 LLM. 미리 판정(spec)도 이 함수를 쓴다(상태를 바꾸지 않는다)."""
        targets, blank, st = self._targets()
        t0 = time.monotonic()
        if self._judge_fn is not None:
            heard, result = await self._judge_fn(pcm, {"targets": targets, "blank": blank, "stage": st})
        else:
            heard, result = await self._analyze_real(pcm, targets, blank, st)
        self.judge_calls += 1
        self.judge_ms.append(int((time.monotonic() - t0) * 1000))
        if result not in RESULTS:
            result = "wrong"
        if st in ("rep", "rep2", "native", "native2") and result == "close":
            result = "correct"                    # 따라 말하기는 근접이면 통과
        return heard, result

    async def _analyze_real(self, pcm: bytes, targets: list[str], blank: bool, st: str) -> tuple[str, str]:
        self.stt_s += len(pcm) / _MIC_BPS
        try:
            heard, lang = await recognize(pcm, targets)
        except Exception as exc:  # noqa: BLE001 - 받아쓰기 실패는 «모름»으로(다시 들려준다)
            logger.warning("normalcall clipexpr: STT 실패(모름으로): %r", exc)
            return "", "dunno"
        result, sim = rule_judge(heard, targets, blank=blank)
        how = "규칙"
        if result is None:
            how = "LLM"
            result = await self._text_judge(heard, targets[0], st)
        logger.info("normalcall clipexpr judge: call_id=%s 조각=%s 단계=%s 목표=«%s» 들음=«%s» 언어=%s 자모=%.2f 판정=%s(%s)",
                    self._cid(), self._frag(), st, targets[0], heard[:60], lang, sim, result, how)
        return heard, result

    def _add_usage(self, resp: Any) -> None:
        um = getattr(resp, "usage_metadata", None)
        self.judge_in += int(getattr(um, "prompt_token_count", 0) or 0)
        self.judge_out += int(getattr(um, "candidates_token_count", 0) or 0)
        su = getattr(self._state, "sidecar_usage", None)
        if su is not None:
            with contextlib.suppress(Exception):
                su.add_response(self._model, resp)   # 원가 계기판(normalcall usage)에 합산

    async def _text_judge(self, heard: str, target: str, st: str) -> str:
        from google.genai import types
        self.llm_calls += 1
        try:
            resp = await self._client.aio.models.generate_content(
                model=self._model,
                contents=f"Stage: {st}\nTarget phrase: {target}\nLearner said (STT): {heard}",
                config=types.GenerateContentConfig(
                    system_instruction=_TEXT_JUDGE_SI, temperature=0, response_mime_type="application/json",
                    response_schema=TextJudgeOut, thinking_config=types.ThinkingConfig(thinking_budget=0),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
            )
            self._add_usage(resp)
            out = TextJudgeOut.model_validate_json(resp.text or "{}")
            return out.result if out.result in ("correct", "close", "wrong", "other") else "wrong"
        except Exception as exc:  # noqa: BLE001 - 판정 불가는 오답으로(통화는 이어간다)
            logger.warning("normalcall clipexpr: 텍스트 판정 실패(오답으로): %r", exc)
            return "wrong"

    async def _say(self, guide: str, heard: str) -> tuple[str, Optional[bytes]]:
        """예외 턴 — 텍스트 LLM 영어 한두 문장 + Cloud TTS(같은 Fenrir)."""
        if self._say_fn is not None:
            return await self._say_fn(guide, heard)
        from google.genai import types
        self.llm_calls += 1
        it = self.items[min(self.idx, len(self.items) - 1)]
        si = (f"{PERSONA.get(self.char, '')}\nThe learner speaks English and is practicing Korean. Current phrase: «{it.surface}» "
              f"(meaning: {it.en}). Reply in English only, one or two short sentences, no Korean, no questions except asking "
              "them to try again. Do not tease what speech recognition heard.")
        recent = "\n".join(self._recent[-2:])
        resp = await self._client.aio.models.generate_content(
            model=self._model, contents=f"Recent:\n{recent}\nLearner just said (STT): {heard or '(nothing)'}\nTask: {guide}",
            config=types.GenerateContentConfig(system_instruction=si, temperature=0.7, max_output_tokens=80,
                                               thinking_config=types.ThinkingConfig(thinking_budget=0),
                                               automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
        )
        self._add_usage(resp)
        text = re.sub(r"[가-힣]+", "", (resp.text or "").strip()).strip()
        if not text:
            return "", None
        self.tts_chars += len(text)
        return text, await tts_pcm(text, lang="en", voice=CHAR_VOICE.get(self.char, "Fenrir"))   # PM-DEC-890 — 캐릭터 목소리

    # ── 내보내기 ─────────────────────────────────────────────────────────── #
    async def _emit_pcm(self, pcm: bytes, text: str, face: str, face_now: str) -> str:
        if face != face_now:
            # 영상통화 표정 — Live 의 set_face 와 같은 길(서버가 sentence 마커로 앱에 보낸다). 응답은 필요 없다(auto_acked).
            self._face_n += 1
            await self._out.put(LiveEvent(kind="tool_call", fn_name="set_face", fn_id=f"clip-face-{self._face_n}",
                                          fn_args={"emotion": face}, auto_acked=True))
        await self._out.put(LiveEvent(kind="out_tr", text=text))
        for i in range(0, len(pcm), _CHUNK):
            await self._out.put(LiveEvent(kind="audio", audio=pcm[i:i + _CHUNK]))
        now = time.monotonic()
        self._play_until = max(self._play_until, now) + len(pcm) / _OUT_BPS
        return face

    async def _react(self, kind: str) -> int:
        """PM-DEC-1019 — 판정 반응. 앱이 앞 소리를 다 튼 뒤에 보내고 그 길이만큼 기다린다(그동안 비버 클립이 안 나간다 = 클립 통화의 답 보류).
        발동 안 하면 0 이고 기다리지도 않는다. 기다리는 동안 듣지 않는다(_listening 은 판정 뒤 이미 꺼져 있다)."""
        if _reaction.gate(self._state) is None:
            return 0
        lead = self._play_until - time.monotonic()
        if lead > 0:
            await asyncio.sleep(lead)
        ms = await _reaction.fire(self._state, kind, getattr(self._state, "rx_send", None), where=f"clip:{self._judged}")
        if ms:
            await asyncio.sleep(ms / 1000.0)
        return ms

    async def _turn(self, clip_ids: list[str], *, result: str, heard: str = "", t_end: Optional[float] = None,
                    say_guide: Optional[str] = None, note: str = "", fallback: Optional[list[str]] = None,
                    react: Optional[str] = None) -> None:
        """비버 턴 하나 = [반응] + [예외 응답(TTS)] + 클립 순서열 + turn_end. 끝나면 다시 듣는다."""
        if react:
            await self._react(react)
        self.turns += 1
        first_sound: Optional[float] = None
        face_now = "neutral"
        said = ""
        if say_guide:
            t_say = time.monotonic()
            pcm: Optional[bytes] = None
            try:
                said, pcm = await self._say(say_guide, heard)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 예외 응답 실패는 클립으로 대신한다
                logger.warning("normalcall clipexpr: 예외 응답 실패 — 클립으로 대신: %r", exc)
            if pcm:
                face_now = await self._emit_pcm(pcm, said, "neutral", face_now)
                first_sound = time.monotonic()
            else:
                clip_ids = list(fallback or [self._pick("g_back")]) + clip_ids
                said = ""
            logger.info("normalcall clipexpr say: call_id=%s 조각=%s 답=%s 판정=%s 응답=«%s» 걸림=%.2fs",
                        self._cid(), self._frag(), self._judged, result, said[:80], time.monotonic() - t_say)
        for cid in clip_ids:
            c = self.clips.get(cid)
            if c is None:
                logger.warning("normalcall clipexpr: 클립 없음 %s(건너뜀)", cid)
                continue
            self._on_clip_start(cid, c, max(0.0, self._play_until - time.monotonic()))   # PM-DEC-893 — 재생 시작까지 남은 초
            face_now = await self._emit_pcm(c.pcm, (" " if first_sound else "") + c.text, c.face, face_now)
            if first_sound is None:
                first_sound = time.monotonic()
            self.clip_count += 1
            t_rel = self._t_rel()
            logger.info("normalcall clipexpr clip: call_id=%s 조각=%s t=%.1f id=%s 답=%s 판정=%s 들음=«%s» 다음=%s 초=%.2f 표정=%s",
                        self._cid(), self._frag(), t_rel, cid, self._judged, result, heard[:60], self._judged_label(), c.sec,
                        c.face)
            if self._notify is not None:
                with contextlib.suppress(Exception):
                    await self._notify({"type": "clip", "id": cid, "result": result, "answered": self._judged,
                                        "next": self._judged_label(), "face": c.face, "fragment": self._frag(),
                                        "t": round(t_rel, 2)})
        lat = None
        if t_end is not None and first_sound is not None:
            lat = int((first_sound - t_end) * 1000)
            self.latency_ms.append(lat)
        logger.info("normalcall clipexpr turn: call_id=%s 조각=%s 답=%s 판정=%s 다음=%s 클립=%s 말끝→첫소리_ms=%s 예외=%s%s",
                    self._cid(), self._frag(), self._judged, result, self._judged_label(), ",".join(clip_ids) or "-", lat,
                    "say" if say_guide else "-", f" ({note})" if note else "")
        spoken = " ".join(x for x in [said] + [self.clips[c].text for c in clip_ids if c in self.clips] if x)
        self._recent = (self._recent + ([f"Learner: {heard}"] if heard else []) + [f"{self.char}: {spoken}"])[-4:]
        self._judged = "-"
        self._save()
        self._on_turn_audio_end(max(0.0, self._play_until - time.monotonic()))
        await self._out.put(LiveEvent(kind="turn_end"))
        if self._closing:
            return
        self._reset_listen()

    def _on_clip_start(self, cid: str, clip: "Clip", delay_s: float) -> None:
        """클립 하나가 앱에서 재생되기 시작할 때(지금부터 delay_s 뒤) — 표현학습은 쓰지 않는다(PM-DEC-893)."""

    def _on_turn_audio_end(self, delay_s: float) -> None:
        """이 턴 소리가 앱에서 다 재생될 때(지금부터 delay_s 뒤)."""

    def _t_rel(self) -> float:
        t0 = getattr(self._state, "call_start_ts", None)
        return (asyncio.get_running_loop().time() - t0) if t0 else 0.0

    def _reset_listen(self) -> None:
        self._speech.clear()
        self._voiced_s = self._silence_s = self._idle_s = self._pre_gap_s = 0.0
        self._listening = True
