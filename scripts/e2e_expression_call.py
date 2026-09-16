# -*- coding: utf-8 -*-
"""[dev] 표현학습 E2E 하네스 — 학습자를 **대본**으로 대신해 ground truth 로 자동 채점한다.

## 왜
사장님이 5분 통화하고 우리가 전사를 읽어 «앵무새냐 자발이냐» 를 가리는 게 지금 방식이다.
학습자를 대본으로 대신하면 **어느 답이 자발이고 어느 답이 복창인지 하네스 자신이 안다** ⇒
DB 판정(quiz_passed_at · call.expression_result)을 기대값과 기계적으로 대조할 수 있다.
통화 1398 에서 무너진 축(퀴즈 주기 · 거짓 칭찬 · 자발 산출 0)은 전부 로그가 안 재던 축이다 —
이 하네스가 그 축을 매 통화 잰다. 계획: docs/20260911_1810_표현학습-E2E-하네스-계획.md

## 수동 하네스다 (pytest 아님 — smoke 규율)
**실서비스 DB** 에 붙는다(demo-api 는 app-api 와 같은 Supabase). 테스트 계정 행만 만진다 — testfree(92·Free·2.5) · testmax(88·Max·3.1) · testpro(91·Pro·2.5) (--email). ⛔ 다른 계정 금지.
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
    ## 이어하기 · 끊김 없는 전환 · 언어
    $E2E --segments 2 --runs 1 --email testmax@gmail.com             # 조각 이어하기(continues_call_id) → *_resume_call<id>.md · Free 는 거절 확인
    $E2E --segments 2 --seamless --segment-min 2 [--seamless-silent] [--wait-close]   # fragment_end→fragment_saved→silent_resume → *_seamless_call<id>.md
    $E2E --language ja --reset --runs 1 --email testpro@gmail.com     # 일본어(target_language 전환·끝나면 복구 · 차시 기본 ja=1)

    ## 판정 대조(4차~ LLM 판정)
    $E2E ... --judge llm                    # 기본: 서버 판정 결과(cur_call.items)·서버 퀴즈 창 로그를 정본으로 ①재드릴 ②번호 순·세트 ③표기 변형 ④공개 뒤 복창
    $E2E ... --judge string                 # 옛 문자열 판정기 서버용(하네스 자체 매칭 기대)
    $E2E ... --answer-style kana|roman|hangul    # 정답을 다른 표기로 말한다(ko 는 roman 만) — ⚠ STT 가 표기를 되돌리므로 표기 내성은 리플레이로 본다
    $E2E ... --offset-expect passed         # 서버 퀴즈 세트 밖 자발 정답도 passed 여야(5차 A-2 이후) · 기본 unjudged
    표기 내성 리플레이(통화 0): scripts/e2e_replay_transcript.py --language ja|ko --items 6 [--from-json steps.json]

    ⛔ 비밀번호는 E2E_PASSWORD env 로만 준다.

한글 콘솔이 깨지면 보고서 파일(docs/e2e/*.md)을 Read 로 본다.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
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
DEFAULT_PASSWORD = None            # ⛔ 리터럴 금지(공개 저장소) — E2E_PASSWORD env 로만. dev 테스트 계정(testfree=92 Free/2.5 · testmax=88 Max/3.1). 다른 계정 금지.
MEMBER_ID = 0                     # ⛔ 하드코딩 아님 — main() 이 --email 로 DB(member.email)에서 찾아 채운다. 다른 계정 금지(사장님 20).
LEVEL_NO = 1
LANGUAGE = "ko"                   # --language 로 바뀐다(ko|ja). 차시·판정·리셋·학습자 음성이 전부 이 값을 따른다
LOCALE = "en"
_LANG_CHARS = {"ko": r"[가-힣]", "ja": r"[ぁ-んァ-ヶー一-龯々]"}   # 대상 언어 문자(언어 판정·정규화용)
QUIZ_GROUP = 3                    # mastery_repository.EXPRESSION_QUIZ_GROUP (채점 기준 — 아래에서 실값으로 덮는다)
CUR_ITEMS_PER_CALL = 18           # 계획 §2 CUR_ITEMS_PER_CALL — 서버 settings 가 있으면 main 이 덮는다
DEFAULT_LESSON_NO = 4             # A1-T01-1 (30항목) — 시나리오 기본 차시

SR_IN = 16000                     # 클라→서버 PCM16 mono
FRAME_MS = 40
FRAME_BYTES = SR_IN * 2 * FRAME_MS // 1000   # 1280
PRE_SPEECH_S = 0.8                # turn_end 뒤 이만큼 쉬고 말한다
POST_SPEECH_SILENCE_S = 1.2       # 발화 뒤 무음(VAD 종료 감지)
LEARNER_VOICE = "Charon"          # 비버 음색과 다르게(Chirp3-HD 로스터)
ANSWER_STYLE = ""                 # --answer-style: "" | hangul | roman | kana — 정답을 다른 표기로 말한다(4차 ③ LLM 판정이 표기 달라도 통과시키나)
OFFSET_EXPECT = "unjudged"        # --offset-expect: 서버 퀴즈 세트 밖 자발 정답의 기대 — unjudged(4차: 서버 미판정이 정상) | passed(5차 A 배포 뒤: 서버가 판정·기록)
JUDGE_MODE = "llm"                # --judge: llm(서버 판정 결과·로그와 대조 — 기대 ①~④) | string(옛 문자열 판정기 — 하네스 자체 매칭 기대)

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
DISTRACTORS = ["좋아요", "맞아요", "배고파요", "물 주세요", "비싸요", "또 봐요", "어서 오세요", "안녕히 계세요"]
# ⚠ 6차 재검(1621·1622): «알겠어요»·«잠시만요» 는 판정기가 «멈춤»(pending)으로 볼 수 있다 — 오답 방해어로 쓰면 힌트 경로 기대가 흔들려 뺐다.
# ja 통화에 한국어 방해어를 말하면 ja STT 가 잡음으로 적는다(«물 주세요»→«お 譲り せよ») — ja 는 차시 밖 일본어 내용어를 쓴다.
DISTRACTORS_JA = ["いただきます", "おやすみなさい", "おいしいです", "たかいです", "ねこです"]
STALL_WORDS = {"알겠어요", "잠시만요", "네", "わかりました", "ちょっと待って", "ちょっとまって"}
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
    r"\b(quiz|pop quiz|review|recap|let'?s see if you (actually |really )?(remember|learned)|see if you (actually |really )?(remember|learned)|"
    r"what you('ve| have)? (actually |really )?learned|"
    r"see what you (actually |really )?(remember|know)|"
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

_PUNCT_RE = re.compile(r"[\s\.\,\!\?\~\'\"“”‘’\(\)\[\]·…:;\-。、「」『』！？・（）]")


def norm_ko(s: str) -> str:
    """대상 언어 대조용 정규화 — 공백·문장부호 제거 + NFC(ja 는 NFKC: 전각 영숫자·반각 가나 통일). 이름은 유산이다(ja 도 쓴다)."""
    return _PUNCT_RE.sub("", unicodedata.normalize("NFKC" if LANGUAGE == "ja" else "NFC", s or ""))


def has_surface(text: str, surface: str) -> bool:
    """표면형이 텍스트에 나왔나. ⚠ 두 음절 이하(「이」「제」「저」「명」)는 부분문자열이면 어느 문장에서든 걸린다(«생일이 언제예요?» 에 「이」) —
    그 경우 서버와 같은 낱말 경계 매처(quiz_judge.mentions: 어절 = 표면형 + 조사 꼬리)를 쓴다. 긴 표면형은 정규화 부분일치."""
    if not surface:
        return False
    short = len(norm_ko(surface)) <= (3 if LANGUAGE == "ja" else 2)
    if short and re.search(_LANG_CHARS.get(LANGUAGE, r"[가-힣]"), surface):
        try:
            from domains.learning.service.quiz_judge import mentions as _mentions
            return bool(_mentions(text, surface, LANGUAGE))    # ja: NFKC·조사 12·です/ます 표지(0f1cfe5)
        except Exception:  # noqa: BLE001 - 매처가 없으면 부분일치로
            pass
    if LANGUAGE == "ja" and ("～" in surface or "~" in surface):
        # 일본어 문형 라벨(～は～です)은 부분문자열이 안 된다 — 서버 템플릿 매처로
        try:
            from domains.learning.service.quiz_judge import mentions as _mentions
            return bool(_mentions(text, surface, LANGUAGE))
        except Exception:  # noqa: BLE001
            return False
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


# ── 학습자 답 표기 변형(--answer-style) ─────────────────────────────── #
_RR_ON = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_RR_V = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
_RR_CO = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t"]


def hangul_romanize(text: str) -> str:
    """한글 → 로마자(개정 로마자 음절 단위 · 연음·동화 없음 = 학습자가 적는 식). 한글 아닌 글자는 그대로."""
    out = []
    for ch in text or "":
        c = ord(ch) - 0xAC00
        out.append(_RR_ON[c // 588] + _RR_V[(c % 588) // 28] + _RR_CO[c % 28] if 0 <= c < 11172 else ch)
    return "".join(out)


_KANA_HANGUL = dict(zip(
    "あいうえおかきくけこがぎぐげごさしすせそざじずぜぞたちつてとだぢづでどなにぬねのはひふへほばびぶべぼぱぴぷぺぽまみむめもやゆよらりるれろわをゔぁぃぅぇぉ",
    "아이우에오카키쿠케코가기구게고사시스세소자지즈제조타치츠테토다지즈데도나니누네노하히후헤호바비부베보파피푸페포마미무메모야유요라리루레로와오부아이우에오"))
_KANA_YOON = {"きゃ": "캬", "きゅ": "큐", "きょ": "쿄", "ぎゃ": "갸", "ぎゅ": "규", "ぎょ": "교", "しゃ": "샤", "しゅ": "슈", "しょ": "쇼",
              "じゃ": "자", "じゅ": "주", "じょ": "조", "ちゃ": "차", "ちゅ": "추", "ちょ": "초", "にゃ": "냐", "にゅ": "뉴", "にょ": "뇨",
              "ひゃ": "햐", "ひゅ": "휴", "ひょ": "효", "びゃ": "뱌", "びゅ": "뷰", "びょ": "뵤", "ぴゃ": "퍄", "ぴゅ": "퓨", "ぴょ": "표",
              "みゃ": "먀", "みゅ": "뮤", "みょ": "묘", "りゃ": "랴", "りゅ": "류", "りょ": "료"}


def kana_to_hangul(kana: str) -> str:
    """가나 → 한글 음차(한국인 학습자가 적는 식 · 근사). ん=받침 ㄴ · っ=받침 ㅅ · ー 생략. 가나 아닌 글자는 그대로."""
    try:
        import jaconv
        kana = jaconv.kata2hira(kana or "")
    except Exception:  # noqa: BLE001
        kana = kana or ""
    out: list[str] = []

    def _coda(idx: int, fallback: str) -> None:
        if out and len(out[-1]) == 1 and 0 <= ord(out[-1]) - 0xAC00 < 11172 and (ord(out[-1]) - 0xAC00) % 28 == 0:
            out[-1] = chr(ord(out[-1]) + idx)
        else:
            out.append(fallback)

    i = 0
    while i < len(kana):
        two = kana[i:i + 2]
        if two in _KANA_YOON:
            out.append(_KANA_YOON[two]); i += 2; continue
        ch = kana[i]
        if ch == "ん":
            _coda(4, "은")
        elif ch == "っ":
            _coda(19, "")
        elif ch == "ー":
            pass
        else:
            out.append(_KANA_HANGUL.get(ch, ch))
        i += 1
    return "".join(out)


def _kakasi_tokens(text: str) -> list[tuple[str, str, str]]:
    import pykakasi
    return [(t["orig"], t["hira"], t["hepburn"]) for t in pykakasi.kakasi().convert(text or "")]


def styled_answer(text: str, style: str, language: Optional[str] = None) -> Optional[tuple[str, str]]:
    """정답 문자열 → (말할 표기, TTS 언어) · 바꿀 게 없으면 None.
    ko: roman = 로마자를 영어 음성으로(«saramyo») · hangul = 기본 표기(None).
    ja: kana = 한자를 가나로(ja 음성) · roman = 헵번 로마자를 영어 음성으로(조사 は→wa·へ→e) · hangul = 한글 음차를 한국어 음성으로.
    ⚠ 서버 전사(STT)가 어떤 글자로 적을지는 우리가 못 정한다 — 발음·음성 언어로 변형을 유도하고, 실제 전사는 보고서에 그대로 남긴다."""
    lang = language or LANGUAGE
    if not style or not text:
        return None
    if lang == "ko":
        if style == "roman":
            r = hangul_romanize(text)
            return (r, "en") if r != text else None
        return None
    toks = _kakasi_tokens(text)
    if style == "kana":
        k = "".join(h for _, h, _ in toks)
        return (k, "ja") if k != text else None
    if style == "roman":
        words = [("wa" if o == "は" else "e" if o == "へ" else hep) for o, _, hep in toks]
        r = " ".join(w for w in words if w.strip() and not re.fullmatch(r"[。、．，.,!?！？\s]+", w))
        return (r, "en") if r else None
    if style == "hangul":
        g = "".join(("와" if o == "は" else "에" if o == "へ" else kana_to_hangul(h)) for o, h, _ in toks)
        return (g, "ko") if g != text else None
    return None


def _tts_cache_name(text: str, lang: str) -> str:
    """TTS 캐시 파일 이름 — ⚠ 옛 이름은 [0-9A-Za-z가-힣] 밖 글자를 전부 _ 로 바꿔 **가나·한자 답이 같은 파일로 겹쳤다**(길이만 같으면 다른 문장 음성이 재생). 해시를 붙인다."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "_", f"{lang}_{text}")[:60] + "_" + hashlib.md5(f"{lang}\n{text}".encode("utf-8")).hexdigest()[:12] + ".pcm"


def looks_cut_off(text: str) -> bool:
    """비버 말이 끊긴 조각/빈 턴인가 — 빈 턴, 또는 4어절 이하 · 문장부호로 안 끝남 · 인용 없음(«Hahaha! That's» · «Hahaha! You» — 1621 2.5 에서 4회).
    ⚠ 이런 턴에 «Okay.» 로 답하면 학습자 턴이 늘어 서버 퀴즈 창이 6턴 상한으로 강제 닫힌다(1621 seq2·seq4). 사람은 말 끊김엔 기다린다."""
    t = (text or "").strip()
    if not t:
        return True
    if quoted_segments(t):
        return False
    return len(t.split()) <= 4 and t[-1] not in ".!?。！？…\"”」'"


def _norm_repeat(text: str) -> str:
    """동일 문장 판정용 정규화 — 공백·문장부호·대소문자 무시(«Say it!» 과 «Say it.» 은 같은 말)."""
    return re.sub(r"[\s\W_]+", "", (text or "").lower())


def beaver_turn_stats(turns: list) -> dict:
    """조각(통화) 안 비버 턴 통계 — n · 평균 글자수 · 최대 글자수 · **동일 문장 연속 반복**(정규화 텍스트가 같은 비버 턴이 잇달아 나온 최장 런,
    사이의 학습자 턴은 무시 · 1602 t73~t87 8회) · 반복 구간 수(런 ≥2). 하네스가 재생한 턴(태그 «재생»)·빈 턴은 뺀다. 글자수 = 공백 제외."""
    bt = [t for t in turns if t.role == "beaver" and (t.text or "").strip() and not any("재생" in x for x in t.tags)]
    lens = [len(re.sub(r"\s+", "", t.text)) for t in bt]
    best = {"count": 0, "text": "", "span": ""}
    runs = 0
    run_start = bt[0] if bt else None
    run = 1
    for prev, cur in zip(bt, bt[1:]):
        if _norm_repeat(cur.text) == _norm_repeat(prev.text):
            run += 1
            if run == 2:
                runs += 1
            if run > best["count"]:
                best = {"count": run, "text": cur.text, "span": f"t{run_start.n}~t{cur.n}"}
        else:
            run = 1
            run_start = cur
    return {"n": len(bt), "avg_chars": (sum(lens) / len(lens)) if lens else 0.0, "max_chars": max(lens) if lens else 0,
            "repeat_max": best["count"], "repeat_text": best["text"], "repeat_span": best["span"], "repeat_runs": runs}


def beaver_stats_row(label: str, st: dict) -> str:
    rep_ = (f"{st['repeat_max']}회 {st['repeat_span']} «{st['repeat_text'][:60]}»" if st["repeat_max"] >= 2 else "0")
    return f"| {label} | {st['n']} | {st['avg_chars']:.0f} | {st['max_chars']} | {rep_} | {st['repeat_runs']} |"


