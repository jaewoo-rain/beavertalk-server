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
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional, TypeVar

from fastapi.concurrency import run_in_threadpool
from google import genai
from pydantic import BaseModel, Field
from sqlalchemy import select
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
from core.persona_prompt import _LOCALE_LABEL
from domains.account.models.member import Member
from domains.account.models.member_reason import REASON_LABELS
from domains.commerce.models.character import Character
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
from domains.learning.models.sentence import Sentence
from domains.learning.repository import mastery_repository
from domains.learning.service import mastery_service

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


def _load_member_character(
    db: Session, member_id: int, character_id: int, language: str = "ko"
) -> dict:
    """회원+캐릭터 공용 조회(일반/레벨테스트 셋업의 공통 분모) — 평범한 값만 반환.

    (멀티랭귀지) korean_level 은 member_language_level[language](ko 는 member.korean_level
    dual-read 폴백) — 언어별 현재 레벨/콜드스타트를 반영한다.

    Returns:
        {role, personality, rules, voice, locale, interests, name,
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
    rules = ch.rules if ch else None
    voice = (ch.voice.name if (ch and ch.voice and ch.voice.name) else DEFAULT_VOICE)

    return {
        "role": role,
        "personality": personality,
        "rules": rules,
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
        {role, personality, rules, voice, level_profile, locale, interests, name,
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

    history = _load_history(db, member_id) if member_found else None

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
        {role, personality, rules, voice, locale, interests, name} —
        build_leveltest_instruction 의 입력과 1:1.
    """
    base = _load_member_character(db, member_id, character_id)
    base.pop("korean_level")
    base.pop("member_found")
    return base


