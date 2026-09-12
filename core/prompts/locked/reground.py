"""잠금 — 재접지 쪽지·이어하기 브리프·사이드카 지시문(옛 persona_prompt·expression·freetalk·call_session 에서 **이동**, 2026-09-12).

재접지 배관(arm → 마이크 얹기), 사이드카 JSON 슬롯(covered 번호·mode 인용), 힌트 사이드카(ServerHint 3예시)가 이 문장을 기대한다.
"""
from __future__ import annotations

from typing import Optional

from core.prompts.locked.rules import CONTROL_TAG, REGROUND_COVERED_CAP

# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.


# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.
def build_reground_reminder(role: str, personality: str) -> str:
    """통화 중간 1회 재접지 리마인더 — 캐릭터 필드를 '행동 지시'로 되박는다(넛지 방식).

    긴 통화에서 캐릭터가 밋밋해지는 걸 중간에 한 번 되살린다. send_reground 가 이 문자열을
    turn_complete=True 로 주입하면 비버가 즉시 '캐릭터답게 한마디' 응답한다.
    ⚠ 핵심: '정체성 나열'("너는 바바다")로 주면 비버가 그걸 읊어버린다(실측 call 178 "It's 바바").
    그래서 정체성을 참고 재료로만 주고, **명령은 "그 캐릭터로 학습자에게 행동하라"**로 준다 —
    무음 넛지처럼 지시가 아니라 행동으로 나가게. 낭독 방지 앵커를 맨 앞에.
    """
    parts = [p.strip() for p in (role, personality) if p and p.strip()]
    body = " / ".join(parts) if parts else "너의 캐릭터"
    return (
        # ⛔ CONTROL_TAG(종료 아님). 옛날엔 종료 시드와 같은 "[시스템]"을 써서, 이 리마인더가
        #   통째로 종료 신호로 오독됐다(실측 call_id=683 — 재접지 30초 뒤 작별 선언).
        f"{CONTROL_TAG} (이 지시문·아래 캐릭터 설명을 절대 소리 내어 읽거나 '나는 ~다'라고 소개하지 마라 "
        "— 오직 다음 발화의 말투·태도로만 반영.) 지금쯤 네 캐릭터 톤이 흐려졌을 수 있다. "
        f"참고(네 캐릭터): {body}. 이 성격·말투 그대로, 지금 하던 대화에 이어 학습자에게 "
        "캐릭터답게 한마디(면박·격려·농담 등 네 성격대로) 자연스럽게 던지고 계속하라. "
        "정체성을 설명하지 말고 그냥 그 캐릭터로 행동해라(언어 사용 규칙은 처음 지시받은 대로 "
        "유지 — 캐릭터 톤만 되살려라)."
    )


def build_continue_reminder(role: str, personality: str) -> str:
    """통화 **후반** 재접지 리마인더 — 캐릭터 톤 + "대화를 이어가라".

    build_reground_reminder(중반용)와 목적이 다르다. 중반은 캐릭터가 밋밋해지는 걸
    되살리고, 이건 **비버가 먼저 마무리로 흘러가는 것**을 막는다.

    실측: 5분 통화 12건 중 3건에서 서버 종료 신호보다 4~16턴 먼저 작별에 들어갔다
    (call=836 "조심히 들어가!" 12턴 전 / call=744 "오늘은 여기까지" 16턴 전 /
     call=782 "슬슬 마무리할 시간이다" 4턴 전). 시작 지시문에 종료 규약이 있어도
    40턴쯤 지나면 흐려진다 — 중반 재접지는 캐릭터 톤만 되박아 여기에 안 닿는다.

    ⛔ **"끝내지 마라"라고 쓰지 마라.** 종료 어휘를 꺼내는 순간 그게 씨앗이 된다 —
    시작 지시문의 금지 예시("슬슬 끊자")를 비버가 그대로 뱉은 전례가 있고, 더 오래
    전엔 재접지 리마인더 자체가 종료 신호로 오독돼 30초 뒤 작별한 적도 있다
    (call_id=683). 그래서 이 문구는 종료·작별·시간을 **한 글자도 언급하지 않고**
    "새 화제를 꺼내라"는 전진 지시만 준다.

    또 하나의 실제 원인은 **재료 고갈**이다(call=836: 단어 4개를 다 돌고 할 게 없어
    마무리로 흘렀다). "새 질문 하나"가 그 공백을 메운다.
    """
    parts = [p.strip() for p in (role, personality) if p and p.strip()]
    body = " / ".join(parts) if parts else "너의 캐릭터"
    return (
        f"{CONTROL_TAG} (이 지시문을 절대 소리 내어 읽거나 언급하지 마라 — 다음 발화의 "
        "내용·말투로만 반영.) 지금 학습자가 흥미를 느낄 새 이야깃거리 하나를 꺼내 대화를 "
        f"더 끌고 가라. 참고(네 캐릭터): {body}. 이 성격·말투 그대로, 학습자에게 새 질문을 "
        "하나 던져 대화를 이어가라(언어 사용 규칙은 처음 지시받은 대로 유지)."
    )