BEAVER_STATS_HEAD = ["| 조각 | 비버 턴 | 평균 글자수 | 최대 글자수 | 동일 문장 연속 반복(최대) | 반복 구간 수 |", "|---|---|---|---|---|---|"]


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
    examples: tuple[str, ...] = ()    # cur_item.examples 전부(프리토킹 답 은행 · 문법 예문 회전)

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
        if LANGUAGE == "ja":
            # 일본어: 한두 글자 어휘는 「Xです」(매처가 です 표지를 받는다 — 실측 「私です」·「名前です」 ✔) · 동사 원형(う단)은 두 번
            if self.kind != "chunk" and len(core) <= 2:
                if re.search(r"[うくぐすつぬぶむる]$", self.surface):
                    return f"{self.surface}、{self.surface}"
                return f"{self.surface}です"
            return self.surface
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
            if LANGUAGE != "ja":
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
    block: int = 0                   # 앵커 회차(1부터) — ② 블록 안 출제 순서 검사용. 0 = 앵커 없는 재출제
    answer_turns: list[int] = field(default_factory=list)   # 이 회차에 답한 학습자 턴 번호(재발화 포함)
    heard: bool = False              # ④ 그 답 턴 중 하나라도 input_transcript 가 왔나 — 안 왔으면 서버엔 «무음 턴»(학습자 턴 아님)


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
    drill_done_at: Optional[float] = None    # 드릴에서 처음 정답·복창을 낸 시각 — 이 뒤의 재질문만 «재드릴»(1624 #2: 드릴 중 재질문을 세던 결함)
    surface_uttered: bool = False     # 표면형이 비버 공개나 학습자 발화로 실제 한 번 나왔나 (= drilled 기대의 조건)
    surface_heard: bool = False       # 서버가 «들은» 쪽 — 비버 공개, 또는 학습자 턴의 input_transcript 에 표면형이 있었다
                                      #   (TTS 「이거 주세요」→STT 「이거 지세요」 처럼 보낸 것과 들린 것이 다르면 서버는 못 본다)
    rounds: list[QuizRound] = field(default_factory=list)
    beaver_said_correct_after_wrong: list[int] = field(default_factory=list)   # 거짓 칭찬 턴 번호
    superseded_by: int = 0            # 오식별로 판명돼 다른 항목으로 대체됨(판정표·주기 계산에서 제외)

    @property
    def expected_passed(self) -> bool:
        # 판정 창 = 퀴즈 여는 비버 턴(앵커) **포함**(P4 안 함, bt-back 2026-09-15) — 그 턴에서 비버가 공개하면 회차 revealed → 복창 → 기대 failed.
        # ④ 서버가 들은(전사 온) 자발 정답만
        return any(r.spontaneous_correct and r.heard for r in self.rounds)

    @property
    def unjudgeable_correct(self) -> str:
        """자발 정답을 냈지만 서버가 못 받는 이유(표시용): «무음 턴(전사 없음)» · 없으면 ""."""
        rs = [r for r in self.rounds if r.spontaneous_correct]
        if not rs or self.expected_passed:
            return ""
        return "무음 턴(전사 없음)"

    @property
    def expectation_ambiguous(self) -> bool:
        """자발 정답이 **앵커 없는** 회차에서만 났다 — 판정기 규칙(결정 6-3)상 «그 항목만 보류» 가 허용된다."""
        anchored_pass = any(r.spontaneous_correct and r.anchored and r.heard for r in self.rounds)
        unanchored_pass = any(r.spontaneous_correct and not r.anchored and r.heard for r in self.rounds)
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
        fn = self.cache_dir / _tts_cache_name(text, lang)
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
def picker_listing(candidates: list["Item"], max_examples: int = 3) -> str:
    """LLM 픽커 후보 목록 — 표면형 · 뜻 · **예문**(최대 3). 비버는 [문형] 항목을 라벨 뜻이 아니라 예문 문장을 영어로 옮겨 묻는다
    («When is your birthday?» ← 생일이 언제예요? · «I am a company employee» ← 저는 회사원입니다 — 1617 탈선). 예문엔 영어 번역이 없어
    (cur_item.examples) 시드로 못 풀므로 LLM 에게 대상 언어 예문을 그대로 보여 준다."""
    lines = []
    for i, c in enumerate(candidates):
        exs = [e for e in (list(c.examples) or ([c.example] if c.example else [])) if e][:max_examples]
        lines.append(f"{i + 1}. {c.surface} — {c.en}" + (" — examples: " + " / ".join(exs) if exs else ""))
    return "\n".join(lines)


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

        listing = picker_listing(candidates)
        tl = "Japanese" if LANGUAGE == "ja" else "Korean"
        sysi = (f"You classify which {tl} expression a tutor's utterance is asking the learner to produce. "
                f"The tutor speaks English and describes a situation or gives the English meaning, without saying "
                f"the {tl}. The tutor may also ask for a whole sentence in English (e.g. \"When is your birthday?\") that is the "
                f"translation of one of an expression's example sentences, or that uses its grammar pattern — pick that expression. "
                "Answer with the number of the matching expression from the list, or 0 if the utterance "
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
        self.anchor_pending: list[list[int]] = []         # ① 앵커마다 그 순간 «드릴했지만 아직 퀴즈에 안 오른» 항목 id(드릴 순)
        self.quiz_block_asked: set[int] = set()            # 이 퀴즈 블록에서 이미 낸 항목
        self.template_sentence: dict[int, str] = {}        # [문형] 항목 → 비버가 공개한 «자기 연습 문장»(1592: 예문 대신 이걸 말해야 받아 준다)
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
        self.continues_call_id: Optional[int] = None   # ① 조각 이어하기 — start.continues_call_id 로 보낸 앞 조각 번호
        self.resumed: Optional[bool] = None            # call_started.call_id == continues_call_id 면 True(이어짐), 다르면 False(새 통화로 폴백=거절)
        self.passed_before: set[int] = set()           # ② 통화 전 이미 quiz_passed 인 항목
        self.silence_until: float = -1.0          # 프리토킹 의도적 침묵 중이면 그 끝 시각(워치독이 쉰다)
        self.ft_silence_done = False
        self.ft_silence: dict = {}                # {"start": t, "nudge_at": t|None, "nudge_text": str}
        self.character_id: Optional[int] = None
        self.hints: list[dict] = []               # (e) hint 프레임 — 예시 수·reading(가나) 있는 예시 수
        self.empty_streak = 0                     # 연속 빈 비버 턴 수(3.1 빈 턴 루프 방어)
        # ⭐ H8 끊김 없는 조각 전환(--seamless, 계획 docs/plans/2026-09-13-끊김없는-조각-전환.md §2)
        self.seamless = False                     # 조각1: --segment-min 뒤 «학습자 발화 → 비버 turn_end» 에서 fragment_end
        self.switch_at: float = -1.0              # 전환 대기 시작 시각(now 기준). <0 = 아직
        self.switching = False                    # fragment_end 를 보냈다(그 뒤엔 말하지 않는다)
        self.fragment_end_sent_at: Optional[float] = None
        self.fragment_saved_at: Optional[float] = None
        self.fragment_saved: Optional[dict] = None
        self.call_ended_before_saved = False      # fragment_end 뒤 fragment_saved 전에 call_ended 가 왔다(있으면 안 된다)
        self.ws_closed_at: Optional[float] = None
        self.ws_close_code: Optional[int] = None
        self.silent_resume = False                # 조각2: start.silent_resume=true — 비버는 학습자 첫 발화를 기다려야 한다
        self.silent_never = False                 # (g) 변형: 학습자가 끝내 말하지 않는다 → 무음 3단(넛지→확인→종료) 관찰
        self.hold_reply = False                   # 비버 턴이 와도 말하지 않는다(전환 뒤·무음 관찰 중)
        self.started_at: Optional[float] = None   # call_started 시각
        self.first_speech_at: Optional[float] = None    # 조각2 에서 학습자가 처음 말한 시각
        self.watch_done = False                   # 관찰 창이 끝났다((g) 는 학습자가 안 말하므로 이걸로 (c) 계수를 멈춘다)
        self.pre_speech: dict = {"audio_bytes": 0, "transcripts": [], "turn_starts": 0}   # (c) 첫 발화 전 비버 출력
        self.beaver_turn_times: list[tuple[float, str]] = []   # (g) call_started 기준 비버 turn_end 시각·텍스트
        self.send_ctrl = None                     # run_call 이 준다: async (dict) → ws.send(json)
        self.reconnect_on_saved = False           # fragment_saved 즉시 재연결(앱 동작) / False = 서버 close 까지 기다려 close 지연을 잰다
        self.resume_prompt: str = ""              # 조각1 마지막 비버 턴(조각2 첫 발화 = 그 턴에 대한 답)
        self.fragment_index: Optional[int] = None
        self.max_fragments: Optional[int] = None

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
        pool = self.distractor_pool or (DISTRACTORS_JA if LANGUAGE == "ja" else DISTRACTORS)
        d = pool[self.distractor_i % len(pool)]
        self.distractor_i += 1
        return d

    def policy_of(self, k: int) -> int:
        return (k - 1) % 6 + 1

    def _link_answer_turn(self, turn: "Turn") -> None:
        """이 학습자 턴이 퀴즈 회차의 답이면 회차에 턴 번호를 단다(④ 전사 도착으로 heard 를 켜기 위해). 드릴 답은 달지 않는다."""
        rec = self.current
        if rec is None or not rec.rounds:
            return
        rd = rec.rounds[-1]
        if self.mode == "quiz" or (not rd.anchored and rec is not self._last_started):
            rd.answer_turns.append(turn.n)

    def _mark_heard(self, turn: "Turn") -> None:
        rec = self.records.get(turn.item_id)
        if rec is None:
            return
        for rd in rec.rounds:
            if turn.n in rd.answer_turns:
                rd.heard = True

    def unquizzed_ids(self) -> list[int]:
        """드릴은 끝났고 아직 어느 퀴즈 회차에도 안 오른 항목(드릴 순) — 서버 «미출제» 와 같은 뜻(3차 ①)."""
        return [i for i in self.drilled_order if i in self.records and not self.records[i].rounds and not self.records[i].superseded_by]

    # ---- 프레임 처리 --------------------------------------------------------- #
    async def on_json(self, msg: dict, uplink: Uplink) -> None:
        t = msg.get("type")
        if t == "call_started":
            self.call_id = int(msg["call_id"]) if msg.get("call_id") else None
            # ⭐ auto 코스: 서버가 정한 코스를 알려준다(계획 §8). 명시 코스여도 서버 값이 오면 기록만 한다.
            self.course_from_server = msg.get("course")
            self.character_id = msg.get("character_id")
            if self.continues_call_id is not None:
                self.resumed = (self.call_id == self.continues_call_id)
                self.log(f"이어하기: continues_call_id={self.continues_call_id} → {'같은 call 로 이어짐 ✔' if self.resumed else '⛔ 새 통화로 폴백(거절)'}")
            if self.course == "auto" and self.course_from_server in ("expression", "freetalk"):
                self.course = self.course_from_server
            self.fragment_index = msg.get("fragment_index")
            self.max_fragments = msg.get("max_fragments")
            self.started_at = self.now()
            self.log(f"call_started call_id={self.call_id} character={msg.get('character_id')} course={self.course_from_server or '(없음)'} → 검증 코스 {self.course}"
                     + (f" · fragment_index={self.fragment_index} max_fragments={self.max_fragments}" if self.fragment_index is not None else ""))
            if self.silent_resume:
                # 조각2(silent): 앱처럼 마이크를 열어 두고(무음 프레임) 비버가 먼저 말하는지 본다 — 학습자는 관찰 창이 끝난 뒤에만 말한다
                self.hold_reply = True
                self.silence_until = 1e9
                uplink.open = True
        elif t == "fragment_saved":
            # ⭐ H8 S6: 이 조각의 저장이 끝났다 — 서버는 이 프레임 뒤 소켓을 닫는다(call_ended 는 오지 않아야 한다)
            self.fragment_saved_at = self.now()
            self.fragment_saved = dict(msg)
            self.end_reason = "fragment_saved"
            dt = (self.fragment_saved_at - self.fragment_end_sent_at) * 1000 if self.fragment_end_sent_at is not None else float("nan")
            self.log(f"fragment_saved call_id={msg.get('call_id')} fragment_index={msg.get('fragment_index')} · fragment_end 뒤 {dt:.0f}ms")
            if self.reconnect_on_saved:
                # 앱과 같게: fragment_saved 를 받으면 **서버의 close 를 기다리지 않고** 바로 재연결한다(이 소켓은 우리가 닫는다).
                #   서버 close 지연 실측은 reconnect_on_saved=False 로(1596·1597·1598: 2ms~10s).
                self.ended = True
                self.log("→ 즉시 재연결(서버 close 를 기다리지 않음)")
        elif t == "turn_start":
            uplink.open = False
            uplink.cut()
            if self.silent_resume and self.first_speech_at is None and not self.watch_done:
                self.pre_speech["turn_starts"] += 1
            if self.pending_speak and not self.pending_speak.done():
                self.pending_speak.cancel()
            self.cur_turn_id = msg.get("turn_id")
            self.cur_text = []
        elif t == "output_transcript":
            self.cur_text.append(msg.get("text") or "")
            if self.silent_resume and self.first_speech_at is None and not self.watch_done and (msg.get("text") or "").strip():
                self.pre_speech["transcripts"].append(msg.get("text") or "")
        elif t == "input_transcript":
            stt = (msg.get("text") or "").strip()
            if stt:
                for tn in reversed(self.turns[-6:]):
                    if tn.role == "learner" and not tn.stt:
                        tn.stt = stt
                        self._mark_heard(tn)
                        rec = self.records.get(tn.item_id)
                        if rec is not None and has_surface(stt, rec.item.surface):
                            rec.surface_heard = True
                        break
                else:
                    if self.last_learner is not None:
                        self.last_learner.stt = (self.last_learner.stt + " " + stt).strip()
                        self._mark_heard(self.last_learner)
        elif t == "turn_end":
            text = "".join(self.cur_text).strip()
            self.cur_turn_id = None
            self.cur_text = []
            if not text:
                # ⛔ 빈 비버 턴(전사 0) — 3.1 ja 1586 에서 65초부터 4분간 1.3초마다 빈 턴이 왔고 하네스의 «Okay.» 가 그 루프를 먹였다.
                #   연속 2번까지만 받아 주고 그 뒤엔 침묵(워치독 기준 시각도 안 갱신 → 서버 무음 넛지가 진짜 턴을 낼 때까지 조용히).
                self.empty_streak += 1
                if self.empty_streak > 2:
                    self.add_turn("beaver", "").tags.append("빈 턴(무응답)")
                    uplink.open = True
                    return
            else:
                self.empty_streak = 0
            self.last_beaver_end = self.now()
            self.last_beaver_text = text
            if self.started_at is not None:
                self.beaver_turn_times.append((round(self.now() - self.started_at, 1), text))
            if self.seamless and self.switch_at >= 0 and not self.switching and self.last_learner is not None \
                    and self.last_learner.t >= self.switch_at and self.send_ctrl is not None:
                # ⭐ H8 §2: 전환 대기 중 «학습자 발화 → 비버 응답 turn_end» — 이 턴은 기록만 하고(답하지 않는다) fragment_end 를 보낸다.
                #   소켓은 열어 둔다: 서버가 저장(마지막 판정 → record_expression → usage → 전사) 뒤 fragment_saved 를 보내고 닫는다.
                self.hold_reply = True
                self.switching = True
                self.silence_until = 1e9
                await self.on_beaver_turn(text, uplink)
                if self.turns and self.turns[-1].role == "beaver":
                    self.turns[-1].tags.append("조각 경계(이 턴 뒤 fragment_end)")
                self.fragment_end_sent_at = self.now()
                uplink.open = False        # 마이크 프레임 중단 — 서버 읽기 펌프가 fragment_end 뒤 멈추므로 계속 보내면 close 핸드셰이크가 막힌다(1596·1598·1599: 10s)
                await self.send_ctrl({"type": "fragment_end"})
                self.log(f"→ fragment_end 전송({self.now():.1f}s · 전환 대기 {self.now() - self.switch_at:.1f}s 뒤) · fragment_saved 대기(상한 5s)")
                return
            await self.on_beaver_turn(text, uplink)
        elif t == "call_ended":
            self.ended = True
            self.end_reason = msg.get("reason", "")
            if self.switching and self.fragment_saved_at is None:
                self.call_ended_before_saved = True
                self.log("⛔ fragment_end 뒤 fragment_saved 전에 call_ended 가 왔다")
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
        elif t == "hint":
            exs = msg.get("examples") or []
            self.hints.append({"turn_id": msg.get("turn_id"), "n": len(exs), "reading": sum(1 for e in exs if e.get("reading"))})
        elif t in ("pong", "sentence", "teaching_plan"):
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
            pend = self.unquizzed_ids()
            self.anchor_pending.append(pend)
            ok = len(pend) >= QUIZ_GROUP          # ① 3차 규칙: 미출제 3개가 모이면(드릴 총량 3·6·9 아님)
            tags.append(f"앵커@드릴{len(self.drilled_order)}·미출제{len(pend)}{'✔' if ok else '✖'}")
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
        if exclude and self.current.item.kind == "grammar" and self.matches_template(text, self.current.item):
            # 문형 답(예문 「생일이 언제예요?」)을 비버가 «자기 연습 문장»(「이 옷은 얼마예요?」)으로 고쳤다 — 같은 문형이다(1592).
            # 오식별이 아니라 비버가 정한 문장을 요구하는 것 → 현 항목 유지(제외하지 않는다)
            exclude = set()
        item_id, how = await self.identify(text, seg, revealed_ids, is_question, new_item_cue, exclude=exclude)
        if item_id and how == "template":
            # 템플릿으로 잡혔다 = 비버가 문형 문장을 **말했다**(공개). 표면형 대조엔 안 잡히므로 여기서 공개로 표시하고 그 문장을 기억한다
            sent = next((q for q in quoted_segments(text) if re.search(_LANG_CHARS.get(LANGUAGE, r"[가-힣]"), q)), "")
            if sent:
                self.template_sentence[item_id] = sent
            if item_id not in revealed_ids:
                revealed_ids = revealed_ids + [item_id]
        if exclude and item_id and item_id != self.current.item.item_id and item_id not in self.records:
            # 비버가 우리 «정답» 을 고치며 다른 뜻(예문·설명)을 댔고 그게 다른 미드릴 항목이다 → 처음 짚은 항목이 오식별이었다(1441 이름→명)
            old = self.current
            old.superseded_by = item_id
            if old.item.item_id in self.drilled_order:
                self.drilled_order.remove(old.item.item_id)
            tags.append(f"오식별 정정: {old.item.surface}→{self.items[item_id].surface}")
            self._start_item(item_id, turn, how + "(정정)", pre_reveal=item_id in mentioned)
            item_id_started = True
        elif (how == "template" and item_id and item_id not in self.records and self.current is not None and self.mode == "drill"
              and self.current.item.kind == "vocab" and len(norm_ko(self.current.item.surface)) <= 2
              and self.current.item.item_id in mentioned and "correct" not in self.current.drill_answers
              and not self.current.rounds):
            # 비버가 문형 연습 문장(「이 옷은 얼마예요?」)을 공개했고, 우리가 직전 턴에 짚은 항목은 그 문장 속 한 음절 어휘(「이」)다 —
            # 어휘가 아니라 문형을 드릴하는 중이었다(1592: LLM/키워드가 "this" 를 「이」 로 짚음). 앞 항목은 오식별로 대체.
            old = self.current
            old.superseded_by = item_id
            if old.item.item_id in self.drilled_order:
                self.drilled_order.remove(old.item.item_id)
            tags.append(f"오식별 정정: {old.item.surface}→{self.items[item_id].surface}")
            self._start_item(item_id, turn, how + "(정정)", pre_reveal=True)
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
                    rec.rounds.append(QuizRound(n=len(rec.rounds) + 1, asked_at=self.now(), block=len(self.anchors)))
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
        if self.hold_reply:
            tags.append("보류(전환/무음 관찰)")
            uplink.open = True
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
        if self.silence_until > 0 and self.ft_silence.get("nudge_at") is None:
            self.ft_silence["nudge_at"] = round(self.now(), 1)
            self.ft_silence["nudge_text"] = text
            tags.append(f"넛지(침묵 {self.now() - self.ft_silence['start']:.0f}s 뒤)")
            self.silence_until = -1.0
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
        if self.hold_reply:
            tags.append("보류(전환/무음 관찰)")
            uplink.open = True
            return
        reply, lang, kind = self.freetalk_reply(text)
        self.pending_speak = asyncio.create_task(self._speak_later(reply, lang, kind, uplink))

    # 프리토킹 v1 대본(계획 §9 하네스 v1): 비버 과제에 **한국어로** 답한다 — 차시 항목·문법 예문에서 고른다.
    #   학습자 턴 3·8번째는 «I don't know»(모국어 턴 유도) · 5번째는 영어 문장(«What does that mean?») · 나머지는 한국어.
    FT_IDK_TURNS = (3, 9)
    FT_EN_TURN = 5
    FT_EN_LINE = "What does that mean?"
    FT_SILENT_TURN = 7               # 의도적 침묵 — 무음 1단 넛지가 몇 초에 오는지 잰다(최대 70초 대기)
    FT_SILENCE_MAX_S = 70.0

    def lesson_items_list(self) -> list[Item]:
        """프리토킹 소재 = **이 차시의 항목 전부**(복습 표시와 무관 — 표현학습을 다 끝낸 차시라 전부 drilled 다)."""
        lid = (self.lesson or {}).get("lesson_id")
        its = [it for it in self.items.values() if lid and it.lesson_id == lid]
        return its or [it for it in self.items.values() if not it.review] or list(self.items.values())

    def freetalk_bank(self) -> list[str]:
        """차시별 한국어 답 은행 — 문법 예문 전부 + 문형에서 만든 자기소개 문장 + 청크 인접쌍 2부 + 어휘 예문."""
        if getattr(self, "_ft_bank", None):
            return self._ft_bank
        bank: list[str] = []
        lesson_items = self.lesson_items_list()
        labels = " ".join(it.surface for it in lesson_items if it.kind == "grammar")
        if LANGUAGE == "ja":
            bank += ["はじめまして。私はジョンです。", "私はアメリカ人です。", "私の名前はジョンです。"]
        if LANGUAGE == "ko" and ("이에요" in labels or "예요" in labels):
            bank += ["저는 John이에요.", "저는 미국 사람이에요.", "제 이름은 John이에요."]
        if LANGUAGE == "ko" and ("입니다" in labels or "입니까" in labels):
            bank += ["저는 학생입니다.", "비버 씨는 어느 나라 사람입니까?"]
        # 청크(차시 1): 인접쌍 2부 — 인사·안부·사과에 응답으로 어울리는 것
        chunk_pairs = {"안녕하세요?": "안녕하세요.", "잘 지냈어요?": "네, 잘 지냈어요.", "만나서 반갑습니다": "만나서 반갑습니다.",
                       "죄송합니다": "괜찮아요.", "미안해요": "괜찮아요.", "감사합니다": "아니에요.", "고마워요": "아니에요.",
                       "이름이 뭐예요?": "저는 John이에요.", "안녕히 가세요": "안녕히 계세요.", "안녕히 계세요": "안녕히 가세요."}
        for it in lesson_items:
            if it.kind == "chunk":
                bank.append(chunk_pairs.get(it.surface, it.surface if it.surface.endswith(("요", "다", "요?", "니다")) else it.surface + "."))
        for it in lesson_items:
            if it.kind == "grammar":
                bank += [e for e in it.examples if e]
        for it in lesson_items:
            if it.kind == "vocab" and it.example and len(norm_ko(it.example)) >= 4:
                bank.append(it.example)
        seen: set[str] = set()
        self._ft_bank = [b for b in bank if not (norm_ko(b) in seen or seen.add(norm_ko(b)))] or ["네, 좋아요."]
        return self._ft_bank

    def freetalk_reply(self, text: str) -> tuple[str, str, str]:
        """(문장, 언어, 종류). 비버 턴에 차시 항목이 보이면 그 항목의 예문/인접쌍으로, 아니면 은행을 순서대로. 3·8번째 idk · 5번째 영어."""
        self.ft_i += 1
        n = self.ft_i
        if n == self.FT_SILENT_TURN and not self.ft_silence_done:
            return "", "", "ft_silence"
        if n in self.FT_IDK_TURNS:
            return IDK_EN, "en", "ft_idk"
        if n == self.FT_EN_TURN:
            return self.FT_EN_LINE, "en", "ft_en"
        hits = surfaces_in(text, {it.item_id: it for it in self.lesson_items_list()})
        if hits:
            it = self.items[hits[0]]
            if it.kind == "chunk":
                pairs = {"안녕하세요?": "안녕하세요.", "잘 지냈어요?": "네, 잘 지냈어요.", "이름이 뭐예요?": "저는 John이에요.",
                         "죄송합니다": "괜찮아요.", "감사합니다": "아니에요."}
                return pairs.get(it.surface, it.surface), LANGUAGE, "ft_ko"
            if it.kind == "grammar" and it.examples:
                return it.examples[(n - 1) % len(it.examples)], LANGUAGE, "ft_ko"
            # 어휘 과제엔 그 어휘가 든 «자기 얘기» 문장으로(예문보다 대화답게): 이름·나라·국가명
            if LANGUAGE == "ko" and it.surface == "이름":
                return "저는 John이에요.", "ko", "ft_ko"
            if LANGUAGE == "ko" and (it.surface == "나라" or re.search(r"(united states|korea|china|japan|vietnam|canada|russia|indonesia|malaysia|kingdom|america)", norm_en(it.en))):
                return f"저는 {'미국' if it.surface == '나라' else it.surface} 사람이에요.", LANGUAGE, "ft_ko"
            if it.example:
                return it.example, LANGUAGE, "ft_ko"
        bank = self.freetalk_bank()
        return bank[(n - 1) % len(bank)], LANGUAGE, "ft_ko"

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

        raw_quotes = quoted_segments(text)
        quotes = [norm_en(q) for q in raw_quotes]
        quotes = [q for q in quotes if q and re.search(r"[a-z]", q)]     # 한국어 인용(공개)은 제외
        # [문형] 항목은 비버가 «연습 문장을 상황에 맞게» 새로 만든다(1592: 「이 옷은 얼마예요?」 ← N은/는 N이에요/예요) — 표면형·예문에
        # 안 걸리므로 **서버와 같은 템플릿 매처**(quiz_judge.mentions)로 문형을 잡는다. 인용된 대상 언어 문장(없으면 턴 전체)이
        # 후보 문법 항목의 템플릿에 유일하게 걸리고, 그 문장이 어느 어휘 항목의 예문도 아니면 문형이 이긴다(「이」 같은 한 음절
        # 어휘는 아무 문장에나 들어 있다 — 그걸로 공개 판정하면 문형 드릴이 어휘로 찍힌다).
        tpl_id: Optional[int] = None
        lang_re = _LANG_CHARS.get(LANGUAGE, r"[가-힣]")
        ko_quotes = [q for q in raw_quotes if re.search(lang_re, q)]
        if ko_quotes or re.search(lang_re, text):
            try:
                from domains.learning.service.quiz_judge import is_template, mentions as _mentions
                probe = ko_quotes or [text]
                tpl = [it for it in order if it.kind == "grammar" and is_template(it.surface, LANGUAGE)
                       and any(_mentions(q, it.surface, LANGUAGE) for q in probe)]
            except Exception:  # noqa: BLE001
                tpl = []
            if len(tpl) == 1:
                ex_hit = any(norm_ko(self.items[i].example) and norm_ko(self.items[i].example) in norm_ko(q)
                             for i in mentioned for q in probe)
                if not ex_hit:
                    tpl_id = tpl[0].item_id
        if tpl_id is not None and not quotes:
            return tpl_id, "template"
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
                # 「저」=«i» 같은 한 글자 뜻이 아무 인용에나 걸리지 않게(≥4자) · 한 낱말 뜻("this")이 인용된 **문장** 안에
                # 들어 있는 건 포함이 아니다(1592: "How much is this clothing?" → 「이」) — 그건 아래 키워드 규칙(질문 턴)의 몫
                if len(q) >= 4 and len(phrase) >= 4 and (q in phrase or (phrase in q and (len(phrase.split()) >= 2 or len(q.split()) <= 2))):
                    best = max(best or (0, ""), (200 + min(len(q), 40), f'quote~"{q}"'))
                    continue
                if len(q.split()) >= 3 and len(phrase.split()) < 2:
                    continue          # 인용된 문장 ↔ 한 낱말 뜻: 키워드("this")로도 안 잡는다 — 그 문장은 문형·청크 연습이다(1592)
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
        if tpl_id is not None:
            return tpl_id, "template"
        if is_question and self.picker.enabled:
            pick, how = await self.picker.pick(text, order[:24], seg[-600:])
            if pick:
                return pick, how
            return None, how
        return None, "continuation"

    @staticmethod
    def matches_template(text: str, item: "Item") -> bool:
        """턴에 인용된 대상 언어 문장(없으면 턴 전체)이 이 [문형] 항목의 템플릿에 걸리나 — 서버와 같은 매처(quiz_judge)."""
        lang_re = _LANG_CHARS.get(LANGUAGE, r"[가-힣]")
        probe = [q for q in quoted_segments(text) if re.search(lang_re, q)] or [text]
        try:
            from domains.learning.service.quiz_judge import is_template, mentions as _mentions
            return bool(is_template(item.surface, LANGUAGE)) and any(_mentions(q, item.surface, LANGUAGE) for q in probe)
        except Exception:  # noqa: BLE001
            return False

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
            if looks_cut_off(text):
                return None, "", "wait"          # 말 끊김 — 기다린다(워치독이 6s 뒤 대신 말한다)
            return "Okay.", "en", "ack"
        p = rec.policy
        surface = rec.item.answer        # ⚠ 이름은 surface 지만 «말할 답» 이다 — 문법 항목은 예문(패턴 표기는 말할 수 없다)
        if rec.item.kind == "grammar" and self.template_sentence.get(rec.item.item_id):
            surface = self.template_sentence[rec.item.item_id]   # 비버가 정한 연습 문장이 있으면 그걸(예문은 «틀렸다» 고 한다 — 1592)
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
            if looks_cut_off(text):
                return None, "", "wait"          # 말 끊김 — 기다린다(워치독이 6s 뒤 대신 말한다)
            return "Okay.", "en", "ack"
        # 드릴 모드인데 이 항목에 «앵커 없는 회차» 가 열려 있으면(끝낸 항목 재질문) 퀴즈 정책으로 답한다
        in_requiz = self.mode == "drill" and bool(rec.rounds) and not rec.rounds[-1].anchored \
            and rec is not self._last_started
        if self.mode == "drill" and not in_requiz:
            rec.drill_attempts += 1
            if p == 2:
                rec.drill_answers.append("parrot" if rec.drill_revealed else "idk")
                if rec.drill_revealed:
                    return surface, LANGUAGE, "parrot"
                return IDK_EN, "en", "idk"
            if p == 4:
                if rec.drill_attempts >= 4:
                    # 포기했어야 한다(최대 2번 재시도). 계속 물으면 통화를 다 태우니 **공개된 표면형을 복창해 풀어준다** —
                    # 드릴 복창은 통과가 아니므로 기대(drilled only)는 그대로다. 보고서에 «포기 안 함» 이 남는다.
                    if rec.drill_attempts == 4:
                        self.notes.append(f"#{rec.k} {surface}: 드릴 오답 3회 뒤에도 비버가 포기하지 않았다 → 4회째부터 복창으로 풀어줌")
                    if rec.drill_revealed:
                        rec.drill_answers.append("parrot")
                        return surface, LANGUAGE, "parrot"
                    rec.drill_answers.append("idk")
                    return IDK_EN, "en", "idk"
                rec.drill_answers.append("distractor")
                return self.next_distractor(), LANGUAGE, "distractor"
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
            rec.rounds.append(QuizRound(n=1, asked_at=self.now(), block=len(self.anchors)))
            self.quiz_block_asked.add(rec.item.item_id)
        rd = rec.rounds[-1]
        if rd.revealed:
            rd.answers.append("parrot")
            return surface, LANGUAGE, "parrot"
        if p in (1, 6):
            rd.answers.append("correct")
            rd.spontaneous_correct = True
            return surface, LANGUAGE, "correct"
        if p == 2 or p == 4:
            rd.answers.append("idk")
            return IDK_EN, "en", "idk"
        if p == 3:
            if not rec.item.casual:
                # 어휘·문법엔 반말이 없다 — 격식 함정 대신 «오답 1회 → 공개 → 복창» 으로 not-passed 를 잰다
                rd.answers.append("distractor")
                return self.next_distractor(), LANGUAGE, "distractor"
            rd.answers.append("casual")
            return rec.item.casual, LANGUAGE, "casual"
        if p == 5:
            if not rd.answers:
                rd.answers.append("distractor")
                return self.next_distractor(), LANGUAGE, "distractor"
            # 오답 뒤 비버가 공개 없이 다시 물었다(힌트 경로) → 정답
            rd.answers.append("correct")
            rd.spontaneous_correct = True
            rd.hint_path = True
            return surface, LANGUAGE, "correct"
        return surface, LANGUAGE, "correct"

    async def _speak_later(self, reply: str, lang: str, kind: str, uplink: Uplink) -> None:
        if kind == "ft_silence":
            await self._ft_silence(uplink)
            return
        spoke = False
        say, say_lang, style_tag = reply, lang, ""
        if ANSWER_STYLE and kind == "correct" and lang == LANGUAGE:
            st = styled_answer(reply, ANSWER_STYLE)
            if st:
                say, say_lang = st
                style_tag = f"표기:{ANSWER_STYLE} ← {reply}"
        try:
            pcm = await self.voice.pcm(say, say_lang)
            # 비버가 턴을 연달아 내 우리 발화가 취소되기만 하면(1438: 3턴 혼잣말) 다음엔 쉬지 않고 바로 말한다
            await asyncio.sleep(PRE_SPEECH_S if self.cancel_streak == 0 else 0.15)
            uplink.open = True
            spoke = True
            turn = self.add_turn("learner", say, kind=kind, item_id=self.current.item.item_id if self.current else 0)
            if style_tag:
                turn.tags.append(style_tag)
            self._link_answer_turn(turn)
            self.last_learner = turn
            self.since_learner = []
            if kind in ("correct", "parrot") and self.current is not None and (
                    norm_ko(reply) in (norm_ko(self.current.item.surface), norm_ko(self.current.item.answer))
                    or any(has_surface(reply, v) for v in self.current.item.variants)):
                self.current.surface_uttered = True
            if kind == "correct":
                self.spontaneous += 1
            if kind in ("correct", "parrot") and self.mode == "drill" and self.current is not None and self.current.drill_done_at is None:
                self.current.drill_done_at = self.now()
            self.log(f"👤 {say}   [{kind}]" + (f"  ({style_tag})" if style_tag else ""))
            await uplink.speak(pcm)
            self.last_spoke_at = self.now()
            self.cancel_streak = 0
            # ① 내 발화의 input_transcript 가 3초 안에 안 오면(한·두 음절 오디오를 Gemini 가 버린다 — 1437) 더 긴 형태로 한 번 더.
            #   비버가 이미 말을 시작했으면(turn_start) 들은 것이니 재발화하지 않는다.
            if kind in ("correct", "parrot", "casual", "distractor") and lang == LANGUAGE:
                for _ in range(30):
                    await asyncio.sleep(0.1)
                    if turn.stt or self.cur_turn_id is not None or self.ended:
                        break
                if not turn.stt and self.cur_turn_id is None and not self.ended:
                    item = self.current.item if self.current is not None else None
                    longer = item.long_form if item is not None else f"{reply} {reply}"
                    if item is not None and item.kind == "grammar" and self.template_sentence.get(item.item_id):
                        longer = reply          # 비버가 정한 연습 문장을 말한 것 — 예문으로 바꿔 말하면 «틀렸다» 가 된다(1592)
                    if norm_ko(longer) == norm_ko(reply):
                        longer = f"{reply}. {reply}."
                    turn.tags.append("재발화(전사 없음)")
                    say2, lang2, tag2 = longer, lang, ""
                    if style_tag:
                        st2 = styled_answer(longer, ANSWER_STYLE)
                        if st2:
                            say2, lang2 = st2
                            tag2 = f"표기:{ANSWER_STYLE} ← {longer}"
                    t2 = self.add_turn("learner", say2, kind=kind, item_id=turn.item_id)
                    t2.tags.append("재발화")
                    if tag2:
                        t2.tags.append(tag2)
                    self._link_answer_turn(t2)
                    self.last_learner = t2
                    self.log(f"👤 {say2}   [{kind}·재발화 — 3초 안 전사 없음]")
                    await uplink.speak(await self.voice.pcm(say2, lang2))
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

    async def _ft_silence(self, uplink: Uplink) -> None:
        """의도적 침묵: 마이크는 열어 두고(무음 프레임) 비버 넛지 턴이 올 때까지 최대 FT_SILENCE_MAX_S 기다린다. 넛지가 오면
        on_json(turn_start)이 이 태스크를 취소하고 그 턴에 정상 응답한다 — 넛지 도착 시각은 on_freetalk_turn 이 기록."""
        self.ft_silence_done = True
        t0 = self.now()
        self.silence_until = t0 + self.FT_SILENCE_MAX_S
        self.ft_silence = {"start": round(t0, 1), "nudge_at": None, "nudge_text": ""}
        uplink.open = True
        t = self.add_turn("learner", "(침묵 — 넛지 대기)", kind="ft_silence")
        t.tags.append("의도적 침묵")
        self.last_learner = t
        self.since_learner = []
        self.log(f"👤 (침묵 시작 — 무음 넛지가 몇 초에 오나, 최대 {self.FT_SILENCE_MAX_S:.0f}s)")
        try:
            while self.now() < self.silence_until and not self.ended:
                await asyncio.sleep(0.5)
            if self.ft_silence.get("nudge_at") is None:
                self.log(f"   (침묵 {self.FT_SILENCE_MAX_S:.0f}s 동안 넛지 없음 → 다시 말한다)")
                self.silence_until = -1.0
                reply, lang, kind = self.freetalk_reply("")
                await self._speak_later(reply, lang, kind, uplink)
        except asyncio.CancelledError:
            raise
        finally:
            self.silence_until = -1.0

    async def watchdog(self, uplink: Uplink) -> None:
        """①⛔ 무응답 방지 — 비버 turn_end 뒤 6초 동안 내가 소리를 안 냈으면(발화 태스크가 죽었든 취소됐든) 무조건 말한다.
        «따라 하라» 문구(say/repeat + 표면형)면 그 표현(짧으면 X요)을, 아니면 «I don't know». 비버가 말하는 중이면 끝나기를 기다린다."""
        try:
            while not self.ended:
                await asyncio.sleep(0.5)
                if self.probe or self.cur_turn_id is not None or self.last_beaver_end <= 0 or self.now() < self.silence_until:
                    continue
                if self.now() - self.last_beaver_end < 6.0 or self.last_spoke_at >= self.last_beaver_end:
                    continue
                if self.pending_speak is not None and not self.pending_speak.done():
                    self.pending_speak.cancel()
                self.watchdog_fires += 1
                if self.course == "freetalk":
                    reply, lang, kind = self.freetalk_reply(self.last_beaver_text)
                else:
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
        if not m or not re.search(_LANG_CHARS.get(LANGUAGE, r"[가-힣]"), m.group(2)):
            return None
        q = m.group(2).strip().rstrip(".!?")
        # (우리 오답을 «"이름요"? What is that?» 처럼 되풀이한 건 앞에 명령형이 없어 위 정규식에 안 걸린다)
        hits = surfaces_in(q, self.items)
        # 문장(2어절 이상)을 시켰는데 걸린 건 그 안의 한 음절 어휘(「이 옷은 얼마예요?」 의 「이」)면 항목 답이 아니라 **그 문장** 을 복창(1592)
        if hits and not (len(q.split()) >= 2 and len(norm_ko(self.items[hits[0]].surface)) <= 2):
            it = self.items[hits[0]]
            return it.answer, LANGUAGE, "parrot"
        tail = "です" if LANGUAGE == "ja" else "요"
        return (f"{q}{tail}" if len(norm_ko(q)) <= 1 else q), LANGUAGE, "parrot"


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
                   lesson: dict | None = None, distractors: list[str] | None = None,
                   continues_call_id: Optional[int] = None, passed_before: set[int] | None = None,
                   seamless: bool = False, switch_after_s: float = 120.0, silent_resume: bool = False,
                   silent_never: bool = False, resume_prompt: str = "", watch_s: float = 15.0,
                   cut_after_s: Optional[float] = None, reconnect_on_saved: bool = False) -> Session:
    import websockets

    ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + WS_PATH + f"?token={token}"
    sess = Session(items, voice, picker, probe=probe, verbose=verbose, course=course, lesson=lesson)
    sess.distractor_pool = list(distractors or [])
    sess.continues_call_id = continues_call_id
    sess.passed_before = set(passed_before or ())
    sess.seamless = seamless
    sess.silent_resume = silent_resume
    sess.silent_never = silent_never
    sess.resume_prompt = resume_prompt
    sess.reconnect_on_saved = reconnect_on_saved
    # call_type: "expression" | "freetalk" | "auto"(서버가 정해 call_started.course 로 알림 — 계획 §8)
    start = {"type": "start", "character_id": 1, "locale": LOCALE, "duration_min": duration_min,
             "call_type": course, "aec": {"supported": False}, "sample_rate": SR_IN, "num_channels": 1,
             "tz_offset_min": 540}
    if continues_call_id is not None:
        # ① 이어하기 — 앱이 다음 조각에서 돌려주는 값(protocol.ClientStart.continues_call_id, str|int). 서버가 본인·TTL·조각 상한을 검증하고
        #   거절이면 **새 통화로 폴백**한다(call_started.call_id 가 달라진다) — Free(상한 1)는 그게 정상이다.
        start["continues_call_id"] = str(continues_call_id)
    if silent_resume:
        start["silent_resume"] = True       # ⭐ H8 S1/S2: 재개 시드 0 — 비버는 학습자 첫 발화를 기다린다
    async with websockets.connect(ws_url, max_size=None, ping_interval=20, ping_timeout=20,
                                  open_timeout=30) as ws:
        sess.t0 = time.perf_counter()

        async def _send_ctrl(d: dict) -> None:
            await ws.send(json.dumps(d))

        sess.send_ctrl = _send_ctrl
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
            await asyncio.sleep(cut_after_s if cut_after_s is not None else duration_min * 60)
            if not sess.ended:
                sess.ended = True
                sess.end_reason = "client_cut"
                sess.log(f"client_cut: {(cut_after_s if cut_after_s is not None else duration_min * 60) / 60:.1f}분 도달 → 소켓 닫음(앱과 같은 무음 컷)")
                with contextlib.suppress(Exception):
                    await ws.close()

        async def seamless_switch() -> None:
            # ⭐ H8 조각1: --segment-min 뒤 «전환 대기» → (turn_end 처리기가 fragment_end 전송) → fragment_saved 5s 상한 → 서버가 닫는다.
            #   fragment_end 를 90s 안에 못 보내면(학습자·비버 교환이 안 일어남) 종전 무음 컷으로 닫는다.
            await asyncio.sleep(switch_after_s)
            sess.switch_at = sess.now()
            sess.log(f"전환 대기 시작({switch_after_s:.0f}s) — 다음 «학습자 발화 → 비버 turn_end» 에서 fragment_end")
            for _ in range(900):
                await asyncio.sleep(0.1)
                if sess.fragment_end_sent_at is not None or sess.ended:
                    break
            if sess.fragment_end_sent_at is None and not sess.ended:
                sess.errors.append("seamless: 90s 안에 fragment_end 를 못 보냈다 → 무음 컷")
                sess.ended = True
                sess.end_reason = "client_cut(전환 실패)"
                with contextlib.suppress(Exception):
                    await ws.close()
                return
            for _ in range(50):
                await asyncio.sleep(0.1)
                if sess.fragment_saved_at is not None or sess.ended:
                    break
            if sess.fragment_saved_at is None and not sess.ended:
                sess.errors.append("seamless: fragment_end 뒤 5s 안에 fragment_saved 가 없다 → 클라가 닫음(폴백)")
                sess.log("⛔ fragment_saved 5s 상한 초과 → 소켓 닫음")
                sess.ended = True
                sess.end_reason = "client_cut(fragment_saved 없음)"
                with contextlib.suppress(Exception):
                    await ws.close()

        async def silent_opener() -> None:
            # ⭐ H8 조각2: call_started 뒤 watch_s 동안 비버 출력(오디오·전사·turn_start)을 세고, 그 뒤 학습자가 먼저 말한다
            #   (조각1 마지막 비버 턴에 답한다 — 브리프가 잇는지 (d) 로 본다). (g) silent_never 면 끝까지 말하지 않는다.
            for _ in range(300):
                await asyncio.sleep(0.1)
                if sess.started_at is not None or sess.ended:
                    break
            if sess.started_at is None:
                return
            await asyncio.sleep(watch_s)
            sess.pre_speech["audio_bytes"] = sess.beaver_audio_bytes
            sess.pre_speech["watch_s"] = watch_s
            sess.watch_done = True
            sess.log(f"무음 관찰 {watch_s:.0f}s 끝 — 비버 오디오 {sess.beaver_audio_bytes}B · 전사 {len(sess.pre_speech['transcripts'])} · turn_start {sess.pre_speech['turn_starts']}")
            if silent_never:
                sess.log("(g) 학습자는 끝까지 말하지 않는다 — 무음 3단 관찰")
                return
            sess.hold_reply = False
            sess.silence_until = -1.0
            sess.first_speech_at = sess.now()
            if resume_prompt:
                sess.log(f"조각1 마지막 비버 턴에 답한다: {resume_prompt[:100]}")
                await sess.on_beaver_turn(resume_prompt, uplink)
                if sess.turns and sess.turns[-1].role == "beaver":
                    sess.turns[-1].tags.append("조각1 마지막 턴(재생 — 실제 조각2 발화 아님)")
                elif len(sess.turns) >= 2 and sess.turns[-2].role == "beaver":
                    sess.turns[-2].tags.append("조각1 마지막 턴(재생 — 실제 조각2 발화 아님)")
            else:
                sess.pending_speak = asyncio.create_task(sess._speak_later("Okay, let's continue.", "en", "ack", uplink))

        cut_task = asyncio.create_task(seamless_switch() if (seamless and not silent_resume) else client_cut())
        opener_task = asyncio.create_task(silent_opener()) if silent_resume else None
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
                    if sess.fragment_saved_at is None:
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
            sess.ws_closed_at = sess.now()
            sess.ws_close_code = getattr(ws, "close_code", None)
            if sess.fragment_saved_at is not None:
                sess.ended = True
                who = "하네스가 닫음(즉시 재연결)" if reconnect_on_saved else "서버가 닫음"
                sess.log(f"소켓 닫힘 · {who} · fragment_saved 뒤 {(sess.ws_closed_at - sess.fragment_saved_at) * 1000:.0f}ms · close_code={sess.ws_close_code}")
            for t in (up_task, ka_task, cut_task, wd_task, opener_task, sess.pending_speak):
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
    order_ok: bool = True            # ② 퀴즈 블록 안 항목 번호 오름차순
    redrill_ok: bool = True          # ① (llm) 가르친·통과 항목이 다시 드릴되지 않았다
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


