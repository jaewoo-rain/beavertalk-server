"""`core/openai/` 격리 — **Gemini 자산 유입을 막는 유일한 기계적 장치**.

사장님 지시(2026-10-04): 「지피티 전용으로 폴더 새로 만들고 거기서 처음부터 작성하라고.
자꾸 제미나이 있던 흔적 가져오지마」

⛔ 이 시험을 끄거나 금지 목록을 줄이지 마라. 약속으로는 못 막는다 — 「새 파일」로만
가르면 기존 조립 결과를 복사해 두 줄 고치는 식으로 흔적이 새어 들어오고, 그러면
「오지 않는 종료 신호를 기다리는」 지시문이 다시 만들어진다.

⭐ **AST 로 본다**(문자열 grep 이 아니다) — 주석·독스트링에 적힌 모듈 이름은 통과시키고
  **실제 import 만** 잡는다. 이 파일들의 독스트링은 금지 목록을 설명하기 위해 그 이름을
  일부러 적고 있다.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

PKG = pathlib.Path(__file__).resolve().parents[1] / "core" / "openai"

# ⛔ Gemini 전용 자산. 이 중 하나라도 import 하면 실패다.
FORBIDDEN_PREFIXES = (
    "core.prompts",        # locked · editable 전부(대본·툴 선언 문구·시드)
    "core.gemini_live",    # LiveEvent·LiveSessionProtocol·set_face_tool·open_session
    "core.persona_prompt",  # 일반 통화 조립기·재접지 쪽지·종료 시드
)
# ⚠ 도메인도 금지다 — 어댑터는 도메인·DB 를 모른다(CLAUDE.md 의 core 규율).
FORBIDDEN_PREFIXES += ("domains.", "db.")


def _py_files() -> list[pathlib.Path]:
    return sorted(PKG.rglob("*.py"))


def _imported_modules(path: pathlib.Path) -> list[str]:
    """이 파일이 실제로 import 하는 모듈 이름(절대 경로 기준) 전부."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # 상대 import — 이 패키지 안이므로 금지 대상이 될 수 없다. 이름만 남긴다.
                out.append("core.openai." + (node.module or ""))
            elif node.module:
                out.append(node.module)
    return out


def test_openai_package_has_files():
    """경로가 틀려서 «검사할 파일이 0개» 로 조용히 통과하는 일을 막는다."""
    files = _py_files()
    assert len(files) >= 5, [str(f) for f in files]
    names = {f.name for f in files}
    assert {"__init__.py", "session.py", "audio.py", "tools.py"} <= names


@pytest.mark.parametrize("path", _py_files(), ids=lambda p: p.name)
def test_openai_folder_never_imports_gemini_assets(path: pathlib.Path):
    """`core/openai/` 의 모든 .py 를 AST 로 파싱해 import 를 전수 검사한다."""
    bad = [
        m for m in _imported_modules(path)
        if any(m == pre.rstrip(".") or m.startswith(pre) for pre in FORBIDDEN_PREFIXES)
    ]
    assert not bad, (
        "%s 가 Gemini/도메인 자산을 import 한다: %s\n"
        "⛔ 이 패키지는 백지에서 쓴다. 필요한 사실은 **값을 베껴 적고** import 하지 않는다."
        % (path.name, bad)
    )


def test_prompt_file_has_no_close_protocol_vocabulary():
    """⛔ 지시문에 **종료를 설명하는 어휘**가 없어야 한다.

    이 엔진엔 종료 시드가 없다. 「서버가 알린다」·「마무리」 류를 쓰면 모델이 **오지 않는
    신호를 기다린다**. 그리고 종료 개념을 가르친 과거 3건(call 706·852·870)은 전부
    모델이 스스로 통화를 끊는 사고로 끝났다 — 수단을 모르면 그 수단을 못 쓴다.
    """
    from core.openai.prompts import expression as ex

    text = ex.build_expression_instruction(
        role="선생님", personality="다정함", locale_label="English",
        items=[{"obj": "고마워요", "des": "thank you", "ex": "도와줘서 고마워요."}] * 6,
        quiz_group=3,
    )
    banned = ["마무리", "마지막", "여기까지", "종료", "작별", "통화를 끝", "서버가 알린", "끝내는 때"]
    hit = [w for w in banned if w in text]
    assert not hit, "종료 어휘가 들어갔다: %s" % hit


