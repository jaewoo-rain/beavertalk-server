"""GPT 전용 **프리토킹(freetalk)** 통화 지시문 — 순수 문자열 조립, LLM 생성 0.

## ⛔⛔ 표현학습 대본(`expression.py`)을 베끼지 마라 — **언어 규칙이 정반대다**
| | 표현학습 | 이 코스 |
|---|---|---|
| 언어 | 모국어 90% + 정답 토막만 목표어 | **처음부터 끝까지 목표어.** 예외는 한 턴뿐 |
| 하는 일 | 묻고 · 판정하고 · 따라 시킨다 | **역할극.** 가르치지 않는다 |
| 서버가 쥔 것 | 퀴즈 창 · 재시도 한도 · 정답 판정 | **없다** |
| 체크판·진도 | 쓴다 | **하나도 안 쓴다** |
베끼면 모국어 발판이 새어 들고, 그게 이 코스에서 제일 먼저 깨지는 자리다.
`tests/test_openai_isolation.py` 가 「모국어로 말하라는 지시가 없다」를 문자열로 지킨다.

## 이 코스가 무엇인가 (Gemini 판 `core/prompts/freetalk.py` 와 **설계는 같다**)
차시의 상황을 목표어로 해 보는 **역할극**이다. 비버는 브리프의 «상대» 인물이 되어 그 상황
속에서 그냥 대화한다. 연습을 시키지 않고, 설명·따라 말하기·정오 판정이 **없다**.
학습자가 막히면 **그 턴만** 선생님으로 돌아와 알려주고 다시 인물로 돌아간다.

## ⛔ 여기에 싣지 않는 것 (일부러 없다)
- 드릴·따라 말하기·정답 판정 — 그건 표현학습이다.
- 체크판·증거·진도 — 이 통화는 아무것도 쓰지 않는다.
- 종료·작별 어휘 — **한 글자도.** 부정문도 금지다(「작별하지 마라」조차 안 된다).
  근거: 종료 개념을 가르친 과거 3건(call 706·852·870)이 전부 모델이 스스로 끊는 사고로
  끝났다. 수단을 모르면 그 수단을 못 쓴다. 금지어 목록은 격리 시험에 있다.

## ⭐ 표현학습에서 값을 치르고 배운 것 — **문구가 아니라 원리로** 가져왔다
1. **빈 자리를 남기고 끝나는 명령문을 쓰지 않는다.** `「~라고 말해 봐:」` 처럼 콜론으로
   끝내면 모델이 그 자리를 **채운다**. 표현학습에서 이것이 두 번 터졌다 — 한 번은 「한 턴에
   정답 두 번」(call 1764), 한 번은 「정답 선공개」(call 1766). ⇒ 턴을 **의문문으로** 닫는다.
2. **금지 예시를 나열하지 않는다.** 적어 둔 예시가 **씨앗이 된다**(docs/prompts/README.md
   §3 원칙 2). 「달래는 군말」 목록을 적어 뒀더니 그 말투가 나왔다. ⇒ 전진 지시로 쓴다.
3. **조건문보다 이분법.** 「틀리거나 모른다고 했을 때」는 글자에만 반응해 모델이 「그 밖의
   경우」라는 통을 만들어 빠져나갔다(call 1764). ⇒ 「위가 아닌 모든 경우」로 닫는다.
"""

from __future__ import annotations

import re
from typing import Optional

# ⛔⛔ 아래 두 치환은 **Gemini 판(`core/prompts/locked/freetalk.py`)이 실통화 사고로
#   얻은 것**이다. 격리 규율상 import 할 수 없어 **값을 베껴 적었다**(시험
#   `test_openai_isolation.py` 가 import 를 AST 로 막는다). 원본을 고치면 여기도 본다.

# 레벨1 청크의 빈칸 슬롯(«저는 ◯◯이에요»). 소재 줄에 기호째 실리면 비버가 **그대로 읽는다** —
#   실통화 1606 t23 «저는 ◯◯ 회원 아니에요» 를 지어냈고 1549 는 5턴 연속 작별했다
#   (`domains/learning/cur_l1_layout.py:6` 에 그 기록이 있다). L1 1·3차시에 들어 있고
#   **사용자 대다수가 L1** 이라 가장 많이 지나는 자리다.
# ⚠ 판정·진도는 원래 표면형을 쓴다 — 치환은 **프롬프트에 싣는 순간에만** 한다.
_SLOT_RE = re.compile(r"[◯○〇]{1,}")

