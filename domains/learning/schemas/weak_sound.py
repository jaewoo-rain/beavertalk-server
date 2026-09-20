"""취약 발음 학습 스키마 (pydantic v2) — learning 도메인.

`GET  /pronunciation/weak-sounds`             목록 2종(국적별·나의) + 추천 1개
`GET  /pronunciation/weak-sounds/{sound_key}/lesson`   4단계 콘텐츠 전량
`POST /pronunciation/weak-sounds/{sound_key}/assess`   평가 녹음 제출 → 전/후 점수

설계 사실 2개가 스키마에 드러난다.
1. **연습 단계(단어·문장)에 제출 API 가 없다.** 무채점·자동진행이라 서버가 알 일이 없다.
   점수는 평가 단계 1번만 움직인다.
2. **점수의 출처가 둘이다.** 학습한 소리는 member_sound_score(=평가 점수), 안 한 소리는
   복습 집계값이다. 어느 쪽인지 클라가 알아야 「학습 전」 막대를 그릴 수 있어서 `learned`
   와 `baseline_score` 를 같이 준다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class WeakSoundItem(BaseModel):
    """목록 카드 1장.

    Attributes:
        sound_key: 소리 단위 키(`coda_ㄹ`·`rule_연음`). 학습 진입·점수 갱신의 식별자.
        label: 표시 라벨(받침 ㄹ·연음).
        card_desc: 카드 한 줄 설명 — **소리 내는 법**이다(오류 서술 아님).
        score: 지금 점수 0~100. 표본이 없으면 None(카드에 「기록 없음」).
        learned: 이 소리를 학습해서 나온 점수인가. False 면 복습 집계값이다.
        baseline_score: 첫 학습 직전 점수. 결과 화면의 「학습 전」 막대. 미학습이면 None.
        share: 국적별 목록에서만 채운다 — 그 나라 화자 중 이 소리를 틀리는 비율 %.
    """

    sound_key: str
    label: str
    card_desc: str
    type: str
    diagram: Optional[str] = None
    score: Optional[int] = None
    learned: bool = False
    baseline_score: Optional[int] = None
    attempts: int = 0
    share: Optional[int] = None
    last_learned_at: Optional[datetime] = None


class NationalWeakSounds(BaseModel):
    """국적별 섹션. 억양 국가가 없거나 통계에 없는 나라면 items 가 빈 리스트다."""

    country: Optional[str] = None
    items: list[WeakSoundItem]


class WeakSoundListOut(BaseModel):
    """취약 발음 목록 화면 전체.

    recommended 는 「국적별 1순위 중 80점 미만」 첫 소리다. 전부 80점 이상이면 내 취약
    1순위로 넘어가고, 그것도 없으면 None(추천 배지 미표시).
    """

    national: NationalWeakSounds
    mine: list[WeakSoundItem]
    recommended: Optional[str] = None


class SoundLessonOut(BaseModel):
    """4단계 콘텐츠 전량 — 한 번에 내려주고 단계 이동은 클라에서만 한다.

    단계마다 왕복하면 자동진행(단어→다음 단어)에 네트워크 지연이 끼어든다.
    payload 는 마스터 JSON 그대로다(how_to·words·sentence·test, 규칙은 formula·symbol).
    """

    sound_key: str
    label: str
    type: str
    position: Optional[str] = None
    jamo: Optional[str] = None
    diagram: Optional[str] = None
    card_desc: str
    payload: dict[str, Any]
    score: Optional[int] = None
    learned: bool = False


class CharScoreOut(BaseModel):
    """평가 문장의 글자 1개 — 결과 화면의 글자별 상/중/하."""

    char: str
    score: int
    grade: str


class PhonemeMissOut(BaseModel):
    """틀린 자모 1개 + 그 글자 위치.

    ⚠ dev 의 speechsuper 는 아직 이 값을 내지 않아 **현재는 항상 빈 리스트**다
      (`phoneme_misses` 기능은 `feat/pronunciation` 에만 있다). 결과 화면의 조음 카드는
      이 값이 아니라 **학습한 소리 자체**로 그리므로 화면은 지금도 온전하다.
    """

    char_index: int
    expected: str


class SoundResultOut(BaseModel):
    """제출 결과 — 결과 화면의 62 → 84 막대 + 조음 카드 재료.

    `after` 는 **서버가 채점한 값**이다(SpeechSuper pronunciation). 클라가 점수를 보내는
    설계는 폐기했다 — 위조가 가능했다.
    """

    sound_key: str
    label: str
    before: Optional[int] = None
    after: int
    delta: Optional[int] = None
    best_score: int
    attempts: int
    text: str
    char_scores: list[CharScoreOut]
    phoneme_misses: list[PhonemeMissOut]
