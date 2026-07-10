"""[dev] 통화 1건 덤프 — 전사·복습 문장·체크판 증거·회원 레벨을 한눈에.

통화 품질/판정을 확인할 때 반복해서 쓰는 조회를 스크립트로 고정(스크래치패드 재작성 방지).
한글 콘솔 깨짐 방지: 결과를 UTF-8 파일로 쓰고(--out), 그 파일을 Read 로 확인하는 걸 권장.

사용법:
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_inspect_call.py <call_id> [--out PATH]
    (call_id 생략 시 최신 통화)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from core.config import settings  # noqa: E402

_HANGUL = re.compile(r"[가-힣]")
_LATIN = re.compile(r"[A-Za-z]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("call_id", nargs="?", type=int, default=None)
    ap.add_argument("--out", default=None, help="결과를 UTF-8 파일로 저장(콘솔 깨짐 회피)")
    args = ap.parse_args()

    engine = create_engine(settings.direct_url, poolclass=None)
    lines: list[str] = []
    with engine.connect() as c:
        cid = args.call_id or c.execute(text("SELECT max(call_id) FROM call")).scalar()
        if cid is None:
            print("통화 없음")
            return
        call = c.execute(text(
            "SELECT member_id, call_type, status, assessed_level, mode, summary, "
            "total_time FROM call WHERE call_id=:c"), {"c": cid}).first()
        if call is None:
            print(f"call {cid} 없음")
            return
        lvl = c.execute(text("SELECT korean_level FROM member WHERE member_id=:m"),
                        {"m": call[0]}).scalar()
        lines.append(f"=== call {cid} ===")
        lines.append(f"member={call[0]} korean_level={lvl} | type={call[1]} status={call[2]} "
                     f"assessed={call[3]} mode={call[4]} total_time={call[6]}s")
        lines.append(f"summary: {call[5]}")

        rows = c.execute(text(
            "SELECT role, turn_index, content FROM call_raw_data "
            "WHERE call_id=:c ORDER BY turn_index, call_raw_data_id"), {"c": cid}).all()
        b_ko = b_en = u_char = 0
        lines.append(f"\n--- 전사 ({len(rows)}턴) ---")
        for role, ti, content in rows:
            if not content:
                continue
            who = "🦫비버" if role == "beaver" else "👤유저"
            lines.append(f"[{who} t{ti}] {content}")
            if role == "beaver":
                b_ko += len(_HANGUL.findall(content)); b_en += len(_LATIN.findall(content))
            else:
                u_char += sum(1 for ch in content if ch.isalnum())
        tot = b_ko + b_en
        if tot:
            lines.append(f"\n비버 발화: 한국어 {b_ko}({b_ko * 100 // tot}%) / 영문 {b_en} · "
                         f"유저 유효글자 {u_char}자")

        sents = c.execute(text(
            "SELECT korean_sentence, native_sentence, source_type, voice_url IS NOT NULL "
            "FROM sentence WHERE call_id=:c"), {"c": cid}).all()
        lines.append(f"\n--- 복습 문장 {len(sents)}개 ---")
        for s in sents:
            lines.append(f"  {s[0]} | {s[1]} | {s[2]} | tts:{s[3]}")

        ev = c.execute(text(
            "SELECT li.kind, li.surface, ie.grade_raw, ie.grade_final, ie.verified "
            "FROM item_evidence ie JOIN learning_item li ON li.item_id=ie.item_id "
            "WHERE ie.call_id=:c ORDER BY ie.evidence_id"), {"c": cid}).all()
        lines.append(f"\n--- 체크판 증거 {len(ev)}건 ---")
        for e in ev:
            arrow = "" if e[2] == e[3] else f" (raw {e[2]})"
            lines.append(f"  [{e[0]}] {e[1]}: {e[3]}{arrow} verified={e[4]}")

    text_out = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text_out, encoding="utf-8")
        print(f"저장: {args.out} ({len(lines)}줄)")
    else:
        print(text_out)


if __name__ == "__main__":
    main()
