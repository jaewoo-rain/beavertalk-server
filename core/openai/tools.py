"""OpenAI Realtime 툴 선언 — `set_face` 하나.

## ⛔ 이 파일은 운영(Gemini) 선언을 import 하지 않는다
`core/openai/` 의 격리 규칙(`tests/test_openai_isolation.py`)이 그걸 기계로 막는다.
대신 **벤더 중립인 두 가지 사실**만 밖에서 가져온다(값만 베껴 적고, import 는 안 한다):

1. **인자 이름과 값 집합은 프론트 계약이다.** 서버는 툴콜을 받아
   `sentence` 프레임의 `emotion` 필드로 그대로 실어 보낸다
   (`domains/learning/realtime/protocol.py:306` `emotion: str`).
   앱은 모르는 값을 neutral 로 떨어뜨리므로(같은 파일 docstring) **값을 늘려도 앱
   배포가 필요 없다**. 지금 쓰는 5종은 아바타 클립이 있는 것들이다.
2. **인자 키는 `emotion` 이다.** 2026-10-04 GPT 재측정에서 `{"emotion":"happy"}` 형태로
   **3/3 정상** 왕복했다. (10-03 스파이크 스크립트는 `face` 라는 다른 키를 썼는데,
   그건 프론트 계약과 어긋나는 스파이크 전용 값이었다 — 되살리지 마라.)

## 왜 설명 문구를 길게 안 쓰나
GPT 는 `set_face` 를 블로킹 함수콜로 다루고, 툴이 낀 응답은 **Response 가 2개**가 된다
(실측 — 10-03 §4). 즉 **부르는 횟수가 곧 원가**다. 그래서 "언제 부르나"를 한 줄로
못박고(감정이 실제로 드러나는 턴만), 평범한 턴에는 부르지 말라고 **명시**한다.
⚠ 표정이 꺼진 통화(무료)에는 이 선언 자체를 싣지 않는다 — 호출부가 결정한다.
"""

from __future__ import annotations

SET_FACE_NAME = "set_face"

# ⭐ 아바타 클립이 있는 감정 5종. 앱이 모르는 값은 neutral 로 떨어뜨리므로 늘려도 안전하다.
#   ⛔ `neutral` 을 넣지 마라 — 앱이 클립 재생 뒤 **스스로** idle 로 돌아온다(복귀 호출이
#     턴당 호출을 2배로 만든다 = 원가 2배).
SET_FACE_EMOTIONS: tuple[str, ...] = ("happy", "surprised", "sad", "angry", "laugh")

# 어댑터가 받는 즉시 **자동으로 답하는** 툴. 모델은 블로킹 호출의 응답을 기다리므로
# 늦게 답하면 그 턴이 멈춘다 — 받은 자리에서 바로 답하고 소비측에 `auto_acked=True` 로 알린다.
AUTO_ACK_TOOLS = frozenset({SET_FACE_NAME})

_EMOTION_DESCRIPTION = (
    "happy=기쁨·반가움 / surprised=놀람·의외 / sad=안타까움·미안함 / "
    "angry=못마땅함·핀잔 / laugh=웃음이 터질 만큼 재미있음(happy 보다 강하다)"
)

_SET_FACE_DESCRIPTION = (
    "화면 속 얼굴 표정을 바꾼다. 이 호출은 소리가 아니다 — 말할 내용과 따로 움직인다. "
    "감정이 실제로 드러나는 턴에만, 말을 시작하기 직전에 한 번 부른다. "
    "감정이 드러나지 않는 평범한 턴에는 부르지 않는다(평소 얼굴로 저절로 돌아간다)."
)


def set_face_tool() -> dict:
    """session.update 의 `tools` 에 그대로 들어가는 함수 선언 1개.

    OpenAI Realtime 의 툴 스키마는 **평평하다** — `{"type":"function", "name", "description",
    "parameters"}`. (Chat Completions 처럼 `{"type":"function","function":{...}}` 로 한 겹
    감싸면 안 받는다.) 근거: developers.openai.com Realtime 가이드 session.update 예시.
    """
    return {
        "type": "function",
        "name": SET_FACE_NAME,
        "description": _SET_FACE_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "emotion": {
                    "type": "string",
                    "enum": list(SET_FACE_EMOTIONS),
                    "description": _EMOTION_DESCRIPTION,
                },
            },
            "required": ["emotion"],
            "additionalProperties": False,
        },
    }


def face_rule_block() -> str:
    """지시문에 붙는 `[표정]` 블록. 표정이 켜진 통화에만 붙인다(없으면 빈 문자열).

    ⭐ 선언(위)과 **같은 말**을 해야 한다. 두 곳이 다른 말을 하면 모델은 둘 다 읽고
      더 구체적인 쪽을 따른다 — 그게 어느 쪽인지는 그때그때 달라진다.
    """
    return (
        "[표정]\n"
        f"- 감정이 드러나는 턴은 말하기 직전에 {SET_FACE_NAME} 을 한 번 부른다. "
        "평범한 턴에는 부르지 않는다.\n"
        "- 이 호출은 소리가 아니다. 감정 이름을 말로 읽지 않는다."
    )