def _load_history(db: Session, member_id: int) -> dict | None:
    """최근 학습 이력(프롬프트 주입용): 최근 통화 요약 + 최근 배운 한국어 표현.

    {"summaries": [...최대 5], "expressions": [...최대 30, 중복 제거]} 또는 None(이력 없음).
    persona_prompt._history_block 이 이 형태를 기대한다.
    """
    summaries = [
        s.strip()
        for s in db.scalars(
            select(Call.summary)
            .where(Call.member_id == member_id, Call.summary.is_not(None))
            .order_by(Call.call_date.desc())
            .limit(5)
        ).all()
        if s and s.strip()
    ]
    expr_rows = db.scalars(
        select(Sentence.korean_sentence)
        .join(Call, Sentence.call_id == Call.call_id)
        .where(Call.member_id == member_id, Sentence.korean_sentence.is_not(None))
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
        "kind": entry["study_kind"],  # review/grammar/vocab/chunk (persona 유형 라벨)
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

    # ② 공부 로드 10 (본편 5 + 예비 5)
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
            if alnum < 4 or norm_quote == norm_surface:
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
        # 데모(비한국어 target)는 한국어 커리큘럼 검출이 무의미하므로 검출을 건너뛴다.
        if member_id is None:
            def _resolve_member(db: Session) -> int | None:
                call = db.get(Call, call_id)
                return call.member_id if call is not None else None

            member_id = await run_db(session_factory, _resolve_member)

        cands: list[dict] = []
        if member_id is not None and target_language == "한국어":
            if candidates is not None:
                cands = candidates
            else:
                cands = await run_db(
                    session_factory,
                    lambda db: mastery_repository.load_default_candidates(db, member_id),
                )

        instruction = _analysis_instruction(locale, target_language, locale_label)
        prompt = f"[통화 전사]\n{dialog.strip()}"
        if cands:
            instruction = instruction + "\n" + _DETECTION_INSTRUCTION
            prompt = prompt + "\n\n" + _candidate_table(cands)

        result = await gemini_analysis.generate_structured(
            client,
            settings_obj.JUDGE_MODEL,
            system_instruction=instruction,
            prompt=prompt,
            # 후보 0개면 detections 없는 스키마 — 기존 분석 출력 무변화(하위호환)
            schema=CallAnalysis if cands else _CallAnalysisBase,
        )
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
        for sentence_id, korean in pending:
            try:
                # 언어(_lang_code: 라벨/코드→ISO) + 캐릭터 음색(call_voice) 두 축.
                synthesized = await tts.synthesize(
                    korean, _lang_code(target_language), voice=call_voice
                )  # None 가능(비활성/실패)
                if not synthesized:
                    continue
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
                logger.warning(
                    "normalcall TTS: 실패(무시 — 온디맨드 폴백) sentence=%s call_id=%s (%s)",
                    sentence_id, call_id, exc,
                )

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
                    mastery["evidence"], mastery["levelup"].get("result"), call_id,
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
    """레벨테스트 판정 1콜의 전체 출력(4버킷 — 행동 관찰).

    ⚠ 필드 순서 = 생성 순서(구조화 출력 내 CoT 강제): 인용 → 추론 → 밴드 →
    신뢰도 → 표본질 → 요약 → 학습자 피드백.
    LLM 은 4버킷 밴드(survival/beginner/intermediate/advanced/unknown)만 판정하고,
    최종 앱 레벨(1~13) 숫자는 서버가 소유한다(AI 는 증인, 코드가 심판 — 관통 원칙 1).
    band → level_no 배정은 _place_from_band(_BUCKET_LEVEL 룩업)이 확정한다.
    """

    evidence: list[str] = Field(
        description="학습자(USER)의 한국어 발화 원문 인용, 최대 5개(수정·번역 금지)"
    )
    reasoning: str = Field(description="인용을 근거로 한 판정 추론(한국어)")
    band: Literal["survival", "beginner", "intermediate", "advanced", "unknown"] = Field(
        description="4버킷 밴드(행동 관찰) — survival=입문(인사·청크만) / beginner=초급(단어+구문) / "
        "intermediate=중급(온전한 문장) / advanced=고급(어법 정확+긴 담화) / unknown=표본 부족"
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


# ── 레벨테스트 4버킷(행동 관찰) — 라이브 분류기·통화후 판정관 공유 정의 ──────────
# 이름표(초급/중급)가 아니라 '관찰 가능한 발화 행동'으로 밴드를 읽는다. survival/beginner/
# intermediate/advanced 서수(0..3)는 그대로 재사용하되 의미를 아래 행동정의로 고정한다.
# 이 정의를 _leveltest_instruction(통화후)·_band_classify_instruction(통화중)이 공유해
# 두 판정기가 같은 잣대로 밴드를 읽게 한다(정의 불일치가 저평가·정합 버그의 원인이었음).
# (멀티랭귀지) 버킷 정의는 언어별 — 문법 마커·예시가 대상 언어로 갈린다. 판정 근거는 대상
# 언어 발화만이므로, 대상 언어에 맞는 정의를 써야 학습자 모국어를 실력으로 오독하지 않는다.
_BUCKET_DEFINITIONS_KO = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 관찰 행동으로 읽어라]\n"
    "- advanced(고급): 어법이 대체로 정확하고 여러 문장을 길게 이어 말한다. 복문(연결어미로 절을 엮음)·"
    "다양한 화법·추상/전문 주제 전개가 보이면 고급이다. 유창할수록 확실한 고급.\n"
    "- intermediate(중급): 조사(을/를·에·에서)와 제대로 활용된 종결어미(-아요/어요·-았/었어요·-(으)ㄹ 거예요)를 "
    "갖춘 '온전한 문장'을 만든다(예: '김치를 좋아해요', '저는 자전거를 타요'). 다만 아직 긴 복문·다양한 화법까지는 아니다.\n"
    "- beginner(초급): 단어와 짧은 구를 이어 붙이지만 문법적 문장을 못 만든다 — 조사·활용 없는 전보식·사전형 "
    "나열(예: '김치 좋다', '자전거 좋다', '나는 김치다', 단어 나열). 여러 단어를 뱉어도 조사+활용된 종결어미로 "
    "온전한 문장을 완성하지 못하면 초급이다.\n"
    "- survival(입문): 인사·정형청크(안녕하세요·감사합니다·이거 주세요)·숫자·단어 하나 수준만. 내용어를 거의 못 낸다.\n"
    "★ 핵심 변별 — (초급↔중급) 조사·활용된 종결어미를 갖춘 문법적 문장을 만드는가. 단어 슬롯 채우기('N 좋다')는 "
    "여러 개여도 초급이다. (중급↔고급) 여러 문장을 복문으로 길게 이어 유창하게 말하는가.\n"
    "★ 발음·조사 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자·맞춤법이 아니라 '문장을 만드는 능력과 복잡도'로 판정.\n"
    "★ 짧게 답해도 복문·추상 전개가 있으면 상급으로 읽어라. 유창한 긴 담화를 '표지가 정확히 일치하지 않는다'는 "
    "이유로 깎지 마라. 학습자가 자발적으로 산출한 '가장 높은' 자질로 밴드를 정한다."
)

