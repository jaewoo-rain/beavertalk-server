"""마이페이지 연동 회귀 — 레벨·상위% · 발음 요약 · 레벨테스트 재요청.

핵심 불변식:
  - 레벨은 **학습 언어 스코프**다. 학습 언어가 ko 가 아닌 회원에게 korean_level 을
    그대로 보여주면 남의 레벨이 뜬다.
  - "레벨테스트 다시하기"는 **체크판을 지우지 않는다**. 배운 걸 날리는 게 아니라
    레벨 배정만 다시 받는 것이다(dev 의 완전 백지화와 목적이 다르다).
  - member_language_level 행을 지워야 실제로 레벨테스트가 뜬다 — korean_level 만
    NULL 로 만들면 라우팅이 안 바뀐다(전례 있음).
  - "최근 N세션"은 **점수가 있는** 세션 N개다. 통화만 하고 발음 챌린지를 안 누른
    통화가 대부분이라 단순 최근 N통화로 잡으면 표본이 비어버린다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import Integer, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.registry import Base  # noqa: F401 - 전 모델 import
from domains.account.models.member import Member
from domains.learning.models.call import Call
from domains.learning.models.evaluation import Evaluation
from domains.learning.models.item_evidence import ItemEvidence
from domains.learning.models.member_item_progress import MemberItemProgress
from domains.learning.models.member_language_level import MemberLanguageLevel
from domains.learning.models.sentence import Sentence
from domains.learning.service import level_percentile, mastery_service
from domains.learning.service.pronunciation_service import get_pronunciation_summary


@pytest.fixture()
def db():
    for t in Base.metadata.tables.values():
        for pk in t.primary_key.columns:
            pk.type = Integer()
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def _member(db, language="ko", korean_level=None) -> Member:
    m = Member(language="en", target_language=language, onboarding_completed=True,
               auth_user_id=f"a{db.query(Member).count() + 1}", korean_level=korean_level)
    db.add(m)
    db.commit()
    return m


def _scored_call(db, member_id: int, days_ago: int, scores: list[int]) -> int:
    """점수가 채워진 통화 1건. scores 는 문장별 pronunciation 값."""
    c = Call(member_id=member_id, character_id=1, call_type="normal", status="done",
             call_date=datetime.now(timezone.utc) - timedelta(days=days_ago))
    db.add(c)
    db.flush()
    for s in scores:
        sent = Sentence(call_id=c.call_id, korean_sentence="안녕하세요")
        db.add(sent)
        db.flush()
        db.add(Evaluation(sentence_id=sent.sentence_id, total_score=s,
                          pronunciation=s, fluency=s - 5, rhythm=s - 10))
    db.commit()
    return c.call_id


def _unscored_call(db, member_id: int, days_ago: int) -> int:
    """통화는 했지만 발음 챌린지를 안 눌러 점수가 없는 통화(실제로 대부분)."""
    c = Call(member_id=member_id, character_id=1, call_type="normal", status="done",
             call_date=datetime.now(timezone.utc) - timedelta(days=days_ago))
    db.add(c)
    db.flush()
    sent = Sentence(call_id=c.call_id, korean_sentence="안녕하세요")
    db.add(sent)
    db.flush()
    db.add(Evaluation(sentence_id=sent.sentence_id))  # 점수 전부 NULL
    db.commit()
    return c.call_id


# --------------------------------------------------------------------------- #
# 1) 상위 % 표
# --------------------------------------------------------------------------- #
def test_percent_decreases_as_level_rises():
    """레벨이 오를수록 '상위 N%' 는 작아져야 한다 — 표를 고칠 때 뒤집히기 쉽다."""
    percents = [level_percentile.top_percent(lv) for lv in range(1, 14)]
    assert all(p is not None for p in percents), "1~13 전 레벨을 채워야 한다"
    assert percents == sorted(percents, reverse=True), f"단조감소가 깨졌다: {percents}"


def test_percent_stays_in_display_range():
    """0%·100% 는 문구가 이상해진다."""
    for lv in range(1, 14):
        p = level_percentile.top_percent(lv)
        assert 1 <= p <= 99, f"레벨 {lv} 의 {p}% 는 표시할 수 없는 값"


def test_percent_none_when_level_unset():
    assert level_percentile.top_percent(None) is None


# --------------------------------------------------------------------------- #
# 2) 발음 요약 — "점수 있는 세션 N개"
# --------------------------------------------------------------------------- #
def test_summary_empty_when_no_scores(db):
    m = _member(db)
    _unscored_call(db, m.member_id, days_ago=1)
    s = get_pronunciation_summary(db, m.member_id, sessions=10)
    assert s.sessions == 0 and s.sentence_count == 0
    assert s.total_score is None and s.pronunciation is None


def test_summary_skips_unscored_calls(db):
    """★ 점수 없는 통화가 세션 수를 갉아먹으면 안 된다."""
    m = _member(db)
    for d in range(1, 6):
        _unscored_call(db, m.member_id, days_ago=d)
    _scored_call(db, m.member_id, days_ago=9, scores=[80])
    s = get_pronunciation_summary(db, m.member_id, sessions=3)
    assert s.sessions == 1, "점수 없는 통화가 표본에 섞였다"
    assert s.pronunciation == 80.0


def test_summary_averages_by_sentence_not_by_call(db):
    """문장 단위 평균 — 1문장짜리 통화가 과대 대표되면 안 된다."""
    m = _member(db)
    _scored_call(db, m.member_id, days_ago=1, scores=[50])            # 1문장
    _scored_call(db, m.member_id, days_ago=2, scores=[90] * 9)        # 9문장
    s = get_pronunciation_summary(db, m.member_id, sessions=10)
    assert s.sessions == 2 and s.sentence_count == 10
    assert s.pronunciation == 86.0, "통화별 평균의 평균(70.0)이 나오면 안 된다"
    assert s.fluency == 81.0 and s.rhythm == 76.0


def test_summary_limits_to_requested_sessions(db):
    m = _member(db)
    for d in range(1, 6):
        _scored_call(db, m.member_id, days_ago=d, scores=[70])
    assert get_pronunciation_summary(db, m.member_id, sessions=2).sessions == 2


# --------------------------------------------------------------------------- #
# 3) 레벨테스트 다시하기
# --------------------------------------------------------------------------- #
def test_retest_clears_language_level_row(db):
    """★ mll 행을 지워야 실제로 레벨테스트가 뜬다 — korean_level 만으론 부족."""
    m = _member(db, korean_level=7)
    db.add(MemberLanguageLevel(member_id=m.member_id, language="ko", level_no=7))
    db.commit()

    mastery_service.request_level_retest(db, m, "ko")

    assert db.query(MemberLanguageLevel).count() == 0
    assert m.korean_level is None


def test_retest_preserves_checkboard(db):
    """★ 배운 걸 지우는 게 아니다 — 체크판·증거는 그대로."""
    m = _member(db, korean_level=5)
    db.add(MemberLanguageLevel(member_id=m.member_id, language="ko", level_no=5))
    db.add(MemberItemProgress(member_id=m.member_id, item_id=1, status="mastered"))
    db.add(ItemEvidence(member_id=m.member_id, language="ko", item_id=1,
                        call_id=1, grade_raw="E3", grade_final="E3"))
    db.commit()

    mastery_service.request_level_retest(db, m, "ko")

    assert db.query(MemberItemProgress).count() == 1, "체크판이 지워졌다"
    assert db.query(ItemEvidence).count() == 1, "증거가 지워졌다"


def test_retest_of_other_language_keeps_korean_level(db):
    """학습 언어가 ja 인데 korean_level 을 비우면 한국어 레벨이 날아간다."""
    m = _member(db, language="ja", korean_level=6)
    db.add(MemberLanguageLevel(member_id=m.member_id, language="ja", level_no=3))
    db.add(MemberLanguageLevel(member_id=m.member_id, language="ko", level_no=6))
    db.commit()

    mastery_service.request_level_retest(db, m, "ja")

    assert m.korean_level == 6, "다른 언어 재측정이 한국어 레벨을 건드렸다"
    rows = db.query(MemberLanguageLevel).all()
    assert [r.language for r in rows] == ["ko"]


# --------------------------------------------------------------------------- #
# 4) 이력 주입 언어 격리 — 다른 언어 학습 내용이 새어들면 안 된다
# --------------------------------------------------------------------------- #
def _call_with_content(db, member_id: int, language: str, summary: str, sentence: str):
    c = Call(member_id=member_id, character_id=1, call_type="normal", status="done",
             target_language=language, summary=summary,
             call_date=datetime.now(timezone.utc))
    db.add(c)
    db.flush()
    db.add(Sentence(call_id=c.call_id, korean_sentence=sentence))
    db.commit()
    return c.call_id


def test_history_excludes_other_languages(db):
    """★ 일본어 통화에 한국어 이력이 주입되던 실제 사고.

    prod 실측: ja 회원의 통화 37건 중 36건이 ko 였고, 주입된 summaries 5건·
    expressions 14건이 **전부 한국어**였다. 비버가 "그거 기억나?" 하며 배운 적 없는
    한국어를 꺼냈다.
    """
    from domains.learning.service.normalcall_service import _load_history

    m = _member(db, language="ja")
    _call_with_content(db, m.member_id, "ko", "Basic Korean phrases", "저는 학생이에요.")
    _call_with_content(db, m.member_id, "ko", "Korean study mode", "집에 가요")
    _call_with_content(db, m.member_id, "ja", "日本語の練習", "わたしは学生です。")

    h = _load_history(db, m.member_id, "ja")

    assert h is not None
    assert h["summaries"] == ["日本語の練習"], f"한국어 요약이 섞였다: {h['summaries']}"
    assert h["expressions"] == ["わたしは学生です。"], (
        f"한국어 문장이 섞였다: {h['expressions']}"
    )


def test_history_of_korean_call_is_unaffected(db):
    """한국어 통화는 기존과 동일해야 한다(하위호환)."""
    from domains.learning.service.normalcall_service import _load_history

    m = _member(db)
    _call_with_content(db, m.member_id, "ko", "한국어 요약", "저는 학생이에요.")
    _call_with_content(db, m.member_id, "ja", "日本語", "わたしは学生です。")

    h = _load_history(db, m.member_id, "ko")
    assert h["summaries"] == ["한국어 요약"]
    assert h["expressions"] == ["저는 학생이에요."]


def test_history_none_when_no_call_in_that_language(db):
    """그 언어 통화가 없으면 None — 남의 언어로 채우지 않는다."""
    from domains.learning.service.normalcall_service import _load_history

    m = _member(db, language="ja")
    _call_with_content(db, m.member_id, "ko", "한국어 요약", "저는 학생이에요.")
    assert _load_history(db, m.member_id, "ja") is None


# --------------------------------------------------------------------------- #
# 5) 레벨테스트 표본 게이트 — 모국어로 도망친 통화를 표본으로 세면 안 된다
# --------------------------------------------------------------------------- #
def test_sample_gate_counts_only_target_script():
    """★ 실제 사고(call=818): 일본어 21자인데 한국어 143자가 더해져 게이트 통과.

    합계 164자로 통과 → 마커 1개(〜は〜です)로 A1(2단계) 배정. 일본어 요구
    3연속 실패였는데도 2단계가 나왔다.
    """
    from domains.learning.service.normalcall_service import _user_char_total

    dialog = "\n".join([
        "[BEAVER] 일본어로 인사 해 볼 수 있어요?",
        "[USER] こんにちは。",
        "[USER] 私 は ヤンジェ ウデス。",
        "[USER] 아니요. 모르겠는데요.",
        "[USER] 그냥 일본 가서 했었던 거는 뭐, 마트에 가가지고 이것저것 많이 샀었어요.",
    ])
    ja = _user_char_total(dialog, "ja")
    ko = _user_char_total(dialog, "ko")
    assert ja == 14, f"일본어 문자만 세야 한다: {ja}"   # こんにちは(5) + 私は…デス(9)
    assert ko > ja, "한국어 발화가 훨씬 많다(같은 전사)"


def test_sample_gate_rejects_native_only_call():
    """★ 대상 언어를 한 마디도 안 했으면 0자 — 판정이 돌면 안 된다."""
    from domains.learning.service.normalcall_service import (
        _MIN_LEVELTEST_USER_CHARS, _user_char_total,
    )

    dialog = "\n".join([
        "[BEAVER] 일본어로 말해 볼까요?",
        "[USER] 모르겠는데요. 일본은 예전에 가봤는데 마트에서 이것저것 많이 샀어요.",
    ])
    assert _user_char_total(dialog, "ja") == 0
    assert _user_char_total(dialog, "ja") < _MIN_LEVELTEST_USER_CHARS


def test_sample_gate_korean_call_unaffected():
    """한국어 통화는 기존과 동일해야 한다(하위호환)."""
    from domains.learning.service.normalcall_service import _user_char_total

    dialog = "[USER] 저는 학생이에요. 오늘 학교에 갔어요."
    assert _user_char_total(dialog, "ko") == 15  # 공백·마침표 제외


def test_sample_gate_ignores_punctuation_and_emoji():
    """문장부호·이모지만으로 게이트를 채우는 방어는 유지된다."""
    from domains.learning.service.normalcall_service import _user_char_total

    assert _user_char_total("[USER] ！！？？…… 🦫🦫🦫🦫🦫", "ja") == 0


def test_latin_language_excludes_korean():
    """모국어(한글)로 도망친 영어 레벨테스트도 잡힌다."""
    from core.languages import count_target_script_chars

    assert count_target_script_chars("모르겠는데요 진짜로", "en") == 0
    assert count_target_script_chars("I went to the store", "en") == 15


def test_unregistered_language_falls_back_to_permissive():
    """표에 없는 언어는 예전처럼 전부 계수 — 데이터 부재로 기능이 죽지 않게(R5)."""
    from core.languages import count_target_script_chars

    assert count_target_script_chars("모르겠는데요", "xx") == 6


# --------------------------------------------------------------------------- #
# 6) 배치 캡 — 1↔2 변별(실사용자 대다수가 진짜 초보)
# --------------------------------------------------------------------------- #
def _assessed(band="a1", structures=3, quality="sufficient"):
    from domains.learning.service.normalcall_service import LevelAssessment
    return LevelAssessment(
        band=band, distinct_structures=structures, sample_quality=quality,
        confidence="medium", summary="s", feedback_for_learner="f",
        evidence=[], reasoning="r",
    )


def test_memorized_and_sparse_drops_to_survival():
    """★ 실측 call=818: 일본어 2턴·구조 1개인데 2단계가 나왔다.

    캡 바닥이 2였던 탓에 밴드 a1(=2)에 캡을 걸어도 2로 그대로였다. 두 신호가
    겹치면(외운 것 + 표본 빈약) 생존회화 1단계여야 한다.
    """
    from domains.learning.service.normalcall_service import _place_from_band
    assert _place_from_band(_assessed("a1", structures=1, quality="sparse")) == 1
    # 밴드가 높아도 마찬가지 — 암기 긴 문장으로 상위 밴드를 따내는 게이밍 방지
    assert _place_from_band(_assessed("mid", structures=1, quality="sparse")) == 1


def test_single_signal_still_caps_at_two():
    """하나만 걸리면 종전대로 2 — 과소배치는 자동 레벨업이 회복한다."""
    from domains.learning.service.normalcall_service import _place_from_band
    assert _place_from_band(_assessed("a4", structures=1, quality="sufficient")) == 2
    assert _place_from_band(_assessed("a4", structures=5, quality="sparse")) == 2


def test_healthy_sample_is_not_capped():
    """표본이 충분하고 구조가 다양하면 밴드 그대로."""
    from domains.learning.service.normalcall_service import _BUCKET_LEVEL, _place_from_band
    got = _place_from_band(_assessed("a4", structures=5, quality="sufficient"))
    assert got == _BUCKET_LEVEL["a4"]


# --------------------------------------------------------------------------- #
# 7) grandfathering — 선별엔 쓰되 "배웠다"고 말하지 않는다
# --------------------------------------------------------------------------- #
def _item(db, language: str, kind: str, surface: str, level_no: int = 1):
    from domains.learning.models.learning_item import LearningItem
    it = LearningItem(language=language, kind=kind, surface=surface,
                      level_no=level_no, band=level_no, assign_rule="test",
                      # 문법은 교재 좌표 필수(ck_learning_item_grammar_textbook)
                      textbook_code="T1" if kind == "grammar" else None,
                      topik_grade=1 if kind == "vocab" else None,
                      source_key=f"{language}-{surface}")
    db.add(it)
    db.flush()
    return it


def test_placement_items_are_not_claimed_as_known(db):
    """★ 레벨 배정으로 찍힌 항목을 비버가 "배웠잖아" 라고 하면 안 된다.

    실측: 일본어 2단계 배정 직후 1단계 46개가 증거 0건인 채 introduced 로 찍혔고,
    비버가 배운 적 없는 자기소개를 두고 "그거 기억나?" 라고 했다.
    """
    from domains.learning.repository.mastery_repository import known_grammar

    m = _member(db, language="ja")
    placed = _item(db, "ja", "grammar", "〜は〜です")
    real = _item(db, "ja", "grammar", "〜ました")
    db.add(MemberItemProgress(member_id=m.member_id, item_id=placed.item_id,
                              status="mastered", provenance="placement"))
    db.add(MemberItemProgress(member_id=m.member_id, item_id=real.item_id,
                              status="mastered", provenance="observed"))
    db.commit()

    known = known_grammar(db, m.member_id, "ja")
    assert known == ["〜ました"], f"placement 가 새어나왔다: {known}"


def test_placement_promoted_to_observed_becomes_known(db):
    """실제로 해보면 provenance 가 observed 로 승격되고, 그때는 아는 것으로 센다."""
    from domains.learning.repository.mastery_repository import known_grammar

    m = _member(db, language="ja")
    it = _item(db, "ja", "grammar", "〜は〜です")
    prog = MemberItemProgress(member_id=m.member_id, item_id=it.item_id,
                              status="mastered", provenance="placement")
    db.add(prog)
    db.commit()
    assert known_grammar(db, m.member_id, "ja") == []

    prog.provenance = "observed"   # mastery_service 가 증거 반영 시 하는 일
    db.commit()
    assert known_grammar(db, m.member_id, "ja") == ["〜は〜です"]


# --------------------------------------------------------------------------- #
# 8) 판정 전사 필터 — 모국어에 정중체만 붙인 발화를 근거로 세면 안 된다
# --------------------------------------------------------------------------- #
def test_strip_removes_native_lines_with_polite_suffix():
    """★ 실측 call=823: 「갔다데스」를 일본어 과거형 「〜た」로 읽고 A3(3단계)를 줬다.

    판정관은 자기가 속은 걸 모른다 — 스스로 마커 4종을 찾았다고 확신했다. 그래서
    프롬프트로 부탁하지 않고 입력에서 지운다.
    """
    from domains.learning.service.normalcall_service import _strip_non_target_user_lines

    dialog = "\n".join([
        "[BEAVER] 일본어로 인사할 수 있어요?",
        "[USER] こんにちは 。私 は ヤン ジェウ です よ 。",
        "[BEAVER] 어제는 뭘 하셨어요?",
        "[USER] 어제는 나는 그 연구실 갔다데스, 그리고 프로젝트 했다데스.",
        "[USER] 어 모른다 데스 어렵다 데스",
    ])
    out = _strip_non_target_user_lines(dialog, "ja").splitlines()

    assert "[USER] こんにちは 。私 は ヤン ジェウ です よ 。" in out
    assert not any("갔다데스" in l for l in out), "한국어+데스가 남았다"
    assert not any("모른다 데스" in l for l in out), "한국어+데스가 남았다"


def test_strip_keeps_beaver_lines():
    """비버 질문은 남긴다 — "유도했는데 못 했다"를 판정관이 알아야 한다."""
    from domains.learning.service.normalcall_service import _strip_non_target_user_lines

    dialog = "\n".join([
        "[BEAVER] 어디에 사세요? 일본어로 말해 볼래요?",
        "[USER] 아니요 모르겠는데요",
    ])
    out = _strip_non_target_user_lines(dialog, "ja").splitlines()
    assert out == ["[BEAVER] 어디에 사세요? 일본어로 말해 볼래요?"]


def test_strip_keeps_mixed_line_with_real_target_speech():
    """진짜 일본어가 섞여 있으면 남긴다 — 과하게 지우면 근거가 사라진다."""
    from domains.learning.service.normalcall_service import _strip_non_target_user_lines

    dialog = "[USER] 어제는 연구실 갔어요. 私 は 学生 です 。"
    assert _strip_non_target_user_lines(dialog, "ja") == dialog


def test_strip_korean_call_is_unaffected():
    """한국어 통화는 그대로 — 하위호환."""
    from domains.learning.service.normalcall_service import _strip_non_target_user_lines

    dialog = "\n".join([
        "[BEAVER] 자기소개 해 주세요",
        "[USER] 저는 학생이에요. 오늘 학교에 갔어요.",
    ])
    assert _strip_non_target_user_lines(dialog, "ko") == dialog


def test_prompt_forbids_native_stem_with_polite_suffix():
    """프롬프트 2차 방어도 남아 있어야 한다(필터가 놓친 경계 대비)."""
    from domains.learning.service.normalcall_service import _leveltest_instruction

    p = _leveltest_instruction("ko", "", target_language="일본어")
    assert "갔다데스" in p
    assert "정중체 어미만" in p
