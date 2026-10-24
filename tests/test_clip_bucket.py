"""PM-DEC-1044 — 표현학습 클립 차시를 버킷에서: 업로드만 하면 열리고, 필수 항목 클립이 manifest 에 없으면 Live.
PM-DEC-1052 — 클립은 통화 시작 전 EB_WAIT_S 만 기다리고 나머지는 통화 중에 받는다(못 받으면 그 차례만 TTS)."""
from __future__ import annotations

import io
import json
import wave
from types import SimpleNamespace

import pytest

import domains.learning.realtime.clip_expr as ce

LESSON = "L9-T01-1"
_REAL_REMOTE = ce._remote_lesson          # 모듈 읽을 때 잡아 둔 진짜 버킷 로더(conftest 가 시험마다 L1-S01-1 만 바꿔 끼운다)
ON = SimpleNamespace(CLIP_EXPR="Baba")


def _wav(sec=0.4):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)
        w.writeframes(bytes([1, 0]) * int(24000 * sec))
    return buf.getvalue()


def _objects(items=("1", "2"), drop=(), drop_man=()):
    doc = {"lesson": LESSON, "no": 1, "items": [{"seq": s, "item_id": "9" + s, "surface": f"표현{s}", "en": f"e{s}"} for s in items]}
    man, objs = [], {}
    for s in items:
        for cid in (f"ask_{s}_1", f"model_{s}", f"quiz_{s}"):
            if cid in drop_man:
                continue
            man.append({"id": cid, "slot": "x", "text": cid, "item": f"표현{s}", "sec": 0.4, "face": "neutral"})
            if cid not in drop:
                objs[f"Baba_en/{LESSON}/{cid}.wav"] = _wav()
    objs[f"{LESSON}.json"] = json.dumps(doc).encode()
    objs[f"Baba_en/{LESSON}/manifest.json"] = json.dumps(man).encode()
    return objs


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for c in (ce._EB_LRU, ce._EB_MISS, ce._EB_FUT):
        c.clear()
    yield
    for c in (ce._EB_LRU, ce._EB_MISS, ce._EB_FUT):
        c.clear()


def _serve(monkeypatch, objs):
    import core.storage as storage
    asked = []

    def fake(prefix, path):
        asked.append((prefix, path))
        return objs.get(path) if prefix == ce.EB_PREFIX else None

    monkeypatch.setattr(storage, "download", fake)
    return asked


def test_uploaded_lesson_opens_and_bank_merges_generic_clips(monkeypatch):
    asked = _serve(monkeypatch, _objects())
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) == "Baba"
    clips, items = ce.load_bank("Baba", lesson=LESSON)
    assert [i.seq for i in items] == ["1", "2"]
    assert {"ask_1_1", "model_1", "quiz_2"} <= set(clips)
    assert any(k.startswith("g_") for k in clips)          # 공통 클립은 저장소 것
    assert not any(k.startswith("native_") for k in clips) # 저장소 L1-S01-1 항목 클립은 안 섞인다
    assert all(p == ce.EB_PREFIX for p, _ in asked)


def test_required_clip_missing_from_manifest_falls_back_to_live(monkeypatch):
    _serve(monkeypatch, _objects(drop_man=("model_2",)))
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) is None


def test_missing_wav_opens_and_that_turn_reads_with_tts(monkeypatch):
    """manifest 에 있는데 wav 를 못 받으면 차시는 열고, 그 클립 차례만 같은 목소리 TTS 로 읽는다."""
    import asyncio
    _serve(monkeypatch, _objects(drop=("model_2",)))
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) == "Baba"
    clips, _ = ce.load_bank("Baba", lesson=LESSON)
    assert clips["model_1"].pcm and not clips["model_2"].pcm
    said = []

    async def fake_tts(text, *, lang="en", voice="Fenrir"):
        said.append((text, lang, voice))
        return b"\x01\x00" * 4800

    monkeypatch.setattr(ce, "tts_pcm", fake_tts)
    sess = SimpleNamespace(char="Baba", tts_chars=0)
    got = asyncio.run(ce.ClipExprSession._fill_or_tts(sess, clips["model_2"]))
    assert got is not None and got.pcm and said == [("model_2", "en", "Fenrir")]
    assert asyncio.run(ce.ClipExprSession._fill_or_tts(sess, clips["model_1"])) is clips["model_1"]