def test_prompt_file_has_the_two_measured_prescriptions():
    """⭐ 측정으로 확인된 처방 2개가 **실제로 지시문에 있다**.

    ① 정답 선공개 금지(처방 전 4/4 → 후 0/4) ② 목록 소진 뒤 출구(없으면 8턴 체류 ×2회).
    """
    from core.openai.prompts import expression as ex

    text = ex.build_expression_instruction(
        role="선생님", personality="다정함", locale_label="English",
        items=[{"obj": "고마워요"}] * 6, quiz_group=3)
    assert "입을 떼기 전" in text                  # ① 선공개 금지
    assert text.index("입을 떼기 전") < 400, "금지는 맨 앞에 있어야 한다(GPT 위반 100%)"
    assert "[6번까지 다 돌았으면]" in text          # ② 출구
    assert "되돌아가지 않는다" in text


# --------------------------------------------------------------------------- #
# 시드 묶음 — GPT 문구가 Gemini 문구에서 온 것이 아님을 **글자로** 본다
# --------------------------------------------------------------------------- #
def _longest_common_substring(a: str, b: str) -> str:
    """두 문자열의 최장 공통 부분문자열(O(n·m) DP — 문구 길이가 수백 자라 충분하다)."""
    if not a or not b:
        return ""
    best_len = best_end = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best_len:
                    best_len, best_end = cur[j], i
        prev = cur
    return a[best_end - best_len:best_end]


def _bundle_strings(bundle, *, data: str = "DATA") -> dict[str, str]:
    """묶음이 내보낼 수 있는 모든 문구.

    ⭐ `data` — 동적 문구에 **엔진별로 다른 더미 데이터**를 넣는다. 항목 표면형·번호·뜻은
      서버가 만든 **같은 데이터**라 두 엔진에 글자까지 같이 들어간다 ⇒ 그대로 비교하면
      «데이터 겹침» 이 «문장 베낌» 으로 잡힌다(실제로 그렇게 오탐이 났다). 더미를 갈라
      넣으면 남는 겹침은 **우리가 쓴 문장**뿐이다.
    """
    out = {
        "loop_break": bundle.loop_break,
        "resume_after_slip": bundle.resume_after_slip,
        "drill_move_on": bundle.drill_move_on,
        "quiz_cue": bundle.quiz_cue(
            "<%s-L>" % data, 2, retry=False, locale_label="<%s-N>" % data, target="<%s-T>" % data,
            done_labels=["<%s-D>" % data], remaining_rows=["<%s-R>" % data]),
        "quiz_set_reminder": bundle.quiz_set_reminder("<%s-L>" % data),
    }
    for attr, key in (("_nudge_1", "nudge_1"), ("_nudge_2", "nudge_2")):
        val = getattr(bundle, attr, None)
        if val:
            out[key] = val
    close = getattr(bundle, "_close_seed", None)
    if close is not None:
        out["close_seed"] = close("tag")
    return out


def test_gpt_seed_strings_are_not_copied_from_the_gemini_ones():
    """⛔ 「백지에서 썼다」를 **글자로** 확인한다 — 긴 공통 부분문자열이 있으면 베낀 것이다.

    ⚠ 짧은 겹침은 정상이다: 접두 `[안내]`(계약 — 측정이 그 글자로 났고 서버 안전망이 그
      글자를 찾는다) · «이 문장은 읽지 마라» 류의 한국어 상용구 · 데이터(«물» «가다»·번호·뜻).
      그래서 **임계를 둔다** — 한 문장을 통째로 옮기면 20자를 쉽게 넘는다.
    """
    from domains.learning.realtime import seed_bundle as sb

    gem = _bundle_strings(sb.GEMINI, data="GEM")
    gpt = _bundle_strings(sb.OPENAI, data="GPT")
    offenders = []
    for key, gpt_text in gpt.items():
        gem_text = gem.get(key)
        if not gem_text:
            continue
        common = _longest_common_substring(gpt_text, gem_text)
        if len(common) > 16:
            offenders.append((key, len(common), common))
    assert not offenders, "Gemini 문구를 옮긴 흔적: %s" % offenders


