"""통화 중 주입 문구의 **묶음** — 엔진별로 한 벌씩. 고르는 자리는 통화 시작 한 곳뿐이다.

## 왜 이 파일이 있나
통화 중에 서버가 대화에 끼워 넣는 문구가 8종이다(선톡·루프차단·미끄러짐복구·무음1·2·3단·
퀴즈큐·세트안내·드릴안내). 엔진이 둘이면 주입 자리마다 `if openai:` 가 생기는데, 그러면
① 자리 하나를 빼먹는 날 **Gemini 문구가 GPT 통화에 섞이고** ② 그 사실을 아무도 모른다.

⇒ **통화 시작 때 묶음 하나를 고르고, 그 뒤로는 어떤 주입 자리도 엔진을 묻지 않는다.**
`call_session` 의 주입 자리는 `state.seeds.<이름>` 을 꺼내 쓸 뿐 분기가 없다.

## ⛔ 왜 `core/openai/` 안이 아닌가
묶음을 고르려면 **Gemini 판과 GPT 판을 둘 다** import 해야 한다. 그걸 `core/openai/`
안에 두면 그 패키지가 `core.prompts.*` 를 import 하게 되어 격리가 깨진다
(`tests/test_openai_isolation.py` 가 AST 로 막는다). 그래서 **고르는 일만** 여기(도메인
realtime 층)에 두고, `core/openai/prompts/` 는 **GPT 문자열만** 들고 있는다.

## ⛔ Gemini 묶음은 «종전 그대로» 여야 한다
`GEMINI.apply_to()` 는 **아무것도 하지 않는다**(no-op). `call_session` 이 이미 콜타입별로
`close_seed`·`nudge_seed_1`·`nudge_seed_2` 를 정교하게 세팅해 놨고(레벨테스트 캡·차시
프리토킹 예외 포함), 그걸 여기서 다시 조립하면 **바이트가 어긋날 위험만 생긴다.**
묶음이 덮어쓰는 것은 GPT 쪽뿐이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from core.openai.prompts import seeds as gpt_seeds
from core.prompts.locked.rules import CONTROL_TAG
from core.prompts.locked.seeds import EXPRESSION_DRILL_MOVE_ON, LOOP_BREAK_NOTE
from core.prompts.locked.seeds import expression_quiz_cue, expression_quiz_set_reminder

# ⚠ **`call_session._RESUME_SEED` 를 여기로 옮겼다**(복사가 아니라 이동 — 바이트 그대로).
#   묶음이 이 문구를 들고 있어야 주입 자리가 엔진을 안 묻는다. `call_session` 은 종전 이름을
#   이 값으로 다시 노출한다(`cs._RESUME_SEED` 를 보는 회귀가 있다 — test_normalcall_ws.py:2780).
#   ⛔ 글자를 고치지 마라 — 고치면 그 회귀가 «제어 태그 접두» 계약으로 잡는다.
GEMINI_RESUME_SEED = (
    f"{CONTROL_TAG} 이 메시지는 소리내 읽지 마라. 통화는 아직 끝나지 않았다 — 방금 네 발화에 "
    "대사가 아닌 문구가 섞였거나 먼저 작별하려 했는데, 둘 다 하지 마라. 사과·설명·메타 발언 "
    "없이 방금 하던 대화를 그대로 이어서 학습자에게 한마디만 건네라."
)


class SeedBundle(Protocol):
    """`call_session` 의 주입 자리가 의존하는 전부. ⛔ 여기 없는 것을 주입 자리가 직접
    import 하면 묶음이 무의미해진다 — 새 주입 문구가 생기면 **이 Protocol 에 먼저 더해라.**
    """

    name: str
    loop_break: str                              # 같은 문장 2회 연속 → 다음으로
    resume_after_slip: str                       # 제어문 낭독·조기 작별 복구
    drill_move_on: str                           # 한 항목 체류 상한 초과 → 다음 번호

    def quiz_cue(self, labels: str, n: int, *, retry: bool, locale_label: str, target: str,
                 done_labels: list[str] | None, remaining_rows: list[str] | None) -> str: ...

    def quiz_set_reminder(self, labels: str) -> str: ...

    def apply_to(self, state, *, close_tag: str) -> None:
        """`state` 의 **종료 시드·무음 1·2단**을 이 묶음 것으로 맞춘다(필요하면)."""
        ...


@dataclass(frozen=True, slots=True)
class _Bundle:
    name: str
    loop_break: str
    resume_after_slip: str
    drill_move_on: str
    _quiz_cue: Callable[..., str]
    _quiz_set_reminder: Callable[[str], str]
    # None 이면 `apply_to` 가 state 를 **안 건드린다**(= 종전 그대로).
    _nudge_1: Optional[str] = None
    _nudge_2: Optional[str] = None
    # ⭐ 대화 코스(프리토킹·자유대화) 변형 — 표현학습 글자가 그 두 코스에 꽂히던 것을 가른다
    #   (2026-10-08 F16). Gemini 묶음은 전부 None 이라 **종전 바이트 동일**이다.
    _nudge_1_conv: Optional[str] = None
    _loop_break_conv: Optional[str] = None
    _resume_conv: Optional[str] = None
    _close_seed: Optional[Callable[[str], str]] = None

    def quiz_cue(self, labels: str, n: int, *, retry: bool = False,
                 locale_label: str = "학습자의 모국어", target: str = "한국어",
                 done_labels: list[str] | None = None,
                 remaining_rows: list[str] | None = None) -> str:
        return self._quiz_cue(labels, n, retry=retry, locale_label=locale_label, target=target,
                              done_labels=done_labels, remaining_rows=remaining_rows)

    def quiz_set_reminder(self, labels: str) -> str:
        return self._quiz_set_reminder(labels)

    def loop_break_for(self, state) -> str:
        """루프 차단 쪽지 — 대화 코스면 변형. ⛔ 읽는 자리가 한 곳이라 헬퍼로 둔다."""
        if not getattr(state, "expr_items", None) and self._loop_break_conv:
            return self._loop_break_conv
        return self.loop_break

    def resume_after_slip_for(self, state) -> str:
        """미끄러짐 복구 쪽지 — 대화 코스면 변형(「수업은 아직」이 그쪽엔 안 맞는다)."""
        if not getattr(state, "expr_items", None) and self._resume_conv:
            return self._resume_conv
        return self.resume_after_slip

    def apply_to(self, state, *, close_tag: str) -> None:
        """⛔ **여기가 「분기 한 자리」다.** 호출부엔 `if` 가 없다 — 묶음이 자기 몫만 한다.

        Gemini 묶음은 세 값이 전부 None 이라 **아무것도 안 덮는다**(종전 바이트 동일).
        GPT 묶음만 세 값을 갖고 있어 그때만 덮인다.
        """
        if self._close_seed is not None:
            state.close_seed = self._close_seed(close_tag)
        if self._nudge_1 is not None:
            # ⭐ 표현학습이 아니면(= expr_items 가 비면) 대화 변형을 쓴다. 표현학습 글자는
            #   「항목·다음 번호·수업」과 **「학습자의 모국어로 힌트」**를 시켜 100% 목표어를
            #   깨뜨린다. ⚠ `expr_items` 는 `:4184` 에서 이 호출보다 먼저 세워진다.
            conv = self._nudge_1_conv if not getattr(state, "expr_items", None) else None
            state.nudge_seed_1 = conv or self._nudge_1
        if self._nudge_2 is not None:
            state.nudge_seed_2 = self._nudge_2


# ── Gemini(종전) ─────────────────────────────────────────────────────────────── #
# ⚠ `_nudge_1`·`_nudge_2`·`_close_seed` 가 **일부러 비어 있다** — 위 모듈 독스트링 참조.
GEMINI: SeedBundle = _Bundle(
    name="gemini",
    loop_break=LOOP_BREAK_NOTE,
    resume_after_slip=GEMINI_RESUME_SEED,
    drill_move_on=EXPRESSION_DRILL_MOVE_ON,
    _quiz_cue=expression_quiz_cue,
    _quiz_set_reminder=expression_quiz_set_reminder,
)

# ── GPT ──────────────────────────────────────────────────────────────────────── #
# ⛔⛔ **무음 3단만 남겼다**(2026-10-10 사장님 지시). 루프 차단·미끄러짐 복구·드릴 체류·
#   퀴즈 큐·퀴즈 세트 안내는 **빈 문자열**이다 — 보내는 자리가 빈 쪽지를 삼킨다
#   (`_send_note` · 루프차단/복구 자리 · 퀴즈 arm). 되살리려면 커밋 `5b138bc`.
#   ⚠ 필드를 **지우지 않고 비운다**: `SeedBundle` 프로토콜을 Gemini 가 같이 쓰므로
#     자리를 없애면 그쪽이 깨진다. 여기선 「할 말이 없다」로만 둔다.
def _no_note(*_a, **_k) -> str:
    """쪽지를 보내지 않는다 — 빈 문자열이면 호출부가 건너뛴다."""
    return ""


OPENAI: SeedBundle = _Bundle(
    name="openai",
    loop_break="",
    resume_after_slip="",
    drill_move_on="",
    _quiz_cue=_no_note,
    _quiz_set_reminder=_no_note,
    _nudge_1=gpt_seeds.NUDGE_1,
    _nudge_2=gpt_seeds.NUDGE_2,
    _close_seed=gpt_seeds.close_seed,
    _nudge_1_conv=gpt_seeds.NUDGE_1_CONVERSATION,
)

_BY_ENGINE = {"openai": OPENAI}


def for_engine(engine: str | None) -> SeedBundle:
    """엔진 문자열 → 묶음. 모르는 값은 Gemini(종전) — R5 보수 방향.

    ⛔ 엔진 판정을 여기서 **하지 않는다**(`call_service.live_openai_for` 가 유일한 판정).
      여기는 이미 정해진 값을 묶음으로 바꾸는 표 하나다.
    """
    return _BY_ENGINE.get((engine or "").strip(), GEMINI)
