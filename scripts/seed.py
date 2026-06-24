"""개발용 시드 데이터. 실제 .env 의 DB(Supabase)에 더미 데이터를 넣는다.

실행: python scripts/seed.py
멱등(idempotent) — 이미 있으면 다시 안 넣는다.
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

import db.registry  # noqa: F401  (전 모델 등록 → 관계 해석)
from core.config import settings
from db.engine import build_engine
from db.session import build_session_factory
from domains.commerce.models.character import Character


def seed_characters(db) -> None:
    defaults = [
        {"name": "비비", "price": Decimal("0.00"),
         "description": "기본 무료 캐릭터", "prompt": "친근하고 다정한 한국어 대화 파트너 비비.",
         "image_url": None, "voice_url": None},
        {"name": "주디", "price": Decimal("4900.00"),
         "description": "차분하고 똑부러지는 파트너 주디.", "prompt": "정확한 발음을 짚어주는 선생님 같은 파트너 주디.",
         "image_url": None, "voice_url": None},
        {"name": "레오", "price": Decimal("4900.00"),
         "description": "에너지 넘치는 파트너 레오.", "prompt": "활기차게 대화를 이끄는 친구 같은 파트너 레오.",
         "image_url": None, "voice_url": None},
        {"name": "미나", "price": Decimal("6900.00"),
         "description": "감성적인 파트너 미나.", "prompt": "공감하며 천천히 들어주는 파트너 미나.",
         "image_url": None, "voice_url": None},
    ]
    for c in defaults:
        exists = db.scalar(select(Character).where(Character.name == c["name"]))
        if exists:
            print(f"skip: 캐릭터 '{c['name']}' 이미 존재 (id={exists.character_id})")
            continue
        ch = Character(**c)
        db.add(ch)
        db.flush()
        print(f"added: 캐릭터 '{c['name']}' (id={ch.character_id})")


def main() -> None:
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    db = session_factory()
    try:
        seed_characters(db)
        db.commit()
        print("시드 완료 ✅")
    finally:
        db.close()


if __name__ == "__main__":
    main()
