"""숙제 분석 저장. 외부 HTTP는 이 트랜잭션이 끝난 뒤 호출한다."""
from __future__ import annotations

import copy
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from core.homework_evidence import verify_homework_detections
from core.homework_lifecycle import analysis_cas_matches
from core.homework_lifecycle import ttl_finality_ready
from core import b2b_client
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData

logger = logging.getLogger(__name__)


def _db_deadline(db, deadline: float | None):
    if deadline is None:
        return
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("homework_recovery_deadline")
    if db.get_bind().dialect.name == "postgresql":
        milliseconds = str(max(1, int(remaining * 1000)))
        db.execute(text("SELECT set_config('lock_timeout', :value, true), "
                        "set_config('statement_timeout', :value, true)"), {"value": milliseconds})


def locked_scope(db, call_id: int, expected: tuple[int, int, int]):
    call = db.scalar(select(Call).where(Call.call_id == call_id)
                     .with_for_update().execution_options(populate_existing=True))
    if call is None or call.call_type != "homework":
        return None
    namespace = (call.usage_json or {}).get("homework")
    if not isinstance(namespace, dict) or "runtime" not in namespace:
        return None
    rows = db.scalars(select(CallRawData).where(CallRawData.call_id == call_id)).all()
    if any(type(row.turn_index) is not int or row.turn_index < 0 for row in rows):
        return None
    through = max((row.turn_index for row in rows), default=-1)
    active = call.fragment_started_at is not None and call.fragment_ended_at is None
    return call if analysis_cas_matches(namespace, expected, call.fragment_count or 1, active, through) else None


def set_analysis_state(db, call_id: int, expected: tuple[int, int, int], state: str,
                       *, preserve_result: bool = False) -> bool:
    call = locked_scope(db, call_id, expected)
    if call is None:
        db.rollback()
        return False
    homework = copy.deepcopy(call.usage_json["homework"])
    if preserve_result and state == "failed" and homework["analysis"].get("state") == "ready":
        db.rollback()
        return False
    homework["analysis"]["state"] = state
    if state in {"done", "failed"} and not preserve_result:
        call.status = state
    call.usage_json = {**call.usage_json, "homework": homework}
    db.commit()
    return True


def prepare_analysis(db, call_id: int, generation: int, fragment_count: int,
                     max_fragments: int) -> tuple[int, int, int] | None:
    """마지막 전사 commit 후 범위를 확정한다. 오래된 조각은 준비하지 않는다."""
    if type(max_fragments) is not int or max_fragments <= 0:
        raise ValueError("homework_not_final")
    call = db.scalar(select(Call).where(Call.call_id == call_id)
                     .with_for_update().execution_options(populate_existing=True))
    if call is None or call.call_type != "homework":
        db.rollback()
        return None
    namespace = copy.deepcopy((call.usage_json or {}).get("homework"))
    runtime = namespace["runtime"]
    if (runtime["generation"] != generation or runtime["fragment_count"] != fragment_count
            or call.fragment_count != fragment_count
            or call.fragment_ended_at is None):
        db.rollback()
        return None
    rows = db.scalars(select(CallRawData).where(CallRawData.call_id == call_id)).all()
    if any(type(row.turn_index) is not int or row.turn_index < 0 for row in rows):
        db.rollback()
        raise ValueError("homework_proof_invalid")
    through = max((row.turn_index for row in rows), default=-1)
    runtime.update(active=False, through_turn_index=through)
    namespace["analysis"].update(state="running")
    namespace["finality"] = ({"state": "final", "reason": "budget_exhausted",
                              "max_fragments": max_fragments}
                             if fragment_count >= max_fragments
                             else {"state": "awaiting_resume"})
    call.usage_json = {**(call.usage_json or {}), "homework": namespace}
    db.commit()
    return generation, fragment_count, through