# 일본어 버킷 정의 — 한국어와 같은 행동 잣대, 마커·예시만 일본어(は/が/を 조사, です·ます·ました 종결).
_BUCKET_DEFINITIONS_JA = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 관찰 행동으로 읽어라]\n"
    "- advanced(고급): 어법이 대체로 정확하고 여러 문장을 길게 이어 말한다. 복문(から·ので·たら·ても 등으로 절을 엮음)·"
    "다양한 화법·추상/전문 주제 전개가 보이면 고급이다. 유창할수록 확실한 고급.\n"
    "- intermediate(중급): 조사(は・が・を・に・で)와 제대로 활용된 종결(です・ます・でした・ました・〜たいです)을 "
    "갖춘 '온전한 문장'을 만든다(예: 'キムチが好きです', '私は自転車に乗ります'). 다만 아직 긴 복문·다양한 화법까지는 아니다.\n"
    "- beginner(초급): 단어와 짧은 구를 이어 붙이지만 문법적 문장을 못 만든다 — 조사·활용 없는 전보식·사전형 "
    "나열(예: 'キムチ 好き', '自転車 好き', 단어 나열). 여러 단어를 뱉어도 조사+활용된 종결로 온전한 문장을 "
    "완성하지 못하면 초급이다.\n"
    "- survival(입문): 인사·정형청크(こんにちは·ありがとうございます·これをください)·숫자·단어 하나 수준만. 내용어를 거의 못 낸다.\n"
    "★ 핵심 변별 — (초급↔중급) 조사·활용된 종결(です/ます 등)을 갖춘 문법적 문장을 만드는가. 단어 슬롯 채우기('N 好き')는 "
    "여러 개여도 초급이다. (중급↔고급) 여러 문장을 복문으로 길게 이어 유창하게 말하는가.\n"
    "★ 발음·조사 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자·맞춤법이 아니라 '문장을 만드는 능력과 복잡도'로 판정.\n"
    "★ 짧게 답해도 복문·추상 전개가 있으면 상급으로 읽어라. 유창한 긴 담화를 '표지가 정확히 일치하지 않는다'는 "
    "이유로 깎지 마라. 학습자가 자발적으로 산출한 '가장 높은' 자질로 밴드를 정한다."
)

# 영어 버킷 정의 — 한국어와 같은 행동 잣대, 마커·예시만 영어(관사·시제·문장 완성도).
_BUCKET_DEFINITIONS_EN = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 관찰 행동으로 읽어라]\n"
    "- advanced(고급): 어법이 대체로 정확하고 여러 문장을 길게 이어 말한다. 복문(because·if·that·관계절로 절을 엮음)·"
    "다양한 시제·화법·추상/전문 주제 전개가 보이면 고급이다. 유창할수록 확실한 고급.\n"
    "- intermediate(중급): 주어+동사+목적어의 '온전한 문장'을 관사·올바른 시제(현재/과거/미래)와 함께 만든다"
    "(예: 'I went to school yesterday', 'She likes coffee'). 다만 아직 긴 복문·다양한 화법까지는 아니다.\n"
    "- beginner(초급): 단어와 짧은 구를 이어 붙이지만 문법적 문장을 못 만든다 — 시제·관사 없는 전보식"
    "(예: 'I go school', 'me happy', 'yesterday I go'). 여러 단어를 뱉어도 관사·시제를 갖춘 온전한 문장을 "
    "완성하지 못하면 초급이다.\n"
    "- survival(입문): 인사·정형청크(Hello·Thank you·Excuse me)·숫자·단어 하나 수준만. 내용어를 거의 못 낸다.\n"
    "★ 핵심 변별 — (초급↔중급) 관사·시제를 갖춘 문법 문장을 만드는가. 단어 나열은 여러 개여도 초급이다. "
    "(중급↔고급) 여러 문장을 복문으로 길게 이어 유창하게 말하는가.\n"
    "★ 발음·문법 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자·맞춤법이 아니라 '문장을 만드는 능력과 복잡도'로 판정.\n"
    "★ 짧게 답해도 복문·추상 전개가 있으면 상급으로 읽어라. 학습자가 자발적으로 산출한 '가장 높은' 자질로 밴드를 정한다."
)

# 중국어 버킷 정의 — 한국어와 같은 행동 잣대, 마커·예시만 중국어(양사·了·보어·문장 완성도).
_BUCKET_DEFINITIONS_CN = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 관찰 행동으로 읽어라]\n"
    "- advanced(고급): 어법이 대체로 정확하고 여러 문장을 길게 이어 말한다. 복문(因为/如果/虽然...으로 절을 엮음)·"
    "다양한 보어·把/被 구문·추상/전문 주제 전개가 보이면 고급이다. 유창할수록 확실한 고급.\n"
    "- intermediate(중급): 주어+술어+목적어의 '온전한 문장'을 양사(量词)·완료 '了'·기본 보어와 함께 만든다"
    "(예: '我昨天去了学校', '我喜欢喝咖啡'). 다만 아직 긴 복문·복잡한 보어까지는 아니다.\n"
    "- beginner(초급): 단어와 짧은 구를 이어 붙이지만 문법적 문장을 못 만든다 — 양사·'了' 없는 전보식"
    "(예: '我去学校', '我高兴'). 여러 단어를 뱉어도 양사·완료·보어를 갖춘 온전한 문장을 완성하지 못하면 초급이다.\n"
    "- survival(입문): 인사·정형청크(你好·谢谢·多少钱)·숫자·단어 하나 수준만. 내용어를 거의 못 낸다.\n"
    "★ 핵심 변별 — (초급↔중급) 양사·'了'·기본 보어를 갖춘 문법 문장을 만드는가. 단어 나열은 여러 개여도 초급이다. "
    "(중급↔고급) 여러 문장을 복문으로 길게 이어 유창하게 말하는가.\n"
    "★ 발음(성조)·양사 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자가 아니라 '문장을 만드는 능력과 복잡도'로 판정.\n"
    "★ 짧게 답해도 복문·추상 전개가 있으면 상급으로 읽어라. 학습자가 자발적으로 산출한 '가장 높은' 자질로 밴드를 정한다."
)

