"""normalcall 시스템 지시문 조립(순수 문자열 — LLM 생성 0) — 외부 어댑터.

불변식 템플릿(코드 고정) + 캐릭터 페르소나(role/personality) + 레벨 프로파일
(level.profile) + 흥미·예시 + (있으면) 최근 이력을 한 문자열로 합쳐 Gemini Live
system_instruction 을 만든다. 어떤 조각도 AI 가 만들지 않는다(조립만). 입력은 전부
원시 값(str/list) — 도메인 모델/DB 를 모른다.

공개 심볼:
    - build_system_instruction(...), SEED_OPENING / seed_opening(선톡 시드) — 일반 통화.
    - build_leveltest_instruction(...), seed_leveltest_opening(),
      CLOSE_SEED_LEVELTEST — 레벨테스트 통화(korean_level 미확정 회원). 레벨을 모르므로
      level_profile/history 슬롯이 없고, code-switching 이 역전(안내=모국어, 측정
      질문=한국어)되며 교정 금지.
      ⚠ 비버 자율 진행/OPI(Phase 1, 2026-07): 서버 주입 없이 비버가 스스로 대화를
      이끈다. 쉬운 질문에서 시작해 학습자가 답할 때마다 [따뜻한 반응 + 다음 질문]을 한
      턴에 붙여 말하고, 잘하면 난도를 한 단계씩 계단식으로 올린다. 통화를 언제 끝낼지는
      서버만 알며(종료 규약), 비버는 절대 스스로 끝내지 않는다. 옛 서버 주도 주입
      시드(build_leveltest_question_seed)·프로빙 사다리·천장 함수는 폐기됐다.
일반 통화의 종료 시드는 호출부(realtime call_session)가 소유한다. 레벨테스트 종료
시드(close_seed_leveltest)는 대본 소유자인 이 모듈이 갖는다. 두 대본의 종료 규약
문단은 _RULE_CLOSE_PROTOCOL 상수 하나를 공유한다(문구 이원화 방지).

⛔ _RULE_CLOSE_PROTOCOL 의 앞 두 줄을 "…하지 마라" 형태로 되돌리지 마라. 예전 문구는
   "남은·경과 시간을 언급하지 마라(\"슬슬 끊자\", \"마지막으로\" 등 금지)" 였는데, 실측에서
   비버가 **그 금지 예시를 거의 그대로 뱉었다**(call=782: "슬슬 마무리할 시간이다"). 부정
   지시가 안 먹은 정도가 아니라 예시가 씨앗이 됐다. 5분 통화 12건 중 3건이 종료 신호보다
   4~16턴 먼저 마무리에 들어갔다(call=836/744/782). 그래서 금지 예시를 지우고 "대화를
   이어가라"는 전진 지시로 바꿨다 — 종료 어휘를 아예 꺼내지 않는 것이 요점이다.
   ✅ 단 3~4번째 줄(종료 신호 정의·신호 수신 후 마무리 절차)은 **계약**이라 유지한다.
      그걸 빼면 비버가 종료 신호를 못 알아보고 조기종료 사고가 재발한다(2026-07-27).

⛔ 제어 태그 2종을 절대 다시 합치지 마라 — 종료 태그(CLOSE_TAG_DEFAULT / new_close_tag)
와 종료가 아닌 제어 태그(CONTROL_TAG). 둘 다 "[시스템]" 이던 시절, 종료 규약이 그 접두어
자체를 종료 트리거로 정의한 탓에 재접지·무음 넛지가 종료 신호로 오독돼 통화가 조기에
끊겼다. 근거: docs/20260727_1710_통화-조기종료-종료태그-분리와-안전망.md

P2-b(체크판 통화 블록): build_system_instruction 은 서버가 SQL 로 선별한 재료를 받는
Optional 슬롯 4개(study_items/known_items/recent_topics/promotion_notice)를 갖는다.
전부 미제공이면 출력은 종전과 바이트 동일(하위호환 — tests/test_persona_prompt.py
스냅샷이 지킨다). 공부/대화 블록이 하나라도 제공될 때만 불변 규칙 1 의 모드 불릿이
"블록이 있으면 블록을 따르라" 교체판(_RULE1_MODE_CHECKBOARD)으로 바뀐다.
설계 근거: docs/20260709_1346_level-system-detailed-mechanics.md ②③④⑧.
"""

from __future__ import annotations

# ⭐⭐ 공유 자산은 **core/prompts/common.py 가 소유한다**(2026-09-10, 표현학습 코스 신설).
#   여기 있던 원문을 그대로 옮겼을 뿐이라 출력은 **바이트 동일**하다 —
#   tests/test_prompt_common_snapshot.py 가 그걸 잠근다.
#   ⛔ 복사해 오지 마라. 같은 문구가 두 곳에 있으면 어느 게 진짜인지 아무도 모른다
#     (docs/prompts/README.md 원칙 3). 지뢰 주석도 common.py 가 함께 갖는다.
#   ⚠ 아래 `_` 접두 별칭은 **하위호환**이다 — call_session·cascade_session·
#     normalcall_service·pronunciation_service·테스트가 `persona_prompt._LOCALE_LABEL` 등을
#     그대로 import 한다. 이름을 안 바꿔야 그 전부가 무손상이다.
from core.prompts.common import (  # noqa: F401 - 재수출(하위호환 공개 심볼)
    CLOSE_TAG_DEFAULT,
    CONTROL_TAG,
    DEFAULT_MAX_SENTENCES,
    LOCALE_LABEL,
    PERSONA_TAIL,
    REGROUND_COVERED_CAP,
    RULE_CLOSE_PROTOCOL,
    RULE_NONVERBAL_SOUND,
    RULE_OFF_TOPIC,
    RULE_RESPONSE_LENGTH,
    DEFAULT_LOCALE,
    new_close_tag,
)
from core.prompts.editable_loader import section as _section
from core.prompts.locked.leveltest import LEVELTEST_PROCEDURE


