"""U32-② — 서버 시작 때 오래 «analyzing» 에 멈춘 통화를 되살린다.

통화 뒤 분석·레벨 판정은 통화가 끝난 뒤 fire-and-forget 태스크로 돈다. 배포·스케일 다운으로 인스턴스가
내려가면 그 태스크가 사라지고 통화는 analyzing 으로 굳는다(call 2066 실측). 수동 재분석
(POST /calls/{id}/reanalyze)은 failed 만 받아 굳은 통화를 살릴 길이 없었다.

- 대상: status='analyzing' 이고 마지막 시각(fragment_ended_at → fragment_started_at → call_date)이 기준 시간보다 오래된 통화
- 처리: 한 번에 최대 N 건. 행마다 «analyzing 이면 failed» 비교 후 쓰기(compare-and-set)라 여러 인스턴스가 동시에 돌아도
  한 통화는 한 인스턴스만 가져간다. 가져간 통화는 수동 재분석과 같은 길(prepare_reanalysis → trigger_reanalysis)로 다시 돌린다.
- 스위치 ANALYZING_SWEEP: off(기본 · 아무것도 안 함) · dry(대상만 로그) · on(되살림)
  ⚠ test-api 는 운영 DB 를 쓴다 — 시험 태그는 dry 로만 띄운다.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from domains.learning.models.call import Call

logger = logging.getLogger(__name__)


def stale_analyzing(db: Session, *, stale_s: float, limit: int, now: datetime | None = None) -> list[tuple[int, int]]:
    """기준 시간보다 오래 analyzing 인 통화 (call_id, member_id) — 오래된 것부터 limit 건."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=stale_s)
    last = func.coalesce(Call.fragment_ended_at, Call.fragment_started_at, Call.call_date)
    rows = db.execute(
        select(Call.call_id, Call.member_id)
        .where(Call.status == "analyzing", last.is_not(None), last < cutoff)
        .order_by(Call.call_id)
        .limit(limit)
    ).all()
    return [(int(r[0]), int(r[1])) for r in rows]


def claim(db: Session, call_id: int) -> bool:
    """analyzing 이면 failed 로 돌린다(다른 인스턴스가 먼저 가져갔으면 False)."""
    res = db.execute(
        update(Call).where(Call.call_id == call_id, Call.status == "analyzing").values(status="failed")
    )
    db.commit()
    return res.rowcount == 1


async def sweep(app_state, settings) -> dict:
    """한 번 돈다. 반환: {"mode", "found", "claimed", "restarted"} — 로그·시험용."""
    from domains.learning.realtime.call_session import trigger_reanalysis
    from domains.learning.service import normalcall_service as svc

    mode = str(getattr(settings, "ANALYZING_SWEEP", "off") or "off").lower()
    out = {"mode": mode, "found": 0, "claimed": 0, "restarted": 0}
    if mode not in ("dry", "on"):
        return out
    sf = app_state.session_factory
    stale_s = float(getattr(settings, "ANALYZING_SWEEP_STALE_S", 1800))
    limit = int(getattr(settings, "ANALYZING_SWEEP_LIMIT", 20))
    found = await svc.run_db(sf, lambda db: stale_analyzing(db, stale_s=stale_s, limit=limit))
    out["found"] = len(found)
    logger.info("analyzing 정리: 모드=%s 기준=%.0fs 상한=%d 대상=%d %s", mode, stale_s, limit, len(found),
                [c for c, _ in found][:limit])
    if mode != "on" or not found:
        return out
    client = getattr(app_state, "genai_client", None)
    if client is None:
        logger.warning("analyzing 정리: 분석 클라이언트 없음 — 되살리지 않음")
        return out
    for call_id, member_id in found:
        try:
            if not await svc.run_db(sf, lambda db, c=call_id: claim(db, c)):
                continue
            out["claimed"] += 1
            prep = await svc.run_db(sf, lambda db, c=call_id, m=member_id: svc.prepare_reanalysis(db, c, m))
            if not prep or not prep.get("eligible"):
                continue
            trigger_reanalysis(settings, client, sf, prep["locale"], call_id=call_id,
                               call_type=prep["call_type"], member_id=member_id)
            out["restarted"] += 1
        except Exception as exc:  # noqa: BLE001 - 한 통화 실패로 나머지를 멈추지 않는다(기록만)
            logger.warning("analyzing 정리: call_id=%s 되살리기 실패: %r", call_id, exc)
    logger.info("analyzing 정리 끝: 가져감=%d 재분석 시작=%d", out["claimed"], out["restarted"])
    return out


async def sweep_later(app_state, settings) -> None:
    """기동 직후는 피해서(분석 클라이언트·DB 풀 준비) 지연 뒤 한 번 돈다."""
    try:
        await asyncio.sleep(float(getattr(settings, "ANALYZING_SWEEP_DELAY_S", 60)))
        await sweep(app_state, settings)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 정리 실패로 서버를 죽이지 않는다
        logger.warning("analyzing 정리 실패(무시): %r", exc)
