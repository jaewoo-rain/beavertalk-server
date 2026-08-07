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
async def test_echo_of_beaver_is_not_answered(reply_rig, monkeypatch):
    """⚠ 반대쪽 — 비버 자기 목소리가 전사된 것에는 답하지 않는다(자기 말에 답하면 안 된다)."""
    monkeypatch.setattr(cs.settings, "CASCADE_MIC_ALWAYS_OPEN", False)
    session = CascadeSession(_Transport([]), genai_client=object())
    session._tg = _StubGroup()
    session._history.append({"role": "model", "text": "안녕하세요 반갑습니다. 오늘 기분은 어떠세요?"})

    session._start_reply("오늘 기분은 어떠세요")      # 비버 대사와 겹친다 = 에코
    assert session._tg.started is False, "비버가 자기 말에 답하려 했다"

    session._start_reply("저는 학교에 갔어요")        # 진짜 발화는 답한다
    assert session._tg.started is True


def test_short_backchannel_is_not_treated_as_echo():
    """짧은 맞장구는 겹침으로 못 가른다 — 에코로 몰아 버리면 진짜 발화를 잃는다."""
    session = CascadeSession(_Transport([]), genai_client=object())
    session._history.append({"role": "model", "text": "네 맞아요 그렇죠"})
    assert session._looks_like_echo("네") is False
    assert session._looks_like_echo("맞아요") is False


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