# 프랑스어 버킷 정의 — 한국어와 같은 행동 잣대, 마커·예시만 프랑스어(성수일치·동사활용·시제).
_BUCKET_DEFINITIONS_FR = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 관찰 행동으로 읽어라]\n"
    "- advanced(고급): 어법이 대체로 정확하고 여러 문장을 길게 이어 말한다. 복문(parce que/si/que/관계절)·"
    "다양한 시제(passé composé/imparfait/subjonctif)·추상/전문 주제 전개가 보이면 고급이다. 유창할수록 확실한 고급.\n"
    "- intermediate(중급): 주어+동사+보어의 '온전한 문장'을 성수일치·활용된 동사와 함께 만든다"
    "(예: 'J'ai mangé une pomme', 'Elle aime le café'). 다만 아직 긴 복문·다양한 시제까지는 아니다.\n"
    "- beginner(초급): 단어와 짧은 구를 이어 붙이지만 문법적 문장을 못 만든다 — 활용·성수일치 없는 전보식"
    "(예: 'Je aller école', 'moi content'). 여러 단어를 뱉어도 활용된 동사·성수일치를 갖춘 온전한 문장을 "
    "완성하지 못하면 초급이다.\n"
    "- survival(입문): 인사·정형청크(Bonjour·Merci·Combien)·숫자·단어 하나 수준만. 내용어를 거의 못 낸다.\n"
    "★ 핵심 변별 — (초급↔중급) 성수일치·활용된 동사를 갖춘 문법 문장을 만드는가. 단어 나열은 여러 개여도 초급이다. "
    "(중급↔고급) 여러 문장을 복문으로 길게 이어 유창하게 말하는가.\n"
    "★ 발음·성수일치 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자가 아니라 '문장을 만드는 능력과 복잡도'로 판정.\n"
    "★ 짧게 답해도 복문·추상 전개가 있으면 상급으로 읽어라. 학습자가 자발적으로 산출한 '가장 높은' 자질로 밴드를 정한다."
)