def test_every_bundle_field_differs_between_engines():
    """묶음 필드 중 **하나라도** 두 엔진이 같은 객체면 그 자리는 안 갈렸다는 뜻이다."""
    from domains.learning.realtime import seed_bundle as sb

    gem = _bundle_strings(sb.GEMINI)
    gpt = _bundle_strings(sb.OPENAI)
    same = [k for k in gem if k in gpt and gem[k] == gpt[k]]
    assert not same, "두 엔진이 같은 문구를 쓴다: %s" % same
    # GPT 전용 세 칸(무음 1·2단·종료)은 Gemini 묶음에 **없다**(그쪽은 call_session 이 콜타입별로 정한다)
    assert "nudge_1" in gpt and "nudge_2" in gpt and "close_seed" in gpt
    assert "nudge_1" not in gem and "close_seed" not in gem


def test_gpt_seeds_carry_no_close_tag_and_no_waiting_for_a_signal():
    """⛔ 종료 태그를 박지 않는다(call 852: 난수 접미를 그대로 복사했다) ·
    ⛔ 「서버가 알려 줄 때까지 기다려라」류를 쓰지 않는다(오지 않는 신호다)."""
    from core.prompts.locked.rules import CLOSE_TAG_DEFAULT
    from domains.learning.realtime import seed_bundle as sb

    for key, text in _bundle_strings(sb.OPENAI).items():
        assert CLOSE_TAG_DEFAULT not in text, key
        for banned in ("서버가 알린", "서버가 알려", "신호를 기다", "알려 줄 때까지"):
            assert banned not in text, (key, banned)


def test_gpt_seeds_use_the_measured_bracket_prefix():
    """⭐ `[안내]` 는 **계약**이다 — 측정(낭독 0/5)이 그 글자로 났고, 서버 자기낭독
    안전망(`_scrub_control_tags`)이 같은 글자를 찾는다. 바꾸면 둘이 같이 깨진다."""
    from core.prompts.locked.rules import CONTROL_TAG
    from core.openai.prompts import seeds as gpt

    assert gpt.NOTE_TAG == CONTROL_TAG, "접두가 서버 안전망과 어긋났다"
    from domains.learning.realtime import seed_bundle as sb
    for key, text in _bundle_strings(sb.OPENAI).items():
        assert text.startswith(gpt.NOTE_TAG), key


def test_gpt_seeds_do_not_contain_glyphs_that_could_be_read_aloud():
    """⛔ 주입 문구에 `⛔`·`**` 같은 표식을 넣지 마라 — 소리로 새면 학습자가 듣는다.

    ⚠ 지시문(세션 1회)은 다르다 — 거기선 강조가 실제로 먹었다(측정). 여기는 **대사 직전에
      꽂히는 쪽지**라 낭독 위험이 다르다.
    """
    from domains.learning.realtime import seed_bundle as sb

    for key, text in _bundle_strings(sb.OPENAI).items():
        for glyph in ("⛔", "**", "⭐", "⚠", "#", "`"):
            assert glyph not in text, (key, glyph)


# ─────────────────────────────────────────────────────────────────────────────
# 프리토킹(freetalk) 전용 못 — ⛔ 표현학습 대본이 새어 드는 것을 **문자열로** 막는다
# ─────────────────────────────────────────────────────────────────────────────
_FT_KW = dict(
    role="비버 선생님", personality="장난기 있고 직설적",
    locale_label="영어(English)", situation="카페에서 음료 주문하기", partner="카페 직원",
    items=[{"obj": "물 주세요", "ex": "저기요, 물 주세요.", "role": "chunk"},
           {"obj": "-고 싶어요", "ex": "커피 마시고 싶어요.", "role": "grammar"}],
    probes=["뭐 드릴까요?"], target_language="한국어", name="Baba",
    level_note="초급 — 짧은 문장만 알아듣는다.",
)


def _ft_text():
    from core.openai.prompts import freetalk as ft
    return ft.build_freetalk_instruction(**_FT_KW)


def test_freetalk_declares_target_language_only():
    """⛔⛔ 이 코스는 **100% 목표어**다 — 표현학습(모국어 90%)과 **정반대**다.

    표현학습 대본을 참고하다 모국어 발판이 새어 드는 것이 이 코스에서 제일 먼저 깨지는
    자리다(기획 §4 위험 1). 선언이 있고, 모국어가 목표어보다 많이 나오지 않아야 한다.
    """
    text = _ft_text()
    assert "처음부터 끝까지 한국어다" in text, "목표어 100% 선언이 없다"
    assert "네 말은 전부 영어" not in text, "표현학습 금지3(모국어 위주)이 섞여 들었다"
    assert text.count("한국어") > text.count("영어(English)"), \
        "모국어가 목표어보다 많이 등장한다 — 발판이 새어 든 신호다"


