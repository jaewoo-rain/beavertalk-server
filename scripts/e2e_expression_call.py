# -*- coding: utf-8 -*-
"""[dev] 표현학습 E2E 하네스 — 학습자를 **대본**으로 대신해 ground truth 로 자동 채점한다.

## 왜
사장님이 5분 통화하고 우리가 전사를 읽어 «앵무새냐 자발이냐» 를 가리는 게 지금 방식이다.
학습자를 대본으로 대신하면 **어느 답이 자발이고 어느 답이 복창인지 하네스 자신이 안다** ⇒
DB 판정(quiz_passed_at · call.expression_result)을 기대값과 기계적으로 대조할 수 있다.
통화 1398 에서 무너진 축(퀴즈 주기 · 거짓 칭찬 · 자발 산출 0)은 전부 로그가 안 재던 축이다 —
이 하네스가 그 축을 매 통화 잰다. 계획: docs/20260911_1810_표현학습-E2E-하네스-계획.md

## 수동 하네스다 (pytest 아님 — smoke 규율)
**실서비스 DB** 에 붙는다(demo-api 는 app-api 와 같은 Supabase). 회원 92(testfree@gmail.com) 행만 만진다.
통화 1회 ≈ $0.25 + 3~5분. `duration_min` 은 서버가 **3~15분으로 클램프** 한다(1분 불가).

## 사용법 (⛔ 반드시 conda env · .env 가 있는 루트를 --env-root 로)
    E2E="PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/e2e_expression_call.py --env-root <.env 있는 루트>"
    $E2E --status                 # 회원 92 레벨 · L1 46개 진도
    $E2E --fix-items              # 레벨 1 고정 + 28개 quiz_passed_at 찍어 18개 고정 (1회)
    $E2E --reset                  # 18개 drilled_at/drilled_call_id/quiz_passed_at NULL (매 회 전 — --runs 가 자동으로 한다)
    $E2E --probe --duration 3     # ①②③: 연결 → 비버 첫 질문 → 항목 매칭까지 보고 끊는다
    $E2E --runs 1 --duration 5    # reset → 통화 → 채점 → docs/e2e/ 보고서. exit 1 = 기대 불일치
    $E2E --runs 3 --duration 5 --logs   # 3회 반복 + gcloud 서버 로그 첨부

한글 콘솔이 깨지면 보고서 파일(docs/e2e/*.md)을 Read 로 본다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------- #
# 계약 상수 (브리프 + protocol.py 로 확인)
# --------------------------------------------------------------------------- #
DEFAULT_BASE = "https://beavertalk-app-demo-api-333511894671.asia-northeast3.run.app"
WS_PATH = "/api/v1/calls/stream"
DEFAULT_EMAIL = "testfree@gmail.com"
DEFAULT_PASSWORD = "11111111"     # dev 테스트 계정(testfree=92 Free/2.5 · testmax=88 Max/3.1). 다른 계정 금지.
MEMBER_ID = 0                     # ⛔ 하드코딩 아님 — main() 이 --email 로 DB(member.email)에서 찾아 채운다. 다른 계정 금지(사장님 20).
LEVEL_NO = 1
LANGUAGE = "ko"
LOCALE = "en"
QUIZ_GROUP = 3                    # mastery_repository.EXPRESSION_QUIZ_GROUP (채점 기준 — 아래에서 실값으로 덮는다)

SR_IN = 16000                     # 클라→서버 PCM16 mono
FRAME_MS = 40
FRAME_BYTES = SR_IN * 2 * FRAME_MS // 1000   # 1280
PRE_SPEECH_S = 0.8                # turn_end 뒤 이만큼 쉬고 말한다
POST_SPEECH_SILENCE_S = 1.2       # 발화 뒤 무음(VAD 종료 감지)
LEARNER_VOICE = "Charon"          # 비버 음색과 다르게(Chirp3-HD 로스터)

# --------------------------------------------------------------------------- #
# 고정 18개 (L1 46 중) — 계획서 §1. item_id: (표면형, 영어 뜻, 반말형, 매칭 키워드)
# --------------------------------------------------------------------------- #
FIXED: dict[int, tuple[str, str, str, tuple[str, ...]]] = {
    11096: ("안녕하세요?", "Hello.", "안녕", ("hello", "greet someone", "say hi")),
    11097: ("만나서 반갑습니다", "Nice to meet you.", "만나서 반가워", ("nice to meet", "glad to meet", "pleased to meet", "good to meet")),
    11098: ("잘 지냈어요?", "How have you been?", "잘 지냈어?", ("how have you been", "been doing", "how are you", "how's it going")),
    11102: ("좋은 하루 보내세요", "Have a good day.", "좋은 하루 보내", ("good day", "nice day", "great day", "have a good")),
    11104: ("감사합니다", "Thank you.", "고마워", ("thank",)),
    11107: ("죄송합니다", "I'm sorry.", "미안", ("sorry", "apolog")),
    11109: ("괜찮아요", "It's okay.", "괜찬아", ("it's okay", "it's ok", "its okay", "that's okay", "no problem", "it is okay", "i'm fine")),
    11116: ("진짜요?", "Really?", "진짜?", ("really",)),
    11117: ("맛있어요", "It's delicious.", "맛있어", ("delicious", "tasty", "yummy", "tastes good")),
    11120: ("이름이 뭐예요?", "What's your name?", "이름이 뭐야?", ("your name", "someone's name")),
    11122: ("잘 부탁드립니다", "Please take good care of me.", "잘 부탁해", ("take good care", "take care of me", "look after me", "be kind to me")),
    11123: ("이거 주세요", "This one, please.", "이거 줘", ("this one", "this, please", "point to something", "point at something", "give me this")),
    11124: ("얼마예요?", "How much is it?", "얼마야?", ("how much", "the price", "how much it costs")),
    11125: ("화장실이 어디예요?", "Where is the bathroom?", "화장실이 어디야?", ("bathroom", "restroom", "toilet")),
    11126: ("도와주세요", "Please help me.", "도와줘", ("help me", "ask for help", "need help")),
    11127: ("여기요!", "Excuse me!", "여기", ("excuse me", "waiter", "get someone's attention", "waiter's attention", "call the staff")),
    11129: ("배고파요", "I'm hungry.", "배고파", ("hungry",)),
    11133: ("잘 못 들었어요", "I didn't catch that.", "잘 못 들었어", ("didn't catch", "did not catch", "couldn't hear", "didn't hear", "can't hear", "cannot hear", "catch what")),
}
# 드릴 오답 미끼 — **제외된 28개** 의 표면형. 판정기 후보 목록 밖이라 다른 항목이 통과로 찍힐 길이 없다.
DISTRACTORS = ["좋아요", "맞아요", "알겠어요", "물 주세요", "잠시만요", "또 봐요", "어서 오세요", "안녕히 계세요"]
# ⚠ 고마워요·미안해요는 뺐다 — 뜻이 18개 안의 감사합니다·죄송합니다와 겹쳐 비버의 교정문("that means thanks")이 매칭을 흔든다.
IDK_EN = "I don't know."

# 대본 정책 — «몇 번째로 드릴된 항목이냐» (k-1)%6+1
POLICY_NAMES = {
    1: "정답→퀴즈정답 (passed)",
    2: "모른다→복창→퀴즈 모른다→공개→복창 (failed · 앵무새 함정)",
    3: "정답→퀴즈 반말 (not passed · 격식 함정)",
    4: "드릴 오답 2회→포기 (drilled only · 거짓칭찬 감시)",
    5: "정답→퀴즈 오답→힌트 뒤 정답 (passed · 힌트 경로)",
    6: "정답→퀴즈정답 (passed)",
}

# --------------------------------------------------------------------------- #
# 텍스트 대조 — 비버가 실제로 어떻게 말하는지(전사 1397·1398)에 맞춘 어휘
# --------------------------------------------------------------------------- #
QUIZ_RE = re.compile(
    r"\b(quiz|pop quiz|review|recap|let'?s see if you remember|see if you remember|"
    r"remember what we (learned|practiced|covered)|test (you|time|what)|(quick|little|short|small|mini) (test|check)|a test|let'?s test|time to (test|check|review)|"
    r"check (what|if) you (learned|remember)|퀴즈|복습)\b", re.I)
NEW_ITEM_RE = re.compile(
    r"\b(new (phrase|expression|one|word)|next (one|phrase|expression)|another one|let'?s move on|"
    r"moving on|let'?s learn|next up|learn (a|another|something) new)\b", re.I)
PRAISE_RE = re.compile(
    r"\b(perfect|nailed it|great( job)?|excellent|exactly|correct|you got it|there you go|right on|"
    r"well done|awesome|spot on|that'?s it|that'?s right|good job|nice(ly)?( done)?|yes!|bingo|brilliant|"
    r"you did it|way to go|yep|yup|that'?s the one|good one|not bad)\b|(?:^|\s)(right|good|yes)[.!]|맞아요|정답|잘했|완벽", re.I)
CORRECTION_RE = re.compile(
    r"\b(but|not quite|almost|close|nope|wrong|no,|not right|try again|one more (time|try)|remember|"
    r"polite|formal|add|missing|should be|it'?s actually|actually|instead|the word is|it was|"
    r"it'?s ['\"]|say it like|listen|repeat|not it|not that|not how|not right|still not|past tense|"
    r"ending)\b|아니|다시|틀렸|존댓말|정중", re.I)
QUESTION_RE = re.compile(
    r"\?|\b(how (do|would|can) you (say|ask|tell|greet)|what do you say|what would you say|"
    r"tell me|say it|give it a (shot|try)|try (it|saying|to say|that)|can you say|what was it|"
    r"what is it in|in korean|now say|just say|repeat after me|say that|say this|try again|one more time|"
    r"come on|go ahead|your turn)\b", re.I)
QUIZ_CLOSE_RE = re.compile(
    r"\b(done with the (quiz|test|review)|(quiz|test|review) is (over|done)|end of the (quiz|test)|"
    r"that'?s (it for|the end of) the (quiz|test)|no more quiz|back to (new|learning))\b", re.I)
BRACKET_RE = re.compile(r"\[[^\[\]\n]{1,60}\]")   # [Country] 도 [전화 끊김] 도 — 대괄호가 소리로 나온 것 전부
# «맞았다» 로 받아준 말 — 오답 뒤에 나오면 거짓 칭찬이다(1401 t18 "Close enough" 가 반말을 받아줬다)
ACCEPT_RE = re.compile(r"\b(close enough|good enough|that works|i'?ll take it|fine, moving on)\b|\bfinally!", re.I)
# 새 질문을 여는 문장 — 반응(칭찬·교정) 대조에서 뺀다
QUESTION_LEAD_RE = re.compile(
    r"\b(how (do|would|can|about) you|how (do|would) you (say|ask)|what about|what if|what would|how about|"
    r"tell me|now,? (how|what|tell|let|here|say)|next (one|up)|quiz|and\b\s*$)", re.I)
# 비버가 영어 뜻을 따옴표로 묶는다 — 「how do you say "Hello"?」. 따옴표 안이 곧 «무엇을 묻나» 다.
DQUOTE_RE = re.compile(r"[\"“”]([^\"“”]{1,60})[\"“”]")
SQUOTE_RE = re.compile(r"(?<![A-Za-z])[‘'“]([^'‘’\"]{1,60})[’'”](?![A-Za-z])")

_PUNCT_RE = re.compile(r"[\s\.\,\!\?\~\'\"“”‘’\(\)\[\]·…:;\-]")


def norm_ko(s: str) -> str:
    """한국어 대조용 정규화 — 공백·문장부호 제거 + NFC."""
    return _PUNCT_RE.sub("", unicodedata.normalize("NFC", s or ""))


def has_surface(text: str, surface: str) -> bool:
    return bool(surface) and norm_ko(surface) in norm_ko(text)


def surfaces_in(text: str, items: dict[int, "Item"]) -> list[int]:
    """턴 안에 표면형이 실제로 나온 항목들(긴 것 우선 — 부분 포함 오탐 완화)."""
    hits = [iid for iid, it in items.items() if has_surface(text, it.surface)]
    return sorted(hits, key=lambda i: -len(items[i].surface))


def quoted_segments(text: str) -> list[str]:
    """비버 턴의 따옴표 안 조각들(영어 뜻 인용). 큰따옴표 우선, 없으면 작은따옴표(아포스트로피 회피)."""
    segs = [m.group(1).strip() for m in DQUOTE_RE.finditer(text or "")]
    if not segs:
        segs = [m.group(1).strip() for m in SQUOTE_RE.finditer(text or "")]
    return [q for q in segs if q]


def reaction_part(text: str) -> str:
    """비버 턴에서 «직전 답에 대한 반응» 문장만 — 새 질문·다음 항목 소개 문장은 뺀다.

    "You got it! Now, how do you say \"This one, please\"?" → "You got it!"
    """
    body = strip_quotes(text)
    sents = re.split(r"(?<=[.!?])\s+", body.strip())
    keep: list[str] = []
    for sent in sents:
        if QUESTION_LEAD_RE.search(sent) or sent.strip().endswith("?"):
            break
        keep.append(sent)
    return " ".join(keep) if keep else ""


def strip_quotes(text: str) -> str:
    """따옴표 안을 지운 본문 — 칭찬·교정 어휘 대조는 여기서 한다(«Nice to meet you» 의 Nice 가 칭찬으로 잡히지 않게)."""
    t = DQUOTE_RE.sub(" ", text or "")
    return SQUOTE_RE.sub(" ", t)


def norm_en(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)             # (to one leaving) 류 괄호 뜻 제거
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"[^a-z' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------- #
# 데이터
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    item_id: int
    surface: str
    en: str
    casual: str
    keywords: tuple[str, ...]


@dataclass
class QuizRound:
    """한 항목의 퀴즈 회차 1개(앵커 뒤 처음 물은 때 열리고, 다른 항목으로 넘어가면 닫힌다)."""
    n: int
    asked_at: float
    anchored: bool = True            # 앵커(퀴즈 선언) 아래에서 물었나. False = 드릴 모드에서 끝낸 항목을 다시 물음(재출제/되감기)
    revealed: bool = False           # 이 회차에서 비버가 정답 전체를 말했나
    answers: list[str] = field(default_factory=list)   # 학습자 답의 종류 시퀀스
    spontaneous_correct: bool = False  # 공개 전 스스로 정답 (하네스가 그렇게 고른 것)
    hint_path: bool = False          # 오답 → (공개 없는) 힌트 → 정답 이 성립했나


@dataclass
class ItemRecord:
    item: Item
    k: int                            # 드릴 순번(1부터)
    policy: int                       # (k-1)%6+1
    ident: str = ""                   # 어떻게 식별했나: keyword|phrase|llm|reveal|continuation
    intro_turn: int = -1
    intro_pre_reveal: bool = False    # 첫 소개 턴에 표면형이 있었다(모국어 선질문 위반)
    drill_attempts: int = 0
    drill_revealed: bool = False
    drill_answers: list[str] = field(default_factory=list)
    surface_uttered: bool = False     # 표면형이 비버 공개나 학습자 발화로 실제 한 번 나왔나 (= drilled 기대의 조건)
    surface_heard: bool = False       # 서버가 «들은» 쪽 — 비버 공개, 또는 학습자 턴의 input_transcript 에 표면형이 있었다
                                      #   (TTS 「이거 주세요」→STT 「이거 지세요」 처럼 보낸 것과 들린 것이 다르면 서버는 못 본다)
    rounds: list[QuizRound] = field(default_factory=list)
    beaver_said_correct_after_wrong: list[int] = field(default_factory=list)   # 거짓 칭찬 턴 번호

    @property
    def expected_passed(self) -> bool:
        return any(r.spontaneous_correct for r in self.rounds)

    @property
    def expectation_ambiguous(self) -> bool:
        """자발 정답이 **앵커 없는** 회차에서만 났다 — 판정기 규칙(결정 6-3)상 «그 항목만 보류» 가 허용된다."""
        anchored_pass = any(r.spontaneous_correct and r.anchored for r in self.rounds)
        unanchored_pass = any(r.spontaneous_correct and not r.anchored for r in self.rounds)
        return unanchored_pass and not anchored_pass

    @property
    def quizzed(self) -> bool:
        return bool(self.rounds)


@dataclass
class Turn:
    n: int
    role: str                        # beaver | learner
    t: float                         # 통화 시작 기준 초
    text: str
    tags: list[str] = field(default_factory=list)
    stt: str = ""                    # learner: 서버 input_transcript
    kind: str = ""                   # learner: correct|casual|idk|distractor|parrot|silence
    item_id: int = 0                 # learner: 이 답이 향한 항목(STT 대조용)
    wall: float = 0.0                # epoch 초 — 서버 로그(gcloud timestamp)와 시간 대조용


# --------------------------------------------------------------------------- #
# 환경 부트스트랩 — core.config 는 cwd 의 .env 를 읽는다
# --------------------------------------------------------------------------- #
def bootstrap_env(env_root: str | None) -> Path:
    root = Path(env_root).resolve() if env_root else ROOT
    if not (root / ".env").is_file():
        sys.exit(f"⛔ {root}/.env 가 없다. --env-root 로 .env(+gcp_key.json·tts_key.json) 가 있는 루트를 줘라.")
    os.chdir(root)
    return root


def db_session_factory():
    import logging

    from core.config import settings
    import db.registry  # noqa: F401 — 매퍼 등록
    from db.engine import build_engine
    from db.session import build_session_factory

    engine = build_engine(settings)
    engine.echo = False
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    return build_session_factory(engine)


class Tee:
    """stdout 을 UTF-8 파일에도 복사한다 — conda run 콘솔이 한글을 깨뜨려도 파일은 온전하다."""

    def __init__(self, path: Path) -> None:
        self.f = open(path, "a", encoding="utf-8")
        self.out = sys.stdout

    def write(self, s: str) -> None:
        self.out.write(s)
        self.f.write(s)
        self.f.flush()

    def flush(self) -> None:
        self.out.flush()
        self.f.flush()


def resolve_member(db, email: str) -> int:
    """이메일로 member_id. 탈퇴 안 한 활성 행 1개여야 한다 — 없거나 여럿이면 중단(엉뚱한 계정을 만지지 않게)."""
    from sqlalchemy import select
    from domains.account.models.member import Member

    rows = db.scalars(select(Member).where(Member.email == email)).all()
    rows = [m for m in rows if getattr(m, "deleted_at", None) is None] or rows
    if len(rows) != 1:
        sys.exit(f"⛔ email={email} 에 해당하는 member 가 {len(rows)}명 — 중단")
    return int(rows[0].member_id)


def load_items(db) -> dict[int, Item]:
    """고정 18개의 표면형·뜻을 **DB 에서** 읽어 표와 맞춘다(커리큘럼이 바뀌면 여기서 드러난다)."""
    from sqlalchemy import select
    from domains.learning.models.learning_item import LearningItem

    rows = db.scalars(select(LearningItem).where(LearningItem.item_id.in_(list(FIXED)))).all()
    got = {r.item_id: r for r in rows}
    items: dict[int, Item] = {}
    for iid, (surface, en, casual, kws) in FIXED.items():
        r = got.get(iid)
        if r is None:
            sys.exit(f"⛔ learning_item {iid} 가 DB 에 없다 — FIXED 표를 다시 맞춰라.")
        if norm_ko(r.surface) != norm_ko(surface):
            print(f"⚠ item {iid} 표면형 불일치 표={surface!r} DB={r.surface!r} → DB 를 쓴다")
            surface = r.surface
        db_en = None
        try:
            m = json.loads(r.meanings or "{}")
            db_en = m.get("en") if isinstance(m, dict) else None
        except ValueError:
            pass
        items[iid] = Item(iid, surface, db_en or en, casual, kws)
    return items


def all_level_items(db) -> list:
    from sqlalchemy import select
    from domains.learning.models.learning_item import LearningItem

    return list(db.scalars(
        select(LearningItem).where(LearningItem.language == LANGUAGE, LearningItem.level_no == LEVEL_NO)
        .order_by(LearningItem.item_id)).all())


def progress_rows(db, item_ids: list[int]) -> dict[int, Any]:
    from sqlalchemy import select
    from domains.learning.models.member_item_progress import MemberItemProgress as P

    return {r.item_id: r for r in db.scalars(
        select(P).where(P.member_id == MEMBER_ID, P.item_id.in_(item_ids))).all()}


# --------------------------------------------------------------------------- #
# ① 항목 고정 / 초기화 / 상태
# --------------------------------------------------------------------------- #
def cmd_status(sf) -> None:
    from domains.learning.repository import mastery_repository as mr

    with sf() as db:
        lvl = mr.get_language_level(db, MEMBER_ID, LANGUAGE)
        items = all_level_items(db)
        prog = progress_rows(db, [i.item_id for i in items])
        print(f"member {MEMBER_ID}: level(ko)={lvl}  L{LEVEL_NO} items={len(items)}  progress rows={len(prog)}")
        for it in items:
            p = prog.get(it.item_id)
            mark = "★" if it.item_id in FIXED else " "
            st = "-" if p is None else (
                f"drilled={p.drilled_at and p.drilled_at.strftime('%m-%d %H:%M')} call={p.drilled_call_id} "
                f"passed={p.quiz_passed_at and p.quiz_passed_at.strftime('%m-%d %H:%M')}")
            print(f" {mark} {it.item_id} {it.surface:<22s} {st}")


def cmd_fix_items(sf) -> None:
    """레벨 1 고정 + FIXED 밖 28개에 quiz_passed_at 을 찍어 선별 풀을 18개로 고정한다."""
    from domains.learning.models.member_item_progress import MemberItemProgress as P
    from domains.learning.repository import mastery_repository as mr
    from domains.learning.service import mastery_service

    with sf() as db:
        lvl = mr.get_language_level(db, MEMBER_ID, LANGUAGE)
        if lvl != LEVEL_NO:
            mr.upsert_language_level(db, MEMBER_ID, LANGUAGE, LEVEL_NO)
            print(f"레벨 {lvl} → {LEVEL_NO} (member_language_level + member.korean_level)")
        items = all_level_items(db)
        others = [i.item_id for i in items if i.item_id not in FIXED]
        prog = progress_rows(db, others)
        now = datetime.now(timezone.utc)
        created = stamped = 0
        for iid in others:
            row = prog.get(iid)
            if row is None:
                row = P(member_id=MEMBER_ID, item_id=iid, provenance=mastery_service.PROVENANCE_EXPRESSION)
                db.add(row)
                created += 1
            if row.quiz_passed_at is None:
                row.quiz_passed_at = now
                stamped += 1
        db.commit()
        remaining = mr.count_expression_remaining(db, MEMBER_ID, LEVEL_NO, language=LANGUAGE)
        print(f"고정 완료: 제외 {len(others)}개(행 생성 {created} · passed 찍음 {stamped}) → 남은 풀 {remaining}개 (기대 {len(FIXED)})")
        if remaining != len(FIXED):
            print("⚠ 남은 풀이 18 이 아니다 — --status 로 확인해라")


def cmd_reset(sf, quiet: bool = False) -> None:
    """18개의 drilled_at · drilled_call_id · quiz_passed_at 을 NULL 로. 레벨이 밀렸으면 1 로 되돌린다."""
    from domains.learning.repository import mastery_repository as mr

    with sf() as db:
        lvl = mr.get_language_level(db, MEMBER_ID, LANGUAGE)
        if lvl != LEVEL_NO:
            print(f"⚠ 레벨이 {lvl} 다(승급 부작용?) → {LEVEL_NO} 로 되돌린다")
            mr.upsert_language_level(db, MEMBER_ID, LANGUAGE, LEVEL_NO)
        prog = progress_rows(db, list(FIXED))
        n = 0
        for row in prog.values():
            if row.drilled_at or row.drilled_call_id or row.quiz_passed_at:
                n += 1
            row.drilled_at = None
            row.drilled_call_id = None
            row.quiz_passed_at = None
        db.commit()
        remaining = mr.count_expression_remaining(db, MEMBER_ID, LEVEL_NO, language=LANGUAGE)
        if not quiet:
            print(f"초기화: {n}행 NULL 복원 · 남은 풀 {remaining}개")
        if remaining != len(FIXED):
            print(f"⚠ 남은 풀 {remaining} ≠ {len(FIXED)} — --fix-items 를 먼저 돌렸나?")


# --------------------------------------------------------------------------- #
# ④ 학습자 음성 — Cloud TTS LINEAR16/16k (core.tts 의 클라이언트·음성 해석을 그대로 쓴다)
# --------------------------------------------------------------------------- #
class Voice:
    def __init__(self) -> None:
        self.cache_dir = Path(tempfile.gettempdir()) / "bt_e2e_tts"
        self.cache_dir.mkdir(exist_ok=True)
        self.mem: dict[tuple[str, str], bytes] = {}

    async def pcm(self, text: str, lang: str) -> bytes:
        key = (text, lang)
        if key in self.mem:
            return self.mem[key]
        fn = self.cache_dir / (re.sub(r"[^0-9A-Za-z가-힣]", "_", f"{lang}_{text}")[:80] + ".pcm")
        if fn.is_file():
            data = fn.read_bytes()
        else:
            data = await self._synth(text, lang)
            fn.write_bytes(data)
        self.mem[key] = data
        return data

    async def _synth(self, text: str, lang: str) -> bytes:
        from core import tts as core_tts
        from google.cloud import texttospeech

        cli = core_tts._client()
        if cli is None:
            sys.exit("⛔ Cloud TTS 클라이언트 없음(tts_key.json) — 학습자 음성을 만들 수 없다")
        lang_code, voice_name = core_tts._resolve_voice(lang, LEARNER_VOICE)
        resp = await cli.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(language_code=lang_code, name=voice_name),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16, sample_rate_hertz=SR_IN),
        )
        data = bytes(resp.audio_content)
        if data[:4] == b"RIFF":            # WAV 헤더 제거 — data 청크 뒤부터가 PCM
            i = data.find(b"data")
            data = data[i + 8:] if i >= 0 else data[44:]
        if len(data) % 2:
            data = data[:-1]
        return data


class Uplink:
    """40ms 프레임 실시간 페이싱 업링크. 마이크가 열린 동안(turn_end~다음 turn_start)만 보낸다 —
    말할 것이 없으면 무음 프레임(VAD 종료 감지 + 마이크 흉내). 서버도 비버 발화중 프레임은 버린다(barge-in off)."""

    def __init__(self, ws) -> None:
        self.ws = ws
        self.buf = bytearray()
        self.open = False
        self.sent_frames = 0
        self.speech_done = asyncio.Event()
        self.speech_done.set()

    async def run(self) -> None:
        silence = bytes(FRAME_BYTES)
        nxt = time.perf_counter()
        while True:
            nxt += FRAME_MS / 1000.0
            d = nxt - time.perf_counter()
            if d > 0:
                await asyncio.sleep(d)
            else:
                nxt = time.perf_counter()
            if not self.open:
                continue
            if self.buf:
                frame = bytes(self.buf[:FRAME_BYTES])
                del self.buf[:FRAME_BYTES]
                if len(frame) < FRAME_BYTES:
                    frame += bytes(FRAME_BYTES - len(frame))
                if not self.buf:
                    self.speech_done.set()
            else:
                frame = silence
            await self.ws.send(frame)
            self.sent_frames += 1

    async def speak(self, pcm: bytes) -> None:
        self.buf += pcm + bytes(int(SR_IN * 2 * POST_SPEECH_SILENCE_S))
        self.speech_done.clear()
        await self.speech_done.wait()

    def cut(self) -> None:
        self.buf.clear()
        self.speech_done.set()


# --------------------------------------------------------------------------- #
# LLM 폴백 — 문자열로 안 가릴 때 번호 하나 (generate_structured 1회)
# --------------------------------------------------------------------------- #
class Picker:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.client = None
        self.calls = 0
        if enabled:
            self.client = self._make_client()
            if self.client is None:
                print("⚠ LLM 폴백 비활성(genai 클라이언트 없음) — 문자열 대조만 쓴다")
                self.enabled = False

    @staticmethod
    def _make_client():
        from core.config import settings
        try:
            from google import genai
            if settings.GEMINI_API_KEY:
                return genai.Client(api_key=settings.GEMINI_API_KEY)
            key = settings.GOOGLE_APPLICATION_CREDENTIALS or "gcp_key.json"
            if Path(key).is_file():
                from google.oauth2 import service_account
                creds = service_account.Credentials.from_service_account_file(
                    key, scopes=["https://www.googleapis.com/auth/cloud-platform"])
                return genai.Client(vertexai=True, project=settings.GCP_PROJECT,
                                    location=settings.GCP_LOCATION, credentials=creds)
        except Exception as exc:  # noqa: BLE001
            print(f"⚠ genai 클라이언트 생성 실패: {exc}")
        return None

    async def pick(self, question: str, candidates: list[Item], context: str) -> tuple[int, str]:
        """비버 턴이 묻는 항목 번호(item_id) 또는 0. 실패도 0."""
        if not self.enabled or not candidates:
            return 0, "disabled"
        from pydantic import BaseModel
        from core.config import settings
        from core.gemini_analysis import generate_structured

        class Pick(BaseModel):
            item_no: int
            reason: str

        listing = "\n".join(f"{i + 1}. {c.surface} — {c.en}" for i, c in enumerate(candidates))
        sysi = ("You classify which Korean expression a tutor's utterance is asking the learner to produce. "
                "The tutor speaks English and describes a situation or gives the English meaning, without saying "
                "the Korean. Answer with the number of the matching expression from the list, or 0 if the utterance "
                "is not asking for any of them (e.g. small talk, encouragement, or asking to repeat something already said).")
        prompt = f"[Recent context]\n{context}\n\n[Tutor utterance to classify]\n{question}\n\n[Expressions]\n{listing}"
        self.calls += 1
        try:
            res = await generate_structured(self.client, settings.JUDGE_MODEL, system_instruction=sysi,
                                            prompt=prompt, schema=Pick, temperature=0.0, thinking_budget=0)
        except Exception as exc:  # noqa: BLE001
            return 0, f"llm-error {exc}"
        if res is None or not (1 <= res.item_no <= len(candidates)):
            return 0, f"llm-none({getattr(res, 'reason', '')})"
        return candidates[res.item_no - 1].item_id, f"llm({res.reason[:60]})"


# --------------------------------------------------------------------------- #
# ②③⑤ 통화 세션 — 듣기(상태기계) + 대본 정책 + 말하기
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, items: dict[int, Item], voice: Voice, picker: Picker, *, probe: bool,
                 verbose: bool) -> None:
        self.items = items
        self.voice = voice
        self.picker = picker
        self.probe = probe
        self.verbose = verbose
        self.t0 = time.perf_counter()
        self.call_id: Optional[int] = None
        self.turns: list[Turn] = []
        self.mode = "drill"
        self.current: Optional[ItemRecord] = None
        self.records: dict[int, ItemRecord] = {}
        self.drilled_order: list[int] = []
        self.anchors: list[tuple[int, int]] = []          # (turn_n, drilled_count_at_anchor)
        self.quiz_block_asked: set[int] = set()            # 이 퀴즈 블록에서 이미 낸 항목
        self.beaver_audio_bytes = 0
        self.cur_turn_id: Optional[str] = None
        self.cur_text: list[str] = []
        self.since_learner: list[str] = []                 # 학습자 말 없이 이어진 비버 턴들
        self.pending_speak: Optional[asyncio.Task] = None
        self.last_learner: Optional[Turn] = None
        self.distractor_i = 0
        self.spontaneous = 0
        self.ended = False
        self.end_reason = ""
        self.errors: list[str] = []
        self.first_turn_end_seen = asyncio.Event()
        self.notes: list[str] = []
        self._last_started: Optional[ItemRecord] = None   # 가장 최근 _start_item 한 항목(재출제 판별용)

    # ---- 유틸 -------------------------------------------------------------- #
    def now(self) -> float:
        return time.perf_counter() - self.t0

    def log(self, msg: str) -> None:
        line = f"[{self.now():6.1f}s] {msg}"
        print(line, flush=True)

    def add_turn(self, role: str, text: str, **kw) -> Turn:
        t = Turn(len(self.turns), role, self.now(), text, wall=time.time(), **kw)
        self.turns.append(t)
        return t

    def next_distractor(self) -> str:
        d = DISTRACTORS[self.distractor_i % len(DISTRACTORS)]
        self.distractor_i += 1
        return d

    def policy_of(self, k: int) -> int:
        return (k - 1) % 6 + 1

    # ---- 프레임 처리 --------------------------------------------------------- #
    async def on_json(self, msg: dict, uplink: Uplink) -> None:
        t = msg.get("type")
        if t == "call_started":
            self.call_id = int(msg["call_id"]) if msg.get("call_id") else None
            self.log(f"call_started call_id={self.call_id} character={msg.get('character_id')}")
        elif t == "turn_start":
            uplink.open = False
            uplink.cut()
            if self.pending_speak and not self.pending_speak.done():
                self.pending_speak.cancel()
            self.cur_turn_id = msg.get("turn_id")
            self.cur_text = []
        elif t == "output_transcript":
            self.cur_text.append(msg.get("text") or "")
        elif t == "input_transcript":
            stt = (msg.get("text") or "").strip()
            if stt:
                for tn in reversed(self.turns[-6:]):
                    if tn.role == "learner" and not tn.stt:
                        tn.stt = stt
                        rec = self.records.get(tn.item_id)
                        if rec is not None and has_surface(stt, rec.item.surface):
                            rec.surface_heard = True
                        break
                else:
                    if self.last_learner is not None:
                        self.last_learner.stt = (self.last_learner.stt + " " + stt).strip()
        elif t == "turn_end":
            text = "".join(self.cur_text).strip()
            self.cur_turn_id = None
            self.cur_text = []
            await self.on_beaver_turn(text, uplink)
        elif t == "call_ended":
            self.ended = True
            self.end_reason = msg.get("reason", "")
            if not self.call_id and msg.get("call_id"):
                self.call_id = int(msg["call_id"])
            self.log(f"call_ended reason={self.end_reason}")
        elif t == "error":
            self.errors.append(f"{msg.get('code')}: {msg.get('message')}")
            self.log(f"⛔ error {msg}")
            if not msg.get("recoverable", True):
                self.ended = True
                self.end_reason = f"error:{msg.get('code')}"
        elif t in ("pong", "sentence", "teaching_plan", "hint"):
            pass
        else:
            self.log(f"? 미지 메시지 {t}")

    # ---- 비버 턴 해석 -------------------------------------------------------- #
    async def on_beaver_turn(self, text: str, uplink: Uplink) -> None:
        turn = self.add_turn("beaver", text)
        self.since_learner.append(text)
        seg = " ".join(self.since_learner)      # 학습자 말 없이 이어진 비버 발화 전체
        tags = turn.tags

        if BRACKET_RE.search(text):
            tags.append("[대괄호]")
        mentioned = surfaces_in(text, self.items)
        # 퀴즈 «닫는» 선언("We're done with the quiz")은 앵커가 아니다 — 1409 t28 오탐
        quiz_anchor = bool(QUIZ_RE.search(text)) and not QUIZ_CLOSE_RE.search(text)
        new_item_cue = bool(NEW_ITEM_RE.search(text))
        is_question = bool(QUESTION_RE.search(text))

        # 거짓 칭찬 — 직전 학습자 답이 대본상 오답이면 이 턴의 칭찬을 본다
        if self.last_learner is not None and self.last_learner.kind in ("idk", "casual", "distractor") \
                and len(self.since_learner) == 1:
            body = reaction_part(text)
            praise = ACCEPT_RE.search(body) or PRAISE_RE.search(body)
            corr = None if ACCEPT_RE.search(body) else CORRECTION_RE.search(body)
            if praise and not corr:
                tags.append(f"⛔거짓칭찬({praise.group(0)})")
                if self.current is not None:
                    self.current.beaver_said_correct_after_wrong.append(turn.n)
            elif praise and corr:
                tags.append(f"칭찬+교정({praise.group(0)}/{corr.group(0)})")

        # ① 앵커 → 퀴즈 모드
        if quiz_anchor and self.mode != "quiz":
            self.mode = "quiz"
            self.quiz_block_asked = set()
            self.anchors.append((turn.n, len(self.drilled_order)))
            ok = len(self.drilled_order) % QUIZ_GROUP == 0 and self.drilled_order
            tags.append(f"앵커@{len(self.drilled_order)}번째{'✔' if ok else '✖'}")
        elif quiz_anchor:
            tags.append("앵커(재)")

        # ② 공개 — 표면형이 턴에 있다. 학습자가 방금 정답을 낸 뒤의 에코는 공개가 아니다
        echo_ok = self.last_learner is not None and self.last_learner.kind == "correct" \
            and len(self.since_learner) == 1
        revealed_ids = [i for i in mentioned if not (echo_ok and self.current and i == self.current.item.item_id)]
        if mentioned:
            tags.append("표면형:" + "·".join(self.items[i].surface for i in mentioned))
            for i in mentioned:
                if i in self.records:
                    self.records[i].surface_uttered = True
                    self.records[i].surface_heard = True

        # ③ 어느 항목인가
        item_id, how = await self.identify(text, seg, revealed_ids, is_question, new_item_cue)
        if item_id:
            tags.append(f"→{self.items[item_id].surface}({how})")
        # «묻는 턴» = 물음표·명령형이 있거나, 항목의 **영어 뜻을 댔다**(끝이 잘린 턴 "if you want to say "X,"" 도 묻는 것이다)
        asked = is_question or (item_id is not None and how != "reveal" and how != "continuation")

        # 상태 전이 — 퀴즈 모드에서 미드릴 항목이 나오면 퀴즈가 끝난 것이다(드릴 복귀)
        if self.mode == "quiz" and item_id and item_id not in self.records:
            self.mode = "drill"
            tags.append("퀴즈끝→드릴")
        if self.mode == "drill" and new_item_cue and item_id is None and not mentioned:
            tags.append("새항목예고(미식별)")

        if self.mode == "drill":
            if item_id and item_id not in self.records:
                self._start_item(item_id, turn, how, pre_reveal=item_id in mentioned)
            elif item_id and item_id in self.records and self.current and item_id != self.current.item.item_id \
                    and (is_question or item_id in revealed_ids):
                # 끝낸 항목을 드릴 모드에서 다시 묻는다 — 앵커 없는 재출제이거나 되감기다.
                # 회차를 열되 anchored=False 로 표시한다(판정기는 이 항목을 보류할 수 있다 — 결정 6-3).
                rec = self.records[item_id]
                rec.rounds.append(QuizRound(n=len(rec.rounds) + 1, asked_at=self.now(), anchored=False))
                self.current = rec
                tags.append(f"⛔앵커없는재출제/되감기(회차{len(rec.rounds)})")
            if self.current is not None and self.current.item.item_id in revealed_ids:
                if self.current.rounds and not self.current.rounds[-1].anchored:
                    self.current.rounds[-1].revealed = True
                    tags.append("재출제공개")
                else:
                    self.current.drill_revealed = True
                    tags.append("드릴공개")
        else:  # quiz
            rec = self.records.get(item_id) if item_id else None
            if rec is not None:
                # 새 회차: 다른 항목으로 옮겼거나(재출제 포함) 이 블록에서 처음 묻는 항목
                if rec is not self.current or rec.item.item_id not in self.quiz_block_asked:
                    rec.rounds.append(QuizRound(n=len(rec.rounds) + 1, asked_at=self.now()))
                    self.quiz_block_asked.add(rec.item.item_id)
                    tags.append(f"퀴즈회차{len(rec.rounds)}")
                self.current = rec
            if self.current is not None and self.current.rounds and self.current.item.item_id in revealed_ids:
                self.current.rounds[-1].revealed = True
                tags.append("퀴즈공개")

        self.log(f"🦫 {text[:140]}{'…' if len(text) > 140 else ''}")
        if tags:
            self.log("   " + " ".join(tags))
        self.first_turn_end_seen.set()
        if self.probe:
            return

        # ④ 말하기 — 대본 정책
        reply_text, lang, kind = self.decide_reply(text, mentioned, asked)
        if reply_text is None:
            self.log("   (대기 — 이번 턴엔 말하지 않는다)")
            uplink.open = True
            return
        self.pending_speak = asyncio.create_task(self._speak_later(reply_text, lang, kind, uplink))

    async def identify(self, text: str, seg: str, mentioned: list[int], is_question: bool,
                       new_item_cue: bool) -> tuple[Optional[int], str]:
        # ⚠ `mentioned` 는 호출부가 **에코를 뺀** 표면형 목록(revealed_ids)을 준다 — 학습자가 방금 맞힌 답을 비버가
        #   되풀이한 것("You nailed it. 배고파요. Next…")을 공개로 읽으면 다음 항목 질문이 묻힌다(1403 t2).
        """어느 항목을 묻나 — ① 따옴표 안 영어 뜻 ② 따옴표 없는 여러 단어 구절 ③ 키워드(질문 턴만) ④ LLM 1회.

        후보 순서(같은 점수면 앞이 이긴다):
          drill  : 현 항목 → 미드릴 → 이미 끝낸 항목(재출제·되감기 감지용 — 점수 감점)
          quiz   : 이 블록에서 아직 안 낸 드릴 항목 → 낸 것(재출제) → 미드릴(퀴즈 종료 감지)
        """
        drilled = [self.records[i].item for i in self.drilled_order]
        undrilled = [it for iid, it in self.items.items() if iid not in self.records]
        if self.mode == "quiz":
            pri = [c for c in drilled if c.item_id not in self.quiz_block_asked]
            order = pri + [c for c in drilled if c not in pri] + undrilled
            penalty = {c.item_id: 0 for c in drilled}
        else:
            cur = [self.current.item] if self.current is not None else []
            order = cur + [c for c in undrilled if c not in cur] + [c for c in drilled if c not in cur]
            penalty = {c.item_id: 30 for c in drilled if c not in cur}   # 끝낸 항목은 감점 — 새 항목이 우선

        quotes = [norm_en(q) for q in quoted_segments(text)]
        quotes = [q for q in quotes if q and re.search(r"[a-z]", q)]     # 한국어 인용(공개)은 제외
        if mentioned and not quotes:
            # 영어 인용은 없고 한국어 표면형만 있다 = 공개·복창 요구 턴("Now say '화장실이 어디예요?'").
            # 그 항목이다 — 본문의 감탄("Yes, really!")을 키워드로 읽으면 엉뚱한 항목이 시작된다(1401).
            return mentioned[0], "reveal"
        low = norm_en(strip_quotes(text)) if quotes else norm_en(text)
        scored: list[tuple[int, int, str]] = []
        for it in order:
            phrase = norm_en(it.en)
            pen = penalty.get(it.item_id, 0)
            best: Optional[tuple[int, str]] = None
            for q in quotes:
                if q == phrase:
                    best = (300, f'quote="{q}"')
                    break
                if len(q) >= 4 and (q in phrase or phrase in q):
                    best = max(best or (0, ""), (200 + min(len(q), 40), f'quote~"{q}"'))
                    continue
                for kw in it.keywords:
                    k = norm_en(kw)
                    if k and re.search(rf"(?<![a-z']){re.escape(k)}(?![a-z])", q):
                        best = max(best or (0, ""), (150, f'quote-kw:{kw}'))
                        break
            if best is None and len(phrase.split()) >= 2 and phrase in low:
                best = (100 + len(phrase), "phrase")
            if best is None and is_question and not quotes:
                for kw in it.keywords:
                    k = norm_en(kw)
                    if k and re.search(rf"(?<![a-z']){re.escape(k)}(?![a-z])", low):
                        best = (50 + len(k), f"kw:{kw}")
                        break
            if best is not None:
                scored.append((best[0] - pen, it.item_id, best[1]))
        if scored:
            scored.sort(key=lambda s: -s[0])
            top = scored[0]
            close = [s for s in scored if s[0] >= top[0] - 5 and s[1] != top[1]]
            if close and self.picker.enabled and is_question:
                ids = [top[1]] + [s[1] for s in close[:3]]
                pick, how = await self.picker.pick(text, [self.items[i] for i in ids], seg[-600:])
                if pick:
                    return pick, how
            return top[1], top[2]

        # 표면형만 있고 영어 뜻이 없다(공개·복창 요구 턴) → 그 표면형의 항목
        if mentioned and len(mentioned) == 1:
            return mentioned[0], "reveal"
        if is_question and self.picker.enabled:
            pick, how = await self.picker.pick(text, order[:24], seg[-600:])
            if pick:
                return pick, how
            return None, how
        return None, "continuation"

    def _start_item(self, item_id: int, turn: Turn, how: str, *, pre_reveal: bool) -> None:
        k = len(self.drilled_order) + 1
        rec = ItemRecord(item=self.items[item_id], k=k, policy=self.policy_of(k), ident=how,
                         intro_turn=turn.n, intro_pre_reveal=pre_reveal)
        self.records[item_id] = rec
        self.drilled_order.append(item_id)
        self.current = rec
        self._last_started = rec
        turn.tags.append(f"새항목#{k} 정책{rec.policy}" + (" ⛔선공개" if pre_reveal else " 선질문✔"))

    # ---- 대본 정책 ----------------------------------------------------------- #
    def decide_reply(self, text: str, mentioned: list[int], asked: bool = True) -> tuple[Optional[str], str, str]:
        """(말할 문장, 언어, 종류). 종류: correct|casual|idk|distractor|parrot."""
        rec = self.current
        if rec is None:
            # 아직 항목을 못 잡았다 — 비버가 물었으면 모른다고 답해 공개를 유도한다(공개로 식별된다)
            if QUESTION_RE.search(text):
                return IDK_EN, "en", "idk"
            return None, "", ""
        p = rec.policy
        surface = rec.item.surface
        if not asked and not mentioned:
            # 묻지 않은 턴(앵커 선언·인사·감탄) — 학습자처럼 짧게 수긍만 한다
            return "Okay.", "en", "ack"
        # 드릴 모드인데 이 항목에 «앵커 없는 회차» 가 열려 있으면(끝낸 항목 재질문) 퀴즈 정책으로 답한다
        in_requiz = self.mode == "drill" and bool(rec.rounds) and not rec.rounds[-1].anchored \
            and rec is not self._last_started
        if self.mode == "drill" and not in_requiz:
            rec.drill_attempts += 1
            if p == 2:
                rec.drill_answers.append("parrot" if rec.drill_revealed else "idk")
                if rec.drill_revealed:
                    return surface, "ko", "parrot"
                return IDK_EN, "en", "idk"
            if p == 4:
                if rec.drill_attempts >= 4:
                    # 포기했어야 한다(최대 2번 재시도). 계속 물으면 통화를 다 태우니 **공개된 표면형을 복창해 풀어준다** —
                    # 드릴 복창은 통과가 아니므로 기대(drilled only)는 그대로다. 보고서에 «포기 안 함» 이 남는다.
                    if rec.drill_attempts == 4:
                        self.notes.append(f"#{rec.k} {surface}: 드릴 오답 3회 뒤에도 비버가 포기하지 않았다 → 4회째부터 복창으로 풀어줌")
                    if rec.drill_revealed:
                        rec.drill_answers.append("parrot")
                        return surface, "ko", "parrot"
                    rec.drill_answers.append("idk")
                    return IDK_EN, "en", "idk"
                rec.drill_answers.append("distractor")
                return self.next_distractor(), "ko", "distractor"
            # 1·3·5·6: 첫 시도 정답. 비버가 먼저 공개했으면(선질문 위반) 그 답은 복창이다
            kind = "parrot" if (rec.drill_revealed and rec.drill_attempts == 1) else "correct"
            rec.drill_answers.append(kind)
            return surface, "ko", kind
        # 퀴즈
        if not rec.rounds:
            rec.rounds.append(QuizRound(n=1, asked_at=self.now()))
            self.quiz_block_asked.add(rec.item.item_id)
        rd = rec.rounds[-1]
        if rd.revealed:
            rd.answers.append("parrot")
            return surface, "ko", "parrot"
        if p in (1, 6):
            rd.answers.append("correct")
            rd.spontaneous_correct = True
            return surface, "ko", "correct"
        if p == 2 or p == 4:
            rd.answers.append("idk")
            return IDK_EN, "en", "idk"
        if p == 3:
            rd.answers.append("casual")
            return rec.item.casual, "ko", "casual"
        if p == 5:
            if not rd.answers:
                rd.answers.append("distractor")
                return self.next_distractor(), "ko", "distractor"
            # 오답 뒤 비버가 공개 없이 다시 물었다(힌트 경로) → 정답
            rd.answers.append("correct")
            rd.spontaneous_correct = True
            rd.hint_path = True
            return surface, "ko", "correct"
        return surface, "ko", "correct"

    async def _speak_later(self, reply: str, lang: str, kind: str, uplink: Uplink) -> None:
        spoke = False
        try:
            pcm = await self.voice.pcm(reply, lang)
            await asyncio.sleep(PRE_SPEECH_S)
            uplink.open = True
            spoke = True
            turn = self.add_turn("learner", reply, kind=kind, item_id=self.current.item.item_id if self.current else 0)
            self.last_learner = turn
            self.since_learner = []
            if kind in ("correct", "parrot") and self.current is not None and norm_ko(reply) == norm_ko(self.current.item.surface):
                self.current.surface_uttered = True
            if kind == "correct":
                self.spontaneous += 1
            self.log(f"👤 {reply}   [{kind}]")
            await uplink.speak(pcm)
        except asyncio.CancelledError:
            if not spoke:
                self.log("   (비버가 먼저 말해 발화 취소)")
            raise


# --------------------------------------------------------------------------- #
# 통화 1회
# --------------------------------------------------------------------------- #
def get_token(base: str, email: str, password: str) -> str:
    import httpx

    r = httpx.post(f"{base}/__dev/signup", json={"email": email, "password": password}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"⛔ 토큰 실패 {r.status_code}: {r.text[:200]}")
    return r.json()["access_token"]


async def run_call(base: str, token: str, items: dict[int, Item], voice: Voice, picker: Picker, *,
                   duration_min: int, probe: bool, verbose: bool) -> Session:
    import websockets

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + WS_PATH + f"?token={token}"
    sess = Session(items, voice, picker, probe=probe, verbose=verbose)
    start = {"type": "start", "character_id": 1, "locale": LOCALE, "duration_min": duration_min,
             "call_type": "expression", "aec": {"supported": False}, "sample_rate": SR_IN, "num_channels": 1,
             "tz_offset_min": 540}
    async with websockets.connect(ws_url, max_size=None, ping_interval=20, ping_timeout=20,
                                  open_timeout=30) as ws:
        sess.t0 = time.perf_counter()
        await ws.send(json.dumps(start))
        sess.log(f"WS 연결 · start 전송 (duration_min={duration_min}, call_type=expression)")
        uplink = Uplink(ws)
        up_task = asyncio.create_task(uplink.run())

        async def keepalive() -> None:
            while True:
                await asyncio.sleep(15)
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps({"type": "ping", "t": int(time.time() * 1000)}))

        ka_task = asyncio.create_task(keepalive())
        try:
            async for raw in ws:
                if isinstance(raw, (bytes, bytearray)):
                    sess.beaver_audio_bytes += len(raw)
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await sess.on_json(msg, uplink)
                if sess.ended:
                    with contextlib.suppress(Exception):
                        await ws.send(json.dumps({"type": "playback_done"}))
                    break
                if probe and sess.first_turn_end_seen.is_set():
                    sess.log("probe: 첫 비버 턴 해석 완료 → 끊는다")
                    break
        except Exception as exc:  # noqa: BLE001
            sess.errors.append(f"ws: {type(exc).__name__}: {exc}")
            sess.log(f"⛔ WS 종료 {type(exc).__name__}: {exc}")
        finally:
            for t in (up_task, ka_task, sess.pending_speak):
                if t is not None:
                    t.cancel()
    return sess


# --------------------------------------------------------------------------- #
# ⑥ 채점
# --------------------------------------------------------------------------- #
@dataclass
class Score:
    judge_ok: bool = True
    period_ok: bool = True
    praise_ok: bool = True
    lines: list[str] = field(default_factory=list)
    db_rows: dict[int, dict] = field(default_factory=dict)
    expr_result: list[dict] = field(default_factory=list)
    level_after: Optional[int] = None
    call_row: dict = field(default_factory=dict)


def read_db_outcome(sf, call_id: Optional[int], items: dict[int, Item]) -> Score:
    from sqlalchemy import text as sql
    from domains.learning.repository import mastery_repository as mr

    sc = Score()
    with sf() as db:
        sc.level_after = mr.get_language_level(db, MEMBER_ID, LANGUAGE)
        prog = progress_rows(db, list(items))
        for iid, row in prog.items():
            sc.db_rows[iid] = {"drilled_call_id": row.drilled_call_id, "drilled_at": row.drilled_at,
                               "quiz_passed_at": row.quiz_passed_at}
        if call_id:
            r = db.execute(sql("SELECT call_type, status, total_time, expression_result, summary, usage_engine, "
                               "usage_json, usage_in_audio, usage_in_text, usage_out_audio, usage_out_text "
                               "FROM call WHERE call_id=:c"), {"c": call_id}).first()
            if r is not None:
                sc.call_row = {"call_type": r[0], "status": r[1], "total_time": r[2], "summary": r[4],
                               "usage_engine": r[5]}
                # 원가는 estimate_call_cost_usd 로만(계약 — call_usage_engine_contract)
                try:
                    from domains.learning.service import normalcall_service as _ns
                    uj = r[6] if isinstance(r[6], dict) else (json.loads(r[6]) if r[6] else None)
                    cost, unknown = _ns.estimate_call_cost_usd(
                        r[5], in_audio=r[7] or 0, in_text=r[8] or 0, out_audio=r[9] or 0, out_text=r[10] or 0, usage_json=uj)
                    sc.call_row["cost_usd"] = round(cost, 4)
                    if unknown:
                        sc.call_row["cost_unknown_vendors"] = unknown
                    sc.call_row["usage"] = {"in_audio": r[7], "in_text": r[8], "out_audio": r[9], "out_text": r[10]}
                except Exception as exc:  # noqa: BLE001 - 표시용
                    sc.call_row["cost_usd"] = f"?({exc})"
                try:
                    sc.expr_result = json.loads(r[3]) if r[3] else []
                except ValueError:
                    sc.expr_result = []
    return sc


# ⭐ T16 — 서버가 «[시스템] 지금 퀴즈» 큐를 얹어 퀴즈를 연다(docs/20260911_2000_표현학습-T16-…md §1). 그 로그 줄의
#   시각과 비버의 앵커 턴(하네스가 문구로 잡은 것)을 대조해 «큐→앵커 지연» 과 «큐 없이 난 앵커» 를 센다.
#   ⚠ 문구는 expr-build 가 정한다 — «normalcall 표현학습 퀴즈 큐» 로 시작하게 부탁했다. 구현 뒤 call_session.py 에서 확인해 맞춘다.
QUIZ_CUE_LOG_PREFIX = "normalcall 표현학습 퀴즈 큐"   # = call_session.EXPR_QUIZ_CUE_LOG_PREFIX (25c64fe 확인)
# 서버는 한 퀴즈에 세 줄을 남긴다: «arm:»(묶음 찼다) → «얹기:»(다음 학습자 발화에 큐 전송) → «열림:»(다음 비버 turn_start).
# 앵커와 대조할 순간은 **얹기** 다 — 모델이 큐를 받은 시각. arm 은 학습자가 말할 때까지 기다린다(대기=Ns 가 그 줄에 있다).
QUIZ_CUE_STAGE = "얹기"
QUIZ_CUE_MATCH_WINDOW_S = 30.0      # 큐 뒤 이 안에 난 앵커만 그 큐의 것(실측 2.5 = 2~5s · 3.1 = 10~16s. 60s 는 다음 큐의 앵커를 훔쳤다 — 1420)
_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z?\s+(.*)$")


def parse_quiz_cues(log_lines: list[str], stage: str = QUIZ_CUE_STAGE) -> list[tuple[float, str]]:
    """gcloud `value(timestamp,textPayload)` 줄들 → [(epoch, payload)] — 퀴즈 큐 **해당 단계** 줄만. 시각 없는 줄은 버린다.

    stage="" 이면 접두가 있는 줄 전부(arm·얹기·열림)."""
    out: list[tuple[float, str]] = []
    needle = f"{QUIZ_CUE_LOG_PREFIX} {stage}" if stage else QUIZ_CUE_LOG_PREFIX
    for ln in log_lines or []:
        m = _LOG_TS_RE.match(ln.strip())
        if not m or needle not in m.group(2):
            continue
        ts = m.group(1)
        try:
            dt = datetime.strptime(ts[:26], "%Y-%m-%dT%H:%M:%S.%f" if "." in ts else "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        out.append((dt.replace(tzinfo=timezone.utc).timestamp(), m.group(2)))
    out.sort()
    return out


def match_quiz_cues(cues: list[tuple[float, str]], anchors: list[tuple[int, float]],
                    window_s: float = QUIZ_CUE_MATCH_WINDOW_S) -> dict:
    """큐(서버) ↔ 앵커(비버 문구) 시간 대조.

    Args:
        cues: [(epoch, payload)] 시간순.
        anchors: [(turn_n, epoch)] — 하네스가 앵커로 본 비버 턴.
    Returns:
        {"pairs": [(cue_epoch, turn_n, delay_s)], "cues_without_anchor": [cue_epoch…],
         "anchors_without_cue": [turn_n…]} — 큐 하나에 앵커 하나(큐 뒤 window 안, 시간순 첫 것).
    """
    pairs: list[tuple[float, int, float]] = []
    used: set[int] = set()
    lonely_cues: list[float] = []
    for c_t, _ in cues:
        hit = None
        for tn, a_t in sorted(anchors, key=lambda x: x[1]):
            if tn in used or a_t < c_t:
                continue
            if a_t - c_t <= window_s:
                hit = (tn, a_t)
            break
        if hit is None:
            lonely_cues.append(c_t)
        else:
            used.add(hit[0])
            pairs.append((c_t, hit[0], round(hit[1] - c_t, 1)))
    lonely_anchors = [tn for tn, _ in anchors if tn not in used]
    return {"pairs": pairs, "cues_without_anchor": lonely_cues, "anchors_without_cue": lonely_anchors}


def fetch_server_logs(call_started: datetime, call_ended: datetime, service: str) -> list[str]:
    """(선택) gcloud logging read — 표현학습 판정·arm 줄만."""
    # ⚠ 앞 여유를 크게 주면 **직전 통화의 꼬리**(마지막 판정·진도 저장·큐)가 섞여 큐 대조가 남의 큐를 짝짓는다(1406 에서 발생).
    #   로그에 call_id 가 없어 시간으로만 가른다 — 통화 시작(WS 연결) 2초 전부터.
    a = (call_started - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    b = (call_ended + timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
    flt = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" '
           f'AND timestamp>="{a}" AND timestamp<="{b}" '
           # ⭐ T17-6 «늦은 전사» 가설 확정용 — 학습자 전사 조각(«👤 user:») 과 턴 flush(«👤 USER[t..]») 를 시간순으로 같이 붙인다.
           #   재개 시드 주입·제어 태그 스크럽 줄도(T17-1 벙어리 턴 규칙).
           'AND (textPayload:"표현학습" OR textPayload:"재접지" OR textPayload:"compress" OR textPayload:"arm" '
           'OR textPayload:"👤" OR textPayload:"재개 시드" OR textPayload:"제어 태그" OR textPayload:"압축 감지")')
    try:
        out = subprocess.run(["gcloud", "logging", "read", flt, "--project", "bt-dev-web-01", "--limit", "1000",
                              "--format", "value(timestamp,textPayload)", "--order", "asc"],
                             capture_output=True, text=True, encoding="utf-8", timeout=120, shell=(os.name == "nt"))
        return [ln for ln in (out.stdout or "").splitlines() if ln.strip()] or [f"(로그 없음) {out.stderr[:200]}"]
    except Exception as exc:  # noqa: BLE001
        return [f"(gcloud 실패: {exc})"]


def score_and_report(sess: Session, sc: Score, items: dict[int, Item], *, duration_min: int, run_no: int,
                     server_logs: list[str] | None, out_dir: Path) -> tuple[Path, bool]:
    L: list[str] = []
    cid = sess.call_id
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    L.append(f"# 표현학습 E2E — call {cid} ({stamp}, run {run_no})")
    L.append("")
    L.append(f"- 통화 길이 요청 {duration_min}분 · 실제 {sess.turns[-1].t if sess.turns else 0:.0f}초 · 종료 사유 `{sess.end_reason}` · "
             f"비버 턴 {sum(1 for t in sess.turns if t.role == 'beaver')} · 학습자 턴 {sum(1 for t in sess.turns if t.role == 'learner')} · "
             f"비버 오디오 {sess.beaver_audio_bytes / 48000:.0f}초 · LLM 폴백 {sess.picker.calls}회")
    L.append(f"- DB call: {sc.call_row} · 레벨(뒤) {sc.level_after}")
    first_b = next((t for t in sess.turns if t.role == "beaver"), None)
    empty_b = [t for t in sess.turns if t.role == "beaver" and not t.text.strip()]
    dbl = [t for i, t in enumerate(sess.turns) if t.role == "beaver" and i > 0 and sess.turns[i - 1].role == "beaver" and sess.turns[i - 1].text.strip() and t.text.strip()]
    rep_b = [t for t in sess.turns if t.role == "beaver" and re.search(r"\b(\w{3,}(?: \w+){0,3})\b[,.!? ]+\1\b", t.text, re.I)]
    L.append(f"- 엔진 관찰: usage_engine `{sc.call_row.get('usage_engine')}` · 원가 ${sc.call_row.get('cost_usd')} · "
             f"첫 비버 발화 {first_b.t if first_b else float('nan'):.1f}s · 빈 비버 턴 {len(empty_b)} · 학습자 없이 연속 비버 턴 {len(dbl)} · "
             f"같은 구절 반복 턴 {len(rep_b)}" + (" (" + ", ".join(f"t{t.n}" for t in rep_b[:8]) + ")" if rep_b else ""))
    if sess.errors:
        L.append(f"- ⛔ 오류: {sess.errors}")
    L.append("")

    # ── 판정 정확도 ─────────────────────────────────────────────── #
    L.append("## 1. 판정 정확도 — 기대(하네스 ground truth) ↔ DB")
    L.append("")
    L.append("| # | 항목 | 정책 | 식별 | 기대 drilled | DB drilled | 기대 passed | DB passed | result.passed | 판정 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    res_by_id = {int(r.get("item_id", 0)): r for r in sc.expr_result if isinstance(r, dict)}
    judge_ok = True
    for iid in sess.drilled_order:
        rec = sess.records[iid]
        row = sc.db_rows.get(iid, {})
        db_drilled = row.get("drilled_call_id") == cid
        db_passed = row.get("quiz_passed_at") is not None
        exp_drilled = rec.surface_uttered
        # 보낸 것과 들린 것이 다르면(STT) 서버는 표면형을 못 봤다 — drilled 은 어느 쪽이든 허용(~), 대신 표시한다
        stt_amb = exp_drilled and not rec.surface_heard
        exp_passed = rec.expected_passed
        rp = res_by_id.get(iid, {}).get("passed")
        amb = rec.expectation_ambiguous
        drilled_ok = (db_drilled == exp_drilled) or stt_amb
        ok = drilled_ok and (amb or db_passed == exp_passed)
        judge_ok &= bool(ok)
        exp_s = ("passed~" if amb else "passed") if exp_passed else "—"
        drilled_s = ("✔~(STT 불일치)" if stt_amb else "✔") if exp_drilled else "✖(표면형 미출현)"
        amb = amb or (stt_amb and db_drilled != exp_drilled)
        L.append(f"| {rec.k} | {rec.item.surface} | {rec.policy} | {rec.ident} | {drilled_s} | {'✔' if db_drilled else '✖'} | "
                 f"{exp_s} | {'passed' if db_passed else '—'} | {rp} | {'~' if (ok and amb) else ('✔' if ok else '✖')} |")
    extra = [iid for iid, row in sc.db_rows.items() if row.get("drilled_call_id") == cid and iid not in sess.records]
    for iid in extra:
        judge_ok = False
        row = sc.db_rows[iid]
        L.append(f"| – | {items[iid].surface} | – | (하네스 미드릴) | ✖ | ✔ | — | {'passed' if row.get('quiz_passed_at') else '—'} | "
                 f"{res_by_id.get(iid, {}).get('passed')} | ✖ 과검출 |")
    sc.judge_ok = judge_ok
    L.append("")
    L.append("- 기대 drilled = 표면형이 비버 공개나 학습자 발화로 실제 한 번 나왔다(결정 6 «모국어 설명만으론 안 됨»). "
             "기대 passed = 퀴즈 회차에서 **공개 전 자발 정답**(하네스가 고른 답). `passed~` = 그 정답이 앵커 없는 재출제에서만 났다 → 판정기가 보류해도 된다(결정 6-3), 어느 쪽이든 ✔(~)")
    L.append("")

    # ── 퀴즈 주기 ──────────────────────────────────────────────── #
    cue_match: dict | None = None
    if server_logs is not None:
        cues = parse_quiz_cues(server_logs)
        if sess.turns:
            # 이 통화 첫 턴보다 앞선 큐는 직전 통화의 것이다(gcloud 창이 시간으로만 갈리므로 한 번 더 거른다)
            cues = [c for c in cues if c[0] >= sess.turns[0].wall - 15.0]
        anchor_walls = [(tn, sess.turns[tn].wall) for tn, _ in sess.anchors if 0 <= tn < len(sess.turns)]
        cue_match = match_quiz_cues(cues, anchor_walls)
        cue_match["cues"] = cues
    L.append("## 2. 퀴즈 주기 — 앵커가 3·6·9번째 항목 직후에만 났나")
    if not sess.anchors:
        L.append(f"- 앵커 0회 (드릴 {len(sess.drilled_order)}개) — " +
                 ("✖ 3개 이상 드릴했는데 퀴즈가 없었다" if len(sess.drilled_order) >= QUIZ_GROUP else "묶음이 안 차 판단 불가"))
        sc.period_ok = len(sess.drilled_order) < QUIZ_GROUP
    else:
        cue_by_turn = {tn: (c_t, d) for c_t, tn, d in cue_match["pairs"]} if cue_match else {}
        for tn, cnt in sess.anchors:
            ok = cnt > 0 and cnt % QUIZ_GROUP == 0
            sc.period_ok &= ok
            if server_logs is None or not (cue_match and cue_match["cues"]):
                cue_s = ""          # 로그를 안 붙였거나 큐 줄이 0(T16 전) — 열을 비운다(아래 요약 줄이 이유를 말한다)
            elif tn in cue_by_turn:
                cue_s = f" · 큐→앵커 {cue_by_turn[tn][1]:.1f}s"
            else:
                cue_s = " · ⛔큐 없이 난 앵커"
            L.append(f"- 턴 {tn}: {cnt}번째 항목 뒤 {'✔' if ok else '✖'}{cue_s}")
    if server_logs is not None:
        # ⭐ T16 열 — 서버 큐(로그) ↔ 비버 앵커(문구) 대조. 큐 줄이 0이면 «구현 전이거나 문구가 다르다» 로 읽어라.
        n_c = len(cue_match["cues"]) if cue_match else 0
        if n_c == 0:
            L.append(f"- 서버 퀴즈 큐 로그 0줄 (접두 «{QUIZ_CUE_LOG_PREFIX}») — T16 배포 전이거나 로그 문구가 다르다")
        else:
            delays = [d for _, _, d in cue_match["pairs"]]
            n_arm = len(parse_quiz_cues(server_logs, "arm"))
            n_open = len(parse_quiz_cues(server_logs, "열림"))
            L.append(f"- 서버 큐 단계: arm {n_arm} · 얹기 {n_c} · 열림 {n_open}" +
                     (" ⚠ arm 뒤 얹기 안 됨 " + str(n_arm - n_c) if n_arm > n_c else ""))
            L.append(f"- 서버 퀴즈 큐(얹기) {n_c}회 · 앵커로 이어진 큐 {len(cue_match['pairs'])} "
                     f"(지연 {', '.join(f'{d:.1f}s' for d in delays) or '—'}) · 큐 뒤 앵커 없음 {len(cue_match['cues_without_anchor'])} · "
                     f"큐 없이 난 앵커 {len(cue_match['anchors_without_cue'])}")
            for c_t in cue_match["cues_without_anchor"]:
                L.append(f"  - ⛔ 큐 {datetime.fromtimestamp(c_t, timezone.utc).strftime('%H:%M:%S')}Z 뒤 {QUIZ_CUE_MATCH_WINDOW_S:.0f}s 안에 비버가 퀴즈를 열지 않았다")
    quizzed = [sess.records[i] for i in sess.drilled_order if sess.records[i].quizzed]
    L.append(f"- 퀴즈에 오른 항목 {len(quizzed)}/{len(sess.drilled_order)}: " +
             ", ".join(f"{r.item.surface}(회차{len(r.rounds)}{'' if all(x.anchored for x in r.rounds) else '·앵커없음' + str(sum(1 for x in r.rounds if not x.anchored))})" for r in quizzed))
    L.append("")

    # ── 모국어 선질문 ─────────────────────────────────────────── #
    L.append("## 3. 모국어 선질문 — 새 항목 첫 턴에 표면형이 없었나")
    pre = [r for r in sess.records.values() if r.intro_pre_reveal]
    L.append(f"- 위반 {len(pre)}/{len(sess.records)}: " + (", ".join(f"#{r.k} {r.item.surface}" for r in pre) or "없음 ✔"))
    L.append("")

    # ── 거짓 칭찬 ────────────────────────────────────────────── #
    L.append("## 4. 거짓 칭찬 — 대본상 오답 다음 비버 턴")
    fp = [(r, tn) for r in sess.records.values() for tn in r.beaver_said_correct_after_wrong]
    sc.praise_ok = not fp
    if fp:
        for r, tn in fp:
            L.append(f"- ✖ #{r.k} {r.item.surface} 턴 {tn}: 「{sess.turns[tn].text[:120]}」")
    else:
        L.append("- 없음 ✔")
    mixed = [t for t in sess.turns if any(tag.startswith("칭찬+교정") for tag in t.tags)]
    if mixed:
        L.append(f"- 칭찬+교정 혼합(애매) {len(mixed)}건: " + "; ".join(f"턴{t.n}" for t in mixed))
    L.append("")

    # ── 자발 산출 · 포기 경로 · 힌트 경로 ─────────────────────── #
    L.append("## 5. 자발 산출 · 경로")
    drill_first = sum(1 for r in sess.records.values() if r.drill_answers and r.drill_answers[0] == "correct")
    quiz_spont = sum(1 for r in sess.records.values() for rd in r.rounds if rd.spontaneous_correct)
    sess.spontaneous = drill_first + quiz_spont
    L.append(f"- 자발 산출 {sess.spontaneous}회 = 드릴 첫 시도 정답 {drill_first} + 퀴즈 공개 전 정답 {quiz_spont} (항목·회차당 1회)")
    for r in sess.records.values():
        if r.policy == 4:
            L.append(f"- 포기 경로 #{r.k} {r.item.surface}: 드릴 시도 {r.drill_attempts}회 · 거짓칭찬 {len(r.beaver_said_correct_after_wrong)}건")
        if r.policy == 5 and r.rounds:
            rd = r.rounds[0]
            L.append(f"- 힌트 경로 #{r.k} {r.item.surface}: 답 {rd.answers} · 공개 {'있음(→failed 기대)' if rd.revealed else '없음'} · 힌트경로 {'성립' if rd.hint_path else '불성립'}")
        if r.policy == 3 and r.rounds:
            L.append(f"- 격식 함정 #{r.k} {r.item.surface}: 반말 「{r.item.casual}」 답 {r.rounds[0].answers} · 공개 {'있음' if r.rounds[0].revealed else '없음'}")
        if r.policy == 2 and r.rounds:
            L.append(f"- 앵무새 함정 #{r.k} {r.item.surface}: 퀴즈 답 {r.rounds[0].answers} · 공개 {'있음' if r.rounds[0].revealed else '없음 ⚠(공개 없이 넘어감)'}")
    for n in sess.notes:
        L.append(f"- ⚠ {n}")
    L.append("")

    # ── 전사 기타 ─────────────────────────────────────────────── #
    L.append("## 6. 전사 관찰")
    br = [t for t in sess.turns if "[대괄호]" in t.tags]
    L.append(f"- 대괄호 [Country] 류: {len(br)}건" + (" — " + "; ".join(f"턴{t.n}" for t in br) if br else " ✔"))
    rw = [t for t in sess.turns if any("되감기" in tag for tag in t.tags)]
    L.append(f"- 되감기/앵커 없는 퀴즈 의심: {len(rw)}건" + (" — " + "; ".join(f"턴{t.n}" for t in rw) if rw else " ✔"))
    stt_bad = [t for t in sess.turns if t.role == "learner" and t.stt and norm_ko(t.stt) != norm_ko(t.text)]
    stt_none = [t for t in sess.turns if t.role == "learner" and not t.stt and t.kind != "ack"]
    L.append(f"- input_transcript 부재(학습자 턴): {len(stt_none)}건" + (" — " + ", ".join(f"턴{t.n}" for t in stt_none) if stt_none else " ✔"))
    L.append(f"- STT 불일치(학습자 보낸 말 ↔ input_transcript): {len(stt_bad)}건" +
             ("".join(f"\n  - 턴{t.n} 보냄「{t.text}」 들림「{t.stt}」" for t in stt_bad) or " ✔"))
    L.append("")

    if server_logs is not None:
        L.append("## 7. 서버 로그 (gcloud) — 마지막 판정 ms · arm · 큐 · 학습자 전사 조각/flush · 재개 시드")
        def _cnt(needle: str) -> int:
            return sum(1 for ln in server_logs if needle in ln)
        L.append(f"- 재개 시드 주입 {_cnt('대화 재개 시드 주입(')}회 · 제어 태그 스크럽만(시드 생략) {_cnt('제어 태그 스크럽만')}회 · "
                 f"폴백 채택 {_cnt('provenance=stt_fallback')} · 폴백 기각 {_cnt('퀴즈 폴백 기각')} · 👤 user 조각 {_cnt('👤 user:')} · USER flush {_cnt('USER[t')}")
        arms = [(re.search(r"근거=([^,]+)", ln), re.search(r"peak=(\d+)", ln), re.search(r"바닥=(\d+)", ln), re.search(r"대화=(\d+)", ln))
                for ln in server_logs if "재접지 arm" in ln]
        comps = [re.search(r"압축 감지 #(\d+) \(prompt (\d+) → (\d+)\)", ln) for ln in server_logs]
        comps = [m for m in comps if m]
        L.append(f"- 컨텍스트 압축 감지 {len(comps)}회" + (": " + " · ".join(f"#{m.group(1)} {m.group(2)}→{m.group(3)}" for m in comps) if comps else ""))
        if arms:
            L.append("- 재접지 arm: " + " · ".join(
                f"{(a.group(1) if a else '?')} peak={(pk.group(1) if pk else '?')} 바닥={(fl.group(1) if fl else '?')} 대화={(dv.group(1) if dv else '?')}"
                for a, pk, fl, dv in arms))
        else:
            L.append("- 재접지 arm 0회")
        L.append("```")
        L.extend(server_logs[:1000])
        L.append("```")
        L.append("")

    # ── 전사 ─────────────────────────────────────────────────── #
    L.append("## 전사")
    L.append("```")
    for t in sess.turns:
        who = "🦫" if t.role == "beaver" else "👤"
        extra_s = f"  [{t.kind}]" if t.kind else ""
        stt_s = ("" if not t.stt else (f"  (들림: {t.stt})" if norm_ko(t.stt) == norm_ko(t.text) else f"  ⚠(들림: {t.stt})"))
        L.append(f"{t.t:6.1f}s {who} t{t.n}: {t.text}{extra_s}{stt_s}")
        if t.tags:
            L.append(f"          ↳ {' '.join(t.tags)}")
    L.append("```")

    passed_all = sc.judge_ok and sc.period_ok and sc.praise_ok
    L.insert(2, f"**결과: {'✔ 전부 기대와 일치' if passed_all else '✖ 불일치'}** — 판정 {'✔' if sc.judge_ok else '✖'} · "
                f"퀴즈주기 {'✔' if sc.period_ok else '✖'} · 거짓칭찬 {'✔' if sc.praise_ok else '✖'} · "
                f"선질문 위반 {len(pre)} · 자발 산출 {sess.spontaneous}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stamp}_call{cid or 'none'}.md"
    path.write_text("\n".join(L), encoding="utf-8")
    # 원자료(턴·항목 기록·DB 결과)도 남긴다 — 채점 규칙이 바뀌면 통화를 다시 걸지 않고 다시 읽을 수 있게
    raw = {
        "call_id": cid, "duration_min": duration_min, "end_reason": sess.end_reason, "errors": sess.errors,
        "anchors": sess.anchors, "drilled_order": sess.drilled_order,
        "quiz_cue_match": ({k: v for k, v in cue_match.items() if k != "cues"} | {"cues": [[t, p] for t, p in cue_match["cues"]]})
        if cue_match else None,
        "turns": [{"n": t.n, "role": t.role, "t": round(t.t, 1), "wall": round(t.wall, 3), "text": t.text, "kind": t.kind, "stt": t.stt, "tags": t.tags}
                  for t in sess.turns],
        "records": {str(iid): {"surface": r.item.surface, "k": r.k, "policy": r.policy, "ident": r.ident,
                               "intro_pre_reveal": r.intro_pre_reveal, "drill_attempts": r.drill_attempts,
                               "drill_answers": r.drill_answers, "drill_revealed": r.drill_revealed,
                               "surface_uttered": r.surface_uttered, "surface_heard": r.surface_heard,
                               "rounds": [{"n": x.n, "anchored": x.anchored, "revealed": x.revealed, "answers": x.answers,
                                           "spontaneous_correct": x.spontaneous_correct, "hint_path": x.hint_path} for x in r.rounds],
                               "false_praise_turns": r.beaver_said_correct_after_wrong}
                    for iid, r in sess.records.items()},
        "db": {"rows": {str(k): {kk: (vv.isoformat() if hasattr(vv, "isoformat") else vv) for kk, vv in v.items()} for k, v in sc.db_rows.items()},
               "expression_result": sc.expr_result, "call": sc.call_row, "level_after": sc.level_after},
        "server_logs": server_logs,
    }
    path.with_suffix(".json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    return path, passed_all


# --------------------------------------------------------------------------- #
def main() -> None:
    global QUIZ_GROUP
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-root", default=os.environ.get("BEAVERTALK_ENV_ROOT"),
                    help=".env·gcp_key.json·tts_key.json 이 있는 루트(기본: 이 저장소 루트 또는 $BEAVERTALK_ENV_ROOT)")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    ap.add_argument("--password", default=os.environ.get("E2E_PASSWORD", DEFAULT_PASSWORD))
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--fix-items", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--probe", action="store_true", help="①②③만: 첫 비버 턴 해석까지 보고 끊는다")
    ap.add_argument("--runs", type=int, default=0, help="reset → 통화 → 채점 을 N 회")
    ap.add_argument("--no-reset", action="store_true", help="--runs 앞의 자동 reset 생략")
    ap.add_argument("--duration", type=int, default=5, help="duration_min (서버가 3~15 로 클램프)")
    ap.add_argument("--no-llm", action="store_true", help="항목 매칭 LLM 폴백 끄기")
    ap.add_argument("--logs", action="store_true", help="gcloud logging read 로 서버 로그 첨부")
    ap.add_argument("--service", default="beavertalk-app-demo-api")
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "e2e"))
    ap.add_argument("--tee", default=None, help="콘솔 출력을 이 UTF-8 파일에도 쓴다")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.tee:
        sys.stdout = Tee(Path(args.tee))

    bootstrap_env(args.env_root)
    from domains.learning.repository import mastery_repository as mr
    QUIZ_GROUP = int(mr.EXPRESSION_QUIZ_GROUP)
    sf = db_session_factory()
    global MEMBER_ID
    with sf() as db:
        MEMBER_ID = resolve_member(db, args.email)
        from domains.learning.service import call_service as _cs
        try:
            plan = _cs.effective_plan(db, MEMBER_ID)
            engine = _cs.live_engine_for(db, MEMBER_ID)
        except Exception as exc:  # noqa: BLE001 - 표시용
            plan, engine = f"?({exc})", "?"
    print(f"회원 {args.email} → member_id={MEMBER_ID} · 플랜={plan} · live_engine_for={engine}")

    if args.status:
        cmd_status(sf)
    if args.fix_items:
        cmd_fix_items(sf)
    if args.reset:
        cmd_reset(sf)
    if not (args.probe or args.runs):
        return

    with sf() as db:
        items = load_items(db)
        lvl = mr.get_language_level(db, MEMBER_ID, LANGUAGE)
        others = [i.item_id for i in all_level_items(db) if i.item_id not in FIXED]
        leak = [iid for iid in others if progress_rows(db, [iid]).get(iid) is None
                or progress_rows(db, [iid])[iid].quiz_passed_at is None]
    if lvl != LEVEL_NO or leak:
        # 남은 풀 크기가 아니라 «제외 28개가 전부 passed 인가» 를 본다 — 18개 쪽은 매 회 --reset 이 되돌린다
        print(f"⚠ 레벨={lvl} 미고정 제외항목={len(leak)}개 — --fix-items 를 먼저 돌려야 18개가 고정된다")
        if not args.probe:
            sys.exit(2)

    token = get_token(args.base, args.email, args.password)
    voice = Voice()
    picker = Picker(enabled=not args.no_llm)
    if args.duration < 3:
        print("⚠ duration_min 은 서버가 3분으로 올린다(DEMO_DURATION_MIN_MINUTES=3)")

    if args.probe:
        sess = asyncio.run(run_call(args.base, token, items, voice, picker, duration_min=args.duration,
                                    probe=True, verbose=args.verbose))
        print("\n=== probe 결과 ===")
        for t in sess.turns:
            print(f"{t.role}: {t.text}\n   {t.tags}")
        return

    runs_ok = 0
    for run_no in range(1, args.runs + 1):
        print(f"\n════════ run {run_no}/{args.runs} ════════")
        if not args.no_reset:
            cmd_reset(sf, quiet=False)
        started = datetime.now(timezone.utc)
        sess = asyncio.run(run_call(args.base, token, items, voice, picker, duration_min=args.duration,
                                    probe=False, verbose=args.verbose))
        ended = datetime.now(timezone.utc)
        time.sleep(3)  # 저장은 call_ended 전에 끝나지만(call_session 2838→2892) 소켓 정리 여유
        sc = read_db_outcome(sf, sess.call_id, items)
        logs = fetch_server_logs(started, ended, args.service) if args.logs else None
        path, ok = score_and_report(sess, sc, items, duration_min=args.duration, run_no=run_no,
                                    server_logs=logs, out_dir=Path(args.out_dir))
        runs_ok += int(ok)
        print(f"\n보고서: {path}")
        print(f"결과: {'✔ 일치' if ok else '✖ 불일치'} — 판정 {'✔' if sc.judge_ok else '✖'} 퀴즈주기 {'✔' if sc.period_ok else '✖'} "
              f"거짓칭찬 {'✔' if sc.praise_ok else '✖'} · 드릴 {len(sess.drilled_order)} · 자발 {sess.spontaneous}")
    print(f"\n총 {runs_ok}/{args.runs} 회 일치")
    sys.exit(0 if runs_ok == args.runs else 1)


if __name__ == "__main__":
    main()
