"""nationality_service — 국적 예측 이력 적재 + SpeakCountry(억양) 재계산.

매 통화의 user 음성으로 외부 국적 API 를 호출한 결과(predictions 원응답 dict)를 받아:
  1. NationalityPrediction 이력 upsert — call_id UNIQUE 라 재분석 재실행에 **멱등**
     (select-then-write: 기존 call_id 행 있으면 update, 없으면 insert — 크로스DB 안전).
  2. FIFO 5-cap — 회원별 최근 RECENT_CAP 개만 남기고 초과분 삭제(앱 레벨).
  3. 최근 이력 나라별 prob 평균 — **분모=보유 이력 개수(len, 최대 RECENT_CAP)**. 있는 이력
     안에서 특정 나라가 빠진 회차만 0으로 셈한다("없는 회차 0"). 이력 5개 미만이어도 있는
     만큼만 나눠 초기 프로필 %가 희석되지 않는다(통화 1건이면 top1 확률 그대로). country
     **이름**(iso 아님) 기준. 랭킹/top1 은 분모와 무관하게 동일.
  4. SpeakCountry upsert — top3 국가명·percent(round(avg*100)). 없으면 신규 insert 후
     member.speak_country_id 링크, 있으면 그 행 UPDATE(링크 유지). top3 미달 슬롯은 None.
  5. 단일 commit(R3 — service 가 트랜잭션 경계·명시적 커밋).

방어: predictions 가 비었거나 형식 이상이면 아무것도 안 하고 return(graceful, R5).
통화당 1건(단일 writer) 전제라 member 행 기준 재계산이 자연 원자적 — 락 불필요.

T5(call_session._trigger_nationality)가 아래 시그니처로 호출한다(고정).
"""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from domains.account.models.nationality_prediction import NationalityPrediction
from domains.account.models.speak_country import SpeakCountry
from domains.account.repository.nationality_repository import NationalityRepository

# 재계산 창(=FIFO cap). 최근 이 개수만 유지한다. 평균 분모는 이 값이 아니라 **실제 보유
# 이력 개수(len, ≤RECENT_CAP)** — 초기(이력<5) 프로필 % 희석 방지. 창 크기만 바꾸려면 이 상수.
RECENT_CAP = 5


def record_and_recompute(
    db: Session, member_id: int, call_id: int, predictions: dict
) -> None:
    """예측 이력 적재 + 최근5 평균으로 SpeakCountry 재계산(단일 트랜잭션)."""
    preds_list = _extract_predictions(predictions)
    if preds_list is None:
        return  # 비었거나 형식 이상 → no-op(graceful)

    repo = NationalityRepository(db)
    member = repo.get_member(member_id)
    if member is None:
        return  # 회원 부재 → no-op(FK 무결성 보호)

    top1 = _resolve_top1(predictions, preds_list)

    # 1. 이력 upsert (call_id UNIQUE 멱등)
    existing = repo.get_by_call_id(call_id) if call_id is not None else None
    if existing is not None:
        existing.predictions = preds_list
        existing.top1 = top1
    else:
        db.add(
            NationalityPrediction(
                member_id=member_id,
                call_id=call_id,
                predictions=preds_list,
                top1=top1,
            )
        )
    db.flush()  # prediction_id·created_at 확정 → 정확한 최신순 정렬 보장

    # 2. FIFO 5-cap: 최신순 전량 로드 → 초과분 삭제
    rows = list(repo.list_for_member(member_id))
    recent = rows[:RECENT_CAP]
    for stale in rows[RECENT_CAP:]:
        db.delete(stale)

    # 3. 최근 이력 나라별 prob 평균(분모=보유 개수 len, 최대 RECENT_CAP) → 내림차순 top3
    top3 = _average_top3(recent)

    # 4. SpeakCountry upsert + 링크
    sc = member.speak_country
    if sc is None:
        sc = SpeakCountry()
        db.add(sc)
        member.speak_country = sc  # relationship 할당 → flush 시 speak_country_id 세팅
    _apply_top3(sc, top3)

    # 5. 명시적 커밋(R3)
    db.commit()


