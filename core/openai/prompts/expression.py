"""GPT 전용 표현학습 지시문 — **백지에서 썼다**.

## ⛔ 이 파일의 출처
Gemini 지시문(`core/prompts/locked/*`·`core/prompts/editable/*`)의 문장을 **한 줄도
옮기지 않았다.** 격리 시험(`tests/test_openai_isolation.py`)이 import 를 기계로 막고,
문장은 아래 세 가지 입력만 보고 새로 썼다:

### (1) 도메인 사실 (DB·커리큘럼)
오늘의 표현 N개(번호·표면형·뜻·예문) · 퀴즈는 `quiz_group` 개마다 · 같은 항목 최대 3회
재시도 · 무음 3단(서버가 처리) · 학습자 모국어 · 목표어 · 캐릭터 페르소나.

### (2) 측정으로 확인된 교훈 (2026-10-03 생짜 15분 1통 + 처방 재측정)
| 지켜야 할 것 | 실측 |
|---|---|
| 정답을 먼저 말하지 않는다 | 처방 전 선공개 **4/4(100%)** → 처방 후 **0/4** |
| 번호 순서 + 목록 소진 뒤 **출구** | 출구가 없으면 항목6에 **8턴 체류 ×2회** |
| 끝내는 때를 **기다리지 않는다** | 이 엔진엔 종료 시드가 없다 — 기다리면 영원히 안 끝난다 |
| 안내·리액션은 학습자 **모국어** | 규칙만 주면 한국어로 나온다 ⇒ 여러 번 떠받친다 |
| 한 턴에 질문 하나 | 「응답 2개 + 질문 3번」 위반 관측 |
| 반말 답은 맞은 게 아니다 | 가르친 표현 반말 **0/52**(지켜졌다 — 유지할 축) |
| 의역 정답을 틀렸다고 하지 않는다 | `alt_polite` **4/5 기각**(운영 1636 재현) |
| 서버 큐를 받아 질문하되 **태그를 읽지 않는다** | `[안내]` 낭독 **0/5**(지켜졌다) |

### (3) GPT 특성 — Gemini 와 다르게 쓴 이유
- 선질문 위반: Gemini **6.2%** vs GPT **100%** ⇒ **금지를 맨 앞에, 단독 블록으로.**
- 번호 순서: GPT 는 **혼자 지켰다**(드릴 1회차 이탈 0) ⇒ 번호 강제에 지면을 덜 쓴다.
- 퀴즈: GPT 는 **스스로 안 열었다**(0회·3라운드) ⇒ 「스스로 열라」고 쓰지 않고
  **「큐가 오면 그때」** 로만 쓴다. 안 하는 걸 시키면 다른 규칙과 모순이 생긴다.

## ⛔ 이 파일에 쓰지 않은 것 (의도된 부재)
- **종료·마무리·작별을 설명하는 문장** — 한 글자도 없다. 모델이 수단을 모르면 그 수단을
  못 쓴다. 과거 3건(706·852·870)이 전부 「종료를 가르쳤더니 스스로 끊었다」였다.
- **조각·이어하기·재개·압축·재접지** — 이 엔진엔 없는 개념이다.
- **톤 부사와 예시 대사** — 톤은 캐릭터(role/personality)가 소유한다. 예시 대사를 쓰면
  모델이 그걸 그대로 뱉는다.
- **제어 태그의 글자 모양** — 서버가 붙이는 접두를 「읽지 말라」고만 하고, 어떤 글자인지
  보여주지 않는다.
"""

from __future__ import annotations

# 한 턴에 말할 문장 수 상한. 길면 학습자가 못 따라오고 원가도 는다.
DEFAULT_MAX_SENTENCES = 3
# 같은 항목을 몇 번까지 다시 시키나. 서버 상한과 같은 숫자여야 한다(출구가 어긋나면
# 「서버는 넘겼는데 비버는 안 넘어간」 상태가 된다).
DEFAULT_RETRY_LIMIT = 3


def _row(idx: int, item: dict, *, target: str) -> str:
    """항목 한 줄: `3. 고마워요 — 뜻: thank you — 예: "도와줘서 고마워요."`"""
    surface = str(item.get("obj") or "").strip()
    row = "%d. %s" % (idx, surface)
    des = str(item.get("des") or "").strip()
    if des:
        row += " — 뜻: %s" % des
    ex = str(item.get("ex") or "").strip()
    if ex:
        row += ' — %s 예: "%s"' % (target, ex)
    return row


def _items_block(items: list[dict], *, target: str) -> str:
    rows = [_row(i, it, target=target) for i, it in enumerate(items, start=1)]
    if not rows:
        rows = ["(오늘 다룰 표현이 없다)"]
    return "[오늘의 표현]\n" + "\n".join(rows)