def _ed(key: str) -> str:
    """editable/normal.md 의 섹션(사람이 고치는 문구). 검사 실패 시 로더가 기본판으로 폴백한다."""
    return _section("normal", key)


def _edlt(key: str) -> str:
    """editable/leveltest.md 의 섹션."""
    return _section("leveltest", key)


_LOCALE_LABEL = LOCALE_LABEL
_DEFAULT_LOCALE = DEFAULT_LOCALE

# 선톡(첫 발화) 시드. call_session 이 통화 시작 직후 1회 send_text_turn 으로 주입.
# target_language 로 "무엇을 공부할지"만 바꾼다(기본 한국어 — 프로덕션 출력 무손상).
# ⭐ 잠금/편집 분리(2026-09-12): 선톡 시드 말투는 editable/normal.md `seed_opening` / `seed_opening_lean`(슬롯 {target}).
def seed_opening(target_language: str = "한국어") -> str:
    return _ed("seed_opening").format(target=target_language)


def seed_opening_lean(target_language: str = "한국어") -> str:
    return _ed("seed_opening_lean").format(target=target_language)


# ⭐ 잠금 분리(2026-09-12): seed_resume, close_seed_leveltest → core/prompts/locked/seeds.py 로 **이동**(복사 아님 — 바이트 그대로).
from core.prompts.locked.seeds import (
    close_seed_leveltest,
    seed_resume,
)


SEED_OPENING = seed_opening()  # 하위호환 상수(기본 한국어). 데모는 seed_opening(target) 사용.

# ── 서버 → 비버 제어 태그 ──────────────────────────────────────────────── #
# ⭐ `CONTROL_TAG` · `CLOSE_TAG_DEFAULT` · `new_close_tag()` 는 이제 core/prompts/common.py
#   가 소유한다(위 import 에서 재수출). ⛔ 두 태그를 절대 다시 합치지 마라 — 그 사고
#   이력과 근거 문서는 common.py 모듈 독스트링에 그대로 있다(call_id=683·706·852).


# 통화 종료 규약(불변 규칙 중 유일하게 콜타입 간 공유되는 문단 — 번호 없이 본문만).
# 일반 통화 대본과 레벨테스트 대본이 이 상수를 그대로 삽입한다. ⛔ 문구를 바꾸면
# build_system_instruction 출력이 변해 통화 회귀가 난다 — tests/test_persona_prompt.py
# 스냅샷 테스트(바이트 동일)가 지킨다. {close_tag} 외의 {중괄호}를 넣지 마라(.format 충돌).
#
# ⛔⛔ 여기에 '종료'를 다시 설명하지 마라. 이 문단은 **대화를 이어가라**는 전진 지시와
#   **낭독 금지** 두 가지만 말한다. 종료 신호·종료 태그·마무리 절차는 한 글자도 없다.
#
#   왜: 종료 메커니즘을 지시문에서 가르치면 모델이 그걸 **실행 수단으로 쓴다**. 세 번
#   당했다.
#     ① 리터럴 3회 노출 → 비버가 "[시스템]"을 낭독하고 스스로 종료(call 706)
#     ② 리터럴 1회 + 난수 접미 → 난수까지 그대로 복사해 종료(call 852: 서버가 시드를
#        보내기 2분 39초 전에 "[통화종료:d963] 오늘 공부 너무 수고했어!"). 난수는
#        '우연한 재현'만 막고 '복사'는 못 막는다.
#     ③ 리터럴 0회 + "대괄호로 시작하는 안내문이 종료 신호" 성질 서술 → 모델이 규칙을
#        일반화해 **없는 태그를 발명**했다(call 870: "[마무리] 네. 수고하셨어요." 8회).
#        지시문 자체가 [페르소나]·[불변 규칙]처럼 대괄호 라벨을 11종 쓰므로, "대괄호=
#        서버 신호"는 모델에게 흉내낼 수 있는 문법이 된다.
#   공통 교훈: 흉내낼 원본을 지우는 것으로는 부족하고, **개념을 지워야** 한다. 모델이
#   종료라는 수단을 모르면 그 수단을 쓸 수 없다.
#
#   그럼 종료는 어떻게 되나: 때가 되면 서버가 평범한 지시문을 대화에 넣는다. 모델은
#   미리 알 필요 없이 그때 문맥으로 따르면 된다(in-context instruction). 낭독 방지도
#   모델의 준수에 기대지 않고 서버 필터가 강제한다(call_session._CONTROL_TAG_RE).
#
# ⚠ 낭독 금지 줄은 남긴다 — 이걸 빼면 재접지·넛지 안내문까지 소리 내어 읽는다.
#   핵심은 "대괄호를 읽지 마라"이지 "대괄호가 종료 신호다"가 아니다. 둘을 다시 엮지 마라.
# ⭐ 원문은 core/prompts/common.RULE_CLOSE_PROTOCOL 이 소유한다(2026-09-10 이전, 이 자리).
#   표현학습·프리토킹 코스가 같은 문단을 쓰므로 옮겼다 — 별칭이라 출력은 바이트 동일.
_RULE_CLOSE_PROTOCOL = RULE_CLOSE_PROTOCOL
# ⚠ 위 두 번째 줄이 왜 있나(15분 실측): L1 생존회화 청크에 "안녕히 가세요"·"또 봐" 같은
#   표현이 들어 있다. 그걸 가르치자 비버가 **가르치는 행위를 실제 작별로 옮겨** "없으면
#   오늘은 여기까지 …" 로 미끄러졌다(통화 3분 55초 지점부터 반복). 커리큘럼에서 그 표현을
#   빼는 건 답이 아니다 — 학습자에게 필요한 표현이다. 대신 "가르치는 것 ≠ 끝내는 것"을
#   구분해 주고 다음 행동을 지정한다.
# ⛔ 여기에 금지 예시를 쓰지 마라("'여기까지' 같은 말 금지" 식). 부정 지시 + 예시 조합은
#   모델이 그 예시를 그대로 뱉게 만든다(call 836/744/782 실측). 전진 지시로만 쓴다.