_ITEM_LIST_RE = re.compile(r"표현학습 목록:\s*(.+)$")
_ITEM_NUM_RE = re.compile(r"(\d+)=")


def parse_item_numbers(log_lines: list[str] | None) -> dict[int, str]:
    """서버 «normalcall 표현학습 목록: 1=인사말 2=N은/는 N이에요/예요 …» → {번호: 표면형}.
    ⚠ 이게 번호 **정본**이다(서버 state.expr_items 기준). cur_call.items 스냅샷 위치로 세면 어긋난다 —
    ko 1626 은 스냅샷 8항목인데 서버는 #15 를 썼다(7차 재검). 표면형에 공백이 있어(«N입니까?, N입니다») 다음 «n=» 앞까지를 한 항목으로 자른다."""
    out: dict[int, str] = {}
    for ln in log_lines or []:
        m = _ITEM_LIST_RE.search(ln)
        if not m:
            continue
        body = m.group(1).strip()
        body = re.sub(r"\(\s*\d+\s*개[^)]*\)\s*$", "", body).strip()   # 꼬리 «(18개, 복습 0)»(8차 A) 제거
        hits = list(_ITEM_NUM_RE.finditer(body))
        for i, h in enumerate(hits):
            end = hits[i + 1].start() if i + 1 < len(hits) else len(body)
            surface = body[h.end():end].strip().rstrip("·,").strip()      # 구분자 « · » 꼬리 제거
            if surface:
                out[int(h.group(1))] = surface
    return out


