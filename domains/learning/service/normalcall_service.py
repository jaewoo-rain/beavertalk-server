"""normalcall 서비스 — 통화 DB I/O(동기) + 통화후 분석 오케스트레이션(비동기).

레이어 규율(플랜): realtime(async WS) → 이 서비스 → core 어댑터/모델. 동기 DB 함수는
`db: Session` 을 받고 명시적 commit(프로젝트 컨벤션). 분석은 gemini 호출이라 async 지만
DB 접근은 `run_db`(run_in_threadpool + 짧은 세션)로 감싼다 — 장수명 세션 점유 금지.

"무엇을 분석하는가"(프롬프트·출력 스키마)는 도메인 지식이라 여기(서비스)가 소유하고,
호출 메커니즘은 core.gemini_analysis 가 담당한다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional, TypeVar

from fastapi.concurrency import run_in_threadpool
from google import genai
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from core import curriculum_hints, gemini_analysis, storage, tts
from core.audio import (
    INPUT_SAMPLE_RATE,
    OUTPUT_SAMPLE_RATE,
    pcm16_to_mp3,
    pcm16_to_wav,
)
from core.config import Settings, settings
from core.gemini_live import DEFAULT_VOICE
from core.languages import count_target_script_chars, resolve_language
from core.persona_prompt import _LOCALE_LABEL
from domains.account.models.member import Member
from domains.account.models.member_reason import REASON_LABELS
from domains.alarm.models.alarm import Alarm
from domains.commerce.models.character import Character
from domains.commerce.models.member_character import MemberCharacter
from domains.commerce.service import entitlements
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.service import call_service
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
from domains.learning.models.sentence import Sentence
from domains.learning.repository import mastery_repository
from domains.learning.service import mastery_service
from domains.push.models.push_dispatch_log import PushDispatchLog

logger = logging.getLogger(__name__)

T = TypeVar("T")


# --------------------------------------------------------------------------- #
# async ↔ sync 브리지
# --------------------------------------------------------------------------- #
async def run_db(session_factory: sessionmaker, fn: Callable[[Session], T]) -> T:
    """별도 스레드에서 새 세션을 열어 fn(db) 실행 후 닫는다(이벤트 루프 비차단).

    fn 내부에서 명시적 commit 한다(프로젝트 컨벤션). 장수명 WS 가 세션을 오래
    점유하지 않도록 "짧게 열고 닫는" 단위로만 호출한다.
    """
    def _work() -> T:
        db = session_factory()
        try:
            return fn(db)
        finally:
            db.close()

    return await run_in_threadpool(_work)


# --------------------------------------------------------------------------- #
# 통화 준비/저장 (동기 DB)
# --------------------------------------------------------------------------- #
def _base_locale(lang: str | None) -> str:
    """모국어 식별자를 베이스 언어 코드로 정규화한다. 'ko-KR'→'ko', 'en_US'→'en', 없으면 'en'.

    (멀티랭귀지) _LOCALE_LABEL·meanings 등은 2자 코드 키라, 클라가 'ko-KR' 같은 지역 포함
    코드를 저장하면 조회 미스로 영어 폴백된다 — 한국인 학습자(도그푸딩)가 영어로 안내받는
    버그. 베이스 코드로 낮춰 'ko'→'한국어'로 잡히게 한다.
    """
    if not lang or not lang.strip():
        return "en"
    return lang.strip().replace("_", "-").split("-")[0].lower() or "en"


def character_display_name(db: Session, character_id: int) -> str | None:
    """캐릭터 표시 이름 — 통화 시작 통지에 실어 보낸다(캐스케이드 `call_started`).

    ⭐ 왜 id 만으로 부족한가: 프론트는 **이름**으로 화면 자산을 고르는데 **캐릭터 id 가
      환경마다 다르다**(prod 9=Popo / dev 3=Popo). id 만 주면 클라가 이름으로 되짚어야 하고,
      그 매핑이 틀리면 얼굴이 조용히 어긋난다.
    ⚠ 행이 없으면 None 이다 — 부르는 쪽은 그래도 id 를 보낸다(R5).
    """
    ch = db.get(Character, character_id)
    return (ch.name or None) if ch else None


def resolve_call_character(
    db: Session, member_id: int, inbound_call_id: Optional[str] = None
) -> int:
    """이 통화의 캐릭터를 **서버가 정한다**. 클라는 캐릭터를 지정하지 못한다.

    두 가지 실측 사고를 함께 막는다.

    ① **엉뚱한 캐릭터로 연결** — 앱의 `call_loading` 이 `args is int ? args : 1` 로
       폴백해, 인자를 안 넘기는 진입점(마이페이지·기록·온보딩완료)에서 항상 1(BABA)
       을 보냈다. prod 통화 701 건 중 421 건(60%)이 사용자가 고른 캐릭터가 아닌
       상대와 연결됐다(통화=1·선택=10 이 214 건).

    ② **미구매 캐릭터 통화** — 예전엔 소유 검증이 전혀 없어서 `db.get(Character, id)`
       만 했다. prod 에서 미구매 Bibi($10)로 126 건이 진행됐고, 앱을 고친 클라이언트
       라면 유료 캐릭터를 얼마든지 부를 수 있었다.

    둘 다 뿌리가 같다 — **캐릭터를 클라가 정했다**. 그래서 `start.character_id` 를
    폐기하고 서버가 두 출처에서만 읽는다:

        수신통화(알람)  inbound_call_id → push_dispatch_log → alarm.character_id
        그 외 모든 경로  member.character_id (사용자가 고른 대표 캐릭터)

    `inbound_call_id` 는 앱이 **고른 값이 아니라** 서버가 푸시로 내려준 불투명한
    uuid 다. 위조해도 얻는 게 없다: 남의 uuid 는 모르고, 알아도 그 알람 주인이
    아니면 아래에서 거절된다. 알람 캐릭터는 이미 사용자가 알람을 만들 때 고른
    것이므로 소유 검증을 따로 하지 않는다(알람 생성 시점의 책임).

    폴백 사슬(앞이 실패하면 다음으로):
        알람 캐릭터 → member.character_id(소유 확인) → 가장 싼 캐릭터

    ⚠ **거절이 아니라 폴백이다.** 통화는 사용자가 이미 마이크를 켜고 기다리는
    순간이라, 캐릭터 문제로 연결을 끊으면 "전화가 안 됨"으로 보인다. 조용히
    대표 캐릭터로 내려놓고 경고 로그를 남긴다(운영에서 집계 가능).

    Returns:
        실제로 통화에 쓸 character_id.
    """
    # ① 수신통화 — 서버가 발송할 때 남긴 로그로 알람을 되짚는다.
    if inbound_call_id:
        row = db.execute(
            select(Alarm.character_id, Alarm.member_id)
            .join(PushDispatchLog, PushDispatchLog.alarm_id == Alarm.alarm_id)
            .where(PushDispatchLog.call_id == inbound_call_id)
        ).first()
        if row is None:
            # 로그가 purge 됐거나(오래된 통화) 컬럼 추가 이전 발송.
            logger.info(
                "normalcall: inbound_call_id=%s 로 알람 못 찾음 → 대표 캐릭터",
                inbound_call_id,
            )
        elif row.member_id != member_id:
            # 남의 알람 uuid 를 들고 온 경우 — 캐릭터를 넘겨주지 않는다.
            logger.warning(
                "normalcall: 남의 알람 inbound_call_id member=%s owner=%s → 거절",
                member_id, row.member_id,
            )
        else:
            return row.character_id

    # ② 사용자가 고른 대표 캐릭터. 소유를 확인한다 — member.character_id 는
    #    ondelete=SET NULL 인 단순 FK 라 "고르기만 하고 안 산" 상태가 될 수 있다.
    #    Max 구독은 카탈로그 전체를 열어주므로 소유 없이도 통과한다(구매가 아니라 접근 —
    #    member_character 행은 만들지 않는다. 해지하면 다시 잠겨야 하기 때문).
    member = db.get(Member, member_id)
    selected = member.character_id if member else None
    if selected is not None and (
        db.get(MemberCharacter, (member_id, selected))
        or entitlements.has_all_characters(db, member_id)
    ):
        return selected

    # ③ 마지막 폴백 = 가장 싼 캐릭터(온보딩 기본 무료 캐릭터). id 를 하드코딩하지
    #    않는 이유는 IAP 상품 매핑과 같다 — 환경마다 character_id 가 다르다
    #    (prod 1,2,9,10,11 / dev 1,2,3,4,5).
    cheapest = db.scalar(
        select(Character.character_id).order_by(
            Character.price.asc().nulls_first(), Character.character_id.asc()
        ).limit(1)
    )
    logger.warning(
        "normalcall: 소유 캐릭터 없음 member=%s (대표=%s) → 기본 캐릭터 %s",
        member_id, selected, cheapest,
    )
    return int(cheapest) if cheapest is not None else 1


def _load_member_character(
    db: Session, member_id: int, character_id: int, language: str = "ko"
) -> dict:
    """회원+캐릭터 공용 조회(일반/레벨테스트 셋업의 공통 분모) — 평범한 값만 반환.

    (멀티랭귀지) korean_level 은 member_language_level[language](ko 는 member.korean_level
    dual-read 폴백) — 언어별 현재 레벨/콜드스타트를 반영한다.

    Returns:
        {role, personality, voice, locale, interests, name,
         korean_level(내부용 — 언어별), member_found(내부용)}.
    """
    member = db.get(Member, member_id)
    locale = _base_locale(member.language if member else None)
    name = (member.name if member and member.name else None)
    # 흥미·소재 = 온보딩 학습이유(member_reason) 를 사람이 읽을 한국어 라벨로.
    interests = (
        [REASON_LABELS.get(r.reason, r.reason) for r in member.reasons]
        if member else []
    )

    ch = db.get(Character, character_id)
    role = (ch.role if ch else "") or ""
    personality = (ch.personality if ch else "") or ""
    voice = (ch.voice.name if (ch and ch.voice and ch.voice.name) else DEFAULT_VOICE)

    return {
        "role": role,
        "personality": personality,
        "voice": voice,
        "locale": locale,
        "interests": interests,
        "name": name,
        "korean_level": mastery_repository.get_language_level(db, member_id, language),
        "member_found": member is not None,
    }


def load_call_setup(
    db: Session, member_id: int, character_id: int, language: str = "ko"
) -> dict:
    """통화 시작에 필요한 프롬프트 입력 + voice 를 한 번에 조회한다(LLM 0).

    Returns:
        {role, personality, voice, level_profile, locale, interests, name,
         history, needs_level_test, korean_level, study_items, known_items,
         recent_topics, promotion_notice, candidates}.
        needs_level_test=True(= korean_level 미확정)면
        call_session 이 레벨테스트로 자동 라우팅한다(D11). ORM 객체가 아니라
        평범한 값만 담아 async 컨텍스트로 안전히 넘긴다.

    P2-c2(mechanics ① 3-b~e): 체크판 통화 재료를 여기서 1회에 선별한다 —
        study_items    persona 스키마 [{slot, kind, obj, ex, des}] (공부 본편 5+예비 5)
        known_items    {grammar: [...≤40], targets: [{obj, ex, hint}]} (대화 가이드)
        recent_topics  최근 통화 요약 ≤5 (history.summaries 재활용 — 중복 화제 회피)
        promotion_notice  승급 직후 여부(⑧)
        candidates     검출 후보 ≤30 (주입 항목 injected=True + 기본 후보 병합)
    learning_item 미시드·쿼리 결과 0·korean_level 미확정(레벨테스트 예정)이면 각 키
    None(promotion 은 False) — persona 블록 미주입으로 기존 프롬프트와 동일(R5).
    """
    base = _load_member_character(db, member_id, character_id, language)
    korean_level = base.pop("korean_level")
    member_found = base.pop("member_found")

    # 레벨 미확정 → 레벨테스트 자동 라우팅 신호(D11). 아래 폴백 레벨 2 는 명시
    # call_type="normal" 등으로 일반 통화가 강행될 때만 실제 사용된다.
    # (멀티랭귀지) korean_level 은 이미 language 스코프 — needs_level_test 도 언어별.
    needs_level_test = korean_level is None
    # 레벨 미설정 폴백 = 2(Basic A). 1 은 생존 회화 — 레벨테스트가 배정하는 전용 레벨.
    level_no = korean_level if korean_level else 2

    # (멀티랭귀지) level 은 (language, level_no) 로 유일 — language 필터 필수(무필터면 다중 행).
    level = db.scalar(
        select(Level).where(Level.language == language, Level.level_no == level_no)
    )
    level_profile = (level.profile if level else "") or ""

    history = _load_history(db, member_id, language) if member_found else None

    # 체크판 재료(P2-c2) — 레벨 확정 회원만. 선별 실패는 통화를 막지 않는다(R5 폴백).
    materials = _EMPTY_MATERIALS
    if member_found and korean_level is not None:
        try:
            materials = _load_study_materials(
                db, member_id, level_no, base["locale"], language
            )
        except Exception:  # noqa: BLE001 - 재료 없이도 통화는 기존 프롬프트로 진행
            logger.exception(
                "normalcall 재료 선별 실패(무시 — 기존 프롬프트 폴백) member=%s", member_id
            )
            materials = _EMPTY_MATERIALS

    # 최근 통화 소재: 체크판 블록이 하나라도 살아있을 때만 summaries 를 재배치 —
    # 커리큘럼 미가동 회원의 프롬프트(기존 [최근 학습 이력] 블록)를 바꾸지 않는다.
    recent_topics = None
    if materials["study_items"] is not None or materials["known_items"] is not None:
        recent_topics = (history or {}).get("summaries") or None

    return {
        **base,
        "level_profile": level_profile,
        "history": history,
        "needs_level_test": needs_level_test,
        # P2.5(D16): 동적 힌트 발동 조건(normal && level 1) 판정 재료 — 원값 그대로.
        "korean_level": korean_level,
        # 언어 정책 밴드(한국어 위주 전환) — level_no(미확정 폴백 2=beginner)로 4밴드 분류.
        # persona 는 이 라벨 문자열만 받아 규칙 3 언어 정책을 고른다(어댑터 순수성).
        "lang_band": mastery_repository.band_of(level_no, language),
        **materials,
        "recent_topics": recent_topics,
    }


def load_level_test_setup(db: Session, member_id: int, character_id: int) -> dict:
    """레벨테스트 통화 셋업 — 레벨을 모르는 상태 전제라 level_profile/history 없음.

    Returns:
        {role, personality, voice, locale, interests, name} —
        build_leveltest_instruction 의 입력과 1:1.
    """
    base = _load_member_character(db, member_id, character_id)
    base.pop("korean_level")
    base.pop("member_found")
    return base


def _load_history(
    db: Session, member_id: int, language: str = "ko"
) -> dict | None:
    """최근 학습 이력(프롬프트 주입용): 최근 통화 요약 + 최근 배운 표현.

    {"summaries": [...최대 5], "expressions": [...최대 30, 중복 제거]} 또는 None(이력 없음).
    persona_prompt._history_block 이 이 형태를 기대한다.

    ⚠ **language 로 반드시 거른다.** 예전엔 member_id 로만 걸러서, 한국어를 공부하다
    일본어로 바꾼 학습자의 일본어 통화에 **한국어 요약·문장이 그대로 주입**됐다.
    비버가 "그거 기억나?" 하며 배운 적 없는 한국어를 꺼내는 원인이었다
    (실측: ja 회원의 통화 37건 중 ko 36건 → summaries 5건·expressions 14건 전부 한국어).

    체크판·힌트 선별(mastery_repository)은 처음부터 LearningItem.language 로 걸렀는데
    이 이력 주입 경로만 빠져 있었다. Call.target_language 는 NOT NULL 이라 조인 없이
    바로 조건에 넣을 수 있다.
    """
    summaries = [
        s.strip()
        for s in db.scalars(
            select(Call.summary)
            .where(
                Call.member_id == member_id,
                Call.target_language == language,
                Call.summary.is_not(None),
            )
            .order_by(Call.call_date.desc())
            .limit(5)
        ).all()
        if s and s.strip()
    ]
    # 컬럼명이 korean_sentence 지만 실제로는 **학습 대상 언어** 문장이 들어간다
    # (멀티랭귀지에서 컬럼을 재사용했다). 그래서 언어 구분은 컬럼이 아니라
    # 통화의 target_language 로만 할 수 있다.
    expr_rows = db.scalars(
        select(Sentence.korean_sentence)
        .join(Call, Sentence.call_id == Call.call_id)
        .where(
            Call.member_id == member_id,
            Call.target_language == language,
            Sentence.korean_sentence.is_not(None),
        )
        .order_by(Sentence.sentence_id.desc())
        .limit(30)
    ).all()
    expressions = list(dict.fromkeys(e.strip() for e in expr_rows if e and e.strip()))
    if not summaries and not expressions:
        return None
    return {"summaries": summaries, "expressions": expressions}


# --------------------------------------------------------------------------- #
# 통화 시작 체크판 재료 (P2-c2 — mechanics ①~③·⑧·⑨)
# --------------------------------------------------------------------------- #
# 복습 슬롯: 브리지/버벅임이면 확대(복습 70%≈본편 5 중 3), 정상이면 밴드 상한
# (초·중급 0~2, L1·고급 0~1) — mechanics ②⑨. review_slots 는 상한이며 practicing
# 재고가 부족하면 그만큼만 찬다(나머지는 신규 어휘가 채움).
_REVIEW_SLOTS_BRIDGE = 3
_REVIEW_SLOTS_BY_BAND = {"survival": 1, "beginner": 2, "intermediate": 2, "advanced": 1}

# 재료 폴백(전부 미주입) — persona 블록이 하나도 안 붙는 기존 프롬프트 동작.
_EMPTY_MATERIALS: dict = {
    "study_items": None,
    "known_items": None,
    "promotion_notice": False,
    "candidates": None,
}

_STUDY_DES_MAX_CHARS = 120  # des(참고) 꼬리 길이 상한 — 프롬프트 비대 방지


def _study_des(item: LearningItem, locale: str) -> Optional[str]:
    """공부 항목 des(참고) — 문법 explanation 우선, 어휘는 meanings JSON 의 locale 뜻."""
    if item.explanation and item.explanation.strip():
        return item.explanation.strip()[:_STUDY_DES_MAX_CHARS]
    if item.meanings:
        try:
            obj = json.loads(item.meanings)
        except (ValueError, TypeError):
            return None
        if isinstance(obj, dict):
            val = obj.get(locale) or obj.get("en") or next(iter(obj.values()), None)
            if isinstance(val, list):
                val = val[0] if val else None
            if val:
                return str(val).strip()[:_STUDY_DES_MAX_CHARS] or None
    return None


def _study_roman(item: LearningItem) -> Optional[str]:
    """학습항목의 표음(발음) 표기 — 카드 발음 줄 재료(mechanics ⑪).

    (멀티랭귀지) 표음 표기 우선순위:
      ① item.reading(전용 컬럼) — 일본어 가나·중국어 병음 등 언어별 표음. 한국어는 NULL.
      ② meanings JSON 의 "roman"(한국어 생존청크의 RR 로마자, P2.5).
    한국어는 reading=NULL 이라 종전(②만)과 동일 — 바이트 불변. 일본어는 가나가 카드에 표시된다.
    없으면 None(카드에 발음 줄 미표시).
    """
    if item.reading and item.reading.strip():
        return item.reading.strip()
    if not item.meanings:
        return None
    try:
        obj = json.loads(item.meanings)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict):
        val = obj.get("roman")
        if val:
            return str(val).strip() or None
    return None


def _study_item_dto(entry: dict, locale: str) -> dict:
    """선별 결과 1건 → persona study_items 스키마({slot, kind, obj, ex, des}).

    P2.5 확장: item_id(카드↔hint_used 상관용)·roman(RR 표기)을 함께 싣는다 —
    persona_prompt 는 아는 키만 읽으므로 프롬프트 출력은 종전과 동일(하위호환).
    """
    item: LearningItem = entry["item"]
    return {
        "slot": entry["slot"],
        "kind": entry["study_kind"],  # grammar/vocab/chunk (유형 축)
        # ⭐ 상태 축(2026-08-17) — new/again/review. persona 가 유형과 함께 읽어
        #   "통문장·다시" 로 렌더하고, 상태별 시작 절차를 고른다. 없으면 persona 가
        #   옛 렌더로 폴백한다(하위호환).
        "state": entry.get("state"),
        "obj": item.surface,
        "ex": mastery_repository.first_example(item),
        "des": _study_des(item, locale),
        "item_id": item.item_id,
        "roman": _study_roman(item),
    }


def _load_study_materials(
    db: Session, member_id: int, level_no: int, locale: str, language: str = "ko"
) -> dict:
    """체크판 통화 재료를 1회에 선별한다(mechanics ① 3-b~e — 통화 중 DB 접근 0).

    공부 10(②, 브리지/버벅임 비중 ⑨ 반영) + 대화 가이드(③ 아는 문법≤40+유도 5)
    + 승급 멘트 여부(⑧) + 검출 후보 ≤30(⑤ — 주입 injected=True + 기본 후보 병합).
    learning_item 미시드/결과 0 이면 해당 키 None(R5 — persona 블록 미주입).
    (멀티랭귀지) 선별·집계를 전부 language 로 스코프(대상 언어 커리큘럼만).
    """
    # 커리큘럼 미시드 방어 — 대상 언어 항목이 하나도 없으면 전부 기존 동작(쿼리 1회 조기 종료).
    if db.scalar(
        select(LearningItem.item_id).where(LearningItem.language == language).limit(1)
    ) is None:
        return _EMPTY_MATERIALS

    # ⑨ 복습 비중 → 복습 슬롯 수(브리지·버벅임 시 확대).
    ratio = mastery_repository.bridge_or_struggle_ratio(db, member_id, language)
    band = mastery_repository.band_of(level_no, language)
    review_slots = (
        _REVIEW_SLOTS_BRIDGE if ratio >= mastery_repository.BRIDGE_REVIEW_RATIO
        else _REVIEW_SLOTS_BY_BAND[band]
    )

    # ② 공부 로드 30 (본편 5 + 예비 25) — L1 만 총 10(전부 청크, 2026-08-16)
    picked = mastery_repository.pick_study_items(
        db, member_id, level_no, review_slots=review_slots, bridge_prev_ratio=ratio,
        language=language,
    )
    study_items = [_study_item_dto(e, locale) for e in picked] or None

    # ③ 대화 모드 가이드 — 아는 문법 soft 범위 + 유도 표현(freetalking 미션 힌트)
    grammar = mastery_repository.known_grammar(db, member_id, language)
    target_items = mastery_repository.pick_chat_targets(db, member_id, level_no, language)
    targets = []
    for it in target_items:
        ex = mastery_repository.first_example(it)
        targets.append(
            {"obj": it.surface, "ex": ex, "hint": curriculum_hints.hint_for(it.surface, ex)}
        )
    known_items = {"grammar": grammar, "targets": targets} if (grammar or targets) else None

    # ⑤ 검출 후보: 오늘 주입(공부 10 + 유도, 교집합 dedup) injected=True + 기본 후보 병합.
    injected: dict[int, LearningItem] = {}
    for e in picked:
        injected.setdefault(e["item"].item_id, e["item"])
    for it in target_items:
        injected.setdefault(it.item_id, it)
    candidates = [mastery_repository.to_candidate(i, injected=True) for i in injected.values()]
    for c in mastery_repository.load_default_candidates(db, member_id, language=language):
        if len(candidates) >= mastery_repository.CANDIDATE_CAP:
            break
        if c["item_id"] not in injected:
            candidates.append(c)
    candidates = candidates[: mastery_repository.CANDIDATE_CAP]

    return {
        "study_items": study_items,
        "known_items": known_items,
        "promotion_notice": mastery_repository.promotion_pending(db, member_id, language),  # ⑧
        "candidates": candidates or None,
    }


# ⭐⭐ 이어하기 유효시간(2026-08-19 사장님 결정). "이어서" 를 누를 수 있는 창.
#   ⚠ 짧으면 화장실 다녀온 사이 끊기고, 길면 30분 뒤 돌아와 이어서 어색해진다.
RESUME_TTL_S = 300.0
# ⭐ 이어하기 브리프에 실을 대화 발췌의 글자 예산. 프롬프트가 이미 ~5,000 토큰이라
#   여기에 통화 전체를 넣으면 조각3에서 두 배가 된다. 1,200자 ≈ 400~600토큰.
_RESUME_EXCERPT_CHARS = 1200


def resume_call(
    db: Session, member_id: int, continues_call_id: int, *, max_fragments: int,
) -> tuple[int | None, str]:
    """이어하기 요청을 검증하고 **그 통화 행을 그대로 돌려준다**(새로 만들지 않는다).

    ⭐ 조각을 새 행으로 만들지 않는 이유: 목록·분석·발음 점수·한도가 전부 `call_id`
      기준이다. 행이 하나면 **묶을 게 없다** — 15분 대화가 저절로 1건이다.
      (설계 초안의 `root_call_id` 묶기보다 싸고, 놓칠 자리가 적다.)

    ⛔ 검증 3가지. 하나라도 어긋나면 이어하지 않고 **새 통화로 떨어진다**(거절이 아니라
      폴백이다 — 이어하기가 안 된다고 통화를 막으면 그게 더 나쁘다):
        ① 본인 통화인가      — 남의 call_id 를 들고 와도 통과하면 안 된다
        ② TTL 안인가          — 마지막 조각이 끝난 지 5분 이내
        ③ 조각 상한 안인가    — Free 1 / Pro·Max 3

    Returns:
        (call_id, 사유). call_id 가 None 이면 이어하기 불가(호출부가 새 통화를 만든다).
    """
    call = db.query(Call).filter(Call.call_id == continues_call_id).first()
    if call is None:
        return None, "없는 통화"
    if call.member_id != member_id:
        # ⛔ 남의 통화에 내 발화를 이어 붙이는 것을 막는다. 로그에 남긴다(탐지용).
        return None, "본인 통화 아님"
    if (call.call_type or "normal") != "normal":
        # 레벨테스트는 조각 개념이 없다(3분 하드캡은 측정 설계다).
        return None, "일반 통화 아님"

    last = call.call_date
    if last is not None:
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        # ⚠ `call_date` 는 **시작 시각**이라 조각 길이(6분)만큼 이미 흘러 있다.
        #   TTL 은 "조각이 끝난 뒤"부터 재야 하므로 조각 길이를 얹어 준다.
        if elapsed > RESUME_TTL_S + call_service.CALL_FRAGMENT_S:
            return None, "유효시간 초과(%.0fs)" % elapsed

    used = call.fragment_count or 1
    if used >= max_fragments:
        return None, "조각 상한(%d/%d)" % (used, max_fragments)

    call.fragment_count = used + 1
    # ⚠ `call_date` 를 갱신한다 — 다음 조각의 TTL 기준이 되어야 한다. 통화 "시작" 시각이
    #   밀리지만, 이 값의 소비처는 목록 정렬과 TTL 이고 둘 다 최신이 맞다.
    call.call_date = datetime.now(timezone.utc)
    call.status = "ongoing"     # 조각1 분석이 이미 done 으로 바꿔 놨을 수 있다
    db.commit()
    return call.call_id, "조각 %d/%d" % (call.fragment_count, max_fragments)


def next_turn_index(db: Session, call_id: int) -> int:
    """이 통화에 이미 저장된 턴 수 — 이어하기에서 `turn_index` 를 **이어서** 매긴다.

    ⛔ 0 부터 다시 매기면 조각2의 첫 턴이 조각1의 첫 턴을 덮어쓰거나 순서가 섞인다.
      전사·증거가 전부 이 인덱스로 정렬된다.
    """
    return int(
        db.query(func.count(CallRawData.call_raw_data_id))
        .filter(CallRawData.call_id == call_id)
        .scalar() or 0
    )


class ResumeOut(BaseModel):
    """이어하기 요약 사이드카의 구조화 출력 — ⛔ **문장이 아니라 슬롯만** 받는다.

    ⛔ 여기에 문장 필드를 추가하지 마라. 사이드카가 문장을 만들면 그 문장이 곧 프롬프트가
      되고, "LLM 생성 0, 순수 조립"(persona_prompt 규율)이 이어하기 경로에서만 무너진다.
      `RegroundOut` 이 같은 이유로 같은 모양을 하고 있다 — 그 규율을 여기도 따른다.

    ⭐ `learner_facts` 가 이 설계의 핵심이다. 사장님 시나리오("내가 뭐 좋아한다고 했지?")는
      **문장이 아니라 사실**을 묻는 것이라 슬롯으로 정확히 표현된다. 원문을 통째로 넘기면
      비버가 그 안에서 사실을 다시 찾아야 하고, 길어질수록 못 찾는다.
    """

    topic: str = ""                    # 하던 얘기 — 짧은 명사구
    learner_facts: list[str] = []      # 학습자에 대해 알게 된 것(짧은 구)
    pending: str = ""                  # 하다 만 것 — 짧은 구


_RESUME_SUMMARY_INSTRUCTION = (
    "너는 회화 통화의 상태 요약기다. 아래 대화를 읽고 JSON 슬롯만 채워라. "
    "**문장을 만들지 마라.**\n"
    "- topic: 대화가 흐르던 화제를 짧은 명사구 하나로(최대 15자). 없으면 빈 문자열.\n"
    "- learner_facts: 이 대화에서 **학습자에 대해 알게 된 사실**을 짧은 구로(각 20자 이내, "
    "최대 5개). 예: \"김치찌개를 좋아함\". ⛔ 대화에 근거가 없으면 넣지 마라.\n"
    "- pending: 끝내지 못하고 하던 중이던 것을 짧은 구로(최대 20자). 없으면 빈 문자열."
)


def _learner_only(excerpt: str | None) -> str | None:
    """발췌에서 **학습자 발화만** 남긴다 — 비버 대사는 따라 할 대본이 된다."""
    if not excerpt:
        return None
    kept = [p for p in excerpt.split(" / ") if p.startswith("학습자:")]
    return " / ".join(kept) or None


def _resume_transcript(db: Session, call_id: int) -> str:
    """이 통화의 전사를 요약기에 넣을 한 덩어리로. ⛔ DB 접근만(async 없음 — run_db 안이다)."""
    rows = (
        db.query(CallRawData.content, CallRawData.role)
        .filter(CallRawData.call_id == call_id, CallRawData.content.isnot(None))
        .order_by(CallRawData.turn_index)
        .all()
    )
    return "\n".join(
        "%s: %s" % ("비버" if (r or "") == "beaver" else "학습자", (c or "").strip())
        for c, r in rows if c and len(c.strip()) >= 3
    )[-4000:]


def _save_resume_context(db: Session, call_id: int, slots: dict) -> None:
    """다음 조각이 쓸 요약 슬롯을 저장한다.

    ⭐⭐ **몇 턴까지 본 요약인지 같이 박는다**(`turns`). 이게 없으면 낡은 요약을 최신인 줄
      알고 쓴다 — 조각2 분석이 끝나기 전에 조각3을 이으면 **조각2 대화가 통째로 빠진다.**
      (사장님 지적 2026-08-19: "요약한 다음 뒤에 나온 내용들은 어떻게 되는 건데?")
    """
    import json as _json

    call = db.get(Call, call_id)
    if call is not None:
        payload = dict(slots)
        payload["turns"] = next_turn_index(db, call_id)
        call.resume_context = _json.dumps(payload, ensure_ascii=False)
        db.commit()


async def summarize_for_resume_text(client, model: str, tail: str) -> dict | None:
    """조각이 끝날 때 **다음 조각이 쓸 요약**을 만든다(LLM 1회).

    ⛔ 이어하기 시점에 돌리면 통화 시작이 그만큼 늦는다. 조각이 끝날 때 미리 만들어
      DB 에 두면 "이어서" 를 누른 사용자는 **지연 0** 이다.
    ⚠ 실패해도 조용히 None — 요약이 없다고 이어하기가 막히면 안 된다(호출부가 폴백한다).
    """
    if not tail:
        return None
    try:
        out = await gemini_analysis.generate_structured(
            client, model,
            system_instruction=_RESUME_SUMMARY_INSTRUCTION,
            prompt=tail, schema=ResumeOut, temperature=0.0, thinking_budget=0,
        )
    except Exception as exc:  # noqa: BLE001 — 요약 실패가 분석을 죽이면 안 된다(R5)
        logger.warning("normalcall 이어하기 요약 실패(무시) — %s", exc)
        return None
    if out is None:
        return None
    return {
        "topic": (getattr(out, "topic", "") or "").strip()[:30],
        "learner_facts": [
            f.strip()[:30] for f in (getattr(out, "learner_facts", None) or [])
            if isinstance(f, str) and f.strip()
        ][:5],
        "pending": (getattr(out, "pending", "") or "").strip()[:40],
    }


def resume_materials(db: Session, call_id: int, language: str = "ko") -> dict:
    """이어하기 브리프의 **재료**를 모은다 — ⭐ 대부분은 LLM 이 아니라 DB 가 안다.

    | 항목            | 출처 |
    |-----------------|------|
    | 이미 다룬 것    | ✅ `item_evidence`(이 통화) — 증거가 원본이다 |
    | 잘 해낸 것      | ✅ 등급 E2(유도)·E3(자발) |
    | 헷갈려 하는 것  | ✅ 등급 F(오류) |
    | 무슨 얘기 중이었나 | ⚠ 전사 마지막 턴 — LLM 없이 원문을 그대로 쓴다 |

    ⛔ LLM 에 "뭘 배웠나"를 묻지 않는다. 증거가 이미 정답을 갖고 있고, 물으면 환각이 섞인다.
      (관통 원칙 ②: 증거가 원본, 나머지는 파생 계산.)
    ⚠ `curious`(궁금해했던 것)는 지금 안 채운다 — 그건 대화에서만 나오고 LLM 이 필요하다.
      v1 은 DB 재료만으로 간다. 값어치가 확인되면 그때 사이드카를 붙인다.
    """
    from domains.learning.models.item_evidence import ItemEvidence
    from domains.learning.models.learning_item import LearningItem

    rows = (
        db.query(ItemEvidence.grade_final, LearningItem.surface)
        .join(LearningItem, LearningItem.item_id == ItemEvidence.item_id)
        .filter(ItemEvidence.call_id == call_id, ItemEvidence.language == language)
        .order_by(ItemEvidence.evidence_id)
        .all()
    )
    covered, strong, weak = [], [], []
    for grade, title in rows:
        if not title:
            continue
        if title not in covered:
            covered.append(title)
        if grade in ("E2", "E3") and title not in strong:
            strong.append(title)
        elif grade == "F" and title not in weak:
            weak.append(title)
    # ⚠ 잘 해낸 것에 들어간 항목은 '헷갈림'에서 뺀다 — 한 통화에서 틀렸다 맞혔으면
    #   결과는 맞힌 쪽이다. 둘 다 실으면 비버가 모순된 지시를 받는다.
    weak = [w for w in weak if w not in strong]

    # ⭐⭐ **대화 발췌** — 브리프의 알맹이다(2026-08-19 사장님: "저 요약은 엄청 짧잖아.
    #   최소한 어떤 이야기했는지들을 같이 넘겨야하잖아").
    #   ⛔ `call.summary` 만으로는 부족하다. 실측값이 `'Practicing goodbyes and favorite
    #     food'` — 큰 그림일 뿐 **무슨 말을 했는지가 없다.** 비버가 "내가 뭐 좋아한다고
    #     했지?"에 답하려면 그 말 자체가 있어야 한다.
    #   ⛔ 마지막 N턴만 자르는 것도 안 된다. 실측(call 1085): 학습자가 김치찌개를 말한 건
    #     t6 인데 마지막 4턴 창은 t15~t18 이다 — 비버가 t14 에서 **우연히** 되풀이해서
    #     살았다. 그 우연이 없으면 통째로 사라진다.
    #   ⇒ **최신 쪽부터 글자수 예산까지** 담는다. 예산 안에서는 오래된 것도 살아남는다.
    #
    # ⚠ 컬럼 이름은 `content` 다(`text` 가 아니다 — 이걸 틀려 브리프가 통째로 실패했고
    #   비버가 조각2에서 다시 인사했다).
    rows = (
        db.query(CallRawData.content, CallRawData.role)
        .filter(CallRawData.call_id == call_id, CallRawData.content.isnot(None))
        .order_by(CallRawData.turn_index.desc())
        .all()
    )
    excerpt, used = [], 0
    for content, role in rows:
        line = (content or "").strip()
        # ⛔ 빈 턴(무음)과 **잘린 꼬리**를 거른다. 끊는 순간 비버가 말하다 만 조각
        #   ("I'm sorry,")을 "하던 얘기"로 주면 비버가 그 상황을 이어받으려 한다.
        if len(line) < 3:
            continue
        who = "나" if (role or "") == "beaver" else "학습자"
        piece = "%s: %s" % (who, line[:160])
        if used + len(piece) > _RESUME_EXCERPT_CHARS:
            break
        excerpt.append(piece)
        used += len(piece)
    excerpt.reverse()          # 시간 순으로 되돌린다(읽는 순서가 대화 순서여야 한다)
    topic = " / ".join(excerpt) or None

    # ⭐ 학습자가 **실제로 한 말** — "내가 뭐 좋아한다고 했지?" 류에 가장 직접적인 재료다.
    #   통화후 분석이 뽑아 둔 것이라 추가 비용이 0이다.
    said = [
        r[0].strip() for r in
        db.query(Sentence.korean_sentence)
        .filter(Sentence.call_id == call_id, Sentence.korean_sentence.isnot(None))
        .order_by(Sentence.sentence_id).limit(8).all()
        if r[0] and r[0].strip()
    ]
    # ⭐⭐ **1순위: 조각 종료 때 만들어 둔 슬롯**(topic·learner_facts·pending).
    #   원문을 그대로 주는 것보다 낫다 — 비버가 사실을 다시 찾을 필요가 없다.
    #   ⚠ 분석이 fire-and-forget 이라 조각1→2 에서는 아직 없을 수 있다(실측: 요약 완성이
    #     이어하기보다 1초 늦었다). 그때만 아래 발췌 폴백으로 내려간다.
    slots = {}
    raw_ctx = db.query(Call.resume_context).filter(Call.call_id == call_id).scalar()
    if raw_ctx:
        try:
            import json as _json

            slots = _json.loads(raw_ctx) or {}
        except (ValueError, TypeError):
            slots = {}
        # ⛔⛔ **낡은 요약을 최신인 줄 알고 쓰면 안 된다.** 요약은 조각이 끝날 때
        #   fire-and-forget 으로 만들어지므로, 조각2 분석이 끝나기 전에 조각3을 이으면
        #   저장된 것은 **조각1까지만 본 요약**이다 — 그대로 쓰면 조각2가 통째로 빠진다.
        #   ⇒ 만든 시점의 턴 수와 지금 턴 수를 비교해 **뒤처지면 버린다.**
        #     버리면 호출부가 즉석 생성으로 내려가 최신으로 다시 만든다.
        #   ⚠ `turns` 가 없는 것은 이 필드 도입 **전에** 저장된 요약이다. 최신인지 알 수
        #     없으므로 낡은 것으로 취급한다(모르면 다시 만드는 편이 안전하다).
        seen = slots.get("turns")
        now_turns = next_turn_index(db, call_id)
        if not isinstance(seen, int) or seen < now_turns:
            logger.info(
                "normalcall 이어하기 요약: 낡음(%s턴까지 → 지금 %d턴) — 다시 만든다 call_id=%s",
                seen, now_turns, call_id,
            )
            slots = {}

    # ⭐ 한 줄 요약(LLM). 짧지만 **오래된 화제까지** 담는 유일한 값이라 같이 준다.
    #   ⚠ 분석이 fire-and-forget 이라 조각1→2 에서는 아직 없을 수 있다(실측: 요약 완성이
    #     이어하기보다 1초 늦었다). 없으면 발췌만으로 간다 — 그래서 둘 다 넘긴다.
    summary = (
        db.query(Call.summary).filter(Call.call_id == call_id).scalar() or ""
    ).strip() or None
    # ⛔ **슬롯이 있으면 발췌를 안 보낸다.** 둘 다 보내면 프롬프트가 두 배가 되고,
    #   요약본과 원문이 어긋날 때 비버가 어느 쪽을 믿을지 모른다.
    return {
        "covered": covered[:12], "strong": strong[:6], "weak": weak[:6],
        "topic": slots.get("topic") or None,
        "pending": slots.get("pending") or None,
        "facts": slots.get("learner_facts") or None,
        # ⛔⛔ **폴백 발췌에서 비버 발화를 뺀다**(2026-08-19 실측). 비버의 첫 인사가 발췌
        #   맨 앞에 오자 비버가 그걸 **대본으로 읽어 글자까지 똑같이 다시 인사했다.**
        #   학습자 발화만 남기면 따라 할 대본이 없다.
        #   ⚠ 애초에 이 폴백은 호출부가 즉석 요약으로 대체한다 — 여기 남은 건 그것마저
        #     실패했을 때의 마지막 그물이다.
        "excerpt": None if slots else _learner_only(topic),
        "said": said, "summary": summary, "curious": None,
    }


def create_call(
    db: Session, member_id: int, character_id: int, call_type: str = "normal",
    *, target_language: str = "ko",
) -> int:
    """통화 행을 생성하고(status=ongoing) call_id 를 반환한다.

    call_type: "normal"(기본) | "level_test" — 콜타입 라우팅 결과(call_session 결정).
    target_language: 이 통화의 학습 대상 언어코드(멀티랭귀지, 기본 'ko') — 커리큘럼 선별·
        증거/이력 집계 스코프. call_session 이 resolve 한 LanguageSpec.code 를 넘긴다.
    """
    call = Call(
        member_id=member_id,
        character_id=character_id,
        call_date=datetime.now(timezone.utc),
        status="ongoing",
        call_type=call_type,
        target_language=target_language,
    )
    db.add(call)
    db.commit()
    db.refresh(call)
    return call.call_id


def _upload_segment_pcm(
    call_id: int, member_id: int, turn_index: int, role: str, pcm: bytes
) -> str | None:
    """세그먼트 PCM 을 MP3(ffmpeg 없으면 WAV 폴백)로 변환해 private 버킷에 업로드.

    Returns:
        storage key. storage 미설정/업로드 실패면 None(호출부가 voice_url=None 유지 — R5).
    """
    sr = INPUT_SAMPLE_RATE if role == "user" else OUTPUT_SAMPLE_RATE
    base = f"calls/{member_id}/{call_id}/{turn_index:04d}_{role}"
    # 표준 MP3 로 저장(어디서든 재생). ffmpeg 없으면 WAV 로 폴백.
    mp3 = pcm16_to_mp3(bytes(pcm), sample_rate=sr)
    if mp3 is not None:
        return storage.upload(
            settings.SUPABASE_BUCKET_RECORDINGS, base + ".mp3", mp3, "audio/mpeg"
        )
    wav = pcm16_to_wav(bytes(pcm), sample_rate=sr)
    return storage.upload(
        settings.SUPABASE_BUCKET_RECORDINGS, base + ".wav", wav, "audio/wav"
    )


def save_segments(
    db: Session,
    call_id: int,
    segments: list[dict],
    member_id: int,
    *,
    upload_audio: bool = True,
) -> int | list[dict]:
    """턴 세그먼트를 CallRawData 로 저장한다(음성은 private 버킷 업로드, key 보관).

    segments: [{turn_index, role('user'|'beaver'), text, pcm}]. 빈 세그먼트는 건너뛴다.
    storage 미설정이면 voice_url=None(전사만 저장). 부분 실패해도 가능한 만큼 저장.

    upload_audio(P2.6 — 결과 페이지 체감 속도):
        True(기본, 통화중 점진 flush): 종전 그대로 — 변환·업로드 동기 수행 후
            voice_url 까지 채워 커밋. 저장 행 수(int) 반환.
        False(통화 종료 최종 persist): 오디오 변환·업로드(~9s)를 건너뛰고 **텍스트
            행을 먼저 커밋**(voice_url=None) — 분석이 즉시 시작 가능. 이후
            upload_segment_audio 에 넘길 pending 목록
            [{call_raw_data_id, turn_index, role, pcm}] 반환(pcm 없는 행 제외).
    """
    rows: list[tuple[CallRawData, dict]] = []
    for seg in segments:
        pcm = seg.get("pcm") or b""
        key = None
        if pcm and upload_audio:
            key = _upload_segment_pcm(
                call_id, member_id, seg["turn_index"], seg["role"], bytes(pcm)
            )
        row = CallRawData(
            call_id=call_id,
            role=seg["role"],
            turn_index=seg["turn_index"],
            content=(seg.get("text") or None),
            voice_url=key,
        )
        db.add(row)
        rows.append((row, seg))
    db.flush()  # PK(call_raw_data_id) 확보 — commit 후 expire 재조회 없이 수집
    pending = [
        {
            "call_raw_data_id": row.call_raw_data_id,
            "turn_index": seg["turn_index"],
            "role": seg["role"],
            "pcm": bytes(seg.get("pcm") or b""),
        }
        for row, seg in rows
        if (seg.get("pcm") or b"")
    ]
    db.commit()
    if upload_audio:
        return len(rows)
    return pending


def upload_segment_audio(
    db: Session, call_id: int, member_id: int, pending: list[dict]
) -> int:
    """선저장(텍스트만) 세그먼트의 오디오를 후행 업로드해 voice_url 을 채운다(P2.6).

    pending: save_segments(upload_audio=False) 반환 목록. 통화 종료 후 별도 백그라운드
    태스크에서 실행 — 분석 파이프라인과 병렬. 행 단위 graceful: 변환·업로드 실패는
    그 행만 건너뛴다(voice_url None 유지 — R5). 업로드 성공 행 수 반환.
    """
    done = 0
    for p in pending:
        try:
            key = _upload_segment_pcm(
                call_id, member_id, p["turn_index"], p["role"], p["pcm"]
            )
        except Exception:  # noqa: BLE001 - 행 단위 흡수(다른 행은 계속)
            logger.exception(
                "normalcall 오디오 후행 업로드 실패(무시 — voice_url None 유지) "
                "call_id=%s row=%s", call_id, p["call_raw_data_id"],
            )
            continue
        if not key:
            continue
        row = db.get(CallRawData, p["call_raw_data_id"])
        if row is not None:
            row.voice_url = key
            done += 1
    db.commit()
    return done


def finalize_call(db: Session, call_id: int, *, total_time: int, status: str) -> None:
    """통화 종료 메타(총 시간/상태)를 갱신한다."""
    call = db.get(Call, call_id)
    if call is None:
        return
    call.total_time = total_time
    call.status = status
    db.commit()


# ── 원가 계기판 2단계: usage 영속화 + 원가 산식 ─────────────────────────────
# Live 토큰 단가(USD / 1M 토큰). Vertex·AI Studio 동일(2026-08 기준).
# ⛔ 이 표를 DB 에 넣지 마라. 단가는 벤더가 바꾸는 값이고, 통화 행에 달러를 박아 두면
#   단가가 바뀐 순간 과거와 현재를 같은 잣대로 못 본다. 토큰은 사실이고 원가는 파생이다.
#   나중에 회계(실청구액 대사)가 필요해지면 여기에 적용일자를 붙여 확장한다.
LIVE_TOKEN_PRICE_USD = {
    "in_audio": 3.00,
    "in_text": 0.50,
    "out_audio": 12.00,
    "out_text": 2.00,
}


def estimate_usage_cost_usd(
    *, in_audio: int = 0, in_text: int = 0, out_audio: int = 0, out_text: int = 0
) -> float:
    """**Live 전용** — 모달리티 4항 × Live 단가 = 통화 원가(USD).

    4항을 나눠 두는 이유가 여기 있다 — 단가가 최대 24배까지 차이난다(텍스트 입력 $0.5 vs
    오디오 출력 $12). 합쳐 놓은 숫자로는 원가를 계산할 수가 없다.

    ⛔ 캐스케이드 행에 이 함수를 쓰지 마라. 캐스케이드는 같은 `usage_in_text` 컬럼에
      **LLM 토큰**을 담는데 단가가 다르다($0.30 vs Live $0.50) — 조용히 틀린 값이 나오고,
      하필 그 값이 "캐스케이드가 싼가"의 근거로 쓰인다. 엔진이 섞인 데이터에는
      estimate_call_cost_usd(engine=...) 를 써라.

    ⚠ **사고(thinking) 토큰은 여기 안 들어간다 — 확신이 없어서 일부러 뺐다.**
      캐스케이드 쪽은 out_text + thoughts 로 센다(사고 토큰이 출력 단가로 과금되므로).
      Live 도 같은 논리로 보이지만, 더하기 전에 확인이 안 된 게 둘이다:
        ① 이중계상 위험이 클라이언트마다 다르다. AI Studio 는 candidates 에 사고 토큰이
           **포함**돼 나오고 Vertex 는 **빠진다**. 이 앱은 Vertex 지만(USE_VERTEX), 원가는
           모달리티 분해(response_tokens_details)로 계산하는데 그 분해가 사고 토큰을
           품는지는 문서에서 확인하지 못했다. 품는다면 더하는 순간 이중계상이다.
        ② 애초에 이 모델은 사고를 안 한다. GEMINI_LIVE_MODEL 이
           'gemini-live-2.5-flash-native-audio'(비-사고 대화형)이고, 사고형은
           '...-native-audio-thinking-dialog' 라는 **다른 모델 id** 다.
           실측 call 909 도 sum_thoughts=0 이었다.
      즉 지금 더해도 값이 안 변하고, 틀리면 조용히 과대계상이 된다 — 그래서 안 더한다.
      대신 sum_thoughts>0 인 Live 통화가 나오면 call_session 이 **경고를 찍는다**(이 판단이
      낡았다는 신호). 그 로그가 보이면 ①을 실측으로 확인하고 여기 산식을 고쳐라.
    """
    p = LIVE_TOKEN_PRICE_USD
    return (
        in_audio * p["in_audio"] + in_text * p["in_text"]
        + out_audio * p["out_audio"] + out_text * p["out_text"]
    ) / 1_000_000


# ── 원가 계기판 3단계: 엔진 구분 + 캐스케이드 단가 ───────────────────────────
# 🧒 왜 엔진 이름을 남기나: Live 통화와 캐스케이드 통화가 같은 테이블·같은 컬럼에 섞이면
#   AVG(원가) 가 두 엔진의 평균이 돼 버려, **"캐스케이드가 정말 싼가"를 증명할 수 없다.**
#   그게 캐스케이드 프로젝트의 유일한 목적인데. 나중에 백필할 근거도 안 남는다.
#
# ⛔ 이 문자열들은 cascade-impl 과 공유하는 **계약**이다. 임의로 바꾸지 마라 —
#   한쪽만 바꾸면 두 엔진의 행이 서로 다른 이름으로 쌓여 비교가 깨진다.
ENGINE_LIVE_GEMINI = "live:gemini-native-audio"
ENGINE_LIVE_OPENAI = "live:openai-realtime"


def build_engine_tag(mode: str, *components: str) -> str:
    """엔진 태그를 계약 형식('<모드>:<구성요소를 + 로 연결>')으로 조립한다.

    >>> build_engine_tag("cascade", "google-stt-v2", "gemini-2.5-flash", "cloud-tts-chirp3-hd")
    'cascade:google-stt-v2+gemini-2.5-flash+cloud-tts-chirp3-hd'

    STT/TTS 조합까지 문자열에 박아 두는 게 요점이다 — 나중에 Whisper 로 바꿔도 스키마 변경
    없이 **같은 컬럼에서 갈라진다**. 빈 구성요소는 무시한다(폴백으로 한 다리가 빠진 경우).
    """
    parts = [c.strip() for c in components if c and c.strip()]
    return f"{mode}:{'+'.join(parts)}"


# 캐스케이드 구성요소 단가(2026-08-07 조사).
# ⚠ 공식 가격 페이지는 표가 JS 로 그려져 본문 추출이 안 됐다 — 아래는 검색 결과 요약 기준이다.
#   **과금 판단(플랜 가격 결정 등) 전에는 반드시 콘솔 청구서로 재확인해라.** 계기판 추이를
#   보는 용도로는 충분하지만, 이 숫자로 가격을 정하면 안 된다.
# ⛔ 무료 한도(TTS Chirp3 HD 월 100만 자, STT 월 60분 등)는 **일부러 반영하지 않았다.**
#   한도는 프로젝트 전체에 걸쳐 소진되는 값이라 통화 1건에 배분할 수가 없다. 여기 값은
#   "한도를 다 쓴 뒤의 한계원가"이고, 그게 증설 판단에 필요한 숫자다.
STT_PRICE_USD_PER_S = {
    "google-stt-v2": 0.016 / 60,          # 실시간/스트리밍 표준가 $0.016/분(대량 시 $0.004까지)
    "openai-whisper": 0.006 / 60,         # whisper-1 정가 $0.006/분, 볼륨 할인 없음
    "gpt-4o-mini-transcribe": 0.003 / 60,  # 참고용
    # ⭐ 실시간 전사(Realtime API). 구글의 **1/5.3** — 통화 원가 $0.44 → 약 $0.26.
    # ⚠ **침묵 과금 여부 미확인.** 우리는 마이크 상시개방이라 침묵도 흘린다. 벤더가 발화만
    #   과금한다면 이 계산은 **과대**다(과소보다 안전한 방향이라 그대로 둔다). 청구서로 확인해라.
    "openai-gpt-4o-mini-transcribe": 0.003 / 60,
    "openai-gpt-4o-transcribe": 0.006 / 60,
}

# ⛔ **OpenAI TTS(`gpt-4o-mini-tts`)는 단가표에 넣지 않았다.** 1차 자료(pricing)가
#   "Audio ... $12.00 / 1M **tokens**" 라 과금 단위가 **오디오 토큰**인데, **초→토큰 환산율이
#   문서에 없다**(Gemini 는 1초=25tok 을 확인했지만 그 값을 남의 벤더에 쓰면 그건 추측이다).
#   ⇒ 지금은 `unknown` 으로 드러난다(`tts:openai-gpt-4o-mini-tts`). 조용히 0원이 되는 것보다
#     "모른다"가 낫다 — 274044a 가 고친 게 정확히 그 종류의 결함이다.
#   ⭐ `audio_s` 는 이미 모으고 있다. 환산율이 확인되면 **여기 한 줄**만 추가하면 계산된다.

# ── TTS 는 **과금 단위가 두 종류**다. 섞으면 조용히 틀린다 ────────────────────
# 클래식 TTS(Chirp3-HD 등) = **문자 수** 과금. Gemini-TTS = **출력 오디오 토큰** 과금.
# ⛔ 문자 수에 토큰 단가를 곱하지 마라(그 반대도). 아래 두 표를 벤더로 갈라 쓴다.
TTS_PRICE_USD_PER_CHAR = {
    "cloud-tts-chirp3-hd": 30.0 / 1_000_000,   # $30/1M 자
    "cloud-tts-neural2": 16.0 / 1_000_000,
    "cloud-tts-wavenet": 16.0 / 1_000_000,
    "cloud-tts-standard": 4.0 / 1_000_000,
}

# Gemini-TTS: 출력 오디오 1초 = **정확히 25 토큰**. 그래서 원가가 문자 수가 아니라
# **합성된 오디오 길이**에 붙는다 — 같은 문장이라도 천천히 읽으면 더 비싸다.
# ⛔ 그래서 이 엔진들은 tts.chars 로 계산할 수 없다. tts.audio_s 가 없으면
#   추정하지 않고 **미상으로 드러낸다**(0 원으로 먹으면 "캐스케이드가 공짜"가 된다).
GEMINI_TTS_TOKENS_PER_AUDIO_S = 25

# USD / 1M 토큰. in=입력 텍스트, out=출력 오디오.
#
# ⛔ **같은 모델인데 API 마다 이름이 다르다. 이 표에 두 이름 체계가 섞여 있다 — 통일하지 마라.**
#   Cloud TTS(우리가 호출하는 쪽)      : gemini-2.5-flash-tts, gemini-2.5-pro-tts …
#   Gemini API(ai.google.dev, 가격 출처): gemini-2.5-flash-preview-tts, …-pro-preview-tts …
#   `vendors.tts.vendor` 로 **실제 들어오는 건 Cloud TTS 이름**이다(cascade 가 Cloud TTS 의
#   voice.model_name 을 그대로 넣는다). Gemini API 이름은 **가격 출처일 뿐** 우리가 받는
#   이름이 아니다 — 그래도 지우지 않는다(다른 경로로 올 수 있다).
#   🧒 왜 이 경고가 여기 있나: 가격 페이지 이름만 남기면 **동작은 하는데 값이 안 잡힌다**.
#     예외도 로그도 없이 전부 '미상 벤더'가 되고, 그러면 캐스케이드 통화가 원가 표본에서
#     통째로 사라진다. 오늘 LINEAR16(비스트리밍엔 유효·스트리밍엔 무효)과 같은 종류의 함정이다.
#     "이름을 통일하자"는 정리는 **원가 계측을 조용히 죽인다.**
#
# ✅ 단가는 2026-08-07 **공식 가격표 본문에서 확인**(ai.google.dev/gemini-api/docs/pricing) —
#   이 프로젝트에서 가격을 원문으로 확인한 첫 사례다. 나머지 단가표는 검색 요약 기준이다.
TTS_TOKEN_PRICE_USD_PER_1M = {
    # ── Cloud TTS 이름(실제로 들어오는 키) ──
    "gemini-2.5-flash-tts":            {"in_text": 0.50, "out_audio": 10.00},
    "gemini-2.5-pro-tts":              {"in_text": 1.00, "out_audio": 20.00},
    "gemini-3.1-flash-tts-preview":    {"in_text": 1.00, "out_audio": 20.00},
    # ── Gemini API 이름(가격 출처. 우리 경로로는 안 오지만 남겨둔다) ──
    "gemini-2.5-flash-preview-tts":    {"in_text": 0.50, "out_audio": 10.00},
    "gemini-2.5-pro-preview-tts":      {"in_text": 1.00, "out_audio": 20.00},
    # ⚠ **미확인 — flash 단가를 보수적으로 차용했다.** 공식 가격표에 lite TTS 행이 없다
    #   (텍스트 모델의 lite 는 flash 의 1/3 이라 실제론 더 쌀 가능성이 크다).
    #   과소 계상이 과대 계상보다 위험해서(캐스케이드가 싸 보인다) 비싼 쪽으로 뒀다.
    #   ⛔ 이 줄의 숫자로 플랜 가격을 정하지 마라. 청구서로 확인되면 고쳐라.
    #   앞의 것이 Cloud TTS 이름(실제 키), 뒤는 있을 법한 GA 표기 대비.
    "gemini-2.5-flash-lite-preview-tts": {"in_text": 0.50, "out_audio": 10.00},
    "gemini-2.5-flash-lite-tts":         {"in_text": 0.50, "out_audio": 10.00},
}

# cascade 가 Cloud TTS 로 지정할 수 있는 model_name 전량(공식 문서 확인).
# 회귀 테스트가 이 목록을 그대로 돌며 "하나도 미상으로 안 빠지는지"를 지킨다 —
# 새 모델이 붙으면 여기에 한 줄 추가하는 것만으로 누락이 드러난다.
CLOUD_TTS_GEMINI_MODELS = (
    "gemini-2.5-flash-tts",
    "gemini-2.5-flash-lite-preview-tts",
    "gemini-2.5-pro-tts",
    "gemini-3.1-flash-tts-preview",
)

# LLM 단가(USD / 1M 토큰). 캐스케이드의 LLM 다리는 **텍스트만** 받는다(오디오는 STT 가 처리).
LLM_TOKEN_PRICE_USD = {
    "gemini-2.5-flash": {"in_text": 0.30, "out_text": 2.50},
    "gemini-2.5-flash-lite": {"in_text": 0.10, "out_text": 0.40},  # 2026-10-16 은퇴 예고
}


def _llm_tokens_cost_usd(entry: dict | None) -> tuple[float, str | None]:
    """LLM 토큰 묶음 1건의 원가. 반환 (원가, **모르는 벤더 이름 또는 None**).

    ⛔ LLM 토큰 원가식은 여기 하나뿐이다. 캐스케이드의 LLM 다리도, 통화중 사이드카도,
      통화후 분석도 전부 이 함수를 탄다 — 같은 계산을 두 벌 두면 한쪽만 고쳐지고
      두 숫자가 조용히 갈라진다.
    ⛔ out_text + thoughts 다. 둘을 더하는 건 실수가 아니다 — **빼면 원가가 과소 계상된다.**
      gemini-2.5-flash 는 사고(thinking) 토큰을 **출력 단가로 과금**하는데, 그 토큰은
      응답 본문(candidates)에 들어오지 않는다. out_text 만 세면 낸 돈의 일부가 통계에서
      사라지고, 하필 그 통계가 "캐스케이드가 Live 보다 싼가"의 근거로 쓰인다.
      (Vertex 기준. AI Studio 는 candidates 에 사고 토큰이 포함돼 나오므로, 만약
       거기로 옮기면 이 덧셈이 이중계상이 된다 — 그때 다시 판단해라.)
    ⭐ 출력 단가가 입력의 8배다($2.50 vs $0.30/1M) — out 을 빠뜨리면 크게 틀린다.
    """
    e = entry or {}
    if not (e.get("in_text") or e.get("out_text") or e.get("thoughts")):
        return 0.0, None
    price = LLM_TOKEN_PRICE_USD.get(e.get("vendor"))
    if price is None:
        return 0.0, str(e.get("vendor"))
    out_billable = int(e.get("out_text") or 0) + int(e.get("thoughts") or 0)
    return (
        int(e.get("in_text") or 0) * price["in_text"] + out_billable * price["out_text"]
    ) / 1_000_000, None


def _tts_cost_usd(entry: dict | None) -> tuple[float, list[str]]:
    """TTS 사용량 1건의 원가. 반환 (원가, 미상 목록).

    ⛔ TTS 원가식도 여기 하나뿐이다 — 캐스케이드의 TTS 다리도, 통화후 문장 TTS 도 이 함수를
      탄다. **과금 단위가 엔진마다 다르다**는 판정이 이 함수의 핵심이고, 그걸 두 벌 두면
      한쪽이 반드시 틀린다.
    ⛔ 토큰 과금 엔진(Gemini-TTS)에 chars 를 쓰지 마라 — 문자→오디오초 환산은 말하는
      속도에 따라 배로 틀린다(그럴듯한 거짓 숫자). audio_s 가 없으면 **미상으로 드러낸다.**
    ⚠ 반대로 문자 과금 엔진(Chirp3-HD)은 chars 가 **정답**이다 — 거기에 audio_s 를 요구하면
      MP3 를 디코딩해야 알 수 있는 값을 이유 없이 요구하는 셈이 된다.
    """
    tts = entry or {}
    total = 0.0
    unknown: list[str] = []
    tts_vendor = tts.get("vendor")
    tok_price = TTS_TOKEN_PRICE_USD_PER_1M.get(tts_vendor)
    if tok_price is not None:
        # ── 토큰 과금 엔진(Gemini-TTS) ──
        secs = tts.get("audio_s")
        if not secs:
            # 잴 수 없으면 잰 척하지 않는다 — 무엇이 없어서 못 쟀는지까지 남긴다.
            unknown.append(f"tts:{tts_vendor}(audio_s 없음 — 토큰 과금이라 chars 로 못 잰다)")
        else:
            out_tok = float(secs) * GEMINI_TTS_TOKENS_PER_AUDIO_S
            # 입력 텍스트 토큰은 선택 — 있으면 더한다. 없어도 무시할 만하다
            # (오디오 출력 대비 1% 미만: 6,000자 ≈ 1,500tok × $0.5/1M ≈ $0.0008).
            total += (
                out_tok * tok_price["out_audio"]
                + int(tts.get("in_text") or 0) * tok_price["in_text"]
            ) / 1_000_000
    elif tts.get("chars"):
        # ── 문자 과금 엔진(Chirp3-HD 등) ──
        price = TTS_PRICE_USD_PER_CHAR.get(tts_vendor)
        if price is None:
            unknown.append(f"tts:{tts_vendor}")
        else:
            total += int(tts["chars"]) * price
    elif tts.get("audio_s"):
        # 초는 왔는데 벤더를 모른다 — 조용히 넘기지 않는다.
        unknown.append(f"tts:{tts_vendor}")
    return total, unknown


# usage_json 안의 **엔진 무관** 곁가지 사용량 키. Live 통화든 캐스케이드 통화든 이것들은
# 똑같이 돈다 — 그래서 engine 분기 **안이 아니라 위**에서 더한다.
# ⛔ 단위가 다르므로 **키를 섞지 마라**: LLM 키는 토큰, TTS 키는 문자(또는 오디오 초)다.
SIDE_LLM_KEYS = ("sidecars", "analysis")
SIDE_TTS_KEYS = ("tts",)
SIDE_USAGE_KEYS = SIDE_LLM_KEYS + SIDE_TTS_KEYS   # "이 통화가 곁가지를 쟀나" 판정용


def estimate_side_cost_usd(usage_json: dict | None) -> tuple[float, list[str]]:
    """Live·캐스케이드 **양쪽 공통**인 곁가지 원가(사이드카 + 통화후 분석 + 통화후 문장 TTS).

    🧒 왜 따로 있나: `call.usage_*` 는 Live 세션(또는 캐스케이드 3다리)만 담는다. 그런데
      통화 1건에는 동적 힌트·재접지 브리프·레벨테스트 턴 판정(통화중), 문장 추출·검출·
      레벨 판정(통화후), 그리고 **복습 문장 TTS**(통화후)가 더 돈다. 이 몫이 빠진 숫자가
      "5분 $0.19" 로 보고됐다.
    ⚠ 과거 통화에는 이 키들이 아예 없다 — 그러면 0 이고 원가는 **예전 값 그대로** 나온다
      (하위호환). ⛔ 그때의 0 은 "공짜"가 아니라 **안 잰 통화**라는 뜻이다.
    """
    total = 0.0
    unknown: list[str] = []
    for key in SIDE_LLM_KEYS:
        cost, vendor_unknown = _llm_tokens_cost_usd((usage_json or {}).get(key))
        total += cost
        if vendor_unknown:
            unknown.append(f"{key}:{vendor_unknown}")
    for key in SIDE_TTS_KEYS:
        cost, tts_unknown = _tts_cost_usd((usage_json or {}).get(key))
        total += cost
        unknown += tts_unknown
    return total, unknown


def estimate_cascade_cost_usd(vendors: dict | None) -> tuple[float, list[str]]:
    """캐스케이드 원가(USD)를 usage_json.vendors 에서 계산한다.

    반환: (원가, **단가표에 없는 벤더 이름들**). 두 번째 값이 요점이다 — 모르는 벤더를
    조용히 0 원으로 먹으면 "캐스케이드가 공짜"라는 그럴듯한 거짓말이 나온다. 모르면
    모른다고 드러내고, 호출부가 그 행을 표본에서 뺄지 정하게 한다.

    기대 형태(계약):
      {"stt": {"vendor": ..., "audio_s": 902.4},
       "llm": {"vendor": ..., "in_text": 41000, "out_text": 3200, "thoughts": 1500},
       "tts": {"vendor": ..., "chars": 8400}}          ← 문자 과금 엔진
       "tts": {"vendor": "gemini-2.5-flash-tts", "audio_s": 452.0}  ← 토큰 과금 엔진
    llm.thoughts 는 선택이지만 **있으면 출력 원가에 더해진다**(아래 산식 주석 참조).
    ⚠ Gemini-TTS 계열은 **audio_s(합성된 오디오 초)가 필수**다. 없으면 chars 가 와도
      계산하지 않고 미상으로 낸다 — 과금 단위가 문자가 아니라 오디오 길이이기 때문이다.
    """
    total = 0.0
    unknown: list[str] = []
    v = vendors or {}

    stt = v.get("stt") or {}
    if stt.get("audio_s"):
        price = STT_PRICE_USD_PER_S.get(stt.get("vendor"))
        if price is None:
            unknown.append(f"stt:{stt.get('vendor')}")
        else:
            total += float(stt["audio_s"]) * price

    llm_cost, llm_unknown = _llm_tokens_cost_usd(v.get("llm"))
    total += llm_cost
    if llm_unknown:
        unknown.append(f"llm:{llm_unknown}")

    tts_cost, tts_unknown = _tts_cost_usd(v.get("tts"))
    total += tts_cost
    unknown += tts_unknown

    return total, unknown


def estimate_call_cost_usd(
    engine: str | None,
    *,
    in_audio: int = 0, in_text: int = 0, out_audio: int = 0, out_text: int = 0,
    usage_json: dict | None = None,
) -> tuple[float, list[str]]:
    """통화 1건의 원가(USD)를 **엔진에 맞는 단가로** 계산한다. 반환 (원가, 미상 벤더).

    🧒 왜 엔진을 받나: `usage_in_text` 라는 같은 컬럼이 Live 에선 Live 텍스트 토큰($0.50/1M),
      캐스케이드에선 LLM 토큰($0.30/1M)이다. 엔진을 모르고 계산하면 **틀린 값이 조용히**
      나오고, 그 값이 두 엔진 비교의 근거가 된다. 그래서 여기가 유일한 입구다.

    engine 이 NULL(계기판 이전 통화)이면 Live 로 본다 — 캐스케이드는 이 컬럼이 생긴 뒤에만
    존재하므로, NULL 은 전부 Live 통화다.

    ⭐ 2026-08-17: **엔진 몫 + 곁가지 몫**이다. 사이드카·통화후 분석은 엔진과 무관하게
      (Live 든 캐스케이드든) 돌기 때문에 engine 분기 **안이 아니라 위**에서 더한다.
      ⛔ 새 산식을 만들지 마라 — 원가의 유일한 입구는 계속 이 함수다.
    """
    if engine and engine.startswith("cascade:"):
        base, unknown = estimate_cascade_cost_usd((usage_json or {}).get("vendors"))
    else:
        base, unknown = estimate_usage_cost_usd(
            in_audio=in_audio, in_text=in_text, out_audio=out_audio, out_text=out_text
        ), []
    side, side_unknown = estimate_side_cost_usd(usage_json)
    return base + side, unknown + side_unknown


def save_call_usage(
    db: Session, call_id: int, summary: dict, *, engine: str | None = None
) -> bool:
    """usage 요약을 통화 행에 남긴다(원가 계기판 2·3단계). 저장했으면 True.

    call_session._usage_summary 가 만든 요약을 받는다 — 로그 줄과 **같은 계산 결과**다.
    자주 집계하는 값(모달리티 4항·총합·최대 컨텍스트·메시지 수)만 컬럼으로 승격하고,
    나머지는 usage_json 에 통째로 둔다(스키마를 안 바꾸고 새 필드를 받는 그릇).

    engine 은 계약 문자열('live:gemini-native-audio' / 'cascade:stt+llm+tts')이다.
    ⚠ 캐스케이드 호출부는 컬럼 규약을 지켜라 — in_text/out_text = **LLM 토큰**,
      in_audio/out_audio = 0. STT·TTS 는 단위가 초·문자라 컬럼이 아니라
      summary["vendors"] 로 넘긴다(usage_json.vendors 에 그대로 남는다).
      원가 계산은 반드시 estimate_call_cost_usd(engine=...) 로.

    ⛔ 통화가 없으면 조용히 False — 계기판 때문에 예외를 올릴 이유가 없다(R5).
    설계 근거: docs/20260805_1950_원가계기판-2단계-영속화-설계.md
              docs/20260807_0028_엔진구분-usage_engine-과-peak-수정-계획.md
    """
    call = db.get(Call, call_id)
    if call is None:
        return False
    in_mod = summary.get("in_mod") or {}
    out_mod = summary.get("out_mod") or {}
    # engine 인자 우선, 없으면 요약에 실려 온 값. 둘 다 없으면 NULL(= 미기록)로 남긴다 —
    # 0 이나 'unknown' 으로 채우면 "모른다"와 "정말 그 엔진"이 구별되지 않는다.
    call.usage_engine = engine or summary.get("engine")
    call.usage_msgs = int(summary.get("msgs") or 0)
    call.usage_in_audio = int(in_mod.get("AUDIO") or 0)
    call.usage_in_text = int(in_mod.get("TEXT") or 0)
    call.usage_out_audio = int(out_mod.get("AUDIO") or 0)
    call.usage_out_text = int(out_mod.get("TEXT") or 0)
    call.usage_total = int(summary.get("sum_total") or 0)
    call.usage_peak_prompt = int(summary.get("peak_prompt") or 0)
    # 컬럼으로 뺀 4종(AUDIO/TEXT × in/out) 외의 모달리티가 오면 여기 남는다 —
    # 새 모달리티(VIDEO 등)가 생겨도 컬럼 추가 없이 관측이 이어진다.
    extra_in = {k: v for k, v in in_mod.items() if k not in ("AUDIO", "TEXT")}
    extra_out = {k: v for k, v in out_mod.items() if k not in ("AUDIO", "TEXT")}
    call.usage_json = {
        "dropped": summary.get("dropped"),
        "monotonic": summary.get("monotonic"),
        "last_prompt": summary.get("last_prompt"),
        "last_total": summary.get("last_total"),
        "sum_prompt": summary.get("sum_prompt"),
        "sum_resp": summary.get("sum_resp"),
        "sum_thoughts": summary.get("sum_thoughts"),
        # ⭐ 캐시된 컨텍스트 토큰(2026-08-16). ⛔ 원가식엔 **안 쓴다** — 값이 나온 뒤에 정한다.
        #   ⚠ 벤더가 필드를 안 준 통화는 `None` 이다(0 으로 접으면 "캐시 0"과 구별이 안 된다).
        #   로그는 30일이면 사라지므로 여기 남겨야 **추이**를 볼 수 있다.
        "sum_cached": summary.get("sum_cached"),
        "t_first": summary.get("t_first"),
        "t_last": summary.get("t_last"),
        "compressions": summary.get("compressions"),
        "epochs": summary.get("epochs"),
        "reconnects": summary.get("reconnects"),
        # 압축 사이클 peak(압축마다 리셋되는 값). 컬럼의 peak_prompt 는 통화 전체 최대치라
        # 둘이 갈라진다 — 왜 갈라졌는지 나중에 봐야 해서 참고용으로 같이 남긴다.
        "cycle_peak": summary.get("cycle_peak"),
        # 캐스케이드 다리별 사용량(STT 초·TTS 문자·LLM 토큰). 단위가 토큰이 아니라
        # 컬럼에 못 들어가는 값들이다 — 원가는 estimate_cascade_cost_usd 가 여기서 계산한다.
        **({"vendors": summary["vendors"]} if summary.get("vendors") else {}),
        # ⭐ 통화중 사이드카(힌트·재접지·레벨테스트 턴 판정)의 LLM 토큰. ⛔ Live 컬럼
        #   (usage_in_text 등)에 **섞지 마라** — 단가가 다르고, 섞으면 두 엔진 비교가
        #   오염된다. 원가는 estimate_call_cost_usd 가 engine 분기 **위**에서 더한다.
        **({"sidecars": summary["sidecars"]} if summary.get("sidecars") else {}),
        **({"in_other": extra_in} if extra_in else {}),
        **({"out_other": extra_out} if extra_out else {}),
    }
    db.commit()  # R3 — 쓰기는 service 가 명시적으로 커밋
    return True


def add_call_usage_extra(db: Session, call_id: int, key: str, entry: dict | None) -> bool:
    """usage_json 에 곁가지 사용량 1건을 **나중에** 얹는다(통화후 분석 몫). 썼으면 True.

    🧒 왜 UPDATE 인가: 통화중 usage 는 통화가 끝나는 순간 저장되는데, 통화후 분석은
      **그 뒤에** 돈다. 같은 저장 경로에 몰면 분석 몫이 통째로 유실된다(시점이 다르다).

    ⛔ JSONB 는 제자리 변경(dict 를 그냥 mutate)으로는 더티가 안 잡힌다 — **새 dict 를
      대입**해야 UPDATE 가 나간다. 여기서 실수하면 값이 조용히 안 써진다.
    ⛔ R5: 통화가 없거나 entry 가 비면 조용히 False. 계기판 때문에 분석이 죽으면 안 된다.
    ⚠ 같은 키가 이미 있으면 **덮어쓴다** — 분석 재시도는 같은 통화를 다시 다 도는 것이라
      누적이 아니라 최신값이 맞다(재시도 중복 계상 방지).
    """
    if not entry:
        return False
    call = db.get(Call, call_id)
    if call is None:
        return False
    call.usage_json = {**(call.usage_json or {}), key: entry}
    db.commit()  # R3
    return True


async def _save_analysis_usage(session_factory, call_id: int, usage) -> None:
    """통화후 분석 LLM 몫을 usage_json.analysis 에 얹는다(비동기 경로 공용 — R5).

    ⛔ 어떤 실패도 삼킨다. 계기판이 분석을 죽이면 안 된다 — 원가를 못 재는 것과
      분석 결과를 통째로 잃는 것은 비교 대상이 아니다.
    ⚠ 통화 usage 행이 아직 없으면(라이브 계측 미수신) usage_json 은 NULL 이었다가
      여기서 analysis 키 하나만 있는 dict 가 된다 — 그 편이 유실보다 낫다.
    """
    try:
        entry = usage.as_dict() if usage is not None else None
        if not entry:
            return
        await run_db(
            session_factory,
            lambda db: add_call_usage_extra(db, call_id, "analysis", entry),
        )
    except Exception as exc:  # noqa: BLE001 - R5
        logger.warning("normalcall usage: 분석 몫 기록 실패(무시) call_id=%s: %s", call_id, exc)


async def _save_tts_usage(
    session_factory, call_id: int, chars: int, calls: int, failed: int
) -> None:
    """통화후 **문장 TTS** 사용량을 usage_json.tts 에 얹는다(R5 — 어떤 실패도 삼킨다).

    ⛔ LLM 키(sidecars/analysis)와 **섞지 않는다** — 단위가 다르다(문자 vs 토큰).
      원가 계산도 _tts_cost_usd 가 따로 맡는다(엔진별 과금 단위 판정이 거기 있다).
    ⚠ **성공한 합성이 하나도 없으면 아무것도 안 쓴다**(실패만 있어도 마찬가지). 실패는 과금이
      0 이라 원가 정보가 없는데, 그걸 쓰면 usage_json 이 NULL 이 아니게 되어 "계측 안 됨"과
      "잰 결과 0원"의 구별이 깨진다(그 구별이 이 프로젝트의 규약이다). 실패 횟수는 성공이
      하나라도 있을 때 calls_failed 로 함께 남고, 아니면 경고 로그에만 남는다.
    ⚠ vendor 는 실제로 돈 엔진 이름을 그대로 쓴다(core.tts.CHIRP3_ENGINE). 하드코딩된
      문자열을 여기 또 적으면 엔진이 바뀔 때 단가표와 조용히 어긋난다.
    """
    try:
        if not (chars or calls):
            return
        entry = {
            "vendor": tts.CHIRP3_ENGINE,
            "calls": calls,
            "chars": chars,
            **({"calls_failed": failed} if failed else {}),
        }
        await run_db(
            session_factory, lambda db: add_call_usage_extra(db, call_id, "tts", entry)
        )
    except Exception as exc:  # noqa: BLE001 - R5
        logger.warning("normalcall usage: 문장 TTS 몫 기록 실패(무시) call_id=%s: %s", call_id, exc)


def set_status(db: Session, call_id: int, status: str) -> None:
    """통화 분석 상태만 갱신한다(ongoing/analyzing/done/failed)."""
    call = db.get(Call, call_id)
    if call is None:
        return
    call.status = status
    db.commit()


def get_status(db: Session, call_id: int, member_id: int) -> str | None:
    """소유자 확인 후 통화 상태를 반환한다(없거나 타인 통화면 None)."""
    call = db.get(Call, call_id)
    if call is None or call.member_id != member_id:
        return None
    return call.status


def get_status_detail(db: Session, call_id: int, member_id: int) -> dict | None:
    """소유자 확인 후 상태+콜타입+판정 레벨을 반환한다(없거나 타인 통화면 None).

    status 폴링 응답 확장용(D11): 클라 결과 화면이 call_type 으로 분기하고,
    level_test 면 assessed_level 로 판정 결과를 얻는다(신규 엔드포인트 없음).
    """
    call = db.get(Call, call_id)
    if call is None or call.member_id != member_id:
        return None
    return {
        "status": call.status,
        "call_type": call.call_type,
        "assessed_level": call.assessed_level,
    }


def prepare_reanalysis(db: Session, call_id: int, member_id: int) -> dict | None:
    """실패한 통화의 재분석을 준비한다(수동 재시도, A). 소유자 확인 + 상태 게이트 + status 리셋.

    'failed' 통화만 재분석 대상이다(done 은 이미 완료, ongoing/analyzing 은 진행 중).
    전사(call_raw_data)는 실패해도 보존되므로 재료는 그대로 있고, 증거 재적립 멱등 가드
    (_apply_call_mastery)가 중복을 막아 재실행이 안전하다.

    Returns:
        None                                : 없거나 타인 통화(404).
        {"eligible": False, "status": <현재>} : 재분석 불가 상태(409).
        {"eligible": True, "status": "analyzing", "call_type", "locale", "member_id"}:
            status 를 'analyzing' 으로 되돌리고 커밋(R3) — 호출부가 백그라운드 분석을 띄운다.
    """
    call = db.get(Call, call_id)
    if call is None or call.member_id != member_id:
        return None
    if call.status != "failed":
        return {"eligible": False, "status": call.status}
    member = db.get(Member, member_id)
    locale = _base_locale(member.language if member else None)
    call.status = "analyzing"
    db.commit()
    return {
        "eligible": True,
        "status": "analyzing",
        "call_type": call.call_type,
        "locale": locale,
        "member_id": member_id,
    }


# --------------------------------------------------------------------------- #
# 통화후 분석 (비동기 — gemini 호출 + DB 는 run_db)
# --------------------------------------------------------------------------- #
class LearnedExpression(BaseModel):
    """통화에서 배운 표현 1건."""

    korean: str
    translation: str
    source_type: Literal["asked", "corrected", "drilled"]
    learner_attempt: str | None = None


class ItemDetection(BaseModel):
    """검출 후보 항목 1건의 사용 판정(mechanics ⑤ — LLM 은 증인, 심판은 서버 검증 게이트).

    item_id 는 [검출 후보] 표의 ID 그대로(closed-set), quote 는 USER 발화 원문 인용 강제 —
    _verify_detections 가 전사 부분일치로 환각을 차단한다.
    """

    item_id: int
    evidence: Literal["E1", "E2", "E3", "F"]
    quote: str
    note: str | None = None


class _CallAnalysisBase(BaseModel):
    """통화후 분석 공통 출력 — 검출 후보 0개일 때의 응답 스키마.

    후보가 없으면 detections 필드·지시문 자체를 생략해(스키마 분리) 기존 프롬프트를
    오염시키지 않는다(하위호환 — 기존 분석 경로 출력 무변화).
    """

    summary: str
    detected_mode: Literal["study", "chat", "mixed"]
    expressions: list[LearnedExpression]
    # 요구1: 통화 전체를 돌아본 비버 선생님의 격려 한마디(학습자 모국어 1문장). 후보 0/有
    # 양경로 공통이라 부모에 둔다. 파싱 누락 시 default "" → _save_analysis 가 None 저장.
    feedback: str = Field(
        default="",
        description="통화 전체를 돌아본 격려 코칭 한마디(학습자 모국어 1문장)",
    )


class CallAnalysis(_CallAnalysisBase):
    """통화후 분석 1콜의 전체 출력(+ 항목 사용 검출 — 추가 콜 0, 기존 1콜에 병합).

    detections 기본 [] — 구스키마 응답도 파싱을 통과한다(graceful).
    """

    detections: list[ItemDetection] = Field(default_factory=list)


def _analysis_instruction(
    locale: str, target_language: str = "한국어", locale_label: str | None = None
) -> str:
    """통화후 분석용 시스템 지시문(한국어). locale/locale_label 로 번역·요약 언어를,
    target_language 로 교육 대상 언어를 지정(기본 한국어 — 프로덕션 출력 무손상)."""
    label = locale_label or _LOCALE_LABEL.get(locale, _LOCALE_LABEL["en"])
    return (
        f"너는 {target_language} 학습자와 AI 선생님(BEAVER)의 {target_language} 통화 전사를 분석하는 도구다.\n"
        "전사에서 학습자가 '배운 표현'을 뽑고, 각 표현을 학습자 모국어로 번역하고, "
        "통화 한 줄 요약과 통화 모드를 함께 JSON 으로만 출력하라.\n"
        "[배운 표현의 3가지 종류]\n"
        f"- asked: 학습자가 '○○를 {target_language}로 어떻게 말해요?' 처럼 물어서 비버가 알려준 표현.\n"
        f"- corrected: 학습자가 어색하게 말한 것을 비버가 자연스러운 {target_language}로 고쳐준 표현. "
        "이때 learner_attempt 에 학습자의 원래(어색한) 발화를 넣는다.\n"
        "- drilled: 공부 모드에서 비버가 가르치고 학습자가 따라 말한 표현.\n"
        "[규칙]\n"
        f"- korean 에는 반드시 '올바른 최종 {target_language}'만 넣는다(어색한 발화·오류형 금지).\n"
        "- translation 은 각 표현을 " + label + " 로 번역.\n"
        "- 위 3종에 해당하는 학습 포인트가 없으면 expressions 는 빈 배열([]).\n"
        "- summary 는 통화의 핵심 소재를 " + label + " 로 아주 짧게 요약한다. "
        "완결된 문장이 아니라 주제를 나타내는 명사구로, 주어·서술어 없이 2~4어절 이내. "
        "ex) 강아지 산책과 음악 취향 / 주말 여행 계획 / 좋아하는 한국 음식\n"
        "- detected_mode: 공부 위주면 study, 자유대화 위주면 chat, 둘 다면 mixed.\n"
        "- feedback 은 통화 '전체'를 돌아보며 학습자를 다독이는 격려 코칭을 " + label + " 로 "
        "딱 1문장 쓴다(비버 선생님이 직접 건네는 따뜻한 말투, 과장·오글거림 금지). "
        "통화의 구체적인 순간 하나를 짧게 언급하되, 점수·레벨·숫자·'틀렸다'는 절대 쓰지 않는다. "
        "통화가 아주 짧거나 발화가 적어도 참여 자체를 격려하는 1문장을 반드시 쓴다(빈 문자열 금지).\n"
        "- 전사가 부정확할 수 있으니 명백히 학습된 표현만 보수적으로 뽑는다."
    )


# 항목 사용 판정 지시문(mechanics ⑤ 3단계) — 검출 후보가 있을 때만 기존 지시문 뒤에 부착.
# "항목당 1건" 원칙에 E3 예외(최대 2건)를 둔다 — fast-track 조건 ①(같은 통화 E3 2건)이
# 1건 제한과 양립할 수 없기 때문(서버 dedup·상한이 남용을 차단한다).
_DETECTION_INSTRUCTION = (
    "[항목 사용 판정]\n"
    "입력 마지막의 [검출 후보] 표에 있는 학습 항목이 학습자(USER) 발화에서 실제로 쓰였는지 "
    "판정해 detections 배열로 출력하라.\n"
    "- closed-set: 표에 있는 항목만 판정한다. 표 밖의 항목을 만들어내거나 id 를 바꾸지 마라. "
    "item_id 는 표의 ID 숫자를 그대로 쓴다.\n"
    "- evidence 등급 정의:\n"
    "  - E1(모방): 비버가 방금 말했거나 가르친 표현을 그대로(또는 거의 그대로) 따라 말함.\n"
    "  - E2(유도): 비버의 질문·유도에 답하면서 그 항목을 올바르게 사용함.\n"
    "  - E3(자발): 비버가 먼저 꺼내지 않았는데 학습자가 스스로 그 항목을 올바르게 사용함.\n"
    "  - F(오류): 그 항목을 쓰려다 명백히 틀리게 사용함.\n"
    "- quote: 판정 근거가 된 학습자(USER) 발화를 전사 원문 그대로 인용한다(수정·요약·번역 "
    "금지). 전사에 없는 quote 는 서버가 무효 처리한다.\n"
    "- 항목당 1건만 출력한다 — 성공(E1/E2/E3)이 여러 번이면 최고 등급 1건만, 성공이 없고 "
    "오류만 있으면 F 1건. 예외: 자발(E3) 사용이 서로 다른 문장으로 여러 번이면 E3 를 최대 "
    "2건까지 출력할 수 있다.\n"
    "- 사용 판정 기준: 단어(vocab)=그 단어가 발화 표면에 실제 출현 / 문법(grammar)=그 문형이 "
    "발화에서 실제로 실현됨 / 통문장(chunk)=문장의 핵심부가 산출됨.\n"
    "[ASR 한계 — 보수 판정]\n"
    "- 전사는 음성인식 결과라 철자·띄어쓰기가 왜곡될 수 있다. F(오류)는 오류가 전사에 "
    "명시적으로 남았을 때만 판정한다. 철자·띄어쓰기 차이는 오류가 아니다.\n"
    "- 판단이 애매한 항목은 출력하지 마라. 사용 흔적이 없는 후보도 출력하지 않는다. "
    "해당 없으면 detections 는 빈 배열([])."
)


def _candidate_table(candidates: list[dict]) -> str:
    """검출 후보 표(프롬프트 부착용) — `ID|종류|항목|예문` 파이프 구분 텍스트."""
    lines = ["[검출 후보]", "ID|종류|항목|예문"]
    for c in candidates:
        surface = str(c.get("surface") or "").replace("|", "/").replace("\n", " ")
        example = str(c.get("example") or "").replace("|", "/").replace("\n", " ")
        lines.append(f"{c['item_id']}|{c.get('kind', '')}|{surface}|{example}")
    return "\n".join(lines)


def _load_dialog_rows(db: Session, call_id: int) -> list[dict]:
    """통화 전사 행을 turn 순서대로 평범한 dict 로 반환한다(검증 게이트·전사 조립 공용)."""
    rows = db.scalars(
        select(CallRawData)
        .where(CallRawData.call_id == call_id)
        .order_by(CallRawData.turn_index, CallRawData.call_raw_data_id)
    ).all()
    return [
        {"turn_index": r.turn_index, "role": r.role, "content": r.content} for r in rows
    ]


def _dialog_from_rows(rows: list[dict]) -> str:
    """전사 행 목록 → [USER]/[BEAVER] 전사 텍스트."""
    lines = []
    for r in rows:
        if not r.get("content"):
            continue
        who = "USER" if r.get("role") == "user" else "BEAVER"
        lines.append(f"[{who}] {r['content']}")
    return "\n".join(lines)


def _build_dialog(db: Session, call_id: int) -> str:
    """CallRawData 를 turn 순서대로 [USER]/[BEAVER] 전사로 조립한다(텍스트만)."""
    return _dialog_from_rows(_load_dialog_rows(db, call_id))


# 레벨테스트 전사 필터: USER 줄이 대상 언어를 이만큼은 담아야 판정 재료로 남는다.
# 2자 = 「です」 같은 최소 정중체 하나. 1자로 두면 「あ」 한 글자짜리 감탄사도 통과한다.
_MIN_LINE_TARGET_CHARS = 2


def _strip_non_target_user_lines(dialog: str, language: str) -> str:
    """레벨테스트 판정 전, USER 줄에서 **대상 언어가 없는 것**을 걷어낸다.

    ⚠ 왜 필요한가 — 실측(call=823, ja). 학습자가 한국어에 「데스」만 붙였는데
    판정관이 그걸 일본어 문법으로 읽고 A3(3단계)를 줬다:

        [user] 어제는 나는 그 연구실 갔다데스, 프로젝트 했다데스
        [user] 어 모른다 데스 어렵다 데스
        판정관: "'갔다데스','했다데스' 와 같이 동사의 과거형(〜た)을 사용하려 시도했으며,
                 '모른다 데스' 와 같이 부정형(〜ない)을 사용하려 했습니다 … A3 밴드"

    한국어 어미 '-다' 가 일본어 「〜た」로, '모른다' 가 「〜ない」로 둔갑했다. 프롬프트에
    "대상 언어 발화만 인용하라"고 적혀 있어도 판정관은 **자기가 속은 걸 모른다** — 스스로
    마커 4종을 찾았다고 확신했다. 그래서 LLM 에게 부탁하지 않고 **입력에서 지운다**.
    「갔다데스」는 일본어 문자가 0자라 판정관 눈에 아예 들어가지 않는다.

    BEAVER 줄은 남긴다 — 무엇을 물었는지가 있어야 "유도했는데 못 했다"를 판정관이 안다.
    표본 게이트(_user_char_total)와 역할이 다르다: 게이트는 **판정을 돌릴지**를 정하고,
    이건 **무엇을 근거로 삼을지**를 정한다. 게이트를 통과한 통화 안에서도 오염은 남는다.
    """
    prefix = "[USER] "
    kept: list[str] = []
    for line in dialog.splitlines():
        if line.startswith(prefix):
            body = line[len(prefix):]
            if count_target_script_chars(body, language) < _MIN_LINE_TARGET_CHARS:
                continue
        kept.append(line)
    return "\n".join(kept)


def _verify_detections(
    db: Session,
    call_id: int,
    member_id: int,
    detections: list[ItemDetection],
    candidates: list[dict],
    dialog_rows: list[dict],
    hinted_from_turn_index: set[int] | None = None,
) -> list[mastery_service.VerifiedEvidence]:
    """LLM 검출을 서버 규칙으로 검증한다 — AI 는 증인, 코드가 심판(관통 원칙 1).

    순서 고정(mechanics ⑤ 4단계):
        ① item_id 가 후보 밖 → 폐기 (closed-set)
        ② quote 가 USER 전사에 부재(공백 정규화 부분일치) → 폐기 (환각 차단)
        ③ E3 인데 직전 2 BEAVER 턴에 그 항목/인용 포함 → E1 강등 (앵무새 방어)
        ④ quote 유효 글자(letter/digit) 4자 미만 또는 항목 단독 발화(E2/E3) → E1 강등
           (⛔ 단독 발화 강등은 **chunk 제외** — 통문장은 단독 발화가 정답이다. 아래 주석)
        ④' 힌트 열람 직후 USER 턴의 E2/E3 → E1 강등 (D16 오염 방지 — 아래 참조)
        ⑤ 동일 정규화 인용 중복(항목 내) → 1건
    grade_raw(원판정)/grade_final(강등 반영) 모두 보존. db 는 시그니처 계약 유지용 —
    현 구현은 전사·후보만으로 판정한다(추가 조회 불요).

    hinted_from_turn_index(D16): hint_used 수신 시점의 state.next_turn_index 집합.
    turn_id 는 CallRawData 에 저장되지 않는 휘발 식별자라 전사와 조인할 수 없다 —
    대신 힌트 열람 순간의 next_turn_index(그 이후 처음 flush 될 세그먼트의 turn_index)를
    기록해 두면 "열람 직후 첫 USER 턴" = 그 값 이상의 첫 USER 행으로 결정된다
    (barge-in off 라 힌트는 비버 턴 종료 후 열리고, 다음 flush 는 사용자 답변이다).
    힌트를 보고 읽은 발화가 자발(E3)/유도(E2)로 잡혀 "잘씀"이 부풀지 않게 E1 로만
    인정한다(정적 카드 힌트와 동일 규칙 — mechanics ⑬). in-memory 전달이라 통화
    크래시 시 유실될 수 있으나, 그 오차는 과크레딧 1회 허용으로 수용한다(테이블
    신설 대신 — mechanics ⑬ 정합, 사용자에게 유리한 쪽 오차).
    """
    cand_by_id = {int(c["item_id"]): c for c in candidates}

    # 전사 인덱스: USER 턴(행 위치·USER 순번·turn_index·정규화 내용) / BEAVER 턴(행 위치·정규화)
    user_rows: list[tuple[int, int, int, str]] = []
    beaver_rows: list[tuple[int, str]] = []
    ordinal = 0
    for pos, r in enumerate(dialog_rows):
        content = r.get("content") or ""
        if not content.strip():
            continue
        norm = mastery_service.normalize_text(content)
        if r.get("role") == "user":
            ti = r.get("turn_index")
            user_rows.append((pos, ordinal, ti if ti is not None else pos, norm))
            ordinal += 1
        else:
            beaver_rows.append((pos, norm))

    # D16: 힌트 열람 마커 → 강등 대상 USER turn_index 집합(마커 이후 첫 USER 턴 1개).
    # user_rows 는 turn_index 오름차순이라 첫 매치가 곧 "열람 직후 첫 USER 발화".
    hinted_user_turns: set[int] = set()
    for marker in hinted_from_turn_index or ():
        nxt = next((ti for _pos, _ordn, ti, _norm in user_rows if ti >= marker), None)
        if nxt is not None:
            hinted_user_turns.add(nxt)

    # 후보별 비버 최초 언급 위치(행 순서) — fast-track 조건 ④(선발화) 재료
    first_mention: dict[int, int | None] = {}
    for item_id, c in cand_by_id.items():
        norm_surface = mastery_service.normalize_text(c.get("surface"))
        first_mention[item_id] = next(
            (bpos for bpos, bnorm in beaver_rows if norm_surface and norm_surface in bnorm),
            None,
        )

    out: list[mastery_service.VerifiedEvidence] = []
    seen: set[tuple[int, str]] = set()
    for d in detections:
        cand = cand_by_id.get(d.item_id)
        if cand is None:  # ① 후보 밖 → 폐기
            continue
        if d.evidence not in ("E1", "E2", "E3", "F"):
            continue
        norm_quote = mastery_service.normalize_text(d.quote)
        if not norm_quote:
            continue
        hit = next(
            ((pos, ordn, ti) for pos, ordn, ti, unorm in user_rows if norm_quote in unorm),
            None,
        )
        if hit is None:  # ② USER 전사 부재 → 폐기
            continue
        pos, user_ordinal, turn_index = hit

        grade = d.evidence
        norm_surface = mastery_service.normalize_text(cand.get("surface"))
        if grade == "E3":  # ③ 직전 2 BEAVER 턴 에코 → E1
            recent = [bnorm for bpos, bnorm in beaver_rows if bpos < pos][-2:]
            if any(
                (norm_surface and norm_surface in bnorm) or norm_quote in bnorm
                for bnorm in recent
            ):
                grade = "E1"
        if grade in ("E2", "E3"):  # ④ 4자 미만·항목 단독 발화 → E1
            alnum = sum(1 for ch in d.quote if ch.isalnum())
            # ⛔ 2026-08-16: **단독 발화 강등을 chunk 에는 걸지 않는다**(사장님 라인 결정).
            #   L1 통문장은 "통째로 말하기"가 학습 목표 그 자체라 단독 발화가 곧 정답이다.
            #   ⚠ 실측이 결정타였다(운영 DB item_evidence 502건): 청크 산출(E2/E3) 원판정
            #     16건 중 7건이 강등됐는데, 강등 여부를 가른 것이 **문장부호**였다 —
            #     normalize_text 는 공백만 지우므로 surface '안녕하세요?' 에 대해 STT 가
            #     '안녕하세요.' 로 찍으면 생존, '안녕하세요?' 로 찍으면 강등. 즉 규칙이
            #     의도대로가 아니라 **STT 우연**으로 작동하고 있었다.
            #   ⛔ 정규화가 문장부호까지 지우게 고치는 쪽은 함정이다 — 그러면 청크 산출이
            #     전멸하고 L1 은 영영 마스터가 안 된다.
            #   ⭐ 왜 안전한가(코드로 확인함): 진짜 앵무새 방어는 위의 ③이다 —
            #     `if grade == "E3"` 가지에서 **직전 2 BEAVER 턴**에 surface 또는 quote 가
            #     들어 있으면 E1 로 내린다. ④를 빼도 "비버가 방금 말한 걸 그대로 따라한
            #     E3"는 ③이 계속 막는다. 그리고 마스터는 ①(score≥3.0)·②(서로 다른 통화
            #     2회 && 다른 날 2일)에 여전히 묶여 있어 한 통화로는 절대 못 딴다.
            #   ⚠ 남는 구멍은 **에코 E2** 다 — ③은 E3 에만 걸리므로, 비버가 방금 들려준
            #     통문장을 따라 말한 것을 분석 LLM 이 E2 로 라벨하면 이제 살아남는다.
            #     실측상 LLM 은 따라말하기를 압도적으로 E1 로 본다(청크 E1 54 : E2 6)이라
            #     현재 크기는 작다. ⇒ **청크 E2 비율이 갑자기 뛰면 여기부터 의심해라.**
            solo_demote = cand.get("kind") != "chunk"
            if alnum < 4 or (solo_demote and norm_quote == norm_surface):
                grade = "E1"
        if grade in ("E2", "E3") and turn_index in hinted_user_turns:
            grade = "E1"  # ④' D16: 힌트 열람 직후 발화 — 모방 수준으로만 인정

        norm_hash = mastery_service.text_hash(norm_quote)
        key = (d.item_id, norm_hash)
        if key in seen:  # ⑤ 동일 정규화 인용 중복 → 1건
            continue
        seen.add(key)

        mention_pos = first_mention.get(d.item_id)
        out.append(
            mastery_service.VerifiedEvidence(
                item_id=d.item_id,
                kind=cand.get("kind") or "vocab",
                grade_raw=d.evidence,
                grade_final=grade,
                quote=d.quote,
                turn_index=turn_index,
                user_turn_ordinal=user_ordinal,
                norm_hash=norm_hash,
                injected=bool(cand.get("injected")),
                before_first_mention=(mention_pos is None or pos < mention_pos),
            )
        )
    return out


def _apply_call_mastery(
    db: Session,
    call_id: int,
    member_id: int,
    detections: list[ItemDetection],
    candidates: list[dict],
    dialog_rows: list[dict],
    hinted_from_turn_index: set[int] | None = None,
) -> dict:
    """검증→증거·상태전이→레벨업을 **한 세션·단일 commit** 으로 수행(⑤ 4~7단계).

    부분 커밋 창 금지 — 증거가 반영됐는데 승급 판정이 빠진 상태가 남지 않는다.
    (D15: 유효통화 산출·저장 단계 폐지 — 통화 수 파생값은 증거통화로 계산.)
    """
    # 리뷰 M1: 회원 단위 직렬화 — 같은 회원의 통화 2개가 동시 분석되면 progress upsert 가
    # uq_member_item 충돌로 증거를 통째 유실할 수 있어, 파이프라인 선두에서 행 잠금.
    # (evaluate_level_up 의 재잠금은 멱등이라 무해. sqlite 테스트에선 no-op.)
    if mastery_repository.get_member_for_update(db, member_id) is None:
        logger.warning("normalcall 체크판: member 부재 → 스킵 member_id=%s", member_id)
        return {"verified": 0, "discarded": len(detections), "evidence": None, "levelup": None}
    # 리뷰 M4: 같은 call 의 증거가 이미 있으면 재적립 금지(분석 재시도 대비 멱등 가드).
    if mastery_repository.has_call_evidence(db, call_id):
        logger.warning("normalcall 체크판: call_id=%s 증거 기존재 → 스킵(이중 적립 방지)", call_id)
        return {"verified": 0, "discarded": 0, "evidence": None, "levelup": None}

    verified = _verify_detections(
        db, call_id, member_id, detections, candidates, dialog_rows,
        hinted_from_turn_index=hinted_from_turn_index,
    )
    _call = db.get(Call, call_id)
    call_language = _call.target_language if _call is not None else "ko"
    evidence_summary = mastery_service.apply_evidence(
        db, member_id, call_id, verified, language=call_language,
    )

    levelup = mastery_service.evaluate_level_up(
        db, member_id, trigger_call_id=call_id, language=call_language,
    )
    db.commit()
    return {
        "verified": len(verified),
        "discarded": len(detections) - len(verified),
        "evidence": evidence_summary,
        "levelup": levelup,
    }


def _save_analysis(db: Session, call_id: int, result: _CallAnalysisBase, locale: str) -> list[tuple[int, str]]:
    """요약/모드 저장 + 표현별 Sentence(+Evaluation placeholder) + **status=done** 단일 커밋.

    P2.6: 결과 페이지 폴링이 status==done 에서 풀리므로, 요약·표현과 done 을 같은
    커밋에 담아 LLM 1콜 직후 결과 화면이 열리게 한다. TTS·체크판은 done 이후 후행.

    Returns:
        [(sentence_id, korean), ...] — 이후 TTS 합성 대상.
    """
    call = db.get(Call, call_id)
    if call is not None:
        call.summary = result.summary
        call.mode = result.detected_mode
        # 요구1: 격려 한마디 저장(같은 커밋). 파싱 누락·데모·빈통화 폴백은 자연 None.
        call.feedback = getattr(result, "feedback", "") or None
        call.status = "done"  # P2.6 — 결과 화면 즉시 해제(요약·표현과 같은 커밋)

    pending: list[tuple[int, str]] = []
    seen: set[str] = set()  # 같은 한국어 표현 중복 저장 방지(모델이 가끔 중복 산출)
    for e in result.expressions:
        key = (e.korean or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        s = Sentence(
            call_id=call_id,
            korean_sentence=e.korean,
            native_sentence=e.translation,
            locale=locale,
            source_type=e.source_type,
            is_bookmarked=False,
            evaluation=Evaluation(),  # placeholder(점수 None) — 연습 채점 시 채움
        )
        db.add(s)
        db.flush()  # sentence_id 확보
        pending.append((s.sentence_id, e.korean))
    db.commit()
    return pending


def _set_sentence_tts(db: Session, sentence_id: int, url: str) -> None:
    """표현(Sentence)의 TTS 음성 URL 을 저장한다(public 버킷 재생 URL)."""
    s = db.get(Sentence, sentence_id)
    if s is None:
        return
    s.voice_url = url
    db.commit()


def _voice_for_call(db: Session, call_id: int) -> str | None:
    """통화 캐릭터의 Gemini Live voice 이름(표현 TTS 를 같은 목소리로 내기 위함)."""
    call = db.get(Call, call_id)
    if call is None:
        return None
    ch = db.get(Character, call.character_id)
    return ch.voice.name if (ch and ch.voice and ch.voice.name) else None


async def analyze_call(
    call_id: int,
    client: genai.Client,
    settings_obj: Settings,
    session_factory: sessionmaker,
    *,
    locale: str,
    target_language: str = "한국어",
    locale_label: str | None = None,
    member_id: int | None = None,
    candidates: list[dict] | None = None,
    hinted_from_turn_index: set[int] | None = None,
) -> None:
    """통화 전사를 분석해 표현·요약을 저장하고 표현별 TTS 를 합성한다(전체 graceful).

    통화 종료 후 백그라운드에서 호출된다. 어떤 단계가 실패해도 통화 자체엔 영향이
    없으며, 빈 통화면 status=done(빈 결과), 분석 호출 실패면 failed 로 둔다.

    P2 확장(mechanics ⑤): 기존 1콜에 항목 사용 검출(detections)을 병합하고, 결과를
    검증 게이트→증거·상태전이→레벨업 판정으로 잇는다(한 세션·단일 commit).
    - member_id: 미전달(하위호환) 시 call 행에서 해석.
    - candidates: 검출 후보 [{item_id, kind, surface, example, injected}]. 미전달 시
      기본 구성(practicing 오래된 순 18 + introduced 최신 12, 상한 30 — c2 가 주입
      항목을 넘기기 전의 폴백). 후보 0개면 검출 지시문·필드 자체를 생략(프롬프트 오염 방지).
    - hinted_from_turn_index: 힌트 열람 마커(D16, _verify_detections 참조) — 열람 직후
      첫 USER 턴의 E2/E3 를 E1 로 강등. in-memory 전달(통화 세션 → 분석 태스크)이라
      크래시로 유실되면 과크레딧 1회 허용(mechanics ⑬ — 테이블 신설 대신 수용).
    """
    try:
        dialog_rows = await run_db(session_factory, lambda db: _load_dialog_rows(db, call_id))
        dialog = _dialog_from_rows(dialog_rows)
        if not dialog.strip():
            logger.info("normalcall 분석: 전사 없음 → done(빈 결과) call_id=%s", call_id)
            await run_db(session_factory, lambda db: set_status(db, call_id, "done"))
            return

        # member_id 해석(하위호환 — 기존 호출부는 미전달) + 검출 후보 구성.
        #
        # ⛔ 이 게이트를 `target_language == "한국어"` 로 되돌리지 마라. 앱이 한국어
        #   전용이던 시절엔 "비한국어 = 데모" 였지만, 멀티랭귀지 이후엔 일본어·영어가
        #   정식 학습 언어다. 그 하드코딩 때문에 ja/en 통화는 후보 표도 검출 지시문도
        #   안 붙고 응답 스키마에서 detections 필드 자체가 빠져(schema 분기 참조),
        #   증거가 **구조적으로 0건**이었다. 증거가 없으면 상태 전이가 없고, 전이가
        #   없으면 pick_study_items 정렬 키가 안 변해 매 통화 같은 항목이 나온다
        #   (실측 member=20: ja progress 168행 전부 placement 벌크 그대로, last_seen_at
        #   동일, 통화 852~855 가 전부 "私はカーラです/隣/卵"로 시작).
        #
        #   통화 시작(pick_study_items)은 이미 멀티랭귀지인데 통화 후 검출만 한국어에
        #   묶여 있던, 반쪽짜리 다국어화였다. 판정 기준은 언어가 무엇이냐가 아니라
        #   **그 언어에 커리큘럼이 시드돼 있느냐**(LanguageSpec.has_curriculum)다.
        if member_id is None:
            def _resolve_member(db: Session) -> int | None:
                call = db.get(Call, call_id)
                return call.member_id if call is not None else None

            member_id = await run_db(session_factory, _resolve_member)

        # 후보 선별·검출은 언어 스코프다 — 라벨("일본어")이 아니라 코드("ja")로 건다.
        lang_spec = resolve_language(target_language)
        lang_code = lang_spec.code if lang_spec else "ko"

        cands: list[dict] = []
        if member_id is not None and lang_spec is not None and lang_spec.has_curriculum:
            if candidates is not None:
                cands = candidates
            else:
                cands = await run_db(
                    session_factory,
                    lambda db: mastery_repository.load_default_candidates(
                        db, member_id, language=lang_code
                    ),
                )

        instruction = _analysis_instruction(locale, target_language, locale_label)
        prompt = f"[통화 전사]\n{dialog.strip()}"
        if cands:
            instruction = instruction + "\n" + _DETECTION_INSTRUCTION
            prompt = prompt + "\n\n" + _candidate_table(cands)

        analysis_usage = gemini_analysis.LlmUsage()
        result = await gemini_analysis.generate_structured(
            client,
            settings_obj.JUDGE_MODEL,
            system_instruction=instruction,
            prompt=prompt,
            usage=analysis_usage,
            # 후보 0개면 detections 없는 스키마 — 기존 분석 출력 무변화(하위호환)
            schema=CallAnalysis if cands else _CallAnalysisBase,
            # 추론 예산 상한 명시. 미지정이면 모델 기본값(동적 thinking)이 켜져 추론 토큰이
            # 출력 단가로 무제한 과금된다. 0(완전 비활성)이 아니라 512 인 이유: 이 콜은
            # 후보표(≤30행) 대조 + 인용 검증 게이트로 이어지는 검출 과업이라, 추론을 아예
            # 끄면 인용 정확도가 떨어져 하류 게이트에서 탈락 → 검출 recall 하락 → 증거
            # 적립·레벨업 지연으로 번진다. 동적 예산의 상한만 깎는 절충이다.
            # (대조군: 통화중 사이드카들은 지연이 우선이라 thinking_budget=0.)
            thinking_budget=512,
        )
        # ⛔ 결과 판정 **전에** 남긴다 — 실패해도 그 콜의 토큰은 이미 과금됐다.
        await _save_analysis_usage(session_factory, call_id, analysis_usage)
        if result is None:
            logger.warning("normalcall 분석: _analyze 실패 → failed call_id=%s", call_id)
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
            return

        # P2.6: 요약·표현 저장과 status=done 을 같은 커밋으로 — 여기서 결과 페이지
        # 폴링이 풀린다(TTS×N·체크판을 기다리지 않음).
        pending = await run_db(
            session_factory, lambda db: _save_analysis(db, call_id, result, locale)
        )
        logger.info(
            "normalcall 분석: mode=%s 표현 %d개 → done call_id=%s",
            result.detected_mode, len(pending), call_id,
        )
        # ⭐⭐ **다음 조각이 쓸 요약을 지금 만든다**(2026-08-19). 이어하기 시점에 돌리면
        #   "이어서" 를 누른 사용자가 그만큼 기다린다 — 여기서 만들어 두면 **지연 0** 이다.
        #   ⛔ 원문 전사를 넘기는 게 아니라 **슬롯**(topic·learner_facts·pending)이다.
        #     원문을 그대로 주면 비버가 그 안에서 사실을 다시 찾아야 하고, 길수록 못 찾는다.
        #   ⚠ 실패해도 통화 결과에는 영향이 없다 — 이어하기가 발췌 폴백으로 내려갈 뿐이다.
        try:
            rows = await run_db(
                session_factory, lambda db: _resume_transcript(db, call_id)
            )
            slots = await summarize_for_resume_text(
                client, settings_obj.JUDGE_MODEL, rows
            )
            if slots:
                await run_db(
                    session_factory,
                    lambda db: _save_resume_context(db, call_id, slots),
                )
                logger.info(
                    "normalcall 이어하기 요약: 화제=%r 사실 %d개 하던것=%r call_id=%s",
                    slots.get("topic"), len(slots.get("learner_facts") or []),
                    slots.get("pending"), call_id,
                )
        except Exception as exc:  # noqa: BLE001 — R5
            logger.warning("normalcall 이어하기 요약 실패(발췌로 폴백) — %s", exc)
    except Exception as exc:  # noqa: BLE001 - done 이전(전사/LLM/저장) 실패 → failed
        logger.exception("normalcall 분석: 예외 → failed call_id=%s (%s)", call_id, exc)
        try:
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
        except Exception:  # noqa: BLE001
            pass
        return

    # ------------------------------------------------------------------ #
    # done 이후 후행 단계(P2.6) — 어떤 실패도 status 를 되돌리지 않는다(로그만).
    # 결과 화면은 이미 열렸고, TTS 는 온디맨드 합성(POST /sentences/{id}/tts)이 폴백.
    # ------------------------------------------------------------------ #
    try:
        # 표현별 TTS 합성(Cloud TTS, 대상 언어 Chirp3-HD + 통화 캐릭터 음색) → public 버킷 업로드
        # → Sentence.voice_url(재생 URL). synthesize 는 (bytes, content_type)|None.
        # (멀티랭귀지 + 음색) target_language 로 언어(일본어 문장은 일본어 음성) + call_voice 로
        # 통화 캐릭터 목소리 — 두 축을 함께.
        # 문장 단위 graceful — 한 문장 실패가 나머지 문장·체크판을 막지 않는다.
        call_voice = await run_db(session_factory, lambda db: _voice_for_call(db, call_id))
        # ⭐ 원가 계기판(2026-08-17) — 이 루프가 통화당 문장 수만큼 **과금되는 합성**을 돈다.
        #   여태 한 글자도 안 세고 있었다(실측 call 1046: 문장 8개 합성 → 원가 0원으로 잡힘).
        #   ⛔ 단위는 **문자**다: 이 경로는 Cloud TTS Chirp3-HD(MP3)이고 그 엔진은 문자 과금이다.
        #     (토큰 과금 엔진 Gemini-TTS 는 audio_s 가 있어야 하고 chars 로는 못 잰다 —
        #      판정은 _tts_cost_usd 한 곳이 한다. 여기서 새 산식을 만들지 않는다.)
        #   ⚠ 실패한 합성은 chars 에 안 넣는다(과금 안 됨). 대신 몇 번 실패했는지는 남긴다.
        tts_chars = tts_calls = tts_failed = 0
        for sentence_id, korean in pending:
            try:
                # 언어(_lang_code: 라벨/코드→ISO) + 캐릭터 음색(call_voice) 두 축.
                synthesized = await tts.synthesize(
                    korean, _lang_code(target_language), voice=call_voice
                )  # None 가능(비활성/실패)
                if not synthesized:
                    tts_failed += 1
                    continue
                tts_calls += 1
                tts_chars += len((korean or "").strip())
                audio, content_type = synthesized
                ext = "mp3" if content_type == "audio/mpeg" else "wav"
                path = f"tts/{call_id}/{sentence_id}.{ext}"
                key = storage.upload(
                    settings_obj.SUPABASE_BUCKET_SAMPLES, path, audio, content_type
                )
                url = storage.public_url(settings_obj.SUPABASE_BUCKET_SAMPLES, key) if key else None
                if url:
                    await run_db(
                        session_factory, lambda db, sid=sentence_id, u=url: _set_sentence_tts(db, sid, u)
                    )
            except Exception as exc:  # noqa: BLE001 - 문장 단위 흡수(온디맨드 폴백 존재)
                tts_failed += 1
                logger.warning(
                    "normalcall TTS: 실패(무시 — 온디맨드 폴백) sentence=%s call_id=%s (%s)",
                    sentence_id, call_id, exc,
                )
        await _save_tts_usage(session_factory, call_id, tts_chars, tts_calls, tts_failed)

        # 체크판 파이프라인(검증→증거→전이→레벨업) — 사용자 노출과 무관(D2)한 후행
        # 단계. 단일 commit 원자성은 _apply_call_mastery 내부에서 그대로 유지.
        if member_id is not None:
            detections = list(getattr(result, "detections", None) or [])
            try:
                mastery = await run_db(
                    session_factory,
                    lambda db: _apply_call_mastery(
                        db, call_id, member_id, detections, cands, dialog_rows,
                        hinted_from_turn_index=hinted_from_turn_index,
                    ),
                )
                logger.info(
                    "normalcall 체크판: 검출 %d→검증 %d, 증거 %s, 레벨업 %s call_id=%s",
                    len(detections), mastery["verified"],
                    # ⚠ 검출 0건이면 `evidence`·`levelup` 이 **None** 이다(위 조기 반환 2곳).
                    #   무조건 `.get()` 을 부르면 여기서 터지고, 잡히는 자리가 "체크판 실패"라
                    #   원인이 검출 로직처럼 보인다 — 실제로는 **로그 줄이 범인**이다.
                    #   실측(2026-08-19 call 1085 조각2): 'NoneType' object has no attribute 'get'
                    mastery["evidence"], (mastery["levelup"] or {}).get("result"), call_id,
                )
            except Exception as exc:  # noqa: BLE001 - 체크판 실패는 분석을 죽이지 않음
                logger.exception(
                    "normalcall 체크판: 실패(무시 — done·표현 저장은 완료) call_id=%s (%s)",
                    call_id, exc,
                )
        logger.info("normalcall 분석: 후행 단계(TTS·체크판) 완료 call_id=%s", call_id)
    except Exception as exc:  # noqa: BLE001 - done 이후 실패는 status 무변경(결과 화면 무손상)
        logger.exception(
            "normalcall 분석: done 이후 후행 단계 예외(무시 — status=done 유지) call_id=%s (%s)",
            call_id, exc,
        )


# --------------------------------------------------------------------------- #
# 레벨테스트 통화후 판정 (P1 — docs/20260709_1346 ⑩)
# --------------------------------------------------------------------------- #
class LevelAssessment(BaseModel):
    """레벨테스트 판정 1콜의 전체 출력(7밴드 — 마커 기반).

    ⚠ 필드 순서 = 생성 순서(구조화 출력 내 CoT 강제): 인용 → 추론 → 변주게이트 → 밴드 →
    신뢰도 → 표본질 → 요약 → 학습자 피드백.
    LLM 은 7밴드(chunk/a1/a2/a3/a4/mid/adv/unknown)만 판정하고,
    최종 앱 레벨(1~13) 숫자는 서버가 소유한다(AI 는 증인, 코드가 심판 — 관통 원칙 1).
    band → level_no 배정은 _place_from_band(_BUCKET_LEVEL 룩업 + 변주 게이트)이 확정한다.
    """

    evidence: list[str] = Field(
        description="학습자(USER)의 한국어 발화 원문 인용, 최대 5개(수정·번역 금지)"
    )
    reasoning: str = Field(description="인용을 근거로 한 판정 추론(한국어)")
    distinct_structures: int = Field(
        default=0,
        description="학습자가 서로 다른 문법 구조를 몇 개 productive하게 산출했나(변주 게이트). "
        "같은 문장 반복이나 인상적 문장 하나만 외워 말하고 나머지는 못 하면 ≤1 — 서버가 레벨을 낮게 캡한다.",
    )
    band: Literal["chunk", "a1", "a2", "a3", "a4", "mid", "adv", "unknown"] = Field(
        description="7밴드(마커 기반) — chunk=A0(정형표현·단어) / a1=A1(조사+현재/과거 활용 단문) / "
        "a2=A2(이유·미래·희망·조건) / a3=A3(명사인용·추측·비교·관형) / a4=A4(경험·허락·변화·반말) / "
        "mid=B중급(동사간접화법·피동) / adv=C고급(격식·논증·추상) / unknown=표본 부족"
    )
    confidence: Literal["high", "medium", "low"] = Field(description="판정 신뢰도")
    sample_quality: Literal["sufficient", "sparse", "none"] = Field(
        description="한국어 발화 표본질 — 2턴 이하면 sparse/none"
    )
    summary: str = Field(description="통화 핵심 소재 요약(학습자 모국어 명사구 2~4어절)")
    feedback_for_learner: str = Field(
        description="학습자에게 보여줄 격려 1~2문장(모국어, 레벨·점수 숫자 금지)"
    )


class LeveltestVerdict(BaseModel):
    """레벨테스트 한 답변의 사이드카 O/X 판정 출력(단일 문항 채점).

    heard_grammar 는 판정 근거가 된 학습자의 실제 한국어 구절을 그대로 인용한다
    (추측·재작성 금지). 관통원칙3: pass 는 이 인용이 실제 발화에 실재해야만
    데이터로 인정된다(환각 방어 — 호출부에서 부분 문자열로 교차 검증).
    """

    result: Literal["pass", "fail", "unclear", "no_attempt"] = Field(
        description="pass=목표 문법 실재 / fail=답을 시도했으나 목표 미충족 / "
        "unclear=애매·부분정답 / no_attempt=아직 진짜 답을 시도 안 함(머뭇·필러·인사만)"
    )
    heard_grammar: str = Field(
        default="",
        description="판정 근거로 들린 학습자의 실제 한국어 구절(원문 인용, 없으면 빈 문자열)",
    )


# ── 레벨테스트 7밴드(마커 기반) — 라이브 분류기·통화후 판정관 공유 정의 ──────────
# OPI 재설계(2026-07-24): 4버킷(survival/beginner/intermediate/advanced)을 커리큘럼 문법
# 마커에 맞춘 7밴드(chunk/a1/a2/a3/a4/mid/adv)로 확장한다. 각 밴드는 '바로 위 밴드 마커의
# 부재'로 상한이 정의된다(마커 체크리스트). 골든 전사·유도 시뮬로 검증된 KO 기준(sim_elicit
# JUDGE)을 이식. 이 정의를 _leveltest_instruction(통화후)·_leveltest_turn_instruction(통화중)이
# 공유해 두 판정기가 같은 마커 잣대를 쓰게 한다(정의 불일치가 저평가·정합 버그의 원인이었음).
# (멀티랭귀지) 버킷 정의는 언어별 — 문법 마커·예시가 대상 언어로 갈린다. 판정 근거는 대상
# 언어 발화만이므로, 대상 언어에 맞는 정의를 써야 학습자 모국어를 실력으로 오독하지 않는다.
# KO 만 정밀 검증됨 — JA/EN/CN/FR/VI 는 같은 7밴드 구조로 확장(언어별 마커 검증 필요).
_BUCKET_DEFINITIONS_KO = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 문법 마커로 읽어라. "
    "각 밴드는 '바로 위 밴드 마커의 부재'로 상한이 정해진다]\n"
    "- chunk(L1): 정형표현(안녕하세요·감사합니다·이거 주세요)·숫자·이름·단어, 또는 조사·활용 없는 전보식 "
    "나열('저 서울','김치 좋다'). 새 문장 생성 없음.\n"
    "- a1(L2): 조사(을/를·이/가·에·에서)+현재/과거 활용 종결(-아요/어요·-았/었어요·N이에요)로 기초 단문. "
    "고/지만/안까지. (A2 마커 없음)\n"
    "- a2(L3): +이유 -아서·미래 -(으)ㄹ 거예요·희망 -고 싶다·조건 -(으)면·진행 -고 있다·능력 -(으)ㄹ 수 있다·"
    "현재관형 -은/-는 N.\n"
    "- a3(L4): +명사인용 N(이)라고 하다·추측 -(으)ㄴ/는 것 같다/-겠-·이유 -(으)니까·비교 -보다·과거/미래 관형 "
    "-(으)ㄴ N·-(으)ㄹ N·명사화 -는 것·배경 -는데·나열 -거나/-다가.\n"
    "- a4(L5): +경험 -(으)ㄴ 적 있다·허락 -아도 되다·변화 -게 되다/-아지다·순서 -기 전에·의도 -(으)ㄹ까 하다·"
    "반말 -다.\n"
    "- mid(L6): 동사 간접화법 -다고/-냐고/-자고 하다·전언체 -대(요)·피동 -이/히/리/기-·-느라고/-자마자/-더니·"
    "목적 -기 위해·사동 -게 하다.\n"
    "- adv(L10): 격식·문어체·논증·인용확장·-(으)ㅁ에도 불구하고 등 C급, 추상/사회 주제를 길고 논리적으로.\n"
    "★ 간접화법 형태 구분: 명사인용 N(이)라고 하다=a3 / 동사인용 -다고/-냐고/-자고 하다=mid.\n"
    "★ 발음·조사 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자·맞춤법이 아니라 '문법 마커의 폭과 복잡도'로 판정.\n"
    "★ 학습자가 자발적으로 산출한 '가장 높은' 마커로 밴드를 정한다. 짧게 답해도 상위 마커가 있으면 그 밴드로 읽어라."
)

# 일본어 버킷 정의(7밴드 확장 — 언어별 검증 필요) — 한국어와 같은 마커 잣대, 마커·예시만 일본어.
_BUCKET_DEFINITIONS_JA = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 문법 마커로 읽어라. "
    "각 밴드는 '바로 위 밴드 마커의 부재'로 상한이 정해진다]\n"
    "- chunk(L1): 정형표현(こんにちは·ありがとうございます·これをください)·숫자·이름·단어, 또는 조사·활용 없는 "
    "전보식 나열('キムチ 好き','自転車 好き'). 새 문장 생성 없음.\n"
    "- a1(L2): 「〜は〜です」명사문·조사(は・も・を・の)·「〜が好き」·「〜を〜ます」로 기초 단문. (A2 마커 없음)\n"
    "- a2(L3): +존재 〜がある/いる·이동 〜に行く·권유 〜ませんか/ましょう·이유 〜から·형용사 수식·희망 〜が欲しい.\n"
    "- a3(L4): +진행 〜ている·과거 〜でした/ました·시간 〜とき·변화 〜になる/くなる·명사화 〜こと.\n"
    "- a4(L5): +희망 〜たい·시도 〜てみる·방향 〜ていく/くる·정중 의뢰 〜てくださいませんか·비교 〜と〜とどちらが.\n"
    "- mid(L6): 인용 〜という/〜と言っていました·이유 〜ので·조건 〜なら·금지 〜てはいけない·〜てから·양태 〜そうだ·수동 〜(ら)れる.\n"
    "- adv(L10): 격식·문어체·구어 〜っていうか·완곡 〜ないこともない/〜に違いない·격식 접속(〜にあたって 등) C급, 추상 주제를 길고 논리적으로.\n"
    "★ 발음·조사 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자·맞춤법이 아니라 '문법 마커의 폭과 복잡도'로 판정.\n"
    "★ 학습자가 자발적으로 산출한 '가장 높은' 마커로 밴드를 정한다. 짧게 답해도 상위 마커가 있으면 그 밴드로 읽어라."
)

# 영어 버킷 정의(7밴드 확장 — 언어별 검증 필요) — 한국어와 같은 마커 잣대, 마커·예시만 영어.
_BUCKET_DEFINITIONS_EN = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 문법 마커로 읽어라. "
    "각 밴드는 '바로 위 밴드 마커의 부재'로 상한이 정해진다]\n"
    "- chunk(L1): 정형표현(Hello·Thank you·Excuse me)·숫자·이름·단어, 또는 관사·시제 없는 전보식 나열"
    "('I go school','me happy'). 새 문장 생성 없음.\n"
    "- a1(L2): be동사·관사(a/an/the)·현재형(I like...)·복수로 주어+동사+목적어 짧은 단문. (A2 마커 없음)\n"
    "- a2(L3): +과거형(-ed·went)·be going to 미래·can/can't·비교급(-er/more)·there is/are.\n"
    "- a3(L4): +현재진행·현재완료 경험(have been/done)·have to/should·빈도부사.\n"
    "- a4(L5): +will 미래·1형 조건문(if+present)·최상급·동명사/부정사(to/-ing).\n"
    "- mid(L6): 2형 조건문(if+past)·관계대명사(who/which/that)·수동태·간접화법(reported speech)·과거진행·현재완료진행.\n"
    "- adv(L10): 부정어 도치·담화표지·미묘한 양태·명사화·격식체 C급, 추상/사회 주제를 길고 논리적으로.\n"
    "★ 발음·문법 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자·맞춤법이 아니라 '문법 마커의 폭과 복잡도'로 판정.\n"
    "★ 학습자가 자발적으로 산출한 '가장 높은' 마커로 밴드를 정한다. 짧게 답해도 상위 마커가 있으면 그 밴드로 읽어라."
)

# 중국어 버킷 정의(7밴드 확장 — 언어별 검증 필요) — 한국어와 같은 마커 잣대, 마커·예시만 중국어.
_BUCKET_DEFINITIONS_CN = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 문법 마커로 읽어라. "
    "각 밴드는 '바로 위 밴드 마커의 부재'로 상한이 정해진다]\n"
    "- chunk(L1): 정형표현(你好·谢谢·多少钱)·숫자·이름·단어, 또는 양사·'了' 없는 전보식 나열"
    "('我去学校','我高兴'). 새 문장 생성 없음.\n"
    "- a1(L2): '是'·'有'·대명사·양사(量词)·숫자·'吗?' 의문으로 주어+술어+목적어 짧은 단문. (A2 마커 없음)\n"
    "- a2(L3): +'了' 완료·능원동사(想/要/会/能)·비교 '比'·'因为...所以'·정도부사.\n"
    "- a3(L4): +동사중첩·'过' 경험·동량사/시량사·형용사중첩.\n"
    "- a4(L5): +결과보어·추향보어(来/去)·'又...又...'·'...以前/以后'.\n"
    "- mid(L6): '被' 피동·离合词·가능보어·정도보어·간접인용('他说...')·접사(第-/老-).\n"
    "- adv(L10): 격식 접속('从...来看'·'在...看来'·'到...为止')·문어체 C급, 추상/사회 주제를 길고 논리적으로.\n"
    "★ 발음(성조)·양사 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자가 아니라 '문법 마커의 폭과 복잡도'로 판정.\n"
    "★ 학습자가 자발적으로 산출한 '가장 높은' 마커로 밴드를 정한다. 짧게 답해도 상위 마커가 있으면 그 밴드로 읽어라."
)

# 프랑스어 버킷 정의(7밴드 확장 — 언어별 검증 필요) — 한국어와 같은 마커 잣대, 마커·예시만 프랑스어.
_BUCKET_DEFINITIONS_FR = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 문법 마커로 읽어라. "
    "각 밴드는 '바로 위 밴드 마커의 부재'로 상한이 정해진다]\n"
    "- chunk(L1): 정형표현(Bonjour·Merci·Combien)·숫자·이름·단어, 또는 활용·성수일치 없는 전보식 나열"
    "('Je aller école','moi content'). 새 문장 생성 없음.\n"
    "- a1(L2): être/avoir·관사(le/la/un/une)·-er 동사 현재형·형용사 성수일치·소유형용사로 짧은 단문. (A2 마커 없음)\n"
    "- a2(L3): +passé composé·futur proche(aller+inf)·부정(ne...pas)·비교(plus...que).\n"
    "- a3(L4): +imparfait·목적격 대명사(COD/COI)·재귀동사.\n"
    "- a4(L5): +futur simple·1형 가정(si+présent)·관계대명사(qui/que)·부분관사(du/de la).\n"
    "- mid(L6): subjonctif présent·conditionnel présent·관계대명사(dont/où)·discours indirect(간접화법)·수동태.\n"
    "- adv(L10): connecteurs logiques·subjonctif 뉘앙스·명사화·격식체 C급, 추상/사회 주제를 길고 논리적으로.\n"
    "★ 발음·성수일치 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자가 아니라 '문법 마커의 폭과 복잡도'로 판정.\n"
    "★ 학습자가 자발적으로 산출한 '가장 높은' 마커로 밴드를 정한다. 짧게 답해도 상위 마커가 있으면 그 밴드로 읽어라."
)

# 베트남어 버킷 정의(7밴드 확장 — 언어별 검증 필요) — 한국어와 같은 마커 잣대, 마커·예시만 베트남어.
_BUCKET_DEFINITIONS_VI = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 문법 마커로 읽어라. "
    "각 밴드는 '바로 위 밴드 마커의 부재'로 상한이 정해진다]\n"
    "- chunk(L1): 정형표현(Xin chào·Cảm ơn·Bao nhiêu tiền)·숫자·이름·단어, 또는 시제표지·분류사 없는 전보식 "
    "나열('Tôi đi trường','tôi vui'). 새 문장 생성 없음.\n"
    "- a1(L2): 'là'·'có'·인칭대명사·분류사·숫자·'có...không?' 의문·cũng/đều로 짧은 단문. (A2 마커 없음)\n"
    "- a2(L3): +'đã' 과거·'đã...chưa?' 완료·시간 의문·'à/chứ' 의문.\n"
    "- a3(L4): +'muốn/định'·비교 'bằng'·복수(những/các)·'ai cũng'.\n"
    "- a4(L5): +'sắp'·'vừa...vừa'·'vì...nên'·'hình như...thì phải'.\n"
    "- mid(L6): 'được'(가능/피동)·간접화법 'nói rằng'·'tự...lấy'·어기조사(nhỉ/nhé)·'hóa ra'.\n"
    "- adv(L10): 격식 구조·담화표지·'kẻ...người...'·부정확 수량 표현 C급, 추상/사회 주제를 길고 논리적으로.\n"
    "★ 발음(성조)·표지 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자가 아니라 '문법 마커의 폭과 복잡도'로 판정.\n"
    "★ 학습자가 자발적으로 산출한 '가장 높은' 마커로 밴드를 정한다. 짧게 답해도 상위 마커가 있으면 그 밴드로 읽어라."
)

_BUCKET_DEFINITIONS_BY_LANG: dict[str, str] = {
    "ko": _BUCKET_DEFINITIONS_KO,
    "ja": _BUCKET_DEFINITIONS_JA,
    "en": _BUCKET_DEFINITIONS_EN,
    "zh": _BUCKET_DEFINITIONS_CN,
    "fr": _BUCKET_DEFINITIONS_FR,
    "vi": _BUCKET_DEFINITIONS_VI,
}


def _lang_code(target_language: str) -> str:
    """대상 언어 라벨('일본어')/코드('ja') → ISO 코드. 미지원·미상은 'ko' 폴백."""
    from core.languages import resolve_language

    spec = resolve_language(target_language)
    return spec.code if spec is not None else "ko"


def _bucket_definitions(target_language: str) -> str:
    """대상 언어의 7밴드 마커 정의. 미등록 언어는 한국어 정의로 폴백."""
    return _BUCKET_DEFINITIONS_BY_LANG.get(_lang_code(target_language), _BUCKET_DEFINITIONS_KO)

# 7밴드 → 최종 앱 레벨(1~13). 초급은 촘촘히(chunk~a4 = L1~5), 중급·고급은 각 1칸 coarse
# (mid=L6·adv=L10 — 자동 레벨업이 내부를 채운다). 각 밴드가 '확실히 할 수 있는' 바닥에 보수
# 배정 — 과배치(강등 불가 → plateau) 방지, 부족분은 체크판 자동 레벨업이 단조 상승으로 회복.
# (sim_elicit.py BAND 와 동일: chunk1/a1_2/a2_3/a3_4/a4_5/mid_6/adv_10)
_BUCKET_LEVEL: dict[str, int] = {
    "chunk": 1,   # A0 생존 — 정형표현·단어·전보식
    "a1": 2,      # A1 초급1 — 조사+현재/과거 활용 단문
    "a2": 3,      # A2~A4 초급 상단 — 이유·미래·희망·조건 + 명사인용·추측·경험·허락 (넓은 레벨: a3/a4 흡수)
    "a3": 3,      # ⚠ 넓은 레벨(1,2,3,6,10): a3(A3)를 L3에 흡수 — 유도난이도↑·사용자 적음. 판정관은 마커 탐지만.
    "a4": 3,      # ⚠ 넓은 레벨: a4(A4)도 L3에 흡수. 초급 상단 정밀도는 자동 레벨업이 회복.
    "mid": 6,     # B 중급 — 동사간접화법·피동 (coarse)
    "adv": 10,    # C 고급 — 격식·논증·추상 (coarse)
}

# 표본 하한: USER 목표어 발화(공백 제외)가 이 미만이면 LLM 콜 없이 최하 레벨을 배정한다.
# (예전엔 "판정 스킵·미저장"이었다 — analyze_level_test_call 의 해당 분기 주석 참조.)
_MIN_LEVELTEST_USER_CHARS = 20

# 표본 미달 시 배정할 레벨. L1 = 생존 회화(정형 표현·숫자) — 목표어를 20자도 못 만든
# 학습자의 자리다. _BUCKET_LEVEL 의 "chunk" 밴드와 같은 값이며, 이 둘은 같이 움직여야 한다.
_MIN_LEVEL_NO = 1

# 루브릭 파일(운영 튜닝용 — 존재하면 전문이 {rubric} 슬롯에 들어간다). 없으면 아래 상수.
_LEVELTEST_RUBRIC_PATH = (
    Path(__file__).resolve().parents[3] / "assets" / "level" / "leveltest_rubric.md"
)

# 기본 루브릭 — assets/level/level_profiles_13.json 의 13개 profile 을 시드로 한
# 레벨별 1줄 요약(수기 압축, 파일 폴백용 하드코딩). 판정관이 밴드→단계를 고르는 기준표.
_DEFAULT_LEVELTEST_RUBRIC = """레벨 1 (A0, 생존 회화): 정형 표현(안녕하세요·감사합니다·이거 주세요)과 숫자만. 배운 문장 그대로가 발화의 전부.
레벨 2 (초급 A, beginner 1): 인사·자기소개. 'N이에요/예요', '있어요/없어요', 현재 '-아요/어요'·과거 '-았/었-', 조사 을/를·에·에서, '안' 부정. 5~10음절 단문.
레벨 3 (초급 A, beginner 2): +높임 '-(으)시-', 이유 '-아서/어서', 미래 '-(으)ㄹ 거예요', 진행 '-고 있다', 희망 '-고 싶다', 조건 '-(으)면'.
레벨 4 (초급 A, beginner 3): 간접화법 시작 'N(이)라고 하다', 추측 '-는 것 같다', 비교 'N보다', '-(으)ㄹ 때'. 짧은 복문으로 추측·비교·조건 표현.
레벨 5 (초급 A, beginner 4): 경험 '-(으)ㄴ 적 있다', 허락·금지 '-아도 되다/-(으)면 안 되다', 변화 '-게 되다', 비유 'N처럼'. 일상 대부분을 짧은 복문으로.
레벨 6 (중급 B, intermediate 1): 간접화법 본격('-다고/-냐고/-자고/-(으)라고 하다'), 전언 '-대(요)', 피동, '-느라고', '-자마자'. 문장이 길어지고 화법이 다양.
레벨 7 (중급 B, intermediate 2): 추측 '-나 보다', 예정 '-(으)ㄹ 예정/계획이다', 회상·후회 '-(으)ㄹ걸 그랬다', 가정 '-다면'. 의도·추측·후회 복문.
레벨 8 (중급 B, intermediate 3): '-다 보면', '-(으)ㄹ 뿐만 아니라', '-는 바람에', 양보 '-더라도', 대조 '-(으)ㄴ 반면에', 명사화 '-(으)ㅁ'. 관용 표현 혼용.
레벨 9 (중급 B, intermediate 4): '-(으)ㄹ 리(가) 없다', 근거 'N에 의하면', 격식 이유 '-(으)므로', 회상 '-더라고요'. 추론·근거·격식 표현.
레벨 10 (고급 C, advanced 1): 문어·격식체 진입 — 'N(으)로 인해', '-(으)ㅁ에 따라', '-다시피', 통계·설명 담화. 추상·사회 주제를 길고 논리적으로.
레벨 11 (고급 C, advanced 2): 관용·강조 '뭐니 뭐니 해도', '-기 십상이다', '-(으)ㄹ 법하다', 논평 '-다는 지적이 있다'. 뉘앙스·평가·논평을 자연스럽게.
레벨 12 (고급 C, advanced 3): 격식 문어와 강한 관용 — '-(으)ㅁ에도 불구하고', 'N마저/조차', 강조·대조 구문. 정교하고 격식 있는 긴 발화.
레벨 13 (고급 C, advanced 4): 거의 원어민. 미묘한 뉘앙스·완곡·속담, 토론·설득·추상 담화, 상황에 맞는 격식 조절."""


# 일본어 루브릭 — level_profiles_ja(사다리 앵커) 기반 레벨별 1줄 요약. 판정관이 밴드→단계 이해용.
_DEFAULT_LEVELTEST_RUBRIC_JA = """레벨 1 (A0, 생존 회화): 정형 표현(こんにちは·ありがとうございます·これをください)과 숫자만. 배운 문장 그대로가 발화의 전부.
레벨 2 (초급 A, beginner 1 / A1): 「〜は〜です」 명사문, 조사 は・も・を・の, 「〜が好き」, 기본 동사 「〜を〜ます」. です・ます 짧은 단문.
레벨 3 (초급 A, beginner 2 / A2): 존재 「〜がある/いる」, 이동 「〜に行く」, 권유 「〜ませんか/ましょう」, 이유 「〜から」, 형용사 수식, 「〜が欲しい」.
레벨 4 (초급 A, beginner 3 / A3): 진행 「〜ている」, 과거 「〜でした/ました」, 「〜とき」, 변화 「〜になる/くなる」, 명사화 「〜こと」.
레벨 5 (초급 A, beginner 4 / A4): 희망 「〜たい」, 시도 「〜てみる」, 방향 「〜ていく/くる」, 정중 의뢰 「〜てくださいませんか」, 비교 「〜と〜とどちらが」.
레벨 6 (중급 B, intermediate 1 / B1): 인용 「〜という」, 이유 「〜ので」, 조건 「〜なら」, 금지 「〜てはいけない」, 「〜てから」, 양태 「〜そうだ」.
레벨 7 (중급 B, intermediate 2 / B2): 목적 「〜ように」, 「〜たり」, 완료 「〜てしまう」, 난이 「〜にくい/やすい」, 비교 「〜のほうが」, 「〜ても」.
레벨 8 (중급 B, intermediate 3 / B3): 의무 「〜なければならない」, 전문 「〜って言っていました」, 조건 「〜ば」, 「〜けど〜から」 복합 접속.
레벨 9 (중급 B, intermediate 4 / B4): 경어 「尊敬語·お〜になる」, 추량 「〜みたいだ/でしょうか」, 「〜はじめる」, 「〜ておく」, 「〜たあと」.
레벨 10 (고급 C, advanced 1 / C1): 구어 「〜っていうか」, 수동·자발 「〜(ら)れる」, 완곡 「〜ないこともない」, 「〜に違いない」. 추상 주제.
레벨 11 (고급 C, advanced 2 / C2): 격식 접속 「〜にあたって/に先立って/をきっかけに/ゆえに/に伴って」. 문어·논설 수준.
레벨 12 (고급 C, advanced 3 / C3): 고급 관용 「〜を余儀なくされる/を禁じ得ない/ならでは/たる者」. 원어민 서면 수준.
레벨 13 (고급 C, advanced 4 / C4): 최상급 문어·수사 「〜にもほどがある/それまでだ/に相違ない」. 뉘앙스·완곡·반어 자유자재."""


# 영어 루브릭 — 표준 영어 CEFR 레벨별 1줄 요약. 판정관이 밴드→단계 이해용.
_DEFAULT_LEVELTEST_RUBRIC_EN = """레벨 1 (A0, 생존 회화): 정형 표현(Hello·Thank you·Excuse me)과 숫자만. 배운 문장 그대로가 발화의 전부.
레벨 2 (초급 A, beginner 1 / A1): be동사·관사(a/an/the)·현재형('I like...')·복수. 주어+동사+목적어 짧은 단문.
레벨 3 (초급 A, beginner 2 / A2): 과거형(-ed·went)·be going to 미래·can/can't·비교급·there is/are.
레벨 4 (초급 A, beginner 3 / A3): 현재진행·현재완료(경험)·have to/should·빈도부사. 시제 구분.
레벨 5 (초급 A, beginner 4 / A4): will 미래·1형 조건문·현재완료 vs 과거·최상급·동명사/부정사.
레벨 6 (중급 B, intermediate 1 / B1): 현재완료진행·과거진행·2형 조건문·관계대명사·수동태·간접화법 도입.
레벨 7 (중급 B, intermediate 2 / B2): 3형 조건문·과거완료·전 시제 수동·추측 조동사(must/might)·비제한 관계절.
레벨 8 (중급 B, intermediate 3 / B3): 혼합 조건문·wish/if only·사역(have sth done)·분사구문·고급 조동사.
레벨 9 (중급 B, intermediate 4 / B4): 기본 도치·분열문(It/What cleft)·미래완료/진행·고급 수동·가정법.
레벨 10 (고급 C, advanced 1 / C1): 부정어 도치·담화 표지·미묘한 양태·생략·전치. 추상 주제를 길게.
레벨 11 (고급 C, advanced 2 / C2): 복합 분열문·고급 도치·완곡/hedging·명사화·격식체. 논설 수준.
레벨 12 (고급 C, advanced 3 / C3): 관용 표현·미묘한 강조·문어적 수사. 원어민 서면 수준.
레벨 13 (고급 C, advanced 4 / C4): 수사·완곡·반어·레지스터 자유자재. 문학·전문 담화 수준."""

# 중국어 루브릭 — 표준 중국어(HSK/CEFR) 레벨별 1줄 요약.
_DEFAULT_LEVELTEST_RUBRIC_CN = """레벨 1 (A0, 생존 회화): 정형 표현(你好·谢谢·多少钱)과 숫자만. 배운 문장 그대로가 발화의 전부.
레벨 2 (초급 A, beginner 1 / A1): '是'·'有'·대명사·양사(量词)·숫자·'吗?' 의문. 주어+술어+목적어 짧은 단문.
레벨 3 (초급 A, beginner 2 / A2): '了' 완료·능원동사(想/要/会/能)·비교 '比'·'因为...所以'·정도부사.
레벨 4 (초급 A, beginner 3 / A3): 동사중첩·'过' 경험·동량사/시량사·형용사중첩. 진행·경험 구분.
레벨 5 (초급 A, beginner 4 / A4): 결과보어·추향보어(来/去)·'又...又...'·'(在)...以前/以后'.
레벨 6 (중급 B, intermediate 1 / B1): '被' 피동·离合词·양사중첩·접사(第-/老-/小-). 施事·受事 명확.
레벨 7 (중급 B, intermediate 2 / B2): 가능보어·'越...越...'·'一...也/都+不/没'·정도보어.
레벨 8 (중급 B, intermediate 3 / B3): 차용양사·'(自)...以来'·'在...方面/上/下'. 관용 표현 혼용.
레벨 9 (중급 B, intermediate 4 / B4): 让步复句·반문구·이중부정 강조·'连...也/都...' 강조.
레벨 10 (고급 C, advanced 1 / C1): '从...来看'·'到...为止'·'拿...来说'·'在...看来'. 추상 주제를 길게.
레벨 11 (고급 C, advanced 2 / C2): 유사접사(超-/-化/-式)·'为了...而...'·'非...不可' 강조. 논설 수준.
레벨 12 (고급 C, advanced 3 / C3): '所谓...就是...'·'无非...而已'·'以...为...'. 원어민 서면 수준.
레벨 13 (고급 C, advanced 4 / C4): '话又说回来'·'X了又Y' 등 구어·문어 수사 자유자재. 문학·전문 담화 수준."""

# 프랑스어 루브릭 — 표준 프랑스어 CEFR 레벨별 1줄 요약.
_DEFAULT_LEVELTEST_RUBRIC_FR = """레벨 1 (A0, 생존 회화): 정형 표현(Bonjour·Merci·Combien)과 숫자만. 배운 문장 그대로가 발화의 전부.
레벨 2 (초급 A, beginner 1 / A1): être/avoir·관사(le/la/un/une)·-er 동사 현재형·형용사 성수일치·소유형용사. 짧은 단문.
레벨 3 (초급 A, beginner 2 / A2): passé composé·futur proche(aller+inf)·부정(ne...pas)·비교(plus...que).
레벨 4 (초급 A, beginner 3 / A3): imparfait·목적격 대명사(COD/COI)·재귀동사. 과거 묘사·구분.
레벨 5 (초급 A, beginner 4 / A4): futur simple·1형 가정(si+présent)·관계대명사(qui/que)·부분관사.
레벨 6 (중급 B, intermediate 1 / B1): subjonctif présent·conditionnel présent·관계대명사(dont/où)·passé composé vs imparfait.
레벨 7 (중급 B, intermediate 2 / B2): subjonctif 완전·plus-que-parfait·수동태·2형 가정(si+imparfait)·gérondif.
레벨 8 (중급 B, intermediate 3 / B3): conditionnel passé·subjonctif passé·3형 가정·복잡 관계절.
레벨 9 (중급 B, intermediate 4 / B4): mise en relief(c'est...que)·도치 의문·고급 수동·담화 연결.
레벨 10 (고급 C, advanced 1 / C1): connecteurs logiques·subjonctif 뉘앙스·participe présent. 추상 담화를 길게.
레벨 11 (고급 C, advanced 2 / C2): 명사화·완곡·격식체·복잡 종속. 논설 수준.
레벨 12 (고급 C, advanced 3 / C3): passé simple(문어)·관용 표현·미묘한 강조. 원어민 서면 수준.
레벨 13 (고급 C, advanced 4 / C4): 수사·완곡·반어·레지스터 자유자재. 문학·전문 담화 수준."""

# 베트남어 루브릭 — 표준 베트남어 레벨별 1줄 요약.
_DEFAULT_LEVELTEST_RUBRIC_VI = """레벨 1 (A0, 생존 회화): 정형 표현(Xin chào·Cảm ơn·Bao nhiêu tiền)과 숫자만. 배운 문장 그대로가 발화의 전부.
레벨 2 (초급 A, beginner 1 / A1): 'là'·'có'·인칭대명사·분류사·숫자·'có...không?' 의문·cũng/đều. 짧은 단문.
레벨 3 (초급 A, beginner 2 / A2): 'đã...chưa?' 완료·시간 의문·'à/chứ' 의문. 어제 한 일을 시간과 함께.
레벨 4 (초급 A, beginner 3 / A3): 'muốn/định'·비교 'bằng'·복수(những/các)·'ai cũng'. 요청·비교.
레벨 5 (초급 A, beginner 4 / A4): 'sắp'·'vừa...vừa'·'vì...nên'·'hình như...thì phải'. 계획·이유.
레벨 6 (중급 B, intermediate 1 / B1): 'được'(가능/피동)·'tự...lấy'·어기조사(nhỉ/nhé)·'hóa ra'.
레벨 7 (중급 B, intermediate 2 / B2): 중첩(sáng sáng)·'nói chung/riêng'·'một mặt...mặt khác'. 관용 혼용.
레벨 8 (중급 B, intermediate 3 / B3): 'trừ/kể cả'·'sự+동사' 명사화·복잡 의문. 추상 전개.
레벨 9 (중급 B, intermediate 4 / B4): 'quả là'·'huống chi'·중첩 강조. 강조·추론.
레벨 10 (고급 C, advanced 1 / C1): 'kẻ...người...'·부정확 수량 표현·격식 구조. 추상 주제를 길게.
레벨 11 (고급 C, advanced 2 / C2): 'biết đâu đấy'·'liệu+'·담화 표지. 논설 수준.
레벨 12 (고급 C, advanced 3 / C3): 복잡 구문·동사 변별(mời/nhờ/khuyên). 원어민 서면 수준.
레벨 13 (고급 C, advanced 4 / C4): 'Cứ+동사+đi'·'dù sao...cũng' 등 구어·문어 수사 자유자재. 문학·전문 담화 수준."""


def _load_leveltest_rubric(target_language: str = "한국어") -> str:
    """루브릭 텍스트 로드 — 대상 언어별. 한국어는 파일(assets/level/leveltest_rubric.md)→상수,
    그 외 지원 언어는 해당 언어 상수. 미등록 언어는 한국어 상수 폴백."""
    code = _lang_code(target_language)
    if code == "ja":
        return _DEFAULT_LEVELTEST_RUBRIC_JA
    if code == "en":
        return _DEFAULT_LEVELTEST_RUBRIC_EN
    if code == "zh":
        return _DEFAULT_LEVELTEST_RUBRIC_CN
    if code == "fr":
        return _DEFAULT_LEVELTEST_RUBRIC_FR
    if code == "vi":
        return _DEFAULT_LEVELTEST_RUBRIC_VI
    try:
        if _LEVELTEST_RUBRIC_PATH.is_file():
            text = _LEVELTEST_RUBRIC_PATH.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError as exc:
        logger.warning("leveltest 루브릭 파일 읽기 실패 → 상수 폴백: %s", exc)
    return _DEFAULT_LEVELTEST_RUBRIC


def _leveltest_instruction(
    locale: str, rubric: str, locale_label: str | None = None,
    target_language: str = "한국어",
) -> str:
    """레벨테스트 판정관 시스템 지시문. 근거는 USER 의 **대상 언어** 발화만(모국어 배제),
    ASR 왜곡 주의, 판정 절차(인용→밴드→단계→레벨) 강제, 망설여지면 낮은 쪽.

    (멀티랭귀지) target_language 로 측정 대상 언어를 지정한다(기본 한국어 — 프로덕션
    출력 무손상). 도그푸딩처럼 **학습자 모국어와 서버 메타언어가 같은 경우**(한국인이
    일본어 학습), 대상 언어를 명시하지 않으면 판정관이 학습자의 모국어(한국어)를 실력으로
    오독한다 — 그래서 '{target} 발화만 근거, 모국어는 배제'를 대상 언어로 못박는다.
    """
    label = locale_label or _LOCALE_LABEL.get(locale, _LOCALE_LABEL["en"])
    t = target_language
    return (
        f"너는 {t} 학습자와 AI 선생님(BEAVER)의 레벨테스트 통화 전사를 보고 학습자의 "
        f"{t} 레벨(1~13)을 판정하는 도구다. JSON 으로만 출력하라.\n"
        "[근거 규칙]\n"
        f"- 판정 근거는 오직 [USER]의 '{t}' 발화뿐이다. [BEAVER](선생님) 발화와 USER 의 "
        "모국어 발화는 실력의 근거가 아니다(비버를 따라 말한 직후의 단순 반복도 약한 근거로만).\n"
        "- 전사는 음성인식(ASR) 결과라 철자·띄어쓰기가 왜곡될 수 있다. 철자·맞춤법을 기준으로 "
        "삼지 말고, 사용한 문법의 폭(문형 다양성)·어휘 등급·응답 길이·질문에 맞게 대응했는지를 "
        "기준으로 삼아라.\n"
        + _bucket_definitions(t) + "\n"
        "[변주 게이트 — 매우 중요] distinct_structures: 학습자가 '서로 다른' 문법 구조를 몇 개 자발적으로 "
        "산출했는가. 같은 문장 반복이나, 인상적인 문장 '하나'만 외워 말하고 나머지는 못 하면 ≤1 이다 — 이땐 "
        "그 문장이 아무리 복잡해도 암기지 실력이 아니므로 서버가 밴드를 낮게 캡한다(암기 하나 ≠ 실력).\n"
        "[증거 가중 — 비대칭 채점]\n"
        "- 자발 성공(비버가 문형을 깔아 주지 않았는데 학습자가 스스로 그 마커를 만들어 냄) = 강한 양성. "
        "그 수준을 밴드로 인정할 근거가 된다.\n"
        "- 유도 성공(비버가 예시·선택지로 떠먹여 준 뒤에야 성립) = 약한 양성. 통째로 외운 청크가 아닌지 "
        "교차확인(아래)을 통과해야만 근거로 쓴다.\n"
        "- 암기 청크 위양성 배제: 같은 문형이 서로 다른 어휘·상황 2개에서 성립해야 '안다'고 인정한다. "
        "한 번만, 그것도 정형 표현으로 나온 것은 근거로 치지 마라.\n"
        "- 유도 실패는 그 '마커'를 근거에서 뺄 뿐이다. 이미 학습자가 자발적으로 보여준 상위 마커를 "
        "무효화하거나 밴드를 낮추지 마라.\n"
        "[판정 절차 — 반드시 이 순서대로]\n"
        f"① evidence: 학습자(USER)의 {t} 발화 원문을 최대 5개 인용해 모은다(수정·번역 금지).\n"
        "② distinct_structures: 서로 다른 문법 구조를 몇 개 productive하게 냈는지 센다(변주 게이트).\n"
        "③ band: 위 [밴드 기준]의 마커 체크리스트에 비추어 7밴드(chunk/a1/a2/a3/a4/mid/adv) 중 하나를 고른다. "
        "학습자가 '실제로 산출한 가장 높은' 마커로 정하라 — 짧게 답했어도 그 밴드 마커가 실재하면 그 밴드다. "
        "관찰된 상위 마커를 '경계에서 망설여진다'는 이유로 낮추지 마라.\n"
        f"- ⛔ **모국어 문장에 {t} 정중체 어미만 붙인 것은 {t} 산출이 아니다.** 예) 한국어 "
        "'갔다'에 '데스'를 붙인 '갔다데스', '모른다 데스'. 문장의 뼈대가 모국어면 그 언어의 "
        "문법을 부린 것이 아니므로 evidence 에서 빼고 마커로도 세지 마라. 특히 모국어 어미를 "
        f"{t} 활용형으로 오인하지 마라(한국어 '-다' ≠ 일본어 「〜た」).\n"
        "[표본 규칙]\n"
        f"- 채점 가능한(scorable) {t} 발화가 2턴 이하면 sample_quality 를 sparse(빈약), 사실상 없으면 "
        "none(전무)으로 표시하고 confidence 를 낮춘다.\n"
        "- 발화가 딱 1개뿐이라도, 그 1개가 자발적인 온전한 문장·복문·추상 전개면 밴드는 관찰된 복잡도를 "
        "존중한다(바닥으로 깎지 마라). 다만 sample_quality=sparse 로 표시해 서버가 보수 처리하게 둔다.\n"
        f"- {t} 발화가 사실상 없고 모국어뿐이면 band=unknown 으로 둔다(서버가 생존 레벨로 배정).\n"
        "[출력 필드 규칙]\n"
        "- summary: 통화의 핵심 소재를 " + label + " 로 요약. 완결 문장이 아니라 명사구 2~4어절.\n"
        "- feedback_for_learner: 학습자에게 보여줄 따뜻한 격려 1~2문장(" + label + "). "
        "레벨·점수·등급 같은 숫자는 절대 쓰지 마라.\n"
        "[레벨 기준표 — 각 밴드가 담는 커리큘럼 단계 이해용 참고(밴드 결정은 위 관찰 기준으로 한다)]\n" + rubric
    )


def _place_from_band(result: LevelAssessment) -> int | None:
    """7밴드를 최종 앱 레벨(1~13)로 배정한다 — 순수 딕셔너리 룩업 + 변주 게이트(코드가 심판).

    AI는 증인(밴드만 판정), 코드가 심판(레벨 숫자 확정 — 관통 원칙 1). 각 밴드는
    '확실히 할 수 있는' 바닥(_BUCKET_LEVEL)에 보수 배정하고, 부족분은 자동 레벨업이
    단조 상승으로 회복한다(과배치=강등 불가 plateau 방지). 이 함수는 발화 표본이 20자
    이상일 때만 호출된다(none=모국어뿐 전제).
    None 반환 = LLM 모순 출력(판정 신뢰 불가) — 호출부가 status=failed·미저장 처리.
    """
    # 모순: 표본이 충분(sufficient)한데 밴드를 못 정했다(unknown) — 판정 신뢰 불가.
    if result.band == "unknown" and result.sample_quality == "sufficient":
        return None
    # 밴드 미상 / 표본 전무 — 발화(20자+)는 있었으니 사실상 모국어만 → 생존 1.
    if result.band == "unknown" or result.sample_quality == "none":
        return 1
    level = _BUCKET_LEVEL[result.band]
    # 변주 게이트(sim_elicit.py to_level): 서로 다른 구조가 ≤1이면 아무리 밴드가 높아도
    # 암기/반복이므로 캡(게이밍 방지 — 반복충·암기긴문장충).
    #
    # ⚠ 캡 바닥이 오래 2였는데, 그러면 **1↔2 변별이 아예 안 됐다.** 인사 한 마디 +
    #   외운 자기소개만 해도 밴드 a1(=2)이 나오고 캡을 걸어도 2라 무효였다(실측
    #   call=818: 일본어 2턴·21자, 즉흥 산출 3연속 실패인데 2단계 배정).
    #   실사용자 대다수가 생존회화도 안 되는 진짜 초보라, 이 구간의 변별이 핵심이다.
    #   그래서 **두 신호가 겹치면 바닥을 1(생존회화)로** 내린다:
    #     구조 ≤1  = 문법을 부린 게 아니라 통째로 외운 것
    #     sparse   = 표본 2턴 이하 — 그 하나조차 재확인되지 않음
    #   하나만 걸리면 종전대로 2 — 과소배치는 자동 레벨업이 단조 상승으로 회복하지만,
    #   한 번에 바닥까지 떨어뜨리면 실제 A1 학습자가 생존회화를 다시 듣게 된다.
    thin_sample = result.sample_quality == "sparse"
    memorized = result.distinct_structures <= 1
    if memorized and thin_sample:
        level = min(level, 1)
    elif memorized or thin_sample:
        level = min(level, 2)
    return level


def _user_char_total(dialog: str, language: str = "ko") -> int:
    """전사에서 USER 가 **대상 언어로** 말한 글자수를 센다 — 판정 스킵 가드용.

    ⚠ **모국어는 세지 않는다.** 예전엔 isalnum() 으로 아무 문자나 셌는데, 그러면
    학습자가 모국어로 도망친 통화가 "표본 충분" 으로 통과한다. 실측(call=818, ja):

        일본어 발화  こんにちは / 私はヤンジェウデス            → 21자
        한국어 발화  "모르겠는데요" + 일본 여행 이야기 114자    → 143자
        합계 164자 ≥ 20 → 게이트 통과 → 마커 1개(〜は〜です)로 A1(2단계) 배정

    일본어 요구 3연속 실패인데 2단계가 나왔다. 대상 언어 문자만 세면 21자로,
    이 통화는 여전히 통과하지만(21 ≥ 20) 한국어로만 떠든 통화는 0자로 걸린다.

    문장부호·기호·이모지는 어느 언어에서도 안 세므로 그 방어는 그대로 유지된다.
    """
    prefix = "[USER] "
    return sum(
        count_target_script_chars(line[len(prefix):], language)
        for line in dialog.splitlines()
        if line.startswith(prefix)
    )


def _save_level_assessment(
    db: Session, call_id: int, member_id: int, level_no: int, result: LevelAssessment
) -> bool:
    """레벨 배정 + 판정 메타 + status=done 을 단일 트랜잭션(단일 commit)으로 저장.

    member.korean_level 과 call.assessed_level 이 어긋난 채 남지 않도록 반드시 한 커밋.
    member/call 어느 한쪽이라도 없으면 아무것도 저장하지 않고 False(부분 저장 창 제거) —
    호출부가 status=failed 처리한다.
    """
    # 리뷰 M1: FOR UPDATE 로 회원 단위 직렬화 — 동시 레벨테스트 2건의 grandfathering
    # progress insert 가 uq_member_item 충돌로 유실되는 창 제거(sqlite 테스트에선 no-op).
    member = mastery_repository.get_member_for_update(db, member_id)
    call = db.get(Call, call_id)
    if member is None or call is None:
        return False
    # (멀티랭귀지) 레벨 배정 대상 언어 = 이 통화의 target_language(기본 ko).
    language = call.target_language or "ko"
    # 이전 레벨(placement from_level) — 언어별. 최초 배정이면 None.
    prior_level = mastery_repository.get_language_level(db, member_id, language)
    # 레벨 기록은 member_language_level upsert(ko 는 member.korean_level 도 dual-write).
    mastery_repository.upsert_language_level(db, member_id, language, level_no)
    call.assessed_level = level_no
    call.assessment_note = result.reasoning
    call.summary = result.summary
    call.status = "done"
    # ⭐ 레벨 배정 기록(2026-08-16 — grandfathering **제거**): 건너뛴 레벨의 항목을 만들지
    #   않는다. "레벨이 처음 3이면 배운 거 0" — 안 만들면 배정 직후 승급(#247)도, 하락 후
    #   재료 0(member 20)도 같이 사라진다. 내려갈 때만 mastered→introduced 로 되돌린다.
    #   history(reason='placement')는 그대로 남는다 — 레벨 배정과 같은 커밋에 합류.
    mastery_service.record_placement(
        db, member_id, level_no, trigger_call_id=call_id, from_level=prior_level,
        language=language,
    )
    db.commit()
    return True


async def analyze_level_test_call(
    call_id: int,
    client: genai.Client,
    settings_obj: Settings,
    session_factory: sessionmaker,
    *,
    member_id: int,
    locale: str,
    target_language: str = "한국어",
    locale_label: str | None = None,
) -> None:
    """레벨테스트 통화후 판정 — 전사 1콜 판정 → 서버 클램프 → 레벨 배정(전체 graceful).

    analyze_call 과 동일 패턴(백그라운드, run_db 짧은 세션). 결과 상태:
        - USER 목표어 발화 <20자: LLM 콜 없이 **최하 레벨(1) 배정**·done. 표본 미달은
          측정 실패가 아니라 대개 측정 결과 그 자체다(목표어를 못 만든 학습자).
          미저장으로 두면 "다시하기" 직후 레벨이 없는 상태로 갇힌다.
        - LLM 실패/모순 출력(클램프 None)/member·call 소실: status=failed·미저장.
        - 성공: member.korean_level + call.assessed_level/note/summary + done 단일 commit.
    (멀티랭귀지) target_language(라벨) 로 판정 대상 언어를 지정 — 판정관 지시문·버킷 정의·
    루브릭이 그 언어로 맞춰진다. 학습자 모국어(=locale)를 실력으로 오독하지 않게 하는 핵심.
    """
    try:
        dialog = await run_db(session_factory, lambda db: _build_dialog(db, call_id))
        # target_language 는 라벨("일본어")로 오므로 코드로 되돌린다 — 미지원이면
        # is_target_script_char 가 보수적으로 전부 계수한다(기존 동작).
        _spec = resolve_language(target_language)
        _lang_code = _spec.code if _spec else "ko"
        user_chars = _user_char_total(dialog, _lang_code)
        if user_chars < _MIN_LEVELTEST_USER_CHARS:
            # 표본 미달 → **최하 레벨 배정**(LLM 콜 없이).
            #
            # ⛔ 미저장으로 되돌리지 마라. 옛 동작은 status=done + 레벨 미기록이었는데,
            #   "다시하기"(request_level_retest)가 member_language_level 행을 지운 뒤라
            #   재측정이 스킵되면 **레벨이 없는 상태로 갇힌다** — 다음 통화도 계속
            #   레벨테스트로 라우팅되고, 그 통화도 짧으면 또 스킵이라 영원히 안 빠져나온다.
            #   실측(member=20, 2026-08-02): call 866·871 이 각각 15자·14자로 연속 스킵,
            #   ja 레벨 행이 사라진 채 남았다.
            #
            #   그리고 표본 미달은 "측정 실패"가 아니라 **그 자체가 측정 결과**인 경우가
            #   대부분이다 — 위 통화 전사는 학습자가 목표어를 모른다고만 말한 것이었고,
            #   사다리 엔진도 그래서 55초 만에 조기 종료시켰다(nonspeaker_streak=4).
            #   목표어를 20자도 못 만든 사람의 레벨은 1이 맞다.
            #
            #   저장은 성공 경로와 **같은 _save_level_assessment** 를 탄다 — 레벨 upsert +
            #   call 메타 + grandfathering + history(placement)가 한 커밋으로 묶여야
            #   "레벨은 생겼는데 체크판은 비었다" 같은 반쪽 상태가 안 생긴다.
            logger.info(
                "leveltest 판정: USER %s 발화 %d자(<%d) → 최하 레벨 %d 배정·done call_id=%s member=%s",
                _lang_code, user_chars, _MIN_LEVELTEST_USER_CHARS,
                _MIN_LEVEL_NO, call_id, member_id,
            )
            sparse = LevelAssessment(
                evidence=[],
                reasoning=(
                    f"목표어 발화가 {user_chars}자로 최소 표본({_MIN_LEVELTEST_USER_CHARS}자)에 "
                    "미달해 판정관을 돌리지 않고 최하 레벨을 배정했다(표본 미달 = 산출 없음)."
                ),
                distinct_structures=0,
                band="unknown",
                confidence="low",
                sample_quality="none",
                summary="",
                feedback_for_learner="",
            )
            saved = await run_db(
                session_factory,
                lambda db: _save_level_assessment(
                    db, call_id, member_id, _MIN_LEVEL_NO, sparse
                ),
            )
            if not saved:
                logger.warning(
                    "leveltest 판정: 표본미달 저장 실패(member/call 소실) → failed call_id=%s",
                    call_id,
                )
                await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
            return

        # 모국어에 정중체만 붙인 줄(「갔다데스」)을 판정 재료에서 제거한다 —
        # 판정관은 자기가 속은 걸 모르므로 입력에서 지우는 편이 확실하다.
        dialog = _strip_non_target_user_lines(dialog, _lang_code)

        lt_usage = gemini_analysis.LlmUsage()
        result = await gemini_analysis.generate_structured(
            client,
            settings_obj.JUDGE_MODEL,
            system_instruction=_leveltest_instruction(
                locale, _load_leveltest_rubric(target_language), locale_label,
                target_language=target_language,
            ),
            prompt=f"[통화 전사]\n{dialog.strip()}",
            schema=LevelAssessment,
            temperature=0.0,
            usage=lt_usage,
        )
        await _save_analysis_usage(session_factory, call_id, lt_usage)
        if result is None:
            logger.warning("leveltest 판정: LLM 실패 → failed·미저장 call_id=%s", call_id)
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
            return

        level_no = _place_from_band(result)
        if level_no is None:
            # LLM 모순 출력(예: sufficient 인데 band=unknown) — 판정 신뢰 불가 → 미저장.
            logger.warning(
                "leveltest 판정: 모순 출력(band=%s sample=%s) → failed·미저장 call_id=%s",
                result.band, result.sample_quality, call_id,
            )
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
            return
        logger.info(
            "leveltest 판정: band=%s → 레벨 %d 배정 (confidence=%s sample=%s) call_id=%s member=%s",
            result.band, level_no, result.confidence, result.sample_quality,
            call_id, member_id,
        )
        saved = await run_db(
            session_factory,
            lambda db: _save_level_assessment(db, call_id, member_id, level_no, result),
        )
        if not saved:
            # member/call 소실(탈퇴 등) — 부분 저장 없이 실패 처리.
            logger.warning(
                "leveltest 판정: member/call 소실 → failed·미저장 call_id=%s member=%s",
                call_id, member_id,
            )
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
            return
        logger.info("leveltest 판정: 완료 → done call_id=%s level=%d", call_id, level_no)
    except Exception as exc:  # noqa: BLE001 - 백그라운드 판정은 어떤 예외도 흡수
        logger.exception("leveltest 판정: 예외 → failed call_id=%s (%s)", call_id, exc)
        try:
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
        except Exception:  # noqa: BLE001
            pass


# ── 사이드카 O/X 판정기(레벨테스트 통화중, 문항 단위) ────────────────────────
# 통화후 전사 1콜(analyze_level_test_call)과 달리, 사다리 엔진이 한 문항의 답변
# 하나를 즉시 pass/fail/unclear 로 채점받아 다음 노드를 고르게 하는 실시간 사이드카.

_LEVELTEST_JUDGE_INSTRUCTION = (
    "너는 한국어 문법 채점자다. 아래 학습자 발화가 '목표 문법'을 실제로 산출했는지 "
    "판정하라.\n"
    "이 테스트는 '이 학습자가 이 단계 수준 이상을 할 수 있나'를 재는 것이다. 딱 그 형태만 "
    "고집하지 말고 관대하게 판정하라.\n"
    "규칙:\n"
    "- ★ pass(관대하게): 학습자가 문법적으로 온전한 한국어 문장을 만들었고, 그것이 목표 문법 "
    "수준 '이상'이면 pass. 목표 형태소가 그대로 있으면 당연히 pass. 목표와 시제·구조가 정확히 "
    "일치하지 않아도, 목표보다 더 높거나 복잡한 문법(예: 현재를 물었는데 과거·복문·존댓말로 "
    "답함)으로 온전한 문장을 만들었으면 pass — 더 어려운 걸 해내면 이 단계는 당연히 통과다. "
    "(예: 목표가 현재형 문장인데 '나는 어제 쉬었어'라고 과거로 답 → pass. 목표가 과거인데 "
    "'-(으)ㄴ 적 있어요' 경험으로 답 → pass.)\n"
    "- ★ no_attempt: 학습자가 아직 진짜 답을 시도하지 않았으면 result=no_attempt(실패 아님, "
    "절대 fail 로 처리 말 것). 머뭇·필러('음','어','uhm','잠깐만'), 인사만('안녕하세요'), "
    "되묻기('네?','뭐라고요?','여보세요?'), 질문과 무관한 한두 마디, 말이 끊긴 미완성.\n"
    "- fail: 학습자가 온전한 문장을 만들지 못하고 단어·조각만 나열했거나, 만든 문장이 목표 "
    "수준에 명백히 못 미칠 때만(예: 과거 서술을 물었는데 현재 단문 하나도 못 만듦). 즉 "
    "'온전한 문장을 시도했으나 이 단계 수준에 못 미침'일 때만 fail — 유효한 문장을 fail 하지 마라.\n"
    "- unclear: 문장은 시도했는데 판단이 곤란하거나 반쪽인 경우.\n"
    "- 발음·조사의 사소한 오류는 감점하지 않는다.\n"
    "- heard_grammar 에는 판정 근거가 된 학습자의 실제 구절을 원문 그대로 인용하라"
    "(추측·재작성 금지, 근거가 없으면 빈 문자열).\n"
    "출력은 반드시 주어진 JSON 스키마를 따른다."
)


async def judge_leveltest_answer(
    client: genai.Client,
    *,
    target_desc: str,
    answer_text: str,
) -> str:
    """레벨테스트 한 답변을 목표 문법 기준으로 pass/fail/unclear 판정(사이드카 1콜).

    통화중 사다리 엔진이 문항마다 호출한다. 어떤 실패든(client 부재·빈 입력·LLM
    실패·환각) "unclear" 로 흡수해 엔진이 교차확인/강제전진으로 처리하게 한다(R5).

    Args:
        client: lifespan 이 만든 genai.Client(없으면 통화 자체가 비활성이나 방어).
        target_desc: 이 문항이 재는 목표 문법 기준(사다리 노드 제공, 한국어).
        answer_text: 학습자가 방금 한 발화(한국어, in_tr 전사).

    Returns:
        "pass" | "fail" | "unclear" (문자열). 판정 불능·환각은 항상 "unclear".
    """
    # graceful 가드(R5): client 없음 / 빈 발화 / 빈 목표 → 판정 불능.
    if client is None or not answer_text or not answer_text.strip():
        return "unclear"
    if not target_desc or not target_desc.strip():
        return "unclear"

    try:
        verdict = await gemini_analysis.generate_structured(
            client,
            settings.JUDGE_MODEL,
            system_instruction=_LEVELTEST_JUDGE_INSTRUCTION,
            prompt=(
                f"[목표 문법]\n{target_desc.strip()}\n\n"
                f"[학습자 발화]\n{answer_text.strip()}"
            ),
            schema=LeveltestVerdict,
            temperature=0.0,
            thinking_budget=0,  # 통화중 실시간 사이드카 — 지연 최소화(추론 비활성).
        )
    except Exception as exc:  # noqa: BLE001 - 사이드카는 어떤 예외도 흡수(R5)
        logger.warning("leveltest judge: 판정 예외(무시) → unclear: %s", exc)
        return "unclear"

    if verdict is None:
        return "unclear"

    # ★ 인용 검증(관통원칙3): pass 인데 근거 구절이 실제 발화에 없으면 환각 →
    # unclear 로 강등. 순수 파이썬 부분 문자열 매칭(LLM 없이 검증 가능). 여기 O/X 판정은
    # 단일 노드 통과/실패라 오탐 대가가 작아 엄격 부분문자열로 검증한다.
    if verdict.result == "pass":
        heard = (verdict.heard_grammar or "").strip()
        if not heard or heard not in answer_text:
            logger.info(
                "leveltest judge: pass 인용 미검증(heard=%r) → unclear 강등", heard
            )
            return "unclear"
        return "pass"

    if verdict.result in ("fail", "no_attempt"):
        return verdict.result
    return "unclear"


# ── 종료 판정 사이드카 (레벨테스트 Phase 2 — '끝낼까 말까' 전용) ─────────────
# 최종 레벨은 통화후 판정관(analyze_level_test_call, 전사 전체)이 정한다. 통화중 밴드 판정은
# 그와 독립·중복이라 제거했다. 이 사이드카는 오직 종료 트리거만 본다: 답변이 실제 대상 언어인가
# (비화자 결정론 컷) + 등반 실패(정체·막힘)로 지금 끝낼까(should_end, 전체 맥락 근거).

def _leveltest_turn_instruction(target_language: str = "한국어") -> str:
    """레벨테스트 '종료 판정 전용' 지시문 — 매 턴 (1)답변이 대상 언어인가 + (2)지금 끝낼까만
    판정한다(밴드 정밀분류는 통화후 판정관 몫 — 여기선 '끝낼까 말까'만).

    (멀티랭귀지) 대상 언어를 명시해 학습자 모국어를 실력으로 오독하지 않게 한다(기본 한국어)."""
    t = target_language
    return (
        f"너는 {t} 레벨테스트 통화를 지켜보며 두 가지만 판단한다:\n"
        f"(1) answer_in_target: [학습자 최신 발화]가 실제 {t}인가? 학습자 모국어나 다른 언어면 "
        f"false({t} 어휘·문법 표지가 실제로 있어야 true — 어순만 닮은 건 불충분). 인사·머뭇·"
        "\"몰라요\"만이면 false.\n"
        "(2) should_end: 지금 통화를 끝내야 하나?\n"
        "\n"
        "이건 '올라가는' 시험 — 비버가 매 턴 더 어렵게 묻는다. 학습자 천장이 드러나면 끝낸다. "
        "[전체 대화]의 최근 2~3턴 흐름으로 판단하라:\n"
        "■ should_end=true — 최근 2~3턴에 하나라도 뚜렷하면:\n"
        " ① 정체(천장): 비버가 더 어렵게 밀었는데 답이 더 복잡해지지 않는다(같은 수준 반복 / "
        "더 쉬운 답 / 더 어려운 질문엔 못 답). → 더 재도 그대로.\n"
        f" ② 막힘: 최근 여러 턴 {t}를 거의 못 낸다(영어로 도망·\"몰라요\"·머뭇 반복). → 더 얻을 것 없음.\n"
        "■ should_end=false:\n"
        " ③ 직전 턴에 '더 어려운' 걸 새로 해냈다 → 아직 오르는 중, 계속.\n"
        " ④ 대화가 아직 짧아 흐름이 안 보인다.\n"
        "★ 정체/막힘이 2~3턴 뚜렷하면 미루지 마라. 계속 새 수준을 보이면 성급히 끝내지 마라.\n"
        "출력은 주어진 JSON 스키마: answer_in_target(bool), should_end(bool), end_reason(근거 한 줄)."
    )


class LeveltestTurnRead(BaseModel):
    """레벨테스트 종료 판정 전용 사이드카 출력(밴드 정밀분류 없음 — '끝낼까 말까'만).

    answer_in_target: 최신 발화가 실제 대상 언어인가(모국어·타 언어·머뭇·인사면 False).
    should_end: 등반 실패(정체·막힘)로 지금 통화를 끝내야 하는가. 호출부가 게이트를 통과할 때만
    최종 반영한다. 최종 레벨은 통화후 판정관(전사 전체)이 정한다 — 이 사이드카는 종료 트리거 전용.
    """

    answer_in_target: bool = False  # 최신 발화가 실제 대상 언어인가(모국어·타 언어·머뭇·인사면 False)
    should_end: bool = False  # 등반 실패(정체·막힘)로 지금 마쳐야 하나
    end_reason: str = ""  # 종료 판단 근거(로깅용)


async def judge_leveltest_turn(
    client: genai.Client,
    *,
    transcript: list[str],
    latest_answer: str,
    prior_question: str | None = None,
    target_language: str = "한국어",
    usage: gemini_analysis.LlmUsage | None = None,
) -> tuple[bool, bool]:
    """종료 판정 전용 사이드카(1콜): (answer_in_target, should_end).

    밴드 정밀분류를 뺀다 — 최종 레벨은 통화후 판정관(analyze_level_test_call, 전사 전체)이
    정하므로 통화중 밴드 판정은 중복이었다. 이 사이드카는 오직 '끝낼까 말까'만 본다:
    (1) 최신 발화가 실제 대상 언어인지(비화자 결정론 컷 재료), (2) 등반 실패(정체·막힘)로
    지금 끝내야 하는지(transcript 전체 맥락 근거).

    Args:
        client: lifespan 이 만든 genai.Client(없으면 통화 자체가 비활성이나 방어).
        transcript: 지금까지 누적된 Q/A 전사(이번 최신 발화 이전까지 — 맥락용).
        latest_answer: 학습자가 방금 한 발화(대상 언어, in_tr 전사).
        prior_question: 직전 비버 질문(문맥용, 선택).
        usage: 토큰 수집기(원가 계기판). 안 넘기면 종전과 동일하다.

    Returns:
        (answer_in_target, should_end).
        answer_in_target: 최신 발화가 실제 대상 언어인가(모국어·타 언어·머뭇·인사·실패면 False).
        should_end: 전체 대화상 지금 마쳐도 되는가(호출부가 플로어 게이트로 최종 반영).
    """
    # graceful 가드(R5): client 없음 / 빈 발화 → 판정 불능(비화자로 취급, 종료 안 함).
    if client is None or not latest_answer or not latest_answer.strip():
        return False, False

    convo = "\n".join(transcript[-30:])  # 최근 30턴만(컨텍스트 과대·비용 방지)
    ctx = f"[전체 대화]\n{convo}\n\n" if convo else ""
    q = ""
    if prior_question and prior_question.strip():
        q = f"[직전 비버 질문]\n{prior_question.strip()}\n\n"

    try:
        read = await gemini_analysis.generate_structured(
            client,
            settings.JUDGE_MODEL,
            system_instruction=_leveltest_turn_instruction(target_language),
            prompt=(
                f"{ctx}"
                f"{q}[학습자 최신 발화]\n{latest_answer.strip()}"
            ),
            schema=LeveltestTurnRead,
            temperature=0.0,
            usage=usage,
            thinking_budget=0,  # 통화중 실시간 사이드카 — 지연 최소화(추론 비활성).
        )
    except Exception as exc:  # noqa: BLE001 - 사이드카는 어떤 예외도 흡수(R5)
        logger.warning("leveltest turn judge: 예외(무시) → (False, False): %s", exc)
        return False, False

    if read is None:
        return False, False

    answer_in_target = bool(read.answer_in_target)
    should_end = bool(read.should_end)
    if should_end:
        logger.info("leveltest turn judge: should_end=True (%s)", (read.end_reason or "")[:80])
    return answer_in_target, should_end