# 불변 규칙 1 의 모드 불릿 원문(기본판). 상수 추출은 재배치일 뿐 — _INVARIANTS_TEMPLATE 은
# 이 상수를 그대로 이어 붙여 종전과 바이트 동일하다(스냅샷 테스트가 지킨다).
# ⭐ 잠금/편집 분리(2026-09-12) — 페르소나 문단·규칙 1(모드 분기)·3(언어 사용)·4(교정)는 editable/normal.md, 규칙 2·5·6·7·PERSONA_TAIL 은 잠금
#   (locked/rules.py). 조립 결과는 분리 전과 **바이트 동일**(tests/test_persona_prompt.py·test_prompt_common_snapshot.py 동결 스냅샷).
_RULE1_MODE_DEFAULT = _ed("rule1_mode_default")
_RULE1_MODE_CHECKBOARD = _ed("rule1_mode_checkboard")
_INVARIANTS_TEMPLATE = (
    _ed("persona_intro") + " " + PERSONA_TAIL + """

[불변 규칙 — 캐릭터와 무관하게 항상 지켜라]
"""
    + _ed("rule1_head") + "\n" + _RULE1_MODE_DEFAULT + "\n" + _ed("rule1_tail") + """
2. """ + _RULE_CLOSE_PROTOCOL + "\n"
    + _ed("rule3") + "\n"
    + _ed("rule4") + "\n"
    + RULE_RESPONSE_LENGTH + "\n" + RULE_NONVERBAL_SOUND + "\n" + RULE_OFF_TOPIC
)


# 불변 규칙 1 모드 불릿 교체판 — 공부/대화 블록이 하나라도 주입될 때만 사용.
# .format 을 통과하므로 {target} 외의 리터럴 중괄호를 넣지 마라.

# ⛔⛔ **끊긴 참조 2건을 고쳤다**(2026-08-19). 둘 다 모델에게 "저기를 봐라"라고
#   해 놓고 그 자리에 아무것도 없던 것이다 — 지시문 안에서 길을 잃으면 모델은 규칙을
#   따르는 대신 **자기 마음대로 채운다.**
#
#   ① 규칙 1 이 "**위** 선톡 질문에 대한 답을 듣고 모드를 정해라"였다. 그런데 선톡 시드는
#     이 지시문에 **없다** — `call_session` 이 통화 시작 직후 별도 턴으로 주입한다
#     (`seed_opening`, 이 파일 상단). 즉 "위"에는 아무것도 없다. Live 는 세션 내내 컨텍스트가
#     남아 대화 이력에서 찾아낼 수는 있지만, 문서 안에서는 끊긴 화살표다.
#     ⇒ 가리키지 말고 **스스로 말하게** 바꿨다("네가 통화 첫머리에 던진 모드 질문").
#     ⚠ 질문 문구를 리터럴로 옮겨 적지 않았다 — 금지든 예시든 리터럴은 씨앗이 된다
#       (README §3 원칙 2, call 782). 시드가 이미 그 질문을 시킨다.
#
#   ② [대화 모드 — 착지] 불릿이 "발판의 정도는 **아래** 밴드 정책이 정한다"였는데,
#     밴드 정책(`{lang_policy}`)은 바로 **위**에 꽂힌다. 조립 순서가
#       [모드별 언어] → {lang_policy} → [착지]
#     이므로 [모드별 언어]의 "아래"는 맞고 [착지]의 "아래"만 틀렸다. 한 글자 문제지만
#     모델이 아래를 뒤지다 못 찾으면 초급에게 댈 모국어 발판을 자기 재량으로 정한다.
#
#   ⚠ 이 두 줄이 **Live 지시문을 바꾼다** — `tests/test_persona_prompt.py` 의 바이트 동일
#     스냅샷이 이번엔 "무영향 증명"이 아니다. 동결본에 **같은 두 치환만** 적용해 재기준화했다
#     (통째로 다시 뽑지 않았다 — 그러면 실수로 바꾼 곳까지 같이 얼어붙는다).
#
# ⛔ **규칙 6(비언어 발성 길이)을 새로 세웠다**(2026-08-22, 실측 call_id=1136).
#   증상: 정상 턴이 1.2~4.5초인데 웃음으로 시작한 턴 2개가 **19.3초·39.1초**였다
#     (통화의 20%). 그 동안 전사는 첫 조각 뒤로 안 온다 = 글자 없는 소리만 나간 것이다.
#     ⛔ barge-in off 라 그 시간 내내 학습자 마이크가 서버로 **안 간다**
#       (`call_session.py` 의 `state.turn_id is None` 관문) — "조용히 해"가 안 닿는다.
#
#   ⛔⛔ **규칙 5(응답 길이)에 붙이지 마라** — 사장님 지시(2026-08-22): "응답 길이를 손대면
#     어떡함. 응답 길이 짧게 하지 마셈. 그냥 감탄사만 너무 긴 게 마음에 안 든 거임."
#     처음엔 규칙 5 에 "말이 아닌 소리도 이 길이 안에 든다"를 붙였다가 **되돌렸다.**
#     길이 규칙 하나에 두 대상을 묶으면 모델이 둘을 뭉개 **할 말까지 줄인다** — 고치려던
#     것보다 나쁜 회귀다. 그래서 규칙 5 는 바이트 그대로 두고 **별도 규칙**으로 뺐고,
#     규칙 6 본문에 "할 말의 분량은 규칙 5 그대로다"를 명시해 전이를 막았다.
#     ⚠ 원칙 3(중복은 한 규칙에 위임)과 부딪치지 않는다 — 소유 대상이 다르다.
#       규칙 5 = 말의 분량, 규칙 6 = 말이 아닌 소리의 길이.
#
#   ⛔⛔ **리터럴을 쓰지 마라** — 웃음소리·감탄사를 예시로 적으면 비버가 그걸 뱉는다
#     (README 원칙 2, call 782 선례: 금지 예시를 그대로 낭독했다). 성질로만 서술했다.
#   ⛔ **웃음 자체를 금지하지 않았다** — 웃을지 말지는 캐릭터 소유다(원칙 1). 길이만 건다.
#     여기서 "웃지 마라"로 쓰면 장난기·츤데레 캐릭터가 통째로 뭉개진다.
#   ⚠ 효과 미확정: 네이티브 오디오가 **비언어 발성을 지시로 통제할 수 있는지** 검증된 바
#     없다. 안 먹으면 턴 길이 상한(코드·R4) 또는 클라 오디오 큐 상한으로 간다.
# ⭐ 규칙 5(응답 길이)·6(비언어 발성 길이)·7(학습 밖 이탈 차단)의 원문은
#   core/prompts/common.py 가 소유한다(2026-09-10). 표현학습·프리토킹 코스가 **같은 번호로**
#   그대로 쓴다 — ⛔ 번호를 다시 매기면 규칙 6·7 안의 "규칙 5" 인용이 끊긴다(common 주석).
#   이어붙이기라 출력은 바이트 동일이다.

