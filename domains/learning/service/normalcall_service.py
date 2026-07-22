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
def _load_member_character(db: Session, member_id: int, character_id: int) -> dict:
    """회원+캐릭터 공용 조회(일반/레벨테스트 셋업의 공통 분모) — 평범한 값만 반환.

    Returns:
        {role, personality, rules, voice, locale, interests, name,
         korean_level(내부용), member_found(내부용)}.
    """
    member = db.get(Member, member_id)
    locale = (member.language if member and member.language else "en")
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
        "korean_level": (member.korean_level if member else None),
        "member_found": member is not None,
    }


def load_call_setup(db: Session, member_id: int, character_id: int) -> dict:
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
    base = _load_member_character(db, member_id, character_id)
    korean_level = base.pop("korean_level")
    member_found = base.pop("member_found")

    # 레벨 미확정 → 레벨테스트 자동 라우팅 신호(D11). 아래 폴백 레벨 2 는 명시
    # call_type="normal" 등으로 일반 통화가 강행될 때만 실제 사용된다.
    needs_level_test = korean_level is None
    # 레벨 미설정 폴백 = 2(Basic A). 1 은 생존 회화 — 레벨테스트가 배정하는 전용 레벨.
    level_no = korean_level if korean_level else 2

    level = db.scalar(select(Level).where(Level.level_no == level_no))
    level_profile = (level.profile if level else "") or ""

    history = _load_history(db, member_id) if member_found else None

    # 체크판 재료(P2-c2) — 레벨 확정 회원만. 선별 실패는 통화를 막지 않는다(R5 폴백).
    materials = _EMPTY_MATERIALS
    if member_found and korean_level is not None:
        try:
            materials = _load_study_materials(db, member_id, level_no, base["locale"])
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
    """학습항목의 RR 로마자 표기 — meanings JSON 의 "roman" 키(청크가 보유, P2.5).

    teaching_plan 카드(mechanics ⑪)의 roman 줄 재료. 없으면 None(카드에 로마자 미표시).
    """
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


def _load_study_materials(db: Session, member_id: int, level_no: int, locale: str) -> dict:
    """체크판 통화 재료를 1회에 선별한다(mechanics ① 3-b~e — 통화 중 DB 접근 0).

    공부 10(②, 브리지/버벅임 비중 ⑨ 반영) + 대화 가이드(③ 아는 문법≤40+유도 5)
    + 승급 멘트 여부(⑧) + 검출 후보 ≤30(⑤ — 주입 injected=True + 기본 후보 병합).
    learning_item 미시드/결과 0 이면 해당 키 None(R5 — persona 블록 미주입).
    """
    # 커리큘럼 미시드 방어 — 항목이 하나도 없으면 전부 기존 동작(쿼리 1회로 조기 종료).
    if db.scalar(select(LearningItem.item_id).limit(1)) is None:
        return _EMPTY_MATERIALS

    # ⑨ 복습 비중 → 복습 슬롯 수(브리지·버벅임 시 확대).
    ratio = mastery_repository.bridge_or_struggle_ratio(db, member_id)
    band = mastery_repository.band_of(level_no)
    review_slots = (
        _REVIEW_SLOTS_BRIDGE if ratio >= mastery_repository.BRIDGE_REVIEW_RATIO
        else _REVIEW_SLOTS_BY_BAND[band]
    )

    # ② 공부 로드 10 (본편 5 + 예비 5)
    picked = mastery_repository.pick_study_items(
        db, member_id, level_no, review_slots=review_slots, bridge_prev_ratio=ratio
    )
    study_items = [_study_item_dto(e, locale) for e in picked] or None

    # ③ 대화 모드 가이드 — 아는 문법 soft 범위 + 유도 표현(freetalking 미션 힌트)
    grammar = mastery_repository.known_grammar(db, member_id)
    target_items = mastery_repository.pick_chat_targets(db, member_id, level_no)
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
    for c in mastery_repository.load_default_candidates(db, member_id):
        if len(candidates) >= mastery_repository.CANDIDATE_CAP:
            break
        if c["item_id"] not in injected:
            candidates.append(c)
    candidates = candidates[: mastery_repository.CANDIDATE_CAP]

    return {
        "study_items": study_items,
        "known_items": known_items,
        "promotion_notice": mastery_repository.promotion_pending(db, member_id),  # ⑧
        "candidates": candidates or None,
    }


