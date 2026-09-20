"""취약 발음 학습 마스터 시드 — sound_lesson(30) + national_sound_stat(34개국).

실행: python scripts/seed_sound_lessons.py [--dry-run]
멱등 — sound_key / (country_iso, sound_key) 로 select-then-write upsert.

소스:
    assets/pronunciation/lessons.json            30과(자음 소리 25 + 음운 규칙 5)
    assets/pronunciation/national_weak_sounds.json  34개국 × 취약 소리

두 소스는 하네스(#23 제품기획 `_output/2026-09-18_비버톡_국적별발음학습/content`)의
검사기 통과본이다. 여기서 콘텐츠를 **고치지 않는다** — 고치면 두 벌이 갈린다.
이 스크립트가 하는 일은 모양 검증 + 적재뿐이다.

★ 적재 전에 두 소스의 sound_key 를 교차 검증한다. 국가 통계가 가리키는 소리에 학습 과가
  없으면 앱에서 「학습하기」가 빈 화면으로 떨어진다 — 그건 런타임에 알면 늦다.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

import db.registry  # noqa: F401  (전 모델 등록 → 관계 해석)
from core.config import settings
from db.engine import build_engine
from db.session import build_session_factory
from domains.learning.models.national_sound_stat import NationalSoundStat
from domains.learning.models.sound_lesson import SoundLesson

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "pronunciation"
_LESSONS = _ASSETS / "lessons.json"
_NATIONAL = _ASSETS / "national_weak_sounds.json"

# payload 로 넘기는 4단계 콘텐츠 키. 나머지(sound_key·label·type 등)는 컬럼으로 뽑는다.
_PAYLOAD_KEYS = (
    "how_to", "words", "sentence", "test", "formula", "symbol", "rep_syllable",
)


def load_sources() -> tuple[list[dict], dict[str, dict]]:
    lessons = json.loads(_LESSONS.read_text(encoding="utf-8"))["lessons"]
    national = json.loads(_NATIONAL.read_text(encoding="utf-8"))
    return lessons, national


def validate(lessons: list[dict], national: dict[str, dict]) -> list[str]:
    """적재 전 모양 검증 — 오류 문자열 목록(빈 리스트면 통과)."""
    errs: list[str] = []
    keys = [l["sound_key"] for l in lessons]
    if len(keys) != len(set(keys)):
        errs.append("lessons.json 에 sound_key 중복")
    for l in lessons:
        for req in ("sound_key", "type", "label", "card_desc", "how_to", "words", "sentence", "test"):
            if not l.get(req):
                errs.append(f"{l.get('sound_key')} — 필수 필드 없음: {req}")
        if len(l.get("words") or []) != 4:
            errs.append(f"{l['sound_key']} — 단어가 4개가 아니다({len(l.get('words') or [])})")
        if l["type"] not in ("sound", "rule"):
            errs.append(f"{l['sound_key']} — type 이 sound/rule 이 아니다: {l['type']}")
    have = set(keys)
    for iso, row in national.items():
        if not row.get("country") or not row.get("weak_sounds"):
            errs.append(f"{iso} — country/weak_sounds 없음")
            continue
        for it in row["weak_sounds"]:
            if it["sound_key"] not in have:
                errs.append(f"{iso} — 학습 과가 없는 소리: {it['sound_key']}")
    return errs


def seed_lessons(session, lessons: list[dict]) -> tuple[int, int]:
    """sound_lesson upsert → (신규, 갱신)."""
    created = updated = 0
    for order, l in enumerate(lessons):
        payload = {k: l[k] for k in _PAYLOAD_KEYS if k in l}
        row = session.scalar(select(SoundLesson).where(SoundLesson.sound_key == l["sound_key"]))
        if row is None:
            session.add(SoundLesson(
                sound_key=l["sound_key"], type=l["type"], label=l["label"],
                position=l.get("position"), jamo=l.get("jamo"), diagram=l.get("diagram"),
                card_desc=l["card_desc"], sort_order=order, payload=payload,
            ))
            created += 1
            continue
        row.type = l["type"]
        row.label = l["label"]
        row.position = l.get("position")
        row.jamo = l.get("jamo")
        row.diagram = l.get("diagram")
        row.card_desc = l["card_desc"]
        row.sort_order = order
        row.payload = payload
        updated += 1
    return created, updated


def seed_national(session, national: dict[str, dict]) -> tuple[int, int, int]:
    """national_sound_stat upsert → (신규, 갱신, 삭제).

    rank 는 소스에 없다 — share 내림차순으로 이 자리에서 매긴다(동률은 sound_key 사전순).
    소스에서 빠진 (국가, 소리) 조합은 지운다. 남겨두면 목록에 유령 항목이 뜬다.
    """
    created = updated = 0
    wanted: set[tuple[str, str]] = set()
    for iso, row in national.items():
        items = sorted(row["weak_sounds"], key=lambda i: (-int(i["share"]), i["sound_key"]))
        for rank, it in enumerate(items, start=1):
            wanted.add((iso, it["sound_key"]))
            existing = session.scalar(
                select(NationalSoundStat).where(
                    NationalSoundStat.country_iso == iso,
                    NationalSoundStat.sound_key == it["sound_key"],
                )
            )
            if existing is None:
                session.add(NationalSoundStat(
                    country_iso=iso, country_name=row["country"],
                    sound_key=it["sound_key"], share=int(it["share"]), rank=rank,
                ))
                created += 1
                continue
            existing.country_name = row["country"]
            existing.share = int(it["share"])
            existing.rank = rank
            updated += 1

    deleted = 0
    for row in session.scalars(select(NationalSoundStat)).all():
        if (row.country_iso, row.sound_key) not in wanted:
            session.delete(row)
            deleted += 1
    return created, updated, deleted


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="검증만 하고 DB 를 건드리지 않는다")
    args = ap.parse_args()

    lessons, national = load_sources()
    errs = validate(lessons, national)
    if errs:
        print(f"검증 실패 {len(errs)}건:")
        for e in errs[:30]:
            print("  -", e)
        return 1
    print(f"검증 통과 — 학습 {len(lessons)}과 · 국가 {len(national)}")
    if args.dry_run:
        return 0

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    with session_factory() as session:
        lc, lu = seed_lessons(session, lessons)
        nc, nu, nd = seed_national(session, national)
        session.commit()
    print(f"sound_lesson        신규 {lc} · 갱신 {lu}")
    print(f"national_sound_stat 신규 {nc} · 갱신 {nu} · 삭제 {nd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