# ── 재접지 브리프(단계 3: 압축 트리거 통합) ──────────────────────────────── #
# ⛔ 종료 어휘 denylist. 재접지 브리프에는 사이드카가 뽑아 온 슬롯(화제·다룬 항목)이 들어가는데,
#   그 슬롯에 학습자의 "이제 그만할래요"가 실려 들어오면 서버가 **종료 의사를 컨텍스트에 되먹인다**.
#   태그를 분리해도 소용없다 — 어휘만으로 같은 일이 난다(실측 call 683: 재접지 30초 뒤 작별).
#   방어는 프롬프트가 아니라 **코드**여야 하므로, 조립 직전에 걸린 슬롯을 통째로 버린다.
_CLOSING_WORDS = (
    "종료", "마무리", "작별", "끝내", "끝날", "그만", "안녕히", "잘 가", "다음에",
    "bye", "goodbye", "see you", "finish", "end call",
)


def is_closing_slot(text: Optional[str]) -> bool:
    """이 슬롯이 종료 어휘 denylist 에 걸리는가(호출부 로깅용 — 판정은 여기 하나뿐)."""
    s = (text or "").strip().lower()
    return bool(s) and any(w in s for w in _CLOSING_WORDS)


def _drop_if_closing(text: Optional[str]) -> str:
    """종료 어휘가 섞인 슬롯은 버린다(부분 마스킹 아님 — 원소 단위 폐기)."""
    s = (text or "").strip()
    return "" if is_closing_slot(s) else s