def test_freetalk_has_no_drill_or_judging_vocabulary():
    """⛔ 드릴·판정 어휘가 없어야 한다 — 「가르치지 않는다」가 이 코스의 정체다.

    `core/prompts/freetalk.py` 가 못박아 둔 것: 「여기에 드릴·따라 말하기·판정을 실지
    마라 — 그건 표현학습이다」.
    """
    text = _ft_text()
    banned = ["따라 말해", "정답", "맞혔", "퀴즈", "오답", "점수"]
    hit = [w for w in banned if w in text]
    assert not hit, "드릴·판정 어휘가 들어갔다: %s" % hit


def test_freetalk_has_no_close_protocol_vocabulary():
    """⛔ 종료 어휘 금지는 **코스와 무관**하다(call 706·852·870). 부정문도 금지다."""
    text = _ft_text()
    banned = ["마무리", "마지막", "여기까지", "종료", "작별", "통화를 끝", "서버가 알린", "끝내는 때"]
    hit = [w for w in banned if w in text]
    assert not hit, "종료 어휘가 들어갔다: %s" % hit


def test_freetalk_pins_the_chapter_as_the_only_source():
    """⭐ 사장님 지시(2026-10-08): 차시(챕터)가 **유일한** 소재다 — 밖에서 화제를 안 가져온다."""
    text = _ft_text()
    assert "유일한" in text and "상황 밖에서 화제를 가져오지 않는다" in text
    assert "카페에서 음료 주문하기" in text and "카페 직원" in text, "브리프가 안 실렸다"
    assert '"커피 마시고 싶어요."' in text, "문형은 **예문으로** 실려야 한다(이름을 말하지 않는다)"
    assert "-고 싶어요" not in text, "문형 이름이 그대로 실렸다 — 예문으로만 쓴다"


def test_freetalk_opening_seed_carries_no_material():
    """⛔ 시드에 소재 표현을 적으면 비버가 그걸 읽어 버린다 — 그 순간 역할극이 수업이 된다."""
    from core.openai.prompts import freetalk as ft
    seed = ft.seed_freetalk_opening("한국어", "카페에서 음료 주문하기")
    assert "물 주세요" not in seed and "커피 마시고" not in seed
    assert "질문 하나로 닫는다" in seed, "턴 착지 규약이 시드에도 있어야 한다"


# ─────────────────────────────────────────────────────────────────────────────
# 자유대화(chat) 전용 못 — 역할극이 아니고, 빈 블록을 안 내보낸다
# ─────────────────────────────────────────────────────────────────────────────
_CHAT_KW = dict(
    role="비버 선생님", personality="장난기 있고 직설적",
    locale_label="영어(English)", target_language="한국어", name="Baba",
    level_note="초급 — 짧은 문장만 알아듣는다.",
)
_MEM = {"summary": "여행 이야기를 했다", "topics": ["제주도 여행"],
        "facts": ["강아지를 키운다"], "next_topics": ["다음 휴가 계획"]}


def _chat_text(**kw):
    from core.openai.prompts import chat as ch
    return ch.build_chat_instruction(**{**_CHAT_KW, **kw})


def test_chat_shares_the_hard_won_blocks_with_freetalk():
    """⛔⛔ 두 코스가 **같은 글자**를 써야 한다 — 복제하면 한쪽만 고쳐져 조용히 갈라진다.

    공유 4개(금지·턴 착지·막혔을 때·recast)는 표현학습에서 값을 치르고 얻은 규칙이다.
    """
    from core.openai.prompts import chat as ch, freetalk as ft
    kw = dict(target_language="한국어", locale_label="영어(English)")
    text = _chat_text(memory=_MEM)
    assert ft.block_forbidden(**kw) in text, "금지 블록이 공유 글자가 아니다"
    assert ft.block_stuck(**kw) in text, "막혔을 때 블록이 공유 글자가 아니다"
    assert ft.block_recast(target_language="한국어") in text
    assert ft.block_turn_landing(target_language="한국어", max_sentences=2) in text
    # chat 모듈이 freetalk 의 블록을 **쓴다**(복제 아님)
    import inspect
    src = inspect.getsource(ch)
    assert "from core.openai.prompts.freetalk import" in src


