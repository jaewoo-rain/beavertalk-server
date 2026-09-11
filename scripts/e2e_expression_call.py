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

    ## cur_* 체계 (DB 대공사 2단계 H1 — API 계약 docs/plans/2026-09-12-cur-2단계-통화경로-이전.md §2, 어댑터 scripts/e2e_cur_adapter.py)
    $E2E --status                          # GET /cur/me
    $E2E --reset --lesson 4                # POST /__dev/cur-reset {lesson_no:4}  (--fix-items 도 같은 호출 = «차시 고정»)
    $E2E --runs 1 --course expression      # 표현학습(판정 대조 그대로 · 항목 출처 = cur)
    $E2E --runs 1 --course freetalk [--expect-locked]   # 프리토킹(판정 없음 · 상황/표현 등장 · /cur/me 전이) · 잠금이면 COURSE_LOCKED
    $E2E --runs 1 --course auto            # 서버가 정한 코스(call_started.course)로 검증
    $E2E --scenario lesson-cycle           # reset(차시4) → 표현 1통(18) → 표현 2통(12+복습6) → 프리토킹 → /cur/me no=5 · PASS/FAIL 표
    ⛔ 비밀번호는 E2E_PASSWORD env 로만 준다.

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_cur_adapter import COURSE_LOCKED_CODE, CurApi, CurApiError, summarize_me  # noqa: E402

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
CUR_ITEMS_PER_CALL = 18           # 계획 §2 CUR_ITEMS_PER_CALL — 서버 settings 가 있으면 main 이 덮는다
DEFAULT_LESSON_NO = 4             # A1-T01-1 (30항목) — 시나리오 기본 차시

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
# 비버가 직전 «정답» 을 받지 않았다는 신호(교정 어휘가 없어도) — 동음 후보 갈아타기 전용(1441: «What is that? We're talking about …»)
REJECT_RE = re.compile(
    r"\b(what (is|was) that|not (quite|it|right|that)|wrong|no,|nope|we'?re talking about|i mean|i asked|i said|"
    r"properly|try again|again|that'?s not)\b", re.I)
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
    """표면형이 텍스트에 나왔나. ⚠ 두 음절 이하(「이」「제」「저」「명」)는 부분문자열이면 어느 문장에서든 걸린다(«생일이 언제예요?» 에 「이」) —
    그 경우 서버와 같은 낱말 경계 매처(quiz_judge.mentions: 어절 = 표면형 + 조사 꼬리)를 쓴다. 긴 표면형은 정규화 부분일치."""
    if not surface:
        return False
    if len(norm_ko(surface)) <= 2 and re.search(r"[가-힣]", surface):
        try:
            from domains.learning.service.quiz_judge import mentions as _mentions
            return bool(_mentions(text, surface))
        except Exception:  # noqa: BLE001 - 매처가 없으면 부분일치로
            pass
    return norm_ko(surface) in norm_ko(text)


