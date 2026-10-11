# -*- coding: utf-8 -*-
"""프롬프트 DB override — **배관이 깨지는 것만** 막는 검사의 계약(2026-10-10 사장님 지시).

## 왜 이 파일이 중요한가
글자가 DB 로 가면 **pytest 가 그 글자를 못 본다.** 오늘만 시험이 세 번 사고를 잡았다
(쪽지 닻이 사라진 블록을 가리킨 것 · `[이번 차시]` 이름 계약 · 종료 쪽지 언어).
그 구멍을 런타임 검사(`core.openai.prompts.override.vet`)가 메우고, **이 파일이 그 검사를
지킨다.** 검사가 느슨해지면 여기서 먼저 깨져야 한다.

## ⛔ 공통 스키마는 없다
네 대본이 **다 쓰는 슬롯 0개, 라벨 0개**다(측정 2026-10-10). 그래서 검사는 늘
「그 대본 vs **그 대본의** 코드 기본값」이다 — 대본끼리 비교하지 않는다.
"""
from __future__ import annotations

import pytest

from core.openai.prompts import expression as ex
from core.openai.prompts.override import MAX_LEN_FACTOR, vet


def _default() -> str:
    return ex.SCRIPT_TEMPLATE


# --------------------------------------------------------------------------- #
# 통과해야 하는 것 — 사장님이 **말을 고치는** 일상
# --------------------------------------------------------------------------- #
def test_wording_change_passes():
    """⭐ 이게 이 기능의 목적이다 — 슬롯·라벨을 안 건드리고 문장만 고치면 통과한다."""
    body = _default().replace("순서대로 하나씩 가르친다", "번호 순서대로 하나씩 꼼꼼히 가르친다")
    ok, why = vet("expression", body, _default())
    assert ok, why


def test_the_default_itself_passes():
    """기본값이 자기 검사를 통과하지 못하면 그건 배포 오류다."""
    ok, why = vet("expression", _default(), _default())
    assert ok, why


# --------------------------------------------------------------------------- #
# 막아야 하는 것 — 넷
# --------------------------------------------------------------------------- #
def test_slot_typo_is_rejected():
    """⛔⛔ 슬롯 오타 하나가 `%` 포맷을 깨뜨려 **통화가 열리기도 전에 죽는다.**"""
    ok, why = vet("expression", _default().replace("%(locale)s", "%(locale_label)s"), _default())
    assert not ok and "슬롯" in why, why


def test_missing_slot_is_rejected():
    """⛔ 슬롯이 빠지면 그 자리에 값이 안 들어간다 — 비버가 빈 지시를 받는다."""
    body = _default().replace("%(group)d개를 가르칠 때마다 그 %(group)d개를 하나씩 "
                              "다시 물어 복습한 뒤 다음 %(group)d개로 간다.\n", "")
    ok, why = vet("expression", body, _default())
    assert not ok and "슬롯" in why, why


def test_new_bracket_label_is_rejected():
    """⛔⛔ 대본은 `[오늘의 표현]` 을 **이름으로** 가리키고 그 블록은 코드가 만든다.

    이름을 바꾸면 대본이 **없는 블록**을 가리킨다 — 2026-10-09 에 쪽지 닻이 사라진 블록을
    가리켜 같은 사고가 났다(그때는 시험이 잡았다. DB 글자는 시험이 못 본다).
    """
    ok, why = vet("expression", _default() + "\n[새 블록]\n여기 뭔가", _default())
    assert not ok and "라벨" in why, why


def test_more_close_vocabulary_is_rejected():
    """⛔ 종료 어휘를 **늘리면** 막는다 — 종료 개념을 가르친 과거 3건(call 706·852·870)이
    전부 모델이 스스로 통화를 끊는 사고로 끝났다.

    ⚠ 「늘지 않았다」만 본다 — 기본값이 이미 쓰는 만큼(전문의 「서버가 종료를 알릴 때까지」)은
      허용해야 한다. 그렇지 않으면 기본값 자체가 거부된다.
    """
    ok, why = vet("expression", _default() + "\n이제 마무리하고 작별 인사를 해라.", _default())
    assert not ok and "종료 어휘" in why, why