def item_numbers_by_id(num_to_surface: dict[int, str], items: dict) -> dict[int, int]:
    """{번호: 표면형} + 하네스 항목 → {item_id: 번호}. 표면형이 같은 항목이 둘이면 둘 다 버린다(모호)."""
    by_surface: dict[str, list[int]] = {}
    for iid, it in (items or {}).items():
        by_surface.setdefault(norm_ko(it.surface), []).append(iid)
    out: dict[int, int] = {}
    for num, surface in num_to_surface.items():
        cand = by_surface.get(norm_ko(surface)) or []
        if len(cand) == 1:
            out[cand[0]] = num
    return out


_QUIZ_OPEN_RE = re.compile(r"퀴즈 큐 열림: seq=(\d+).*?항목=\[([0-9,\s]*)\]")


def parse_quiz_sets(log_lines: list[str] | None) -> list[tuple[Optional[float], int, list[int]]]:
    """서버 «normalcall 표현학습 퀴즈 큐 열림: seq=N open_seg=.. 항목=[a, b, c]» → [(epoch|None, seq, [번호…])] 시간순.
    4차 LLM 판정은 **열린 세트 안 항목만** 판정한다(1615 #13·1616 #7: 세트 밖 정답 = 미판정)."""
    out: list[tuple[Optional[float], int, list[int]]] = []
    for ln in log_lines or []:
        m = _LOG_TS_RE.match(ln.strip())
        body = m.group(2) if m else ln
        q = _QUIZ_OPEN_RE.search(body)
        if not q:
            continue
        ts = None
        if m:
            base, _, frac = m.group(1).partition(".")
            ts = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() + (float("0." + frac) if frac else 0.0)
        out.append((ts, int(q.group(1)), [int(x) for x in re.findall(r"\d+", q.group(2))]))
    return out


_SIDECAR_RE = re.compile(
    r"판정 사이드카: call_id=(\d+)\s+(\S+)\s*·\s*가르침 (\d+)회\(건너뜀 (\d+)·실패 (\d+)·폴백 (\d+)\)\s*·\s*정답 (\d+)회\(실패 (\d+)\)"
    r"\s*·\s*지연 p50 (\d+)ms 최대 (\d+)ms\s*·\s*토큰 in (\d+) out (\d+)")
_SIDECAR_TIMEOUT_RE = re.compile(r"타임아웃\(([\d.]+)s\)\s*가르침\s*(\d+)\s*·\s*정답\s*(\d+)")


def parse_judge_sidecar(line: str | None) -> Optional[dict]:
    """«normalcall 판정 사이드카: call_id=N LLM · 가르침 a회(건너뜀·실패·폴백) · 정답 b회(실패) · 지연 p50/최대 · 토큰 in/out[ · 타임아웃(4.5s) 가르침 x·정답 y]»
    → 필드 dict. ⚠ 줄 끝에 앵커를 두지 않는다 — 6차(a43ff44)에 타임아웃 꼬리가 붙었고 앞으로도 꼬리가 늘 수 있다. 꼬리 없는 옛 줄은 timeout_* = None."""
    m = _SIDECAR_RE.search(line or "")
    if not m:
        return None
    keys = ("call_id", "mode", "taught", "taught_skip", "taught_fail", "taught_fallback", "quiz", "quiz_fail", "p50_ms", "max_ms", "tok_in", "tok_out")
    d: dict = {k: (v if k == "mode" else int(v)) for k, v in zip(keys, m.groups())}
    t = _SIDECAR_TIMEOUT_RE.search(line or "")
    d["timeout_s"] = float(t.group(1)) if t else None
    d["timeout_taught"] = int(t.group(2)) if t else None
    d["timeout_quiz"] = int(t.group(3)) if t else None
    return d


_QUIZ_CLOSE_LOG_RE = re.compile(r"퀴즈 (?:큐 강제 닫힘|닫힘\(LLM 판정(?:·마지막)?\)|닫힘\(서버[^)]*\)): seq=(\d+)")


def parse_quiz_windows(log_lines: list[str] | None) -> list[tuple[int, Optional[float], Optional[float], list[int]]]:
    """서버 퀴즈 창 [(seq, 열림 epoch, 닫힘 epoch|None, 번호…)] — 닫힘 = «퀴즈 닫힘(LLM 판정[·마지막])» 또는 «강제 닫힘» 중 첫 줄.
    5차 재검(1618 #10 · 1620 되감기 5): 하네스는 비버 문구로 퀴즈 모드를 추정해 닫힌 뒤 질문을 세트 밖으로, 못 잡은 여는 턴 뒤 질문을
    되감기로 셌다 — 서버 창 시각이 정본."""
    wins: dict[int, list] = {}
    order: list[int] = []
    for ts, seq, nums in parse_quiz_sets(log_lines):
        if seq not in wins:
            wins[seq] = [seq, ts, None, nums]
            order.append(seq)
    for ln in log_lines or []:
        m = _LOG_TS_RE.match(ln.strip())
        body = m.group(2) if m else ln
        q = _QUIZ_CLOSE_LOG_RE.search(body)
        if not q or not m:
            continue
        seq = int(q.group(1))
        base, _, frac = m.group(1).partition(".")
        ts = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() + (float("0." + frac) if frac else 0.0)
        if seq in wins and wins[seq][2] is None:
            wins[seq][2] = ts
    return [tuple(wins[k]) for k in order]


def in_quiz_window(epoch: float, windows: list, slack: float = 1.0) -> Optional[int]:
    """그 시각이 들어가는 서버 퀴즈 창의 seq(없으면 None). 닫힘 없는 창은 끝까지 열린 것으로 본다."""
    for seq, t_open, t_close, _nums in windows:
        if t_open is None:
            continue
        if t_open - slack <= epoch and (t_close is None or epoch <= t_close + slack):
            return seq
    return None


def llm_judge_table(records: dict, drilled_order: list[int], quiz_items: list[dict], turns: list,
                    server_sets: Optional[list[list[int]]] = None, offset_expect: str = "unjudged",
                    num_of: Optional[dict[int, int]] = None) -> tuple[bool, list[str], dict]:
    """4차(LLM 판정) 기대 — **서버 판정 결과(cur_call.items 스냅샷 = result.quiz_items)** 를 정본으로, 하네스는 «무엇을 했는가» 만 댄다.
      ③ 퀴즈 회차의 공개 전 자발 정답 + 그 턴 전사 있음 → 서버 passed 여야(표기 변형 답이면 ③ 로 따로 센다) · 앵커 없는 재출제만이면 passed~
      ④ 공개 뒤 복창 / 드릴만 한 항목 / 오답·모름 → 서버 passed 면 ✖ · 반말 답은 ~(LLM 이 격식을 어떻게 볼지 미정)
      과검출 = 서버 passed 인데 하네스 기록에 없는 항목 ✖. 가르쳤나(서버 drilled)는 LLM 문맥 판정이라 불일치를 **세기만** 한다(하네스 식별도 추정이다).
      세트 밖 = server_sets(서버 «퀴즈 큐 열림» 항목 번호들)가 주어졌고 그 항목 번호가 어느 세트에도 없으면 서버는 판정하지 않는다 —
        자발 정답이어도 기대 «—(세트 밖)» · 비버 이탈로 센다(1615 #13 · 1616 #7). server_sets None = 로그 없음 → 종전 기대.
    반환 (ok, 표 줄, counts{styled:[n,ok], reveal:[n,ok], drill_only:[n,ok], silent:n, teach_mismatch:[ids], extra_passed:[ids], off_set:[번호]})."""
    qi = {int(q.get("item_id") or 0): q for q in (quiz_items or [])}
    # 번호 정본은 서버 «표현학습 목록» 로그(num_of) — 없으면 옛 방식(스냅샷 위치)
    num_of = dict(num_of or {}) or {int(q.get("item_id") or 0): n + 1 for n, q in enumerate(quiz_items or [])}
    in_sets = None if server_sets is None else {x for st in server_sets for x in st}
    by_n = {t.n: t for t in turns}
    cnt = {"styled": [0, 0], "reveal": [0, 0], "drill_only": [0, 0], "silent": 0, "teach_mismatch": [], "extra_passed": [], "off_set": []}
    lines = ["| # | 항목 | 하네스가 한 것 | 답(말한 표기 → 서버 전사) | 서버 drilled | 서버 passed | 기대 | 판정 |",
             "|---|---|---|---|---|---|---|---|"]
    ok_all = True
    for iid in drilled_order:
        rec = records.get(iid)
        if rec is None or rec.superseded_by:
            continue
        q = qi.get(iid)
        srv_pass = bool(q and q.get("passed"))
        srv_drill = bool(q.get("drilled", True)) if q is not None else False
        ans_turns = [by_n[n] for rd in rec.rounds for n in rd.answer_turns if n in by_n]
        styled = [t for t in ans_turns if any(x.startswith("표기:") for x in t.tags)]
        mark = ""
        num = num_of.get(iid)
        if rec.expected_passed and in_sets is not None and num is not None and num not in in_sets:
            cnt["off_set"].append(num)
            if offset_expect == "passed":
                # 5차 A: 서버가 세트 밖 정답도 판정·기록한다 → passed 여야
                ok, did, exp = srv_pass, "퀴즈 자발 정답 · 서버 퀴즈 세트 밖(비버 이탈)", "passed(세트 밖)"
            else:
                ok, did, exp = True, "퀴즈 자발 정답 · 서버 퀴즈 세트 밖(비버 이탈)", "—(세트 밖)"
                mark = "~" if srv_pass else ""
        elif rec.expected_passed and all(rd.hint_path for rd in rec.rounds if rd.spontaneous_correct and rd.heard) and any(
                (by_n[n].text or "").strip().rstrip("?.!") in STALL_WORDS
                for rd in rec.rounds for n in rd.answer_turns if n in by_n and by_n[n].kind in ("distractor", "casual")):
            # 첫 답이 «알겠어요/잠시만요» 류 — 판정기가 멈춤(pending)으로 볼 수도, 답(failed)으로 볼 수도 있다(1618 failed · 1621 pending) → 어느 쪽이든 ~
            ok, did, exp = True, "멈춤형 첫 답 → 힌트 뒤 정답", "passed~/failed~"
            mark = "~"
        elif rec.expected_passed and all(rd.hint_path for rd in rec.rounds if rd.spontaneous_correct and rd.heard):
            # 5차 B(968ddb1·74d19db): 질문 직후 발화는 틀려도 답 → 첫 답 오답이면 failed · 다시 물어 맞혀도 되돌리지 않는다(1618 さようなら)
            ok, did, exp = not srv_pass, "첫 답 오답 → 힌트 뒤 정답(5차 B)", "—(첫 답 오답)"
            cnt["hint_fail"] = cnt.get("hint_fail", 0) + 1
        elif rec.expected_passed:
            ok = srv_pass or rec.expectation_ambiguous
            did = "퀴즈 자발 정답" + (" · 표기 변형 ③" if styled else "") + (" · 앵커 없는 재출제" if rec.expectation_ambiguous else "")
            exp = "passed~" if rec.expectation_ambiguous else "passed"
            mark = "~" if (ok and rec.expectation_ambiguous and not srv_pass) else ""
            if styled:
                cnt["styled"][0] += 1
                cnt["styled"][1] += int(bool(ok))
        elif rec.unjudgeable_correct:
            ok, did, exp = True, "자발 정답 · 전사 없음(무음 턴)", "—~"
            mark = "~" if srv_pass else ""
            cnt["silent"] += 1
        elif any(rd.revealed for rd in rec.rounds):
            ok, did, exp = not srv_pass, "비버 공개 뒤 복창 ④", "—"
            cnt["reveal"][0] += 1
            cnt["reveal"][1] += int(ok)
        elif not rec.rounds:
            ok, did, exp = not srv_pass, "드릴만(퀴즈 없음) ④", "—"
            cnt["drill_only"][0] += 1
            cnt["drill_only"][1] += int(ok)
        elif any("casual" in rd.answers for rd in rec.rounds):
            ok, did, exp = True, "반말 답", "—~"
            mark = "~" if srv_pass else ""
        else:
            ok, did, exp = not srv_pass, "오답/모름 " + "·".join(a for rd in rec.rounds for a in rd.answers), "—"
        if q is None or not srv_drill:
            cnt["teach_mismatch"].append(iid)
        ok_all &= bool(ok)
        ans = " / ".join(f"{t.text[:30]} → «{(t.stt or '(전사 없음)')[:30]}»" for t in (styled or ans_turns)[:2]) or "—"
        lines.append(f"| {rec.k} | {rec.item.surface} | {did} | {ans} | {'✔' if srv_drill else '✖'} | {'passed' if srv_pass else '—'} | {exp} | "
                     f"{mark or ('✔' if ok else '✖')} |")
    for iid, q in qi.items():
        if q.get("passed") and (iid not in records or records[iid].superseded_by):
            ok_all = False
            cnt["extra_passed"].append(iid)
            lines.append(f"| – | {q.get('surface')} | (하네스 기록 없음) | — | {'✔' if q.get('drilled', True) else '✖'} | passed | — | ✖ 과검출 |")
    return ok_all, lines, cnt


def quiz_blocks(records: dict) -> dict[int, list[int]]:
    """앵커 회차(block ≥1)별로 «처음 물은 시각» 순 항목 id — 같은 블록에서 다시 물은 건 첫 번만."""
    ev: dict[int, list[tuple[float, int]]] = {}
    for iid, rec in records.items():
        if rec.superseded_by:
            continue
        for rd in rec.rounds:
            if rd.anchored and rd.block >= 1:
                ev.setdefault(rd.block, []).append((rd.asked_at, iid))
    out: dict[int, list[int]] = {}
    for b, lst in sorted(ev.items()):
        seen: list[int] = []
        for _, iid in sorted(lst):
            if iid not in seen:
                seen.append(iid)
        out[b] = seen
    return out


