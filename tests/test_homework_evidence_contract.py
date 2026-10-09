"""기존 순수 검증기의 숙제 재사용 경계. 귀속·진도 쓰기 없음."""
import hashlib

from domains.learning.service import mastery_service as mastery
from domains.learning.service import normalcall_service as svc


class NoDatabaseAccess:
    def __getattr__(self, name):
        raise AssertionError(f"verification must not access DB: {name}")


def verify(detections, rows, hints=None):
    return svc._verify_detections(
        NoDatabaseAccess(), 1, 1,
        [svc.ItemDetection(item_id=item, evidence=grade, quote=quote)
         for item, grade, quote in detections],
        [{"item_id": 1, "surface": "의사", "kind": "vocab"}],
        rows, hinted_from_turn_index=hints,
    )


def test_hash_preserves_case_punctuation_and_unicode():
    quote = " A\t가\n! "
    assert mastery.normalize_text(quote) == "A가!"
    assert mastery.text_hash(mastery.normalize_text(quote)) == hashlib.sha1(
        "A가!".encode("utf-8")
    ).hexdigest()
    assert mastery.text_hash("A가!") != mastery.text_hash("a가!")
    assert mastery.text_hash("A가!") != mastery.text_hash("A가")


def test_beaver_only_quote_and_outside_closed_set_are_discarded():
    rows = [{"role": "beaver", "content": "저는 의사입니다", "turn_index": 0},
            {"role": "user", "content": "잘 모르겠어요", "turn_index": 1}]
    assert verify([(1, "E3", "저는 의사입니다"),
                   (999, "E1", "잘 모르겠어요")], rows) == []


def test_echo_demotes_and_records_actual_user_turn_without_db():
    quote = "저는 의사입니다"
    rows = [{"role": "beaver", "content": quote, "turn_index": 5},
            {"role": "user", "content": quote, "turn_index": 6}]
    result = verify([(1, "E3", quote)], rows)
    assert len(result) == 1
    assert (result[0].grade_raw, result[0].grade_final) == ("E3", "E1")
    assert (result[0].quote, result[0].turn_index) == (quote, 6)


def test_hint_demotion_and_normalized_duplicate_without_db():
    rows = [{"role": "user", "content": "저는 의사입니다", "turn_index": 8}]
    result = verify([(1, "E2", "저는 의사입니다"),
                     (1, "E2", "저는\t의사입니다")], rows, {7})
    assert len(result) == 1
    assert result[0].grade_final == "E1"
    assert result[0].turn_index == 8
