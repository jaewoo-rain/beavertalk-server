"""일본어 커리큘럼 raw → 정규화 JSON 변환 (멀티랭귀지 T4b · 일본어 수직 관통).

실행: PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/curriculum/parse_jp.py
멱등 — 같은 입력이면 항상 같은 산출물을 덮어쓴다.

한국어(parse_xlsx.py)와 **같은 중간포맷**을 산출해 scripts/seed.py 가 그대로 적재하게 한다.
다만 원본 데이터 형태가 전혀 달라(한국어 xlsx 시트 vs 일본어 JSON+CEFR문장통합) 파서는 별도다.
한국어 cefr_v1 과 개념은 동일: **CEFR 문장통합본의 타깃어휘·문법단계가 어휘 레벨의 정답**이다.

입력 (level/05.다른 언어 CEFR/jp_cefr_rebuild/):
    JP_CEFR_문장_통합.xlsx  9,018문장 (번호/문법단계/문법단원/문법구조/문장/…/타깃어휘/타깃품사/어휘등급JEV)
                            → 어휘 레벨 배분·예문의 정답. 타깃어휘 dedup(최저 CEFR 단계 승).
    jp_grammar_12.json      문법 691건 (문법단계/문법구조/예문/문법단원/토픽/분류)
    jp_vocab_by_lv.json     표제어 17,206 (표기→읽기[가나]·한국어뜻 조인 소스)

산출 (assets/level/curriculum_v2_ja/):
    grammar.json          문법 (source_key=g:ja:{surface}, level_no=문법단계+1[A1→2..C4→13],
                          is_core=레벨별 단원 순 상위 45 — 한국어 grammar_core_cap 과 동수)
    vocab.json            어휘 (source_key=v:ja:{surface}, level_no=문법단계+1, example=CEFR 문장,
                          reading=가나, meanings={"ko":한국어뜻}, is_core=레벨별 상위 100)
추가 산출 (assets/level/level_profiles_ja.json):
    레벨 1(생존)~13(C4) 프로파일 골격. profile 본문은 T5(콘텐츠 저작)에서 채운다 — 여기선 placeholder.

레벨 구조(플랜 결정 line 99): level_no 전역 고정 — 1≡생존, 2~13≡A1~C4(한국어와 동일 축).
생존청크(level 1)·레벨 프로파일 본문·레벨테스트 앵커는 T5 저작 대상(여기 미포함).
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl

_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = _ROOT / "level" / "05.다른 언어 CEFR" / "jp_cefr_rebuild"
SENT_XLSX = _SRC / "JP_CEFR_문장_통합.xlsx"
GRAMMAR_JSON = _SRC / "jp_grammar_12.json"
VOCAB_BY_LV_JSON = _SRC / "jp_vocab_by_lv.json"

OUT_DIR = _ROOT / "assets" / "level" / "curriculum_v2_ja"
PROFILES_JSON = _ROOT / "assets" / "level" / "level_profiles_ja.json"

# CEFR 12단계 → 앱 레벨(단계+1: A1→2 … C4→13). 한국어와 동일 축(1=생존).
CEFR_STAGES: tuple[str, ...] = (
    "A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4", "C1", "C2", "C3", "C4",
)
STAGE_TO_LEVEL: dict[str, int] = {s: i + 2 for i, s in enumerate(CEFR_STAGES)}
STAGE_IDX: dict[str, int] = {s: i for i, s in enumerate(CEFR_STAGES)}

ASSIGN_RULE = "cefr_ja_v1"
GRAMMAR_CORE_CAP = 45   # 레벨별 문법 core 상한(한국어 grammar_core_cap_v1 과 동수 — 승급 게이트 크기 일치)
VOCAB_CORE_CAP = 100    # 레벨별 어휘 core 상한(대략 한국어 초급 목표치)


class ParseError(Exception):
    """원본이 규칙을 벗어남 — 즉시 중단."""


def _band(level_no: int) -> int:
    """레벨 → 밴드(2~5 초급=1 / 6~9 중급=2 / 10~13 고급=3). 한국어와 동일 경계."""
    return 1 if level_no <= 5 else (2 if level_no <= 9 else 3)


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ---------------------------------------------------------------- 읽기·한국어뜻 조인 소스

def build_reading_gloss() -> tuple[dict[str, str], dict[str, str]]:
    """jp_vocab_by_lv.json → (표기→읽기[가나], 표기→한국어뜻). 첫 등장 우선."""
    data = json.loads(VOCAB_BY_LV_JSON.read_text(encoding="utf-8"))
    reading: dict[str, str] = {}
    gloss: dict[str, str] = {}
    for items in data.values():
        for it in items:
            s = _clean(it.get("표기"))
            if not s:
                continue
            if s not in reading:
                r = _clean(it.get("읽기"))
                if r:
                    reading[s] = r
                g = _clean(it.get("한국어뜻"))
                if g:
                    gloss[s] = g
    return reading, gloss


# ---------------------------------------------------------------- 어휘 (CEFR 문장통합본)

def parse_vocab(reading: dict[str, str], gloss: dict[str, str]) -> list[dict]:
    """CEFR 문장통합본 타깃어휘 → 어휘 항목. 최저 CEFR 단계가 이기고, 그 문장을 예문으로.

    한국어 cefr_v1 과 동형: (surface) 별 최초(최저단계) 출현이 레벨·예문을 확정한다.
    """
    wb = openpyxl.load_workbook(SENT_XLSX, read_only=True)
    ws = wb[wb.sheetnames[0]]
    # surface → 확정 레코드 (최저 단계 우선; 동단계면 번호 빠른 문장)
    chosen: dict[str, dict] = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        no, stage, unit, struct, sentence = row[0], row[1], row[2], row[3], row[4]
        target, tpos, jev = row[9], row[10], row[11]
        stage = _clean(stage)
        surface = _clean(target)
        if not stage or not surface:
            continue
        if stage not in STAGE_TO_LEVEL:
            raise ParseError(f"어휘: 미지의 문법단계 {stage!r} (#{no})")
        si = STAGE_IDX[stage]
        prev = chosen.get(surface)
        if prev is not None and (si, no) >= (prev["_si"], prev["_no"]):
            continue
        chosen[surface] = {
            "surface": surface, "stage": stage, "_si": si, "_no": no,
            "sentence": _clean(sentence), "pos": _clean(tpos),
            "jev": _jev_int(jev),
        }
    wb.close()

    items: list[dict] = []
    for c in chosen.values():
        level_no = STAGE_TO_LEVEL[c["stage"]]
        surface = c["surface"]
        pos = c["pos"]
        meanings = {"ko": gloss[surface]} if surface in gloss else None
        items.append({
            "kind": "vocab",
            "source_key": f"v:ja:{surface}",
            "band": _band(level_no),
            "topik_grade": c["jev"],          # JEV 급(1~6) — 참조용(일본어엔 TOPIK 없음)
            "level_no": level_no,
            "assign_rule": ASSIGN_RULE,
            "surface": surface,
            "reading": reading.get(surface),  # 가나 (없으면 None)
            "pos_primary": pos,
            "pos_list": [pos] if pos else None,
            "pos_raw": pos,
            "is_verb_priority": False,
            "is_core": False,                 # rank_and_cap 에서 레벨별 상위 100 True
            "example": c["sentence"],
            "meanings": meanings,
            "_si": c["_si"], "_no": c["_no"],  # 정렬용(직렬화 전 제거)
        })
    _rank_and_cap(items, VOCAB_CORE_CAP)
    return items


def _jev_int(raw) -> int | None:
    """'JEV3' → 3. 결측/이상은 None."""
    s = _clean(raw)
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else None


# ---------------------------------------------------------------- 문법 (jp_grammar_12.json)

def parse_grammar() -> list[dict]:
    """jp_grammar_12.json → 문법 항목. surface 중복은 최저 단계가 승(dedup)."""
    data = json.loads(GRAMMAR_JSON.read_text(encoding="utf-8"))
    chosen: dict[str, dict] = {}
    for i, e in enumerate(data):
        stage = _clean(e.get("문법단계"))
        surface = _clean(e.get("문법구조"))
        if not stage or not surface:
            continue
        if stage not in STAGE_TO_LEVEL:
            raise ParseError(f"문법: 미지의 단계 {stage!r} (surface={surface!r})")
        si = STAGE_IDX[stage]
        seq = e.get("번호") if isinstance(e.get("번호"), int) else i
        prev = chosen.get(surface)
        if prev is not None and (si, seq) >= (prev["_si"], prev["_seq"]):
            continue
        chosen[surface] = {
            "surface": surface, "stage": stage, "_si": si, "_seq": seq,
            "unit": _clean(e.get("문법단원")), "unit_title": _clean(e.get("토픽")),
            "grammar_type": _clean(e.get("분류")),
            "example": _clean(e.get("예문")),
        }
    items: list[dict] = []
    for c in chosen.values():
        level_no = STAGE_TO_LEVEL[c["stage"]]
        items.append({
            "kind": "grammar",
            "source_key": f"g:ja:{c['surface']}",
            "band": _band(level_no),
            "textbook_code": f"JP-{c['stage']}",
            "level_no": level_no,
            "assign_rule": ASSIGN_RULE,
            "surface": c["surface"],
            "reading": None,
            "unit": c["unit"],
            "unit_title": c["unit_title"],
            "grammar_type": c["grammar_type"],
            "examples": [c["example"]] if c["example"] else None,
            "explanation": None,
            "caution": None,
            "is_core": False,   # rank_and_cap 에서 레벨별 상위 45 True
            "_si": c["_si"], "_seq": c["_seq"],
        })
    _rank_and_cap(items, GRAMMAR_CORE_CAP)
    return items


# ---------------------------------------------------------------- core 선정·순위

def _rank_and_cap(items: list[dict], cap: int) -> None:
    """레벨 내 (단계, 원본 순서)로 정렬 → priority_rank 부여 + 상위 cap 개 is_core=True."""
    by_level: dict[int, list[dict]] = defaultdict(list)
    for it in items:
        by_level[it["level_no"]].append(it)
    for level_no, group in by_level.items():
        group.sort(key=lambda x: (x["_si"], x.get("_no", x.get("_seq", 0))))
        for rank, it in enumerate(group, start=1):
            it["priority_rank"] = rank
            it["seq_no"] = rank
            it["is_core"] = rank <= cap


# ---------------------------------------------------------------- 레벨 프로파일 골격

def build_profiles() -> dict:
    """레벨 1(생존)~13(C4) 프로파일 골격. profile 본문은 T5 저작 — 여기선 placeholder."""
    levels = [{
        "level_no": 1, "band": "생존", "grade": None,
        "stage_name": "생존 회화", "textbook": None,
        "profile": "(T5 저작 예정) 일본어 생존 표현 — 인사·숫자·정형 표현 46청크.",
    }]
    for stage in CEFR_STAGES:
        level_no = STAGE_TO_LEVEL[stage]
        band = _band(level_no)
        levels.append({
            "level_no": level_no,
            "band": {1: "초급", 2: "중급", 3: "고급"}[band],
            "grade": {1: "A", 2: "B", 3: "C"}[band],
            "stage_name": f"CEFR {stage}",
            "textbook": f"JP-{stage}",
            "profile": f"(T5 저작 예정) 일본어 CEFR {stage} 레벨 발화 프로파일.",
        })
    return {
        "_comment": "일본어 레벨 프로파일 골격(T4b 생성). profile 본문·생존청크는 T5 저작 대상. "
                    "level_no 축은 한국어와 동일(1=생존, 2~13=A1~C4).",
        "language": "ja",
        "levels": levels,
    }


# ---------------------------------------------------------------- 검증·저장

def _strip_internal(items: list[dict]) -> None:
    for it in items:
        it.pop("_si", None)
        it.pop("_no", None)
        it.pop("_seq", None)


def validate(grammar: list[dict], vocab: list[dict]) -> None:
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, ok))

    gkeys = Counter(g["source_key"] for g in grammar)
    vkeys = Counter(v["source_key"] for v in vocab)
    check(f"문법 source_key 유일 ({len(grammar)}건)", all(c == 1 for c in gkeys.values()))
    check(f"어휘 source_key 유일 ({len(vocab)}건)", all(c == 1 for c in vkeys.values()))
    check("전 항목 level_no ∈ 2..13", all(2 <= x["level_no"] <= 13 for x in grammar + vocab))
    check("어휘 읽기(가나) 채움율 ≥ 95%",
          sum(1 for v in vocab if v.get("reading")) / max(1, len(vocab)) >= 0.95)
    check("어휘 예문 채움율 100%", all(v.get("example") for v in vocab))
    check("문법 예문 채움율 ≥ 95%",
          sum(1 for g in grammar if g.get("examples")) / max(1, len(grammar)) >= 0.95)

    failed = [n for n, ok in checks if not ok]
    for n, ok in checks:
        print(f"  {'OK ' if ok else 'FAIL'} {n}")
    if failed:
        raise AssertionError(f"검증 실패 {len(failed)}건: {failed}")


def _dump(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"생성: {path.relative_to(_ROOT)}")


def main() -> None:
    if not SENT_XLSX.exists():
        raise SystemExit(f"입력 없음: {SENT_XLSX} (level/05.다른 언어 CEFR/ 데이터 필요)")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("[1/4] 읽기·한국어뜻 조인 소스 로드")
    reading, gloss = build_reading_gloss()
    print(f"  표제어 {len(reading)} (읽기) / {len(gloss)} (한국어뜻)")

    print("[2/4] 어휘 파싱 (CEFR 문장통합본 타깃어휘)")
    vocab = parse_vocab(reading, gloss)
    vdist = Counter(v["level_no"] for v in vocab)
    print(f"  어휘 {len(vocab)}건 | 레벨분포:", {lv: vdist[lv] for lv in range(2, 14)})

    print("[3/4] 문법 파싱 (jp_grammar_12.json)")
    grammar = parse_grammar()
    gdist = Counter(g["level_no"] for g in grammar)
    print(f"  문법 {len(grammar)}건 | 레벨분포:", {lv: gdist[lv] for lv in range(2, 14)})

    print("[4/4] 저장 + 검증")
    _strip_internal(grammar)
    _strip_internal(vocab)
    grammar.sort(key=lambda g: (g["level_no"], g["seq_no"]))
    vocab.sort(key=lambda v: (v["level_no"], v["seq_no"]))
    meta = {"source": "level/05.다른 언어 CEFR/jp_cefr_rebuild",
            "generated_by": "scripts/curriculum/parse_jp.py", "language": "ja"}
    _dump(OUT_DIR / "grammar.json",
          {**meta, "assign_rule": ASSIGN_RULE, "count": len(grammar), "items": grammar})
    _dump(OUT_DIR / "vocab.json",
          {**meta, "assign_rule": ASSIGN_RULE, "count": len(vocab), "items": vocab})
    _dump(PROFILES_JSON, build_profiles())
    validate(grammar, vocab)
    print("검증 통과 — 일본어 curriculum_v2_ja 산출 완료")


if __name__ == "__main__":
    main()
