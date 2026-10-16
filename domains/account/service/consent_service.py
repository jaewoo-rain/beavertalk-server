"""가입 동의 기록·판정 (PM-DEC-735·736 · 앱 빌드 72 동의 화면).

- 필수 항목 4개: 만 14세 · 이용약관 · 개인정보 수집·이용(음성 포함) · 개인정보 국외 이전.
- 항목마다 「동의한 문서 버전」을 남긴다. 현행 버전은 [CURRENT_VERSIONS] 한 곳에서만 정한다.
- 재표시(PM-DEC-736 ③)는 **필수 문서 버전이 바뀔 때만**: 항목 중 하나라도 현행 버전 행이 없으면 consent_required.
- 동의 없이 이탈한 계정은 지우지 않는다(PM-DEC-736 ②) — 다음 로그인 때 consent_required 로 다시 뜬다.
"""

from __future__ import annotations

from typing import Iterable, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from domains.account.models.member_consent import MemberConsent

# ⭐ 문서 버전 = 그 문서 시행일. 약관·처리방침을 개정해 다시 동의를 받아야 하면 **여기 값만** 바꾼다.
#   age14 는 문서가 없어 문구 판(v1)으로 둔다. privacy_collect·overseas_transfer 는 처리방침 버전을 따른다.
TERMS_VERSION = "2026-10-16"
PRIVACY_VERSION = "2026-10-16"
CURRENT_VERSIONS: dict[str, str] = {
    "age14": "v1",
    "terms": TERMS_VERSION,
    "privacy_collect": PRIVACY_VERSION,
    "overseas_transfer": PRIVACY_VERSION,
}
REQUIRED_ITEMS: tuple[str, ...] = tuple(CURRENT_VERSIONS)


def _agreed_current(db: Session, member_id: int) -> set[str]:
    """현행 버전으로 동의한 항목 코드 집합."""
    rows = db.execute(
        select(MemberConsent.item, MemberConsent.doc_version).where(MemberConsent.member_id == member_id)
    ).all()
    return {item for item, ver in rows if CURRENT_VERSIONS.get(item) == ver}


def consent_required(db: Session, member_id: int) -> bool:
    """필수 항목 중 현행 버전 동의가 하나라도 없으면 True(앱이 동의 화면을 띄운다)."""
    return not set(REQUIRED_ITEMS) <= _agreed_current(db, member_id)


def record(
    db: Session,
    member_id: int,
    items: Iterable[tuple[str, str]],
    *,
    app_version: Optional[str] = None,
    platform: Optional[str] = None,
    locale: Optional[str] = None,
) -> int:
    """필수 4항목 동의를 한 번에 기록한다. 새로 쓴 행 수를 돌려준다.

    - 필수 항목이 하나라도 빠지거나 모르는 항목이 오면 422(부분 동의는 받지 않는다 — 화면이 전부 켜야 버튼이 열린다).
    - 버전이 현행과 다르면 409 + 현행 버전(앱이 옛 문서를 보여준 경우 · 앱은 프로필을 다시 받아 화면을 다시 띄운다).
    - 이미 현행 버전으로 동의한 항목은 다시 쓰지 않는다(재전송·두 번 누름 멱등).
    """
    given: dict[str, str] = {}
    for item, ver in items:
        if item not in CURRENT_VERSIONS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"알 수 없는 동의 항목: {item}")
        given[item] = ver
    missing = [i for i in REQUIRED_ITEMS if i not in given]
    if missing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"필수 동의 항목 누락: {', '.join(missing)}")
    stale = {i: v for i, v in given.items() if v != CURRENT_VERSIONS[i]}
    if stale:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": "동의 문서 버전이 현행과 다릅니다.", "current_versions": CURRENT_VERSIONS},
        )
    done = _agreed_current(db, member_id)
    new = [i for i in REQUIRED_ITEMS if i not in done]
    for item in new:
        db.add(MemberConsent(
            member_id=member_id, item=item, doc_version=CURRENT_VERSIONS[item],
            app_version=app_version, platform=platform, locale=locale,
        ))
    db.commit()
    return len(new)