def test_slow_bucket_starts_within_budget_and_fills_during_call(monkeypatch):
    """call 2874 — 189 클립 첫 받기 15.3초 동안 무음. 통화 시작은 EB_WAIT_S 만 기다린다."""
    import asyncio
    import threading
    import time
    objs = _objects(items=tuple(str(i) for i in range(1, 21)))
    gate = threading.Event()
    import core.storage as storage

    def slow(prefix, path):
        if path.endswith(".wav"):
            gate.wait(5)
        return objs.get(path)

    monkeypatch.setattr(storage, "download", slow)
    monkeypatch.setattr(ce, "EB_WAIT_S", 0.2)
    t0 = time.monotonic()
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) == "Baba"
    assert time.monotonic() - t0 < 1.5
    clips, items = ce.load_bank("Baba", lesson=LESSON)
    assert len(items) == 20 and not clips["ask_1_1"].pcm
    gate.set()
    assert asyncio.run(ce.ensure_pcm(clips["ask_1_1"])) is True
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not all(c.pcm for c in clips.values()):
        time.sleep(0.05)
    assert all(c.pcm for c in clips.values()) and not ce._EB_FUT


def test_missing_lesson_falls_back_to_live(monkeypatch):
    _serve(monkeypatch, {})
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) is None


def test_repo_lesson_unchanged_and_does_not_touch_bucket(monkeypatch):
    asked = _serve(monkeypatch, {})
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson="L1-S01-1") == "Baba"
    assert asked == []


@pytest.mark.parametrize("heard,once", [
    ("다시 말해 주세요 다시 말해 주세요", "다시 말해 주세요"),
    ("잘 안 들렸어요. 잘 안 들렸어요.", "잘 안 들렸어요"),
    ("네 네 네", "네"),
    ("뭐 뭐라고요", "뭐 뭐라고요"),                  # 더듬기는 되풀이가 아니다
    ("천천히 말해 주세요", "천천히 말해 주세요"),
])
def test_collapse_repeats(heard, once):
    assert ce.collapse_repeats(heard) == once


def test_repeated_correct_answer_is_correct_not_close():
    """clipb 2866 퀴즈: 정답을 두 번 말하면 close 였다."""
    assert ce.rule_judge("다시 말해 주세요 다시 말해 주세요", ["다시 말해 주세요"], blank=False)[0] == "correct"
    assert ce.rule_judge("감사합니다 감사합니다", ["다시 말해 주세요"], blank=False)[0] is None   # 틀린 말은 접어도 틀림



def test_needed_clip_jumps_the_queue(monkeypatch):
    """인사 재생 중 첫 질문 클립은 줄(189개) 뒤에서 기다리지 않고 바로 받는다(기다림 0 · PM-DEC-1052 다음 판)."""
    import asyncio
    import threading
    objs = _objects(items=tuple(str(i) for i in range(1, 21)))
    hold = threading.Event()
    import core.storage as storage

    def slow(prefix, path):
        if path.endswith(".wav") and not path.endswith("ask_7_1.wav"):
            hold.wait(5)                       # 줄의 나머지는 막혀 있다
        return objs.get(path)

    monkeypatch.setattr(storage, "download", slow)
    monkeypatch.setattr(ce, "EB_WAIT_S", 0.0)
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson=LESSON) == "Baba"
    clips, _ = ce.load_bank("Baba", lesson=LESSON)
    try:
        assert asyncio.run(ce.ensure_pcm(clips["ask_7_1"])) is True
    finally:
        hold.set()


@pytest.mark.parametrize("heard,target,want", [
    ("몰라요", "몰라요", "correct"),
    ("잘 모르겠어요", "잘 모르겠어요.", "correct"),
    ("몰라요", "다시 말해 주세요", "dunno"),          # 정답이 아니면 모름 그대로
    ("잘 모르겠어요", "천천히 말해 주세요", "dunno"),
])
def test_dunno_phrase_as_target_is_judged_by_match(heard, target, want):
    """2889 — 정답이 «몰라요» 인 문항에서 «몰라요» 가 dunno 로 처리됐다."""
    assert ce.rule_judge(heard, [target], blank=False)[0] == want