def quiz_order_check(blocks: dict[int, list[int]], num_of: dict[int, int]) -> tuple[bool, list[str]]:
    """② 3차 규칙: 한 퀴즈 블록 안 출제 순서는 **항목 번호 오름차순**(번호 = cur_call.items 1-기준 위치 = 서버 로그 «항목 N»).
    번호 모르는 항목(목록 밖)은 순서 판정에서 뺀다. 번호 목록이 비면(옛 경로) 판단 불가 → True."""
    if not num_of:
        return True, ["- 항목 번호 없음(quiz_items 비어 있음) — 순서 판단 불가"]
    ok_all = True
    lines = []
    for b, iids in blocks.items():
        nums = [num_of.get(i) for i in iids]
        known = [n for n in nums if n is not None]
        ok = all(a < c for a, c in zip(known, known[1:]))
        ok_all &= ok
        lines.append(f"- 블록 {b}: 출제 번호 {' → '.join(str(n) if n is not None else '?' for n in nums)} {'✔' if ok else '✖ 오름차순 아님'}")
    return ok_all, lines


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
           'OR textPayload:"👤" OR textPayload:"재개 시드" OR textPayload:"제어 태그" OR textPayload:"압축 감지" OR textPayload:"재연결" OR textPayload:"무음" OR textPayload:"cur open_call" OR textPayload:"이어하기" OR textPayload:"판정" OR textPayload:"판정 사이드카" OR textPayload:"강제 닫힘")')
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
    _bs = beaver_turn_stats(sess.turns)
    sc.cur["beaver_stats"] = _bs
    L.append(f"- 비버 턴 글자수 평균 {_bs['avg_chars']:.0f} · 최대 {_bs['max_chars']} · 동일 문장 연속 반복 "
             + (f"**{_bs['repeat_max']}회** {_bs['repeat_span']} «{_bs['repeat_text'][:80]}» (구간 {_bs['repeat_runs']})" if _bs["repeat_max"] >= 2 else "0"))
    if sess.course != "freetalk":
        passed_times = {iid: min(x.asked_at for x in r.rounds if x.spontaneous_correct)
                        for iid, r in sess.records.items() if any(x.spontaneous_correct for x in r.rounds)}
        n_re, ev = count_redrills(sess.turns, sess.items, sess.passed_before, passed_times)
        total_passed = len(sess.passed_before | set(passed_times))
        sc.cur["redrill"] = {"n": n_re, "total_passed": total_passed, "events": ev}
        in_list = sorted({int(q.get("item_id") or 0) for q in (sc.cur.get("quiz_items") or [])} & sess.passed_before)
        L.append(f"- **통과 항목 재드릴 {n_re}/총 통과 {total_passed}**" + (" — " + ", ".join(f"t{tn}「{sess.items[i].surface}」" for tn, i in ev[:10]) if ev else " ✔")
                 + f" (통화 전 통과 {len(sess.passed_before)} · 이 통화 통과 {len(passed_times)} · 서버 목록에 든 통화 전 통과 항목 {len(in_list)} — 복습 채움이면 서버 선별, 아니면 비버 이탈)")
    if sess.watchdog_fires or sess.speak_errors:
        L.append(f"- ⚠ 하네스 워치독 발화 {sess.watchdog_fires}회 · 발화 태스크 예외 {sess.speak_errors}회 — 무응답 방지가 동작했다(원인은 §전사 태그 «워치독»·«대체발화»)")
    if LANGUAGE != "ko" or sc.cur.get("lang_check"):
        lc = sc.cur.get("lang_check") or {}
        L.append(f"- **언어** target={LANGUAGE} · locale={LOCALE} · /cur/me.language={lc.get('me_language')} {'✔' if lc.get('me_language') == LANGUAGE else '✖'} · "
                 f"학습자 TTS {lc.get('voice')} · 계정 target_language {lc.get('orig_target')}→{LANGUAGE}(끝나면 복구)")
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
    _sec1_i = len(L)
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
        exp_s = ("passed~" if amb else "passed") if exp_passed else ("—(" + rec.unjudgeable_correct + ")" if rec.unjudgeable_correct else "—")
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
    if JUDGE_MODE == "llm" and sess.course != "freetalk":
        # ⭐ 4차: 판정은 서버 LLM 이 한다 — 하네스 자체 매칭(표면형·템플릿) 기대를 버리고 서버 결과를 정본으로 ①~④ 만 본다
        del L[_sec1_i:]
        L[-1] = "## 1. 판정 — 서버 LLM 판정 결과(cur_call.items) ↔ 하네스가 한 것 (기대 ①~④)"
        if ANSWER_STYLE:
            L.append(f"- 학습자 정답 표기: **{ANSWER_STYLE}** (말한 표기와 서버 전사를 «답» 열에 둘 다 적는다)")
        L.append("")
        _srv_sets = parse_quiz_sets(server_logs) if server_logs is not None else []
        sc.cur["server_quiz_sets"] = [(seq, nums) for _, seq, nums in _srv_sets]
        _num_src = parse_item_numbers(server_logs)
        _num_of = item_numbers_by_id(_num_src, sess.items)
        sc.cur["item_numbers"] = {"source": "서버 목록 로그" if _num_src else "cur_call.items 위치(폴백)", "n": len(_num_of)}
        ok_llm, tbl, cnt = llm_judge_table(sess.records, sess.drilled_order, sc.cur.get("quiz_items") or [], sess.turns,
                                           server_sets=[nums for _, _, nums in _srv_sets] if _srv_sets else None,
                                           offset_expect=OFFSET_EXPECT, num_of=_num_of)
        L.append(f"- 항목 번호 정본: {sc.cur['item_numbers']['source']} ({len(_num_of)}개 대응"
                 + (f" · 서버 목록 {len(_num_src)}항목" if _num_src else "") + ")")
        L += tbl
        sc.judge_ok = ok_llm
        sc.cur["llm_judge"] = cnt
        _wins = parse_quiz_windows(server_logs) if server_logs is not None else []
        _off = (sess.turns[0].wall - sess.turns[0].t) if sess.turns else 0.0
        # 앵커 없는 회차라도 그 질문 시각이 서버 퀴즈 창 안이면 퀴즈 질문이다(하네스가 여는 문구를 못 잡은 것 — 1620)
        unanch = [r for r in sess.records.values() if not r.superseded_by and r.drill_done_at is not None and any(
            (not rd.anchored) and rd.asked_at > r.drill_done_at
            and not (_wins and in_quiz_window(rd.asked_at + _off, _wins) is not None) for rd in r.rounds)]
        rd_ = sc.cur.get("redrill") or {}
        sc.redrill_ok = not unanch and not rd_.get("n")
        L.append("")
        L.append(f"- ① 가르친 항목 재드릴: 앵커 없는 재출제/되감기 {len(unanch)}건"
                 + (" " + ", ".join(f"「{r.item.surface}」" for r in unanch) if unanch else "")
                 + f" · 통과 항목 재드릴 {rd_.get('n', 0)} → {'✔' if sc.redrill_ok else '✖'}")
        L.append("- ② 번호 순 출제: §2 «퀴즈순서»")
        L.append(f"- ③ 표기 변형 정답 → 통과: {cnt['styled'][1]}/{cnt['styled'][0]}" + ("" if ANSWER_STYLE else " (--answer-style 없음)"))
        L.append(f"- ④ 공개 뒤 복창 → 통과 아님: {cnt['reveal'][1]}/{cnt['reveal'][0]} · 드릴만 → 통과 아님: {cnt['drill_only'][1]}/{cnt['drill_only'][0]}")
        L.append(f"- 서버 퀴즈 세트 {sc.cur['server_quiz_sets'] or '(로그 없음 — 세트 밖 판별 안 함)'} · 세트 밖 자발 정답(비버 이탈, 서버 미판정이 정상) 번호 {cnt['off_set']}")
        L.append(f"- 참고: 무음 턴 정답 {cnt['silent']} · 서버가 «가르침» 으로 안 친 하네스 드릴 항목 {len(cnt['teach_mismatch'])} {cnt['teach_mismatch'][:10]} · 과검출 {cnt['extra_passed']}")
        jl = [ln for ln in (server_logs or []) if "판정" in ln]
        side = [ln for ln in (server_logs or []) if "판정 사이드카" in ln]
        sc.cur["judge_sidecar"] = side
        fb = [ln for ln in (server_logs or []) if "표현학습 퀴즈 판정(서버" in ln]
        if server_logs is not None:
            # 4차(app-api 00160): 통화 종료 1줄 «normalcall 판정 사이드카: 가르침 n(건너뜀·실패·폴백)·정답 m(실패)·지연·토큰» · 옛 «퀴즈 판정(서버)» 줄은 폴백일 때만
            _sp = parse_judge_sidecar(side[-1]) if side else None
            sc.cur["judge_sidecar_parsed"] = _sp
            _to = ("" if _sp is None else (f" · **타임아웃({_sp['timeout_s']}s) 가르침 {_sp['timeout_taught']}·정답 {_sp['timeout_quiz']}**"
                                            if _sp["timeout_s"] is not None else " · (타임아웃 꼬리 없음 — 6차 전 서버)"))
            L.append(f"- **서버 판정 사이드카**: " + (f"`{side[-1].split('판정 사이드카', 1)[-1].strip()}`{_to}" if side else "⚠ 줄 없음(로그 창 밖이거나 사이드카 미동작)")
                     + f" · 폴백 판정 줄 {len(fb)}")
            L.append(f"- 서버 판정 로그 {len(jl)}줄" + (":" if jl else " (gcloud 창에 없음)"))
            L += [f"  - `{ln[:220]}`" for ln in jl[:30]]
    L.append("")
    unm = [] if JUDGE_MODE == "llm" else [r for r in sess.records.values() if not r.item.server_matchable]
    if unm:
        L.append("- ⚠ 서버 미인식 " + str(len(unm)) + "건 — 표면형 매처(quiz_judge.mentions)도 못 알아보고 예문도 없다: "
                 + ", ".join(f"「{r.item.surface}」←「{r.item.answer}」" for r in unm) + " → 서버는 이 항목을 drilled/passed 로 찍을 수 없다(커리큘럼 예문 또는 매처 수정 재료)")
    if JUDGE_MODE != "llm":
        L.append("- 기대 drilled = 표면형이 비버 공개나 학습자 발화로 실제 한 번 나왔다(결정 6 «모국어 설명만으론 안 됨»). "
             "기대 passed = 퀴즈 회차에서 **공개 전 자발 정답**(하네스가 고른 답) · 판정 창은 퀴즈를 여는 비버 턴 **포함**(그 턴에서 공개하면 복창 → failed 기대) "
             "· ④ 그 답 턴에 input_transcript 가 온 것만(전사 없는 턴은 서버에 «무음 턴» — 학습자 턴 아님). `passed~` = 그 정답이 앵커 없는 재출제에서만 났다 → 판정기가 보류해도 된다(결정 6-3), 어느 쪽이든 ✔(~)")
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
    L.append(f"## 2. 퀴즈 주기·순서 — 미출제 {QUIZ_GROUP}개가 모이면 퀴즈 · 블록 안 항목 번호 오름차순(3차 규칙)")
    pend_end = sess.unquizzed_ids()
    if not sess.anchors:
        L.append(f"- 앵커 0회 (드릴 {len(sess.drilled_order)}개 · 끝 미출제 {len(pend_end)}) — " +
                 (f"✖ 미출제 {QUIZ_GROUP}개가 모였는데 퀴즈가 없었다" if len(pend_end) >= QUIZ_GROUP else f"미출제 {QUIZ_GROUP}개 미만 — 판단 불가"))
        sc.period_ok = len(pend_end) < QUIZ_GROUP
        if JUDGE_MODE == "llm" and cue_match and cue_match.get("cues"):
            # 서버가 퀴즈를 열었는데 하네스가 앵커를 못 봤다 = 비버 문구 미검출일 수 있다(1617) — 실패로 두지 않고 표시만
            sc.period_ok = True
            L.append(f"  - ⚠ 서버 큐 {len(cue_match['cues'])}회 · 하네스 앵커 0 — 퀴즈 여는 비버 문구를 하네스가 못 잡았을 수 있다(전사 확인)")
    else:
        cue_by_turn = {tn: (c_t, d) for c_t, tn, d in cue_match["pairs"]} if cue_match else {}
        srv_cue_mode = JUDGE_MODE == "llm" and bool(cue_match and cue_match.get("cues"))
        srv_wins = parse_quiz_windows(server_logs) if server_logs is not None else []

        def _in_srv_window(tn: int) -> bool:
            return bool(srv_wins) and 0 <= tn < len(sess.turns) and in_quiz_window(sess.turns[tn].wall, srv_wins, slack=2.0) is not None
        for a_i, (tn, cnt) in enumerate(sess.anchors):
            pend = sess.anchor_pending[a_i] if a_i < len(sess.anchor_pending) else []
            # 4차: «미출제 3개» 는 서버가 LLM 가르침 판정으로 센다 — 하네스 식별(1616 #6 미식별)로 세면 어긋난다. 큐 로그가 있으면 «그 앵커가 큐 뒤에 났나» 로 본다
            ok = (tn in cue_by_turn or _in_srv_window(tn)) if srv_cue_mode else len(pend) >= QUIZ_GROUP
            sc.period_ok &= ok
            if server_logs is None or not (cue_match and cue_match["cues"]):
                cue_s = ""          # 로그를 안 붙였거나 큐 줄이 0(T16 전) — 열을 비운다(아래 요약 줄이 이유를 말한다)
            elif tn in cue_by_turn:
                cue_s = f" · 큐→앵커 {cue_by_turn[tn][1]:.1f}s"
            elif srv_cue_mode and _in_srv_window(tn):
                cue_s = " · (서버 퀴즈 창 안 — 이어가기 문구, 새 퀴즈 아님)"
            else:
                cue_s = " · ⛔큐 없이 난 앵커"
            late = (" (서버 큐 기준 — 하네스 미출제는 참고)" if srv_cue_mode else
                    (f" (늦음 +{len(pend) - QUIZ_GROUP})" if ok and len(pend) > QUIZ_GROUP else ""))
            L.append(f"- 턴 {tn}: 드릴 {cnt} · 미출제 {len(pend)} {'✔' if ok else '✖'}{late}{cue_s}")
        if len(pend_end) >= QUIZ_GROUP:
            L.append(f"- ⚠ 끝에 미출제 {len(pend_end)}개 — 마지막 큐 뒤 통화가 끝났거나 큐 누락(서버 큐 로그로 확인)")
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
    num_of = item_numbers_by_id(parse_item_numbers(server_logs), sess.items) \
        or {int(q.get("item_id") or 0): n + 1 for n, q in enumerate(sc.cur.get("quiz_items") or [])}
    _blocks = quiz_blocks(sess.records)
    sc.order_ok, order_lines = quiz_order_check(_blocks, num_of)
    L += order_lines
    srv_sets_ts = parse_quiz_sets(server_logs) if server_logs is not None else []
    if srv_sets_ts and sess.turns and num_of:
        off = sess.turns[0].wall - sess.turns[0].t          # now 기준 초 → epoch
        for b, iids in _blocks.items():
            firsts = [rd.asked_at for i in iids for rd in sess.records[i].rounds if rd.anchored and rd.block == b]
            if not firsts:
                continue
            cand = [x for x in srv_sets_ts if x[0] is not None and x[0] <= min(firsts) + off + 3.0]
            if not cand:
                continue
            _, seq, nums = cand[-1]
            _w = {w[0]: w for w in parse_quiz_windows(server_logs)}
            _close = _w.get(seq, (None, None, None, None))[2]

            def _asked(i):
                return min(rd.asked_at for rd in sess.records[i].rounds if rd.anchored and rd.block == b)
            late = [num_of.get(i) for i in iids if _close is not None and _asked(i) + off > _close + 1.0]
            outside = [num_of.get(i) for i in iids if num_of.get(i) is not None and num_of.get(i) not in nums
                       and not (_close is not None and _asked(i) + off > _close + 1.0)]
            if late:
                L.append(f"  - 블록 {b}: 서버 seq={seq} 닫힘 뒤 질문 {late} — 세트 밖으로 세지 않음(하네스 퀴즈 모드 잔류)")
            if outside:
                sc.order_ok = False
            L.append(f"- 블록 {b} ↔ 서버 seq={seq} 세트 {nums}: " + (f"✖ 세트 밖 출제 {outside}" if outside else "✔ 세트 안"))
    for b, iids in _blocks.items():
        pend = sess.anchor_pending[b - 1] if 0 < b <= len(sess.anchor_pending) else []
        extra = [i for i in iids if i not in pend]
        missing = [i for i in pend[:QUIZ_GROUP] if i not in iids]
        if pend and (extra or missing):
            L.append(f"  - 블록 {b} 참고: 미출제였던 {[num_of.get(i, '?') for i in pend]} 중 안 낸 것 {[num_of.get(i, '?') for i in missing]} · 블록 밖 항목 {[num_of.get(i, '?') for i in extra]}")
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

    if LANGUAGE == "ja":
        # ── ja 확인 (a)~(g) ──
        L.append("## ja 확인 (a)~(g)")
        srv = [ln for ln in (server_logs or []) if "cur open_call" in ln or "cur 표현학습: lesson=" in ln]
        les_code = (sc.cur.get("lesson") or {}).get("code")
        a_ok = any(les_code and les_code in ln for ln in srv)
        L.append(f"- (a) cur open_call lesson={les_code}(ja): {'✔' if a_ok else ('— 서버 로그 없음' if server_logs is None else '✖')}" + ("" if not srv else " · " + srv[0].split("call_session:")[-1].split("curriculum_service:")[-1][:140]))
        pc = sc.cur.get("prompt_check") or {}
        L.append(f"- (b) 대본(로컬 조립): 격식 표지(です·ます) 줄 {'✔' if pc.get('formality') else '✖'} · [문형] 연습 문장 줄 {'✔' if pc.get('grammar_line') else '✖'}" + (f" · 오류 {pc.get('error')}" if pc.get("error") else f" · {pc.get('chars')}자"))
        recs = list(sess.records.values())
        p1 = [r for r in recs if r.policy in (1, 6) and r.rounds]
        p1_ok = sum(1 for r in p1 if (sc.db_rows.get(r.item.item_id) or {}).get("quiz_passed_at"))
        p3 = [r for r in recs if r.policy == 3 and r.rounds and r.item.casual]
        p3_corr = sum(1 for r in p3 if any(("교정" in tg or "칭찬+교정" in tg) for t in sess.turns for tg in t.tags if t.role == "beaver" and t.t > r.rounds[0].asked_at) )
        p3_np = sum(1 for r in p3 if not (sc.db_rows.get(r.item.item_id) or {}).get("quiz_passed_at"))
        p4 = [r for r in recs if r.policy == 4]
        p4_ok = sum(1 for r in p4 if r.drill_attempts <= 3)
        L.append(f"- (c) 판정: 정답 일본어 문장 통과 {p1_ok}/{len(p1)} · 반말(だ) 미통과 {p3_np}/{len(p3)}(격식 재요청 관측 {p3_corr}) · 오답 재시도 ≤3 {p4_ok}/{len(p4)}")
        L.append(f"- (d) 퀴즈 큐 미출제 {QUIZ_GROUP}개마다: §2 참조(앵커별 미출제 {[len(x) for x in sess.anchor_pending]})")
        L.append(f"- (e) 힌트 프레임 {len(sess.hints)}개 · reading(가나) 있는 예시 {sum(h_['reading'] for h_ in sess.hints)} — ⚠ 표현학습은 힌트 사이드카를 끈다(call_session enable_hints = call_type != 'expression') → 0 이 정상, 프리토킹에서 본다")
        ko0, ko1 = sc.cur.get("ko_before") or {}, sc.cur.get("ko_after") or {}
        ko_same = ko0.get("rows") == ko1.get("rows") and ko0.get("passed") == ko1.get("passed") and ko0.get("updated_max") == ko1.get("updated_max")
        ja_rows = sum(1 for r in sc.db_rows.values() if r.get("drilled_call_id") == sess.call_id)
        L.append(f"- (f) 종료 저장: ja cur_member_item(이 통화) {ja_rows}행 · ko 진도 무변화 {'✔' if ko_same else '✖'} (rows {ko0.get('rows')}→{ko1.get('rows')} · passed {ko0.get('passed')}→{ko1.get('passed')})")
        L.append(f"- (g) 거짓 칭찬 {sum(len(r.beaver_said_correct_after_wrong) for r in recs)}건")
        L.append("")
    passed_all = sc.judge_ok and sc.period_ok and sc.order_ok and sc.praise_ok and sc.redrill_ok
    L.insert(2, f"**결과: {'✔ 전부 기대와 일치' if passed_all else '✖ 불일치'}** — 판정 {'✔' if sc.judge_ok else '✖'} · "
                f"퀴즈주기 {'✔' if sc.period_ok else '✖'} · 퀴즈순서 {'✔' if sc.order_ok else '✖'} · 거짓칭찬 {'✔' if sc.praise_ok else '✖'} · "
                f"{('재드릴 ' + ('✔' if sc.redrill_ok else '✖') + ' · 판정기 llm' + (' · 표기 ' + ANSWER_STYLE if ANSWER_STYLE else '') + ' · ') if JUDGE_MODE == 'llm' else '판정기 string · '}"
                f"선질문 위반 {len(pre)} · 자발 산출 {sess.spontaneous}")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stamp}_{LANGUAGE + '_' if LANGUAGE != 'ko' else ''}call{cid or 'none'}.md"
    if path.exists():
        # 같은 call·같은 분(--seamless 는 조각2 보고서를 먼저 쓰고 조각1 을 나중에 쓴다) — 덮어쓰지 않는다(1598~1600 조각2 보고서가 지워졌다)
        path = path.with_name(path.stem + f"_run{run_no}.md")
    path.write_text("\n".join(L), encoding="utf-8")
    # 원자료(턴·항목 기록·DB 결과)도 남긴다 — 채점 규칙이 바뀌면 통화를 다시 걸지 않고 다시 읽을 수 있게
    raw = {
        "call_id": cid, "duration_min": duration_min, "end_reason": sess.end_reason, "errors": sess.errors,
        "anchors": sess.anchors, "anchor_pending": sess.anchor_pending, "drilled_order": sess.drilled_order,
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


def casual_ja(answer: str) -> str:
    """일본어 반말(보통형) — です→だ 만 다룬다(ます 활용은 손대지 않는다 → "" = 오답 변형). 서버 매처는 だ 를 격식 없음으로 본다(실측)."""
    a = (answer or "").strip()
    for end, rep_ in (("いです。", "い。"), ("いです", "い"), ("ですか。", "？"), ("です。", "だ。"), ("ですか", "？"), ("です", "だ")):
        if a.endswith(end):     # い형용사(いいです→いい)는 だ 를 붙이지 않는다
            return a[: -len(end)] + rep_
    return ""


def casual_for(surface: str) -> str:
    """반말형 추정. 표가 있으면 표, 없으면 어미 규칙, 그래도 없으면 ""(정책3 이 오답 변형으로 돈다)."""
    if LANGUAGE == "ja":
        return ""          # ja 는 답(예문/Xです)에서 casual_ja 로 만든다(Item 생성 뒤)
    hint = _CHUNK_HINTS.get(norm_ko(surface))
    if hint:
        return hint[0]
    t = surface.strip().rstrip("?!.")
    q = "?" if surface.strip().endswith("?") else ""
    for end, rep_ in (("이에요", "이야"), ("예요", "야"), ("어요", "어"), ("아요", "아"), ("해요", "해"), ("워요", "워"), ("돼요", "돼")):
        if t.endswith(end):
            return t[: -len(end)] + rep_ + q
    return ""   # 명사·동사 원형·문법 패턴·-세요 명령형 — 반말 함정 대신 오답 변형


# «that one»·«this thing» 같은 지시 표현이 어휘 「이」·「그」 의 키워드로 걸려 엉뚱한 항목을 짚었다(1626 t14 kw:one) — 키워드에서 뺀다
GENERIC_KEYWORDS = {"one", "ones", "this", "that", "these", "those", "it", "its", "thing", "things", "here", "there"}


def keywords_for(surface: str, en: str, kind: str) -> tuple[str, ...]:
    hint = _CHUNK_HINTS.get(norm_ko(surface))
    if hint:
        return hint[1]
    # ⚠ 쉼표·세미콜론으로 **먼저** 가른 뒤 정규화한다 — norm_en 이 쉼표를 지워 «name, title» 이 «name title» 한 덩이가 됐다(1441 명↔이름)
    parts = [norm_en(k) for k in re.split(r"[;,/]| or ", en or "")]
    kws = [k for k in parts if len(k) >= 3 and k not in ("to be", "the") and k not in GENERIC_KEYWORDS]
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
        passed_any = {r.item_id for r in mine if r.quiz_passed_at is not None}      # ② 재드릴 메트릭의 «이미 통과» 기준
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
        def _ex_str(e) -> str:
            return e if isinstance(e, str) else (str(e.get("ko") or e.get("text") or "") if isinstance(e, dict) else "")
        ex_all = tuple(x for x in (_ex_str(e) for e in (exs if isinstance(exs, list) else [])) if x)
        ex = ex_all[0] if ex_all else ""
        item = Item(it.item_id, it.surface, str(en), "" if it.kind == "grammar" else casual_for(it.surface),
                    keywords_for(it.surface, str(en), it.kind),
                    kind=it.kind, example=ex, lesson_id=lesson_id, role=role, review=review, seq=seq, examples=ex_all)
        if LANGUAGE == "ja":
            item.casual = casual_ja(item.answer)       # 정책3 격식 함정: 「私はカーラです。」→「私はカーラだ。」
        try:
            from domains.learning.service.quiz_judge import mentions as _mentions
            # 서버(cur 경로)는 표면형 매처 OR **예문 문장** 으로 문법을 알아본다(bt-back H3-③). 하네스는 문법 답으로 예문을 말하므로
            # 예문이 있으면 인식된다. 둘 다 없을 때만 «미인식».
            item.server_matchable = bool(_mentions(item.answer, item.surface, LANGUAGE)) or (item.kind == "grammar" and bool(item.example))
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
        "lesson_item_ids": lesson_ids, "drilled_before": drilled_any, "passed_before": passed_any,
    }