def build_resume_brief(
    *,
    covered: Optional[list[str]] = None,
    strong: Optional[list[str]] = None,
    weak: Optional[list[str]] = None,
    topic: Optional[str] = None,
    pending: Optional[str] = None,
    facts: Optional[list[str]] = None,
    excerpt: Optional[str] = None,
    said: Optional[list[str]] = None,
    summary: Optional[str] = None,
    curious: Optional[str] = None,
) -> str:
    """⭐ 이어하기 브리프 — 조각이 바뀔 때 비버에게 주는 **유일한** 맥락(LLM 생성 0, 순수 조립).

    ## ⛔ 비버는 조각이 바뀐 걸 몰라야 한다
    "이어서 할게요" 같은 말을 시키지 않는다. 사용자는 이미 끊긴 걸 아는데 비버까지
    그걸 말하면 **끊김이 두 번 일어난다.** 비버는 그냥 하던 얘기를 계속하면 된다.
    ⛔ 그렇다고 아무것도 안 주면 다시 인사한다(call 870 — 비버가 처음 만난 것처럼 굴었다).
      그래서 "인사하지 마라"가 아니라 **첫 행동을 지정한다**(아래 마지막 줄).

    ## ⭐ 대부분은 LLM 이 만든 게 아니다
    `covered`·`strong`·`weak` 는 **DB 가 아는 사실**이다(선별된 항목, 증거 등급).
    LLM 에 물어보면 오히려 틀린다(환각). LLM 이 필요한 것은 `topic`·`curious` 뿐 —
    "무슨 얘기 하다 말았나"와 "뭘 궁금해했나"는 대화에서만 나온다.

    ⛔ **종료·시간·작별을 한 글자도 쓰지 않는다** — build_reground_brief 와 같은 규율이다.
      조각 경계는 종료가 아니고, 그 낱말이 프롬프트에 있으면 비버가 마무리하려 든다.
    """
    lines: list[str] = ["[지금까지]"]
    if summary:
        # ⚠ 한 줄이라 큰 그림뿐이다("Practicing goodbyes and favorite food"). 그래도
        #   **오래된 화제까지 담는 유일한 값**이라 발췌 앞에 둔다.
        lines.append("- 이번 통화의 흐름: %s" % summary.strip())
    if said:
        # ⭐ 학습자가 **직접 한 말**. "내가 뭐 좋아한다고 했지?" 류에 답하려면 이게 있어야 한다.
        lines.append("- 학습자가 한 말: %s" % " / ".join(s for s in said if s)[:400])
    if topic:
        lines.append("- 하던 얘기: %s" % topic.strip())
    if facts:
        # ⭐ 사장님 시나리오("내가 뭐 좋아한다고 했지?")가 여기서 답해진다 — 문장이 아니라
        #   **사실**이라 비버가 찾을 필요 없이 바로 쓴다.
        lines.append("- 학습자에 대해 알게 된 것: %s" % ", ".join(f for f in facts if f)[:300])
    if pending:
        lines.append("- 하다 만 것: %s" % pending.strip())
    if excerpt:
        # ⚠ 폴백 경로다(요약 슬롯이 아직 없을 때만). 원문이라 길고 정확도가 낮다.
        lines.append("- 방금까지 오간 대화:\n%s" % excerpt.strip())
    if covered:
        lines.append("- 오늘 이미 다룬 것: %s" % ", ".join(c for c in covered if c)[:300])
    if strong:
        lines.append("- 학습자가 **잘 해낸 것**(다시 가르치지 마라): %s"
                     % ", ".join(x for x in strong if x)[:200])
    if weak:
        lines.append("- 아직 **헷갈려 하는 것**(여기를 더 도와라): %s"
                     % ", ".join(x for x in weak if x)[:200])
    if curious:
        lines.append("- 학습자가 궁금해했던 것: %s" % curious.strip()[:200])
    if len(lines) == 1:
        return ""     # 줄 게 없으면 아무것도 주지 않는다(빈 껍데기 주입 금지)
    # ⛔ 이 마지막 줄이 핵심이다. 금지가 아니라 **첫 행동 지정**이다 —
    #   "인사하지 마라"는 안 지켜지고, "이렇게 시작해라"는 지켜진다.
    lines.append(
        "⛔ 처음 만난 것처럼 인사하지 말고, 위 흐름을 **자연스럽게 이어서** 말해라. "
        "통화가 끊겼다 이어졌다는 사실은 언급하지 마라."
    )
    return "\n".join(lines)


