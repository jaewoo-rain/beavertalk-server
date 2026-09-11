"""normalcall 단일 양방향 브리지 — 5분 한국어 통화 본체(async 오케스트레이션).

────────────────────────────────────────────────────────────────────────────
🧒 12살에게 큰 그림부터: 이 파일이 하는 일은 "전화 교환수"다.
  한쪽 끝엔 학습자(휴대폰 앱 = 클라이언트, 이하 '클라'), 다른 쪽 끝엔 비버 선생님을
  연기하는 AI(구글 Gemini Live). 이 파일은 두 사람 사이에 앉아서 목소리를 실시간으로
  주고받게 이어준다. 그리고 5분이 지나면 "이제 시간 됐어요~" 하고 통화를 예쁘게 끊는다.

  왜 '실시간'이 어렵나? 전화는 내가 말하는 소리(클라→Gemini)와 상대가 말하는 소리
  (Gemini→클라)가 **동시에** 흘러야 자연스럽다. 한쪽씩 번갈아 하면 무전기처럼 뚝뚝
  끊긴다. 그래서 두 방향을 각각 쉬지 않고 퍼 나르는 '펌프(pump)' 2개를 **동시에** 돌린다.
  (펌프 = 물을 계속 퍼내는 기계처럼, 한 방향의 소리를 계속 받아서 반대편으로 밀어주는
   무한루프 코루틴.) 이게 이 파일의 심장인 '2펌프' 구조다.

  왜 TaskGroup? TaskGroup = "여러 일을 동시에 시키되, 하나라도 실패하면 나머지도
  깔끔히 멈추는 묶음". 펌프 하나가 죽었는데 다른 펌프만 계속 돌면 '반쪽짜리 좀비 통화'가
  된다(내 목소리는 가는데 상대 목소리는 안 오는 식). TaskGroup이 하나 죽으면 나머지를
  자동 취소해서 이런 어정쩡한 상태를 원천 차단한다.

  왜 절대 백스톱(asyncio.timeout)? Gemini 연결 자체가 ~10분쯤 되면 저쪽에서 먼저 뚝
  끊어버린다(우리가 통제 못 하는 종료 — 그러면 뒤처리를 우리가 못 챙긴다). 그래서 그 전에
  **우리가 먼저** 딱 끊어서 정리 순서를 우리 손에 쥔다. 이게 '절대 백스톱'(최후의 안전장치).

  왜 barge-in off? '바지인(barge-in)' = 상대가 말하는 도중에 끼어들어 말을 끊는 것. 비버가
  말하는 동안 마이크를 열어두면 AI가 자기 목소리·주변 잡음을 듣고 헷갈려서 말이 엉키거나
  끊긴다. 그래서 비버가 말할 땐 학습자 마이크 입력을 아예 안 보낸다(barge-in off). 트레이드
  오프: 진짜로 끼어들어 말 끊기는 못 한다. 하지만 학습앱이라 오히려 이게 더 안정적이고 안전.
────────────────────────────────────────────────────────────────────────────

beavertalk 의 검증된 bridge.py(2펌프 + 시계워처 + asyncio.timeout 절대 백스톱 +
TaskGroup + barge-in off)를 이 프로젝트로 포팅. 차이:
    - DB 는 동기 SQLAlchemy → normalcall_service 를 run_db(스레드풀+짧은세션)로 호출.
    - 통화중 1분마다 누적 세그먼트를 점진 flush(긴 통화·크래시 내성). 종료 시 나머지 flush.
    - 페르소나/레벨/locale 은 통화 시작 전 1회 DB 조회해 평범한 값으로 넘긴다(ORM 반입 금지).
    - 콜타입 라우팅(D11): ① start.call_type 명시(단 데모·prod 재측정은 normal 강등)
      ② 서버 자동(korean_level 미확정 → level_test, 데모는 자동 진입 금지).
      분기는 전부 통화 시작 전(대본·시드·call_type 기록)/종료 후(분석 디스패치)에만 —
      통화중 코드 경로(_run_session 이하)는 콜타입과 무관하게 동일하다.

⛔ 불변: TaskGroup 2펌프 · asyncio.timeout 절대 백스톱 · barge-in off · _finish_call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from typing import Iterable, NamedTuple, AsyncContextManager, Callable, Optional

from fastapi.concurrency import run_in_threadpool
from google import genai
from pydantic import BaseModel
from sqlalchemy.orm import sessionmaker

from core import audio, b2b_client, gemini_analysis
from core.audio import (
    INPUT_SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    frame_rms,
    pcm16_to_wav,
)
from core.config import Settings, settings as _settings
from core.languages import (
    DEFAULT_LANGUAGE,
    LanguageSpec,
    SUPPORTED_LANGUAGES,
    count_target_script_chars,
    normalize_locale,
    resolve_language,
)
from core.gemini_live import (
    DEFAULT_VOICE,
    SET_FACE_TOOL,
    LiveEvent,
    LiveSessionProtocol,
    open_session,
)
from core.persona_prompt import (
    _LOCALE_LABEL,
    CONTROL_TAG,
    REGROUND_COVERED_CAP,
    build_leveltest_instruction,
    build_resume_brief,
    seed_resume,
    build_continue_reminder,
    build_reground_brief,
    build_reground_reminder,
    build_system_instruction,
    close_seed_leveltest,
    is_closing_slot,
    new_close_tag,
    seed_leveltest_opening,
    seed_opening,
)
from core.prompts.expression import (
    NUDGE_SEED_1_EXPRESSION,
    build_expression_instruction,
    build_expression_reground_brief,
    seed_expression_opening,
    seed_expression_resume,
)
from core.prompts.freetalk import (
    NUDGE_SEED_1_FREETALK,
    build_freetalk_instruction,
    seed_freetalk_opening,
)
from core.stt import normalize_language_codes
from domains.learning.service import call_service
from domains.learning.service import quiz_judge
from domains.learning.service import normalcall_service as svc
from domains.learning.realtime.protocol import (
    HintExample,
    ServerCallEnded,
    ServerCallStarted,
    ServerError,
    ServerHint,
    ServerInputTranscript,
    ServerMessage,
    ServerOutputTranscript,
    ServerSentenceMarker,
    ServerPong,
    ServerTeachingPlan,
    ServerTurnEnd,
    ServerTurnStart,
    TeachingItem,
    client_adapter,
    server_adapter,
)

logger = logging.getLogger(__name__)

# 통화 길이: 경과 시 종료 시드(정상 작별 시작), 백스톱(강제 종료). 1분마다 중간 저장.
#
# 일반 통화 길이의 소스는 **구독 플랜**이다(Free 5분 / Pro·Max 15분 —
# call_service.CALL_DURATION_S_BY_PLAN). 아래 상수는 그 위에 얹는 **전 회원 강제값**으로,
# env(NORMAL_CALL_DURATION_S)가 있을 때만 값이 있다(없으면 None → 플랜이 결정).
# ⚠ 테스트는 이 모듈 속성을 monkeypatch 한다 — 값이 박히면 플랜 조회를 건너뛰므로
#   종전과 동일하게 동작한다(짧은 시계로 통화를 끝내는 회귀 테스트들이 그 경로다).
CALL_DURATION_S: Optional[float] = _settings.NORMAL_CALL_DURATION_S
# 레벨테스트(Phase 1): 인-콜 판정·주입 없이 비버 자율 진행. 종료는 3분 하드캡(이 시계) 또는
# 무음 3단/GoAway 가 종료 파이프로 우아하게 몬다(R5 안전망 — 서버는 통화중 질문을 주입하지 않음).
LEVELTEST_MAX_S = 180.0          # 레벨테스트 하드캡(3분) — call_duration_s 의 base
# 숙제 통화 하드캡(5분). 플랜·env·클라 override 를 **전부** 무시한다 —
# 교사가 낸 과제는 반 전체가 같은 조건이어야 하기 때문이다(2026-09-04 사장님 지시).
HOMEWORK_CALL_DURATION_S = 300.0
# 연결 자체 한계 ~10분(S2)을 선점: 서버가 GoAway/연결종료로 뚝 끊기 전에 우리가 먼저
# 우아하게 마무리하도록 540s(9분)로 하향. 정상 5분 통화는 이 상한에 닿지 않아 무영향.
ABSOLUTE_CALL_TIMEOUT_S = 540.0  # 이 상한(9분) 넘으면 강제 종료(백스톱, 연결 ~10분 선점)
# ── 세션 재연결(15분 통화) ──────────────────────────────────────────────────
# ⭐ 압축은 **세션**(오디오 15분) 한계만 풀고 **연결 수명(~10분)** 은 못 푼다. 둘은 별개다.
#   15분 통화 = Live 연결 2개 이상이라는 뜻이고, 그래서 세대 루프가 필요하다.
#
# 스왑 트리거는 GoAway 가 **아니라** 이 시계다. GoAway 는 언제 올지·time_left 형식이
# 무엇인지 우리가 못 정하는 반면, 시계는 결정적이라 테스트가 발화시킬 수 있다. GoAway 는
# 보조 트리거, 스트림 종료는 폴백.
SEED_TO_HANGUP_S = 22.0        # 종료 시드 후 정상 종료 안 되면 강제 종료까지(작별 절단 방지 여유. 진짜 상한은 ABSOLUTE_CALL_TIMEOUT_S)
PLAYBACK_DONE_WAIT_S = 7.0     # call_ended 후 playback_done ack 대기 상한(작별 꼬리 드레인 여유 —
#                                클라가 작별 오디오 다 재생(최대 6s)한 뒤 ack 보내므로 그보다 길게)
FLUSH_INTERVAL_S = 60.0         # 통화중 누적 세그먼트 점진 저장 주기(1분)
# 국적 추론(_trigger_nationality)용으로 붙잡아 두는 **user 원음 상한**(초).
#
# 🧒 왜 상한이 필요한가: 예전엔 통화 오디오 전체(비버+학습자)를 state.segments 안에 통화가
#   끝날 때까지 들고 있었다. DB 저장(flush)이 끝나도 안 놓아줬다 — 15분 통화면 통화당
#   30~50MB 가 계속 RAM 에 앉아 있고, Cloud Run 인스턴스 하나가 받을 수 있는 동시 통화 수를
#   그대로 깎아먹는다. 그런데 저장이 끝난 뒤에도 그 바이트가 실제로 필요한 곳은 **딱 하나**,
#   통화후 국적 추론뿐이고 그건 user 원음만 쓴다. 그래서 저장 직후 비버 PCM 은 전량 놓아주고,
#   user PCM 은 이 상한만큼만 따로 이어붙여 보관한다(나머지는 놓아준다).
# 60초 = 호출 게이트(NATIONALITY_MIN_SPEECH_S, 기본 10초)의 6배라 추론 표본은 넉넉하고,
# 16k·PCM16 기준 약 1.9MB 로 고정된다(통화가 길어져도 안 자란다).
NATIONALITY_PCM_MAX_S = 60.0
# 무음 3단 넛지(A2): 클라 마이크는 상시 스트리밍이라 무음을 오디오 부재로 못 잰다 —
# 무음 = 마지막 활동(학습자 in_tr / 비버 turn_end / 넛지) 이후 경과. 비버 idle(turn_id None)일 때만
# 카운트하고, 각 단계는 "직전 활동 이후" 신선한 무음을 잰다(비버 발화 직후 넛지 폭발 방지).
IDLE_NUDGE1_S = 60.0  # 1단: 비버 발화 종료 후 무음 60s → 새 화제로 가볍게 이어가라(작별 금지). 학습자가 한국어 문장을 떠올리는 시간을 넉넉히 준다(짧으면 생각 중에 넛지가 끼어듦)
IDLE_NUDGE2_S = 10.0  # 2단: 1단 넛지 후 재무음 10s → 모국어로 "거기 있어?" 확인
IDLE_CLOSE_S = 12.0   # 3단: 2단 넛지 후 재무음 12s → 작별 시드 직접 주입(우아한 종료)
# 레벨테스트(fast-probe) 무음 캐던스: 3분 안에 여러 계단을 재야 해 일반보다 짧게. 값은
# run_call 이 call_type 에 따라 state.idle_* 에 꽂는다(일반은 위 상수 그대로 — 바이트 무변경).
LEVELTEST_IDLE_NUDGE1_S = 60.0  # 1단: 무음 60s(일반과 동일) → 방금 질문을 더 쉽게/선택지로 다시(작별 금지). 학습자가 긴 답변을 깊게 생각하는 시간을 넉넉히(25s는 생각 중에 넛지가 끼어들었음)
LEVELTEST_IDLE_NUDGE2_S = 8.0   # 2단: +8s → 모국어 확인
LEVELTEST_IDLE_CLOSE_S = 10.0   # 3단: +10s → 종료 시드 주입
# ── 레벨테스트 Phase 2: 종료 판정 전용 사이드카('끝낼까 말까'만 — 질문 주입 0) ──
# 서버가 매 유저 답변을 사이드카로 조용히 판정(answer_in_target·should_end)하고, 종료 트리거가
# 서면 종료 시드만 주입한다. ★ 질문은 절대 주입하지 않는다(should_close 만 세우고 기존 종료
# 파이프에 합류). 최종 레벨은 통화후 판정관(전사 전체)이 정한다 — 사이드카는 종료 트리거 전용.
# 종료 트리거 3종: ① should_end(판정관 등반실패) ② 비화자 결정론 컷(answer_in_target=False 연속)
#   ③ 하드 턴캡(total_answers >= MAX_ANSWERS — 무한 관측 방지).
LEVELTEST_BAND_TIME_FLOOR_S = 45.0  # 조기종료 시간 플로어(경과 최소 — should_end/비화자컷에 적용, 초반 표본 조기종료 방지)
LEVELTEST_BAND_MAX_ANSWERS = 10     # 관측 답변 수 안전 상한(하드 턴캡 — 이 수 넘으면 종료)
LEVELTEST_BAND_NONSPEAKER_MAX = 5   # 대상 언어 산출 실패(answer_in_target=False)가 이만큼 연속이면 비화자 결정론 컷(한국어 못 하는 사람이 오래 붙잡히는 역설 방지)
# 종료 판정 사이드카(C): 매 답변마다 전체 전사를 LLM에 넣어 "지금 끝내도 되나(should_end)" 판정 —
# 등반 실패(정체·막힘)를 맥락으로 조기 종료. 시간 플로어·최소 답변 충족 후에만 반영.
LEVELTEST_END_JUDGE_MIN_ANSWERS = 3  # should_end 조기종료를 반영하기 시작하는 최소 답변 수(성급한 종료 방지)
# ── 재접지(단계 3: 시각 비율 → 압축 신호 통합) ────────────────────────────
# 🧒 재접지 = 대화가 길어지면 AI 가 캐릭터·규칙·지금까지 한 얘기를 조금씩 잊는데(드리프트),
#   중간중간 짧게 되박아 주는 것. 옛날엔 "통화 길이의 50%·80% 지점"이라는 **시계**로 넣었다.
#
# 왜 시계를 버리나: 진짜로 기억을 지우는 건 시간이 아니라 **컨텍스트 압축**이다. 압축은
#   trigger_tokens 에 닿는 순간 오래된 대화부터 버리는데, 그 시점은 통화마다 다르다(말이
#   많으면 빨리, 적으면 늦게). 시계로 맞추면 압축과 어긋나 "이미 잊은 뒤에 되박는" 일이 생긴다.
#
# 3층 구조(우선순위):
#   ① 선제 arm  — prompt_token_count 가 ARM_RATIO × trigger 를 넘으면 = 압축 임박.
#                 압축 **직전**에 얹은 요약은 컨텍스트 최신단이라 그 압축을 반드시 살아남는다.
#   ② 사후 감지 — prompt 가 직전 최고치 대비 급감 = 압축이 이미 일어났다. ①이 유저 침묵으로
#                 얹히지 못한 채 지나간 경우의 보정.
#   ③ 시간 폴백 — usage_metadata 가 아예 안 오는 환경(필드 미제공·모킹)에서도 돌아야 한다.
#                 마지막 주입 이후 GAP 경과면 arm(R5 — 자동으로 옛 시간 기반 동작으로 강등).
REGROUND_ARM_RATIO = 0.85        # 압축 임박 판정(× LIVE_CTX_TRIGGER_TOKENS)
# ⭐ T15-6 표현학습 전용: 바닥 위 남은 자리(trigger − floor)가 이보다 좁으면 ①임박 산식을 끈다(위 _reground_due
#   주석). 1398 에서 첫 arm 이 40초에 섰다 — 40초 분량의 대화 토큰(입력 오디오 ≈ 수백~천 토큰)이 room×0.85 를
#   넘었다는 뜻이라 room 이 대략 1,000~2,000 대였다는 역산. 그 두 배를 하한으로 둔다. 일반 통화엔 적용 안 됨.
EXPR_REGROUND_MIN_ROOM_TOKENS = 3000
# 사후 감지 문턱. **절대 토큰이 아니라 압축 낙차(trigger − target)에서 파생**한다.
#
# ⛔⛔ 2026-08-17: 여기 절대값(2000)과 peak 대비 비율(0.85)이 **둘 다 눈이 멀어 있었다.**
#   두 값 다 16000/12000 시절에 맞춰져 있었는데 prod 가 8000/7000 으로 내려가면서
#   낙차 상한이 1000 이 됐다 ⇒ 2000 은 **영원히 못 넘고**, 실측 낙차 494 는 peak 대비
#   0.936 이라 0.85 문턱도 못 넘는다. 실측 call 1045: 7,659 → 7,165(낙차 494),
#   `compressions=0`. 압축은 **실제로 돌았다**(monotonic=false·재연결 0·last_prompt≈target).
#   ⇒ 설정을 바꿀 때마다 같이 안 움직이는 상수는 계측을 조용히 죽인다.
#
# 새 규칙 두 개(둘 다 만족해야 압축):
#   ① 그럴듯함 — peak 가 ARM_RATIO × trigger 이상. 압축이 **일어날 수 있는 자리**였나.
#      (예전 "peak 대비 0.85" 자리를 대신한다. 작은 컨텍스트의 요동을 여기서 자른다.)
#   ② 낙차 — 기대 낙차(trigger − target)의 DROP_MIN_RATIO 이상 떨어졌다.
# ⚠ 0.4 의 근거: 실측 낙차/기대 낙차 = 494/1000 = 0.49 다(압축이 target 보다 조금 위에서
#   멈춘다 — 턴 경계 컷). 그 아래로 여유를 두되 절반보다는 낮게. 16000/12000 에 대입하면
#   1600 이라 옛 2000 과 같은 자리대다 — 잡음 배제라는 원래 목적을 유지한다.
REGROUND_DROP_MIN_RATIO = 0.4
# 연속 주입 최소 간격. 압축 1회당 1회 arm 을 **여기가 최종적으로 조인다.**
#
# ⛔⛔ 2026-09-06: 60 → 150. 압축이 통화당 몇 번 도는지는 우리가 못 정한다 —
#   실측 통화 1324 는 5분에 **6회**였다(8,079→7,096 / 15,130→7,481 / … 매 턴 경계마다).
#   바닥(지시문)이 트리거의 90% 라 모든 턴 경계에서 "트리거 초과"가 참이기 때문이다.
#   ⇒ 압축마다 얹으면 5분에 5~6회, 그것도 전부 `자리=마이크`(학습자가 말을 꺼내는 순간)라
#   말허리를 자른다(QA #2 — 답하는 도중 다른 항목으로 점프). 재접지는 **드리프트 보정**이지
#   턴 진행 장치가 아니다.
#   ⇒ 5분 2회 / 15분 6회로 조인다. 잊는 건 여전하지만 **끊는 횟수**를 절반 이하로 줄인다.
#   ⚠ 이 값을 되돌리려면 압축 횟수부터 줄여라(트리거 상향 — docs/20260906_1820_* §5-C).
REGROUND_MIN_GAP_S = 150.0
# 프롬프트에 실을 **예비** 학습항목 상한(본편은 전량). 화면 카드(teaching_plan)는 이 값과
# 무관하게 선별분 전량이 간다 — 두 소비처가 갈라져 있다(주입 1449행 / 카드 1476행).
#
# ⭐ 왜 가르나(2026-09-06): 선별은 예비를 25개 준다(mastery_repository.STUDY_RESERVE_TOTAL).
#   그런데 Live 는 **매 턴 프롬프트 전액 재과금**이라, 5분 통화에서 4~5개밖에 안 쓰는 예비
#   25개를 20턴 내내 다시 실어 나른다. 실측 렌더 — 예비 25개 = 1,512자(지시문의 16.2%),
#   5개로 줄이면 1,205자(≈900~1,200토큰) 감소. 턴당 원가의 약 10% 다.
# ⚠ 화면은 안 바뀐다. 카드가 줄면 학습자가 "오늘 할 게 줄었다"고 읽는데 그건 사실이 아니다.
# ⚠ 15분 통화(이어하기)에서 예비 5개가 모자란지는 **아직 안 쟀다**. 모자라면 여기만 올린다.
PROMPT_RESERVE_MAX = 5
REGROUND_MAX_PER_CALL = 8        # 통화당 주입 상한(15분 예상 6회 + 여유). 폭주 방지 하드캡
# 표현학습 진도 판정 사이드카의 통화당 상한(폭주 방지).
# ⚠ 정상 흐름은 «재접지 arm 마다 1회 + 조각 끝 1회» 라 최대 9회다(arm 상한이 8).
#   12 는 그 위의 여유이고, 넘으면 코드가 멈춘다 — 미판정은 «통과 안 함» 이라 안전한 방향이다.
EXPR_PROGRESS_MAX_PER_CALL = 12
# STT 폴백 판정기에 넘길 퀴즈 창 전사 길이 상한(글자). 퀴즈 구간(3문항)이 이걸 넘을 일은 없다 — 규칙만 유지한다.
EXPR_TRANSCRIPT_MAX_CHARS = 12000
# ⚠ T16(2026-09-11): 옛 «커서 이후 + 겹침 4 · 경계선» 창 규칙은 **지웠다** — 판정 입력은 서버가 연 퀴즈 창
#   (open_seg..닫힘)뿐이라 경계가 없다. 위 상수의 역사는 docs/prompts/README.md §8 (T14 D~F) 에 남아 있다.
# ⭐⭐ **조각 끝 마지막 판정의 상한**(초). 늦으면 있는 것만 쓰고 진행한다.
#   ⛔ 없애지 마라 — 무한정 기다리면 LLM 이 죽었을 때 **조각2 가 안 열린다.** 통화가 멈추는
#     게 진도 하나보다 나쁘다(R5).
#
# ⛔⛔ **조각 경계의 빈 시간은 «놀고 있는» 시간이 아니다**(2026-09-10 정정). 그 구간에서
#   이어하기 요약이 fire-and-forget 으로 돌고 있고, **그 경합에서 이미 한 번 졌다** —
#   위 :2062 주석의 실측이 그것이다(07:00:48 저장 → 07:00:51 이어하기 → 07:00:53 요약 완성).
#   최악 사례 간격 **3초**. 여기서 저장이 늦으면 조각2 의 선별이 옛 DB 를 읽어 같은 항목을 또
#   가르친다(재드릴 1회 — 선별상 정상 경로, 데이터 오염은 아니다).
#
# ## 왜 2.0초인가 (2026-09-11, T14 2차 반려 P1 — bt-back·fable)
#   실측점은 **하나**다: 1397 에서 옛 지시문 2.3k + 전사 전체 3.9k ≈ **6.2k 자 → 1,201ms**.
#   T14 뒤의 입력은 새 지시문 4.4k(fable 실측, 실제 뜻·예문 포함) + 창(커서 이후 + 겹침 4 ≈ 2.0k)
#   ≈ **6.4k** — 전사에서 뺀 만큼(−1.9k)을 규칙 블록이 도로 먹었다(+2.1k). 초과 사례보다 크다.
#   1.2초였던 근거(«축소해서 상한 안에 든다»)는 **수치로 안 선다.** 그래서:
#         2.0초 상한 → 조각2 경합 최악 3초 중 **여유 1.0초**
#   마지막 퀴즈 유실(1.2초면 **확정**)이 조각2 재드릴 위험(여유 1초)보다 비싸다 — 그게 T14 가
#   없애려던 증상이다. 계측(«마지막 판정 %.0fms»)은 이미 있다 — **첫 실통화에서 그 줄을 보고 다시 정한다.**
#   ⛔ «요약이 1.0~1.4초니 비슷하겠지» 같은 유추로 이 수치를 다시 만지지 마라 — 실측점만 근거다.
# ⭐ T16: 이 상한은 **STT 폴백 LLM 호출에만** 걸린다 — 서버 검색(passed/failed 확정)은 즉시라 예산을 안 쓴다.
#   입력이 퀴즈 창뿐이라 실측은 줄어들 것 — 로그 1줄로 확인한다.
EXPR_FINAL_JUDGE_TIMEOUT_S = 2.0
# 시간 폴백 간격 = clamp(통화길이 / 2.5, 120s, 240s).
#   5분(300s) → 120s → 2회 = 옛 0.5·0.8 지점 2회와 실질 동일(5분 하위호환)
#   15분(900s) → 240s → 3회 + 압축 arm ≈ 6회 = 0.40회/분(5분과 같은 빈도)
REGROUND_GAP_DIVISOR = 2.5
REGROUND_GAP_MIN_S = 120.0
REGROUND_GAP_MAX_S = 240.0
# 재접지 모드 스위치(이상 시 코드 한 줄로 하드닝 폴백):
#   "on_user_turn" — 신방식: arm 후 유저 발화 시작(첫 in_tr) 시 그 턴에 얹기(turn_complete=False).
#                    비버가 [유저발화+리마인더]에 1회 응답 → 이중발화·종료오염 제거 목표.
#                    ⚠ 오디오 턴+텍스트 병합은 Gemini 미보장 → 실측 검증 대상(T7).
#   "legacy_idle"  — 구방식: duration/2 idle 에 send_reground(turn_complete=True) 별도 응답(이중발화).
#   "off"          — 재접지 전면 비활성 = 하드닝만(가장 안전한 폴백).
# ⭐ env 로 바꾼다(`LIVE_REGROUND_MODE`) — 끄고 재는 실험에 재빌드가 들지 않게
#   (2026-09-02). 기본값은 종전과 같다.
REGROUND_MODE = _settings.LIVE_REGROUND_MODE
# ⭐ 어느 통로로 나갔는지 **로그에 남긴다** — 두 통로의 `interrupted` 발생률을 로그만으로
#   비교하려면 통화마다 이 값이 찍혀 있어야 한다(env 로 바뀌므로 코드만 봐선 모른다).
REGROUND_TRANSPORT = _settings.LIVE_REGROUND_TRANSPORT
# 재접지 얹기 시점. ⭐ env 로 바꾼다(`LIVE_REGROUND_ATTACH_AT`) — 자리를 바꿔 재는 실험에
#   재빌드가 들지 않게(2026-09-02). 상세한 근거는 그 설정의 주석에 있다.
#   "mic_open"(기본) — 업링크 마이크 프레임. **도착 자체가 비버 침묵의 증거**다.
#   "first"/"final"  — 옛 자리(입력 전사). ⛔ 전사는 비버 답과 **같이 오는 늦은 보고서**라
#                      얹는 순간 비버가 이미 입을 연 뒤일 수 있다(실측 82회 중 6회 잘림).
REGROUND_ATTACH_AT = _settings.LIVE_REGROUND_ATTACH_AT
REGROUND_VOICE_RMS = _settings.LIVE_REGROUND_VOICE_RMS
# 교육 대상 언어 기본 라벨(오버라이드/미지원 폴백 시). 언어 결정은 core.languages 레지스트리가
# 소유 — 여기선 파생 라벨만. ko.label == "한국어" 라 기존 통화 프롬프트 바이트 불변.
_DEFAULT_TARGET_LABEL = SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE].label

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
def _close_seed(close_tag: str) -> str:
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
_NUDGE_SEED_1 = (
    f"{CONTROL_TAG} 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고 "
    "가볍게 새 화제로 한 문장만 이어가라."
)
_NUDGE_SEED_2 = (
    f"{CONTROL_TAG} 학습자가 계속 조용하다. 이 메시지는 소리내 읽지 말고, 모국어로 "
    "'거기 있어? 잘 들려?'를 한 번만 부드럽게 물어라."
)
# 레벨테스트 1단 넛지: 일반과 달리 '새 화제로 이어가라' 대신 **방금 질문을 다시 묻는다** —
# 작별하지 말고 방금 한 질문을 더 쉽게 바꾸거나 선택지를 주며 모국어로 다시 묻게 한다.
_NUDGE_SEED_1_LEVELTEST = (
    f"{CONTROL_TAG} 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고 "
    "방금 한 질문을 더 쉽게 바꾸거나 선택지를 주며(예/아니오 또는 둘 중 고르기) "
    "모국어로 딱 한 번만 다시 물어라."
)

# ── 자기낭독 안전망(2026-07-27) ────────────────────────────────────────── #
# 비버가 서버 제어 태그를 **소리 내어 읽으면**, 그 출력이 자기 컨텍스트에 남아 다음 턴에
# 종료 신호로 읽힌다(자기충족 루프). 실측 call_id=706: 서버 주입 0인데 t≈80s 에
# '"[시스템]" 종료' 를 읽고 혼자 작별 → 통화는 안 끊긴 채 47초 死구간 → 사용자가 직접 종료.
#
# 태그 분리·난수화로 확률은 낮췄지만 그건 전부 "모델이 지시를 지킨다"에 기대는 방어다.
# 이 검출·되돌리기만이 모델에 의존하지 않는다 — 뚫려도 死구간이 안 생기게 한다.
#
# ⚠ out_tr 은 토큰 단위로 쪼개져 온다("[시스템]" / " 통화가"). 청크 하나만 보면 대괄호가
#   갈라져 못 잡으므로 **턴 누적 텍스트**에 대해 검사한다(turn_end 시점, flush 직전).
#
# ⛔ 태그 **이름 화이트리스트**로 되돌리지 마라. 옛 정규식은
#   `\[\s*(?:시스템|안내|통화종료|통화\s*시작)[^\]]*\]` 로 아는 이름만 잡았는데,
#   모델이 새 이름을 지어내면 그대로 뚫린다 — 실측 call_id=870 에서 서버에 존재하지도
#   않는 "[마무리]" 를 8번 출력했고 필터가 한 번도 안 걸렸다(그 사이 비버는 4분 24초에
#   자체 종료하고 60초를 죽은 채 보냈다). 이름을 나열하는 방어는 항상 한 발 늦는다.
#
# 이제 **이름과 무관하게 대괄호 덩어리 전부**를 제어 태그로 본다. 음성 대화 전사에
# 대괄호가 나올 일은 사실상 없어(말은 라벨이 아니다) 오탐 위험이 낮고, 30일치 실측에서도
# 옛 정규식의 오탐은 0이었다 — 좁혔던 건 이름뿐이니 이름만 푼다.
#
# ⚠ 맨 앞으로 앵커(^)하지 마라. 비버는 태그를 따옴표째 인용하며 읽는다 — 실측 회귀
#   케이스가 '"[시스템]" 종료' 라 대괄호 앞에 따옴표가 붙는다. 앵커를 걸면 놓친다.
# 안쪽 길이는 40자로 묶는다(대괄호 두 개가 멀리 떨어져 문장을 통째로 삼키는 것 방지).
_CONTROL_TAG_RE = re.compile(r"\[[^\]]{0,40}\]")

# ⭐ T14-C4 **자리표시 예외**(2026-09-11, 통화 1397 실측). 커리큘럼의 ◯◯ 자리표시를 비버가
#   영어로 옮겨 "How do you say 'I'm from [Country]'?" 라고 말했는데, 위 정규식이 그걸 제어
#   태그 누출로 잡아 **정상 문장을 자르고 복구 시드를 넣었다**(33:37). [Name]·[Place] 도 같다.
#
#   기준은 **위치**다(T14 반려 P2-2, fable QA). 어휘 화이트리스트도, «대문자 한 단어» 모양만도 아니다:
#     · 발명 태그는 발화 **맨 앞**에 온다 — call 870 «[마무리] 네. 수고하셨어요» ×8. 표현학습은 비버가
#       영어로 말하니 같은 발명이 «[Closing] Bye!» «[Note] …» 꼴로 나온다 — 모양만 보면 자리표시와
#       정확히 겹쳐 **빠진다.** 위치로 가른다: 맨 앞 대괄호는 무조건 누출이다.
#     · 자리표시는 문장 **안**에 온다 — «I'm from [Country]». 안쪽이 영문 낱말(공백·아포스트로피
#       허용)이면 자리표시로 보고 남긴다. 어휘를 미리 다 알 필요가 없다.
#     · 한글·콜론·숫자를 품은 대괄호(«[공부 모드]» «[시스템]» «[통화종료:ab12]»)는 **어디에 있어도**
#       누출이다 — 우리 제어 태그는 전부 이 꼴이고, 비버는 그걸 따옴표째 인용하며 읽는다
#       (실측 '"[시스템]" 종료' — 위 «맨 앞으로 앵커하지 마라» 지뢰가 그 얘기다). 그래서 «맨 앞»
#       판정은 따옴표·공백을 건너뛰고 본다: '"[Closing]" Bye' 도 맨 앞이다.
#   ⚠ daec11a 주석의 «[Quiz Time] 은 두 단어라 걸린다» 보증은 **문장 안에서는 이제 없다** — 영문 다단어
#     대괄호는 문장 안이면 자리표시([Your Name])로 통과한다(2차 반려 문서 항목). 실증은 없다. 옛 사고(706·870)는
#     전부 **맨 앞·한글**이었고 그 둘은 그대로 걸린다. 다음 사람은 daec11a 주석을 믿지 마라.
_PLACEHOLDER_TAG_RE = re.compile(r"^\[[A-Za-z][A-Za-z' ]*\]$")
_LEADING_JUNK_RE = re.compile(r"""^[\s"“”'‘’(]*""")


def _is_placeholder_tag(tag: str) -> bool:
    """`[Country]`·`[Your Name]` 처럼 **영문 낱말만** 든 대괄호인가(모양 검사 — 위치는 안 본다)."""
    return bool(_PLACEHOLDER_TAG_RE.match(tag))


# ⭐ **커리큘럼 표기의 대괄호는 누출이 아니다**(3차 반려 P1-B, codex — bt-back 이 자산을 셌다: grammar.json
#   459 항목 중 **21개**가 정상 표면형에 대괄호를 쓴다). «N이/가 있어요[없어요]» · «이거는[그거는, 저거는]N이에요/
#   예요» · «N에 가요[와요]» · «N 앞[뒤, 옆]» · «N개[병, 잔, 그릇]» … 이게 obj 그대로 지시문에 실리고, 비버가
#   읽으면 **한글 대괄호 = 위치 무관 누출**이라 저장 전사가 훼손되고 `tag_leak_seen` 으로 재개 시드까지 들어간다.
#   L2 문법을 가르치는 순간 터진다(codex 재현: 「오늘 표현은 N이/가 있어요[없어요]입니다」 → `[없어요]` 삭제).
#   ⇒ 허용 목록: **이번 통화 expr_items 표면형에 든 대괄호 조각**만 누출에서 뺀다(통화 시작에 집합으로 뽑아 state).
#   ⛔ `[안내]`·종료 태그·맨 앞 발명 태그 방어는 그대로다 — 허용은 그 통화의 항목에서 온 조각만이다.
#   ⚠ 커리큘럼 표기를 음성용으로 정규화하는 길(«있어요/없어요»)은 안 간다 — 가르치는 내용을 바꾸는 일이고
#     korean-linguist 몫이다.
def _expression_bracket_allowlist(items: "Iterable[dict]") -> frozenset[str]:
    """이번 통화 항목 표면형(obj)에 든 `[…]` 조각 전부 — 누출 필터의 허용 목록."""
    out: set[str] = set()
    for it in items or ():
        for m in _CONTROL_TAG_RE.finditer(str((it or {}).get("obj") or "")):
            out.add(m.group(0))
    return frozenset(out)


def _is_control_tag_leak(text: str, m: "re.Match[str]", allow: frozenset[str] = frozenset()) -> bool:
    """이 대괄호가 누출인가 — 모양이 영문 자리표시여도 **발화 맨 앞이면 누출**(발명 태그).
    이번 통화 항목 표면형에서 온 조각(`allow`)은 어디에 있어도 누출이 아니다."""
    if m.group(0) in allow:
        return False
    if not _is_placeholder_tag(m.group(0)):
        return True
    return m.start() == _LEADING_JUNK_RE.match(text).end()


def _find_control_tag_leak(text: str, allow: frozenset[str] = frozenset()) -> "re.Match[str] | None":
    """제어 태그 누출을 찾는다 — **문장 안의 자리표시와 이번 통화의 커리큘럼 대괄호는 건너뛴다.**

    ⚠ `_CONTROL_TAG_RE.search` 를 직접 쓰지 마라. 그러면 첫 대괄호가 자리표시일 때 거기서
      멈춰 뒤에 있는 진짜 누출을 놓치거나, 반대로 자리표시를 누출로 잡는다. 전부 훑는다.
    """
    for m in _CONTROL_TAG_RE.finditer(text):
        if _is_control_tag_leak(text, m, allow):
            return m
    return None


def _scrub_control_tags(text: str, allow: frozenset[str] = frozenset()) -> str:
    """저장본에서 제어 태그를 걷어낸다 — **문장 안의 자리표시·커리큘럼 대괄호는 남긴다**(비버의 정상 대사다)."""
    return _CONTROL_TAG_RE.sub(
        lambda m: "" if _is_control_tag_leak(text, m, allow) else m.group(0), text
    )

# ⭐⭐ **비버가 tool 호출을 「글자로」 뱉는 것**을 걷어낸다(2026-09-01 실측).
#
#   모델이 `set_face` 를 함수로 부르지 않고 **대사에 섞어 말한다.** 그러면
#   `tool_call` 이벤트가 안 오고(로그에 `Live tool 즉시응답` 이 없다), 그 문자열이
#   그대로 자막에 찍힌다. 실측(call 09-01 23:00~23:01):
#
#       🦫 beaver: set_face{emotion:sad}
#       BEAVER[t7] 읽기=측정불가(소리 0.0초 글자 21)      ⛔ 그 턴이 통째로 벙어리
#       … 12초 침묵 … 👤 USER[t8]: 여보세요.              ← 사장님이 부르셨다
#       🦫 beaver: set_face{emotion:neutral}}아, 친구      ⛔ 중괄호가 두 개, 대사가 이어붙음
#
#   ⚠ **표정은 이미 못 쓴다** — 함수가 안 불렸으니 마커도 없다. 여기서 하는 일은
#     「자막이 더러워지는 것」과 「저장본이 오염되는 것」을 막는 것뿐이다.
#   ⚠ INJECT 와 무관하다. 2026-08-26(INJECT 이전)에도 같은 무늬가 있었다:
#     `response:set_face{emotion:laughWhoa, asking about me…`
#
#   ⛔ 닫는 괄호를 요구하지 않는다 — 실측이 `{emotion:sad}` · `{emotion:neutral}}` ·
#     `{emotion:laughWhoa` 처럼 **제각각**이다. 여는 괄호부터 «닫는 괄호 또는 공백»
#     까지를 최소 일치로 지우고, 남은 닫는 괄호는 뒤에서 정리한다.
#   ⛔⛔ **대사를 먹지 않는 것이 최우선이다.** 닫는 괄호가 없는 변형
#     (`{emotion:laughWhoa, asking about me?`)에서 «괄호부터 끝까지»로 지우면
#     학습자에게 보여야 할 문장이 통째로 사라진다 — 오염보다 나쁘다.
#     ⇒ 안쪽은 **감정 이름이 될 만한 것까지만** 문다: 값은 영문 소문자 낱말 하나다
#       (neutral·happy·surprised·sad·angry·laugh). 그 뒤에 붙은 글자는 대사다.
_FACE_ECHO_RE = re.compile(
    r"`?\s*(?:response\s*:\s*)?set_face\s*"
    r"[({\[]\s*(?:emotion\s*[:=]\s*)?['\"]?[a-z_]{0,12}['\"]?\s*[)}\]]*\s*`?"
)


# ⭐ **두 번째 무늬 — 함수는 제대로 불렸는데 인자 JSON 의 꼬리만 샌다**(2026-09-09, 통화 1365).
#   위 무늬(09-01)는 "함수를 안 부르고 통째로 말해버린" 것이라 `set_face` 라는 글자가 남아
#   있었다. 이번 건 다르다 — 호출은 정상이었고(`Live tool 즉시응답: set_face`, 05:59:32),
#   그 **인자의 뒷토막만** 말 채널로 새어 t5 의 **첫 조각**으로 나갔다:
#
#       05:59:46.712  🦫 beaver: :"happy"}                    ← 조각 하나가 통째로 이것
#       05:59:55.404  BEAVER[t5]: :"happy"}Not bad, you actually know it! ...
#
#   ⛔ 옛 필터가 둘 다 비껴간다: `_CONTROL_TAG_RE` 는 [대괄호]만 보고, `_strip_face_echo`
#     는 첫 줄이 `if "set_face" not in text` 라 **글자가 없는 파편**은 즉시 통과했다.
#   빈도: 1365 에서 표정 호출 22번 중 1번(≈5%). 같은 날 1360 엔 없었다.
#
#   ⛔⛔ 여기서도 **대사를 먹지 않는 것이 최우선**이다. 그래서 두 겹으로 좁힌다:
#     ① 값이 **닫힌 어휘**여야 한다(neutral·happy·surprised·sad·angry·laugh — gemini_live 의
#        enum 과 같은 목록). 임의 낱말을 물지 않는다.
#     ② **닫는 중괄호를 요구한다.** 이게 진짜 앵커다 — 따옴표 친 낱말 뒤에 `}` 가 오는
#        문장은 자연 발화에 없다. 반대로 위 09-01 무늬는 닫는 괄호가 제각각이라 요구하지
#        않았다(거기선 `set_face` 글자가 앵커였다). **앵커가 다르니 규칙도 다르다.**
_FACE_JSON_TAIL_RE = re.compile(
    r"""\{?\s*(?:"?emotion"?\s*)?:?\s*["'](?:neutral|happy|surprised|sad|angry|laugh)["']\s*\}+"""
)


def _strip_face_echo(text: str) -> str:
    """대사에 섞인 `set_face{...}` 와 그 **인자 JSON 파편**을 걷어낸다.

    둘은 무늬가 다르다 — 위 두 주석 블록 참조. 해당 없으면 원문 그대로 반환한다.
    """
    if "set_face" in text:
        text = _FACE_ECHO_RE.sub("", text)
    if "}" in text:                      # 값싼 사전 검사 — 파편엔 반드시 닫는 괄호가 있다
        text = _FACE_JSON_TAIL_RE.sub("", text)
    return text


# 통화당 재개 시드 주입 상한(무한 루프 방지).
# 2 → 6: 탐지가 이름 화이트리스트에서 "맨 앞 대괄호 전부"로 넓어져 잡히는 빈도가 오른다.
# 상한을 넘기면 서버가 되돌리기를 포기해 통화가 死구간으로 끝난다 — 옛 상한 2 는 30일간
# 8건의 누출 중 3건에서 소진됐다(즉 3통화가 그 상태로 죽었다). 되돌리기는 텍스트 1회
# 주입이라 비용이 거의 없으니, 무한 루프만 막을 정도로 넉넉히 둔다.
_RESUME_MAX = 6
# 태그가 섞인 턴이라도 소리가 이만큼 있고 본문이 남으면 «벙어리 턴» 이 아니다 — 재개 시드를 넣지 않는다(T17-1).
TAG_LEAK_SPOKEN_MIN_S = 0.5
_RESUME_SEED = (
    f"{CONTROL_TAG} 이 메시지는 소리내 읽지 마라. 통화는 아직 끝나지 않았다 — 방금 네 발화에 "
    "대사가 아닌 문구가 섞였거나 먼저 작별하려 했는데, 둘 다 하지 마라. 사과·설명·메타 발언 "
    "없이 방금 하던 대화를 그대로 이어서 학습자에게 한마디만 건네라."
)

SessionFactory = Callable[..., AsyncContextManager[LiveSessionProtocol]]

# 통화후 분석 task 강참조 보관소(GC 방지).
# 🧒 왜 이 집합이 필요한가: asyncio.create_task 로 백그라운드 작업을 띄우면, 파이썬은
#   그 작업을 아무도 '붙잡고' 있지 않으면(어떤 변수도 가리키지 않으면) 도중에 쓰레기라고
#   여기고 없애버릴 수 있다(가비지 컬렉션=GC). 그러면 통화후 분석이 조용히 중간에 사라진다.
#   그래서 이 집합(set)에 task 를 넣어 **강하게 붙잡아** 끝까지 살아있게 한다. 작업이 끝나면
#   done 콜백(_on_analysis_done)이 집합에서 빼내 메모리 누수도 막는다(붙잡되, 끝나면 놓는다).
_analysis_tasks: set[asyncio.Task] = set()


def _new_turn_id() -> str:
    return uuid.uuid4().hex[:12]


async def _send_json(ws, message: ServerMessage) -> None:
    await ws.send_text(server_adapter.dump_json(message).decode("utf-8"))


def _resolve_target_language(settings: Settings, override: Optional[str]) -> LanguageSpec:
    """교육 대상 언어 결정 → LanguageSpec(멀티랭귀지).

    is_demo 개념 폐지: prod/dev 구분 없이 지원 언어면 그대로 간다. override(언어코드,
    _read_initial_start 가 resolve 한 값)가 없거나 미지원이면 settings.DEFAULT_TARGET_LANGUAGE
    로 폴백(warning). 언어별 동작(회화 전용/레벨테스트/힌트)은 spec.has_curriculum·leveltest 가
    결정 — 하류 분기는 코드가 아니라 이 레지스트리 한 행을 본다.
    """
    spec = resolve_language(override) if override else None
    if spec is None:
        if override:
            logger.warning(
                "normalcall: 미지원 target_language(%s) → 기본(%s) 폴백",
                override, settings.DEFAULT_TARGET_LANGUAGE,
            )
        spec = resolve_language(settings.DEFAULT_TARGET_LANGUAGE) or SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE]
    return spec


def _call_target_language(
    member_target_language: Optional[str], assignment_id: Optional[int]
) -> Optional[str]:
    """이 통화가 진행될 학습 대상 언어(코드). 과제 통화만 예외다.

    평소 통화는 `member.target_language` 가 **단일 소스**다(위 `run_call` 주석).
    ⭐ **과제 통화는 반 커리큘럼 언어로 건다**(2026-09-04 사용자 결정).

    학습자가 앱을 일본어 학습으로 두고 있어도 교사가 낸 것은 한국어 과제다.
    예전에는 통화가 학습자 언어로 걸렸고, B2B 는 언어가 다르면 목표를 빈 배열로
    돌려주므로(설계대로) **주입이 조용히 0건**이 됐다 — 통화는 평범한 일본어 대화가
    되고 교사 화면에는 영원히 「미수행」으로 남았다(2026-09-04 실측 call_id=1290:
    첫마디가 "오늘 일본어 공부할래…" 였고 목표 10개 중 0건이 실렸다).

    ⛔ 평소 통화는 건드리지 않는다 — `assignment_id` 가 실린 통화에만 걸린다.
    ⚠ 그 언어의 레벨이 없는 학습자는 레벨테스트로 라우팅된다(기존 규칙 그대로).
    """
    if assignment_id is None:
        return member_target_language
    if member_target_language != b2b_client.CURRICULUM_LANGUAGE:
        logger.info(
            "normalcall: 과제 통화(assignment_id=%s) — 언어를 %s 로 고정"
            " (member.target_language=%s)",
            assignment_id, b2b_client.CURRICULUM_LANGUAGE, member_target_language,
        )
    return b2b_client.CURRICULUM_LANGUAGE


def _input_language_codes(target_code: str, locale: str) -> list[str]:
    """입력 전사에 **들릴 언어**를 알려 줄 BCP-47 목록(학습 언어 + 모국어). 없으면 빈 목록.

    ⭐ 왜(2026-08-20): 힌트 없이 자동 감지에 맡겼더니 짧은 발화가 통째로 다른 언어로 찍혔다
      — 실측 call_id=1097(ko 학습/en 모국어): "피우다"→`フィウダ` · "다"→`套` ·
      "아주"→`और च` · 짧은 응답→`für 10`·`kumite`·`Sí.`.
      이 전사는 CallRawData 로 **저장**돼 이어하기 요약·통화후 문장 추출·증거 인용 검증이
      읽는다 ⇒ 화면이 아니라 **데이터** 문제다.

    ⛔ 매핑을 새로 만들지 않고 `core.stt.normalize_language_codes` 를 그대로 쓴다. 그쪽은
      "검증한 코드만 매핑하고 모르는 건 버린다"는 규율을 이미 담고 있다(en-US·ko-KR 만
      확인됨). 표를 하나 더 만들면 같은 질문에 답이 둘이 된다.
      ⚠ 이름이 STT 라 "캐스케이드 물건"으로 보이지만 아니다 — `core/stt.py` 는 발음
      챌린지(`/pron/stt/ws`)도 쓴다. 캐스케이드를 지울 때 **같이 지우면 라이브가 깨진다**
      (그쪽 함수에 경고를 박아 뒀다 · docs/20260813_0040_캐스케이드-데모잔재-정리목록.md §2-b).

    ⛔⛔ **하나라도 매핑이 안 되면 통째로 포기한다**(빈 목록 = 자동 감지 = 종전 동작).
      부분 힌트가 무힌트보다 나쁘기 때문이다 — 일본어 학습자(ja 미매핑)에게 모국어
      `en-US` 하나만 얹으면 일본어 발화를 영어로 알아들으라고 시키는 꼴이 된다.
      ⇒ 지금 실제로 힌트가 붙는 조합은 ko·en 이고, 나머지 언어는 **오늘과 완전히 같다.**
    """
    wanted = [target_code] + ([locale] if locale and locale != target_code else [])
    mapped = normalize_language_codes(wanted)
    if len(mapped) != len(wanted):
        logger.info(
            "normalcall: 입력 전사 언어 힌트 생략(target=%s locale=%s → %s) — 부분 힌트는 "
            "무힌트보다 나쁘다", target_code, locale, mapped,
        )
        return []
    return mapped


# 데모/dev 통화 길이 override 범위(분). 사장님 요청: 레벨 데모에서 3~15분 선택.
DEMO_DURATION_MIN_MINUTES = 3
DEMO_DURATION_MAX_MINUTES = 15


def _resolve_call_duration(
    settings: Settings, duration_min: Optional[int], base: Optional[float] = None
) -> float:
    """통화 길이(초) 결정. 데모/dev 에서만 클라가 3~15분 override 가능. prod 는 무시(기본값).

    duration_min 없음 → base(콜타입 기본값). prod 에서 override 오면 무시+warning
    (실서비스는 통화 길이를 클라가 못 정한다 — 오남용/버그 방지). non-prod 는 3~15분 클램프.

    base: 콜타입별 기본 통화 길이. 호출부가 정한다 — 일반 통화는 플랜별 길이(또는 env
    강제값), 레벨테스트는 LEVELTEST_MAX_S(3분 캡). None(미지정)이면 env 강제값을
    **런타임에** 읽고(테스트 monkeypatch 반영 — 리터럴 기본값으로 박으면 def-time 에
    고정돼 monkeypatch 가 안 먹는다), 그것도 없으면 Free 길이로 떨어진다.
    """
    if base is None:
        base = (
            CALL_DURATION_S
            if CALL_DURATION_S is not None
            else call_service.FREE_CALL_DURATION_S
        )
    if duration_min is None:
        return base
    if settings.ENV == "prod":
        logger.warning("normalcall: prod 에서 duration_min 오버라이드 무시(%s분)", duration_min)
        return base
    clamped = max(DEMO_DURATION_MIN_MINUTES, min(DEMO_DURATION_MAX_MINUTES, int(duration_min)))
    return float(clamped * 60)


class HintOut(BaseModel):
    """동적 힌트 사이드카(D16) 구조화 출력 — 비버 질문에 대한 예시 답변 3개."""

    examples: list[HintExample]


class RegroundOut(BaseModel):
    """재접지 사이드카 구조화 출력 — **문장이 아니라 슬롯만** 받는다.

    ⛔ 여기에 문장 필드를 추가하지 마라. 사이드카가 문장을 만들면 그 문장이 곧 프롬프트가
      되고, "LLM 생성 0, 순수 조립"(persona_prompt 규율)이 재접지 경로에서만 무너진다.
      서버가 준 목록에서 **번호**를 고르고, 화제는 짧은 명사구 한 조각만 준다.
    """

    mode: str = ""            # "study" | "chat" | "" (제안일 뿐 — 채택은 서버가 판정)
    mode_quote: str = ""      # 모드 전환 근거 인용. 전사에 **실재해야** 채택된다
    covered: list[int] = []   # 서버가 준 목록의 번호(1-base)
    topic: str = ""           # 지금 대화 흐름 한 조각(짧은 명사구)


class _CallState:
    """두 펌프가 공유하는 통화 상태(세그먼트 누적 + 시계 + 종료 플래그)."""

    __slots__ = (
        "turn_id", "call_start_ts", "should_close", "close_seed_sent", "close_reply_started",
        "seed_sent_ts",
        "playback_done_event", "segments", "persisted_count", "nationality_pcm",
        "close_requested",
        "cur_user_pcm", "cur_user_text", "cur_beaver_pcm", "cur_beaver_text", "next_turn_index",
        # 표정(set_face) — 호출 수 · 마커 seq · 마지막으로 **보낸** 값(중복 억제 기준)
        "face_calls", "face_seq", "face_last", "face_last_pcm",
        # ⭐ 이어하기 조각의 시작 턴 인덱스(0=첫 조각). 통화후 **검증 범위**의 기준이다.
        "resume_from_turn",
        # ⭐ 표정 v2 계측 — 이 턴에서 오디오·전사가 **각각 처음 온 시각**(monotonic).
        "face_first_audio_at", "face_first_tr_at", "greeting_held_text",
        # ⭐ 학습자 in_tr 이 마지막으로 온 시각 — 체감 지연 계측의 기준점.
        #   ⛔ last_activity_ts 로 대신하지 마라. 그건 **비버 turn_end 도** 갱신해서
        #     "학습자가 말을 끝낸 시각"이 아니다.
        "learner_last_tr_at", "learner_first_tr_at",
        # ⭐ 소리 없이 연달아 온 set_face 수 — 폭주 차단기의 기준(오디오가 흐르면 0)
        "face_streak",
        # ⭐ 학습자가 이 통화에서 **한 번이라도 말했나**. 첫 인사 턴 판정의 기준이다.
        #   ⛔ 인사 턴은 정의상 학습자 발화 **이전**이다 — 그래서 이 한 값이 정확히 가른다.
        "learner_spoke",
        # ⭐ 이 조각에서 **비버가 마친 턴 수**. (지금은 로그·진단용)
        #   ⛔ next_turn_index 로 대신하지 마라 — 그건 학습자 세그먼트도 같이 세고,
        #     무음이면 flush 자체가 생략돼(pcm·text 둘 다 비면 early-return) 값이 어긋난다.
        #     실제로 그 오차 때문에 회귀가 깨졌다(2026-08-20).
        "beaver_turns",
        # ⭐ 벙어리 인사를 다시 시드했는가(통화당 1회). 아래 재시드 자리 참조.
        "greeting_reseeded",
        "close_seed",
        "last_turn_id", "hint_ctx", "hint_task", "hint_tasks",
        "hinted_turn_ids", "hinted_next_turn_index",
        "last_activity_ts", "silence_stage", "call_duration_s",
        "idle_nudge1_s", "idle_nudge2_s", "idle_close_s", "nudge_seed_1",
        "tag_leak_seen", "resume_sent",
        "reground_reminder", "reground_pending", "reground_injected", "user_turn_open",
        "continue_reminder", "continue_injected",
        # 재접지 통합(단계 3) — 압축 신호 관측 + 사이드카 + 모드 sticky
        "reground_count", "last_reground_ts", "reground_arm_reason",
        "reground_ctx", "reground_items", "reground_tasks", "reground_persona",
        "covered_nums",
        # ── 표현학습 코스(2026-09-10) ──
        # expr_items: 이번 통화에서 다룰 표현 [{item_id, obj, des, ex, quiz_passed}].
        #   ⭐ **빈/참이 곧 «이 통화가 표현학습인가» 의 게이트**다 — 별도 플래그를 만들지
        #     마라(레벨 시스템 관통원칙 ② — 증거가 원본, 나머지는 파생).
        # expr_quiz_pass / expr_quiz_fail: 퀴즈 판정 결과(item_id 집합). fail 이 오답퀴즈의 재료다.
        # ⛔ **`expr_drilled` 를 만들지 않았다.** 그건 이미 `covered_nums` 다 —
        #   `_note_covered_items` 가 채우고 `_covered_labels` 가 라벨로 되짚는다.
        #   같은 사실을 두 곳에 두면 반드시 갈라진다.
        # expr_ctx: STT 폴백 판정기 {client, model, locale_label, target_language}. None = 비활성(R5).
        # expr_tasks: 폴백 태스크 강참조(GC 방지) — run_call finally 가 전량 취소.
        # expr_sidecar_calls: 이 통화의 폴백 LLM 호출 수(상한 방어).
        # ── T16 퀴즈 상태기계(서버 소유) ──
        # expr_quiz_seq: 세운 정규 큐 수 · expr_quiz_set: 이번 퀴즈 항목 번호(1-base) · expr_quizzed: 퀴즈에 오른 번호 전부
        # expr_quiz_cue_pending: 대기 중 큐 문구(None=없음) — 재접지 슬롯과 **별개** · expr_quiz_cue_armed_ts: 계측
        # expr_quiz_awaiting_open: 큐가 얹혔고 다음 비버 turn_start 에서 창을 연다 · expr_quiz_open/_open_seg: 창
        # expr_quiz_stray: 창 안에서 비버가 낸 quiz_set 밖 번호(닫힘 안전판) · expr_covered_by_beaver: 비버 발화로 covered 된 번호
        # expr_retry_cued: 오답 재출제 큐를 이미 세웠나(통화당 1회)
        "expr_items", "expr_tag_allow", "expr_quiz_pass", "expr_quiz_fail", "expr_ctx", "expr_tasks",
        "expr_sidecar_calls",
        "expr_quiz_seq", "expr_quiz_set", "expr_quizzed", "expr_quiz_cue_pending", "expr_quiz_cue_armed_ts",
        "expr_quiz_awaiting_open", "expr_quiz_open", "expr_quiz_open_seg", "expr_quiz_stray",
        "expr_covered_by_beaver", "expr_retry_cued", "expr_quiz_prev_num",
        "expr_quiz_covered_at_open", "expr_quiz_drill_num",
        "call_mode", "usage_prompt_peak", "usage_prompt_max", "usage_prompt_floor",
        "compression_seen",
        "band_observe", "band_client", "band_awaiting", "total_answers", "nonspeaker_streak",
        # ⭐ 이 통화가 레벨테스트인가 — 종료 소유권 판정에 쓴다(레벨테스트는 서버가 끝낸다)
        "is_leveltest",
        "last_beaver_question", "band_tasks", "band_target_language",
        # 🔬 턴 절단 진단(2026-08-31, 임시 계측). 동작을 바꾸지 않는다 — 로그만.
        "diag_turn_open_ts", "diag_last_audio_ts", "diag_audio_bytes", "diag_interrupts",
        # 입력 전사 언어 힌트(BCP-47 2개) — 세대를 건너 산다(세션마다 다시 넘긴다)
        "input_language_codes", "live_model", "live_vertex",
        "leveltest_transcript",
        # 세션 재연결(15분) — 세대를 건너 사는 값은 전부 여기 있어야 한다. 태스크 지역
        # 변수에 두면 세대가 바뀔 때 통째로 사라진다(시계·무음·flush 태스크가 재생성된다).
        # ⚠ `resume_handle`·`session_epoch` 는 남는다 — Gemini 가 주는 재개 핸들과 세대 번호는
        #   로그·관측에 쓰인다. ⛔ 스왑 상태(reconnects/swap_requested/last_swap_ts)는
        #   재연결 기계와 함께 사라졌다(2026-08-19).
        "resume_handle", "session_epoch", "reconnects",
        # ⭐ 이 조각의 선톡 시드 원문(벙어리 인사 재시드용).
        "seed_text",
        "usage_log", "usage_dropped", "sidecar_usage",
        # ⭐ 지시문 분할 주입(2026-08-23). setup 에는 짧은 코어만 싣고 나머지 페르소나를
        #   붙은 뒤 조각으로 밀어넣는다. 빈 리스트 = 주입 완료 또는 스위치 off.
        "diag_batches", "diag_events", "diag_dropped",
    )

    def __init__(self) -> None:
        # 종료 시드 텍스트(콜타입별 — normal 기본, 레벨테스트는 run_call 이 교체).
        # 주입 시점·파이프(_inject_close_seed)는 불변, 문자열만 바뀐다(R4).
        # ⚠ run_call 이 통화별 난수 태그로 다시 세팅한다 — 여기 기본값은 테스트/폴백용.
        self.close_seed: str = _close_seed(new_close_tag())
        # 자기낭독 안전망: 이번 비버 턴에 제어 태그가 섞였는지 / 재개 시드를 몇 번 넣었는지.
        self.tag_leak_seen = False
        self.resume_sent = 0
        # 세션 재개 핸들. 서버가 주기적으로 밀어주고, 우리는 최신 것만 들고 있다가
        # 재연결에 쓴다. ⚠ resumable=False 로 온 갱신은 **덮어쓰지 않는다** — 그 시점
        # 상태(모델 생성 중·tool 실행 중)로 재개하면 데이터가 유실된다.
        self.resume_handle: Optional[str] = None
        self.session_epoch = 0        # 이 통화에서 몇 번째 연결인가(1부터)
        self.reconnects = 0           # T22 — Gemini 쪽 끊김 뒤 같은 통화에 다시 붙인 횟수(usage_json "reconnects")
        self.seed_text = ""
        self.turn_id: Optional[str] = None
        # 🔬 턴 절단 진단(임시). diag_turn_open_ts: 이번 비버 턴이 열린 loop.time().
        #   diag_last_audio_ts: 마지막 오디오 조각 도착 시각(조각 간 공백 계측).
        #   diag_audio_bytes: 이번 턴 누적 오디오 바이트. diag_interrupts: 인터럽트 누적.
        self.diag_turn_open_ts: Optional[float] = None
        self.diag_last_audio_ts: Optional[float] = None
        self.diag_audio_bytes: int = 0
        self.diag_interrupts: int = 0
        self.call_start_ts: Optional[float] = None
        self.should_close = False
        self.close_seed_sent = False
        # 종료 시드 후 비버가 '실제로 작별 턴을 시작'했는지. 빈 turn_end(이전 활동 잔여)로
        # 작별 전에 조기 종료되는 버그 방지 — 이 플래그가 서야만 turn_end 로 종료한다.
        self.close_reply_started = False
        self.seed_sent_ts: Optional[float] = None
        self.playback_done_event = asyncio.Event()
        self.segments: list[dict] = []
        self.persisted_count = 0  # 이미 DB 에 저장한 세그먼트 수(점진 flush 커서)
        # 저장이 끝난 세그먼트에서 회수해 둔 user 원음(국적 추론 전용, NATIONALITY_PCM_MAX_S 상한).
        # ⚠ 이게 있어야 세그먼트 PCM 을 flush 직후 놓아줄 수 있다 — 상세는 _release_persisted_pcm.
        self.nationality_pcm = bytearray()
        # 종료 요청 신호(TaskGroup **밖**의 사이드카 → 시계워처). 사이드카가 죽은 세션을 잡고
        # 직접 종료 시드를 주입하던 경로를 이 이벤트로 대체했다(_band_observe_sidecar 참조).
        self.close_requested = asyncio.Event()
        self.cur_user_pcm = bytearray()
        self.cur_user_text: list[str] = []
        self.cur_beaver_pcm = bytearray()
        self.cur_beaver_text: list[str] = []
        self.next_turn_index = 0
        self.beaver_turns = 0
        self.greeting_reseeded = False
        self.learner_spoke = False
        self.face_calls = 0        # 표정: set_face 호출 수(꺼져 있으면 영원히 0)
        self.face_seq = 0          # 마커 seq(통화 스코프 — 턴마다 리셋하지 않는다)
        self.face_last = ""        # 마지막으로 **보낸** 감정. 같으면 안 보낸다
        # ⭐ 마지막으로 마커를 **보낸** 시점의 턴 오디오 바이트. 같은 자리 겹침을 가른다.
        #   -1 = 아직 이 통화에서 보낸 적 없음.
        self.face_last_pcm = -1
        self.face_streak = 0       # 오디오 없이 연달아 온 호출 수(차단기)
        self.resume_from_turn = 0  # 이어하기 조각의 시작 턴(0=첫 조각 — 전체가 이 조각이다)
        # ⭐ 첫 인사 턴에서 **소리가 오기 전에 도착한 자막 조각**을 붙잡아 둔다.
        #   소리가 오면 즉시 흘리고, 벙어리로 끝나면 그대로 버린다(유령 자막 방지).
        #   근거는 `_greeting_subtitle_on_hold` 주석.
        self.greeting_held_text: list[str] = []
        self.face_first_audio_at = 0.0   # 표정 v2 계측(턴마다 리셋)
        self.face_first_tr_at = 0.0
        self.diag_batches = 0                # 클라 계측 배치 수(상한 방어·요약)
        self.diag_events = 0                 # 받은 이벤트 총수
        self.diag_dropped = 0                # 클라가 상한으로 버린 총수
        self.learner_last_tr_at: Optional[float] = None
        self.learner_first_tr_at: Optional[float] = None
        # ── P2.5(D16) 동적 힌트 사이드카 ──
        # last_turn_id: 방금 끝난 비버 턴 id(턴 종료 시 turn_id 가 None 으로 리셋되므로 별도 보존).
        self.last_turn_id: Optional[str] = None
        # hint_ctx: run_call 이 조립한 {client, model, instruction}. None = 힌트 비활성.
        self.hint_ctx: Optional[dict] = None
        self.hint_task: Optional[asyncio.Task] = None  # 세션당 동시 1개(새 질문 → 이전 취소)
        self.hint_tasks: set[asyncio.Task] = set()     # 강참조(GC 방지) — 종료 시 전량 취소
        # hinted_turn_ids: hint_used 로 열람된 turn_id(중복 열람 dedup + 로그).
        self.hinted_turn_ids: set[str] = set()
        # hinted_next_turn_index: 열람 시점의 next_turn_index 마커 — turn_id 는
        # CallRawData 에 저장되지 않는 휘발 값이라 전사와 조인 불가. 대신 이 마커
        # 이상의 첫 USER turn_index 가 "열람 직후 발화" = 통화후 E1 강등 대상(D16).
        self.hinted_next_turn_index: set[int] = set()
        # ── A2 무음 3단 넛지 ──
        # last_activity_ts: 마지막으로 '무언가 말한' loop.time() — 학습자 in_tr **또는 비버
        #   turn_end(발화 종료) 또는 넛지 주입. 무음 = 이 시각 이후 경과. 비버 발화 시간을
        #   무음으로 세지 않게(=넛지가 비버 발화 직후 터지지 않게) 하는 핵심. None = 아직 없음.
        # silence_stage: 0=무넛지, 1=1단 주입됨, 2=2단 주입됨(3단은 직접 종료 시드 주입).
        self.last_activity_ts: Optional[float] = None
        self.silence_stage: int = 0
        # 통화 길이(초). run_call 이 콜타입·플랜을 보고 덮어쓴다(데모/dev 는 거기서
        # start.duration_min 으로 3~15분 override). _watch_call_clock 이 모듈 상수 대신
        # 이 값을 본다. 여기 기본값은 run_call 을 안 거치는 펌프 단위 테스트용 안전값이라
        # 플랜을 모르는 상태의 보수적 선택(Free 길이 또는 env 강제값)으로 둔다.
        self.call_duration_s: float = _resolve_call_duration(_settings, None)
        # 무음 3단 캐던스 + 1단 넛지 시드(콜타입별 — run_call 이 꽂는다). 기본은 일반 통화 값.
        # _watch_idle 은 모듈 상수 대신 이 필드를 본다(레벨테스트만 짧은 캐던스로 override).
        self.idle_nudge1_s: float = IDLE_NUDGE1_S
        self.idle_nudge2_s: float = IDLE_NUDGE2_S
        self.idle_close_s: float = IDLE_CLOSE_S
        self.nudge_seed_1: str = _NUDGE_SEED_1
        # 단발 재접지 리마인더(일반 통화만, run_call 에서 조립). None = 비활성.
        self.reground_reminder: Optional[str] = None
        # 후반 재접지 문구(대화 지속). None = 비활성(레벨테스트 등).
        self.continue_reminder: Optional[str] = None
        self.continue_injected: bool = False
        # 재접지 상태기계(on_user_turn):
        #   reground_pending: arm 됨(fire_at 도달) — 다음 유저 발화 시작 시 얹는다.
        #   reground_injected: 이미 얹음(단일 소유권 가드, 통화당 1회).
        #   user_turn_open: 지금 유저 발화 턴이 열려 있나(첫 in_tr True → 비버 응답 시작 시 False).
        self.reground_pending: bool = False
        self.reground_injected: bool = False
        self.user_turn_open: bool = False
        # ── 재접지 통합(단계 3) ──
        # reground_count: 이번 통화에 실제로 얹은 횟수(상한 REGROUND_MAX_PER_CALL).
        #   ⚠ reground_injected(1회성 불리언)를 이걸로 대체한다 — 옛 게이트는 중반 재접지가
        #     얹히면 True 로 굳어 **후반 리마인더가 영원히 안 얹혔다**(실측 결함).
        # last_reground_ts: 마지막 주입 시각(최소 간격·시간 폴백 기준).
        # reground_arm_reason: 이번 arm 의 근거("compress"/"post-compress"/"time") — 로그·테스트용.
        # reground_ctx: 재접지 사이드카 {client, model, instruction}. None = 사이드카 비활성.
        # reground_items: 사이드카에 번호로 떠먹일 학습 항목 라벨(서버가 소유하는 목록).
        # covered_nums: **서버가 누적 소유하는** "이미 다룬 항목" 번호(1-base, append-only).
        #   ⛔ 사이드카에 맡기지 않는다 — 2026-09-09 통화 1360 참조(아래 _note_covered_items).
        # reground_persona: (role, personality) — 문구 조립 재료.
        # call_mode: 공부/대화 모드. **서버가 sticky 로 소유**하고, 사이드카 제안은 전사에
        #   실재하는 인용이 증명될 때만 받아들인다(AI 는 증인, 코드가 심판).
        self.reground_count: int = 0
        self.last_reground_ts: Optional[float] = None
        self.reground_arm_reason: str = ""
        self.reground_ctx: Optional[dict] = None
        self.reground_items: list[str] = []
        self.covered_nums: list[int] = []
        self.reground_tasks: set[asyncio.Task] = set()
        self.reground_persona: tuple[str, str] = ("", "")
        # ── 표현학습 진도(서버 소유) ──
        # ⭐⭐ **압축이 이 설계의 최대 적이다.** 컨텍스트 압축은 오래된 대화부터 지우므로
        #   통화 초반에 가르친 것이 제일 먼저 사라진다(실측 통화 1360: 압축 #4 가 9,219
        #   토큰을 지우자 비버가 1번 항목으로 되감았고 마지막 91초가 반복이었다).
        #   ⇒ 진도를 **LLM 의 기억에 맡기지 않는다.** 이 세 값은 파이썬 메모리 + DB 라
        #     압축과 완전히 무관하다.
        self.expr_items: list[dict] = []
        self.expr_tag_allow: frozenset[str] = frozenset()   # 항목 표면형의 [대괄호] 조각 — 누출 필터 허용 목록
        self.expr_quiz_pass: set[int] = set()
        self.expr_quiz_fail: set[int] = set()
        self.expr_ctx: Optional[dict] = None
        self.expr_tasks: set[asyncio.Task] = set()
        self.expr_sidecar_calls: int = 0
        self.expr_quiz_seq: int = 0
        self.expr_quiz_set: list[int] = []
        self.expr_quizzed: set[int] = set()
        self.expr_quiz_cue_pending: Optional[str] = None
        self.expr_quiz_cue_armed_ts: Optional[float] = None
        self.expr_quiz_awaiting_open: bool = False
        self.expr_quiz_open: bool = False
        self.expr_quiz_open_seg: int = 0
        self.expr_quiz_stray: list[int] = []
        self.expr_covered_by_beaver: set[int] = set()
        self.expr_retry_cued: bool = False
        self.expr_quiz_prev_num: Optional[int] = None   # 큐 직전 마지막 covered 번호 — 여는 B 의 공개 예외(T17-4)
        self.expr_quiz_covered_at_open: set[int] = set()   # 열 때 covered 였던 번호 — 재언급은 닫힘이 아니다(T20)
        self.expr_quiz_drill_num: Optional[int] = None     # 열 때 아직 안 다룬 가장 앞 번호 = 드릴 중이던 항목(T20)
        self.call_mode: str = "chat"
        # 압축 관측: prompt_token_count 의 최고치와 급감(=압축) 횟수.
        # ⚠ peak 와 max 는 **다른 값이다.**
        #   usage_prompt_peak — 압축 사이클 peak. 압축될 때마다 바닥에서 다시 센다.
        #     낙차를 재는 기준선이자 재접지 arm 게이트의 입력이라 반드시 리셋돼야 한다.
        #   usage_prompt_max  — 통화 전체 최대치. **절대 리셋 없음**(단조증가).
        #     DB(call.usage_peak_prompt)로 가는 건 이쪽이다. 압축 트리거 하향 실험이
        #     "이 통화가 실제로 몇 토큰까지 갔나"를 물으므로. 예전엔 사이클 peak 가
        #     저장돼 call 909 에서 13,355(DB) vs 15,904(실제)로 어긋났다.
        self.usage_prompt_peak: int = 0
        self.usage_prompt_max: int = 0
        #   usage_prompt_floor — 이 통화의 **바닥**(대화 0줄일 때의 prompt). 첫 usage 1건으로
        #     고정한다. 시스템 지시문 + teaching_plan + 도구 정의라 통화 내내 매 턴 다시 실린다.
        #     ⭐ 재접지 arm 이 "컨텍스트가 찼나"를 물을 때 재야 하는 건 **바닥 위에 쌓인 대화**지
        #     바닥 자체가 아니다(2026-09-06). 0 이면 아직 첫 usage 전 = 게이트 비활성.
        self.usage_prompt_floor: int = 0
        self.compression_seen: int = 0
        # ── 레벨테스트 Phase 2: 종료 판정 전용 사이드카('끝낼까 말까'만 — 밴드 정밀분류 없음) ──
        # band_observe: 관측 활성(레벨테스트만 run_call 이 True). False → 전 경로 무동작(일반 통화 무영향).
        # band_client: judge_leveltest_turn 에 넘길 genai.Client(사이드카가 참조).
        # band_awaiting: 사이드카 in-flight 가드(동시 1건만 — 다음 답변은 완료 후 판정).
        # total_answers: 관측된 전체 답변 시도 수(하드 턴캡 재료 + 판정관 조기종료 게이트).
        # nonspeaker_streak: 대상 언어 산출 실패(answer_in_target=False) 연속 수 —
        #   NONSPEAKER_MAX 도달 시 비화자 결정론 컷(한국어 못 하는 사람이 오래 붙잡히는 역설 방지).
        # last_beaver_question: 직전 flush 된 비버 발화 스냅샷(사이드카의 prior_question 문맥).
        # band_tasks: 사이드카 강참조(GC 방지) — run_call finally 가 전량 취소.
        self.band_observe: bool = False
        # ⛔ 레벨테스트는 **길이 시계로 끝나야 한다**(3분 하드캡은 측정 설계다). 조각·프론트
        #   종료 소유권과 무관하다 — 이 값이 그 두 세계를 가른다.
        self.is_leveltest: bool = False
        self.band_client = None
        self.band_awaiting: bool = False
        # (멀티랭귀지) 종료 판정관이 판정할 대상 언어 라벨(run_call 이 세팅, 기본 한국어).
        self.band_target_language: str = _DEFAULT_TARGET_LABEL
        # 입력 전사에 들릴 언어(학습 언어 + 모국어). 빈 목록 = 힌트 없음(자동 감지).
        self.input_language_codes: list[str] = []
        # ⭐ 이 통화에 쓸 Live 모델(플랜에서 갈린다). None 이면 어댑터 기본값.
        #   Max=영상(3.1·표정) / Free·Pro=음성(2.5·표정없음) — call_service.live_model_for
        self.live_model: str | None = None
        # ⭐ 이 통화의 백엔드(2026-09-08). True=Vertex / False=AI Studio / None=전역 설정.
        #   ⛔ `live_model` 과 **한 묶음**이다 — 따로 정해지면 어긋난 조합이 되고 1008 이다.
        self.live_vertex: bool | None = None
        self.total_answers: int = 0  # 관측된 전체 답변 시도(하드 턴캡 + 조기종료 게이트)
        self.nonspeaker_streak: int = 0  # answer_in_target=False 연속 수(비화자 결정론 컷)
        self.last_beaver_question: str = ""
        self.band_tasks: set[asyncio.Task] = set()
        # 종료 판정 사이드카(C)용 전체 전사 누적 — "Q: … / A: …" 턴별. 종료 판정관이 맥락으로 읽는다.
        self.leveltest_transcript: list[str] = []
        # ── 원가 계기판(Phase 0) ──
        # usage_log: Gemini Live 가 메시지마다 실어 보내는 과금 계측(usage_metadata)의 시계열.
        #   지금껏 이 값을 읽지 않아 통화 원가가 추정치뿐이었다. 통화 종료 시 1줄로 방출하고
        #   버린다(DB 저장 없음 — 관측 단계). usage_dropped: 상한 초과로 버린 개수.
        self.usage_log: list[dict] = []
        self.usage_dropped: int = 0
        # sidecar_usage: 통화중 LLM 사이드카(동적 힌트·재접지 브리프·레벨테스트 턴 판정)의
        #   토큰. ⛔ Live usage 와 **다른 그릇**이다 — 단가가 다르고, 섞으면 두 엔진
        #   비교가 오염된다. usage_json.sidecars 로 따로 나간다.
        self.sidecar_usage = gemini_analysis.LlmUsage()


def _as_int(v) -> int | None:
    """와이어에서 온 값을 int 로. 못 읽으면 None(호출부가 폴백한다).

    ⚠ 프론트가 call_id 를 **문자열로** 보낸다("1234"). 여기서 안 받아 주면 이어하기가
      조용히 안 되고, 원인은 로그에 안 남는다.
    """
    try:
        return int(str(v).strip()) if v is not None and str(v).strip() else None
    except (TypeError, ValueError):
        return None


class _ClientDisconnect(Exception):
    """클라 WS 종료 내부 신호."""


class _CallFinished(Exception):
    """통화 정상 종료(작별 후/백스톱) 내부 신호."""



def _release_persisted_pcm(state: _CallState, upto: int) -> int:
    """저장이 끝난 세그먼트 [0, upto) 의 PCM 바이트를 놓아준다. 해제한 바이트 수를 반환.

    🧒 통화 오디오는 무겁다(비버 출력 24kHz·학습자 입력 16kHz, 둘 다 16bit). 15분 통화면
      다 합쳐 수십 MB 다. 예전엔 DB 저장이 끝난 뒤에도 이 바이트가 state.segments 안에
      그대로 남아 통화가 끝날 때까지 메모리를 잡고 있었다(저장 커서 persisted_count 만
      올렸다). 저장이 끝난 오디오는 이미 스토리지에 있으니 서버가 계속 들고 있을 이유가 없다.

    ⚠ 딱 하나 예외: 통화후 **국적 추론**은 학습자(user) 원음을 다시 읽는다. 그래서 놓아주기
      전에 user PCM 만 nationality_pcm 으로 옮겨 담는다 — 단 NATIONALITY_PCM_MAX_S 상한까지만.
      추론 게이트가 10초라 60초면 표본은 충분하고, 보관량이 통화 길이와 무관하게 고정된다.

    ⚠ 오디오 업로드 경로와 충돌하지 않는다: 점진 flush(upload_audio=True)는 저장 시점에
      업로드까지 끝내고, 최종 persist(upload_audio=False)는 save_segments 가 pending 목록에
      **바이트 사본**을 떠 가므로 후행 업로드가 여기 원본에 의존하지 않는다.

    호출 규약: 반드시 저장이 성공한 뒤에만, 저장된 구간까지만 부른다(upto <= persisted_count).
    통화 종료 뒤 마지막 회수만 예외로 전체 구간을 부른다(그 시점엔 아무도 원본을 안 읽는다).
    """
    limit = int(INPUT_SAMPLE_RATE * SAMPLE_WIDTH_BYTES * NATIONALITY_PCM_MAX_S)
    freed = 0
    for seg in state.segments[:upto]:
        pcm = seg.get("pcm")
        if not pcm:
            continue
        if seg["role"] == "user" and len(state.nationality_pcm) < limit:
            state.nationality_pcm.extend(pcm[: limit - len(state.nationality_pcm)])
        freed += len(pcm)
        seg["pcm"] = b""
    return freed


def _on_learner_transcript_piece(state: _CallState, text: str) -> None:
    """학습자 전사 조각(in_tr) 하나를 받는다 — 버퍼에 쌓고, **표현학습이면 조각 단위로 covered 를 잰다.**

    ## T17-6 (통화 1405 ③ 괜찮아요) — 왜 flush 시점만으로는 안 되나
    학습자가 「괜찮아요.」 라고 정확히 말했는데(서버 in_tr 그대로) DB 에 drilled 가 안 찍혔다. 시간표:
        t78 B 영어 질문 → t79 U 「괜찮아요.」 → t80 B **빈 턴** → t81 U 「오케이.」 → t82 B
    in_tr 은 비버 답과 같이 오는 늦은 보고서다(재접지 주석 «6/82 잘림» 과 같은 성질). t79 의 전사가 t80 의
    turn_start(=flush) **뒤에** 도착하면 t81 의 전사와 한 버퍼에 남고, flush 는 `"".join` 이라
    「괜찮아요.오케이.」 한 덩어리가 된다 → `mentions` 는 낱말 경계를 요구하므로(꼬리 «오케이» 는 조사가 아니다)
    거짓 → 영영 covered 가 안 된다. 다른 항목이 다 drilled 인데 이것만 빠진 이유가 이것이다(로그·코드로 추적).
    ⇒ 조각이 **도착하는 순간** 그 조각만으로 대조한다. 저장 텍스트(`"".join`)는 건드리지 않는다 — 일반 통화의
      저장본이 바뀐다. flush 시점 대조는 그대로 두되 `idx in covered_nums` 로 멱등이다.
    ⛔ 일반 통화는 한 바이트도 안 바뀐다 — `_note_covered_items(source="user")` 가 expr_items 게이트로 즉시 되돌아간다.
    """
    state.cur_user_text.append(text)
    logger.info("normalcall 👤 user: %s", text)
    if state.expr_items:
        _note_covered_items(state, text, source="user")


def _flush_user_segment(state: _CallState) -> None:
    if not state.cur_user_pcm and not state.cur_user_text:
        return
    text = "".join(state.cur_user_text).strip()
    logger.info("👤 USER[t%d]: %s", state.next_turn_index, text or "(무음/전사없음)")
    # ⭐ 표현학습에서는 **학습자가 말했을 때** 그 항목을 다룬 것이 된다 — 비버는 모국어로
    #   묻고 정답을 말하지 않기 때문이다. `normal` 통화에서는 이 호출이 즉시 되돌아간다
    #   (게이트는 `state.expr_items` — 함수 독스트링 참조).
    _note_covered_items(state, text, source="user")
    state.segments.append(
        {"turn_index": state.next_turn_index, "role": "user", "text": text, "pcm": bytes(state.cur_user_pcm)}
    )
    state.next_turn_index += 1
    state.cur_user_pcm = bytearray()
    state.cur_user_text = []


def _reading_speed_line(text: str, audio_bytes: int) -> str:
    """비버 발화 1건의 **읽기 속도** — 캐스케이드의 `읽기=…` 와 같은 잣대로 낸다.

    ⛔ 바이트→초는 `core.audio.output_audio_s` **하나**를 쓴다. 두 경로가 각자 계산하면
      비교 자체가 무의미해진다(그게 이 계측의 유일한 목적이다).
    ⚠ 분자는 `len(text)` — 캐스케이드도 같은 규칙(문장 전체 길이)이라 그대로 맞춘다.
      공백·문장부호가 섞이지만 **두 쪽이 같은 방식으로 섞이므로** 비교는 성립한다.

    언어 구분: Live 는 마커를 안 쓰므로 **문자 종류**로 가른다
    (`core.languages` 의 스크립트 표 — 레벨테스트가 쓰는 것과 같은 잣대다).
    한 발화가 한 언어에 90% 이상 쏠렸을 때만 그 언어로 이름표를 붙이고, 섞였으면 `mixed`
    로 낸다 — 섞인 발화는 **어느 언어가 몇 초를 썼는지 알 수 없기 때문**이다(오디오는
    통짜 하나다). ⛔ 모르면 아는 척하지 않는다. 전체 값만으로도 판단은 된다.
    """
    audio_s = audio.output_audio_s(audio_bytes)
    chars = len(text or "")
    if audio_s <= 0 or chars <= 0:
        return "읽기=측정불가(소리 %.1f초 글자 %d)" % (audio_s, chars)
    ko = count_target_script_chars(text, "ko")
    latin = count_target_script_chars(text, "en")
    script_total = ko + latin
    label = "mixed"
    if script_total:
        if ko >= script_total * 0.9:
            label = "ko"
        elif latin >= script_total * 0.9:
            label = "en"
    return "읽기=%.1f자per초 [%s:%d자/%.1f초] (한글 %d 라틴 %d)" % (
        chars / audio_s, label, chars, audio_s, ko, latin,
    )


def _note_covered_items(
    state: _CallState, text: str, *, source: str = "beaver",
) -> None:
    """방금 끝난 발화에서 학습 항목 라벨을 찾아 `covered_nums` 에 누적한다.

    🧒 왜 서버가 직접 세나: "비버가 이 항목을 다뤘나"는 LLM 판단이 필요 없다. 전사는
      서버에 다 있고 항목 라벨도 서버가 소유한다(`state.reground_items`) — **문자열 대조로
      끝난다.** AI 는 증인이고 판정은 코드가 한다(레벨 시스템 관통원칙 ①③).

    ## 2026-09-09 통화 1360 — 이걸 사이드카에 맡겨서 난 사고
    재접지 쪽지의 "이미 다룬 것" 을 사이드카가 골랐다. 사이드카는 **최근 12턴 창**
    (`_transcript_tail`)만 보고, 실린 개수 상한도 4개였다(`REGROUND_COVERED_CAP`).
    ⇒ 7개를 드릴한 시점에 `covered=3/3` 이 나갔다(04:55:32).
    ⇒ 17초 뒤 압축이 초반을 9,219토큰 지우자(#4 16372→7153), 비버에게 남은 **유일한
      "무엇을 했나" 증거가 그 부분 목록**이 됐다. 목록에 없던 불·동안·피우다는 **안 한 것**이
      되고, 58초 뒤 비버가 *"Now let's move on to something else"* 라면서 **1번 항목 "불"로
      되감았다**(t39). 마지막 91초(30%)가 이미 한 것이었고 비버는 그걸 몰랐다.
    ⇒ 창이 좁아서 난 일이므로 창을 넓히는 게 아니라 **출처를 통화 전체를 아는 쪽(서버)으로
      옮긴다.** 압축과 무관해지고, 사이드카를 기다릴 필요도 없어진다(첫 arm 부터 실린다).

    ⛔ **학습자 발화는 보지 않는다.** "다뤘다"는 가르친 쪽의 사실이다. 학습자가 우연히 낸
      단어를 "가르쳤다"로 치면 **아직 안 가르친 항목을 잃는다**. 1360 실측으로 비버 발화만
      봐도 충분하다 — 비버는 드릴할 때 항상 라벨을 그대로 인용했다(`Repeat after me: "불"`
      · `Next word: "동안"`) ⇒ 7항목 7개 다 잡힌다.
    ⛔ 어간·조사 정규화를 하지 않는다(라벨 전체가 들어있는지만 본다). 미검출은 "한 번 더
      가르친다"로 끝나지만, 오검출은 **가르칠 기회를 영영 없앤다.** 보수적인 쪽으로 간다.

    append-only 다 — 한 번 들어간 번호는 빠지지 않는다(증거가 원본, 나머지는 파생 계산).

    ## ⭐⭐ 2026-09-10 — 표현학습만 **학습자 발화까지** 본다 (사장님 결정)
    위의 «비버만 본다» 규율은 **일반 통화 전제**였다. 표현학습은 흐름이 반대다 — 비버가
    모국어로 묻고 **정답을 말하지 않는다**:

        비버   : "헤어질 때 뭐라고 하지?"    ← 라벨이 안 나온다
        학습자 : "안녕히 가세요"              ← 여기서만 나온다

    비버만 보면 그 항목이 영원히 «안 다룬 것» 이 되고, 다음 통화에 또 나온다.

    ⛔ **`normal` 통화의 동작은 한 바이트도 안 바뀐다.** 게이트는 `state.expr_items` 하나다 —
      그게 비어 있으면 학습자 발화는 통째로 무시된다. 일반 통화에서 학습자 발화를 세면 위
      경고(«우연히 낸 단어를 가르쳤다로 친다»)가 그대로 되살아난다.
    ⚠ 표현학습에는 그 위험이 없다: 여기서 학습자가 라벨을 말했다는 것은 **그 항목을 실제로
      산출했다**는 뜻이고, 그게 정확히 우리가 세려는 사건이다. 자유대화와 전제가 다르다.
    """
    if source == "user" and not state.expr_items:
        return  # ⛔ 일반 통화는 비버만 본다(9638a26 규율 그대로)
    if not text or not state.reground_items:
        return
    # ⭐ 표현학습만 **낱말 경계 대조**를 쓴다(`quiz_judge.mentions`).
    #   ⛔ 생짜 `label in text` 는 항목 「물」에 "어제 **선물**을 받았어요" 를 잡는다 ⇒ 그
    #     항목이 완료로 처리돼 **가르치지도 않고 건너뛴다.** L2 이상은 90%가 어휘이고
    #     대부분 1~2글자라 여기가 주 무대다(근거·규율은 `mentions` 독스트링).
    #   ⛔ `normal` 은 **생짜 비교 그대로** 다 — 여기서 대조 규칙을 바꾸면 일반 통화의
    #     covered 검출 폭이 달라져 재접지 쪽지가 바뀐다(그 경로는 손대지 않는다).
    expr = bool(state.expr_items)
    for idx, label in enumerate(state.reground_items, start=1):
        label = (label or "").strip()
        if not label or idx in state.covered_nums:
            continue
        hit = quiz_judge.mentions(text, label) if expr else (label in text)
        if hit:
            state.covered_nums.append(idx)
            if expr:
                _expression_quiz_tick(state, idx, source=source, text=text)   # T16 — 서버 퀴즈 상태기계(닫힘 ① · 큐 arm)


# --------------------------------------------------------------------------- #
# 표현학습 퀴즈 — **서버가 열고, 서버가 찾고, LLM 은 STT 폴백만** (T16, 2026-09-11 사장님 승인)
# --------------------------------------------------------------------------- #
# ## 왜 (docs/20260911_2000_표현학습-T16-서버주도-퀴즈-상태기계.md §0)
# 판정기 지시문에 «드릴 정답은 passed 가 아니다» 가 **명시돼 있는데도** 4통화 일관해서 드릴 첫 시도 정답이
# passed 로 찍혔다(1404: 7/18 전부). 비버 대본에 «퀴즈는 3·6·9번 직후에만» 이 있는데도 4통화 중 3통화가
# 6·7·10번째에 냈다. 둘 다 **LLM 이 세거나 구간을 가르는 일**이고 세 번(T8·T14·T15) 고쳐도 남았다.
# ⇒ 세는 것과 구간을 여는 것을 **서버가** 한다. 사장님: «배웠는가 → 서버가 퀴즈를 연다 → 그때만 맞췄는가».
#
# ## 상태기계 (서버 소유)
#   [drill]      covered_nums 를 센다(문자열 대조 — 비버·학습자 발화). ⛔ 판정기 없음. passed/failed 는 존재할 수 없다.
#      │ len(covered) >= G*(seq+1) (G = EXPRESSION_QUIZ_GROUP)  또는  전부 covered 인데 아직 퀴즈에 안 오른 것이 있음(꼬리·P0-3)
#      ▼
#   [cue-armed]  expr_quiz_cue_pending = 큐 문구(CONTROL_TAG 접두). 재접지와 **슬롯을 공유하지 않는다** — 얹기 자리·RMS
#                2관문만 빌린다(`_maybe_attach_reground_on_mic`). reground_count/last_reground_ts 는 건드리지 않는다
#                (큐가 재접지 간격 150s 를 소모하면 쪽지가 0회가 돼 되감기 방어가 죽는다). 실패하면 pending 유지 → 다음 발화.
#      │ 얹힘 → expr_quiz_awaiting_open = True
#      ▼
#   [quiz]       **open_seg = 큐가 얹힌 뒤 다음 비버 turn_start(flush 직후)의 len(segments)** — 얹기 자리에서 잡으면
#                큐를 태운 학습자 발화(드릴 3번 실패 뒤 복창이 지배적 경로)가 창 첫 U 가 되고 공개 B 는 창 밖이라
#                앵무새가 통과한다(fable P1-1).
#                닫힘 ①: **비버 발화로** covered 된 새 번호가 «아직 안 다룬 가장 앞 번호»(순번상 다음 항목) — 항목 혼동
#                        (묻던 것과 다른 표면형 공개, J)은 멀리 앞선 번호라 걸러진다. 안전판: quiz_set 밖 새 번호(비버)가
#                        2개 이상 쌓이면 그것도 닫힘. ⛔ 학습자 발화로 covered 된 번호는 닫힘에 안 쓴다(codex P1-1 —
#                        오답으로 4번 표면형을 말하면 1~3 퀴즈가 조기 닫힌다).
#                닫힘 ②: 통화 종료(마지막 판정 경로).
#                닫힐 때 서버가 **직접** 찾는다(아래 §판정). 다음 큐 arm 은 닫힌 뒤에만.
#
# ## 판정 — passed/failed 는 서버가 창을 훑어 확정한다 (fable P1-5)
#   창(open_seg..close)과 quiz_set 을 서버가 여니 옛 문자열 판정이 실패한 이유(드릴/퀴즈 못 가름·앵무새)가 없다.
#   항목마다 창을 순서대로 훑어 **먼저 나오는 사건**으로 확정:
#       U 에 표면형(템플릿 인식 `quiz_judge.mentions`) + V4 격식(`keeps_formality`)  → passed
#       B 에 표면형                                                                 → failed(공개)
#       둘 다 없음                                                                  → 미판정
#   단조(passed→failed 되돌림 금지) 유지. 지연 0 · 비용 0.
#   LLM 은 **미판정 항목에만** 부른다 — STT 폴백(하네스 M: 「이거 주세요」→「이거 지세요」 3/3 이라 그 학습자는 영원히
#   통과 불가). 스키마는 [{num, answer_seg}] 만. 서버 검증: answer_seg 는 창 안 U · 그 앞 창 안 B 에 표면형 없음 ·
#   **그 다음 B 에도 표면형 없음**(비버가 정정하지 않고 넘어갔다 = 소리를 들은 쪽의 증언) → passed,
#   로그 provenance=stt_fallback. ⛔ 편집거리는 쓰지 않는다(가세요/계세요).
#   마지막 판정 상한(EXPR_FINAL_JUDGE_TIMEOUT_S)은 폴백 호출에만 — 서버 검색은 즉시.
# ⛔ 옛 드릴 구간 사이드카(arm 마다 전사 전체/커서/겹침/경계선/phase/스냅샷)는 **전부 지웠다** — 두 판정 경로가 살아
#   있으면 «어느 경로가 판정했나» 가 두 갈래가 된다. 되살리지 마라.

# 퀴즈 큐 로그 접두 — ⛔ 고정. 하네스(c4f6bbb, QUIZ_CUE_LOG_PREFIX)가 이 접두로 큐↔비버 앵커를 시간 대조한다.
EXPR_QUIZ_CUE_LOG_PREFIX = "normalcall 표현학습 퀴즈 큐"
# 닫힘 안전판 — quiz_set 밖 새 번호(비버 발화)가 이만큼 쌓이면 «순번상 다음 항목» 이 아니어도 닫는다.
EXPR_QUIZ_STRAY_CLOSE = 2


class ExpressionQuizFallbackItem(BaseModel):
    num: int
    answer_seg: int | None = None


class ExpressionQuizOut(BaseModel):
    """STT 폴백 판정기 출력 — **번호와 세그먼트 번호만**(문장 필드 금지 — RegroundOut 과 같은 이유).

    서버가 준 항목 번호와 전사의 세그먼트 번호(«U13:»)를 고르게 하고, 서버는 그 번호를 자기 목록으로 되짚어
    검증한다 — 환각이 들어올 자리가 없다.
    """

    items: list[ExpressionQuizFallbackItem] = []


def _expression_quiz_cue(state: _CallState, nums: list[int], *, retry: bool = False) -> str:
    """퀴즈 큐 문구(시스템 텍스트, CONTROL_TAG 접두 — ⛔ «[시스템]» 은 종료 태그와 같던 시절 태그라 금지, call 706)."""
    ctx = state.expr_ctx or {}
    locale_label = ctx.get("locale_label") or "학습자의 모국어"
    target = ctx.get("target_language") or "한국어"
    labels = " ".join("«%s»" % state.reground_items[n - 1] for n in nums if 1 <= n <= len(state.reground_items))
    lead = "아까 틀린" if retry else "방금 배운"
    return (
        f"{CONTROL_TAG} 지금 퀴즈를 내라. {lead} {labels} {len(nums)}개를 한 문제씩 — {locale_label}로 뜻·상황을 주고 "
        f"{target}로 말하게 하라. 정답을 먼저 말하지 마라. {len(nums)}개가 끝나면 다음 새 표현으로 넘어가라. "
        "네 말에 대괄호나 '퀴즈 시작' 같은 단계 표시를 넣지 마라 — 그냥 말로 내라."
    )


def _arm_expression_quiz_cue(state: _CallState, nums: list[int], *, retry: bool = False) -> None:
    """큐를 세운다 — 다음 학습자 발화 시작(마이크·RMS)에 얹힌다. 재접지 슬롯과 별개다."""
    state.expr_quiz_set = list(nums)
    state.expr_quizzed.update(nums)
    # ⭐ T17-4 (1405 ② 진짜요) — 큐를 받은 **여는 비버 턴**이 직전 드릴 피드백(«polite form, 진짜요?»)과 퀴즈 시작을
    #   한 턴에 담아 첫 사건이 공개→failed 가 됐다. 큐 직전 마지막 covered 항목을 기억해 두고, 여는 B 세그먼트에서
    #   **그 항목의** 표면형만 공개로 세지 않는다(그 세그먼트만).
    state.expr_quiz_prev_num = state.covered_nums[-1] if state.covered_nums else None
    if not retry:
        state.expr_quiz_seq += 1
    else:
        state.expr_retry_cued = True
    state.expr_quiz_cue_pending = _expression_quiz_cue(state, nums, retry=retry)
    state.expr_quiz_cue_armed_ts = asyncio.get_running_loop().time() if _loop_running() else None
    logger.info(
        "%s arm: seq=%d 항목=%s retry=%s covered=%d", EXPR_QUIZ_CUE_LOG_PREFIX,
        state.expr_quiz_seq, nums, retry, len(state.covered_nums),
    )


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _expression_quiz_maybe_arm(state: _CallState) -> None:
    """[drill] 에서 큐를 세울 때가 됐나 — ⛔ 퀴즈가 열려 있거나 큐가 대기 중이면 세우지 않는다(닫힌 뒤에만)."""
    if not state.expr_items or state.expr_quiz_open or state.expr_quiz_awaiting_open:
        return
    if state.expr_quiz_cue_pending is not None or state.should_close or state.close_seed_sent:
        return
    g = svc.EXPRESSION_QUIZ_GROUP
    unquizzed = [n for n in state.covered_nums if n not in state.expr_quizzed]
    if len(state.covered_nums) >= g * (state.expr_quiz_seq + 1) and len(unquizzed) >= g:
        _arm_expression_quiz_cue(state, unquizzed[:g])
        return
    all_covered = len(state.covered_nums) >= len(state.expr_items)
    if all_covered and unquizzed:
        # ⭐ 꼬리(P0-3): 목록 끝에 G개가 안 남았어도 남은 것만으로 — 비버가 스스로 퀴즈를 열지 않으니 서버가 연다.
        _arm_expression_quiz_cue(state, unquizzed)
        return
    if all_covered and not unquizzed and state.expr_quiz_fail and not state.expr_retry_cued:
        # ⭐ 오답 재출제(기획 ⑥) — 같은 기계로 한 번 더. 통화당 1회. 종료 구간이면 위에서 이미 걸러졌다.
        failed_nums = [
            n for n, it in enumerate(state.expr_items, 1)
            if it.get("item_id") is not None and int(it["item_id"]) in state.expr_quiz_fail
        ]
        if failed_nums:
            _arm_expression_quiz_cue(state, failed_nums, retry=True)


def _expression_quiz_tick(state: _CallState, idx: int, *, source: str, text: str = "") -> None:
    """covered 에 번호가 **새로** 들어온 직후 한 번 — 닫힘 판정(①) 뒤 arm 판정.

    `text` = 그 번호를 낸 발화(아직 segments 에 안 들어갔다 — flush 순서). 닫힘이면 이 발화를 창 꼬리에 넣는다:
    «It's 저는 미국 사람이에요. Now next: 도와주세요» 처럼 **공개와 다음 항목 소개가 한 턴**에 오는 게 흔하다 — 빼면
    그 공개가 창 밖이 돼 failed 가 미판정으로 샌다.
    """
    if not state.expr_items:
        return
    if source == "beaver":
        state.expr_covered_by_beaver.add(idx)
    if state.expr_quiz_open:
        # ⛔ 닫힘은 **비버 발화로** covered 된 번호만 본다(codex P1-1).
        # ⛔ T20: 열 때 이미 covered 였던 번호는 새 소개가 아니다(재언급). 여는 세그먼트(아직 segments 에 안 들어간 여는 비버
        #   턴 = len(segments) == open_seg)에서 큐 직전 드릴 중이던 항목(drill_num)의 표면형은 드릴 피드백이다 — 닫힘 트리거가
        #   아니고 stray 도 아니다. 여는 턴이 그 밖의 **진짜 새 항목**을 소개하면 그건 그대로 ① 규칙을 탄다.
        if source == "beaver" and idx in state.expr_quiz_covered_at_open:
            return
        opening = len(state.segments) == state.expr_quiz_open_seg
        if source == "beaver" and opening and idx == state.expr_quiz_drill_num:
            return
        if source == "beaver" and idx not in state.expr_quiz_set:
            state.expr_quiz_stray.append(idx)
            next_num = min((n for n in range(1, len(state.expr_items) + 1)
                            if n not in state.covered_nums or n == idx), default=idx)
            if idx == next_num or len(state.expr_quiz_stray) >= EXPR_QUIZ_STRAY_CLOSE:
                _close_expression_quiz(
                    state, why="다음 항목 소개" if idx == next_num else "밖 번호 %d개" % len(state.expr_quiz_stray),
                    closing_text=text,
                )
        return
    _expression_quiz_maybe_arm(state)


def _expression_quiz_open_on_beaver_turn(state: _CallState) -> None:
    """큐가 얹힌 뒤 **다음 비버 turn_start** — 여기서 창을 연다(fable P1-1). flush 직후에 불려 len(segments) 가 창의 첫 인덱스다."""
    if state.expr_quiz_awaiting_open:
        state.expr_quiz_awaiting_open = False
        state.expr_quiz_open = True
        state.expr_quiz_open_seg = len(state.segments)
        state.expr_quiz_stray = []
        # ⭐ T20 (1410 seq=3) — 여는 비버 턴은 «직전 드릴 피드백 + 퀴즈 시작» 이 한 턴에 오는 게 정상이다. 그 턴이 큐 직전에
        #   드릴 중이던 항목(= 열 때 아직 안 다룬 가장 앞 번호)의 표면형을 공개하면(«It's 괜찮아요. Now, quiz time again!»)
        #   옛 코드는 그걸 «다음 항목 소개» 로 읽어 창을 열자마자 닫았다(창 42~42, [6,7,8] 전부 미판정, 뒤 40초 정답 유실).
        #   열 때 그 번호를 기억해 두고 여는 세그먼트에서는 닫힘 트리거로 안 쓴다. 열 때 이미 covered 였던 번호도 제외(T17-4 취지).
        covered_now = set(state.covered_nums)
        state.expr_quiz_covered_at_open = covered_now
        state.expr_quiz_drill_num = next(
            (n for n in range(1, len(state.expr_items) + 1) if n not in covered_now), None,
        )
        logger.info("%s 열림: seq=%d open_seg=%d 항목=%s", EXPR_QUIZ_CUE_LOG_PREFIX,
                    state.expr_quiz_seq, state.expr_quiz_open_seg, state.expr_quiz_set)


def _expression_quiz_span(state: _CallState, *, include_tail: bool) -> list[tuple[int, str, str]]:
    """퀴즈 창 = (세그먼트 인덱스, 역할, 텍스트) — open_seg 부터. include_tail 이면 아직 flush 안 된 버퍼도(가상 인덱스)."""
    span: list[tuple[int, str, str]] = []
    for i in range(state.expr_quiz_open_seg, len(state.segments)):
        seg = state.segments[i]
        text = (seg.get("text") or "").strip()
        if text:
            span.append((i, "beaver" if seg.get("role") == "beaver" else "user", text))
    if include_tail:
        n = len(state.segments)
        tb = "".join(state.cur_beaver_text).strip()
        tu = "".join(state.cur_user_text).strip()
        if tb:
            span.append((n, "beaver", tb))
        if tu:
            span.append((n + 1, "user", tu))
    return span


def _num_item(state: _CallState, n: int) -> tuple[int | None, str]:
    """번호 → (item_id, 표면형)."""
    it = state.expr_items[n - 1] if 1 <= n <= len(state.expr_items) else {}
    iid = it.get("item_id")
    return (int(iid) if iid is not None else None), str(it.get("obj") or "")


def _server_judge_quiz(state: _CallState, span: list[tuple[int, str, str]], quiz_set: list[int]) -> dict:
    """서버 검색 — 항목마다 창을 순서대로 훑어 **먼저 나오는 사건**으로 확정한다. 반환 {passed, failed, pending}(번호)."""
    out = {"passed": [], "failed": [], "pending": []}
    for n in quiz_set:
        iid, surface = _num_item(state, n)
        if iid is None or not surface:
            continue
        verdict = ""
        for idx, role, text in span:
            if not quiz_judge.mentions(text, surface):
                continue
            if role == "user":
                if quiz_judge.keeps_formality(text, surface):
                    verdict = "passed"
                    break
                continue                       # 반말 산출(V4) — 사건이 아니다. 계속 훑는다(뒤에 공개가 오면 failed)
            if idx == state.expr_quiz_open_seg and n == state.expr_quiz_prev_num:
                continue                       # T17-4: 여는 B 의 직전 드릴 피드백 — 공개가 아니다(그 항목·그 세그먼트만)
            verdict = "failed"                 # 비버가 표면형을 말했다 = 공개
            break
        if verdict == "passed":
            state.expr_quiz_pass.add(iid)
            state.expr_quiz_fail.discard(iid)
            out["passed"].append(n)
        elif verdict == "failed":
            if iid not in state.expr_quiz_pass:
                state.expr_quiz_fail.add(iid)
            out["failed"].append(n)
        else:
            out["pending"].append(n)
    return out


def _expression_quiz_fallback_instruction(state: _CallState, nums: list[int]) -> str:
    """STT 폴백 판정기 지시문 — 미판정 항목만, 세그먼트 번호로 답한다(순수 문자열 조립)."""
    ctx = state.expr_ctx or {}
    target = ctx.get("target_language") or "한국어"
    rows = []
    for n in nums:
        _iid, surface = _num_item(state, n)
        it = state.expr_items[n - 1]
        row = f"{n}. {surface}"
        if it.get("des"):
            row += f" — 뜻: {it['des']}"
        if it.get("ex"):
            row += ' — 예문: "%s"' % it["ex"]
        rows.append(row)
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


def _verify_stt_fallback(
    state: _CallState, span: list[tuple[int, str, str]], n: int, answer_seg: int | None,
) -> tuple[bool, str]:
    """폴백 제안 검증 — 3조건: 창 안 U · 그 앞 창 안 B 에 표면형 없음 · 그 다음 B 에도 표면형 없음(정정 없이 넘어갔다)."""
    _iid, surface = _num_item(state, n)
    by_idx = {i: (role, text) for i, role, text in span}
    if not isinstance(answer_seg, int) or answer_seg not in by_idx or by_idx[answer_seg][0] != "user":
        return False, "창 밖·U 아님"
    if answer_seg < state.expr_quiz_open_seg:
        return False, "창 밖"
    if not quiz_judge.keeps_formality(by_idx[answer_seg][1], surface):
        return False, "격식(반말)"             # T17-5: 1405 ④ 폴백이 「잘 부탁해」 를 passed 로 — 서버 판정과 같은 V4
    for i, role, text in span:
        if i >= answer_seg:
            break
        if role == "beaver" and quiz_judge.mentions(text, surface):
            return False, "앞 B 에 공개"
    for i, role, text in span:
        if i > answer_seg and role == "beaver":
            if quiz_judge.mentions(text, surface):
                return False, "다음 B 가 정정"
            break
    return True, ""


async def _expression_quiz_stt_fallback(
    state: _CallState, span: list[tuple[int, str, str]], pending: list[int],
) -> list[int]:
    """미판정 항목에만 LLM 을 부른다(R5 — 실패해도 미판정 그대로). 반환 = 폴백으로 passed 된 번호."""
    ctx = state.expr_ctx
    if ctx is None or not pending or not any(role == "user" for _, role, _ in span):
        return []
    if state.expr_sidecar_calls >= EXPR_PROGRESS_MAX_PER_CALL:
        return []
    state.expr_sidecar_calls += 1
    lines = [("B%d: " if role == "beaver" else "U%d: ") % i + text for i, role, text in span]
    transcript = chr(10).join(lines)
    if len(transcript) > EXPR_TRANSCRIPT_MAX_CHARS:                # 퀴즈 구간이 이걸 넘을 일은 없다 — 규칙만 유지
        transcript = transcript[-EXPR_TRANSCRIPT_MAX_CHARS:]
    passed_now: list[int] = []
    try:
        result = await gemini_analysis.generate_structured(
            ctx["client"], ctx["model"],
            system_instruction=_expression_quiz_fallback_instruction(state, pending),
            prompt=f"[퀴즈 전사]{chr(10)}{transcript}",
            schema=ExpressionQuizOut,
            temperature=0.0,
            thinking_budget=0,
            usage=state.sidecar_usage,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 폴백 실패는 미판정일 뿐(R5)
        logger.warning("normalcall 표현학습 퀴즈 폴백 실패(무시): %s", exc)
        return []
    if result is None:
        return []
    for item in getattr(result, "items", None) or []:
        n = getattr(item, "num", None)
        if not isinstance(n, int) or n not in pending:
            continue
        ok, why = _verify_stt_fallback(state, span, n, getattr(item, "answer_seg", None))
        iid, surface = _num_item(state, n)
        if ok and iid is not None:
            state.expr_quiz_pass.add(iid)
            state.expr_quiz_fail.discard(iid)
            passed_now.append(n)
            logger.info("normalcall 표현학습 퀴즈 판정 provenance=stt_fallback: 항목 %d «%s» answer_seg=%s",
                        n, surface, item.answer_seg)
        else:
            logger.info("normalcall 표현학습 퀴즈 폴백 기각: 항목 %d «%s» answer_seg=%s (%s)",
                        n, surface, getattr(item, "answer_seg", None), why or "item_id 없음")
    return passed_now


def _close_expression_quiz(state: _CallState, *, why: str, closing_text: str = "") -> None:
    """닫힘 ① — 창을 잡고 서버 판정을 **즉시** 하고, 미판정이 있으면 폴백을 띄운다(논블로킹).

    `closing_text` = 닫힘을 일으킨 비버 발화(아직 flush 전). 창 꼬리에 B 로 넣는다 — 공개+다음 소개가 한 턴인 경로.
    """
    if not state.expr_quiz_open:
        return
    span = _expression_quiz_span(state, include_tail=False)
    if closing_text.strip():
        span.append((len(state.segments), "beaver", closing_text.strip()))
    quiz_set = list(state.expr_quiz_set)
    state.expr_quiz_open = False
    state.expr_quiz_set = []
    state.expr_quiz_stray = []
    res = _server_judge_quiz(state, span, quiz_set)
    logger.info(
        "normalcall 표현학습 퀴즈 판정(서버): seq=%d 닫힘=%s 창=%d~%d(%d세그) set=%s passed=%s failed=%s 미판정=%s",
        state.expr_quiz_seq, why, state.expr_quiz_open_seg, len(state.segments), len(span), quiz_set,
        res["passed"], res["failed"], res["pending"],
    )
    if res["pending"] and state.expr_ctx is not None and not state.should_close and _loop_running():
        task = asyncio.create_task(
            _expression_quiz_stt_fallback(state, span, res["pending"]), name="normalcall-expr-quiz-fallback",
        )
        state.expr_tasks.add(task)
        task.add_done_callback(state.expr_tasks.discard)
    _expression_quiz_maybe_arm(state)


async def _final_expression_progress(state: _CallState) -> None:
    """⭐⭐ **조각 끝 마지막 판정** — DB 쓰기 **전에**(사장님 지시). 열린 퀴즈가 있으면 그 창을 닫고 판정한다(닫힘 ②).

    ## ⛔⛔ 쓰기를 LLM 에 걸지 않는다
    서버 검색은 즉시다. 폴백 LLM 만 EXPR_FINAL_JUDGE_TIMEOUT_S 로 묶고, timeout·예외·None 어느 쪽이든 있는 것만 저장한다(R5).
    이 함수는 예외를 밖으로 내보내지 않는다 — 저장 경로가 이 함수의 성패에 묶이면 LLM 이 죽는 날 **조각2 가 안 열린다.**
    ⚠ 조각 경계는 이어하기 요약과 경합한다(EXPR_FINAL_JUDGE_TIMEOUT_S 주석) — 소요를 로그로 남긴다.
    ⚠ 큐만 세워졌고 열리지 않은 퀴즈는 drilled 로만 남는다(안전 방향 — 큐 idle 폴백을 두지 않는다, codex P1-2).
    """
    if not state.expr_items:
        return
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    over = False
    try:
        # 닫힘 ① 로 띄운 폴백이 아직 돌고 있으면 같은 예산 안에서 기다린다 — 그 결과도 이 조각의 저장에 실려야 한다.
        pending_tasks = {t for t in state.expr_tasks if not t.done()}
        if state.expr_quiz_open:
            span = _expression_quiz_span(state, include_tail=True)
            quiz_set = list(state.expr_quiz_set)
            state.expr_quiz_open = False
            state.expr_quiz_set = []
            res = _server_judge_quiz(state, span, quiz_set)
            logger.info(
                "normalcall 표현학습 퀴즈 판정(서버·마지막): seq=%d 창=%d~끝(%d세그) set=%s passed=%s failed=%s 미판정=%s",
                state.expr_quiz_seq, state.expr_quiz_open_seg, len(span), quiz_set,
                res["passed"], res["failed"], res["pending"],
            )
            if res["pending"]:
                await asyncio.wait_for(
                    _expression_quiz_stt_fallback(state, span, res["pending"]),
                    timeout=EXPR_FINAL_JUDGE_TIMEOUT_S,
                )
        if pending_tasks:
            remaining = max(0.0, EXPR_FINAL_JUDGE_TIMEOUT_S - (loop.time() - t0))
            done, not_done = await asyncio.wait(pending_tasks, timeout=remaining)
            over = over or bool(not_done)
    except (TimeoutError, asyncio.TimeoutError):
        over = True
    except Exception as exc:  # noqa: BLE001 - 저장을 막으면 안 된다(R5)
        logger.warning("normalcall 표현학습: 마지막 판정 실패(무시): %s", exc)
    if state.expr_quiz_cue_pending is not None:
        logger.info("%s 미얹힘(통화 끝): seq=%d 항목=%s — drilled 로만 남는다", EXPR_QUIZ_CUE_LOG_PREFIX,
                    state.expr_quiz_seq, state.expr_quizzed and sorted(state.expr_quizzed)[-len(state.expr_quiz_set or []):])
    logger.info(
        "normalcall 표현학습 마지막 판정: %.0fms (상한 %.0fms%s)",
        (loop.time() - t0) * 1000.0, EXPR_FINAL_JUDGE_TIMEOUT_S * 1000.0,
        " — ⚠초과, 있는 것만 저장" if over else "",
    )


def _expression_result_snapshot(state: _CallState, drilled_set: set[int]) -> list[dict]:
    """결과 화면 스냅샷 `[{item_id, surface, meaning, passed, failed}]` — covered ∪ 통과분(위 호출부 주석).

    T19: `meaning`(선별 DTO 의 des — 화면에 모국어 뜻) · `failed`(이 통화 퀴즈에서 공개를 받았다). passed 면 failed=False(단조).
    ⛔ JSON 키 이름은 플러터(flt-build)와 계약이다 — item_id · surface · meaning · passed · failed. 바꾸지 마라.
    """
    out: list[dict] = []
    for i in state.expr_items:
        if i.get("item_id") is None:
            continue
        iid = int(i["item_id"])
        if iid not in drilled_set and iid not in state.expr_quiz_pass:
            continue
        passed = iid in state.expr_quiz_pass
        out.append({
            "item_id": iid,
            "surface": str(i.get("obj") or ""),
            "meaning": (str(i["des"]) if i.get("des") else None),
            "passed": passed,
            "failed": (iid in state.expr_quiz_fail) and not passed,
        })
    return out


def _expr_covered_ids(state: _CallState) -> list[int]:
    """이 통화에서 **다룬 항목의 item_id**(순서 = 다룬 순서).

    ⛔⛔ **표면형으로 되짚지 마라.** `covered_nums` 는 `state.reground_items` 의 번호이고
      그 목록은 `expr_items` 와 **같은 순서**로 만들어진다 ⇒ 번호로 곧장 item_id 가 나온다.
      한때 «라벨 목록 → surface set → 그 표면형을 가진 항목» 으로 되짚었는데, 같은 레벨에
      **동일 표면형이 91그룹**(assets/level/curriculum_v2/vocab.json 전수 스캔)이라
      **두 항목에 함께** 진도가 찍혔다. `quiz_passed_at` 은 되돌릴 수 없다 — 엉뚱한 항목이
      영구히 «완료» 가 된다.
    """
    items = state.expr_items
    return [
        int(items[n - 1]["item_id"])
        for n in state.covered_nums
        if 1 <= n <= len(items) and items[n - 1].get("item_id") is not None
    ]


def _expr_labels(state: _CallState, ids: set[int]) -> list[str]:
    """item_id 집합 → 표면형 목록(쪽지·로그용). 순서는 목록 순서 그대로."""
    return [
        str(i.get("obj")) for i in state.expr_items
        if int(i.get("item_id") or -1) in ids and i.get("obj")
    ]


def _covered_labels(state: _CallState) -> list[str]:
    """`covered_nums` → 브리프에 실을 라벨 목록(순서 = 다룬 순서)."""
    items = state.reground_items
    return [items[n - 1] for n in state.covered_nums if 1 <= n <= len(items)]


def _flush_beaver_segment(state: _CallState) -> None:
    if not state.cur_beaver_pcm and not state.cur_beaver_text:
        return
    text = "".join(state.cur_beaver_text)
    # 자기낭독 정화: 비버가 서버 제어 태그를 읽어버린 경우 저장본에서 걷어낸다. 통화후
    # 분석·문장 추출이 "[시스템] 통화가 종료되었습니다" 같은 걸 학습 문장으로 삼지 않게.
    # (자막은 이미 나간 뒤라 손대지 않는다 — 조각 단위라 부분 마스킹이 더 이상해진다.)
    if _find_control_tag_leak(text, state.expr_tag_allow):
        text = _scrub_control_tags(text, state.expr_tag_allow)
    # ⭐ 조각 경계에 걸쳐 쪼개진 tool 낭독은 여기서 잡힌다 — 이 시점엔 턴 전체가 이어져 있다.
    #   (자막 경로는 조각 단위라 못 잡는 것이 있다. 두 겹으로 거른다.)
    text = _strip_face_echo(text)
    text = text.strip()
    logger.info("🦫 BEAVER[t%d]: %s", state.next_turn_index, text or "(전사없음)")
    # ⭐ **말하기 속도 실측**(2026-08-10). 사장님: "라이브에서는 속도가 딱 좋아" — 그러니
    #   Live 값이 캐스케이드 TTS 의 **목표 숫자**가 된다. 지금은 "몇 자per초가 정상인가"에
    #   근거가 없어서, 느리다는 판정이 귀에만 있고 숫자로 못 옮겨진다.
    #   ⛔ 계측만이다. 흐름·버퍼는 건드리지 않는다(R4) — 이미 있는 pcm/전사의 **길이만** 센다.
    logger.info("🦫 BEAVER[t%d] %s", state.next_turn_index,
                _reading_speed_line(text, len(state.cur_beaver_pcm)))
    # 레벨테스트 밴드 관측: 방금 끝난 비버 발화(직전 질문)를 스냅샷 — 다음 유저 답변 관측의
    # prior_question 문맥. band_observe=False(일반 통화)면 무동작.
    if state.band_observe and text:
        state.last_beaver_question = text
    # ⭐ 재접지 쪽지의 "이미 다룬 것" 을 여기서 **서버가 누적한다**(2026-09-09, 통화 1360).
    #   턴이 확정되는 유일한 자리라 압축·사이드카와 무관하게 통화 전체를 센다.
    _note_covered_items(state, text)
    state.segments.append(
        {"turn_index": state.next_turn_index, "role": "beaver", "text": text, "pcm": bytes(state.cur_beaver_pcm)}
    )
    state.next_turn_index += 1
    state.beaver_turns += 1
    state.cur_beaver_pcm = bytearray()
    # ⛔ **여기서 같이 비운다.** `face_last_pcm` 은 `cur_beaver_pcm` 의 길이를 재는 값이라,
    #   버퍼가 0으로 돌아가는데 이 값이 남으면 **다음 턴의 0 과 이전 턴의 0 이 같은 자리로
    #   착각**된다 ⇒ 새 턴의 첫 마커가 「같은자리」로 조용히 버려진다.
    #   회귀가 잡았다(test_face_markers_flow_and_duplicates_are_dropped: sad 가 사라졌다).
    #   ⚠ 두 값이 같은 전제를 나눠 가지므로 **비우는 자리도 하나**여야 한다.
    state.face_last_pcm = -1
    state.cur_beaver_text = []


# --------------------------------------------------------------------------- #
# 원가 계기판(Phase 0) — Live usage_metadata 수집·방출
# --------------------------------------------------------------------------- #
# 통화당 적재 상한. 15분 통화·이상 상황에서 메모리·로그가 폭주하지 않게 한다.
# 초과분은 개수만 세고 버린다(요약의 Σ 가 과소가 되지만, dropped 로 드러난다).
_USAGE_LOG_MAX = 400


def _modality_pairs(details) -> list[tuple[str, int]]:
    """ModalityTokenCount 리스트를 (모달리티명, 토큰수) 튜플로 정규화한다.

    modality 는 SDK enum(MediaModality) 이라 .name 을 우선 쓰고, 문자열이면 그대로 쓴다.
    필드가 없거나 형태가 다르면 조용히 건너뛴다 — 계측이 통화를 죽이면 안 된다.
    """
    out: list[tuple[str, int]] = []
    for m in details or []:
        mod = getattr(m, "modality", None)
        name = getattr(mod, "name", None) or (str(mod) if mod is not None else "UNKNOWN")
        count = getattr(m, "token_count", None)
        if count:
            out.append((name, int(count)))
    return out


def _observe_compression(state: _CallState, prompt) -> None:
    """prompt_token_count 시계열로 **컨텍스트 압축**을 관측한다(재접지 트리거의 눈).

    🧒 Live 는 "압축했다"는 이벤트를 안 준다(전 필드를 다 뒤졌다). 유일한 단서가 매 턴
      실려 오는 입력 토큰 수다 — 대화가 쌓이면 계속 늘다가, 압축이 일어나면 **뚝 떨어진다**.
      그 톱니를 보고 추론하는 게 여기다.

    두 신호를 세운다:
      - 임박(선제): 최고치가 ARM_RATIO × trigger 를 넘었다 → 곧 압축된다.
      - 발생(사후): 최고치 대비 급감했다 → 방금 압축됐다(선제 arm 이 유저 침묵으로 못 얹힌 경우).

    ⚠ 미탐(주입 누락)은 무해하고 오탐(불필요 주입)은 이중발화 위험이라, 임계는 미탐 쪽으로
      보수적으로 잡는다 — 절대 낙차(DROP_MIN_TOKENS)까지 함께 요구한다.
    판정 결과는 플래그가 아니라 상태값(peak·compression_seen)으로만 남긴다. arm 여부는
    워처가 이 값을 읽어 정한다(관측과 결정의 분리).
    """
    if not prompt:
        return
    p = int(prompt)
    # 바닥은 **첫 1건으로 고정**한다 — 이 시점의 prompt 가 곧 지시문 크기다(대화 0줄).
    # ⛔ min() 으로 계속 낮추지 않는다. 압축 후 값이 바닥보다 낮게 찍히는 순간이 있는데
    #   그걸 바닥으로 삼으면 기준이 통화 중에 흔들려 arm 이 다시 눈멀게 된다.
    if state.usage_prompt_floor == 0:
        state.usage_prompt_floor = p
    # 통화 전체 최대치는 압축과 무관하게 여기서만 갱신한다(아래 리셋에 걸리지 않는 자리).
    if p > state.usage_prompt_max:
        state.usage_prompt_max = p
    peak = state.usage_prompt_peak
    if p > peak:
        state.usage_prompt_peak = p
        return
    # 급감 = 압축. ①압축이 일어날 수 있는 자리였나 ②기대 낙차의 일정 비율 이상 떨어졌나
    # — 둘 다 만족해야 한다(작은 요동 배제). 문턱은 설정에서 파생된다(위 상수 주석).
    trigger = _settings.LIVE_CTX_TRIGGER_TOKENS
    plausible = peak >= trigger * REGROUND_ARM_RATIO
    drop_min = max(0, trigger - _settings.LIVE_CTX_TARGET_TOKENS) * REGROUND_DROP_MIN_RATIO
    if plausible and peak - p >= drop_min:
        state.compression_seen += 1
        state.usage_prompt_peak = p  # 새 사이클의 바닥에서 다시 센다
        logger.info(
            "normalcall: 컨텍스트 압축 감지 #%d (prompt %d → %d)",
            state.compression_seen, peak, p,
        )


def _record_usage(state: _CallState, um) -> None:
    """Live usage_metadata 1건을 시계열에 적재한다(예외 전량 흡수, R5).

    🧒 왜 이걸 모으나: Live API 는 비버가 한 턴 말할 때마다 **그때까지의 대화 전체**를
      입력으로 다시 과금한다. 통화가 길어질수록 매 턴의 입력값이 커지는 구조라, 통화
      원가의 대부분이 여기서 나온다. 그런데 서버가 이 숫자를 여태 한 번도 안 봤다 —
      가장 비싼 항목이 가장 안 보이는 상태였다. 여기서 그걸 받아 적는다.

    ⚠ 이 값이 "메시지별 증분"인지 "세션 누적"인지는 서버가 정하고 문서로 확정되지
      않았다. 그래서 해석하지 않고 원본 그대로 시계열에 쌓아, 종료 로그가 Σ(합)과
      last(마지막 값)를 **둘 다** 내보내게 한다 — 단조증가면 누적, 톱니면 증분이다.
      같은 시계열이 "컨텍스트 압축이 실제로 발동하는가"도 함께 답한다.
    """
    try:
        # 압축 관측은 적재 상한과 무관하게 계속 돈다 — 상한을 넘긴 긴 통화야말로 압축이
        # 가장 활발한 구간이라, 여기서 끊으면 재접지가 후반부터 눈이 먼다.
        _observe_compression(state, getattr(um, "prompt_token_count", None))
        if len(state.usage_log) >= _USAGE_LOG_MAX:
            state.usage_dropped += 1
            return
        t: Optional[float] = None
        if state.call_start_ts is not None:
            t = round(asyncio.get_running_loop().time() - state.call_start_ts, 1)
        state.usage_log.append({
            "t": t,                               # 통화 시계 기준 경과초(첫 턴 전이면 None)
            "turn": state.next_turn_index,        # 이 시점의 세그먼트 커서(턴 진행도 근사)
            "prompt": getattr(um, "prompt_token_count", None),
            "resp": getattr(um, "response_token_count", None),
            "total": getattr(um, "total_token_count", None),
            "thoughts": getattr(um, "thoughts_token_count", None),
            "cached": getattr(um, "cached_content_token_count", None),
            "tool_in": getattr(um, "tool_use_prompt_token_count", None),
            "in_detail": _modality_pairs(getattr(um, "prompt_tokens_details", None)),
            "out_detail": _modality_pairs(getattr(um, "response_tokens_details", None)),
        })
    except Exception as exc:  # noqa: BLE001 - 계측 실패가 통화를 죽이면 안 된다(R5)
        logger.debug("normalcall usage: 적재 실패(무시): %s", exc)


def _usage_summary(state: _CallState) -> Optional[dict]:
    """usage 시계열을 요약 1건으로 접는다(순수 함수 — 로그와 DB 의 **단일 소스**).

    로그 줄과 영속화(call.usage_*)가 서로 다른 계산을 하면 "로그엔 이렇게 찍혔는데 DB 엔
    다른 값"이 된다. 그래서 계산은 여기 한 곳에만 두고, 로그는 이 결과를 문자열로 만들고
    DB 는 같은 결과를 컬럼에 넣는다.

    usage 가 한 건도 없으면 None — 호출부가 "계측 미수신"으로 분기한다(0 토큰과 구별).
    """
    log = state.usage_log
    if not log:
        return None

    def _s(key: str) -> int:
        return sum(int(e[key] or 0) for e in log)

    in_mod: dict[str, int] = {}
    out_mod: dict[str, int] = {}
    for e in log:
        for name, cnt in e["in_detail"]:
            in_mod[name] = in_mod.get(name, 0) + cnt
        for name, cnt in e["out_detail"]:
            out_mod[name] = out_mod.get(name, 0) + cnt

    times = [e["t"] for e in log if e["t"] is not None]
    last = log[-1]
    # 단조성: total 이 한 번도 줄지 않으면 누적 의심, 줄었으면 증분 확정(+압축 발동 신호).
    totals = [int(e["total"] or 0) for e in log]
    monotonic = all(b >= a for a, b in zip(totals, totals[1:]))
    return {
        "msgs": len(log),
        "dropped": state.usage_dropped,
        "t_first": times[0] if times else None,
        "t_last": times[-1] if times else None,
        "sum_prompt": _s("prompt"), "sum_resp": _s("resp"),
        "sum_thoughts": _s("thoughts"), "sum_total": _s("total"),
        # ⭐⭐ **캐시된 컨텍스트 토큰**(2026-08-16). 여태 적재만 하고 **버리고 있었다.**
        #   왜 이게 지금 제일 값진 한 줄인가: `Σ(in_audio)` 는 **같은 오디오를 매 요청 다시 실은**
        #   값이다(call 1026: 59회). 벤더가 그 재전송분을 **캐시로 할인하는지**에 따라 라이브
        #   원가가 통째로 달라지는데, 우리는 그 답이 될 필드를 받아 놓고 안 봤다.
        #   ⇒ 설계 문서가 미해결로 남긴 "재개가 컨텍스트를 재과금하는가"의 첫 단서다.
        #   ⛔ **관측만이다.** 원가식은 안 건드린다 — 값이 나온 뒤에 정한다.
        #   ⚠ 벤더가 이 필드를 **한 번도 안 준** 통화는 `None` 이다. 0 으로 접으면
        #     "캐시가 0 이었다"와 "필드를 안 줬다"가 구별되지 않는다(이 프로젝트 규약).
        "sum_cached": _s("cached") if any(e["cached"] is not None for e in log) else None,
        "last_prompt": last["prompt"], "last_total": last["total"],
        "monotonic": monotonic,
        "in_mod": in_mod, "out_mod": out_mod,
        # 압축·재연결 관측(원가 추이와 함께 봐야 의미가 있는 값들).
        # ⚠ peak_prompt 는 **통화 전체 최대치**(단조증가)다. 압축마다 리셋되는 사이클 peak 는
        #   cycle_peak 로 따로 낸다 — DB 에 사이클 peak 를 넣었더니 "이 통화가 몇 토큰까지
        #   갔나"를 못 보게 됐던 게 call 909(13,355 vs 실제 15,904)에서 드러났다.
        "peak_prompt": state.usage_prompt_max,
        "cycle_peak": state.usage_prompt_peak,
        "compressions": state.compression_seen,
        "epochs": state.session_epoch, "reconnects": state.reconnects,
        # ⭐ 통화중 LLM 사이드카 몫(힌트·재접지·레벨테스트 턴 판정). 한 번도 안 돌았으면
        #   None 이라 저장에서 통째로 빠진다 — "0 원"이 아니라 "안 돌았다"는 뜻이다.
        # ⚠ 여기 실리려면 Live usage 가 1건이라도 있어야 한다(위 `if not log: return None`).
        #   Live 계측이 통째로 없는 통화는 원가 행 자체가 안 생기므로 같이 없는 게 맞다.
        "sidecars": state.sidecar_usage.as_dict(),
    }


def _ratio_pct(part: int | None, whole: int | None) -> str:
    """`part/whole` 을 퍼센트 문자열로 — 못 재면 `-` 다(0% 로 찍지 않는다).

    ⛔ 0% 와 "모른다"는 다르다. 캐시 필드를 안 주는 통화를 0% 로 세면 **평균이 조용히 내려가고**,
      그 표로 "캐시는 안 돈다"는 틀린 결론을 내리게 된다.
    """
    if part is None or not whole:
        return "-"
    return "%.1f%%" % (part * 100.0 / whole)


def _log_usage_summary(state: _CallState, call_id: int | None, call_type: str) -> None:
    """통화 종료 시 usage 요약을 로그로 방출한다(요약 1줄 + 선택 시계열 1줄).

    ⛔ 이 줄의 형식을 바꾸지 마라. `key=value` 로 못박아 둔 덕에 Cloud Logging 로그 기반
    메트릭이 **코드 변경 0줄**로 숫자를 뽑아가고 있고, 조사도 이 줄을 grep 해서 한다.
    영속화(2단계)가 붙은 뒤에도 로그는 그대로다 — DB 는 30일 이후를 위한 것이고,
    로그는 지금 당장 보기 위한 것이라 둘 다 필요하다.
    """
    s = _usage_summary(state)
    if s is None:
        # usage 가 한 건도 안 왔다 = 필드 미제공이거나 모킹 세션. 원인 추적용으로만 남긴다.
        logger.info("normalcall usage: call_id=%s type=%s msgs=0 (usage_metadata 미수신)",
                    call_id, call_type)
        return
    in_mod, out_mod = s["in_mod"], s["out_mod"]

    logger.info(
        "normalcall usage: call_id=%s type=%s msgs=%d dropped=%d t=[%s..%s] "
        "sum_prompt=%d sum_resp=%d sum_thoughts=%d sum_total=%d "
        "sum_cached=%s cached_ratio=%s "
        "last_prompt=%s last_total=%s monotonic=%s "
        "sum_in=%s sum_out=%s",
        call_id, call_type, s["msgs"], s["dropped"],
        f"{s['t_first']:.1f}" if s["t_first"] is not None else "?",
        f"{s['t_last']:.1f}" if s["t_last"] is not None else "?",
        s["sum_prompt"], s["sum_resp"], s["sum_thoughts"], s["sum_total"],
        # ⭐ **비율이 답이다.** 절대값만으로는 "캐시가 도는가"를 못 읽는다 — `sum_prompt` 와
        #   같은 축(Σ)으로 세서 나눈다. 높으면 재전송분이 할인되고 있다는 뜻이고, 그러면
        #   지금 원가식이 **과대**다. 0 이면 지금 식이 그대로 맞다.
        #   ⚠ 필드를 안 준 통화는 `-` 다(0 으로 찍으면 "캐시가 없었다"는 거짓 표본이 된다).
        "-" if s["sum_cached"] is None else s["sum_cached"],
        _ratio_pct(s["sum_cached"], s["sum_prompt"]),
        s["last_prompt"], s["last_total"], s["monotonic"],
        # 모달리티 분해 — 오디오/텍스트 단가가 6배 차이라 이게 있어야 원가가 계산된다.
        ",".join(f"{k}={v}" for k, v in sorted(in_mod.items())) or "-",
        ",".join(f"{k}={v}" for k, v in sorted(out_mod.items())) or "-",
    )

    if s["sum_thoughts"]:
        # 🧒 이 줄이 보이면 원가 산식이 낡았다는 뜻이다. Live 원가는 모달리티 4항만 곱하고
        #   사고 토큰을 안 더한다 — 지금 모델(비-사고 native-audio)이 사고를 안 해서
        #   0 이라는 전제 위에 서 있는 계산이다. 사고형 모델로 갈아탔거나 모델이 조용히
        #   바뀌었으면 그 전제가 깨지고 **원가가 과소 계상된다.**
        #   확인할 것: 모달리티 분해(sum_out)가 이 토큰을 이미 품는지. 안 품으면
        #   estimate_usage_cost_usd 에 out_text 단가로 더해라(주석에 근거 있음).
        logger.warning(
            "normalcall usage: call_id=%s 사고 토큰 %d 관측 — Live 원가 산식이 이 값을 "
            "안 세고 있다(과소 계상 가능). estimate_usage_cost_usd 주석 참조",
            call_id, s["sum_thoughts"],
        )

    if _settings.LIVE_USAGE_TRACE:
        # 시계열 상세: 압축 발동 판정용(톱니 = 발동, 단조증가 = 미발동).
        trace = " ".join(f"{e['t']}:{e['prompt']}/{e['total']}" for e in state.usage_log)
        logger.info("normalcall usage trace: call_id=%s t:prompt/total %s", call_id, trace)


async def _persist_usage(db_session_factory, state: _CallState, call_id: int | None) -> None:
    """usage 요약을 통화 행에 남긴다(원가 계기판 2단계 — 로그 30일 보존을 넘기기 위해).

    ⛔ 통화 경로가 아니다. 통화 루프가 끝난 뒤 붙는 부가 작업이라, 실패해도 전사 저장·분석·
      통화 종료는 그대로 간다(R5). 그래서 _persist_remaining 과 **트랜잭션을 나눈다** —
      한 트랜잭션에 묶으면 usage 오류 하나가 통화 기록을 같이 죽인다.
    ⛔ usage 가 한 건도 없으면 **아무것도 쓰지 않는다.** NULL 로 남아야 "계측 안 됨"과
      "정말 0 토큰"이 구별된다.
    """
    if call_id is None:
        return
    summary = _usage_summary(state)
    if summary is None:
        return
    try:
        await svc.run_db(
            db_session_factory,
            # ⛔ 엔진 태그는 **반드시** 넘긴다. 이게 비면 나중에 캐스케이드 행과 섞여
            #   "어느 엔진의 원가인지" 되짚을 수 없게 된다(계약: models/call.py usage_engine).
            # ⭐⭐ **모델을 태그에 싣는다**(2026-09-04). 플랜에 따라 2.5/3.1 이 갈리는데
            #   종전 태그(`live:gemini-native-audio`)는 모델을 안 담아 **두 모델의 통화가
            #   같은 문자열로 섞였다** — "2.5 로 내린 게 얼마 아꼈나"를 DB 로 못 묻는다.
            #   ⛔ `ENGINE_LIVE_GEMINI` 상수는 **안 건드린다** — 캐스케이드와 공유하는
            #     계약이고, 원가 분기가 `startswith("cascade:")` 라 접두사만 지키면 된다.
            #   ⇒ `build_engine_tag("live", <모델id>)` = "live:gemini-3.1-flash-live-preview"
            #   ⚠ 모델을 모르면(재개 세대 등) 종전 상수로 떨어진다 — 값이 비는 것보다 낫다.
            lambda db: svc.save_call_usage(
                db, call_id, summary,
                engine=(
                    svc.build_engine_tag("live", state.live_model)
                    if state.live_model else svc.ENGINE_LIVE_GEMINI
                ),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 계기판 저장 실패가 통화를 죽이면 안 된다(R5)
        logger.warning("normalcall usage: 영속화 실패(무시) call_id=%s: %s", call_id, exc)


# --------------------------------------------------------------------------- #
# 진입점
# --------------------------------------------------------------------------- #
async def run_call(
    client_ws,
    settings: Settings,
    client: genai.Client,
    db_session_factory: sessionmaker,
    *,
    member_id: int,
    member_target_language: str | None = None,
    live_session_factory: SessionFactory | None = None,
) -> None:
    """노멀콜 단일 통화를 양방향 중계한다(인증은 ws_router 가 끝낸 뒤 호출).

    Args:
        client_ws: 이미 accept 된 FastAPI WebSocket.
        settings: 서버 설정.
        client: lifespan 의 genai.Client(app.state.genai_client).
        db_session_factory: app.state.session_factory(SQLAlchemy sessionmaker).
        member_id: 인증된 회원 id.
        member_target_language: DB(member.target_language)의 학습 대상 언어. 이 값이
            **단일 소스**다 — 클라 start.target_language 는 환경과 무관하게 무시한다.
            None 이면 settings.DEFAULT_TARGET_LANGUAGE 폴백. 기본 None 이라 기존 호출·테스트 무영향.
        live_session_factory: Live 세션 CM 팩토리(모킹 확장점). None 이면 호출 시점에
            모듈의 open_session 을 사용한다(기본 인자로 박지 않아 monkeypatch 가능).
    """
    # 기본값을 함수 정의 시점에 바인딩하지 않고 호출 시점에 해석 → 테스트에서
    # `open_session` 을 monkeypatch 하면 그대로 반영된다(운영은 실제 open_session).
    factory = live_session_factory or open_session
    # 1) 첫 start → character_id / locale / target_language / call_type / duration override.
    try:
        start = await _read_initial_start(client_ws)
        inbound_call_id = start.inbound_call_id
        client_character_id = start.character_id   # ⛔ 로그 전용 — 통화에 쓰지 않는다
        locale_override = start.locale
        target_override = start.target_language
        call_type_override = start.call_type
        duration_override = start.duration_min
        # ⭐ 이어하기 — 클라가 보내는 값은 문자열일 수 있다("1234"). 못 읽으면 조용히 무시하고
        #   새 통화로 간다(거절이 아니라 폴백 — 이어하기가 안 된다고 통화를 막으면 더 나쁘다).
        continues_call_id = _as_int(start.continues_call_id)
        # ⭐ 과제 통화 — 못 읽으면 조용히 무시하고 평소 통화로 간다(이어하기와 같은
        #   폴백 규율). 자격 검증은 B2B 서비스가 하므로 여기서 판단하지 않는다.
        assignment_id = _as_int(start.assignment_id)
    except _ClientDisconnect:
        logger.info("normalcall: start 수신 전 클라 종료")
        return

    # 교육 대상 언어(멀티랭귀지) → LanguageSpec. is_demo 폐지: 언어별 동작은 spec 한 행이
    # 결정한다. spec.label 을 페르소나 대상 언어로, 모국어 라벨은 _LOCALE_LABEL 기본을 쓴다
    # (locale="ko" 도 이제 "한국어"로 해석 — 데모용 override hack 제거). ko 는 label=="한국어"·
    # has_curriculum·leveltest 라 기존 한국어 통화 경로·프롬프트 바이트 불변.
    # (멀티랭귀지) 레벨/커리큘럼 선별·needs_level_test 가 언어 스코프라 load_call_setup 전에 해석.
    # (멀티랭귀지) 학습 대상 언어의 단일 소스는 **DB(member.target_language)** 다.
    #   옛날엔 앱 SharedPreferences 가 원본이라 ① 복원 전에 통화가 시작되면 저장값 대신
    #   기본 'ko' 가 실려 나가고(잠금화면 수신통화가 그 구간) ② 재설치 시 리셋되고
    #   ③ 서버가 거는 예약전화인데 언어는 클라가 정하는 모순이 있었다.
    #
    # ⛔ start.target_language 는 **환경과 무관하게 항상 무시한다**. ENV 로 게이트하지 마라 —
    #   실서비스(app-api)조차 ENV="test" 라 prod 게이트는 무력하다(실측). 언어를 바꾸는
    #   유일한 통로는 PATCH /members/me {target_language} 다. 데모(level_call_demo.html)도
    #   통화 전에 그 PATCH 를 호출한다 — 실제 동작과 같은 경로를 타게.
    #   근거: docs/20260728_0125_학습언어-DB-단일소스화와-모국어-정규화.md
    if target_override is not None:
        # 구버전 앱 탐지용 — 전송이 사라지면 이 로그도 사라진다.
        logger.info(
            "normalcall: start.target_language(%s) 무시 — DB(member.target_language=%s) 사용",
            target_override, member_target_language,
        )
    call_target_language = _call_target_language(member_target_language, assignment_id)
    spec = _resolve_target_language(settings, call_target_language)
    target_language = spec.label

    # 통화 캐릭터는 **서버가 정한다** — start.character_id 는 무시한다.
    #   수신통화(알람)면 inbound_call_id → push_dispatch_log → alarm.character_id,
    #   그 외에는 member.character_id(소유 확인). 자세한 근거는 resolve_call_character.
    # ⛔ ENV 로 게이트하지 마라 — 실서비스(app-api)조차 ENV="test" 인 적이 있어
    #   prod 게이트는 무력하다(target_language 에서 겪은 그대로).
    character_id = await svc.run_db(
        db_session_factory,
        lambda db: svc.resolve_call_character(db, member_id, inbound_call_id),
    )
    if client_character_id is not None and client_character_id != character_id:
        # 구버전 앱 탐지용 — 전송이 사라지면 이 로그도 사라진다.
        logger.info(
            "normalcall: start.character_id(%s) 무시 — 서버 결정 %s (inbound=%s)",
            client_character_id, character_id, inbound_call_id,
        )
    # ⚠ `call_started` 는 여기서 보내지 않는다 — **통화 행이 아직 없어서 call_id 를 못 싣는다**
    #   (아래 3) 에서 만든다). 클라는 그 번호를 다음 조각의 `continues_call_id` 로 돌려줘야
    #   하므로 **번호가 실린 뒤에** 보낸다. 여전히 오디오보다 먼저다(계약 유지).

    # 2) 프롬프트 입력 조회(레벨 프로파일·페르소나·voice·locale) — 1회, 짧은 세션.
    #    needs_level_test(= 언어별 레벨 미확정)도 여기서 얻는다(추가 DB 비용 0, D11).
    setup = await svc.run_db(
        db_session_factory,
        # ⭐ 이어하기면 체인의 call_id 를 넘긴다 — 선별이 "이 통화에서 이미 다뤘나"를
        #   라벨로 붙일 수 있게(조각들이 같은 행을 쓰므로 이 하나로 정확히 표현된다).
        #   ⚠ 아직 검증 전 값이다. 남의 id 를 넣어도 무해하다 — `last_call_id` 는 **이 회원의**
        #     progress 행에 있는 값이라, 그 회원이 실제로 그 통화를 하지 않았으면 안 맞는다.
        lambda db: svc.load_call_setup(
            db, member_id, character_id, spec.code,
            chain_call_id=continues_call_id, assignment_id=assignment_id,
        ),
    )
    # 읽기 쪽 방어: 저장 시 정규화(MemberService)를 넣었지만, 과거 데이터·다른 경로로 들어온
    # "ko-KR" 이 남아 있으면 _LOCALE_LABEL 조회가 미스나 **영어로 폴백**한다(실측 3건).
    locale = normalize_locale(locale_override or setup["locale"]) or setup["locale"]

    # 콜타입 라우팅(D11): ① 클라 명시 — 단 아래 1건은 normal 로 강등 ② 서버 자동.
    #   강등) 레벨테스트 미지원 언어(spec.leveltest=False, 예: 회화 전용 신 언어):
    #         그 언어 루브릭/대본이 없어 판정이 무의미 → 명시여도 level_test 금지.
    # 자동: 레벨테스트 지원 언어(spec.leveltest) + 레벨 미확정일 때만 level_test.
    #
    # 🧒 여기서 "강등"은 **이번 통화의 종류**를 level_test → normal 로 돌린다는 뜻이다.
    #   학습자 레벨(member_language_level·korean_level)은 전혀 건드리지 않는다.
    #
    # ⛔ 레벨 재측정을 ENV 로 막지 마라. 옛날엔 "prod && 레벨 보유자면 강등"이 있었는데
    #   전부 틀린 전제였다 — ① 실서비스(app-api)조차 ENV="test" 라 그 분기는 애초에 안
    #   걸렸고 ② 환경마다 동작이 갈리면 **테스트한 경로와 배포된 경로가 달라진다**
    #   (학습 언어 버그가 정확히 그렇게 살아남았다) ③ 재측정 허용 여부는 제품 규칙이지
    #   서버가 어디 떠 있느냐의 문제가 아니다. 레벨 재측정은 환경과 무관하게 허용한다.
    #
    # ⭐ 2026-09-10: 코스 2종(expression·freetalk)이 늘었다. 둘은 **명시로만** 들어온다 —
    #   홈 화면 버튼이 정하는 것이지 서버가 추측할 값이 아니다. 그래서 아래 자동 라우팅
    #   (else 절)은 한 글자도 안 바꿨다.
    if call_type_override is not None:
        call_type = call_type_override
        # ⛔ 표현학습은 **커리큘럼이 있는 언어**에서만 성립한다 — 가르칠 항목이 커리큘럼에서
        #   나오기 때문이다(`pick_expression_items`). 회화 전용 신 언어에서 명시로 들어오면
        #   항목 0개로 «표현 목록이 빈 표현학습» 이 되므로 normal 로 강등한다(레벨테스트
        #   미지원 언어와 **같은 규율**). ⚠ freetalk 은 항목을 안 쓰므로 이 게이트가 없다.
        if call_type == "expression" and not spec.has_curriculum:
            logger.warning(
                "normalcall: 커리큘럼 없는 언어(target=%s)에서 call_type=expression 명시 "
                "→ normal 강등(가르칠 항목이 0개다) member=%s", spec.code, member_id,
            )
            call_type = "normal"
        if call_type == "level_test" and not spec.leveltest:
            logger.warning(
                "normalcall: 레벨테스트 미지원 언어(target=%s) 통화에서 call_type=level_test 명시 "
                "→ normal 강등(루브릭·대본 부재 판정 오염 방지) member=%s", spec.code, member_id,
            )
            call_type = "normal"
    else:
        call_type = "level_test" if (spec.leveltest and setup["needs_level_test"]) else "normal"

    # ── 일일 통화 한도 ─────────────────────────────────────────────────── #
    # 콜타입별로 따로 센다 — 레벨테스트를 썼어도 일반 통화 1회가 남는다.
    #
    # 여기서 막는 이유(위치가 중요): 콜타입 라우팅 **직후**라 한도를 콜타입별로 판정할 수
    # 있고, create_call(통화 행)·Live 세션 open 이 **모두 아래**라 거절해도 잔여물도
    # Gemini 비용도 0이다. 클라 게이팅은 우회되므로 서버가 거절해야 한다.
    # 근거: docs/20260729_1243_일일-통화-한도-서버-거절.md
    tz_offset_min = start.tz_offset_min or 0
    # ⛔⛔ **이어하기는 한도 검사를 건너뛴다** — 안 그러면 15분 체인이 조각1에서 죽는다.
    #
    #   한도 판정은 «오늘 성립한 통화가 하나라도 있나»(EXISTS)이고, 성립 기준이
    #   «학습자가 한 번이라도 말했나» 다(`has_call_in_window`). 그러면:
    #       조각1  5분 통화 → 학습자가 말함 → "오늘 통화함" 성립
    #       조각2  접속     → "이미 통화했네" → 거절   ⛔
    #   **체인 전체가 1통화** 인데(사장님 결정 2026-08-19: "pro랑 max는 체인으로 15분
    #   연달아서가 1통화") 조각1이 그 1통화를 다 써 버린다. 광고는 15분인데 5분에 끊긴다.
    #
    #   ⭐ 남용은 **조각 상한이 막는다.** `resume_call` 이 `fragment_count >= max_fragments`
    #     면 거절하므로(Free 는 1이라 아예 못 잇는다), 이어하기를 무한 반복할 수 없다.
    #     ⇒ 여기서 건너뛰어도 «하루 1통화» 는 조각 상한과 함께 그대로 성립한다.
    #
    #   ⚠ `continues_call_id` 는 아직 **검증 전**이다(본인 통화인지·상한인지는 아래
    #     `resume_call` 이 본다). 남의 id 를 들고 와 한도를 우회하는 것처럼 보이지만,
    #     그 경우 `resume_call` 이 거절해 `call_id is None` 이 되고 **새 통화로 폴백**하는데
    #     그 폴백 경로는 이 검사를 이미 지난 뒤다.
    #     ⇒ 이 구멍을 막으려면 검증을 한도 검사보다 **앞으로** 옮겨야 하는데, 그건 DB 왕복이
    #       하나 더 늘고 거절 경로가 둘로 갈린다. 이득이 «남의 call_id 를 아는 사람이
    #       하루 한 번 더 통화한다» 뿐이라 지금은 안 한다. 나중에 새면 그때 옮긴다.
    if continues_call_id is None and await svc.run_db(
        db_session_factory,
        lambda db: call_service.is_daily_limit_reached(
            db, member_id, call_type, tz_offset_min
        ),
    ):
        logger.info(
            "normalcall: 일일 한도 초과 거절 member=%s call_type=%s tz=%s",
            member_id, call_type, tz_offset_min,
        )
        with contextlib.suppress(Exception):
            await _send_json(client_ws, ServerError(
                code="DAILY_LIMIT",
                message={
                    "level_test": "오늘의 레벨테스트를 이미 사용했어요.",
                    "expression": "오늘의 표현학습을 이미 사용했어요.",
                    "freetalk": "오늘의 프리토킹을 이미 사용했어요.",
                }.get(call_type, "오늘의 통화를 이미 사용했어요."),
                recoverable=False,
            ))
        return

    teaching_items: list[TeachingItem] = []  # P2.5 teaching_plan(normal + 재료 있을 때만)
    # ⭐ 표현학습이 이번 통화에서 다룰 표현(선별 결과). 다른 콜타입에서는 **빈 리스트**이고,
    #   그 빈/참이 곧 «이 통화가 표현학습인가» 의 런타임 게이트가 된다(state.expr_items).
    expr_items: list[dict] = []
    reground_reminder: str | None = None  # 일반 통화만 세팅(레벨테스트는 재접지 안 함)
    continue_reminder: str | None = None   # 후반 재접지(대화 지속) — 일반 통화만
    # 이 통화 전용 종료 태그(난수). ⚠ system_instruction 과 종료 시드가 **같은 값**을 써야
    # 한다 — 어긋나면 비버가 종료 신호를 못 알아보고 작별 없이 백스톱으로 끊긴다.
    close_tag = new_close_tag()
    # ⭐ 플랜에서 고를 Live 모델. **두 분기 앞에서** 선언한다 — 레벨테스트 경로는 아래
    #   플랜 분기를 안 타므로, 여기 없으면 state 대입에서 UnboundLocalError 가 난다.
    #   ⚠ None 이면 어댑터가 `settings.GEMINI_LIVE_MODEL` 로 떨어진다(종전 동작 그대로).
    live_model: str | None = None
    # ⭐ [live_model] 과 **같은 이유로** 분기 앞에서 잡는다 — 레벨테스트는 플랜 분기를
    #   안 타므로 여기 없으면 아래 tool 게이트에서 UnboundLocalError 가 난다.
    #   ⚠ 기본 False(=음성통화)다. 모르면 **도구를 안 싣는 쪽**이 안전하다 —
    #     싣는 쪽으로 틀리면 2.5 세션이 1011 로 죽는다(아래 :1625 주석).
    wants_video: bool = False
    # ⭐ [live_model]·[wants_video] 와 **같은 이유로** 분기 앞에서 잡는다.
    #   ⚠ None = 전역 `USE_VERTEX` 그대로(종전 동작). 레벨테스트는 사장님 결정으로
    #     AI Studio 3.1 을 쓰므로 아래 :level_test 분기에서 명시로 덮는다.
    live_vertex: bool | None = None
    if call_type == "level_test":
        # 레벨테스트 대본 — 레벨/이력 슬롯 없는 전용 셋업(회원당 사실상 1회라 재조회 비용 수용).
        lt_setup = await svc.run_db(
            db_session_factory, lambda db: svc.load_level_test_setup(db, member_id, character_id)
        )
        system_instruction = build_leveltest_instruction(
            role=lt_setup["role"],
            personality=lt_setup["personality"],
            locale=locale,
            interests=lt_setup["interests"],
            name=lt_setup["name"],
            target_language=target_language,
            close_tag=close_tag,
        )
        # Phase 1(주입 기계 제거): 서버가 질문을 주입하지 않는다. 비버가 첫 질문을 자유롭게
        # 시작하도록 오프닝 시드만 던진다(사다리 부트스트랩 없음 — 이중발화·마커낭독 소멸).
        seed_text = seed_leveltest_opening(target_language)
        voice = lt_setup["voice"]
        # ⭐⭐ **레벨테스트는 AI Studio 3.1 로 명시한다**(2026-09-08 사장님 결정).
        #   ⛔ 예전엔 아무것도 안 정해 `settings.GEMINI_LIVE_MODEL` 폴백으로 흘렀는데,
        #     그 값은 **AI Studio 이름**이다. 통화 백엔드가 플랜별로 갈리면서 전역
        #     `USE_VERTEX` 를 켜는 날이 오면 「Vertex 클라이언트 + AI Studio 모델명」이
        #     되어 **1008 로 죽는다** — 2026-09-06 Free·Pro 2주 장애와 같은 모양이다.
        #     레벨테스트는 플랜 분기를 안 타므로 아무도 못 보고 지나간다. 그래서 못박는다.
        #   ⚠ 3.1 인 이유: 레벨테스트는 학습자 발화를 판정하는 통화라 응답 지연·안정성이
        #     곧 측정 품질이다(2.5 는 루프 7%·1011 관측). 원가보다 정확도가 먼저다.
        _lt_client = getattr(
            getattr(getattr(client_ws, "app", None), "state", None),
            "genai_client_studio", None,
        )
        if _lt_client is not None:
            client, live_vertex = _lt_client, False
            live_model = settings.LIVE_MODEL_VIDEO or settings.GEMINI_LIVE_MODEL
        logger.info(
            "normalcall 레벨테스트: 백엔드=studio 모델=%s (명시 고정)",
            live_model or settings.GEMINI_LIVE_MODEL,
        )
    else:
        # 커리큘럼 없는 언어(spec.has_curriculum=False, 회화 전용)는 레벨 프로파일·체크판
        # 재료를 주입하지 않는다(무의미). ko 는 has_curriculum=True 라 기존 경로 그대로.
        inject_materials = spec.has_curriculum
        level_profile = setup["level_profile"] if inject_materials else ""
        # ⭐⭐ **플랜이 통화 종류를 정한다**(2026-09-04 사장님 지시).
        #   Max → 영상통화(표정 O · 3.1) / Free·Pro → 음성통화(표정 X · 2.5)
        #   ⛔ 판정은 `effective_plan` 을 쓰는 두 함수 하나씩으로만 간다 — 상태(state)와
        #     플랜(plan)은 다른 축이라(grace/on_hold/ending 은 직전 플랜을 유지) 상태
        #     문자열을 직접 보면 앱(`impliedTier`)과 판정이 갈린다.
        #   ⚠ 실패는 Free 로 떨어진다(R5) — 모르면 싸게 주는 편이 안전하다.
        # ⛔ DB 는 이 자리에 `db` 로 없다 — 이 함수는 동기 세션을 직접 안 들고
        #   `svc.run_db(db_session_factory, lambda db: ...)` 로 스레드풀에 넘긴다
        #   (위 resolve_call_character·load_call_setup 과 같은 규약). 안 지키면 NameError 다.
        # ⚠ 한 번의 run_db 로 **둘을 같이** 읽는다 — 두 번 부르면 그 사이 구독이 바뀔 때
        #   «영상은 주는데 모델은 음성» 같은 어긋난 조합이 나온다.
        wants_video, (live_backend, live_model) = await svc.run_db(
            db_session_factory,
            lambda db: (
                call_service.call_video_for(db, member_id),
                # ⭐⭐ **백엔드와 모델을 한 함수에서 같이 고른다**(2026-09-08).
                #   따로 물으면 어긋나고, 어긋나면 1008 로 통화가 통째로 죽는다 —
                #   2026-09-06 demo-api 가 USE_VERTEX 만 뒤집고 이름을 안 바꿔 2주 죽었다.
                call_service.live_engine_for(db, member_id),
            ),
        )
        # ⭐ 그 백엔드의 클라이언트로 갈아탄다. 없으면 **모델까지 같이 되돌린다**(R5) —
        #   클라이언트만 되돌리고 이름을 두면 그게 정확히 9/6 사고의 재현이다.
        _picked = getattr(
            getattr(getattr(client_ws, "app", None), "state", None),
            "genai_client_%s" % live_backend, None,
        )
        if _picked is not None:
            client, live_vertex = _picked, (live_backend == call_service.BACKEND_VERTEX)
        else:
            live_vertex = None
            fallback = (settings.LIVE_MODEL_VIDEO if wants_video
                        else settings.LIVE_MODEL_VOICE) or settings.GEMINI_LIVE_MODEL
            if fallback != live_model:
                logger.warning(
                    "normalcall 플랜분기: %s 클라이언트 없음 → 기본 클라이언트로 되돌린다"
                    " (모델도 %s → %s)", live_backend, live_model, fallback,
                )
                live_model = fallback
        # ⛔ `state` 는 **아직 없다**(1457행에서 만들어진다). 여기선 지역변수로 들고
        #   있다가 state 가 생긴 뒤에 싣는다 — 여기서 대입하면 UnboundLocalError 다.
        logger.info(
            "normalcall 플랜분기: 영상=%s 백엔드=%s 모델=%s (표정차단기=%s)",
            wants_video, live_backend, live_model, settings.LIVE_FACE_SPIKE,
        )
        # ⭐⭐ **여기서부터 코스별로 갈린다.** 위 플랜 분기(영상·백엔드·모델)는 세 코스가
        #   그대로 **공유**한다 — 표현학습·프리토킹도 Max 면 영상, Free·Pro 면 음성이다.
        #   ⛔ 레벨테스트만 이 블록 밖에 있다(자기 백엔드를 명시로 고정한다).
        if call_type == "expression":
            # ⚠ 표현학습이 더하는 DB 왕복은 **이 선별 1회**뿐이다 — 캐릭터·레벨 프로파일·
            #   흥미는 위 `setup`(load_call_setup)이 이미 갖고 있다.
            # ⚠ 레벨 미확정이면 2(Basic A)로 폴백한다 — load_call_setup 의 폴백과 같은 값.
            expr_items = await svc.run_db(
                db_session_factory,
                lambda db: svc.load_expression_items(
                    db, member_id, setup.get("korean_level") or 2, locale, spec.code,
                ),
            )
            system_instruction = build_expression_instruction(
                role=setup["role"],
                personality=setup["personality"],
                level_profile=level_profile,
                locale=locale,
                interests=setup["interests"],
                name=setup["name"],
                items=expr_items,
                quiz_group=svc.EXPRESSION_QUIZ_GROUP,
                target_language=target_language,
                close_tag=close_tag,
                # ⭐ T21-B — 모델은 위 플랜 분기(live_engine_for)가 고른 그대로다. 여기선 그 이름에 "3.1" 이 들었는지
                #   **하나**만 본다 — 대본의 «[3.1 말투]» 블록 유무가 갈릴 뿐, 모델 선택 로직은 건드리지 않는다.
                model_family="3.1" if "3.1" in (live_model or "") else "2.5",
            )
            seed_text = seed_expression_opening(target_language)
            logger.info(
                "normalcall 표현학습: 표현 %d개 선별(퀴즈 %d개마다) level=%s",
                len(expr_items), svc.EXPRESSION_QUIZ_GROUP, setup.get("korean_level"),
            )
        elif call_type == "freetalk":
            # ⛔ 학습 항목을 **하나도** 싣지 않는다(D8). 주제 선정·배운 표현 활용은 미기획이라
            #   페르소나와 캐릭터로만 대화한다. 레벨 프로파일은 싣는다 — 항목은 안 줘도
            #   «어느 난이도로 말할지»는 알아야 초보에게 고급 문장이 안 나간다.
            system_instruction = build_freetalk_instruction(
                role=setup["role"],
                personality=setup["personality"],
                level_profile=level_profile,
                locale=locale,
                interests=setup["interests"],
                name=setup["name"],
                target_language=target_language,
                close_tag=close_tag,
            )
            seed_text = seed_freetalk_opening(target_language)
        else:
            system_instruction = build_system_instruction(
                role=setup["role"],
                personality=setup["personality"],
                level_profile=level_profile,
                locale=locale,
                interests=setup["interests"],
                name=setup["name"],
                history=setup["history"],
                target_language=target_language,
                study_items=_prompt_study_items(setup.get("study_items")) if inject_materials else None,
                known_items=setup.get("known_items") if inject_materials else None,
                recent_topics=setup.get("recent_topics") if inject_materials else None,
                promotion_notice=bool(setup.get("promotion_notice")) and inject_materials,
                lang_band=setup.get("lang_band", "beginner"),
                close_tag=close_tag,
                # ⭐⭐ **플랜이 표정을 정한다**(2026-09-04). Max=영상통화(표정 O) /
                #   Free·Pro=음성통화(표정 X). 판정은 `call_service.call_video_for` 하나로 간다
                #   — 상태(state)와 플랜(plan)은 다른 축이라 상태 문자열을 직접 보면 앱과 갈라진다.
                #
                #   ⛔ `LIVE_FACE_SPIKE` 는 이제 **비상 차단기**다. 켜져 있어도 플랜이 아니면
                #     안 준다. 반대로 끄면 Max 도 못 받는다(사고 시 전원 차단용).
                #     ⚠ 의미가 바뀌었다 — 예전엔 "표정 기능 자체의 on/off" 였다.
                #   ⚠ 표정을 빼면 지시문이 1,058자(≈423토큰) 준다(실측). Live 는 매 턴 전액
                #     재과금이라 20메시지 통화면 그 몫만 ≈8,460 토큰이다(통화 1289 기준).
                face_tool=bool(settings.LIVE_FACE_SPIKE) and wants_video,
            )
            seed_text = seed_opening(target_language)
        voice = setup["voice"]
        # 재접지 리마인더(일반 통화 + REGROUND_MODE != "off"). 통합 재접지는 캐릭터 3필드에
        # 맥락 슬롯을 얹어 조립하므로(build_reground_brief) 페르소나 원재료를 그대로 넘긴다.
        # 아래 두 문자열은 하위호환(legacy 문구 · 기존 테스트 계약)용으로 계속 만든다.
        if REGROUND_MODE != "off":
            reground_reminder = build_reground_reminder(setup["role"], setup["personality"])
            continue_reminder = build_continue_reminder(setup["role"], setup["personality"])
        # P2.5: 학습 카드용 teaching_plan — 프롬프트 주입(study_items)과 단일 소스.
        # ⛔ **normal 에만 보낸다**(2026-09-10). `setup["study_items"]` 는 `pick_study_items`
        #   가 고른 «일반 통화의 오늘 항목» 이라, 표현학습·프리토킹에 그대로 보내면 화면에
        #   **이번 통화에서 다루지 않을 카드**가 뜬다. 표현학습의 카드는 자기 선별 결과로
        #   따로 보내야 하고, 그건 결과 화면과 함께 뒤에서 다룬다(T12).
        if inject_materials and setup.get("study_items") and call_type == "normal":
            teaching_items = _teaching_plan_items(setup["study_items"])

    # 3) 통화 행 — ⭐ **이어하기면 새로 만들지 않고 그 행에 계속 쓴다**(2026-08-19).
    #    행이 하나면 통화 목록·통화후 분석·발음 점수·일일 한도가 저절로 하나로 묶인다.
    #    ⚠ 검증(본인 통화·TTL 5분·조각 상한)은 서비스가 한다. 어긋나면 None 을 돌려주고
    #      여기서 **새 통화로 폴백**한다 — 이어하기 실패가 통화 실패가 되면 안 된다.
    call_id = None
    resume_reason = ""
    resumed = False
    # ⚠ 여기 목록과 `svc.resume_call` 의 화이트리스트는 **같은 뜻이어야 한다.** 한쪽만
    #   넓히면 «관문은 통과했는데 서비스가 거절» 이 되어 조용히 새 통화로 떨어진다.
    #   ⛔ 레벨테스트는 양쪽 모두에서 빠져 있다(조각 개념 없음 — 3분 하드캡은 측정 설계다).
    if continues_call_id is not None and call_type in ("normal", "expression", "freetalk"):
        max_fragments = await svc.run_db(
            db_session_factory,
            lambda db: call_service.call_fragments_for_member(db, member_id),
        )
        call_id, resume_reason = await svc.run_db(
            db_session_factory,
            lambda db: svc.resume_call(
                db, member_id, continues_call_id, max_fragments=max_fragments
            ),
        )
        resumed = call_id is not None
        logger.info(
            "normalcall 이어하기: continues=%s → %s (%s)",
            continues_call_id, "call_id=%d" % call_id if resumed else "새 통화로 폴백",
            resume_reason,
        )
    if call_id is None:
        call_id = await svc.run_db(
            db_session_factory,
            lambda db: svc.create_call(
                db, member_id, character_id, call_type, target_language=spec.code
            ),
        )

    # 통화 화면 아바타를 대화 상대와 맞추라고 알려준다(구버전 앱은 무시 → 기존 동작).
    # ⭐ `call_id` 를 같이 싣는다 — 클라가 이어하기에 쓸 번호다. `call_ended` 에만 있으면
    #   끊기 버튼(소켓 선(先)종료)에서 그 프레임이 도착하지 않아 번호를 영영 못 받는다.
    await _send_json(
        client_ws,
        ServerCallStarted(
            character_id=character_id,
            call_id=str(call_id),
            # ⛔ 이 값을 안 실으면 클라는 자기 기본값으로 돈다 — 그러면 "끄는 스위치가
            #   서버에 있다"는 말이 거짓이 된다. 필드만 만들어 두고 아무도 안 채우던
            #   상태를 여기서 닫는다(2026-08-25).
            diag=settings.LIVE_DIAG_LEVEL,
        ),
    )

    state = _CallState()
    # ⭐ 플랜에서 고른 모델을 state 에 싣는다(위에서 읽어 뒀다). 세션 팩토리와 usage 태그가
    #   이 값을 본다. ⚠ 레벨테스트 경로는 위 분기를 안 타므로 None 이고, 그러면
    #   어댑터가 `settings.GEMINI_LIVE_MODEL` 로 떨어진다(종전 동작).
    state.live_model = live_model
    state.live_vertex = live_vertex
    if resumed:
        # ⛔⛔ **턴 인덱스를 이어서 매긴다.** 0 부터 다시 매기면 조각2의 첫 턴이 조각1의
        #   첫 턴과 같은 번호가 되어 전사·증거 정렬이 통째로 어긋난다(같은 행에 쓰므로
        #   충돌이 조용히 난다 — 새 행이었으면 안 났을 사고다).
        state.next_turn_index = await svc.run_db(
            db_session_factory, lambda db: svc.next_turn_index(db, call_id)
        )
        # ⭐ 검증 범위의 기준. 이 턴 앞은 **이전 조각**이고, 그건 이미 그때 판정됐다.
        state.resume_from_turn = state.next_turn_index
        logger.info("normalcall 이어하기: 턴 인덱스 %d 부터 이어서 기록", state.next_turn_index)
    # ⭐⭐ **표현학습의 조각 승계는 브리프가 아니라 «시드 + 목록»이 한다**(D17).
    #   ⛔ 아래 `build_resume_brief` 를 타지 않는다 — 그건 «하던 대화를 이어가라» 이고,
    #     표현학습에 필요한 것은 «(통과) 표시가 없는 가장 앞 항목부터» 다. 그리고 그 정보는
    #     **이미 지시문 안에 있다**(선별이 통과분을 빼고 목록을 다시 만든다) — 사이드카도
    #     JSON 도 필요 없다. 서버가 정답을 갖고 있다.
    #   ⛔ 시드를 갈아야 한다. `seed_expression_opening` 은 «1번부터 시작해라» 라서 조각2에
    #     그대로 나가면 처음으로 되감는다 — **시드는 지시문을 이긴다**(실측 call 1087).
    if resumed and call_type == "expression":
        seed_text = seed_expression_resume(target_language)
        logger.info("normalcall 표현학습 이어하기: 표시 없는 가장 앞 항목부터 재개")
    elif resumed:
        # ⭐⭐ **브리프를 지시문에 얹는다** — 이게 없으면 비버가 처음 만난 것처럼 인사한다
        #   (call 870 의 재발). 사용자는 끊긴 걸 아는데 비버만 모르는 게 제일 어색하다.
        #   ⛔ "이어서 할게요" 를 시키지 않는다 — 그러면 끊김이 두 번 일어난다.
        #     브리프 마지막 줄이 **첫 행동을 지정**한다(금지가 아니라 지정).
        try:
            mats = await svc.run_db(
                db_session_factory, lambda db: svc.resume_materials(db, call_id, spec.code)
            )
            # ⭐⭐ **슬롯이 아직 없으면 지금 만든다**(2026-08-19 실측 사고).
            #   조각 종료 시점 생성은 fire-and-forget 이라 이어하기가 그걸 **앞지른다**:
            #     07:00:48 저장 → 07:00:51 이어하기(슬롯 없음) → 07:00:53 요약 완성
            #   그때 발췌 폴백을 탔고, 짧은 통화라 **비버의 첫 인사가 발췌 맨 앞**에 왔다.
            #   ⇒ 비버가 그걸 요약이 아니라 **대본으로 읽어** 글자까지 똑같이 다시 인사했다
            #     (t10 == t1). 원문을 주면 따라 한다 — 그게 원문 덤프의 진짜 위험이다.
            #   ⚠ 여기서 LLM 을 한 번 더 부르지만 **체감 지연은 0에 가깝다**: Live 세션을
            #     여는 데만 2초가 걸리고(실측 07:00:51→07:00:53) 요약은 thinking 0 · 짧은
            #     전사라 그보다 빠르다.
            if not mats.get("topic") and not mats.get("facts") and client is not None:
                tail = await svc.run_db(
                    db_session_factory, lambda db: svc._resume_transcript(db, call_id)
                )
                slots = await svc.summarize_for_resume_text(
                    client, settings.JUDGE_MODEL, tail
                )
                if slots:
                    mats["topic"] = slots.get("topic") or None
                    mats["facts"] = slots.get("learner_facts") or None
                    mats["pending"] = slots.get("pending") or None
                    mats["excerpt"] = None      # ⛔ 슬롯이 생겼으면 발췌는 안 보낸다
                    await svc.run_db(
                        db_session_factory,
                        lambda db: svc._save_resume_context(db, call_id, slots),
                    )
                    logger.info(
                        "normalcall 이어하기 요약(즉석): 화제=%r 사실 %d개",
                        slots.get("topic"), len(slots.get("learner_facts") or []),
                    )
            brief = build_resume_brief(**mats)
            # ⛔⛔ **시드를 갈아야 한다 — 지시문만으로는 안 진다**(2026-08-19 실측 call 1087).
            #   `seed_opening` 은 "짧게 인사부터 하고, 오늘 공부할래 수다 떨래?를 물어라" 다.
            #   조각2 에서 그게 그대로 나가자 비버가 방금 하던 대화를 버리고 **처음으로
            #   돌아갔다**(t8 이 t1 과 같은 질문). 브리프에 "인사하지 마라"가 있어도 소용없다 —
            #   **시드는 직접 명령이고 지시문은 배경**이라 시드가 이긴다.
            #   ⚠ 브리프 유무와 무관하게 간다: 브리프가 비어도 "다시 묻기"는 막아야 한다.
            seed_text = seed_resume(target_language)
            if brief:
                system_instruction = system_instruction + "\n\n" + brief
                # ⚠ `사실`·`하던것`·`발췌` 를 같이 찍는다(2026-08-19). 전에는 DB 기반 셋만
                #   찍어서, 요약이 `사실 3개` 를 만들었는데 브리프 로그는 `다룬 0 · 잘함 0 ·
                #   헷갈림 0` 이라 **아무것도 안 들어간 것처럼 보였다.** 관측 구멍이었다.
                #   ⭐ `발췌` 는 폴백을 탔는지를 가른다 — 붙어 있으면 요약이 없었다는 뜻이다.
                logger.info(
                    "normalcall 이어하기 브리프: 다룬 %d · 잘함 %d · 헷갈림 %d · 사실 %d · "
                    "화제=%s · 하던것=%s%s",
                    len(mats["covered"]), len(mats["strong"]), len(mats["weak"]),
                    len(mats.get("facts") or []),
                    (mats["topic"] or "")[:30] or "없음",
                    (mats.get("pending") or "")[:30] or "없음",
                    " · ⚠발췌폴백" if mats.get("excerpt") else "",
                )
        except Exception as exc:   # noqa: BLE001 — 브리프 실패가 통화를 막으면 안 된다(R5)
            logger.warning("normalcall 이어하기 브리프 실패(맥락 없이 진행) — %s", exc)
    # 통화 길이: 데모/dev 는 클라가 3~15분 지정 가능(prod 무시). _watch_call_clock 이 참조.
    state.close_seed = _close_seed(close_tag)  # 지시문과 같은 난수 태그로 재조립
    if settings.LIVE_INPUT_LANGUAGE_CODES:
        state.input_language_codes = _input_language_codes(spec.code, locale)
    # ⭐ 표현학습 진도를 state 에 싣는다 — 이 리스트가 비어 있지 않다는 것 자체가
    #   «이 통화는 표현학습» 의 런타임 게이트다(별도 플래그 없음).
    state.expr_items = expr_items
    # 커리큘럼 표기의 대괄호(«있어요[없어요]»)를 제어 태그로 오인하지 않게 — 이 통화 항목에서 온 조각만.
    state.expr_tag_allow = _expression_bracket_allowlist(expr_items)
    if expr_items:
        # ⭐ T16 — STT 폴백 판정기 재료. 서버 검색이 미판정으로 남긴 항목에만 LLM 을 부른다.
        #   ⚠ 힌트 사이드카와 같은 모델(JUDGE_MODEL)·같은 usage 그릇을 쓴다 — 원가가 한
        #     칸에 모여야 «표현학습이 얼마나 더 드는가» 를 나중에 잴 수 있다.
        #   쪽지(_build_expression_note)·큐 문구도 여기 라벨을 쓴다 — state 에 따로 칸을 안 판다.
        state.expr_ctx = {
            "client": client,
            "model": settings.JUDGE_MODEL,
            "locale_label": _LOCALE_LABEL.get(locale) or _LOCALE_LABEL["en"],
            "target_language": target_language,
        }
    state.reground_reminder = reground_reminder  # 일반 통화만 값 있음(첫 arm 전 기본 문구)
    state.continue_reminder = continue_reminder  # 하위호환(legacy 문구)
    if call_type != "level_test" and REGROUND_MODE != "off":
        # 재접지 통합(단계 3): 문구 조립 재료 + 사이드카에 번호로 떠먹일 항목 목록 + 모드 시드.
        # ⛔ 모드는 여기서 서버가 정하고 이후 sticky 다 — 사이드카 제안은 인용 검증을 통과해야
        #   바뀐다(_apply_mode_proposal). 학습 재료가 있으면 공부, 없으면 대화.
        state.reground_persona = (setup["role"] or "", setup["personality"] or "")
        if call_type in ("expression", "freetalk"):
            # ⛔⛔ **두 코스는 normal 의 학습 항목 기계를 물려받지 않는다**(2026-09-10 QA).
            #   게이트를 `expr_items` 로 두면 두 경우가 아래 else 로 떨어진다:
            #     · 프리토킹 — 항목이 애초에 0개인데(D8) `study_items[:10]` 을 물고
            #       call_mode='study' 가 된다 ⇒ 재접지 쪽지가 «학습 항목을 대화에서 쓰게
            #       하라» 로 나가는데 지시문엔 그 항목이 **하나도 없다.**
            #     · 표현학습 — 그 레벨을 전량 통과해 풀이 비면 «표현 0개짜리 표현학습» 이
            #       normal 항목을 물고 돈다.
            #   ⇒ 판정은 **콜타입**으로 한다. 항목 유무는 그 다음 문제다.
            # ⭐ 표현학습: 검출 목록이 곧 **오늘의 표현 전량**이다.
            # ⛔⛔ **여기서 [:10] 로 자르지 마라.** 자르면 11~18번이 «비버가 다뤄도 서버는
            #   모르는» 항목이 되고, 그건 영원히 «안 가르친 것» 으로 남아 다음 통화에 또
            #   나온다(그리고 재접지 쪽지가 «이미 다룬 것» 에서 빠뜨려 되감기를 부른다 —
            #   통화 1360 이 정확히 부분 목록 때문에 난 사고다).
            # ⚠ `REGROUND_COVERED_CAP`(10)은 **쪽지에 몇 개를 적을지**의 상한이지 검출
            #   상한이 아니다 — 검출은 18개 전부 돈다. 두 숫자를 섞지 마라.
            state.reground_items = [str(it["obj"]) for it in expr_items if it.get("obj")]
            # ⚠ 항목이 0개면(프리토킹 · 전량 통과한 표현학습) 'chat' 이다 — 'study' 로
            #   굳히면 쪽지가 없는 항목을 가리킨다.
            state.call_mode = "study" if state.reground_items else "chat"
        else:
            # ⚠ `normal` 은 **한 글자도 안 바뀐다** — 상한 10 은 일반 통화의 계약이다
            #   (공급원 study_items 가 본편 5 + 예비라 10 이면 사실상 전량이다).
            state.reground_items = [
                str(it.get("obj")) for it in (setup.get("study_items") or []) if it.get("obj")
            ][:10]
            state.call_mode = "study" if state.reground_items else "chat"
        state.reground_ctx = {
            "client": client,
            "model": settings.JUDGE_MODEL,
            "instruction": _reground_instruction(state.reground_items, target_language),
        }
    # Phase 1: 레벨테스트도 in-band tool 을 쓰지 않는다(인-콜 판정 없음 — 종료는 3분캡/무음).
    # 따라서 tools=None(일반 통화와 동일 — 세션 팩토리 시그니처 무손상).
    live_tools = None
    # ⭐ 표정 계측 스파이크 — 이 스위치가 켜진 통화만 `set_face` 를 받는다(2026-08-18).
    #   ⛔ 기능이 아니다. 클라로 아무것도 안 보내고 화면도 안 바뀐다 — **로그만** 남긴다.
    #   ⚠ 이게 `live_tools` 의 **첫 실사용**이다. 지금까지 항상 None 이라 "모델이 tool 을
    #     부른다"를 이 프로젝트에서 아무도 본 적이 없다. 그래서 재는 것이다.
    #   ⛔⛔ **레벨테스트에는 붙이지 마라**(2026-08-23). 이 분기가 `call_type` 검사 **밖**에
    #     있어서, 스위치를 켜면 레벨테스트 통화에도 tool 이 붙었다. 레벨테스트 지시문은
    #     2,111자라 "긴 지시문 + tool" 사망 구간(실측 2,697자 0/7)에 들어간다 — 즉 스위치를
    #     켜는 순간 레벨테스트가 통째로 죽는다. 지금까지 스위치가 off 라 잠복해 있었다.
    #     ⚠ 레벨테스트에 표정을 넣으려면 일반 통화와 같은 분할이 필요한데, 첫 2턴이 곧
    #       0단·1단 **측정 구간**이라 페르소나 미완성 구간이 그대로 측정 오염이 된다.
    #       일반 통화에서 검증한 뒤 별건으로 다룬다.
    # ⛔⛔ **`wants_video` 를 빼지 마라.** 2026-09-04 플랜 분기가 표정을 Max 전용으로
    #   가를 때 **지시문 쪽(:1457)만 고치고 여기를 안 고쳤다.** 그래서 Free·Pro(2.5)
    #   세션 setup 에도 `set_face` 선언이 실렸다 — 지시문은 표정을 안 시키니 사람 눈엔
    #   "표정 없음"으로 보이는데, 와이어에는 tool 이 나간다.
    #   ⇒ 그 조합이 2026-09-07 에 **Free·Pro 통화를 전부 죽였다**(1011 internal error,
    #     사용자 첫 발화 직후). 이 저장소가 이미 실측으로 못박아 둔 사망 조건이다:
    #       영문 무인자 5툴                        30/30 생존
    #       현행 SET_FACE_TOOL(한국어 설명+enum)    0/21 전부 1011   (CODEX_RESULT_1011.md)
    #       긴 지시문 + set_face 를 setup 에        0/8             (커밋 05462f8)
    #   ⚠ 두 조건이 **같은 값**이어야 한다 — 한쪽만 고치면 "지시문엔 없는데 tool 은 있는"
    #     이 상태로 조용히 돌아온다. 회귀 `test_plan_call_split.py` 가 이 자리를 잠근다.
    if settings.LIVE_FACE_SPIKE and wants_video and call_type != "level_test":
        live_tools = [SET_FACE_TOOL]
    if call_type == "level_test":
        # ⛔ 종료 소유권: 레벨테스트는 **언제나 서버**다(아래 워처 참조). 3분 하드캡은
        #   상품 혜택이 아니라 **측정 설계**라, 클라가 언제 닫든 서버가 캡에서 끝내야 한다.
        state.is_leveltest = True
        # T1: 3분 하드캡(base=LEVELTEST_MAX_S). 데모가 duration_min 을 주면 3~15분 클램프가
        # 우선(데모의 명시 선택) — prod/일반 경로는 이 값에 못 닿아 무영향. 워처·리그라운드·
        # 넛지는 이 한 값(state.call_duration_s)으로 흡수한다(무수정).
        state.call_duration_s = _resolve_call_duration(
            settings, duration_override, base=LEVELTEST_MAX_S
        )
        # 종료 시드 문자열만 교체(주입 파이프 불변). 태그는 지시문과 같은 난수 태그.
        state.close_seed = close_seed_leveltest(close_tag)
        # T3: 무음 캐던스 단축 + 1단 넛지 내용 전환(질문 재출제 유지).
        state.idle_nudge1_s = LEVELTEST_IDLE_NUDGE1_S
        state.idle_nudge2_s = LEVELTEST_IDLE_NUDGE2_S
        state.idle_close_s = LEVELTEST_IDLE_CLOSE_S
        state.nudge_seed_1 = _NUDGE_SEED_1_LEVELTEST
        # Phase 2: 종료 판정 사이드카 활성 — 매 유저 답변을 사이드카로 종료 판정만 하고(질문 주입 0)
        # 종료 트리거가 서면 종료 시드만 주입한다. band_client = 판정 사이드카가 쓸 genai.Client.
        state.band_observe = True
        state.band_client = client
        state.band_target_language = target_language  # (멀티랭귀지) 판정관 대상 언어
    else:
        # 일반 통화 길이 = 구독 플랜(Free 5분 / Pro·Max 15분). env 강제값이 있으면 그게
        # 이긴다 — dev/demo 에서 구독 없이 15분 경로를 밟기 위한 탈출구이고, 테스트가
        # monkeypatch 하는 지점도 여기다(값이 박히면 DB 조회 자체를 건너뛴다).
        if CALL_DURATION_S is not None:
            plan_duration_s = CALL_DURATION_S
        else:
            plan_duration_s = await svc.run_db(
                db_session_factory,
                lambda db: call_service.call_duration_s_for_member(db, member_id),
            )
        state.call_duration_s = _resolve_call_duration(
            settings, duration_override, base=plan_duration_s
        )
        # ⭐ **숙제 통화는 무조건 5분이다**(2026-09-04 사장님 지시).
        #
        #   플랜도 env 강제값도 클라 override 도 이기지 못한다. 교사가 낸 과제는 반
        #   전체가 같은 조건이어야 하고, 학습자마다 구독이 달라 통화 길이가 갈리면
        #   교사 화면의 수치끼리 비교가 성립하지 않는다.
        #   ⛔ 늘리지 마라. 늘리려면 반 단위 설정이 먼저 있어야 한다.
        #   ⚠ 절대 백스톱은 안 바뀐다 — max(540, 300+22+30) = 540 그대로다(R4 불변식).
        if assignment_id is not None:
            state.call_duration_s = HOMEWORK_CALL_DURATION_S
        # ⭐ 캐던스(60/+10/+12)는 세 코스가 **같다**(기획 §2-9 임계표) — 값은 일반과 같게
        #   두고 **1단 시드 문구만** 코스별로 가른다. 실통화로 재본 뒤 조정한다.
        #   ⛔ 표현학습에서 1단을 줄이고 싶어지면 레벨테스트 전례를 먼저 봐라 — 25초로
        #     줄였다가 "생각 중에 넛지가 끼어들었다"로 60초로 되돌렸다(LEVELTEST_IDLE_NUDGE1_S).
        #     드릴은 학습자가 문장을 떠올리는 시간이다.
        state.idle_nudge1_s = IDLE_NUDGE1_S
        state.idle_nudge2_s = IDLE_NUDGE2_S
        state.idle_close_s = IDLE_CLOSE_S
        # ⛔⛔ 표현학습에 일반 1단 시드("가볍게 새 화제로")를 쓰면 **그 항목을 건너뛴다.**
        #   여기서 무음은 «대화가 끊겼다»가 아니라 «학습자가 지금 항목을 못 하고 있다»다.
        #   ⚠ 2단·3단은 공통으로 둔다 — "거기 있어?"와 작별은 코스와 무관하다.
        state.nudge_seed_1 = {
            "expression": NUDGE_SEED_1_EXPRESSION,
            "freetalk": NUDGE_SEED_1_FREETALK,
        }.get(call_type, _NUDGE_SEED_1)

    # P2.5(D16) 동적 힌트 사이드카 활성 조건: 커리큘럼 있는 언어(ko) 전 통화(레벨테스트·일반,
    # 레벨 무관)에 힌트 제공. 회화 전용 언어(has_curriculum=False)는 제외 — 예시 답변 생성
    # 프롬프트가 그 언어 커리큘럼에 맞춰져 있지 않아 무의미(R5). 상세는 mechanics ⑬.
    # ⛔ **표현학습·프리토킹에는 힌트가 없다**(D7 — 사장님: 화면 UI 자체를 없앤다).
    #   ⚠ 여기서 끄는 것이 곧 «화면에 안 뜬다» 다 — 힌트는 서버가 push 해야만 보이므로
    #     ctx 를 안 만들면 사이드카도, ServerHint 프레임도, hint_used 강등도 전부 사라진다
    #     (`_spawn_hint_task` 가 ctx None 이면 즉시 되돌아간다).
    #   ⭐ 표현학습에서 힌트가 해로운 이유: 이 코스의 퀴즈는 «배운 표현을 맞히기» 라
    #     예시 답변을 띄우면 **정답을 그대로 보여주는 것**이 된다. 판정이 무의미해진다.
    #   ⚠ `normal`·`level_test` 는 종전 그대로다(hint_used 강등 경로 포함).
    enable_hints = spec.has_curriculum and call_type not in ("expression", "freetalk")
    if enable_hints:
        label = _LOCALE_LABEL.get(locale) or _LOCALE_LABEL["en"]
        state.hint_ctx = {
            "client": client,
            "model": settings.JUDGE_MODEL,
            "instruction": _hint_instruction(label, target_language),
            # 원가 계기판 — 힌트 사이드카는 state 를 안 받으므로 ctx 에 수집기를 실어 보낸다
            # (시그니처를 안 바꾼다). ⚠ 여러 힌트 태스크가 같은 객체에 더한다 — 단일
            # 이벤트루프라 GIL 밖 경합이 없다(락 불요).
            "usage": state.sidecar_usage,
        }

    logger.info(
        "normalcall 시작: member=%s character=%s locale=%s voice=%s call_type=%s call_id=%s "
        "hints=%s teaching_plan=%d",
        member_id, character_id, locale, voice, call_type, call_id,
        enable_hints, len(teaching_items),
    )

    # P2.5: teaching_plan 1회 push(mechanics ⑪) — 통화 시작 직후, 펌프(핫패스) 밖.
    # 데이터 없으면 미전송 = 기존 화면. 실패해도 통화는 계속(R5 — 카드만 미표시).
    if teaching_items:
        try:
            await _send_json(client_ws, ServerTeachingPlan(items=teaching_items))
        except Exception as exc:  # noqa: BLE001 - 카드 미표시일 뿐 통화 무영향
            logger.warning("normalcall: teaching_plan push 실패(무시): %s", exc)

    # 절대 백스톱: 기본은 ABSOLUTE_CALL_TIMEOUT_S(540s, 연결 ~10분 선점). 단 데모가 통화 길이를
    # 길게 잡으면(예: 15분) 이 상한이 시계보다 먼저 떨어져 통화를 잘라버린다 — 그래서 선택 길이
    # +마무리 여유를 하한으로 삼아 시계가 정상 종료할 시간을 준다. 짧은/기본 통화는 그대로 540s.
    # ⭐ 2026-08-04: 예전엔 "10분 초과 선택은 연결 한계로 GoAway 가 먼저 올 수 있다(감수)"였다.
    #   이제 세대 루프가 연결을 갈아끼우므로 통화 길이와 연결 수명이 분리됐다 — 연결 수명은
    #   지금은 세대가 하나뿐이라 통화 전체를 이 백스톱만이 지킨다. 없애면 안 된다:
    #   재연결이 생긴 뒤로는 버그 하나가 곧 무한 세션(= 무한 과금)이 될 수 있다.
    absolute_timeout = max(
        ABSOLUTE_CALL_TIMEOUT_S, state.call_duration_s + SEED_TO_HANGUP_S + 30.0
    )
    try:
        async with asyncio.timeout(absolute_timeout):
            await _run_session(
                client_ws,
                state=state,
                system_instruction=system_instruction,
                voice=voice or DEFAULT_VOICE,
                seed_text=seed_text,
                settings=settings,
                client=client,
                live_session_factory=factory,
                db_session_factory=db_session_factory,
                call_id=call_id,
                member_id=member_id,
                tools=live_tools,
            )
    except TimeoutError:
        # ⚠ 상수가 아니라 **실제 적용된** 상한을 찍는다 — 데모/장통화는 값이 다른데
        #   상수를 찍으면 로그가 거짓말을 한다(15분 통화에서 952s 인데 540s 로 보인다).
        logger.warning("normalcall 통화 상한(%.0fs) 초과 — 강제 종료", absolute_timeout)
    except _ClientDisconnect:
        logger.info("normalcall 클라 연결 종료")
    except _CallFinished:
        logger.info("normalcall 통화 정상 종료")
        if state.band_observe:
            logger.info(
                "normalcall: 레벨테스트 종료판정 사이드카 종료 total_answers=%d nonspeaker_streak=%d "
                "(통화후 판정관이 전사로 최종 확정)",
                state.total_answers, state.nonspeaker_streak,
            )
    except Exception as exc:  # noqa: BLE001 - 최종 방어선
        logger.exception("normalcall 브리지 오류: %s", exc)
    finally:
        # 🧒 왜 finally(통화후 파이프라인)인가: 통화는 여러 방식으로 끝난다 — 정상 작별
        #   (_CallFinished), 학습자가 앱을 꺼서 끊김(_ClientDisconnect), 시간 초과 강제 종료
        #   (TimeoutError), 예상 못 한 오류(Exception). 이 뒤처리(전사 저장·분석·오디오 업로드·
        #   국적 추론)는 **어떤 경로로 끝나든 딱 한 곳에서** 보장돼야 한다. try/except 마다
        #   중복으로 적으면 하나만 빠뜨려도 통화 기록이 유실된다. finally 는 위에서 무슨 일이
        #   있었든 반드시 실행되므로, 뒤처리를 여기 한 곳에 모아 '절대 빠지지 않게' 만든다.
        #   무거운 작업(분석·업로드·국적 추론)은 전부 fire-and-forget(띄워만 놓고 안 기다림)로
        #   백그라운드에 넘겨, 학습자 쪽 소켓을 붙잡지 않고 빠르게 통화를 마무리한다.
        # D16: 미완 힌트 태스크 전량 취소 — 통화가 끝났는데 늦은 힌트가 나가는 것 방지.
        for t in list(state.hint_tasks):
            t.cancel()
        # Phase 2: 미완 밴드 관측 사이드카 전량 취소(통화 종료 후 뒤늦은 관측·종료 시도 방지).
        for t in list(state.band_tasks):
            t.cancel()
        # 단계 3: 미완 재접지 브리프 사이드카 전량 취소(끝난 통화의 문구를 만들 이유가 없다).
        for t in list(state.reground_tasks):
            t.cancel()
        # ⚠ 퀴즈 판정 사이드카도 같이 취소한다. **DB 커밋보다 먼저** 취소되는 것이 맞다 —
        #   통화가 끝난 뒤 도착한 판정은 이번 통화 결과에 못 실리고(아래 커밋이 이미 지났다),
        #   그 항목은 미판정으로 남아 다음 통화에 다시 나온다(안전한 방향).
        for t in list(state.expr_tasks):
            t.cancel()
        _flush_user_segment(state)
        _flush_beaver_segment(state)
        # ⭐⭐ **표현학습 진도 커밋** — 이 통화가 어떻게 끝났든(정상 작별·끊김·백스톱·예외)
        #   여기 한 곳에서 딱 한 번 쓴다. finally 인 이유는 위 뒤처리와 같다.
        #   ⚠ 커밋이 실패해도 통화는 이미 끝났다 — 예외를 흡수하고 로그만 남긴다(R5).
        #     잃는 것은 이번 조각의 진도뿐이고, 그 항목은 다음 통화에 다시 나온다.
        # ⛔ 게이트가 `expr_items` 면 **레벨을 다 뗀 회원이 승급을 못 받는다** — 선별이 빈
        #   목록을 주는 그 상태가 정확히 «다 뗐다» 이기 때문이다. 콜타입으로 판정한다.
        if call_type == "expression" and call_id is not None:
            # ⭐⭐ **순서가 계약이다**(사장님 지시 2026-09-10):
            #     ① 마지막 판정 LLM  ② state 갱신  ③ DB 쓰기  ④ 조각2 가 DB 에서 뽑는다
            #   ⛔ ①을 빼면 **마지막 arm 이후 구간이 판정 없이 버려지고**, 그 구간에서 맞힌
            #     항목을 조각2 가 다시 가르친다.
            #   ⛔ 그렇다고 쓰기를 ①에 **걸지 않는다** — 상한(EXPR_FINAL_JUDGE_TIMEOUT_S) 안에 안 오면 있는 것만
            #     쓰고 진행한다. 이 함수는 예외를 밖으로 안 내보낸다(R5).
            await _final_expression_progress(state)
            try:
                # ⛔⛔ **표면형으로 되짚지 마라** — 같은 레벨에 동일 표면형이 91그룹 실재한다
                #   (assets/level/curriculum_v2/vocab.json 전수 스캔). surface set 으로 역매핑하면
                #   **두 항목에 함께** drilled/quiz_passed_at 이 찍히고, 그건 되돌릴 수 없다.
                drilled = _expr_covered_ids(state)
                drilled_set = set(drilled)
                # ⭐ 결과 화면 스냅샷 — **이 통화의 사실**이라 통화 행에 적는다.
                #   ⛔ 진도 행으로 되짚으면 나중 통화가 `drilled_call_id` 를 덮어써서
                #     지난 결과가 조용히 사라진다(재드릴은 선별상 정상 경로다).
                # ⛔ 대상은 **covered ∪ 통과분**이다. covered 만 보면, 사이드카가 어떤 항목을
                #   `passed` 에만 넣고 `drilled` 에 안 넣었을 때 **DB 엔 통과가 찍히는데 화면엔
                #   안 나온다**(지시문이 그 조합을 막지 않는다 — 실제로 일어날 수 있다).
                snapshot = _expression_result_snapshot(state, drilled_set)
                stats = await svc.run_db(
                    db_session_factory,
                    lambda db: svc.save_expression_progress(
                        db, member_id, call_id,
                        drilled_ids=drilled, passed_ids=sorted(state.expr_quiz_pass),
                        snapshot=snapshot, language=spec.code,
                    ),
                )
                logger.info(
                    "normalcall 표현학습 진도 저장: 드릴 %d · 통과 %d (오답 %d — 이 통화 한정)"
                    " · 승급 %s",
                    stats["drilled"], stats["passed"], len(state.expr_quiz_fail),
                    (stats.get("levelup") or {}).get("result", "판정없음"),
                )
            except Exception as exc:  # noqa: BLE001 - 진도 유실일 뿐 통화는 끝났다(R5)
                logger.warning("normalcall 표현학습 진도 저장 실패(무시): %s", exc)
        # 원가 계기판(Phase 0): 어떤 경로로 끝나든(정상 작별·클라 끊김·백스톱·예외) 딱 한 번
        # 방출한다. 로그가 통화후 파이프라인을 막지 않게 예외는 전량 흡수(R5).
        with contextlib.suppress(Exception):
            _log_usage_summary(state, call_id, call_type)
        # ⭐ 클라 계측이 실제로 왔는지 통화당 1줄. **0 이면 앱이 안 보낸 것**이고, 왔는데
        #   숫자가 이상하면 앱 문제다 — 그 둘을 가르는 유일한 줄이다.
        if state.diag_batches or state.diag_events:
            logger.info(
                "normalcall 계측요약: call_id=%s batches=%d events=%d dropped=%d",
                call_id, state.diag_batches, state.diag_events, state.diag_dropped,
            )
        # ⭐ 페르소나 주입 결과를 통화당 1줄로 남긴다. ⛔ 지우지 마라 — 주입 실패는 **조용한**
        #   품질 저하다. 이 줄이 없으면 아무도 모르고 "요즘 비버가 이상해"만 남는다.
        # 2단계(영속화): 로그는 30일이면 사라진다. 원가 추이를 계속 보려면 행에 남아야 한다.
        # 예외는 함수 안에서 흡수한다(R5) — 통화 기록·분석과 트랜잭션을 나눠 둔 이유.
        await _persist_usage(db_session_factory, state, call_id)
        # P2.6: 전사(텍스트) 선저장 — 오디오 MP3 변환·업로드(~9s)는 pending 으로 분리.
        pending_audio = await _persist_remaining(db_session_factory, state, call_id, member_id)
        # 분석 태스크를 먼저 생성(분석 우선 착수) → 오디오 업로드는 병렬 후행.
        _trigger_analysis(
            call_id, client, settings, db_session_factory, locale,
            target_language=target_language, locale_label=None,
            call_type=call_type, member_id=member_id,
            candidates=setup.get("candidates") if call_type == "normal" else None,
            # D16: 힌트 열람 마커(in-memory) — 크래시 유실 시 과크레딧 1회 허용.
            hinted_from_turn_index=set(state.hinted_next_turn_index) or None,
            # ⭐ 이어하기면 **이 조각이 시작한 턴**부터만 검증한다(근거는 analyze_call 주석).
            since_turn_index=state.resume_from_turn or None,
        )
        _trigger_audio_upload(db_session_factory, call_id, member_id, pending_audio)
        # 마지막 회수·해제(B1): 아직 안 놓아준 세그먼트의 PCM 을 여기서 전부 정리한다.
        # 이 시점 이후 원본을 읽는 코드는 없다 — 오디오 후행 업로드는 save_segments 가 뜬
        # 사본(pending_audio)을 쓰고, 국적 추론은 아래 nationality_pcm 을 쓴다.
        _release_persisted_pcm(state, len(state.segments))
        # 요구5: 국적 추론 훅(fire-and-forget) — 통화 내내 회수해 둔 user 원음을 넘긴다. 통화
        # 루프 종료 후 가산일 뿐 2펌프·절대 백스톱·종료 규약 무영향(R4). 예외 전량 흡수(R5).
        _trigger_nationality(
            db_session_factory, call_id, member_id,
            user_pcm=bytes(state.nationality_pcm),
        )
        await _finish_call(client_ws, state, call_id)


def _trigger_analysis(
    call_id, client, settings, db_session_factory, locale,
    *, target_language: str = _DEFAULT_TARGET_LABEL, locale_label: str | None = None,
    call_type: str = "normal", member_id: int | None = None,
    candidates: list[dict] | None = None,
    hinted_from_turn_index: set[int] | None = None,
    # ⭐ 이어하기 조각의 **시작 턴 인덱스**. 있으면 검증(증거 판정)을 그 뒤 턴으로 좁힌다.
    #   ⚠ 요약·표현 추출은 전체를 그대로 본다 — 좁히는 것은 검증뿐이다.
    since_turn_index: int | None = None,
) -> None:
    """통화후 분석을 백그라운드 task 로 띄운다(non-blocking, GC 방지 보관).

    call_type 디스패치: level_test → 레벨 판정(analyze_level_test_call, member_id 필수),
    normal → 기존 표현 추출 + 항목 검출(analyze_call). candidates 는 통화 시작 때
    선별한 검출 후보(주입 injected=True 포함, P2-c2) — None 이면 analyze_call 이
    기본 후보(practicing 18+introduced 12)로 폴백한다.
    hinted_from_turn_index(D16)는 항목 검출이 있는 analyze_call 에만 의미가 있다
    (레벨테스트 판정은 증거 적립이 없어 미전달).
    """
    # ⭐⭐ **표현학습·프리토킹은 항목 검출을 끄고 돌린다**(2026-09-10, bt-back 승인).
    #   ⛔ 문장 추출·요약은 **반드시 돌려야 한다** — 안 그러면 결과 화면의 `sentences` 가
    #     통째로 빈다(기획 §5). 그래서 analyze_call 을 안 부르는 게 아니라 **후보를 0으로**
    #     둔다(analyze_call 은 후보 0이면 검출 지시문·필드 자체를 생략한다).
    #   ⚠ 왜 꺼야 하나: 두 코스는 옛 증거 사슬(등급 E0~E3/F · 숙달 전이 · 승급 게이트)을
    #     쓰지 않는다(D12 — 승급이 «퀴즈 통과» 로 갈아탔다). 켜 두면 쓸모없는 LLM 몫을
    #     내고 **엉뚱한 증거**가 쌓인다(표현학습의 드릴 복창이 E1 로 적립된다).
    #   ⚠ `hinted_from_turn_index` 도 같이 끈다 — 두 코스엔 힌트가 없다(D7).
    #   ⛔⛔ **`None` 이 아니라 빈 리스트다.** `analyze_call` 에서 `candidates=None` 은
    #     «안 준다» 가 아니라 «기본 후보(practicing 18 + introduced 12)를 대신 뽑아라» 다
    #     (normalcall_service.py 의 `if candidates is not None:` 분기). None 으로 두면
    #     끄려던 검출이 그대로 돈다 — 정확히 반대 결과다.
    if call_type in ("expression", "freetalk"):
        candidates = []
        hinted_from_turn_index = None
    if call_type == "level_test" and member_id is not None:
        coro = svc.analyze_level_test_call(
            call_id, client, settings, db_session_factory,
            member_id=member_id, locale=locale,
            target_language=target_language, locale_label=locale_label,
        )
    else:
        coro = svc.analyze_call(
            call_id, client, settings, db_session_factory,
            locale=locale, target_language=target_language, locale_label=locale_label,
            member_id=member_id, candidates=candidates,
            hinted_from_turn_index=hinted_from_turn_index,
            # ⭐ 이어하기 조각이면 **이 조각의 턴만** 검증한다(근거는 analyze_call 주석).
            since_turn_index=since_turn_index,
            # ⛔ 두 코스는 옛 승급 사슬을 쓰지 않는다(기획 §5) — 판정도 안 돈다.
            #   ⚠ «후보가 0인가» 로 판정하면 안 된다: normal 도 후보가 빈 경우가 정상이고
            #     그때는 승급이 돌아야 한다. 아는 것은 **콜타입뿐**이라 여기서 명시한다.
            skip_level_up=call_type in ("expression", "freetalk"),
        )
    task = asyncio.create_task(coro, name=f"normalcall-analysis-{call_id}")
    _analysis_tasks.add(task)
    task.add_done_callback(_on_analysis_done)

    # ⭐⭐ **이어하기 요약은 분석과 나란히 돈다**(2026-08-19 실측: 준비까지 7초 걸렸다).
    #   전에는 `analyze_call` **안에서, 분석이 끝난 뒤에** 돌렸다. 그래서 요약 LLM 자체는
    #   1초인데 앞의 분석 5초를 통째로 기다렸다:
    #     08:41:54 저장 → 08:41:59 분석 done(+5s) → 08:42:00 요약(+6s)
    #   ⇒ 요약이 필요한 것은 **전사뿐**이고 전사는 저장 시점에 이미 DB 에 있다.
    #     분석을 기다릴 이유가 없다. 따로 띄우면 둘이 동시에 돈다.
    #   ⚠ 레벨테스트는 조각 개념이 없으므로 안 만든다.
    #   ⚠ 실패해도 아무것도 안 깨진다 — 이어하기가 즉석 생성으로 내려갈 뿐이다(R5).
    if call_type != "level_test":
        summary_task = asyncio.create_task(
            svc.build_resume_context(call_id, client, settings, db_session_factory),
            name=f"normalcall-resume-summary-{call_id}",
        )
        _analysis_tasks.add(summary_task)
        summary_task.add_done_callback(_on_analysis_done)


def trigger_reanalysis(
    settings: Settings,
    client,
    db_session_factory,
    locale: str,
    *,
    call_id: int,
    call_type: str,
    member_id: int,
) -> None:
    """수동 재분석(A) — 실패한 통화의 통화후 분석을 다시 백그라운드로 띄운다.

    라우터(POST /calls/{id}/reanalyze)가 status 를 'analyzing' 으로 되돌린 뒤 호출한다.
    통화 시작 때의 in-memory 컨텍스트(candidates·힌트 마커)는 이미 사라졌으므로 None 폴백
    (analyze_call 이 기본 후보로 대체). 대상 언어는 **call.target_language**(그 통화가 학습한
    언어코드)를 읽어 그 언어 루브릭으로 재실행한다(하드코딩 기본 금지 — 멀티랭귀지). 조회
    실패 시에만 기본 언어로 폴백. 증거 중복은 멱등 가드가 막는다.

    ⚠️ 이벤트루프 위에서 호출해야 한다(asyncio.create_task) — async 엔드포인트에서만.
    call.target_language 조회는 단건 PK get(짧고 드문 수동 엔드포인트)이라 동기 세션으로 읽는다.
    """
    from domains.learning.models.call import Call  # 지연 import(모델↔realtime 순환 회피)

    code = DEFAULT_LANGUAGE
    try:
        with db_session_factory() as db:
            call = db.get(Call, call_id)
            if call is not None and call.target_language:
                code = call.target_language
    except Exception as exc:  # noqa: BLE001 - 조회 실패는 기본 언어로 폴백(재분석은 계속)
        logger.warning(
            "normalcall: 재분석 target_language 조회 실패 → 기본(%s) 폴백 call_id=%s: %s",
            DEFAULT_LANGUAGE, call_id, exc,
        )
    spec = resolve_language(code) or SUPPORTED_LANGUAGES[DEFAULT_LANGUAGE]
    _trigger_analysis(
        call_id, client, settings, db_session_factory, locale,
        target_language=spec.label, locale_label=None,
        call_type=call_type, member_id=member_id,
        candidates=None, hinted_from_turn_index=None,
    )


def _on_analysis_done(task: asyncio.Task) -> None:
    _analysis_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("normalcall 분석 task 예외(무시): %s", exc)


async def _persist_remaining(
    db_session_factory, state: _CallState, call_id: int, member_id: int
) -> list[dict]:
    """아직 저장 안 한 세그먼트를 **텍스트 먼저** 일괄 저장 + 통화 종료 메타 갱신(graceful).

    P2.6: 최종 persist 는 upload_audio=False — 전사 행을 즉시 커밋(voice_url=None)해
    분석이 오디오 변환·업로드(~9s)를 기다리지 않는다. 반환한 pending 목록으로
    _trigger_audio_upload 가 병렬 업로드 태스크를 띄운다(통화중 점진 flush 는 종전 True).
    """
    new = state.segments[state.persisted_count:]
    duration_s = 0
    if state.call_start_ts is not None:
        duration_s = int(asyncio.get_running_loop().time() - state.call_start_ts)
    pending_audio: list[dict] = []
    try:
        if new:
            pending_audio = await svc.run_db(
                db_session_factory,
                lambda db: svc.save_segments(db, call_id, new, member_id, upload_audio=False),
            )
            state.persisted_count += len(new)
        await svc.run_db(
            db_session_factory, lambda db: svc.finalize_call(db, call_id, total_time=duration_s, status="analyzing")
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("normalcall: 통화 저장 실패(무시): %s", exc)
    logger.info(
        "normalcall: 저장 완료 call_id=%s segments=%d duration=%ds (오디오 후행 %d건)",
        call_id, len(state.segments), duration_s, len(pending_audio),
    )
    return pending_audio


def _trigger_audio_upload(
    db_session_factory, call_id: int, member_id: int, pending: list[dict]
) -> None:
    """세그먼트 오디오 후행 업로드를 백그라운드 task 로 띄운다(P2.6, non-blocking).

    분석 태스크와 같은 _analysis_tasks 강참조 패턴(GC 방지) 재사용. 예외는 전량
    흡수 — 실패 시 해당 행 voice_url 만 None 유지(R5, 전사·분석은 무손상).
    """
    if not pending:
        return

    async def _upload() -> None:
        try:
            done = await svc.run_db(
                db_session_factory,
                lambda db: svc.upload_segment_audio(db, call_id, member_id, pending),
            )
            logger.info(
                "normalcall: 오디오 후행 업로드 완료 %d/%d건 call_id=%s",
                done, len(pending), call_id,
            )
        except Exception as exc:  # noqa: BLE001 - 업로드 실패는 voice_url None 유지
            logger.warning(
                "normalcall: 오디오 후행 업로드 실패(무시 — voice_url None 유지) call_id=%s: %s",
                call_id, exc,
            )

    task = asyncio.create_task(_upload(), name=f"normalcall-audio-upload-{call_id}")
    _analysis_tasks.add(task)
    task.add_done_callback(_on_analysis_done)


def _trigger_nationality(
    db_session_factory, call_id: int, member_id: int, user_pcm: bytes
) -> None:
    """user 턴 음성으로 국적을 추론해 프로필을 갱신하는 훅을 백그라운드 task 로 띄운다(요구5).

    _trigger_audio_upload 와 100% 동일 패턴(GC 방지 강참조 + done 콜백). 예외는 전량
    흡수 — 국적 추론 실패는 통화·분석에 무손상(R5). 매 통화(레벨테스트 포함)에서 돈다.

    user_pcm: 통화 내내 회수해 둔 학습자 원음(_release_persisted_pcm 이 이어붙인 단일 바이트열,
    NATIONALITY_PCM_MAX_S 상한). 비면 아무것도 하지 않는다.

    🧒 왜 GCS(클라우드 저장소)에서 오디오를 도로 내려받지 않고, 통화 중 메모리에 쌓아둔
      user PCM(원음 조각들)을 바로 쓰나? 이유 셋:
      1) 레이스 회피: 오디오 업로드는 백그라운드에서 늦게(수 초 뒤) 끝난다. 국적 추론이
         "업로드가 다 됐겠지" 하고 내려받으면 아직 안 올라간 파일을 못 찾을 수 있다. 메모리에
         이미 들고 있는 원음을 쓰면 그 '기다림·순서 맞추기'가 아예 필요 없다.
      2) 원본 무손실: 저장용 오디오는 MP3 같은 압축을 거치며 음질이 살짝 깎인다. 국적을
         목소리로 추론하는 API 엔 원본(손실 없는 PCM)이 더 정확하다.
      3) 공짜 데이터: 어차피 통화 내내 학습자 목소리를 state.segments 에 모아뒀으니, 그걸
         그대로 이어붙이면 추가 다운로드 비용 0.
    🧒 왜 10초(NATIONALITY_MIN_SPEECH_S) 미만이면 건너뛰나: 말이 너무 짧으면 국적 추론
      모델이 "말한 게 없음(no_speech)"이라 판단해 쓸모없는 결과를 준다. 헛돈·헛시간을 아끼려
      아주 짧은 통화는 아예 안 보낸다.

    파이프라인: user PCM concat → 총 발화 길이 게이트(NATIONALITY_MIN_SPEECH_S 미만 skip)
    → WAV 변환 → predict_nationality(외부 API, threadpool 격리) → predictions 가 있으면
    nationality_service.record_and_recompute(이력 적재 + 최근5 평균 재계산, account 도메인 소유).
    """
    if not user_pcm:
        return

    async def _run() -> None:
        try:
            pcm = bytes(user_pcm)
            total_s = len(pcm) / (INPUT_SAMPLE_RATE * SAMPLE_WIDTH_BYTES)
            if total_s < _settings.NATIONALITY_MIN_SPEECH_S:
                logger.debug(
                    "normalcall: 국적 추론 skip(발화 %.1fs < %.1fs) call_id=%s",
                    total_s, _settings.NATIONALITY_MIN_SPEECH_S, call_id,
                )
                return
            wav = pcm16_to_wav(pcm, sample_rate=INPUT_SAMPLE_RATE)
            # 지연 import — realtime → account 서비스 순환 회피(호출 시점에만 해석).
            from core.nationality import predict_nationality
            from domains.account.service import nationality_service

            predictions = await run_in_threadpool(predict_nationality, wav, "wav")
            if not predictions:
                logger.debug("normalcall: 국적 추론 결과 없음(skip) call_id=%s", call_id)
                return
            await svc.run_db(
                db_session_factory,
                lambda db: nationality_service.record_and_recompute(
                    db, member_id, call_id, predictions
                ),
            )
            logger.info("normalcall: 국적 추론·갱신 완료 call_id=%s member=%s", call_id, member_id)
        except Exception as exc:  # noqa: BLE001 - 국적 추론 실패는 통화·분석 무손상(R5)
            logger.warning(
                "normalcall: 국적 추론 실패(무시 — 통화·분석 무손상) call_id=%s: %s",
                call_id, exc,
            )

    task = asyncio.create_task(_run(), name=f"normalcall-nationality-{call_id}")
    _analysis_tasks.add(task)
    task.add_done_callback(_on_analysis_done)


async def _run_session(
    client_ws,
    *,
    state: _CallState,
    system_instruction: str,
    voice: str,
    seed_text: str,
    settings: Settings,
    client: genai.Client,
    live_session_factory: SessionFactory,
    db_session_factory: sessionmaker,
    call_id: int,
    member_id: int,
    tools: Optional[list] = None,
) -> None:
    """Live 세션 + 2펌프 + 시계워처 + 점진 flush 를 동시에 실행(타임아웃 안쪽).

    tools: function-call 선언(현재 모든 콜타입 None — Phase 1 은 in-band tool 미사용). None 이면
    factory 에 아예 넘기지 않아 기존 세션 팩토리 시그니처(system_instruction/voice)와 바이트 동일
    (테스트의 가짜 팩토리도 무손상). 값이 있을 때만 tools= 를 흘려 open_session 이 config 에 주입.
    """
    # ⛔⛔ **세대 루프를 없앴다**(2026-08-19, 이어하기 설계 §8-b 사장님 지시).
    #   15분 통화를 한 세션으로 버티려고 만든 재연결 기계였는데, 조각이 6분이면
    #   **사장(死藏) 코드**다. 실측(라이브 56건):
    #       재연결 0회  53건  길이 0~508초
    #       재연결 1회   3건  길이 496·512·536초   ← 전부 8분 이상
    #       ⭐ 5분 30초 이하 48건 중 재연결 발생: **0건**
    #   난 3건은 정확히 `SESSION_ROTATE_AT_S=480`(8분) 회전 시계가 돈 것이다.
    #
    # ⭐⭐ **이어하기가 재연결을 대체한다.** GoAway 가 4분에 와도 재연결로 살릴 필요가 없다 —
    #   조각을 거기서 끝내고 "이어서 하시겠습니까?"를 띄우면 사용자가 새 세션으로 잇는다.
    #   이어하기가 하는 일이 정확히 그것이다.
    #
    # ⚠ 같이 사라진 것: `_SessionSwap` · `_watch_session_rotate` · `_swap_eligible` ·
    #   SESSION_ROTATE_AT_S / MAX_RECONNECTS / RECONNECT_MIN_REMAINING_S / SWAP_FLAP_GUARD_S.
    #   ⛔ `resume_handle` 수신은 **남긴다** — 세션 재개 핸들은 Gemini 가 주는 것이고
    #     로그·관측에 쓰인다. 다만 그걸로 다시 붙는 경로가 없어졌을 뿐이다.
    #
    # ⭐ T22 (2026-09-12, 사장님 «대응이라도 해줘», docs/20260912_0010_통화-T22-…) — **Gemini 쪽 비정상 끊김(1006) 뒤
    #   같은 통화에 한 번 더 붙는다.** 위 회전 기계와 다르다: 시계로 갈아끼우는 게 아니라 **저쪽이 끊었을 때만**, 통화당
    #   1회, 앱 소켓이 살아 있을 때만이다(통화 1418: Vertex TCP 리셋 → ConnectionClosedError → APIError 1006 → 브리지 종료
    #   → 앱엔 통화 끝. 08-25 이후 1건. 원인은 Google 측 — 우리는 복구만 한다).
    #   ⛔ R4 무수정 — 2펌프·TaskGroup·백스톱(이 함수 바깥 asyncio.timeout 안에서 2세대도 돈다)·barge-in·종료 규약 그대로.
    #     여기 더한 것은 «최대 2세대» 루프 하나다. 조건이 하나라도 아니면 예외를 그대로 올린다(저장 경로 무변경).
    send_seed = True
    while True:
        try:
            await _run_one_generation(
                client_ws,
                state=state,
                system_instruction=system_instruction,
                voice=voice,
                seed_text=seed_text,
                settings=settings,
                client=client,
                live_session_factory=live_session_factory,
                db_session_factory=db_session_factory,
                call_id=call_id,
                member_id=member_id,
                tools=tools,
                send_seed=send_seed,
            )
            return
        except BaseExceptionGroup as eg:
            why = _reconnect_refusal(state, eg, asyncio.get_running_loop().time())
            if why:
                if _is_gemini_side_closure(eg):
                    logger.warning("normalcall 재연결 안 함(%s) — 예외를 그대로 올린다: %r", why, eg)
                raise
            state.reconnects += 1
            logger.warning(
                "normalcall 재연결: Gemini 쪽 끊김(%r) → 같은 통화에 2세대를 연다(epoch %d→%d, 재연결 %d/%d, 세그먼트 %d)",
                _leaf_exceptions(eg)[:1], state.session_epoch, state.session_epoch + 1,
                state.reconnects, RECONNECT_MAX_PER_CALL, len(state.segments),
            )
            _reset_turn_state_for_reconnect(state)
            seed_text = _reconnect_brief(state)
            send_seed = True


async def _run_one_generation(
    client_ws,
    *,
    state: _CallState,
    system_instruction: str,
    voice: str,
    seed_text: str,
    settings: Settings,
    client: genai.Client,
    live_session_factory: SessionFactory,
    db_session_factory: sessionmaker,
    call_id: int,
    member_id: int,
    tools: Optional[list] = None,
    send_seed: bool = True,
) -> None:
    """연결 1개 = TaskGroup 1세대.

    ⚠ 예전엔 스왑이 필요하면 `_SessionSwap` 을 올려 호출부가 새 세대를 열었다. 조각이 6분이
      되면서 그 경로가 죽었고(설계 §8-b), 지금은 세대가 하나 — T22 부터는 **Gemini 쪽 끊김에만 최대 둘**이다.
    send_seed: 이 세대의 첫 턴으로 `seed_text` 를 보낼지. 1세대는 선톡, T22 재연결 세대는 재개 브리프다.
    """
    state.session_epoch += 1
    # ⭐ 선톡 시드를 state 에 남긴다 — 벙어리 인사 재시드(아래 펌프)가 같은 문장을 쓴다.
    state.seed_text = seed_text
    factory_kwargs = {"system_instruction": system_instruction, "voice": voice}
    if tools is not None:
        factory_kwargs["tools"] = tools
    # 재개 세대에만 핸들을 넘긴다 — 1세대는 종전과 완전히 동일한 호출이라 기존 가짜
    # 팩토리(엄격 시그니처)가 그대로 돈다.
    if state.resume_handle:
        factory_kwargs["resume_handle"] = state.resume_handle
    # ⛔ tools·resume_handle 과 같은 규율 — **값이 있을 때만** 넘긴다. 항상 넘기면 엄격한
    #   시그니처를 가진 테스트용 가짜 팩토리가 전부 깨진다.
    if state.input_language_codes:
        factory_kwargs["input_language_codes"] = state.input_language_codes
    # ⭐ 플랜별 모델(2026-09-04). 위와 **같은 규율** — 값이 있을 때만 넘긴다.
    #   ⛔ 항상 넘기면 엄격한 시그니처를 가진 테스트용 가짜 팩토리가 전부 깨진다.
    #   ⚠ 고르는 곳은 `call_service.live_model_for` 하나다. 여기선 실어 나르기만 한다.
    if state.live_model:
        factory_kwargs["model"] = state.live_model
    # ⭐ 이 통화의 백엔드(2026-09-08). 위와 **같은 규율** — None 이면 안 넘긴다.
    #   ⛔ 항상 넘기면 엄격한 시그니처를 가진 테스트용 가짜 팩토리가 전부 깨진다.
    #   ⚠ 이 값이 `build_live_config` 의 safety_settings·transparent 를 가른다. 틀리면
    #     세션이 **열리지도 않는다**(AI Studio 에 Vertex 전용 필드 → 1007).
    if state.live_vertex is not None:
        factory_kwargs["vertex"] = state.live_vertex
    async with live_session_factory(client, settings, **factory_kwargs) as session:
        try:
            # 🧒 여기가 심장. TaskGroup 안에 여러 '일꾼'을 동시에 띄운다. 이 묶음은 하나라도
            #   예외로 죽으면 나머지를 자동 취소한다 → 반쪽짜리 좀비 통화가 절대 안 생긴다.
            #   일꾼 6명이 하나의 공유 메모장(state, _CallState)을 함께 보며 협력한다:
            #     ① 펌프 클라→Gemini : 학습자 마이크 소리를 받아 AI 로 밀어준다(barge-in off 적용).
            #     ② 펌프 Gemini→클라 : AI 목소리·자막을 받아 학습자에게 밀어준다(턴 상태기계).
            #     ③ 시계워처         : 5분 되면 "이제 끝낼 시간" 신호(should_close)를 세우고,
            #                          정상 작별이 안 되면 최후에 강제 종료(백스톱).
            #     ④ 무음워처         : 학습자가 오래 조용하면 3단계로 부드럽게 대응(넛지→확인→종료).
            #     ⑤ 재접지          : 통화 중간에 캐릭터를 딱 1회 되박아 AI 가 성격을 잊는 것 완화.
            #     ⑥ 점진 flush      : 1분마다 대화를 DB 에 조금씩 저장(도중에 죽어도 기록이 남게).
            #   ⚠ 왜 펌프를 '동시에' 2개? 전화는 양방향이 동시에 흘러야 자연스럽다(맨 위 큰 그림).
            #     하나의 루프로 "받고→보내고→받고→보내고" 번갈아 하면 무전기처럼 끊긴다.
            async with asyncio.TaskGroup() as tg:
                tg.create_task(_pump_client_to_gemini(client_ws, session, state), name="nc-client->gemini")
                tg.create_task(_pump_gemini_to_client(client_ws, session, state), name="nc-gemini->client")
                tg.create_task(_watch_call_clock(state, session), name="nc-clock")
                tg.create_task(_watch_idle(session, state), name="nc-idle")
                tg.create_task(_reground_watch(session, state), name="nc-reground")
                tg.create_task(
                    _periodic_flush(db_session_factory, state, call_id, member_id), name="nc-flush"
                )
                # 선톡 트리거: AI 에게 먼저 오프닝 한마디를 던져 "네가 먼저 인사하며 시작해"라고
                # 시동을 건다. 이걸 안 하면 둘 다 서로 말하기만 기다려 통화가 조용히 멈춘다.
                # ⛔ 선톡 시드는 1세대만이다 — 다시 보내면 비버가 통화 중간에 또 인사한다.
                #   T22 재연결 세대는 **재개 브리프**를 같은 자리에 1턴 보낸다(seed_text 가 그것이고 send_seed=True).
                #   ⚠ `state.seed_text` 는 위에서 그 브리프로 바뀌지만, 벙어리 인사 재시드는 epoch==1 에만 걸린다.
                if send_seed and seed_text:
                    await session.send_text_turn(seed_text)  # 1세대: 선톡 트리거 / 재연결 세대: 재개 브리프
        # 🧒 TaskGroup 은 일꾼이 죽으면 그 예외들을 여러 개 담는 **봉투(ExceptionGroup)** 로
        #   감싸 던진다. 여기서 봉투를 풀어 우리 신호(_CallFinished=정상 끝, _ClientDisconnect=
        #   클라가 끊음)를 홑겹 예외로 다시 던진다 → 호출부의 평범한
        #   except 가 사람이 읽기 쉽게 처리한다.
        #
        # ⛔ except* 절을 여러 개 쓰지 마라(B4). except* 는 매치되는 절을 **전부** 실행하고,
        #   절들이 던진 예외를 다시 그룹으로 묶어 올린다 — 즉 신호가 둘 이상 섞인 봉투에서
        #   `except* A` / `except* B` 를 나란히 쓰면 결과가 ExceptionGroup([A, B]) 이 되어
        #   호출부의 `except _CallFinished` 가 **아무것도 못 잡는다**. 통화가 오류로
        #   끝난다. 그래서 봉투를 직접 받아 우선순위로 딱 하나만 고른다.
        except BaseExceptionGroup as eg:
            signal = _pick_call_signal(eg)
            if signal is None:
                raise
            raise signal


# ── T22 재연결 ──────────────────────────────────────────────────────────────── #
RECONNECT_MAX_PER_CALL = 1          # 통화당 1회 — 두 번째 끊김은 그대로 종료(끊김이 연속이면 저쪽 장애다)
RECONNECT_MIN_REMAINING_S = 20.0    # 남은 통화 시간이 이보다 짧으면 안 붙는다(붙여도 작별 시간뿐이다)
# Gemini 쪽 끊김으로 보는 APIError 코드 — 1000 정상 종료 · 1006 abnormal closure(1418) · 1011 internal error
_GEMINI_CLOSURE_CODES = frozenset({1000, 1006, 1011})


def _leaf_exceptions(eg: BaseException) -> list[BaseException]:
    """봉투를 끝까지 풀어 홑겹 예외 목록으로."""
    if isinstance(eg, BaseExceptionGroup):
        out: list[BaseException] = []
        for e in eg.exceptions:
            out.extend(_leaf_exceptions(e))
        return out
    return [eg]


def _is_gemini_closure_leaf(exc: BaseException) -> bool:
    """홑겹 예외 하나가 «Gemini 쪽 끊김» 인가(순수 함수).

    잡는 것: `google.genai.errors.APIError`(code 1000/1006/1011 또는 메시지에 closure/closed) ·
    `websockets.exceptions.ConnectionClosed*`(genai 가 쓰는 websockets 라이브러리) · `ConnectionResetError` · `OSError(errno 104)`.
    ⛔ 앱 소켓 쪽(starlette `WebSocketDisconnect`, 우리 `_ClientDisconnect`, RuntimeError 등)은 거짓이다 — 앱 소켓이 끊긴 통화에
      새 Gemini 연결을 열면 아무도 없는 통화가 된다.
    ⚠ 한계: 타입으로 가른다. uvicorn 은 앱 소켓 끊김을 WebSocketDisconnect 로 감싸므로 websockets.ConnectionClosed 가 앱 쪽에서
      올라오는 일은 없다(genai 만 그 라이브러리를 직접 쓴다).
    """
    try:
        from google.genai import errors as _genai_errors
        if isinstance(exc, _genai_errors.APIError):
            code = getattr(exc, "code", None)
            msg = str(exc).lower()
            return code in _GEMINI_CLOSURE_CODES or "closure" in msg or "closed" in msg
    except Exception:  # noqa: BLE001 - 라이브러리 부재는 분류만 좁힌다
        pass
    try:
        from websockets.exceptions import ConnectionClosed as _WsClosed
        if isinstance(exc, _WsClosed):
            return True
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, ConnectionResetError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 104:
        return True
    return False


def _is_gemini_side_closure(eg: BaseException) -> bool:
    """봉투(또는 홑겹)의 예외가 **전부** Gemini 쪽 끊김인가 — 하나라도 다른 종류면 거짓(그건 진짜 오류다)."""
    leaves = [e for e in _leaf_exceptions(eg) if not isinstance(e, asyncio.CancelledError)]
    return bool(leaves) and all(_is_gemini_closure_leaf(e) for e in leaves)


def _reconnect_refusal(state: _CallState, eg: BaseException, now: float) -> str:
    """재연결을 **안 하는** 이유(빈 문자열이면 한다). 설계 §1 의 조건 ①~⑤."""
    if not _is_gemini_side_closure(eg):
        return "Gemini 쪽 끊김이 아님"
    if state.should_close or state.close_seed_sent:
        return "종료 구간"
    if state.reconnects >= RECONNECT_MAX_PER_CALL:
        return "통화당 상한(%d)" % RECONNECT_MAX_PER_CALL
    remaining = state.call_duration_s - ((now - state.call_start_ts) if state.call_start_ts is not None else 0.0)
    if remaining < RECONNECT_MIN_REMAINING_S:
        return "남은 시간 %.0fs < %.0fs" % (remaining, RECONNECT_MIN_REMAINING_S)
    return ""


def _reset_turn_state_for_reconnect(state: _CallState) -> None:
    """2세대 앞에서 **턴 상태만** 리셋한다 — 세그먼트·covered·퀴즈 상태·시계·재접지 카운터는 전부 유지.

    열린 비버 턴이 있었으면 그 자막은 세그먼트로 flush 하고 버린다(이미 나간 오디오는 클라가 재생한다).
    학습자 버퍼(pcm·전사)는 비운다 — 1세대 펌프가 죽어 있던 사이의 마이크 프레임은 버린다(v1).
    """
    if state.cur_beaver_text or state.cur_beaver_pcm:
        _flush_beaver_segment(state)
    state.cur_beaver_text = []
    state.cur_beaver_pcm = bytearray()
    state.cur_user_text = []
    state.cur_user_pcm = bytearray()
    state.turn_id = None
    state.user_turn_open = False


def _reconnect_brief(state: _CallState) -> str:
    """2세대 첫 턴 — 재개 브리프(CONTROL_TAG 접두). 끊김을 사과하지 말고 하던 것을 그대로 이어가라.

    표현학습: 재접지 쪽지 재료(드릴한 것·맞힌 것·틀린 것·다음 항목) + 퀴즈 창이 열려 있으면 남은 문항.
    그 외: 마지막 비버 문장 한 줄(«직전 화제»). 큐 pending 이면 그대로 두면 다음 발화에 얹힌다.
    """
    head = (f"{CONTROL_TAG} 연결이 잠깐 끊겼다가 이어졌다. 끊긴 것을 사과하지 말고, 인사도 다시 하지 말고, "
            "하던 것을 그대로 이어가라. ")
    if state.expr_items:
        body = _build_expression_note(state)
        if state.expr_quiz_open and state.expr_quiz_set:
            judged = state.expr_quiz_pass | state.expr_quiz_fail
            remaining = [
                state.reground_items[n - 1] for n in state.expr_quiz_set
                if 1 <= n <= len(state.reground_items)
                and int((state.expr_items[n - 1].get("item_id") or -1)) not in judged
            ]
            if remaining:
                body += " 지금은 %s가 연 퀴즈 중이다 — 남은 문항: %s." % (
                    CONTROL_TAG, " · ".join("«%s»" % r for r in remaining))
        return head + body
    last_beaver = next((s.get("text") for s in reversed(state.segments) if s.get("role") == "beaver" and s.get("text")), "")
    return head + ("직전 화제: 네가 마지막으로 한 말은 «%s» 였다." % last_beaver if last_beaver else "")


# 통화 신호 우선순위(B4). ⛔ 순서가 곧 규칙이다 — **종료 > 클라 끊김 > 스왑**.
# 종료가 걸린 봉투를 스왑으로 처리하면 이미 끝난 통화가 되살아나고, 클라가 이미 끊었는데
# 스왑하면 아무도 없는 통화에 새 연결을 연다.
_CALL_SIGNALS: tuple[type[Exception], ...] = (_CallFinished, _ClientDisconnect)


def _pick_call_signal(eg: BaseExceptionGroup) -> Optional[Exception]:
    """TaskGroup 봉투에서 통화 신호 하나를 우선순위로 골라 돌려준다(없으면 None).

    신호가 아닌 예외(진짜 오류)가 같이 들어 있으면 여기서 로그로 남긴다 — 신호를 홑겹으로
    올리면서 봉투를 버리기 때문에, 안 남기면 그 오류가 조용히 사라진다.
    """
    for sig in _CALL_SIGNALS:
        if eg.subgroup(sig) is None:
            continue
        rest = eg.split(sig)[1]  # 이 신호를 뺀 나머지(다른 신호 + 진짜 오류)
        if rest is not None and rest.split(_CALL_SIGNALS)[1] is not None:
            logger.warning(
                "normalcall: 통화 신호(%s)와 함께 올라온 예외(무시하지 않고 기록만): %r",
                sig.__name__, rest.split(_CALL_SIGNALS)[1],
            )
        return sig()
    return None


async def _periodic_flush(db_session_factory, state: _CallState, call_id: int, member_id: int) -> None:
    """통화중 FLUSH_INTERVAL_S 마다 누적 세그먼트를 점진 저장(긴 통화·크래시 내성)."""
    while True:
        await asyncio.sleep(FLUSH_INTERVAL_S)
        new = state.segments[state.persisted_count:]
        if not new:
            continue
        target = state.persisted_count + len(new)
        try:
            await svc.run_db(
                db_session_factory, lambda db: svc.save_segments(db, call_id, new, member_id)
            )
            state.persisted_count = target
            # ⭐ 저장이 끝났으니 PCM 을 놓아준다(B1). 안 놓으면 통화 오디오 전체가 통화
            #   내내 RAM 에 남아 15분 통화 하나가 30~50MB 를 물고 있게 된다.
            freed = _release_persisted_pcm(state, target)
            logger.info(
                "normalcall: 점진 flush %d개(누적 %d) call_id=%s pcm해제=%dKB",
                len(new), target, call_id, freed // 1024,
            )
        except Exception as exc:  # noqa: BLE001 - flush 실패는 다음 주기/종료시 재시도
            logger.warning("normalcall: 점진 flush 실패(무시): %s", exc)


class StartParams(NamedTuple):
    """첫 start 프레임에서 뽑은 값들.

    옛날엔 평범한 5-tuple 이었는데 필드가 늘면서 호출부가 위치로 풀어야 했다. 이름이
    붙으면 순서를 틀릴 수 없고 뒤에 필드를 더해도 기존 언패킹이 안 깨진다.
    (NamedTuple 이라 == (a, b, ...) 비교도 그대로 된다 — 기존 테스트 무영향.)
    """

    # ⛔ 서버가 무시한다(로그 전용). 통화 캐릭터는 resolve_call_character 가 정한다.
    #    미전송(신버전 앱)이면 None — 그래서 int 가 아니라 int | None 이다.
    character_id: int | None
    locale: str | None
    target_language: str | None
    call_type: str | None
    duration_min: int | None
    tz_offset_min: int | None = None
    # 수신통화(알람)일 때만. 서버가 푸시로 내려준 통화 id 를 앱이 되돌려준 값.
    inbound_call_id: str | None = None
    # ⭐ 이어하기 — 직전 조각의 call_id. ⚠ **기본값을 준다**(맨 뒤에 붙인 이유이기도 하다):
    #   기존 호출부·테스트가 이 필드를 안 넘겨도 안 깨진다(NamedTuple 규율, 위 독스트링).
    #   ⛔ 프론트가 문자열로 보낸다("1234") — 여기서는 **원문 그대로** 들고, int 변환은
    #     호출부(_as_int)가 한다. 파싱 실패를 이 자리에서 삼키면 원인이 로그에 안 남는다.
    continues_call_id: str | int | None = None
    # ⭐ 과제 통화 — 숙제 상세의 회화 카드에서 시작한 통화만 보낸다. 맨 뒤에 붙여
    #   기본값을 준 이유는 `continues_call_id` 와 같다(기존 호출부·테스트 보호).
    assignment_id: str | int | None = None


async def _read_initial_start(client_ws) -> StartParams:
    """첫 start 에서 character_id / locale / target_language / call_type / duration_min /
    tz_offset_min 확보.

    target_language 는 **언어코드**로 해석한다(멀티랭귀지): resolve_language 로 정규화해
    지원 코드/구 데모 라벨("프랑스어")은 canonical code("fr")로, 미지원/부재는 원문 그대로
    통과시킨다(_resolve_target_language 가 최종 경고+DEFAULT 폴백). call_type None = 서버 판단
    (D11 자동 라우팅), "normal"/"level_test" = 클라 명시(우선). duration_min None = 서버 기본
    통화 길이, 값 있으면 데모/dev 에서 3~15분 override.
    """
    from starlette.websockets import WebSocketDisconnect

    invalid_warned = False  # 검증 실패 warning 은 통화당 1회만(스팸 방지)
    try:
        for _ in range(6):
            try:
                message = await asyncio.wait_for(client_ws.receive(), timeout=2.0)
            except asyncio.TimeoutError:
                break
            if message.get("type") == "websocket.disconnect":
                raise _ClientDisconnect()
            text = message.get("text")
            if text is not None:
                try:
                    cm = client_adapter.validate_python(json.loads(text))
                except Exception as exc:  # noqa: BLE001 - 깨진 후보는 폐기하고 계속 대기
                    if not invalid_warned:
                        invalid_warned = True
                        # 원문은 앞부분만(민감정보·로그 폭주 방지) — 폴백 진행 원인 추적용.
                        logger.warning(
                            "normalcall: start 후보 메시지 검증 실패(폐기) — %s / 원문 일부: %.80s",
                            exc, text,
                        )
                    continue
                if cm.type == "start":
                    raw_target = getattr(cm, "target_language", None)
                    # 언어코드로 정규화(지원 코드/구 라벨 → canonical code). 미지원은 원문 유지
                    # → _resolve_target_language 가 경고+DEFAULT 폴백.
                    spec = resolve_language(raw_target)
                    target_code = spec.code if spec is not None else raw_target
                    # character_id 는 **선택**이 됐다 — 미전송이면 None 그대로 둔다.
                    # int(None) 은 터지고, 여기서 기본값 1 을 채우면 "안 보냄"과
                    # "BABA 선택"이 다시 구별되지 않는다(그게 원래 버그였다).
                    raw_cid = getattr(cm, "character_id", None)
                    return StartParams(
                        character_id=int(raw_cid) if raw_cid is not None else None,
                        locale=getattr(cm, "locale", None),
                        target_language=target_code,
                        call_type=getattr(cm, "call_type", None),
                        duration_min=getattr(cm, "duration_min", None),
                        tz_offset_min=getattr(cm, "tz_offset_min", None),
                        inbound_call_id=getattr(cm, "inbound_call_id", None),
                        continues_call_id=getattr(cm, "continues_call_id", None),
                        assignment_id=getattr(cm, "assignment_id", None),
                    )
    except WebSocketDisconnect as exc:
        raise _ClientDisconnect() from exc
    # 캐릭터 기본값을 여기서 정하지 않는다 — 서버 해석기(resolve_call_character)가
    # member.character_id 로 정한다. 예전엔 DEFAULT_CHARACTER_ID=1 을 채웠는데, 그
    # "일단 BABA" 폴백이 통화의 60%를 엉뚱한 캐릭터로 보낸 원인이었다.
    return StartParams(None, None, None, None, None)


# --------------------------------------------------------------------------- #
# P2.5: teaching_plan + 동적 힌트 사이드카 (D16 — mechanics ⑪·⑬)
# --------------------------------------------------------------------------- #
def _prompt_study_items(study_items: list[dict] | None) -> list[dict] | None:
    """프롬프트에 실을 학습항목 — 본편 전량 + 예비 앞에서 PROMPT_RESERVE_MAX 개.

    ⛔ 화면 카드(_teaching_plan_items)는 이걸 쓰지 않는다. 카드는 선별분 전량이 가야 한다.
    ⚠ 순서를 지킨다 — 선별이 낸 순서가 곧 우선순위다(뒤에서 자르면 그 순위가 보존된다).
    """
    if not study_items:
        return study_items
    out = []
    reserve_taken = 0
    for it in study_items:
        if it.get("slot") == "reserve":
            if reserve_taken >= PROMPT_RESERVE_MAX:
                continue
            reserve_taken += 1
        out.append(it)
    return out


def _teaching_plan_items(study_items: list[dict]) -> list[TeachingItem]:
    """study_items(persona 스키마 + item_id/roman) → teaching_plan 카드 항목(P2.5).

    프롬프트 주입과 단일 소스(mechanics ⑪): ko=obj / example=ex / meaning=des / kind /
    roman=학습항목 meanings JSON 의 "roman"(청크 RR 표기). item_id 가 없는 항목
    (구형 dto — hint_used 상관 불가)은 건너뛴다.
    """
    items: list[TeachingItem] = []
    for it in study_items or []:
        obj = it.get("obj")
        item_id = it.get("item_id")
        if not obj or item_id is None:
            continue
        items.append(
            TeachingItem(
                item_id=int(item_id),
                ko=str(obj),
                roman=it.get("roman"),
                meaning=it.get("des"),
                example=it.get("ex"),
                kind=str(it.get("kind") or ""),
            )
        )
    return items


def _hint_instruction(locale_label: str, target_language: str = "한국어") -> str:
    """동적 힌트 사이드카 시스템 지시문(순수 문자열 조립 — LLM 생성 0).

    (멀티랭귀지) target_language 로 예시 답변 언어를 지정한다(기본 한국어 — 기존 출력 무손상).
    korean 필드는 스키마·클라 호환상 이름을 유지하되 **내용은 대상 언어**다(일본어 통화면
    일본어 문장). roman 문구는 한국어만 RR 표기법을 명시, 그 외는 일반 로마자.
    레벨 프로파일은 주입하지 않는다 — 힌트는 어차피 '짧고 쉬운 구어체 1문장'이라 레벨 무관.
    """
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


# 클라 계측 배치 방어 상한. ⛔ 클라를 믿지 않는다 — 폭주하는 앱 한 대가 로그를 먹으면
#   그 시간대의 **다른 통화 진단이 통째로 묻힌다**.
_DIAG_MAX_EVENTS = 200          # 배치당 이벤트 수
_DIAG_MAX_KEYS = 24             # 이벤트 dict 의 키 수
_DIAG_MAX_STR = 64              # 문자열 값 길이
# ⭐ 통화당 배치 수. 클라가 **5초마다 1배치**를 보내므로 곧 «커버하는 통화 길이»다.
#   ⛔ 40 이면 **200초에서 계측이 조용히 죽는다.** 그런데 사장님 증상(음성·영상 띄엄띄엄)은
#     1~2분부터 시작해 **후반으로 갈수록 심해진다** — 즉 문제가 가장 심한 구간이 서버에
#     한 건도 안 남는다. 실제로 이번 조사에서 200초 이후 `ping_ms`·`q` 를 못 봐서
#     debug 로그캣에 의존해야 했다.
#   ⇒ 200 = 약 16분. 5분 통화(+이어하기 조각)를 끝까지 덮는다.
#   ⚠ 상한 자체는 유지한다 — 폭주하는 앱 한 대가 그 시간대 다른 통화 진단을 묻는 것을
#     막는 것이 이 상한의 목적이고, 그 목적은 그대로다.
_DIAG_MAX_BATCHES = 200


def _clip_diag_event(ev: object) -> dict | None:
    """이벤트 dict 하나를 상한 안으로 깎는다. dict 가 아니면 버린다."""
    if not isinstance(ev, dict):
        return None
    out: dict = {}
    for k, v in list(ev.items())[:_DIAG_MAX_KEYS]:
        key = str(k)[:_DIAG_MAX_STR]
        if isinstance(v, str):
            out[key] = v[:_DIAG_MAX_STR]
        elif isinstance(v, (int, float, bool)) or v is None:
            out[key] = v
        else:
            out[key] = str(v)[:_DIAG_MAX_STR]   # 중첩 구조는 안 받는다
    return out


def _record_client_diag(state: _CallState, msg) -> None:
    """⭐ 클라 계측 배치를 로그로 흘린다(2026-08-24). 저장·응답 없음.

    ## 왜 서버가 이걸 받아야 하나
    체감 지연·표정 발화 시각·재생 대조는 **클라에만 있는 값**이다. 3.1 은 학습자 전사를
    자기 응답과 같이 내보내서 서버 쪽 원점이 무너졌다(응답지연 끝기준 0.00초 사태).

    ## ⛔ 규율
    - **응답하지 않는다**(hint_used 와 같은 규율 — ack 불요).
    - **예외를 통화로 새게 하지 않는다**(R5). 계측이 통화를 죽이면 본말전도다.
    - 배치 `seq` 의 구멍은 **유실**이다. 그대로 로그에 남겨 나중에 셀 수 있게 한다.
    - 클라가 버린 수(`dropped`)가 0 이 아니면 **경고로 올린다** — 조용한 손실 금지.

    ## ⚠ 형식을 바꾸지 마라
    `key=value` 로 못 박아 둔 덕에 Cloud Logging 로그 기반 메트릭이 코드 변경 0줄로
    숫자를 뽑아간다(`normalcall usage:` 줄과 같은 규율).
    """
    try:
        state.diag_batches += 1
        if state.diag_batches > _DIAG_MAX_BATCHES:
            if state.diag_batches == _DIAG_MAX_BATCHES + 1:   # 한 번만 알린다
                logger.warning(
                    "normalcall 계측: 배치 상한(%d) 초과 — 이후 무시 seq=%s",
                    _DIAG_MAX_BATCHES, getattr(msg, "seq", None),
                )
            return
        raw = list(getattr(msg, "events", None) or [])[:_DIAG_MAX_EVENTS]
        events = [e for e in (_clip_diag_event(x) for x in raw) if e]
        dropped = int(getattr(msg, "dropped", 0) or 0)
        state.diag_events += len(events)
        state.diag_dropped += dropped
        line = json.dumps(
            {
                "seq": getattr(msg, "seq", None),
                "level": getattr(msg, "level", None),
                "anchor": getattr(msg, "anchor_epoch_ms", None),
                "dropped": dropped,
                "events": events,
            },
            ensure_ascii=False, separators=(",", ":"),
        )
        (logger.warning if dropped else logger.info)(
            "normalcall 계측: batch=%d n=%d dropped=%d %s",
            state.diag_batches, len(events), dropped, line,
        )
    except Exception as exc:  # noqa: BLE001 - 계측 실패는 통화 무영향(R5)
        logger.warning("normalcall 계측: 배치 처리 실패(무시): %s", exc)


def _record_client_timing(state: _CallState, msg) -> None:
    """클라 실측 타이밍 1건.

    ⛔ 이 타입이 Live 유니온에 **없어서** 지금까지 서버가 버리고 있었다(2026-08-24 발견).
      클라는 보내고 있었는데 `제어 메시지 무시` 로 삼켜졌다.

    ⭐ 읽는 법 — 이 줄 하나로 **뺄셈**이 성립한다:
        클라 재생 몫 = audible − (같은 turn 의 서버 `첫소리`)
        사용자 체감    = 입벌림기준  ⇐ ⭐ 사장님이 실제로 기다리는 시간
      `입벌림기준` 이 -1 이면 클라 VAD 가 그 턴의 원점을 못 잡은 것이다(추정 아님, 부재).
    """
    try:
        logger.info(
            "normalcall 클라타이밍: turn=%s 입벌림기준=%sms audible=%sms "
            "turn_start=%sms cushion=%sms%s",
            getattr(msg, "turn_id", None),
            getattr(msg, "speech_to_sound_ms", None),
            getattr(msg, "audible_ms", None),
            getattr(msg, "turn_start_ms", None),
            getattr(msg, "cushion_ms", None),
            " ⚠추정(네이티브 잔량 없음)" if getattr(msg, "estimated", False) else "",
        )
    except Exception as exc:  # noqa: BLE001 - R5
        logger.warning("normalcall 클라타이밍: 처리 실패(무시): %s", exc)


def _record_hint_used(state: _CallState, msg) -> None:
    """hint_used 적재(응답·저장 없음 — in-memory, mechanics ⑬).

    같은 turn_id 재열람은 1회만 기록(중복 강등 방지). 마커 = 현재 next_turn_index:
    barge-in off 라 힌트는 비버 턴 종료(세그먼트 flush) 후에 열리므로, 이 값 이상의
    첫 USER turn_index 가 "열람 직후 발화" — 통화후 _verify_detections 가 그 턴의
    E2/E3 를 E1 로 강등한다. 크래시로 유실되면 과크레딧 1회 허용(테이블 신설 대신 수용).
    """
    turn_id = getattr(msg, "turn_id", None)
    if turn_id is not None and turn_id in state.hinted_turn_ids:
        return
    if turn_id is not None:
        state.hinted_turn_ids.add(turn_id)
    state.hinted_next_turn_index.add(state.next_turn_index)
    logger.info(
        "normalcall: hint_used turn_id=%s item_id=%s stage=%s → 강등 마커 t>=%d",
        turn_id, getattr(msg, "item_id", None), getattr(msg, "stage", None),
        state.next_turn_index,
    )


def _spawn_hint_task(client_ws, state: _CallState) -> None:
    """비버 턴 종료 시 동적 힌트 태스크를 띄운다(D16 — 펌프에서는 태스크 생성만).

    ⛔ 격리(R4/R5): 2펌프 경로의 추가 비용은 create_task 1회뿐 — LLM 콜·ws send 는
    전부 백그라운드에서 일어나며, 느리거나 실패해도 통화 무영향(힌트만 미표시).
    세션당 동시 1개: 새 질문이 오면 이전 미완 힌트는 취소(낡은 질문의 힌트가 늦게
    뜨는 혼선 방지). 호출 시점은 _flush_beaver_segment **이전**이어야 한다 —
    질문 전문(cur_beaver_text)이 flush 로 비워지기 전에 캡처.
    """
    ctx = state.hint_ctx
    if ctx is None:  # 힌트 비활성(레벨테스트/레벨1 외) — 기존 동작
        return
    turn_id = state.last_turn_id
    question = "".join(state.cur_beaver_text).strip()
    # 질문 휴리스틱: 물음표 포함 턴만(설명·안내 턴에 힌트를 띄우면 소음 — mechanics ⑬).
    if not turn_id or "?" not in question:
        return
    prev = state.hint_task
    if prev is not None and not prev.done():
        prev.cancel()
    task = asyncio.create_task(
        _hint_sidecar(client_ws, ctx, turn_id, question),
        name=f"normalcall-hint-{turn_id}",
    )
    state.hint_task = task
    state.hint_tasks.add(task)  # 강참조(GC 방지) — run_call finally 가 전량 취소
    task.add_done_callback(state.hint_tasks.discard)


async def _hint_sidecar(client_ws, ctx: dict, turn_id: str, question: str) -> None:
    """힌트 1건 생성 → ws push (백그라운드 태스크 본문 — 예외 전량 흡수, R5).

    generate_structured 는 단발 HTTP 콜 — Live 소켓과 별개 연결이라 상호 간섭이
    없다(점진 flush 와 같은 검증된 패턴). barge-in off 라 생성 0.5~1.5초가 정확히
    학습자의 "생각하는 틈"에 도착한다(mechanics ⑬). thinking_budget=0 으로 지연 최소화.
    """
    from starlette.websockets import WebSocketState

    try:
        result = await gemini_analysis.generate_structured(
            ctx["client"],
            ctx["model"],
            system_instruction=ctx["instruction"],
            prompt=question,
            schema=HintOut,
            temperature=0.3,
            thinking_budget=0,
            usage=ctx.get("usage"),   # 원가 계기판 — 없으면 종전과 동일
        )
        # getattr 방어: generate_structured 실패(None)·이형 응답 모두 조용히 미표시.
        raw = getattr(result, "examples", None) if result is not None else None
        examples = [
            HintExample(
                korean=k,
                roman=getattr(e, "roman", None),
                native=getattr(e, "native", "") or "",
            )
            for e in (raw or [])
            if (k := (getattr(e, "korean", None) or "").strip())
        ][:3]  # 최대 3개(모델이 더 줘도 절단), korean 없는 예시는 버림
        if not examples:
            return
        if client_ws.client_state != WebSocketState.CONNECTED:
            return  # 통화가 먼저 끝났으면 미전송(무해)
        await _send_json(client_ws, ServerHint(turn_id=turn_id, examples=examples))
        logger.info("normalcall 💡 hint[turn=%s]: %d개 %s", turn_id, len(examples), examples[0].korean)
    except asyncio.CancelledError:
        raise  # 취소(새 질문/통화 종료)는 정상 경로
    except Exception as exc:  # noqa: BLE001 - 힌트 실패는 미표시일 뿐 통화 무영향
        logger.warning("normalcall 힌트 사이드카 실패(무시 — 힌트 미표시): %s", exc)


# --------------------------------------------------------------------------- #
# 레벨테스트 Phase 2: 조용한 밴드 관측 → 서버 천장검출 조기종료 (질문 주입 0)
# --------------------------------------------------------------------------- #
def _band_ceiling_reached(state: _CallState, elapsed: float) -> bool:
    """하드 턴캡: 관측된 전체 답변 수가 안전 상한(MAX_ANSWERS)에 닿았는지(순수 함수 — 부작용 0).

    종료 판정 전용 refactor(Phase 2): 밴드 천장(obs_max)·plateau·비화자(obs_max<=0) 판정을
    제거했다. '등반 실패' 감지는 판정관 should_end(맥락)와 비화자 결정론 컷(nonspeaker_streak)이
    맡고, 이 함수는 오직 무한 관측을 막는 하드 턴캡만 담당한다. elapsed 는 호출부 시그니처
    호환용(현 구현은 미사용 — 턴캡은 시간 무관).
    """
    return state.total_answers >= LEVELTEST_BAND_MAX_ANSWERS


def _spawn_band_observe(state: _CallState) -> None:
    """유저 답변 1건을 조용히 밴드 관측하는 사이드카를 띄운다(무주입 — should_close 만).

    ⛔ 격리(R4/R5): 2펌프 경로의 추가 비용은 create_task 1회뿐. 분류 LLM 콜은 백그라운드
    사이드카에서 일어나며, 느리거나 실패해도 통화 무영향(관측 1건 누락일 뿐 — 3분캡/무음이
    백스톱). ★ 질문 주입 코드 없음: 사이드카는 천장 도달 시 종료를 **요청**만 한다.
    band_awaiting 1회 가드로 동시 1건만(진행중이면 이 답변은 관측 스킵 — 다음 답변에 재개).
    호출 시점은 _flush_user_segment **이전**이어야 한다(cur_user_text 가 비워지기 전 캡처).

    ⛔ session 을 넘기지 않는다(B2). 이 태스크는 TaskGroup **밖**이라 세대(연결)보다 오래
      살 수 있는데, 세션은 세대의 지역 변수다 — 잡고 있으면 세션 교체 후 **죽은 세션**에
      주입하게 된다. 세션에 손대는 일은 전부 세대 안(펌프·워처)에서만 한다.
    """
    if not state.band_observe or state.band_awaiting or state.should_close:
        return  # 종료 진행중이면 관측 불필요(m4: LLM 콜 낭비 방지)
    answer = "".join(state.cur_user_text).strip()
    if not answer:
        return  # 무발화 턴(오프닝 등) — 관측 대상 아님
    state.band_awaiting = True  # create_task 전 선점(동시 1건 가드)
    task = asyncio.create_task(
        _band_observe_sidecar(state, answer, state.last_beaver_question),
        name="normalcall-band-observe",
    )
    state.band_tasks.add(task)  # 강참조(GC 방지) — run_call finally 가 전량 취소
    task.add_done_callback(state.band_tasks.discard)


async def _band_observe_sidecar(
    state: _CallState, answer: str, prior_question: str
) -> None:
    """답변 1건 종료 판정 → 종료 트리거면 종료를 **요청**한다(백그라운드, R5).

    judge_leveltest_turn 이 (answer_in_target, should_end) 를 준다(밴드 정밀분류 없음 — 최종
    레벨은 통화후 판정관 몫). 세 종료 트리거 중 하나면 종료:
      ① should_end(판정관 등반실패 감지) — 시간 플로어 & 최소 답변(END_JUDGE_MIN) 충족 시.
      ② 비화자 결정론 컷 — answer_in_target=False 연속 NONSPEAKER_MAX — 시간 플로어 충족 시.
      ③ 하드 턴캡(_band_ceiling_reached) — total_answers >= MAX_ANSWERS(무한 관측 방지).
    어느 트리거든 should_close 를 세우고 close_requested 를 깨운다. **실제 종료 시드 주입은
    세대 안의 워처·펌프가 한다** — 비버 idle & 유저 응답 대기 없음이면 시계워처가 즉시,
    발화중이면 펌프가 다음 깨끗한 turn_end 에서 주입한다.

    ⛔ 세션을 직접 잡지 않는다(B2). 이 태스크는 TaskGroup 밖이라 세션 교체보다 오래 살 수
      있어서, 예전처럼 session 을 캡처해 _inject_close_seed 를 부르면 **죽은 세션에 주입**하는
      유일한 경로가 된다. 신호만 넘기고 주입은 살아 있는 세대에 맡긴다.
    ★ 질문 주입 없음. 예외·CancelledError 처리는 힌트 사이드카와 동일(취소 재전파, 그 외 흡수).
    """
    answer_in_target = False
    should_end = False
    try:
        answer_in_target, should_end = await svc.judge_leveltest_turn(
            state.band_client,
            transcript=state.leveltest_transcript,
            latest_answer=answer,
            prior_question=prior_question,
            target_language=state.band_target_language,
            usage=state.sidecar_usage,   # 원가 계기판(레벨테스트 턴 판정도 LLM 콜이다)
        )
    except asyncio.CancelledError:
        raise  # 취소(통화 종료)는 정상 경로 — 재전파
    except Exception as exc:  # noqa: BLE001 - 판정 실패는 1건 누락일 뿐 통화 무영향
        logger.warning("normalcall: 종료 판정 사이드카 실패(무시 — 1건 누락): %s", exc)
        answer_in_target, should_end = False, False
    finally:
        state.band_awaiting = False  # in-flight 해제 → 다음 답변 판정 허용

    state.total_answers += 1
    # 비화자 스트릭: 대상 언어 산출 실패면 누적, 성공이면 리셋(연속 실패만 컷 재료).
    if answer_in_target:
        state.nonspeaker_streak = 0
    else:
        state.nonspeaker_streak += 1

    # 판정관에 넘길 전사 누적(다음 턴 맥락) — 원문 그대로 Q/A(인용 아님).
    state.leveltest_transcript.append(
        f"Q: {(prior_question or '').strip()}\nA: {answer.strip()}"
    )

    loop = asyncio.get_running_loop()
    elapsed = (
        loop.time() - state.call_start_ts if state.call_start_ts is not None else 0.0
    )
    floor_ok = elapsed >= LEVELTEST_BAND_TIME_FLOOR_S
    # ① 하드 턴캡(시간 무관 — 무한 관측 방지). ② 비화자 결정론 컷(연속 실패). ③ 판정관 should_end.
    hard_cap = _band_ceiling_reached(state, elapsed)
    nonspeaker_cut = floor_ok and state.nonspeaker_streak >= LEVELTEST_BAND_NONSPEAKER_MAX
    judge_end = (
        should_end
        and floor_ok
        and state.total_answers >= LEVELTEST_END_JUDGE_MIN_ANSWERS
    )
    reached = hard_cap or nonspeaker_cut or judge_end
    logger.info(
        "normalcall: 종료판정 answer_in_target=%s should_end=%s total=%d nonspeaker_streak=%d "
        "elapsed=%.0fs 턴캡=%s 비화자컷=%s 판정종료=%s",
        answer_in_target, should_end, state.total_answers, state.nonspeaker_streak,
        elapsed, hard_cap, nonspeaker_cut, judge_end,
    )
    if not reached:
        return
    # 종료 트리거 → 종료 파이프 합류(새 종료 경로 없음). 이미 종료 진행중이면 양보.
    if state.should_close or state.close_seed_sent:
        return
    _request_close(state)
    logger.info(
        "normalcall: 레벨테스트 종료 트리거(턴캡=%s 비화자컷=%s 판정종료=%s) → 종료 플래그",
        hard_cap, nonspeaker_cut, judge_end,
    )


# --------------------------------------------------------------------------- #
# 펌프: 클라 → Gemini
# --------------------------------------------------------------------------- #
async def _pump_client_to_gemini(client_ws, session: LiveSessionProtocol, state: _CallState) -> None:
    """클라 → Gemini. barge-in off: 비버 발화중이면 마이크 미전송. forward 먼저 후 누적.

    🧒 이 펌프는 '학습자 → AI' 한 방향만 담당하는 무한루프다. 소켓에서 프레임을 하나씩 받아
      종류를 구분한다: **바이너리(bytes) = 목소리(PCM 오디오)**, **텍스트 = JSON 제어 신호**
      (ping/playback_done/hint_used). 이 '바이너리=소리, 텍스트=명령' 규약이 protocol.py 다.

    🧒 barge-in off 의 핵심 한 줄이 바로 아래 `state.turn_id is None` 조건이다. turn_id 가
      값이 있으면 = "지금 비버가 말하는 중". 그때는 학습자 마이크 오디오를 **AI 로 안 보낸다**
      (조건이 거짓이라 send_audio 를 건너뜀). 왜? 비버 목소리가 학습자 스피커로 나가는데
      마이크가 그 소리를 다시 주워 AI 로 되돌리면, AI 가 제 목소리를 듣고 헷갈려 말이 끊기거나
      엉킨다(에코·자기간섭). 비버가 말을 마쳐 turn_id 가 None 이 되면 그때부터 다시 마이크를
      흘려보낸다. 대가: 학습자가 진짜로 끼어들어 말을 끊는 건 불가. 학습앱이라 이게 더 안전.
    """
    from starlette.websockets import WebSocketDisconnect

    try:
        while True:
            message = await client_ws.receive()
            if message.get("type") == "websocket.disconnect":
                raise _ClientDisconnect()
            data = message.get("bytes")
            # 오디오 프레임 & 비버 idle(turn_id None)일 때만 AI 로 전달 = barge-in off 의 관문.
            # ⛔ 첫 비버 턴 전에 마이크를 막아 보았다가 **되돌렸다**(2026-08-23).
            #   ①1011 을 하나도 못 줄였고(실서비스 4/4 그대로 사망)
            #   ②`cur_user_pcm` 이 안 쌓여 통화후 국적 추론이 죽었다
            #     (test_call_end_releases_pcm_but_still_feeds_nationality 가 잡았다).
            #   지금 게이트는 종전대로 barge-in off 하나뿐이다.
            if data and state.turn_id is None:
                # ⭐ 재접지를 **오디오보다 먼저** 넣는다 — 대화에 [쪽지][사용자말] 순으로
                #   앉아야 비버가 둘을 묶어 한 번 답한다(사장님 요구: "따로 넣지 말고
                #   사용자가 말하는 타이밍에 같이"). 뒤에 넣으면 말이 끊긴 뒤에 붙는다.
                if REGROUND_MODE == "on_user_turn" and REGROUND_ATTACH_AT == "mic_open":
                    await _maybe_attach_reground_on_mic(session, state, data)
                await session.send_audio(data)
                state.cur_user_pcm.extend(data)  # 통화후 국적 추론용으로 원음도 메모리에 쌓아둠
                continue
            text = message.get("text")
            if text is not None:
                await _handle_client_control(client_ws, text, state)
                continue
    except WebSocketDisconnect as exc:
        raise _ClientDisconnect() from exc


async def _handle_client_control(client_ws, text: str, state: _CallState) -> None:
    try:
        msg = client_adapter.validate_python(json.loads(text))
    except Exception as exc:  # noqa: BLE001 - 미지/깨진 제어 무시
        logger.warning("normalcall 제어 메시지 무시: %s", exc)
        return
    if msg.type == "ping":
        # ⭐ 서버 epoch ms 를 같이 준다 — 클라가 NTP 식으로 시계 오프셋을 잡아 계측
        #   이벤트를 서버 시계 위에 올린다(protocol.ServerPong.s 참조).
        await _send_json(client_ws, ServerPong(
            t=getattr(msg, "t", None),
            s=int(time.time() * 1000),
        ))
    elif msg.type == "playback_done":
        state.playback_done_event.set()
    elif msg.type == "hint_used":
        _record_hint_used(state, msg)  # 적재만(응답 불요, D16)
    elif msg.type == "client_diag":
        _record_client_diag(state, msg)  # 적재만(응답 불요 — hint_used 와 같은 규율)
    elif msg.type == "client_timing":
        _record_client_timing(state, msg)


# --------------------------------------------------------------------------- #
# 펌프: Gemini → 클라
# --------------------------------------------------------------------------- #
async def _pump_gemini_to_client(client_ws, session: LiveSessionProtocol, state: _CallState) -> None:
    """Gemini → 클라(상태기계). 턴 경계에서 세그먼트 확정 + 5분 종료 로직.

    🧒 이 펌프는 'AI → 학습자' 한 방향을 담당하며, 동시에 통화의 '심판' 역할도 한다. AI 가
      쏟아내는 이벤트(오디오 조각 / 자막 / 턴 종료 / GoAway 예고)를 하나씩 받아 학습자에게
      forward 하면서, 대화의 '턴(turn)' 상태를 관리한다. 턴 = "지금 누가 말할 차례인가".
      비버가 말하기 시작하면 turn_id 를 켜고(=발화중), 말을 마치면(turn_end) turn_id 를 끈다.
      이 turn_id 하나가 barge-in off(위 펌프의 관문)와 무음 판정·종료 타이밍을 전부 좌우한다.

    🧒 종료 규약(왜 이렇게 조심스럽게 끊나): 통화를 언제 끝낼지는 **AI 가 아니라 서버 시계**가
      정한다(프롬프트가 비버에게 통화 길이를 안 알려줘서, 비버 혼자 멋대로 작별 못 함). 끝낼
      때가 되면 시계워처가 should_close 를 세우고, "[통화종료:난수] …" 종료 시드(작별 대본)를 별도
      완결 턴으로 주입한다. 단, **비버가 조용하고(idle) 유저 턴도 닫힌 깨끗한 순간에만** 넣는다
      — 말 도중에 끼워넣으면 하던 말이 잘리거나 학습자 응답이 작별로 둔갑하기 때문. 그래서
      아래에서 turn_end(발화가 끝난 깨끗한 경계)마다 종료 여부를 판단한다.
    """
    event_count = 0
    async for event in session.events():
        event_count += 1

        # 원가 계기판(Phase 0): 과금 계측은 대화 이벤트가 아니다 — 적재만 하고 즉시 continue.
        # _forward_event 로 내려보내지 않으므로 턴 상태기계·barge-in·무음 시계·종료 규약
        # 어디에도 닿지 않는다(R4). 클라로 나가는 것도 없다(WS 프로토콜 불변).
        if event.kind == "usage":
            _record_usage(state, event.usage)
            continue

        # ⭐⭐ **표정**(2026-08-19). native-audio 는 출력이 곧 소리라 감정을 텍스트 태그로
        #   시키면 그대로 낭독한다(`_EMOTION_TAG_RULE` 이 Live 에 금지된 이유) ⇒ 표정은
        #   **function-call 로만** 나올 수 있다. 그걸 받아 클라로 마커 프레임을 흘린다.
        #
        # ⛔ **여기서 다루는 이유**: `send_tool_response` 에 `session` 이 필요한데
        #   `_forward_event(client_ws, event, state)` 에는 세션이 없다. 그리고 `usage` 와 같은
        #   규율로 즉시 continue 한다 — `_forward_event` 로 안 내려가므로 턴 상태기계·
        #   barge-in·무음 시계·종료 규약 어디에도 안 닿는다(R4).
        #
        # ⭐⭐ **순서가 전부다.** 프론트는 이 프레임을 도착 시점에 반영하지 않고 오디오 봉투에
        #   위치로 꽂아 두었다가(`at:_envAdded`) 재생이 그 지점에 닿을 때 터뜨린다. 그래서
        #   서버가 지킬 것은 "그 감정이 붙을 오디오 **앞에** 흘린다" 하나뿐이다 — Live 는
        #   Gemini→클라 펌프가 하나라 받은 순서가 그대로 나가 자동으로 지켜진다.
        #   ⚠ 실측(2026-08-18, 28호출): 27/28 이 그 턴 오디오 **0.00초** 지점에 왔다.
        #     즉 모델은 **말하기 전에** 표정을 정한다. 이 설계의 성립 조건이 그것이었다.
        if event.kind == "tool_call":
            # ⛔⛔ **어느 tool 인지 반드시 가른다**(2026-08-19, 회귀가 잡았다).
            #   이 분기는 `tool_call` 전체를 받는데, 이 프로젝트에는 표정 말고도
            #   `leveltest_ceiling_reached` 가 있다(지금은 안 쓰지만 배관은 살아 있다).
            #   이름을 안 보면 **레벨테스트 종료 신호가 표정 마커로 둔갑한다.**
            # ⛔ 스위치도 같이 본다. 꺼져 있으면 도구를 선언조차 안 하지만, "꺼짐 = 와이어
            #   무변화"는 **두 겹으로** 지킨다 — 선언과 소비 중 한쪽만 고치는 사고가 이
            #   프로젝트에서 반복됐다.
            # ⚠ `_settings` 다 — 이 모듈은 `settings as _settings` 로 받는다(:60). 펌프에는
            #   `settings` 파라미터가 없어서 그 이름을 쓰면 **NameError 로 펌프가 죽는다**
            #   (회귀가 잡았다: 마커가 0건이고 통화가 즉시 끝났다).
            is_face = event.fn_name == "set_face" and bool(_settings.LIVE_FACE_SPIKE)
            state.face_calls += 1
            if is_face:
                state.face_streak += 1
            # ⭐⭐ **폭주 차단기**(2026-08-19 실측 사고: 32초에 89회, 그동안 발화 0건).
            #   `WHEN_IDLE` 은 "하던 일 끝나면 재개"인데 턴 사이에는 **할 일이 없어 즉시
            #   재개**한다. 그런데 재개한 모델이 또 `set_face` 를 부른다 ⇒ 무한 루프.
            #   ⇒ 소리 없이 N회를 넘기면 **마커를 안 보낸다**.
            #   ⚠ 프롬프트로도 눌렀지만(한 턴 한 번) **모델이 안 지키면 그대로 재발**한다.
            #     지시는 부탁이고, 이건 계약이다.
            #   ⛔⛔ **블로킹 전환(2026-08-20)으로 이 차단기의 힘이 줄었다 — 알고 써라.**
            #     예전엔 "SILENT 로 답해 재개를 안 촉구한다"가 폭주를 실제로 끊었다. 지금은
            #     scheduling 을 안 붙이므로 그 손잡이가 없고, 남은 효과는 **마커 억제뿐**이다
            #     (클라는 안 흔들리지만 모델은 계속 부를 수 있다).
            #     ⭐ 그래도 블로킹에서는 폭주 자체가 구조적으로 어렵다: 모델이 우리 응답을
            #       기다리므로 응답 없이 89회를 쏟아내는 그 경로가 성립하지 않는다.
            #       ⇒ 실측으로 확인하기 전까지 차단기는 **남겨 둔다**. 지우지 마라.
            runaway = is_face and state.face_streak > max(1, _settings.LIVE_FACE_MAX_CONSECUTIVE)
            if runaway:
                is_face = False      # 마커도 보내지 않고, 아래 응답도 SILENT 로 내려간다
            emotion = str((event.fn_args or {}).get("emotion") or "").strip() if is_face else ""
            secs = len(state.cur_beaver_pcm) / 48000.0   # 24kHz·16bit·mono = 48,000 B/s
            # ⭐ **직전과 같으면 안 보낸다.** 실측 28회 중 7회(25%)가 같은 값 연속이었다
            #   (`surprised → surprised`). 프론트는 상태를 안 들고 오는 대로 적용하므로
            #   같은 값을 또 보내면 **영상 컨트롤러를 헛되이 흔든다** — 하드 디코더가 2~3개
            #   한계라(sync_avatar.dart:21) 이 낭비가 공짜가 아니다.
            #   ⛔ `turn_id` 로 리셋하지 않는다. 표정은 턴을 넘어 유지되는 상태다 —
            #     턴마다 비우면 다음 턴 첫 마커가 항상 중복으로 나간다.
            duplicate = emotion == state.face_last
            # ⛔⛔ **첫 인사 턴의 호출은 버린다**(2026-08-20 실측).
            #   프롬프트에 "첫 인사에서는 부르지 마라"를 넣었는데 **모델이 안 지켰다**
            #   (call 1117: 5.60초 지점에서 호출). 그리고 지시문이 예고한 그대로,
            #   인사가 한 턴 안에서 **글자까지 똑같이 두 번** 나갔다:
            #     "여보세요! What do you want to do today? ...여보세요! What do you ..."
            #     [en:144자/11.3초]  ← 첫 문장 끝(5.6초)에서 호출 → 그 뒤 반복
            #   ⭐ 지시는 부탁이고 이건 계약이다 — 폭주 차단기와 같은 규율이다.
            #     서버가 버리면 마커도 안 나가고 모델 설득에 기대지 않아도 된다.
            #
            #   ⚠ 판정 기준: **학습자가 아직 한 마디도 안 했나.**
            #     인사 턴은 정의상 학습자 발화 **이전**이다 — 이 한 값이 정확히 가른다.
            #
            #   ⛔⛔ 앞서 두 기준이 각각 실패했다. 되돌리지 마라:
            #     · `beaver_turns == 0` 만  → 모델이 turn_complete 없이 50초를 혼자 떠들면
            #       (call 1120) 영원히 인사 턴이라 중간 마커 5개가 전부 버려졌다.
            #     · + "소리 1초 미만" 을 얹음 → 이번엔 인사 턴의 **8.60초** 지점 호출이
            #       통과했고(call 1121) 그 직후 인사가 글자까지 똑같이 반복됐다.
            #       ⇒ 인사 중복은 호출 **위치와 무관하게** 첫 턴이면 일어난다.
            #
            #   ⭐ 그래서 위치가 아니라 **대화가 시작됐는가**로 가른다. call 1121 의
            #     8.60초 호출은 학습자 발화 전이라 버려지고, call 1120 처럼 학습자가 말한
            #     뒤의 긴 턴 중간 호출은 살아난다. 두 사고를 동시에 막는 유일한 기준이다.
            #   ⭐ 이어하기 조각도 자연히 맞다 — 거기엔 인사가 없고, 첫 조각에서 이미
            #     말한 학습자라도 **조각마다 새 세션**이라 첫 응답 전까지는 억제된다.
            #     조각 첫 턴 하나를 잃지만 인사 중복 위험이 0 이라 대가가 작다.
            opening = not state.learner_spoke
            # ⛔⛔ **턴이 열렸는지로 막지 않는다.** 모델은 말하기 **전에** 표정을 정하므로
            #   (실측 27/28 이 오디오 0.00초 지점) 이 시점에 `state.turn_id` 는 대개 **None**
            #   이다. 여기서 걸러내면 마커가 거의 전부 사라진다.
            #   ⚠ 그렇다고 여기서 턴을 새로 열면 더 나쁘다 — `_forward_event` 의
            #     `turn_started` 가 영영 False 가 되어 **학습자 발화 확정·통화 시계 시작**이
            #     통째로 건너뛰어진다(R4). 턴은 오디오/전사가 연다. 우리는 앞서갈 뿐이다.
            # ⭐⭐ **같은 자리에 두 번 꽂지 않는다**(2026-08-31 실측).
            #
            #   프론트는 마커를 **오디오 봉투 번호**에 꽂는다(`at=_envAdded`). 그러니 소리가
            #   한 바이트도 안 나간 사이에 두 번 부르면 **꽂히는 자리가 같아진다.**
            #   그러면 같은 밀리초에 둘 다 터지고, 뒤엣것이 앞엣것을 **즉시 덮는다**.
            #
            #   실측(call 1252):
            #     09:57:44.667  set_face(happy)   턴누적오디오=0.00초  seq=3
            #     09:57:44.668  set_face(neutral) 턴누적오디오=0.00초  seq=4   ← 0.5ms 뒤
            #     클라: mk_fire seq=3 · seq=4 가 **같은 t=103.32** → vid_emo 0건
            #   ⇒ 마커 8개가 나갔는데 화면에 뜬 것은 **3개뿐**이었다.
            #
            # ⭐ **먼저 온 것을 남긴다 — 마지막 것이 아니다.** 짝은 거의 항상
            #   `<감정> → neutral` 이라(모델이 표정을 켜고 곧바로 되돌린다), 마지막을 쓰면
            #   **neutral 만 남아 아무 표정도 안 뜬다.** 보여줄 값은 앞엣것이다.
            #   ⚠ 뒤엣것을 버려도 손해가 없다: 다음에 소리가 흐른 뒤 부르면 그때 나간다.
            #
            # ⛔ `duplicate`(같은 값 억제)와는 **다른 축**이다. 저건 값이 같을 때, 이건
            #   값이 달라도 **자리가 같을 때**다. 둘 다 필요하다.
            # ⚠ 관문은 **env 로 끈 채 시작한다**(LIVE_FACE_SLOT_MERGE=False). 위 실측이
            #   INJECT=true 구간 것이라 전제가 달라졌다 — 지금도 씹히는지 다시 재고,
            #   씹히면 켠다. 끈 상태에서도 아래 로그가 「같은 자리였다」를 남기므로
            #   **관문을 켜지 않고도 판정할 수 있다.**
            same_slot_seen = (
                state.face_last_pcm >= 0
                and len(state.cur_beaver_pcm) == state.face_last_pcm
            )
            same_slot = same_slot_seen and bool(_settings.LIVE_FACE_SLOT_MERGE)
            if emotion and not duplicate and not opening and not same_slot:
                state.face_seq += 1
                await _send_json(client_ws, ServerSentenceMarker(
                    turn_id=state.turn_id or "", seq=state.face_seq, emotion=emotion,
                ))
                state.face_last = emotion
                state.face_last_pcm = len(state.cur_beaver_pcm)
            # ⛔ `turn=` 를 뺀 채로 오래 굴렸다. 그래서 「이 마커가 의도한 턴에 떴는가」를
            #   **판정할 수 없었다** — 클라 계측에는 마커가 언제 화면에 떴는지가 남는데
            #   서버에는 그것이 어느 턴 것인지가 안 남아, 두 줄을 조인할 키가 없었다.
            #   ⚠ 빈 문자열이면 **호출 시점에 열린 턴이 없었다**는 뜻이다(모델은 말하기
            #     전에 부르므로 정상적으로 자주 그렇다). 그 사실 자체가 판정 재료다.
            logger.info(
                "normalcall 표정: %s(%s) turn=%s 턴누적오디오=%.2f초 %d번째 %s 직전전사=%r",
                event.fn_name, emotion or "?", state.turn_id or "(열린 턴 없음)",
                secs, state.face_calls,
                "⛔폭주차단(연속%d회·SILENT)" % state.face_streak if runaway else
                "⛔첫인사턴(버림)" if (emotion and opening) else
                "중복(안 보냄)" if duplicate else
                "⛔같은자리(앞엣것 유지)" if (emotion and same_slot) else
                "⚠같은자리(관문 꺼짐·그대로 보냄 seq=%d)" % state.face_seq
                if (emotion and same_slot_seen) else
                ("전송 seq=%d" % state.face_seq if emotion else "값 없음(안 보냄)"),
                ("".join(state.cur_beaver_text))[-40:],
            )
            # ⛔ 응답은 돌려준다 — 안 주면 모델 쪽에 미완 호출이 남는다.
            #   ⭐⭐ 블로킹에서는 이게 **더 강한 의무**다. 모델이 이 응답을 기다리며 멈춰
            #     있으므로, 안 주면 통화가 그 자리에서 얼어붙는다. except 로 삼키더라도
            #     로그는 반드시 남긴다(아래 warning).
            try:
                # ⛔⛔ **어댑터가 이미 답했으면 또 보내지 않는다**(2026-08-20).
                #   표정 tool 의 응답은 `gemini_live.events()` 가 **파싱 즉시** 보낸다 —
                #   여기까지 오는 동안 위쪽에서 클라 WS 마커 전송(await)과 로깅이 끼는데,
                #   그 지연이 응답을 턴 밖으로 밀어내 "두 번 말하기"를 만들었다.
                #   ⛔ 같은 fn_id 에 두 번 답하면 그 자체가 새 입력이 된다. 반드시 가른다.
                #   ⚠ 레벨테스트 종료 신호는 즉시 ack 대상이 아니라 여기서 보낸다(SILENT).
                if not getattr(event, "auto_acked", False):
                    await session.send_tool_response(
                        event.fn_id, event.fn_name, blocking=is_face)
            except Exception as exc:   # noqa: BLE001 — 표정이 통화를 죽이면 안 된다(R5)
                logger.warning("normalcall 표정: tool 응답 실패(무시) — %s", exc)
            continue

        # A3 GoAway: 서버가 곧 연결을 닫겠다는 예고(연결 ~10분 한계, S2). 뚝 끊기기 전에
        # 우리가 먼저 우아하게 마무리한다 — 기존 종료 파이프에 합류: should_close 를 세우고,
        # idle 이면 즉시 짧은 작별 시드를 주입(발화중이면 펌프가 turn_end 에서 주입).
        if event.kind == "go_away":
            # ⭐ 예전엔 "재연결이 가능하면 연결 교체" 갈래가 있었다. 재연결 기계가 사라지면서
            #   (설계 §8-b) 이제 GoAway 는 **언제나 우아한 마무리**로 간다.
            # ⚠ 조각 설계에서는 그게 오히려 맞다: GoAway 가 4분에 와도 조각을 거기서 끝내고
            #   "이어서 하시겠습니까?"를 띄우면 사용자가 새 세션으로 잇는다 — 이어하기가
            #   재연결이 하던 일을 그대로 한다.
            logger.warning(
                "normalcall: GoAway 수신(time_left=%s) → 종료 절차", event.time_left,
            )
            state.should_close = True
            if state.turn_id is None:
                await _inject_close_seed(session, state)
            continue

        # ⛔ `interrupted` 를 **아무 분기도 안 받고 있었다**(2026-08-26 확인).
        #   어댑터는 만들어 낸다(`gemini_live.py:520`) — 소비만 0이다. 그러니 예외도
        #   경고도 없이 조용히 사라진다. 그게 위험한 이유:
        #     모델이 자기 턴을 버렸는데 **클라 큐에는 그 오디오가 남아 있다.**
        #     barge-in off 라 우리가 끊는 일은 없지만, 모델이 스스로 끊을 수는 있다.
        #     그러면 비버가 **버려진 문장을 계속 말한다.**
        # ⭐ 다만 **한 번도 관측된 적이 없다.** 본 적 없는 것에 처리기를 짓지 않는다 —
        #   먼저 보이게 만든다. 이 줄이 실제로 찍히면 그때 처방을 고른다.
        if event.kind == "interrupted":
            # ⛔⛔ **분기가 두 개면 안 된다**(2026-09-03 dev 병합). dev 가 같은 신호를
            #   `_forward_event` 안에도 달았는데, 이 분기가 `continue` 하므로 그쪽은
            #   **영원히 도달 불가**였다 — 죽은 계측은 "재고 있다"는 착각만 만든다.
            #   그래서 그쪽을 지우고 dev 가 세던 값을 여기로 옮겼다.
            #   (`diag_interrupts` 는 아래 「턴 절단 의심」 판정이 읽는다 — 안 옮기면
            #    그 판정이 언제나 0을 본다.)
            state.diag_interrupts += 1
            _open = state.diag_turn_open_ts
            _el = (asyncio.get_running_loop().time() - _open) if _open else -1.0
            logger.warning(
                "normalcall ⛔interrupted #%d: 모델이 자기 턴을 버렸다 turn=%s "
                "(경과 %.2fs, 오디오 %dB) "
                "(클라 큐에 남은 오디오는 그대로 재생된다 — 처리기 없음)",
                state.diag_interrupts, state.turn_id or "(열린 턴 없음)",
                _el, state.diag_audio_bytes,
            )
            continue

        # 세션 재개 핸들 갱신. resumable=False 는 "지금 상태로는 재개 불가"(모델 생성 중·
        # tool 실행 중)라는 뜻이라 **덮어쓰지 않는다** — 그 상태로 재개하면 데이터가 유실된다.
        if event.kind == "resume_update":
            if event.resumable and event.resume_handle:
                first = state.resume_handle is None
                state.resume_handle = event.resume_handle
                if first:
                    logger.info("normalcall: 세션 재개 핸들 수신(epoch=%d)", state.session_epoch)
            continue

        # 재접지 얹기(on_user_turn): "첫 in_tr"(유저 발화 시작) 판별은 _forward_event 가
        # user_turn_open 을 True 로 바꾸기 전에 해야 한다.
        in_tr_first = event.kind == "in_tr" and not state.user_turn_open

        turn_started = await _forward_event(client_ws, event, state)

        if turn_started:
            # 레벨테스트 밴드 관측(무주입): 비버 응답 시작 = 직전 유저 답변 마침 → flush 로
            # cur_user_text 가 비워지기 전에 답변을 캡처해 관측 사이드카를 띄운다(논블로킹).
            _spawn_band_observe(state)
            _flush_user_segment(state)  # 비버 발화 시작 → 직전 사용자 세그먼트 확정
            _expression_quiz_open_on_beaver_turn(state)  # T16 — 큐가 얹힌 뒤 첫 비버 턴 = 퀴즈 창 시작(open_seg)
            state.user_turn_open = False  # 비버가 응답 시작 = 유저 발화 턴 종료
            if state.call_start_ts is None:
                state.call_start_ts = asyncio.get_running_loop().time()
                logger.info("normalcall: 통화 시계 시작(첫 turn_start)")
            if state.close_seed_sent:
                # 종료 시드 후 비버가 실제 작별 턴을 시작했다 — 이 턴 끝에서만 종료.
                state.close_reply_started = True

        # ── 재접지 "유저 발화 턴에 얹기"(on_user_turn) ──
        # arm 됐으면(reground_pending) 유저 발화 턴에 리마인더를 turn_complete=False 로 얹어
        # 비버가 [유저발화+리마인더]에 1회 응답하게 한다(이중발화·잔류 제거 목표).
        # ⛔ 가드①(핵심 안전): should_close/close_seed_sent 면 절대 안 얹음 — 종료 근처 늦은
        #    in_tr 이 작별 턴을 오염(174/178 재발)하는 것을 원천 차단.
        # ⛔ 가드②: 대기 중인 arm 하나당 정확히 1회(reground_pending 을 await 전에 내린다).
        #    ⚠ 옛 게이트(reground_injected, 통화당 1회성)를 쓰지 마라 — 중반 재접지가 얹히면
        #      True 로 굳어 **후반 리마인더가 영영 안 얹혔다**. 횟수 상한은 arm 쪽(_reground_due)이
        #      REGROUND_MAX_PER_CALL 로 건다.
        # ⛔ 옛 자리(입력 전사)다. `mic_open` 에서는 **안 돈다** — 전사는 비버 답과 같이
        #   오는 늦은 보고서라 얹는 순간 비버가 이미 입을 연 뒤일 수 있다(실측 6/82 잘림).
        #   되돌릴 길로만 남긴다(`LIVE_REGROUND_ATTACH_AT=first|final`).
        if (REGROUND_MODE == "on_user_turn" and REGROUND_ATTACH_AT in ("first", "final")
                and event.kind == "in_tr"):
            attach_now = in_tr_first if REGROUND_ATTACH_AT == "first" else bool(event.is_final)
            if attach_now:
                await _attach_reground(session, state, f"전사·{REGROUND_ATTACH_AT}")

        if event.kind == "turn_end":
            _spawn_hint_task(client_ws, state)  # D16 힌트 사이드카 — 태스크 생성만(논블로킹)
            # ⭐ 자기낭독 안전망: flush 전(누적 텍스트가 살아 있을 때) 태그 누출을 판정한다.
            _detect_tag_leak(state)
            # ⭐ flush 가 비우기 **전에** 이 턴의 오디오 양을 잰다(아래 재시드 판정 재료).
            turn_pcm_bytes = len(state.cur_beaver_pcm)
            _flush_beaver_segment(state)
            # ⭐⭐ **벙어리 인사 재시드**(2026-08-27 실측). 자세한 근거는 아래 함수 주석.
            reseeded_now = _greeting_was_mute(state, turn_pcm_bytes)
            if reseeded_now:
                await _reseed_greeting(session, state)
            if state.close_seed_sent:
                # ⭐ 작별 턴이 실제로 시작됐을 때만 종료. 그 전(빈 turn_end — 이전 활동 잔여)
                # 이면 무시하고 작별을 기다린다(조기 종료로 작별 인사 잘림 방지). 작별이 끝내
                # 안 오면 _watch_call_clock 의 SEED_TO_HANGUP_S 백스톱이 강제 종료(무한대기 X).
                if state.close_reply_started:
                    logger.info("normalcall: 작별 발화 종료 → 종료 절차")
                    raise _CallFinished()
                logger.info("normalcall: 종료 시드 후 빈 turn_end — 작별 발화 대기(조기종료 방지)")
            elif state.should_close:  # 비버 발화중 경로: 이 턴 끝에서 주입(턴 안 자름)
                await _inject_close_seed(session, state)
            elif state.tag_leak_seen:
                # 서버는 아직 끝낼 생각이 없는데 비버가 제어 태그를 읽었다 → 되돌린다.
                # (should_close/close_seed_sent 경로가 위에서 먼저 걸리므로 여기는 '정상
                #  진행 중'인 경우뿐 — 정상 작별을 이 시드가 덮어쓰는 일은 없다.)
                await _inject_resume_seed(session, state)

    # 스트림이 끝났다. 통화가 아직 살아 있고 재개가 가능하면 종료가 아니라 교체다 —
    # 저쪽이 예고 없이 끊는 경우(네트워크·서버 재시작)가 여기로 온다.
    logger.warning("normalcall: Live 이벤트 스트림 종료(서버측 close) events=%d", event_count)
    raise _CallFinished()


def _greeting_subtitle_on_hold(state: _CallState) -> bool:
    """지금 온 자막 조각을 **붙잡아야 하나** — 첫 인사 턴이고 아직 소리가 없을 때만.

    ⛔ 왜 필요한가(2026-09-03, call 1281): 벙어리 인사는 **자막만 뜨고 소리가 없다.**
      그다음 재시드가 같은 인사를 소리와 함께 다시 한다 ⇒ 사장님 눈에는 «말한 걸 또
      말한다»로 보인다. 감지는 옳았고(연결 0.85초·0바이트 = 문서화된 벙어리 서명),
      틀린 것은 **소리를 확인하기도 전에 화면에 그린 것**이다.

    ⚠ 전제를 `_greeting_was_mute` 와 **일부러 맞춘다** — 그쪽이 재시드할 턴에서만
      붙잡아야 한다. 둘이 어긋나면 재시드는 안 하는데 자막만 사라지는 구멍이 난다.
      다른 점은 하나뿐이다: 저쪽은 **끝난 턴**을 보므로 `beaver_turns == 1`,
      이쪽은 **진행 중인 턴**을 보므로 `beaver_turns == 0` 이다(:795 에서 턴 끝에 +1).

    ⚠ 비용: 실측 111건 중 110건이 오디오가 전사보다 **0~2ms 먼저** 온다
      (`+0ms` 53 · `+1ms` 55 · `+2ms` 2, 최대 `+29ms` 1건). 정상 턴에서는 붙잡는
      순간이 거의 없다. 그리고 인사 구간에만 걸리므로 통화 중 자막은 영향이 없다.
    """
    return (
        state.session_epoch == 1          # 재개 세대엔 선톡 자체가 없다
        and not state.greeting_reseeded   # 이미 재시드했으면 그 턴은 진짜 소리가 난다
        and not state.learner_spoke       # 학습자가 말했으면 인사 구간이 아니다
        and state.beaver_turns == 0       # 아직 첫 비버 턴이 안 끝났다
        and not state.face_first_audio_at  # ⭐ 이 턴에 소리가 한 바이트도 안 왔다
    )


def _greeting_was_mute(state: _CallState, turn_pcm_bytes: int) -> bool:
    """방금 끝난 턴이 **소리 없는 첫 인사**였나.

    ## 무엇을 보고 판정하나

    선톡 시드로 시작한 **첫 비버 턴**이 오디오 한 바이트 없이 끝났으면 벙어리다.
    학습자는 자막만 보고 아무 소리도 못 듣는다.

    ## ⛔ 실측 (2026-08-25~27, app-api 통화 11건)

        연결됨 → 첫 턴 완료
          벙어리  0.68 · 0.69 · 0.71 · 0.72 · 0.72 · 0.97초   → 오디오 0.0초
          정상    6.63 · 8.20 · 8.22 · 8.52 · 9.70초          → 오디오 5.8~9.0초
        겹치는 구간이 없다. 발생률 6/11.

    벙어리 턴은 **109자 전사가 한 덩어리로** 오고 44ms 뒤 turn_complete 가 붙는다.
    정상이면 전사가 조각 14개로 2초에 걸쳐 오고 그동안 오디오가 흐른다.

    ⚠ 배제한 것(전부 로그 근거):
      · set_face 아니다 — 그 시각 tool 호출 0건(첫 호출은 11초 뒤)
      · interrupted 아니다 — 해당 구간 경고 0건
      · 글자 수 아니다 — 벙어리 99~116자 / 정상 100~158자로 겹친다
      · 우리 배관 아니다 — usage 가 `sum_resp=0 sum_out=-`. 흘릴 바이트가 없었다

    ⇒ **원인은 아직 모른다.** 벤더가 오디오 없는 턴을 돌려준다는 것까지가 확정이다.
      원인 규명과 **별개로** 증상을 끊는다.

    ## ⭐ 왜 재시드가 통할 것이라 보나

    같은 세션에서 **첫 턴만** 벙어리고 그다음 턴은 멀쩡하다:

        call 1210  t1 오디오 0.0초 ⛔ → t3 오디오 6.5초 ✅
        call 1211  t1 벙어리 → 이후 240초 오디오 3,343토큰 정상
        call 1224  t1 벙어리 → 이후 185초 오디오 2,377토큰 정상

    세션이 망가진 게 아니라 시드로 연 그 한 턴만 비었다.
    ⚠ 다만 t3 는 **학습자 음성**이 연 턴이고 재시드는 텍스트 턴이라 같은 종류가
      아니다 — 근거지 증명이 아니다. 실통화가 판정한다.

    ## ⛔ 새로 걸어도 안 낫는다 — 그래서 서버가 고쳐야 한다

        16:04:03 걸기 → 벙어리 → 끊음 / 16:04:22 다시 걸기 → 또 벙어리
    실측 3쌍이 3번 다 그랬다. "다시 걸어보세요"는 답이 아니다.
    """
    return (
        state.session_epoch == 1        # 재개 세대엔 선톡 자체가 없다
        and not state.greeting_reseeded  # 통화당 1회
        and not state.learner_spoke      # 학습자가 말했으면 인사 구간이 아니다
        and state.beaver_turns == 1      # 방금 끝난 것이 첫 비버 턴
        and turn_pcm_bytes == 0          # 그 턴에 소리가 없었다
        and not state.should_close
        and not state.close_seed_sent
        and bool(state.seed_text)
    )


async def _reseed_greeting(session: LiveSessionProtocol, state: _CallState) -> None:
    """벙어리로 끝난 첫 인사를 **같은 시드로 한 번 더** 부른다.

    ⭐ 원가가 거의 0이다 — 벙어리 턴은 출력 토큰이 0으로 청구된다(`sum_resp=0`).
      실패해도 잃는 게 없고, 성공하면 학습자가 인사를 듣는다.

    ⛔ **`turn_complete=True` 여야 한다**(= [send_text_turn]). 이 파일이 실통화 4건으로
      못박아 둔 것은 «`turn_complete=False` 로 열어 둔 턴에 마이크 무음이 흘러드는 동안
      1011 로 죽는다»이다(call 1142/1143/1150~1154). 완결 턴은 그 창을 안 만든다 —
      종료 시드([_inject_close_seed])가 **같은 자리(turn_end)에서 매 통화** 그렇게
      주입되고 있고 사고가 없다. 그 전례 위에 선다.

    ⚠ [state.beaver_turns] 는 건드리지 않는다. 재시드가 만든 턴도 «인사 턴»이라
      표정 마커 억제([state.learner_spoke] 기준)가 그대로 걸려야 한다.
    """
    state.greeting_reseeded = True  # await 전 선점 — 재진입해도 두 번 안 보낸다
    try:
        await session.send_text_turn(state.seed_text)
        logger.warning(
            "normalcall: ⛔벙어리 인사 감지(오디오 0바이트) → 선톡 시드 재전송 1회 "
            "turns=%d epoch=%d", state.beaver_turns, state.session_epoch,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — 재시드 실패로 통화를 죽이지 않는다(R5)
        logger.warning("normalcall: 인사 재시드 실패(무시): %s", exc)


async def _forward_event(client_ws, event: LiveEvent, state: _CallState) -> bool:
    """단일 LiveEvent 를 즉시 forward 하며 진행중 세그먼트에 누적. 새 턴이면 True.

    🧒 왜 '즉시 forward'가 중요한가: 전화에서 상대 목소리가 0.5초라도 늦게 오면 뚝뚝 끊겨
      들린다. 그래서 오디오 조각이 오면 **먼저 학습자에게 send_bytes 로 밀어주고**(반응성
      최우선), 그 다음에 나중 저장·분석용으로 메모리 버퍼에 복사한다. 순서를 반대로 해서
      "저장 먼저, 전송 나중"으로 하면 매 조각마다 아주 살짝 지연이 쌓여 끊김으로 들린다.

    🧒 '턴 시작' 감지: 오디오나 자막(out_tr)의 **첫 이벤트**가 왔는데 turn_id 가 아직 없으면,
      "비버가 지금 막 말을 시작했다"는 뜻이다. 그 순간 turn_id 를 새로 켜고 클라에 turn_start
      를 보내며 True(=새 턴 시작)를 돌려준다. 호출부는 이 True 로 '직전 학습자 발화 확정'과
      '통화 시계 시작' 같은 턴 경계 처리를 한다.
    """
    turn_started = False

    if event.kind == "audio":
        if state.turn_id is None:
            state.turn_id = _new_turn_id()
            await _send_json(client_ws, ServerTurnStart(turn_id=state.turn_id))
            turn_started = True
        if event.audio:
            # 🔬 ⛔ 턴이 열리는 경로는 **둘**이다(audio / out_tr). 자막이 먼저 오는 통화가
            #   대부분이라 audio 분기에서만 초기화하면 영영 리셋이 안 된다(2026-08-31 실측:
            #   공백이 매 턴 7~17초로 찍히고 누적 바이트가 단조증가했다). ⇒ "아직 안 열렸으면
            #   지금 연다"로 바꾸고, 실제 리셋은 turn_end 가 소유한다.
            if state.diag_turn_open_ts is None:
                state.diag_turn_open_ts = asyncio.get_running_loop().time()
            # 🔬 조각 간 공백 — 이벤트 루프 블로킹/모델 스톨을 가른다. 임계 넘을 때만 찍는다.
            _now = asyncio.get_running_loop().time()
            if state.diag_last_audio_ts is not None:
                _gap = _now - state.diag_last_audio_ts
                if _gap >= 1.0:
                    logger.warning("🔬 오디오 공백 %.2fs (턴 %s, 누적 %dB)",
                                   _gap, state.turn_id, state.diag_audio_bytes)
            state.diag_last_audio_ts = _now
            state.diag_audio_bytes += len(event.audio)
            # ⭐ 소리가 나왔다 = 모델이 정상으로 돌아왔다 ⇒ 표정 폭주 카운터를 푼다.
            #   ⛔ 턴 경계가 아니라 **오디오**로 푼다. 폭주는 턴 사이에서 나므로
            #     턴 경계로 풀면 차단기가 영영 안 걸리거나 매번 풀린다.
            state.face_streak = 0
            # ⭐⭐ **표정 v2 계측**(2026-08-20). tool-call 이 3번 실패해서 남은 길은
            #   "출력 전사로 감정을 뽑아 그 자리에 마커를 끼우는 것"인데, 그게 성립하려면
            #   **전사가 그 오디오보다 먼저 와야** 한다(프론트가 마커를 오디오 위치에 꽂는다).
            #   ⛔ 이건 문서로 못 정한다. 두 이벤트가 같은 펌프를 지나므로 도착 시각을
            #     재면 바로 답이 나온다. 그 한 숫자가 v2 설계의 성립 여부를 정한다.
            if not state.face_first_audio_at:
                state.face_first_audio_at = asyncio.get_running_loop().time()
                # ⭐⭐ **체감 지연 계측**(2026-08-21). 학습자가 말을 끝낸 뒤 비버의 첫
                #   소리가 나오기까지. 이 앱에서 "느리다"의 정의가 정확히 이것이다.
                #   ⛔ 로그에서 역산하려 하지 마라 — "턴 종료 − 오디오 길이"로 재면
                #     **음수가 나온다**(입력 전사가 늦게 와서 학습자 발화 시각이 뒤로
                #     밀린다). 실제로 그렇게 재다 5.8초 음수가 나왔다.
                #   ⚠ 백엔드 비교의 근거가 된다: 오프라인 실측은 1턴에서 Vertex 0.9초
                #     vs AI Studio 6.5초였는데, 2턴 이후는 하네스 적체로 판정 못 했다.
                #   ⛔⛔ **끝기준 하나만 찍지 마라 — 3.1 에서 무너진다**(2026-08-24 실측).
                #     `learner_last_tr_at` 은 매 in_tr 마다 갱신되는데, 3.1 은 학습자
                #     전사의 **마지막 조각을 자기 응답과 같이** 내보낸다. 그래서 끝기준이
                #     4ms 로 붙어 `응답지연: 0.00초` 가 5턴 내내 찍혔다 — 즉답이 아니라
                #     **기준점이 사라진 것**이다(2.5 는 전사가 350~1856ms 먼저 왔다).
                #   ⇒ 시작기준(학습자 첫 전사)을 같이 찍는다. 학습자 발화 길이가 섞이지만
                #     **무너지지 않는다** — 두 값을 같이 봐야 해석이 된다.
                if state.learner_first_tr_at is not None or state.learner_last_tr_at is not None:
                    since_start = (
                        state.face_first_audio_at - state.learner_first_tr_at
                        if state.learner_first_tr_at is not None else float("nan")
                    )
                    since_end = (
                        state.face_first_audio_at - state.learner_last_tr_at
                        if state.learner_last_tr_at is not None else float("nan")
                    )
                    # 끝기준이 0 에 붙었다 = 전사가 응답과 같이 왔다 = 그 숫자는 못 믿는다.
                    collapsed = since_end == since_end and since_end < 0.05
                    logger.info(
                        "normalcall 응답지연: 시작기준 %.2f초 · 끝기준 %.2f초%s turn=%s",
                        since_start, since_end,
                        " ⛔끝기준 무의미(전사가 응답과 동시 도착)" if collapsed else "",
                        state.turn_id or "-",
                    )
                    state.learner_last_tr_at = None    # 턴당 한 번만
                    state.learner_first_tr_at = None
            await client_ws.send_bytes(event.audio)  # forward 먼저(반응성 우선) → 그 다음 버퍼 누적
            state.cur_beaver_pcm.extend(event.audio)

    elif event.kind == "in_tr":
        text = event.text or ""
        # A2: 입력 전사 = 학습자 활동 → 무음 시계 리셋 + 넛지 단계 원복(발화 재개).
        state.last_activity_ts = asyncio.get_running_loop().time()
        # ⭐ 시작기준 앵커 — 이 학습자 턴의 **첫** 전사에서만 찍는다(끝기준과 짝).
        #   `user_turn_open` 은 바로 아래에서 True 가 되므로 여기서 보면 아직 이전 값이다.
        if not state.user_turn_open:
            state.learner_first_tr_at = state.last_activity_ts
        state.learner_last_tr_at = state.last_activity_ts
        state.silence_stage = 0  # 발화 재개 → 넛지 단계 리셋
        state.user_turn_open = True  # 유저 발화 턴 열림(비버 turn_start 시 flush 에서 False)
        # ⭐ 학습자가 말했다 = 인사 구간이 끝났다. 표정 마커를 이제부터 내보낸다.
        state.learner_spoke = True
        await _send_json(client_ws, ServerInputTranscript(text=text))
        if text:
            _on_learner_transcript_piece(state, text)

    elif event.kind == "out_tr":
        if state.turn_id is None:
            state.turn_id = _new_turn_id()
            await _send_json(client_ws, ServerTurnStart(turn_id=state.turn_id))
            turn_started = True
        text = event.text or ""
        if not state.face_first_tr_at and (text or "").strip():
            state.face_first_tr_at = asyncio.get_running_loop().time()
        # ⛔ **자막으로 나가기 전에** tool 낭독을 걷어낸다(2026-09-01). 여기서 안 막으면
        #   `set_face{emotion:sad}` 가 화면에 그대로 뜬다 — 사장님이 실제로 보셨다.
        #   ⚠ 전사는 조각으로 오므로 한 조각 안에 온전히 들어온 것만 잡힌다. 조각 경계에
        #     걸쳐 쪼개진 것은 못 잡는다 — 그건 아래 저장 경로가 한 번 더 거른다.
        text = _strip_face_echo(text)
        if not text:
            return turn_started   # 낭독만 있던 조각 — 빈 자막을 보내지 않는다
        # ⭐⭐ **벙어리 인사의 자막은 내보내지 않는다**(2026-09-03 실측, call 1281).
        #
        #   ⛔ 증상: 사장님이 "자기가 말하고 그거 그대로 다시 듣는다"고 하셨다. 실제로는
        #     ①벙어리 인사의 **자막만** 뜨고(소리 없음) ②재시드가 같은 인사를 **소리와 함께**
        #     다시 하는 것이었다. 클라는 새 turn_start 에서 자막을 리셋하므로 같은 문장이
        #     두 번 그려진다 ⇒ 말한 걸 또 말하는 것으로 보인다.
        #
        #   ⭐ 판정 자체는 옳았다 — 연결 0.85초 만에 0바이트로 끝났고, 이건 문서화된
        #     벙어리 서명 그대로다(벙어리 0.68~0.97초 / 정상 6.63~9.70초, 겹침 없음).
        #     고칠 것은 감지가 아니라 **아직 소리를 못 들은 인사를 화면에 먼저 그린 것**이다.
        #
        #   ⇒ 첫 인사 턴에 한해 **오디오 첫 바이트가 오기 전의 자막을 붙잡는다.**
        #     소리가 오면 붙잡아 둔 것을 즉시 흘리고 그 뒤로는 평소대로 스트리밍한다.
        #     벙어리로 끝나면 붙잡은 채로 버려진다 — 유령 자막이 안 뜬다.
        #
        #   ⚠ 비용이 0에 가깝다는 근거: 실측 111건 중 110건이 **오디오가 전사보다
        #     0~2ms 먼저** 온다(`전사-오디오=+0ms` 53건 · `+1ms` 55건 · `+2ms` 2건).
        #     즉 정상 턴에서는 붙잡는 순간이 아예 없거나 2ms다. 최대값도 29ms 1건뿐.
        #   ⚠ 인사 구간에만 건다 — `learner_spoke` 이후엔 이 경로를 안 탄다. 통화 중
        #     자막이 늦어지는 일은 없다.
        if _greeting_subtitle_on_hold(state):
            state.greeting_held_text.append(text)
            state.cur_beaver_text.append(text)   # 저장·분석은 그대로 받는다
            logger.info("normalcall 🦫 beaver(자막보류·소리대기): %s", text)
            return turn_started
        if state.greeting_held_text:
            # 소리가 왔다 = 벙어리가 아니다 ⇒ 붙잡아 둔 자막을 한 번에 흘린다.
            held = "".join(state.greeting_held_text)
            state.greeting_held_text.clear()
            await _send_json(
                client_ws, ServerOutputTranscript(text=held, turn_id=state.turn_id)
            )
        await _send_json(client_ws, ServerOutputTranscript(text=text, turn_id=state.turn_id))
        if text:
            state.cur_beaver_text.append(text)
            logger.info("normalcall 🦫 beaver: %s", text)

    elif event.kind == "turn_end":
        turn_id = state.turn_id or _new_turn_id()
        # ⭐⭐ **표정 v2 의 성립 조건을 한 줄로 답한다**(2026-08-20).
        #   프론트는 마커를 **오디오 위치**에 꽂는다(at:_envAdded → 재생이 닿을 때 반영).
        #   그러니 전사에서 감정을 뽑아 마커를 끼우려면 **전사가 그 오디오보다 먼저** 와야 한다.
        #     음수(전사가 먼저)  → ✅ v2 성립. 전사 자리에 마커를 끼우면 소리와 맞는다
        #     양수(오디오가 먼저) → ⛔ 표정이 그만큼 늦는다. 다른 길을 찾아야 한다
        #   ⚠ 두 이벤트는 **같은 펌프**를 순서대로 지나므로(:2549 audio · :2573 out_tr)
        #     이 차이가 곧 Gemini 가 준 순서다 — 우리 큐가 섞은 값이 아니다.
        if state.face_first_audio_at or state.face_first_tr_at:
            a, t = state.face_first_audio_at, state.face_first_tr_at
            logger.info(
                "normalcall 표정계측: turn=%s 전사-오디오=%s (전사=%s 오디오=%s)",
                turn_id,
                "%+.0fms" % ((t - a) * 1000) if (a and t) else "한쪽만",
                "있음" if t else "없음", "있음" if a else "없음",
            )
        # ⛔ 붙잡아 둔 인사 자막은 **여기서 버린다.** 소리가 왔다면 위에서 이미 흘렀고,
        #   안 왔다면 그게 벙어리 턴이라 화면에 그리면 안 된다(재시드가 다시 말한다).
        #   다음 턴으로 새면 엉뚱한 턴에 옛 인사가 붙는다.
        if state.greeting_held_text:
            logger.info(
                "normalcall: 벙어리 인사 자막 %d조각 폐기(소리 없이 턴 종료) turn=%s",
                len(state.greeting_held_text), turn_id,
            )
            state.greeting_held_text.clear()
        state.face_first_audio_at = 0.0
        state.face_first_tr_at = 0.0
        await _send_json(client_ws, ServerTurnEnd(turn_id=turn_id))
        state.last_turn_id = turn_id  # D16: 방금 끝난 턴 id 보존(힌트 태스크 재료)
        # 🔬 절단 판정 — 오디오가 유난히 짧게 끝난 턴을 서버가 스스로 표시한다.
        #   PCM24k mono 16bit = 48,000 B/s ⇒ 1초 미만 = 48,000B 미만.
        if state.diag_turn_open_ts is not None and 0 < state.diag_audio_bytes < 48000:
            _el = asyncio.get_running_loop().time() - state.diag_turn_open_ts
            logger.warning("🔬 턴 절단 의심: 턴 %s 오디오 %dB(%.2fs분) 벽시계 %.2fs 인터럽트누적=%d",
                           turn_id, state.diag_audio_bytes,
                           state.diag_audio_bytes / 48000.0, _el, state.diag_interrupts)
        state.diag_turn_open_ts = None
        state.diag_last_audio_ts = None
        state.diag_audio_bytes = 0
        state.turn_id = None
        # ⭐ 무음 시계 리셋: 비버가 방금 말을 멈췄다 = 여기서부터 무음이 시작된다.
        # (안 하면 시계가 통화 시작부터 흘러 비버의 긴 발화 직후 넛지가 즉시 터진다.)
        state.last_activity_ts = asyncio.get_running_loop().time()

    return turn_started


def _detect_tag_leak(state: _CallState) -> None:
    """방금 끝난 비버 턴에 서버 제어 태그가 섞였는지 판정한다(누적 텍스트 기준).

    🧒 왜 누적 텍스트인가: 출력 자막(out_tr)은 토큰 단위로 쪼개져 온다 — 실측 로그에서
      "[시스템]" 과 " 통화가" 가 별개 이벤트로 왔다. 조각 하나만 보면 대괄호가 갈라져
      정규식이 못 잡는다. 그래서 턴이 끝나는 순간, 그 턴의 조각을 전부 이어 붙인
      문자열에 대해 한 번만 검사한다.

    정상 종료 구간(should_close/close_seed_sent)에서는 판정하지 않는다 — 그땐 서버가
    의도적으로 마무리시키는 중이라, 되돌리면 작별을 방해한다.
    """
    if state.should_close or state.close_seed_sent:
        return
    text = "".join(state.cur_beaver_text)
    match = _find_control_tag_leak(text, state.expr_tag_allow)
    if not match:
        return
    # ⭐ T17-1 (통화 1406 t16~t21) — 비버가 «[퀴즈 시작]/[퀴즈 계속]» 같은 단계 표시를 **말하면서** 붙였다. 옛 코드는
    #   태그가 보이면 무조건 재개 시드를 넣어 1.5초 간격으로 질문을 4번 연속 냈다(_RESUME_MAX 6 까지). 재개 시드는
    #   원래 «태그를 낭독하느라 소리가 0초인 벙어리 턴»(call 706 · 위 안전망 주석) 용이다. 그 턴에 **소리가 있고**
    #   태그를 뺀 본문이 남으면 비버는 대화를 하고 있는 것이다 — 저장본 스크럽만 하고 되돌리지 않는다.
    audio_s = audio.output_audio_s(len(state.cur_beaver_pcm))
    body = _scrub_control_tags(text, state.expr_tag_allow).strip()
    if audio_s >= TAG_LEAK_SPOKEN_MIN_S and body:
        logger.info(
            "normalcall: 제어 태그 스크럽만(소리 %.1f초·본문 %d자 — 벙어리 턴이 아니라 재개 시드 생략): %r",
            audio_s, len(body), match.group(0),
        )
        return
    state.tag_leak_seen = True
    logger.warning(
        "normalcall: 제어 태그 누출 감지(비버가 지시문을 낭독, 소리 %.1f초) — 대화 재개 시도: %r",
        audio_s, match.group(0),
    )


async def _inject_resume_seed(session: LiveSessionProtocol, state: _CallState) -> None:
    """태그를 낭독한 비버를 대화로 되돌린다(자기낭독 안전망의 실행부).

    상한(_RESUME_MAX)을 두는 이유: 되돌리기 자체도 텍스트 주입이라, 비버가 그것마저
    읽는 병리 상태가 되면 무한 왕복이 된다. 상한을 넘으면 로그만 남기고 통화를 그대로
    둔다 — 시계·무음 넛지·백스톱이 여전히 정상 종료를 책임진다(R5).
    """
    state.tag_leak_seen = False  # 판정은 턴 단위 — 매번 리셋
    if state.resume_sent >= _RESUME_MAX:
        logger.warning("normalcall: 태그 누출 재개 상한(%d) 도달 — 주입 생략", _RESUME_MAX)
        return
    state.resume_sent += 1
    try:
        await session.send_text_turn(_RESUME_SEED)
        logger.info("normalcall: 대화 재개 시드 주입(%d/%d)", state.resume_sent, _RESUME_MAX)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 재개 실패는 통화 무영향(R5)
        logger.warning("normalcall: 대화 재개 시드 주입 실패(무시): %s", exc)


# 조각 하나당 재시도 상한. _RESUME_MAX 와 같은 규율 — 무한 왕복을 만들지 않는다.
_PERSONA_PART_MAX_RETRY = 2
_PERSONA_PRE_FIRST = (
    f"{CONTROL_TAG} 이 문구는 읽지 마라. 아래는 지금부터 지킬 네 운영 규칙이다. "
    "통화는 이미 진행 중이다 — 방금 학습자가 한 말에 그대로 이어서, 규칙대로 대답해라.\n"
)
_PERSONA_PRE_NEXT = (
    f"{CONTROL_TAG} 이 문구는 읽지 마라. 규칙이 이어진다 — "
    "방금 학습자가 한 말에 그대로 이어서 대답해라.\n"
)
# ⛔ "다시 인사하지 마라" 같은 **부정 지시 + 금지어**를 쓰지 마라. 이 코드베이스가 세 번
#   당했다(call 836/744/782) — 금지 예시가 씨앗이 돼서 모델이 그대로 뱉는다. 대신 첫 행동을
#   지정한다("방금 학습자가 한 말에 그대로 이어서"). seed_resume 이 쓰는 그 규율과 같다.
# ⛔ 접두어는 CONTROL_TAG 다. 종료 태그와 절대 공유하지 마라(docs/20260727_1710). 이걸 써야
#   비버가 낭독해도 _CONTROL_TAG_RE 안전망이 저장본에서 걷어낸다.


def _request_close(state: _CallState) -> None:
    """통화를 마무리해 달라고 **요청**한다(TaskGroup 밖 사이드카 전용, 세션 무접촉).

    should_close 플래그 + close_requested 이벤트를 함께 세운다. 플래그만 세워도 워처들이
    다음 폴링(0.2s)에 알아채지만, 이벤트를 같이 깨우면 시계워처가 **즉시** 종료 시드 주입
    단계로 넘어간다 — 예전에 사이드카가 세션을 직접 잡고 주입해 벌었던 지연차(B2)를
    죽은 세션 참조 없이 되돌려준다. 주입 자체는 언제나 살아 있는 세대(워처·펌프)의 몫.
    """
    state.should_close = True
    state.close_requested.set()


async def _inject_close_seed(session: LiveSessionProtocol, state: _CallState) -> None:
    """종료 시드를 정확히 1회만 주입한다(펌프·워처 공용, 단일 소유권 가드).

    🧒 왜 '딱 1회' 가드가 필요한가: 종료 시드(작별 대본)를 넣을 수 있는 후보가 둘이다 —
      펌프(비버가 말을 마친 turn_end 에서)와 시계워처(비버가 조용할 때 직접). 둘이 동시에
      "지금이야!" 하고 넣으면 비버가 작별을 두 번 하는 사고가 난다. 그래서 실제로 보내기 전
      (await 전)에 close_seed_sent 깃발을 먼저 꽂아, 다른 쪽이 들어와도 '이미 보냄'을 보고
      돌아가게 한다. asyncio 는 한 번에 한 줄만 실행(단일 스레드)이라 이 '먼저 깃발 꽂기'만으로
      경합이 안전하게 막힌다(락 불필요). '단일 소유권 가드' = 이 일의 주인은 딱 한 명이 되게.
    단일 스레드 asyncio 라 await 전에 close_seed_sent 를 선점하면 펌프/워처가 동시에
    주입해도 한 번만 나간다. 비버 발화중이면 펌프가 turn_end 에서, 소강(idle)이면 워처가
    직접 호출한다. send_client_content 는 idle 세션에 넣으면 즉시 작별 턴을 만든다(비interrupt).
    """
    if state.close_seed_sent:
        return
    state.close_seed_sent = True  # await 전에 선점 → 이중 주입 방지
    state.seed_sent_ts = asyncio.get_running_loop().time()
    await session.send_text_turn(state.close_seed)  # 콜타입별 시드(normal/레벨테스트)
    logger.info("normalcall: 종료 시드 주입")


# --------------------------------------------------------------------------- #
# 통화 시계 워처 + 종료
# --------------------------------------------------------------------------- #
async def _watch_call_clock(state: _CallState, session: LiveSessionProtocol) -> None:
    """종료 신호를 기다렸다가 시드 주입을 보장하고 하드 백스톱을 건다.

    ⛔⛔ **2026-08-19 임시 — 길이 만료는 프론트가 소유한다.** 예전에는 이 워처가
      `call_duration_s` 경과를 직접 재서 종료를 시작했다. 지금은 조각 경계를 프론트가
      잡으므로(5분에 소켓 닫기) 그 루프를 껐다. 근거·복구 방법은 아래 본문 주석에 있다.
      ⇒ 이 워처가 반응하는 신호는 이제 **GoAway · 무음 3단 · 사이드카 종료요청**뿐이다.

    ⭐ RC1(소강 스타베이션) 방지: 종료 마크가 비버 발화중에 떨어지면 펌프가 그 턴 끝(turn_end)에서
    시드를 주입하지만, 소강(idle, turn_id None) 구간이면 turn_end 가 오지 않아 시드가 영영
    안 나간다. 그래서 워처가 idle 을 감지하면 직접 주입한다(작별 없는 무음 종료 방지).
    """
    loop = asyncio.get_running_loop()
    while state.call_start_ts is None:
        await asyncio.sleep(0.2)

    # ⛔⛔⛔ **임시: 종료 타이밍을 프론트가 잡는다**(2026-08-19 사장님 지시) ⛔⛔⛔
    #
    #   조각(6분)은 이제 **서버 시계가 아니라 프론트가** 끝낸다 — 5분에 "이어서
    #   하시겠습니까?"를 띄우고 **소켓을 닫는다**. 서버는 그 닫힘을 `_ClientDisconnect`
    #   로 받아 저장·분석까지 정상으로 돈다(실측 call 1078: "클라 연결 종료" → "저장 완료").
    #
    #   ⭐ 그리고 그게 조각 설계와 **맞다**: 조각 1·2 의 경계에서 비버가 작별을 하면 안 된다
    #     (이어하기 설계 §8 "무음 컷 — 주입 0"). 소켓만 닫으면 주입이 0이라 자동으로 그렇게 된다.
    #
    # ⚠ **되돌리기: `LIVE_CALL_END_OWNER="server"`** 하나면 예전 동작이 그대로 살아난다.
    #   코드를 주석으로 지우지 않은 이유는 그 설정의 주석에 적어 뒀다(회귀 3건이 걸려 있다).
    #
    # ⛔ **안 끈 것 — 이건 길이가 아니라 안전이다:**
    #   ① `ABSOLUTE_CALL_TIMEOUT_S`(540초) 절대 백스톱(run_call 의 asyncio.timeout).
    #      **프론트가 영영 안 닫아도 9분에 끝난다.** 무한 과금 방어.
    #   ② GoAway · 무음 3단 · 사이드카 `_request_close` — 길이와 무관한 종료 사유.
    #   ③ 아래 시드 주입·백스톱 — should_close 가 서면 그대로 돈다.
    #
    # ⚠ **마지막 조각의 작별은 지금 아무도 안 한다.** 프론트가 3번째도 그냥 닫으면 비버가
    #   인사 없이 끊긴다. 이어하기 본구현에서 서버가 "마지막 조각"을 알게 되면 그때
    #   시드를 되살린다(설계 §8 표: 조각3 = 기존 종료 시드 무수정).
    # ⛔⛔ **레벨테스트는 스위치를 안 탄다**(회귀가 잡았다: band 사이드카 폴백 시험이 캡을
    #   기다리다 타임아웃). 3분 하드캡은 상품 혜택이 아니라 **측정 설계**라 클라가 언제
    #   닫든 서버가 캡에서 끝내야 한다. 조각·이어하기는 일반 통화의 개념이다.
    client_owns_end = (
        not state.is_leveltest
        and (_settings.LIVE_CALL_END_OWNER or "").strip().lower() == "client"
    )
    while not state.should_close:
        # T2: 조기종료(GoAway/무음3단/사이드카)가 캡 이전에 should_close 를 세우면 즉시
        # 백스톱 관리로 진입 — 안 그러면 조기 close 후에도 캡까지 매달린다.
        if not client_owns_end and loop.time() - state.call_start_ts >= state.call_duration_s:
            break
        # 폴링 0.2s 유지 + 종료 요청이 오면 즉시 깨어난다(_request_close). TaskGroup 밖
        # 사이드카가 세션을 직접 잡지 않고도 지연 없이 종료 시드를 내보내게 하는 통로(B2).
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(state.close_requested.wait(), 0.2)
    state.should_close = True
    logger.info(
        "normalcall: 종료 플래그(%s)",
        "프론트 소유 — 신호 수신" if client_owns_end
        else "%.0fs 경과/조기신호" % state.call_duration_s,
    )

    # 시드가 주입될 때까지 감시. idle 이면 워처가 즉시 주입, 발화중이면 펌프 turn_end 주입을 기다림.
    # ⭐ 종료 레이스(call 197): 유저가 5분 직전 마지막에 말하면 "유저 발화 끝~비버 응답 시작"
    #   빈틈에도 turn_id 는 None 이라, 여기서 시드를 주입하면 비버의 유저 응답이 작별로 둔갑한다
    #   (close_reply_started 오설정 → 작별 없이 종료). user_turn_open 이면 워처는 양보하고,
    #   비버가 유저에게 먼저 응답(turn_started 로 user_turn_open=False)한 뒤 그 turn_end 에서
    #   펌프(_pump ...932 elif should_close)가 깨끗한 idle 에 시드를 주입 → 비버가 시드에 진짜 작별.
    seed_wait_deadline = loop.time() + SEED_TO_HANGUP_S
    while not state.close_seed_sent and loop.time() < seed_wait_deadline:
        if state.turn_id is None and not state.user_turn_open:  # 비버 idle & 유저 응답 대기 없음
            await _inject_close_seed(session, state)
            break
        await asyncio.sleep(0.2)

    base = state.seed_sent_ts if state.seed_sent_ts is not None else loop.time()
    while loop.time() - base < SEED_TO_HANGUP_S:
        await asyncio.sleep(0.2)
    logger.warning("normalcall: 종료 백스톱 도달 → 강제 종료")
    raise _CallFinished()


async def _watch_idle(session: LiveSessionProtocol, state: _CallState) -> None:
    """무음 3단 넛지(A2). 학습자 무음 → 비버가 얼지 않게 재개시키고, 끝내 무응답이면 우아히 종료.

    🧒 왜 '3단계'로 나눠 부드럽게 대응하나: 학습자가 잠깐 조용하다고 바로 전화를 끊으면
      매정하다(생각 중일 수도, 한국어 문장을 떠올리는 중일 수도 있다). 그래서 사람이 하듯
      단계적으로 배려한다 — ① 오래 조용하면 비버가 가볍게 새 화제로 말을 이어가고("넛지"),
      ② 그래도 계속 조용하면 모국어로 "거기 있어? 잘 들려?" 확인, ③ 그래도 응답이 없으면
      그제서야 작별 시드를 넣어 우아하게 통화를 끝낸다. 넛지 = "얼어붙은 대화를 살짝 찔러
      다시 흐르게 하는 부드러운 자극".

    핵심 제약: 클라 마이크는 상시 스트리밍이라 오디오 프레임 부재로 무음을 못 잰다. 무음은
    last_activity_ts(학습자 in_tr · 비버 turn_end · 넛지 주입 시각) 이후 경과로만 잰다.
    ⭐ 비버 발화 시간을 무음으로 세지 않는다: turn_end 마다 기준이 리셋되므로 각 단계는
    "직전 활동 이후 신선한 무음"을 재고, 비버의 긴 발화 직후 넛지가 즉시 터지지 않는다.

    비버 idle(turn_id None)일 때만 카운트한다: 발화중엔 넛지가 무의미하고 barge-in off 라
    마이크도 안 나간다. 넛지 주입은 종료 시드와 같은 파이프(send_text_turn)로 새 턴을 만든다.

    ⛔ 우선순위 종료 > 무음: should_close(시계/종료/GoAway)가 서면 즉시 워처 종료. 3단은
    비버가 idle 이므로(turn_end 이 안 옴) **직접 종료 시드를 주입**한다 — go_away 처리와 동일.
    안 그러면 should_close 만 서고 아무도 작별 시드를 안 넣어 통화가 조용히 멈춘다(버그).
    """
    loop = asyncio.get_running_loop()
    # 통화 시계가 시작(첫 turn_start)될 때까지 대기 — 선톡 시드 응답 전엔 무음 판정 무의미.
    while state.call_start_ts is None:
        await asyncio.sleep(0.2)
    # 최초 기준: 아직 아무 활동도 없으면 통화 시작을 무음 기준점으로 삼는다(오프닝 turn_end 에
    # 곧 갱신됨 — 그때부터가 진짜 무음 시작).
    if state.last_activity_ts is None:
        state.last_activity_ts = state.call_start_ts

    while True:
        await asyncio.sleep(0.2)
        if state.should_close:  # 종료 우선 — 넛지 중단
            return
        if state.turn_id is not None:  # 비버 발화중 — 무음 아님
            continue
        # 각 단계는 "직전 활동(발화/넛지) 이후" 신선한 무음을 잰다. 성공 시 last_activity_ts 를
        # 갱신해 다음 단계가 그 시점부터 다시 세도록(비버 무응답이어도 넛지 폭주 방지).
        idle = loop.time() - (state.last_activity_ts or state.call_start_ts)

        # 단계는 실제 주입 성공 시에만 전진(시니어 리뷰 Q1 하드닝 — 상태-행동 일치).
        # 임계·1단 시드는 콜타입별(state.idle_*/nudge_seed_1). 일반은 60/10/12 + 새 화제,
        # 레벨테스트는 25/8/10 + '같은 계단 재측정' 넛지(run_call 이 꽂음). 2·3단 시드는 공통.
        if state.silence_stage == 0 and idle >= state.idle_nudge1_s:
            if await _inject_nudge(session, state, state.nudge_seed_1):
                state.silence_stage = 1
                state.last_activity_ts = loop.time()
                logger.info("normalcall: 무음 1단(%.0fs) → 넛지 주입", state.idle_nudge1_s)
        elif state.silence_stage == 1 and idle >= state.idle_nudge2_s:
            if await _inject_nudge(session, state, _NUDGE_SEED_2):
                state.silence_stage = 2
                state.last_activity_ts = loop.time()
                logger.info("normalcall: 무음 2단(+%.0fs) → 확인 넛지 주입", state.idle_nudge2_s)
        elif state.silence_stage == 2 and idle >= state.idle_close_s:
            logger.info("normalcall: 무음 3단(+%.0fs) → 작별 시드 직접 주입·종료", state.idle_close_s)
            state.should_close = True
            await _inject_close_seed(session, state)  # 비버 idle → 직접 주입(go_away 와 동일)
            return


async def _inject_nudge(session: LiveSessionProtocol, state: _CallState, seed: str) -> bool:
    """무음 넛지 시드를 idle 세션에 1회 주입(종료 시드와 같은 파이프). 실제 주입 시 True.

    ⛔ 종료 우선/단일 소유권 존중: should_close 가 이미 서있거나 비버가 발화중(turn_id)이면
    주입하지 않고 False 를 돌려준다 — 종료 시드 주입과의 경합을 피하고, 발화 턴을 자르지
    않는다. 호출부는 이 반환값으로 silence_stage 전진을 게이팅한다(상태-행동 일치 보장).
    넛지는 새 턴을 만들 뿐 close_seed_sent 가드는 건드리지 않는다(종료 시드 전용).
    """
    if state.should_close or state.turn_id is not None:
        return False
    await session.send_text_turn(seed)
    return True


def _reground_gap_s(state: _CallState) -> float:
    """시간 폴백 간격 — clamp(통화길이 / 2.5, 120s, 240s).

    5분 통화는 120s(2회)로 옛 시각 트리거(0.5·0.8 지점 2회)와 실질 동일하고, 15분은
    240s(3회) + 압축 arm 으로 합계 ≈ 6회 = 분당 0.40회. **빈도는 5분과 같다** —
    통화가 길어졌다고 재접지가 촘촘해지지 않게 분당으로 맞춘 값이다.
    """
    return max(REGROUND_GAP_MIN_S, min(REGROUND_GAP_MAX_S,
                                       state.call_duration_s / REGROUND_GAP_DIVISOR))


async def _attach_reground(session: LiveSessionProtocol, state: _CallState, where: str) -> None:
    """대기 중인 재접지를 지금 얹는다. **두 자리(마이크/전사)가 이 한 곳을 공유한다.**

    ⛔ 가드를 여기 모아 둔 이유 — 자리가 늘 때마다 가드를 베껴 쓰면 언젠가 하나가 빠진다.
      이 프로젝트가 이미 겪은 실패다(`build_system_instruction` 인자 누락 3회).
    ⛔ 가드①(핵심 안전): should_close/close_seed_sent 면 절대 안 얹는다 — 종료 근처에
      얹으면 작별 턴이 오염된다(174/178 재발).
    ⛔ 가드②: 대기 중인 arm 하나당 정확히 1회. `reground_pending` 을 await **전에** 내린다.
      ⚠ 옛 게이트(`reground_injected`, 통화당 1회성)를 쓰지 마라 — 중반에 한 번 얹히면
        True 로 굳어 **후반 리마인더가 영영 안 얹혔다**. 횟수 상한은 arm 쪽이 건다.
    """
    if state.should_close or state.close_seed_sent:
        return
    # ⭐ T16 — 퀴즈 큐가 대기 중이면 **큐가 먼저** 간다. 재접지 pending 은 그대로 남겨 다음 발화에 보낸다
    #   (둘을 한 문자열로 합치지 않는다). reground_count/last_reground_ts 는 건드리지 않는다 — 큐가 재접지
    #   간격 150s 를 소모하면 쪽지가 0회가 돼 되감기 방어가 죽는다(fable P1-3).
    if state.expr_quiz_cue_pending is not None:
        await _attach_quiz_cue(session, state, where)
        return
    if not (state.reground_pending and state.reground_reminder):
        return
    state.reground_pending = False    # await 전 선점(단일 소유권)
    state.reground_injected = True    # 하위호환 플래그(1회 이상 얹혔는가)
    state.reground_count += 1
    state.last_reground_ts = asyncio.get_running_loop().time()
    try:
        await session.send_reground(state.reground_reminder, turn_complete=False)
        logger.info(
            "normalcall: 재접지 얹기(자리=%s, 근거=%s, %d회째, tc=False, 통로=%s, 비버턴=%s)",
            where, state.reground_arm_reason, state.reground_count,
            REGROUND_TRANSPORT, state.turn_id or "(열린 턴 없음)",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 재접지 실패는 통화 무영향(R5)
        logger.warning("normalcall: 재접지 얹기 실패(무시): %s", exc)


async def _maybe_attach_reground_on_mic(
    session: LiveSessionProtocol, state: _CallState, data: bytes,
) -> None:
    """업링크 마이크 프레임에 재접지를 얹는다 — **비버가 확실히 침묵인 자리.**

    ## 왜 이 자리인가
    이 함수가 불리는 지점은 `data and state.turn_id is None` 을 이미 통과했다. 그리고
    클라는 비버 발화중(+소리 꼬리)엔 프레임을 **아예 안 보낸다**(`_micGated`).
    ⇒ **프레임이 여기 있다 = 클라·서버가 각각 "비버는 안 말한다"를 보증했다.**
      끊을 것이 없으므로 `interrupted` 가 나도 버릴 턴이 없다.

    ## ⛔ 그런데 "프레임이 왔다"가 "사람이 말한다"는 아니다
    클라는 마이크를 **상시** 보낸다 — 클라에도 VAD 가 있지만 계측 전용이고 전송을 안 거른다
    (`normalcall_controller.dart:2447` 주석이 못박아 뒀다). 침묵 프레임에 얹으면 비버가
    쪽지만 보고 **혼자 말한다.** 그래서 서버가 RMS 로 직접 본다.

    ⚠ **핫패스다**(초당 45~90프레임). RMS 는 `reground_pending` 일 때만 계산한다 —
      대기 중이 아닐 때의 비용은 불린 검사 하나다(R4).
    """
    if state.expr_quiz_cue_pending is None and not (state.reground_pending and state.reground_reminder):
        return
    if state.should_close or state.close_seed_sent:
        return
    if frame_rms(data) < REGROUND_VOICE_RMS:
        return  # 아직 침묵 — 사람이 입을 열 때까지 들고 기다린다
    await _attach_reground(session, state, "마이크")


async def _attach_quiz_cue(session: LiveSessionProtocol, state: _CallState, where: str) -> None:
    """퀴즈 큐를 지금 얹는다(재접지의 자리·RMS 2관문을 빌려 왔다 — `_attach_reground` 가 부른다).

    ⛔ 재접지의 «await 전 내림» 과 **다르다** — 큐는 필수, 재접지는 선택. 전송 예외면 pending 을 **유지**해
      다음 발화에서 재시도한다. idle 채널(send_text_turn) 폴백은 두지 않는다 — 학습자 발화가 안 오면 퀴즈가
      미뤄질 뿐이고, 통화 끝의 열리지 않은 퀴즈는 drilled 로만 남는다(안전 방향, codex P1-2).
    ⛔ reground_count · last_reground_ts 를 건드리지 않는다(fable P1-3).
    """
    cue = state.expr_quiz_cue_pending
    if cue is None:
        return
    now = asyncio.get_running_loop().time()
    waited = (now - state.expr_quiz_cue_armed_ts) if state.expr_quiz_cue_armed_ts is not None else 0.0
    try:
        await session.send_reground(cue, turn_complete=False)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 다음 발화에서 재시도(R5)
        logger.warning("%s 얹기 실패(다음 발화 재시도): %s", EXPR_QUIZ_CUE_LOG_PREFIX, exc)
        return
    state.expr_quiz_cue_pending = None
    state.expr_quiz_awaiting_open = True
    # ⛔ 접두 고정 — 하네스가 이 줄로 큐↔비버 앵커를 시간 대조한다(QUIZ_CUE_LOG_PREFIX).
    logger.info(
        "%s 얹기: seq=%d 항목=%s 얹기=%s 대기=%.0fs 비버턴=%s", EXPR_QUIZ_CUE_LOG_PREFIX,
        state.expr_quiz_seq, state.expr_quiz_set, where, waited, state.turn_id or "(열린 턴 없음)",
    )


def _reground_due(state: _CallState, now: float) -> str:
    """지금 재접지를 arm 해야 하는가 — 근거 문자열(빈 문자열이면 아니다).

    ⛔ 종료가 최우선이다. 마무리 중에 되박으면 작별이 오염된다(174/178 재발).
    ⛔ 이미 대기 중(reground_pending)이면 새로 세우지 않는다 — 한 턴에 두 리마인더가
      겹치면 비버 응답이 장황해지고, 유저 발화 하나에 서버 텍스트 500 토큰이 붙는다.
    """
    if state.should_close or state.close_seed_sent or state.reground_pending:
        return ""
    if state.reground_count >= REGROUND_MAX_PER_CALL:
        return ""
    # 최소 간격: 같은 압축 주기 안에서 두 번 얹지 않는다.
    if state.last_reground_ts is not None and now - state.last_reground_ts < REGROUND_MIN_GAP_S:
        return ""
    trigger = _settings.LIVE_CTX_TRIGGER_TOKENS
    # ① 선제 — 압축 임박. 압축 직전에 얹은 요약은 최신단에 있어 그 압축을 살아남는다.
    #
    # ⛔⛔ 2026-09-06: 여기는 **바닥 위에 쌓인 대화**를 재야 한다. 예전엔 절대값이었다
    #   (`peak >= trigger * 0.85`). 그런데 지시문이 커지면서 바닥이 임계를 통째로 넘겼다 —
    #   실측 통화 1310·1324: 트리거 8,000 × 0.85 = 6,800 인데 **통화 시작 8초의 peak 가
    #   6,937 / 7,194**. 대화가 한 줄도 없는데 조건이 참이라 재접지가 통화 내내 상시 발동했고,
    #   학습자가 답하는 도중에 브리프가 얹혀 비버가 첫인사를 다시 하거나 과제를 버렸다
    #   (QA #1~3, `docs/20260906_1820_재접지-상시발동-원인과-처방.md`).
    #   ⇒ 주석이 원래 말하던 "컨텍스트가 **찼다**"의 뜻대로, 바닥을 빼고 **남은 자리**로 잰다.
    #
    # ⚠ 바닥이 트리거를 이미 먹었으면(room <= 0) ①을 **끈다**. 그 통화는 임박을 예고할
    #   여유 자체가 없다 — 켜두면 예전처럼 상시 참이다. 압축은 실제로 돌 테니 ②사후 감지가
    #   받는다(미탐은 무해, 오탐은 이중발화 — 원래 설계도 미탐 쪽으로 보수적이다).
    floor = state.usage_prompt_floor
    room = trigger - floor
    # ⭐ T15-6 표현학습은 **남은 자리가 너무 좁으면 ①을 끈다**(1398: 첫 arm 이 40초에 «근거=compress», 실제
    #   압축은 3분 22초 뒤 52:45). T14 로 지시문이 커져 바닥이 트리거에 붙었고, room 의 85% 가 대화 몇 턴
    #   분량이라 임박 산식이 통화 시작 직후 참이 된다 — 예고 가치가 없다. 위 «room <= 0 이면 끈다» 와 같은
    #   논리의 연장이다: 압축은 실제로 돌 테니 ②사후 감지·③시간 폴백이 받는다(미탐은 무해).
    #   ⛔ 일반 통화는 이 분기를 안 탄다(expr_items 게이트) — 동작 바이트 동일.
    #   ⚠ 하한 값은 실측 1건(1398)으로 잡았다 — 그 통화의 바닥·peak 가 로그에 없어서(아래 arm 로그에 이번에
    #     추가) 정확한 room 은 모른다. 다음 실통화의 «peak=·바닥=·대화=» 로 다시 정한다.
    if state.expr_items and 0 < room < EXPR_REGROUND_MIN_ROOM_TOKENS:
        room = 0
    if floor and room > 0 and state.usage_prompt_peak - floor >= room * REGROUND_ARM_RATIO:
        return "compress_imminent"   # ⚠ 라벨: 임박 **산식**이다 — 실제 압축 감지는 아래 post-compress 만(T15-6)
    # ② 사후 — 이미 압축됐다(선제 arm 이 유저 침묵으로 못 얹힌 경우의 보정).
    if state.compression_seen > state.reground_count:
        return "post-compress"
    # ③ 시간 폴백 — usage 가 안 오는 환경에서도 옛 동작만큼은 보장한다(R5).
    base = state.last_reground_ts or state.call_start_ts
    if base is not None and now - base >= _reground_gap_s(state):
        return "time"
    return ""


def _build_expression_note(state: _CallState) -> str:
    """표현학습 재접지 쪽지를 **지금 진도로** 조립한다(arm·사이드카가 공유).

    ⛔ arm 때 한 번 만들고 끝내면 **쪽지가 항상 한 arm 늦는다** — 사이드카가 그 arm 의 판정을
      돌려줘도 이미 만든 문자열은 그대로라, 「아직 틀린 표현」 칸이 다음 arm 까지 빈다
      (= 오답퀴즈가 한 주기 늦게 시작한다). 일반 판에는 있던 «돌아오면 업그레이드» 가
      이 경로엔 없었다.
    ⇒ 두 곳이 **같은 함수**를 부르고, 사이드카는 아직 안 얹힌 쪽지를 다시 조립한다.
    """
    role, personality = state.reground_persona
    drilled = _covered_labels(state)
    passed = _expr_labels(state, state.expr_quiz_pass)
    failed = _expr_labels(state, state.expr_quiz_fail)
    # ⭐ 다음에 다룰 것 = 아직 통과도 드릴도 안 된 가장 앞 항목(서버가 안다).
    #   ⛔ 표면형 집합으로 «했나» 를 묻지 마라 — 동음이의(91그룹 실재)면 한쪽만 다뤄도
    #     **둘 다 완료로 보여** 안 다룬 항목이 next 후보에서 사라진다. identity 는 item_id 다.
    done_ids = set(_expr_covered_ids(state)) | state.expr_quiz_pass
    nxt = next((str(i["obj"]) for i in state.expr_items
                if i.get("obj") and int(i.get("item_id") or -1) not in done_ids), None)
    return build_expression_reground_brief(
        role, personality, drilled=drilled, passed=passed, failed=failed, next_label=nxt,
        # ⭐ T14 ③ 쪽지 착지문이 «{모국어}로 묻고 기다려라» 를 말한다 — 라벨을 넘긴다.
        locale_label=(state.expr_ctx or {}).get("locale_label") or "학습자의 모국어",
    )


def _arm_reground(state: _CallState, reason: str) -> None:
    """재접지를 arm 한다 — 문구는 **지금 당장 조립 가능한 것**으로 먼저 채운다.

    사이드카(맥락 슬롯 채우기)를 기다리지 않는 이유: 유저가 말을 시작하는 순간이 얹을
    자리인데, LLM 을 기다리면 그 자리를 놓친다. 그래서 캐릭터 기본 문구로 즉시 arm 하고,
    사이드카가 제때 돌아오면 아직 안 얹힌 문구를 **업그레이드**한다(실패해도 재접지는 나간다 — R5).
    """
    role, personality = state.reground_persona
    # ⭐⭐ **표현학습은 자기 쪽지를 쓴다.** 일반 브리프는 «이미 다룬 것» 한 칸뿐인데,
    #   이 코스는 진도가 세 갈래다 — 드릴한 것 / 맞힌 것 / **틀린 것**. 마지막 칸이
    #   오답퀴즈의 재료라, 그 줄이 없으면 오답 재출제가 아예 안 나간다(기획 ⑥).
    #   ⛔ 얹는 배관은 **그대로**다(arm → 마이크 프레임 + RMS 2관문). 문구만 갈아 끼운다 —
    #     새 배관을 만들면 «비버가 혼자 두 번 말하거나 말하다 마는» 버그 방어를 처음부터
    #     다시 지어야 한다.
    if state.expr_items:
        state.reground_reminder = _build_expression_note(state)
        state.reground_pending = True
        state.reground_arm_reason = reason
        # ⚠ peak·바닥·대화를 같이 남긴다(T15-6) — 1398 감사에서 이 세 값이 없어 room 을 역산해야 했다.
        logger.info(
            "normalcall 재접지 arm(표현학습, 근거=%s, %d/%d회, 드릴 %d · 통과 %d · 오답 %d, 압축감지=%d, peak=%d, 바닥=%d, 대화=%d)",
            reason, state.reground_count + 1, REGROUND_MAX_PER_CALL,
            len(state.covered_nums), len(state.expr_quiz_pass), len(state.expr_quiz_fail),
            state.compression_seen, state.usage_prompt_peak, state.usage_prompt_floor,
            max(0, state.usage_prompt_peak - state.usage_prompt_floor),
        )
        return
    # ⭐ covered 를 **여기서부터** 싣는다(2026-09-09). 예전엔 사이드카가 돌아와야 실렸는데,
    #   얹기 자리가 "마이크"라 학습자가 빨리 답하면 사이드카가 못 따라온다 — 통화 1360 의
    #   1회째는 arm→얹기가 **1.18초**였고 사이드카는 1.4초라 결과가 조용히 버려졌다
    #   (`if not state.reground_pending: return`). 서버 누적이면 기다릴 게 없다.
    state.reground_reminder = build_reground_brief(
        role, personality, mode=state.call_mode, covered=_covered_labels(state),
    )
    state.reground_pending = True
    state.reground_arm_reason = reason
    logger.info(
        "normalcall: 재접지 arm(근거=%s, %d/%d회, 압축감지=%d, peak=%d, 바닥=%d, 대화=%d)",
        reason, state.reground_count + 1, REGROUND_MAX_PER_CALL,
        state.compression_seen, state.usage_prompt_peak,
        state.usage_prompt_floor,
        max(0, state.usage_prompt_peak - state.usage_prompt_floor),
    )


async def _reground_watch(session: LiveSessionProtocol, state: _CallState) -> None:
    """재접지 arm 워처 — 압축 신호(주) + 시간 폴백(보조)으로 통화당 N회 arm 한다.

    🧒 재접지 = 대화가 길어져 AI 가 캐릭터·맥락을 잊기 전에 짧게 되박아 주는 것.
      옛날엔 "통화의 50%·80% 지점"이라는 시계로 넣었는데, 진짜로 기억을 지우는 건 시간이
      아니라 **컨텍스트 압축**이다(오래된 대화부터 버린다). 그래서 압축 시점에 맞춰 넣는다.

    ⛔ 얹는 건 이 태스크가 아니다. 여기서는 arm 만 세우고, 실제 주입은 펌프가 다음 유저
      발화 턴에 turn_complete=False 로 얹는다(기존 경로 승계 — 새 주입 경로를 만들지 않는다).
    ⚠ 이 태스크는 세대마다 새로 뜬다(세션 재연결). 상태는 _CallState 에 살아 있으므로
      횟수·마지막 주입 시각이 그대로 이어진다 — 스왑이 재접지를 되살리지 않는다.
    """
    # 비활성 조건: 모드 off, 또는 되박을 재료가 아예 없음(레벨테스트가 여기 해당).
    # ⚠ T16: 표현학습 진도 판정은 더는 이 루프에 없다(서버 퀴즈 상태기계 — `_expression_quiz_tick`). 그래서
    #   옛 «표현학습 예외» 도 걷어냈다 — 이 스위치는 재접지만 끈다.
    if (REGROUND_MODE == "off"
            or (not state.reground_persona[0] and not state.reground_reminder)):
        return
    loop = asyncio.get_running_loop()
    while state.call_start_ts is None:
        if state.should_close:
            return
        await asyncio.sleep(0.2)

    while True:
        await asyncio.sleep(0.2)
        if state.should_close or state.close_seed_sent:
            return  # 종료 우선 — 남은 arm 은 버린다
        reason = _reground_due(state, loop.time())
        if not reason:
            continue
        if REGROUND_MODE == "off":
            # 재접지는 끈 채로 판정 주기만 유지한다(다음 `_reground_due` 의 기준점).
            state.last_reground_ts = loop.time()
            continue
        if REGROUND_MODE == "legacy_idle":
            await _reground_legacy_inject(session, state)
            continue
        _arm_reground(state, reason)
        # 일반 통화의 «맥락 슬롯» 사이드카 — 자기 게이트(표현학습이면 안 뜬다)로 되돌아간다.
        _spawn_reground_sidecar(state)


async def _reground_legacy_inject(session: LiveSessionProtocol, state: _CallState) -> None:
    """구방식 폴백(legacy_idle): 비버 idle 일 때 별도 완결 턴으로 주입(이중발화 감수).

    on_user_turn 병합이 Gemini 미보장이라, 실기기에서 이중발화가 보이면 코드 한 줄
    (REGROUND_MODE)로 여기로 되돌린다. 회귀 대비로 남긴다.
    """
    if state.turn_id is not None or state.silence_stage > 0:
        return
    role, personality = state.reground_persona
    # ⭐ 표현학습이면 **자기 쪽지**를 쓴다 — 일반 브리프는 «다룬 것» 한 칸뿐이라 오답퀴즈
    #   재료(「아직 틀린 표현」)가 통째로 빠진다.
    if state.expr_items:
        text = _build_expression_note(state)
    else:
        text = state.reground_reminder or build_reground_brief(
            role, personality, mode=state.call_mode, covered=_covered_labels(state)
        )
    try:
        await session.send_reground(text, turn_complete=True)
        state.reground_count += 1
        state.last_reground_ts = asyncio.get_running_loop().time()
        logger.info("normalcall: 캐릭터 재접지 주입(legacy_idle, tc=True)")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 재접지 실패는 통화 무영향(R5)
        logger.warning("normalcall: 재접지 주입 실패(무시): %s", exc)


def _reground_instruction(items: list[str], target_language: str) -> str:
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


def _transcript_tail(state: _CallState, turns: int = 12, *, only_user: bool = False) -> str:
    """사이드카에 넘길 최근 전사 꼬리(오디오 아님 — 텍스트만).

    통화 전체를 넣지 않는 이유: 재접지가 필요한 건 **지금 흐름**이고, 전체를 넣으면
    사이드카 입력이 통화만큼 커져 원가가 통화와 함께 자란다(재접지가 비싸지면 안 된다).
    """
    lines = [
        f"{'학습자' if s['role'] == 'user' else '선생님'}: {s['text']}"
        for s in state.segments[-turns:]
        if (s.get("text") or "").strip()
        and not (only_user and s["role"] != "user")
    ]
    return "\n".join(lines)


# 모드 전환 인용에서 찾는 두 축 — **주제어**(무엇을)와 **요청어**(해 달라)를 **둘 다** 요구한다.
# ⛔ 한쪽만 보면 드릴 복창이 그대로 통과한다: 1325 의 학습자 줄 "친구하고 이야기를 해요."는
#   비버가 시켜서 따라 말한 문장인데 주제어 '이야기'를 갖고 있다. **요청어가 함께** 있어야
#   요청이다. 반대로 요청어만 보면 "I want to run in Korea."(1325 t2)가 통과한다 — 실제로
#   통과했고, 그게 이 관문이 생긴 이유다.
# ⭐ 2026-09-09(통화 1366) — **정당한 요청이 기각됐다.** 학습자 t26 "우리 이제 한국어로
#   대화해 보자." 가 두 축 모두에서 빗나갔다:
#     주제어 → "대화" 가 목록에 없었다(얘기·이야기·수다만 있었다)
#     요청어 → "대화해 보자" 에 "하자" 가 없다(대·화·해·보·자 — 연속 부분문자열이 아니다)
#   로그: `재접지 모드 전환 제안 기각(요청 표현 없음) study→chat`
#   ⇒ "대화" 와 "보자" 를 넣는다. **실측 1건으로만 넓힌다**(09-07 규율).
#   ⛔ 1325 의 오검출은 이걸 넣어도 여전히 막힌다 — "I want to run in Korea." 는 요청어
#     (want)만 있고 **주제어가 없어서** 걸린 것이라, 주제어를 늘려도 통과하지 못한다.
#     드릴 복창("친구하고 이야기를 해요")도 요청어가 없어 그대로 막힌다.
_MODE_TOPIC_WORDS: dict[str, tuple[str, ...]] = {
    "chat": ("얘기", "이야기", "대화", "수다", "talk", "chat", "conversation"),
    "study": ("공부", "배우", "연습", "가르", "학습",
              "study", "learn", "practice", "teach", "lesson"),
}
_MODE_REQUEST_WORDS: tuple[str, ...] = (
    "싶", "할래", "하자", "보자", "그만", "말고", "그냥", "대신", "줘", "주세요", "주라",
    "want", "let's", "lets", "just", "rather", "instead", "stop", "can we", "could we",
)


def _quote_requests_mode(quote: str, proposed: str) -> bool:
    """인용이 **그 모드를 요청하는 말**인가(관문② — 관문①은 인용의 실재 검사).

    🧒 왜 필요한가: 관문①은 "이 문장이 전사에 있나"만 본다. 있으면 통과다 — **문장의 뜻은
      한 글자도 안 본다.** 그래서 판정 사이드카가 아무 학습자 줄이나 근거로 갖다 붙여도
      막을 방법이 없었다. 지어낸 인용(환각)만 막히고, 있는 말을 잘못 읽는 **과독**은 그대로
      통과한다.

    ## 2026-09-07 통화 1325 — 이 관문이 없어서 난 사고
    비버 t1 "learning 할래, 그냥 chat 할래?" → 학습자 답이 STT 에 **"I want to run in
    Korea."** 로 적혔다(learn→run). 사이드카는 이 줄을 근거로 `chat` 을 냈고, 인용이 전사에
    실재하니 **+34초에 study→chat 이 채택**됐다. 비버는 소리를 듣고 제대로 드릴을 돌리고
    있었는데 서버 모드만 뒤집힌 것이다. 그 뒤 2회째 재접지에 chat 문구가 실려 통화 마지막
    96초가 여행 잡담으로 갔다.
    ⛔ 되돌릴 길도 없었다 — 15:59 에 사이드카가 스스로 낸 chat→study 정정은 인용을 최근
      12턴에서 못 찾아 기각됐다(그때는 드릴만 흐르고 있었다). **들어가긴 쉽고 나오긴
      불가능한 문**이라, 들어가는 쪽을 조인다.

    ⭐ 왜 "드릴 진행 중이면 뒤집지 마라"(서버 관측 게이트)를 **안 넣었나**: 이 관문을 통과
      하려면 이미 요청어가 있어야 한다. 드릴 복창은 요청어가 없어 여기서 걸리므로 그 게이트는
      막을 게 없고, 반대로 학습자가 드릴 도중 "그만하고 얘기해요"라고 한 **정당한 전환**만
      막는다(불변 규칙 1 위반). 잡을 건 없고 놓칠 것만 생기는 관문은 넣지 않는다.
    """
    q = (quote or "").lower()
    return (
        any(t in q for t in _MODE_TOPIC_WORDS.get(proposed, ()))
        and any(r in q for r in _MODE_REQUEST_WORDS)
    )


def _apply_mode_proposal(state: _CallState, proposed: str, quote: str, tail: str) -> None:
    """모드는 **서버가 sticky 로 소유**한다 — 사이드카 제안은 인용이 증명될 때만 채택.

    🧒 왜 그냥 안 믿나: 압축이 통화 초반을 삼키면 사이드카는 "지금 보이는 몇 턴"만 보고
      모드를 다르게 부를 수 있다(공부하던 통화를 잡담으로 오인). 그때마다 모드가 흔들리면
      비버가 통화 중간에 성격이 바뀐 것처럼 군다. 그래서 **전사에 실제로 있는 말**로
      전환이 증명될 때만 바꾼다 — AI 는 증인이고, 판단은 코드가 한다(레벨 시스템 관통원칙 ①).

    ## 2026-09-03 — 증인에게 던지는 **질문**을 바꿨다(사장님 지시: "공부를 원하면 공부 모드로만")
    8/31 에 인용을 학습자 줄로 좁혔는데도 구멍이 남아 있었다. 사이드카 지시문이 mode 를
    "지금 흐르는 모양"으로 정의했기 때문이다 — 비버가 잡담으로 흐르면 학습자는 **그 잡담에
    대답만 해도** 그 대답이 학습자 줄에 실재하는 chat 증거가 된다. 자기가 흘린 걸 학습자
    입을 빌려 증거로 삼는, 8/31 되먹임 고리의 잔여분이다.
    ⇒ `_reground_instruction` 에서 mode 를 **"학습자가 말로 요청한 모드"**로 다시 정의하고,
      요청이 없으면 빈 문자열을 내게 했다(빈 값은 아래 첫 관문에서 그대로 기각된다).
    ⭐ 여기서 **하드 차단(study→chat 금지)을 하지 않은 이유**: 서버 모드는 재접지 문구를
      고르는 데 쓰인다. 학습자가 진짜로 "그만하고 얘기하자"고 했는데 서버가 study 로 굳으면
      재접지가 매번 학습자 뜻과 반대로 끌어당긴다. 모드를 정하는 건 학습자다(불변 규칙 1).
    """
    if proposed not in ("study", "chat") or proposed == state.call_mode:
        return
    q = (quote or "").strip()
    if len(q) < 4 or q not in tail:      # 관문① 원문에 없는 인용 = 환각 → 기각
        logger.info("normalcall: 재접지 모드 전환 제안 기각(인용 미검증) %s→%s",
                    state.call_mode, proposed)
        return
    if not _quote_requests_mode(q, proposed):   # 관문② 있는 말을 잘못 읽은 것 = 과독 → 기각
        logger.info("normalcall: 재접지 모드 전환 제안 기각(요청 표현 없음) %s→%s (인용: %.30s)",
                    state.call_mode, proposed, q)
        return
    logger.info("normalcall: 재접지 모드 전환 채택 %s→%s (인용: %.20s)",
                state.call_mode, proposed, q)
    state.call_mode = proposed


def _spawn_reground_sidecar(state: _CallState) -> None:
    """arm 직후 맥락 슬롯을 채우는 사이드카를 띄운다(논블로킹 — 얹기를 막지 않는다).

    ⛔ 격리(R4/R5): 2펌프 경로 밖에서만 돈다. 느리거나 실패하면 arm 때 조립해 둔 기본
    문구가 그대로 얹힌다(재접지가 사라지지 않는다). 비활성(reground_ctx None)이면 무동작.
    """
    ctx = state.reground_ctx
    if ctx is None or state.should_close:
        return
    # ⛔ **표현학습은 이 사이드카를 안 쓴다.** 두 가지 이유가 있다:
    #   ① 덮어쓴다 — 이 사이드카는 돌아오면 `build_reground_brief`(일반 판)로 쪽지를 다시
    #     조립한다. 그러면 방금 arm 이 만든 **표현학습 쪽지(드릴·통과·오답)가 통째로 사라지고**
    #     오답퀴즈 재료가 없어진다.
    #   ② 줄 게 없다 — 이 사이드카가 채우는 슬롯은 mode(공부/대화)와 topic(대화 흐름)인데,
    #     표현학습엔 모드 축이 없고(D1) 화제는 항목이 정한다. 진도는 서버가 이미 갖고 있다.
    #   ⇒ 안 부르는 것이 옳고, 덤으로 통화당 LLM 호출이 최대 8회 준다.
    # ⚠ 게이트가 `expr_items` 가 아니라 **`reground_items` 부재**인 이유: 프리토킹도 이
    #   사이드카가 필요 없다(항목이 0개라 covered 로 고를 것이 없고, 모드 축도 없다).
    #   ⇒ «떠먹일 목록이 없으면 부르지 않는다» 하나로 세 경우를 다 덮는다.
    if state.expr_items or not state.reground_items:
        return
    task = asyncio.create_task(_reground_sidecar(state), name="normalcall-reground-brief")
    state.reground_tasks.add(task)  # 강참조(GC 방지) — run_call finally 가 전량 취소
    task.add_done_callback(state.reground_tasks.discard)


async def _reground_sidecar(state: _CallState) -> None:
    """대기 중인 재접지 문구를 맥락 슬롯(다룬 항목·화제·모드)으로 업그레이드한다.

    ⛔ 문장 조립은 여기서 하지 않는다 — build_reground_brief(persona_prompt)가 한다.
      이 함수가 하는 일은 "슬롯을 받아 검증하고 넘기기"뿐이다. 종료 어휘 denylist 도
      조립 함수 안에 있어, 어느 경로로 슬롯이 들어와도 같은 방어를 통과한다.
    """
    ctx = state.reground_ctx
    tail = _transcript_tail(state)
    if ctx is None or not tail:
        return
    try:
        result = await gemini_analysis.generate_structured(
            ctx["client"], ctx["model"],
            system_instruction=ctx["instruction"],
            prompt=tail,
            schema=RegroundOut,
            temperature=0.0,
            thinking_budget=0,
            usage=state.sidecar_usage,
        )
        if result is None:
            return
        # ⛔⛔ 인용 검증은 **학습자 줄에서만** 한다(2026-08-31 실측). 예전엔 전사 전체를
        #   넘겨서 **비버 자기 말이 전환 근거가 됐다** — 세 통 연속 그랬다:
        #     1253 study→chat (인용: 무슨 음악을 들어요?)             ← 비버 t35
        #     1255 study→chat (인용: What other spicy foo)            ← 비버 t31
        #     1256 study→chat (인용: 혹시 한국 음식 좋아하는 거 있어?) ← 비버 t21
        #   학습자는 "공부할래"라고 한 뒤 모드를 바꾸자고 한 적이 한 번도 없다. 비버가
        #   잡담으로 흐르면 → 그 흐른 말이 증거가 되고 → 서버가 chat 으로 확정하고 →
        #   이후 재접지마다 "지금처럼 편한 대화를 계속 이어가라"가 꽂혀 **되돌아올 길이
        #   막힌다.** 자기가 흘린 걸 자기가 증거로 삼는 되먹임 고리다.
        # ⭐ 모드를 바꾸는 건 **학습자의 의사**다(불변 규칙 1: "학습자가 도중에 모드를
        #   바꾸고 싶다고 명시하면 따라가라"). 그러니 증거도 학습자 발화여야 한다.
        _apply_mode_proposal(
            state, getattr(result, "mode", "") or "",
            getattr(result, "mode_quote", "") or "",
            _transcript_tail(state, only_user=True),
        )
        # ⭐ 사이드카 covered 는 **서버 누적에 합집합으로 얹는다**(2026-09-09) — 버리지
        #   않는 이유: 비버가 라벨과 다른 말로 다룬 경우(`_note_covered_items` 의 문자열
        #   대조가 놓치는 자리)를 사이드카가 잡아줄 수 있다. 빼지는 않는다(append-only).
        items = state.reground_items
        for n in (getattr(result, "covered", None) or []):
            if isinstance(n, int) and 1 <= n <= len(items) and n not in state.covered_nums:
                state.covered_nums.append(n)
        covered = _covered_labels(state)
        # 이미 얹혔거나 종료 구간이면 업그레이드는 무의미하다(다음 arm 때 새로 받는다).
        if not state.reground_pending or state.should_close or state.close_seed_sent:
            return
        role, personality = state.reground_persona
        topic = getattr(result, "topic", "") or ""
        state.reground_reminder = build_reground_brief(
            role, personality,
            mode=state.call_mode,
            covered=covered,
            topic=topic,
        )
        # ⛔ **실린 개수**를 찍는다 — 예전엔 입력 개수를 찍었다. covered 에 denylist 가
        #   걸려 있던 시절(∼2026-08-17) 3개 중 1개만 실려도 로그는 covered=3 이라
        #   아무도 못 봤다(call 1045). 버려진 슬롯은 조용히 넘어가면 안 된다.
        dropped = " topic=버림" if is_closing_slot(topic) else ""
        logger.info(
            "normalcall: 재접지 브리프 업그레이드(mode=%s covered=%d/%d topic=%s%s)",
            state.call_mode, min(len(covered), REGROUND_COVERED_CAP), len(covered),
            topic[:12], dropped,
        )
    except asyncio.CancelledError:
        raise  # 취소(통화 종료)는 정상 경로
    except Exception as exc:  # noqa: BLE001 - 실패 시 기본 문구가 얹힌다(R5)
        logger.warning("normalcall: 재접지 사이드카 실패(무시 — 기본 문구 사용): %s", exc)


async def _finish_call(client_ws, state: _CallState, call_id: int | None) -> None:
    """call_ended 송신 → playback_done ack 대기 → WS close(전부 graceful).

    🧒 왜 곧바로 소켓을 안 닫고 기다리나: 비버의 작별 인사 오디오가 방금 학습자 쪽으로
      마지막까지 흘러갔는데, 서버가 소켓을 즉시 끊으면 아직 스피커에서 재생 중이던 작별
      인사 꼬리가 뚝 잘린다. 그래서 ① "통화 끝났어요(call_ended)"를 알린 뒤, ② 클라가
      "작별 오디오 다 재생했어요"라고 보내는 신호(playback_done ack)를 잠깐 기다리고,
      ③ 그제서야 소켓을 닫는다. ack 가 끝내 안 와도 무한정 기다리진 않고 PLAYBACK_DONE_WAIT_S
      만큼만 기다리다 닫는다(상대가 이미 끊었을 수도 있으니). 'graceful' = 갑자기 끊지 않고
      상대가 마무리할 틈을 주며 예의 바르게 닫는 것. 매 단계 client_state 를 확인해 이미
      닫힌 소켓에 또 쓰다가 에러 나는 것도 막는다.
    """
    from starlette.websockets import WebSocketState

    with contextlib.suppress(Exception):
        if client_ws.client_state == WebSocketState.CONNECTED:
            await _send_json(client_ws, ServerCallEnded(call_id=str(call_id or ""), reason="done"))
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(state.playback_done_event.wait(), timeout=PLAYBACK_DONE_WAIT_S)
    with contextlib.suppress(Exception):
        if client_ws.client_state != WebSocketState.DISCONNECTED:
            await client_ws.close()