# ⭐ 값의 소유자는 core/prompts/common.DEFAULT_MAX_SENTENCES 다(2026-09-10) — 표현학습·
#   프리토킹도 같은 규칙 5 문구를 쓰므로 숫자가 두 곳에 있으면 안 된다(README 원칙 3).
_DEFAULT_MAX_SENTENCES = DEFAULT_MAX_SENTENCES

# ⭐ 잠금 분리(2026-09-12): _EMOTION_TAG_RULE, _FACE_TOOL_RULE, _LANGUAGE_MARKER_RULE → core/prompts/locked/face.py 로 **이동**(복사 아님 — 바이트 그대로).
from core.prompts.locked.face import (
    EMOTION_TAG_RULE,
    FACE_TOOL_RULE,
    LANGUAGE_MARKER_RULE,
    face_rule_for,
)
_EMOTION_TAG_RULE = EMOTION_TAG_RULE
_FACE_TOOL_RULE = FACE_TOOL_RULE          # 운영(qual) 규칙 블록 — 옛 문구는 locked.face.FACE_TOOL_RULE_LEGACY
_LANGUAGE_MARKER_RULE = LANGUAGE_MARKER_RULE


def face_tool_rule() -> str:
    """`LIVE_FACE_RULE_MODE` 에 맞는 [표정] 블록(운영 qual = FACE_TOOL_RULE).

    ⭐ 2026-09-12 ctx-lab: 이 블록은 이제 **세 대본**(일반 `build_system_instruction(face_tool=)` · 표현학습 ·
      프리토킹 `face_rule=`)에 같은 문구로 붙는다 — 그전엔 일반 통화에만 붙고 표현학습·프리토킹은 선언만 있어
      모델이 매 턴 set_face 를 불렀다(1451: 24/25 턴, 2× 과금).
    """
    from core.config import settings  # 지연 import — 이 모듈은 순수 문자열 조립이라 모듈 상단에 두지 않는다
    return face_rule_for(settings.LIVE_FACE_RULE_MODE)[0]


# ⭐ 잠금 분리(2026-09-12): _history_block, _STUDY_KIND_LABEL, _STUDY_STATE_LABEL, _study_state_procedure, _STUDY_RESERVE_HEADER, _STUDY_FIVE_CHECK, _STUDY_FIVE_CHECK_L1_TAIL, _STUDY_NEXT_TAIL, _study_procedure, _render_study_item, _study_block, _KNOWN_GRAMMAR_FALLBACK, _known_block, _PROMOTION_NOTICE_TEMPLATE → core/prompts/locked/normal.py 로 **이동**(복사 아님 — 바이트 그대로).
from core.prompts.locked.normal import (
    KNOWN_GRAMMAR_FALLBACK,
    PROMOTION_NOTICE_TEMPLATE,
    STUDY_FIVE_CHECK,
    STUDY_FIVE_CHECK_L1_TAIL,
    STUDY_KIND_LABEL,
    STUDY_NEXT_TAIL,
    STUDY_RESERVE_HEADER,
    STUDY_STATE_LABEL,
    history_block,
    known_block,
    render_study_item,
    study_block,
    study_procedure,
    study_state_procedure,
)
_history_block = history_block
_STUDY_KIND_LABEL = STUDY_KIND_LABEL
_STUDY_STATE_LABEL = STUDY_STATE_LABEL
_study_state_procedure = study_state_procedure
_STUDY_RESERVE_HEADER = STUDY_RESERVE_HEADER
_STUDY_FIVE_CHECK = STUDY_FIVE_CHECK
_STUDY_FIVE_CHECK_L1_TAIL = STUDY_FIVE_CHECK_L1_TAIL
_STUDY_NEXT_TAIL = STUDY_NEXT_TAIL
_study_procedure = study_procedure
_render_study_item = render_study_item
_study_block = study_block
_KNOWN_GRAMMAR_FALLBACK = KNOWN_GRAMMAR_FALLBACK
_known_block = known_block
_PROMOTION_NOTICE_TEMPLATE = PROMOTION_NOTICE_TEMPLATE


# =========================================================================== #
# P2-b 체크판 통화 블록 (공부 모드 체크판 / 대화 모드 유도 / 승급 알림)
# 설계 근거: docs/20260709_1346_level-system-detailed-mechanics.md ②③④⑧.
# 전부 순수 문자열 조립 — 아래 상수·함수는 .format 을 거치지 않는 f-string 조립이므로
# 리터럴 중괄호 제약이 없다(_PROMOTION_NOTICE_TEMPLATE 만 .format — 중괄호 금지).
# =========================================================================== #


