"""잠금 — 서버가 대화에 끼워 넣는 시드·넛지·종료 문구(옛 persona_prompt·expression·freetalk·call_session 에서 **이동**, 2026-09-12).

무음 3단 넛지(일반·레벨테스트·표현학습·프리토킹 옛/차시판) · 이어하기 시드 · 종료 시드 · 표현학습 퀴즈 큐. 전부 CONTROL_TAG/close_tag 접두 규약(call 683·706)에 묶여 있다.
"""
from __future__ import annotations

from core.prompts.locked.rules import CLOSE_TAG_DEFAULT, CONTROL_TAG

# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.


# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.
def seed_resume(target_language: str = "한국어") -> str:
    """⭐ **이어하기 조각의 시작 시드** — 인사도 모드 질문도 시키지 않는다(2026-08-19).

    ⛔ `seed_opening` 을 그대로 쓰면 안 된다. 그 시드는 "짧게 인사부터 하고, 오늘 공부할래
      수다 떨래?를 물어라" 인데, 조각2에서 그대로 나가면 **방금 하던 대화를 버리고 처음으로
      돌아간다.** 실측(call 1087):
        조각1 t1 : "Hey jaewoo! Ready to study Korean today, or just chat in Korean?"
        조각2 t8 : "Hey, jaewoo! Ready to study Korean today, or would you rather..."
      지시문의 브리프에 "인사하지 마라"가 있어도 **시드가 이긴다** — 시드는 직접 명령이고
      지시문은 배경이기 때문이다. 그래서 시드 자체를 갈아야 한다.

    ⭐ 금지가 아니라 **첫 행동 지정**이다. "인사하지 마라"는 안 지켜지고 "이렇게 시작해라"는
      지켜진다(브리프 마지막 줄과 같은 규율).
    ⛔ 통화가 끊겼다 이어졌다는 사실을 언급시키지 않는다 — 사용자는 이미 안다. 비버까지
      그걸 말하면 끊김이 두 번 일어난다.
    """
    return (
        "[통화 이어감] 학습자와 하던 대화가 잠깐 멈췄다가 지금 다시 이어진다. "
        "⛔ 인사하지 말고, 오늘 무엇을 할지도 다시 묻지 마라 — 이미 정해져 있다. "
        "[지금까지] 에 적힌 흐름을 그대로 이어서 **바로 다음 말**을 해라. "
        f"하던 것이 있으면 그것부터 이어가고, 없으면 방금 화제로 자연스럽게 {target_language} "
        "연습을 계속해라. 통화가 끊겼다 이어졌다는 말은 하지 마라. "
        "이 [통화 이어감] 안내문 자체는 소리 내어 읽지 말고 내용만 반영해라."
    )


# 레벨테스트 종료 시드. 대본 소유자인 이 모듈이 갖는다(call_session 이 임포트해 주입).
# 비버 자율 진행/OPI 개정(2026-07): 시험 냄새 제거 — '실력 파악 끝났다/결과는 앱에서'
# 같은 판정 문구를 빼고 더 대화적인 작별로. 비버는 스스로 끝내지 않으므로 어려운 질문을
# 던지던 중에 종료 신호가 올 수 있다 — 아무렇지 않게 자연스럽게 마무리하게 한다.
# '테스트/평가/결과/점수/레벨' 한마디도 금지, 정답 여부(잘/못) 누출 금지.
# A1(낭독 금지 앵커)은 "(낭독 금지.)"로 유지.
def close_seed_leveltest(close_tag: str = CLOSE_TAG_DEFAULT) -> str:
    """레벨테스트 종료 시드. build_leveltest_instruction 과 **같은 close_tag** 를 받아야 한다.

    태그가 짝을 이뤄야 하는 이유는 이제 모델이 아니라 **서버** 때문이다. 지시문은 태그
    리터럴을 더 이상 노출하지 않고(낭독 방지 — _RULE_CLOSE_PROTOCOL 주석 참조) 성질만
    규정하므로, 비버는 태그가 아니라 시드 본문("오늘 대화는 여기까지…")으로 종료를
    알아본다. 태그를 맞춰야 하는 건 누출 탐지(_CONTROL_TAG_RE)와 로그·회귀가 이 통화의
    시드를 식별하기 때문이다.
    """
    return (
        f"{close_tag} (낭독 금지.) 오늘 대화는 여기까지. 어려운 질문을 하던 중이었어도 아무렇지 "
        "않게 자연스럽게 마무리해라. 학습자 모국어로 '오늘 얘기 즐거웠다, 곧 딱 맞는 수업으로 "
        "다시 보자'처럼 따뜻하게 작별. '테스트/평가/결과/점수/레벨'은 한마디도 금지. 잘했는지 "
        "못했는지도 티내지 마라. 1~2문장. "
        "★ 절대 대괄호 안 문구나 '통화가 종료'·'종료' 같은 말을 입에 담지 마라 — 오직 학습자 "
        "모국어로 친근한 작별 한마디만 해라(로봇 같은 종료 멘트 금지)."
    )


# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.
# ⭐⭐ 조각2·3 전용 시드. ⛔ `persona_prompt.seed_resume` 를 쓰면 안 된다 — 그건 «하던
#   대화를 이어가라» 이지 «다음 번호 항목부터» 가 아니다. 그리고 **시드는 지시문을 이긴다**
#   (실측 call 1087: 지시문에 "인사하지 마라"가 있어도 시드가 인사를 시키자 인사했다 —
#   시드는 직접 명령이고 지시문은 배경이다). 그래서 시드 자체를 갈아야 한다.
# ⛔ 통화가 끊겼다 이어졌다는 사실을 언급시키지 않는다 — 사용자는 이미 안다.
# ⭐ 표현학습 재개 쪽지 재작성(2026-09-14 C6, 사장님 지시 형식 — 실통화 1602 조각2·3: «드릴 4·통과 0» 재료 없는 쪽지가 되감기를 불렀고 비버 턴이
#   조각1 의 1.5배로 길어졐다). 재료 = cur_call.items(drilled/passed/failed 표면형) + call_raw_data 마지막 2~4턴(각 ≤120자). 총 ≤ RESUME_NOTE_MAX_CHARS.
#   seed_expression_resume(조각2 시드 · 비버가 먼저 이어간다)과 brief_expression_silent_resume(silent 조각 지시문 쪽지 · 학습자가 먼저 말한다)이 몸통을 공유한다.
#   조각1 대본(seed_expression_opening·지시문)은 무변경.
RESUME_NOTE_MAX_CHARS = 900
_RESUME_NOTE_LIST_CAP = 12


def _join_cap(items: list[str] | None, cap: int = _RESUME_NOTE_LIST_CAP) -> str:
    xs = [str(x).strip() for x in (items or []) if str(x).strip()]
    if not xs:
        return ""
    head = ", ".join(xs[:cap])
    return head + (" 외 %d개" % (len(xs) - cap) if len(xs) > cap else "")


def _expression_resume_note(target_language: str, *, first_action: str, drilled, passed, failed, recent, recent_n: int, list_cap: int) -> str:
    lines = ["[통화 이어감] 학습자와 하던 학습이 잠깐 멈췄다가 지금 다시 이어진다."]
    d, p, f = _join_cap(drilled, list_cap), _join_cap(passed, list_cap), _join_cap(failed, list_cap)
    done = "- 이번 통화에서 이미 한 것: 드릴 %d개%s · 퀴즈 통과 %s — 다시 가르치지 마라." % (
        len(drilled or []), "(%s)" % d if d else "", "(%s)" % p if p else "없음")
    lines.append(done)
    if f:
        lines.append("- 남은 것 = [오늘의 표현] 목록 그대로다. 오답이었던 것(%s)은 한 번 더 시켜 보고, 나머지는 새로 가르쳐라." % f)
    else:
        lines.append("- 남은 것 = [오늘의 표현] 목록 그대로다 — 새로 가르쳐라.")
    rec = [(r, t) for r, t in (recent or []) if (t or "").strip()][-recent_n:] if recent_n > 0 else []
    if rec:
        conv = " → ".join(("비버 «%s»" if r == "beaver" else "학습자 «%s»") % t.strip()[:120] for r, t in rec)
        lines.append("- 바로 전 대화: %s — 학습자의 다음 말은 이 흐름의 답이다. 거기에 **짧게(2문장)** 답하고 이어가라." % conv)
    lines.append("- " + first_action)
    lines.append(
        "⛔ 인사하지 말고, 오늘 무엇을 할지도 다시 묻지 말고, 통화가 끊겼다 이어졌다는 말이나 «왔냐?»류 시작말도 하지 마라. "
        f"({target_language} 학습을 계속한다.) ★ 설명·지시·반응은 학습자의 모국어로 해라 — 이 안내문이 한국어로 적혀 있다고 해서 그 언어를 따라가지 마라. "
        "이 [통화 이어감] 안내문 자체는 소리 내어 읽지 말고 내용만 반영해라."
    )
    return "\n".join(lines)