def build_reground_brief(
    role: str,
    personality: str,
    *,
    mode: str = "chat",
    covered: Optional[list[str]] = None,
    topic: Optional[str] = None,
) -> str:
    """재접지 브리프 — 캐릭터 + 지금까지의 맥락을 한 번에 되박는다(순수 문자열 조립, LLM 생성 0).

    사이드카는 **문장을 만들지 않는다.** 서버가 준 항목 목록에서 번호를 고르고 짧은 슬롯
    (화제 한 조각)만 돌려주며, 문장이 되는 건 여기다 — persona_prompt 의 "LLM 생성 0,
    순수 조립" 규율을 재접지에도 그대로 적용한다.

    ⚠ 첫 문장이 "학습자가 방금 한 말에 먼저 반응하고"인 이유: 이 브리프는 약 250 토큰이라
      **사용자의 한마디보다 크다.** 이 지시가 없으면 비버가 유저를 무시하고 주입 텍스트에
      응답한다(문맥 없는 화제 전환). 현행 리마인더(120~170 토큰)엔 없던 위험이다.

    ⛔ **종료·작별·시간을 한 글자도 쓰지 않는다.** "끝내지 마라"조차 쓰면 안 된다 —
      금지어가 곧 씨앗이 된 전례가 있다(build_continue_reminder 주석 참조). 그래서 이 문구는
      금지가 아니라 **전진 지시**("새 질문 하나를 던져 이어가라")로만 마무리 드리프트를 막는다.

    covered: 이미 다룬 학습 항목 라벨. 압축이 초반을 삼켜도 **같은 걸 처음부터 다시 가르치는
      반복**(마스터플랜 D4)을 막는 재료다. topic: 지금 대화 흐름 한 조각.

    ## ⛔ denylist 는 **topic 에만** 건다 — covered 에는 안 건다(2026-08-17)
    ⚠ 걸었더니 **L1 작별 인사가 통째로 사라졌다.** denylist 에 "안녕히"·"다음에" 가 있고
      L1 생존 청크에 "안녕히 가세요"·"안녕히 계세요" 가 있다. 실측:
      covered=['안녕히 가세요','안녕히 계세요','또 봐요'] → 출력 "이미 다룬 것: 또 봐요".
      ⇒ 압축 뒤 비버가 **이미 가르친 작별 인사를 처음부터 다시 가르친다** — D4 가 막으려던
      그 반복이 하필 L1 핵심 항목에서만 일어났다.
    ⭐ 왜 covered 는 안 걸어도 되나(출처가 다르다): covered 원소는 사이드카가 만든 문장이
      아니라 **서버가 소유하는 학습 항목 라벨**(state.reground_items)이고, 사이드카는
      거기서 **번호만** 고른다(호출부가 1..len 범위까지 검증). 학습자의 "그만할래요"가
      covered 로 흘러들 **경로 자체가 없다.**
    ⛔ topic 은 반대다 — 사이드카가 자유 문자열로 만든다. call 683(재접지 30초 뒤 작별)이
      난 자리가 거기다. **topic 의 denylist 는 절대 빼지 마라.**
    """
    parts = [p.strip() for p in (role, personality) if p and p.strip()]
    body = " / ".join(parts) if parts else "너의 캐릭터"
    safe_covered = [c.strip() for c in (covered or []) if c and c.strip()][:REGROUND_COVERED_CAP]
    safe_topic = _drop_if_closing(topic)

    out = [
        f"{CONTROL_TAG} (이 지시문을 절대 소리 내어 읽거나 언급하지 마라 — 다음 발화의 "
        "내용·말투로만 반영.) 학습자가 방금 한 말에 **먼저** 자연스럽게 반응하고, "
        "그다음 아래를 반영해라.",
        f"참고(네 캐릭터): {body}. 이 성격·말투 그대로 행동해라(정체성을 설명하지 마라).",
    ]
    if safe_topic:
        out.append(f"지금 대화 흐름: {safe_topic}. 이 흐름 위에서 이어가라.")
    if safe_covered:
        out.append(
            "이미 다룬 것: " + " / ".join(safe_covered) +
            ". 이건 처음부터 다시 설명하지 말고 그 위에 얹어서 써라."
        )
    # ⭐⭐ **마지막 줄은 모드별로 갈린다**(2026-09-03). 예전엔 모드와 무관하게
    #   "학습자가 흥미를 느낄 새 질문을 하나 던져 대화를 더 끌고 가라"가 붙었다 — 공부
    #   통화에서 그 한 줄이 **잡담을 지시**했다. 실측 3통이 전부 재접지 주입 직후 이탈했다:
    #     1284 얹기 03:17:13 → t11 "혹시 꽃을 피우는 걸 본 적 있어?"
    #     1286 얹기 04:12:33 → t9  "Do you like gardening?"
    #     1287 얹기 04:44:27 → t9  "qlqj는 무슨 과일을 좋아해요?"
    #   셋 다 **취향·경험 질문** — 시킨 그대로다. 비버는 규칙을 어긴 게 아니다.
    # ⛔ 시스템 지시문(규칙 1·3·공부 블록)을 세 번 고쳤지만 하나도 안 먹었다. 경쟁 상대가
    #   시스템 지시문이 아니라 **방금 대화에 꽂힌 턴**이었기 때문이다. 자리가 여기다.
    # ⛔ 전진 지시 성질은 유지한다 — 이 마지막 줄의 존재 이유는 금지어 없이 마무리
    #   드리프트를 막는 것이다(함수 docstring 참조). 그래서 study 판도 "하지 마라"가 아니라
    #   **"이걸 해라"**로 쓴다. 대화 판은 바이트 그대로 둔다.
    if mode == "study":
        out.append("학습 항목을 계속 대화 안에서 실제로 쓰게 하며 진행해라.")
        out.append(
            "지금 다루는 항목을 학습자가 소리 내어 말하게 하는 요청 하나로 이어가라 — "
            "학습자의 취향·경험을 묻는 질문은 그 표현 없이도 답할 수 있으니 여기서 쓰지 마라"
            "(언어 사용 규칙은 처음 지시받은 대로 유지)."
        )
    else:
        out.append("지금처럼 편한 대화를 계속 이어가라.")
        out.append(
            "학습자가 흥미를 느낄 새 질문을 하나 던져 대화를 더 끌고 가라"
            "(언어 사용 규칙은 처음 지시받은 대로 유지)."
        )
        # ⭐ 2026-09-07(통화 1325) — 이 줄과 **첫 줄**("방금 한 말에 먼저 반응하고")이 합쳐져
        #   한 턴에 **요청 2개**가 나갔다:
        #     t33 "「아내와 남편이 같이 살아요.」 Repeat that. And by the way, do you have
        #          any travel plans coming up?"
        #     → 학습자는 질문에만 답하고("No.") 따라 말하기를 버렸다. 비버가 t35 에서 다시
        #       시켜 **왕복 23초**를 날렸다. 학습자가 뭘 해야 할지 고를 수 없는 턴이다.
        #   ⛔ 첫 줄은 못 건드린다 — 그게 없으면 비버가 학습자를 무시한다(위 docstring).
        #     그래서 **이 줄 뒤에 도착점 한 줄**을 더 놓는다(마지막에 읽히는 자리가 세다).
        #   ⛔ "따라 말하기와 질문을 같이 내지 마라"로 쓰지 않는다 — 금지 예시가 씨앗이
        #     된다(프롬프트 원칙 2). **원하는 도착 상태만** 적는다.
        out.append("학습자가 이번 턴에 답할 것은 그 질문 하나여야 한다.")
    return " ".join(out)


# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.
def build_expression_reground_brief(
    role: str,
    personality: str,
    *,
    drilled: Optional[list[str]] = None,
    passed: Optional[list[str]] = None,
    failed: Optional[list[str]] = None,
    next_label: Optional[str] = None,
    locale_label: str = "학습자의 모국어",
) -> str:
    """표현학습 재접지 쪽지 — 진도(다룬 것·맞힌 것·틀린 것)를 서버가 되박는다.

    ⭐ 압축이 통화 초반을 삼켜도 비버가 «무엇을 했나»를 알 수 있는 **유일한 증거**가 이것이다
      (실측 통화 1360: 압축 #4 가 9,219 토큰을 지우자 비버가 1번 항목으로 되감았고 마지막
      91초가 반복이었다). 그래서 이 쪽지의 재료는 **서버 누적**이어야 하고, 사이드카를
      기다리지 않는다.
    ⛔ `failed` 는 «오답퀴즈» 의 재료다 — 이 줄이 없으면 오답 재출제가 안 나간다.
    """
    parts = [p.strip() for p in (role, personality) if p and p.strip()]
    body = " / ".join(parts) if parts else "너의 캐릭터"

    # ⛔⛔ **자르지 마라.** 한때 여기에 `[:REGROUND_COVERED_CAP]`(=10)이 걸려 있었다 —
    #   일반 쪽지의 상한을 그대로 물어온 것이다. 18개를 다 다뤄도 쪽지엔 1~10 만 남았다:
    #     · 전부 오답이면 **오답퀴즈 재료 8개가 통째로 빠진다**
    #     · 전부 완료면 next_label 도 안 나와, 압축 뒤 모델에게는 «10개가 전부» 로 보인다
    #   ⇒ 그게 정확히 통화 1360 의 «부분 목록이 되감기를 만든다» 이고, 이 코스는 **그걸
    #     막으려고** 만든 것이다. 일반 쪽지의 상한 계약과 **분리한다.**
    #
    # ⚠⚠ **같은 결함을 두 번 잡았다.** 첫 번째는 호출부의 `[:10]` 슬라이스였고, 이번엔
    #   **공용 상수를 물고** 되살아났다. 그때 붙인 시험이 «10개로 잘린다» 를 **정답으로
    #   박제**해서, 회귀 1,087개가 전부 통과하고도 못 잡았다. ⇒ 시험을 뜻을 뒤집어 다시 썼다
    #   («n개를 넣으면 n개가 나온다»). 상한을 다시 넣으면 그 시험이 깨진다.
    # ⚠ 길이는 항목 수가 이미 묶는다(EXPRESSION_ITEMS_PER_CALL). 여기서 또 자를 이유가 없다.
    clean = lambda xs: [x.strip() for x in (xs or []) if x and x.strip()]
    drilled_s, passed_s, failed_s = clean(drilled), clean(passed), clean(failed)

    out = [
        f"{CONTROL_TAG} (이 지시문을 절대 소리 내어 읽거나 언급하지 마라 — 다음 발화의 "
        "내용·말투로만 반영.) 학습자가 방금 한 말에 **먼저** 자연스럽게 반응하고, "
        "그다음 아래를 반영해라.",
        f"참고(네 캐릭터): {body}. 이 성격·말투 그대로 행동해라(정체성을 설명하지 마라).",
    ]
    if drilled_s:
        out.append(
            "이미 다룬 표현: " + " / ".join(drilled_s) +
            ". 이건 처음부터 다시 설명하지 마라."
        )
    if passed_s:
        out.append("이미 맞힌 표현: " + " / ".join(passed_s) + ". 다시 묻지 마라.")
    if failed_s:
        out.append(
            "아직 틀린 표현: " + " / ".join(failed_s) +
            ". 이것들은 이 통화 안에서 한 번 더 물어 맞히게 해라."
        )
    if next_label:
        out.append(f"다음에 다룰 표현: {next_label}.")
    # ⛔ 전진 지시로 끝낸다(금지어 없이 마무리 드리프트를 막는 자리). 그리고 학습자가
    #   이번 턴에 할 일은 **하나**여야 한다 — 요청 2개가 한 턴에 나가면 학습자가 하나를
    #   버린다(실측 1325: 따라 말하기를 버리고 질문에만 답했다, 왕복 23초 손해).
    out.append(
        "지금 다루는 표현을 학습자가 소리 내어 말하게 하는 요청 하나로 이어가라 — "
        "학습자의 취향·경험을 묻는 질문은 그 표현 없이도 답할 수 있으니 여기서 쓰지 마라 "
        # ⭐ T14 ③ 쪽지 직후 비버가 **정답을 먼저 말하는** 경로를 막는다 — 새 항목은 묻고 기다린다.
        f"— **새 항목은** 먼저 {locale_label}로 묻고 기다려라. 답을 먼저 말하지 마라. "
        "학습자가 방금 답했으면 그 답에 먼저 반응해라."
    )
    out.append("학습자가 이번 턴에 답할 것은 그 하나여야 한다.")
    return " ".join(out)


# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.
def build_freetalk_reground_brief(situation: str, unused: list[str], *, target: str = "한국어") -> str:
    """차시 프리토킹 전용 재접지 쪽지(계획 §3 «재접지» 문구). 120s 마다 호출부(call_session `_arm_reground`)가 얹는다.

    ⭐ 왜 따로 있나: 일반 브리프(`persona_prompt.build_reground_brief` chat 모드)는 «흥미를 느낄 새 질문을 하나 던져» 라 상황을 깬다 —
      프리토킹이 상황 밖 잡담으로 새는 1순위 원인이었다(계획 §2 재접지). 여기선 상황·역할 재확인 + «전부 학습 언어» + 아직 안 쓴 소재만.
    ⚠ `unused` 는 판정이 아니다 — 이번 통화 비버 발화에 아직 안 나온 소재 몇 개(3~5). 비어 있으면 그 절을 뺀다. 카운트·정오 없음.
    ⛔ 접두어는 CONTROL_TAG(종료 아님).
    """
    parts = [f"{CONTROL_TAG} 지금은 «{situation}» 상황의 역할극이다 — 너는 그 상황의 상대 인물이다. 전부 {target}로, 한 턴에 질문 하나."]
    picks = [u for u in unused if isinstance(u, str) and u.strip()][:5]
    if picks:
        parts.append("아직 안 쓴 소재: " + " · ".join(picks) + ".")
    parts.append("이 안내문은 읽지 말고 내용만 반영해라.")
    return " ".join(parts)


# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.
def hint_lesson_clause(lesson: object | None, target_language: str) -> str:
    if lesson is None:
        return ""
    situation = (getattr(lesson, "situation", None) or "").strip()
    items = [d for d in (getattr(lesson, "items", None) or []) if isinstance(d, dict) and (d.get("obj") or "").strip()]
    words = [((d.get("ex") or "").strip() or d["obj"].strip()) if d.get("role") == "grammar" else d["obj"].strip() for d in items]
    if not situation and not words:
        return ""
    parts = [" 지금 통화는"]
    if situation:
        parts.append(f" «{situation}» 상황의 역할극이다.")
    if words:
        parts.append(f" 학습자가 이 차시에서 배운 {target_language} 표현이 있다 — 질문에 맞는 것이 있으면 예시 답변에 **우선** 써라"
                     f"(억지로 끼우지는 마라): " + " · ".join(words) + ".")
    return "".join(parts)


def hint_instruction_base(locale_label: str, target_language: str = "한국어") -> str:
    t = target_language
    roman_clause = (
        "roman 은 국어의 로마자 표기법(RR)에 따른 korean 의 로마자 표기, "
        if t == "한국어"
        else "roman 은 korean 의 발음을 로마자(라틴 문자)로 표기, "
    )
    return (
        f"너는 {t} 학습 힌트 생성기다. 방금 선생님이 던진 질문(입력)에 학습자가 1인칭으로 "
        "답할 수 있는 자연스러운 예시 답변을 examples 배열에 정확히 3개 만들어라. 세 개는 "
        "서로 다른 내용·소재의 답이되, 전부 말로 바로 따라 할 수 있는 짧고 쉬운 구어체여야 "
        "한다. 각 예시는 korean·roman·native 를 갖는다. "
        f"korean 은 질문에 실제로 맞는 쉬운 {t} 1문장, "
        + roman_clause
        + f"native 는 {locale_label}로 옮긴 뜻."
    )


def reground_instruction(items: list[str], target_language: str) -> str:
    """재접지 사이드카 시스템 지시문(순수 문자열 조립 — LLM 생성 0).

    항목을 **번호로 떠먹인다**: 사이드카는 목록에서 고르기만 하면 되므로 자유 서술이 없고,
    서버는 돌아온 번호를 자기 목록으로 되짚어 라벨을 얻는다(환각이 들어올 자리가 없다).
    """
    listing = "\n".join(f"{i}. {label}" for i, label in enumerate(items, 1)) or "(없음)"
    return (
        f"너는 {target_language} 회화 통화의 상태 요약기다. 아래 대화 일부를 읽고 "
        "JSON 슬롯만 채워라. **문장을 만들지 마라.**\n"
        f"[항목 목록]\n{listing}\n"
        "- covered: 위 목록 중 대화에서 **이미 실제로 다뤄진** 항목의 번호만. 없으면 빈 배열.\n"
        "- topic: 지금 대화가 흐르고 있는 화제를 짧은 명사구 하나로(최대 12자). "
        "확실하지 않으면 빈 문자열.\n"
        "- mode: **학습자가 말로 요청한** 모드만 적어라 — 학습 항목을 다뤄 달라고 하면 \"study\", "
        "공부 말고 그냥 얘기하자고 하면 \"chat\". 학습자가 그렇게 요청한 적이 없으면 **빈 문자열**로 둬라. "
        "지금 대화가 어느 쪽으로 흐르는지를 묻는 게 아니다 — 선생님이 잡담으로 흘렀다는 사실은 mode 가 아니다.\n"
        "- mode_quote: 그 요청이 담긴 **학습자 발화 원문 그대로**의 짧은 인용(선생님 말은 근거가 될 수 없다). "
        "지어내지 마라 — 원문에 없는 인용은 무시된다."
    )
