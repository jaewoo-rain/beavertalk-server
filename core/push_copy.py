"""알림 푸시의 표시 문구 — **서버가 로케일을 아는 경우에만** 쓴다.

수신통화(`push_defaults.py`)와 다르다. 거기서는 잠금화면 발신자명이라 로케일을 알 수 없어
브랜드명 하나로 뒀다. 여기는 **회원 행이 있는 발송**이라 `member.language` 를 안다.

⚠ 문구를 여기 두는 것은 **한시 조치**다. 앱은 로케일 30종을 갖고 있고 서버는 둘뿐이다.
   앱이 data-only 페이로드를 받아 자기 문구로 그리게 되면 이 모듈을 지워야 한다.
   그때까지 모르는 로케일은 **영어**로 간다 — B2B 학습자는 한국어를 배우는 외국인이라
   한국어 폴백이 오히려 안 읽힌다.
"""

from __future__ import annotations

# (제목, 본문) — 본문의 `{class_name}` 은 반 이름이다(교사 자유입력 · 번역하지 않는다).
_HOMEWORK_REMINDER: dict[str, tuple[str, str]] = {
    "ko": ("아직 안 한 과제가 있어요", "{class_name} · 마감이 다가옵니다."),
    "en": ("You have homework left", "{class_name} · The deadline is coming up."),
}
_FALLBACK = "en"


def homework_reminder(language: str | None, *, class_name: str) -> tuple[str, str]:
    """숙제 미수행 알림의 제목·본문."""
    title, body = _HOMEWORK_REMINDER.get((language or "").lower(), _HOMEWORK_REMINDER[_FALLBACK])
    return title, body.format(class_name=class_name)
