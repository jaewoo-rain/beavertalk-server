"""취약 발음 학습 엔드포인트 결정적 테스트 (외부 의존 0).

검증 대상:
    GET  /api/v1/pronunciation/weak-sounds
    GET  /api/v1/pronunciation/weak-sounds/{sound_key}/lesson
    POST /api/v1/pronunciation/weak-sounds/{sound_key}/assess

핵심 불변식 5개를 고정한다.
1. **소리 단위는 위치 포함 키**다 — 초성 ㄱ 과 받침 ㄱ 이 한 버킷으로 섞이면 실패.
2. 국적별 목록은 `speak_country.first_country` 로 찾고, 통계에 없는 나라면 **빈 목록**
   (500 아님).
3. 목록의 점수는 학습 점수 > 복습 집계 순으로 덮인다.
4. 평가 제출은 **멱등이 아니라 누적**이다(attempts 증가·최신 점수 반영·최고점 유지).
   단 `baseline_score` 는 첫 제출에만 박힌다 — 「학습 전」은 한 번뿐인 사건이다.
   채점은 **서버가** 한다 — 클라가 점수를 못 보낸다(위조 차단). SpeechSuper 는 목으로
   대체해 네트워크 0 으로 돌린다.
5. 추천은 국적별 1순위 중 80점 미만. 표본 없는(None) 소리도 추천 대상이다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from core.config import settings as app_settings
from core.supabase_auth import AuthUser
from domains.account.models.member import Member
from domains.account.models.speak_country import SpeakCountry
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.member_sound_score import MemberSoundScore
from domains.learning.models.national_sound_stat import NationalSoundStat
from domains.learning.models.review import Review
from domains.learning.models.sentence import Sentence
from domains.learning.models.sound_lesson import SoundLesson

import core.deps as deps
import domains.learning.service.weak_sound_service as wsvc


def _fake_verify(token):
    if token and token.startswith("auth-"):
        return AuthUser(uid=token, email=f"{token}@test.io")
    return None


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(deps, "verify_token", _fake_verify)


class _Scorer:
    """assess_pronunciation 목 — 호출된 ref_text 를 기록하고 정해진 점수를 낸다."""

    def __init__(self, score=84):
        self.score = score
        self.calls: list[str] = []

    def __call__(self, ref_text, audio_url=None, *, language="ko"):
        self.calls.append(ref_text)
        return {
            "evaluation": {"total_score": self.score, "pronunciation": self.score,
                           "fluency": self.score, "rhythm": self.score},
            "char_scores": [{"char": "평", "score": self.score, "grade": "상"}],
            "phonemes": [],
            "phoneme_misses": [{"char_index": 0, "expected": "ㅍ"}],
        }


@pytest.fixture()
def scorer(monkeypatch):
    rec = _Scorer()
    monkeypatch.setattr(wsvc, "assess_pronunciation", rec)
    return rec


def _post_audio(client, sound_key, hdr):
    """평가 녹음 제출 — multipart. 바이트 내용은 목이 무시한다."""
    return client.post(
        f"/api/v1/pronunciation/weak-sounds/{sound_key}/assess",
        files={"audio": ("rec.wav", b"RIFFfake", "audio/wav")},
        headers=hdr,
    )


@pytest.fixture()
def session_factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _build_app(session_factory):
    from main import create_app
    app = create_app()
    app.state.session_factory = session_factory
    app.state.settings = app_settings
    app.state.genai_client = object()
    return app


def _hdr(auth="auth-member"):
    return {"Authorization": f"Bearer {auth}"}


def _phon(alpha, pron, position):
    """실경로와 같은 모양 — alpha 는 자모, 위치는 따로 온다."""
    key = None
    if position == "초성":
        key = f"onset_{alpha}"
    elif position == "종성":
        key = f"coda_{alpha}"
    return {"phoneme": alpha, "alpha": alpha, "pronunciation": pron,
            "position": position, "sound_key": key}


def _lesson(sound_key, label, order, *, type_="sound", position=None, jamo=None):
    return SoundLesson(
        sound_key=sound_key, type=type_, label=label, position=position, jamo=jamo,
        diagram="Airflow / test", card_desc=f"{label} 소리 내는 법", sort_order=order,
        payload={"how_to": ["a", "b", "c"], "words": [1, 2, 3, 4],
                 "sentence": {"text": "문장"}, "test": {"text": "평가"}},
    )


@pytest.fixture()
def seeded(session_factory):
    """회원(억양=Vietnam) / 통화 1건 / 복습 3건 + 마스터 4과 + 국가 통계 3행."""
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="선생님", personality="다정",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.flush()
        country = SpeakCountry(first_country="Vietnam")
        db.add(country)
        db.flush()
        member = Member(language="en", korean_level=1, onboarding_completed=True,
                        auth_user_id="auth-member",
                        speak_country_id=country.speak_country_id)
        db.add(member)
        db.flush()
        call = Call(member_id=member.member_id, character_id=ch.character_id,
                    status="done", call_type="normal")
        db.add(call)
        db.flush()

        s1 = Sentence(call_id=call.call_id, korean_sentence="가", native_sentence="a",
                      locale="en")
        s2 = Sentence(call_id=call.call_id, korean_sentence="나", native_sentence="b",
                      locale="en")
        db.add_all([s1, s2])
        db.flush()
        # 같은 자모 ㄱ 이 초성(90)·받침(40) 으로 갈린다 — 버킷이 섞이면 65 로 뭉개진다.
        db.add(Review(sentence_id=s1.sentence_id, counted=True, feedback={"phonemes": [
            _phon("ㄱ", 90, "초성"), _phon("ㄱ", 40, "종성"),
        ]}))
        db.add(Review(sentence_id=s2.sentence_id, counted=True, feedback={"phonemes": [
            _phon("ㄹ", 55, "종성"), _phon("ㅏ", 20, "중성"),
        ]}))
        db.flush()

        db.add_all([
            _lesson("onset_ㄱ", "초성 ㄱ", 0, position="초성", jamo="ㄱ"),
            _lesson("coda_ㄱ", "받침 ㄱ", 1, position="종성", jamo="ㄱ"),
            _lesson("coda_ㄹ", "받침 ㄹ", 2, position="종성", jamo="ㄹ"),
            _lesson("rule_연음", "연음", 3, type_="rule"),
        ])
        db.add_all([
            NationalSoundStat(country_iso="VN", country_name="Vietnam",
                              sound_key="coda_ㄹ", share=41, rank=1),
            NationalSoundStat(country_iso="VN", country_name="Vietnam",
                              sound_key="rule_연음", share=33, rank=2),
            NationalSoundStat(country_iso="VN", country_name="Vietnam",
                              sound_key="coda_ㄱ", share=28, rank=3),
        ])
        db.commit()
        return {"member_id": member.member_id, "call_id": call.call_id}
    finally:
        db.close()


# ── 목록 ──────────────────────────────────────────────────────────────────── #
def test_list_separates_onset_and_coda(session_factory, seeded):
    """같은 자모 ㄱ 이 초성 90 · 받침 40 으로 따로 집계된다(섞이면 둘 다 65)."""
    client = TestClient(_build_app(session_factory))
    body = client.get("/api/v1/pronunciation/weak-sounds", headers=_hdr()).json()
    by_key = {i["sound_key"]: i for i in body["national"]["items"] + body["mine"]}
    assert by_key["coda_ㄱ"]["score"] == 40
    assert by_key["onset_ㄱ"]["score"] == 90


def test_list_national_uses_speak_country_and_rank(session_factory, seeded):
    """국적별 목록은 억양 국가로 찾고 rank 순서를 지킨다. share 도 함께 내려간다."""
    client = TestClient(_build_app(session_factory))
    body = client.get("/api/v1/pronunciation/weak-sounds", headers=_hdr()).json()
    assert body["national"]["country"] == "Vietnam"
    assert [i["sound_key"] for i in body["national"]["items"]] == [
        "coda_ㄹ", "rule_연음", "coda_ㄱ",
    ]
    assert [i["share"] for i in body["national"]["items"]] == [41, 33, 28]
    # 표본 없는 소리는 점수 None — 카드가 「기록 없음」으로 그려진다.
    assert body["national"]["items"][1]["score"] is None


def test_list_unknown_country_returns_empty_not_error(session_factory, seeded):
    """통계에 없는 국가명이면 국적 섹션만 빈다 — 화면 전체를 죽이지 않는다."""
    db = session_factory()
    db.query(SpeakCountry).update({"first_country": "Narnia"})
    db.commit()
    db.close()
    client = TestClient(_build_app(session_factory))
    r = client.get("/api/v1/pronunciation/weak-sounds", headers=_hdr())
    assert r.status_code == 200
    assert r.json()["national"] == {"country": "Narnia", "items": []}


def test_list_excludes_vowels_and_duplicates(session_factory, seeded):
    """모음(sound_key None)은 목록에 없고, 국적별에 뜬 소리는 내 목록에서 빠진다."""
    client = TestClient(_build_app(session_factory))
    body = client.get("/api/v1/pronunciation/weak-sounds", headers=_hdr()).json()
    mine = {i["sound_key"] for i in body["mine"]}
    national = {i["sound_key"] for i in body["national"]["items"]}
    assert not any(k.startswith("vowel") or "ㅏ" in k for k in mine | national)
    assert mine.isdisjoint(national)


def test_recommend_first_national_below_80(session_factory, seeded):
    """추천 = 국적별 1순위 중 80점 미만(표본 없음도 포함) 첫 소리."""
    client = TestClient(_build_app(session_factory))
    body = client.get("/api/v1/pronunciation/weak-sounds", headers=_hdr()).json()
    assert body["recommended"] == "coda_ㄹ"  # 55점


def test_learned_score_overrides_aggregate(session_factory, seeded):
    """학습 점수가 복습 집계를 덮고 learned=True 로 표시된다."""
    db = session_factory()
    db.add(MemberSoundScore(member_id=seeded["member_id"], sound_key="coda_ㄹ",
                            score=92, best_score=92, attempts=1, baseline_score=55))
    db.commit()
    db.close()
    client = TestClient(_build_app(session_factory))
    body = client.get("/api/v1/pronunciation/weak-sounds", headers=_hdr()).json()
    item = next(i for i in body["national"]["items"] if i["sound_key"] == "coda_ㄹ")
    assert (item["score"], item["learned"], item["baseline_score"]) == (92, True, 55)
    # 1순위가 92점이 됐으니 추천은 다음 후보로 넘어간다.
    assert body["recommended"] == "rule_연음"


# ── 과(lesson) ────────────────────────────────────────────────────────────── #
def test_lesson_returns_full_payload(session_factory, seeded):
    """4단계 콘텐츠를 한 번에 내려준다(단계별 왕복 없음)."""
    client = TestClient(_build_app(session_factory))
    r = client.get("/api/v1/pronunciation/weak-sounds/coda_ㄹ/lesson", headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert set(body["payload"]) == {"how_to", "words", "sentence", "test"}
    assert len(body["payload"]["words"]) == 4
    assert body["score"] == 55 and body["learned"] is False


def test_lesson_404_unknown_sound(session_factory, seeded):
    client = TestClient(_build_app(session_factory))
    r = client.get("/api/v1/pronunciation/weak-sounds/coda_ㅋ/lesson", headers=_hdr())
    assert r.status_code == 404


# ── 평가 제출(서버 채점) ──────────────────────────────────────────────────── #
def test_assess_scores_on_server_with_lesson_text(session_factory, seeded, scorer):
    """서버가 lesson 의 평가 문장으로 채점한다 — 클라는 점수를 보내지 않는다."""
    client = TestClient(_build_app(session_factory))
    r = _post_audio(client, "coda_ㄹ", _hdr())
    assert r.status_code == 200, r.text
    assert scorer.calls == ["평가"]  # payload["test"]["text"]
    body = r.json()
    assert body["after"] == 84
    assert body["text"] == "평가"
    assert body["phoneme_misses"] == [{"char_index": 0, "expected": "ㅍ"}]


def test_assess_first_submit_sets_baseline(session_factory, seeded, scorer):
    """첫 제출: baseline=직전 집계(55), before=55, delta=+29."""
    client = TestClient(_build_app(session_factory))
    body = _post_audio(client, "coda_ㄹ", _hdr()).json()
    assert (body["before"], body["after"], body["delta"]) == (55, 84, 29)
    assert (body["best_score"], body["attempts"]) == (84, 1)
    db = session_factory()
    assert db.query(MemberSoundScore).one().baseline_score == 55
    db.close()


def test_assess_retry_keeps_baseline_and_best(session_factory, seeded, scorer):
    """재도전으로 점수가 내려가도 baseline·best 는 유지되고 attempts 만 늘어난다."""
    client = TestClient(_build_app(session_factory))
    _post_audio(client, "coda_ㄹ", _hdr())
    scorer.score = 71
    body = _post_audio(client, "coda_ㄹ", _hdr()).json()
    assert (body["before"], body["after"], body["delta"]) == (84, 71, -13)
    assert (body["best_score"], body["attempts"]) == (84, 2)
    db = session_factory()
    row = db.query(MemberSoundScore).one()  # 행이 둘 되면 여기서 터진다
    assert row.baseline_score == 55 and row.score == 71
    db.close()


def test_assess_clamps_out_of_range(session_factory, seeded, scorer):
    """벤더가 범위 밖 값을 줘도 0~100 으로 자른다."""
    scorer.score = 140
    client = TestClient(_build_app(session_factory))
    assert _post_audio(client, "rule_연음", _hdr()).json()["after"] == 100


def test_assess_no_baseline_when_no_sample(session_factory, seeded, scorer):
    """표본이 없던 소리는 baseline·before·delta 가 전부 null 이다."""
    scorer.score = 77
    client = TestClient(_build_app(session_factory))
    body = _post_audio(client, "rule_연음", _hdr()).json()
    assert body["before"] is None and body["delta"] is None and body["after"] == 77


def test_assess_tolerates_scorer_without_phoneme_misses(session_factory, seeded, monkeypatch):
    """dev 의 speechsuper 는 `phoneme_misses` 를 내지 않는다 — 빈 리스트로 내려앉는다.

    그 기능은 `feat/pronunciation` 에만 있다. 병합 전까지 이 경로가 깨지지 않아야 한다.
    """
    def bare(ref_text, audio_url=None, *, language="ko"):
        return {
            "evaluation": {"total_score": 80, "pronunciation": 80,
                           "fluency": 80, "rhythm": 80},
            "char_scores": [{"char": "평", "score": 80, "grade": "상"}],
            "phonemes": [],
        }

    monkeypatch.setattr(wsvc, "assess_pronunciation", bare)
    client = TestClient(_build_app(session_factory))
    body = _post_audio(client, "coda_ㄹ", _hdr()).json()
    assert body["after"] == 80
    assert body["phoneme_misses"] == []


def test_assess_404_unknown_sound(session_factory, seeded, scorer):
    client = TestClient(_build_app(session_factory))
    assert _post_audio(client, "coda_ㅋ", _hdr()).status_code == 404
    assert scorer.calls == []  # 없는 소리면 채점도 하지 않는다
