"""CallRepository — 통화 조회/추가/삭제."""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload, selectinload

from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.models.sentence import Sentence


class CallRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_basic(self, call_id: int) -> Optional[Call]:
        """소유 검증·rating 수정용(연관 미로딩)."""
        return self.db.get(Call, call_id)

    def get_detail(self, call_id: int) -> Optional[Call]:
        """상세용 — 발화(컬렉션)=selectin, 그 안 평가(스칼라)=joined, 캐릭터=joined."""
        return self.db.get(
            Call,
            call_id,
            options=[
                joinedload(Call.character),
                selectinload(Call.sentences).joinedload(Sentence.evaluation),
            ],
        )

    def get_with_raw(self, call_id: int) -> Optional[Call]:
        return self.db.get(Call, call_id, options=[selectinload(Call.raw_data)])

    def list_by_member(
        self, member_id: int, limit: int = 20, offset: int = 0
    ) -> Sequence[Call]:
        stmt = (
            select(Call)
            .where(Call.member_id == member_id)
            .options(joinedload(Call.character))  # 목록엔 캐릭터만(발화 미포함)
            .order_by(Call.call_date.desc(), Call.call_id.desc())
            .limit(limit)
            .offset(offset)
        )
        return self.db.scalars(stmt).all()

    def has_call_in_window(
        self, member_id: int, start_utc, end_utc, call_type: str | None = None
    ) -> bool:
        """[start_utc, end_utc) 안에 **성립한 통화**가 있는지(EXISTS).

        성립 = status in(done, analyzing) AND **학습자가 최소 한 번 말했다**
        (call_raw_data 에 role='user' 이고 전사가 빈 값이 아닌 행이 존재).

        왜 '유저가 말했는가'인가: 옛 기준은 total_time >= 10초 였는데 자의적이었다.
        실측(prod)에서 normal 통화 405건 중 205건이 **학습자 발화 0건**이고, 그중 44건은
        10초를 넘겨 하루를 소모했다(최장 324초 — 비버 혼자 5분을 떠든 통화). 마이크가 안
        열렸거나 듣기만 한 통화가 한도를 깎으면 안 된다.

        선톡(비버가 먼저 거는 첫 발화)은 role='beaver' 라 자동으로 제외된다.

        ⚠ 성립하지 않은 통화도 **행은 남긴다**(삭제하지 않는다). Live 세션을 연 비용은
        이미 나갔으므로 그 증거가 있어야 요금을 설명할 수 있고, 버그 조사 재료이기도 하다.

        call_type: 주면 그 콜타입만 센다(일일 한도용 — level_test 와 normal 은 서로의
            한도를 깎지 않는다). None 이면 전 콜타입.
        """
        spoke = (
            select(CallRawData.call_raw_data_id)
            .where(
                CallRawData.call_id == Call.call_id,
                CallRawData.role == "user",
                CallRawData.content.isnot(None),
                CallRawData.content != "",
            )
            .exists()
        )
        inner = select(Call.call_id).where(
            Call.member_id == member_id,
            Call.call_date >= start_utc,
            Call.call_date < end_utc,
            Call.status.in_(("done", "analyzing")),
            spoke,
        )
        if call_type is not None:
            inner = inner.where(Call.call_type == call_type)
        return bool(self.db.scalar(select(inner.exists())))

    def sum_total_time_in_window(
        self, member_id: int, start_utc, end_utc, *, exclude_call_types: tuple[str, ...] = (),
    ) -> int:
        """[start_utc, end_utc) 안에 **시작한** 통화의 `total_time` 합(초) — 하루 통화 총량
        예산(C4, 2026-09-23) 집계용.

        ⚠ `has_call_in_window` 와 달리 "학습자가 말했나"(spoke)를 걸지 않는다 — 예산은
          **써버린 시간**을 재는 것이라, 마이크가 안 열린 통화도 Gemini 세션이 열려 있던
          시간만큼 total_time 이 쌓였다면 그 소비가 실제다(옛 count 한도의 "성립" 기준과는
          목적이 다르다).
        status 는 (done, analyzing, ongoing) 만 센다 — 아직 저장 안 끝난 ongoing 도 진행
          중인 소비라 빼면, 끊고 바로 또 거는 구멍이 생긴다.
        """
        stmt = select(func.coalesce(func.sum(Call.total_time), 0)).where(
            Call.member_id == member_id,
            Call.call_date >= start_utc,
            Call.call_date < end_utc,
            Call.status.in_(("done", "analyzing", "ongoing")),
        )
        if exclude_call_types:
            stmt = stmt.where(Call.call_type.notin_(exclude_call_types))
        return int(self.db.scalar(stmt) or 0)

    def add(self, call: Call) -> Call:
        self.db.add(call)
        return call

    def delete(self, call: Call) -> None:
        self.db.delete(call)
