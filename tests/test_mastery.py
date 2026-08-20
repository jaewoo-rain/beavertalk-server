"""P2 체크판·레벨업 회귀 테스트 — 분석 측 (외부 의존 0).

검증 대상:
    - _verify_detections 5규칙: 후보 밖 폐기 / 전사 부재 quote 폐기 / 에코 E3→E1 강등 /
      4자 미만·단독 발화 강등 / 동일 정규화 인용 중복 1건.
      ⭐ 2026-08-16: **단독 발화 강등은 chunk 제외**(통문장은 통째가 정답) — 갈리는 것이
      오직 kind 임을 고정한 짝 시험 + 청크에도 4자 미만·에코는 그대로 문다는 짝.
    - 상태 전이: E1→introduced / E2→practicing / D14(성공 산출 E2·E3 3회 — 2회 불가,
      2통화·2일 분산 + E3≥1) / chunk 2회 특례 / **마지막이 F 여도 승격**(2026-08-16 ④ 제거)
      — 다만 F 가 점수를 통과선 아래로 끌면 ①이 그대로 막는다(짝 시험).
    - 통화당 상한: 항목 순증 +2.0 / MASTERED 승격 caps(문법 2) / 신규 INTRODUCED 8.
    - fast-track: 5조건 1통화 승격(미확정) → 게이트(G2) 미산입 → 다음 통화 E2 확정 /
      F만 2통화 → PRACTICING(2.0) 복귀.
    - evaluate_level_up: D12 게이트 분모 문법(+L1 청크) 전용(어휘 UNSEEN 무관 승급) /
      게이트 = **G1 && G2 뿐**(2026-08-16 사장님 지시로 G4·G5 제거 — 관측만 남는다) /
      승급 시 korean_level+1 + history(gate_snapshot, gate_scope="grammar_chunk") /
      같은 trigger 재호출 멱등 / k=13 스킵.
    - 레벨 배정: **건너뛴 레벨은 기록 0**(2026-08-16 grandfathering 제거) / 기존 관측 행
      보존 / 하락 시 mastered→introduced 강등 / 상승 시 무변경 / placement history 유지.

기대값 소스: 스크래치패드 smoke_mastery.py(수동 점검 — 전 파트 통과 확인 후 이식).
DB 는 인메모리 sqlite(BigInteger+Identity PK → Integer 치환 — tests/test_level_test_call.py
컨벤션). LLM·네트워크 호출 없음 — 검증 게이트·전이·게이트 판정은 전부 순수 서버 코드.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Integer, create_engine, select, update
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.item_evidence import ItemEvidence
from domains.learning.models.learning_item import LearningItem
from domains.learning.models.level import Level
from domains.learning.models.member_item_progress import MemberItemProgress
from domains.learning.models.member_level_history import MemberLevelHistory

import domains.learning.service.normalcall_service as svc
from domains.learning.repository import mastery_repository
from domains.learning.service import mastery_service
from domains.learning.service.mastery_service import VerifiedEvidence

NOW = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# 인메모리 DB (BigInteger+Identity PK 는 sqlite 에서 autoincrement 안 되므로 Integer 로 치환)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def session_factory():
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


def _chunk(no: int, ko: str) -> LearningItem:
    return LearningItem(kind="chunk", source_key=f"c:{no:02d}:{ko}", band=1,
                        language="ko", level_no=1, assign_rule="chunk_v1",
                        surface=ko, seq_no=no)


def _grammar(key: str, surface: str, *, level: int = 2, seq: int = 1) -> LearningItem:
    return LearningItem(kind="grammar", source_key=f"g:basic_a:{key}", band=1,
                        language="ko", level_no=level, assign_rule="textbook_v1",
                        surface=surface, textbook_code="basic_a", seq_no=seq)


def _vocab(key: str, surface: str, *, level: int = 2, core: bool = True) -> LearningItem:
    return LearningItem(kind="vocab", source_key=f"v:{key}", band=1,
                        language="ko", level_no=level,
                        assign_rule="vocab_split_v1", surface=surface,
                        topik_grade=1, is_core=core)


@pytest.fixture()
def env(session_factory):
    """Voice/Character/Level(1·2) + 레벨1 회원(가입 10일 전) + 항목 시드.

    항목: L1 청크 c1~c4 / L2 문법 g1 / L2 core 어휘 v_core / L2 non-core 어휘 v_ext —
    smoke_mastery.py 와 동일 구성(기대값 호환).
    """
    db = session_factory()
    voice = Voice(name="Fenrir", gender="male")
    db.add(voice)
    db.flush()
    ch = Character(name="비비", role="선생님", personality="다정",
                   voice_id=voice.voice_id, price=0)
    db.add(ch)
    db.add_all([Level(language="ko", level_no=1, profile="생존"),
                Level(language="ko", level_no=2, profile="초급A")])
    member = Member(language="en", korean_level=1, onboarding_completed=True,
                    auth_user_id="auth-m1", created_at=NOW - timedelta(days=10))
    db.add(member)
    db.flush()

    c1, c2, c3, c4 = (_chunk(1, "안녕하세요"), _chunk(2, "감사합니다"),
                      _chunk(3, "이거 주세요"), _chunk(4, "잘 부탁드립니다"))
    g1 = _grammar("-(으)면", "-(으)면")
    v_core = _vocab("학교00", "학교", core=True)
    v_ext = _vocab("칠판00", "칠판", core=False)
    db.add_all([c1, c2, c3, c4, g1, v_core, v_ext])
    db.flush()
    db.commit()

    yield {"db": db, "member": member, "ch": ch,
           "c1": c1, "c2": c2, "c3": c3, "c4": c4,
           "g1": g1, "v_core": v_core, "v_ext": v_ext}
    db.close()


def _new_call(env, *, when=None, status="analyzing", **kw) -> Call:
    db = env["db"]
    call = Call(member_id=env["member"].member_id, character_id=env["ch"].character_id,
                call_date=when if when is not None else NOW, status=status, **kw)
    db.add(call)
    db.flush()
    return call


def _ve(item: LearningItem, grade: str, quote: str, ordn: int,
        ti: int | None = None, raw: str | None = None) -> VerifiedEvidence:
    return VerifiedEvidence(
        item_id=item.item_id, kind=item.kind, grade_raw=raw or grade, grade_final=grade,
        quote=quote, turn_index=ti if ti is not None else ordn, user_turn_ordinal=ordn,
        norm_hash=mastery_service.text_hash(mastery_service.normalize_text(quote)),
    )


def _backdate_call_evidence(db, call_id: int, dt: datetime) -> None:
    """증거 created_at 백데이트 — MASTERED "2일 분산" 조건 재현."""
    db.execute(
        update(ItemEvidence).where(ItemEvidence.call_id == call_id).values(created_at=dt)
    )
    db.commit()


def _progress(db, member_id: int, item: LearningItem) -> MemberItemProgress | None:
    return mastery_repository.get_progress_map(db, member_id, [item.item_id]).get(item.item_id)


# _apply_call_mastery 파이프라인용 공용 전사(검증 게이트 입력)
_DIALOG_ROWS = [
    {"turn_index": 0, "role": "beaver", "content": "오늘은 감사합니다 를 배워 볼까요"},
    {"turn_index": 1, "role": "user", "content": "감사합니다 정말 감사합니다"},
    {"turn_index": 2, "role": "beaver", "content": "잘했어요"},
    {"turn_index": 3, "role": "user", "content": "안녕하세요 저는 밥을 좋아해요"},
    {"turn_index": 4, "role": "user", "content": "이거 주세요"},
    {"turn_index": 5, "role": "user", "content": "좋아"},
]


# --------------------------------------------------------------------------- #
# (1) 서버 검증 게이트 5규칙 — _verify_detections
# --------------------------------------------------------------------------- #
def test_verify_detections_five_rules(env):
    """① 후보 밖 폐기 ② 전사 부재 폐기 ③ 에코 E3→E1 ④ 4자 미만·단독 발화 E1 ⑤ 중복 1건."""
    db, member = env["db"], env["member"]
    c1, c2, c3, c4 = env["c1"], env["c2"], env["c3"], env["c4"]
    call = _new_call(env, when=NOW - timedelta(days=1))
    db.commit()

    cands = [
        {"item_id": c.item_id, "kind": c.kind, "surface": c.surface,
         "example": None, "injected": False}
        for c in (c1, c2, c3, c4)
    ]
    D = svc.ItemDetection
    detections = [
        D(item_id=99999, evidence="E3", quote="안녕하세요 저는 밥을 좋아해요"),   # ① 후보 밖
        D(item_id=c3.item_id, evidence="E3", quote="존재하지 않는 문장이에요"),    # ② 전사 부재
        D(item_id=c2.item_id, evidence="E3", quote="감사합니다 정말 감사합니다"),  # ③ 에코
        D(item_id=c1.item_id, evidence="E3", quote="안녕하세요 저는 밥을 좋아해요"),  # 유지 E3
        D(item_id=c1.item_id, evidence="E3", quote="안녕하세요 저는 밥을  좋아해요"),  # ⑤ 중복
        D(item_id=c3.item_id, evidence="E3", quote="이거 주세요"),                 # 청크 단독 발화
        D(item_id=c4.item_id, evidence="E2", quote="좋아"),                        # ④ 4자 미만
    ]
    verified = svc._verify_detections(
        db, call.call_id, member.member_id, detections, cands, _DIALOG_ROWS
    )

    by = {(v.item_id, v.grade_raw): v for v in verified}
    assert len(verified) == 4, f"검증 통과 4건 기대, {len(verified)}"
    assert by[(c2.item_id, "E3")].grade_final == "E1"  # ③ 에코 강등
    assert by[(c1.item_id, "E3")].grade_final == "E3"  # 정상 E3 보존
    # ⛔ 2026-08-16 뒤집음: 청크의 단독 발화는 **강등하지 않는다**(통문장은 통째가 정답).
    #   c3 는 chunk 라 원판정 E3 그대로다. 비-chunk 단독 발화 강등은 아래 전용 시험이 지킨다.
    assert by[(c3.item_id, "E3")].grade_final == "E3"
    assert by[(c4.item_id, "E2")].grade_final == "E1"  # ④ 4자 미만 강등
    assert all(v.grade_raw in ("E2", "E3") for v in verified)  # 원판정 보존


def test_solo_utterance_demotion_skips_chunks_only(env):
    """④ 단독 발화 강등: 단어·문법엔 걸고 **통문장엔 안 건다**(2026-08-16).

    같은 조건(인용 == 원문, 4자 이상, 직전 비버 턴에 없음)을 kind 만 바꿔 두 번 통과시킨다
    — 갈리는 것이 오직 kind 임을 고정한다. 후보는 dict 계약이라 DB 행이 필요 없다.
    """
    db, member = env["db"], env["member"]
    call = _new_call(env)
    db.commit()

    surface = "책상이 있어요"
    cands = [
        {"item_id": 8001, "kind": "vocab", "surface": surface, "injected": False},
        {"item_id": 8002, "kind": "chunk", "surface": surface, "injected": False},
    ]
    rows = [
        {"turn_index": 0, "role": "beaver", "content": "다음 걸 해 볼까요"},
        {"turn_index": 1, "role": "user", "content": surface},
    ]
    D = svc.ItemDetection
    verified = svc._verify_detections(
        db, call.call_id, member.member_id,
        [D(item_id=8001, evidence="E2", quote=surface),
         D(item_id=8002, evidence="E2", quote=surface)],
        cands, rows,
    )
    by = {v.item_id: v for v in verified}
    assert by[8001].grade_final == "E1"   # 단어 — 항목만 덜렁 말한 것은 따라말하기
    assert by[8002].grade_final == "E2"   # 통문장 — 통째로 말한 것이 곧 산출
    assert all(v.grade_raw == "E2" for v in verified)   # 원판정은 둘 다 보존


def test_chunks_still_lose_the_four_char_and_echo_guards(env):
    """청크 면제는 ④ '단독 발화' 한 줄뿐 — 4자 미만(④)과 에코(③)는 그대로 문다."""
    db, member = env["db"], env["member"]
    call = _new_call(env)
    db.commit()

    cands = [{"item_id": 8003, "kind": "chunk", "surface": "네", "injected": False},
             {"item_id": 8004, "kind": "chunk", "surface": "밥을 먹었어요", "injected": False}]
    rows = [
        {"turn_index": 0, "role": "beaver", "content": "밥을 먹었어요 라고 해 보세요"},
        {"turn_index": 1, "role": "user", "content": "네 밥을 먹었어요"},
    ]
    D = svc.ItemDetection
    verified = svc._verify_detections(
        db, call.call_id, member.member_id,
        [D(item_id=8003, evidence="E2", quote="네"),               # ④ 4자 미만
         D(item_id=8004, evidence="E3", quote="밥을 먹었어요")],    # ③ 직전 비버 턴 에코
        cands, rows,
    )
    by = {v.item_id: v for v in verified}
    assert by[8003].grade_final == "E1"
    assert by[8004].grade_final == "E1"


# --------------------------------------------------------------------------- #
# (2) 상태 전이 — introduced / practicing / D14 / chunk 특례 / F 와 점수
# --------------------------------------------------------------------------- #
def test_e1_creates_introduced_and_e2_promotes_practicing(env):
    """UNSEEN + E1 → introduced(전이 없음) / UNSEEN + E2 → practicing(즉시 전이)."""
    db, mid = env["db"], env["member"].member_id
    v1, v2 = env["v_core"], env["v_ext"]
    call = _new_call(env)
    s = mastery_service.apply_evidence(db, mid, call.call_id, [
        _ve(v1, "E1", "학교 라고 따라했어요", 0),
        _ve(v2, "E2", "칠판 이 저기 있어요", 1),
    ])
    db.commit()

    p1, p2 = _progress(db, mid, v1), _progress(db, mid, v2)
    assert p1.status == "introduced" and abs(p1.score - 0.5) < 1e-9
    assert p2.status == "practicing"
    assert s["introduced"] == 2 and s["practicing"] == 1


def test_mastered_by_three_prompted_without_spontaneous(env):
    """⭐⭐ 2026-08-20 사장님 지시 — **자발(E3) 없이 유도(E2) 3회면 문법도 mastered.**

    지시 원문: "자발이 안되면 유도로 3번이상 말하면 통과로 하자".
    이전엔 문법·어휘에 "산출 3회 **그중 E3≥1**" 이 붙어 있어, 유도만 쌓인 학습자는
    영원히 mastered 가 못 됐다(chunk 는 원래 특례라 해당 없음 — 그래서 L1 회원에겐
    이 변경이 아무것도 안 바꾼다. 이 시험이 문법 항목 g1 을 쓰는 이유다).

    ⛔ 이 시험이 지키는 건 "E3 를 요구하지 않는다" 하나다. 산출 3회·2통화·2일은
      그대로 요구한다 — 그건 게이밍 방어라 같이 풀리면 안 된다.
    ⚠ 이 완화가 기대는 안전핀(힌트 열람 → E1 강등)은 현재 **프론트가 hint_used 를
      안 보내서 데이터가 안 들어온다**(call 1101: 힌트 10회 / hint_used 0회).
      mastery_service._mastered_conditions_met docstring 참조.
    """
    db, mid, g = env["db"], env["member"].member_id, env["g1"]

    # call1(어제): E2 2회 — 서로 다른 인용이라 ⑤ 중복에 안 걸린다
    call1 = _new_call(env, when=NOW - timedelta(days=1))
    mastery_service.apply_evidence(db, mid, call1.call_id, [
        _ve(g, "E2", "비가 오면 집에 있어요", 0),
    ])
    db.commit()
    _backdate_call_evidence(db, call1.call_id, NOW - timedelta(days=1))

    # call2(오늘): E2 → 산출 2회 · 2통화 · 2일 이지만 아직 3회가 아니다
    call2 = _new_call(env)
    mastery_service.apply_evidence(db, mid, call2.call_id, [
        _ve(g, "E2", "주말이 되면 뭐 해요", 1),
    ])
    db.commit()
    p = _progress(db, mid, g)
    assert p.status == "practicing", f"산출 2회로 승격되면 안 됨: {p.status}"

    # call3: E2 → 산출 3회. **E3 는 한 건도 없다.** 그래도 mastered 여야 한다.
    call3 = _new_call(env)
    mastery_service.apply_evidence(db, mid, call3.call_id, [
        _ve(g, "E2", "피곤하면 일찍 자요", 0),
    ])
    db.commit()
    p = _progress(db, mid, g)
    assert p.spontaneous_count == 0, "전제 붕괴 — 이 시험은 자발 0 이어야 의미가 있다"
    assert p.prompted_count == 3
    assert p.status == "mastered", "자발 없이 유도 3회면 통과해야 한다(2026-08-20)"


def test_mastered_requires_three_productions_d14(env):
    """D14: 성공 산출(E2/E3) 2회로는 mastered 불가 — 3회(2통화·2일, E3≥1)면 승격."""
    db, mid, g = env["db"], env["member"].member_id, env["g1"]

    # call1(어제): E3+E1 → practicing, score 2.0 (E1 은 산출 미산입)
    call1 = _new_call(env, when=NOW - timedelta(days=1))
    mastery_service.apply_evidence(db, mid, call1.call_id, [
        _ve(g, "E3", "비가 오면 집에 있어요", 0),
        _ve(g, "E1", "시간이 있으면 이라고 따라했어요", 2),
    ])
    db.commit()
    _backdate_call_evidence(db, call1.call_id, NOW - timedelta(days=1))

    # call2(오늘): E2 → score 3.0·2통화·2일·E3≥1 이지만 산출 2회 → 승격 불가
    call2 = _new_call(env)
    mastery_service.apply_evidence(db, mid, call2.call_id, [
        _ve(g, "E2", "주말이 되면 뭐 해요", 1),
    ])
    db.commit()
    p = _progress(db, mid, g)
    assert p.status == "practicing", f"산출 2회로 승격되면 안 됨(D14): {p.status}"
    assert abs(p.score - 3.0) < 1e-9

    # call3: E2 → 산출 3회 충족 → mastered(observed)
    call3 = _new_call(env)
    s3 = mastery_service.apply_evidence(db, mid, call3.call_id, [
        _ve(g, "E2", "피곤하면 일찍 자요", 0),
    ])
    db.commit()
    p = _progress(db, mid, g)
    assert p.status == "mastered" and p.provenance == "observed"
    assert p.mastered_at is not None
    assert s3["mastered"] == 1


def test_chunk_two_production_special_case(env):
    """chunk 는 산출 2회 특례(E3 불요) — 같은 이력의 문법은 3회 미달로 미승격."""
    db, mid = env["db"], env["member"].member_id
    ck, gg = env["c1"], env["g1"]

    call1 = _new_call(env, when=NOW - timedelta(days=1))
    mastery_service.apply_evidence(db, mid, call1.call_id, [
        _ve(ck, "E3", "안녕하세요 처음 뵙겠습니다", 0),
        _ve(gg, "E3", "비가 오면 집에 있어요", 1),
    ])
    db.commit()
    _backdate_call_evidence(db, call1.call_id, NOW - timedelta(days=1))

    call2 = _new_call(env)
    mastery_service.apply_evidence(db, mid, call2.call_id, [
        _ve(ck, "E3", "안녕하세요 오늘 날씨가 좋네요", 0),
        _ve(gg, "E3", "시간이 있으면 같이 가요", 1),
    ])
    db.commit()

    p_ck, p_gg = _progress(db, mid, ck), _progress(db, mid, gg)
    assert p_ck.status == "mastered", f"chunk 2회 특례 실패: {p_ck.status} score={p_ck.score}"
    assert p_gg.status == "practicing", f"문법이 2회로 승격됨(D14 위반): {p_gg.status}"


def test_a_final_f_no_longer_blocks_mastery(env):
    """⛔ **틀린 채로 끝난 통화에서도 마스터가 확정된다** — ④를 뺀 대가다(2026-08-16 사장님 지시).

    ⚠ 성질을 **뒤집어** 다시 쓴 것이다(지운 게 아니다). 예전 성질: "최근 증거가 F 면 승격 불가".

    ⭐ 그리고 이 시험이 **왜 score 를 4.0 으로 올려 놓고 시작하는지**가 핵심이다:
      F 는 점수 −1.0 이라 score 가 3.0 아래로 떨어지면 **①이 여전히 막는다.**
      ⇒ ④ 제거의 실질 효과는 "score 가 넉넉한 항목이 한 번 틀렸다고 마스터가 미뤄지지
        않는다"이지 **"틀려도 무조건 마스터"가 아니다.** 그 경계를 아래 시험이 짝으로 지킨다.
    """
    db, mid, g = env["db"], env["member"].member_id, env["g1"]

    call1 = _new_call(env, when=NOW - timedelta(days=1))
    mastery_service.apply_evidence(db, mid, call1.call_id, [
        _ve(g, "E3", "비가 오면 집에 있어요", 0),
        _ve(g, "E2", "주말이 되면 뭐 해요", 2),
    ])
    db.commit()
    _backdate_call_evidence(db, call1.call_id, NOW - timedelta(days=1))

    # call2: E2·E3 뒤 마지막이 F — score 3.0·산출 4회·2통화·2일·E3≥1 전부 충족 상태
    call2 = _new_call(env)
    mastery_service.apply_evidence(db, mid, call2.call_id, [
        _ve(g, "E2", "피곤하면 일찍 자요", 0),
        _ve(g, "E3", "심심하면 전화해 주세요", 2),
        _ve(g, "F", "배고프으면 이라고 잘못 말했어요", 4),
    ])
    db.commit()

    p = _progress(db, mid, g)
    # ⭐ score 3.0 = ① 통과선에 **정확히** 걸친다(2.0 + 순증상한 2.0 − F 1.0).
    assert abs(p.score - 3.0) < 1e-9
    assert p.status == "mastered", f"마지막이 F 라고 아직 막는다: {p.status}"


def test_an_f_that_drops_the_score_still_blocks(env):
    """⭐⭐ **F 가 무해해진 게 아니다** — score 를 3.0 아래로 끌면 ①이 그대로 막는다.

    ⛔ 위 시험과 **짝**이다. 둘 중 하나만 보면 "이제 틀려도 다 마스터된다"로 잘못 읽는다.
      ④를 뺀 뒤 F 의 주 방어선은 **점수**다.
    """
    db, mid, g = env["db"], env["member"].member_id, env["g1"]

    call1 = _new_call(env, when=NOW - timedelta(days=1))
    mastery_service.apply_evidence(db, mid, call1.call_id, [
        _ve(g, "E3", "비가 오면 집에 있어요", 0),
        _ve(g, "E2", "주말이 되면 뭐 해요", 2),
    ])
    db.commit()
    _backdate_call_evidence(db, call1.call_id, NOW - timedelta(days=1))

    # call2: 성공 산출은 채우되 F 를 **두 번** — score 가 3.0 아래로 내려간다.
    call2 = _new_call(env)
    mastery_service.apply_evidence(db, mid, call2.call_id, [
        _ve(g, "E2", "피곤하면 일찍 자요", 0),
        _ve(g, "E3", "심심하면 전화해 주세요", 2),
        _ve(g, "F", "배고프으면 이라고 잘못 말했어요", 4),
        _ve(g, "F", "또 배고프으면 이라고 했어요", 6),
    ])
    db.commit()

    p = _progress(db, mid, g)
    assert p.score < 3.0, f"이 시험의 전제(점수가 통과선 아래)가 깨졌다: {p.score}"
    assert p.status == "practicing", f"점수가 모자란데 마스터됐다: {p.status}"


# --------------------------------------------------------------------------- #
# (3) 통화당 상한 — 순증 +2.0 / MASTERED caps / INTRODUCED 8
# --------------------------------------------------------------------------- #
def test_net_gain_cap_per_call(env):
    """한 통화 항목별 순증 +2.0 상한 — E3×2+E2(명목 +4.0)도 score 2.0 에서 멈춤."""
    db, mid, g = env["db"], env["member"].member_id, env["g1"]
    call = _new_call(env)
    # user_turn_ordinal 0·1(사이 턴 0) → fast-track 쌍 조건 불충족(순수 점수 경로)
    mastery_service.apply_evidence(db, mid, call.call_id, [
        _ve(g, "E3", "비가 오면 집에 있어요", 0),
        _ve(g, "E3", "심심하면 전화해 주세요", 1),
        _ve(g, "E2", "피곤하면 일찍 자요", 2),
    ])
    db.commit()
    p = _progress(db, mid, g)
    assert abs(p.score - 2.0) < 1e-9, f"순증 +2.0 상한 위반: {p.score}"
    assert p.status == "practicing"


def test_mastered_promotion_cap_grammar_two_per_call(env):
    """MASTERED 승격 상한(문법 2/통화) — 3개가 동시 충족해도 2개만 승격."""
    db, mid = env["db"], env["member"].member_id
    gs = [_grammar(f"-cap{i}", f"-문법cap{i}", seq=10 + i) for i in range(3)]
    db.add_all(gs)
    db.flush()

    call1 = _new_call(env, when=NOW - timedelta(days=1))
    mastery_service.apply_evidence(db, mid, call1.call_id, [
        _ve(g, "E3", f"어제 {g.surface} 를 자발적으로 썼어요 {i}", i)
        for i, g in enumerate(gs)
    ])
    db.commit()
    _backdate_call_evidence(db, call1.call_id, NOW - timedelta(days=1))

    # call2: 각각 E2+E2 → score 3.5·산출 3회·2통화·2일·E3≥1 전부 충족(3개 모두)
    call2 = _new_call(env)
    evs = []
    for i, g in enumerate(gs):
        evs.append(_ve(g, "E2", f"오늘 {g.surface} 를 유도로 썼어요 {i}", 2 * i))
        evs.append(_ve(g, "E2", f"다시 {g.surface} 를 한번 더 썼어요 {i}", 2 * i + 1))
    s = mastery_service.apply_evidence(db, mid, call2.call_id, evs)
    db.commit()

    statuses = [_progress(db, mid, g).status for g in gs]
    assert statuses == ["mastered", "mastered", "practicing"], statuses
    assert s["mastered"] == 2


def test_introduced_cap_eight_per_call(env):
    """신규 INTRODUCED 상한 8/통화 — 초과분은 행 없이 증거만 append(반영 0)."""
    db, mid = env["db"], env["member"].member_id
    vs = [_vocab(f"cap{i:02d}", f"단어cap{i}") for i in range(10)]
    db.add_all(vs)
    db.flush()

    call = _new_call(env)
    s = mastery_service.apply_evidence(db, mid, call.call_id, [
        _ve(v, "E1", f"{v.surface} 라고 따라했어요", i) for i, v in enumerate(vs)
    ])
    db.commit()

    assert s["introduced"] == 8 and s["evidence"] == 10, s
    prog_map = mastery_repository.get_progress_map(db, mid, [v.item_id for v in vs])
    assert len(prog_map) == 8, f"progress 행 8개 기대: {len(prog_map)}"
    ev_count = len(mastery_repository.list_evidence_for_items(
        db, mid, [v.item_id for v in vs]
    ))
    assert ev_count == 10  # 상한 초과 2건도 증거는 남는다(다음 통화 재포착)


# --------------------------------------------------------------------------- #
# (4)+(5) fast-track 수명주기 + 증거통화 게이트 + evaluate_level_up (smoke PART 2~3 이식)
# --------------------------------------------------------------------------- #
def test_fast_track_and_levelup_pipeline(env):
    """fast-track 승격(미확정)→G2 stay→확정/복귀→증거통화 승급→멱등→G5 잠금 관통.

    미확정 fast-track 이 G2 에 미산입됨을 두 시점으로 확인한다 — call1 직후 stay
    (g2=0.0: 미확정 c2·c4 미산입), 승급 스냅샷 g2=0.5(확정 c2 + observed c3 만 산입).
    D15: 게이트에 통화 수 조건 없음(snapshot 에 g3 부재) — G4 창은 증거통화 기반.
    """
    db, mid = env["db"], env["member"].member_id
    c1, c2, c3, c4 = env["c1"], env["c2"], env["c3"], env["c4"]

    # ── call1: c1 E3(→practicing), c2 E3×2(fast-track), c3 E2, c4 E3×2(fast-track) ──
    call1 = _new_call(env, when=NOW - timedelta(days=1))
    s1 = mastery_service.apply_evidence(db, mid, call1.call_id, [
        _ve(c1, "E3", "안녕하세요 저는 밥을 좋아해요", 0),
        _ve(c2, "E3", "우리 감사합니다 해요", 1),
        _ve(c2, "E3", "감사합니다 선생님 오늘도 재밌었어요", 4),
        _ve(c3, "E2", "이거 주세요 라고 말했어요", 2),
        _ve(c4, "E3", "잘 부탁드립니다 선생님", 3),
        _ve(c4, "E3", "오늘부터 잘 부탁드립니다 많이 가르쳐 주세요", 6),
    ])
    db.commit()
    pm = mastery_repository.get_progress_map(
        db, mid, [c1.item_id, c2.item_id, c3.item_id, c4.item_id]
    )
    assert pm[c1.item_id].status == "practicing"
    assert pm[c2.item_id].status == "mastered" and pm[c2.item_id].provenance == "fast_track"
    assert pm[c2.item_id].fast_track_confirmed_at is None, "fast-track 은 미확정이어야"
    assert pm[c4.item_id].status == "mastered" and pm[c4.item_id].provenance == "fast_track"
    assert pm[c3.item_id].status == "practicing"
    assert s1["fast_tracked"] == 2 and s1["evidence"] == 6, s1
    assert abs(pm[c2.item_id].score - 2.0) < 1e-9, f"순증 상한 위반: {pm[c2.item_id].score}"

    _backdate_call_evidence(db, call1.call_id, NOW - timedelta(days=1))

    # ── stay: G2 미충족 — 미확정 fast-track(c2·c4)은 분자 미산입(확정 0/4) ──
    r0 = mastery_service.evaluate_level_up(db, mid, trigger_call_id=call1.call_id)
    db.commit()
    assert r0["result"] == "stay", r0
    snap0 = r0["snapshot"]
    assert snap0["g1"]["pass"] is True and snap0["g2"]["ratio"] == 0.0, snap0
    assert snap0["g2"]["pass"] is False
    assert "g3" not in snap0, "D15 — 체류 게이트(G3) 폐지"
    assert snap0["g4"]["pass"] is True  # 증거 6 < 10 → 표본 부족 pass

    # ── call2: c3 정식 MASTERED(2통화·2일) / c2 E2 → fast-track 확정 / c4 F 1회 ──
    call2 = _new_call(env)
    mastery_service.apply_evidence(db, mid, call2.call_id, [
        _ve(c3, "E3", "저기요 이거 주세요 두 개요", 0),
        _ve(c3, "E2", "네 이거 주세요 할게요", 2),
        _ve(c2, "E2", "감사합니다 또 만나요", 3),
        _ve(c4, "F", "잘 부탁드립니다를 잘 부탁드리세요 라고 잘못", 4),
    ])
    db.commit()
    pm = mastery_repository.get_progress_map(db, mid, [c2.item_id, c3.item_id, c4.item_id])
    assert pm[c3.item_id].status == "mastered" and pm[c3.item_id].provenance == "observed"
    assert pm[c2.item_id].fast_track_confirmed_at is not None, "fast-track 확정 실패"
    assert pm[c4.item_id].status == "mastered", "F 1회로는 복귀하면 안 됨"

    # ── call3: c4 F만 2회째 → PRACTICING(score 2.0) 복귀 ──
    call3 = _new_call(env)
    s3 = mastery_service.apply_evidence(db, mid, call3.call_id, [
        _ve(c4, "F", "내일도 잘 부탁드리세요 라고 또 잘못", 1),
    ])
    db.commit()
    p4 = _progress(db, mid, c4)
    assert p4.status == "practicing" and abs(p4.score - 2.0) < 1e-9
    assert s3["fast_track_reverted"] == 1

    # 기본 검출 후보 폴백 — practicing(c1, c4)만, mastered 제외
    got = {c["item_id"] for c in mastery_repository.load_default_candidates(db, mid)}
    assert got == {c1.item_id, c4.item_id}, got

    # ── 승급 1→2: 파이프라인 관통(D15 — 통화 수 시드 없이 게이트 4종만으로) ──
    # (call3 은 위에서 증거를 직접 적립했으므로 M4 가드에 걸린다 — 새 통화로 검증)
    call3b = _new_call(env)
    res = svc._apply_call_mastery(db, call3b.call_id, mid, [], [], _DIALOG_ROWS)
    assert res["levelup"]["result"] == "promoted", res["levelup"]
    assert res["levelup"]["from_level"] == 1 and res["levelup"]["to_level"] == 2
    assert db.get(Member, mid).korean_level == 2

    hist = db.scalar(select(MemberLevelHistory).where(
        MemberLevelHistory.trigger_call_id == call3b.call_id
    ))
    assert hist is not None and hist.reason == "gate_promotion" and hist.gate_snapshot

    snap = res["levelup"]["snapshot"]
    assert snap["gate_scope"] == "grammar_chunk"
    assert snap["denominator"] == 4  # L1 청크 4 — g1(L2)·어휘 미산입(D12)
    assert snap["g2"]["ratio"] == 0.5, snap  # 확정 c2 + observed c3 / 4 — 미확정 복귀 c4 제외
    # G4 창 = 최근 5 증거통화(call1~3 — call3b 는 증거 0이라 창에 없음)
    assert snap["g4"]["evidence_total"] == 11 and snap["g4"]["f_count"] == 2, snap
    assert "g3" not in snap, "D15 — 체류 게이트(G3) 폐지"

    # 리뷰 M4: 증거가 이미 적립된 call 은 파이프라인 재실행 시 통째 스킵(이중 적립 방지)
    res_skip = svc._apply_call_mastery(db, call3.call_id, mid, [], [], _DIALOG_ROWS)
    assert res_skip["evidence"] is None and res_skip["levelup"] is None

    # ── 멱등: 같은 trigger 재호출 → 스킵, 레벨 그대로 ──
    r2 = mastery_service.evaluate_level_up(db, mid, trigger_call_id=call3b.call_id)
    assert r2 == {"result": "skipped", "reason": "already_evaluated"}, r2
    assert db.get(Member, mid).korean_level == 2

    # ── ⛔ G5 는 이제 **판정에 안 쓴다**(2026-08-16 사장님 지시). 그래도 stay 다 ──
    #    ⚠ 예전엔 이 자리가 "일수 미달이라 stay"였다. 지금 stay 하는 **이유가 다르다**:
    #      승급 직후 새 레벨 항목엔 증거가 0 이라 **G1 이 막는다.**
    #      ⇒ 시험이 통과하는 것만 보고 "G5 가 아직 산다"고 읽으면 안 된다. 이유를 못박는다.
    call4 = _new_call(env, status="done")
    r4 = mastery_service.evaluate_level_up(db, mid, trigger_call_id=call4.call_id)
    db.commit()
    assert r4["result"] == "stay", r4
    snap4 = r4["snapshot"]
    assert snap4["g1"]["pass"] is False, "이제 막는 것은 G1 이다(G5 가 아니다)"
    g5 = snap4["g5"]
    assert g5["pass"] is False and g5["enforced"] is False, g5  # 기록은 하되 판정엔 안 쓴다


def test_evaluate_skips_at_max_level(env):
    """k=13(MAX_LEVEL) → 승급 판정 스킵."""
    db = env["db"]
    m13 = Member(language="en", korean_level=13, onboarding_completed=True,
                 auth_user_id="auth-m13")
    db.add(m13)
    db.flush()
    call = Call(member_id=m13.member_id, character_id=env["ch"].character_id,
                call_date=NOW, status="done")
    db.add(call)
    db.flush()
    r = mastery_service.evaluate_level_up(db, m13.member_id, trigger_call_id=call.call_id)
    assert r == {"result": "skipped", "reason": "max_level"}, r
    assert db.get(Member, m13.member_id).korean_level == 13


# --------------------------------------------------------------------------- #
# (5) D12 — 승급 게이트 분모는 문법(+L1 청크)만 (smoke PART 5 이식)
# --------------------------------------------------------------------------- #
def test_gate_denominator_is_grammar_only_d12(env):
    """레벨 2 게이트 분모 = 문법만 — 어휘(core/non-core) 미산입, 어휘 UNSEEN 무관 승급."""
    db = env["db"]
    g1, v_core = env["g1"], env["v_core"]

    gate2 = mastery_repository.list_gate_items(db, 2)
    assert set(gate2) == {(g1.item_id, "grammar")}, f"레벨2 게이트는 문법 전용: {gate2}"

    # 어휘 진행 0(UNSEEN)인 채 문법만 마스터 → 승급 2→3 (구 게이트라면 G1=0.5 탈락)
    m5 = Member(language="en", korean_level=2, onboarding_completed=True,
                auth_user_id="auth-m5", created_at=NOW - timedelta(days=10))
    db.add(m5)
    db.flush()
    db.add(MemberItemProgress(
        member_id=m5.member_id, item_id=g1.item_id, status="mastered",
        score=4.0, provenance="observed",
        repeat_count=0, prompted_count=1, spontaneous_count=2, miss_count=0,
        first_seen_at=NOW - timedelta(days=9), last_seen_at=NOW,
        mastered_at=NOW - timedelta(days=1),
    ))
    # D15: 통화 수 시드 불요 — 게이트에 체류(G3) 조건이 없다(증거 0 → G4 표본 부족 pass).
    call5 = Call(member_id=m5.member_id, character_id=env["ch"].character_id,
                 call_date=NOW, status="done")
    db.add(call5)
    db.flush()

    r5 = mastery_service.evaluate_level_up(db, m5.member_id, trigger_call_id=call5.call_id)
    db.commit()
    assert r5["result"] == "promoted" and r5["from_level"] == 2 and r5["to_level"] == 3, r5
    snap5 = r5["snapshot"]
    assert snap5["gate_scope"] == "grammar_chunk"
    assert snap5["denominator"] == 1, f"어휘가 분모에 산입됨: {snap5}"
    assert snap5["g1"]["ratio"] == 1.0 and snap5["g2"]["ratio"] == 1.0, snap5
    # 어휘는 여전히 UNSEEN — 게이트가 어휘를 요구하지 않았다
    assert not mastery_repository.get_progress_map(db, m5.member_id, [v_core.item_id])


# --------------------------------------------------------------------------- #
# (6) 레벨 배정 — ⛔ grandfathering 은 **없앴다**(2026-08-16)
# --------------------------------------------------------------------------- #
def test_placement_creates_no_rows_for_skipped_levels(env):
    """⛔⛔ **건너뛴 레벨은 기록 0이다.** 예전엔 ≤k−2 mastered / k−1 introduced 를 찍었다.

    ⚠ 기대값이 뒤집힌 것은 숫자를 맞춘 게 아니라 **성질이 바뀐 것**이다:
      *"레벨이 처음 3이면 배운 거 0으로 해야 함. 레벨 1·2를 배웠다고 처리하면 안 돼."*(사장님)

    그 장치가 **양쪽으로** 틀렸다는 게 실측으로 확인됐다:
      · 하락 후: L1 이 전부 mastered 라 고를 재료가 0 → `teaching_plan=0` → 증거 0 → **영원히 갇힘**
      · 배정 직후: placement 가 게이트 분자가 되어 **첫 통화에 승급**(#247·#249)
    ⇒ 값 조정이 아니라 제거가 답이다. ⭐ 기존 관측 행은 그대로 보존된다(아래).
    """
    db = env["db"]
    c1, v_ext = env["c1"], env["v_ext"]

    m3 = Member(language="en", korean_level=None, onboarding_completed=True,
                auth_user_id="auth-m3")
    db.add(m3)
    db.flush()
    lt_call = Call(member_id=m3.member_id, character_id=env["ch"].character_id,
                   call_date=NOW, status="analyzing", call_type="level_test")
    db.add(lt_call)
    db.flush()
    # 기존 관측 행 — placement 가 덮으면 안 됨
    db.add(MemberItemProgress(
        member_id=m3.member_id, item_id=c1.item_id, status="practicing",
        score=1.5, provenance="observed",
        repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
        first_seen_at=NOW, last_seen_at=NOW,
    ))
    db.flush()

    gf = mastery_service.record_placement(
        db, m3.member_id, 3, trigger_call_id=lt_call.call_id, from_level=None
    )
    m3.korean_level = 3
    db.commit()

    # ⛔ 하위 레벨 행을 **하나도** 만들지 않는다.
    assert gf == {"mastered": 0, "introduced": 0, "demoted": 0}, gf

    # ⭐ 기존 관측 행은 그대로다 — 제거가 "학습 기록을 지운다"는 뜻이 아니다.
    p_c1 = _progress(db, m3.member_id, c1)
    assert p_c1.status == "practicing" and p_c1.provenance == "observed", "기존 행이 바뀌었다"

    made = mastery_repository.get_progress_map(
        db, m3.member_id,
        [env["c2"].item_id, env["c3"].item_id, env["c4"].item_id,
         env["g1"].item_id, env["v_core"].item_id, v_ext.item_id],
    )
    assert not made, f"건너뛴 레벨에 행이 생겼다: {made}"

    hist3 = mastery_repository.get_latest_history(db, m3.member_id)
    assert (hist3.reason == "placement" and hist3.to_level == 3
            and hist3.trigger_call_id == lt_call.call_id)

    # placement history 의 trigger_call 덕에 같은 통화 evaluate 는 멱등 스킵
    r3 = mastery_service.evaluate_level_up(db, m3.member_id, trigger_call_id=lt_call.call_id)
    assert r3 == {"result": "skipped", "reason": "already_evaluated"}, r3


def test_placement_alone_never_promotes(env):
    """⛔ placement 만으로는 절대 승급하지 않는다 — 하나도 안 배우고 레벨이 오르던 버그.

    재현(실측 member=20, ja): 예전에 레벨 3 을 받아 grandfathering 이 L1 항목 전부를
    placement/mastered 로 찍었다. 그 뒤 "레벨테스트 다시하기"로 레벨 1 을 받자, 그
    placement 행들이 이제 **현재 레벨의 게이트 항목**이 됐다. 첫 통화가 끝나자마자
    g1=g2=100% 로 승급 — 증거는 0건인데. 2026-08-02 하루에 두 번 났다(#247·#249).

    레벨이 **내려갈 때만** 터진다. 올바르게 배정된 레벨에서는 grandfathering 이 ≥k 를
    건드리지 않아 게이트 항목에 placement 가 아예 없다.
    """
    db, member = env["db"], env["member"]
    call = _new_call(env, status="done")

    # L1 게이트 항목(청크 4개)을 전부 placement/mastered 로 — 옛 상위 레벨 배정의 잔재.
    for ch_item in (env["c1"], env["c2"], env["c3"], env["c4"]):
        db.add(MemberItemProgress(
            member_id=member.member_id, item_id=ch_item.item_id,
            status="mastered", provenance="placement",
            score=mastery_service.PLACEMENT_MASTERED_SCORE,
            repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
            first_seen_at=NOW, last_seen_at=NOW, mastered_at=NOW,
        ))
    db.commit()

    r = mastery_service.evaluate_level_up(db, member.member_id, trigger_call_id=call.call_id)

    assert r["result"] == "stay", f"placement 만으로 승급했다: {r}"
    snap = r.get("gate") or r.get("snapshot") or {}
    if snap:  # 스냅샷을 돌려주는 구현이면 분자가 0이어야 한다
        assert snap.get("mastered_confirmed") == 0, snap
        assert snap.get("introduced_plus") == 0, snap
        assert snap.get("placement_excluded") == 4, snap
    assert mastery_repository.get_language_level(db, member.member_id, "ko") in (1, None), \
        "레벨이 올라갔다"


# --------------------------------------------------------------------------- #
# (7) 멀티랭귀지 — member-only 집계·선별·레벨 출처 언어 격리 (T3 리스크 1)
# --------------------------------------------------------------------------- #
def test_language_isolation_member_only_aggregates(env):
    """ko/ja 동시 학습 시 증거·이력·레벨·선별이 language 로 안 섞인다(오염 차단).

    특히 get_latest_history(진입시각)·증거통화 집계가 무필터면 최신 ja 를 잡아 ko 를
    오염시키는 치명 케이스를 언어별 필터가 막는지 검증한다.
    """
    db, mid = env["db"], env["member"].member_id

    # ja 레벨 마스터 + ja 학습 항목(문법 1) 시드
    db.add_all([Level(language="ja", level_no=1, profile="ja survival"),
                Level(language="ja", level_no=2, profile="ja beginner")])
    ja_g = LearningItem(kind="grammar", source_key="g:ja:xx", band=1,
                        language="ja", level_no=2, assign_rule="textbook_v1",
                        surface="ja문법", textbook_code="basic_a", seq_no=1)
    db.add(ja_g)
    db.flush()

    # ── member-only 이력: ko(먼저) + ja(나중) — 무필터면 최신 ja 가 잡힌다 ──
    db.add_all([
        MemberLevelHistory(member_id=mid, language="ko", from_level=None,
                           to_level=1, reason="placement",
                           created_at=NOW - timedelta(days=5)),
        MemberLevelHistory(member_id=mid, language="ja", from_level=None,
                           to_level=2, reason="placement",
                           created_at=NOW - timedelta(days=1)),
    ])
    db.flush()
    assert mastery_repository.get_latest_history(db, mid, "ko").language == "ko"
    assert mastery_repository.get_latest_history(db, mid, "ja").language == "ja"

    # ── member-only 증거통화: ko 2통화 + ja 1통화 ──
    ko_c1 = _new_call(env, target_language="ko")
    ko_c2 = _new_call(env, target_language="ko")
    ja_c = _new_call(env, target_language="ja")
    for c, lang, item in ((ko_c1, "ko", env["g1"]), (ko_c2, "ko", env["g1"]),
                          (ja_c, "ja", ja_g)):
        db.add(ItemEvidence(member_id=mid, language=lang, item_id=item.item_id,
                            call_id=c.call_id, grade_raw="E2", grade_final="E2",
                            verified=True, score_delta=1.0, created_at=NOW))
    db.commit()

    assert mastery_repository.count_evidence_calls_since(db, mid, None, "ko") == 2
    assert mastery_repository.count_evidence_calls_since(db, mid, None, "ja") == 1
    assert set(mastery_repository.list_recent_evidence_call_ids(db, mid, language="ja")) \
        == {ja_c.call_id}

    # ── 레벨 출처: ko 폴백(korean_level=1) vs ja 콜드스타트(mll·korean_level 부재=None) ──
    assert mastery_repository.get_language_level(db, mid, "ko") == 1
    assert mastery_repository.get_language_level(db, mid, "ja") is None

    # ── 선별: learning_item.language 로 후보 격리 ──
    for item in (env["g1"], ja_g):
        db.add(MemberItemProgress(
            member_id=mid, item_id=item.item_id, status="practicing",
            score=2.0, provenance="observed",
            repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
            first_seen_at=NOW, last_seen_at=NOW))
    db.commit()
    ja_cands = {c["item_id"]
                for c in mastery_repository.load_default_candidates(db, mid, language="ja")}
    ko_cands = {c["item_id"]
                for c in mastery_repository.load_default_candidates(db, mid, language="ko")}
    assert ja_cands == {ja_g.item_id}
    assert env["g1"].item_id in ko_cands and ja_g.item_id not in ko_cands


def test_language_scoped_levelup_writes_member_language_level(env):
    """ja 레벨업 판정은 member_language_level[ja] 만 쓰고 ko(korean_level)를 안 건드린다."""
    db = env["db"]
    # ja 레벨 1 회원 — mll 로 ja=1 배정, ko 는 미학습(korean_level None)
    m = Member(language="en", korean_level=None, onboarding_completed=True,
               auth_user_id="auth-ja1", created_at=NOW - timedelta(days=10))
    db.add(m)
    db.add_all([Level(language="ja", level_no=1, profile="ja survival"),
                Level(language="ja", level_no=2, profile="ja beginner")])
    db.flush()
    mastery_repository.upsert_language_level(db, m.member_id, "ja", 1)
    db.commit()
    # ja 게이트 대상(청크 1개)만 있고 이미 mastered → 승급 1→2
    ja_ck = LearningItem(kind="chunk", source_key="c:ja:01", band=1,
                         language="ja", level_no=1, assign_rule="chunk_v1",
                         surface="ja청크", seq_no=1)
    db.add(ja_ck)
    db.flush()
    db.add(MemberItemProgress(
        member_id=m.member_id, item_id=ja_ck.item_id, status="mastered",
        score=4.0, provenance="observed",
        repeat_count=0, prompted_count=1, spontaneous_count=2, miss_count=0,
        first_seen_at=NOW - timedelta(days=9), last_seen_at=NOW,
        mastered_at=NOW - timedelta(days=1)))
    ja_call = _new_call(env, status="done", target_language="ja")
    ja_call.member_id = m.member_id
    db.flush()

    r = mastery_service.evaluate_level_up(
        db, m.member_id, trigger_call_id=ja_call.call_id, language="ja"
    )
    db.commit()
    assert r["result"] == "promoted" and r["to_level"] == 2, r
    # ja 레벨만 2로 상승, ko(korean_level)는 여전히 None(dual-write 는 ko 만)
    assert mastery_repository.get_language_level(db, m.member_id, "ja") == 2
    assert db.get(Member, m.member_id).korean_level is None
    hist = mastery_repository.get_latest_history(db, m.member_id, "ja")
    assert hist.reason == "gate_promotion" and hist.language == "ja"


# --------------------------------------------------------------------------- #
# (6-2) 레벨이 **내려갈 때** — 마스터를 되돌린다 (2026-08-16 사장님 설계)
# --------------------------------------------------------------------------- #
def test_a_lower_placement_demotes_mastered_to_introduced(env):
    """⭐⭐ *"레벨1로 내려가면 레벨1·2는 배운 흔적은 있는데 **마스터는 안 된 걸로**"*(사장님)

    ⇒ 행을 **지우지 않는다**(배운 흔적은 남는다). 상태·점수만 되돌려 **복습**으로 돈다.
    ⛔ `item_evidence` 는 안 건드린다 — append-only 감사 로그다. 증거는 "그때 이렇게 말했다"는
      **사실**이고, 강등이 그 사실을 지우는 게 아니다.
    """
    db = env["db"]
    m = Member(language="en", korean_level=3, onboarding_completed=True, auth_user_id="auth-dn")
    db.add(m)
    db.flush()
    # 진짜로 올라가며 마스터한 항목(observed) + 아직 연습 중인 항목
    db.add(MemberItemProgress(
        member_id=m.member_id, item_id=env["c1"].item_id, status="mastered",
        score=5.0, provenance="observed",
        repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
        first_seen_at=NOW, last_seen_at=NOW, mastered_at=NOW,
    ))
    db.add(MemberItemProgress(
        member_id=m.member_id, item_id=env["c2"].item_id, status="practicing",
        score=2.0, provenance="observed",
        repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
        first_seen_at=NOW, last_seen_at=NOW,
    ))
    db.flush()

    out = mastery_service.record_placement(db, m.member_id, 1, from_level=3, language="ko")
    db.commit()

    assert out["demoted"] == 1, out
    p1 = _progress(db, m.member_id, env["c1"])
    assert p1.status == "introduced", "마스터가 안 풀렸다 — 복습 대상이 안 된다"
    assert p1.score == 0.0 and p1.mastered_at is None, "점수·마스터 시각이 남아 거짓말한다"
    # ⚠ practicing 은 이미 복습 대상이라 그대로 둔다(건드릴 이유가 없다).
    p2 = _progress(db, m.member_id, env["c2"])
    assert p2.status == "practicing" and p2.score == 2.0


def test_going_up_never_touches_existing_records(env):
    """⛔ *"3에서 5로 올라가면 3 배운 건 그대로 놔두고 4는 0으로 기록해."*(사장님)

    올라갈 때 강등이 걸리면 **진짜로 배운 기록이 날아간다.** 방향을 반드시 본다.
    """
    db = env["db"]
    m = Member(language="en", korean_level=3, onboarding_completed=True, auth_user_id="auth-up")
    db.add(m)
    db.flush()
    db.add(MemberItemProgress(
        member_id=m.member_id, item_id=env["c1"].item_id, status="mastered",
        score=5.0, provenance="observed",
        repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
        first_seen_at=NOW, last_seen_at=NOW, mastered_at=NOW,
    ))
    db.flush()

    out = mastery_service.record_placement(db, m.member_id, 5, from_level=3, language="ko")
    db.commit()

    assert out["demoted"] == 0, out
    p1 = _progress(db, m.member_id, env["c1"])
    assert p1.status == "mastered" and p1.score == 5.0, "올라가는데 기록을 건드렸다"


def test_the_first_placement_has_no_direction_and_demotes_nothing(env):
    """⚠ 첫 배정은 `from_level=None` 이다 — 방향을 모르니 **아무것도 안 한다.**"""
    db = env["db"]
    m = Member(language="en", korean_level=None, onboarding_completed=True, auth_user_id="auth-1st")
    db.add(m)
    db.flush()

    out = mastery_service.record_placement(db, m.member_id, 3, from_level=None, language="ko")
    db.commit()

    assert out == {"mastered": 0, "introduced": 0, "demoted": 0}, out


def test_the_history_row_is_still_the_single_source_of_level_entry(env):
    """⚠ 행 생성을 없앴다고 **이력까지 없애면 안 된다** — "레벨 진입 시각"의 단일 소스다."""
    db = env["db"]
    m = Member(language="en", korean_level=None, onboarding_completed=True, auth_user_id="auth-h")
    db.add(m)
    db.flush()

    mastery_service.record_placement(db, m.member_id, 4, from_level=2, language="ko")
    db.commit()

    hist = mastery_repository.get_latest_history(db, m.member_id)
    assert hist.reason == "placement" and hist.to_level == 4 and hist.from_level == 2


def test_the_old_name_still_points_at_the_same_function():
    """⚠ 이름만 남기고 동작이 다르면 안 된다 — 별칭으로 묶었다."""
    assert mastery_service.apply_grandfathering is mastery_service.record_placement


# --------------------------------------------------------------------------- #
# (7) ⛔ 승급 게이트는 **G1 ∧ G2 뿐** (2026-08-16 사장님 지시 "G4, G5는 빼주라")
# --------------------------------------------------------------------------- #
#
# ⚠ 예전 성질을 **뒤집어** 다시 쓴 것이다(지운 게 아니다). 그리고 대가를 여기 박아 둔다:
#     G4 제거 → **계속 틀려도(F비율이 높아도) 승급한다**
#     G5 제거 → **하루에 여러 레벨을 올라갈 수 있다**(체류 일수 조건이 없다)
# ⚠ D15 는 G5 를 **일부러 남겼었다**("연쇄 승급 방지 안전핀"). 그 안전핀을 뺀 것이므로,
#   연쇄 승급이 실제로 관측되면 여기부터 의심해라.


def _gate_ready_member(env, *, level: int = 1):
    """L1 청크 4개에 **관측 증거**를 채워 G1·G2 를 통과시킨 회원(게이트 판정 직전 상태)."""
    db = env["db"]
    m = Member(language="en", korean_level=level, onboarding_completed=True,
               auth_user_id=f"auth-gate-{level}-{id(env) % 10000}",
               created_at=NOW - timedelta(days=30))
    db.add(m)
    db.flush()
    for item in (env["c1"], env["c2"], env["c3"], env["c4"]):
        db.add(MemberItemProgress(
            member_id=m.member_id, item_id=item.item_id, status="mastered",
            score=5.0, provenance="observed",
            repeat_count=0, prompted_count=0, spontaneous_count=0, miss_count=0,
            first_seen_at=NOW, last_seen_at=NOW, mastered_at=NOW,
        ))
    db.flush()
    return m


def test_promotion_needs_only_g1_and_g2(env):
    """⭐ 승급 조건은 **둘뿐**이다 — G4·G5 가 빠져도 G1·G2 만 채우면 오른다."""
    db = env["db"]
    m = _gate_ready_member(env)
    call = _new_call(env, status="done")
    db.commit()

    r = mastery_service.evaluate_level_up(db, m.member_id, trigger_call_id=call.call_id)
    db.commit()

    assert r["result"] == "promoted" and r["to_level"] == 2, r
    assert r["snapshot"]["g1"]["pass"] and r["snapshot"]["g2"]["pass"]


def test_a_high_failure_ratio_no_longer_blocks(env):
    """⛔ **계속 틀려도 승급한다** — G4 를 뺀 대가다. 그 사실을 성질로 못박는다.

    (예전 성질: "최근 증거통화의 F비율이 문턱 이상이면 stay")
    """
    db = env["db"]
    m = _gate_ready_member(env)
    # 최근 증거통화에 F 를 잔뜩 심는다 — 예전이라면 G4 가 막았을 상태.
    for n in range(3):
        c = Call(member_id=m.member_id, character_id=env["ch"].character_id,
                 call_date=NOW, status="done")
        db.add(c)
        db.flush()
        for item in (env["c1"], env["c2"], env["c3"], env["c4"]):
            db.add(ItemEvidence(
                member_id=m.member_id, language="ko", item_id=item.item_id,
                call_id=c.call_id, grade_raw="F", grade_final="F",
                verified=True, score_delta=-1.0, created_at=NOW,
            ))
    db.flush()
    call = _new_call(env, status="done")
    db.commit()

    r = mastery_service.evaluate_level_up(db, m.member_id, trigger_call_id=call.call_id)
    db.commit()

    assert r["result"] == "promoted", r
    g4 = r["snapshot"]["g4"]
    assert g4["enforced"] is False, "G4 가 아직 판정에 쓰인다"
    assert g4["f_count"] > 0, "이 시험의 전제(F 가 쌓였다)가 깨졌다"


def test_zero_days_in_level_no_longer_blocks(env):
    """⛔ **체류 0일이어도 승급한다** — G5 를 뺀 대가다(연쇄 승급 방지 안전핀이 없다)."""
    db = env["db"]
    m = _gate_ready_member(env)
    # 방금 게이트 승급으로 이 레벨에 들어왔다 = 체류 0일. 예전엔 G5 가 막았다.
    db.add(MemberLevelHistory(
        member_id=m.member_id, language="ko", from_level=0, to_level=1,
        reason="gate_promotion", trigger_call_id=None, created_at=NOW,
    ))
    db.flush()
    call = _new_call(env, status="done")
    db.commit()

    r = mastery_service.evaluate_level_up(db, m.member_id, trigger_call_id=call.call_id)
    db.commit()

    assert r["result"] == "promoted", r
    g5 = r["snapshot"]["g5"]
    assert g5["days"] == 0 and g5["pass"] is False and g5["enforced"] is False, g5


def test_the_removed_gates_are_still_recorded(env):
    """⭐ **관측은 계속한다** — 이게 "빼길 잘했나"를 나중에 되물을 유일한 자료다.

    ⛔ 키를 지우면 과거 스냅샷과 모양이 달라져 비교가 통째로 끊긴다. 그래서 남기고
      `enforced=False` 로 **판정에 안 쓴다는 사실**을 같이 기록한다.
    """
    db = env["db"]
    m = _gate_ready_member(env)
    call = _new_call(env, status="done")
    db.commit()

    snap = mastery_service.evaluate_level_up(
        db, m.member_id, trigger_call_id=call.call_id)["snapshot"]

    for key in ("g4", "g5"):
        assert key in snap, f"{key} 기록이 사라졌다 — 과거 스냅샷과 비교가 끊긴다"
        assert snap[key]["enforced"] is False
        assert "threshold" in snap[key] or "days_required" in snap[key]


def test_the_decision_never_reads_the_removed_gates():
    """⛔⛔ 판정 분기에 **다시 들어가면** 시험이 먼저 실패한다.

    ⚠ 사장님 지시로 뺀 것이다. 되살리려면 그 대가(위 두 시험)를 먼저 읽어라.
    """
    import inspect

    src = inspect.getsource(mastery_service.evaluate_level_up)
    decision = [ln for ln in src.splitlines() if "if not (" in ln and "g1_pass" in ln]
    assert decision, "승급 판정 분기를 못 찾았다(모양이 바뀌었으면 이 시험도 고쳐라)"
    assert decision[0].strip() == "if not (g1_pass and g2_pass):", decision


def test_the_idempotency_guard_does_not_block_the_next_fragment(env):
    """⛔⛔ **재시도 방지 가드가 이어하기 조각2를 막던 것**(2026-08-19 실측 call 1093).

    조각들은 **같은 call_id 를 공유**한다(이어하기 설계). 그래서 `call_id` 단위 가드는
    조각1이 증거를 남기는 순간 조각2를 통째로 스킵한다:
        WARNING 체크판: call_id=1093 증거 기존재 → 스킵(이중 적립 방지)
        체크판: 검출 3→검증 0, 증거 None
    사장님이 조각2에서 정확히 말했는데 진도가 하나도 안 쌓였다.

    ⇒ 조각 범위(turn_index)로 물으면 원래 목적(같은 조각 재분석 차단)은 그대로이면서
      다음 조각은 통과한다.
    """
    from domains.learning.models.item_evidence import ItemEvidence
    from domains.learning.repository import mastery_repository as repo

    db = env["db"]
    call = _new_call(env)
    mid, iid = env["member"].member_id, env["c1"].item_id
    db.add(ItemEvidence(
        member_id=mid, language="ko", item_id=iid, call_id=call.call_id, turn_index=3,
        grade_raw="E2", grade_final="E2", verified=True, score_delta=1.0,
    ))
    db.commit()

    # 같은 조각(turn 0~) 을 다시 분석하면 여전히 막힌다 — 원래 목적.
    assert repo.has_call_evidence(db, call.call_id) is True
    assert repo.has_call_evidence(db, call.call_id, 0) is True

    # ⭐ 다음 조각(turn>=10)에는 아직 증거가 없다 → 통과해야 한다.
    assert repo.has_call_evidence(db, call.call_id, 10) is False

    # 그 조각에서 증거가 생기면 그 조각의 재분석은 막힌다.
    db.add(ItemEvidence(
        member_id=mid, language="ko", item_id=env["c2"].item_id,
        call_id=call.call_id, turn_index=12,
        grade_raw="E2", grade_final="E2", verified=True, score_delta=1.0,
    ))
    db.commit()
    assert repo.has_call_evidence(db, call.call_id, 10) is True
