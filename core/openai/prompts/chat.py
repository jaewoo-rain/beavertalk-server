"""GPT 전용 **자유대화(chat)** 통화 지시문 — 순수 문자열 조립, LLM 생성 0.

## 프리토킹과 무엇이 같고 무엇이 다른가
`freetalk.py` 의 **공유 블록 4개를 그대로 쓴다**(복제하지 않는다 — 한쪽만 고쳐지면 조용히
갈라진다): `block_forbidden` · `block_turn_landing` · `block_stuck` · `block_recast`.
그 4개가 표현학습에서 값을 치르고 얻은 규칙이다(빈 자리 금지·이분법·recast).

| | 프리토킹 | 자유대화(이 파일) |
|---|---|---|
| 뼈대 | **역할극** — 비버가 차시의 «상대» 인물 | **그냥 비버.** 인물을 맡지 않는다 |
| 소재 | 차시가 **유일한** 소재 | **관심사·기억**(둘 다 없으면 소재 제약 없음) |
| 블록 | [이번 상황](상황·상대·소재·probes) | **[관심사]·[기억]**(각각 있을 때만) |
| 서버가 쥔 것 | 없다 | 없다 |

## ⛔ 빈 블록을 내보내지 않는다
`[관심사]`·`[기억]` 은 **내용이 있을 때만** 붙인다. 빈 머리말만 나가면 모델이 **지어낸다**
(Gemini 판 `core/prompts/chat.py` 가 같은 규율을 못박아 뒀다 — QA C7-③).
`memory_is_substantial` 이 유일한 관문이고, 「아는 척」 시드도 그 관문을 지난다.

## ⛔ 여기에 싣지 않는 것
- 역할극·차시·드릴·판정 — 전부 다른 코스 것이다.
- 종료·작별 어휘 — **한 글자도.** 부정문도 금지(call 706·852·870). 격리 시험이 지킨다.
"""

from __future__ import annotations

from typing import Optional

# ⛔ 종전 대본의 공유 블록 import 를 지웠다(2026-10-09 대본 교체).

# ⭐ 「아는 척」 시드 — 서버가 첫 턴으로 밀어 넣는 **지시문**이다(비버가 할 말이 아니다).
#   ⛔⛔ 비버의 대사를 그대로 담으면 안 된다. Gemini 판에서 그 사고가 났다(R4-a):
#     옛 문구("오랜만이에요! 지난번에 …")를 `user` 턴에 실었더니 비버가 **학습자가 한
#     말로 받아 자기 말에 스스로 답했다** — 선톡이 실종됐다(실측).
#   ⇒ 이 코드베이스의 모든 시드와 같은 «대괄호 지시» 형식으로 쓴다.
_RECALL_SEED = (
    "[지시] 오랜만에 학습자에게 다시 전화를 건 상황이다. 지난번에 {topic} 이야기했던 것을 "
    "자연스럽게 언급하며 안부를 묻고, 그 일이 어떻게 됐는지 짧게 물어라. "
    "질문 하나로 닫고 학습자의 대답을 기다린다."
)


def memory_is_substantial(memory: dict | None) -> bool:
    """기억이 「아는 척」 할 만큼 있나 — 화제·사실이 하나도 없으면 빈약.

    ⛔ 이 함수가 `[기억]` 블록과 「아는 척」 시드의 **유일한** 관문이다. 둘이 서로 다른
      기준을 쓰면 「시드는 아는 척하는데 블록엔 내용이 없는」 상태가 된다.
    """
    if not memory:
        return False
    return bool(memory.get("topics") or memory.get("facts"))


def _pick_recall_topic(memory: dict) -> str:
    """시드에 쓸 화제 하나 — `topics` 우선(더 최근·더 화제스럽다), 없으면 `facts`."""
    for key in ("topics", "facts"):
        for item in memory.get(key) or []:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _interests_block(interests: list[str] | None) -> str:
    """[관심사] — 실제 회원 관심사가 하나도 없으면 **빈 문자열**(블록 자체 생략)."""
    real = [i.strip() for i in (interests or []) if isinstance(i, str) and i.strip()]
    if not real:
        return ""
    return ("[관심사]\n"
            "학습자가 좋아하는 것: %s\n"
            "- 화제가 필요할 때 여기서 고른다. 다 쓰려 하지 말고 **한 번에 하나**만."
            % ", ".join(real))


