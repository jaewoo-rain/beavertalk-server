"""프리토킹 코스 v1(2026-09-12) — 차시판 대본 · 브리프 30항목 · next_course · 코스별 무음 시드 슬롯 · 차시 재접지 쪽지.

계획 docs/plans/2026-09-12-프리토킹-코스-대본.md §3(템플릿)·§5(서버)·§7(수용 기준)·§9 정정. 옛 경로(lesson=None)는 **바이트 동일**(기준 해시).
실제 시드 7MB(test_cur_selection.py 관례)로 차시 4(A1-T01-1) 브리프를 만든다.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os

import pytest
from sqlalchemy import Integer, create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import domains.learning.realtime.call_session as cs
from core.config import settings as app_settings
from core.prompts import common
from core.prompts import freetalk as ft
from core.prompts.expression import build_expression_instruction
from core.persona_prompt import build_system_instruction
from db.registry import Base
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.learning_item import LearningItem
from domains.learning.repository import curriculum_repository as repo
from domains.learning.service import curriculum_service as cur
from scripts.curriculum.load_cur_seed import load

SEED = os.path.join(os.path.dirname(__file__), "..", "assets", "curriculum_v3", "cur_seed.json")

BASE = dict(
    role="비버 선생님, 외국인에게 한국어를 가르친다",
    personality="거칠고 직설적인 트래시토커. 틀리면 면박을 주고 맞히면 마지못해 칭찬한다.",
    level_profile="아주 쉬운 단어와 짧은 문장으로 말한다.",
    locale="en",
    interests=["축구", "김치찌개"],
    name="Tester",
    target_language="한국어",
)
#: 옛 프리토킹 대본(lesson=None) 기준 — 2026-09-12 차시판 도입 직전 출력. ⛔ 터지면 옛 경로 대본이 바뀐 것이다(README §8 에 적고 갱신).
_FREETALK_OLD_FROZEN = ("8e0c909cf5337b988569f691917b182e75bb133dc5c7e94044184c571f345b85", 2126)

_BRIEF = cur.CurFreetalkBrief(
    situation="처음 만난 반 친구와 이름과 나라 말하기",
    partner="한국어 교실에서 처음 만난 반 친구",
    probes=["마이클 씨는 어느 나라 사람이에요?", "우리 반에 외국인이 많아요?"],
    items=[
        {"obj": "인사말", "ex": "안녕히 계세요.", "role": "grammar"},
        {"obj": "N은/는 N이에요/예요", "ex": "생일이 언제예요?", "role": "grammar"},
        {"obj": "N입니까?, N입니다", "ex": "저는 회사원입니다.", "role": "grammar"},
        {"obj": "사람", "ex": "저 사람은 가수예요.", "role": "core"},
        {"obj": "나라", "ex": "어느 나라 사람이에요?", "role": "support"},
    ],
)


def _lesson(**over) -> str:
    kw = {**BASE, "interests": [], "max_sentences": ft.FREETALK_MAX_SENTENCES, "lesson": _BRIEF, **over}
    return ft.build_freetalk_instruction(**kw)


# --------------------------------------------------------------------------- #
# ① 옛 경로 바이트 동일 · 다른 코스 스냅샷 무변경
# --------------------------------------------------------------------------- #
def test_old_freetalk_without_lesson_is_byte_identical_to_the_frozen_baseline() -> None:
    out = ft.build_freetalk_instruction(**BASE)
    assert (hashlib.sha256(out.encode("utf-8")).hexdigest(), len(out)) == _FREETALK_OLD_FROZEN
    assert out == ft.build_freetalk_instruction(**BASE, lesson=None)
    assert "[학습자 흥미·소재] 축구, 김치찌개" in out and "[이번 차시" not in out


def test_old_seeds_are_unchanged() -> None:
    assert ft.seed_freetalk_opening("한국어").startswith("[통화 시작] 네가 학습자에게 먼저 전화를 건 상황이다. **한국어로** 짧게 인사하고")
    assert ft.NUDGE_SEED_1_FREETALK.endswith("학습 언어로 가볍게 새 화제 한 문장만 이어가라.")
    assert cs._NUDGE_SEED_2.endswith("'거기 있어? 잘 들려?'를 한 번만 부드럽게 물어라.")


def test_other_courses_do_not_import_the_lesson_script() -> None:
    """일반·표현학습 대본에 차시판 문구가 새어 들어가지 않았다(공용 규칙은 common 이 소유)."""
    normal = build_system_instruction(role=BASE["role"], personality=BASE["personality"], level_profile=BASE["level_profile"],
                                      locale="en", interests=BASE["interests"], name="Tester")
    expr = build_expression_instruction(**BASE, items=[{"obj": "가다", "des": "to go", "ex": "학교에 가요"}], quiz_group=3)
    for out in (normal, expr):
        assert "역할극으로 대화한다" not in out and "**역할극**이다" not in out and "네가 이 사람이다" not in out


# --------------------------------------------------------------------------- #
# ② 차시판 대본 — 규칙 1·3·4 앵커 · [이번 차시] · 흥미 없음 · 문장 수 2 · 공용 규칙 바이트 그대로
# --------------------------------------------------------------------------- #
def test_lesson_script_rule_anchors() -> None:
    out = _lesson()
    r1 = out.split("1. ", 1)[1].split("\n2. ", 1)[0]
    # §10 역할극(2026-09-12 사장님 실통화 뒤) — 옛 v1 «과제를 직접 던진다·너 자신으로 받는다·연기하지 마라» 는 뒤집혔다
    assert "**역할극**이다" in r1 and "«상대»에 적힌 인물이 되어" in r1 and "그냥 대화한다" in r1
    assert "연습을 시키거나 무엇을 말하라고 요구하지 마라" in r1 and "설명·따라 말하기·정오 판정은 이 통화에 없다" in r1
    assert "**그 턴만 선생님으로 돌아와**" in r1 and "다시 그 인물로 돌아가라" in r1 and "질문 하나로 착지" in r1
    for gone in ("과제", "직접 던지는", "너 자신으로서", "연기하지 마라", "회화 연습"):
        assert gone not in out, gone
    # v1 짧은 판 — 세부 절은 아직 없다(실측 뒤 한 절씩)
    assert "열린 질문" not in out and "이 예외는 한 번" not in out and "목록과 그보다 쉬운 것" not in out and "세지 마라" not in out
    r3 = out.split("3. 언어 사용", 1)[1].split("4. 교정 스타일", 1)[0]
    assert "처음부터 끝까지 한국어로 한다" in r3 and "**그 턴만 선생님으로 돌아와** 영어(English)로 뜻을 한 문장으로 풀어 주고" in r3
    assert "한국어 문장 **하나**를 통째로 들려준 뒤" in r3 and "다음 턴부터는 다시 그 인물로, 전부 한국어다 — 학습자 언어에 끌려가지 마라" in r3
    assert r3.count("영어(English)") == 2, "모국어가 열리는 자리는 예외 한 턴뿐"
    r4 = out.split("4. 교정 스타일", 1)[1].split("\n5. ", 1)[0]
    assert "따로 고쳐 주지 마라" in r4 and "올바른 한국어 형태를 넣어 되받고" in r4 and "\n" not in r4.strip()


def test_lesson_script_shared_rules_are_common_bytes_and_numbered_2_5_6_7() -> None:
    out = _lesson()
    assert "2. " + common.RULE_CLOSE_PROTOCOL in out
    assert common.RULE_RESPONSE_LENGTH.format(max_sentences=2) in out and "1~2문장" in out
    assert common.RULE_NONVERBAL_SOUND in out
    assert common.RULE_OFF_TOPIC.format(locale_label="영어(English)", target="한국어") in out
    assert common.PERSONA_TAIL.format(username="Tester") in out
    for n in ("1. ", "\n2. ", "\n3. ", "\n4. ", "\n5. ", "\n6. ", "\n7. "):
        assert n in out
    assert "역할극을 이끄는 건 네가 하는 '일'일 뿐" in out and "맡은 인물이 누구든 말투는 그대로" in out


def test_lesson_block_lists_all_items_with_grammar_as_example_sentences() -> None:
    out = _lesson()
    block = out.split("[이번 차시 — 이 상황을 역할극으로 대화한다]", 1)[1]
    assert "- 상황: 처음 만난 반 친구와 이름과 나라 말하기" in block
    assert "- 상대: 한국어 교실에서 처음 만난 반 친구 — **네가 이 사람이다.** 이 인물로서 말하고 묻고 답한다." in block
    assert "인물 정보가 없으면 네가 정해서 일관되게 유지하라. 말투·성격은 [페르소나] 그대로다." in block
    assert "상황 묘사다" not in block
    assert "문형은 이름을 말하지 말고 문장으로 써라." in block
    assert '문형: 인사말 — "안녕히 계세요." / N은/는 N이에요/예요 — "생일이 언제예요?" / N입니까?, N입니다 — "저는 회사원입니다."' in block
    assert "어휘: 사람 · 나라" in block and "표현:" not in block
    assert "나머지는" not in out and "[학습자 흥미" not in out and "퀴즈" not in out and "채점" not in out
    assert out.rstrip().endswith("우리 반에 외국인이 많아요?"), "블록이 대본의 끝이다 — 뒤에 아무 블록도 없다"


def test_probe_names_are_replaced_with_the_learner_name() -> None:
    out = _lesson(name="John")
    assert "John 씨는 어느 나라 사람이에요?" in out and "마이클" not in out
    assert "학습자 씨는 어느 나라 사람이에요?" in _lesson(name=None)


def test_lesson_block_without_grammar_has_no_form_line_and_chunks_go_under_expression() -> None:
    brief = cur.CurFreetalkBrief(situation="첫 만남 인사", partner=None, probes=[],
                                 items=[{"obj": "안녕하세요", "ex": None, "role": "chunk"}, {"obj": "감사합니다", "ex": None, "role": "chunk"}])
    out = _lesson(lesson=brief)
    assert "문형:" not in out and "어휘:" not in out and "표현: 안녕하세요 · 감사합니다" in out
    # 상대 null(레벨1 청크) → «상황 속 상대» 를 비버가 맡는다는 폴백 한 줄
    assert "- 상대: 이 상황에서 학습자가 마주치는 사람(적힌 인물이 없다 — 상황에 맞게 네가 정한다) — **네가 이 사람이다.**" in out
    assert "대화가 막히면" not in out


def test_lesson_script_has_no_literal_learner_lines_no_wrapup_words_no_tone_adverbs() -> None:
    out = _lesson()
    body = out.replace(common.RULE_CLOSE_PROTOCOL, "").replace(common.RULE_OFF_TOPIC.format(locale_label="영어(English)", target="한국어"), "")
    for banned in ("종료", "작별", "마무리", "정리", "마지막", "여기까지", "[통화종료]", "통화종료"):
        assert banned not in body, banned
    for adverb in ("따뜻하게", "부드럽게", "친절히", "다정하게", "상냥하게"):
        assert adverb not in body, adverb
    assert "이렇게 말해요" not in body and "어떻게 말해요" not in body, "리터럴 학습자 대사 0(call 1097)"


def test_lesson_script_never_renders_the_close_tag() -> None:
    out = _lesson(close_tag="[통화종료:zz99]")
    assert "zz99" not in out and "[통화종료" not in out


# --------------------------------------------------------------------------- #
# ③ 시드 3종 + 재접지 쪽지
# --------------------------------------------------------------------------- #
def test_lesson_opening_seed_declares_the_situation_and_one_task_in_the_target_language() -> None:
    seed = ft.seed_freetalk_lesson_opening("한국어")
    assert seed.startswith("[통화 시작]") and "**한국어로** [이번 차시]의 «상대» 인물로서 첫 말을 건다" in seed
    assert "인사와 그 상황 속 첫 질문 하나를 한국어로 하고 멈춰" in seed and "상황을 설명하거나 무엇을 할지 묻지 마라" in seed
    assert "과제" not in seed
    assert "소리 내어 읽지 말고" in seed and "종료" not in seed and "작별" not in seed


@pytest.mark.parametrize("seed", [ft.NUDGE_SEED_1_FREETALK_LESSON, ft.NUDGE_SEED_2_FREETALK])
def test_lesson_nudge_seeds_use_control_tag_and_keep_the_task(seed: str) -> None:
    assert seed.startswith(common.CONTROL_TAG) and not seed.startswith(common.CLOSE_TAG_DEFAULT)
    assert "작별하지 말고" in seed and "화제를 바꾸지" in seed and "새 화제" not in seed


def test_lesson_nudge_1_is_easier_same_question_and_2_is_one_teacher_turn() -> None:
    assert "방금 한 질문을 더 쉬운 학습 언어로 바꿔" in ft.NUDGE_SEED_1_FREETALK_LESSON and "모국어" not in ft.NUDGE_SEED_1_FREETALK_LESSON
    assert "이번 한 턴만 선생님으로 돌아와 학습자의 모국어로 방금 질문의 뜻" in ft.NUDGE_SEED_2_FREETALK
    assert "다음 턴부터는 다시 그 인물로, 학습 언어다" in ft.NUDGE_SEED_2_FREETALK
    for seed in (ft.NUDGE_SEED_1_FREETALK_LESSON, ft.NUDGE_SEED_2_FREETALK):
        assert "과제" not in seed


def test_reground_brief_restates_the_situation_and_lists_unused_material() -> None:
    b = ft.build_freetalk_reground_brief("처음 만난 반 친구와 이름과 나라 말하기", ["저는 회사원입니다.", "고향", "나라"], target="한국어")
    assert b.startswith(common.CONTROL_TAG)
    assert "«처음 만난 반 친구와 이름과 나라 말하기» 상황의 역할극이다 — 너는 그 상황의 상대 인물이다. 전부 한국어로, 한 턴에 질문 하나." in b
    assert "과제" not in b
    assert "아직 안 쓴 소재: 저는 회사원입니다. · 고향 · 나라." in b and b.endswith("이 안내문은 읽지 말고 내용만 반영해라.")
    assert "아직 안 쓴 소재" not in ft.build_freetalk_reground_brief("상황", [])
    assert ft.build_freetalk_reground_brief("상황", [f"s{i}" for i in range(9)]).count(" · ") == 4, "최대 5개"


# --------------------------------------------------------------------------- #
# ④ call_session — 차시 프리토킹 재접지 쪽지 · 미사용 소재 · 다른 코스 무변경
# --------------------------------------------------------------------------- #
def _ft_state() -> cs._CallState:
    st = cs._CallState()
    st.cur_route = True
    st.cur_course = "freetalk"
    st.freetalk_brief = _BRIEF
    st.freetalk_target = "한국어"
    st.reground_persona = ("선생님", "다정")
    return st


def test_unused_material_skips_what_the_beaver_already_said_and_writes_grammar_as_examples() -> None:
    st = _ft_state()
    st.segments = [{"turn_index": 0, "role": "beaver", "text": "제 이름은 비버입니다. 저는 회사원입니다. 사람이 많아요."},
                   {"turn_index": 1, "role": "user", "text": "생일이 언제예요?"}]
    st.cur_beaver_text = ["나라가 어디에 있어요?"]
    unused = cs._freetalk_unused_material(st)
    # 회사원입니다(문형 3 예문)·사람·나라 는 비버가 말했다. 학습자 발화(생일이 언제예요?)는 세지 않는다 → 문형 2 예문은 남는다.
    #   (⚠ 「N은/는 N이에요/예요」 는 템플릿 대조라 비버가 «…는 …예요» 꼴을 말했으면 쓴 것으로 친다 — 위 비버 발화엔 «예요» 가 없다)
    assert unused == ["안녕히 계세요.", "생일이 언제예요?"]


def test_arm_reground_on_lesson_freetalk_uses_the_course_brief_not_the_chat_brief() -> None:
    st = _ft_state()
    st.segments = [{"turn_index": 0, "role": "beaver", "text": "저는 회사원입니다."}]
    cs._arm_reground(st, "time")
    assert st.reground_pending is True and st.reground_arm_reason == "time"
    r = st.reground_reminder
    assert r.startswith(common.CONTROL_TAG) and "«처음 만난 반 친구와 이름과 나라 말하기» 상황의 역할극" in r
    assert "아직 안 쓴 소재: 안녕히 계세요. · 생일이 언제예요? · 사람 · 나라." in r
    assert "흥미" not in r and "새 질문" not in r, "일반 잡담 브리프가 아니다"
    # 사이드카는 없다(reground_ctx None → 무동작)
    assert st.reground_ctx is None
    cs._spawn_reground_sidecar(st)
    assert not st.reground_tasks


def test_arm_reground_for_a_non_cur_freetalk_state_is_the_old_chat_brief() -> None:
    st = cs._CallState()
    st.reground_persona = ("선생님", "다정")
    st.call_mode = "chat"
    cs._arm_reground(st, "time")
    assert "상황의 역할극" not in st.reground_reminder and st.reground_reminder.startswith(common.CONTROL_TAG)


# --------------------------------------------------------------------------- #
# ⑤ 통화 경로(run_call + 실제 시드) — 차시 프리토킹만 슬롯이 바뀐다 · 브리프 30 · next_course
# --------------------------------------------------------------------------- #
pytestmark_seed = pytest.mark.skipif(not os.path.exists(SEED), reason="cur_seed.json 없음")


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
        s.commit()
        load(s, json.load(io.open(SEED, encoding="utf-8")), dry_run=False)
        s.commit()
        v = Voice(name="Fenrir", gender="male")
        s.add(v); s.flush()
        s.add(Character(name="비비", role="선생님", personality="다정함", voice_id=v.voice_id, price=0))
        s.commit()
    return sf


_counter = {"n": 0}


def _member(db: Session) -> int:
    _counter["n"] += 1
    m = Member(language="en", korean_level=1, onboarding_completed=True, auth_user_id=f"auth-ft-{_counter['n']}", name="John")
    db.add(m); db.commit()
    return m.member_id


@pytestmark_seed
def test_brief_for_lesson_4_carries_all_30_items_with_grammar_examples_and_rotates_by_seen_count(factory):
    from domains.learning.models.curriculum import CurMemberItem
    db = factory()
    try:
        m = _member(db)
        lesson4 = repo.lesson_by_no(db, "ko", 4)
        assert lesson4.code == "A1-T01-1"
        brief = cur._brief(db, lesson4, m)
        assert len(brief.items) == 30 and len(brief.surfaces) == 30, "상한 18 폐기 — 전부"
        assert all(set(d) == {"obj", "ex", "role"} for d in brief.items)
        grammar = [d for d in brief.items if d["role"] == "grammar"]
        assert len(grammar) == 3 and all(d["ex"] for d in grammar), "문형은 반드시 예문과 함께"
        # 회전: seen_count 0 → 예문[0], 1 → 예문[1](표현학습과 같은 순서)
        target = grammar[1]
        items = {it.surface: it for _li, it in repo.lesson_items(db, lesson4.lesson_id)}
        exs = json.loads(items[target["obj"]].examples)
        assert len(exs) >= 2 and target["ex"] == exs[0]
        db.add(CurMemberItem(member_id=m, lesson_id=lesson4.lesson_id, item_id=items[target["obj"]].item_id, seen_count=1))
        db.commit()
        brief2 = cur._brief(db, lesson4, m)
        assert next(d for d in brief2.items if d["obj"] == target["obj"])["ex"] == exs[1]
        # 대본에 30개 전부 + 문형 «— "예문"» + probes 이름 치환
        out = ft.build_freetalk_instruction(role="선생님", personality="다정", level_profile="쉬움", locale="en", interests=[],
                                            name="John", max_sentences=2, lesson=brief)
        for d in brief.items:
            assert d["obj"] in out
        for d in grammar:
            assert f'{d["obj"]} — "{d["ex"]}"' in out
        assert "마이클" not in out and "John 씨" in out
    finally:
        db.close()


@pytestmark_seed
def test_next_course_follows_the_lesson_status(factory):
    db = factory()
    try:
        m = _member(db)
        assert cur.me(db, m)["next_course"] == "expression"
        c = Call(member_id=m, character_id=1, call_type="expression")
        db.add(c); db.commit()
        opened = cur.open_call(db, m, c.call_id, "expression")
        ids = [d["item_id"] for d in opened.items]
        cur.record_expression(db, c.call_id, opened.items, drilled_ids=ids, passed_ids=ids, failed_ids=[])
        me = cur.me(db, m)
        assert me["status"] == "expression_done" and me["next_course"] == "freetalk"
    finally:
        db.close()


class _WS:
    def __init__(self, incoming):
        self._incoming = list(incoming); self.sent_text = []; self.closed_with = None
        from starlette.websockets import WebSocketState
        self._WS = WebSocketState; self.client_state = WebSocketState.CONNECTED

    async def receive(self):
        return self._incoming.pop(0) if self._incoming else {"type": "websocket.disconnect"}

    async def send_text(self, t): self.sent_text.append(t)
    async def send_bytes(self, b): pass
    async def close(self, code=None): self.closed_with = code; self.client_state = self._WS.DISCONNECTED


class _Sess:
    def __init__(self, script, ws=None, wait_hint=False):
        self.sent_text_turns = []; self.script = script; self.ws = ws; self.wait_hint = wait_hint
    async def send_audio(self, b): pass
    async def send_text_turn(self, t): self.sent_text_turns.append(t)
    async def send_reground(self, t, *, turn_complete=True): self.sent_text_turns.append(t)

    async def events(self):
        for role, txt in self.script:
            if role == "B":
                yield cs.LiveEvent(kind="out_tr", text=txt); yield cs.LiveEvent(kind="turn_end")
                if self.wait_hint and "?" in txt and self.ws is not None:
                    # 힌트 사이드카는 백그라운드 — 프레임이 나갈 때까지(최대 3초) 세션을 살려 둔다
                    for _ in range(300):
                        if any('"type":"hint"' in t or '"type": "hint"' in t for t in self.ws.sent_text):
                            break
                        await asyncio.sleep(0.01)
            else:
                yield cs.LiveEvent(kind="in_tr", text=txt, is_final=True)


async def _run(factory, member_id, call_type, holder, script, monkeypatch, *, wait_hint=False):
    ws = _WS([{"type": "websocket.receive", "text": json.dumps({"type": "start", "character_id": 1, "call_type": call_type})}])

    @contextlib.asynccontextmanager
    async def _f(client, settings, *, system_instruction, voice, **_kw):
        sess = _Sess(script, ws=ws, wait_hint=wait_hint); holder["session"] = sess; holder["system_instruction"] = system_instruction
        yield sess

    async def _capture(session, state):
        holder["state"] = state
    monkeypatch.setattr(cs, "_reground_watch", _capture)
    await cs.run_call(ws, app_settings, object(), factory, member_id=member_id, live_session_factory=_f)
    for _ in range(300):
        if not cs._analysis_tasks:
            break
        await asyncio.sleep(0.01)
    holder["frames"] = [json.loads(t) for t in ws.sent_text]
    return holder


@pytestmark_seed
@pytest.mark.asyncio
async def test_lesson_freetalk_call_uses_course_slots_and_expression_call_does_not(factory, monkeypatch):
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 0.2)
    db = factory()
    m = _member(db)
    lesson1 = repo.lesson_by_no(db, "ko", 1)
    c = Call(member_id=m, character_id=1, call_type="expression"); db.add(c); db.commit()
    opened = cur.open_call(db, m, c.call_id, "expression")
    ids = [d["item_id"] for d in opened.items]
    cur.record_expression(db, c.call_id, opened.items, drilled_ids=ids, passed_ids=ids, failed_ids=[])
    db.close()

    h = await _run(factory, m, "auto", {}, [("B", "안녕하세요? 저는 비버예요. 이름이 뭐예요?"), ("U", "저는 John이에요.")], monkeypatch)
    assert next(f for f in h["frames"] if f["type"] == "call_started")["course"] == "freetalk"
    st = h["state"]
    assert st.cur_route and st.cur_course == "freetalk" and st.freetalk_brief is not None
    assert len(st.freetalk_brief.items) == 15 and st.freetalk_target == "한국어"
    # 슬롯 — 차시 프리토킹만
    assert st.nudge_seed_1 == ft.NUDGE_SEED_1_FREETALK_LESSON and st.nudge_seed_2 == ft.NUDGE_SEED_2_FREETALK
    assert st.idle_nudge1_s == 30.0 and st.idle_nudge2_s == cs.IDLE_NUDGE2_S
    # 일반 재접지 기계는 안 쓴다 — 항목 0 · chat · 사이드카 없음 · 주입 0
    assert st.reground_items == [] and st.call_mode == "chat" and st.reground_ctx is None
    assert st.reground_pending is False and st.reground_count == 0
    assert all(common.CONTROL_TAG not in t for t in h["session"].sent_text_turns), "재접지·넛지 주입 0"
    assert h["session"].sent_text_turns[0] == ft.seed_freetalk_lesson_opening("한국어")
    assert "[이번 차시 — 이 상황을 역할극으로 대화한다]" in h["system_instruction"] and "1~2문장" in h["system_instruction"]
    assert "[학습자 흥미" not in h["system_instruction"]

    # 표현학습(cur) 통화 — 공용 값 그대로
    db = factory(); m2 = _member(db); db.close()
    h2 = await _run(factory, m2, "auto", {}, [("B", "안녕하세요.")], monkeypatch)
    st2 = h2["state"]
    assert st2.cur_course == "expression" and st2.freetalk_brief is None
    assert st2.nudge_seed_2 == cs._NUDGE_SEED_2 and st2.idle_nudge1_s == cs.IDLE_NUDGE1_S
    assert st2.reground_ctx is not None and len(st2.reground_items) == 15


# --------------------------------------------------------------------------- #
# ⑥ 힌트 — 프리토킹은 보인다(사장님 2026-09-12), 표현학습은 없다(D7). 차시 프리토킹은 지시문에 이번 차시 소재
# --------------------------------------------------------------------------- #
def _hint_stub(seen: list):
    async def _gen(client, model, *, system_instruction, prompt, schema, temperature=0.2, thinking_budget=None, usage=None):
        if schema is cs.HintOut:
            seen.append(system_instruction)
            return cs.HintOut(examples=[
                cs.HintExample(korean="저는 존이에요.", roman="jeoneun jon-ieyo", native="I'm John."),
                cs.HintExample(korean="저는 미국 사람이에요.", roman="jeoneun miguk saram-ieyo", native="I'm American."),
                cs.HintExample(korean="고향은 시카고예요.", roman="gohyang-eun sikago-yeyo", native="My hometown is Chicago."),
            ])
        return None
    return _gen


def test_hint_instruction_with_lesson_appends_the_lesson_clause_and_without_is_byte_identical():
    base = cs._hint_instruction("영어(English)", "한국어")
    assert base == cs._hint_instruction("영어(English)", "한국어", lesson=None) and "한국어 학습 힌트" in base
    with_lesson = cs._hint_instruction("영어(English)", "한국어", lesson=_BRIEF)
    assert with_lesson.startswith(base)
    tail = with_lesson[len(base):]
    assert "«처음 만난 반 친구와 이름과 나라 말하기» 상황의 역할극이다." in tail
    assert "**우선** 써라" in tail and "안녕히 계세요. · 생일이 언제예요? · 저는 회사원입니다. · 사람 · 나라." in tail, "문형은 예문으로"
    assert "N은/는" not in tail


@pytestmark_seed
@pytest.mark.asyncio
async def test_lesson_freetalk_pushes_hints_with_lesson_material_and_expression_pushes_none(factory, monkeypatch):
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 0.2)
    seen: list[str] = []
    monkeypatch.setattr(cs.gemini_analysis, "generate_structured", _hint_stub(seen))
    db = factory()
    m = _member(db)
    c = Call(member_id=m, character_id=1, call_type="expression"); db.add(c); db.commit()
    opened = cur.open_call(db, m, c.call_id, "expression")
    ids = [d["item_id"] for d in opened.items]
    cur.record_expression(db, c.call_id, opened.items, drilled_ids=ids, passed_ids=ids, failed_ids=[])
    lesson1 = repo.lesson_by_no(db, "ko", 1)
    db.close()

    h = await _run(factory, m, "auto", {}, [("B", "안녕하세요? 이름이 뭐예요?"), ("U", "저는 존이에요.")], monkeypatch, wait_hint=True)
    assert next(f for f in h["frames"] if f["type"] == "call_started")["course"] == "freetalk"
    hints = [f for f in h["frames"] if f.get("type") == "hint"]
    turn_ends = [f["turn_id"] for f in h["frames"] if f.get("type") == "turn_end"]
    assert len(hints) == 1 and len(hints[0]["examples"]) == 3 and hints[0]["turn_id"] == turn_ends[0]
    assert hints[0]["examples"][0] == {"korean": "저는 존이에요.", "roman": "jeoneun jon-ieyo", "native": "I'm John."}
    assert h["state"].hint_ctx is not None
    assert seen and f"«{lesson1.situation}» 상황의 역할극이다." in seen[0] and "**우선** 써라" in seen[0]

    # 표현학습(cur) — 힌트 0(D7)
    db = factory(); m2 = _member(db); db.close()
    seen.clear()
    h2 = await _run(factory, m2, "auto", {}, [("B", "따라 하세요. 안녕하세요?")], monkeypatch, wait_hint=False)
    assert next(f for f in h2["frames"] if f["type"] == "call_started")["course"] == "expression"
    assert h2["state"].hint_ctx is None and not [f for f in h2["frames"] if f.get("type") == "hint"] and not seen


@pytestmark_seed
@pytest.mark.asyncio
async def test_old_freetalk_path_keeps_old_seed_and_slots_when_cur_is_disabled(factory, monkeypatch):
    monkeypatch.setattr(app_settings, "CUR_ENABLED", False)   # run_call 은 넘겨받은 settings(app_settings)를 읽는다
    monkeypatch.setattr(cs, "SEED_TO_HANGUP_S", 0.2)
    db = factory(); m = _member(db); db.close()
    h = await _run(factory, m, "freetalk", {}, [("B", "안녕!")], monkeypatch)
    st = h["state"]
    assert not st.cur_route and st.cur_course == "" and st.freetalk_brief is None
    assert st.nudge_seed_1 == ft.NUDGE_SEED_1_FREETALK and st.nudge_seed_2 == cs._NUDGE_SEED_2 and st.idle_nudge1_s == cs.IDLE_NUDGE1_S
    assert h["session"].sent_text_turns[0] == ft.seed_freetalk_opening("한국어")
    assert "[이번 차시" not in h["system_instruction"] and "[학습자 흥미·소재]" in h["system_instruction"]
    # 옛 프리토킹도 힌트는 켜진다(사장님: 프리토킹은 힌트 있음) — 차시 소재 절은 없다
    assert st.hint_ctx is not None and "상황의 역할극" not in st.hint_ctx["instruction"]