def surfaces_in(text: str, items: dict[int, "Item"]) -> list[int]:
    """턴 안에 표면형(문법은 라벨 조각·예문 포함)이 실제로 나온 항목들(긴 것 우선 — 부분 포함 오탐 완화)."""
    hits = [iid for iid, it in items.items() if any(has_surface(text, v) for v in getattr(it, "variants", [it.surface]))]
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
    casual: str                       # 반말형. "" = 없음(어휘·문법) → 정책3 은 «오답→공개→복창» 변형으로 돈다
    keywords: tuple[str, ...]
    kind: str = "chunk"               # cur_item.kind: chunk | vocab | grammar
    example: str = ""                 # cur_item.examples[0] — 문법 항목은 이걸 «말할 답» 으로 쓴다(패턴 표기는 말할 수 없다)
    lesson_id: int = 0                # 이 항목이 실린 차시(복습 항목은 제 차시)
    role: str = ""                    # cur_lesson_item.role: grammar | must | core | support
    review: bool = False              # 하네스 예측: 이번 통화에 «복습» 으로 실릴 후보
    seq: int = 0
    # 서버 문자열 판정(quiz_judge.mentions — cur 경로도 T16 그대로, bt-back 결정 ①)이 «말할 답» 에서 이 항목을 알아보는가.
    # 문법 템플릿(N은/는 …)은 예문에 따라 못 알아볼 수 있다 → False 면 서버는 drilled/passed 를 절대 못 찍는다(기대도 그렇게 둔다)
    server_matchable: bool = True

    @property
    def answer(self) -> str:
        """학습자가 «정답» 으로 말할 것.

        ⚠ 한·두 음절을 혼자 말하지 않는다 — 1437 에서 「이」 를 세 번 말했는데 Gemini 입력 전사가 세 번 다 비었다(한 음절 오디오를 버린다).
          · 문법(패턴 표기): 예문(연습 문장)
          · 동사·형용사(…다): 「가다, 가다」 — 예문은 활용형(갈 거예요)이라 서버 매처가 못 알아본다
          · 두 음절 이하 명사·대명사 등: 「{surface}요」 — 매처는 «요» 꼬리를 조사로 받아 알아본다(실측)
          · 그 외: 표면형
        """
        if self.kind == "grammar" and self.example:
            return self.example
        core = norm_ko(self.surface)
        if self.kind != "chunk" and len(core) <= 2:
            if self.surface.endswith("다"):
                return f"{self.surface}, {self.surface}"
            return f"{self.surface}요"
        return self.surface

    @property
    def long_form(self) -> str:
        """전사가 안 왔을 때 한 번 더 말하는 더 긴 형태 — 예문(있으면), 아니면 답을 두 번."""
        if self.example and self.kind != "grammar":
            return self.example
        a = self.answer
        return f"{a} {a}"

    @property
    def variants(self) -> list[str]:
        """전사 대조용 표기들 — 문법은 라벨의 쉼표/슬래시 조각(«N입니까?»)과 예문도 그 항목이 나온 것으로 본다(서버도 예문 OR)."""
        out = [self.surface]
        if self.kind == "grammar":
            out += [p_.strip() for p_ in re.split(r"[,/]", self.surface) if len(norm_ko(p_)) >= 3]
            if self.example:
                out.append(self.example)
        return out


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
    superseded_by: int = 0            # 오식별로 판명돼 다른 항목으로 대체됨(판정표·주기 계산에서 제외)

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
                 verbose: bool, course: str = "expression", lesson: dict | None = None) -> None:
        self.items = items
        self.voice = voice
        self.picker = picker
        self.probe = probe
        self.verbose = verbose
        # ⭐ cur 체계: 코스. "expression" | "freetalk" | "auto"(서버가 call_started.course 로 알려주면 그걸로 확정)
        self.course = course
        self.course_from_server: Optional[str] = None
        self.locked = False                       # ServerError COURSE_LOCKED 로 끊김(잠긴 프리토킹)
        self.lesson = lesson or {}                # /cur/me 의 lesson dict(no·code·situation·partner…) — 프리토킹 관찰용
        self.ft_i = 0                             # 프리토킹 대본 커서
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
        self.distractor_pool: list[str] = []      # cur: 이번 통화 목록 밖 같은 차시 표면형(없으면 DISTRACTORS)
        self.cancel_streak = 0                    # 비버 연속 턴으로 우리 발화가 취소된 횟수(연속) — 1 이상이면 다음 발화는 즉시
        self._last_reply_item: Optional[ItemRecord] = None
        self.last_beaver_corrected = False        # 직전 «정답» 을 비버가 고쳤다(칭찬 없이) — 동음 후보 갈아타기 신호
        self.last_beaver_end: float = 0.0         # 마지막 비버 turn_end 시각(now 기준) — 워치독용
        self.last_spoke_at: float = -1.0          # 마지막으로 내가 소리를 낸 시각
        self.last_beaver_text: str = ""           # 워치독이 «따라 하라» 문구를 볼 때 쓴다
        self.watchdog_fires = 0
        self.speak_errors = 0

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
        pool = self.distractor_pool or DISTRACTORS
        d = pool[self.distractor_i % len(pool)]
        self.distractor_i += 1
        return d

    def policy_of(self, k: int) -> int:
        return (k - 1) % 6 + 1

    # ---- 프레임 처리 --------------------------------------------------------- #
    async def on_json(self, msg: dict, uplink: Uplink) -> None:
        t = msg.get("type")
        if t == "call_started":
            self.call_id = int(msg["call_id"]) if msg.get("call_id") else None
            # ⭐ auto 코스: 서버가 정한 코스를 알려준다(계획 §8). 명시 코스여도 서버 값이 오면 기록만 한다.
            self.course_from_server = msg.get("course")
            if self.course == "auto" and self.course_from_server in ("expression", "freetalk"):
                self.course = self.course_from_server
            self.log(f"call_started call_id={self.call_id} character={msg.get('character_id')} course={self.course_from_server or '(없음)'} → 검증 코스 {self.course}")
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
            self.last_beaver_end = self.now()
            self.last_beaver_text = text
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
            if msg.get("code") == COURSE_LOCKED_CODE:
                # 잠긴 프리토킹 — 서버가 소켓을 닫는다(계획 §2). 실패가 아니라 «잠금 확인» 이다(--expect-locked 면 기대값)
                self.locked = True
            if not msg.get("recoverable", True):
                self.ended = True
                self.end_reason = f"error:{msg.get('code')}"
        elif t in ("pong", "sentence", "teaching_plan", "hint"):
            pass
        else:
            self.log(f"? 미지 메시지 {t}")

    # ---- 비버 턴 해석 -------------------------------------------------------- #
    async def on_beaver_turn(self, text: str, uplink: Uplink) -> None:
        if self.course == "freetalk":
            await self.on_freetalk_turn(text, uplink)
            return
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

        # ③ 비버가 직전 «정답» 을 고쳤나(칭찬 없이 교정 어휘) — 동음 후보 갈아타기·반복 금지에 쓴다
        body0 = reaction_part(text)
        whole0 = strip_quotes(text)
        self.last_beaver_corrected = bool(self.last_learner is not None and self.last_learner.kind == "correct"
                                          and len(self.since_learner) == 1
                                          and (CORRECTION_RE.search(body0) or REJECT_RE.search(whole0))
                                          and not (ACCEPT_RE.search(body0) or PRAISE_RE.search(body0)))
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
        # (비버가 우리 오답 「이름요」 를 따옴표로 되풀이하므로 «현 항목이 언급됐다» 를 제외 조건으로 쓰지 않는다)
        exclude = {self.current.item.item_id} if (self.last_beaver_corrected and self.current is not None and self.mode == "drill"
                                                   and "(정정)" not in self.current.ident) else set()
        item_id, how = await self.identify(text, seg, revealed_ids, is_question, new_item_cue, exclude=exclude)
        if exclude and item_id and item_id != self.current.item.item_id and item_id not in self.records:
            # 비버가 우리 «정답» 을 고치며 다른 뜻(예문·설명)을 댔고 그게 다른 미드릴 항목이다 → 처음 짚은 항목이 오식별이었다(1441 이름→명)
            old = self.current
            old.superseded_by = item_id
            if old.item.item_id in self.drilled_order:
                self.drilled_order.remove(old.item.item_id)
            tags.append(f"오식별 정정: {old.item.surface}→{self.items[item_id].surface}")
            self._start_item(item_id, turn, how + "(정정)", pre_reveal=item_id in mentioned)
            item_id_started = True
        else:
            item_id_started = False
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
            if item_id and item_id not in self.records and not item_id_started:
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
        self._last_reply_item = self.current

    async def on_freetalk_turn(self, text: str, uplink: Uplink) -> None:
        """프리토킹: 판정 없음. 비버 턴에 차시 표현이 나오는지·상황(situation)/상대(partner) 문구가 나오는지만 기록하고,
        학습자는 차시 예문을 돌려 말한다(비버가 차시 표현을 끌어내는지 보려고 재료를 준다)."""
        turn = self.add_turn("beaver", text)
        tags = turn.tags
        if BRACKET_RE.search(text):
            tags.append("[대괄호]")
        mentioned = surfaces_in(text, self.items)
        if mentioned:
            tags.append("차시표현:" + "·".join(self.items[i].surface for i in mentioned))
        if len(self.turns) == 1 or all(t.role == "beaver" for t in self.turns):
            tags.append("첫턴")
        self.log(f"🦫 {text[:140]}{'…' if len(text) > 140 else ''}")
        if tags:
            self.log("   " + " ".join(tags))
        self.first_turn_end_seen.set()
        if self.probe:
            return
        reply, lang = self.freetalk_reply(text)
        self.pending_speak = asyncio.create_task(self._speak_later(reply, lang, "freetalk", uplink))

    def freetalk_reply(self, text: str) -> tuple[str, str]:
        """차시 항목의 예문(있으면)·표면형을 순서대로 돌려 말한다. 예문이 문장이라 대화가 굴러가고, 표현 등장을 잴 수 있다."""
        pool = [it for it in self.items.values() if not it.review] or list(self.items.values())
        if not pool:
            return "네, 좋아요.", "ko"
        it = pool[self.ft_i % len(pool)]
        self.ft_i += 1
        return (it.example or it.surface), "ko"

    async def identify(self, text: str, seg: str, mentioned: list[int], is_question: bool,
                       new_item_cue: bool, exclude: set[int] | None = None) -> tuple[Optional[int], str]:
        # ⚠ `mentioned` 는 호출부가 **에코를 뺀** 표면형 목록(revealed_ids)을 준다 — 학습자가 방금 맞힌 답을 비버가
        #   되풀이한 것("You nailed it. 배고파요. Next…")을 공개로 읽으면 다음 항목 질문이 묻힌다(1403 t2).
        """어느 항목을 묻나 — ① 따옴표 안 영어 뜻 ② 따옴표 없는 여러 단어 구절 ③ 키워드(질문 턴만) ④ LLM 1회.

        후보 순서(같은 점수면 앞이 이긴다):
          drill  : 현 항목 → 미드릴 → 이미 끝낸 항목(재출제·되감기 감지용 — 점수 감점)
          quiz   : 이 블록에서 아직 안 낸 드릴 항목 → 낸 것(재출제) → 미드릴(퀴즈 종료 감지)
        """
        drilled = [self.records[i].item for i in self.drilled_order]
        # 오식별로 대체된 항목(superseded)은 후보에서 영구 제외 — 비버가 우리 오답(«name?»)을 되풀이해도 다시 잡히지 않게
        dead = {iid for iid, r in self.records.items() if r.superseded_by}
        undrilled = [it for iid, it in self.items.items() if iid not in self.records]
        if self.mode == "quiz":
            pri = [c for c in drilled if c.item_id not in self.quiz_block_asked]
            order = pri + [c for c in drilled if c not in pri] + undrilled
            penalty = {c.item_id: 0 for c in drilled}
        else:
            cur = [self.current.item] if self.current is not None else []
            order = cur + [c for c in undrilled if c not in cur] + [c for c in drilled if c not in cur]
            penalty = {c.item_id: 30 for c in drilled if c not in cur}   # 끝낸 항목은 감점 — 새 항목이 우선
        exclude = (exclude or set()) | dead
        if exclude:
            order = [c for c in order if c.item_id not in exclude]

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
                if len(q) >= 4 and len(phrase) >= 4 and (q in phrase or phrase in q):   # 「저」=«i» 같은 한 글자 뜻이 아무 인용에나 걸리지 않게
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
            # 아직 항목을 못 잡았다 — 비버가 물었으면 모른다고 답해 공개를 유도한다(공개로 식별된다). 안 물었어도 침묵하지 않는다(1438).
            if QUESTION_RE.search(text):
                return IDK_EN, "en", "idk"
            return "Okay.", "en", "ack"
        p = rec.policy
        surface = rec.item.answer        # ⚠ 이름은 surface 지만 «말할 답» 이다 — 문법 항목은 예문(패턴 표기는 말할 수 없다)
        pr = self.parrot_request(text)
        if pr is not None:
            # «Say 명» 처럼 표면형을 대고 따라 하라면 정책과 무관하게 복창한다(1441: 「명」 을 3턴 동안 안 따라 해 무음 종료)
            if self.mode == "drill":
                rec.drill_attempts += 1
                rec.drill_answers.append("parrot")
            elif rec.rounds:
                rec.rounds[-1].answers.append("parrot")
            return pr
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
            if rec.drill_answers.count("correct") >= 2 and self.last_beaver_corrected:
                # ③ 같은 «정답» 을 두 번 냈는데 비버가 두 번 다 고쳤다 — 항목을 잘못 짚었을 가능성이 크다(1441 이름↔명). 세 번 반복 대신 모른다고 해 공개를 받는다
                rec.drill_answers.append("idk")
                return IDK_EN, "en", "idk"
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
            if not rec.item.casual:
                # 어휘·문법엔 반말이 없다 — 격식 함정 대신 «오답 1회 → 공개 → 복창» 으로 not-passed 를 잰다
                rd.answers.append("distractor")
                return self.next_distractor(), "ko", "distractor"
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
            # 비버가 턴을 연달아 내 우리 발화가 취소되기만 하면(1438: 3턴 혼잣말) 다음엔 쉬지 않고 바로 말한다
            await asyncio.sleep(PRE_SPEECH_S if self.cancel_streak == 0 else 0.15)
            uplink.open = True
            spoke = True
            turn = self.add_turn("learner", reply, kind=kind, item_id=self.current.item.item_id if self.current else 0)
            self.last_learner = turn
            self.since_learner = []
            if kind in ("correct", "parrot") and self.current is not None and (
                    norm_ko(reply) in (norm_ko(self.current.item.surface), norm_ko(self.current.item.answer))
                    or any(has_surface(reply, v) for v in self.current.item.variants)):
                self.current.surface_uttered = True
            if kind == "correct":
                self.spontaneous += 1
            self.log(f"👤 {reply}   [{kind}]")
            await uplink.speak(pcm)
            self.last_spoke_at = self.now()
            self.cancel_streak = 0
            # ① 내 발화의 input_transcript 가 3초 안에 안 오면(한·두 음절 오디오를 Gemini 가 버린다 — 1437) 더 긴 형태로 한 번 더.
            #   비버가 이미 말을 시작했으면(turn_start) 들은 것이니 재발화하지 않는다.
            if kind in ("correct", "parrot", "casual", "distractor") and lang == "ko":
                for _ in range(30):
                    await asyncio.sleep(0.1)
                    if turn.stt or self.cur_turn_id is not None or self.ended:
                        break
                if not turn.stt and self.cur_turn_id is None and not self.ended:
                    item = self.current.item if self.current is not None else None
                    longer = item.long_form if item is not None else f"{reply} {reply}"
                    if norm_ko(longer) == norm_ko(reply):
                        longer = f"{reply}. {reply}."
                    turn.tags.append("재발화(전사 없음)")
                    t2 = self.add_turn("learner", longer, kind=kind, item_id=turn.item_id)
                    t2.tags.append("재발화")
                    self.last_learner = t2
                    self.log(f"👤 {longer}   [{kind}·재발화 — 3초 안 전사 없음]")
                    await uplink.speak(await self.voice.pcm(longer, lang))
                    self.last_spoke_at = self.now()
        except asyncio.CancelledError:
            if not spoke:
                self.cancel_streak += 1
                self.log(f"   (비버가 먼저 말해 발화 취소 ×{self.cancel_streak})")
            raise
        except Exception as exc:  # noqa: BLE001 — ⛔ 태스크 예외는 소리 없이 죽는다(1441 61초 침묵 의심). 적고, 대신 아무 말이라도 한다
            self.speak_errors += 1
            self.errors.append(f"speak: {type(exc).__name__}: {exc}")
            self.log(f"⛔ 발화 실패({type(exc).__name__}: {exc}) → 대체 발화")
            with contextlib.suppress(Exception):
                uplink.open = True
                fb = self.add_turn("learner", IDK_EN, kind="idk")
                fb.tags.append("대체발화(예외)")
                self.last_learner = fb
                self.since_learner = []
                await uplink.speak(await self.voice.pcm(IDK_EN, "en"))
                self.last_spoke_at = self.now()

    async def watchdog(self, uplink: Uplink) -> None:
        """①⛔ 무응답 방지 — 비버 turn_end 뒤 6초 동안 내가 소리를 안 냈으면(발화 태스크가 죽었든 취소됐든) 무조건 말한다.
        «따라 하라» 문구(say/repeat + 표면형)면 그 표현(짧으면 X요)을, 아니면 «I don't know». 비버가 말하는 중이면 끝나기를 기다린다."""
        try:
            while not self.ended:
                await asyncio.sleep(0.5)
                if self.probe or self.course == "freetalk" or self.cur_turn_id is not None or self.last_beaver_end <= 0:
                    continue
                if self.now() - self.last_beaver_end < 6.0 or self.last_spoke_at >= self.last_beaver_end:
                    continue
                if self.pending_speak is not None and not self.pending_speak.done():
                    self.pending_speak.cancel()
                self.watchdog_fires += 1
                reply, lang, kind = self.parrot_request(self.last_beaver_text) or (IDK_EN, "en", "idk")
                self.log(f"⏱ 워치독 #{self.watchdog_fires}: 비버 turn_end 뒤 {self.now() - self.last_beaver_end:.1f}s 무발화 → 「{reply}」")
                uplink.open = True
                t = self.add_turn("learner", reply, kind=kind, item_id=self.current.item.item_id if self.current else 0)
                t.tags.append("워치독")
                self.last_learner = t
                self.since_learner = []
                try:
                    await uplink.speak(await self.voice.pcm(reply, lang))
                    self.last_spoke_at = self.now()
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"watchdog speak: {exc}")
        except asyncio.CancelledError:
            raise

    def parrot_request(self, text: str) -> Optional[tuple[str, str, str]]:
        """② «Say X» / «Repeat after me: X» / «Try saying X» — X 가 목록 항목이면 그 항목의 답(짧으면 X요·문법은 예문)을 복창.
        항목이 아니어도 따옴표 안 한국어면 그대로(한 음절이면 «X요»)."""
        # 명령형 + 바로 따옴표 한국어(«Say "명"» · «Repeat after me: "…"» · «Try saying "명"»)만. «How do you say …?» 는 질문이지 복창 요청이 아니다.
        if not text:
            return None
        m = re.search(r"\b(say|repeat(?: after me)?|try saying|say it like this|listen)\s*[:,]?\s*[\"“'‘]([^\"”'’]{1,60})[\"”'’]", text, re.I)
        if not m or not re.search(r"[가-힣]", m.group(2)):
            return None
        q = m.group(2).strip().rstrip(".!?")
        # (우리 오답을 «"이름요"? What is that?» 처럼 되풀이한 건 앞에 명령형이 없어 위 정규식에 안 걸린다)
        hits = surfaces_in(q, self.items)
        if hits:
            it = self.items[hits[0]]
            return it.answer, "ko", "parrot"
        return (f"{q}요" if len(norm_ko(q)) <= 1 else q), "ko", "parrot"


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
                   duration_min: int, probe: bool, verbose: bool, course: str = "expression",
                   lesson: dict | None = None, distractors: list[str] | None = None) -> Session:
    import websockets

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + WS_PATH + f"?token={token}"
    sess = Session(items, voice, picker, probe=probe, verbose=verbose, course=course, lesson=lesson)
    sess.distractor_pool = list(distractors or [])
    # call_type: "expression" | "freetalk" | "auto"(서버가 정해 call_started.course 로 알림 — 계획 §8)
    start = {"type": "start", "character_id": 1, "locale": LOCALE, "duration_min": duration_min,
             "call_type": course, "aec": {"supported": False}, "sample_rate": SR_IN, "num_channels": 1,
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

        # ⭐ T23 (2026-09-12): 서버는 통화 길이로 끊지도 작별 시드도 넣지 않는다 — **앱이 소켓을 닫는다**(스위치 없음, 코드에서 삭제).
        #   하네스도 클라이니 duration 에 닿으면 앱과 같은 무음 컷으로 소켓을 닫는다(안 닫으면 540s 백스톱까지 간다).
        async def client_cut() -> None:
            await asyncio.sleep(duration_min * 60)
            if not sess.ended:
                sess.ended = True
                sess.end_reason = "client_cut"
                sess.log(f"client_cut: {duration_min}분 도달 → 소켓 닫음(앱과 같은 무음 컷)")
                with contextlib.suppress(Exception):
                    await ws.close()

        cut_task = asyncio.create_task(client_cut())
        wd_task = asyncio.create_task(sess.watchdog(uplink))
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
            for t in (up_task, ka_task, cut_task, wd_task, sess.pending_speak):
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
    cur: dict = field(default_factory=dict)      # cur 체계: {lesson, me_pre, me_post, quiz_items, new_ids, review_ids, api_error}


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
           'OR textPayload:"👤" OR textPayload:"재개 시드" OR textPayload:"제어 태그" OR textPayload:"압축 감지" OR textPayload:"재연결")')
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
    if sess.watchdog_fires or sess.speak_errors:
        L.append(f"- ⚠ 하네스 워치독 발화 {sess.watchdog_fires}회 · 발화 태스크 예외 {sess.speak_errors}회 — 무응답 방지가 동작했다(원인은 §전사 태그 «워치독»·«대체발화»)")
    if sc.cur:
        c = sc.cur
        les = c.get("lesson") or {}
        pre, post = c.get("me_pre") or {}, c.get("me_post") or {}
        # ③ 목록 크기 정본: 서버 로그 «normalcall cur 표현학습: … 항목 N(복습 M)» > 통화 전 예측. 결과 행(다룬 것)은 참고
        srv_n = srv_m = None
        for ln in (server_logs or []):
            m_ = re.search(r"cur 표현학습.*?항목\s*(\d+)\s*\(복습\s*(\d+)\)", ln)
            if m_:
                srv_n, srv_m = int(m_.group(1)), int(m_.group(2))
        size_src = f"서버로그 목록 {srv_n}(복습 {srv_m})" if srv_n is not None else f"예측 새 {c.get('predicted_new')} · 복습 {c.get('predicted_review')}"
        c["list_new"] = (srv_n - srv_m) if srv_n is not None else c.get("predicted_new")
        c["list_review"] = srv_m if srv_n is not None else c.get("predicted_review")
        L.append(f"- **cur 차시** no={les.get('no')} {les.get('code')} · 통화 전 drilled {pre.get('items_drilled')}/{pre.get('items_total')} "
                 f"→ 후 {post.get('items_drilled')}/{post.get('items_total')} · status {pre.get('status')}→{post.get('status')} · "
                 f"이번 통화 목록 = {size_src} · 결과 행(다룬 것) 새 {len(c.get('new_ids') or [])} · 복습 {len(c.get('review_ids') or [])}"
                 f"{' (review 플래그 없음 → 통화 전 drilled 로 추정)' if c.get('review_estimated') else ''}"
                 f"{' · ⚠ API 오류: ' + str(c.get('api_error')) if c.get('api_error') else ''}")
        # 누적 컬럼 단조 확인 — 이번 통화 passed 인 항목은 cur_member_item.quiz_passed_at 이 있어야 하고, 이전 통화 passed 가 지워지면 안 된다
        cum = c.get("cum_rows") or {}
        broke = [iid for iid, row in sc.db_rows.items() if row.get("quiz_passed_at") and iid in cum and cum[iid].get("quiz_passed_at") is None]
        L.append(f"- 누적 컬럼(cur_member_item) 단조 확인: 이번 통화 passed {sum(1 for r in sc.db_rows.values() if r.get('quiz_passed_at'))}건 중 "
                 f"누적 quiz_passed_at 없음 {len(broke)}건{' ⛔ ' + str(broke) if broke else ' ✔'} · 회원 누적 drilled {sum(1 for r in cum.values() if r.get('drilled_at'))} · passed {sum(1 for r in cum.values() if r.get('quiz_passed_at'))}")
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
    sup = [r for r in sess.records.values() if r.superseded_by]
    if sup:
        L.append("- 오식별 정정 " + str(len(sup)) + "건(하네스가 처음 잘못 짚은 항목 — 판정표 제외): "
                 + ", ".join(f"{r.item.surface}→{sess.items[r.superseded_by].surface}" for r in sup))
    for iid in sess.drilled_order:
        rec = sess.records[iid]
        row = sc.db_rows.get(iid, {})
        db_drilled = row.get("drilled_call_id") == cid
        db_passed = row.get("quiz_passed_at") is not None
        exp_drilled = rec.surface_uttered
        # 보낸 것과 들린 것이 다르면(STT) 서버는 표면형을 못 봤다 — drilled 은 어느 쪽이든 허용(~), 대신 표시한다
        stt_amb = exp_drilled and not rec.surface_heard
        # ⚠ 문법 항목: 하네스는 예문을 말한다. 서버 판정은 T16 그대로(quiz_judge.mentions 템플릿 인식 — 결정 ①)이므로
        #   «서버가 그 예문에서 템플릿을 알아보는가» 를 같은 함수로 미리 계산했다(server_matchable). 못 알아보면 서버는
        #   drilled/passed 를 못 찍는 게 정상 — 기대도 그렇게 두고 표에 «템플릿 미인식» 을 남긴다(커리큘럼 예문·매처 수정 재료).
        unmatchable = not rec.item.server_matchable
        if unmatchable:
            exp_drilled = False
            exp_passed = False
        exp_passed = rec.expected_passed
        rp = res_by_id.get(iid, {}).get("passed")
        amb = rec.expectation_ambiguous
        stt_amb = stt_amb and not unmatchable
        drilled_ok = (db_drilled == exp_drilled) or stt_amb
        ok = drilled_ok and (amb or db_passed == exp_passed)
        judge_ok &= bool(ok)
        exp_s = ("passed~" if amb else "passed") if exp_passed else "—"
        drilled_s = ("✔~(STT 불일치)" if stt_amb else "✔") if exp_drilled else ("✖(템플릿 미인식 — 서버 못 봄)" if unmatchable else "✖(표면형 미출현)")
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
    unm = [r for r in sess.records.values() if not r.item.server_matchable]
    if unm:
        L.append("- ⚠ 서버 미인식 " + str(len(unm)) + "건 — 표면형 매처(quiz_judge.mentions)도 못 알아보고 예문도 없다: "
                 + ", ".join(f"「{r.item.surface}」←「{r.item.answer}」" for r in unm) + " → 서버는 이 항목을 drilled/passed 로 찍을 수 없다(커리큘럼 예문 또는 매처 수정 재료)")
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
        recon = [ln for ln in server_logs if "재연결" in ln]
        L.append(f"- 재연결 로그 {len(recon)}줄"
                 + ("".join("\n  - " + ln.split("call_session:")[-1][:160] for ln in recon[:10]) if recon else ""))
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
# cur_* 체계 (DB 대공사 2단계 H1) — 항목 출처·상태·초기화를 cur 로. 옛 learning_item 경로(FIXED·cmd_* 위)는 읽기 전용 유산.
# --------------------------------------------------------------------------- #
# L1 청크(차시 1)의 반말형·키워드 힌트 — 옛 FIXED 표를 표면형으로 되짚는다. 그 밖은 아래 규칙.
_CHUNK_HINTS: dict[str, tuple[str, tuple[str, ...]]] = {norm_ko(v[0]): (v[2], v[3]) for v in FIXED.values()}