# =========================================================================== #
# 밴드별 언어 정책(한국어 위주 전환) — 규칙 3에 주입.
# 목표는 학습자가 한국어를 실제로 '산출'하게 하는 것. 퍼센트가 아니라 '어떤 화행을
# 한국어로 돌릴지'의 행동 규칙으로 지시한다(LLM 은 비율을 자기검증 못 함).
# 키 = mastery_repository.band_of 라벨(survival/beginner/intermediate/advanced).
# 값은 .format(target=, locale_label=) 로 선처리한 뒤 규칙 3의 {lang_policy} 에 꽂는다.
# =========================================================================== #
# ⭐ 잠금/편집 분리(2026-09-12): 밴드 정책(대화 모드 모국어 발판 비율 서술)은 editable/normal.md `lang_policy_*`.
_LANG_POLICY: dict[str, str] = {
    "survival": _ed("lang_policy_survival"),
    "beginner": _ed("lang_policy_beginner"),
    "intermediate": _ed("lang_policy_intermediate"),
    "advanced": _ed("lang_policy_advanced"),
}


def build_system_instruction(
    *,
    role: str,
    personality: str,
    level_profile: str,
    locale: str,
    interests: list[str],
    name: str | None = None,
    history: object | None = None,
    target_language: str = "한국어",
    locale_label: str | None = None,
    study_items: list[dict] | None = None,
    known_items: dict | None = None,
    recent_topics: list[str] | None = None,
    promotion_notice: bool = False,
    lang_band: str = "beginner",
    close_tag: str = CLOSE_TAG_DEFAULT,
    max_sentences: int | None = None,
    language_marker: bool = False,
    emotion_tags: tuple[str, ...] = (),
    face_tool: bool = False,
) -> str:
    """normalcall Live 세션용 system_instruction 을 조립한다(LLM 생성 0).

    조립 순서: 불변식 → 캐릭터 페르소나(role/personality) → 레벨 프로파일 →
    흥미·예시 → (있으면) 공부 체크판 블록 → 대화 가이드 블록 → 최근 통화 소재 →
    최근 이력 → 승급 알림.

    하위호환: study_items/known_items/recent_topics 가 전부 None(또는 빈 값)이고
    promotion_notice=False 면 출력은 종전과 바이트 동일하다(스냅샷 테스트가 지킨다).
    공부/대화 블록이 하나라도 제공될 때만 불변 규칙 1 의 모드 불릿이 교체판으로 바뀐다.

    Args:
        role: 캐릭터 역할/정체성(character.role).
        personality: 캐릭터 성격·말투(character.personality).
        level_profile: 레벨 발화 프로파일(level.profile).
        locale: 학습자 모국어 식별자(미지원이면 영어 폴백).
        interests: 관심사 목록(비면 "일상").
        name: 학습자 이름(없으면 "학습자" 폴백).
        history: 최근 이력 dict 또는 None. ⚠ known_items 가 제공되면 expressions 파트는
            주입하지 않고(대화 유도 목표와 이중 주입 방지), recent_topics 가 제공되면
            summaries 파트는 주입하지 않는다(recent_topics 가 대체). 둘 다 제공되면
            history 는 통째로 무시된다.
        target_language: 가르치는 대상 언어(기본 "한국어"). 데모에서만 "프랑스어" 등으로 넘긴다.
        locale_label: 모국어 라벨 오버라이드(기본 None → _LOCALE_LABEL 조회). 데모 전용(예: ko→"한국어").
        study_items: 공부 모드 체크판 항목(본편 5+예비 5, mechanics ②). 각 dict 는
            {slot: "main"|"reserve", kind: "grammar"|"vocab"|"chunk"|"review",
             obj: str, ex: str|None, des: str|None}.
            ⚠ L1 왕초보 변형은 **lang_band == "survival"** 이면 켜진다(2026-08-17).
            밴드를 안 넘기면 옛 판별(grammar 없음 + chunk 있음)로 폴백한다.
        known_items: 대화 모드 가이드(mechanics ③) —
            {grammar: list[str](≤40 soft 범위, 빈 리스트면 레벨 프로파일 폴백 문구),
             targets: [{obj, ex, hint}](유도 표현 3~5)}.
        recent_topics: 최근 통화 소재(중복 화제 회피 1줄).
        promotion_notice: True 면 맨 끝에 승급 알림 1줄(mechanics ⑧).
        close_tag: 이 통화의 종료 신호 태그. 기본 CLOSE_TAG_DEFAULT(스냅샷 기준값).
            ⚠ 2026-08-02 부터 **출력에 실리지 않는다** — 지시문이 태그 리터럴을 보여주면
            비버가 그대로 복사해 스스로 종료했다(실측 call_id=852, 30일 8건). 인자는
            시그니처 하위호환과 호출부 대칭(run_call 이 시드·지시문에 같은 값을 넘긴다)을
            위해 남긴다. 태그는 서버 전용이다 — 시드 조립과 누출 탐지에만 쓴다.

    Returns:
        Gemini Live system_instruction 문자열.
    """
    # ⭐⭐ 응답 길이 문구의 숫자는 **호출부의 상한에서 온다**(2026-08-13).
    #   ⚠ 오늘 아침 결함과 같은 모양이 재발했다: 서버는 3문장에서 끊는데 프롬프트는 "1~4문장"을
    #     시켰다. 문장 경계라 말이 잘리진 않지만 **모델이 4번째를 쓰고 우리가 버린다**(낭비)이고,
    #     무엇보다 **다음 사람이 어느 게 진짜인지 못 안다.** 손으로 쓴 숫자가 두 곳에 있으면 안 된다.
    #   ⚠ 이건 오늘 지웠던 `short_reply` 분기의 부활이 **아니다.** 그건 사람이 고른 **다른 문구**를
    #     캐스케이드에만 주는 갈래였고, 이건 **같은 문구에 자기 설정값을 채우는 것**이다 —
    #     갈래가 생기는 게 아니라 출처가 하나로 모인다.
    #   ⚠ Live 는 상한이 없어 값을 안 넘긴다 → 기본 4 → **출력 바이트 그대로**(회귀로 고정).
    #   ⚠ 언어별로 다른 값이 필요해질 수 있다(영어는 문장이 길어 4문장이 200자·20초였다).
    #     그때는 **호출부가 언어를 보고 숫자를 골라 넘기면 된다** — 이 함수는 안 바뀐다.
    max_sentences = _DEFAULT_MAX_SENTENCES if not max_sentences else max(1, int(max_sentences))
    locale_label = locale_label or _LOCALE_LABEL.get(locale, _LOCALE_LABEL[_DEFAULT_LOCALE])
    interests_text = ", ".join(i for i in interests if i) or "일상"
    username = (name or "").strip() or "학습자"

    # 블록이 하나라도 있으면 규칙 1 모드 불릿을 교체판으로(없으면 템플릿 원문 그대로 —
    # 바이트 동일 유지). replace 대상은 템플릿 조립에 쓰인 상수 원문이라 항상 매칭된다.
    template = _INVARIANTS_TEMPLATE
    if study_items or known_items:
        template = template.replace(_RULE1_MODE_DEFAULT, _RULE1_MODE_CHECKBOARD)

    # 밴드별 언어 정책(규칙 3)을 선처리해 주입 — 미상 밴드는 beginner 폴백(보수적).
    lang_policy = _LANG_POLICY.get(lang_band, _LANG_POLICY["beginner"]).format(
        target=target_language, locale_label=locale_label
    )
    invariants = template.format(
        max_sentences=max_sentences,
        locale_label=locale_label,
        role=role or "친근한 한국어 대화 파트너",
        personality=personality or "다정하고 편안한 말투",
        username=username,
        target=target_language,
        lang_policy=lang_policy,
        close_tag=close_tag,
    )

    parts = [
        invariants,
        f"\n[학습자 수준]\n{level_profile}",
        f"\n[학습자 흥미·소재] {interests_text}",
    ]
    if study_items:
        parts.append(_study_block(
            study_items, target=target_language, locale_label=locale_label,
            lang_band=lang_band,
        ))
    if known_items:
        parts.append(_known_block(known_items, target=target_language, locale_label=locale_label))
    topics = [t for t in (recent_topics or []) if t]
    if topics:
        parts.append(
            "\n[최근 통화 소재] " + " / ".join(topics)
            + " — 이 화제들을 그대로 반복하지 말고, 새 화제나 확장으로 이어가라."
        )
    # 이중 주입 방지: known_items → expressions 억제, recent_topics → summaries 억제.
    if isinstance(history, dict) and (known_items or topics):
        history = {
            "summaries": [] if topics else history.get("summaries"),
            "expressions": [] if known_items else history.get("expressions"),
        }
    history_block = _history_block(history)
    if history_block:
        parts.append(history_block)
    if promotion_notice:
        parts.append("\n" + _PROMOTION_NOTICE_TEMPLATE.format(locale_label=locale_label))
    if emotion_tags:
        # ⛔ 옵트인이다(캐스케이드 전용). **Live 에는 절대 붙이지 마라** — Live 는 모델이
        #   직접 소리를 내므로 태그를 **그대로 읽어 버린다**(서버가 걷어낼 자리가 없다).
        #   기본값(빈 튜플)에서는 이 블록이 안 붙어 기존 호출부 출력이 바이트 동일하다.
        parts.append(
            _EMOTION_TAG_RULE.format(tags=" ".join(f"<{t}>" for t in emotion_tags))
        )
    if face_tool:
        # ⛔ 옵트인. 기본 False 에서는 이 블록이 안 붙어 **기존 호출부 출력이 바이트 동일**하다
        #   (스냅샷 회귀가 그걸 지킨다). Live 스파이크만 True 로 켠다.
        parts.append(face_tool_rule())
    if language_marker:
        # ⛔ 옵트인이다. 기본값(False)에서는 이 블록이 안 붙어 **기존 호출부의 출력이
        #   바이트 동일**하다(스냅샷 테스트가 그걸 지킨다).
        parts.append(
            _LANGUAGE_MARKER_RULE.format(
                target=target_language, locale_label=locale_label
            )
        )
    return "\n".join(parts)


