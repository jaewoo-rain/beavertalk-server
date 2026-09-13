"""E2E 하네스 — [문형] 항목 식별: 비버가 예문 대신 **새 연습 문장**을 만들면(1592 「이 옷은 얼마예요?」) 서버와 같은 템플릿 매처로
문형을 잡고, 그 문장 속 한 음절 어휘(「이」)로 오식별하지 않는다. 서버·DB 없이(quiz_judge 는 순수 함수)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import e2e_expression_call as h  # noqa: E402


class _Voice:
    async def pcm(self, text, lang):
        return b"\0" * 320


class _Picker:
    enabled = False
    calls = 0
    client = None


def _items():
    return {
        24: h.Item(24, "이", "this", "", ("this",), kind="vocab", example="이 사람은 제 동생이에요."),
        73: h.Item(73, "말레이시아", "Malaysia", "", ("malaysia",), kind="vocab", example="말레이시아는 나라예요."),
        10638: h.Item(10638, "N은/는 N이에요/예요", "N is N", "", ("n is n",), kind="grammar", example="생일이 언제예요?",
                      examples=("생일이 언제예요?",)),
    }


def _session():
    return h.Session(_items(), _Voice(), _Picker(), probe=True, verbose=False, course="expression", lesson={"no": 4})


def test_invented_template_sentence_identifies_grammar_not_vocab():
    sess = _session()
    text = 'It\'s "이 옷은 얼마예요?" Now say it, you loser!'
    mentioned = h.surfaces_in(text, sess.items)
    assert mentioned == [24]                                   # 표면형 대조로는 「이」 만 걸린다
    item_id, how = asyncio.run(sess.identify(text, text, mentioned, False, False))
    assert (item_id, how) == (10638, "template")


def test_vocab_example_sentence_still_identifies_vocab():
    sess = _session()
    text = 'It\'s "이 사람은 제 동생이에요." Say it.'          # 「이」 의 예문 그대로 = 어휘 공개(템플릿에도 걸리지만 예문이 이긴다)
    item_id, how = asyncio.run(sess.identify(text, text, h.surfaces_in(text, sess.items), False, False))
    assert (item_id, how) == (24, "reveal")


def test_one_word_meaning_inside_quoted_sentence_is_not_a_quote_match():
    sess = _session()
    text = 'Try this one: how do you ask "How much is this clothing?" in Korean?'
    item_id, how = asyncio.run(sess.identify(text, text, [], True, True))
    assert item_id is None                                     # "this" ⊂ 문장 은 「이」 가 아니다(LLM 이 없으면 미식별)


def test_parrot_request_of_sentence_repeats_sentence_not_short_vocab():
    sess = _session()
    reply = sess.parrot_request('I told you to say "이 옷은 얼마예요?" not something else!')
    assert reply == ("이 옷은 얼마예요", "ko", "parrot")
