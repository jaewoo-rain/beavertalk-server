"""표시 문구 번역 캐시 조회·적재 — §6(cur_text_i18n) · §10(call_summary_translation). 순수 DB(commit 없음)."""

from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from domains.learning.models.call_summary_translation import call_summary_translation as cst
from domains.learning.models.cur_text_i18n import CurTextI18n


def get_cur_text(db: Session, kind: str, source: str, locale: str) -> Optional[str]:
    return db.scalar(
        select(CurTextI18n.text).where(
            CurTextI18n.kind == kind, CurTextI18n.source == source, CurTextI18n.locale == locale,
        )
    )


def add_cur_text(db: Session, kind: str, source: str, locale: str, text: str) -> None:
    db.add(CurTextI18n(kind=kind, source=source, locale=locale, text=text))


def get_summary_translations(db: Session, call_ids: Iterable[int], locale: str) -> dict[int, str]:
    ids = list(call_ids)
    if not ids:
        return {}
    rows = db.execute(
        select(cst.c.call_id, cst.c.text).where(cst.c.call_id.in_(ids), cst.c.locale == locale)
    ).all()
    return {r.call_id: r.text for r in rows}


def add_summary_translations(
    db: Session, rows: list[tuple[int, str, Optional[str]]], locale: str,
) -> None:
    """rows = [(call_id, 번역문, 원문 언어|None)]."""
    if rows:
        db.execute(insert(cst), [
            {"call_id": cid, "locale": locale, "text": text, "source_locale": src}
            for cid, text, src in rows
        ])