# =========================================================================== #
# Live setup 분할 (2026-08-23) — 지시문을 setup 밖으로 빼서 1011 을 피한다
# =========================================================================== #
#
# ⛔⛔ **왜 있나.** `gemini-2.5-flash-native-audio-preview-09-2025`(AI Studio)에서
#   **긴 system_instruction 과 function tool 이 같은 setup 페이로드에 함께 있으면
#   통화가 100% 죽는다**(1011, `receive()` 0번째 메시지). 같은 시간대 라운드로빈 실측:
#     · 지시문 5,057자 + set_face 를 setup 에 전부 → **0/8**
#     · 코어 46자 + 툴 → 붙은 뒤 나머지를 통화 중 주입 → **14/14**(무음턴 0/70)
#     · 긴 지시문 + 툴 **없음** → 7/8   ⇒ 둘이 같은 setup 에 있을 때만 죽는다
#   벤더 티켓이 열려 있다: googleapis/python-genai#1832 (2025-12-08, open, 워크어라운드 없음).
#
# ⛔ **위치가 아니라 총량이다.** 지시문을 선톡 시드(첫 메시지)로 옮기는 것도 1/6 으로 실패했다.
#   setup~첫 응답 구간의 총량만 줄이면 되고, 붙은 뒤에는 5,057자를 통째로 넣어도 안 죽는다.
#
# ⛔ **build_system_instruction 을 고치지 마라.** 바이트 스냅샷 3건이 출력을 얼려 두고 있다
#   (tests/test_persona_prompt.py). 분할은 그 출력을 **받아서 자르는 후처리**로만 한다.

