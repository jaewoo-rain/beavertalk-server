"""캐스케이드 P1 — LLM → 문장 분할 → TTS → 비버 송출, 그리고 barge-in 취소.

여기서 고정하는 것:
  ① 문장 분할(첫 소리를 앞당기는 장치) — 종결부호·최소·최대 길이
  ② 문장 이름표는 **마지막 오디오 조각**에 붙는다(원장 절단이 걸친 문장을 버리게 하려면)
  ③ 사용자 발화 1건 → 비버 턴 1건(turn_start → 오디오 → turn_end), 이력·원가가 함께 남는다
  ④ ⛔ 빈 전사로는 LLM 을 부르지 않는다(원가만 나가고 헛대답)
  ⑤ barge-in 취소: audio_cancel 을 내고 **turn_end 는 내지 않는다**(I4), 세션은 살아 있고,
     못 들려준 글자가 원가에 남는다(합성했으면 이미 과금됐다)
"""

import asyncio

import pytest

import core.stt as stt_mod
from core.stt import SPEECH_BEGIN, SPEECH_END
from domains.learning.realtime import cascade_session as cs
from domains.learning.realtime.cascade_reply import SentenceBuffer, speak_stream
from domains.learning.realtime.cascade_session import CascadeInbound, CascadeSession

_FRAME = b"\x00\x01" * 240   # 10ms PCM24k


# --------------------------------------------------------------------------- #
# ① 문장 분할
# --------------------------------------------------------------------------- #
def test_sentence_buffer_splits_on_terminator():
    buf = SentenceBuffer()
    assert buf.push("안녕하세요 반가워요. ") == ["안녕하세요 반가워요."]
    assert buf.push("오늘 뭐 하셨") == []          # 아직 문장이 안 끝났다
    assert buf.push("어요? 저는요") == ["오늘 뭐 하셨어요?"]
    assert buf.flush() == "저는요"                 # 종결부호가 없어도 남은 말은 내보낸다


def test_sentence_buffer_keeps_short_fragments_together():
    """너무 잘게 쪼개면 TTS 요청이 늘고 운율이 끊긴다 — 최소 길이 전에는 안 끊는다."""
    buf = SentenceBuffer()
    assert buf.push("네. ") == []                       # 맞장구만으로는 안 끊는다
    assert buf.push("그럼 내일 봐요. ") == ["네. 그럼 내일 봐요."]


def test_sentence_buffer_caps_runaway_output():
    """모델이 종결부호 없이 폭주해도 상한에서 끊는다(첫 소리가 영영 안 나오면 안 된다)."""
    buf = SentenceBuffer()
    out = buf.push("가 " * 200)
    assert out and all(len(s) <= 121 for s in out)


# --------------------------------------------------------------------------- #
# ② 문장 이름표 위치
# --------------------------------------------------------------------------- #
class _RecordingBeaver:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send(self, pcm: bytes, text: str = "") -> None:
        self.sent.append((len(pcm), text))


@pytest.mark.asyncio
async def test_sentence_label_goes_on_the_last_chunk():
    """이름표가 앞 조각에 붙으면, 문장을 다 듣기 전에 이력에 남는다 — 못 들은 말을 들었다고 치게 된다."""
    async def chunks():
        for _ in range(3):
            yield _FRAME

    beaver = _RecordingBeaver()
    sent = await speak_stream(beaver, chunks(), "안녕하세요")
    assert sent == len(_FRAME) * 3
    assert [t for _, t in beaver.sent] == ["", "", "안녕하세요"]


# --------------------------------------------------------------------------- #
# ③~⑤ 세션 통합 (LLM·TTS 는 페이크 — 크레덴셜·과금 0)
# --------------------------------------------------------------------------- #
class _FakeChat:
    """ChatStream 대역 — 정해진 조각을 흘리고 usage 를 남긴다."""

    def __init__(self, pieces, usage=None) -> None:
        self._pieces = pieces
        self.text = ""
        self.usage_metadata = usage
        self.failed = False

    async def chunks(self):
        for piece in self._pieces:
            self.text += piece
            yield piece
            await asyncio.sleep(0)


class _Usage:
    prompt_token_count = 120
    candidates_token_count = 30
    thoughts_token_count = 0
    cached_content_token_count = 0
    total_token_count = 150


class _Transport:
    """스크립트를 순서대로 내주고 나간 것을 모은다. 기다리는 이벤트가 나오면 stop."""

    def __init__(self, scripted, wait_for="turn_end") -> None:
        self._scripted = list(scripted)
        self.events: list[dict] = []
        self.audio = 0
        self._wait_for = wait_for
        self._done = asyncio.Event()

    async def send_event(self, event: dict) -> None:
        self.events.append(event)
        if event.get("type") == self._wait_for:
            self._done.set()

    async def send_audio(self, frame: bytes) -> None:
        self.audio += len(frame)

    async def receive(self) -> CascadeInbound:
        if self._scripted:
            item = self._scripted.pop(0)
            if isinstance(item, float):
                await asyncio.sleep(item)
                return await self.receive()
            return item
        await self._done.wait()
        return CascadeInbound(kind="control", control={"type": "stop"})

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]

    def first(self, type_: str) -> dict | None:
        return next((e for e in self.events if e.get("type") == type_), None)


def _ctl(**kwargs) -> CascadeInbound:
    return CascadeInbound(kind="control", control=kwargs)


@pytest.fixture
def reply_rig(monkeypatch):
    """페이크 STT + 페이크 LLM + 페이크 TTS. 대답 배관만 검사한다."""
    monkeypatch.setattr(stt_mod.settings, "STT_V2_FAKE", True)
    monkeypatch.setattr(stt_mod.settings, "CASCADE_TURN_SILENCE_MS", 60)
    monkeypatch.setattr(cs.settings, "CASCADE_TURN_MIN_WAIT_MS", 20)
    # 선톡은 기본 ON 이지만, 아래 테스트들은 **사용자 발화에 대한 대답**을 본다.
    # 선톡이 먼저 돌면 그 대답이 첫 비버 턴이 되어 검사 대상이 흐려진다 — 선톡 자체는
    # 전용 테스트(test_greeting_speaks_first)에서 본다.
    monkeypatch.setattr(cs.settings, "CASCADE_GREETING", False)
    stt_mod.get_speech_v2_client.cache_clear()

    state = {"chat": None, "tts_calls": [], "chunk_delay": 0.0, "chunks": 2}

    def _open(client, model, **kwargs):
        state["chat"] = _FakeChat(["안녕하세요 반갑습니다. ", "오늘 기분은 어떠세요?"], _Usage())
        state["system"] = kwargs.get("system_instruction", "")
        state["thinking"] = kwargs.get("thinking_budget", "미지정")
        state["history"] = list(kwargs.get("history") or [])
        state["history_or_seed"] = kwargs.get("user_text", "")
        return state["chat"]

    async def _tts(text, **kwargs):
        state["tts_calls"].append(text)

        async def _gen():
            for _ in range(state["chunks"]):
                await asyncio.sleep(state["chunk_delay"])
                yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.gemini_chat, "open_chat_stream", _open)
    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    yield state
    stt_mod.get_speech_v2_client.cache_clear()