def _memory_block(memory: dict | None) -> str:
    """[기억] — 빈약하면 **빈 문자열**(블록 자체 생략. 빈 머리말은 환각을 부른다)."""
    if not memory_is_substantial(memory):
        return ""
    lines = ["[기억 — 지난 자유대화에서 알게 된 것]"]
    summary = (memory.get("summary") or "").strip()
    if summary:
        lines.append("- 지금까지 나눈 이야기: %s" % summary)
    facts = [f for f in (memory.get("facts") or []) if isinstance(f, str) and f.strip()]
    if facts:
        lines.append("- 학습자에 대해 알게 된 것: " + ", ".join(facts[:15]))
    nexts = [t for t in (memory.get("next_topics") or []) if isinstance(t, str) and t.strip()]
    if nexts:
        lines.append("- 다음에 물어볼 만한 것: " + ", ".join(nexts[:5]))
    lines.append("⛔ 여기 적힌 것만 아는 척한다. **적혀 있지 않은 것을 지어내지 마라.**")
    return "\n".join(lines)


def build_chat_instruction(
    *,
    role: str,
    personality: str,
    locale_label: str,
    interests: Optional[list[str]] = None,
    memory: dict | None = None,
    target_language: str = "한국어",
    name: Optional[str] = None,
    level_note: str = "",
    max_sentences: int | None = None,
    face_rule: str = "",
) -> str:
    """자유대화 대본 — ⭐ **사장님이 정제한 전문을 그대로 쓴다**(2026-10-09).

    ⛔ 이 글자를 「개선」하지 마라. 종전 대본은 커밋 `399aee2` 에 있다.
    ⭐ 전문이 「제공된 기억이 있으면 지난 이야기로, 없으면 관심사로」라고 가리키므로
      `[기억]`·`[관심사]` 두 블록은 **계약**이다. 둘 다 빈약하면 블록 자체가 빠지고,
      그러면 대본의 「없으면」 갈래로 저절로 떨어진다(빈 머리말은 환각을 부른다).
    ⚠ `name` · `level_note` · `max_sentences` 는 받기만 하고 쓰지 않는다.
    """
    blocks = [
        "# 역할\n"
        "%s\n"
        "%s\n"
        "캐릭터 말투는 유지하되 아래 규칙에 따라 %s로 잡담한다."
        % (role, personality, target_language),

        "# 시작\n"
        "%s로 인사하고, 제공된 기억이 있으면 지난 이야기로, 없으면 관심사로\n"
        "대화를 시작한다." % target_language,

        "# 대화\n"
        "- 말의 내용에 반응하며 네 생각과 질문을 섞고, 질문할 때는 하나만 묻고\n"
        "  기다린다.\n"
        "- 화제를 따라 자연스럽게 이어가며, 말이 막히면 네 생각을 덧붙이거나 화제를\n"
        "  바꾼다.\n"
        "- 가르치거나 평가하거나 따라 말하게 하지 않는다.\n"
        "- 의미가 통하면 이어가고, 필요한 교정만 답변에 자연스럽게 섞는다.\n"
        "- 모국어 질문에는 %s로 답한 뒤 %s 대화로 돌아온다.\n"
        "- 제공되지 않은 기억을 지어내지 않는다.\n"
        "- 서버가 종료를 알릴 때까지 짧게 주고받는다."
        % (locale_label, target_language),
    ]
    for extra in (_interests_block(interests), _memory_block(memory)):
        if extra:
            blocks.append(extra)
    if face_rule.strip():
        blocks.append(face_rule.strip())
    return "\n\n".join(blocks) + "\n"


def seed_chat_opening(target_language: str, memory: dict | None) -> str:
    """「아는 척」 선톡 시드 — 기억이 빈약하면 **빈 문자열**(호출부가 폴백해야 한다).

    ⚠ `target_language` 는 다른 `seed_*` 와 시그니처를 맞추려고 받는다. 이 시드는 **서버가
      비버에게 주는 지시**라 한국어로 쓴다(비버가 소리로 읽지 않는다 — 공유 금지 3번).
    """
    if not memory_is_substantial(memory):
        return ""
    topic = _pick_recall_topic(memory or {})
    if not topic:
        return ""
    del target_language
    return _RECALL_SEED.format(topic=topic)


def seed_chat_plain_opening(target_language: str) -> str:
    """기억이 없을 때의 선톡 시드 — 그냥 말을 건다.

    ⛔ 「무엇을 물어라」를 적지 않는다. 적으면 매 통화 첫 질문이 똑같아진다 —
      화제는 `[관심사]`·`[기억]` 이 있으면 거기서, 없으면 비버가 고른다.
    """
    return (
        "[지시] %s로 짧게 인사하고 질문 하나로 닫는다. "
        "안내문을 소리로 읽지 마라." % target_language
    )