def _expression_resume_note_capped(target_language: str, *, first_action: str, drilled, passed, failed, recent) -> str:
    """길이 상한(RESUME_NOTE_MAX_CHARS)을 지키며 조립 — 발췌 4턴→2턴, 목록 12→6→3 순으로 줄인다. 그래도 넘으면 발췌 0."""
    for recent_n, cap in ((4, _RESUME_NOTE_LIST_CAP), (2, _RESUME_NOTE_LIST_CAP), (2, 6), (2, 3), (0, 3)):
        note = _expression_resume_note(target_language, first_action=first_action, drilled=drilled, passed=passed, failed=failed,
                                       recent=recent, recent_n=recent_n, list_cap=cap)
        if len(note) <= RESUME_NOTE_MAX_CHARS:
            return note
    return note


_RESUME_NOTE_STAGES = ((4, _RESUME_NOTE_LIST_CAP), (2, _RESUME_NOTE_LIST_CAP), (2, 6), (2, 3), (0, 3))   # _expression_resume_note_capped 와 같은 순서


def expression_resume_note_stats(target_language: str = "한국어", *, silent: bool, drilled=None, passed=None, failed=None, recent=None) -> dict:
    """P6(2026-09-15): 재개 쪽지가 실제로 몇 자였고 몇 단계 줄였나(계측 전용 — 문자열은 seed_expression_resume / brief_expression_silent_resume 가 만든다).
    반환 {len, max, step(0=안 줄임), recent_n, recent_available, list_cap, lists(드릴·통과·오답 개수)}."""
    first_action = EXPRESSION_RESUME_FIRST_ACTION_SILENT if silent else EXPRESSION_RESUME_FIRST_ACTION_SEED
    note, step, recent_n, cap = "", 0, 0, 0
    for step, (recent_n, cap) in enumerate(_RESUME_NOTE_STAGES):
        note = _expression_resume_note(target_language, first_action=first_action, drilled=drilled, passed=passed, failed=failed,
                                       recent=recent, recent_n=recent_n, list_cap=cap)
        if len(note) <= RESUME_NOTE_MAX_CHARS:
            break
    return {
        "len": len(note), "max": RESUME_NOTE_MAX_CHARS, "step": step, "recent_n": recent_n,
        "recent_available": len([1 for _r, t in (recent or []) if (t or "").strip()]), "list_cap": cap,
        "lists": (len(drilled or []), len(passed or []), len(failed or [])),
    }


EXPRESSION_RESUME_FIRST_ACTION_SEED = (
    "지금 바로 이어가라: 학습자의 마지막 말에 짧게 답한 뒤 [오늘의 표현] 목록의 **맨 앞 항목**으로 가라."
)
EXPRESSION_RESUME_FIRST_ACTION_SILENT = (
    "⛔ 학습자가 먼저 말한다 — 먼저 말을 꺼내지 말고 기다렸다가, 학습자의 말에 짧게 답한 뒤 [오늘의 표현] 목록의 **맨 앞 항목**으로 가라."
)


def seed_expression_resume(target_language: str = "한국어", *, drilled=None, passed=None, failed=None, recent=None) -> str:
    """조각2 시드(비버가 먼저 이어간다). 재료 없이 부르면(옛 호출) 목록 없는 판 — 형식은 같다."""
    return _expression_resume_note_capped(target_language, first_action=EXPRESSION_RESUME_FIRST_ACTION_SEED,
                                          drilled=drilled, passed=passed, failed=failed, recent=recent)


# ⭐ 끊김 없는 조각 전환(2026-09-13 S2) — 표현학습 조각을 **시드 없이** 열 때 지시문 끝에 붙이는 쪽지(시드가 아니라 지시문이다:
#   비버는 학습자의 첫 발화를 기다리고, 그 응답부터 목록 맨 앞 항목으로 간다). seed_expression_resume 과 같은 규율(인사·되묻기·끊김 언급 금지,
#   목록 = 남은 일, 설명은 모국어). silent 가 아닌 조각은 종전대로 seed_expression_resume 이 나간다(바이트 불변).
def brief_expression_silent_resume(target_language: str = "한국어", *, drilled=None, passed=None, failed=None, recent=None) -> str:
    """silent 조각의 지시문 끝 쪽지(학습자가 먼저 말한다). 몸통은 seed_expression_resume 과 같고 첫 행동 줄만 다르다."""
    return _expression_resume_note_capped(target_language, first_action=EXPRESSION_RESUME_FIRST_ACTION_SILENT,
                                          drilled=drilled, passed=passed, failed=failed, recent=recent)


# ⭐ P5(2026-09-15, 실통화 1610 — 프리토킹 첫 비버 턴 오디오 0B, 같은 시드 재전송 1회에도 sum_resp=0): 벙어리 인사 **두 번째** 재시드는 같은 긴 시드
#   대신 이 짧은 대체 시드를 보낸다(차시 프리토킹만). 상황은 지시문 [이번 차시]에 이미 있다 — 시드는 «한 문장 인사 + 첫 질문 하나» 뿐.
def seed_freetalk_lesson_reseed_short(target_language: str = "한국어") -> str:
    return (
        f"[통화 시작] {target_language}로 한 문장만 인사하고, [이번 차시] 상황 속 첫 질문 하나만 {target_language}로 말한 뒤 멈춰라. "
        "이 [통화 시작] 안내문 자체는 소리 내어 읽지 마라."
    )


