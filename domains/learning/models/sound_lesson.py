"""sound_lesson (취약 발음 학습 콘텐츠 마스터) — learning 도메인.

소리 1개 = 학습 1과. 30행(자음 소리 25 + 음운 규칙 5)이고 **읽기 전용 마스터**다.
런타임 쓰기는 없다 — `scripts/seed_sound_lessons.py` 가 `assets/pronunciation/lessons.json`
으로 upsert 한다. 앱은 DB 만 읽는다(에셋 파일을 앱에 넣지 않는다 — 콘텐츠를 고칠 때마다
앱 심사를 다시 받게 되기 때문).

4단계 콘텐츠(이해·단어 4개·문장 1개·평가 1문장)는 `payload` JSON 한 컬럼에 담는다.
단계마다 테이블을 쪼개면 조인 4번으로 한 화면을 그리게 되고, 콘텐츠는 서버가 통째로
내려주면 끝나는 읽기 전용 덩어리라 정규화 이득이 없다.
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import JSON, BigInteger, Identity, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, TimestampMixin


class SoundLesson(Base, TimestampMixin):
    __tablename__ = "sound_lesson"

    sound_lesson_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    sound_key: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True,
        comment="소리 키 — onset_ㄱ · coda_ㄹ · rule_연음 (집계·점수와 같은 단위)",
    )
    type: Mapped[str] = mapped_column(
        Text, nullable=False, comment="sound(자모 소리) | rule(음운 규칙)",
    )
    label: Mapped[str] = mapped_column(
        Text, nullable=False, comment="표시 라벨 — 받침 ㄹ · 연음",
    )
    position: Mapped[Optional[str]] = mapped_column(
        Text, comment="onset | coda (소스 lessons.json 표기). 규칙 항목은 없음(NULL)",
    )
    jamo: Mapped[Optional[str]] = mapped_column(
        Text, comment="자모 1자. 규칙 항목은 없음(NULL)",
    )
    diagram: Mapped[Optional[str]] = mapped_column(
        Text, comment="조음 도해 자산명 — Airflow / <이름>. 규칙 항목은 없음",
    )
    card_desc: Mapped[str] = mapped_column(
        Text, nullable=False, comment="목록 카드의 한 줄 설명(소리 내는 법. 오류 서술 금지)",
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", comment="목록 정렬 순서",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False,
        comment="4단계 콘텐츠 전량 — how_to·words·sentence·test(+규칙은 formula·symbol)",
    )