def create_call(
    db: Session, member_id: int, character_id: int, call_type: str = "normal"
) -> int:
    """통화 행을 생성하고(status=ongoing) call_id 를 반환한다.

    call_type: "normal"(기본) | "level_test" — 콜타입 라우팅 결과(call_session 결정).
    """
    call = Call(
        member_id=member_id,
        character_id=character_id,
        call_date=datetime.now(timezone.utc),
        status="ongoing",
        call_type=call_type,
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
    evidence_summary = mastery_service.apply_evidence(db, member_id, call_id, verified)

    levelup = mastery_service.evaluate_level_up(db, member_id, trigger_call_id=call_id)
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
        # 표현별 TTS 합성(Cloud TTS, 한국어 Chirp3-HD) → public 버킷 업로드
        # → Sentence.voice_url(재생 URL). synthesize 는 (bytes, content_type)|None.
        # 통화 캐릭터의 voice 로 합성 → 표현 오디오가 방금 통화한 목소리와 같다.
        # 문장 단위 graceful — 한 문장 실패가 나머지 문장·체크판을 막지 않는다.
        call_voice = await run_db(session_factory, lambda db: _voice_for_call(db, call_id))
        for sentence_id, korean in pending:
            try:
                synthesized = await tts.synthesize(korean, voice=call_voice)  # None 가능(비활성/실패)
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
    """레벨테스트 판정 1콜의 전체 출력.

    ⚠ 필드 순서 = 생성 순서(구조화 출력 내 CoT 강제): 인용 → 추론 → 밴드 →
    밴드 내 단계 → 최종 레벨 → 신뢰도 → 표본질 → 요약 → 학습자 피드백.
    수치 제약(level_in_band 1~4, level_no 1~13)은 pydantic 하드 제약으로 걸지
    않는다 — 위반 시 파싱 전체가 죽는 것보다 서버 클램프가 안전(judge 지시문+
    description 으로 유도, _clamp_assessed_level 이 강제).
    """

    evidence: list[str] = Field(
        description="학습자(USER)의 한국어 발화 원문 인용, 최대 5개(수정·번역 금지)"
    )
    reasoning: str = Field(description="인용을 근거로 한 판정 추론(한국어)")
    band: Literal["beginner", "intermediate", "advanced", "unknown"] = Field(
        description="밴드 — beginner=레벨2~5 / intermediate=6~9 / advanced=10~13 / unknown=표본 부족"
    )
    level_in_band: int | None = Field(
        default=None, description="밴드 내 단계 1~4 (band=unknown 이면 null)"
    )
    level_no: int | None = Field(
        default=None, description="최종 앱 레벨 1~13 (1=생존 회화, band=unknown 이면 null)"
    )
    confidence: Literal["high", "medium", "low"] = Field(description="판정 신뢰도")
    sample_quality: Literal["sufficient", "sparse", "none"] = Field(
        description="한국어 발화 표본질 — 2턴 이하면 sparse/none"
    )
    summary: str = Field(description="통화 핵심 소재 요약(학습자 모국어 명사구 2~4어절)")
    feedback_for_learner: str = Field(
        description="학습자에게 보여줄 격려 1~2문장(모국어, 레벨·점수 숫자 금지)"
    )


# 밴드 → 앱 레벨(1~13) 범위. 레벨 1(생존 회화)은 밴드 밖 특수 배정.
_BAND_RANGE: dict[str, tuple[int, int]] = {
    "beginner": (2, 5),
    "intermediate": (6, 9),
    "advanced": (10, 13),
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


def _load_leveltest_rubric() -> str:
    """루브릭 텍스트 로드 — 파일(assets/level/leveltest_rubric.md) → 상수 폴백."""
    try:
        if _LEVELTEST_RUBRIC_PATH.is_file():
            text = _LEVELTEST_RUBRIC_PATH.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError as exc:
        logger.warning("leveltest 루브릭 파일 읽기 실패 → 상수 폴백: %s", exc)
    return _DEFAULT_LEVELTEST_RUBRIC


def _leveltest_instruction(
    locale: str, rubric: str, locale_label: str | None = None
) -> str:
    """레벨테스트 판정관 시스템 지시문(한국어). 근거는 USER 한국어 발화만,
    ASR 왜곡 주의, 판정 절차(인용→밴드→단계→레벨) 강제, 망설여지면 낮은 쪽."""
    label = locale_label or _LOCALE_LABEL.get(locale, _LOCALE_LABEL["en"])
    return (
        "너는 한국어 학습자와 AI 선생님(BEAVER)의 레벨테스트 통화 전사를 보고 학습자의 "
        "한국어 레벨(1~13)을 판정하는 도구다. JSON 으로만 출력하라.\n"
        "[근거 규칙]\n"
        "- 판정 근거는 오직 [USER]의 '한국어' 발화뿐이다. [BEAVER](선생님) 발화와 USER 의 "
        "모국어 발화는 실력의 근거가 아니다(비버를 따라 말한 직후의 단순 반복도 약한 근거로만).\n"
        "- 전사는 음성인식(ASR) 결과라 철자·띄어쓰기가 왜곡될 수 있다. 철자·맞춤법을 기준으로 "
        "삼지 말고, 사용한 문법의 폭(문형 다양성)·어휘 등급·응답 길이·질문에 맞게 대응했는지를 "
        "기준으로 삼아라.\n"
        "[판정 절차 — 반드시 이 순서대로]\n"
        "① evidence: 학습자(USER)의 한국어 발화 원문을 최대 5개 인용해 모은다(수정·번역 금지).\n"
        "② band: 아래 [레벨 기준표]에 비추어 밴드를 먼저 정한다 — beginner(레벨 2~5) / "
        "intermediate(레벨 6~9) / advanced(레벨 10~13).\n"
        "③ level_in_band: 밴드 안에서 단계 1~4 를 정한다.\n"
        "④ level_no 계산: beginner=1+level_in_band(→2~5), intermediate=5+level_in_band(→6~9), "
        "advanced=9+level_in_band(→10~13). 단, 한국어 발화가 있긴 하나 전부 인사 수준 미만이거나 "
        "사실상 모국어뿐이면 level_no=1(생존 회화)로 한다.\n"
        "⑤ 두 레벨 사이에서 망설여지면 항상 낮은 쪽을 골라라.\n"
        "[표본 규칙]\n"
        "- 학습자의 한국어 발화가 2턴 이하면 sample_quality 를 sparse(빈약) 또는 none(전무)으로 "
        "하고, band=unknown, level_in_band=null, level_no=null 로 둔다.\n"
        "[출력 필드 규칙]\n"
        "- summary: 통화의 핵심 소재를 " + label + " 로 요약. 완결 문장이 아니라 명사구 2~4어절.\n"
        "- feedback_for_learner: 학습자에게 보여줄 따뜻한 격려 1~2문장(" + label + "). "
        "레벨·점수·등급 같은 숫자는 절대 쓰지 마라.\n"
        "[레벨 기준표]\n" + rubric
    )


def _clamp_assessed_level(result: LevelAssessment) -> int | None:
    """LLM 판정을 서버 규칙으로 클램프해 최종 level_no(1~13)를 확정한다.

    AI는 증인, 코드가 심판(관통 원칙 1) — band-level_no 정합을 코드가 강제한다.
    이 함수는 발화 표본이 20자 이상 존재한 뒤에만 호출된다(none=모국어뿐 전제).
    None 반환 = LLM 모순 출력(판정 신뢰 불가) — 호출부가 status=failed·미저장 처리.
    """
    # 모순: 표본이 충분(sufficient)한데 밴드를 못 정했다(unknown) — 판정 신뢰 불가.
    if result.band == "unknown" and result.sample_quality == "sufficient":
        return None
    # 특수: 밴드 미상/표본 전무 — 발화(20자+)는 있었으니 사실상 모국어만 → 생존 1.
    if result.band == "unknown" or result.sample_quality == "none":
        return 1
    # 명시적 생존 판정(한국어 발화가 전부 인사 수준 미만)은 표본이 sufficient 가 아닐
    # 때만 존중. sufficient + 밴드 명시인데 1 이면 모순 → 아래 밴드 재계산 경로로.
    if result.level_no == 1 and result.sample_quality != "sufficient":
        return 1

    lo, hi = _BAND_RANGE[result.band]
    level = result.level_no
    if level is None or not (lo <= level <= hi):
        # band-level_no 불일치 → band 기준 재계산(밴드 시작 + 단계 - 1).
        if result.level_in_band is not None and 1 <= result.level_in_band <= 4:
            level = lo + result.level_in_band - 1
        else:
            # 그래도 불능 → 밴드 중앙값−1(망설여지면 낮게): 3 / 7 / 11.
            level = (lo + hi) // 2

    # 표본 빈약 + 저신뢰 → 1단계 하향(하한 1). "애매하면 항상 낮게".
    if result.sample_quality == "sparse" and result.confidence == "low":
        level -= 1
    return max(1, min(13, level))


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
    prior_level = member.korean_level  # 최초 배정이면 None(placement from_level)
    member.korean_level = level_no
    call.assessed_level = level_no
    call.assessment_note = result.reasoning
    call.summary = result.summary
    call.status = "done"
    # grandfathering(P2, mechanics ⑩): ≤k−2 → MASTERED(placement) / k−1 → INTRODUCED /
    # ≥k → UNSEEN(행 없음) + history(reason='placement') — 레벨 배정과 같은 커밋에 합류.
    mastery_service.apply_grandfathering(
        db, member_id, level_no, trigger_call_id=call_id, from_level=prior_level
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
    target_language 는 데모 대비 시그니처 유지용 — 루브릭·판정은 한국어 기준이다.
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
                locale, _load_leveltest_rubric(), locale_label
            ),
            prompt=f"[통화 전사]\n{dialog.strip()}",
            schema=LevelAssessment,
            temperature=0.0,
        )
        if result is None:
            logger.warning("leveltest 판정: LLM 실패 → failed·미저장 call_id=%s", call_id)
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
            return

        level_no = _clamp_assessed_level(result)
        if level_no is None:
            # LLM 모순 출력(예: sufficient 인데 band=unknown) — 판정 신뢰 불가 → 미저장.
            logger.warning(
                "leveltest 판정: 모순 출력(band=%s sample=%s) → failed·미저장 call_id=%s",
                result.band, result.sample_quality, call_id,
            )
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
            return
        logger.info(
            "leveltest 판정: band=%s level_in_band=%s level_no(LLM)=%s → 확정 %d "
            "(confidence=%s sample=%s) call_id=%s member=%s",
            result.band, result.level_in_band, result.level_no, level_no,
            result.confidence, result.sample_quality, call_id, member_id,
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