@pytest.mark.asyncio
async def test_user_turn_produces_one_beaver_turn(reply_rig):
    """사용자 발화 1건 → 비버 턴 1건. 오디오는 turn_start 뒤에, turn_end 는 마지막 바이트 뒤에."""
    transport = _Transport([
        _ctl(type="start"),
        _ctl(type="__test_event", event=SPEECH_BEGIN),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    types = transport.types()
    assert types.count("turn_start") == 1, transport.events   # 비버 턴
    assert types.count("turn_end") == 1, transport.events
    assert types.index("turn_start") < types.index("turn_end")
    assert transport.audio > 0                                 # 실제로 소리가 나갔다
    # 문장 2개가 각각 합성됐다(첫 문장이 끝나는 즉시 흘린다 = 첫 소리 앞당기기)
    assert reply_rig["tts_calls"] == ["안녕하세요 반갑습니다.", "오늘 기분은 어떠세요?"]
    # ⭐ 추론 토큰은 끈다(원가·첫 소리 둘 다 손해)
    assert reply_rig["thinking"] == 0
    # 이력: 사용자 발화 + 비버가 실제로 한 말
    assert session._history[0]["role"] == "user" and session._history[0]["text"] == "안녕"
    assert session._history[1]["role"] == "model"
    assert "오늘 기분은 어떠세요?" in session._history[1]["text"]


@pytest.mark.asyncio
async def test_usage_records_llm_tokens_and_tts_chars(reply_rig):
    """원가 3구간이 한 번의 대답으로 모두 채워진다(STT 는 세션 종료 시)."""
    transport = _Transport([
        _ctl(type="start"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    summary = session.usage.summary()
    assert summary["in_text"] == 120 and summary["out_text"] == 30
    assert (summary["in_audio"], summary["out_audio"]) == (0, 0)   # 캐스케이드 LLM 은 오디오를 안 받는다
    tts = summary["vendors"]["tts"]
    assert tts["calls"] == 2
    assert tts["chars"] == len("안녕하세요 반갑습니다.") + len("오늘 기분은 어떠세요?")
    assert summary["engine"].startswith("cascade:")
    assert "gemini" in summary["engine"] and "cloud-tts" in summary["engine"]


@pytest.mark.asyncio
async def test_empty_transcript_never_calls_llm(reply_rig):
    """⛔ 빈 턴으로 LLM 을 부르면 원가만 나가고 비버가 헛대답을 한다(결함 C 판단)."""
    transport = _Transport([
        _ctl(type="start"),
        _ctl(type="__test_event", event=SPEECH_BEGIN),
        _ctl(type="__test_event", event=SPEECH_END),
    ], wait_for="user_turn_end")
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    assert reply_rig["chat"] is None                 # LLM 호출 자체가 없다
    assert reply_rig["tts_calls"] == []
    assert "turn_start" not in transport.types()     # 비버 턴도 없다


@pytest.mark.asyncio
async def test_greeting_speaks_first(reply_rig, monkeypatch):
    """⭐ 선톡 — 사용자가 한 마디도 안 했는데 비버가 먼저 말한다.

    안 하면 둘 다 서로 말하기를 기다려 통화가 조용히 멈춘다(Live 도 같은 이유로 시드를 던진다).
    덤으로 콜드 스타트를 흡수한다 — 실측에서 첫 대답만 9971ms 였고 그다음은 2.6~3.0초였다.
    """
    monkeypatch.setattr(cs.settings, "CASCADE_GREETING", True)
    transport = _Transport([_ctl(type="start")], wait_for="turn_end")
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    assert "turn_start" in transport.types(), transport.events   # 비버가 먼저 열었다
    assert transport.audio > 0                                   # 실제로 소리가 나갔다
    assert "user_turn_start" not in transport.types()            # 사용자는 말한 적이 없다
    # 시드는 "네가 먼저 인사하며 시작해" 라는 지시다 — 소리 내어 읽을 문장이 아니다.
    assert "[통화 시작]" in reply_rig["history_or_seed"]


@pytest.mark.asyncio
async def test_barge_in_cancels_reply_without_killing_session(reply_rig, monkeypatch):
    """⭐ barge-in: audio_cancel 을 내고 **turn_end 는 안 낸다**(I4). 세션은 살아 있다.

    그리고 합성했지만 못 들려준 글자를 원가에 남긴다 — 그 문장은 이미 과금됐다.
    """
    monkeypatch.setattr(cs.settings, "CASCADE_MIC_ALWAYS_OPEN", True)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_RMS", 0.0)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_MIN_MS", 0)
    # 비버가 **실제로 들리고 있는** 상태를 만든다(2026-08-07 관문 ⓪-1). 클라 버퍼 추정을
    # 0 으로 두고 임계를 낮춰, 몇 프레임만 나가도 '들렸다'가 되게 한다.
    monkeypatch.setattr(cs.settings, "CASCADE_CLIENT_BUFFER_MS", 0)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_MIN_AUDIBLE_MS", 20)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_CONFIRM", "immediate")
    reply_rig["chunk_delay"] = 0.05     # 비버가 말하는 동안 끼어들 시간을 만든다
    reply_rig["chunks"] = 20

    transport = _Transport([
        _ctl(type="start"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
        0.25,                                        # 비버가 말하기 시작한다
        _ctl(type="__test_event", event=SPEECH_BEGIN),   # 사용자가 끼어든다
        0.15,
    ], wait_for="audio_cancel")
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    types = transport.types()
    assert "audio_cancel" in types, transport.events
    assert "turn_end" not in types, transport.events     # I4 — 취소된 턴에는 종료를 안 낸다
    cancel = transport.first("audio_cancel")
    assert cancel["turn_id"] == transport.first("turn_start")["turn_id"]
    # 세션은 죽지 않았다(TaskGroup 이 취소로 무너지면 아래 요약 자체가 안 나온다)
    assert session.usage.summary() is not None


# --------------------------------------------------------------------------- #
# dead air 회귀 — 2026-08-07 사장님 45분 통화(98턴)에서 나온 결함
#   취소 14건 중 7건이 '들린글자=0' 이었고, 그 뒤가 전부 빈 턴 → 침묵이었다.
#   통화 중 비버에게 하신 말이 로그에 남았다: '그냥 너가 말을 안 듣잖아'
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_inaudible_beaver_is_not_cancelled(reply_rig, monkeypatch):
    """⭐ 비버가 아직 안 들리면 잡음으로 죽이지 않는다 — 끊어봐야 멈출 소리가 없다."""
    monkeypatch.setattr(cs.settings, "CASCADE_MIC_ALWAYS_OPEN", True)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_RMS", 0.0)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_MIN_MS", 0)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_CONFIRM", "immediate")
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_MIN_AUDIBLE_MS", 300)
    monkeypatch.setattr(cs.settings, "CASCADE_CLIENT_BUFFER_MS", 600)  # 기본값 — 아직 안 들린다
    reply_rig["chunk_delay"] = 0.02
    reply_rig["chunks"] = 6                      # 60ms 분량 = 들린 것 0

    transport = _Transport([
        _ctl(type="start"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
        0.15,
        _ctl(type="__test_event", event=SPEECH_BEGIN),   # 잡음
        0.2,
    ], wait_for="turn_end")
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    assert "audio_cancel" not in transport.types(), transport.events
    assert "turn_end" in transport.types(), transport.events   # 대답을 끝까지 했다


@pytest.mark.asyncio
async def test_transcript_confirm_rejects_noise(reply_rig, monkeypatch):
    """③ 전사 확인 관문 — 소리만 나고 전사가 없으면 비버를 끊지 않는다(설계엔 있었고 구현이 없었다)."""
    monkeypatch.setattr(cs.settings, "CASCADE_MIC_ALWAYS_OPEN", True)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_RMS", 0.0)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_MIN_MS", 0)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_CONFIRM", "transcript")
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_MIN_AUDIBLE_MS", 20)
    monkeypatch.setattr(cs.settings, "CASCADE_CLIENT_BUFFER_MS", 0)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_SUSTAIN_MS", 5000)  # 지속 폴백은 안 걸리게
    reply_rig["chunk_delay"] = 0.02
    reply_rig["chunks"] = 30

    transport = _Transport([
        _ctl(type="start"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
        0.15,
        _ctl(type="__test_event", event=SPEECH_BEGIN),   # 잡음 — 전사가 따라오지 않는다
        0.3,
        _ctl(type="stop"),          # 턴 종료를 기다리지 않고 결정적으로 끝낸다
    ], wait_for="__never__")
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    assert "audio_cancel" not in transport.types(), transport.events


@pytest.mark.asyncio
async def test_cancelled_reply_resumes_instead_of_dead_air(reply_rig, monkeypatch):
    """⭐⭐ 취소 뒤 빈 턴이 와도 **침묵하지 않는다** — 못 들려준 말을 이어서 한다(LLM 재호출 0)."""
    monkeypatch.setattr(cs.settings, "CASCADE_TURN_SILENCE_MS", 60)
    session = CascadeSession(_Transport([]), genai_client=object())
    session._tg = _StubGroup()
    # 비버가 49자를 준비했는데 한 글자도 못 들려주고 죽은 상태를 만든다(관측된 b36).
    session._on_reply_cancelled("b1", "안녕하세요. 오늘은 뭐 하셨어요?")
    assert session._interrupted is not None

    resumed = session._resume_interrupted()
    assert resumed is True
    assert session._tg.started, "빈 턴 뒤에 아무 일도 안 일어나면 그게 dead air 다"
    assert reply_rig["chat"] is None, "이어가기는 LLM 을 다시 부르지 않는다"


@pytest.mark.asyncio
async def test_resume_is_skipped_when_user_actually_spoke(reply_rig):
    """사용자가 실제로 말했으면 되살리지 않는다 — 그건 대화가 진행된 것이다."""
    session = CascadeSession(_Transport([]), genai_client=object())
    session._tg = _StubGroup()
    session._on_reply_cancelled("b1", "안녕하세요. 오늘은 뭐 하셨어요?")
    session._interrupted = None          # _close_turn 이 비지 않은 턴에서 하는 일
    assert session._resume_interrupted() is False
    assert session._tg.started is False


class _StubGroup:
    """TaskGroup 대역 — 태스크를 실제로 돌리지 않고 '시작됐는지'만 본다."""

    def __init__(self) -> None:
        self.started = False

    def create_task(self, coro):
        self.started = True
        coro.close()          # 실행하지 않는다(경고 방지)
        return None


# --------------------------------------------------------------------------- #
# barge-in 기각 뒤의 발화 (2026-08-07 사장님 통화)
#   "중간에 말하면 마이크 인식은 되어서 전사는 되는데 대답이 끊기지도 않고,
#    대답 다 해도 내가 중간에 말한 거에 대한 답변을 하지 않아"
#   기각의 뜻은 "비버를 끊지 않는다"지 "사용자 말을 무시한다"가 아니다.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_rejected_bargein_utterance_still_gets_answered(reply_rig, monkeypatch):
    """⭐ 비버가 말하는 중에 한 말도 **대답이 끝난 뒤 답을 받는다**(버리지 않는다)."""
    monkeypatch.setattr(cs.settings, "CASCADE_MIC_ALWAYS_OPEN", False)   # barge-in 은 기각된다
    reply_rig["chunk_delay"] = 0.03
    reply_rig["chunks"] = 8

    transport = _Transport([
        _ctl(type="start"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
        0.12,                                   # 비버가 말하는 중
        _ctl(type="__test_say", text="지금 몇 시야"),   # 끼어들어 말한다(기각될 것)
        _ctl(type="__test_event", event=SPEECH_END),
        0.9,
        _ctl(type="stop"),
    ], wait_for="__never__")
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    # 두 발화 모두 사용자 턴으로 잡혔다(전사만 뜨고 사라지지 않았다)
    said = [e["text"] for e in transport.events if e.get("type") == "user_turn_end"]
    assert "지금 몇 시야" in said, transport.events
    # 그리고 그 발화가 **답을 받았다** — 이력에 사용자 발화로 들어갔다는 게 증거다
    assert any(h["role"] == "user" and h["text"] == "지금 몇 시야" for h in session._history), \
        session._history


@pytest.mark.asyncio
async def test_repeating_the_beaver_is_answered(reply_rig, monkeypatch):
    """⭐ **따라 말하기는 답을 받는다** — 어학 앱에서 그건 정상 학습 행동이다.

    2026-08-08: 서버가 '비버 대사와 겹치면 에코'로 분류해 진짜 발화 5건을 죽였다
    (사장님: "왜 응답을 안 하지?"). 우리는 프롬프트에서 직접 "2번 따라 말하게" 라고 시킨다.
    ⛔ 에코 억제는 **음향 층(클라 AEC)의 일**이지 말 내용으로 추측할 일이 아니다.
    실측: 비버 재생 중 마이크 에너지 최대 0.0443 < 임계 0.05, 그리고 **재생 중 전사 0건**.
    """
    monkeypatch.setattr(cs.settings, "CASCADE_MIC_ALWAYS_OPEN", False)
    session = CascadeSession(_Transport([]), genai_client=object())
    session._tg = _StubGroup()
    session._history.append({"role": "model", "text": "책이 탁자 위에 있어요."})

    await session._start_reply("책이 탁자 위에 있어요")   # 비버 대사와 100% 겹친다
    assert session._tg.started is True, "따라 말하기를 버렸다"


def test_no_text_similarity_gate_remains():
    """⛔ 텍스트 유사도로 발화를 거르는 분류기가 **남아 있으면 안 된다**.

    창(재생 중인가)만 남겨도 같은 종류의 사고가 난다 — barge-in 이 안 걸린 발화가
    재생 중이라는 이유로 통째로 죽는다. 그래서 이 경로에서 통째로 걷어냈다.
    남은 방어는 두 층이다: ①클라 AEC ②에너지 게이트(CASCADE_BARGEIN_RMS).
    """
    import inspect

    source = inspect.getsource(cs)
    for gone in ("_looks_like_echo", "_beaver_said_recently", "_ECHO_OVERLAP"):
        assert gone not in source, gone


@pytest.mark.asyncio
async def test_aec_missing_is_logged_as_a_warning(reply_rig, caplog):
    """AEC 선언이 없으면 **경고로 보이게** 한다 — 없앤 방어를 조용히 두지 않는다."""
    import logging

    transport = _Transport([_ctl(type="start")], wait_for="ready")
    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(
            CascadeSession(transport, genai_client=object()).run(), timeout=5
        )
    assert any("AEC" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING), caplog.text


# --------------------------------------------------------------------------- #
# 언어 마커 (사장님 설계) — "오늘은 __How are you?__ 를 배워볼까?"
#   타깃 언어 부분을 __ 로 감싸 오면 그 경계로 잘라 **구간마다 그 언어로** 읽는다.
# --------------------------------------------------------------------------- #
def test_marker_splits_into_language_segments():
    from domains.learning.realtime.cascade_reply import split_by_language

    segments = split_by_language("오늘은 __How are you?__ 를 배워볼까?", "ko", "en")
    assert segments == [
        ("오늘은", "ko"),
        ("How are you?", "en"),
        ("를 배워볼까?", "ko"),
    ]


def test_marker_absent_or_unbalanced_falls_back_whole():
    """⛔ 마커 준수에 전부를 걸지 않는다 — 없거나 짝이 안 맞으면 통째로 기본 언어로 낸다."""
    from domains.learning.realtime.cascade_reply import split_by_language

    assert split_by_language("그냥 한국어 문장", "ko", "en") == [("그냥 한국어 문장", "ko")]
    # 짝이 안 맞는다(모델이 반만 지켰다) — 말이 사라지는 것보다 통째로 내는 게 낫다.
    assert split_by_language("오늘은 __How are you 를 배울까?", "ko", "en") == [
        ("오늘은 How are you 를 배울까?", "ko")
    ]


def test_strip_markers_is_the_single_place():
    """마커를 지우는 지점은 한 곳이다 — 나중에 DB·복습을 붙이는 사람이 헤매지 않게."""
    from domains.learning.realtime.cascade_reply import strip_markers

    assert strip_markers("오늘은 __How are you?__ 를") == "오늘은 How are you? 를"
    assert strip_markers("") == ""


@pytest.mark.asyncio
async def test_each_language_segment_is_synthesized_with_its_language(reply_rig, monkeypatch):
    """구간마다 **그 언어로** 합성 요청이 나가고, 마커는 TTS 에 안 들어간다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_LANGUAGE", "ko")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_TARGET_LANGUAGE", "en")
    calls: list[tuple[str, str]] = []

    async def _tts(text, **kwargs):
        calls.append((text, kwargs.get("language")))

        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    session = CascadeSession(_Transport([]), genai_client=object())
    await session.beaver.begin()      # 불변식 I1 — 비버 턴 밖에서는 오디오를 못 보낸다
    await session._speak("오늘은 __How are you?__ 를 배워볼까?")

    assert calls == [
        ("오늘은", "ko"),
        ("How are you?", "en"),
        ("를 배워볼까?", "ko"),
    ]
    assert all("__" not in text for text, _ in calls)   # 마커를 소리로 읽지 않는다


@pytest.mark.asyncio
async def test_marker_state_is_logged_for_every_sentence(reply_rig, monkeypatch, caplog):
    """⭐ 마커가 **실제로 걸렸는지**가 로그에 남는다 — 없으면 실험이 성립하지 않는다.

    폴백이 조용해서(마커를 안 써도 통째 재생 = 소리는 정상) "끊김이 줄었다"는 판단이
    '마커가 걸린 상태'에서 나온 건지 아닌지 못 가른다. 그래서 셋을 갈라 찍는다.
    ⛔ 대사 원문은 찍지 않는다(통화 내용이 로그에 남는다).
    """
    import logging

    monkeypatch.setattr(cs.settings, "CASCADE_TTS_LANGUAGE", "ko")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_TARGET_LANGUAGE", "en")
    session = CascadeSession(_Transport([]), genai_client=object())
    await session.beaver.begin()

    with caplog.at_level(logging.INFO):
        await session._speak("오늘은 __How are you?__ 를 배워볼까?")   # 마커 있음
        await session._speak("그냥 한국어만 말한다")                    # 마커 없음
        await session._speak("반쪽만 __지켰다")                         # 짝 안 맞음
    lines = [r.getMessage() for r in caplog.records if "언어구간" in r.getMessage()]
    assert len(lines) == 3, lines
    assert "3개 ko/en/ko 마커=있음" in lines[0]
    assert "마커=없음" in lines[1]
    assert "마커=짝안맞음" in lines[2]
    # 통화 내용이 로그로 새지 않는다
    assert all("How are you" not in line for line in lines), lines
    assert session._marker_seen == {"있음": 1, "없음": 1, "짝안맞음": 1}


@pytest.mark.asyncio
async def test_first_sentence_alone_then_batched(reply_rig, monkeypatch):
    """⭐ 요청 수를 줄이되 **첫 소리는 안 늦춘다** — 첫 문장 단독, 나머지는 묶음.

    2026-08-07 실통화에서 문장마다 스트림을 여느라 턴당 7회(57 calls/8턴)까지 갔고 분당 요청
    쿼터에 걸려 429 → 다른 엔진으로 폴백 → 한 대답 안에서 목소리가 섞였다.
    """
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_BATCH_CHARS", 1000)   # 끝까지 안 끊기게

    def _open(client, model, **kwargs):
        chat = _FakeChat(["첫 번째 문장입니다. ", "두 번째 문장입니다. ", "세 번째 문장입니다."])
        reply_rig["chat"] = chat
        return chat

    monkeypatch.setattr(cs.gemini_chat, "open_chat_stream", _open)
    transport = _Transport([
        _ctl(type="start"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    await asyncio.wait_for(CascadeSession(transport, genai_client=object()).run(), timeout=5)

    calls = reply_rig["tts_calls"]
    assert len(calls) == 2, calls                 # 문장 3개인데 요청은 2번
    assert calls[0] == "첫 번째 문장입니다."       # 첫 문장은 단독 = 첫 소리 유지
    assert calls[1] == "두 번째 문장입니다. 세 번째 문장입니다."


@pytest.mark.asyncio
async def test_batch_flushes_at_the_cap(reply_rig, monkeypatch):
    """묶음이 무한정 커지면 뒤쪽 첫 소리가 늦는다 — 상한에서 끊어 보낸다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_BATCH_CHARS", 8)

    def _open(client, model, **kwargs):
        chat = _FakeChat(["첫 번째 문장입니다. ", "두 번째 문장입니다. ", "세 번째 문장입니다."])
        reply_rig["chat"] = chat
        return chat

    monkeypatch.setattr(cs.gemini_chat, "open_chat_stream", _open)
    transport = _Transport([
        _ctl(type="start"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    await asyncio.wait_for(CascadeSession(transport, genai_client=object()).run(), timeout=5)
    assert reply_rig["tts_calls"] == [
        "첫 번째 문장입니다.", "두 번째 문장입니다.", "세 번째 문장입니다."
    ]


@pytest.mark.asyncio
async def test_quota_429_pins_engine_for_this_call_only(reply_rig, monkeypatch, caplog):
    """⭐ 429 를 한 번 맞으면 **이 통화 동안** Gemini 재시도를 멈춘다(다음 통화는 다시 시작).

    실측: 한도 분당 10회 / 수요 평균 19.2·피크 27. 소진된 상태에서 문장마다 찔러봐야
    실패해도 요청은 나가고(회복이 늦어진다) 첫소리만 늘어난다. 그리고 통화 중간에 엔진이
    왔다갔다 하면 목소리가 바뀐다.
    """
    import logging

    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ENGINE", "gemini-tts")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_GEMINI_MODEL", "gemini-2.5-flash-tts")
    allowed: list[bool] = []

    async def _tts(text, **kwargs):
        allowed.append(kwargs.get("allow_gemini"))
        report = kwargs.get("report")
        if report is not None and kwargs.get("allow_gemini"):
            report["quota"] = True           # 첫 호출에서 429
            report["fallback_from"] = "gemini-2.5-flash-tts"
            report["engine"] = cs.tts.CHIRP3_ENGINE

        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    session = CascadeSession(_Transport([]), genai_client=object())
    await session.beaver.begin()
    with caplog.at_level(logging.WARNING):
        await session._speak("첫 번째 문장입니다.")
        await session._speak("두 번째 문장입니다.")

    assert allowed == [True, False], allowed          # 두 번째부터는 안 찌른다
    assert session._tts_gemini_calls == 1             # ①의 효과를 재는 값
    assert any("엔진 고정" in r.getMessage() for r in caplog.records), caplog.text
    # ⛔ 세션 단위여야 한다 — 새 세션은 다시 gemini 로 시작한다
    fresh = CascadeSession(_Transport([]), genai_client=object())
    assert fresh._tts_gemini_off is False


# --------------------------------------------------------------------------- #
# 데모 화면에서 엔진 고르기 (⛔ dev 한정 편의 — 클라가 서버 기본값을 덮는다)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_client_can_pick_engine_and_rate(reply_rig, monkeypatch):
    """start 에서 고른 값이 **세션 값**으로 잡히고 합성 호출까지 전달된다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ENGINE", "chirp3-hd")
    seen: list[dict] = []

    async def _tts(text, **kwargs):
        seen.append(kwargs)

        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    transport = _Transport([
        _ctl(type="start", ttsEngine="gemini-tts", speakingRate=1.25, stylePrompt="밝게"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    assert session._tts_engine == "gemini-tts"
    assert seen and seen[0]["engine"] == "gemini-tts"
    assert seen[0]["speaking_rate"] == pytest.approx(1.25)
    assert seen[0]["style_prompt"] == "밝게"


@pytest.mark.asyncio
async def test_unknown_engine_is_rejected(reply_rig, monkeypatch):
    """⚠ 클라가 아무 문자열이나 보내면 안 된다 — 거절하고 서버 기본값으로 간다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ENGINE", "chirp3-hd")
    transport = _Transport([_ctl(type="start", ttsEngine="아무거나")], wait_for="ready")
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)
    assert session._tts_engine == "chirp3-hd"


@pytest.mark.asyncio
async def test_only_the_switching_reply_logs_both_engines(reply_rig, monkeypatch):
    """⭐ **엔진이 바뀐 대답만 혼합으로 찍히고, 그다음은 단일**로 찍힌다.

    예전엔 `_tts_engines` 를 어디서도 안 비워서 세션 전체가 누적됐다 — 429 백오프가 걸린
    통화는 **전 구간이 chirp+gemini 로 보였고**, 턴 단위 A/B 판정이 아예 불가능했다.
    주석은 '이 턴에서'라고 적혀 있었는데 실제는 세션 누적이라, 그 거짓 주석을 근거로
    다른 탭이 문서에 틀린 문장을 쓰기도 했다.
    """
    calls = {"n": 0}

    async def _tts(text, **kwargs):
        calls["n"] += 1
        report = kwargs.get("report")
        if report is not None:
            # 첫 문장만 gemini 가 내고, 그 뒤로는 chirp 이 낸다(= 그 대답만 혼합이다).
            report["engine"] = "gemini-2.5-flash-tts" if calls["n"] == 1 else cs.tts.CHIRP3_ENGINE

        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    session = CascadeSession(_Transport([]), genai_client=object())
    session._tg = _StubGroup()

    await session._run_reply("안녕")
    assert session._tts_engines == {"gemini-2.5-flash-tts", cs.tts.CHIRP3_ENGINE}

    await session._run_reply("또 안녕")
    assert session._tts_engines == {cs.tts.CHIRP3_ENGINE}, "이전 대답의 엔진이 남았다"


# --------------------------------------------------------------------------- #
# Gemini 배치 모드 — 전체 합성 후 재생 (⛔ 프로덕션 방식 아님, 소리 판정용)
#   Gemini 는 합성 배속 1.3x 라 실시간을 못 따라간다(실측) → 문장 중간에 끊긴다.
#   끊긴 소리로는 감정·발음이 좋은지 판단할 수가 없어서, 지연을 내주고 끊김을 없앤 모드다.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_batch_mode_synthesizes_everything_before_speaking(reply_rig, monkeypatch):
    """⭐ 전부 합성한 **뒤에** 소리가 나간다 — 중간에 끊길 자리가 없다."""
    order: list[str] = []

    async def _tts(text, **kwargs):
        order.append(f"tts:{text[:6]}")

        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    transport = _Transport([
        _ctl(type="start", ttsEngine="gemini-batch"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)

    types = transport.types()
    # 준비 알림이 **소리보다 먼저** 나간다(침묵을 설명하지 않으면 "끊겼나?"가 된다)
    assert "beaver_preparing" in types, transport.events
    assert types.index("beaver_preparing") < types.index("turn_start")
    stages = [e["stage"] for e in transport.events if e.get("type") == "beaver_preparing"]
    assert stages[0] == "llm" and "tts" in stages, stages
    assert types.count("turn_start") == 1 and types.count("turn_end") == 1
    assert transport.audio > 0


@pytest.mark.asyncio
async def test_batch_mode_splits_by_language_and_joins(reply_rig, monkeypatch):
    """구간마다 그 언어로 합성해 **이어붙인다**. ⛔ 병렬로 쏘지 않는다(순간 집중이 429 를 부른다)."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_LANGUAGE", "ko")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_TARGET_LANGUAGE", "en")
    calls: list[tuple[str, str]] = []
    inflight = {"now": 0, "max": 0}

    def _open(client, model, **kwargs):
        chat = _FakeChat(["오늘은 __How are you?__ 를 배워볼까요?"])
        reply_rig["chat"] = chat
        return chat

    async def _tts(text, **kwargs):
        inflight["now"] += 1
        inflight["max"] = max(inflight["max"], inflight["now"])
        calls.append((text, kwargs.get("language")))
        await asyncio.sleep(0)
        inflight["now"] -= 1

        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.gemini_chat, "open_chat_stream", _open)
    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    transport = _Transport([
        _ctl(type="start", ttsEngine="gemini-batch"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    await asyncio.wait_for(CascadeSession(transport, genai_client=object()).run(), timeout=5)

    assert calls == [("오늘은", "ko"), ("How are you?", "en"), ("를 배워볼까요?", "ko")]
    assert inflight["max"] == 1, "구간을 병렬로 쐈다 — 429 를 부른다"


@pytest.mark.asyncio
async def test_batch_mode_is_gemini_only(reply_rig, monkeypatch):
    """⛔ Chirp 은 지금 방식(문장 단위 스트리밍) 그대로다 — 배치는 Gemini 전용이다."""
    transport = _Transport([
        _ctl(type="start", ttsEngine="chirp3-hd"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    await asyncio.wait_for(CascadeSession(transport, genai_client=object()).run(), timeout=5)
    assert "beaver_preparing" not in transport.types(), transport.events


@pytest.mark.asyncio
async def test_batch_mode_ignores_bargein_while_synthesizing(reply_rig, monkeypatch, caplog):
    """⭐ 합성 중(소리가 아직 안 나감)에는 끼어들어도 **대답을 취소하지 않는다**.

    배치 모드는 20초 넘게 조용하다. 그 사이 "여보세요?" 한 번에 21초치 합성이 날아가면
    **이 모드의 목적(끊김 없이 소리를 들어보기)이 배반된다.**
    ⛔ 다만 사용자 발화를 **버리지는 않는다** — 대답이 끝난 뒤 답한다(dead-air 재발 방지).
    """
    import logging

    monkeypatch.setattr(cs.settings, "CASCADE_MIC_ALWAYS_OPEN", True)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_RMS", 0.0)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_MIN_MS", 0)
    monkeypatch.setattr(cs.settings, "CASCADE_BARGEIN_CONFIRM", "immediate")

    async def _slow_tts(text, **kwargs):
        await asyncio.sleep(0.15)          # 합성이 오래 걸리는 상황

        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _slow_tts)
    transport = _Transport([
        _ctl(type="start", ttsEngine="gemini-batch"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
        0.1,                                # 합성 중이다(소리는 아직 안 나갔다)
        _ctl(type="__test_event", event=SPEECH_BEGIN),
        _ctl(type="__test_say", text="여보세요"),
        _ctl(type="__test_event", event=SPEECH_END),
        0.6,
        _ctl(type="stop"),          # 결정적으로 끝낸다(턴 종료를 기다리지 않는다)
    ], wait_for="__never__")
    session = CascadeSession(transport, genai_client=object())
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(session.run(), timeout=5)
    assert any("합성 중" in r.getMessage() for r in caplog.records), caplog.text

    types = transport.types()
    assert "audio_cancel" not in types, transport.events   # 합성이 날아가지 않았다
    assert "turn_end" in types, transport.events           # 대답을 끝까지 들려줬다
    # ⛔ 그 발화는 버려지지 않는다 — 사용자 턴으로 잡혀 '대답 뒤에 답할 대상'이 된다.
    #   (대기열→답변까지는 test_rejected_bargein_utterance_still_gets_answered 가 본다.
    #    여기서는 세션을 stop 으로 끝내므로 그 뒤 처리까지는 보지 않는다.)
    assert types.count("user_turn_start") == 2, transport.events
    assert any(e.get("text") == "여보세요" for e in transport.events
               if e.get("type") == "input_transcript"), transport.events


# --------------------------------------------------------------------------- #
# 읽기 속도 실측 — 세 모드를 **로그로** 비교할 수 있어야 한다
#   사장님: "batch 랑 말하는 속도 똑같은데?" → 로그가 확인하거나 반박해줘야 한다.
# --------------------------------------------------------------------------- #
def test_reading_speed_uses_spoken_chars_and_names_the_language():
    """⛔ 분자는 **실제로 소리가 나간 글자**다(원가용 tts_chars 가 아니라).

    tts_chars 는 'API 에 넘긴 글자'라 barge-in 으로 끊긴 몫까지 들어간다 — 그걸 오디오 초로
    나누면 **28자/초 같은 불가능한 값**이 나온다(실측 로그에서 실제로 나왔다).
    ⚠ 언어도 반드시 붙는다: "Hello, how are you today?" 25자 vs "안녕하세요 오늘 어때요?" 14자 —
      같은 시간을 말해도 글자 수가 다르다. 언어를 빼고 자/초를 비교하면 틀린 결론이 나온다.
    """
    session = CascadeSession(_Transport([]), genai_client=object())
    sec = int(cs.BEAVER_BYTES_PER_MS * 1000)          # 1초치 PCM 바이트
    session._reply_spans = [("ko", 20, sec * 2), ("en", 30, sec * 2)]

    summary = session._reading_summary(None)
    assert "들린글자=50" in summary
    assert "오디오=4.0초" in summary
    assert "읽기=12.5자per초" in summary               # 50자 / 4.0초
    assert "ko:20자/2.0초/10.0자per초" in summary      # 언어별로 갈라 보여준다
    assert "en:30자/2.0초/15.0자per초" in summary


def test_synthesis_speed_is_only_claimed_where_it_can_be_measured():
    """⛔ 실시간은 합성과 재생이 겹쳐 '합성 소요'가 정의되지 않는다 — 억지 숫자를 만들지 않는다.

    억지로 만든 값이 배치의 실측값과 나란히 놓이면 **비교가 더 나빠진다.**
    """
    session = CascadeSession(_Transport([]), genai_client=object())
    sec = int(cs.BEAVER_BYTES_PER_MS * 1000)
    session._reply_spans = [("ko", 40, sec * 4)]

    assert "합성배속=측정불가" in session._reading_summary(None)      # 실시간
    assert "합성배속=2.00x" in session._reading_summary(2.0)          # 배치(오디오 4초 / 합성 2초)


@pytest.mark.asyncio
async def test_both_modes_log_the_same_reading_fields(reply_rig, monkeypatch, caplog):
    """실시간과 배치가 **같은 형식**으로 찍어야 두 모드를 비교할 수 있다."""
    import logging

    async def _tts(text, **kwargs):
        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    lines: list[str] = []
    for engine in ("chirp3-hd", "gemini-batch"):
        transport = _Transport([
            _ctl(type="start", ttsEngine=engine),
            _ctl(type="__test_say", text="안녕"),
            _ctl(type="__test_event", event=SPEECH_END),
        ])
        with caplog.at_level(logging.INFO):
            caplog.clear()
            await asyncio.wait_for(
                CascadeSession(transport, genai_client=object()).run(), timeout=5
            )
        lines.append(next(r.getMessage() for r in caplog.records
                          if r.getMessage().startswith("cascade 대답")))
    for line in lines:
        for field in ("들린글자=", "오디오=", "읽기=", "합성배속="):
            assert field in line, (field, line)


# --------------------------------------------------------------------------- #
# 선행 버퍼는 **엔진마다 다르다** (2026-08-08 재측정)
#   Gemini 는 합성이 재생보다 최대 1.16~1.48초 뒤처지는데 200ms 만 모으고 시작했다 →
#   언더런(= "끊긴다")이 나는 게 당연했다. 배속 자체는 1.68~1.94x 로 실시간보다 빠르다 —
#   문제는 속도가 아니라 **출발 버퍼**였다.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_lead_buffer_is_per_engine(reply_rig, monkeypatch):
    """Gemini 를 고르면 선행 버퍼가 커지고, Chirp 은 전역 기본값(작게)을 그대로 쓴다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_LEAD_MS_GEMINI", 1500)

    for engine, expected in (("chirp3-hd", None), ("gemini-tts", 1500), ("gemini-batch", 1500)):
        transport = _Transport([_ctl(type="start", ttsEngine=engine)], wait_for="ready")
        session = CascadeSession(transport, genai_client=object())
        await asyncio.wait_for(session.run(), timeout=5)
        assert session.beaver.lead_ms == expected, engine


@pytest.mark.asyncio
async def test_pacer_uses_the_session_lead(reply_rig, monkeypatch):
    """⭐ 페이서가 **세션 값**을 쓴다 — 전역 상수만 보면 엔진별 설정이 무의미해진다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_LEAD_MS", 200)
    slept: list[float] = []

    async def _sleep(sec):
        slept.append(sec)

    one_second = b"\x00" * int(cs.BEAVER_BYTES_PER_MS * 1000)

    beaver = cs.BeaverOutput(_Transport([]), sleep=_sleep)
    await beaver.begin()
    beaver.lead_ms = 1500                       # Gemini 상태
    await beaver.send(one_second)
    await beaver.send(one_second)               # 누적 1초 선행 — 1.5초 한도 안쪽이다
    assert slept == [], "선행 1.5초 안쪽인데 기다렸다(그러면 버퍼가 안 쌓여 언더런이 난다)"

    beaver2 = cs.BeaverOutput(_Transport([]), sleep=_sleep)
    await beaver2.begin()                        # lead_ms=None → 전역 200ms
    await beaver2.send(one_second)
    await beaver2.send(one_second)
    assert slept and slept[0] > 0.5, slept       # 200ms 를 넘겼으니 기다린다


@pytest.mark.asyncio
async def test_gemini_does_not_send_the_first_sentence_alone(reply_rig, monkeypatch):
    """짧은 요청은 Gemini 에 특히 불리하다(고정 오버헤드 ≈1.3초) — 첫 문장도 묶는다.

    ⛔ Chirp 은 지금대로 첫 문장을 단독 즉시 송출한다(그쪽은 오버헤드가 작다).
    """
    def _open(client, model, **kwargs):
        chat = _FakeChat(["첫 번째 문장입니다. ", "두 번째 문장입니다. ", "세 번째 문장입니다."])
        reply_rig["chat"] = chat
        return chat

    monkeypatch.setattr(cs.gemini_chat, "open_chat_stream", _open)
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_BATCH_CHARS_GEMINI", 1000)

    for engine, expected_calls in (("gemini-tts", 1), ("chirp3-hd", 2)):
        reply_rig["tts_calls"].clear()
        transport = _Transport([
            _ctl(type="start", ttsEngine=engine),
            _ctl(type="__test_say", text="안녕"),
            _ctl(type="__test_event", event=SPEECH_END),
        ])
        await asyncio.wait_for(
            CascadeSession(transport, genai_client=object()).run(), timeout=5
        )
        assert len(reply_rig["tts_calls"]) == expected_calls, (engine, reply_rig["tts_calls"])


# --------------------------------------------------------------------------- #
# ElevenLabs 2종 — 구글이 아니라 별도 어댑터(core/elevenlabs_tts.py)를 탄다
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_elevenlabs_is_rejected_without_a_key(reply_rig, monkeypatch):
    """⛔ 키가 없으면 **명확히 거절**한다 — 조용히 다른 엔진으로 바꾸면 그 소리를
    ElevenLabs 로 착각하게 된다(오늘 폴백에서 배운 그대로)."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ELEVEN_API_KEY", "")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ENGINE", "chirp3-hd")
    transport = _Transport([_ctl(type="start", ttsEngine="elevenlabs-v3")], wait_for="ready")
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)
    assert session._tts_engine == "chirp3-hd"


@pytest.mark.asyncio
async def test_elevenlabs_uses_its_own_adapter_and_model_id(reply_rig, monkeypatch):
    """모델 ID 가 **문서 문자열 그대로** 나가고, 원가 벤더도 모델별로 갈린다.

    ⚠ 오늘 Cloud TTS vs Gemini API 에서 이름 규칙이 달라 세 번 밟았다 — 그래서 문자열을 고정한다.
    """
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ELEVEN_API_KEY", "x")   # 값은 검사 안 한다
    seen: list[str] = []

    async def _stream(text, **kwargs):
        seen.append(kwargs["model_id"])
        report = kwargs.get("report")
        if report is not None:
            report["engine"] = kwargs["model_id"]
        yield _FRAME

    monkeypatch.setattr(cs.elevenlabs_tts, "synthesize_stream", _stream)
    for choice, expected in (("elevenlabs-flash", "eleven_flash_v2_5"),
                             ("elevenlabs-multilingual", "eleven_multilingual_v2"),
                             ("elevenlabs-v3", "eleven_v3")):
        seen.clear()
        transport = _Transport([
            _ctl(type="start", ttsEngine=choice),
            _ctl(type="__test_say", text="안녕"),
            _ctl(type="__test_event", event=SPEECH_END),
        ])
        session = CascadeSession(transport, genai_client=object())
        await asyncio.wait_for(session.run(), timeout=5)
        assert seen and all(m == expected for m in seen), (choice, seen)
        # 원가 벤더도 모델별로 갈린다(뭉개면 단가를 못 가른다)
        assert session.usage.summary()["vendors"]["tts"]["vendor"] == expected


@pytest.mark.asyncio
async def test_elevenlabs_chars_are_counted_once(reply_rig, monkeypatch):
    """⛔ **API 에 넘긴 문자를 두 번 세지 않는다** — 2026-08-09 발견한 이중계상.

    이 가지가 상단 계측에 더해 한 번 더 세고 있어서 ElevenLabs 원가가 **두 배**로 잡혔다.
    원가가 이 프로젝트의 유일한 동기라, 이런 이중계상은 "캐스케이드가 싼가"의 결론을 뒤집는다.
    """
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ELEVEN_API_KEY", "x")
    asked: list[str] = []

    async def _stream(text, **kwargs):
        asked.append(text)
        report = kwargs.get("report")
        if report is not None:
            report["engine"] = kwargs["model_id"]
        yield _FRAME

    monkeypatch.setattr(cs.elevenlabs_tts, "synthesize_stream", _stream)
    transport = _Transport([
        _ctl(type="start", ttsEngine="elevenlabs-multilingual"),
        _ctl(type="__test_say", text="안녕"),
        _ctl(type="__test_event", event=SPEECH_END),
    ])
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)
    assert asked, "합성 요청이 없었다"
    assert session.usage.summary()["vendors"]["tts"]["chars"] == sum(len(t) for t in asked)


@pytest.mark.asyncio
async def test_elevenlabs_target_language_can_use_its_own_voice(reply_rig, monkeypatch):
    """⭐ **한국어 구간은 다른 음성으로 읽을 수 있어야 한다.**

    ElevenLabs 는 다국어 음성 하나가 두 언어를 다 읽는다. 그 음성이 영어권 화자에서
    만들어졌으면 한국어가 외국인 억양으로 나온다 — 비버는 발음 선생님이고 학습자가 그대로
    따라 하므로, 목소리가 사람 같아도 **발음이 틀리면 못 쓴다.**
    ⚠ 미설정이면 기존과 같다(음성 하나가 다 읽는다).
    """
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ELEVEN_API_KEY", "x")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_LANGUAGE", "en")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_TARGET_LANGUAGE", "ko")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ELEVEN_VOICE_ID_TARGET", "ko-voice")
    session = CascadeSession(_Transport([]), genai_client=object())
    session._tts_engine = "elevenlabs-multilingual"
    assert session._eleven_voice_for("ko") == "ko-voice"
    assert session._eleven_voice_for("en") is None      # 기본 음성으로 폴백
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ELEVEN_VOICE_ID_TARGET", "")
    assert session._eleven_voice_for("ko") is None      # 미설정 = 동작 무변경


@pytest.mark.asyncio
async def test_new_elevenlabs_model_is_rejected_without_a_key(reply_rig, monkeypatch):
    """키가 없으면 **그 엔진만** 거절된다 — 앱은 죽지 않는다(R5)."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ELEVEN_API_KEY", "")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ENGINE", "chirp3-hd")
    transport = _Transport([_ctl(type="start", ttsEngine="elevenlabs-multilingual")],
                           wait_for="ready")
    session = CascadeSession(transport, genai_client=object())
    await asyncio.wait_for(session.run(), timeout=5)
    assert session._tts_engine == "chirp3-hd"


def test_elevenlabs_lead_buffer_is_conservative_until_measured():
    """⚠ 실측 전이라 보수적으로 잡는다 — 오늘 짧은 요청 측정이 결론을 뒤집었다."""
    from core.config import settings as live

    assert live.CASCADE_TTS_LEAD_MS_ELEVEN >= live.CASCADE_TTS_LEAD_MS


# --------------------------------------------------------------------------- #
# 구두점 단독 구간 (2026-08-08 실통화)
#   사장님: "? 를 쿼스쳔마크라고 읽기 시작하고 그러네."
#   "That's right! __맞아요__?" 를 쪼개면 마지막 조각이 "?" 하나가 되고, 그것만 TTS 에
#   보내면 문맥이 없어 **기호를 단어로 읽는다**. 로그: `언어구간: 5개 en/ko/en/ko/en`
# --------------------------------------------------------------------------- #
def test_punctuation_never_becomes_its_own_segment():
    """⛔ 구두점만 남은 조각은 단독 구간이 되면 안 된다 — 앞 구간에 붙인다."""
    from domains.learning.realtime.cascade_reply import split_by_language

    segments = split_by_language("That's right! __맞아요__?", "en", "ko")
    assert len(segments) == 2, segments
    assert segments[0] == ("That's right!", "en")
    assert segments[1] == ("맞아요?", "ko")      # 물음표가 앞 구간에 붙었다
    assert all(any(ch.isalnum() for ch in text) for text, _ in segments), segments


def test_short_but_real_utterances_survive():
    """⚠ 길이로 자르면 안 된다 — "네", "Oh" 는 짧지만 **진짜 발화**다."""
    from domains.learning.realtime.cascade_reply import split_by_language

    assert split_by_language("네", "ko", "en") == [("네", "ko")]
    assert split_by_language("__Oh__ 그렇군요", "ko", "en") == [
        ("Oh", "en"), ("그렇군요", "ko"),
    ]


def test_leading_punctuation_attaches_forward():
    """앞 구간이 없으면(첫 조각이 구두점) **뒤에** 붙인다 — 버리지는 않는다."""
    from domains.learning.realtime.cascade_reply import split_by_language

    assert split_by_language("…__좋아요__", "ko", "en") == [("…좋아요", "en")]


def test_marker_only_punctuation_input_makes_no_segment():
    """읽을 말이 하나도 없으면 구간을 만들지 않는다(합성 요청도 안 나간다)."""
    from domains.learning.realtime.cascade_reply import split_by_language

    # 마커가 없어도 **읽을 말이 없으면 구간 0개**다 — 합성 요청 자체가 안 나간다.
    assert split_by_language("...", "ko", "en") == []
    assert split_by_language("__?__", "ko", "en") == []




# --------------------------------------------------------------------------- #
# 원가 계측의 일반 성질 — **센 문자 == API 에 넘긴 문자** (엔진 무관)
#   2026-08-09 ElevenLabs 가지에서 이중계상을 발견했고, 2026-08-10 **공용 폴백 경로에도**
#   같은 결함이 있는 걸 확인했다(폴백 시 문장을 한 번 더 셌다). 엔진이 바뀌어도 이 성질은
#   남아야 한다 — 원가가 이 프로젝트의 유일한 동기다.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_tts_chars_are_counted_once_per_sentence(monkeypatch):
    """정상 경로: 한 문장을 합성하면 그 문장 길이만큼만 센다."""
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ENGINE", "chirp3-hd")
    asked: list[str] = []

    async def _tts(text, **kwargs):
        asked.append(text)

        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    session = CascadeSession(_Transport([]), genai_client=object())
    await session.beaver.begin()
    await session._speak("문장 하나입니다.")
    assert asked
    assert session.usage.summary()["vendors"]["tts"]["chars"] == sum(len(t) for t in asked)


@pytest.mark.asyncio
async def test_tts_chars_are_not_double_counted_on_fallback(monkeypatch):
    """⛔ **폴백에서도 두 번 세지 않는다.**

    의도한 엔진이 실패해 다른 엔진이 소리를 내면 벤더 **이름만** 바뀌어야 한다. 예전 코드는
    거기서 문장을 한 번 더 세서 원가가 두 배가 됐다 — 그리고 하필 그 숫자가 "Live 보다 싼가"의
    근거로 쓰인다.
    """
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_ENGINE", "gemini-tts")
    monkeypatch.setattr(cs.settings, "CASCADE_TTS_GEMINI_MODEL", "gemini-2.5-flash-tts")
    asked: list[str] = []

    async def _tts(text, **kwargs):
        asked.append(text)
        report = kwargs.get("report")
        if report is not None:
            report["fallback_from"] = "gemini-2.5-flash-tts"
            report["engine"] = cs.tts.CHIRP3_ENGINE

        async def _gen():
            yield _FRAME
        return _gen()

    monkeypatch.setattr(cs.tts, "synthesize_stream", _tts)
    session = CascadeSession(_Transport([]), genai_client=object())
    await session.beaver.begin()
    await session._speak("폴백이 일어난 문장입니다.")
    tts_usage = session.usage.summary()["vendors"]["tts"]
    assert tts_usage["chars"] == sum(len(t) for t in asked), tts_usage
    # 벤더는 **실제로 소리를 낸 엔진**으로 정정된다(이름만 바뀐다)
    assert tts_usage["vendor"] == cs.tts.CHIRP3_ENGINE
