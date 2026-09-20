"""weak_sound 라우터 — 취약 발음 학습(목록·과·평가 반영).

`/calls` 가 아니라 `/pronunciation` 아래 둔다. 이 기능은 **통화 1건에 매달리지 않는다** —
회원의 누적 발음과 국적 통계를 본다. call_id 없는 경로를 /calls 밑에 끼우면 다음 사람이
통화 하위 자원으로 오해한다.

라우터는 얇게 — DTO 검증 + 인증 + service 호출뿐(레이어 규율).
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from core.deps import CurrentMember, DbSession
from domains.learning.schemas.weak_sound import (
    SoundLessonOut,
    SoundResultOut,
    WeakSoundListOut,
)
from domains.learning.service import weak_sound_service as svc

router = APIRouter(prefix="/pronunciation", tags=["pronunciation"])


@router.get("/weak-sounds", response_model=WeakSoundListOut)
def get_weak_sounds(member: CurrentMember, db: DbSession) -> WeakSoundListOut:
    """취약 발음 목록 — 국적별 3 + 내 취약 3 + 추천 1개.

    국적 정보나 발음 기록이 없어도 200 이다(빈 목록). 마이페이지 진입 버튼이 회원 상태에
    따라 실패하면 안 된다.
    """
    return svc.get_weak_sounds(db, member.member_id)


@router.get("/weak-sounds/{sound_key}/lesson", response_model=SoundLessonOut)
def get_lesson(sound_key: str, member: CurrentMember, db: DbSession) -> SoundLessonOut:
    """4단계 콘텐츠 전량 — 이해·단어 4개·문장 1개·평가 1문장.

    단계 이동은 클라가 한다(단어→단어 자동진행에 왕복이 끼면 끊긴다). 없는 소리면 404.
    """
    out = svc.get_lesson(db, member.member_id, sound_key)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "학습할 소리를 찾을 수 없습니다.")
    return out


@router.post("/weak-sounds/{sound_key}/assess", response_model=SoundResultOut)
async def assess(
    sound_key: str,
    member: CurrentMember,
    db: DbSession,
    audio: UploadFile = File(...),
) -> SoundResultOut:
    """평가 단계 녹음(multipart) → 서버 채점 → 학습 전/후 점수 + 조음 재료.

    앱은 **점수를 보내지 않는다**(2026-09-20 확정). 녹음만 올리고 채점은 서버가 한다 —
    클라가 계산한 점수를 받으면 100 을 보내는 것을 막을 수 없다.
    녹음은 WAV(PCM16/16k/mono) 를 전제한다(복습 경로와 같다).

    ⚠️ 연습 단계(단어·문장)는 무채점이라 여기로 오지 않는다. 이 호출은 4단계 1회뿐이다.
    재도전은 몇 번이든 허용하고 최신 점수로 갱신한다(최고점은 따로 남는다).
    """
    raw = await audio.read()
    out = svc.assess(db, member.member_id, sound_key, raw, audio.content_type)
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "학습할 소리를 찾을 수 없습니다.")
    return out
