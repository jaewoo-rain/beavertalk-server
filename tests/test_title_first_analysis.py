"""PM-DEC-362(2026-10-04) — 통화 분석 **2단계 분리**(제목 먼저)의 회귀 시험.

## 무엇을 못질하나
통화가 끝나면 앱은 대기 화면에 날짜·통화시간을 먼저 띄운다. 그런데 제목(`call.summary`)은
통화후 분석 1콜(표현·현지인표현·격려·검출 — **실측 6.0초**, thoughts 394)과 **한 커밋**이라
제목 하나 때문에 그 전부를 기다렸다(`_save_analysis`). 그래서 제목만 떼어
경량 콜 1회(출력 1필드·thinking 0 — **실측 1.0~1.3초**)로 먼저 저장한다.

| # | 시험 | 왜 |
|---|---|---|
| 1 | 1단계가 `summary`·`summary_lang` 를 채운다 · `status` 는 **analyzing 그대로** | 상태값 신설 금지가 설계의 핵심 — 앱은 «summary 가 찼나» 로 판단한다 |
| 2 | 2단계가 **이미 있는 제목을 안 덮는다**(일반 + 레벨테스트) | 안 막으면 결과 화면이 열리는 순간 제목이 바뀐다 |
| 3 | 1단계 실패 시 2단계가 **현행처럼** 채운다 | 폴백이 살아 있어야 R5 |
| 4 | **경합** — 2단계가 먼저 끝나면 1단계는 LLM 도 안 부르고 덮지도 않는다 | 짧은 통화에서 실제로 일어난다 |
| 5 | 레벨테스트 경로도 1단계가 돈다(콜타입 무관) | 빼면 레벨테스트만 제목이 늦어 일관성이 깨진다 |
| 6 | 지시문을 **공유**한다 + 모국어 못박기 2줄 | 복제하면 한쪽만 고쳐져 화면마다 제목 톤이 갈린다 |
| 7 | 원가가 `usage_json.title` 몫을 **더한다** | `estimate_call_cost_usd` 가 유일한 원가 입구 |
| 8 | 스키마 **무변화** — 응답 키 집합 그대로 | 테이블·열·DTO 를 늘리지 않는다는 계약 |

근거: docs/plans/2026-10-04-통화분석-2단계분리-제목먼저.md
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.config import Settings
from db.registry import Base  # noqa: F401  (전 모델 import 부수효과)
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.call_raw_data import CallRawData
from domains.learning.realtime import call_session
from domains.learning.service import normalcall_service as svc


# --------------------------------------------------------------------------- #
# 픽스처
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


@pytest.fixture()
def ctx(session_factory):
    db = session_factory()
    voice = Voice(name="V", gender="male")
    db.add(voice)
    db.flush()
    ch = Character(
        name="바바", role="선생님", personality="시크",
        voice_id=voice.voice_id, price=0,
    )
    db.add(ch)
    db.flush()
    m = Member(
        language="en", korean_level=1, onboarding_completed=True,
        auth_user_id="auth-title-first",
    )
    db.add(m)
    db.flush()
    call_id = svc.create_call(db, m.member_id, ch.character_id, "chat")
    db.add(CallRawData(call_id=call_id, role="beaver", turn_index=0,
                       content="주말에 뭐 했어?"))
    db.add(CallRawData(call_id=call_id, role="user", turn_index=1,
                       content="북한산에 등산을 하고 싶어요."))
    db.add(CallRawData(call_id=call_id, role="beaver", turn_index=2,
                       content="등산 좋지! 커피는 어떤 거 좋아해?"))
    # 1단계는 전사만 보고 돈다 — status 는 통화 저장 직후의 실제 값(analyzing)으로 둔다.
    db.get(Call, call_id).status = "analyzing"
    db.commit()
    return {
        "db": db, "session_factory": session_factory,
        "member_id": m.member_id, "character_id": ch.character_id, "call_id": call_id,
    }


class _FakeOut:
    def __init__(self, summary: str) -> None:
        self.summary = summary


def _patch_llm(monkeypatch, summary: str | None, *, record: list | None = None):
    """`gemini_analysis.generate_structured` 를 가짜로 — 제목 1콜만 가로챈다."""
    async def fake(client, model, **kw):
        if record is not None:
            record.append({"model": model, **kw})
        if summary is None:
            return None
        usage = kw.get("usage")
        if usage is not None:
            usage.add_response(model, object())   # usage_metadata 없음 → calls 만 +1
        return _FakeOut(summary)

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", fake)


def _settings() -> Settings:
    """⚠ `_env_file=None` — 이 PC 의 `.env`(= 운영 설정)가 시험 결과를 흔들지 않게 한다."""
    return Settings(
        _env_file=None,
        DATABASE_URL_POOL="postgresql+psycopg2://u:p@localhost:5432/dummy",
    )


# --------------------------------------------------------------------------- #
# 1. 1단계가 제목을 채운다 · status 는 analyzing 그대로
# --------------------------------------------------------------------------- #
def test_stage1_writes_summary_and_lang_but_never_touches_status(ctx, monkeypatch):
    calls: list = []
    _patch_llm(monkeypatch, "Weekend hiking plans", record=calls)

    asyncio.run(svc.build_call_title(
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="en",
    ))

    db = ctx["session_factory"]()
    call = db.get(Call, ctx["call_id"])
    assert call.summary == "Weekend hiking plans"
    assert call.summary_lang == "en"
    # ⛔ 상태값 신설·변경 금지 — 결과 화면 폴링을 푸는 것은 계속 2단계의 done 이다.
    assert call.status == "analyzing"
    # 경량 1콜 계약: 추론 0 · 출력 1필드
    assert len(calls) == 1
    assert calls[0]["thinking_budget"] == 0
    assert calls[0]["schema"] is svc.CallTitle
    assert set(svc.CallTitle.model_fields) == {"summary"}


def test_stage1_uses_the_same_transcript_read_as_the_resume_summary(ctx, monkeypatch):
    """재료는 이어하기 요약과 **같은 읽기**(`_resume_transcript`)다 — 두 벌이면 잘림 규칙이 갈린다."""
    calls: list = []
    _patch_llm(monkeypatch, "Hiking and coffee", record=calls)

    asyncio.run(svc.build_call_title(
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="en",
    ))

    db = ctx["session_factory"]()
    expected = svc._resume_transcript(db, ctx["call_id"])
    assert calls[0]["prompt"] == expected
    assert "학습자: 북한산에 등산을 하고 싶어요." in expected


def test_stage1_skips_when_transcript_is_empty(ctx, monkeypatch):
    calls: list = []
    _patch_llm(monkeypatch, "제목", record=calls)
    db = ctx["session_factory"]()
    db.query(CallRawData).filter(CallRawData.call_id == ctx["call_id"]).delete()
    db.commit()

    asyncio.run(svc.build_call_title(
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="en",
    ))

    assert calls == []          # 전사가 없으면 LLM 을 아예 안 부른다
    db2 = ctx["session_factory"]()
    assert db2.get(Call, ctx["call_id"]).summary is None


# --------------------------------------------------------------------------- #
# 2. 2단계가 이미 있는 제목을 덮지 않는다 (일반 · 레벨테스트)
# --------------------------------------------------------------------------- #
def test_stage2_analysis_does_not_overwrite_an_existing_title(ctx):
    db = ctx["session_factory"]()
    call = db.get(Call, ctx["call_id"])
    call.summary = "Weekend hiking plans"      # 1단계가 먼저 썼다
    call.summary_lang = "en"
    db.commit()

    result = svc.CallAnalysis(
        summary="분석이 만든 다른 제목",
        detected_mode="chat",
        expressions=[],
        feedback="잘했어요.",
    )
    svc._save_analysis(db, ctx["call_id"], result, "ja")

    db2 = ctx["session_factory"]()
    call2 = db2.get(Call, ctx["call_id"])
    assert call2.summary == "Weekend hiking plans"       # ⛔ 안 바뀐다
    assert call2.summary_lang == "en"                    # summary 와 한 쌍으로 유지
    # 나머지 칸은 종전대로 2단계가 쓴다 — 제목만 양보하는 것이다
    assert call2.mode == "chat"
    assert call2.feedback == "잘했어요."
    assert call2.status == "done"


def test_stage2_leveltest_does_not_overwrite_an_existing_title(ctx):
    db = ctx["session_factory"]()
    call_id = svc.create_call(db, ctx["member_id"], ctx["character_id"], "level_test")
    call = db.get(Call, call_id)
    call.summary = "Self introduction and hobbies"
    call.summary_lang = "en"
    db.commit()

    result = svc.LevelAssessment(
        evidence=["안녕하세요"], reasoning="근거", distinct_structures=2,
        band="a1", confidence="medium", sample_quality="sufficient",
        summary="판정관이 만든 다른 제목", feedback_for_learner="좋아요",
    )
    assert svc._save_level_assessment(db, call_id, ctx["member_id"], 2, result, "ja")

    db2 = ctx["session_factory"]()
    call2 = db2.get(Call, call_id)
    assert call2.summary == "Self introduction and hobbies"   # ⛔ 안 바뀐다
    assert call2.summary_lang == "en"
    assert call2.assessed_level == 2                          # 레벨 배정은 그대로 돈다
    assert call2.status == "done"


def test_leveltest_sparse_path_no_longer_blanks_a_good_title(ctx):
    """⭐ 덤 — 표본 미달 경로는 `summary=""`·locale 없음으로 들어와 **멀쩡한 제목을 지웠다**."""
    db = ctx["session_factory"]()
    call_id = svc.create_call(db, ctx["member_id"], ctx["character_id"], "level_test")
    call = db.get(Call, call_id)
    call.summary = "Greetings practice"
    call.summary_lang = "en"
    db.commit()

    sparse = svc.LevelAssessment(
        evidence=[], reasoning="표본 미달", distinct_structures=0,
        band="unknown", confidence="low", sample_quality="none",
        summary="", feedback_for_learner="",
    )
    assert svc._save_level_assessment(db, call_id, ctx["member_id"], 1, sparse)

    db2 = ctx["session_factory"]()
    call2 = db2.get(Call, call_id)
    assert call2.summary == "Greetings practice"
    assert call2.summary_lang == "en"        # NULL 로 되돌아가지 않는다


# --------------------------------------------------------------------------- #
# 3. 1단계 실패 → 2단계가 현행처럼 채운다
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("mode", ["llm_none", "llm_raises", "blank_summary"])
def test_stage1_failure_leaves_the_title_to_stage2(ctx, monkeypatch, mode):
    if mode == "llm_raises":
        async def boom(*a, **kw):
            raise RuntimeError("LLM down")
        monkeypatch.setattr(svc.gemini_analysis, "generate_structured", boom)
    else:
        _patch_llm(monkeypatch, None if mode == "llm_none" else "   ")

    # 1단계는 조용히 실패해야 한다(예외가 밖으로 새면 fire-and-forget 로그가 더러워진다)
    asyncio.run(svc.build_call_title(
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="en",
    ))
    db = ctx["session_factory"]()
    assert db.get(Call, ctx["call_id"]).summary is None
    assert db.get(Call, ctx["call_id"]).status == "analyzing"   # 상태도 안 건드렸다

    # 2단계는 종전 그대로 제목을 쓴다
    svc._save_analysis(
        db, ctx["call_id"],
        svc.CallAnalysis(summary="Hiking and coffee", detected_mode="chat",
                         expressions=[], feedback="좋아요"),
        "en",
    )
    db2 = ctx["session_factory"]()
    assert db2.get(Call, ctx["call_id"]).summary == "Hiking and coffee"
    assert db2.get(Call, ctx["call_id"]).summary_lang == "en"


def test_stage1_never_raises_even_if_the_db_write_explodes(ctx, monkeypatch):
    _patch_llm(monkeypatch, "제목")
    monkeypatch.setattr(
        svc, "_save_call_title",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    asyncio.run(svc.build_call_title(          # 예외가 밖으로 새면 여기서 터진다
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="en",
    ))


def test_stage1_is_a_noop_without_a_genai_client(ctx, monkeypatch):
    """R5 — 키 없으면 `client` 가 None 이다. 제목만 비활성, 예외 0."""
    calls: list = []
    _patch_llm(monkeypatch, "제목", record=calls)
    asyncio.run(svc.build_call_title(
        ctx["call_id"], None, _settings(), ctx["session_factory"], locale="en",
    ))
    assert calls == []


# --------------------------------------------------------------------------- #
# 4. 경합 — 2단계가 먼저 끝난 경우
# --------------------------------------------------------------------------- #
def test_stage2_winning_the_race_means_stage1_spends_nothing_and_changes_nothing(
    ctx, monkeypatch,
):
    """⭐ 짧은 통화에서 분석이 먼저 끝나는 경우 — 1단계는 **LLM 도 안 부르고** 안 덮는다.

    게이트는 `status=done` 이다. 2단계는 제목과 `done` 을 **한 커밋**으로 쓰므로
    done 은 «결과 화면이 열렸다» 와 동의어고, 그 뒤로는 제목이 흔들리면 안 된다.
    """
    db = ctx["session_factory"]()
    svc._save_analysis(
        db, ctx["call_id"],
        svc.CallAnalysis(summary="분석이 먼저 쓴 제목", detected_mode="chat",
                         expressions=[], feedback="좋아요"),
        "en",
    )
    assert db.get(Call, ctx["call_id"]).status == "done"
    calls: list = []
    _patch_llm(monkeypatch, "제목 태스크가 늦게 만든 것", record=calls)

    asyncio.run(svc.build_call_title(
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="ja",
    ))

    assert calls == []                       # 돈을 안 쓴다
    db2 = ctx["session_factory"]()
    call = db2.get(Call, ctx["call_id"])
    assert call.summary == "분석이 먼저 쓴 제목"
    assert call.summary_lang == "en"


def test_stage1_rewrites_its_own_title_on_the_next_fragment(ctx, monkeypatch):
    """⛔⛔ **이 시험이 QA 에서 뒤집은 설계를 지킨다.**

    `_trigger_analysis` 는 통화 1건에 **조각마다** 다시 돈다(이어하기). 게이트를
    «제목이 이미 있나» 로 두면 15분 통화의 제목이 **처음 5분**만 설명한다 —
    조각2 의 1단계도 2단계도 «있다» 고 건너뛰기 때문이다(종전엔 조각2 분석이 전사
    전체로 다시 썼다). `status` 기준이라 조각2 에서 통화 전체로 다시 쓴다.
    """
    db = ctx["session_factory"]()
    calls: list = []
    _patch_llm(monkeypatch, "Weekend hiking plans", record=calls)

    # ── 조각1 끝 (status=analyzing) ──
    asyncio.run(svc.build_call_title(
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="en",
    ))
    assert db.get(Call, ctx["call_id"]) is not None
    db1 = ctx["session_factory"]()
    assert db1.get(Call, ctx["call_id"]).summary == "Weekend hiking plans"

    # ── 사용자가 «이어서» 를 눌렀다 → resume_call 이 status 를 ongoing 으로 되돌린다 ──
    call = db1.get(Call, ctx["call_id"])
    call.status = "ongoing"
    db1.add(CallRawData(call_id=ctx["call_id"], role="user", turn_index=3,
                        content="요리 이야기도 하고 싶어요. 김치찌개를 만들었어요."))
    db1.commit()
    # ── 조각2 끝 → _persist_remaining 이 analyzing 으로 되돌린다 ──
    call.status = "analyzing"
    db1.commit()

    _patch_llm(monkeypatch, "Hiking and cooking kimchi stew", record=calls)
    asyncio.run(svc.build_call_title(
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="en",
    ))

    db2 = ctx["session_factory"]()
    assert db2.get(Call, ctx["call_id"]).summary == "Hiking and cooking kimchi stew"
    # 조각2 의 1단계는 **두 조각 전사 전체**를 봤다(조각 필터 없음)
    assert "김치찌개를 만들었어요" in calls[-1]["prompt"]
    assert "북한산에 등산을 하고 싶어요" in calls[-1]["prompt"]


def test_save_call_title_gate_is_status_done_not_the_presence_of_a_title(ctx):
    """커밋 직전 재확인이 마지막 그물이다 — 읽고 쓰는 사이에 2단계가 끝날 수 있다."""
    db = ctx["session_factory"]()
    assert svc._save_call_title(db, ctx["call_id"], "첫 제목", "en") is True
    # 아직 analyzing → 다음 조각의 1단계는 **다시 쓸 수 있다**
    assert svc._save_call_title(db, ctx["call_id"], "둘째 제목", "ja") is True
    db2 = ctx["session_factory"]()
    assert db2.get(Call, ctx["call_id"]).summary == "둘째 제목"
    assert db2.get(Call, ctx["call_id"]).summary_lang == "ja"
    # done 이 찍히면 아무도 못 바꾼다
    call = db2.get(Call, ctx["call_id"])
    call.status = "done"
    db2.commit()
    assert svc._save_call_title(db2, ctx["call_id"], "셋째 제목", "en") is False
    assert db2.get(Call, ctx["call_id"]).summary == "둘째 제목"


def test_save_call_title_is_silent_when_the_call_row_is_gone(ctx):
    db = ctx["session_factory"]()
    assert svc._save_call_title(db, 999_999, "제목", "en") is False


# --------------------------------------------------------------------------- #
# 5. 띄우는 자리 — 콜타입 무관(레벨테스트 포함)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("call_type", ["chat", "level_test", "expression", "freetalk"])
def test_trigger_analysis_spawns_a_title_task_for_every_call_type(monkeypatch, call_type):
    """분석·이어하기 요약을 띄우는 그 자리에서 제목 태스크도 **나란히** 뜬다.

    ⛔ 레벨테스트를 빼지 않는다 — 같은 전사·같은 «핵심 소재 명사구» 이고, 앱은 통화
      종류로 화면을 가르지 않는다(빼면 레벨테스트만 제목이 늦는다).
    """
    seen: dict = {}

    async def fake_title(call_id, client, settings_obj, factory, **kw):
        seen["title"] = {"call_id": call_id, **kw}

    async def fake_noop(*a, **kw):
        return None

    monkeypatch.setattr(svc, "build_call_title", fake_title)
    monkeypatch.setattr(svc, "analyze_call", fake_noop)
    monkeypatch.setattr(svc, "analyze_level_test_call", fake_noop)
    monkeypatch.setattr(svc, "build_resume_context", fake_noop)

    async def drive():
        call_session._trigger_analysis(
            77, object(), _settings(), object(), "en",
            target_language="한국어", locale_label=None,
            call_type=call_type, member_id=5,
        )
        names = {t.get_name() for t in call_session._analysis_tasks}
        assert "normalcall-title-77" in names
        # 보관소를 비우고 나간다(전역 집합이라 다른 시험으로 새면 안 된다)
        await asyncio.gather(*list(call_session._analysis_tasks))
        await asyncio.sleep(0)

    asyncio.run(drive())
    assert seen["title"]["call_id"] == 77
    assert seen["title"]["locale"] == "en"
    assert seen["title"]["target_language"] == "한국어"


def test_title_task_is_strongly_referenced_and_cleaned_up(monkeypatch):
    """GC 방지 강참조 보관소 + done 콜백 — 이어하기 요약 태스크와 **같은 패턴**이어야 한다."""
    async def fake_noop(*a, **kw):
        return None

    monkeypatch.setattr(svc, "build_call_title", fake_noop)
    monkeypatch.setattr(svc, "analyze_call", fake_noop)
    monkeypatch.setattr(svc, "build_resume_context", fake_noop)

    async def drive():
        call_session._trigger_analysis(
            88, object(), _settings(), object(), "en", call_type="chat", member_id=1,
        )
        tasks = [t for t in call_session._analysis_tasks
                 if t.get_name() == "normalcall-title-88"]
        assert len(tasks) == 1
        await asyncio.gather(*list(call_session._analysis_tasks))
        await asyncio.sleep(0)
        assert not [t for t in call_session._analysis_tasks
                    if t.get_name() == "normalcall-title-88"]

    asyncio.run(drive())


# --------------------------------------------------------------------------- #
# 6. 지시문 — 복제가 아니라 공유
# --------------------------------------------------------------------------- #
def test_the_summary_rule_is_one_object_shared_by_both_stages():
    """⛔ 복제 금지. 두 벌이면 한쪽만 고쳐져 **제목 톤이 경로마다 갈린다**."""
    for locale, target in [("en", "한국어"), ("ja", "영어"), ("vi", "한국어")]:
        label = svc._LOCALE_LABEL.get(locale, svc._LOCALE_LABEL["en"])
        rule = svc._summary_field_rule(label)
        assert rule in svc._analysis_instruction(locale, target)
        assert rule in svc._title_instruction(locale, target)


def test_the_shared_summary_rule_keeps_its_three_examples():
    """prompts/README §3 원칙 4 — 모델은 규칙과 예시가 다르면 예시를 따른다.
    요청서가 가리킨 레벨테스트 판정관 규칙(`_leveltest_instruction`)엔 **예시가 없다** —
    그래서 그쪽이 아니라 분석 규칙을 공유한다."""
    rule = svc._summary_field_rule("영어(English)")
    assert "주어·서술어 없이 2~4어절 이내" in rule
    for ex in ["강아지 산책과 음악 취향", "주말 여행 계획", "좋아하는 한국 음식"]:
        assert ex in rule
    assert rule.endswith("\n")


def test_title_instruction_pins_the_learner_language_twice():
    """⛔ 실측으로 들어온 2줄이다 — 공유 규칙만으로는 제목이 **한국어로** 나왔다
    (en: `'-고 싶어요'와 '-아/어 보세요' 배우기` → 못박은 뒤 `Weekend hiking plans`)."""
    instr = svc._title_instruction("en", "한국어")
    label = svc._LOCALE_LABEL["en"]
    assert f"학습자의 언어는 {label} 다" in instr
    assert f"**{label} 로만** 쓴다" in instr
    assert "한국어 로 가득해도" in instr
    # 멀티랭귀지 — 라벨이 박혀 있지 않다(일본어 학습자에게 영어라고 말하지 않는다)
    ja = svc._title_instruction("ja", "한국어")
    assert svc._LOCALE_LABEL["ja"] in ja
    assert svc._LOCALE_LABEL["en"] not in ja


def test_title_instruction_asks_for_nothing_but_the_title():
    instr = svc._title_instruction("en", "한국어")
    assert "summary 외의 어떤 것도 출력하지 마라" in instr
    for forbidden in ["detected_mode", "expressions", "native_expression", "feedback"]:
        assert forbidden not in instr


def test_title_model_falls_back_to_judge_model_when_unset():
    """⛔ 기본값이 `""` 인 이유는 **실측**이다(2026-10-04): `gemini-2.5-flash-lite` 는 404,
    `gemini-3.5-flash-lite`·`gemini-flash-lite-latest` 는 `thinking_budget=0` 에 400 이다.
    env 로 내리려면 `LLM_TOKEN_PRICE_USD` 단가 행을 같이 넣어야 한다."""
    assert Settings.model_fields["TITLE_MODEL"].default == ""
    s = _settings()
    assert svc._title_model(s) == s.JUDGE_MODEL
    s2 = _settings()
    s2.TITLE_MODEL = "  gemini-2.5-flash  "
    assert svc._title_model(s2) == "gemini-2.5-flash"     # 공백은 깎는다


# --------------------------------------------------------------------------- #
# 7. 원가 — 곁가지 몫으로 더해진다
# --------------------------------------------------------------------------- #
def test_title_usage_is_registered_as_a_side_cost_key():
    assert "title" in svc.SIDE_LLM_KEYS
    assert "title" in svc.SIDE_USAGE_KEYS
    # ⛔ analysis 와 키를 공유하면 add_call_usage_extra 가 서로를 덮는다
    assert "title" != "analysis"


def test_estimate_call_cost_adds_the_title_leg():
    entry = {"vendor": "gemini-2.5-flash", "calls": 1,
             "in_text": 703, "out_text": 8, "thoughts": 0}
    without, _ = svc.estimate_call_cost_usd("live:x", usage_json={})
    with_title, unknown = svc.estimate_call_cost_usd(
        "live:x", usage_json={"title": entry}
    )
    assert unknown == []
    expected = (703 * 0.30 + 8 * 2.50) / 1_000_000
    assert with_title - without == pytest.approx(expected, rel=1e-9)
    # 분석 몫과 **같이** 더해진다(한쪽이 다른 쪽을 가리지 않는다)
    both, _ = svc.estimate_call_cost_usd(
        "live:x", usage_json={"title": entry, "analysis": entry}
    )
    assert both - without == pytest.approx(expected * 2, rel=1e-9)


def test_title_usage_lands_on_its_own_key(ctx, monkeypatch):
    _patch_llm(monkeypatch, "Weekend hiking plans")
    db = ctx["session_factory"]()
    svc.add_call_usage_extra(
        db, ctx["call_id"], "analysis",
        {"vendor": "gemini-2.5-flash", "calls": 1, "in_text": 1214,
         "out_text": 857, "thoughts": 394},
    )

    asyncio.run(svc.build_call_title(
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="en",
    ))

    db2 = ctx["session_factory"]()
    usage = db2.get(Call, ctx["call_id"]).usage_json or {}
    assert "title" in usage and "analysis" in usage        # 서로를 지우지 않는다
    assert usage["analysis"]["in_text"] == 1214


def test_title_usage_is_recorded_even_when_the_result_is_unusable(ctx, monkeypatch):
    """⛔ 응답을 받은 시점에 과금은 이미 끝났다 — 파싱이 비어도 계기판에 남아야 한다."""
    async def fake(client, model, **kw):
        usage = kw.get("usage")
        if usage is not None:
            usage.add_response(model, object())
        return None

    monkeypatch.setattr(svc.gemini_analysis, "generate_structured", fake)
    asyncio.run(svc.build_call_title(
        ctx["call_id"], object(), _settings(), ctx["session_factory"], locale="en",
    ))
    db = ctx["session_factory"]()
    call = db.get(Call, ctx["call_id"])
    assert (call.usage_json or {}).get("title", {}).get("calls") == 1
    assert call.summary is None


# --------------------------------------------------------------------------- #
# 8. 스키마 무변화 — 응답 키 집합이 그대로다
# --------------------------------------------------------------------------- #
def test_response_key_sets_are_unchanged():
    """테이블·열·DTO 를 늘리지 않는다는 계약(요청서: 「테이블·열 추가 없음」)."""
    from domains.learning.schemas.call import CallDetail, CallResult, CallSummary

    assert set(CallSummary.model_fields) == {
        "call_id", "call_date", "total_time", "summary", "rating", "character",
    }
    assert set(CallDetail.model_fields) == set(CallSummary.model_fields) | {"sentences"}
    assert set(CallResult.model_fields) == {
        "call_id", "summary", "feedback", "rating", "average", "sentences",
        "used_items", "quiz_items",
    }


def test_the_read_paths_expose_the_title_while_status_is_still_analyzing(ctx):
    """⭐⭐ **이 시험이 기능 전체를 지킨다.** 1단계가 제목을 써도 읽는 길이 `status=done`
    을 요구하면 앱은 그 제목을 못 본다 — 그러면 분리한 의미가 0 이다.

    확인 지점 3곳 모두 **status 게이트가 없어야 한다**:
      `GET /calls/{id}/result`(CallService.get_call_result) · `GET /calls/{id}` ·`GET /calls`
    ⛔ 나중에 «목록은 done 만» 같은 최적화를 넣으면 여기서 죽는다. 그게 이 시험의 일이다.
    """
    from domains.learning.repository.call_repository import CallRepository
    from domains.learning.service.call_service import CallService

    db = ctx["session_factory"]()
    call = db.get(Call, ctx["call_id"])
    call.summary = "Weekend hiking plans"
    call.summary_lang = "en"
    db.commit()
    assert db.get(Call, ctx["call_id"]).status == "analyzing"

    result = CallService(db).get_call_result(ctx["member_id"], ctx["call_id"])
    assert result.summary == "Weekend hiking plans"

    repo = CallRepository(db)
    assert repo.get_detail(ctx["call_id"]).summary == "Weekend hiking plans"
    listed = {c.call_id: c for c in repo.list_by_member(ctx["member_id"])}
    assert listed[ctx["call_id"]].summary == "Weekend hiking plans"


def test_status_polling_payload_is_unchanged(ctx):
    """⛔ 상태값 신설 금지 — `/status` 는 키도 값도 그대로다(앱은 summary 로 판단한다)."""
    db = ctx["session_factory"]()
    detail = svc.get_status_detail(db, ctx["call_id"], ctx["member_id"])
    assert set(detail) == {"status", "call_type", "assessed_level"}
    assert detail["status"] == "analyzing"


def test_call_model_gained_no_new_column():
    cols = {c.name for c in Call.__table__.columns}
    assert "summary" in cols and "summary_lang" in cols
    for invented in ["title", "title_status", "summary_status", "title_lang"]:
        assert invented not in cols
