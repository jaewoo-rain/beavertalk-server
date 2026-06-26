"""모델 레지스트리 — 전 도메인 모델을 import 해 Base.metadata 에 등록.

Alembic env.py 와 create_all 검증이 이 모듈 하나만 import 하면
14개 테이블을 모두 인식한다. (도메인이 늘면 여기 import 한 줄만 추가)
"""

from db.base import Base

# account
from domains.account.models.member import Member  # noqa: F401
from domains.account.models.member_reason import MemberReason  # noqa: F401
from domains.account.models.speak_country import SpeakCountry  # noqa: F401

# commerce
from domains.commerce.models.character import Character  # noqa: F401
from domains.commerce.models.voice import Voice  # noqa: F401
from domains.commerce.models.member_character import MemberCharacter  # noqa: F401
from domains.commerce.models.discount_event import DiscountEvent  # noqa: F401
from domains.commerce.models.payment import Payment  # noqa: F401
from domains.commerce.models.subscribe import Subscribe  # noqa: F401

# learning
from domains.learning.models.call import Call  # noqa: F401
from domains.learning.models.call_raw_data import CallRawData  # noqa: F401
from domains.learning.models.sentence import Sentence  # noqa: F401
from domains.learning.models.evaluation import Evaluation  # noqa: F401
from domains.learning.models.review import Review  # noqa: F401
from domains.learning.models.level import Level  # noqa: F401

# alarm
from domains.alarm.models.alarm import Alarm  # noqa: F401
from domains.alarm.models.schedule import Schedule  # noqa: F401

__all__ = ["Base"]