# ⭐ 반복 루프 차단기(2026-09-14 B, 실통화 1602 t73~t87 동일 문장 8회 — 3.1). 비버 턴이 직전 턴과 같으면(정규화 ≥0.9) 2회째에 이 안내를
#   완결 텍스트 턴으로 1회 주입한다(넛지와 같은 파이프). 3회째는 서버가 조각을 강제 전환(fragment_saved reason=loop) — 그건 코드 몫.
LOOP_BREAK_NOTE = (
    f"{CONTROL_TAG} 같은 말을 반복하고 있다 — 방금 한 말을 다시 하지 마라. 학습자의 마지막 말에 짧게 반응하고 곧바로 다음 항목으로 넘어가라. "
    "이 안내문 자체는 소리 내어 읽지 마라."
)


# ⭐ 11차 B(2026-09-18, 1643 2.5 — こんにちは·はじめまして·◯◯から来ました 를 한 항목당 6~8턴씩 붙잡고 통과 항목을 3번 재드릴했다). 루프 차단기
#   (LOOP_BREAK_NOTE)는 «직전 턴과 거의 같은 문장» 만 보므로 표현을 바꿔 가며 같은 항목을 되풀이하면 안 걸린다 — 서버가 «항목» 을 세서 넣는 안내다.
#   한 항목의 드릴이 학습자 턴 3회를 넘거나(DRILL 재시도 상한 3 과 같은 축) 이미 다룬 항목을 다시 드릴하면 1회, 통화당 3회까지.
EXPRESSION_DRILL_MOVE_ON = (
    f"{CONTROL_TAG} 그 표현은 충분히 했다 — 다음 번호 항목으로 넘어가라. 이미 다룬 표현은 다시 연습시키지 말고 아직 안 한 가장 앞 번호부터 이어가라. "
    "이 안내문 자체는 소리 내어 읽지 마라."
)


# --------------------------------------------------------------------------- #
# 무음 넛지 1단 (기획서 §2-9)
# --------------------------------------------------------------------------- #
# ⛔⛔ 일반 통화의 1단 시드("가볍게 새 화제로 이어가라")를 쓰면 **그 항목을 건너뛴다.**
#   표현학습에서 무음은 «대화가 끊겼다»가 아니라 **«학습자가 지금 항목을 못 하고 있다»**다.
#   화제를 바꾸면 안 되고, 그 항목을 더 쉽게 다시 물어야 한다 — 레벨테스트 시드와 같은 성격.
# ⚠ 임계는 일반과 같게 둔다(60s). ⛔ 줄이지 마라 — 드릴은 학습자가 문장을 떠올리는
#   시간이다. 레벨테스트에서 25초로 줄였다가 "생각 중에 넛지가 끼어들었다"로 되돌린
#   전례가 있다(call_session.LEVELTEST_IDLE_NUDGE1_S 주석).
# ⛔ 접두어는 CONTROL_TAG(종료 아님) — 종료 태그와 절대 공유하지 마라(call_id=683).
NUDGE_SEED_1_EXPRESSION = (
    f"{CONTROL_TAG} 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고 "
    "**화제를 바꾸지 말고**, 퀴즈 중이면 정답 대신 힌트 하나만 주고, 드릴 중이면 한 번 더 "
    "들려준 뒤 따라 말해 보라고 네 캐릭터대로 한 번만 청하라."
)


# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.
# --------------------------------------------------------------------------- #
# 무음 넛지 1단 (기획서 §2-9)
# --------------------------------------------------------------------------- #
# ⚠ 일반 통화와 **성격은 같고 언어만 다르다** — 새 화제로 이어가되 학습 언어로 한다.
#   (표현학습과 달리 여기서는 «화제를 바꾸지 마라»가 필요 없다. 다룰 항목이 없다.)
# ⛔ 접두어는 CONTROL_TAG(종료 아님) — 종료 태그와 절대 공유하지 마라(call_id=683).
NUDGE_SEED_1_FREETALK = (
    f"{CONTROL_TAG} 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고 "
    "학습 언어로 가볍게 새 화제 한 문장만 이어가라."
)


