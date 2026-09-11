"""주제별 커리큘럼(488차시) 정본을 **하나의 시드**로 합친다 — DB 대공사(cur_* 테이블)의 원료.

원료(정본): 04.앱데이터_JSON/curriculum_all.json · ai_roles.json · topics.json · functions.json
       + 02.배정결과/배정결과_어휘_문법.xlsx (어휘·문법의 전체 속성 — JSON 에 없는 길잡이말·CEFR·첫등장·주의사항 등)
산출:  assets/curriculum_v3/cur_seed.json  (topics·functions·vocab·grammar·lessons)
       docs/curriculum_v3_검토.xlsx        (사람 검토용 — 여기서 고치지 말고 시드를 고친다)
불변식: 어휘 10,636 전건·1차시 1회 · 문법 462 · 차시 488 · 기능 30 · 주제 65 · 미매핑 0 (하나라도 깨지면 exit 1)

사용:  PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/curriculum/build_cur_seed.py "<06.주제별 커리큘럼 폴더>"
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
from collections import Counter, OrderedDict

import openpyxl

STAGES = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4", "C1", "C2", "C3", "C4"]
# 1 = 생존회화 청크(기존), 2=A1 … 13=C4  (사장님 결정 2026-09-12 #1·#7)
LEVEL_NO = {stage: i + 2 for i, stage in enumerate(STAGES)}


def rows(ws, width: int = 20):
    """빈 행은 버리고, 읽기 전용 모드가 꼬리 빈 칸을 잘라 오는 것을 폭 맞춰 채운다."""
    out = []
    for r in ws.iter_rows(values_only=True):
        if len(r) == 0 or not any(v not in (None, "") for v in r):
            continue
        out.append(tuple(r) + (None,) * max(0, width - len(r)))
    return out


def s(v):
    return "" if v is None else str(v).strip()


def main(src: str) -> int:
    J = os.path.join(src, "04.앱데이터_JSON")
    L = json.load(io.open(os.path.join(J, "curriculum_all.json"), encoding="utf-8"))
    R = {r["code"]: r for r in json.load(io.open(os.path.join(J, "ai_roles.json"), encoding="utf-8"))}
    T = json.load(io.open(os.path.join(J, "topics.json"), encoding="utf-8"))
    F = json.load(io.open(os.path.join(J, "functions.json"), encoding="utf-8"))
    fcode = {f["name"]: f["code"] for f in F}
    tcode = {t["name"]: t["code"] for t in T}

    wb = openpyxl.load_workbook(
        os.path.join(src, "02.배정결과", "배정결과_어휘_문법.xlsx"), read_only=True, data_only=True
    )
    av = rows(wb["어휘 주제배정"])[1:]
    ag = rows(wb["문법 기능배정"])[1:]

    vocab: "OrderedDict[str, dict]" = OrderedDict()
    for r in av:
        key = s(r[0])
        vocab[key] = {
            "key": key,
            "headword": re.sub(r"\d{2}$", "", key.split("/")[0].split("·")[0]),
            "en": s(r[1]), "pos": s(r[2]), "guide": s(r[3]), "grade": s(r[4]), "cefr6": s(r[5]),
            "stage": s(r[6]), "topic_code": s(r[7]), "topic_kind": s(r[10]),
            "first_stage": s(r[11]), "first_unit": s(r[12]),
            "freq": float(r[13]) if r[13] not in (None, "") else None,
            "examples": [s(x) for x in r[14:17] if s(x)],
            "lesson_code": None, "role": None,  # 아래서 차시가 채운다
        }
    grammar: "OrderedDict[str, dict]" = OrderedDict()
    for r in ag:
        name = s(r[2])
        grammar[name] = {
            "key": name, "stage": s(r[0]), "unit": s(r[1]), "function_code": s(r[3]), "function": s(r[4]),
            "en": s(r[5]), "desc": s(r[6]), "examples": [s(x) for x in r[7:10] if s(x)], "notes": s(r[10]),
            "textbook": s(r[11]), "textbook_unit": s(r[12]), "task_title": s(r[13]),
            "lesson_codes": [],
        }

    lessons = []
    problems: list[str] = []
    for l in L:
        code = l["code"]
        role = R.get(code)
        if role is None:
            problems.append(f"ai_roles 없음: {code}")
            role = {}
        must = set(role.get("must_use_vocab") or [])
        core_keys, must_keys, sup_keys, gram_keys = [], [], [], []
        for v in l["vocab"]:
            k = v["w"]
            if k not in vocab:
                problems.append(f"{code} 핵심어휘 미매핑: {k}")
                continue
            if vocab[k]["lesson_code"] is not None:
                problems.append(f"{code} 어휘 중복 배정: {k} (이미 {vocab[k]['lesson_code']})")
            vocab[k]["lesson_code"] = code
            vocab[k]["role"] = "must" if k in must else "core"
            (must_keys if k in must else core_keys).append(k)
        for v in l["support"]:
            k = v["w"]
            if k not in vocab:
                problems.append(f"{code} 지원어휘 미매핑: {k}")
                continue
            if vocab[k]["lesson_code"] is not None:
                problems.append(f"{code} 지원어휘 중복 배정: {k}")
            vocab[k]["lesson_code"] = code
            vocab[k]["role"] = "support"
            sup_keys.append(k)
        for g in l["grammar"]:
            k = g["g"]
            if k not in grammar:
                problems.append(f"{code} 문법 미매핑: {k}")
                continue
            grammar[k]["lesson_codes"].append(code)
            gram_keys.append(k)
        funcs = []
        for fname in l["functions"]:
            if fname not in fcode:
                problems.append(f"{code} 기능 미매핑: {fname}")
                continue
            funcs.append(fcode[fname])
        if l["topic_code"] != tcode.get(l["topic"]):
            problems.append(f"{code} 주제코드 불일치: {l['topic_code']} vs {tcode.get(l['topic'])}")
        lessons.append({
            "no": l["no"], "code": code, "stage": l["stage"], "level_no": LEVEL_NO[l["stage"]],
            "topic_code": l["topic_code"], "part": l["part"],
            "situation": l["situation"], "partner": l["partner"],
            "functions": funcs, "grammar_kind": l["grammar_kind"],
            "opening": role.get("opening"), "probes": role.get("probes") or [],
            "success": role.get("success"),              # 기록용 — 판정엔 안 쓴다(결정 #3)
            "guardrails": role.get("guardrails") or [],  # 기록용 — 캐릭터 우선(결정 #5)
            "dialogue": l["dialogue"],                   # 기록용 — 프롬프트에 안 싣는다(결정 #8)
            "grammar_keys": gram_keys, "must_keys": must_keys, "core_keys": core_keys, "support_keys": sup_keys,
            "item_count": len(gram_keys) + len(must_keys) + len(core_keys) + len(sup_keys),
        })

    # ── 불변식 ─────────────────────────────────────────────────────────
    unassigned = [k for k, v in vocab.items() if v["lesson_code"] is None]
    checks = {
        "차시 488": len(lessons) == 488,
        "차시 번호 1..488 연속": [l["no"] for l in lessons] == list(range(1, 489)),
        "차시코드 유일": len({l["code"] for l in lessons}) == 488,
        "어휘 10,636": len(vocab) == 10636,
        "어휘 전건 1차시 배정": not unassigned,
        "문법 462": len(grammar) == 462,
        "문법 전건 ≥1차시": all(g["lesson_codes"] for g in grammar.values()),
        "기능 30": len(F) == 30,
        "주제 65": len(T) == 65,
        "미매핑·중복 0": not problems,
    }
    roles = Counter(v["role"] for v in vocab.values())
    print("역할별 어휘:", dict(roles), "| 문법 배정 합계:", sum(len(g["lesson_codes"]) for g in grammar.values()))
    counts = [l["item_count"] for l in lessons]
    print("차시당 항목 평균: %.1f  최대: %d  최소: %d" % (sum(counts) / 488, max(counts), min(counts)))
    for k, ok in checks.items():
        print(("PASS " if ok else "FAIL ") + k)
    if problems:
        print("문제 %d건:" % len(problems))
        for p in problems[:30]:
            print("  " + p)
    if unassigned:
        print("미배정 어휘 예:", unassigned[:10])

    seed = {
        "meta": {"source": src, "levels": {"1": "생존회화(청크·기존)", **{str(LEVEL_NO[st]): st for st in STAGES}}},
        "topics": T, "functions": F, "lessons": lessons,
        "vocab": list(vocab.values()), "grammar": list(grammar.values()),
    }
    out_json = os.path.join("assets", "curriculum_v3", "cur_seed.json")
    io.open(out_json, "w", encoding="utf-8").write(json.dumps(seed, ensure_ascii=False, indent=1))
    print("시드:", out_json, "%.1f MB" % (os.path.getsize(out_json) / 1e6))

    # ── 검토용 엑셀 ─────────────────────────────────────────────────────
    xw = openpyxl.Workbook()
    ws = xw.active
    ws.title = "읽는 법"
    for line in [
        "이 파일은 cur_seed.json 에서 자동으로 뽑은 검토용 사본이다 — 여기서 고친 것은 반영되지 않는다. 시드(원료 JSON/배정결과)를 고치고 다시 뽑는다.",
        "차시: 1행 = 1차시(488). 진도 순서 = no. level_no 2=A1 … 13=C4 (1 = 생존회화 청크, 기존).",
        "차시별 항목: 차시가 가르치는 항목 전부(문법·필수·핵심·지원). 표현학습 재료 = 이 표 그대로(결정 #2).",
        "어휘: 10,636 전건 — 각 어휘는 정확히 한 차시에 속한다. 문법: 462 — 여러 차시에 나올 수 있다(신규 1 + 이어 연습).",
        "success/guardrails/dialogue 는 기록용(결정 #3·#5·#8) — 프롬프트·판정에 안 쓴다.",
    ]:
        ws.append([line])
    ws = xw.create_sheet("차시")
    ws.append(["no", "code", "stage", "level_no", "topic_code", "topic", "part", "situation", "partner", "functions",
               "grammar_kind", "문법수", "필수", "핵심", "지원", "항목합계", "opening", "probes"])
    tname = {t["code"]: t["name"] for t in T}
    for l in lessons:
        ws.append([l["no"], l["code"], l["stage"], l["level_no"], l["topic_code"], tname.get(l["topic_code"]), l["part"],
                   l["situation"], l["partner"], " · ".join(l["functions"]), l["grammar_kind"],
                   len(l["grammar_keys"]), len(l["must_keys"]), len(l["core_keys"]), len(l["support_keys"]), l["item_count"],
                   l["opening"], " / ".join(l["probes"])])
    ws = xw.create_sheet("차시별 항목")
    ws.append(["lesson_code", "no", "role", "key", "kind", "en", "pos", "grade", "stage(원)", "topic_code",
               "예문1", "예문2", "예문3", "설명"])
    for l in lessons:
        for k in l["grammar_keys"]:
            g = grammar[k]
            ws.append([l["code"], l["no"], "grammar", k, "grammar", g["en"], "", "", g["stage"], "",
                       *(g["examples"] + ["", "", ""])[:3], g["desc"]])
        for role_, keys in (("must", l["must_keys"]), ("core", l["core_keys"]), ("support", l["support_keys"])):
            for k in keys:
                v = vocab[k]
                ws.append([l["code"], l["no"], role_, k, "vocab", v["en"], v["pos"], v["grade"], v["stage"], v["topic_code"],
                           *(v["examples"] + ["", "", ""])[:3], v["guide"]])
    ws = xw.create_sheet("어휘")
    ws.append(["key", "headword", "en", "pos", "guide", "grade", "cefr6", "stage", "topic_code", "topic_kind",
               "first_stage", "first_unit", "freq", "lesson_code", "role", "예문1", "예문2", "예문3"])
    for v in vocab.values():
        ws.append([v["key"], v["headword"], v["en"], v["pos"], v["guide"], v["grade"], v["cefr6"], v["stage"], v["topic_code"],
                   v["topic_kind"], v["first_stage"], v["first_unit"], v["freq"], v["lesson_code"], v["role"],
                   *(v["examples"] + ["", "", ""])[:3]])
    ws = xw.create_sheet("문법")
    ws.append(["key", "stage", "unit", "function_code", "function", "en", "desc", "notes", "textbook", "textbook_unit",
               "task_title", "lesson_codes", "예문1", "예문2", "예문3"])
    for g in grammar.values():
        ws.append([g["key"], g["stage"], g["unit"], g["function_code"], g["function"], g["en"], g["desc"], g["notes"],
                   g["textbook"], g["textbook_unit"], g["task_title"], " · ".join(g["lesson_codes"]),
                   *(g["examples"] + ["", "", ""])[:3]])
    ws = xw.create_sheet("주제")
    ws.append(["code", "area", "name", "kind"])
    for t in T:
        ws.append([t["code"], t["area"], t["name"], t["kind"]])
    ws = xw.create_sheet("기능")
    ws.append(["code", "name"])
    for f in F:
        ws.append([f["code"], f["name"]])
    out_x = os.path.join("docs", "curriculum_v3_검토.xlsx")
    xw.save(out_x)
    print("검토 엑셀:", out_x)
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
