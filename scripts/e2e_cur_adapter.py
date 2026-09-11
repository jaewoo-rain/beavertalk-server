# -*- coding: utf-8 -*-
"""E2E 하네스 ↔ 서버 cur_* API 어댑터 — **계약이 바뀌면 여기만 고친다.**

계약 출처: docs/plans/2026-09-12-cur-2단계-통화경로-이전.md §2 «API» (B3 구현 전 계획 그대로).
    GET  {prefix}/cur/me            → {lesson:{no,code,level_no,situation,topic}, status, items_total, items_drilled,
                                       open:{expression,freetalk}}
    GET  {prefix}/cur/lessons?level= → [{no,code,level_no,situation,status}]
    GET  {prefix}/calls/{id}/result  → …, quiz_items:[{item_id,surface,meaning,passed,failed[,review?]}]
    POST /__dev/cur-reset            {member_id?, lesson_no?} → cur_member_* 삭제 + progress 를 lesson_no 로
인증: Supabase access token(Bearer) — WS 에 쓰는 것과 같은 토큰(core/deps.get_current_member).

⚠ 가정(B3 가 확정하면 상수만 맞춘다):
  · 라우터 접두 = API_PREFIX(/api/v1). /__dev/* 는 루트.
  · quiz_items 의 `review` 플래그는 선택 — 없으면 하네스가 «통화 전 이미 drilled 였던 항목» 으로 추정한다.
  · /cur/me 는 항목 목록을 싣지 않는다 → 표현학습 항목 예측은 DB(cur_lesson_item·cur_item·cur_member_item)에서 §11 규칙으로.
"""

from __future__ import annotations

from typing import Any, Optional

CUR_API_PREFIX = "/api/v1"
CUR_ME = "/cur/me"
CUR_LESSONS = "/cur/lessons"
CUR_RESET_DEV = "/__dev/cur-reset"
CALL_RESULT = "/calls/{call_id}/result"
COURSE_LOCKED_CODE = "COURSE_LOCKED"        # ServerError.code — 잠긴 프리토킹(recoverable=False, 소켓 닫힘)


class CurApiError(RuntimeError):
    def __init__(self, status: int, body: str, url: str) -> None:
        super().__init__(f"{url} → {status}: {body[:200]}")
        self.status = status
        self.body = body


class CurApi:
    """httpx 동기 클라이언트 한 겹. 실패는 CurApiError(상태·본문) 로 — 호출부가 «엔드포인트 없음(404)» 을 알아볼 수 있게."""

    def __init__(self, base: str, token: str, *, timeout: float = 30.0) -> None:
        import httpx

        self.base = base.rstrip("/")
        self._cli = httpx.Client(timeout=timeout, headers={"Authorization": f"Bearer {token}"})

    # ---- 내부 ----
    def _url(self, path: str, *, prefixed: bool = True) -> str:
        return self.base + (CUR_API_PREFIX if prefixed else "") + path

    def _get(self, path: str, *, prefixed: bool = True, params: dict | None = None) -> Any:
        url = self._url(path, prefixed=prefixed)
        r = self._cli.get(url, params=params)
        if r.status_code != 200:
            raise CurApiError(r.status_code, r.text, url)
        return r.json()

    def _post(self, path: str, body: dict, *, prefixed: bool = True) -> Any:
        url = self._url(path, prefixed=prefixed)
        r = self._cli.post(url, json=body)
        if r.status_code not in (200, 201):
            raise CurApiError(r.status_code, r.text, url)
        return r.json() if r.content else {}

    # ---- 계약 ----
    def me(self) -> dict:
        """GET /cur/me — 지금 차시·상태·항목 수·열림."""
        return self._get(CUR_ME)

    def lessons(self, level: Optional[int] = None) -> list[dict]:
        return self._get(CUR_LESSONS, params={"level": level} if level is not None else None)

    def reset(self, *, member_id: Optional[int] = None, lesson_no: Optional[int] = None) -> dict:
        """POST /__dev/cur-reset — cur_member_* 삭제 + progress 를 lesson_no 로. dev 전용(ENV != prod)."""
        body: dict = {}
        if member_id is not None:
            body["member_id"] = member_id
        if lesson_no is not None:
            body["lesson_no"] = lesson_no
        return self._post(CUR_RESET_DEV, body, prefixed=False)

    def call_result(self, call_id: int) -> dict:
        return self._get(CALL_RESULT.format(call_id=call_id))

    def quiz_items(self, call_id: int) -> list[dict]:
        """result.quiz_items — 5키(item_id·surface·meaning·passed·failed) + 선택 review."""
        res = self.call_result(call_id)
        return list(res.get("quiz_items") or [])


def lesson_of(me: dict) -> dict:
    return dict(me.get("lesson") or {})


def summarize_me(me: dict) -> str:
    L = lesson_of(me)
    op = me.get("open") or {}
    return (f"차시 no={L.get('no')} {L.get('code')} L{L.get('level_no')} «{L.get('situation')}» · status={me.get('status')} · "
            f"drilled {me.get('items_drilled')}/{me.get('items_total')} · open expression={op.get('expression')} freetalk={op.get('freetalk')}")
