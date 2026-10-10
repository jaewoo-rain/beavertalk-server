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
| 허용을 **턴 단위로 주면 턴을 통째로 쓴다** | call 1754: 비버 9턴 중 **t11·t13 이 라틴 0**(전부 한국어) · t7 혼합. 셋 다 **오답 피드백 턴**이었다 — 당시 규칙 3 의 허용 ②「학습자가 말한 한국어를 고쳐 줄 때」가 턴 단위라 모델이 그 턴 전체의 면허로 읽었다. ⇒ 허용을 **토막 단위**(적힌 표현·학습자 발화 인용)로 내리고, [진행] 턴 틀의 **문장마다 언어를 괄호로 박았다**(2026-10-07) |
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
# ⭐ 한 문장 단어 상한(2026-10-07, call 1757 실측). 문장 **수**만 묶었더니 모델이
#   「긴 문장 3개」로 채웠다 — t3 237자/15.1초 · t9 252자/13.6초 · t13 219자/13.7초
#   (라틴 191자 ≈ 38단어). 초보 학습자가 15초를 듣고만 있었다.
#   ⛔ 턴 상한은 이 값 × 문장 수로 **계산한다** — 두 수를 따로 박으면 서로 모순돼
#     모델이 한쪽을 버린다(프롬프트 README §8 2026-08-22 의 그 사고).
MAX_WORDS_PER_SENTENCE = 8
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
    self_quiz: bool = False,
) -> str:
    """표현학습 대본 — ⭐ **사장님이 정제한 전문을 그대로 쓴다**(2026-10-09).

    ⛔ 이 글자를 「개선」하지 마라. 종전 대본(다섯 차례 튜닝해 3,198자→2,766자였던 것)은
      커밋 `399aee2` 에 있다. 되돌릴 일이 생기면 git 에서 꺼낸다.
    ⭐ `quiz_group` 은 **쓴다**(2026-10-10) — 「3개마다 복습」의 그 수다. 글자로 박지 않고
      서버 상수에서 뽑아, 서버가 바꾸면 대본이 따라오게 한다(출력은 지금도 「3개」다).
    ⚠ 아래 인자는 **받기만 하고 쓰지 않는다** — 새 대본에 그 자리가 없다. 호출부
      (`call_session.py`)가 넘기고 있어 시그니처는 유지한다. 지우려면 호출부도 같이.
        `name` · `level_note` · `max_sentences` · `retry_limit` · `self_quiz`
      ⛔ 「각 문장 최대 2회」는 **전문의 숫자다**. `retry_limit`(기본 3)으로 바꿔 끼우지
        마라 — 서버 출구 상한과 어긋나면 그때 사장님께 묻는다.
    ⚠ 남기는 것은 `[오늘의 표현]`(전문의 「제공된 표현」이 가리키는 자리)과 표정 규칙뿐이다.
    """
    blocks = [
        "# 역할\n"
        "%s\n"
        "%s\n"
        "제공된 %s 표현을 순서대로 하나씩 가르친다."
        % (role, personality, target_language),

        "# 시작\n"
        "%s로 짧게 인사하고 첫 표현을 알려준다." % locale_label,

        "# 연습\n"
        "%s로 뜻·쓰임을 설명하고, %s로 표현·예문을 들려준 뒤 따라 말하게 한다.\n"
        "원문을 맞히면 현지인 표현도 알려주고 따라 말하게 하며, 맞히면 다음 항목으로\n"
        "넘어간다.\n"
        "틀리면 정답 문장을 다시 들려주되 각 문장 최대 2회 후 다음 항목으로 넘어간다."
        % (locale_label, target_language),

        "# 진행\n"
        "설명·반응은 %(l)s로 하며 캐릭터 말투를 유지한다.\n"
        "%(g)d개를 가르칠 때마다 그 %(g)d개를 하나씩 다시 물어 복습한 뒤 다음 %(g)d개로 간다.\n"
        "하나씩 다루고 답을 기다리며, 서버가 종료를 알릴 때까지 이어간다."
        % {"l": locale_label, "g": int(quiz_group)},

        _items_block(items, target=target_language),
    ]
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