# 베트남어 버킷 정의 — 한국어와 같은 행동 잣대, 마커·예시만 베트남어(시제표지·분류사·어순).
_BUCKET_DEFINITIONS_VI = (
    "[밴드 기준 — 학습자가 '실제로 보여준 최고 수준'으로 판정한다. 이름표가 아니라 아래 관찰 행동으로 읽어라]\n"
    "- advanced(고급): 어법이 대체로 정확하고 여러 문장을 길게 이어 말한다. 복문(vì...nên/nếu...thì/mà)·"
    "다양한 시제표지·복잡 구조·추상/전문 주제 전개가 보이면 고급이다. 유창할수록 확실한 고급.\n"
    "- intermediate(중급): 주어+동사+목적어의 '온전한 문장'을 시제표지(đã/đang/sẽ)·분류사와 함께 만든다"
    "(예: 'Hôm qua tôi đã ăn cơm', 'Tôi thích cà phê'). 다만 아직 긴 복문·복잡 구조까지는 아니다.\n"
    "- beginner(초급): 단어와 짧은 구를 이어 붙이지만 문법적 문장을 못 만든다 — 시제표지·분류사 없는 전보식"
    "(예: 'Tôi đi trường', 'tôi vui'). 여러 단어를 뱉어도 시제표지·분류사를 갖춘 온전한 문장을 "
    "완성하지 못하면 초급이다.\n"
    "- survival(입문): 인사·정형청크(Xin chào·Cảm ơn·Bao nhiêu tiền)·숫자·단어 하나 수준만. 내용어를 거의 못 낸다.\n"
    "★ 핵심 변별 — (초급↔중급) 시제표지·분류사를 갖춘 문법 문장을 만드는가. 단어 나열은 여러 개여도 초급이다. "
    "(중급↔고급) 여러 문장을 복문으로 길게 이어 유창하게 말하는가.\n"
    "★ 발음(성조)·표지 실수·ASR(음성인식) 왜곡은 감점하지 마라. 철자가 아니라 '문장을 만드는 능력과 복잡도'로 판정.\n"
    "★ 짧게 답해도 복문·추상 전개가 있으면 상급으로 읽어라. 학습자가 자발적으로 산출한 '가장 높은' 자질로 밴드를 정한다."
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
    """대상 언어의 4버킷 행동 정의. 미등록 언어는 한국어 정의로 폴백."""
    return _BUCKET_DEFINITIONS_BY_LANG.get(_lang_code(target_language), _BUCKET_DEFINITIONS_KO)

# 버킷 → 최종 앱 레벨(1~13). 각 버킷이 '확실히 할 수 있는' 밴드 바닥에 보수 배정 —
# 과배치(강등 불가 → plateau) 방지, 부족분은 체크판 자동 레벨업이 단조 상승으로 회복한다.
# advanced=6 은 실제 L6~13 을 다 담는 넓은 버킷(향후 프로브로 세분화 여지 — TODO).
_BUCKET_LEVEL: dict[str, int] = {
    "survival": 1,      # 입문 — 인사·청크만
    "beginner": 2,      # 초급 — 단어+구문
    "intermediate": 3,  # 중급 — 온전한 문장
    "advanced": 6,      # 고급 — 어법 정확 + 긴 담화
}

# 판정 스킵 하한: USER 한국어+모국어 발화 총합(공백 제외)이 이 미만이면 LLM 콜 없이 skip.
_MIN_LEVELTEST_USER_CHARS = 20

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
        "[증거 가중 — 비대칭 채점]\n"
        "- 자발 성공(비버가 문형을 깔아 주지 않았는데 학습자가 스스로 그 자질을 만들어 냄) = 강한 양성. "
        "그 수준을 밴드로 인정할 근거가 된다.\n"
        "- 유도 성공(비버가 예시·선택지로 떠먹여 준 뒤에야 성립) = 약한 양성. 통째로 외운 청크가 아닌지 "
        "교차확인(아래)을 통과해야만 근거로 쓴다.\n"
        "- 암기 청크 위양성 배제: 같은 문형이 서로 다른 어휘·상황 2개에서 성립해야 '안다'고 인정한다. "
        "한 번만, 그것도 정형 표현으로 나온 것은 근거로 치지 마라.\n"
        "- 유도 실패는 그 '자질'을 근거에서 뺄 뿐이다. 이미 학습자가 자발적으로 보여준 상위 자질을 "
        "무효화하거나 밴드를 낮추지 마라.\n"
        "[판정 절차 — 반드시 이 순서대로]\n"
        f"① evidence: 학습자(USER)의 {t} 발화 원문을 최대 5개 인용해 모은다(수정·번역 금지).\n"
        "② band: 위 [밴드 기준]에 비추어 4버킷(survival/beginner/intermediate/advanced) 중 하나를 고른다. "
        "학습자가 '실제로 산출한 가장 높은' 수준으로 정하라 — 짧게 답했어도 조사·활용을 갖춘 온전한 문장이면 최소 "
        "중급, 복문·긴 담화면 고급이다. 관찰된 상위 자질을 '경계에서 망설여진다'는 이유로 낮추지 마라.\n"
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
    """4버킷 밴드를 최종 앱 레벨(1~13)로 배정한다 — 순수 딕셔너리 룩업(코드가 심판).

    AI는 증인(밴드만 판정), 코드가 심판(레벨 숫자 확정 — 관통 원칙 1). 각 버킷은
    '확실히 할 수 있는' 밴드 바닥(_BUCKET_LEVEL)에 보수 배정하고, 부족분은 자동 레벨업이
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
    # 표본 빈약(sparse)이면 중급 이상 배치 금지 — 1~2건 표본으로 과배치하지 않는다(보수).
    if result.sample_quality == "sparse":
        level = min(level, 2)
    return level


def _user_char_total(dialog: str) -> int:
    """전사에서 USER 발화의 유의미 글자수를 센다 — 판정 스킵 가드용.

    유니코드 letter/digit(한글·영숫자 등)만 계수 — 문장부호·기호·이모지만으로
    20자를 채워 무의미한 LLM 판정이 도는 것을 막는다.
    """
    prefix = "[USER] "
    return sum(
        sum(1 for ch in line[len(prefix):] if ch.isalnum())
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
    # grandfathering(P2, mechanics ⑩): ≤k−2 → MASTERED(placement) / k−1 → INTRODUCED /
    # ≥k → UNSEEN(행 없음) + history(reason='placement') — 레벨 배정과 같은 커밋에 합류.
    mastery_service.apply_grandfathering(
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
        - USER 발화(letter/digit) <20자: LLM 콜 없이 status=done·미저장(다음 통화 자동 재테스트).
        - LLM 실패/모순 출력(클램프 None)/member·call 소실: status=failed·미저장.
        - 성공: member.korean_level + call.assessed_level/note/summary + done 단일 commit.
    (멀티랭귀지) target_language(라벨) 로 판정 대상 언어를 지정 — 판정관 지시문·버킷 정의·
    루브릭이 그 언어로 맞춰진다. 학습자 모국어(=locale)를 실력으로 오독하지 않게 하는 핵심.
    """
    try:
        dialog = await run_db(session_factory, lambda db: _build_dialog(db, call_id))
        user_chars = _user_char_total(dialog)
        if user_chars < _MIN_LEVELTEST_USER_CHARS:
            # 표본 미달 — 판정 스킵·미저장(korean_level None 유지 → 다음 통화 재테스트).
            logger.info(
                "leveltest 판정: USER 발화 %d자(<%d) → 스킵·done call_id=%s member=%s",
                user_chars, _MIN_LEVELTEST_USER_CHARS, call_id, member_id,
            )
            await run_db(session_factory, lambda db: set_status(db, call_id, "done"))
            return

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
        )
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
    # unclear 로 강등. 순수 파이썬 부분 문자열 매칭(LLM 없이 검증 가능).
    # (의도적으로 엄격: 여기 O/X 판정은 단일 노드 통과/실패라 오탐 대가가 작다. 반면
    #  classify_leveltest_band 는 밴드 천장을 좌우해 저평가 대가가 크므로 _citation_coverage
    #  로 완화한다 — 두 강도 차이는 의도된 것.)
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


