"""NationalityRepository — 순수 DB 접근(SELECT 전용).

국적 예측 이력 적재·SpeakCountry 재계산에 필요한 조회만 담당한다. commit·flush·write 는
하지 않는다(트랜잭션 경계·쓰기·삭제는 nationality_service 가 관리 — R3).
세션은 외부(service)에서 주입받는다.
"""

from __future__ import annotations

from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from domains.account.models.member import Member
from domains.account.models.nationality_prediction import NationalityPrediction


class NationalityRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def get_by_call_id(self, call_id: int) -> Optional[NationalityPrediction]:
        """멱등 upsert 용 — 같은 통화의 기존 예측 이력(있으면 update 대상, 없으면 insert)."""
        stmt = select(NationalityPrediction).where(
            NationalityPrediction.call_id == call_id
        )
        return self.db.scalar(stmt)

    def list_for_member(self, member_id: int) -> Sequence[NationalityPrediction]:
        """회원의 예측 이력을 최신순(created_at DESC, prediction_id DESC)으로 전부.

        FIFO 5-cap 로 행 수는 최대 5~6개(신규 삽입 직후)라 전량 로드해도 저렴하다.
        같은 초에 몰려 created_at 이 동률이면 prediction_id DESC(=최신 삽입)로 결정적 정렬.
        service 가 앞 5개를 재계산에, 초과분을 FIFO 삭제에 쓴다.
        """
        stmt = (
            select(NationalityPrediction)
            .where(NationalityPrediction.member_id == member_id)
            .order_by(
                NationalityPrediction.created_at.desc(),
                NationalityPrediction.prediction_id.desc(),
            )
        )
        return self.db.scalars(stmt).all()

    def get_member(self, member_id: int) -> Optional[Member]:
        """재계산 대상 회원 + 억양(speak_country, M:1)을 함께 로드."""
        return self.db.get(
            Member, member_id, options=[joinedload(Member.speak_country)]
        )
