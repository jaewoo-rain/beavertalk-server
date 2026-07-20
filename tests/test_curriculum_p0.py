"""P0(13레벨 재편 + learning_item 시드) 회귀 방지 테스트.

P0-개정: 어휘 배분·예문의 최종 정답은 CEFR 12단계 통합본(CEFR_문장_통합.xlsx) —
어휘 레벨은 cefr_v1(문법단계+1), example 은 타깃어휘 문장 인라인,
문법 인벤토리는 CEFR 동기화(구표기 4 제거 + 신표기 4 추가).

검증 대상:
    A. assets/level/curriculum_v2/*.json 산출물 불변식 — xlsx/openpyxl 의존 없이
       파이프라인 최종 산출물만 검증(빠름). 건수·source_key 유일성·레벨 배정 규칙(cefr_v1)·
       core 선정 규칙(어휘 쿼터 + 문법 상한 45)·오버라이드 스팟 +
       level_profiles_13.json 텍스트 자산 보존·파생 필드 부재(D15).
    B. scripts/seed.py 의 seed_levels + seed_learning_items 멱등·정합
       (sqlite 인메모리 — test_normalcall_ws.py 의 Integer PK 치환 패턴).
    C. normalcall_service.load_call_setup 의 korean_level 미설정 폴백(→ level_no 2).

외부(실 DB/네트워크) 의존 0. 대용량 JSON 은 module 스코프 fixture 로 1회만 로드.
실행: PYTHONIOENCODING=utf-8 python -m pytest tests/test_curriculum_p0.py -q
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# --- 전 모델 등록(관계 해석) + 시드 대상 모델 ---
from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level

import domains.learning.service.normalcall_service as svc
import scripts.seed as seed

_ASSETS_LEVEL = Path(__file__).resolve().parent.parent / "assets" / "level"
_CURRICULUM = _ASSETS_LEVEL / "curriculum_v2"
_PROFILES_JSON = _ASSETS_LEVEL / "level_profiles_13.json"

# ── 기대 불변식 (docs/20260709_1231_level-system-master-plan.md 기준) ──
GRAMMAR_COUNT = 459
VOCAB_COUNT = 10_636
CHUNK_COUNT = 46
JAMO_COUNT = 40
TOTAL_ITEMS = GRAMMAR_COUNT + VOCAB_COUNT + CHUNK_COUNT  # 11,141

# 교재 코드 → 레벨 (초급 A~D=2~5, 중급 A~D=6~9, 고급 A~D=10~13)
TEXTBOOK_TO_LEVEL = {
    "BKA": 2, "BKB": 3, "BKC": 4, "BKD": 5,
    "IKA": 6, "IKB": 7, "IKC": 8, "IKD": 9,
    "AKA": 10, "AKB": 11, "AKC": 12, "AKD": 13,
}
# TOPIK 등급 → 레벨쌍 (등급당 2레벨 분할 — cefr_v1 에서도 CEFR 단계쌍=TOPIK급이라 유지)
TOPIK_TO_LEVELS = {1: {2, 3}, 2: {4, 5}, 3: {6, 7}, 4: {8, 9}, 5: {10, 11}, 6: {12, 13}}
VERB_PRIORITY_TOTAL = 1_468
# cefr_v1 레벨별 어휘 수 = CEFR 단계 분포 (A1→L2 … C4→L13, 여벌 문장 3건 제외 실측)
CEFR_LEVEL_DIST = {2: 461, 3: 274, 4: 573, 5: 527, 6: 1162, 7: 494,
                   8: 1239, 9: 961, 10: 1269, 11: 1096, 12: 1291, 13: 1289}
# 문법 표기 CEFR 동기화 4쌍 — 공백 제거 정규화 기준 (신표기 존재·구표기 부재)
CEFR_GRAMMAR_ADDED = {"A/V-(으)면(으)ㄹ수록", "N(이)라기보다는", "N(이)라면서(요)?", "V-(으)ㄴ대로"}
CEFR_GRAMMAR_REMOVED = {"N길이", "V-ㄴ다고(요)/는다고(요)", "다시말해서V-(으)려는지", "어디나"}
# core 어휘 레벨별 정원: 2~5 각 100 / 6~9 각 110 / 10~13 각 120
CORE_QUOTA = {**{lv: 100 for lv in (2, 3, 4, 5)},
              **{lv: 110 for lv in (6, 7, 8, 9)},
              **{lv: 120 for lv in (10, 11, 12, 13)}}
# 문법 core 상한(grammar_core_cap_v1): 레벨별 단원 순 상위 45개만 게이트 대상
GRAMMAR_CORE_CAP = 45


# --------------------------------------------------------------------------- #
# 대용량 JSON — module 스코프 1회 로드
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def grammar_items() -> list[dict]:
    return json.loads((_CURRICULUM / "grammar.json").read_text(encoding="utf-8"))["items"]


@pytest.fixture(scope="module")
def vocab_items() -> list[dict]:
    return json.loads((_CURRICULUM / "vocab.json").read_text(encoding="utf-8"))["items"]


@pytest.fixture(scope="module")
def chunk_items() -> list[dict]:
    return json.loads((_CURRICULUM / "survival_chunks.json").read_text(encoding="utf-8"))["items"]


@pytest.fixture(scope="module")
def jamo_items() -> list[dict]:
    return json.loads((_CURRICULUM / "jamo.json").read_text(encoding="utf-8"))["items"]


@pytest.fixture(scope="module")
def level_profiles() -> list[dict]:
    return json.loads(_PROFILES_JSON.read_text(encoding="utf-8"))["levels"]


# --------------------------------------------------------------------------- #
# A. curriculum_v2 JSON 불변식
# --------------------------------------------------------------------------- #
def test_grammar_json_invariants(grammar_items):
    """문법 459건: source_key 유일·`g:` 접두, level_no 전건 2~13."""
    assert len(grammar_items) == GRAMMAR_COUNT
    keys = [e["source_key"] for e in grammar_items]
    assert len(set(keys)) == GRAMMAR_COUNT, "source_key 중복 존재"
    assert all(k.startswith("g:") for k in keys), "g: 접두 위반 항목 존재"
    bad_levels = {e["level_no"] for e in grammar_items} - set(range(2, 14))
    assert not bad_levels, f"허용 밖 level_no: {bad_levels}"


def test_grammar_textbook_level_mapping_spot(grammar_items):
    """교재→레벨 매핑 스팟: BKA 전건 2, AKD 전건 13 (+ 전 교재코드가 기대 매핑 안)."""
    tb2lv: dict[str, set[int]] = defaultdict(set)
    for e in grammar_items:
        tb2lv[e["textbook_code"]].add(e["level_no"])
    assert tb2lv["BKA"] == {2}
    assert tb2lv["AKD"] == {13}
    # 나머지 교재도 단일 레벨 + 기대 매핑
    for code, levels in tb2lv.items():
        assert levels == {TEXTBOOK_TO_LEVEL[code]}, f"{code} → {levels}"


def test_grammar_core_cap_per_level(grammar_items):
    """문법 is_core 레벨별 합 = min(레벨 문법 수, 45) — grammar_core_cap_v1.

    초과 레벨(CEFR 동기화 후 L6=68/L10=51)은 45 로 절단, 나머지는 전건 core
    (L11 은 47→45 로 줄어 정확히 상한).
    """
    total = Counter(e["level_no"] for e in grammar_items)
    core = Counter(e["level_no"] for e in grammar_items if e.get("is_core"))
    for lv, n in sorted(total.items()):
        assert core[lv] == min(n, GRAMMAR_CORE_CAP), (
            f"L{lv}: core {core[lv]} != min({n}, {GRAMMAR_CORE_CAP})"
        )
    # 상한 절단 스팟: L6 은 45 초과 문법(68) → core 45 + 선택 문법 23
    assert total[6] > GRAMMAR_CORE_CAP and core[6] == GRAMMAR_CORE_CAP
    # 버전 기록: 문법 assign_rule 에 core cap 규칙 병기
    assert all("grammar_core_cap_v1" in e["assign_rule"] for e in grammar_items)


def test_grammar_cefr_surface_sync(grammar_items):
    """문법 표기 CEFR 동기화: 신표기 4건 존재·구표기 4건 부재 (공백 제거 비교).

    사장님 확정 — CEFR 12단계 통합본의 문법 인벤토리가 최종본.
    (위치 대조 결과 rename 아님: 제거 4 + 추가 4 — overrides.json grammar_cefr 참조.)
    """
    nospace = {re.sub(r"\s+", "", e["surface"]) for e in grammar_items}
    missing = CEFR_GRAMMAR_ADDED - nospace
    assert not missing, f"CEFR 신표기 누락: {missing}"
    leftover = CEFR_GRAMMAR_REMOVED & nospace
    assert not leftover, f"구표기 잔존: {leftover}"


def test_grammar_eul_lachimyeon_example_corrected(grammar_items):
    """`V-(으)ㄹ라치면` 예문 비문 교정 확인 — '낼라치면' 포함."""
    matches = [e for e in grammar_items if e["surface"] == "V-(으)ㄹ라치면"]
    assert len(matches) == 1, "V-(으)ㄹ라치면 항목이 정확히 1건이어야 함"
    examples = matches[0].get("examples") or []
    assert any("낼라치면" in ex for ex in examples), f"교정 예문 미반영: {examples}"


def test_vocab_json_invariants(vocab_items):
    """어휘 10,636건: source_key 유일, level_no 전건 2~13."""
    assert len(vocab_items) == VOCAB_COUNT
    keys = [e["source_key"] for e in vocab_items]
    assert len(set(keys)) == VOCAB_COUNT, "source_key 중복 존재"
    bad_levels = {e["level_no"] for e in vocab_items} - set(range(2, 14))
    assert not bad_levels, f"허용 밖 level_no: {bad_levels}"


def test_vocab_topik_grade_level_pairs(vocab_items):
    """topik_grade→레벨쌍 정합: 1급→{2,3} … 6급→{12,13}."""
    tg2lv: dict[int, set[int]] = defaultdict(set)
    for e in vocab_items:
        tg2lv[e["topik_grade"]].add(e["level_no"])
    assert dict(tg2lv) == TOPIK_TO_LEVELS, f"topik→레벨 매핑 불일치: {dict(tg2lv)}"


def test_vocab_cefr_level_distribution(vocab_items):
    """cefr_v1 재배분: 레벨별 어휘 수 = CEFR 단계 분포 (A1 매칭분→L2 461 … C4→L13 1,289).

    (구) 지그재그 편차 검증 대체 — 배분의 최종 정답은 CEFR 통합본.
    """
    dist = Counter(e["level_no"] for e in vocab_items if e["assign_rule"] == "cefr_v1")
    assert dict(dist) == CEFR_LEVEL_DIST, f"레벨 분포 불일치: {dict(sorted(dist.items()))}"


def test_vocab_cefr_assign_rule_ratio(vocab_items):
    """assign_rule='cefr_v1' 매칭률 ≥99.5% (미매칭 폴백은 vocab_split_v1 만 허용)."""
    rules = Counter(e["assign_rule"] for e in vocab_items)
    assert set(rules) <= {"cefr_v1", "vocab_split_v1"}, f"미지의 assign_rule: {set(rules)}"
    ratio = rules["cefr_v1"] / len(vocab_items)
    assert ratio >= 0.995, f"cefr_v1 매칭률 {ratio:.4%} < 99.5%"


def test_vocab_cefr_example_spots(vocab_items):
    """예문 인라인 스팟: 매칭 어휘의 example = 타깃어휘 기준 CEFR 문장 1건.

    v:통계00(B4→L9) '통계 자료에 의하면 …' / v:안녕02(A1→L2) '안녕, 반가워요.' /
    v:병02(원본 오기 '명02' 교정 승계, A1→L2) '이 병에 물이 있어요.'
    """
    by_key = {e["source_key"]: e for e in vocab_items}
    spots = [
        ("v:통계00", 9, "통계 자료에 의하면"),
        ("v:안녕02", 2, "반가워요"),
        ("v:병02", 2, "이 병에 물이 있어요"),
    ]
    for key, level, snippet in spots:
        item = by_key.get(key)
        assert item is not None, f"{key} 누락"
        assert item["level_no"] == level, f"{key} level_no={item['level_no']} (기대 {level})"
        assert snippet in (item.get("example") or ""), f"{key} example 불일치: {item.get('example')!r}"
    # 매칭분(cefr_v1) 전건 example 채움
    empty = [e["source_key"] for e in vocab_items
             if e["assign_rule"] == "cefr_v1" and not e.get("example")]
    assert not empty, f"example 누락 {len(empty)}건: {empty[:5]}"


def test_vocab_verb_priority_total(vocab_items):
    """동사 시트 우선 신호 총 1,468건."""
    assert sum(1 for e in vocab_items if e.get("is_verb_priority")) == VERB_PRIORITY_TOTAL


def test_vocab_core_quota_per_level(vocab_items):
    """core 레벨별 정원: 2~5 각 100 / 6~9 각 110 / 10~13 각 120 (총 1,320)."""
    core_by_level = Counter(e["level_no"] for e in vocab_items if e.get("is_core"))
    assert dict(core_by_level) == CORE_QUOTA, f"core 정원 불일치: {dict(sorted(core_by_level.items()))}"


def test_vocab_core_forbidden_pos(vocab_items):
    """core 에 금지 품사 0건: 접사·줄어든말(pos_primary), 의존명사 단독(pos_list)."""
    bad = [
        e["source_key"]
        for e in vocab_items
        if e.get("is_core") and (
            e.get("pos_primary") in ("접사", "줄어든말")
            or (e.get("pos_list") and set(e["pos_list"]) <= {"의존명사"})
        )
    ]
    assert not bad, f"core 금지 품사 위반: {bad}"


def test_vocab_override_spots(vocab_items):
    """오버라이드 스팟: 'v:병02' 존재, source_key '명02' 정확히 1건, '대전01' surface 정제."""
    by_key = {e["source_key"]: e for e in vocab_items}
    assert "v:병02" in by_key, "'v:병02' 항목 누락"
    # '명02' 자연키는 1건만 (v:명02 — 비명02/실명02 처럼 부분 문자열 매칭이 아닌 정확 키)
    exact_myeong = [e for e in vocab_items if e["source_key"] == "v:명02"]
    assert len(exact_myeong) == 1, f"'v:명02' 건수 이상: {len(exact_myeong)}"
    daejeon = by_key.get("v:대전01")
    assert daejeon is not None, "'v:대전01' 항목 누락"
    assert "명사" not in daejeon["surface"], f"surface 에 품사 표기 잔존: {daejeon['surface']}"


def test_chunks_and_jamo_counts_and_key_format(chunk_items, jamo_items):
    """생존 청크 46건·자모 40건, chunk source_key 규격 `c:{no:02d}:{ko}`."""
    assert len(chunk_items) == CHUNK_COUNT
    assert len(jamo_items) == JAMO_COUNT
    # source_key 는 seed 가 파생 — 실제 매핑 함수(_chunk_fields)로 규격 검증
    pat = re.compile(r"^c:\d{2}:.+")
    keys = [seed._chunk_fields(e)["source_key"] for e in chunk_items]
    assert all(pat.match(k) for k in keys), f"규격 위반 키: {[k for k in keys if not pat.match(k)]}"
    assert len(set(keys)) == CHUNK_COUNT, "chunk source_key 중복"
    for e, k in zip(chunk_items, keys):
        assert k == f"c:{e['no']:02d}:{e['ko']}"


def test_level_profiles_text_assets_only(level_profiles):
    """level_profiles_13 = 텍스트 자산(이름류)만 — D15 로 파생 필드 제거.

    grammar_count/vocab_count/grammar_scope/vocab_sample 부재(단일 소스는 curriculum_v2)
    + profile/stage_name 등 수기 언어학 자산 보존을 검증한다.
    """
    by_no = {e["level_no"]: e for e in level_profiles}
    assert set(by_no) == set(range(1, 14))
    removed = ("grammar_count", "vocab_count", "grammar_scope", "vocab_sample")
    for lv in range(1, 14):
        for field in removed:
            assert field not in by_no[lv], f"L{lv} 제거 필드 잔존(D15): {field}"
        assert by_no[lv].get("profile"), f"L{lv} profile 텍스트 소실"
    # stage_name 에 CEFR 라벨 병기: L2='초급 1 (A1)' … L13='고급 4 (C4)'
    cefr_stages = ["A1", "A2", "A3", "A4", "B1", "B2", "B3", "B4", "C1", "C2", "C3", "C4"]
    for lv in range(2, 14):
        assert by_no[lv]["stage_name"].endswith(f"({cefr_stages[lv - 2]})"), (
            f"L{lv} stage_name CEFR 라벨 누락: {by_no[lv]['stage_name']!r}"
        )
    # L1(생존 회화)은 현행 유지 — curriculum_v2 밖(수기 자산)
    assert "(" not in by_no[1]["stage_name"] or by_no[1]["stage_name"] == "생존 회화(Pre-Basic)"


# --------------------------------------------------------------------------- #
# B. 시드 멱등·정합 — sqlite 인메모리 (Integer PK 치환 패턴)
# --------------------------------------------------------------------------- #
def _make_session_factory():
    """test_normalcall_ws.py 와 동일: BigInteger+Identity PK 를 Integer 로 치환."""
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _snapshot(db) -> dict:
    """행수·kind 분포 스냅샷."""
    return {
        "level_rows": db.query(Level).count(),
        "item_rows": db.query(LearningItem).count(),
        "kind_counts": dict(
            Counter(k for (k,) in db.query(LearningItem.kind).all())
        ),
    }


@pytest.fixture(scope="module")
def seeded_db():
    """create_all → (seed_levels+seed_learning_items) 2회 실행 — 1·2회차 스냅샷 보존.

    시드가 대용량(11,141행)이라 module 스코프 1회만 수행하고,
    각 테스트는 스냅샷/세션으로 검증한다(읽기 전용).
    """
    session_factory = _make_session_factory()
    db = session_factory()
    # 1회차
    seed.seed_levels(db)
    seed.seed_learning_items(db)
    db.commit()
    first = _snapshot(db)
    # 2회차 (같은 세션 — 멱등 검증)
    seed.seed_levels(db)
    seed.seed_learning_items(db)
    db.commit()
    second = _snapshot(db)
    yield {"db": db, "first": first, "second": second}
    db.close()


def test_seed_first_run_counts(seeded_db):
    """1회차: level 13행(1~13, L1 grade='A0') · learning_item 11,141행(kind 별).

    (멀티랭귀지) seed 는 language='ko' 를 채운다 — 전건 ko(language 컬럼만 추가).
    """
    db, first = seeded_db["db"], seeded_db["first"]
    assert first["level_rows"] == 13
    level_nos = sorted(n for (n,) in db.query(Level.level_no).all())
    assert level_nos == list(range(1, 14))
    # 전 레벨 language='ko'
    assert {lang for (lang,) in db.query(Level.language).distinct().all()} == {"ko"}
    l1 = db.scalar(select(Level).where(Level.level_no == 1))
    assert l1.grade == "A0", f"L1 grade={l1.grade} (생존 회화는 'A0' 이어야 함)"

    assert first["item_rows"] == TOTAL_ITEMS
    assert first["kind_counts"] == {
        "grammar": GRAMMAR_COUNT, "vocab": VOCAB_COUNT, "chunk": CHUNK_COUNT,
    }
    # 전 learning_item language='ko'
    assert {lang for (lang,) in db.query(LearningItem.language).distinct().all()} == {"ko"}


def test_seed_is_idempotent(seeded_db):
    """2회차 실행 후 행수·kind 분포 불변(멱등 upsert)."""
    assert seeded_db["second"] == seeded_db["first"], (
        f"멱등 위반: 1회차={seeded_db['first']} / 2회차={seeded_db['second']}"
    )


def test_seed_fk_smoke_level_no(seeded_db):
    """FK 스모크: learning_item.level_no 전건이 level.level_no 에 존재."""
    db = seeded_db["db"]
    level_nos = {n for (n,) in db.query(Level.level_no).all()}
    item_levels = {n for (n,) in db.query(LearningItem.level_no).distinct().all()}
    orphans = item_levels - level_nos
    assert not orphans, f"level 에 없는 level_no 참조: {orphans}"


# --------------------------------------------------------------------------- #
# C. 폴백 회귀 — korean_level=None → level_no 2 프로파일
# --------------------------------------------------------------------------- #
def test_load_call_setup_fallback_level_2():
    """korean_level 미설정 member 는 레벨 2(Basic A) 프로파일로 폴백.

    레벨 1(생존 회화)은 레벨테스트 배정 전용 — 폴백이 1 로 가면 회귀.
    """
    session_factory = _make_session_factory()
    db = session_factory()
    try:
        voice = Voice(name="Fenrir", gender="male")
        db.add(voice)
        db.flush()
        ch = Character(name="비비", role="선생님", personality="다정함",
                       voice_id=voice.voice_id, price=0)
        db.add(ch)
        db.add(Level(language="ko", level_no=1, grade="A0", profile="생존 회화 프로파일"))
        db.add(Level(language="ko", level_no=2, grade="A", profile="Basic A 프로파일"))
        member = Member(language="en", korean_level=None,
                        onboarding_completed=True, auth_user_id="auth-fallback")
        db.add(member)
        db.commit()

        setup = svc.load_call_setup(db, member.member_id, ch.character_id)
        assert setup["level_profile"] == "Basic A 프로파일", (
            f"korean_level=None 폴백이 레벨 2 가 아님: {setup['level_profile']!r}"
        )
    finally:
        db.close()