def commit_verified_analysis(db, call_id: int, expected: tuple[int, int, int],
                             detections: list, hints: set[int] | None = None) -> bool:
    """최신 Call 잠금→영속 전사 범위 확인→검증 proof/ready 원자 저장."""
    call = db.scalar(select(Call).where(Call.call_id == call_id)
                     .with_for_update().execution_options(populate_existing=True))
    if call is None or call.call_type != "homework":
        db.rollback()
        return False
    namespace = copy.deepcopy((call.usage_json or {}).get("homework"))
    if not isinstance(namespace, dict) or "runtime" not in namespace:
        db.rollback()
        return False
    rows = db.scalars(select(CallRawData).where(CallRawData.call_id == call_id)
                      .order_by(CallRawData.turn_index, CallRawData.call_raw_data_id)).all()
    dialog = [{"role": row.role, "content": row.content, "turn_index": row.turn_index}
              for row in rows]
    if any(type(row.turn_index) is not int or row.turn_index < 0
           for row in rows):
        db.rollback()
        raise ValueError("homework_proof_invalid")
    through = max((row.turn_index for row in rows if type(row.turn_index) is int), default=-1)
    active = call.fragment_started_at is not None and call.fragment_ended_at is None
    if not analysis_cas_matches(namespace, expected, call.fragment_count or 1, active, through):
        db.rollback()
        return False
    proof = verify_homework_detections(namespace, detections, dialog, through, hints)
    previous = namespace.get("analysis", {}).get("proof", [])
    seen = {(item["reference"], item["id"], item["norm_hash"]) for item in previous}
    proof = previous + [item for item in proof
                        if (item["reference"], item["id"], item["norm_hash"]) not in seen]
    namespace["analysis"] = {"state": "ready", "verification_version": f"homework-v{namespace.get('version', 1)}",
                             "generation": expected[0], "fragment_count": expected[1],
                             "through_turn_index": through, "proof": proof}
    namespace["delivery"].update(state="pending", generation=expected[0])
    call.usage_json = {**(call.usage_json or {}), "homework": namespace}
    db.commit()
    return True


def deliver_homework(db, call_id: int, *, now: datetime | None = None,
                     deadline: float | None = None) -> bool:
    """영속 lease를 commit한 뒤 POST한다. 응답 유실은 같은 call로 복구한다."""
    now = now or datetime.now(timezone.utc)
    _db_deadline(db, deadline)
    call = db.scalar(select(Call).where(Call.call_id == call_id)
                     .with_for_update().execution_options(populate_existing=True))
    if call is None or call.call_type != "homework":
        db.rollback()
        return False
    homework = copy.deepcopy((call.usage_json or {}).get("homework"))
    runtime, analysis = homework["runtime"], homework["analysis"]
    delivery = homework["delivery"]
    generation = runtime["generation"]
    active = call.fragment_started_at is not None and call.fragment_ended_at is None
    if (active or runtime["active"] or call.status != "done" or analysis.get("state") != "ready"
            or analysis.get("generation") != generation
            or delivery.get("state") in {"acknowledged", "rejected"}):
        db.rollback()
        return False
    finality = homework["finality"]
    budget_final = (finality.get("state") == "final" and finality.get("reason") == "budget_exhausted"
                    and type(finality.get("max_fragments")) is int
                    and 0 < finality["max_fragments"] <= call.fragment_count)
    if not budget_final and not ttl_finality_ready(homework, fragment_ended_at=call.fragment_ended_at,
                                                 actual_active=active, now=now):
        db.rollback()
        return False
    for key in ("lease_until", "next_attempt_at"):
        if delivery.get(key) and datetime.fromisoformat(delivery[key]) > now:
            db.rollback()
            return False
    delivery.update(state="pending", generation=generation,
                    attempts=delivery.get("attempts", 0) + 1,
                    lease_until=(now + timedelta(seconds=30)).isoformat())
    attempt = delivery["attempts"]
    member_id, assignment_id = call.member_id, homework["assignment_id"]
    call.usage_json = {**(call.usage_json or {}), "homework": homework}
    db.commit()  # 외부 B2B는 같은 Call을 잠그므로 HTTP 전 반드시 해제한다.
    response, error = None, None
    try:
        response = b2b_client.conversation_result(member_id, assignment_id, call_id,
            **({"deadline": deadline} if deadline is not None else {}))
    except b2b_client.HomeworkMaterialsError as caught:
        error = caught
    _db_deadline(db, deadline)
    call = db.scalar(select(Call).where(Call.call_id == call_id)
                     .with_for_update().execution_options(populate_existing=True))
    latest = copy.deepcopy((call.usage_json or {}).get("homework"))
    if (latest["runtime"]["generation"] != generation
            or latest["delivery"].get("attempts") != attempt):
        db.rollback()
        return False
    delivery = latest["delivery"]
    delivery.pop("lease_until", None)
    if error is None:
        delivery.update(state="acknowledged", outcome=response["outcome"],
                        performed=response["performed"], last_error_code=None)
        delivery.pop("next_attempt_at", None)
    else:
        blocked = error.code in {"homework_analysis_failed", "homework_kind_blocked", "Not Found"}
        delivery.update(state="retryable" if error.recoverable or blocked else "rejected",
                        last_error_code=error.code)
        if error.recoverable or blocked:
            backoff = min(300, 30 * (2 ** min(delivery["attempts"] - 1, 4)))
            delivery["next_attempt_at"] = (now + timedelta(seconds=backoff)).isoformat()
    call.usage_json = {**(call.usage_json or {}), "homework": latest}
    db.commit()
    return error is None


