"""서버 다국어 J2(2026-10-16 PM-DEC-673) — R1 주제 · R2 차시 목록 상황 · R5 통화 상세·결과 요약 · 서버 문구 사전.

규칙: 응답 필드는 추가만 · 새 필드는 번역이 없으면 한국어 원문(null 금지) · 목록 상황은 캐시만(번역 호출 0).
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from core import server_messages
from domains.learning.models.call import Call
from domains.learning.models.cur_text_i18n import CurTextI18n
from domains.learning.service import display_i18n_service as svc
from domains.learning.service.call_service import CallService
from tests.test_display_i18n import FakeClient, _engine_session, _member_with_calls


@pytest.fixture()
def db():
    return _engine_session()


class _NoCall(FakeClient):
    def __init__(self):
        super().__init__(raises=AssertionError("번역을 부르면 안 된다"))


# ---- R1 주제 ----------------------------------------------------------------
def test_topic_translation_reads_preloaded_row_without_call(db):
    db.add(CurTextI18n(kind=svc.KIND_CUR_TOPIC, source="가족과 친척", locale="vi", text="Gia đình và họ hàng"))
    db.commit()
    c = _NoCall()
    assert svc.topic_translation(db, c, "가족과 친척", "vi") == "Gia đình và họ hàng"
    assert c.calls == []


def test_topic_translation_falls_back_to_original_not_null(db):
    assert svc.topic_translation(db, None, "가족과 친척", "ko") == "가족과 친척"   # ko 회원
    assert svc.topic_translation(db, FakeClient(raises=RuntimeError("x")), "가족과 친척", "vi") == "가족과 친척"  # 번역 실패
    assert svc.topic_translation(db, None, None, "vi") is None                    # 주제 없는 레벨1 차시
    assert db.scalars(select(CurTextI18n)).all() == []                             # 실패는 캐시 안 함


def test_topic_rows_do_not_mix_with_situation_kind(db):
    db.add(CurTextI18n(kind=svc.KIND_CUR_SITUATION, source="같은 문구", locale="en", text="situation"))
    db.commit()
    assert svc.topic_translation(db, FakeClient(lambda t, _: ["topic"]), "같은 문구", "en") == "topic"
    kinds = {(r.kind, r.text) for r in db.scalars(select(CurTextI18n))}
    assert kinds == {(svc.KIND_CUR_SITUATION, "situation"), (svc.KIND_CUR_TOPIC, "topic")}


# ---- R2 차시 목록 상황 ------------------------------------------------------
def test_cached_situations_are_cache_only_and_fall_back_to_original(db):
    db.add(CurTextI18n(kind=svc.KIND_CUR_SITUATION, source="인사하기", locale="ja", text="あいさつする"))
    db.commit()
    out = svc.cached_situation_translations(db, ["인사하기", "길 묻기", None, " 인사하기 "], "ja")
    assert out == {"인사하기": "あいさつする", "길 묻기": "길 묻기"}
    assert svc.cached_situation_translations(db, ["인사하기"], "ko") == {"인사하기": "인사하기"}


# ---- R5 통화 상세·결과 ------------------------------------------------------
def test_call_detail_and_result_use_member_language_summary(db):
    mid, (cid,) = _member_with_calls(db, [("주말 근황", "ko")], language="en")
    c = FakeClient(lambda texts, _: [f"EN:{t}" for t in texts])
    svc_ = CallService(db)
    assert svc_.get_call(mid, cid, client=c, locale="en").summary == "EN:주말 근황"
    assert svc_.get_call_result(mid, cid, client=_NoCall(), locale="en").summary == "EN:주말 근황"  # 캐시 재사용
    assert db.get(Call, cid).summary == "주말 근황"                                               # 원문 불변


def test_call_detail_without_locale_or_on_failure_is_original(db):
    mid, (cid,) = _member_with_calls(db, [("주말 근황", "ko")], language="en")
    svc_ = CallService(db)
    assert svc_.get_call(mid, cid).summary == "주말 근황"
    assert svc_.get_call_result(mid, cid, client=FakeClient(raises=RuntimeError("x")), locale="en").summary == "주말 근황"


# ---- 서버 문구 사전 ---------------------------------------------------------
def test_server_message_bundle_has_63_messages_3_labels_29_locales():
    assert len(server_messages.locales()) == 29 and "ko" not in server_messages.locales()
    b = server_messages.bundle("vi")
    assert b["locale"] == "vi" and len(b["messages"]) == 63
    assert set(b["payment_labels"]) == {"subscription", "character", "bundle"}
    assert all(m["text"] and m["text"] != m["ko"] for m in b["messages"])


def test_server_message_bundle_falls_back_to_korean():
    for loc in (None, "ko", "xx"):
        b = server_messages.bundle(loc)
        assert all(m["text"] == m["ko"] for m in b["messages"])
        assert b["payment_labels"]["subscription"] == "구독 결제"


def test_placeholders_survive_translation():
    import re
    data = json.loads(server_messages._PATH.read_text(encoding="utf-8"))
    for m in data["messages"]:
        want = sorted(re.findall(r"\{\d+\}", m["ko"]))
        for loc, text in m["t"].items():
            assert sorted(re.findall(r"\{\d+\}", text)) == want, (m["key"], loc)