# ⭐ 차시판 무음 시드(계획 §3 «시드 3종» → §10 역할극). 차시 프리토킹에서 무음은 «대화가 끊겼다» 가 아니라 «방금 질문을 못 알아들었다» 다 —
#   1단은 화제를 바꾸지 말고 **같은 질문을 더 쉽게**, 2단은 그 턴만 선생님으로 돌아와 모국어 뜻 + 학습 언어 문장 하나 → 학습 언어로 청함 → 다시 역할.
#   (옛 경로·표현학습·일반의 1·2단 시드는 바이트 그대로 — 호출부가 state.nudge_seed_1/2 슬롯으로 코스별로 꽂는다.)
# ⛔ 접두어는 CONTROL_TAG — 종료 태그와 공유 금지(call 683).
NUDGE_SEED_1_FREETALK_LESSON = (
    f"{CONTROL_TAG} 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고, 화제를 바꾸지 말고, "
    "방금 한 질문을 더 쉬운 학습 언어로 바꿔(짧은 말, 쉬운 낱말, 또는 둘 중 고르기) 한 번만 다시 물어라."
)


NUDGE_SEED_2_FREETALK = (
    f"{CONTROL_TAG} 학습자가 계속 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고, 화제를 바꾸지 마라. "
    "이번 한 턴만 선생님으로 돌아와 학습자의 모국어로 방금 질문의 뜻과 학습자가 할 학습 언어 문장 하나를 통째로 들려준 뒤, "
    "학습 언어로 그 문장을 말해 보라고 청해라. 다음 턴부터는 다시 그 인물로, 학습 언어다."
)


# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.
# normal 통화 전용 종료 시드. 레벨테스트는 persona_prompt.close_seed_leveltest(대본 소유자).
# close_tag 는 통화별 난수 태그(new_close_tag) — system_instruction 과 **반드시 같은 값**.
#
# ⛔⛔ **"이 턴은 예외다" 줄을 빼지 마라**(2026-08-22, 실측 call_id=1137·1097).
#   이 시드는 시스템 지시문과 **정면으로 싸운다**:
#     · 규칙 2 는 라벨부터 "대화 지속(**매우 중요**)" 이고 "곧바로 새 화제나 질문을
#       하나 던져 이어가라" 고 한다.
#     · 규칙 3 [착지] 는 "맨 끝을 질문·요청으로 착지시켜라(물음표로 끝내고 멈춰라)".
#     · 이 시드는 정반대 — "질문 시작하지 말고 평서문으로 작별해라".
#   시스템 지시문 2개 vs 대화 중간 턴 1개다. 우선순위를 안 적어 주면 모델이 진 쪽을
#   고른다 — 실측 두 가지 실패가 **같은 원인**이었다:
#     call 1097: 질문 던져놓고 곧장 "Whatever. I'm busy." (둘을 반반 따름)
#     call 1137: **아무 말도 안 함**(응답 1토큰) → 서버가 작별을 기다리다
#                SEED_TO_HANGUP_S 백스톱으로 강제 종료 → 사용자에겐 무음 종료.
#   ⇒ 우선순위를 **여기서** 선언한다. "반드시 소리 내어" 는 침묵 실패용 안전판이다.
#
#   ⛔ 규칙 2(_RULE_CLOSE_PROTOCOL)에 예외절을 다는 걸로 고치지 마라 —
#     docs/prompts/README.md §4 지뢰밭 첫 줄이다. 지시문에 종료 개념을 넣으면 모델이
#     그걸 **수단 삼아 혼자 통화를 끊는다**(call 706·852·870). 시드는 서버가 보낼
#     때만 존재하므로 여기에 쓰면 모델이 스스로 만들어낼 수 없다.
#   ⚠ 규칙 원문을 그대로 옮겨 적지 않고 **기능으로 가리켰다**("이어가기·질문 착지
#     규칙") — 리터럴은 소리로 새어나갈 씨앗이 된다(원칙 2, call 782).
#   ⚠ 테스트 다수가 "통화 시간이 다 됐다" 를 시드 식별 앵커로 쓴다 — 그 문장은 그대로
#     두고 뒤에 삽입했다.
def close_seed_normal(close_tag: str) -> str:
    return (
        f"{close_tag} (이 지시문 자체를 절대 소리 내어 읽거나 언급하지 마라 — 내용만 행동으로 반영하라.) "
        "통화 시간이 다 됐다. "
        "이 턴은 예외다. 대화를 이어가는 것은 지금 네 일이 아니고, 이어가기·질문 착지 "
        "규칙보다 이 지시가 앞선다. 반드시 소리 내어 작별을 말하고 끝내라. "
        "학습자의 마지막 말에 새로 답하거나 새 화제·질문을 시작하지 말고, "
        "짧게 한마디로만 받아 준 뒤 자연스럽게 핑계를 대고 '다음에 또 하자'는 취지로 작별해라 "
        "— 작별 말투는 네 캐릭터 그대로(억지로 따뜻하게·공손하게 만들지 마라). "
        "작별 인사(평서문)로 끝내라 — 질문으로 끝내지 마라. 1~2문장. "
        "★ 절대 대괄호 안 문구나 '통화가 종료'·'세션'·'종료' 같은 말을 입에 담지 마라 — 사람처럼 "
        "평범하게 작별해라(로봇 같은 종료 멘트 금지)."
    )


