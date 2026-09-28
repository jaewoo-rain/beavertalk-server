"""§6 차시 제목 · §10 통화 제목 다국어 — 요청 시 번역 + 캐시 (2026-09-29, 외부 의존 0).

문서: docs/20260929_0050_차시제목-통화제목-다국어-요청시번역.md

검증:
  A. core.text_translate — 순서·개수 보존, 개수 불일치·예외·client None → None, 타임아웃 전달.
  B. 요약 언어 규칙 — 분석 지시문의 LOCALE_LABEL 폴백(표 밖 → en)과 같게 저장.
  C. §6 situation_translation — ko → null, 캐시 우선(0콜), 미스 → 번역·저장(ko 행 없음), 실패 → null·미저장.
  D. §10 localized_summaries / list_calls — 같은 언어면 0콜, 다르면 번역·캐시(B2B 공용 표),
     미스분은 **1콜로 묶음**, 실패·표 없음 → 원문(목록 정상), 원문 불변.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core import text_translate
from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.commerce.models.character import Character
from domains.commerce.models.voice import Voice
from domains.learning.models.call import Call
from domains.learning.models.call_summary_translation import (
    b2b_shared_metadata,
    call_summary_translation as cst,
)
from domains.learning.models.cur_text_i18n import CurTextI18n
from domains.learning.service import display_i18n_service as svc
from domains.learning.service.call_service import CallService


# --------------------------------------------------------------------------- #
# 가짜 genai 클라이언트 — client.models.generate_content(...) 만 흉내
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, items):
        self.parsed = text_translate._Out(items=items)
        self.text = None


class FakeClient:
    def __init__(self, fn=None, *, raises=None):
        self.calls: list[dict] = []
        self._fn = fn or (lambda texts, lang: [f"[{lang}] {t}" for t in texts])
        self._raises = raises
        self.models = self

    def generate_content(self, *, model, contents, config):
        texts = [line.split(". ", 1)[1] for line in contents.split("\n")]
        self.calls.append({"model": model, "texts": texts, "config": config})
        if self._raises:
            raise self._raises
        return _Resp(self._fn(texts, config.system_instruction))


def _engine_session(with_b2b_table: bool = True):
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    for pk in cst.primary_key.columns:
        pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    if with_b2b_table:
        b2b_shared_metadata.create_all(engine)  # 운영엔 B2B 가 만든 표가 이미 있다
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


@pytest.fixture()
def db():
    return _engine_session()


def _member_with_calls(db, summaries: list[tuple[str | None, str | None]], language="en"):
    v = Voice(name=f"V{db.query(Voice).count()}", gender="f"); db.add(v); db.flush()
    ch = Character(name="Baba", role="r", personality="p", voice_id=v.voice_id, price=0)
    db.add(ch); db.flush()
    m = Member(language=language, onboarding_completed=True, auth_user_id=f"a{db.query(Member).count()}")
    db.add(m); db.flush()
    ids = []
    for i, (summary, lang) in enumerate(summaries):
        c = Call(member_id=m.member_id, character_id=ch.character_id, status="done",
                 call_date=datetime(2026, 9, 1 + i, tzinfo=timezone.utc),
                 summary=summary, summary_lang=lang)
        db.add(c); db.flush()
        ids.append(c.call_id)
    db.commit()
    return m.member_id, ids


# =========================================================================== #
# A. core.text_translate
# =========================================================================== #
def test_translate_keeps_order_and_passes_timeout():
    c = FakeClient()
    out = text_translate.translate_texts(c, "m", ["가", "나"], "en", timeout_s=2.5)
    assert out == [f"[{c.calls[0]['config'].system_instruction}] 가",
                   f"[{c.calls[0]['config'].system_instruction}] 나"]
    assert c.calls[0]["config"].http_options.timeout == 2500
    assert "English" in c.calls[0]["config"].system_instruction


@pytest.mark.parametrize("client", [None, FakeClient(raises=RuntimeError("down")),
                                    FakeClient(lambda texts, _: ["only one"])])
def test_translate_failures_return_none(client):
    assert text_translate.translate_texts(client, "m", ["가", "나"], "en", timeout_s=1) is None


def test_language_name_for_unlisted_locale_uses_iso_code():
    assert text_translate.language_name("ja") == "日本語"
    assert "ne" in text_translate.language_name("ne")


# =========================================================================== #
# B. 요약 언어 · 표시 언어
# =========================================================================== #
@pytest.mark.parametrize("locale,expected", [
    ("en", "en"), ("ko-KR", "ko"), ("vi", "vi"), ("ne", "en"), (None, "en"), ("", "en"),
])
def test_summary_lang_follows_analysis_label_fallback(locale, expected):
    assert svc.summary_lang_for(locale) == expected


@pytest.mark.parametrize("lang,expected", [("en-US", "en"), ("KO", "ko"), (None, "en"), ("", "en")])
def test_display_locale(lang, expected):
    assert svc.display_locale(lang) == expected


def test_save_analysis_records_summary_lang(db):
    """분석 저장이 summary 와 같은 커밋에 summary_lang 을 남긴다(표 밖 locale 은 en)."""
    from domains.learning.service.normalcall_service import _CallAnalysisBase, _save_analysis

    _, (cid,) = _member_with_calls(db, [(None, None)])
    result = _CallAnalysisBase(summary="Weekend plans", detected_mode="chat", expressions=[])
    _save_analysis(db, cid, result, "ne")
    c = db.get(Call, cid)
    assert (c.summary, c.summary_lang) == ("Weekend plans", "en")


# =========================================================================== #
# C. §6 situation_translation
# =========================================================================== #
SIT = "처음 만난 사람과 인사하고 자기를 소개하기"


def test_situation_ko_member_is_null_and_no_call(db):
    c = FakeClient()
    assert svc.situation_translation(db, c, SIT, "ko") is None
    assert c.calls == []
    assert db.query(CurTextI18n).count() == 0


def test_situation_miss_translates_and_caches_then_hits(db):
    c = FakeClient(lambda texts, _: ["Greet someone new and introduce yourself"])
    assert svc.situation_translation(db, c, SIT, "en") == "Greet someone new and introduce yourself"
    row = db.query(CurTextI18n).one()
    assert (row.kind, row.source, row.locale) == ("cur_situation", SIT, "en")

    c2 = FakeClient(raises=AssertionError("캐시가 있는데 또 번역했다"))
    assert svc.situation_translation(db, c2, SIT, "en") == "Greet someone new and introduce yourself"
    assert c2.calls == []


def test_situation_existing_curated_row_wins(db):
    """사람이 고쳐 넣은 행이 있으면 그대로 쓴다(덮어쓰지 않는다)."""
    db.add(CurTextI18n(kind="cur_situation", source=SIT, locale="ja", text="検収済み"))
    db.commit()
    assert svc.situation_translation(db, FakeClient(), SIT, "ja") == "検収済み"


@pytest.mark.parametrize("client", [None, FakeClient(raises=TimeoutError("slow"))])
def test_situation_failure_is_null_and_not_cached(db, client):
    assert svc.situation_translation(db, client, SIT, "en") is None
    assert db.query(CurTextI18n).count() == 0


# =========================================================================== #
# D. §10 통화 목록 요약
# =========================================================================== #
def _cached(db):
    return {(r.call_id, r.locale): r.text for r in db.execute(select(cst)).all()}


def test_same_language_summary_untouched_and_no_call(db):
    mid, _ = _member_with_calls(db, [("Weekend plans", "en"), ("주말 근황", None)], language="en")
    c = FakeClient(lambda texts, _: [f"EN:{t}" for t in texts])
    out = CallService(db).list_calls(mid, locale="en", client=c)
    assert [s.summary for s in out] == ["EN:주말 근황", "Weekend plans"]  # 최신순
    assert len(c.calls) == 1 and c.calls[0]["texts"] == ["주말 근황"]


def test_mixed_list_is_unified_in_one_batched_call_and_cached(db):
    """⭐ 요청서 증상 — 한국어 UI 인데 영어 제목이 섞인 목록. 미스분은 1콜로 묶는다."""
    mid, ids = _member_with_calls(db, [
        ("자기소개와 이름 묻기", "ko"),
        ("Introduction phrase", "en"),
        ("Basic Korean greetings", None),   # 과거 행(언어 미기록, 한글 없음)
        ("주말 근황", None),                 # 과거 행(한글) → ko 로 판정, 번역 안 함
    ], language="ko")
    c = FakeClient(lambda texts, _: [f"KO:{t}" for t in texts])
    out = {s.call_id: s.summary for s in CallService(db).list_calls(mid, locale="ko", client=c)}
    assert out == {ids[0]: "자기소개와 이름 묻기", ids[1]: "KO:Introduction phrase",
                   ids[2]: "KO:Basic Korean greetings", ids[3]: "주말 근황"}
    assert len(c.calls) == 1, "미스분을 한 콜로 묶지 않았다"
    assert _cached(db) == {(ids[1], "ko"): "KO:Introduction phrase",
                           (ids[2], "ko"): "KO:Basic Korean greetings"}
    # 원문 불변
    assert db.get(Call, ids[1]).summary == "Introduction phrase"

    c2 = FakeClient(raises=AssertionError("캐시가 있는데 또 번역했다"))
    out2 = {s.call_id: s.summary for s in CallService(db).list_calls(mid, locale="ko", client=c2)}
    assert out2 == out and c2.calls == []


def test_only_the_requested_page_is_translated(db):
    """⛔ 전체 이력을 한 번에 돌리지 않는다 — limit 분량만."""
    mid, _ = _member_with_calls(db, [(f"title {i}", "en") for i in range(5)], language="de")
    c = FakeClient()
    CallService(db).list_calls(mid, limit=2, offset=0, locale="de", client=c)
    assert len(c.calls) == 1 and len(c.calls[0]["texts"]) == 2


@pytest.mark.parametrize("client", [None, FakeClient(raises=TimeoutError("slow"))])
def test_translation_failure_returns_original(db, client):
    mid, _ = _member_with_calls(db, [("Weekend plans", "en")], language="ko")
    out = CallService(db).list_calls(mid, locale="ko", client=client)
    assert [s.summary for s in out] == ["Weekend plans"]
    assert _cached(db) == {}


def test_missing_shared_table_does_not_break_list():
    """B2B 표가 없는 DB(로컬 dev 등)여도 목록은 원문으로 정상."""
    db = _engine_session(with_b2b_table=False)
    mid, _ = _member_with_calls(db, [("Weekend plans", "en")], language="ko")
    out = CallService(db).list_calls(mid, locale="ko", client=FakeClient())
    assert [s.summary for s in out] == ["Weekend plans"]


def test_b2b_cached_row_is_reused(db):
    """B2B 콘솔이 만든 (call_id, en) 번역이 있으면 앱도 그대로 쓴다 — 같은 개체다."""
    mid, (cid,) = _member_with_calls(db, [("주말 근황", "ko")], language="en")
    db.execute(cst.insert().values(call_id=cid, locale="en", text="Weekend catch-up", source_locale="ko"))
    db.commit()
    out = CallService(db).list_calls(mid, locale="en", client=FakeClient(raises=AssertionError("x")))
    assert [s.summary for s in out] == ["Weekend catch-up"]


def test_list_without_locale_is_unchanged(db):
    """locale 을 안 주는 옛 호출부는 종전과 동일(번역 경로 자체를 안 탄다)."""
    mid, _ = _member_with_calls(db, [("Weekend plans", "en")], language="ko")
    assert [s.summary for s in CallService(db).list_calls(mid)] == ["Weekend plans"]