def count_redrills(turns: list, items: dict[int, "Item"], passed_before: set[int], passed_times: dict[int, float]) -> tuple[int, list[tuple[int, int]]]:
    """② «통과 항목 재드릴» — 비버가 **이미 quiz_passed 된** 항목을 다시 드릴/퀴즈로 꺼낸 (턴, 항목) 횟수.

    · 대조 = quiz_judge.item_mentioned(표면형 OR 예문). 없으면 표면형 변형 대조로 폴백.
    · «이미 통과» = 통화 전 통과(passed_before) 또는 이 통화에서 그 턴보다 앞서 통과(passed_times[item] < 턴 시각).
    · 제외 = 직전 학습자 턴이 그 항목을 먼저 꺼낸 경우(학습자가 말한 걸 비버가 되받은 것).
    · 같은 항목이 연속 비버 턴에 걸쳐 나오면 1회로 센다(한 드릴/퀴즈 회차 = 1).
    """
    try:
        from domains.learning.service.quiz_judge import item_mentioned as _im
    except Exception:  # noqa: BLE001
        _im = None

    def hit(text: str, it) -> bool:
        if _im is not None:
            try:
                return bool(_im(text, it.surface, getattr(it, "example", None) or None, LANGUAGE))
            except Exception:  # noqa: BLE001
                pass
        return any(has_surface(text, v) for v in getattr(it, "variants", [it.surface]))

    events: list[tuple[int, int]] = []
    last_item_by_streak: set[int] = set()
    prev_learner_text = ""
    for t in turns:
        if t.role == "learner":
            prev_learner_text = t.text
            last_item_by_streak = set()
            continue
        if t.role != "beaver" or not t.text.strip():
            continue
        for iid, it in items.items():
            already = iid in passed_before or (iid in passed_times and passed_times[iid] < t.t)
            if not already or not hit(t.text, it):
                continue
            if prev_learner_text and hit(prev_learner_text, it):
                continue                       # 학습자가 먼저 꺼냈다
            if iid in last_item_by_streak:
                continue                       # 같은 회차의 연속 턴
            events.append((t.n, iid))
            last_item_by_streak.add(iid)
    return len(events), events


def check_resume(seg1: dict, seg2: dict, *, plan_fragments: int) -> list[tuple[str, str, str, bool]]:
    """① 이어하기 검사 4항목 — (단계, 기대, 실측, PASS).
    seg1: {call_id, course, passed_ids, failed_ids, mi_updated_max}  seg2: {call_id, course_from_server, resumed, drilled_order,
          fragment_count, recorded_fragment, mi_updated_max, quizzed_ids, new_ids, review_ids}
    (c) 는 사장님 결정(2026-09-14, expr-build 2차 ②) «안 배운 것(seq) → 이번 통화 오답 → 예전 복습» — seg2 의 실제 드릴 순서에서
        새 항목(new_ids)이 전부 오답 앞에, 오답이 전부 예전 복습(review_ids − seg1 오답) 앞에 와야 한다. new/review 목록이 없으면(옛 호출)
        종전 «오답 맨 앞» 규칙으로 본다.
    plan_fragments == 1 이면 «거절(새 통화 폴백)» 이 정상이고 나머지 항목은 «해당 없음»."""
    rows: list[tuple[str, str, str, bool]] = []
    if plan_fragments <= 1:
        ok = seg2.get("resumed") is False and seg2.get("call_id") not in (None, seg1.get("call_id"))
        rows.append(("(a) 이어짐/거절", f"조각 상한 {plan_fragments} → 거절 = 새 call_id", f"seg1 {seg1.get('call_id')} → seg2 {seg2.get('call_id')} resumed={seg2.get('resumed')}", ok))
        for k in ("(b) 통과 항목 재등장 없음", "(c) 오답 순서(새 항목 뒤·복습 앞)", "(d) 조각별 저장"):
            rows.append((k, "해당 없음(거절)", "—", True))
        return rows
    same = seg2.get("resumed") is True and seg2.get("call_id") == seg1.get("call_id")
    course_ok = (seg2.get("course_from_server") in (None, seg1.get("course")))
    rows.append(("(a) 같은 call·같은 course", f"call_id {seg1.get('call_id')} · course {seg1.get('course')} · fragment_count 2",
                 f"seg2 call_id {seg2.get('call_id')} resumed={seg2.get('resumed')} · course {seg2.get('course_from_server')} · fragment_count {seg2.get('fragment_count')}",
                 bool(same and course_ok and seg2.get("fragment_count") == 2)))
    passed = set(seg1.get("passed_ids") or [])
    asked2 = list(seg2.get("drilled_order") or []) + list(seg2.get("quizzed_ids") or [])
    reappear = sorted(passed & set(asked2))
    rows.append(("(b) 통과 항목 재등장 없음", f"seg1 passed {len(passed)}개가 seg2 드릴/퀴즈에 없음", f"재등장 {len(reappear)}개 {reappear}", not reappear))
    failed = [i for i in (seg1.get("failed_ids") or []) if i not in passed]
    order = list(seg2.get("drilled_order") or [])
    if seg2.get("new_ids") is None and seg2.get("review_ids") is None:
        head = order[:max(len(failed), 1)]
        front_ok = (not failed) or set(failed) <= set(order[:len(failed) + 1])
        rows.append(("(c) 오답 항목 앞줄", f"seg1 failed {failed} 가 seg2 맨 앞", f"seg2 첫 항목들 {head}", bool(front_ok)))
    else:
        new_ids = [i for i in (seg2.get("new_ids") or []) if i not in failed]
        old_review = [i for i in (seg2.get("review_ids") or []) if i not in failed]
        pos = {iid: n for n, iid in enumerate(order)}
        p_new = [pos[i] for i in new_ids if i in pos]
        p_fail = [pos[i] for i in failed if i in pos]
        p_rev = [pos[i] for i in old_review if i in pos]
        ok_new_first = (not p_new or not p_fail) or max(p_new) < min(p_fail)
        ok_fail_before_rev = (not p_fail or not p_rev) or max(p_fail) < min(p_rev)
        ok_new_before_rev = (not p_new or not p_rev) or max(p_new) < min(p_rev)
        seq = "→".join(("N" if i in new_ids else "F" if i in failed else "R" if i in old_review else "?") for i in order) or "(드릴 0)"
        rows.append(("(c) 오답 순서(새 항목 뒤·복습 앞)",
                     f"seg2 드릴 순서 = 안 배운 것 {len(new_ids)}개 → seg1 오답 {failed} → 예전 복습 {len(old_review)}개",
                     f"실제 순서 {seq} (N=새 항목 F=오답 R=예전 복습 ?=목록 밖) · 드릴 {len(order)}",
                     bool(ok_new_first and ok_fail_before_rev and ok_new_before_rev)))
    rec_ok = seg2.get("recorded_fragment") == 2 and (seg2.get("mi_updated_max") or datetime.min) > (seg1.get("mi_updated_max") or datetime.min)
    rows.append(("(d) 조각별 저장", "cur_call.recorded_fragment=2 · cur_member_item 갱신 2회(seg1 뒤 다시 갱신)",
                 f"recorded_fragment={seg2.get('recorded_fragment')} · mi_updated seg1 {str(seg1.get('mi_updated_max'))[11:19]} → seg2 {str(seg2.get('mi_updated_max'))[11:19]}", bool(rec_ok)))
    return rows


def progress_snapshot(sf, member_id: int, language: str) -> dict:
    """(f) 한 언어의 cur 진도 스냅샷 — 행 수·max(updated_at)·passed 수. ja 통화 앞뒤로 ko 를 찍어 «무변화» 를 확인한다."""
    from sqlalchemy import select
    from domains.learning.models.curriculum import CurLesson, CurMemberItem
    with sf() as db:
        lids = [l for l in db.scalars(select(CurLesson.lesson_id).where(CurLesson.language == language)).all()]
        rows = db.scalars(select(CurMemberItem).where(CurMemberItem.member_id == member_id, CurMemberItem.lesson_id.in_(lids))).all() if lids else []
    return {"language": language, "rows": len(rows), "passed": sum(1 for r in rows if r.quiz_passed_at),
            "updated_max": max((r.updated_at for r in rows if r.updated_at), default=None)}


def local_prompt_check(ctx: dict) -> dict:
    """(b) 대본 확인 — 서버가 조립하는 지시문을 **같은 빌더**로 로컬 조립해 격식 줄(です·ます)·[문형] 연습 문장 줄이 있는지 본다."""
    try:
        from core.prompts.expression import build_expression_instruction
        dto = [{"item_id": it.item_id, "obj": it.surface, "des": it.en, "ex": it.example, "role": it.role or "must", "review": it.review}
               for it in ctx["items"].values() if not it.review]
        kw = dict(role="테스트", personality="담백하다", level_profile="초급", locale=LOCALE, interests=[], items=dto, quiz_group=QUIZ_GROUP,
                  name="John", target_language=("일본어" if LANGUAGE == "ja" else "한국어"))
        try:
            text = build_expression_instruction(language=LANGUAGE, **kw)
        except TypeError:
            text = build_expression_instruction(**kw)
        return {"ok": True, "formality": ("です" in text and "ます" in text) if LANGUAGE == "ja" else ("-요" in text or "습니다" in text),
                "grammar_line": "[문형]" in text and "연습 문장" in text, "chars": len(text)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


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
        lang_lessons = [l for l in db.scalars(select(CurLesson.lesson_id).where(CurLesson.language == LANGUAGE)).all()]
        # ⚠ 언어 스코프 — ja 리셋이 ko 진도를 지우면 안 된다(cur_member_item/lesson/call 은 lesson_id 로 언어를 안다)
        n_call = db.execute(delete(CurCall).where(CurCall.call_id.in_(my_calls), CurCall.lesson_id.in_(lang_lessons))).rowcount if my_calls else 0
        n_item = db.execute(delete(CurMemberItem).where(CurMemberItem.member_id == member_id, CurMemberItem.lesson_id.in_(lang_lessons))).rowcount
        n_les = db.execute(delete(CurMemberLesson).where(CurMemberLesson.member_id == member_id, CurMemberLesson.lesson_id.in_(lang_lessons))).rowcount
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
            "progress": f"{action} lesson_id={lesson.lesson_id}(no={lesson_no}, {LANGUAGE})", "via": "db"}


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
        sc.cur["mi_updated_max"] = max((r.updated_at for r in rows if r.updated_at), default=None)
        if call_id:
            r = db.execute(sql("SELECT call_type, status, total_time, summary, usage_engine, usage_json, usage_in_audio, usage_in_text, "
                               "usage_out_audio, usage_out_text, fragment_count FROM call WHERE call_id=:c"), {"c": call_id}).first()
            cc = db.execute(sql("SELECT lesson_id, course, recorded_fragment, lesson_completed FROM cur_call WHERE call_id=:c"), {"c": call_id}).first()
            sc.cur["cur_call"] = ({"lesson_id": cc[0], "course": cc[1], "recorded_fragment": cc[2], "lesson_completed": cc[3]} if cc else None)
            if r is not None:
                sc.call_row = {"call_type": r[0], "status": r[1], "total_time": r[2], "summary": r[3], "usage_engine": r[4],
                               "fragment_count": r[10]}
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
    try:
        sc.cur["lessons_after"] = api.lessons(level=(ctx["lesson"] or {}).get("level_no"))
    except CurApiError:
        sc.cur["lessons_after"] = []
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


def lang_of(text: str) -> str:
    """비버 턴 언어 — 대상 언어 문자 비율로. ko(=target) ≥ 0.7 · native ≤ 0.3 · 그 사이 mixed. ja 면 가나·한자 vs 라틴+한글."""
    ko = len(re.findall(_LANG_CHARS.get(LANGUAGE, r"[가-힣]"), text or ""))
    la = len(re.findall(r"[A-Za-z]" if LANGUAGE == "ko" else r"[A-Za-z가-힣]", text or ""))
    if ko + la == 0:
        return "empty"
    r = ko / (ko + la)
    return "ko" if r >= 0.7 else ("native" if r <= 0.3 else "mixed")


def freetalk_report(sess: Session, sc: Score, ctx: dict, *, duration_min: int, run_no: int, server_logs: list[str] | None,
                    out_dir: Path, expect_locked: bool, picker: Picker, sc_sf=None) -> tuple[Path, bool]:
    """프리토킹 v1 보고서 — 4열만(계획 §9): ①비버 턴별 언어·모국어 턴 수 ②모국어 턴 다음 비버 턴이 한국어인가 ③문형 이름 발화 턴 수
    ④/cur/me 전이(status·next_course·다음 차시). 판정 없음."""
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
    # 캐릭터 이름(⑤) — call_started.character_id → character.name
    try:
        from sqlalchemy import text as _sql
        with sc_sf() as _db:
            sc.cur["character_name"] = _db.execute(_sql("SELECT name FROM character WHERE character_id=:c"), {"c": sess.character_id or 0}).scalar() or ""
    except Exception:  # noqa: BLE001
        sc.cur["character_name"] = ""
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
    L.append(f"- 통화 길이 요청 {duration_min}분 · 비버 턴 {len(beav)} · 학습자 턴 {len(learner)} · 첫 비버 발화 {beav[0].t if beav else float('nan'):.1f}s · 오류 {sess.errors or '없음'}"
             + (f" · 워치독 {sess.watchdog_fires}회" if sess.watchdog_fires else ""))
    L.append(f"- DB call: {sc.call_row}")
    L.append("")
    # ① 비버 턴별 언어 · 모국어 턴 수
    langs = [(t, lang_of(t.text)) for t in beav]
    n_native = sum(1 for _, lg in langs if lg == "native")
    n_mixed = sum(1 for _, lg in langs if lg == "mixed")
    L.append("## ① 비버 턴별 언어 (한글 비율: ko ≥0.7 · native ≤0.3 · mixed)")
    L.append(f"- 비버 턴 {len(beav)} = ko {sum(1 for _, lg in langs if lg == 'ko')} · **native {n_native}** · mixed {n_mixed}")
    L.append("- " + " ".join(f"t{t.n}:{lg}" for t, lg in langs))
    # ② 모국어 턴 다음 비버 턴이 한국어인가
    L.append("")
    L.append("## ② 모국어 턴 **다음** 비버 턴이 한국어인가 (복귀 성공률)")
    back_ok = back_n = 0
    detail = []
    for i, (t, lg) in enumerate(langs):
        if lg != "native" or i + 1 >= len(langs):
            continue
        nt, nlg = langs[i + 1]
        back_n += 1
        back_ok += int(nlg == "ko")
        detail.append(f"t{t.n}(native)→t{nt.n}:{nlg}")
    L.append(f"- 모국어 턴 {back_n}개 중 다음 턴 한국어 {back_ok}개 = {(100 * back_ok / back_n):.0f}%" if back_n else "- 모국어 턴 없음(마지막 턴 제외)")
    if detail:
        L.append("- " + " · ".join(detail))
    probes = [t for t in learner if t.kind in ("ft_idk", "ft_en")]
    L.append(f"- 하네스가 모국어 턴을 유도한 학습자 턴 {len(probes)}: " + ", ".join(f"t{t.n}「{t.text}」" for t in probes))
    # ③ 문형 이름 발화
    L.append("")
    L.append("## ③ 문형 이름을 비버가 말한 턴 (기대 0)")
    gram = [it for it in sess.items.values() if it.kind == "grammar"]
    label_hits = []
    for t in beav:
        for it in gram:
            labels = [it.surface] + [p_.strip() for p_ in re.split(r"[,/]", it.surface) if len(norm_ko(p_)) >= 3]
            if any(has_surface(t.text, lb) for lb in labels):
                label_hits.append((t, it.surface))
                break
    L.append(f"- 문형 이름 발화 턴 **{len(label_hits)}** " + ("✔" if not label_hits else "— " + "; ".join(f"t{t.n}「{lb}」" for t, lb in label_hits)))
    # ④ /cur/me 전이
    L.append("")
    L.append("## ④ /cur/me 전이 — status · next_course · 다음 차시")
    pre, post = sc.cur.get("me_pre") or {}, sc.cur.get("me_post") or {}
    L.append(f"- 전: status={pre.get('status')} · next_course={pre.get('next_course')} · 차시 no={(pre.get('lesson') or {}).get('no')}")
    L.append(f"- 후: status={post.get('status')} · next_course={post.get('next_course')} · 차시 no={(post.get('lesson') or {}).get('no')}")
    if sc.cur.get("api_error"):
        L.append(f"- ⚠ API 오류: {sc.cur['api_error']}")
    try:
        lessons_after = sc.cur.get("lessons_after") or []
        st = next((l.get("status") for l in lessons_after if l.get("no") == les["no"]), None)
        if lessons_after:
            L.append(f"- /cur/lessons 차시 {les['no']} status={st} {'✔' if st == 'freetalk_done' else ''}")
    except Exception:  # noqa: BLE001
        pass
    # ⑤ 연기 — 비버가 자기 이름이 아닌 인물로 자기소개(«반 친구» 연기) — 캐릭터 이름은 DB character.name
    L.append("")
    L.append("## ⑤ 비버가 자기 이름 아닌 인물로 답한 턴 (연기 · 기대 0)")
    char_name = sc.cur.get("character_name") or ""
    intro_re = re.compile(r"(?:\bI'?m\s+([A-Z][a-z]+)\b|\bmy name is\s+([A-Z][a-z]+)|(?:저는|제 이름은|저는 이름이)\s*([가-힣A-Za-z]{2,12})\s*(?:이에요|예요|입니다|이고|라고))", re.I)
    acting = []
    for t in beav:
        for m in intro_re.finditer(t.text):
            nm = next((g for g in m.groups() if g), "")
            if nm and norm_ko(nm).lower() not in (norm_ko(char_name).lower(), "john", "testfree", "비버", "beaver") \
                    and nm.lower() not in ("sorry", "not", "here", "good", "fine", "glad", "sure", "ready", "done", "back", "gonna", "going"):
                acting.append((t, nm))
    L.append(f"- 캐릭터 이름 «{char_name or '?'}» · 다른 이름으로 자기소개한 턴 **{len(acting)}**" + ("" if not acting else " — " + "; ".join(f"t{t.n}「{nm}」" for t, nm in acting)))
    # ⑥ 턴당 문장 수 ≤2 비율
    L.append("")
    L.append("## ⑥ 턴당 문장 수 (max_sentences=2)")
    def _nsent(x: str) -> int:
        return len([q for q in re.split(r"(?<=[.!?。？！])\s+", x.strip()) if q.strip()])
    ns = [_nsent(t.text) for t in beav]
    L.append(f"- ≤2문장 턴 {sum(1 for k in ns if k <= 2)}/{len(ns)} = {(100 * sum(1 for k in ns if k <= 2) / len(ns)):.0f}% · 평균 {sum(ns) / len(ns):.1f}문장 · 3문장 이상 턴: " +
             (", ".join(f"t{t.n}({k})" for t, k in zip(beav, ns) if k > 2) or "없음") if ns else "- 비버 턴 없음")
    # ⑦ 과제 수 — 비버 질문/요청으로 끝난 턴
    L.append("")
    L.append("## ⑦ 과제 수 — 질문/요청으로 끝난 비버 턴")
    task_turns = [t for t in beav if t.text.strip().endswith(("?", "？")) or re.search(r"\b(say|tell me|ask me|try|repeat|물어보세요|말해 보세요|말해보세요|해 보세요|해보세요)\b", t.text, re.I)]
    L.append(f"- 과제 턴 {len(task_turns)}/{len(beav)} · 과제 없는 턴: " + (", ".join(f"t{t.n}" for t in beav if t not in task_turns) or "없음"))
    # ⑧ 무음 넛지 — 하네스 침묵 프로브 + 서버 로그
    L.append("")
    L.append("## ⑧ 무음 넛지 — 의도적 침묵 프로브 · 서버 로그")
    fs = sess.ft_silence
    if fs:
        if fs.get("nudge_at") is not None:
            L.append(f"- 침묵 시작 {fs['start']}s → 비버 넛지 {fs['nudge_at']}s = **{fs['nudge_at'] - fs['start']:.0f}초 뒤** · 문구 「{(fs.get('nudge_text') or '')[:160]}」")
        else:
            L.append(f"- 침묵 시작 {fs['start']}s → {sess.FT_SILENCE_MAX_S:.0f}s 안에 넛지 없음 ⛔")
    else:
        L.append("- 침묵 프로브 미실행(학습자 턴이 7개에 못 미침)")
    nudges = [ln for ln in (server_logs or []) if "무음" in ln and "넛지" in ln]
    L.append(f"- 서버 로그 넛지 {len(nudges)}줄" + ("".join("\n  - " + ln.split("call_session:")[-1][:120] for ln in nudges[:6]) if nudges else (" (--logs 없음)" if server_logs is None else " — 발동 0")))
    L.append("- ⚠ 1단/2단 시드 **문구**는 서버가 로그에 남기지 않는다 — 넛지 뒤 비버 턴(위 문구)으로 내용을 본다")
    # ⑨ 차시 소재 등장 수 — 비버·학습자 각각(항목 기준 · 변형 포함)
    L.append("")
    L.append("## ⑨ 차시 소재 등장 (항목 수 · 변형=라벨 조각·예문 포함)")
    b_items = {i for t in beav for i in surfaces_in(t.text, lesson_items)}
    l_items = {i for t in learner for i in surfaces_in(t.text, lesson_items)}
    L.append(f"- 비버 {len(b_items)}/{len(lesson_items)}: " + ", ".join(lesson_items[i].surface for i in sorted(b_items)))
    L.append(f"- 학습자(하네스 대본) {len(l_items)}/{len(lesson_items)}: " + ", ".join(lesson_items[i].surface for i in sorted(l_items)))
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
    path = out_dir / f"{stamp}_{LANGUAGE + '_' if LANGUAGE != 'ko' else ''}call{cid or 'none'}_freetalk.md"
    path.write_text("\n".join(L), encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps({
        "call_id": cid, "course": "freetalk", "locked": sess.locked, "end_reason": sess.end_reason, "lesson": les,
        "me_pre": sc.cur.get("me_pre"), "me_post": sc.cur.get("me_post"), "beaver_hits": sorted(b_hits), "learner_hits": sorted(l_hits),
        "ft_silence": sess.ft_silence, "character_name": sc.cur.get("character_name"), "watchdog_fires": sess.watchdog_fires,
        "turns": [{"n": t.n, "role": t.role, "t": round(t.t, 1), "wall": round(t.wall, 3), "text": t.text, "kind": t.kind, "stt": t.stt, "tags": t.tags} for t in sess.turns],
        "server_logs": server_logs}, ensure_ascii=False, indent=1), encoding="utf-8")
    return path, ok