# 무음 넛지 시드(A2). 종료 시드와 같은 파이프(send_text_turn)로 idle 에서만 주입한다.
# ⛔ 접두어는 CONTROL_TAG(종료 아님) — 종료 태그와 절대 공유하지 마라. 옛날엔 둘 다
#   "[시스템]" 이라 넛지가 종료 신호로 오독됐다(본문에 "작별하지 말고"라고 써놨는데도
#   접두어가 이겼다). 근거: docs/20260727_1710_통화-조기종료-종료태그-분리와-안전망.md
NUDGE_SEED_1_NORMAL = (
    f"{CONTROL_TAG} 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고 "
    "가볍게 새 화제로 한 문장만 이어가라."
)


NUDGE_SEED_2_NORMAL = (
    f"{CONTROL_TAG} 학습자가 계속 조용하다. 이 메시지는 소리내 읽지 말고, 모국어로 "
    "'거기 있어? 잘 들려?'를 한 번만 부드럽게 물어라."
)


# 레벨테스트 1단 넛지: 일반과 달리 '새 화제로 이어가라' 대신 **방금 질문을 다시 묻는다** —
# 작별하지 말고 방금 한 질문을 더 쉽게 바꾸거나 선택지를 주며 모국어로 다시 묻게 한다.
NUDGE_SEED_1_LEVELTEST = (
    f"{CONTROL_TAG} 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고 "
    "방금 한 질문을 더 쉽게 바꾸거나 선택지를 주며(예/아니오 또는 둘 중 고르기) "
    "모국어로 딱 한 번만 다시 물어라."
)


# ⛔⛔ 절대 고치지 마라 — 서버 판정/표정/진도 배관이 이 문장을 **그대로** 기대한다(gemini 2.5·3.1 두 모델 모두 같은 문장을 쓴다).
#   고치려면 bt-back 과 시험(tests/test_prompt_locked_hash.py · tests/test_prompt_*.py)을 같이. 사람이 고치는 문구는 core/prompts/editable/*.md 다.
# 표현학습 퀴즈 큐(T16 — 재접지 배관과 별개 슬롯으로 마이크·RMS 관문에 얹힌다). 하네스 로그 계약은 call_session.EXPR_QUIZ_CUE_LOG_PREFIX.
def expression_quiz_cue(
    labels: str, n: int, *, retry: bool, locale_label: str, target: str,
    done_labels: list[str] | None = None, remaining_rows: list[str] | None = None,
) -> str:
    """큐 문구(시스템 텍스트, CONTROL_TAG 접두 — ⛔ «[시스템]» 은 종료 태그와 같던 시절 태그라 금지, call 706).

    ⭐ 4차 C(2026-09-15, 사장님 규칙 ① «배운 건 뒤에 다시 안 배운다»): done_labels(이미 다룬 표현)·remaining_rows(남은 표현 «번호. 뜻 = 표면형»)를 받으면 끝에
      두 줄을 붙인다 — 세션 지시문은 중간에 못 바꾸니 큐가 서버 목록으로 되박는다. 둘 다 None/빈 목록이면 **종전 바이트 동일**.
    """
    lead = "아까 틀린" if retry else "방금 배운"
    base = (
        f"{CONTROL_TAG} 지금 퀴즈를 내라. {lead} {labels} {n}개를 한 문제씩 — {locale_label}로 뜻·상황을 주고 "
        f"{target}로 말하게 하라. 정답을 먼저 말하지 마라. 한 턴에 한 문제만 — 교정하는 턴에도 새 문제를 붙이지 마라. "
        "정답을 들려줬으면 따라 말하게만 하고 그 턴을 끝내라 — 같은 문제를 «어떻게 말해요?» 로 다시 묻지 마라. "
        f"이 {n}개만, 적힌 순서대로 물어라 — 다른 표현은 지금 묻지 마라({n}개를 다 물은 뒤에 다음 새 표현으로 넘어간다). "
        "네 말에 대괄호나 '퀴즈 시작' 같은 단계 표시를 넣지 마라 — 그냥 말로 내라."
    )
    extra: list[str] = []
    if done_labels:
        extra.append("이미 다룬 표현: " + " / ".join(done_labels) + " — 다시 가르치지 마라.")
    if remaining_rows:
        extra.append("퀴즈 뒤 새로 가르칠 남은 표현(번호·뜻): " + " · ".join(remaining_rows) + " — 이 순서로, 이것만 새로 가르쳐라.")
    return base + (" " + " ".join(extra) if extra else "")