# probes 의 시드 고유명(«마이클 씨») → 학습자 이름. 이름 환각의 **반대 방향** 위험(T21-B)이다.
#   `assets/curriculum_v3/cur_seed.json` 의 L2 1차시 probe 가 «마이클 씨는 어느 나라
#   사람이에요?» 다 — 치환하지 않으면 비버가 학습자를 그 이름으로 부른다.
# ⚠ ja 는 「〜さん」 패턴이 따로다(Gemini 판 `PROBE_NAME_RE_BY_LANGUAGE`). 이 코스는 ko 만
#   지원하므로(기획 §2 비범위) ko 패턴만 베꼈다 — ja 를 켜면 그 표도 같이 가져와야 한다.
_PROBE_NAME_RE = re.compile(r"[가-힣A-Za-z]{1,10} 씨")


def _fill_slots(text: str) -> str:
    """«◯◯» 빈칸을 «(이름)»·«(나라)»·«(그 단어)» 로. 문맥이 안 잡히면 «(빈칸)»."""
    if not text or not _SLOT_RE.search(text):
        return text
    if "사람이에요" in text or "에서 왔" in text or "나라" in text:
        label = "나라"
    elif "뭐예요" in text or "무슨 뜻" in text or "뜻이" in text:
        label = "그 단어"
    elif "저는" in text or "제 이름" in text:
        label = "이름"
    else:
        label = "빈칸"
    return _SLOT_RE.sub("(%s)" % label, text)

# 한 턴 문장 수 상한. ⚠ Gemini 판과 같은 값(2)이다 — 역할극은 비버가 길게 말하면
#   학습자 차례가 사라진다. 표현학습의 단어 상한은 여기 없다(자유 발화라 억지가 된다).
DEFAULT_MAX_SENTENCES = 2


def _items_block(items: list[dict], *, target: str) -> str:
    """차시 소재 — 문형(grammar)은 **이름을 말하지 말고 예문으로**, 그 밖은 표면형만.

    ⛔ 이것은 **소재**다. 「이 표현을 말해 봐」로 쓰면 그 순간 역할극이 수업이 된다.
    """
    grammar, surface = [], []
    for it in items or []:
        obj = (it or {}).get("obj") or ""
        if not obj:
            continue
        if ((it or {}).get("role") or "") == "grammar":
            ex = (it or {}).get("ex") or ""
            grammar.append('"%s"' % _fill_slots(ex) if ex else _fill_slots(obj))
        else:
            surface.append(_fill_slots(obj))
    lines = []
    if grammar:
        lines.append("  이런 말이 나오는 상황이면 좋다: %s" % " · ".join(grammar))
    if surface:
        lines.append("  학습자가 이 상황에서 할 만한 %s: %s" % (target, " · ".join(surface)))
    return "\n".join(lines)


# ── 대본 ─────────────────────────────────────────────────────────────────────── #
# ⛔ 종전의 공유 블록 4개(`block_forbidden`·`block_turn_landing`·`block_stuck`·
#   `block_recast`)는 **지웠다**(2026-10-09). 새 대본에 자리가 없고 자유대화가 유일한
#   사용자였다. 되살릴 일이 생기면 커밋 `399aee2` 에서 꺼낸다.
def build_freetalk_instruction(
    *,
    role: str,
    personality: str,
    locale_label: str,
    situation: str,
    partner: Optional[str] = None,
    items: Optional[list[dict]] = None,
    probes: Optional[list[str]] = None,
    target_language: str = "한국어",
    name: Optional[str] = None,
    level_note: str = "",
    max_sentences: int | None = None,
    face_rule: str = "",
) -> str:
    """회화학습(차시별 역할극) 대본 — ⭐ **사장님이 정제한 전문을 그대로 쓴다**(2026-10-09).

    ⛔ 이 글자를 「개선」하지 마라. 종전 대본은 커밋 `399aee2` 에 있다.
    ⭐ 전문이 **`[이번 차시]` 를 이름으로 가리킨다**(「[이번 차시]의 «상대»가 되어」).
      그래서 그 블록 이름은 계약이다 — 바꾸면 대본이 없는 것을 가리킨다.
    ⚠ `level_note` · `max_sentences` 는 받기만 하고 쓰지 않는다(새 대본에 자리가 없다).
      `name` 은 쓴다 — 화제(probes)의 고유명을 학습자 이름으로 바꿔야 비버가 학습자를
      «마이클 씨» 라고 부르지 않는다.
    """
    blocks = [
        "# 역할\n"
        "%s\n"
        "%s\n"
        "[이번 차시]의 «상대»가 되어 %s로 역할극을 진행한다.\n"
        "상대가 없으면 상황에 맞게 정하고, 인물 정보와 캐릭터 말투를 유지한다."
        % (role, personality, target_language),

        "# 시작\n"
        "한 문장으로 인사하고 상황에 맞는 질문 하나를 던진 뒤 답변을 기다린다.",

        "# 대화\n"
        "- 차시 표현을 쓸 상황을 만들되 따라 말하기·정오 판정은 하지 않는다.\n"
        "- 답변을 따라 하나씩 묻고 기다리며, 같은 질문을 반복하거나 스스로 답하지\n"
        "  않는다.\n"
        "- 단답이면 후속 질문으로 문장 답변을 이끌어낸다.\n"
        "- 모국어 질문에는 %s로 답한 뒤 %s 역할극으로 돌아온다.\n"
        "- 답하기 어려워하면 %s로 설명하고 %s 답변 예시 하나를 들려준다.\n"
        "- 틀린 표현은 네 답변에 올바른 %s 형태를 넣어 되받는다.\n"
        "- 장면이 끝나면 같은 상황의 다음 장면으로 이어간다.\n"
        "- 서버가 종료를 알릴 때까지 이어간다."
        % (locale_label, target_language, locale_label, target_language, target_language),

        _lesson_block(
            situation=situation, partner=partner, items=items, probes=probes,
            target_language=target_language, name=name,
        ),
    ]
    if face_rule.strip():
        blocks.append(face_rule.strip())
    return "\n\n".join(blocks) + "\n"