def one_call(args, sf, api: CurApi, token: str, voice: Voice, picker: Picker, *, course: str, lesson_no: int,
             run_no: int, expect_locked: bool = False, continues_call_id: Optional[int] = None,
             expect_fragment: int = 1, call_kw: dict | None = None, after_call=None) -> tuple[Session, Score, Path, bool]:
    """cur 경로 통화 1회: 컨텍스트 예측 → /cur/me(전) → 통화 → 결과 읽기 → 보고서. (판정·큐 대조는 표현학습 그대로)"""
    ctx = load_cur_context(sf, MEMBER_ID, lesson_no, n=CUR_ITEMS_PER_CALL)
    me_pre = cur_status(api)
    if me_pre and (me_pre.get("lesson") or {}).get("no") not in (None, lesson_no):
        print(f"⚠ /cur/me 차시 no={(me_pre.get('lesson') or {}).get('no')} ≠ --lesson {lesson_no} — 서버 차시로 컨텍스트를 다시 읽는다")
        lesson_no = int((me_pre.get("lesson") or {}).get("no"))
        ctx = load_cur_context(sf, MEMBER_ID, lesson_no, n=CUR_ITEMS_PER_CALL)
    print(f"cur 예측: 차시 no={ctx['lesson']['no']} {ctx['lesson']['code']} · 새 항목 {len(ctx['predicted_new'])} · 복습 후보 {len(ctx['review_pool'])} · "
          f"차시 항목 {len(ctx['lesson_item_ids'])} · 회원 drilled {len(ctx['drilled_before'])}")
    ko_before = progress_snapshot(sf, MEMBER_ID, "ko") if LANGUAGE != "ko" else None
    if me_pre is not None and "language" in me_pre and me_pre.get("language") != LANGUAGE:
        print(f"⛔ /cur/me.language={me_pre.get('language')} ≠ --language {LANGUAGE} — 계정 target_language 전환이 안 됐다. 통화하지 않는다")
        raise SystemExit(3)
    started = datetime.now(timezone.utc)
    sess = asyncio.run(run_call(args.base, token, ctx["items"], voice, picker, duration_min=args.duration, probe=False,
                                verbose=args.verbose, course=course, lesson=ctx["lesson"],
                                distractors=[ctx["items"][i].surface for i in ctx["lesson_item_ids"] if i not in ctx["predicted_new"]][:8],
                                continues_call_id=continues_call_id, passed_before=ctx.get("passed_before"), **(call_kw or {})))
    ended = datetime.now(timezone.utc)
    if after_call is not None:
        after_call(sess)        # H8: 조각1 소켓이 닫히자마자 조각2 를 붙인다(결과 읽기·보고서는 그 뒤) — 앱의 «즉시 재연결» 과 같은 타이밍
    sc = read_cur_outcome(sf, api, sess.call_id, ctx, me_pre)
    if not sess.locked:
        for _ in range(12):
            if expect_fragment > 1:
                # ① 이어하기 조각: 앞 조각 저장으로 이미 «저장됨» 처럼 보인다(1564 에서 recorded_fragment=1 을 읽어 (d) 가 거짓 FAIL).
                #   cur_call.recorded_fragment 가 이 조각 번호에 닿을 때까지 기다린다(끊김 경로 저장은 수 초 뒤).
                saved = ((sc.cur.get("cur_call") or {}).get("recorded_fragment") or 0) >= expect_fragment
            else:
                saved = any(r.get("drilled_call_id") == sess.call_id for r in sc.db_rows.values()) \
                    or sc.call_row.get("status") in ("done", "analyzing")
            if saved and sc.call_row.get("total_time"):
                break
            time.sleep(3)
            sc = read_cur_outcome(sf, api, sess.call_id, ctx, me_pre)
    logs = fetch_server_logs(started, ended, args.service) if args.logs else None
    if logs is not None and JUDGE_MODE == "llm" and sess.course != "freetalk" and not sess.locked:
        # 통화 종료 1줄 «판정 사이드카» 는 저장 뒤에 찍혀 Cloud Logging 적재가 늦다(1616: 창 안인데 첫 조회에 없음) — 최대 4×15s 다시 읽는다
        for _ in range(4):
            if any("판정 사이드카" in ln for ln in logs):
                break
            time.sleep(15)
            logs = fetch_server_logs(started, ended, args.service)
    if LANGUAGE != "ko":
        from core import tts as _tts
        sc.cur["lang_check"] = {"me_language": (me_pre or {}).get("language"), "voice": _tts._resolve_voice(LANGUAGE, LEARNER_VOICE)[1],
                                "orig_target": getattr(args, "_orig_target", None)}
        sc.cur["ko_before"] = ko_before
        sc.cur["ko_after"] = progress_snapshot(sf, MEMBER_ID, "ko")
        sc.cur["prompt_check"] = local_prompt_check(ctx)
    if sess.course == "freetalk":
        path, ok = freetalk_report(sess, sc, ctx, duration_min=args.duration, run_no=run_no, server_logs=logs,
                                   out_dir=Path(args.out_dir), expect_locked=expect_locked, picker=picker, sc_sf=sf)
    else:
        path, ok = score_and_report(sess, sc, ctx["items"], duration_min=args.duration, run_no=run_no,
                                    server_logs=logs, out_dir=Path(args.out_dir))
    print(f"\n보고서: {path}")
    return sess, sc, path, ok


def scenario_lesson_cycle(args, sf, api: CurApi, token: str, voice: Voice, picker: Picker) -> int:
    rc, _rows, _ids, _sums = scenario_lesson_cycle_ex(args, sf, api, token, voice, picker)
    return rc


def _call_summary(sess: Session, sc: Score) -> str:
    if sess.course == "freetalk":
        return f"call {sess.call_id} 프리토킹 · locked={sess.locked} · 종료 {sess.end_reason}"
    rd = sc.cur.get("redrill") or {}
    return (f"call {sess.call_id} · 드릴 {len(sess.drilled_order)} · 판정 {'✔' if sc.judge_ok else '✖'} · 퀴즈주기 {'✔' if sc.period_ok else '✖'} · 퀴즈순서 {'✔' if sc.order_ok else '✖'} · 재드릴 {'✔' if sc.redrill_ok else '✖'} · "
            f"거짓칭찬 {'✔' if sc.praise_ok else '✖'} · 재드릴 {rd.get('n', '?')}/{rd.get('total_passed', '?')} · 원가 ${sc.call_row.get('cost_usd')} · {sc.call_row.get('usage_engine')}")


def scenario_lesson_cycle_ex(args, sf, api: CurApi, token: str, voice: Voice, picker: Picker) -> tuple[int, list, list, list]:
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
        return 1, rows, [], []
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
    sums = [_call_summary(sess1, sc1), _call_summary(sess2, sc2), _call_summary(sess3, sc3)]
    return (0 if all(d for _, _, _, d in rows) else 1), rows, [sess1.call_id, sess2.call_id, sess3.call_id], sums


def run_segments(args, sf, api: CurApi, token: str, voice: Voice, picker: Picker, *, lesson_no: int, n: int) -> tuple[int, Path]:
    """① 조각 이어하기: 조각1(하네스 종료 조건 그대로) → 조각2 를 continues_call_id 로. 검사 4항목을 «이어하기» 표로."""
    from domains.learning.service import call_service as _cs
    with sf() as db:
        plan_frag = int(_cs.call_fragments_for_member(db, MEMBER_ID))
        plan = _cs.effective_plan(db, MEMBER_ID)
    print(f"\n════════ 이어하기 {n}조각 · 플랜 {plan} (조각 상한 {plan_frag}) · lesson {lesson_no} ════════")
    sess1, sc1, path1, _ = one_call(args, sf, api, token, voice, picker, course=args.course, lesson_no=lesson_no, run_no=1)
    seg1 = {"call_id": sess1.call_id, "course": sess1.course_from_server or sess1.course,
            "passed_ids": [int(q.get("item_id")) for q in (sc1.cur.get("quiz_items") or []) if q.get("passed")],
            "failed_ids": [int(q.get("item_id")) for q in (sc1.cur.get("quiz_items") or []) if q.get("failed") and not q.get("passed")],
            "mi_updated_max": sc1.cur.get("mi_updated_max")}
    rows_all: list[tuple[str, str, str, bool]] = []
    paths = [path1]
    stats_rows = [beaver_stats_row("조각1", beaver_turn_stats(sess1.turns))]
    prev = seg1
    for k in range(2, n + 1):
        sess2, sc2, path2, _ = one_call(args, sf, api, token, voice, picker, course=args.course, lesson_no=lesson_no, run_no=k,
                                        continues_call_id=prev["call_id"], expect_fragment=k)
        seg2 = {"call_id": sess2.call_id, "course_from_server": sess2.course_from_server, "resumed": sess2.resumed,
                "drilled_order": list(sess2.drilled_order),
                "quizzed_ids": [iid for iid, r in sess2.records.items() if r.rounds],
                "new_ids": list(sc2.cur.get("new_ids") or []), "review_ids": list(sc2.cur.get("review_ids") or []),
                "fragment_count": sc2.call_row.get("fragment_count"),
                "recorded_fragment": (sc2.cur.get("cur_call") or {}).get("recorded_fragment"),
                "mi_updated_max": sc2.cur.get("mi_updated_max")}
        rows = check_resume(prev, seg2, plan_fragments=plan_frag)
        # 조각 k 기준 기대치: (a) fragment_count/recorded_fragment 는 k 여야 한다
        rows = [(a, b.replace("fragment_count 2", f"fragment_count {k}").replace("recorded_fragment=2", f"recorded_fragment={k}"), c,
                 (d if a not in ("(a) 같은 call·같은 course", "(d) 조각별 저장") or plan_frag <= 1 else
                  (d if k == 2 else (seg2.get("fragment_count") == k if a.startswith("(a)") else seg2.get("recorded_fragment") == k))))
                for a, b, c, d in rows]
        for a, b, c, d in rows:
            print(f"  [{'✔' if d else '✖'}] 조각{k} {a}: 기대 {b} / 실측 {c}")
        rows_all += [(f"조각{k} {a}", b, c, d) for a, b, c, d in rows]
        paths.append(path2)
        stats_rows.append(beaver_stats_row(f"조각{k}", beaver_turn_stats(sess2.turns)))
        prev = {"call_id": sess2.call_id if sess2.resumed else sess2.call_id, "course": sess2.course_from_server or sess2.course,
                "passed_ids": [int(q.get("item_id")) for q in (sc2.cur.get("quiz_items") or []) if q.get("passed")],
                "failed_ids": [int(q.get("item_id")) for q in (sc2.cur.get("quiz_items") or []) if q.get("failed") and not q.get("passed")],
                "mi_updated_max": sc2.cur.get("mi_updated_max")}
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stamp}_resume_call{sess1.call_id}.md"
    L = [f"# 이어하기 검사 — call {sess1.call_id} · {n}조각 · 플랜 {plan}(조각 상한 {plan_frag}) · lesson {lesson_no} ({stamp})", "",
         f"- 서버 {args.base} · 각 조각 --duration {args.duration}분(하네스 client_cut) · 조각 보고서: " + " · ".join(str(pp.name) for pp in paths), "",
         "| 검사 | 기대 | 실측 | 판정 |", "|---|---|---|---|"]
    L += [f"| {a} | {b} | {c} | {'PASS' if d else 'FAIL'} |" for a, b, c, d in rows_all]
    L += ["", "## 조각별 비버 턴 통계(글자수 = 공백 제외 · 반복 = 정규화 텍스트 같은 비버 턴이 잇달아 나온 최장 런)", ""] + BEAVER_STATS_HEAD + stats_rows
    path.write_text("\n".join(L), encoding="utf-8")
    print(f"\n이어하기 표: {path}")
    return (0 if all(d for _, _, _, d in rows_all) else 1), path


def raw_turn_index_check(rows: list[tuple]) -> tuple[bool, str]:
    """(f) call_raw_data turn_index 연속·중복 0. rows = [(turn_index, role), …] (DB 순서 무관)."""
    idx = [int(r[0]) for r in rows if r[0] is not None]
    if not idx:
        return False, "행 0"
    srt = sorted(idx)
    dup = len(idx) - len(set(idx))
    uniq = sorted(set(idx))
    gaps = [b for a, b in zip(uniq, uniq[1:]) if b != a + 1]
    ok = dup == 0 and not gaps and srt[0] == 0
    return ok, f"행 {len(idx)} · 범위 {srt[0]}~{srt[-1]} · 중복 {dup} · 건너뜀 {len(gaps)}" + (f"(첫 {gaps[:3]})" if gaps else "")


def seamless_checks(seg1: Session, seg2: Session, *, plan_frag: int, sc1: Score, sc2: Score, raw_rows: list[tuple],
                    silent_never: bool = False, rf1: Optional[int] = None) -> list[tuple[str, str, str, bool]]:
    """(a)~(g) 표. seg1/seg2 는 Session(조각1·2), sc* 는 결과, raw_rows 는 call_raw_data (turn_index, role)."""
    rows: list[tuple[str, str, str, bool]] = []
    # (a)
    if seg1.fragment_end_sent_at is not None and seg1.fragment_saved_at is not None:
        ms = (seg1.fragment_saved_at - seg1.fragment_end_sent_at) * 1000
        close_ms = ((seg1.ws_closed_at or seg1.fragment_saved_at) - seg1.fragment_saved_at) * 1000
        closer = "하네스 close" if seg1.reconnect_on_saved else "서버 close"
        rows.append(("(a) fragment_end→fragment_saved", "≤5000ms · 그 전 call_ended 0 · 뒤 소켓 닫힘",
                     f"{ms:.0f}ms · call_ended {'있음' if seg1.call_ended_before_saved else '0'} · {closer} +{close_ms:.0f}ms(code {seg1.ws_close_code}) · "
                     f"saved.fragment_index={ (seg1.fragment_saved or {}).get('fragment_index')}",
                     ms <= 5000 and not seg1.call_ended_before_saved and seg1.ws_closed_at is not None))
    else:
        rows.append(("(a) fragment_end→fragment_saved", "≤5000ms", f"fragment_end {'전송' if seg1.fragment_end_sent_at is not None else '미전송'} · fragment_saved 없음 · 종료 {seg1.end_reason}", False))
    # (b)
    rows.append(("(b) 재연결 call_started", f"같은 call · fragment_index=2 · max_fragments={plan_frag}",
                 f"resumed={seg2.resumed} · fragment_index={seg2.fragment_index} · max_fragments={seg2.max_fragments}",
                 bool(seg2.resumed) and seg2.fragment_index == 2 and seg2.max_fragments == plan_frag))
    # (c)
    ps = seg2.pre_speech
    rows.append(("(c) 조각2 첫 발화 전 비버 출력", f"{ps.get('watch_s', '?')}s 동안 오디오 0B · 전사 0 · turn_start 0",
                 f"오디오 {ps.get('audio_bytes')}B · 전사 {len(ps.get('transcripts') or [])}{(' ' + repr(ps['transcripts'][:2])) if ps.get('transcripts') else ''} · turn_start {ps.get('turn_starts')}",
                 (ps.get("audio_bytes") == 0 and not ps.get("transcripts") and ps.get("turn_starts") == 0) if seg2.started_at is not None else False))
    if not silent_never:
        # (d) — 육안 항목: 조각1 마지막 교환 + 조각2 첫 비버 응답을 인용(판정은 «응답이 있었다» 까지만 자동)
        first = next((t for t in seg2.turns if t.role == "beaver" and "재생" not in " ".join(t.tags) and t.text), None)
        rows.append(("(d) 조각2 첫 응답이 조각1 을 잇는가(육안)", "조각1 마지막 교환에 대한 응답(브리프 반영)",
                     (f"조각2 첫 비버 턴 @{first.t - (seg2.first_speech_at or 0):.1f}s: «{first.text[:160]}»" if first else "조각2 비버 응답 없음"),
                     first is not None))
    # (e)
    if rf1 is None:
        rf1 = ((sc1.cur.get("cur_call") or {}).get("recorded_fragment"))
    rf2 = ((sc2.cur.get("cur_call") or {}).get("recorded_fragment"))
    rows.append(("(e) 조각별 record_expression", "조각1 뒤 recorded_fragment=1 · 조각2 뒤 =2",
                 f"조각1 뒤 {rf1} · 조각2 뒤 {rf2} · call.fragment_count {sc2.call_row.get('fragment_count')}", rf1 == 1 and rf2 == 2))
    # (f)
    ok, desc = raw_turn_index_check(raw_rows)
    rows.append(("(f) call_raw_data turn_index 연속·중복 0", "0..N-1 연속", desc, ok))
    # (g)
    if silent_never:
        bt = seg2.beaver_turn_times
        n1 = bt[0][0] if bt else None
        rows.append(("(g) 무음 3단(학습자 끝내 침묵)", "첫 넛지 ≥60s(call_started 기준) → 확인 ≈+10s → 작별 ≈+12s → call_ended",
                     f"비버 턴 {[(t, x[:40]) for t, x in bt[:4]]} · 종료 {seg2.end_reason} @{(seg2.ws_closed_at or 0) - (seg2.started_at or 0):.0f}s",
                     n1 is not None and 55 <= n1 <= 90 and seg2.end_reason not in ("client_cut", "")))
    return rows