def casual_for(surface: str) -> str:
    """반말형 추정. 표가 있으면 표, 없으면 어미 규칙, 그래도 없으면 ""(정책3 이 오답 변형으로 돈다)."""
    hint = _CHUNK_HINTS.get(norm_ko(surface))
    if hint:
        return hint[0]
    t = surface.strip().rstrip("?!.")
    q = "?" if surface.strip().endswith("?") else ""
    for end, rep_ in (("이에요", "이야"), ("예요", "야"), ("어요", "어"), ("아요", "아"), ("해요", "해"), ("워요", "워"), ("돼요", "돼")):
        if t.endswith(end):
            return t[: -len(end)] + rep_ + q
    return ""   # 명사·동사 원형·문법 패턴·-세요 명령형 — 반말 함정 대신 오답 변형


def keywords_for(surface: str, en: str, kind: str) -> tuple[str, ...]:
    hint = _CHUNK_HINTS.get(norm_ko(surface))
    if hint:
        return hint[1]
    # ⚠ 쉼표·세미콜론으로 **먼저** 가른 뒤 정규화한다 — norm_en 이 쉼표를 지워 «name, title» 이 «name title» 한 덩이가 됐다(1441 명↔이름)
    parts = [norm_en(k) for k in re.split(r"[;,/]| or ", en or "")]
    kws = [k for k in parts if len(k) >= 3 and k not in ("to be", "the")]
    return tuple(dict.fromkeys(kws))[:4]