# ── 밴드 분류 사이드카 (레벨테스트 Phase 2) ─────────────────────────────────
# judge_leveltest_answer 가 "이 노드 문법을 산출했나(O/X)"를 재는 것과 달리,
# 이 사이드카는 학습자 답 1개가 '보여준 최고 문법 밴드'를 노드 무관하게 절대 분류한다.
# (노드 매칭 방식은 call 246 강등 오판의 원인 — 목표보다 높은 문법으로 답해도
#  노드 미스매치면 실패로 샜다. 밴드 분류는 학습자가 실제로 도달한 천장을 읽는다.)

def _band_classify_instruction(target_language: str = "한국어") -> str:
    """라이브 밴드 분류기 지시문. (멀티랭귀지) 대상 언어의 버킷 정의로 판정 —
    기본 한국어(프로덕션 무손상). 대상 언어를 명시해야 학습자 모국어를 실력으로 오독하지 않는다."""
    t = target_language
    return (
        f"너는 {t} 학습자의 답변 한 개를 보고 그 답변이 보여준 '최고' 밴드를 판정하는 판독기다. "
        "비버(선생님)가 무엇을 물었는지와 무관하게, 학습자 발화 자체에 실재하는 언어로만 판정하라. "
        "[직전 비버 질문]은 문맥 파악용일 뿐 — 비버의 문장을 학습자 실력으로 세지 마라.\n"
        f"[★ 언어 게이트 — 밴드 재기 전에 먼저 판정하라] 이 답변이 실제로 {t}인가? 학습자가 {t}가 "
        f"아니라 자기 모국어나 다른 언어로 답했으면 answer_in_target=false 로 하고, 아무리 유창·복잡해도 "
        f"observed_band=no_attempt 다 — 오직 {t} 발화만이 밴드 근거다. ★ {t}와 학습자 모국어가 어순·"
        f"조사 구조가 비슷해도(예: 한국어↔일본어는 둘 다 조사+정중종결+어순이 닮음), {t}의 어휘·문법 "
        f"표지가 실제로 있어야만 {t}로 인정한다. 다른 언어 어휘로 이뤄진 문장은 문장이 길고 복잡해도 "
        f"answer_in_target=false 이고 no_attempt 다(모국어 유창함을 {t} 실력으로 오독 금지).\n"
        + _bucket_definitions(t) + "\n"
        "- no_attempt: 머뭇·필러('음','어'), 인사만, 되묻기('네?','뭐라고요?'), 질문과 무관한 "
        "한두 마디, 말이 끊긴 미완성.\n"
        "[인용 규칙]\n"
        "- ★ heard_grammar 에는 밴드 근거가 된 학습자의 실제 구절을 전사에 나타난 '그대로'"
        "(오탈자·띄어쓰기 포함) 복사하라. 교정·정규화·번역·재작성 금지. 근거가 없으면 빈 문자열.\n"
        "[출력 순서 — 반드시 이 순서로 생각하라]\n"
        f"① answer_in_target: 이 답변이 {t}인가(아니면 이하 전부 no_attempt/빈값) → ② heard_grammar: "
        "밴드 근거가 된 실제 구절 인용 → ③ decisive_feature: 밴드를 정한 결정 자질 라벨 → "
        "④ observed_band: 밴드 → ⑤ spontaneity: 자발성.\n"
        "출력은 반드시 주어진 JSON 스키마를 따른다."
    )