def run_seamless(args, sf, api: CurApi, token: str, voice: Voice, picker: Picker, *, lesson_no: int) -> tuple[int, Path]:
    """H8: 조각1(--segment-min 뒤 fragment_end→fragment_saved) → 즉시 silent_resume 재연결 → 조각2. (a)~(g) 표 + H6 이어하기 표."""
    from sqlalchemy import text as sql
    from domains.learning.service import call_service as _cs
    with sf() as db:
        plan_frag = int(_cs.call_fragments_for_member(db, MEMBER_ID))
        plan = _cs.effective_plan(db, MEMBER_ID)
    silent_never = bool(args.seamless_silent)
    seg_s = float(args.segment_min) * 60
    print(f"\n════════ 끊김 없는 조각 전환 · {LANGUAGE} · 플랜 {plan}(조각 상한 {plan_frag}) · lesson {lesson_no} · 전환 {args.segment_min}분 · "
          f"{'(g) 조각2 침묵' if silent_never else '조각2 관찰 ' + str(args.watch_s) + 's'} ════════")
    if plan_frag < 2:
        print("⛔ 이 플랜은 조각 1 — 이어하기가 거절된다(testfree). testmax/testpro 로.")
        raise SystemExit(3)
    cap: dict = {}

    def after_seg1(sess1: Session) -> None:
        # 조각1 소켓이 닫혔다 — 조각1 결과를 읽기 **전에** 조각2 를 붙인다(앱과 같은 «fragment_saved 직후 재연결»). (e) 용 recorded_fragment 는 지금 캡처.
        with sf() as db:
            cap["rf1"] = db.execute(sql("SELECT recorded_fragment FROM cur_call WHERE call_id=:c"), {"c": sess1.call_id}).scalar()
            it = db.execute(sql("SELECT items FROM cur_call WHERE call_id=:c"), {"c": sess1.call_id}).scalar()
            cap["items1"] = (json.loads(it) if isinstance(it, str) else (it or []))        # 조각1 판정 스냅샷(조각2 가 덮어쓰기 전)
            cap["mi1"] = db.execute(sql("SELECT max(updated_at) FROM cur_member_item WHERE member_id=:m"), {"m": MEMBER_ID}).scalar()
        if sess1.fragment_saved_at is None:
            print("⛔ 조각1 이 fragment_saved 로 끝나지 않았다 — 그래도 조각2(silent_resume) 를 시도한다")
        last_beaver = next((t.text for t in reversed(sess1.turns) if t.role == "beaver" and t.text), "")
        cap["seg2"] = one_call(args, sf, api, token, voice, picker, course=args.course, lesson_no=lesson_no, run_no=2,
                               continues_call_id=sess1.call_id, expect_fragment=2,
                               call_kw={"silent_resume": True, "silent_never": silent_never, "resume_prompt": last_beaver,
                                        "watch_s": float(args.watch_s), "cut_after_s": (150.0 if silent_never else seg_s)})

    sess1, sc1, path1, _ = one_call(args, sf, api, token, voice, picker, course=args.course, lesson_no=lesson_no, run_no=1,
                                    call_kw={"seamless": True, "switch_after_s": seg_s, "reconnect_on_saved": not args.wait_close}, after_call=after_seg1)
    sess2, sc2, path2, _ = cap["seg2"]
    items1 = cap.get("items1") or []
    seg1 = {"call_id": sess1.call_id, "course": sess1.course_from_server or sess1.course,
            "passed_ids": [int(q.get("item_id")) for q in items1 if q.get("passed")],
            "failed_ids": [int(q.get("item_id")) for q in items1 if q.get("failed") and not q.get("passed")],
            "mi_updated_max": cap.get("mi1")}
    # 재연결 지연 = 조각2 WS 연 시각 − 조각1 소켓 닫힌 시각(둘 다 perf_counter 축). 하네스 몫은 조각2 컨텍스트 예측(DB·/cur/me) 뿐.
    reconnect_delay_s = sess2.t0 - (sess1.t0 + (sess1.fragment_saved_at or sess1.ws_closed_at or 0))
    # (H6 (b)(c)(d) 의 seg1 값은 재연결 직전에 찍은 cur_call.items · cur_member_item.updated_at 스냅샷 — 조각1 결과 읽기는 조각2 뒤라 그걸 쓰면 섞인다)
    # ⚠ 조각2 첫 발화는 «조각1 마지막 비버 턴» 을 하네스가 재생해 답한 것 — 그 턴에서 잡힌 항목은 비버가 조각2 에서 다시 낸 게 아니다(1600: 「이」).
    replay_n = next((t.n for t in sess2.turns if t.role == "beaver" and any("재생" in x for x in t.tags)), None)
    replay_ids = {iid for iid, r in sess2.records.items() if replay_n is not None and r.intro_turn == replay_n}
    seg2 = {"call_id": sess2.call_id, "course_from_server": sess2.course_from_server, "resumed": sess2.resumed,
            "drilled_order": [i for i in sess2.drilled_order if i not in replay_ids],
            "quizzed_ids": [iid for iid, r in sess2.records.items() if r.rounds and iid not in replay_ids],
            "new_ids": list(sc2.cur.get("new_ids") or []), "review_ids": list(sc2.cur.get("review_ids") or []),
            "fragment_count": sc2.call_row.get("fragment_count"),
            "recorded_fragment": (sc2.cur.get("cur_call") or {}).get("recorded_fragment"),
            "mi_updated_max": sc2.cur.get("mi_updated_max")}
    h6_rows = check_resume(seg1, seg2, plan_fragments=plan_frag)
    with sf() as db:
        raw_rows = [tuple(r) for r in db.execute(sql("SELECT turn_index, role, left(content, 60) FROM call_raw_data WHERE call_id=:c ORDER BY turn_index"),
                                                 {"c": sess1.call_id}).all()]
    rows = seamless_checks(sess1, sess2, plan_frag=plan_frag, sc1=sc1, sc2=sc2, raw_rows=raw_rows, silent_never=silent_never, rf1=cap.get("rf1"))
    for a, b, c, d in rows + h6_rows:
        print(f"  [{'✔' if d else '✖'}] {a}: 기대 {b} / 실측 {c}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stamp}_{LANGUAGE + '_' if LANGUAGE != 'ko' else ''}seamless_call{sess1.call_id}.md"
    L = [f"# 끊김 없는 조각 전환(H8) — call {sess1.call_id} · 플랜 {plan}(조각 상한 {plan_frag}) · lesson {lesson_no} ({stamp})", "",
         f"- 서버 {args.base} · 조각1 전환 대기 {args.segment_min}분 뒤 «학습자 발화 → 비버 turn_end» 에서 fragment_end · 조각2 {'침묵(g)' if silent_never else f'{args.watch_s:.0f}s 관찰 뒤 학습자 발화'} · "
         f"조각 보고서: {path1.name} · {path2.name}",
         f"- 조각1 종료 {sess1.end_reason} @{sess1.ws_closed_at or 0:.1f}s · fragment_end @{sess1.fragment_end_sent_at} · fragment_saved @{sess1.fragment_saved_at} · "
         f"fragment_saved → 조각2 WS 연결 {reconnect_delay_s:.1f}s({'서버 close 대기 뒤' if args.wait_close else '즉시'} · 하네스 몫: 조각2 컨텍스트 예측) → call_started +{sess2.started_at}s · 조각2 종료 {sess2.end_reason}",
         f"- 오류: {sess1.errors + sess2.errors or '없음'}", "",
         "## (a)~(g)", "", "| 검사 | 기대 | 실측 | 판정 |", "|---|---|---|---|"]
    L += [f"| {a} | {b} | {c} | {'PASS' if d else 'FAIL'} |" for a, b, c, d in rows]
    L += ["", "## H6 이어하기 검사(재사용)", "", "| 검사 | 기대 | 실측 | 판정 |", "|---|---|---|---|"]
    L += [f"| {a} | {b} | {c} | {'PASS' if d else 'FAIL'} |" for a, b, c, d in h6_rows]
    st1, st2 = beaver_turn_stats(sess1.turns), beaver_turn_stats(sess2.turns)
    ratio = (st2["avg_chars"] / st1["avg_chars"]) if st1["avg_chars"] else 0.0
    L += ["", "## 조각별 비버 턴 통계(글자수 = 공백 제외 · 반복 = 정규화 텍스트 같은 비버 턴이 잇달아 나온 최장 런 · 재생 턴 제외)", ""]
    L += BEAVER_STATS_HEAD + [beaver_stats_row("조각1", st1), beaver_stats_row("조각2", st2),
                              f"| 조각2/조각1 평균 글자수 비 | | {ratio:.2f} | | | |"]
    print(f"  조각별 비버 턴: 조각1 평균 {st1['avg_chars']:.0f}자(최대 {st1['max_chars']}) · 조각2 평균 {st2['avg_chars']:.0f}자(최대 {st2['max_chars']}) · 비 {ratio:.2f} · "
          f"반복 최대 조각1 {st1['repeat_max']} / 조각2 {st2['repeat_max']}")
    L += ["", "## (d) 인용 — 조각1 마지막 교환 → 조각2 첫 응답", "", "```"]
    tail = [t for t in sess1.turns if t.text][-4:]
    L += [f"조각1 {'🦫' if t.role == 'beaver' else '👤'} t{t.n} @{t.t:.1f}s: {t.text}" for t in tail]
    L += ["--- fragment_end → fragment_saved → 재연결(silent_resume) ---"]
    head = [t for t in sess2.turns if t.text][:5]
    L += [f"조각2 {'🦫' if t.role == 'beaver' else '👤'} t{t.n} @{t.t:.1f}s: {t.text}" + (f"  [{' '.join(t.tags)}]" if any('재생' in x or '보류' in x for x in t.tags) else "") for t in head]
    L += ["```", "", "## call_raw_data (turn_index · role · 앞 60자)", "", "```"]
    L += [f"{r[0]:>3} {r[1]:<7} {r[2]}" for r in raw_rows]
    L += ["```"]
    if silent_never:
        L += ["", "## (g) 조각2 비버 턴(call_started 기준 초)", "", "```"] + [f"@{t:6.1f}s {x}" for t, x in sess2.beaver_turn_times] + ["```"]
    path.write_text("\n".join(L), encoding="utf-8")
    print(f"\n끊김 없는 전환 표: {path}")
    return (0 if all(d for _, _, _, d in rows + h6_rows) else 1), path


def run_matrix(args, sf, voice: Voice, picker: Picker) -> int:
    """③ testfree(2.5) → testmax(3.1) 를 **순차**로 lesson-cycle. 같은 서버·같은 DB 라 동시 금지. 한 md 로 묶는다."""
    global MEMBER_ID
    accounts = [("testfree@gmail.com", "2.5"), ("testmax@gmail.com", "3.1")]
    results: dict[str, tuple] = {}
    for email, tag in accounts:
        with sf() as db:
            MEMBER_ID = resolve_member(db, email)
        token = get_token(args.base, email, args.password)
        api = CurApi(args.base, token)
        print(f"\n╔══ 매트릭스 {tag} · {email} → member {MEMBER_ID} ══╗")
        rc, rows, ids, sums = scenario_lesson_cycle_ex(args, sf, api, token, voice, picker)
        results[tag] = (rc, rows, ids, sums, email)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = Path(args.out_dir); out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stamp}_matrix.md"
    steps = [r[0] for r in results["2.5"][1]] if results.get("2.5") else []
    L = [f"# 매트릭스 — lesson-cycle 차시 {args.lesson} · 2.5(testfree) vs 3.1(testmax) ({stamp})", "",
         f"- 서버 {args.base} · --duration {args.duration} · 순차 실행(동시 아님)", "",
         "| 단계 | 기대 | 2.5 testfree 실측 | 판정 | 3.1 testmax 실측 | 판정 |", "|---|---|---|---|---|---|"]
    r25 = {a: (b, c, d) for a, b, c, d in results["2.5"][1]} if "2.5" in results else {}
    r31 = {a: (b, c, d) for a, b, c, d in results["3.1"][1]} if "3.1" in results else {}
    for a in dict.fromkeys(steps + list(r31)):
        b = (r25.get(a) or r31.get(a) or ("", "", False))[0]
        c25 = r25.get(a); c31 = r31.get(a)
        L.append(f"| {a} | {b} | {c25[1] if c25 else '—'} | {('PASS' if c25[2] else 'FAIL') if c25 else '—'} | {c31[1] if c31 else '—'} | {('PASS' if c31[2] else 'FAIL') if c31 else '—'} |")
    L.append("")
    for tag in ("2.5", "3.1"):
        if tag in results:
            rc, rows, ids, sums, email = results[tag]
            L.append(f"## {tag} {email} — 통화 {ids} · {'PASS' if rc == 0 else 'FAIL'}")
            L += [f"- {x}" for x in sums]
            L.append("")
    path.write_text("\n".join(L), encoding="utf-8")
    print(f"\n매트릭스 표: {path}")
    return 0 if all(r[0] == 0 for r in results.values()) else 1


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
    ap.add_argument("--password", default=os.environ.get("E2E_PASSWORD") or DEFAULT_PASSWORD)
    ap.add_argument("--status", action="store_true", help="GET /cur/me")
    ap.add_argument("--fix-items", action="store_true", help="차시 고정 = POST /__dev/cur-reset {lesson_no} (옛 learning_item 고정 대체)")
    ap.add_argument("--reset", action="store_true", help="POST /__dev/cur-reset {lesson_no} — cur_member_* 삭제 + progress 를 --lesson 으로")
    ap.add_argument("--lesson", type=int, default=None, help="cur_lesson.no (기본 ko=4 A1-T01-1 30항목 · ja=1 A1-T01-1 18항목)")
    ap.add_argument("--language", choices=("ko", "ja"), default="ko",
                    help="학습 대상 언어. ja 면 시작 전 PATCH /members/me target_language=ja(끝나면 복구) · 차시·판정·리셋·학습자 음성 전부 ja")
    ap.add_argument("--course", choices=("expression", "freetalk", "auto"), default="expression",
                    help="start.call_type. auto 면 서버가 정한 코스(call_started.course)로 검증")
    ap.add_argument("--expect-locked", action="store_true", help="프리토킹이 COURSE_LOCKED 로 끊기는 것이 기대값(잠금 확인)")
    ap.add_argument("--segments", type=int, default=1, help="① 조각 이어하기: N 조각(조각2 부터 continues_call_id). Free 는 거절 확인")
    ap.add_argument("--matrix", action="store_true", help="③ testfree(2.5)+testmax(3.1) 순차 lesson-cycle → docs/e2e/*_matrix.md")
    ap.add_argument("--seamless", action="store_true",
                    help="H8 끊김 없는 조각 전환(--segments 2 와 함께): 조각1 을 --segment-min 뒤 fragment_end→fragment_saved 로 끝내고 "
                         "즉시 silent_resume 재연결 · 조각2 첫 15s 비버 출력 0 검사 → docs/e2e/*_seamless_call<id>.md")
    ap.add_argument("--seamless-silent", action="store_true", help="H8 (g): 조각2 에서 학습자가 끝내 말하지 않는다 → 무음 3단(60/10/12s) 관찰")
    ap.add_argument("--segment-min", type=float, default=2.0, help="H8: 조각1 전환 대기 시작(분) · 조각2 길이(분)")
    ap.add_argument("--watch-s", type=float, default=15.0, help="H8: 조각2 재연결 뒤 비버가 먼저 말하는지 관찰하는 시간(초)")
    ap.add_argument("--wait-close", action="store_true", help="H8: fragment_saved 뒤 서버 close 까지 기다렸다가 재연결(close 지연 실측) · 기본은 앱처럼 즉시 재연결")
    ap.add_argument("--scenario", choices=("lesson-cycle",), default=None,
                    help="lesson-cycle: reset → 표현 1통 → 표현 2통 → 프리토킹 → /cur/me 다음 차시 (PASS/FAIL 표)")
    ap.add_argument("--probe", action="store_true", help="①②③만: 첫 비버 턴 해석까지 보고 끊는다")
    ap.add_argument("--runs", type=int, default=0, help="[reset →] 통화 → 채점 을 N 회")
    ap.add_argument("--no-reset", action="store_true", help="--runs 앞의 자동 reset 생략(기본: 매 회 reset 안 함 — cur 는 차시가 이어진다; --reset-each 로 켠다)")
    ap.add_argument("--reset-each", action="store_true", help="--runs 매 회 앞에 cur-reset(옛 하네스 동작)")
    ap.add_argument("--duration", type=int, default=5, help="duration_min (서버가 3~15 로 클램프) · 도달 시 하네스가 소켓을 닫는다(client_cut)")
    ap.add_argument("--no-llm", action="store_true", help="항목 매칭 LLM 폴백 끄기")
    ap.add_argument("--answer-style", choices=("hangul", "roman", "kana"), default=None,
                    help="4차 ③: 정답을 다른 표기로 말한다 — ko: roman(로마자·영어 음성) · ja: kana(가나)·roman(헵번·영어 음성)·hangul(한글 음차·한국어 음성)")
    ap.add_argument("--offset-expect", choices=("unjudged", "passed"), default="unjudged",
                    help="서버 퀴즈 세트 밖 자발 정답의 기대 — unjudged(4차) · passed(5차 A 배포 뒤: 세트 밖 정답도 서버가 판정·기록)")
    ap.add_argument("--judge", choices=("llm", "string"), default="llm",
                    help="llm(기본·4차): 서버 LLM 판정 결과를 정본으로 ①재드릴 ②번호 순 ③표기 변형 통과 ④공개 뒤 복창 비통과 · string: 옛 문자열 판정기 기대")
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

    global LANGUAGE, ANSWER_STYLE, JUDGE_MODE, OFFSET_EXPECT
    LANGUAGE = args.language
    JUDGE_MODE = args.judge
    OFFSET_EXPECT = args.offset_expect
    ANSWER_STYLE = args.answer_style or ""
    if ANSWER_STYLE == "kana" and LANGUAGE != "ja":
        sys.exit("⛔ --answer-style kana 는 --language ja 전용이다(ko 는 roman 만 — hangul 은 ko 기본 표기)")
    if ANSWER_STYLE == "hangul" and LANGUAGE == "ko":
        print("⚠ --answer-style hangul 은 ko 의 기본 표기 — 변형 없음")
        ANSWER_STYLE = ""
    if ANSWER_STYLE and LANGUAGE == "ja":
        try:
            import pykakasi  # noqa: F401
        except ImportError:
            sys.exit("⛔ ja --answer-style 은 pykakasi 가 필요하다(conda env)")
    if args.lesson is None:
        args.lesson = 1 if LANGUAGE == "ja" else DEFAULT_LESSON_NO
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
    if not args.password:
        sys.exit("⛔ 비밀번호가 없다 — E2E_PASSWORD env 또는 --password 로 줘라(코드에 리터럴 금지).")
    token = get_token(args.base, args.email, args.password)
    api = CurApi(args.base, token)
    # ⭐ --language ja: 계정의 target_language 를 ja 로 바꾸고(통화·/cur/me 가 이 값을 읽는다) 끝나면 **원래 값으로 복구**
    run_with_target_language(api, LANGUAGE, lambda orig: (setattr(args, "_orig_target", orig), _main_body(args, sf, api, token)))


def run_with_target_language(api, language: str, body) -> None:
    """language != ko 면 PATCH /members/me target_language=language → body(원래값) → **finally 복구**(예외·SystemExit 포함). ko 면 그냥 body(None)."""
    if language == "ko":
        body(None)
        return
    try:
        orig = (api.member_me() or {}).get("target_language")
        api.patch_target_language(language)
        print(f"계정 target_language {orig!r} → {language!r} (끝나면 복구)")
    except CurApiError as exc:
        sys.exit(f"⛔ target_language 전환 실패: {exc}")
    try:
        body(orig)
    finally:
        try:
            api.patch_target_language(orig)
            print(f"계정 target_language 복구 → {orig!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"⛔ target_language 복구 실패({exc}) — 수동으로 PATCH /members/me target_language={orig!r}")


def _main_body(args, sf, api: CurApi, token: str) -> None:

    if args.status:
        cur_status(api)
    if args.fix_items or args.reset:
        if not cur_reset(api, MEMBER_ID, args.lesson, sf=sf):
            sys.exit(2)
        cur_status(api)
    if not (args.probe or args.runs or args.scenario or args.matrix or args.segments > 1 or args.seamless or args.seamless_silent):
        return

    voice = Voice()
    picker = Picker(enabled=not args.no_llm)
    if args.duration < 3:
        print("⚠ duration_min 은 서버가 3분으로 올린다(DEMO_DURATION_MIN_MINUTES=3)")

    if args.matrix:
        sys.exit(run_matrix(args, sf, voice, picker))
    if args.seamless or args.seamless_silent:
        rc, _ = run_seamless(args, sf, api, token, voice, picker, lesson_no=args.lesson)
        sys.exit(rc)
    if args.segments > 1:
        rc, _ = run_segments(args, sf, api, token, voice, picker, lesson_no=args.lesson, n=args.segments)
        sys.exit(rc)
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