def load_cur_context(sf, member_id: int, lesson_no: int, *, n: int) -> dict:
    """DB 에서 차시·항목·회원 상태를 읽어 **이번 통화에 실릴 목록을 §11 규칙으로 예측**한다(읽기 전용).

    /cur/me 는 항목 목록을 싣지 않으므로(계약 §2) 하네스가 표면형·뜻을 알려면 여기서 읽어야 한다.
      ① 지금 차시의 안 배운 항목(cur_member_item.drilled_at NULL) seq 순 [:n]
      ② 모자라면 복습 후보 = 그 회원의 drilled 행 전부(어느 차시든, ①과 item_id 중복 제외) — 순서는 서버 랜덤이 섞이므로 «후보 집합» 으로만
    Returns: {lesson, items: {item_id: Item}(예측 새 항목 + 복습 후보 + 나머지 차시 항목(예측 밖, 오선별 감지용)),
              predicted_new: [item_id], review_pool: [item_id], lesson_item_ids: set, drilled_before: set}
    """
    from sqlalchemy import select
    from domains.learning.models.curriculum import CurItem, CurLesson, CurLessonItem, CurMemberItem

    with sf() as db:
        lesson = db.scalar(select(CurLesson).where(CurLesson.no == lesson_no, CurLesson.language == LANGUAGE))
        if lesson is None:
            sys.exit(f"⛔ cur_lesson no={lesson_no} 가 없다")
        rows = db.execute(
            select(CurLessonItem, CurItem).join(CurItem, CurItem.item_id == CurLessonItem.item_id)
            .where(CurLessonItem.lesson_id == lesson.lesson_id, CurItem.retired_at.is_(None))
            .order_by(CurLessonItem.seq)).all()
        mine = db.scalars(select(CurMemberItem).where(CurMemberItem.member_id == member_id)).all()
        drilled_rows = {(r.lesson_id, r.item_id): r for r in mine if r.drilled_at is not None}
        drilled_any = {iid for (_, iid) in drilled_rows}
        pool_items = {}
        if drilled_any:
            for it in db.scalars(select(CurItem).where(CurItem.item_id.in_(list(drilled_any)))).all():
                pool_items[it.item_id] = it

    def mk(it, *, lesson_id: int, role: str = "", seq: int = 0, review: bool = False) -> Item:
        try:
            m = json.loads(it.meanings) if it.meanings else {}
        except ValueError:
            m = {}
        en = (m.get(LOCALE) or m.get("en") or "") if isinstance(m, dict) else str(m)
        if isinstance(en, list):
            en = en[0] if en else ""
        try:
            exs = json.loads(it.examples) if it.examples else []
        except ValueError:
            exs = []
        ex = exs[0] if isinstance(exs, list) and exs else ""
        ex = ex if isinstance(ex, str) else str(ex.get("ko") or ex.get("text") or "") if isinstance(ex, dict) else ""
        item = Item(it.item_id, it.surface, str(en), "" if it.kind == "grammar" else casual_for(it.surface),
                    keywords_for(it.surface, str(en), it.kind),
                    kind=it.kind, example=ex, lesson_id=lesson_id, role=role, review=review, seq=seq)
        try:
            from domains.learning.service.quiz_judge import mentions as _mentions
            # 서버(cur 경로)는 표면형 매처 OR **예문 문장** 으로 문법을 알아본다(bt-back H3-③). 하네스는 문법 답으로 예문을 말하므로
            # 예문이 있으면 인식된다. 둘 다 없을 때만 «미인식».
            item.server_matchable = bool(_mentions(item.answer, item.surface)) or (item.kind == "grammar" and bool(item.example))
        except Exception:  # noqa: BLE001 - 판정 모듈이 없으면 «알아본다» 로 둔다
            item.server_matchable = True
        return item

    items: dict[int, Item] = {}
    lesson_ids: set[int] = set()
    predicted_new: list[int] = []
    for li, it in rows:
        lesson_ids.add(it.item_id)
        already = (lesson.lesson_id, it.item_id) in drilled_rows
        if not already and len(predicted_new) < n:
            predicted_new.append(it.item_id)
        items[it.item_id] = mk(it, lesson_id=lesson.lesson_id, role=li.role, seq=li.seq)
    review_pool: list[int] = []
    if len(predicted_new) < n:
        for iid, it in pool_items.items():
            if iid in predicted_new:
                continue
            # 그 회원의 가장 최근 차시 행 하나(P1-1) — 어느 행이든 판정 대조 키는 item_id 로 하니 lesson_id 는 참고값
            lid = max(l for (l, i) in drilled_rows if i == iid)
            if iid in items:
                items[iid].review = True
            else:
                items[iid] = mk(it, lesson_id=lid, review=True)
            review_pool.append(iid)
    return {
        "lesson": {"lesson_id": lesson.lesson_id, "no": lesson.no, "code": lesson.code, "level_no": lesson.level_no,
                   "situation": lesson.situation, "partner": lesson.partner, "probes": lesson.probes, "item_count": lesson.item_count},
        "items": items, "predicted_new": predicted_new, "review_pool": review_pool,
        "lesson_item_ids": lesson_ids, "drilled_before": drilled_any,
    }