class LeveltestBandRead(BaseModel):
    """레벨테스트 답 1개의 '보여준 최고 문법 밴드' 판독 출력(사이드카 단발 콜).

    필드 순서 = 생성 순서(CoT): 언어게이트 → 인용 → 결정자질 → 밴드 → 자발성. 먼저 답변이
    실제 대상 언어인지 판정하고(answer_in_target), 아니면 호출부가 밴드를 무효화한다(닮은 언어
    모국어 누수 차단). heard_grammar 는 밴드 근거가 된 학습자의 실제 구절을 그대로 인용한다
    (추측·재작성 금지). 관통원칙3: observed_band 가 intermediate 이상인데 이 인용이 실제 발화에
    실재하지 않으면 호출부에서 한 밴드 강등한다(환각 방어).
    """

    answer_in_target: bool = True  # ① 이 답변이 실제 대상 언어인가(모국어·타 언어면 False → 밴드 무효)
    heard_grammar: str = ""  # ② 밴드 근거가 된 학습자 실제 구절 인용(없으면 "")
    decisive_feature: str = ""  # 밴드를 정한 결정 자질 라벨(예: "간접화법 -다고 하다")
    observed_band: Literal[
        "no_attempt", "survival", "beginner", "intermediate", "advanced"
    ]
    spontaneity: Literal["spontaneous", "elicited", "echo"] = "spontaneous"


# 밴드 → 서수(0..3). no_attempt 는 여기 없음(→ None 반환). 인용검증 강등의 기준선.
_BAND_ORDINAL: dict[str, int] = {
    "survival": 0,
    "beginner": 1,
    "intermediate": 2,
    "advanced": 3,
}


def _citation_coverage(heard: str, answer: str) -> float:
    """heard_grammar 인용이 실제 답변에 얼마나 실재하는지(0.0~1.0) — ASR 왜곡에 강건한 근거 검증.

    공백·문장부호를 제거해 정규화한 뒤, heard 의 어절 토큰 중 정규화된 answer 안에 부분
    문자열로 나타나는 비율을 센다. 0.0 = 인용이 통째로 부재(환각). 순수 파이썬(LLM 없이).
    """
    def _norm(s: str) -> str:
        return re.sub(r"[\s\W_]+", "", s)

    a = _norm(answer)
    tokens = [t for t in heard.split() if _norm(t)]
    if not a or not tokens:
        return 0.0
    hit = sum(1 for t in tokens if _norm(t) in a)
    return hit / len(tokens)


async def classify_leveltest_band(
    client: genai.Client,
    *,
    answer_text: str,
    prior_question: str | None = None,
    target_language: str = "한국어",
) -> int | None:
    """학습자 답 1개의 '보여준 최고 문법 밴드'를 절대 분류한다(사이드카 1콜).

    노드 매칭이 아니라 학습자 발화 자체가 도달한 문법 천장을 읽는다 — 목표보다
    높은 문법으로 답해도 그 상위 밴드로 인정(call 246 강등 오판 방지).

    Args:
        client: lifespan 이 만든 genai.Client(없으면 통화 자체가 비활성이나 방어).
        answer_text: 학습자가 방금 한 발화(한국어, in_tr 전사).
        prior_question: 직전 비버 질문(문맥용, 선택). 실력 근거로 쓰지 않는다.

    Returns:
        None = no_attempt/판정 불가(머뭇·필러·실패·환각·호출실패) /
        0..3 = survival/beginner/intermediate/advanced.
    """
    # graceful 가드(R5): client 없음 / 빈 발화 → 판정 불능.
    if client is None or not answer_text or not answer_text.strip():
        return None

    context = ""
    if prior_question and prior_question.strip():
        context = f"[직전 비버 질문]\n{prior_question.strip()}\n\n"

    try:
        read = await gemini_analysis.generate_structured(
            client,
            settings.JUDGE_MODEL,
            system_instruction=_band_classify_instruction(target_language),
            prompt=f"{context}[학습자 발화]\n{answer_text.strip()}",
            schema=LeveltestBandRead,
            temperature=0.0,
            thinking_budget=0,  # 통화중 실시간 사이드카 — 지연 최소화(추론 비활성).
        )
    except Exception as exc:  # noqa: BLE001 - 사이드카는 어떤 예외도 흡수(R5)
        logger.warning("leveltest band: 분류 예외(무시) → None: %s", exc)
        return None

    if read is None:
        return None

    # ★ 언어 게이트(관통원칙1: 코드가 심판) — 답변이 대상 언어가 아니면 밴드 무효(None).
    #   닮은 언어(한↔일)에서 유창한 모국어 답변이 높은 밴드로 새는 것을 원천 차단.
    if not read.answer_in_target:
        logger.info("leveltest band: 대상 언어 아님(answer_in_target=False) → None")
        return None

    # no_attempt / 판정불가 → None(엔진이 재시도·강제전진으로 처리).
    ordinal = _BAND_ORDINAL.get(read.observed_band)
    if ordinal is None:
        return None

    # ★ 인용 검증(관통원칙3): intermediate(2) 이상인데 근거 구절이 실제 발화에 '통째로'
    # 부재하면(coverage 0) 환각 → 한 밴드 강등. 엄격 부분문자열이 아니라 어절 토큰 겹침으로
    # 판정한다 — ASR(음성인식) 전사 왜곡·조사/띄어쓰기 드리프트로 인용이 바이트 단위로 정확히
    # 일치하지 않아도, 일부라도 겹치면 신뢰한다(과잉 강등=저평가 방지).
    if ordinal >= _BAND_ORDINAL["intermediate"]:
        heard = (read.heard_grammar or "").strip()
        if _citation_coverage(heard, answer_text) == 0.0:
            logger.info(
                "leveltest band: %s 인용 미검증(heard=%r, coverage=0) → 한 밴드 강등",
                read.observed_band,
                heard,
            )
            ordinal -= 1

    return ordinal


