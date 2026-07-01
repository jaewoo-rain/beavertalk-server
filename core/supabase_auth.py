"""Supabase Auth 토큰 검증 (인증 주체 = Supabase GoTrue).

프론트(Flutter/supabase-js)가 Supabase 로 로그인/가입/비번재설정/OAuth 를 끝내고
받은 access token(JWT)을 우리 API 에 Bearer 로 보낸다. 여기서 그 토큰을 검증해
auth user(uuid, email)를 얻는다. service_role 클라이언트의 auth.get_user(jwt) 사용
(서명방식·키교체에 무관하게 Supabase 가 직접 검증 — 옆 프로젝트와 동일 패턴).

Supabase 미설정/패키지 없음/검증 실패 → None (호출부가 401 처리).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from core import storage  # service_role 클라이언트 재사용

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AuthUser:
    uid: str                    # auth.users.id (UUID 문자열)
    email: Optional[str] = None


def verify_token(token: str) -> Optional[AuthUser]:
    """Supabase access token 검증 → AuthUser. 실패/미설정 시 None."""
    if not token:
        return None
    client = storage._get_client()  # SUPABASE_URL + SERVICE_KEY 로 만든 클라이언트
    if client is None:
        logger.warning("supabase_auth: Supabase 클라이언트 없음(설정 확인) → 인증 불가")
        return None
    try:
        resp = client.auth.get_user(token)
        user = getattr(resp, "user", None)
        if user is None or not getattr(user, "id", None):
            return None
        return AuthUser(uid=str(user.id), email=getattr(user, "email", None))
    except Exception as exc:  # noqa: BLE001 - 토큰 무효/네트워크 등 모두 인증 실패로
        logger.info("supabase_auth: 토큰 검증 실패: %s", exc)
        return None


def delete_auth_user(uid: str) -> bool:
    """Supabase auth.users 사용자 삭제(Admin API). 성공 True, 실패/미설정 False.

    회원 탈퇴 시 로컬 member 행과 함께 인증 주체(auth.users)를 지워야 한다. 이걸
    안 지우면 남은 토큰으로 요청 시 find_or_create_by_auth 가 member 를 재생성해
    계정이 부활한다. service_role 클라이언트만 admin API 를 호출할 수 있다.
    """
    if not uid:
        return False
    client = storage._get_client()  # SUPABASE_URL + SERVICE_KEY 로 만든 클라이언트
    if client is None:
        logger.warning("supabase_auth: Supabase 클라이언트 없음(설정 확인) → auth 사용자 삭제 불가")
        return False
    try:
        client.auth.admin.delete_user(uid)
        logger.info("supabase_auth: auth 사용자 삭제 완료 uid=%s", uid)
        return True
    except Exception as exc:  # noqa: BLE001 - 네트워크/권한/이미삭제 등 모두 실패로
        logger.warning("supabase_auth: auth 사용자 삭제 실패 uid=%s — %s", uid, exc)
        return False