def cur_status(api: CurApi) -> Optional[dict]:
    try:
        me = api.me()
    except CurApiError as exc:
        print(f"⚠ GET /cur/me 실패: {exc}")
        return None
    print("cur: " + summarize_me(me))
    return me


def cur_reset_db(sf, member_id: int, lesson_no: int) -> dict:
    """DB 직접 초기화(--env-root 의 DATABASE_URL_POOL): cur_member_item·cur_member_lesson·cur_call(그 회원 통화) 삭제 +
    cur_member_progress.lesson_id = (ko, no=lesson_no) — 없으면 INSERT. **이 회원 행만** 만진다."""
    from sqlalchemy import delete, select
    from domains.learning.models.call import Call
    from domains.learning.models.curriculum import CurCall, CurLesson, CurMemberItem, CurMemberLesson, CurMemberProgress

    with sf() as db:
        lesson = db.scalar(select(CurLesson).where(CurLesson.no == lesson_no, CurLesson.language == LANGUAGE))
        if lesson is None:
            sys.exit(f"⛔ cur_lesson no={lesson_no} 가 없다")
        my_calls = [c for c in db.scalars(select(Call.call_id).where(Call.member_id == member_id)).all()]
        n_call = db.execute(delete(CurCall).where(CurCall.call_id.in_(my_calls))).rowcount if my_calls else 0
        n_item = db.execute(delete(CurMemberItem).where(CurMemberItem.member_id == member_id)).rowcount
        n_les = db.execute(delete(CurMemberLesson).where(CurMemberLesson.member_id == member_id)).rowcount
        prog = db.scalar(select(CurMemberProgress).where(CurMemberProgress.member_id == member_id,
                                                         CurMemberProgress.language == LANGUAGE))
        if prog is None:
            db.add(CurMemberProgress(member_id=member_id, language=LANGUAGE, lesson_id=lesson.lesson_id))
            action = "INSERT"
        else:
            prog.lesson_id = lesson.lesson_id
            action = "UPDATE"
        db.commit()
    return {"deleted": {"cur_call": n_call, "cur_member_item": n_item, "cur_member_lesson": n_les},
            "progress": f"{action} lesson_id={lesson.lesson_id}(no={lesson_no})", "via": "db"}


def cur_reset(api: CurApi, member_id: int, lesson_no: int, quiet: bool = False, sf=None) -> bool:
    """--reset / --fix-items 공용: 차시 고정. API(/__dev/cur-reset) 먼저 — CurrentAdmin 게이트라 testfree 는 403 → DB 폴백.
    (계정을 admin 으로 올리지 않는다 — 비밀번호가 공개 저장소에 있는 계정.)"""
    try:
        res = api.reset(member_id=member_id, lesson_no=lesson_no)
    except CurApiError as exc:
        if exc.status in (401, 403, 404) and sf is not None:
            print(f"cur-reset: API {exc.status} → DB 직접 초기화로 폴백")
            try:
                res = cur_reset_db(sf, member_id, lesson_no)
            except SystemExit:
                raise
            except Exception as exc2:  # noqa: BLE001
                print(f"⛔ DB 초기화 실패: {exc2}")
                return False
        else:
            print(f"⛔ POST /__dev/cur-reset 실패: {exc}")
            return False
    if not quiet:
        print(f"cur-reset: member={member_id} lesson_no={lesson_no} → {res}")
    return True