def test_warmup_loads_generic_and_survives_failures(monkeypatch):
    """seventh — 데우기: 공통 클립을 읽어 두고, 저장소·Speech·TTS 가 실패해도 예외 없이 결과만 남긴다."""
    import core.storage as storage

    def boom(*a, **k):
        raise RuntimeError("no net")

    monkeypatch.setattr(storage, "download", boom)
    monkeypatch.setattr(ce, "_sync_speech_client", boom)
    monkeypatch.setattr(ce, "_sync_tts_client", lambda: None)
    ce._GENERIC.clear()
    out = ce.warmup(("Baba",))
    assert out["storage"].startswith("RuntimeError") and out["speech"].startswith("RuntimeError")
    assert out["tts"].startswith("없음") and int(out["generic_Baba"].split()[0]) > 0 and ("Baba", "en") in ce._GENERIC



def test_client_created_once_under_concurrency(monkeypatch):
    """seventh2 — 데우기 스레드와 통화가 동시에 받아쓰기 클라이언트를 찾으면 하나만 만든다."""
    import threading
    import time as _t
    made = []

    def slow_make():
        _t.sleep(0.2)
        made.append(1)
        ce._SYNC_SPEECH.append(object())
        return ce._SYNC_SPEECH[0]

    ce._SYNC_SPEECH.clear()
    monkeypatch.setattr(ce, "_make_speech_client", slow_make)
    got = []
    ts = [threading.Thread(target=lambda: got.append(ce._sync_speech_client())) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ce._SYNC_SPEECH.clear()
    assert len(made) == 1 and len({id(g) for g in got}) == 1


@pytest.mark.parametrize("heard,target,want", [
    ("내일모레", "낼모레", "correct"),          # B2-T28-1 — 받아쓰기가 본딧말로 적음
    ("낼모레", "내일모레", "correct"),
    ("낼 만나요", "내일 만나요", "correct"),
    ("이야기해요", "얘기해요", "correct"),
    ("그런데요", "근데요", "correct"),
])
def test_abbrev_normalized_both_sides(heard, target, want):
    """PM-DEC-1059 — 줄임말과 본딧말은 같은 답으로 친다."""
    assert ce.rule_judge(heard, [target], blank=False)[0] == want


@pytest.mark.parametrize("heard,target", [
    ("편지를 보낼 거예요", "편지를 보내일 거예요"),   # 낱말 속 «낼» 은 안 바꾼다(보낼)
    ("제가 낼게요", "제가 내일게요"),               # «낼게요»(내다) 는 token 이 아니라 그대로
])
def test_abbrev_does_not_touch_inside_words(heard, target):
    assert ce.normalize_abbrev(heard) == heard
    assert ce.normalize_abbrev("보낼") == "보낼" and ce.normalize_abbrev("낼게요") == "낼게요"


@pytest.mark.parametrize("heard,target", [
    ("내일 만나요", "어제 만나요"),     # 틀린 답이 정규화로 맞게 되지 않는다
    ("그런데요", "그래서요"),
    ("이야기해요", "공부해요"),
])
def test_abbrev_no_false_correct(heard, target):
    assert ce.rule_judge(heard, [target], blank=False)[0] != "correct"


@pytest.mark.parametrize("heard,target", [
    ("깜빡했어요", "깜박했어요"),        # C1-T14-1 — 받아쓰기는 늘 «깜빡-»
    ("깜빡이 켜세요", "깜박이 켜세요"),
    ("깜박하다", "깜빡하다"),
])
def test_kkambak_variant_normalized(heard, target):
    """PM-DEC-1063 — «깜박»·«깜빡» 은 같은 답(낱말 머리)."""
    assert ce.rule_judge(heard, [target], blank=False)[0] == "correct"


def test_kkambak_variant_does_not_touch_other_words():
    assert ce.normalize_abbrev("반짝반짝 깜박") == "반짝반짝 깜빡"      # 낱말 머리만
    assert ce.normalize_abbrev("눈을깜박") == "눈을깜박"                # 낱말 속은 그대로
    assert ce.rule_judge("깜짝 놀랐어요", ["깜박했어요"], blank=False)[0] != "correct"


# ── PM-DEC-1074 — 저장소 차시 우선 갈래 없음: L1-S01-1 도 버킷에서만 ─────────────────── #
def test_l1_s01_reads_bucket_not_repo_and_bucket_clip_wins(monkeypatch):
    """저장소 assets 에 L1-S01-1 이 있어도 로더는 버킷 묶음을 쓴다 — 버킷 판 클립이 나와야 한다."""
    monkeypatch.setattr(ce, "_remote_lesson", _REAL_REMOTE)   # conftest 의 L1-S01-1 고정물 대체를 걷어 낸다
    objs = {k.replace(LESSON, "L1-S01-1"): v for k, v in _objects().items()}
    objs["L1-S01-1.json"] = objs["L1-S01-1.json"].replace(LESSON.encode(), b"L1-S01-1")
    asked = _serve(monkeypatch, objs)
    assert (ce.ROOT / "L1-S01-1.json").exists()          # 저장소 사본은 남아 있다(공통 g_* manifest · 시험 고정물)
    clips, items = ce.load_bank("Baba", lesson="L1-S01-1")
    assert [i.seq for i in items] == ["1", "2"]           # 버킷 차시 파일(저장소 판은 15항목)
    assert clips["ask_1_1"].src == ("Baba", "en", "L1-S01-1")   # 버킷 클립
    assert ("clips_expr", "L1-S01-1.json") in asked


def test_l1_s01_missing_in_bucket_falls_back_to_live(monkeypatch):
    """버킷에 L1-S01-1 묶음이 없으면 저장소 판으로 몰래 돌지 않고 Live(enabled None · lesson_missing 사유)."""
    monkeypatch.setattr(ce, "_remote_lesson", _REAL_REMOTE)
    _serve(monkeypatch, {})
    assert ce.lesson_missing("L1-S01-1", "Baba") == "차시 묶음 없음(L1-S01-1)"
    assert ce.enabled(ON, call_type="expression", voice="Fenrir", locale="en", lesson="L1-S01-1") is None
    with pytest.raises(FileNotFoundError):
        ce.load_bank("Baba", lesson="L1-S01-1")


def test_bucket_lesson_g_clip_overrides_repo_generic(monkeypatch):
    """PM-DEC-1075 — 버킷 묶음에 g_open 이 있으면 저장소 공통 g_open 위에 덮인다(bank.update 가 generic 뒤)."""
    objs = _objects()
    man = json.loads(objs[f"Baba_en/{LESSON}/manifest.json"])
    man.append({"id": "g_open_1", "slot": "인사", "text": "bucket greeting", "sec": 0.4, "face": "neutral"})
    objs[f"Baba_en/{LESSON}/manifest.json"] = json.dumps(man).encode()
    objs[f"Baba_en/{LESSON}/g_open_1.wav"] = _wav()
    _serve(monkeypatch, objs)
    clips, _ = ce.load_bank("Baba", lesson=LESSON)
    assert clips["g_open_1"].text == "bucket greeting" and clips["g_open_1"].src == ("Baba", "en", LESSON)
    assert "g_open_2" in clips                            # 버킷에 없는 공통 클립은 저장소 것 그대로



def test_warmup_runs_bucket_pool_on_first_lesson(monkeypatch):
    """PM-DEC-1082 — 데우기가 버킷 경로로 L1-S01-1 을 받아 일꾼을 미리 돌린다(실패해도 결과만 남긴다)."""
    seen = []
    real = ce._remote_lesson

    def spy(char, lang, lesson):
        seen.append((char, lesson))
        return real(char, lang, lesson)
    monkeypatch.setattr(ce, "_remote_lesson", spy)
    monkeypatch.setattr(ce, "_sync_speech_client", lambda: None)
    monkeypatch.setattr(ce, "_sync_tts_client", lambda: None)
    out = ce.warmup(("Baba",))
    assert seen == [("Baba", "L1-S01-1")]
    got, total = out["eb_pool_Baba"].split()[0].split("/")
    assert int(total) > 0 and got == total


def test_warmup_pool_survives_missing_lesson(monkeypatch):
    monkeypatch.setattr(ce, "_remote_lesson", lambda char, lang, lesson: None)
    monkeypatch.setattr(ce, "_sync_speech_client", lambda: None)
    monkeypatch.setattr(ce, "_sync_tts_client", lambda: None)
    assert ce.warmup(("Baba",))["eb_pool_Baba"].startswith("없음")