def _quiz_numbers(total: int, group: int) -> list[int]:
    """퀴즈가 걸리는 번호 — `group` 개마다. 6개·3이면 [3, 6]."""
    if group <= 0:
        return []
    return [n for n in range(group, total + 1, group)]


def build_expression_instruction(
    *,
    role: str,
    personality: str,
    locale_label: str,
    items: list[dict],
    quiz_group: int,
    target_language: str = "한국어",
    name: str | None = None,
    level_note: str = "",
    max_sentences: int | None = None,
    retry_limit: int = DEFAULT_RETRY_LIMIT,
    face_rule: str = "",
) -> str:
    """표현학습 통화의 system_instruction(순수 문자열 조립 — LLM 생성 0).

    Args:
        role·personality: 캐릭터 2필드(DB). 톤은 전부 여기 위임한다.
        locale_label: 학습자 모국어 이름(예 "English"). **안내·리액션의 언어다.**
        items: `[{obj, des, ex}, ...]` — 순서가 곧 번호다(서버 선별이 낸 순서).
        quiz_group: 몇 개마다 퀴즈인가. ⛔ 기본값을 주지 않는다 — 선별 상수와 한 곳에서만.
        level_note: 학습자 수준 한 줄(없으면 블록을 안 넣는다).
        retry_limit: 같은 항목 재시도 상한. 서버 상한과 같아야 한다.
        face_rule: 표정 블록(표정 OFF 면 빈 문자열 → 블록 자체가 안 붙는다).
    """
    total = len(items)
    sent = DEFAULT_MAX_SENTENCES if not max_sentences else max(1, int(max_sentences))
    learner = (name or "").strip() or "the learner"
    quiz_nums = _quiz_numbers(total, int(quiz_group or 0))
    quiz_where = (
        ", ".join(str(n) for n in quiz_nums) if quiz_nums else "(없음)"
    )

    blocks: list[str] = []

    # ── ① 절대 금지 — GPT 는 금지를 맨 앞에 둬야 지킨다(선질문 위반 100% → 처방 필요) ──
    blocks.append(
        "[절대 금지 — 다른 모든 지시보다 먼저 지킨다]\n"
        "1. 아직 묻지 않은 항목의 %(t)s 표현을 **네가 먼저 말하지 않는다.** 묻기 전에 그\n"
        "   표현을 소리로 내면 그 항목은 그 자리에서 물어볼 거리가 없어진다.\n"
        "   - 네가 할 일은 「무엇을 말해야 하는지」를 %(l)s 로 설명하고, 학습자가 그것을\n"
        "     %(t)s로 말하게 하는 것이다. 설명 안에 그 표현을 끼워 넣지 않는다.\n"
        "   - ⭐ 금지는 **묻기 전**에만이다. 물어봤고 학습자가 틀렸으면 **그 자리에서 바로**\n"
        "     정답 문장을 들려준다 — %(r)d번째까지 기다리지 않는다.\n"
        "2. 한 턴에 질문은 **하나**다. 질문을 두 개 이어 붙이지 않는다.\n"
        "3. 설명·칭찬·핀잔은 **%(l)s 로** 한다. 그 자리에 %(t)s를 쓰지 않는다.\n"
        "   %(t)s는 학습자가 말할 차례와, 네가 정답을 알려 주는 자리에만 나온다.\n"
        "4. 대괄호로 시작하는 문장이 오면 그건 너에게 주는 지시다 — **소리로 읽지 않는다.**"
        % {"t": target_language, "l": locale_label, "r": int(retry_limit)}
    )

    # ── ② 역할 ───────────────────────────────────────────────────────────────── #
    blocks.append(
        "[역할]\n"
        "너는 %s. 성격: %s\n"
        "%s를 배우는 %s 와 음성으로 통화한다. 학습자의 모국어는 %s 다.\n"
        "칭찬·핀잔·농담은 네 성격대로 한다."
        % (role or "a Korean conversation partner", personality or "warm and relaxed",
           target_language, learner, locale_label)
    )
    if level_note.strip():
        blocks.append("[학습자 수준]\n%s" % level_note.strip())

    # ── ③ 종이 ───────────────────────────────────────────────────────────────── #
    blocks.append(_items_block(items, target=target_language))

    # ── ④ 진행 — 번호 강제에 지면을 덜 쓴다(GPT 는 혼자 지켰다) ────────────────── #
    blocks.append(
        "[진행]\n"
        "1번부터 차례로 하나씩 다룬다. **한 턴은 아래 틀 그대로**다 — 번호마다 한 문장씩,\n"
        "그 밖의 말을 붙이지 않는다.\n"
        "\n"
        "▶ 맞혔을 때 — %(n)d문장\n"
        "  ① 네 성격대로 짧게 인정한다.\n"
        "  ② 다음 번호 표현을 **언제 쓰는지** %(l)s 로 말하고 상황을 하나 준다.\n"
        "  ③ 그걸 %(t)s로 말해 보라고 한다.\n"
        "\n"
        "▶ 틀렸을 때 — %(n)d문장 (**몇 번째 시도든 똑같다**)\n"
        "  ① 네 성격대로 반응한다.\n"
        "  ② **정답 문장을 또박또박 한 번 들려준다.**\n"
        "  ③ 따라 말해 보라고 한다.\n"
        "\n"
        "▶ %(r)d번째에도 못 했을 때 — 2문장\n"
        "  ① 네 성격대로 「넘어가자」 한마디.\n"
        "  ② 다음 번호 표현의 상황을 주고 %(t)s로 말해 보라고 한다.\n"
        "  그 항목을 더 붙잡지 않는다.\n"
        "\n"
        "⛔ 위 번호 밖의 말을 붙이지 않는다. 달래는 군말(「어려운 거 아니야」·「부담 없이」·\n"
        "  「담백하게」), 상황을 두 번 설명하는 것, 같은 질문을 되풀이하는 것 — 전부 금지다."
        % {"l": locale_label, "t": target_language,
           "r": int(retry_limit), "n": min(3, sent)}
    )

    # ── ⑤ 맞았나 — 의역 수용 + 반말 기각(둘 다 실측 근거가 있다) ────────────────── #
    blocks.append(
        "[맞았는지 보는 법]\n"
        "- 뜻이 통하면 맞은 것이다. 적힌 글자와 달라도 같은 뜻의 다른 %s 표현이면\n"
        "  맞다고 반응한다. 글자가 똑같아야 한다고 요구하지 않는다.\n"
        "- 다만 **정중한 말씨로 가르친 표현을 반말로 답하면 맞은 것이 아니다.** 그때는\n"
        "  말씨를 올려 다시 말해 보게 한다.\n"
        "- 발음이 조금 어긋난 것은 넘어간다. 통하면 통한 것이다."
        % target_language
    )

    # ── ⑥ 퀴즈 — 「큐가 오면」으로만 쓴다(스스로 열라고 하지 않는다) ──────────────── #
    blocks.append(
        "[퀴즈]\n"
        "%s번 항목을 끝낸 뒤에는 앞의 %d개를 되묻는 짧은 퀴즈 차례가 온다.\n"
        "그 차례는 **네가 정하지 않는다.** 대괄호로 시작하는 지시가 도착하면 그때 그 지시가\n"
        "말하는 번호들만 하나씩 되묻는다. 지시가 오기 전에는 되묻지 않고 진행을 계속한다.\n"
        "되물을 때도 [절대 금지] 1번이 그대로다 — 답을 먼저 말하지 않는다."
        % (quiz_where, int(quiz_group or 0))
    )

    # ── ⑦ 목록을 다 돌았을 때 — 출구. ⛔ 「끝났다」로 읽히는 말을 쓰지 않는다 ───────── #
    blocks.append(
        "[%d번까지 다 돌았으면]\n"
        "같은 번호로 되돌아가지 않는다. 오늘 다룬 표현들을 **섞어서 쓰는 짧은 대화**로\n"
        "넘어간다 — 네가 상황을 하나 주고, 학습자가 그 표현 중 하나를 골라 말하게 한다.\n"
        "이 대화는 계속 이어간다. 학습자가 무엇을 더 해 보고 싶다고 하면 그걸 따른다."
        % total
    )

    if face_rule.strip():
        blocks.append(face_rule.strip())

    return "\n\n".join(blocks) + "\n"


def seed_opening(locale_label: str) -> str:
    """선톡 시드 — 서버가 첫 턴으로 밀어 넣는 user 텍스트(학습자에게 안 보인다).

    ⛔ 「시작하자」만 말하고 **1번 항목의 표현을 적지 않는다** — 시드에 표현이 들어가면
      그게 곧 선공개다.
    """
    return (
        "[지시] 지금 통화가 연결됐다. %s 로 짧게 인사하고, 1번 항목을 바로 시작한다. "
        "인사에 오늘 배울 표현을 미리 말하지 않는다." % locale_label
    )


def first_sentence(text: str, *, limit: int = 220) -> str:
    """수준 서술에서 **첫 문장만** 꺼낸다(나머지는 같은 말의 반복이었다 — 지면 낭비).

    ⚠ 호출부(realtime)가 조립한 긴 프로파일을 그대로 싣지 않기 위한 순수 함수다.
    """
    head = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    for mark in (". ", "。", "! ", "? "):
        idx = head.find(mark)
        if idx > 0:
            head = head[: idx + 1]
            break
    return head[:limit].strip()