# ⭐ 4차 A(2026-09-15, 사장님 «배운 거 체크는 비버가 말하는 것만»): 비버 턴마다 «이 턴에서 다룬 항목» 을 고르는 판정기 지시문. 문자열 대조로는 비버가
#   모국어로 «'고마워요'는 일본어로?» 라고 물은 턴(표면형 없음)을 못 잡았다(실측 1614 — 쪽지 «드릴 4» 인데 실제 6개↑ → 압축 뒤 되가르침).
def expression_taught_judge_instruction(rows: list[str], *, target: str, locale_label: str, done_rows: list[str] | None = None) -> str:
    """8차 C(2026-09-15, 1624 2.5 세트 이탈): done_rows(이미 다룬 항목)를 주면 «이 턴에서 **다시** 물은 번호» 를 retaught 로 받는다 — 서버가 퀴즈 중
    세트 이탈을 알아채 안내를 1회 넣는다. 없으면 종전 바이트."""
    done = [] if not done_rows else [
        "",
        "[이미 다룬 항목 — 선생님이 이번 턴에서 **다시** 물었으면 그 번호만 retaught 에 적어라(새로 가르친 것이 아니다. 없으면 빈 목록)]",
        chr(10).join(done_rows),
    ]
    return chr(10).join([
        f"너는 {target} 표현학습 통화의 보조 판정기다. 선생님(B)은 학습자에게 {target} 표현을 하나씩 가르치고, 설명은 {locale_label}로 할 수 있다.",
        "아래 [남은 항목] 가운데 선생님이 **이번 B 턴에서** 다룬 항목의 번호를 모두 골라라. 다뤘다 = 그 표현을 들려줬거나, 뜻·상황을 설명하며 "
        "어떻게 말하는지 물었거나, 따라 말하게 했거나, 퀴즈로 물었다.",
        "표기가 달라도(가나·한자·로마자·한글 음차·학습자 모국어로 된 뜻) 그 항목을 가리키면 다룬 것이다.",
        "U 줄(직전 학습자)은 문맥일 뿐이다 — 학습자만 말하고 선생님이 이 턴에서 다루지 않은 항목은 넣지 마라.",
        "뜻이 비슷한 다른 항목과 헷갈리면 넣지 마라(확실한 것만). 다룬 항목이 없으면 빈 목록. 번호 외의 문장을 만들지 마라.",
        "",
        "[남은 항목]",
        chr(10).join(rows) or "(없음)",
        *done,
    ])


# ⭐ 4차 B(2026-09-15): 퀴즈 창 안 학습자 턴마다 «맞혔나» 를 항목별로 판정하는 지시문. 판정 기준 4줄이 명세다(bt-back·사장님):
#   표기·문자 체계가 달라도 그 표현이면 통과 · 비버가 알려준 뒤 따라 말하면 failed · 정중형 항목에 반말만이면 통과 아님 · 말 안 했으면 pending.
# ⭐ 8차 C(2026-09-15, 1624 2.5): 퀴즈 창이 열렸는데 비버가 «세트 밖 + 이미 다룬» 항목을 다시 물으면 서버가 넣는 안내(통화당 2회).
def expression_quiz_set_reminder(labels: str) -> str:
    return (
        f"{CONTROL_TAG} 지금 낼 문제는 {labels} 뿐이다 — 이미 다룬 다른 표현은 다시 묻지 말고, 남은 문제를 적힌 순서대로 하나씩 내라. "
        "이 안내문 자체는 소리 내어 읽지 마라."
    )