def recover_pending(db, *, member_id: int | None = None, limit: int = 2,
                    budget_s: float = 6.0) -> int:
    """기동·실요청에서 영속 pending만 재전송한다. 무요청 주기 실행 없음."""
    deadline = time.monotonic() + budget_s
    try:
        return _recover_pending(db, member_id=member_id, limit=limit, deadline=deadline)
    except Exception:
        try:
            db.rollback()
        except Exception:
            logger.warning("숙제 결과 복구 연결 정리 실패")
        logger.warning("숙제 결과 복구 실패 — 원래 조회 결과 유지")
        return 0


def _recover_pending(db, *, member_id, limit, deadline):
    now = datetime.now(timezone.utc)
    homework = Call.usage_json["homework"]
    query = select(Call.call_id).where(
        Call.call_type == "homework", Call.status == "done",
        Call.fragment_ended_at.is_not(None),
        homework["analysis"]["state"].as_string() == "ready",
        homework["delivery"]["state"].as_string().in_(("pending", "retryable")),
    )
    if member_id is not None:
        query = query.where(Call.member_id == member_id)
    # 먼저 DB에서 미도래 lease/backoff를 제외해 앞선 행이 뒤의 복구를 막지 않는다.
    from sqlalchemy import or_
    for key in ("lease_until", "next_attempt_at"):
        value = homework["delivery"][key].as_string()
        query = query.where(or_(value.is_(None), value <= now.isoformat()))
    try:
        _db_deadline(db, deadline)
        ids = list(db.scalars(query.order_by(Call.call_id).limit(limit)))
        db.rollback()  # 후보 조회 트랜잭션을 외부 HTTP 이전에 끝낸다.
    except Exception:
        db.rollback()
        logger.warning("숙제 결과 복구 후보 조회 실패")
        return 0
    recovered = 0
    for call_id in ids:
        if time.monotonic() >= deadline:
            break
        try:
            recovered += int(deliver_homework(db, call_id, deadline=deadline))
        except Exception:
            db.rollback()
            # 외부 예외 문자열은 URL·인증 정보를 포함할 수 있어 출력하지 않는다.
            logger.warning("숙제 결과 복구 실패 call_id=%s", call_id)
    return recovered


def resume_available(call, *, member_id: int, max_fragments: int,
                     locale: str, now: datetime | None = None) -> bool:
    """REST 표시에도 불변 snapshot·진행 조각·TTL·실제 숙제 자격을 적용한다."""
    from core.homework_contract import namespace_materials
    now = now or datetime.now(timezone.utc)
    if call.call_type != "homework" or call.member_id != member_id:
        return False
    namespace = (call.usage_json or {}).get("homework")
    try:
        namespace_materials(namespace)
        runtime = namespace["runtime"]
        if (runtime["active"] or runtime["fragment_count"] != call.fragment_count
                or call.fragment_ended_at is None or call.fragment_count >= max_fragments
                or namespace["finality"].get("state") == "final"):
            return False
        ended = call.fragment_ended_at
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=timezone.utc)
        if (now - ended).total_seconds() > 300:
            return False
        b2b_client.conversation_materials(member_id, assignment_id=namespace["assignment_id"], locale=locale)
    except (KeyError, TypeError, ValueError, b2b_client.HomeworkMaterialsError):
        return False
    return True


async def analyze_homework(call_id, client, settings, factory, *, locale, member_id,
                           generation=None, fragment_count=None, max_fragments=None,
                           hints=None, since_turn_index=None):
    """실 WS 저장 후 하나의 기존 분석 호출로 숙제 검출까지 처리한다."""
    from domains.learning.service import normalcall_service as svc, call_service
    from core.homework_evidence import HomeworkKindBlocked, verification_candidates

    def prepare(db):
        call = db.get(Call, call_id)
        if call is None or call.call_type != "homework":
            return None
        current = call.usage_json["homework"]["runtime"]
        g = generation if generation is not None else current["generation"]
        count = fragment_count if fragment_count is not None else call.fragment_count
        cap = max_fragments if max_fragments is not None else call_service.call_fragments_for_member(db, member_id)
        expected = prepare_analysis(db, call_id, g, count, cap)
        if expected is None:
            return None
        return expected, copy.deepcopy(db.get(Call, call_id).usage_json["homework"])

    job = await svc.run_db(factory, prepare)
    if job is None:
        return
    expected, namespace = job
    try:
        candidates, _ = verification_candidates(namespace)
    except HomeworkKindBlocked:
        candidates = []
    except (KeyError, ValueError):
        await svc.run_db(factory, lambda db: set_analysis_state(db, call_id, expected, "failed"))
        return
    await svc.analyze_call(call_id, client, settings, factory, locale=locale,
                           target_language="ko", member_id=member_id,
                           candidates=candidates, hinted_from_turn_index=hints,
                           since_turn_index=since_turn_index, homework_expected=expected)
