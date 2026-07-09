"""개발용 시드 데이터. 실제 .env 의 DB(Supabase)에 더미/마스터 데이터를 넣는다.

실행: python scripts/seed.py [--prune]
멱등(idempotent) — 이미 있으면 갱신하거나 건너뛴다.
--prune: 소스 JSON 에 없는 stale learning_item 행 삭제(기본은 경고 리포트만).

시드 대상:
    1) voice        : Gemini Live 프리빌트 보이스 30종 (마스터)
    2) level        : 한국어 13단계 레벨 프로파일 (assets/level/level_profiles_13.json)
    3) character    : 캐릭터 4종 (role/personality/rules + voice 매핑)
    4) learning_item: 커리큘럼 마스터 — 문법 459 + 어휘 10,636 + 생존 청크 46
                      (assets/level/curriculum_v2/{grammar,vocab,survival_chunks}.json)
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select

import db.registry  # noqa: F401  (전 모델 등록 → 관계 해석)
from core.config import settings
from db.engine import build_engine
from db.session import build_session_factory
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "level"
_CURRICULUM = _ASSETS / "curriculum_v2"

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
    """한국어 13단계 레벨 프로파일 upsert (assets/level/level_profiles_13.json).

    level_no 자연키 upsert — Rev1(12→13 shift) 적용 후 재실행하면
    기존 2~13 행은 갱신, 1(생존 회화)은 신규 삽입된다.
    """
    src = _ASSETS / "level_profiles_13.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    for e in data["levels"]:
        row = db.scalar(select(Level).where(Level.level_no == e["level_no"]))
        # D15: grammar_count/vocab_count/grammar_scope/vocab_sample 폐지 —
        # 레벨별 문법·어휘의 단일 소스는 learning_item(잉여 캐시 제거).
        fields = dict(
            band=e.get("band"),
            grade=e.get("grade"),
            stage_name=e.get("stage_name"),
            textbook=e.get("textbook"),
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
    """캐릭터는 시드하지 않는다 — 운영 데이터(BABA·BIBI)를 DB에서 직접 관리.

    (2026-07-09 사장님 확정) 정식 캐릭터는 BABA(1, 기본 무료)·BIBI(2) 두 개뿐이며,
    role/personality/rules 원문이 DB에 있다. 과거 이 함수가 비비·주디·레오·미나
    4종을 만들던 것은 잘못된 시드였다(중복 행 사고 이력 — dev DB에서 5~8 삭제 완료).
    시드는 존재 확인만 하고, 빈 DB(신규 환경)면 수동 등록이 필요함을 경고한다.
    """
    count = db.scalar(select(func.count()).select_from(Character))
    if count == 0:
        print("경고: character 0행 — BABA·BIBI를 수동 등록해야 통화가 가능합니다"
              "(캐릭터는 시드 대상 아님).")
        return
    names = db.scalars(select(Character.name).order_by(Character.character_id)).all()
    print(f"characters 확인 ({count}종: {', '.join(names)}) — 시드 대상 아님(수동 관리)")


def _json_or_none(v) -> str | None:
    """JSON 컬럼 컨벤션(TEXT 에 JSON 문자열) — None 은 그대로 NULL."""
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def _grammar_fields(e: dict) -> dict:
    """curriculum_v2/grammar.json 항목 → learning_item 컬럼 매핑(필드명 그대로)."""
    return dict(
        kind="grammar",
        source_key=e["source_key"],
        band=e["band"],
        topik_grade=None,
        textbook_code=e["textbook_code"],
        seq_no=e.get("seq_no"),
        level_no=e["level_no"],
        assign_rule=e["assign_rule"],
        surface=e["surface"],
        homograph_refs=None,
        pos_primary=None,
        pos_list=None,
        pos_raw=None,
        guide_phrase=None,
        is_verb_priority=False,
        is_core=e.get("is_core", False),
        priority_rank=None,
        unit=e.get("unit"),
        unit_title=e.get("unit_title"),
        grammar_type=e.get("grammar_type"),
        examples=_json_or_none(e.get("examples")),
        explanation=e.get("explanation"),
        caution=e.get("caution"),
        meanings=None,
        gen_examples=None,
        gen_version=None,
    )


def _vocab_fields(e: dict) -> dict:
    """curriculum_v2/vocab.json 항목 → learning_item 컬럼 매핑.

    priority_score 는 컬럼이 없어 버린다(순위는 priority_rank 로 보존).
    example(CEFR 문장 1개)은 grammar 와 같은 JSON 배열 형태로 examples 에 싣는다.
    meanings/gen_examples 는 후속 vocab_gen 산출물 — 현재 소스에 없으면 NULL.
    """
    return dict(
        kind="vocab",
        source_key=e["source_key"],
        band=e["band"],
        topik_grade=e["topik_grade"],
        textbook_code=None,
        seq_no=e.get("seq_no"),
        level_no=e["level_no"],
        assign_rule=e["assign_rule"],
        surface=e["surface"],
        homograph_refs=e.get("homograph_refs"),
        pos_primary=e.get("pos_primary"),
        pos_list=_json_or_none(e.get("pos_list")),
        pos_raw=e.get("pos_raw"),
        guide_phrase=e.get("guide_phrase"),
        is_verb_priority=e.get("is_verb_priority", False),
        is_core=e.get("is_core", False),
        priority_rank=e.get("priority_rank"),
        unit=None,
        unit_title=None,
        grammar_type=None,
        examples=_json_or_none([e["example"]] if e.get("example") else None),
        explanation=None,
        caution=None,
        meanings=_json_or_none(e.get("meanings")),
        gen_examples=_json_or_none(e.get("gen_examples")),
        gen_version=e.get("gen_version"),
    )


def _chunk_fields(e: dict) -> dict:
    """curriculum_v2/survival_chunks.json 항목 → learning_item 컬럼 매핑.

    L1 생존 청크(정형 표현 통째 학습). band=1(생존도 초급 밴드),
    교재/TOPIK 좌표 없음(NULL). 전 청크 게이트 대상(is_core=True).
    meanings 에 "roman"(RR 로마자, 소스에 이미 존재)을 함께 싣는다 —
    P2.5 teaching_plan 카드의 roman 줄 재료(normalcall_service._study_roman).
    """
    meanings = {"en": e["meaning_en"]}
    if e.get("roman"):
        meanings["roman"] = e["roman"]
    return dict(
        kind="chunk",
        source_key=f"c:{e['no']:02d}:{e['ko']}",
        band=1,
        topik_grade=None,
        textbook_code=None,
        seq_no=e["no"],
        level_no=1,
        assign_rule="survival_v1",
        surface=e["ko"],
        homograph_refs=None,
        pos_primary=None,
        pos_list=None,
        pos_raw=None,
        guide_phrase=e.get("situation"),
        is_verb_priority=False,
        is_core=True,
        priority_rank=e["no"],
        unit=None,
        unit_title=None,
        grammar_type=None,
        examples=None,
        explanation=None,
        caution=None,
        meanings=_json_or_none(meanings),
        gen_examples=None,
        gen_version=None,
    )


def seed_learning_items(db) -> None:
    """커리큘럼 마스터(learning_item) upsert — source_key 자연키.

    문법 459 + 어휘 10,636 + 생존 청크 46 = 11,141행.
    기존 시드 패턴(SELECT-then-update)을 따르되 규모가 커서 source_key 전건을
    한 번에 선로딩해 dict 조인하고, 1000행 단위 flush. commit 은 main() 에서 1회.
    level FK(level_no) 때문에 seed_levels 이후에 실행해야 한다.
    """
    grammar = json.loads((_CURRICULUM / "grammar.json").read_text(encoding="utf-8"))["items"]
    vocab = json.loads((_CURRICULUM / "vocab.json").read_text(encoding="utf-8"))["items"]
    chunks = json.loads((_CURRICULUM / "survival_chunks.json").read_text(encoding="utf-8"))["items"]

    rows = (
        [_grammar_fields(e) for e in grammar]
        + [_vocab_fields(e) for e in vocab]
        + [_chunk_fields(e) for e in chunks]
    )

    existing = {r.source_key: r for r in db.scalars(select(LearningItem))}
    inserted = updated = 0
    for i, fields in enumerate(rows, start=1):
        row = existing.get(fields["source_key"])
        if row:
            for k, v in fields.items():
                setattr(row, k, v)
            updated += 1
        else:
            db.add(LearningItem(**fields))
            inserted += 1
        if i % 1000 == 0:
            db.flush()
    db.flush()
    print(
        f"learning_items 시드 확인 (문법 {len(grammar)} + 어휘 {len(vocab)}"
        f" + 청크 {len(chunks)} = {len(rows)}건 / 신규 {inserted}, 갱신 {updated})"
    )

    # ── stale 행 리포트: DB 에는 있으나 이번 소스(JSON 3종)에 없는 source_key ──
    # 자동 삭제는 하지 않는다(리네이밍/재배분 사고 가시화가 목적).
    # `python scripts/seed.py --prune` 일 때만 삭제 — 현재 learning_item 을 참조하는
    # progress 류 FK 가 없어 삭제가 안전하다(참조 테이블이 생기면 이 전제 재검토).
    source_keys = {f["source_key"] for f in rows}
    stale = sorted(k for k in existing if k not in source_keys)
    if stale:
        print(f"[경고] 소스에 없는 stale learning_item {len(stale)}건 (최대 20개 표시):")
        for k in stale[:20]:
            print(f"    - {k}")
        if "--prune" in sys.argv:
            for k in stale:
                db.delete(existing[k])
            db.flush()
            print(f"[정보] --prune: stale {len(stale)}건 삭제")
        else:
            print("        삭제하려면: python scripts/seed.py --prune")


def main() -> None:
    engine = build_engine(settings)
    engine.echo = False  # 시드는 1.1만 행 — SQL 에코가 소음·저속 (앱 엔진 설정은 무변경)
    session_factory = build_session_factory(engine)
    db = session_factory()
    try:
        seed_voices(db)   # 캐릭터가 voice 를 참조하므로 먼저
        seed_levels(db)
        seed_characters(db)
        seed_learning_items(db)  # level FK 참조 → seed_levels 뒤
        db.commit()
        print("시드 완료 ✅")
    finally:
        db.close()


if __name__ == "__main__":
    main()