def test_too_long_is_rejected():
    ok, why = vet("expression", _default() + ("가" * (len(_default()) * MAX_LEN_FACTOR)), _default())
    assert not ok and "길다" in why, why


@pytest.mark.parametrize("body", ["", "   ", "\n"])
def test_empty_is_rejected(body: str):
    ok, why = vet("expression", body, _default())
    assert not ok, why


# --------------------------------------------------------------------------- #
# 조립 — override 가 **대본만** 덮어쓴다(데이터 블록은 코드가 만든다)
# --------------------------------------------------------------------------- #
_KW = dict(
    role="비버 선생님", personality="장난스럽다", locale_label="영어(English)",
    items=[{"obj": "고마워요", "des": "thank you", "ex": "도와줘서 고마워요."}],
    quiz_group=3, target_language="한국어", name="재우", face_rule="",
)


def test_override_replaces_the_script_but_keeps_the_data_block():
    """⭐⭐ 이게 설계의 핵심이다 — 라벨·데이터 짝이 **저절로** 지켜지는 이유."""
    body = _default().replace("하나씩 가르친다", "하나씩 꼼꼼히 가르친다")
    out = ex.build_expression_instruction(script_override=body, **_KW)
    assert "꼼꼼히 가르친다" in out, "override 글자가 안 들어갔다"
    assert "[오늘의 표현]" in out and "고마워요" in out, "데이터 블록이 사라졌다"


def test_no_override_is_byte_identical_to_the_default_path():
    """⛔ 기능을 안 쓰는 동안은 **바이트 무영향**이어야 한다."""
    assert ex.build_expression_instruction(script_override=None, **_KW) == \
        ex.build_expression_instruction(**_KW)


def test_switch_is_off_by_default():
    """⛔⛔ 이 DB 는 실서비스와 **공유**다 — 스위치가 켜져 있으면 머지만으로 실서비스가 바뀐다."""
    from core.config import Settings

    assert Settings.model_fields["PROMPT_OVERRIDE_ENABLED"].default is False


def test_the_call_row_records_which_prompt_ran():
    """⭐ 「그 통화가 어느 글자로 돌았나」 — 이게 없으면 전사 분석에서 원인을 못 가른다.

    2026-10-10 에 1815·1817 을 **Cloud Run 리비전**으로 갈라야 했다. override 는 리비전을
    안 바꾸므로 그 단서조차 없어진다 ⇒ call 행에 id 를 적는다.
    """
    from domains.learning.models.call import Call

    assert "prompt_override_id" in Call.__table__.c


