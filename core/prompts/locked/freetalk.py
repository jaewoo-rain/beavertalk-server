"""잠금 — 프리토킹 차시판 [이번 차시] 블록의 **구조**(항목 렌더·probes 이름 치환·상대 폴백). 문장은 editable/freetalk.md 에서 받는다(2026-09-12).

힌트 사이드카(차시 소재 절)·재접지 쪽지(아직 안 쓴 소재 = 문형은 예문)가 items[{obj, ex, role}] 의 같은 렌더 규칙을 기대한다.
"""
from __future__ import annotations

import re

# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.

# probes 의 시드 고유명(«마이클 씨») → 학습자 이름. 이름 환각의 반대 방향 위험(T21-B)을 막는다. 한글·라틴 1~10자 + « 씨».
PROBE_NAME_RE = re.compile(r"[가-힣A-Za-z]{1,10} 씨")
# ja(2026-09-13): 시드 probes 의 고유명 「マイケルさん」 → 「{username}さん」. ko 정규식은 그대로(ko 결과 무변화) — 언어별 패턴을 따로 둔다.
PROBE_NAME_RE_BY_LANGUAGE: dict[str, tuple] = {
    "ko": (PROBE_NAME_RE, "{username} 씨"),
    "ja": (re.compile(r"[ぁ-んァ-ヶ一-龠々ーA-Za-z]{1,10}さん"), "{username}さん"),
}

# 레벨1 청크의 빈칸 슬롯(«저는 ◯◯이에요»). 소재 줄에 기호째 실리면 비버가 **그대로 읽는다**
# (실통화 1606 t23 «저는 ◯◯ 회원 아니에요»). 그래서 렌더 직전에 무엇을 넣을 자리인지로 바꾼다.
# ⚠ 판정·진도는 원래 표면형을 쓴다 — 치환은 **프롬프트에 싣는 순간에만** 한다(서비스의 brief 는 안 건드린다).
SLOT_RE = re.compile(r"[◯○〇]{1,}")


def fill_slots(text: str) -> str:
    """«◯◯» 빈칸을 «(이름)»·«(나라)»·«(그 단어)» 로. 문맥이 안 잡히면 «(빈칸)»."""
    if not text or not SLOT_RE.search(text):
        return text
    if "사람이에요" in text or "에서 왔" in text or "나라" in text:
        label = "나라"
    elif "뭐예요" in text or "무슨 뜻" in text or "뜻이" in text:
        label = "그 단어"
    elif "저는" in text or "제 이름" in text:
        label = "이름"
    else:
        label = "빈칸"
    return SLOT_RE.sub(f"({label})", text)


def lesson_block(lesson: object, *, username: str, header: str, partner_line: str, partner_fallback: str,
                 material_line: str, probes_prefix: str, language: str = "ko") -> str:
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
    name_re, name_tpl = PROBE_NAME_RE_BY_LANGUAGE.get(language, PROBE_NAME_RE_BY_LANGUAGE["ko"])
    probes = [name_re.sub(name_tpl.format(username=username), p.strip())
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
                f"{fill_slots(d['obj'].strip())} — \"{fill_slots(str(d['ex']).strip())}\"" if (d.get("ex") or "").strip()
                else fill_slots(d["obj"].strip())
                for d in grammar
            ))
        if chunks:
            lines.append("  표현: " + " · ".join(fill_slots(d["obj"].strip()) for d in chunks))
        if vocab:
            lines.append("  어휘: " + " · ".join(fill_slots(d["obj"].strip()) for d in vocab))
    if probes:
        lines.append(probes_prefix + " " + " / ".join(probes))
    return "\n".join(lines)
