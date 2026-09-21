"""sound_audio (취약 발음 학습용 미리 구운 TTS) — learning 도메인.

## 왜 미리 굽나

`POST /tts/speech` 는 **아무것도 저장하지 않는다**(그 서비스 docstring 참조). 같은 문장을
열 번 요청하면 열 번 합성하고 열 번 과금된다. 유일한 절약 장치인 ETag 304 는 앱이
`If-None-Match` 를 보내야 도는데 안 보내고 있었고, 앱 캐시는 메모리 32개라 앱을 끄면
사라진다. 즉 **앱을 켤 때마다 전량 재합성**이었다.

그런데 이 기능의 문장은 고정 콘텐츠다 — 30과 × 9개 = 고유 문장 233개, 1,070자.
한 번 구워 두면 그 뒤로는 0원이다. 사용자 한 명이 몇 과만 돌아도 그보다 더 쓴다.

## ⛔ 서명 URL 을 저장하지 마라 — object key 를 저장한다

`object_key` 는 **버킷 안 경로**다. 서명 URL 이 아니다. 서명 URL 을 DB 에 넣으면 7일 뒤
만료되고 그 과는 **영구히 소리가 죽는다** — 2026-08-31 에 실제로 났던 사고이고
`core/storage.py` 의 `playback_url()` 이 그걸 막으려고 있는 함수다. 읽을 때마다 새로 서명한다.

## ⛔ 캐릭터 음색을 쓰지 않는다 — 기준 음성 하나다

발음 학습은 **따라 할 본보기**다. 사람마다 다른 목소리로 나오면 기준이 되지 않는다.
그래서 회원의 캐릭터와 무관하게 **언어 기본 음성**(한국어는 `Aoede`)으로 굽는다.
사장님 지시(2026-09-21) — 「발음 학습은 이전에 한 것처럼 기본적인 TTS로」.

⚠ 통화·힌트는 여전히 캐릭터 음색이다. 이 표는 **발음 학습 전용**이다.

## 그런데도 키에 (문장 · 음색 · 엔진) 셋을 두는 이유

지금은 조합이 하나뿐이다. 그래도 축을 남기는 것은, 기본 음성이나 엔진이 나중에 바뀌었을 때
**옛 소리를 조용히 계속 내보내지 않게** 하려는 것이다. 값이 바뀌면 행이 안 맞아 새로 굽는다.
`tts_service` 의 ETag 키와 같은 축이다 — 두 곳이 어긋나면 한쪽이 옛 소리를 준다.

`text_hash` 로 찾는 이유는 문장이 길고 인덱스 대상이라서다. `text` 는 재생성·디버깅용으로
같이 들고 있는다(해시만 있으면 무엇을 다시 구워야 할지 알 수 없다).

## 없으면 어떻게 되나

**없어도 죽지 않는다.** 앱이 URL 을 못 받으면 종전대로 `POST /tts/speech` 로 떨어진다(R5).
⚠ 단 그 폴백은 캐릭터 음색으로 나온다 — 목소리가 갈린다. 미리 굽는 스크립트가 전량을
채우므로 실제로는 안 타는 길이지만, 타면 소리가 다르다는 것을 알고 있어라.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from sqlalchemy import BigInteger, Identity, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin

# ── 이 표의 키 규약 ─────────────────────────────────────────────────────── #
# 굽는 쪽(scripts/pregen_sound_audio.py)과 읽는 쪽(weak_sound_service)이 **같은 값**을
# 써야 한다. 어긋나면 미리 구운 것을 못 찾고 조용히 매번 합성한다 — 로그에도 안 남는다.
# 그래서 표 정의 옆에 둔다(스크립트에 두면 서비스가 스크립트를 import 하게 된다).

#: 발음 학습이 쓰는 TTS 엔진. ⚠ 앱의 `auto_practice_controller._engine` 과 같아야 한다.
LESSON_ENGINE = "gemini-tts"
#: 음색 — None 은 **언어 기본 음성**(한국어 Aoede). 캐릭터 음색을 쓰지 않는다(위 설명 참조).
LESSON_VOICE: Optional[str] = None
#: 합성 언어. 배우는 대상이 한국어라 고정이다.
LESSON_LANGUAGE = "ko"


def audio_text_hash(text: str) -> str:
    """문장 → 조회 키(sha256 앞 32자). 굽는 쪽·읽는 쪽 공용."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


class SoundAudio(Base, TimestampMixin):
    __tablename__ = "sound_audio"
    __table_args__ = (
        UniqueConstraint("text_hash", "voice", "engine", name="uq_sound_audio"),
    )

    sound_audio_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    text_hash: Mapped[str] = mapped_column(
        Text, nullable=False, index=True,
        comment="sha256(text) 앞 32자 — 조회 키(문장이 길어 본문 인덱스는 비싸다)",
    )
    text: Mapped[str] = mapped_column(
        Text, nullable=False, comment="합성한 원문 — 재생성·대조용",
    )
    voice: Mapped[Optional[str]] = mapped_column(
        Text, comment="음색 이름(Fenrir 등). None 이면 언어 기본 음색",
    )
    engine: Mapped[Optional[str]] = mapped_column(
        Text, comment="TTS 엔진(gemini-tts 등). None 이면 기본 엔진",
    )
    object_key: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="⛔ 버킷 안 경로다. 서명 URL 을 넣지 마라 — 7일 뒤 죽는다",
    )
