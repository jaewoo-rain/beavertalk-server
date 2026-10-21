"""기억 오프라인 회귀 묶음(command center 2026-10-21) — 재생·채점 공용.

입력(tests/fixtures/memory_bundle/):
  bundle.json       call flow 채점 기준(계정별 call 순서 · checkpoints · expect · forbid_current)
  transcripts.json  통화별 실제 전사(«학습자:»/«비버:» 줄)
  start.json        계정별 시작 상태(facts · summary)
  extract/<cid>.json  실제 추출기(Vertex) 원 응답(MemoryOut) — 그 시점 기존 사실을 넣고 한 번 뽑아 고정
재생: 원 응답 → summarize_for_memory 의 실제 후처리(근거 검사·등급·목록 합치기) → _merge_facts → 요약(최악 재압축 = 옛+새 그대로 붙임 · 실제 scrub)
채점: expect(정규식이 facts 어딘가에) · forbid_current(그 주제 줄/요약 문장에 옛 값) · ask_not_fact · no_basis(2251 엄마 직업)
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from domains.learning.service import chat_memory_service as cms
from domains.learning.service import normalcall_service as svc

BASE = Path(__file__).parent / "fixtures" / "memory_bundle"

# 채점 주제 → 그 주제를 말하는 줄을 찾는 낱말(주제 키 이름이 통화마다 다르게 붙는다)
TOPIC_WORDS = {
    "여행": ("여행",), "햄스터": ("햄스터", "반려"), "못 먹는 것": ("못 먹",), "생일": ("생일",),
    "주말 취미": ("주말", "취미"), "주말 운동": ("주말", "취미"), "사는 곳": ("사는 곳", "사는곳"),
    "강아지": ("강아지", "반려"), "차": ("차", "자동차"), "아침 운동": ("아침", "운동"), "저녁 운동": ("저녁", "운동"),
    "선배": ("선배",), "가수": ("가수",), "아빠 직업": ("아빠",), "반려동물": ("반려",),
}


def load(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def chain_key(transcript: str, old_facts: list[str]) -> str:
    """체인 재생 키 — 전사 + 그 순간(지금 코드가 만든) 기존 사실. 병합 코드가 바뀌면 그 지점부터 키가 달라져 다시 뽑는다."""
    import hashlib
    blob = transcript + "\n--old--\n" + json.dumps(list(old_facts), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def extract_path(cid, transcript: str | None = None, old_facts: list[str] | None = None) -> Path:
    if transcript is None:
        return BASE / "extract" / f"{cid}.json"
    return BASE / "extract" / f"{cid}_{chain_key(transcript, old_facts or [])}.json"


def _slots_from_raw(raw: dict, transcript: str, old_facts: list[str]) -> dict:
    """원 응답을 summarize_for_memory 의 실제 후처리에 그대로 통과시킨다(LLM 호출만 고정 응답)."""
    async def fake(client, model, **kw):
        return svc.MemoryOut(**raw)
    orig = svc.gemini_analysis.generate_structured
    svc.gemini_analysis.generate_structured = fake
    try:
        out = asyncio.run(svc.summarize_for_memory(object(), "m", transcript, old_facts))
    finally:
        svc.gemini_analysis.generate_structured = orig
    return out or {}


def _summary_parts(s: dict) -> str:
    parts = []
    if s.get("topic"):
        parts.append(f"화제: {s['topic']}")
    if s.get("learner_facts"):
        parts.append("사실: " + ", ".join(s["learner_facts"]))
    if s.get("pending"):
        parts.append(f"하던 것: {s['pending']}")
    return ". ".join(parts)


def replay_account(acct: str, bundle: dict, start: dict, transcripts: dict):
    """계정 하나를 call 순서대로 재생 → {cid: (facts, summary)} (추출 캐시 없는 통화는 건너뜀 표시)."""
    facts = list(start.get("facts") or [])
    summary = start.get("summary") or ""
    states = {}
    broken = False
    for cid in bundle["accounts"][acct]["calls"]:
        p = extract_path(cid, transcripts[str(cid)], facts)
        if broken or not p.exists():
            states[cid] = None          # 체인이 끊기면 그 뒤는 전부 «캐시 없음»(입력이 달라졌다)
            broken = True
            continue
        raw = json.loads(p.read_text(encoding="utf-8"))
        s = _slots_from_raw(raw, transcripts[str(cid)], facts)
        if not s:
            states[cid] = (list(facts), summary)
            continue
        replaced = list(s.get("replaced") or [])
        merged = cms._merge_facts(facts, s.get("learner_facts"), cms.FACTS_CAP, replaced, s.get("profile"))
        prof = cms.profile_of(merged)
        stale = cms.stale_values(facts, merged, replaced)
        old_s = cms.scrub_summary(summary, prof, stale, merged)
        new_s = cms.scrub_summary(_summary_parts(s), prof)
        sep = " " if (not old_s or old_s.rstrip().endswith((".", "!", "?"))) else ". "
        joined = (old_s + sep + new_s).strip()[: cms.SUMMARY_CHAR_CAP]       # 최악의 재압축 — 옛 문장을 하나도 안 지움(문장 경계는 지킴)
        summary = cms.scrub_summary(joined, prof, stale, merged)
        facts = merged
        states[cid] = (list(facts), summary)
    return states


def _topic_lines(facts, topic):
    words = TOPIC_WORDS.get(topic, tuple(topic.split()))
    out = []
    for f in facts:
        if cms.EPHEMERAL_SEP in f:
            k = f.split(cms.EPHEMERAL_SEP, 1)[0]
        else:
            k = cms.split_fact(f)[0] or f
        if any(w in (k or "") for w in words):
            out.append(f)
    return out


# call flow 77 §3-1 — 지난 일·취소 줄과 과거형 요약 문장은 현재 값이 아니다(단 바뀐 옛 계획을 현재·미래형으로 적은 문장은 실패)
_PAST_LINE = ("취소", "미뤘", "미룸", "다녀왔", "좋았", "갔었", "했었", "그만뒀", "그만둠", "지난", "기억")
_FUTURE = ("계획", "예정", "갈 것", "할 것", "가려고", "싶어 한다", "싶어함", "싶어 함", "하고 싶", "가고 싶")
_PAST_END = re.compile(r"(었|았|였|했|됐|웠|졌)(다|음|고|으며)[.!?]?$")


def _is_current_sentence(x: str) -> bool:
    x = x.strip()
    if x.startswith("화제:"):
        return False          # 이번 통화에서 나눈 화제 이름 — 현재 값 주장이 아니다
    if any(w in x for w in _FUTURE):
        return True
    return not _PAST_END.search(x)


def score_checkpoint(cp: dict, facts: list[str], summary: str) -> list[str]:
    """실패 이유 목록(빈 목록 = 통과)."""
    bad = []
    alltxt = " ".join(facts)
    for topic, rx in (cp.get("expect") or {}).items():
        if not re.search(rx, alltxt):
            bad.append(f"expect {topic}={rx} 없음")
    sents = [x for x in re.split(r"(?<=[.!?])\s+", summary or "") if x.strip()]
    for topic, rx in (cp.get("forbid_current") or {}).items():
        lines = _topic_lines(facts, topic)
        hit = [f for f in lines if re.search(rx, f) and not any(w in f for w in _PAST_LINE)]
        exp = (cp.get("expect") or {}).get(topic)
        if exp:   # 같은 줄에 지금 값(expect)이 함께 있으면 옛 값 목록 누적(allow)일 수 있다 — allow 가 있을 때만 봐준다
            hit = [f for f in hit if not (topic in (cp.get("allow") or {}) and re.search(exp, f))]
        if hit:
            bad.append(f"forbid {topic}={rx} facts {hit}")
        words = TOPIC_WORDS.get(topic, tuple(topic.split()))
        sh = [x for x in sents if any(w in x for w in words) and re.search(rx, x) and _is_current_sentence(x)]
        if sh:
            bad.append(f"forbid {topic}={rx} summary {sh[:1]}")
    asks = [f for f in facts if any(m in f for m in ("기억나", "뭐라고 했", "뭐였"))]
    if asks:
        bad.append(f"ask_not_fact {asks}")
    return bad


EPISODE_LINES_MAX = 3


def score_episode(cp: dict, facts: list[str], summary: str, learner: str = "") -> list[str]:
    """U44 ② 일화 체크포인트(call flow 77 §3-2) — 창 안이면 일화가 facts·요약 어딘가에 · 일화 줄 3줄 이하 · 비버 말 저장 금지."""
    bad = []
    alltxt = " ".join(facts) + " " + (summary or "")
    if cp.get("expect_episode") and not re.search(cp["expect_episode"], alltxt):
        bad.append(f"expect_episode {cp['expect_episode']} 없음")
    if cp.get("expect_detail_any") and not re.search(cp["expect_detail_any"], alltxt):
        bad.append(f"expect_detail {cp['expect_detail_any']} 없음")
    eps = [f for f in facts if cms.EPHEMERAL_SEP in f]
    if len(eps) > EPISODE_LINES_MAX:
        bad.append(f"일화 줄 {len(eps)} > {EPISODE_LINES_MAX}: {eps}")
    # «학습자 발화 확인 전에는 비버 말로 봄» — 학습자 줄에 있으면(2296 t? «…결혼해 주세요.»라고 말했어요) 근거 있음
    if "결혼해 주세요" in (cp.get("forbid") or "") and "결혼해 주세요" in alltxt and "결혼해 주세요" not in learner:
        bad.append("비버 말(결혼해 주세요)이 기억에")
    asks = [f for f in facts if any(m in f for m in ("기억나", "뭐라고 했", "뭐였", "있었다고 했"))]
    if asks:
        bad.append(f"ask_not_fact {asks}")
    return bad


def _start_for(acct: str, bundle: dict, start: dict, transcripts: dict, done: dict) -> dict:
    """일화 계정(«max05(비비 · 일화)»)은 같은 계정 앞 묶음(«max05(비비)») 마지막 상태에서 이어간다 · 없으면 빈 계정."""
    if acct in start:
        return start[acct]
    base = acct.replace(" · 일화", "")
    if base != acct and base in bundle["accounts"]:
        st = done.get(base)
        if st is None:
            st = replay_account(base, bundle, start.get(base, {}), transcripts)
        last = st.get(bundle["accounts"][base]["calls"][-1])
        if last is not None:
            return {"facts": last[0], "summary": last[1]}
    return {}


def run_all():
    bundle, start, transcripts = load("bundle.json"), load("start.json"), load("transcripts.json")
    results = []
    done = {}
    for acct, spec in bundle["accounts"].items():
        states = replay_account(acct, bundle, _start_for(acct, bundle, start, transcripts, done), transcripts)
        done[acct] = states
        for cp in spec["checkpoints"]:
            if cp.get("info_only"):
                continue                     # 창 밖 · 정보(실패 아님)
            st = states.get(cp["after"])
            if st is None:
                results.append((acct, cp["round"], cp["after"], ["추출 캐시 없음"]))
                continue
            if "expect_episode" in cp:
                learner = "\n".join(ln for c in spec["calls"][: spec["calls"].index(cp["after"]) + 1]
                                    for ln in transcripts[str(c)].split("\n") if ln.startswith("학습자:"))
                results.append((acct, cp["round"], cp["after"], score_episode(cp, *st, learner)))
            else:
                results.append((acct, cp["round"], cp["after"], score_checkpoint(cp, *st)))
        if acct.startswith("max10") and states.get(2251):
            f2251 = states[2251][0]
            if any(re.search("치과|엄마 직업", f) for f in f2251):
                results.append((acct, "9차 no_basis", 2251, ["2251 에 엄마 직업 값(근거 없음)"]))
    return results