# --------------------------------------------------------------------------- #
# 순수 계산 헬퍼 (DB 무관 — 단위 테스트 용이)
# --------------------------------------------------------------------------- #
def _extract_predictions(predictions: Any) -> Optional[list[dict[str, Any]]]:
    """원응답 dict 에서 예측 리스트를 꺼낸다. 형식 이상/빈 리스트면 None."""
    if not isinstance(predictions, dict):
        return None
    preds = predictions.get("predictions")
    if not isinstance(preds, list) or not preds:
        return None
    return preds


def _resolve_top1(
    predictions: dict, preds_list: list[dict[str, Any]]
) -> Optional[str]:
    """행 캐시용 top1 **국가명**. 원응답 top1 이 문자열이면 그대로, 아니면 최고 prob 국가명."""
    raw = predictions.get("top1")
    if isinstance(raw, str) and raw:
        return raw
    best_name: Optional[str] = None
    best_prob = float("-inf")
    for e in preds_list:
        name, prob = _entry(e)
        if name is not None and prob > best_prob:
            best_prob, best_name = prob, name
    return best_name


def _average_top3(
    rows: list[NationalityPrediction],
) -> list[tuple[str, float]]:
    """최근 행들의 나라별 prob 평균 top3.

    country 이름(iso 아님) 기준으로 묶고, 분모는 **보유 이력 개수(len(rows), 최대 RECENT_CAP)**.
    이력이 RECENT_CAP 미만이어도 있는 만큼만 평균낸다 — 통화 1건이면 그 top1 확률이 그대로
    반영된다(고정 RECENT_CAP 로 나누면 초기 프로필 %가 부당하게 희석됨). 있는 이력 안에서
    특정 나라가 빠진 회차만 0으로 셈한다("없는 회차 0"). 랭킹/top1 은 분모와 무관하게 동일.
    같은 회차에 같은 나라가 중복되면 그 회차에선 최댓값 하나만 인정한다.
    동점은 국가명 오름차순으로 결정(결정적).
    """
    totals: dict[str, float] = {}
    for row in rows:
        per_row: dict[str, float] = {}
        preds = row.predictions if isinstance(row.predictions, list) else []
        for e in preds:
            name, prob = _entry(e)
            if name is None:
                continue
            if prob > per_row.get(name, float("-inf")):
                per_row[name] = prob
        for name, prob in per_row.items():
            totals[name] = totals.get(name, 0.0) + prob

    averaged = [(name, total / (len(rows) or 1)) for name, total in totals.items()]
    averaged.sort(key=lambda x: (-x[1], x[0]))
    return averaged[:3]


def _apply_top3(sc: SpeakCountry, top3: list[tuple[str, float]]) -> None:
    """top3 → SpeakCountry 슬롯. 미달 슬롯은 None. percent = round(avg*100), [0,100] 클램프.

    prob 은 외부 API 계약상 0~1 소수(실측). 방어적으로 percent 를 0~100 으로 클램프해
    스키마가 0~1 을 벗어난 prob(예: 이미 0~100 스케일)을 받아도 이상값(8500 등)을 막는다.
    """
    slots = (
        ("first_country", "first_percent"),
        ("second_country", "second_percent"),
        ("third_country", "third_percent"),
    )
    for i, (country_attr, pct_attr) in enumerate(slots):
        if i < len(top3):
            name, avg = top3[i]
            setattr(sc, country_attr, name)
            setattr(sc, pct_attr, max(0, min(100, round(avg * 100))))
        else:
            setattr(sc, country_attr, None)
            setattr(sc, pct_attr, None)


def _entry(e: Any) -> tuple[Optional[str], float]:
    """예측 엔트리 → (country_name, prob). 형식 이상이면 (None, 0.0)."""
    if not isinstance(e, dict):
        return None, 0.0
    name = e.get("country")
    if not isinstance(name, str) or not name:
        return None, 0.0
    prob = e.get("prob")
    if isinstance(prob, bool) or not isinstance(prob, (int, float)):
        return None, 0.0
    return name, float(prob)