# --------------------------------------------------------------------------- #
# 서비스 — 사람이 **쓰는** 쪽(dev 편집기 `/__devprompt` 와 스크립트가 같이 부른다)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db():
    """메모리 sqlite 1개. ⚠ BigInteger PK 는 sqlite 에서 자동증가가 안 돼 Integer 로 바꾼다
    (이 레포의 다른 통화 시험들이 쓰는 그 관례)."""
    from sqlalchemy import Integer, create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import db.registry  # noqa: F401 — 매퍼 전량 등록
    from db.base import Base

    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    eng = create_engine("sqlite+pysqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.close()


def _svc():
    from domains.learning.service import prompt_override_service as po_svc

    return po_svc


def _of(db, name="expression"):
    """⚠ `state()` 는 **대본 전부**를 이름순으로 준다 — `[0]` 으로 집으면 대본이 늘 때
    조용히 다른 대본을 보게 된다(2026-10-10 에 실제로 깨졌다). 이름으로 집는다."""
    return next(x for x in _svc().state(db)["scripts"] if x["name"] == name)


def test_empty_table_means_code_default(db):
    """⚠ 행이 하나도 없는 것이 **정상 상태**다 — 이 기능을 안 쓰는 동안은 항상 그렇다."""
    svc = _svc()
    got = svc.body_of(db, "expression")
    assert got["source"] == "code" and got["body"] == svc.default_of("expression")
    assert _of(db)["active"] is None


def test_put_then_body_comes_from_the_db(db):
    svc = _svc()
    body = svc.default_of("expression").replace("하나씩 가르친다", "하나씩 꼼꼼히 가르친다")
    res = svc.put(db, "expression", body, note="시험")
    assert res["ok"], res
    got = svc.body_of(db, "expression")
    assert got["source"] == "override" and got["body"] == body
    # ⭐ 코드 기본값도 같이 온다 — 편집기의 「되돌리기」가 그걸로 만들어진다.
    assert got["default"] == svc.default_of("expression")


def test_put_refuses_a_broken_body_and_writes_nothing(db):
    """⛔⛔ 거부되면 **행이 생기지 않아야** 한다 — 「거부했는데 표에는 남아 있다」가 최악이다."""
    svc = _svc()
    res = svc.put(db, "expression", svc.default_of("expression") + "\n[새 블록]\n뭔가")
    assert not res["ok"] and "라벨" in res["reason"]
    assert _of(db)["history"] == []


def test_put_demotes_the_previous_live_row(db):
    """표를 눈으로 봤을 때 현역이 **하나**여야 사람이 헷갈리지 않는다."""
    svc = _svc()
    d = svc.default_of("expression")
    svc.put(db, "expression", d.replace("가르친다", "가르친다 ", 1))
    svc.put(db, "expression", d.replace("가르친다", "가르친다  ", 1))
    hist = _of(db)["history"]
    assert [r["enabled"] for r in hist] == [True, False], hist


def test_disable_returns_to_the_code_default_without_deleting(db):
    svc = _svc()
    svc.put(db, "expression", svc.default_of("expression").replace("가르친다", "가르친다 ", 1))
    assert svc.disable(db, "expression")["disabled"] == 1
    assert svc.body_of(db, "expression")["source"] == "code"
    # ⭐ 행은 남는다 — 되돌리기가 이 테이블의 존재 이유 절반이다.
    assert len(_of(db)["history"]) == 1


def test_enable_puts_an_old_row_back(db):
    svc = _svc()
    d = svc.default_of("expression")
    first = svc.put(db, "expression", d.replace("가르친다", "가르친다 ", 1))["row"]["override_id"]
    svc.put(db, "expression", d.replace("가르친다", "가르친다  ", 1))
    assert svc.enable(db, "expression", first)["ok"]
    assert svc.body_of(db, "expression")["active"]["override_id"] == first


def test_enable_rechecks_the_old_body_against_the_current_code(db):
    """⛔ 옛 글자가 **지금 코드와 안 맞을 수 있다** — 그 사이 슬롯이 바뀌었으면 막는다.

    과거를 그대로 믿고 켜면 「되돌렸더니 통화가 안 열린다」가 된다.
    """
    svc = _svc()
    from domains.learning.models.prompt_override import PromptOverride

    row = PromptOverride(name="expression", body="슬롯이 없는 옛 글자", enabled=False)
    db.add(row)
    db.commit()
    res = svc.enable(db, "expression", row.override_id)
    assert not res["ok"] and "슬롯" in res["reason"]


def test_check_assembles_a_preview_with_the_data_block():
    """⭐ 미리보기가 이 화면의 핵심 — 데이터 블록이 **어디에 붙나**를 눈으로 본다."""
    svc = _svc()
    r = svc.check("expression", svc.default_of("expression"))
    assert r["ok"] and "[오늘의 표현]" in r["preview"] and "고마워요" in r["preview"]


def test_check_never_touches_the_db():
    """`check` 는 DB 인자를 **받지 않는다** — 미리보기가 쓰기를 섞으면 안 된다."""
    import inspect

    assert "db" not in inspect.signature(_svc().check).parameters


def test_the_script_and_the_editor_share_one_defaults_table():
    """⛔ 기본값 표가 두 군데면 **어긋난다** — 한쪽만 등록된 대본이 생긴다."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_dev_po", Path(__file__).resolve().parent.parent / "scripts" / "dev_prompt_override.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DEFAULTS is _svc().DEFAULTS


# --------------------------------------------------------------------------- #
# 네 대본 전부 — 대본을 새로 뗄 때 **여기서 먼저 깨져야** 한다
# --------------------------------------------------------------------------- #
def _names():
    return _svc().names()


def test_all_four_scripts_are_editable():
    """⭐ 2026-10-10: 네 코스 전부 DB 로 고칠 수 있다(표현학습만이던 상태에서 확장)."""
    assert set(_names()) == {"expression", "freetalk", "chat", "leveltest"}


@pytest.mark.parametrize("name", ["expression", "freetalk", "chat", "leveltest"])
def test_each_default_passes_its_own_vetting(name):
    """⛔⛔ 기본값이 자기 검사를 못 통과하면 **그 대본은 DB 로 못 고친다**(편집기가 늘 거부).

    대본을 새로 뗄 때 가장 흔한 실패다 — 슬롯 표기를 바꿔 놓고 기본값만 안 맞추는 경우.
    """
    r = _svc().check(name, _svc().default_of(name))
    assert r["ok"], "%s: %s" % (name, r["reason"])
    assert r["preview"], "%s: 미리보기가 비었다" % name


@pytest.mark.parametrize("name", ["expression", "freetalk", "chat", "leveltest"])
def test_each_script_rejects_a_missing_slot(name):
    """슬롯 하나를 지우면 거부된다 — 표기 세 가지(`%(x)s`·`%(x)d`·`{x}`)를 다 본다."""
    from core.openai.prompts.override import _slots

    d = _svc().default_of(name)
    victim = sorted(_slots(d)[0])[0]
    broken = (d.replace("%%(%s)s" % victim, "")
               .replace("%%(%s)d" % victim, "")
               .replace("{%s}" % victim, ""))
    assert broken != d, "시험이 슬롯을 못 지웠다(표기를 놓쳤다): %s" % victim
    assert not _svc().check(name, broken)["ok"]


@pytest.mark.parametrize("name,label", [
    ("expression", "오늘의 표현"), ("freetalk", "이번 차시"),
    ("chat", "관심사"), ("chat", "기억"),
])
def test_the_script_may_name_a_block_the_code_builds(name, label):
    """⭐ **코드가 만드는 블록 이름**은 대본에 또박또박 써도 통과해야 한다.

    ⛔ 처음엔 거부했다(2026-10-10) — 기본값의 라벨 집합만 봤기 때문이다. 표현학습 전문은
      `[오늘의 표현]` 을 「제공된 표현」이라고 **말로** 가리키므로, 사장님이 이름을 또박또박
      쓰는 더 분명한 지시가 막혀 버렸다. `override.DATA_LABELS` 로 열었다.
    ⚠ 반대로 **아무것도 가리키지 않는** 새 이름은 계속 막는다(바로 아래 시험).
    """
    svc = _svc()
    body = svc.default_of(name) + "\n[%s] 을 순서대로 쓴다." % label
    r = svc.check(name, body)
    assert r["ok"], r["reason"]


@pytest.mark.parametrize("name", ["expression", "freetalk", "chat", "leveltest"])
def test_each_script_still_rejects_a_label_pointing_at_nothing(name):
    r = _svc().check(name, _svc().default_of(name) + "\n[있지도 않은 블록]\n뭔가")
    assert not r["ok"] and "라벨" in r["reason"]


def test_leveltest_keeps_the_language_ladder_slot():
    """⛔⛔ `{ladder}` 가 빠지면 **적응형 진행이 사라진다** — 레벨테스트가 쉬운 질문만 한다.

    언어마다 다른 사다리가 들어가는 자리다(한국어·일본어·영어·중국어·프랑스어·베트남어).
    """
    from core.openai.prompts.override import _slots

    assert "ladder" in _slots(_svc().default_of("leveltest"))[0]


def test_leveltest_default_still_matches_the_gemini_master():
    """⛔⛔ 레벨테스트 대본은 Gemini 정본과 **같은 글자**로 맞춰 둔 것이다.

    DB override 는 OpenAI 경로에만 걸리므로, DB 로 고치면 두 엔진이 갈라진다 — 그건
    사장님 선택이지만, **코드 기본값끼리는** 계속 같아야 한다. 한쪽만 고치는 걸 막는다.
    """
    from core.openai.prompts import leveltest as ot
    from core.persona_prompt import build_leveltest_instruction as gemini

    kw = dict(role="비버", personality="장난스럽다", locale="en",
              interests=["여행"], name="재우", target_language="한국어")
    assert ot.build_leveltest_instruction(**kw) == gemini(**kw)


# --------------------------------------------------------------------------- #
# `[오늘의 표현]` 묶음 구조 — **구조가 규칙을 이긴다**(2026-10-10, call 1824·1827·1828)
# --------------------------------------------------------------------------- #
def _seven():
    return [{"obj": "표현%d" % i, "des": "m%d" % i, "ex": "예%d" % i} for i in range(1, 8)]


def test_items_block_puts_a_review_line_after_every_bundle():
    """⭐ 「3개마다 되묻는다」를 산문으로 세 번 써도 비버는 평평한 목록을 그냥 행진했다
    (세 통 모두 되묻기 0회). 그래서 되묻기를 목록 **안에** 줄로 박는다."""
    out = ex._items_block(_seven(), target="한국어", group=3)
    lines = out.split("\n")
    assert lines[0] == "[오늘의 표현]"
    marks = [l for l in lines if l.startswith(ex.REVIEW_MARK)]
    assert len(marks) == 3, marks                       # 1~3 · 4~6 · 7(꼬리)
    # 토론2 A안(2026-10-11): 묶음 줄은 영어(`BUNDLE_LINE`) — 한국어 줄은 낭독·누출 씨앗이었다.
    assert "1~3" in marks[0] and "4~6" in marks[1] and marks[2].startswith(ex.REVIEW_MARK + " Review 7")
    # 되묻기 줄은 그 묶음의 **마지막 항목 바로 뒤**에 온다.
    assert lines[lines.index(marks[0]) - 1].startswith("3. 표현3")
    assert lines[lines.index(marks[1]) - 1].startswith("6. 표현6")


def test_items_block_rows_keep_english_labels_inside_bundles():
    """항목 줄 — ⛔ 라벨은 칸 구분 정보라 빼지 않는다(2026-10-11 라벨을 뺀 3통 1858~1860 이 0/3).
    ⭐ 라벨은 **영어**다(토론2 A안 — 한국어 라벨 「뜻:」은 그대로 낭독·누출됐고, 영어 라벨 16통은 한국어
    안내 누출 0). 대상 언어 이름은 호출부가 영어로 넘긴다(「Korean」)."""
    out = ex._items_block(_seven(), target="Korean", group=3)
    assert '4. 표현4 — meaning: m4 — Korean: "예4"' in out
    assert "뜻:" not in out and "한국어 예" not in out


def test_items_block_is_flat_when_group_is_zero():
    out = ex._items_block(_seven(), target="한국어", group=0)
    assert ex.REVIEW_MARK not in out and "묶음" not in out
    assert out.count("\n") == 7                         # 머리 + 7줄


def test_items_block_never_teaches_a_close_word():
    """⛔ 끝 묶음에 「마지막」「여기까지」를 쓰지 않는다 — 종료 어휘를 주면 모델이 수단으로
    쓴다(README 원칙 5, call 706·852·870)."""
    from core.openai.prompts.override import BANNED

    out = ex._items_block(_seven(), target="한국어", group=3)
    assert not any(w in out for w in BANNED), out


def test_builder_bundles_by_quiz_group():
    """빌더가 `quiz_group` 을 그대로 묶음 크기로 넘긴다 — 대본의 `%(group)d` 와 같은 수."""
    out = ex.build_expression_instruction(**dict(_KW, items=_seven(), quiz_group=2))
    assert out.count(ex.REVIEW_MARK) == 4               # 2·2·2·1


def test_preview_shows_more_than_one_bundle():
    """미리보기 재료는 묶음 수보다 **많아야** 묶음 줄이 보인다(3개면 묶음이 하나뿐)."""
    r = _svc().check("expression", _svc().default_of("expression"))
    assert r["preview"].count(ex.REVIEW_MARK) >= 2


# --------------------------------------------------------------------------- #
# 2026-10-10 (3차) — ◯◯ 치환 · 넛지 언어 이름 (call 1824·1827~1830)
# --------------------------------------------------------------------------- #
def test_expression_rows_fill_the_blank_symbol():
    """⛔ 「저는 ◯◯이에요」를 비버가 **기호째 소리로** 읽었다(1824 t1·1828 t9·1830 t1).
    프리토킹이 쓰는 치환을 그대로 쓴다 — «(이름)»·«(나라)»."""
    items = [{"obj": "저는 ◯◯이에요", "des": "I'm ◯◯", "ex": "저는 ◯◯이에요."},
             {"obj": "저는 ◯◯ 사람이에요", "des": "I'm from ◯◯", "ex": ""}]
    out = ex._items_block(items, target="한국어", group=0)
    assert "◯" not in out, out
    assert "저는 (이름)이에요" in out and "저는 (나라) 사람이에요" in out


def test_nudges_name_the_language_and_carry_no_korean_sample():
    """⭐ 넛지 1·2 가 **언어 이름**을 박는다. «거기 있어? 들려?» 예시는 **없다** — 세 통에서
    모델이 그 예시를 한국어로 그대로 읊었다(README 원칙 2 「예시는 씨앗」)."""
    from core.openai.prompts import seeds as s

    n1, n2 = s.nudge_1("영어(English)"), s.nudge_2("영어(English)")
    assert "영어(English)로 힌트" in n1 and "영어(English)로 거기" in n2
    assert "모국어" not in n1 and "모국어" not in n2
    assert "거기 있어?" not in n2 and "들려?" not in n2
    # 라벨이 없으면 종전처럼 「모국어로」로 떨어진다(안전 기본값) — 상수도 그것이다.
    assert s.NUDGE_1 == s.nudge_1() and "모국어로" in s.NUDGE_1


def test_bundle_builds_nudges_with_the_call_locale():
    """`apply_to` 가 함수형 넛지에 통화의 `locale_label` 을 넣는다. Gemini 묶음은 안 건드린다."""
    from types import SimpleNamespace

    from core.openai.prompts import seeds as s
    from domains.learning.realtime import seed_bundle as sb

    st = SimpleNamespace(expr_items=[1], nudge_seed_1="x", nudge_seed_2="y", close_seed="z")
    sb.OPENAI.apply_to(st, close_tag="[T]", locale_label="일본어(日本語)")
    assert st.nudge_seed_1 == s.nudge_1("일본어(日本語)")
    assert st.nudge_seed_2 == s.nudge_2("일본어(日本語)")
    g = SimpleNamespace(expr_items=[1], nudge_seed_1="x", nudge_seed_2="y", close_seed="z")
    sb.GEMINI.apply_to(g, close_tag="[T]", locale_label="일본어(日本語)")
    assert (g.nudge_seed_1, g.nudge_seed_2, g.close_seed) == ("x", "y", "z")