# ⛔⛔ **지시문 분할 주입 기계를 걷어냈다**(2026-09-01). 되살리지 마라.
#   여기 있던 것: LIVE_SETUP_MAX_CHARS(380) · LIVE_PERSONA_CHUNK_CHARS ·
#   _SETUP_CORE_TEMPLATE · _SETUP_CORE_FACE · build_setup_core() ·
#   split_persona_for_injection(). 전부 2.5 시절의 응급처치였다.
#
#   ⭐ 왜 필요했나 — `gemini-2.5-flash-native-audio-preview-09-2025` 는 **긴 지시문 +
#     function tool 이 같은 setup 에 있으면 0/8 로 죽었다**(1011). 그래서 setup 을 380자
#     코어로 줄이고 나머지를 통화 중에 조각으로 밀어넣었다.
#   ⭐ 왜 없앴나 — **3.1 에는 그 제약이 없다.** 전문 9,094자 + tool 을 setup 에 넣어도
#     안 죽는다(2026-08-27~09-01 나흘 실증, 1011 0건). 반대로 주입 쪽이 해가 됐다:
#       out_tr(비버 발화 중)   → 매 턴 interrupted, 대사가 8~19자로 토막      6/6
#       in_tr(학습자 전사 시)  → 두 번째 턴 토막                              2/2
#       turn_end(발화 종료 후) → 토막은 사라졌으나 **지시문 낭독 + 자문자답**
#     ⇒ 3.1 은 대화 턴으로 들어온 지시문을 「상대가 한 말」로 취급한다. 시점 문제가 아니다.
#
#   ⚠ 되살릴 일이 생기면 `git show 5043dba` 계열 커밋에 전문이 있다. 그리고 위 세 실측을
#     먼저 반증해라 — 그 전엔 켜는 것이 통화 품질을 떨어뜨린다.
#   ⭐ 표정 순서 규칙("몇 마디 말한 뒤 부른다")은 `_FACE_TOOL_RULE` 에 남아 있다. 그건
#     setup 에 통째로 들어가므로 첫 턴부터 적용된다 — 코어용 축약본은 이제 필요 없다.

# =========================================================================== #
# 레벨테스트 통화 대본 (korean_level 미확정 회원 — P1, 2026-07 비버 자율 진행/OPI 개정)
# 설계 근거: docs/20260709_1231_level-system-master-plan.md §4,
#           docs/plans/2026-07-12-leveltest-fast-probe.md.
# ⚠ 비버 자율 진행(OPI): 서버 주입 없이 비버가 스스로 대화를 이끈다. 유도 질문 사다리를
# 마음에 두고 쉬운 질문에서 시작해, 학습자가 답할 때마다 [따뜻한 반응 + 다음 질문]을 한
# 턴에 붙여 말하고, 그 단계 문법을 해내면 반드시 한 단계 위로 난도를 올린다(제자리걸음
# 금지). '충분하다'는 비버가 판단하지 않는다 — 통화를 언제 끝낼지는 서버만 알며, 비버는
# 사다리 꼭대기에 닿아도 "왜?"·"예를 들면?"으로 계속 파고들 뿐 절대 스스로 끝내지 않는다.
# 옛 서버 주도 주입(build_leveltest_question_seed)·프로빙 사다리·천장 함수
# (leveltest_ceiling_reached)는 폐기됐다.
# 일반 통화와의 차이: 레벨을 모르므로 level_profile/history 슬롯 없음, code-switching
# 역전(안내·리액션=모국어, 측정 질문=한국어), 레벨·점수 발설 금지. 종료 규약은
# _RULE_CLOSE_PROTOCOL 공유(비버 먼저 작별 절대 금지).
# ⚠ 레벨테스트는 캐릭터 페르소나를 아예 주입하지 않는다(순수 배치 테스트 관점) —
# role/personality 는 시그니처 호환을 위해 계속 받되 대본엔 넣지 않고, 고정
# '시험관' 한 줄로 대체한다. 캐릭터 톤 누출·한국어 과다(실측 call 163)·토큰을 제거.
# 캐릭터는 일반 통화(build_system_instruction)에서 살린다.
# =========================================================================== #

# ⭐ 잠금/편집 분리(2026-09-12): 시험관 소개 한 줄은 editable/leveltest.md `intro`, 측정 절차 본문은 잠금 LEVELTEST_PROCEDURE.
_LEVELTEST_TEMPLATE = _edlt("intro") + "\n\n" + LEVELTEST_PROCEDURE


# ⭐ 잠금 분리(2026-09-12): _LEVELTEST_LADDER_KO, _LEVELTEST_LADDER_JA, _LEVELTEST_LADDER_EN, _LEVELTEST_LADDER_CN, _LEVELTEST_LADDER_FR, _LEVELTEST_LADDER_VI, _LEVELTEST_LADDER → core/prompts/locked/leveltest.py 로 **이동**(복사 아님 — 바이트 그대로).
from core.prompts.locked.leveltest import (
    LEVELTEST_LADDER,
    LEVELTEST_LADDER_CN,
    LEVELTEST_LADDER_EN,
    LEVELTEST_LADDER_FR,
    LEVELTEST_LADDER_JA,
    LEVELTEST_LADDER_KO,
    LEVELTEST_LADDER_VI,
)
_LEVELTEST_LADDER_KO = LEVELTEST_LADDER_KO
_LEVELTEST_LADDER_JA = LEVELTEST_LADDER_JA
_LEVELTEST_LADDER_EN = LEVELTEST_LADDER_EN
_LEVELTEST_LADDER_CN = LEVELTEST_LADDER_CN
_LEVELTEST_LADDER_FR = LEVELTEST_LADDER_FR
_LEVELTEST_LADDER_VI = LEVELTEST_LADDER_VI
_LEVELTEST_LADDER = LEVELTEST_LADDER