def read_cur_outcome(sf, api: CurApi, call_id: Optional[int], ctx: dict, me_pre: Optional[dict]) -> Score:
    """cur 경로의 결과 — DB cur_member_item(회원×차시×항목) + API quiz_items + call 원가. Score.db_rows 는 item_id 키(판정표 호환)."""
    from sqlalchemy import select
    from sqlalchemy import text as sql
    from domains.learning.models.curriculum import CurMemberItem

    sc = Score()
    sc.cur = {"lesson": ctx["lesson"], "me_pre": me_pre, "me_post": None, "quiz_items": [], "new_ids": [], "review_ids": [],
              "review_estimated": False, "api_error": None, "cum_rows": {},
              # ③ 이번 통화 목록 크기는 통화 전 예측이 정본(결과 행은 «안 다룬 복습» 이 빠진다). --logs 면 서버 줄이 덮는다.
              "predicted_new": len(ctx["predicted_new"]),
              "predicted_review": min(max(CUR_ITEMS_PER_CALL - len(ctx["predicted_new"]), 0), len(ctx["review_pool"]))}
    with sf() as db:
        rows = db.scalars(select(CurMemberItem).where(CurMemberItem.member_id == MEMBER_ID)).all()
        for r in rows:
            prev = sc.cur["cum_rows"].get(r.item_id)
            # 누적 컬럼(회원×차시×항목) — 판정 열이 아니라 «단조(되돌아가지 않음)» 확인용. 같은 item 이 여러 차시 행이면 최신 갱신
            if prev is None or (r.updated_at or datetime.min) >= (prev.get("updated_at") or datetime.min):
                sc.cur["cum_rows"][r.item_id] = {"drilled_call_id": r.drilled_call_id, "drilled_at": r.drilled_at,
                                                "quiz_passed_at": r.quiz_passed_at, "quiz_failed_count": r.quiz_failed_count,
                                                "lesson_id": r.lesson_id, "updated_at": r.updated_at}
        if call_id:
            r = db.execute(sql("SELECT call_type, status, total_time, summary, usage_engine, usage_json, usage_in_audio, usage_in_text, "
                               "usage_out_audio, usage_out_text FROM call WHERE call_id=:c"), {"c": call_id}).first()
            if r is not None:
                sc.call_row = {"call_type": r[0], "status": r[1], "total_time": r[2], "summary": r[3], "usage_engine": r[4]}
                try:
                    from domains.learning.service import normalcall_service as _ns
                    uj = r[5] if isinstance(r[5], dict) else (json.loads(r[5]) if r[5] else None)
                    cost, _unk = _ns.estimate_call_cost_usd(r[4], in_audio=r[6] or 0, in_text=r[7] or 0, out_audio=r[8] or 0, out_text=r[9] or 0, usage_json=uj)
                    sc.call_row["cost_usd"] = round(cost, 4)
                    sc.call_row["usage"] = {"in_audio": r[6], "in_text": r[7], "out_audio": r[8], "out_text": r[9]}
                except Exception as exc:  # noqa: BLE001
                    sc.call_row["cost_usd"] = f"?({exc})"
    try:
        sc.cur["me_post"] = api.me()
    except CurApiError as exc:
        sc.cur["api_error"] = str(exc)
    if call_id:
        try:
            qi = api.quiz_items(call_id)
            sc.cur["quiz_items"] = qi
            sc.expr_result = [{"item_id": q.get("item_id"), "surface": q.get("surface"), "passed": q.get("passed"),
                               "failed": q.get("failed"), "review": q.get("review")} for q in qi]
            has_flag = any("review" in q for q in qi)
            for q in qi:
                iid = int(q.get("item_id") or 0)
                if has_flag:
                    is_review = bool(q.get("review"))
                else:
                    is_review = iid in ctx["drilled_before"]           # 통화 전 이미 drilled 였던 항목 = 복습(추정)
                (sc.cur["review_ids"] if is_review else sc.cur["new_ids"]).append(iid)
                # ② **이번 통화 판정의 정본 = cur_call.items 스냅샷(= quiz_items)**: 목록에 있으면 이번 통화에 다뤘다(drilled),
                #    passed/failed 도 이번 통화 것. 누적 컬럼(drilled_call_id 는 첫 통화 값)으로 보면 복습 항목이 전부 ✖ 로 보인다(1435).
                sc.db_rows[iid] = {"drilled_call_id": call_id, "drilled_at": True,
                                   "quiz_passed_at": True if q.get("passed") else None, "failed": bool(q.get("failed")),
                                   "review": is_review, "lesson_id": None}
            sc.cur["review_estimated"] = not has_flag
        except CurApiError as exc:
            sc.cur["api_error"] = (sc.cur["api_error"] or "") + f" | result: {exc}"
    return sc


