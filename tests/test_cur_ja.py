"""일본어 커리큘럼 2단계 배선(2026-09-13) — language 가 통화·라우터·dev 도구·저장소까지 간다 · 판정(quiz_judge) ja 분기 · 프롬프트 ja 줄 ·
힌트 reading. ko 는 그대로(기존 스위트 + 잠금 해시가 증거). sqlite 에 ko·ja 시드를 함께 적재해 «두 언어 공존» 을 그대로 흉내 낸다.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import core.deps as deps
import domains.learning.realtime.call_session as cs
from core.config import settings as app_settings
from core.languages import resolve_target_language
from core.prompts import freetalk as ft
from core.prompts.expression import build_expression_instruction
from core.prompts.locked import expression as lex
from core.prompts.locked import freetalk as lft
from core.prompts.locked import reground as lreg
from core.supabase_auth import AuthUser
from db.registry import Base
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.curriculum import CurMemberItem, CurMemberLesson, CurMemberProgress
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
from domains.learning.models.member_language_level import MemberLanguageLevel
from domains.learning.realtime.protocol import HintExample, ServerHint, server_adapter
from domains.learning.repository import curriculum_repository as repo
from domains.learning.service import curriculum_service as cur
from domains.learning.service import quiz_judge
from scripts.curriculum.load_cur_seed import load

SEED = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed.json")
SEED_JA = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed_ja.json")
pytestmark = pytest.mark.skipif(not (os.path.exists(SEED) and os.path.exists(SEED_JA)), reason="cur_seed*.json 없음")


def _fake_verify(token):
    return AuthUser(uid=token, email=f"{token}@test.io") if token and token.startswith("auth-") else None


@pytest.fixture(autouse=True)
def _auth(monkeypatch):
    monkeypatch.setattr(deps, "verify_token", _fake_verify)


@pytest.fixture(scope="module")
def factory():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            if len(t.primary_key.columns) == 1:
                pk.type = Integer()
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with sf() as s:
        for i in range(46):
            s.add(LearningItem(language="ko", kind="chunk", source_key=f"c:{i}", band=1, level_no=1, assign_rule="seed",
                               surface=f"청크 문장 {i}", meanings=json.dumps({"en": f"chunk {i}"}), examples="[]"))
        for i in range(46):   # ja 청크 46 — 운영 learning_item(kind=chunk, language=ja) 흉내: meanings {en,roman,ko} + reading 열(가나)
            s.add(LearningItem(
                language="ja", kind="chunk", source_key=f"jc:{i}", band=1, level_no=1, assign_rule="survival_v1",
                surface=f"チャンク{i}", reading=f"ちゃんく{i}", meanings=json.dumps({"en": f"chunk {i}", "roman": f"chanku{i}", "ko": f"청크 {i}"}), examples="[]",
            ))
        s.commit()
        load(s, json.load(io.open(SEED, encoding="utf-8")), dry_run=False); s.commit()
        load(s, json.load(io.open(SEED_JA, encoding="utf-8")), dry_run=False, language="ja"); s.commit()
        v = Voice(name="Fenrir", gender="male"); s.add(v); s.flush()
        s.add(Character(name="비비", role="선생님", personality="다정함", voice_id=v.voice_id, price=0))
        s.add(Level(language="ko", level_no=1, profile="초급 학습자"))
        s.add(Level(language="ja", level_no=2, profile="일본어 입문"))
        s.commit()
    return sf


@pytest.fixture()
def db(factory):
    s = factory()
    yield s
    s.rollback(); s.close()


@pytest.fixture()
def client(factory):
    from main import create_app
    app = create_app(); app.state.session_factory = factory; app.state.settings = app_settings; app.state.genai_client = object()
    return TestClient(app)


_n = {"i": 0}


def _member(db: Session, *, target: str | None, role: str = "member", locale: str = "en") -> tuple[int, dict]:
    _n["i"] += 1
    auth = f"auth-ja-{_n['i']}"
    m = Member(language=locale, korean_level=1, onboarding_completed=True, auth_user_id=auth, role=role, target_language=target, name="Taro")
    db.add(m); db.flush()
    if target == "ja":
        db.add(MemberLanguageLevel(member_id=m.member_id, language="ja", level_no=2))
    db.commit()
    return m.member_id, {"Authorization": f"Bearer {auth}"}


def _call(db: Session, member_id: int, call_type="expression") -> int:
    c = Call(member_id=member_id, character_id=1, call_type=call_type, status="done")
    db.add(c); db.commit()
    return c.call_id


# --------------------------------------------------------------------------- #
# ① 서비스 배선 — auto/decide_course · open_call · 진도가 언어별로 따로 간다
# --------------------------------------------------------------------------- #
def test_decide_course_and_progress_are_per_language(db):
    m, _ = _member(db, target="ja")
    assert cur.decide_course(db, m, "ja") == "expression"
    pj = repo.current_progress(db, m, "ja"); pk = repo.current_progress(db, m, "ko")
    assert pj is not None and repo.lesson_by_id(db, pj.lesson_id).code == "L1-S01-1" and pk is None, "ja 포인터만 생겼다(ja 도 청크 1차시부터)"
    assert cur.decide_course(db, m, "ko") == "expression"
    pk = repo.current_progress(db, m, "ko")
    assert repo.lesson_by_id(db, pk.lesson_id).code == "L1-S01-1" and pk.lesson_id != pj.lesson_id
    assert cur.available(db, "ja") and cur.available(db, "ko") and not cur.available(db, "zh")


def test_open_call_ja_selects_the_first_japanese_lesson_with_ko_or_en_meanings(db):
    m, _ = _member(db, target="ja", locale="ko")
    c = _call(db, m)
    o0 = cur.open_call(db, m, c, "auto", language="ja", locale="ko")
    assert o0.lesson.code == "L1-S01-1" and o0.lesson.no == 1 and o0.lesson.level_no == 1 and len(o0.items) == 15, "ja 도 청크 1차시부터(ko 와 동일)"
    assert o0.items[0]["obj"] == "チャンク0" and o0.items[0]["des"] == "청크 0" and o0.items[0]["role"] == "chunk"
    # 포인터를 A1-T01-1(no 4)로 옮겨 어휘·문법 차시를 본다
    prog = repo.current_progress(db, m, "ja"); prog.lesson_id = repo.lesson_by_no(db, "ja", 4).lesson_id; db.commit()
    c = _call(db, m)
    o = cur.open_call(db, m, c, "auto", language="ja", locale="ko")
    assert o.course == "expression" and o.lesson.code == "A1-T01-1" and o.lesson.no == 4 and o.lesson.level_no == 2
    assert o.lesson.language == "ja" and 1 <= len(o.items) <= 18 and all(d["review"] is False for d in o.items)
    vocab = [d for d in o.items if d["role"] != "grammar"]
    assert vocab and all(d["des"] for d in vocab), "뜻이 실린다"
    first = next(d for d in o.items if d["obj"] == "名前")
    assert first["des"] == "이름" and first["ex"] == "名前は田中一郎です。", "locale=ko → meanings.ko · 예문 1개 회전 첫 번째"
    o_en = cur.open_call(db, m, _call(db, m), "auto", language="ja", locale="en")
    assert o_en.resumed is False and next(d for d in o_en.items if d["obj"] == "名前")["des"] == "name", "locale=en → meanings.en"
    assert repo.cur_call(db, c).lesson_id == o.lesson.lesson_id


def test_freetalk_brief_ja_and_lock_is_per_language(db):
    m, _ = _member(db, target="ja")
    c = _call(db, m, "freetalk")
    with pytest.raises(cur.CourseLocked):
        cur.open_call(db, m, c, "freetalk", language="ja")
    # ja 차시 전부 드릴 → ja 프리토킹 열림. ko 는 여전히 잠김(언어별 상태)
    c2 = _call(db, m)
    o = cur.open_call(db, m, c2, "expression", language="ja")
    ids = [d["item_id"] for d in o.items]
    cur.record_expression(db, c2, o.items, drilled_ids=ids, passed_ids=ids, failed_ids=[])
    assert cur.me(db, m, "ja")["status"] == "expression_done" and cur.me(db, m, "ja")["next_course"] == "freetalk"
    assert cur.me(db, m, "ko")["status"] == "learning"
    o3 = cur.open_call(db, m, _call(db, m, "freetalk"), "auto", language="ja")
    assert o3.course == "freetalk" and o3.brief is not None and len(o3.brief.items) == o.lesson.item_count
    assert all(d["ex"] for d in o3.brief.items if d["role"] == "grammar")
    # 프리토킹 종료 → ja 포인터만 다음 차시로(ko 포인터 무변화)
    ko_before = repo.current_progress(db, m, "ko").lesson_id
    assert cur.complete_freetalk(db, o3 and repo.member_call_ids(db, m, "ja")[-1], duration_s=200, normal_end=True) == {"freetalk_done": True, "moved": True}
    assert repo.lesson_by_id(db, repo.current_progress(db, m, "ja").lesson_id).no == 2
    assert repo.current_progress(db, m, "ko").lesson_id == ko_before


def test_review_pool_is_separated_by_language(db):
    m, _ = _member(db, target="ja")
    # ko·ja 각 한 통화씩 드릴
    for lang in ("ko", "ja"):
        c = _call(db, m); o = cur.open_call(db, m, c, "expression", language=lang)
        cur.record_expression(db, c, o.items, drilled_ids=[d["item_id"] for d in o.items], passed_ids=[], failed_ids=[])
    ja_pool = repo.review_pool(db, m, frozenset(), 50, language="ja")
    ko_pool = repo.review_pool(db, m, frozenset(), 50, language="ko")
    assert ja_pool and ko_pool
    assert all(it.language == "ja" for _mi, it in ja_pool) and all(it.language == "ko" for _mi, it in ko_pool)
    # select_items 의 복습 채움도 그 차시 언어만 — ja 차시 2 를 열면 ko 항목이 섞이지 않는다
    l2 = repo.lesson_by_no(db, "ja", 2)
    items = cur.select_items(db, m, l2.lesson_id, locale="en")
    assert all(repo.lesson_by_id(db, d["lesson_id"]).language == "ja" for d in items)


# --------------------------------------------------------------------------- #
# ② 라우터 · dev reset — 회원 target_language 로 언어를 푼다(통화와 같은 해석기)
# --------------------------------------------------------------------------- #
def test_resolver_is_shared_and_falls_back_to_default():
    assert resolve_target_language("ja", default_code="ko").code == "ja"
    assert resolve_target_language(None, default_code="ko").code == "ko"
    assert resolve_target_language("xx", default_code="ko").code == "ko"
    assert resolve_target_language("일본어", default_code="ko").code == "ja", "구 데모 라벨도 통화와 같이 구제"
    assert cs._resolve_target_language(app_settings, "ja").code == "ja"


def test_cur_me_and_lessons_follow_the_members_target_language(client, db):
    _, hj = _member(db, target="ja")
    _, hk = _member(db, target=None)
    me_j = client.get("/api/v1/cur/me", headers=hj).json()
    assert me_j["lesson"]["code"] == "L1-S01-1" and me_j["lesson"]["no"] == 1 and me_j["lesson"]["level_no"] == 1, "ja 도 청크 1차시부터"
    assert me_j["items_total"] == 15 and me_j["next_course"] == "expression"
    me_k = client.get("/api/v1/cur/me", headers=hk).json()
    assert me_k["lesson"]["code"] == "L1-S01-1", "target_language 없음 → 기본(ko)"
    rows = client.get("/api/v1/cur/lessons", params={"level": 2}, headers=hj).json()
    assert len(rows) == 8 and rows[0]["code"] == "A1-T01-1" and rows[0]["no"] == 4 and rows[0]["status"] is None
    assert all(r["code"].startswith("A1-") for r in rows)
    allrows = client.get("/api/v1/cur/lessons", headers=hj).json()
    assert len(allrows) == 348 and allrows[0]["code"] == "L1-S01-1" and allrows[0]["status"] == "learning"
    assert len(client.get("/api/v1/cur/lessons", params={"level": 1}, headers=hj).json()) == 3


def test_cur_reset_deletes_only_the_target_language(client, db):
    m, hdr = _member(db, target="ja", role="admin")
    for lang in ("ko", "ja"):
        c = _call(db, m); o = cur.open_call(db, m, c, "expression", language=lang)
        cur.record_expression(db, c, o.items, drilled_ids=[d["item_id"] for d in o.items][:3], passed_ids=[], failed_ids=[])
    ko_items = db.query(CurMemberItem).join(CurMemberItem.lesson if hasattr(CurMemberItem, "lesson") else CurMemberItem).count() if False else \
        db.execute(text("SELECT COUNT(*) FROM cur_member_item mi JOIN cur_lesson l ON l.lesson_id=mi.lesson_id WHERE mi.member_id=:m AND l.language='ko'"), {"m": m}).scalar()
    assert ko_items > 0
    r = client.post("/__dev/cur-reset", json={"lesson_no": 5}, headers=hdr)                # no 1~3 = L1 청크, 4 = A1-T01-1
    assert r.status_code == 200, r.text
    assert r.json()["language"] == "ja" and r.json()["lesson"]["code"].startswith("A1-") and r.json()["lesson"]["no"] == 5
    assert r.json()["deleted_calls"] == 1, "ja cur_call 만"
    db.expire_all()
    q = "SELECT COUNT(*) FROM {t} x JOIN cur_lesson l ON l.lesson_id=x.lesson_id WHERE x.member_id=:m AND l.language=:lang"
    assert db.execute(text(q.format(t="cur_member_item")), {"m": m, "lang": "ja"}).scalar() == 0
    assert db.execute(text(q.format(t="cur_member_lesson")), {"m": m, "lang": "ja"}).scalar() == 0
    assert db.execute(text(q.format(t="cur_member_item")), {"m": m, "lang": "ko"}).scalar() == ko_items, "ko 진도는 그대로"
    assert repo.lesson_by_id(db, repo.current_progress(db, m, "ja").lesson_id).no == 5
    assert repo.lesson_by_id(db, repo.current_progress(db, m, "ko").lesson_id).no == 1
    assert db.execute(text("SELECT COUNT(*) FROM cur_call c JOIN cur_lesson l ON l.lesson_id=c.lesson_id WHERE l.language='ko'")).scalar() >= 1


def test_cur_reset_ja_points_at_lesson_1_which_is_the_survival_chunk_lesson(client, db):
    """사장님(2026-09-13): «학습 초기화하면 이제 일상회화(0단계)로 되게» — ja 도 no=1 = L1-S01-1(청크)."""
    m, hdr = _member(db, target="ja", role="admin")
    prog = repo.current_progress(db, m, "ja") or cur.ensure_progress(db, m, "ja")
    prog.lesson_id = repo.lesson_by_no(db, "ja", 7).lesson_id; db.commit()          # 진도를 A1 중간까지 옮겨 두고
    r = client.post("/__dev/cur-reset", json={}, headers=hdr)                         # lesson_no 없음 → 1
    assert r.status_code == 200, r.text
    assert r.json()["language"] == "ja" and r.json()["lesson"] == {"no": 1, "code": "L1-S01-1"}
    db.expire_all()
    lesson = repo.lesson_by_id(db, repo.current_progress(db, m, "ja").lesson_id)
    assert (lesson.no, lesson.code, lesson.level_no, lesson.language) == (1, "L1-S01-1", 1, "ja")
    assert repo.lesson_items(db, lesson.lesson_id)[0][1].kind == "chunk" and len(repo.lesson_items(db, lesson.lesson_id)) == 15
    me = client.get("/api/v1/cur/me", headers=hdr).json()
    assert me["lesson"]["code"] == "L1-S01-1" and me["lesson"]["level_no"] == 1 and me["items_total"] == 15
    # 서비스 직접 호출도 같다
    assert cur.reset(db, m, None, "ja")["lesson_code"] == "L1-S01-1"


# --------------------------------------------------------------------------- #
# ③ 판정(quiz_judge) ja 분기 — 5건 · ko 무변화
# --------------------------------------------------------------------------- #
def test_ja_normalize_is_nfkc_and_strips_japanese_punctuation_without_ko_rules():
    assert quiz_judge.normalize("私は　カーラです。", "ja") == "私はカーラです"
    assert quiz_judge.normalize("ＡＢＣ！？", "ja") == "abc"
    assert quiz_judge.normalize("어디에요", "ja") == "어디에요" and quiz_judge.normalize("어디에요") == "어디예요", "에요→예요 는 ko 만"


def test_ja_word_mentions_uses_the_next_character_as_the_boundary():
    assert quiz_judge.mentions("名前は田中一郎です。", "名前", "ja") is True
    assert quiz_judge.mentions("名前は田中一郎です。", "名", "ja") is False, "「名」←「名前」 (뒤가 한자)"
    assert quiz_judge.mentions("私は日本語ができます。", "日本語", "ja") is True
    assert quiz_judge.mentions("私は日本語ができます。", "日本", "ja") is False
    assert quiz_judge.mentions("水を飲みます", "水", "ja") is True and quiz_judge.mentions("水曜日です", "水", "ja") is False


def test_ja_template_and_formality():
    assert quiz_judge.is_template("～は～です", "ja") and quiz_judge.template_mentions("私はカーラです。", "～は～です", "ja")
    assert quiz_judge.template_mentions("カーラです", "～は～です", "ja") is False
    assert quiz_judge.polite_marker("～は～です", "ja") == "です" and quiz_judge.polite_marker("～ができる", "ja") is None
    assert quiz_judge.keeps_formality("私はカーラです。", "～は～です", "ja") is True
    assert quiz_judge.keeps_formality("私はカーラだ", "～は～です", "ja") is False
    assert quiz_judge.keeps_formality("水をください。それから…", "～をください", "ja") is True


def test_ja_item_mentioned_accepts_the_example_sentence():
    assert quiz_judge.item_mentioned("わたしはカーラです", "～は～です", "私はカーラです。", "ja") is True
    assert quiz_judge.item_mentioned("私はカーラです", "～は～です", "私はカーラです。", "ja") is True
    assert quiz_judge.item_mentioned("こんにちは", "～は～です", "私はカーラです。", "ja") is False


def test_ko_judge_is_unchanged_by_the_language_branch():
    assert quiz_judge.mentions("어제 선물을 받았어요", "물") is False and quiz_judge.mentions("물이 있어요", "물") is True
    assert quiz_judge.keeps_formality("이거 얼마예요?", "이거 얼마예요?") is True and quiz_judge.keeps_formality("이거 얼마야", "이거 얼마예요?") is False
    assert quiz_judge.is_template("～は～です") is False, "ko 자리표시 표엔 ～ 가 없다(ko 표면형에 없어 무변화)"
    assert quiz_judge.item_mentioned("나무가 타요.", "V-아요/어요", "나무가 타요.") is True


# --------------------------------------------------------------------------- #
# ④ 프롬프트 — 격식 줄 언어별 · probes 「〜さん」 · 힌트 reading
# --------------------------------------------------------------------------- #
def test_expression_prompt_ja_uses_the_japanese_formality_line_and_ko_is_unchanged():
    items = [{"obj": "名前", "des": "이름", "ex": "名前は田中一郎です。", "role": "must"}, {"obj": "～は～です", "des": "N is N", "ex": "私はカーラです。", "role": "grammar"}]
    kw = dict(role="r", personality="p", level_profile="l", locale="ko", interests=[], name="Taro", quiz_group=3)
    ja = build_expression_instruction(**kw, items=items, target_language="일본어", language="ja")
    ko = build_expression_instruction(**kw, items=items, target_language="일본어")
    assert "격식 표지(です·ます)" in ja and "격식 표지(-요·-습니다·저)" not in ja
    assert "격식 표지(-요·-습니다·저)" in ko and "です·ます" not in ko, "language 를 안 주면 ko 줄(종전)"
    assert "[문형] ～は～です — 뜻: N is N — 연습 문장: \"私はカーラです。\"" in ja
    assert lex.DRILL_FORMALITY_LINE_BY_LANGUAGE["ko"] is lex.DRILL_FORMALITY_LINE


def test_freetalk_probes_replace_san_for_ja_and_ssi_for_ko():
    b = cur.CurFreetalkBrief(situation="初対面", partner="クラスメート", probes=["マイケルさんはどこから来ましたか。", "마이클 씨는 어느 나라 사람이에요?"],
                             items=[{"obj": "名前", "ex": "名前は田中です。", "role": "must"}])
    ja = ft.build_freetalk_instruction(role="r", personality="p", level_profile="l", locale="en", interests=[], name="Taro", max_sentences=2, lesson=b, language="ja")
    assert "Taroさんはどこから来ましたか。" in ja and "마이클 씨는" in ja, "ja 패턴만 — ko 「씨」 는 ja 모드에서 안 건드린다"
    ko = ft.build_freetalk_instruction(role="r", personality="p", level_profile="l", locale="en", interests=[], name="Taro", max_sentences=2, lesson=b)
    assert "Taro 씨는 어느 나라" in ko and "マイケルさん" in ko
    assert lft.PROBE_NAME_RE_BY_LANGUAGE["ko"][0] is lft.PROBE_NAME_RE


def test_hint_reading_is_optional_and_absent_for_ko_frames():
    ko = server_adapter.dump_json(ServerHint(turn_id="t", examples=[HintExample(korean="물 주세요", roman="mul juseyo", native="water please")])).decode()
    assert '"reading"' not in ko and ko == '{"type":"hint","turn_id":"t","examples":[{"korean":"물 주세요","roman":"mul juseyo","native":"water please"}]}'
    ja = json.loads(server_adapter.dump_json(ServerHint(turn_id="t", examples=[HintExample(korean="水をください", roman="mizu o kudasai", native="water please", reading="みずをください")])).decode())
    assert ja["examples"][0]["reading"] == "みずをください"
    assert lreg.hint_reading_clause("ko") == "" and "ひらがな" in lreg.hint_reading_clause("ja")
    assert cs._hint_instruction("한국어", "일본어", language="ja").endswith(lreg.hint_reading_clause("ja"))
    assert cs._hint_instruction("영어(English)", "한국어") == cs._hint_instruction("영어(English)", "한국어", language="ko")


# --------------------------------------------------------------------------- #
# ⑤ 통화 경로 — target_language=ja 회원의 auto 통화가 ja 차시로 열리고 ja 진도에 저장된다
# --------------------------------------------------------------------------- #
class _WS:
    def __init__(self, incoming):
        self._incoming = list(incoming); self.sent_text = []; self.closed_with = None
        from starlette.websockets import WebSocketState
        self._WS = WebSocketState; self.client_state = WebSocketState.CONNECTED
    async def receive(self): return self._incoming.pop(0) if self._incoming else {"type": "websocket.disconnect"}
    async def send_text(self, t): self.sent_text.append(t)
    async def send_bytes(self, b): pass
    async def close(self, code=None): self.closed_with = code; self.client_state = self._WS.DISCONNECTED


class _Sess:
    def __init__(self, script): self.sent_text_turns = []; self.script = script
    async def send_audio(self, b): pass
    async def send_text_turn(self, t): self.sent_text_turns.append(t)
    async def send_reground(self, t, *, turn_complete=True): self.sent_text_turns.append(t)
    async def events(self):
        for role, txt in self.script:
            if role == "B":
                yield cs.LiveEvent(kind="out_tr", text=txt); yield cs.LiveEvent(kind="turn_end")
            else:
                yield cs.LiveEvent(kind="in_tr", text=txt, is_final=True)


@pytest.mark.asyncio
async def test_run_call_auto_for_a_japanese_learner_opens_the_ja_lesson_and_records_ja_progress(factory, monkeypatch):
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 0.2)
    import domains.learning.service.normalcall_service as svc
    monkeypatch.setattr(svc.storage, "upload", lambda *a, **k: "stub-key")
    monkeypatch.setattr(svc.storage, "public_url", lambda *a, **k: "https://stub/url.mp3")

    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(svc.tts, "synthesize", _none)
    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", _none)
    db = factory()
    m, _ = _member(db, target="ja", locale="ko")
    db.close()
    holder = {}

    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **_kw):
        sess = _Sess([("B", "チャンク0 と チャンク1 。"), ("U", "チャンク2"), ("B", "次は チャンク3 です。")])
        holder["session"] = sess; holder["si"] = system_instruction
        yield sess

    ws = _WS([{"type": "websocket.receive", "text": json.dumps({"type": "start", "character_id": 1, "call_type": "auto"})}])
    await cs.run_call(ws, app_settings, object(), factory, member_id=m, member_target_language="ja", live_session_factory=_f)   # ws_router 가 넘기는 값
    for _ in range(300):
        if not cs._analysis_tasks:
            break
        await asyncio.sleep(0.01)
    frames = [json.loads(t) for t in ws.sent_text]
    started = next(f for f in frames if f.get("type") == "call_started")
    assert started["course"] == "expression"
    si = holder["si"]
    assert "일본어" in si and "チャンク0" in si and "격식 표지(です·ます)" in si and "[문형]" not in si, "ja 1차시 = 청크(문형 없음)"
    db = factory()
    try:
        call = db.query(Call).filter(Call.member_id == m).order_by(Call.call_id.desc()).first()
        cc = repo.cur_call(db, call.call_id)
        lesson = repo.lesson_by_id(db, cc.lesson_id)
        assert lesson.language == "ja" and lesson.code == "L1-S01-1" and cc.recorded_at is not None
        mine = repo.member_item_map(db, m, lesson.lesson_id)
        drilled = {iid for iid, r in mine.items() if r.drilled_at is not None}
        assert len(drilled) == 4, "비버 チャンク0·1·3 + 학습자 チャンク2 → drilled 4(ja 판정 분기: 뒤 글자 경계 — 「チャンク0」 ≠ 「チャンク01」)"
        assert repo.current_progress(db, m, "ko") is None, "ko 진도는 만들지 않았다"
    finally:
        db.close()
