"""normalcall 서비스 — 통화 DB I/O(동기) + 통화후 분석 오케스트레이션(비동기).

레이어 규율(플랜): realtime(async WS) → 이 서비스 → core 어댑터/모델. 동기 DB 함수는
`db: Session` 을 받고 명시적 commit(프로젝트 컨벤션). 분석은 gemini 호출이라 async 지만
DB 접근은 `run_db`(run_in_threadpool + 짧은 세션)로 감싼다 — 장수명 세션 점유 금지.

"무엇을 분석하는가"(프롬프트·출력 스키마)는 도메인 지식이라 여기(서비스)가 소유하고,
호출 메커니즘은 core.gemini_analysis 가 담당한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Literal, TypeVar

from fastapi.concurrency import run_in_threadpool
from google import genai
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from core import gemini_analysis, storage, tts
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
from domains.learning.models.level import Level
from domains.learning.models.sentence import Sentence

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
def load_call_setup(db: Session, member_id: int, character_id: int) -> dict:
    """통화 시작에 필요한 프롬프트 입력 + voice 를 한 번에 조회한다(LLM 0).

    Returns:
        {role, personality, rules, voice, level_profile, locale, interests, name, history}.
        ORM 객체가 아니라 평범한 값만 담아 async 컨텍스트로 안전히 넘긴다.
    """
    member = db.get(Member, member_id)
    locale = (member.language if member and member.language else "en")
    name = (member.name if member and member.name else None)
    # 흥미·소재 = 온보딩 학습이유(member_reason) 를 사람이 읽을 한국어 라벨로.
    interests = (
        [REASON_LABELS.get(r.reason, r.reason) for r in member.reasons]
        if member else []
    )
    level_no = member.korean_level if (member and member.korean_level) else 1

    level = db.scalar(select(Level).where(Level.level_no == level_no))
    level_profile = (level.profile if level else "") or ""

    ch = db.get(Character, character_id)
    role = (ch.role if ch else "") or ""
    personality = (ch.personality if ch else "") or ""
    rules = ch.rules if ch else None
    voice = (ch.voice.name if (ch and ch.voice and ch.voice.name) else DEFAULT_VOICE)

    history = _load_history(db, member_id) if member else None

    return {
        "role": role,
        "personality": personality,
        "rules": rules,
        "voice": voice,
        "level_profile": level_profile,
        "locale": locale,
        "interests": interests,
        "name": name,
        "history": history,
    }


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


def create_call(db: Session, member_id: int, character_id: int) -> int:
    """통화 행을 생성하고(status=ongoing) call_id 를 반환한다."""
    call = Call(
        member_id=member_id,
        character_id=character_id,
        call_date=datetime.now(timezone.utc),
        status="ongoing",
    )
    db.add(call)
    db.commit()
    db.refresh(call)
    return call.call_id


def save_segments(db: Session, call_id: int, segments: list[dict], member_id: int) -> int:
    """턴 세그먼트를 CallRawData 로 저장한다(WAV 는 private 버킷 업로드, key 보관).

    segments: [{turn_index, role('user'|'beaver'), text, pcm}]. 빈 세그먼트는 건너뛴다.
    storage 미설정이면 voice_url=None(전사만 저장). 부분 실패해도 가능한 만큼 저장.
    """
    saved = 0
    for seg in segments:
        pcm = seg.get("pcm") or b""
        key = None
        if pcm:
            sr = INPUT_SAMPLE_RATE if seg["role"] == "user" else OUTPUT_SAMPLE_RATE
            base = f"calls/{member_id}/{call_id}/{seg['turn_index']:04d}_{seg['role']}"
            # 표준 MP3 로 저장(어디서든 재생). ffmpeg 없으면 WAV 로 폴백.
            mp3 = pcm16_to_mp3(bytes(pcm), sample_rate=sr)
            if mp3 is not None:
                key = storage.upload(
                    settings.SUPABASE_BUCKET_RECORDINGS, base + ".mp3", mp3, "audio/mpeg"
                )
            else:
                wav = pcm16_to_wav(bytes(pcm), sample_rate=sr)
                key = storage.upload(
                    settings.SUPABASE_BUCKET_RECORDINGS, base + ".wav", wav, "audio/wav"
                )
        db.add(
            CallRawData(
                call_id=call_id,
                role=seg["role"],
                turn_index=seg["turn_index"],
                content=(seg.get("text") or None),
                voice_url=key,
            )
        )
        saved += 1
    db.commit()
    return saved


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


# --------------------------------------------------------------------------- #
# 통화후 분석 (비동기 — gemini 호출 + DB 는 run_db)
# --------------------------------------------------------------------------- #
class LearnedExpression(BaseModel):
    """통화에서 배운 표현 1건."""

    korean: str
    translation: str
    source_type: Literal["asked", "corrected", "drilled"]
    learner_attempt: str | None = None


class CallAnalysis(BaseModel):
    """통화후 분석 1콜의 전체 출력."""

    summary: str
    detected_mode: Literal["study", "chat", "mixed"]
    expressions: list[LearnedExpression]


def _analysis_instruction(locale: str) -> str:
    """통화후 분석용 시스템 지시문(한국어). locale 로 번역/요약 언어를 지정."""
    label = _LOCALE_LABEL.get(locale, _LOCALE_LABEL["en"])
    return (
        "너는 한국어 학습자와 AI 선생님(BEAVER)의 한국어 통화 전사를 분석하는 도구다.\n"
        "전사에서 학습자가 '배운 표현'을 뽑고, 각 표현을 학습자 모국어로 번역하고, "
        "통화 한 줄 요약과 통화 모드를 함께 JSON 으로만 출력하라.\n"
        "[배운 표현의 3가지 종류]\n"
        "- asked: 학습자가 '○○를 한국어로 어떻게 말해요?' 처럼 물어서 비버가 알려준 표현.\n"
        "- corrected: 학습자가 어색하게 말한 것을 비버가 자연스러운 한국어로 고쳐준 표현. "
        "이때 learner_attempt 에 학습자의 원래(어색한) 발화를 넣는다.\n"
        "- drilled: 공부 모드에서 비버가 가르치고 학습자가 따라 말한 표현.\n"
        "[규칙]\n"
        "- korean 에는 반드시 '올바른 최종 한국어'만 넣는다(어색한 발화·오류형 금지).\n"
        "- translation 은 각 표현을 " + label + " 로 번역.\n"
        "- 위 3종에 해당하는 학습 포인트가 없으면 expressions 는 빈 배열([]).\n"
        "- summary 는 통화 내용을 " + label + " 로 반드시 짧은 한 문장 요약. ex:강아지 산택과 음악 취향\n"
        "- detected_mode: 공부 위주면 study, 자유대화 위주면 chat, 둘 다면 mixed.\n"
        "- 전사가 부정확할 수 있으니 명백히 학습된 표현만 보수적으로 뽑는다."
    )


def _build_dialog(db: Session, call_id: int) -> str:
    """CallRawData 를 turn 순서대로 [USER]/[BEAVER] 전사로 조립한다(텍스트만)."""
    rows = db.scalars(
        select(CallRawData)
        .where(CallRawData.call_id == call_id)
        .order_by(CallRawData.turn_index, CallRawData.call_raw_data_id)
    ).all()
    lines = []
    for r in rows:
        if not r.content:
            continue
        who = "USER" if r.role == "user" else "BEAVER"
        lines.append(f"[{who}] {r.content}")
    return "\n".join(lines)


def _save_analysis(db: Session, call_id: int, result: CallAnalysis, locale: str) -> list[tuple[int, str]]:
    """요약/모드 저장 + 표현별 Sentence(+Evaluation placeholder) 생성.

    Returns:
        [(sentence_id, korean), ...] — 이후 TTS 합성 대상.
    """
    call = db.get(Call, call_id)
    if call is not None:
        call.summary = result.summary
        call.mode = result.detected_mode

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


async def analyze_call(
    call_id: int,
    client: genai.Client,
    settings_obj: Settings,
    session_factory: sessionmaker,
    *,
    locale: str,
) -> None:
    """통화 전사를 분석해 표현·요약을 저장하고 표현별 TTS 를 합성한다(전체 graceful).

    통화 종료 후 백그라운드에서 호출된다. 어떤 단계가 실패해도 통화 자체엔 영향이
    없으며, 빈 통화면 status=done(빈 결과), 분석 호출 실패면 failed 로 둔다.
    """
    try:
        dialog = await run_db(session_factory, lambda db: _build_dialog(db, call_id))
        if not dialog.strip():
            logger.info("normalcall 분석: 전사 없음 → done(빈 결과) call_id=%s", call_id)
            await run_db(session_factory, lambda db: set_status(db, call_id, "done"))
            return

        result = await gemini_analysis.generate_structured(
            client,
            settings_obj.JUDGE_MODEL,
            system_instruction=_analysis_instruction(locale),
            prompt=f"[통화 전사]\n{dialog.strip()}",
            schema=CallAnalysis,
        )
        if result is None:
            logger.warning("normalcall 분석: _analyze 실패 → failed call_id=%s", call_id)
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
            return

        pending = await run_db(
            session_factory, lambda db: _save_analysis(db, call_id, result, locale)
        )
        logger.info(
            "normalcall 분석: mode=%s 표현 %d개 call_id=%s",
            result.detected_mode, len(pending), call_id,
        )

        # 표현별 TTS 합성(Vertex Gemini-TTS, genai client 재사용) → public 버킷 업로드
        # → Sentence.voice_url(재생 URL). synthesize_korean 은 (bytes, content_type)|None.
        for sentence_id, korean in pending:
            synthesized = await tts.synthesize_korean(korean, client)  # None 가능(비활성/실패)
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

        await run_db(session_factory, lambda db: set_status(db, call_id, "done"))
        logger.info("normalcall 분석: 완료 → done call_id=%s", call_id)
    except Exception as exc:  # noqa: BLE001 - 백그라운드 분석은 어떤 예외도 흡수
        logger.exception("normalcall 분석: 예외 → failed call_id=%s (%s)", call_id, exc)
        try:
            await run_db(session_factory, lambda db: set_status(db, call_id, "failed"))
        except Exception:  # noqa: BLE001
            pass