def freetalk_report(sess: Session, sc: Score, ctx: dict, *, duration_min: int, run_no: int, server_logs: list[str] | None,
                    out_dir: Path, expect_locked: bool, picker: Picker) -> tuple[Path, bool]:
    """프리토킹 보고서 — 판정 없음. 잠금 / 상황·상대 문구 / 차시 표현 등장 / /cur/me 전이."""
    L: list[str] = []
    cid = sess.call_id
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    les = ctx["lesson"]
    L.append(f"# 프리토킹 E2E — call {cid} ({stamp}, run {run_no}) · 차시 no={les['no']} {les['code']}")
    L.append("")
    beav = [t for t in sess.turns if t.role == "beaver" and t.text.strip()]
    learner = [t for t in sess.turns if t.role == "learner"]
    lesson_items = {i: it for i, it in sess.items.items() if i in ctx["lesson_item_ids"]}
    b_hits = {i for t in beav for i in surfaces_in(t.text, lesson_items)}
    l_hits = {i for t in learner for i in surfaces_in(t.text, lesson_items)}
    ok = True
    if sess.locked:
        verdict = "잠금 확인(정상)" if expect_locked else "⛔ 잠김 — 프리토킹이 열려 있어야 했다"
        ok = expect_locked
        L.append(f"**결과: {verdict}** — ServerError {COURSE_LOCKED_CODE} 로 끊김 · 통화 전 status={((sc.cur.get('me_pre') or {}).get('status'))}")
    else:
        if expect_locked:
            ok = False
            L.append("**결과: ⛔ 잠겨 있어야 했는데 통화가 열렸다**")
        post = sc.cur.get("me_post") or {}
        pre = sc.cur.get("me_pre") or {}
        moved = (post.get("lesson") or {}).get("no")
        L.append(f"**결과: 통화 {sess.turns[-1].t if sess.turns else 0:.0f}초 · 종료 `{sess.end_reason}`** · /cur/me 차시 {((pre.get('lesson') or {}).get('no'))}→{moved} · status {pre.get('status')}→{post.get('status')}")
    L.append(f"- 통화 길이 요청 {duration_min}분 · 비버 턴 {len(beav)} · 학습자 턴 {len(learner)} · 첫 비버 발화 {beav[0].t if beav else float('nan'):.1f}s · 오류 {sess.errors or '없음'}")
    L.append(f"- DB call: {sc.call_row}")
    L.append("")
    L.append("## 1. 상황·상대 (첫 비버 턴)")
    L.append(f"- 차시 situation «{les['situation']}» · partner «{les.get('partner')}» · probes {les.get('probes')}")
    if beav:
        L.append(f"- 첫 턴: 「{beav[0].text[:300]}」")
        sit = None
        if picker.enabled and picker.client is not None:
            try:
                from pydantic import BaseModel
                from core.config import settings
                from core.gemini_analysis import generate_structured

                class SitCheck(BaseModel):
                    situation_set: bool
                    partner_set: bool
                    reason: str

                res = asyncio.run(generate_structured(
                    picker.client, settings.JUDGE_MODEL,
                    system_instruction="You check whether a Korean tutor's opening line sets up the given role-play situation and partner. Answer strictly.",
                    prompt=f"Situation (Korean): {les['situation']}\nPartner (Korean): {les.get('partner')}\n\nOpening line: {beav[0].text}",
                    schema=SitCheck, temperature=0.0, thinking_budget=0))
                if res is not None:
                    sit = res
                    L.append(f"- 상황 설정 {'✔' if res.situation_set else '✖'} · 상대 설정 {'✔' if res.partner_set else '✖'} (LLM: {res.reason[:120]})")
            except Exception as exc:  # noqa: BLE001
                L.append(f"- 상황 판정 LLM 실패: {exc}")
        if sit is None:
            L.append("- 상황·상대 판정: (LLM 꺼짐 — 전사로 확인)")
    L.append("")
    L.append("## 2. 차시 표현 등장")
    L.append(f"- 비버 턴에 나온 차시 표현 {len(b_hits)}/{len(lesson_items)}: " + ", ".join(lesson_items[i].surface for i in sorted(b_hits)) )
    L.append(f"- 학습자(하네스 대본) 발화의 차시 표현 {len(l_hits)}: " + ", ".join(lesson_items[i].surface for i in sorted(l_hits)))
    br = [t for t in sess.turns if "[대괄호]" in t.tags]
    L.append(f"- 대괄호 누출 {len(br)}건" + (" — " + ", ".join(f"턴{t.n}" for t in br) if br else " ✔"))
    L.append("")
    L.append("## 3. /cur/me 전이")
    L.append(f"- 전: {summarize_me(sc.cur.get('me_pre') or {}) if sc.cur.get('me_pre') else '(없음)'}")
    L.append(f"- 후: {summarize_me(sc.cur.get('me_post') or {}) if sc.cur.get('me_post') else '(없음)'}")
    if sc.cur.get("api_error"):
        L.append(f"- ⚠ API 오류: {sc.cur['api_error']}")
    L.append("")
    if server_logs is not None:
        L.append("## 7. 서버 로그 (gcloud)")
        L.append("```")
        L.extend(server_logs[:600])
        L.append("```")
        L.append("")
    L.append("## 전사")
    L.append("```")
    for t in sess.turns:
        who = "🦫" if t.role == "beaver" else "👤"
        stt_s = ("" if not t.stt else (f"  (들림: {t.stt})" if norm_ko(t.stt) == norm_ko(t.text) else f"  ⚠(들림: {t.stt})"))
        L.append(f"{t.t:6.1f}s {who} t{t.n}: {t.text}{stt_s}")
        if t.tags:
            L.append(f"          ↳ {' '.join(t.tags)}")
    L.append("```")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stamp}_call{cid or 'none'}_freetalk.md"
    path.write_text("\n".join(L), encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps({
        "call_id": cid, "course": "freetalk", "locked": sess.locked, "end_reason": sess.end_reason, "lesson": les,
        "me_pre": sc.cur.get("me_pre"), "me_post": sc.cur.get("me_post"), "beaver_hits": sorted(b_hits), "learner_hits": sorted(l_hits),
        "turns": [{"n": t.n, "role": t.role, "t": round(t.t, 1), "wall": round(t.wall, 3), "text": t.text, "kind": t.kind, "stt": t.stt, "tags": t.tags} for t in sess.turns],
        "server_logs": server_logs}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path, ok


def one_call(args, sf, api: CurApi, token: str, voice: Voice, picker: Picker, *, course: str, lesson_no: int,
             run_no: int, expect_locked: bool = False) -> tuple[Session, Score, Path, bool]:
    """cur 경로 통화 1회: 컨텍스트 예측 → /cur/me(전) → 통화 → 결과 읽기 → 보고서. (판정·큐 대조는 표현학습 그대로)"""
    ctx = load_cur_context(sf, MEMBER_ID, lesson_no, n=CUR_ITEMS_PER_CALL)
    me_pre = cur_status(api)
    if me_pre and (me_pre.get("lesson") or {}).get("no") not in (None, lesson_no):
        print(f"⚠ /cur/me 차시 no={(me_pre.get('lesson') or {}).get('no')} ≠ --lesson {lesson_no} — 서버 차시로 컨텍스트를 다시 읽는다")
        lesson_no = int((me_pre.get("lesson") or {}).get("no"))
        ctx = load_cur_context(sf, MEMBER_ID, lesson_no, n=CUR_ITEMS_PER_CALL)
    print(f"cur 예측: 차시 no={ctx['lesson']['no']} {ctx['lesson']['code']} · 새 항목 {len(ctx['predicted_new'])} · 복습 후보 {len(ctx['review_pool'])} · "
          f"차시 항목 {len(ctx['lesson_item_ids'])} · 회원 drilled {len(ctx['drilled_before'])}")
    started = datetime.now(timezone.utc)
    sess = asyncio.run(run_call(args.base, token, ctx["items"], voice, picker, duration_min=args.duration, probe=False,
                                verbose=args.verbose, course=course, lesson=ctx["lesson"],
                                distractors=[ctx["items"][i].surface for i in ctx["lesson_item_ids"] if i not in ctx["predicted_new"]][:8]))
    ended = datetime.now(timezone.utc)
    sc = read_cur_outcome(sf, api, sess.call_id, ctx, me_pre)
    if not sess.locked:
        for _ in range(10):
            saved = any(r.get("drilled_call_id") == sess.call_id for r in sc.db_rows.values()) \
                or sc.call_row.get("status") in ("done", "analyzing")
            if saved and sc.call_row.get("total_time"):
                break
            time.sleep(3)
            sc = read_cur_outcome(sf, api, sess.call_id, ctx, me_pre)
    logs = fetch_server_logs(started, ended, args.service) if args.logs else None
    if sess.course == "freetalk":
        path, ok = freetalk_report(sess, sc, ctx, duration_min=args.duration, run_no=run_no, server_logs=logs,
                                   out_dir=Path(args.out_dir), expect_locked=expect_locked, picker=picker)
    else:
        path, ok = score_and_report(sess, sc, ctx["items"], duration_min=args.duration, run_no=run_no,
                                    server_logs=logs, out_dir=Path(args.out_dir))
    print(f"\n보고서: {path}")
    return sess, sc, path, ok


def scenario_lesson_cycle(args, sf, api: CurApi, token: str, voice: Voice, picker: Picker) -> int:
    """reset(차시) → 표현 1통(18 새) → 표현 2통(12 새 + 복습 6) → 프리토킹 1통 → /cur/me 다음 차시. 각 단계 기대값 대조."""
    lesson_no = args.lesson
    rows: list[tuple[str, str, str, bool]] = []       # (단계, 기대, 실측, PASS)

    def add(step: str, exp: str, got: str, ok: bool) -> None:
        rows.append((step, exp, got, ok))
        print(f"  [{ '✔' if ok else '✖' }] {step}: 기대 {exp} / 실측 {got}")

    print(f"\n════════ 시나리오 lesson-cycle (차시 {lesson_no}) ════════")
    ok0 = cur_reset(api, MEMBER_ID, lesson_no, sf=sf)
    me0 = cur_status(api) or {}
    reset_ok = ok0 and (me0.get("lesson") or {}).get("no") == lesson_no and (me0.get("items_drilled") in (0, None))
    add("reset", f"차시 no={lesson_no} · drilled 0 · status learning", f"no={(me0.get('lesson') or {}).get('no')} · drilled {me0.get('items_drilled')} · {me0.get('status')}",
        reset_ok)
    if not reset_ok:
        # ⛔ 잘못된 차시로 통화 3건($1.3)을 태우지 않는다 — 여기서 멈춘다
        print("⛔ reset 이 기대와 다르다 → 시나리오 중단(통화 안 함)")
        _write_scenario_table(args, lesson_no, rows, [])
        return 1
    total = me0.get("items_total") or 0

    sess1, sc1, _, _ = one_call(args, sf, api, token, voice, picker, course="expression", lesson_no=lesson_no, run_no=1)
    n1 = sc1.cur.get("list_new"); r1 = sc1.cur.get("list_review")          # 목록 크기(서버 로그 > 예측) — 결과 행이 아니다
    post1 = sc1.cur.get("me_post") or {}
    add("표현학습 1통", f"새 {min(CUR_ITEMS_PER_CALL, total)} · 복습 0 · drilled {min(CUR_ITEMS_PER_CALL, total)}/{total}",
        f"새 {n1} · 복습 {r1} · drilled {post1.get('items_drilled')}/{post1.get('items_total')} · 하네스 드릴 {len(sess1.drilled_order)} · 판정 {'✔' if sc1.judge_ok else '✖'}",
        n1 == min(CUR_ITEMS_PER_CALL, total) and r1 == 0)

    sess2, sc2, _, _ = one_call(args, sf, api, token, voice, picker, course="expression", lesson_no=lesson_no, run_no=2)
    n2 = sc2.cur.get("list_new"); r2 = sc2.cur.get("list_review")
    post2 = sc2.cur.get("me_post") or {}
    remain = max(total - min(CUR_ITEMS_PER_CALL, total), 0)
    add("표현학습 2통", f"새 {remain} · 복습 {max(CUR_ITEMS_PER_CALL - remain, 0)} · status expression_done",
        f"새 {n2} · 복습 {r2} · drilled {post2.get('items_drilled')}/{post2.get('items_total')} · status {post2.get('status')} · 판정 {'✔' if sc2.judge_ok else '✖'}",
        n2 == remain and r2 == max(CUR_ITEMS_PER_CALL - remain, 0) and post2.get("status") == "expression_done")

    # ④ 프리토킹 **직전의 현재 차시** 를 /cur/me 로 잡아 둔다 — 프리토킹 뒤 포인터가 넘어가므로 상태는 이 차시로 본다(1436 은 차시 4 를 봐서 None)
    me_ft = cur_status(api) or {}
    ft_no = (me_ft.get("lesson") or {}).get("no") or lesson_no
    ft_level = (me_ft.get("lesson") or {}).get("level_no")
    sess3, sc3, _, ok3 = one_call(args, sf, api, token, voice, picker, course="freetalk", lesson_no=ft_no, run_no=3)
    post3 = sc3.cur.get("me_post") or {}
    add("프리토킹 1통", f"차시 {ft_no} 열림(잠금 아님) · 정상 종료", f"locked={sess3.locked} · 종료 {sess3.end_reason}", (not sess3.locked) and ok3)
    try:
        lessons = api.lessons(level=ft_level)
        st = next((l.get("status") for l in lessons if l.get("no") == ft_no), None)
    except Exception as exc:  # noqa: BLE001
        st = f"?({exc})"
    add(f"차시 {ft_no} 상태", "freetalk_done", str(st), st == "freetalk_done")
    add("/cur/me 다음 차시", f"no={ft_no + 1}", f"no={(post3.get('lesson') or {}).get('no')} · status {post3.get('status')}",
        (post3.get("lesson") or {}).get("no") == ft_no + 1)
    _write_scenario_table(args, lesson_no, rows, [sess1.call_id, sess2.call_id, sess3.call_id])
    return 0 if all(d for _, _, _, d in rows) else 1


def _write_scenario_table(args, lesson_no: int, rows: list, call_ids: list) -> None:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stamp}_scenario_lesson{lesson_no}.md"
    lines = [f"# 시나리오 lesson-cycle — 차시 {lesson_no} ({stamp})", "", "| 단계 | 기대 | 실측 | 판정 |", "|---|---|---|---|"]
    lines += [f"| {a} | {b} | {c} | {'PASS' if d else 'FAIL'} |" for a, b, c, d in rows]
    lines += ["", "통화: " + (" · ".join(str(c) for c in call_ids) or "(없음)")]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n시나리오 표: {path}")


# --------------------------------------------------------------------------- #
def main() -> None:
    global QUIZ_GROUP, CUR_ITEMS_PER_CALL, MEMBER_ID
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-root", default=os.environ.get("BEAVERTALK_ENV_ROOT"),
                    help=".env·gcp_key.json·tts_key.json 이 있는 루트(기본: 이 저장소 루트 또는 $BEAVERTALK_ENV_ROOT)")
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--email", default=DEFAULT_EMAIL)
    # ⛔ 비밀번호 리터럴을 여기 새로 넣지 마라 — E2E_PASSWORD env 로만 (DEFAULT_PASSWORD 정리는 별건)
    ap.add_argument("--password", default=os.environ.get("E2E_PASSWORD", DEFAULT_PASSWORD))
    ap.add_argument("--status", action="store_true", help="GET /cur/me")
    ap.add_argument("--fix-items", action="store_true", help="차시 고정 = POST /__dev/cur-reset {lesson_no} (옛 learning_item 고정 대체)")
    ap.add_argument("--reset", action="store_true", help="POST /__dev/cur-reset {lesson_no} — cur_member_* 삭제 + progress 를 --lesson 으로")
    ap.add_argument("--lesson", type=int, default=DEFAULT_LESSON_NO, help="cur_lesson.no (기본 4 = A1-T01-1, 30항목)")
    ap.add_argument("--course", choices=("expression", "freetalk", "auto"), default="expression",
                    help="start.call_type. auto 면 서버가 정한 코스(call_started.course)로 검증")
    ap.add_argument("--expect-locked", action="store_true", help="프리토킹이 COURSE_LOCKED 로 끊기는 것이 기대값(잠금 확인)")
    ap.add_argument("--scenario", choices=("lesson-cycle",), default=None,
                    help="lesson-cycle: reset → 표현 1통 → 표현 2통 → 프리토킹 → /cur/me 다음 차시 (PASS/FAIL 표)")
    ap.add_argument("--probe", action="store_true", help="①②③만: 첫 비버 턴 해석까지 보고 끊는다")
    ap.add_argument("--runs", type=int, default=0, help="[reset →] 통화 → 채점 을 N 회")
    ap.add_argument("--no-reset", action="store_true", help="--runs 앞의 자동 reset 생략(기본: 매 회 reset 안 함 — cur 는 차시가 이어진다; --reset-each 로 켠다)")
    ap.add_argument("--reset-each", action="store_true", help="--runs 매 회 앞에 cur-reset(옛 하네스 동작)")
    ap.add_argument("--duration", type=int, default=5, help="duration_min (서버가 3~15 로 클램프) · 도달 시 하네스가 소켓을 닫는다(client_cut)")
    ap.add_argument("--no-llm", action="store_true", help="항목 매칭 LLM 폴백 끄기")
    ap.add_argument("--logs", action="store_true", help="gcloud logging read 로 서버 로그 첨부")
    ap.add_argument("--service", default="beavertalk-app-harness-api")
    ap.add_argument("--out-dir", default=str(ROOT / "docs" / "e2e"))
    ap.add_argument("--tee", default=None, help="콘솔 출력을 이 UTF-8 파일에도 쓴다")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.tee:
        sys.stdout = Tee(Path(args.tee))

    bootstrap_env(args.env_root)
    from core.config import settings as _settings
    from domains.learning.repository import mastery_repository as mr
    QUIZ_GROUP = int(mr.EXPRESSION_QUIZ_GROUP)
    CUR_ITEMS_PER_CALL = int(getattr(_settings, "CUR_ITEMS_PER_CALL", CUR_ITEMS_PER_CALL))
    sf = db_session_factory()
    with sf() as db:
        MEMBER_ID = resolve_member(db, args.email)
        from domains.learning.service import call_service as _cs
        try:
            plan = _cs.effective_plan(db, MEMBER_ID)
            engine = _cs.live_engine_for(db, MEMBER_ID)
        except Exception as exc:  # noqa: BLE001 - 표시용
            plan, engine = f"?({exc})", "?"
    print(f"회원 {args.email} → member_id={MEMBER_ID} · 플랜={plan} · live_engine_for={engine} · 서버 {args.base}")

    # 토큰은 상태·초기화(REST)와 통화(WS)가 같이 쓴다(Supabase Bearer)
    token = get_token(args.base, args.email, args.password)
    api = CurApi(args.base, token)

    if args.status:
        cur_status(api)
    if args.fix_items or args.reset:
        if not cur_reset(api, MEMBER_ID, args.lesson, sf=sf):
            sys.exit(2)
        cur_status(api)
    if not (args.probe or args.runs or args.scenario):
        return

    voice = Voice()
    picker = Picker(enabled=not args.no_llm)
    if args.duration < 3:
        print("⚠ duration_min 은 서버가 3분으로 올린다(DEMO_DURATION_MIN_MINUTES=3)")

    if args.scenario == "lesson-cycle":
        sys.exit(scenario_lesson_cycle(args, sf, api, token, voice, picker))

    if args.probe:
        ctx = load_cur_context(sf, MEMBER_ID, args.lesson, n=CUR_ITEMS_PER_CALL)
        sess = asyncio.run(run_call(args.base, token, ctx["items"], voice, picker, duration_min=args.duration,
                                    probe=True, verbose=args.verbose, course=args.course, lesson=ctx["lesson"]))
        print("\n=== probe 결과 ===")
        for t in sess.turns:
            print(f"{t.role}: {t.text}\n   {t.tags}")
        return

    runs_ok = 0
    for run_no in range(1, args.runs + 1):
        print(f"\n════════ run {run_no}/{args.runs} · course={args.course} · lesson={args.lesson} ════════")
        if args.reset_each and not args.no_reset:
            cur_reset(api, MEMBER_ID, args.lesson, quiet=False, sf=sf)
        sess, sc, path, ok = one_call(args, sf, api, token, voice, picker, course=args.course, lesson_no=args.lesson,
                                      run_no=run_no, expect_locked=args.expect_locked)
        runs_ok += int(ok)
        if sess.course == "freetalk":
            print(f"결과: {'✔' if ok else '✖'} — {'잠금 확인' if sess.locked else '통화 ' + sess.end_reason} · "
                  f"/cur/me 후 {summarize_me(sc.cur.get('me_post') or {}) if sc.cur.get('me_post') else '(없음)'}")
        else:
            print(f"결과: {'✔ 일치' if ok else '✖ 불일치'} — 판정 {'✔' if sc.judge_ok else '✖'} 퀴즈주기 {'✔' if sc.period_ok else '✖'} "
                  f"거짓칭찬 {'✔' if sc.praise_ok else '✖'} · 드릴 {len(sess.drilled_order)} · 자발 {sess.spontaneous} · "
                  f"새 {len(sc.cur.get('new_ids') or [])} 복습 {len(sc.cur.get('review_ids') or [])}")
    print(f"\n총 {runs_ok}/{args.runs} 회 일치")
    sys.exit(0 if runs_ok == args.runs else 1)


if __name__ == "__main__":
    main()