def _lesson_block(
    *,
    situation: str,
    partner: Optional[str],
    items: Optional[list[dict]],
    probes: Optional[list[str]],
    target_language: str,
    name: Optional[str],
) -> str:
    """`[이번 차시]` — 대본이 **이름으로 가리키는** 데이터 자리.

    ⚠ 차시 제목은 **장소가 아니라 학습 목표**로 온다(실측: 「못 알아들었을 때 되묻고
      도움 청하기」). 그래서 「어디서 무엇을」이 아니라 「상황」으로만 적고, 상대를
      정하는 일은 대본이 시킨다(「상대가 없으면 상황에 맞게 정하고」).
    """
    lines = ["[이번 차시]", "- 상황: %s" % ((situation or "").strip() or "(없음)")]
    who = (partner or "").strip()
    if who:
        lines.append("- 상대: %s" % who)
    body = _items_block(items or [], target=target_language)
    if body:
        lines.append("- 차시 표현:\n%s" % body)
    if probes:
        learner = (name or "").strip() or "학습자"
        shown = [_PROBE_NAME_RE.sub("%s 씨" % learner, _fill_slots(t.strip()))
                 for t in probes if (t or "").strip()]
        if shown:
            lines.append("- 대화가 멈추면 이런 쪽으로: %s" % " / ".join(shown))
    return "\n".join(lines)


def seed_freetalk_opening(target_language: str, situation: str = "") -> str:
    """선톡 시드 — 서버가 첫 턴으로 밀어 넣는 user 텍스트(학습자에게 안 보인다).

    ⛔ 「시작하자」만 말한다. **소재 표현을 적지 않는다** — 시드에 적으면 비버가 그걸
      읽어 버리고, 그 순간 역할극이 수업이 된다(표현학습 `seed_opening` 과 같은 규율).
    ⛔ 「상황을 설명하라」고 시키지 않는다. 인물은 설명하지 않고 **그 상황에 있는 사람처럼
      말을 건다** — 설명시키면 비버가 장면 밖에서 해설을 시작한다.
    ⛔⛔ **`situation` 을 시드에 적지 않는다**(F-1773). 차시 제목은 장소가 아니라 학습
      목표로 온다(「못 알아들었을 때 되묻고 도움 청하기」). 그게 괄호로 붙으면 **그 문장이
      첫 턴의 대본**이 되어 비버가 연습을 자기가 해 버린다 — 1773 의 첫 턴이 그것이다.
      장면은 지시문의 [이번 상황] 블록이 이미 주므로, 시드는 「그 장면 안에서 시작하라」만
      말한다.
      ⚠ 인자는 **남긴다** — 호출부가 넘기고 있고 시그니처를 바꾸면 그 자리도 같이
        고쳐야 한다. 쓰지 않는 것이 의도다.
    """
    _ = situation     # 의도적으로 쓰지 않는다(위 독스트링)
    return (
        "[지시] 네가 맡은 인물로서 %s로 먼저 말을 건다. 네가 세운 장면 안에서 그곳에 있는\n"
        "사람처럼 한마디 하고, 질문 하나로 닫는다. 장면을 설명하지 마라." % target_language
    )