def build_leveltest_instruction(
    *,
    role: str,
    personality: str,
    locale: str,
    interests: list[str],
    name: str | None = None,
    target_language: str = "한국어",
    locale_label: str | None = None,
    close_tag: str = CLOSE_TAG_DEFAULT,
) -> str:
    """레벨테스트 통화용 system_instruction 을 조립한다(LLM 생성 0, 비버 자율 진행).

    build_system_instruction 과 같은 캐릭터 슬롯(role/personality)을 계속 받지만
    (시그니처 호환), 레벨테스트는 '순수 배치 테스트' 관점이라 캐릭터 페르소나를
    대본에 주입하지 않는다 — 고정 '시험관' 한 줄로 대체한다. level_profile/history
    슬롯도 없다(레벨 미상 전제).

    ⚠ 비버 자율 진행/OPI(Phase 1, 2026-07): 서버 주입 없이 비버가 스스로 대화를
    이끈다. 이 대본은 난이도 사다리(1~6단)를 마음에 두고 "쉬운 질문에서 시작 → 답할
    때마다 [따뜻한 반응 + 다음 질문]을 한 턴에 → 잘하면 한 단계씩 상승 → 절대 스스로
    끝내지 않음"이라는 자율 진행 규약을 담는다. 옛 probe_plan 인자·서버 주입 시드·프로빙
    사다리·천장 함수는 폐기됐다.

    Args:
        role: (미사용 — 호환용) 캐릭터 역할/정체성. 대본에 주입하지 않는다.
        personality: (미사용 — 호환용) 캐릭터 성격·말투. 대본에 주입하지 않는다.
        locale: 학습자 모국어 식별자(미지원이면 영어 폴백).
        interests: 관심사 목록(비면 "일상") — 질문 소재.
        name: 학습자 이름(없으면 "학습자" 폴백).
        target_language: 측정 대상 언어(기본 "한국어").
        locale_label: 모국어 라벨 오버라이드(기본 None → _LOCALE_LABEL 조회).
        close_tag: 이 통화의 종료 신호 태그(기본 CLOSE_TAG_DEFAULT).
            ⚠ 2026-08-02 부터 **출력에 실리지 않는다**(일반 대본과 같은 이유 — 낭독 방지).
            인자는 하위호환용으로 남긴다.

    Returns:
        Gemini Live system_instruction 문자열.
    """
    # role/personality 는 호환용으로만 받고 대본엔 넣지 않는다(순수 배치 테스트).
    locale_label = locale_label or _LOCALE_LABEL.get(locale, _LOCALE_LABEL[_DEFAULT_LOCALE])
    interests_text = ", ".join(i for i in interests if i) or "일상"
    username = (name or "").strip() or "학습자"

    # 사다리 앵커는 언어별(미등록 언어는 한국어 폴백 — 안전 기본값).
    ladder = _LEVELTEST_LADDER.get(target_language, _LEVELTEST_LADDER_KO)

    return _LEVELTEST_TEMPLATE.format(
        locale_label=locale_label,
        username=username,
        target=target_language,
        interests_text=interests_text,
        ladder=ladder,
        close_tag=close_tag,
    )


# ⭐ 잠금/편집 분리(2026-09-12): 레벨테스트 선톡 시드 말투는 editable/leveltest.md `seed_opening`(슬롯 {target}).
def seed_leveltest_opening(target_language: str = "한국어") -> str:
    return _edlt("seed_opening").format(target=target_language)


# ⭐ 잠금 분리(2026-09-12): build_reground_reminder, build_continue_reminder, _CLOSING_WORDS, is_closing_slot, _drop_if_closing, build_resume_brief, build_reground_brief → core/prompts/locked/reground.py 로 **이동**(복사 아님 — 바이트 그대로).
from core.prompts.locked.reground import (
    _CLOSING_WORDS,
    _drop_if_closing,
    build_continue_reminder,
    build_reground_brief,
    build_reground_reminder,
    build_resume_brief,
    is_closing_slot,
)


# covered 상한 — 브리프가 길어지면 유저 한마디를 덮는다(docstring 의 250 토큰 경고 참조).
# ⚠ 호출부 로그가 "몇 개가 실제로 실렸나"를 찍을 때 같은 수를 봐야 하므로 공개 상수다.
# ⭐ 2026-09-09 4 → 10. **부분 목록이 되감기를 만든다**(통화 1360): 7항목을 드릴한 뒤
#   covered=3 이 나갔고, 압축이 초반을 지우자 목록에 없던 3항목이 "안 한 것"이 되어 비버가
#   1번 항목으로 되감았다. 상한이 잘라낸 것도 그 원인의 한 축이다.
#   10 인 이유: 공급원 `state.reground_items` 가 이미 `[:10]` 이라 **항목 전량**이 상한이다.
#   비용은 단어 라벨 6개 ≈ +30토큰이고, 그것도 **주입 턴**이라 매 턴 재과금되는 바닥엔 안 얹힌다.
# ⭐ 값의 소유자는 core/prompts/common.REGROUND_COVERED_CAP 이다(2026-09-10) — 표현학습
#   쪽지도 같은 상한을 쓴다. 여기서는 **같은 이름으로 재수출**한다(call_session 이 이 이름으로
#   import 한다 — `call_session.py:85`). ⚠ 표현학습은 항목이 18개라 «공급원이 [:10]» 이라는
#   위 근거가 성립하지 않는다. 거기서는 이 값이 **나열만** 자른다(검출은 18개 전부 돈다).
# ⇒ 정의는 위 `from core.prompts.common import ...` 한 줄이 가져온다(재수출).


CLOSE_SEED_LEVELTEST = close_seed_leveltest()  # 하위호환 상수(기본 태그). 런타임은 함수를 쓴다.