def test_chat_is_not_roleplay():
    """⛔ 자유대화엔 역할극·차시가 없다 — 그건 프리토킹이다."""
    text = _chat_text(memory=_MEM)
    banned = ["역할극", "네가 맡은 사람", "이번 상황", "인물로 돌아가", "장면"]
    hit = [w for w in banned if w in text]
    assert not hit, "역할극 어휘가 섞여 들었다: %s" % hit
    assert "그냥 이야기한다" in text and "인물을 맡지 않고" in text


def test_chat_omits_empty_interest_and_memory_blocks():
    """⛔⛔ 빈 머리말이 나가면 모델이 **지어낸다**(Gemini 판 QA C7-③ 와 같은 규율)."""
    bare = _chat_text(interests=None, memory=None)
    assert "[관심사]" not in bare and "[기억" not in bare, "빈 블록이 나갔다"
    bare2 = _chat_text(interests=["  ", ""], memory={"topics": [], "facts": []})
    assert "[관심사]" not in bare2 and "[기억" not in bare2, "공백만 있어도 블록을 내면 안 된다"
    full = _chat_text(interests=["축구"], memory=_MEM)
    assert "[관심사]" in full and "축구" in full
    assert "[기억 — 지난 자유대화에서 알게 된 것]" in full
    # ⚠ `topics` 는 **블록에 안 들어간다** — Gemini 판과 같은 계약이다. topics 는
    #   「아는 척」 시드가 화제 하나를 고르는 재료이고, 블록은 summary·facts·next_topics 다.
    assert "여행 이야기를 했다" in full, "summary 가 빠졌다"
    assert "강아지를 키운다" in full, "facts 가 빠졌다"
    assert "다음 휴가 계획" in full, "next_topics 가 빠졌다"
    assert "제주도 여행" not in full, "topics 는 블록이 아니라 시드 재료다"
    assert "적혀 있지 않은 것을 지어내지 마라" in full, "환각 금지 한 줄이 빠졌다"


def test_chat_has_no_close_protocol_vocabulary():
    """⛔ 종료 어휘 금지는 코스와 무관하다(call 706·852·870)."""
    text = _chat_text(interests=["축구"], memory=_MEM)
    banned = ["마무리", "마지막", "여기까지", "종료", "작별", "통화를 끝", "서버가 알린", "끝내는 때"]
    hit = [w for w in banned if w in text]
    assert not hit, "종료 어휘가 들어갔다: %s" % hit


def test_chat_recall_seed_is_a_bracket_instruction_not_a_line():
    """⛔⛔ 시드에 **비버 대사**를 담으면 비버가 자기 말에 스스로 답한다(Gemini R4-a 실측).

    그래서 모든 시드는 «대괄호 지시» 형식이다. 그리고 기억이 빈약하면 **빈 문자열**이라
    호출부가 평범한 선톡으로 폴백한다.
    """
    from core.openai.prompts import chat as ch
    seed = ch.seed_chat_opening("한국어", _MEM)
    assert seed.startswith("[지시]"), "대괄호 지시 형식이 아니다 — 비버가 대사로 읽는다"
    assert "제주도 여행" in seed
    assert ch.seed_chat_opening("한국어", None) == ""
    assert ch.seed_chat_opening("한국어", {"topics": [], "facts": []}) == ""
    plain = ch.seed_chat_plain_opening("한국어")
    assert plain.startswith("[지시]") and "한국어" in plain


def test_chat_memory_gate_is_single_sourced():
    """⛔ `[기억]` 블록과 「아는 척」 시드가 **같은 관문**을 써야 한다.

    다르면 「시드는 아는 척하는데 블록엔 내용이 없는」 상태가 된다.
    """
    from core.openai.prompts import chat as ch
    thin = {"summary": "뭔가 있었다", "topics": [], "facts": []}   # summary 만 있다
    assert ch.memory_is_substantial(thin) is False
    assert ch.seed_chat_opening("한국어", thin) == ""
    assert "[기억" not in _chat_text(memory=thin), "관문이 어긋났다"