class LeveltestEndRead(BaseModel):
    """레벨테스트 종료 판정(전체 맥락 사이드카) 출력."""

    should_end: bool = False  # 지금 통화를 마쳐도 되는가(충분히 파악됨 or 더 못 올라감)
    reason: str = ""          # 판단 근거(로깅용)


def _leveltest_end_instruction(target_language: str = "한국어") -> str:
    """레벨테스트 종료 판정관 지시문 — 전체 대화 맥락으로 '지금 끝내도 되나'를 판정."""
    t = target_language
    return (
        f"너는 {t} 레벨테스트 통화를 지켜보는 종료 판정관이다. 지금까지의 '전체 대화'를 보고 "
        f"이 통화를 지금 마쳐도 되는지(should_end) 판정하라. 다음 중 하나가 확실하면 should_end=true:\n"
        f"① 학습자의 {t} 실력 상한이 충분히 드러났다 — 더 어려운 질문에도 일관된 수준이라 더 재도 그대로다.\n"
        f"② 학습자가 여러 번 시도했는데도 {t}를 거의/전혀 못 낸다(모국어로만 답하거나 계속 막힘) — "
        f"더 붙잡아도 새로 얻을 게 없다.\n"
        f"★ 보수적으로 판정하라: 아직 몇 번 안 물었거나 학습자가 더 보여줄 여지가 있으면 should_end=false. "
        f"짧은 답 한두 개만 보고 성급히 끝내지 마라 — 확실할 때만 true.\n"
        "출력은 반드시 주어진 JSON 스키마를 따른다."
    )


async def classify_leveltest_should_end(
    client: genai.Client,
    *,
    transcript: list[str],
    obs_max: int,
    target_language: str = "한국어",
) -> bool:
    """전체 대화 맥락으로 레벨테스트를 지금 마쳐도 되는지 판정(사이드카 1콜).

    사장님 아이디어: 매 답변마다 전체 전사를 작은 LLM에 넣어 종료 여부 판정 — 카운터(plateau)
    보다 유연하게 '못하는 사람'을 조기 종료. 실패/불확실/client 없음 → False(안전: 안 끝냄).
    """
    if client is None or not transcript:
        return False
    convo = "\n".join(transcript[-30:])  # 최근 30턴만(컨텍스트 과대·비용 방지)
    try:
        read = await gemini_analysis.generate_structured(
            client,
            settings.JUDGE_MODEL,
            system_instruction=_leveltest_end_instruction(target_language),
            prompt=(
                f"[관측된 최고 밴드] {obs_max} (0=입문 ~ 3=고급, -1=아직 없음)\n"
                f"[전체 대화]\n{convo}"
            ),
            schema=LeveltestEndRead,
            temperature=0.0,
            thinking_budget=0,  # 실시간 사이드카 — 지연 최소화.
        )
    except Exception as exc:  # noqa: BLE001 - 사이드카는 어떤 예외도 흡수(R5)
        logger.warning("leveltest end judge: 예외(무시) → False: %s", exc)
        return False
    if read is not None and read.should_end:
        logger.info("leveltest end judge: should_end=True (%s)", (read.reason or "")[:80])
        return True
    return False
