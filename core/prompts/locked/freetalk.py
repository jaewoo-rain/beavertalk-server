"""잠금 — 프리토킹 차시판 [이번 차시] 블록의 **구조**(항목 렌더·probes 이름 치환·상대 폴백). 문장은 editable/freetalk.md 에서 받는다(2026-09-12).

힌트 사이드카(차시 소재 절)·재접지 쪽지(아직 안 쓴 소재 = 문형은 예문)가 items[{obj, ex, role}] 의 같은 렌더 규칙을 기대한다.
"""
from __future__ import annotations

import re

# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.

# probes 의 시드 고유명(«마이클 씨») → 학습자 이름. 이름 환각의 반대 방향 위험(T21-B)을 막는다. 한글·라틴 1~10자 + « 씨».
PROBE_NAME_RE = re.compile(r"[가-힣A-Za-z]{1,10} 씨")


def lesson_block(lesson: object, *, username: str, header: str, partner_line: str, partner_fallback: str,
                 material_line: str, probes_prefix: str) -> str:
    """«[이번 차시]» 블록. items(obj·ex·role) 전부 — 문형은 «이름 — "예문"», 청크는 «표현», 어휘는 headword.

    ⚠ items 가 없는 옛 브리프(surfaces 만)는 그 표면형을 «표현» 줄로 싣는다(호환). 문법 0 인 차시(레벨1 청크)는 «문형:» 줄이 없다.
    ⚠ «상대» 가 없는 차시는 partner_fallback 을 상대로 넣는다(비버가 상황 속 상대를 맡는다 — 계획 §10).
    ⛔ «나머지는 모국어로» 류 모순 문구를 되살리지 마라 — 이 코스는 처음부터 끝까지 학습 언어다(규칙 3).
    """
    situation = (getattr(lesson, "situation", None) or "").strip()
    partner = (getattr(lesson, "partner", None) or "").strip()
    items = [d for d in (getattr(lesson, "items", None) or []) if isinstance(d, dict) and (d.get("obj") or "").strip()]
    if not items:
        items = [{"obj": s, "ex": None, "role": "chunk"}
                 for s in (getattr(lesson, "surfaces", None) or []) if isinstance(s, str) and s.strip()]
    probes = [PROBE_NAME_RE.sub(f"{username} 씨", p.strip())
              for p in (getattr(lesson, "probes", None) or []) if isinstance(p, str) and p.strip()]
    grammar = [d for d in items if d.get("role") == "grammar"]
    chunks = [d for d in items if d.get("role") == "chunk"]
    vocab = [d for d in items if d.get("role") not in ("grammar", "chunk")]

    lines = [header]
    if situation:
        lines.append(f"- 상황: {situation}")
    lines.append(partner_line.format(partner=partner or partner_fallback))
    if items:
        lines.append(material_line)
        if grammar:
            lines.append("  문형: " + " / ".join(
                f"{d['obj'].strip()} — \"{str(d['ex']).strip()}\"" if (d.get("ex") or "").strip() else d["obj"].strip()
                for d in grammar
            ))
        if chunks:
            lines.append("  표현: " + " · ".join(d["obj"].strip() for d in chunks))
        if vocab:
            lines.append("  어휘: " + " · ".join(d["obj"].strip() for d in vocab))
    if probes:
        lines.append(probes_prefix + " " + " / ".join(probes))
    return "\n".join(lines)