def expression_quiz_verdict_instruction(rows: list[str], *, target: str, locale_label: str, extra_rows: list[str] | None = None) -> str:
    """5차 A-2(2026-09-15): extra_rows(세트 밖 남은 항목)를 주면 «선생님이 이 창에서 실제로 물은 경우에만» 판정하라는 칸을 덧붙인다(1615 #13·1616 #7 —
    비버가 세트 밖을 물었고 학습자가 맞혔는데 기록 0). 없으면 종전 바이트."""
    extra = [] if not extra_rows else [
        "",
        "[세트 밖 항목 — 선생님이 이 전사에서 **실제로 물은** 경우에만 판정하라. 묻지 않았으면 pending]",
        chr(10).join(extra_rows),
    ]
    return chr(10).join([
        f"너는 {target} 표현학습 퀴즈의 판정기다. 전사의 각 줄은 «B번호:»(선생님) 또는 «U번호:»(학습자)로 시작한다. "
        f"선생님은 {locale_label}로 뜻·상황을 주고 {target}로 말하게 묻는다.",
        "아래 [퀴즈 항목] 각각에 대해 verdict 를 하나 골라 num 과 함께 답하라:",
        "- passed: 학습자(U)가 선생님이 정답을 말해 주기 **전에** 그 표현을 말했다. 판단 기준은 글자가 아니라 **읽기(발음)** 다 — 읽기가 같으면 "
        "표기·문자 체계가 무엇이든(가나·한자·로마자·한글 음차, 같은 읽기의 다른 한자 포함) 통과다. 받아쓰기가 조금 틀려도 그 읽기로 보이면 통과다.",
        "- 단, **읽기가 다른 낱말**이면 소리가 비슷해도 통과가 아니다 — failed 다(예: «고향» 을 물었는데 «고양이», «こんばんは» 를 물었는데 «こんにちは»).",
        "- 단, 읽기는 **항목 전체**의 읽기여야 한다 — 항목의 앞부분만 말하고 **끝맺음을 빠뜨린 것**(«です»·«ますか»·«-요»·«-습니다» 등)은 "
        "«받아쓰기가 조금 틀린 것» 이 아니라 **다른 말**이다. 통과가 아니다(예: «本当ですか» 를 물었는데 «本当?», «감사합니다» 를 물었는데 «감사»).",
        "- failed: 선생님이 그 표현(정답)을 먼저 들려준 뒤에야 학습자가 따라 말했거나, 학습자가 틀리게 말해 선생님이 정답을 알려줬다.",
        "- 정중형을 가르치는 항목(です·ます·-요·-습니다 등으로 끝나는 표현)에서 학습자가 반말(보통형)만 말했으면 passed 가 아니다 — "
        "**이 규칙이 위의 «읽기가 같으면 통과» 보다 우선한다.** 선생님이 정답을 알려줬으면 failed, 아니면 pending.",
        "- 선생님이 그 항목을 물은 **직후의 학습자 발화는 틀려도 답이다** — 그 표현이 아니면 failed 다. 아래 pending 경우가 아니면 학습자가 한 말은 무엇이든 "
        "(엉뚱한 낱말·짧은 조각·«맞아요»·«그래요» 처럼 무언가를 인정·주장하는 대꾸까지) **답으로 친다** — 회피가 아니라 틀린 답이다.",
        "- pending: 학습자가 그 항목에 아직 답하지 않았다 — 무응답이거나, 모른다고 말했거나(«모르겠어요»·«몰라요»·«기억이 안 나요»·«힌트 주세요»), "
        "질문을 다시 해 달라고 했거나(«뭐라고요?»·«다시 말해 주세요»), 알아들었다·기다려 달라는 반응만 했을 때(«알겠어요»·«네»·«잠시만요»·«잠깐만요»)뿐이다. "
        "«맞아요» 는 이 반응이 아니라 틀린 답이다. 선생님이 아직 묻지 않은 항목도 pending.",
        "뜻이 다른 표현(예: 안녕히 가세요 / 안녕히 계세요)은 같은 표현이 아니다. why 는 20자 이내. 목록에 없는 번호를 만들지 마라.",
        "",
        "[퀴즈 항목]",
        chr(10).join(rows) or "(없음)",
        *extra,
    ])


def expression_quiz_fallback_instruction(rows: list[str], *, target: str) -> str:
    """STT 폴백 판정기 지시문 — 미판정 항목만, 세그먼트 번호로 답한다(순수 문자열 조립). rows = «n. 표면형 — 뜻 — 예문» 줄들."""
    return chr(10).join([
        f"너는 {target} 표현학습 퀴즈 전사의 보조 판정기다. 전사의 각 줄은 «B번호:»(선생님) 또는 «U번호:»(학습자)로 시작한다.",
        "아래 항목은 서버가 전사에서 표현을 **글자로 찾지 못한** 것이다 — 받아쓰기(STT)가 표현을 조금 다르게 적었을 수 있다.",
        "각 항목에 대해, 학습자가 **스스로**(선생님이 정답을 말해 주기 전에) 그 표현을 냈다고 볼 수 있는 U 줄이 있으면 그 번호를 "
        "answer_seg 에 적어라. 없으면 null. 선생님이 정답을 먼저 말한 뒤 학습자가 따라 말한 것은 answer_seg 가 아니다.",
        "⚠ 뜻이 다른 표현(예: 안녕히 가세요 / 안녕히 계세요)은 같은 표현이 아니다. 확실하지 않으면 null.",
        "번호 외의 문장을 만들지 마라.",
        "",
        "[항목]",
        chr(10).join(rows) or "(없음)",
    ])
