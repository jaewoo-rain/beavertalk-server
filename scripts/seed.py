"""개발용 시드 데이터. 실제 .env 의 DB(Supabase)에 더미/마스터 데이터를 넣는다.

실행: python scripts/seed.py
멱등(idempotent) — 이미 있으면 갱신하거나 건너뛴다.

시드 대상:
    1) voice   : Gemini Live 프리빌트 보이스 30종 (마스터)
    2) level   : 한국어 12단계 레벨 프로파일 (assets/level/level_profiles_12.json)
    3) character: 캐릭터 4종 (role/personality/rules + voice 매핑)
"""

import json
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
from domains.commerce.models.voice import Voice
from domains.learning.models.level import Level

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "level"

# Gemini Live 프리빌트 보이스 30종 (이름, 음색 특성). 출처: ai.google.dev speech-generation.
VOICES = [
    ("Zephyr", "밝은(Bright)"), ("Puck", "경쾌한(Upbeat)"), ("Charon", "정보전달형(Informative)"),
    ("Kore", "단단한(Firm)"), ("Fenrir", "활기찬·흥분한(Excitable)"), ("Leda", "젊은(Youthful)"),
    ("Orus", "단단한(Firm)"), ("Aoede", "산뜻한(Breezy)"), ("Callirrhoe", "느긋한(Easy-going)"),
    ("Autonoe", "밝은(Bright)"), ("Enceladus", "숨소리 섞인(Breathy)"), ("Iapetus", "맑은(Clear)"),
    ("Umbriel", "느긋한(Easy-going)"), ("Algieba", "매끄러운(Smooth)"), ("Despina", "매끄러운(Smooth)"),
    ("Erinome", "맑은(Clear)"), ("Algenib", "허스키(Gravelly)"), ("Rasalgethi", "정보전달형(Informative)"),
    ("Laomedeia", "경쾌한(Upbeat)"), ("Achernar", "부드러운(Soft)"), ("Alnilam", "단단한(Firm)"),
    ("Schedar", "고른(Even)"), ("Gacrux", "성숙한(Mature)"), ("Pulcherrima", "적극적인(Forward)"),
    ("Achird", "친근한(Friendly)"), ("Zubenelgenubi", "캐주얼한(Casual)"), ("Vindemiatrix", "온화한(Gentle)"),
    ("Sadachbia", "생기있는(Lively)"), ("Sadaltager", "박식한(Knowledgeable)"), ("Sulafat", "따뜻한(Warm)"),
]


def seed_voices(db) -> None:
    """Gemini Live 보이스 30종 upsert."""
    for name, desc in VOICES:
        exists = db.scalar(select(Voice).where(Voice.name == name))
        if exists:
            exists.description = desc
            continue
        db.add(Voice(name=name, description=desc))
    db.flush()
    print(f"voices 시드 확인 ({len(VOICES)}종)")


def seed_levels(db) -> None:
    """한국어 12단계 레벨 프로파일 upsert (assets/level/level_profiles_12.json)."""
    src = _ASSETS / "level_profiles_12.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    for e in data["levels"]:
        row = db.scalar(select(Level).where(Level.level_no == e["level_no"]))
        fields = dict(
            band=e.get("band"),
            grade=e.get("grade"),
            stage_name=e.get("stage_name"),
            textbook=e.get("textbook"),
            grammar_count=e.get("grammar_count"),
            vocab_count=e.get("vocab_count"),
            grammar_scope=json.dumps(e.get("grammar_scope", []), ensure_ascii=False),
            vocab_sample=json.dumps(e.get("vocab_sample", []), ensure_ascii=False),
            profile=e.get("profile"),
        )
        if row:
            for k, v in fields.items():
                setattr(row, k, v)
        else:
            db.add(Level(level_no=e["level_no"], **fields))
    db.flush()
    print(f"levels 시드 확인 ({len(data['levels'])}단계)")


def seed_characters(db) -> None:
    """캐릭터 4종 upsert. role/personality/rules + voice 매핑.

    비비 = 기본 무료, 욕쟁이 트래시토커(Fenrir). 나머지 voice 는 임의 배정(추후 조정).
    """
    defaults = [
        {"name": "비비", "price": Decimal("0.00"),
         "description": "기본 무료 캐릭터(거칠지만 정 있는 트래시토커)",
         "role": "거칠지만 정 있는 트래시토커 한국어 대화 파트너",
         "personality": "직설적이고 도발적인 말투. 모국어로 거친 농담·면박을 섞는다",
         "rules": "모국어 거친 표현·욕설 허용(과하지 않게, 학습자가 불쾌하지 않을 선). 한국어 표현은 표준·완결형 유지",
         "voice_name": "Fenrir"},
        {"name": "주디", "price": Decimal("4900.00"),
         "description": "차분하고 똑부러지는 파트너 주디.",
         "role": "정확한 발음을 짚어주는 선생님 같은 파트너",
         "personality": "차분하고 똑부러진 말투",
         "rules": "발음·문법 오류를 놓치지 않고 친절하게 교정한다",
         "voice_name": "Kore"},
        {"name": "레오", "price": Decimal("4900.00"),
         "description": "에너지 넘치는 파트너 레오.",
         "role": "활기차게 대화를 이끄는 친구 같은 파트너",
         "personality": "에너지 넘치고 장난스러운 말투",
         "rules": None,
         "voice_name": "Puck"},
        {"name": "미나", "price": Decimal("6900.00"),
         "description": "감성적인 파트너 미나.",
         "role": "공감하며 천천히 들어주는 파트너",
         "personality": "감성적이고 따뜻한 말투",
         "rules": None,
         "voice_name": "Leda"},
    ]
    for c in defaults:
        voice = db.scalar(select(Voice).where(Voice.name == c["voice_name"]))
        voice_id = voice.voice_id if voice else None
        exists = db.scalar(select(Character).where(Character.name == c["name"]))
        if exists:
            exists.description = c["description"]
            exists.role = c["role"]
            exists.personality = c["personality"]
            exists.rules = c["rules"]
            exists.voice_id = voice_id
            print(f"update: 캐릭터 '{c['name']}' (id={exists.character_id}, voice={c['voice_name']})")
            continue
        ch = Character(
            name=c["name"], price=c["price"], description=c["description"],
            role=c["role"], personality=c["personality"], rules=c["rules"],
            voice_id=voice_id,
        )
        db.add(ch)
        db.flush()
        print(f"added: 캐릭터 '{c['name']}' (id={ch.character_id}, voice={c['voice_name']})")


def main() -> None:
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    db = session_factory()
    try:
        seed_voices(db)   # 캐릭터가 voice 를 참조하므로 먼저
        seed_levels(db)
        seed_characters(db)
        db.commit()
        print("시드 완료 ✅")
    finally:
        db.close()


if __name__ == "__main__":
    main()
