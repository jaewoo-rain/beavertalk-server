"""normalcall 단일 양방향 브리지 — 5분 한국어 통화 본체(async 오케스트레이션).

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
import uuid
from typing import AsyncContextManager, Callable, Optional

from google import genai
from pydantic import BaseModel
from sqlalchemy.orm import sessionmaker

from core import gemini_analysis
from core.config import Settings
from core.gemini_live import (
    DEFAULT_VOICE,
    LiveEvent,
    LiveSessionProtocol,
    open_session,
)
from core.persona_prompt import (
    _LOCALE_LABEL,
    CLOSE_SEED_LEVELTEST,
    build_leveltest_instruction,
    build_reground_reminder,
    build_system_instruction,
    seed_leveltest_opening,
    seed_opening,
)
from domains.learning.service import normalcall_service as svc
from domains.learning.realtime.protocol import (
    HintExample,
    ServerCallEnded,
    ServerHint,
    ServerInputTranscript,
    ServerMessage,
    ServerOutputTranscript,
    ServerPong,
    ServerTeachingPlan,
    ServerTurnEnd,
    ServerTurnStart,
    TeachingItem,
    client_adapter,
    server_adapter,
)

logger = logging.getLogger(__name__)

# 통화 길이: 기본 5분 경과 시 종료 시드(정상 작별 시작), 백스톱(강제 종료). 1분마다 중간 저장.
CALL_DURATION_S = 300.0          # 기본 통화 길이(5분). 레벨 데모는 start.duration_min 으로 3~15분 override
# 레벨테스트(Phase 1): 인-콜 판정·주입 없이 비버 자율 진행. 종료는 3분 하드캡(이 시계) 또는
# 무음 3단/GoAway 가 종료 파이프로 우아하게 몬다(R5 안전망 — 서버는 통화중 질문을 주입하지 않음).
LEVELTEST_MAX_S = 180.0          # 레벨테스트 하드캡(3분) — call_duration_s 의 base
# 연결 자체 한계 ~10분(S2)을 선점: 서버가 GoAway/연결종료로 뚝 끊기 전에 우리가 먼저
# 우아하게 마무리하도록 540s(9분)로 하향. 정상 5분 통화는 이 상한에 닿지 않아 무영향.
ABSOLUTE_CALL_TIMEOUT_S = 540.0  # 이 상한(9분) 넘으면 강제 종료(백스톱, 연결 ~10분 선점)
SEED_TO_HANGUP_S = 22.0        # 종료 시드 후 정상 종료 안 되면 강제 종료까지(작별 절단 방지 여유. 진짜 상한은 ABSOLUTE_CALL_TIMEOUT_S)
PLAYBACK_DONE_WAIT_S = 7.0     # call_ended 후 playback_done ack 대기 상한(작별 꼬리 드레인 여유 —
#                                클라가 작별 오디오 다 재생(최대 6s)한 뒤 ack 보내므로 그보다 길게)
FLUSH_INTERVAL_S = 60.0         # 통화중 누적 세그먼트 점진 저장 주기(1분)
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
# ── 레벨테스트 Phase 2: 조용한 밴드 관측 → 서버 천장검출 조기종료 ──
# 서버가 매 유저 답변을 사이드카로 조용히 밴드 분류(0 survival~3 advanced)만 하고, 관측된
# 최고 밴드(obs_max, 단조증가)가 천장에 닿으면 종료 시드를 주입한다. ★ 질문은 절대 주입하지
# 않는다(관측은 대화를 구동하지 않음 — should_close 만 세우고 기존 종료 파이프에 합류).
# 천장 조건(순수 함수 _band_ceiling_reached): 시간 플로어 & 최소 관측수 충족 후
#   obs_max==3(최상위 도달) 또는 plateau_count>=PLATEAU_N(더 못 올라감) 또는 obs_count>=MAX.
LEVELTEST_BAND_TIME_FLOOR_S = 45.0  # 천장 판정 시간 플로어(경과 최소 — 초반 표본 1~2건으로 조기종료 방지)
LEVELTEST_BAND_MIN_ANSWERS = 4      # 천장 판정 전 최소 관측 답변 수(표본 하한)
LEVELTEST_BAND_PLATEAU_N = 3        # 밴드 상승 없이 정체가 이만큼 누적되면 천장(더 못 올라감)
LEVELTEST_BAND_MAX_ANSWERS = 10     # 관측 답변 수 안전 상한(무한 관측 방지 — 이 수 넘으면 천장)
LEVELTEST_BAND_NONSPEAKER_MAX = 5   # 비화자/완전초보: None 포함 전체 답변이 이만큼인데 obs_max<=survival 이면 빨리 종료(한국어 못 하는 사람이 오래 붙잡히는 역설 방지)
# 단발 재접지: 통화 중간(길이의 이 비율 시점)에 캐릭터를 딱 1회 되박아 누적 드리프트 완화.
REGROUND_AT_FRACTION = 0.5
# 재접지 모드 스위치(이상 시 코드 한 줄로 하드닝 폴백):
#   "on_user_turn" — 신방식: arm 후 유저 발화 시작(첫 in_tr) 시 그 턴에 얹기(turn_complete=False).
#                    비버가 [유저발화+리마인더]에 1회 응답 → 이중발화·종료오염 제거 목표.
#                    ⚠ 오디오 턴+텍스트 병합은 Gemini 미보장 → 실측 검증 대상(T7).
#   "legacy_idle"  — 구방식: duration/2 idle 에 send_reground(turn_complete=True) 별도 응답(이중발화).
#   "off"          — 재접지 전면 비활성 = 하드닝만(가장 안전한 폴백).
REGROUND_MODE = "on_user_turn"
# on_user_turn 얹기 시점: "first"(유저 발화 초입, 권장) / "final"(is_final 직후 — 병합이 초입서
# 깨질 때의 대안). Gemini 전문가: final 은 VAD 턴이 이미 닫혀 더 위험 → 기본 first.
REGROUND_ATTACH_AT = "first"
DEFAULT_CHARACTER_ID = 1        # start 에 character_id 없을 때 폴백(BABA, 기본 무료)
DEFAULT_TARGET_LANGUAGE = "한국어"    # 교육 대상 언어 기본값(프로덕션 — 오버라이드 없으면 이 값)
# 데모 전용 모국어 라벨 확장(전역 _LOCALE_LABEL 은 안 건드림 → prod 는 ko→영어 폴백 유지).
_DEMO_LOCALE_EXTRA = {"ko": "한국어"}

# normal 통화 전용 종료 시드. 레벨테스트는 persona_prompt.CLOSE_SEED_LEVELTEST(대본 소유자).
_CLOSE_SEED = (
    "[시스템] (이 지시문 자체를 절대 소리 내어 읽거나 언급하지 마라 — 내용만 행동으로 반영하라.) "
    "통화 시간이 다 됐다. 학습자의 마지막 말에 새로 답하거나 새 화제·질문을 시작하지 말고, "
    "짧게 한마디로만 받아 준 뒤 자연스럽게 핑계를 대고 '다음에 또 하자'며 따뜻하게 작별해라. "
    "작별 인사(평서문)로 끝내라 — 질문으로 끝내지 마라. 1~2문장."
)

# 무음 넛지 시드(A2). 종료 시드와 같은 파이프(send_text_turn)로 idle 에서만 주입한다.
# 프롬프트 규율 재사용: "[시스템]"으로 시작 = 지시문이므로 소리내 읽지 말 것.
_NUDGE_SEED_1 = (
    "[시스템] 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고 "
    "가볍게 새 화제로 한 문장만 이어가라."
)
_NUDGE_SEED_2 = (
    "[시스템] 학습자가 계속 조용하다. 이 메시지는 소리내 읽지 말고, 모국어로 "
    "'거기 있어? 잘 들려?'를 한 번만 부드럽게 물어라."
)
# 레벨테스트 1단 넛지: 일반과 달리 '새 화제로 이어가라' 대신 **방금 질문을 다시 묻는다** —
# 작별하지 말고 방금 한 질문을 더 쉽게 바꾸거나 선택지를 주며 모국어로 다시 묻게 한다.
_NUDGE_SEED_1_LEVELTEST = (
    "[시스템] 학습자가 잠깐 조용하다. 이 메시지는 소리내 읽지 말고, 작별하지 말고 "
    "방금 한 질문을 더 쉽게 바꾸거나 선택지를 주며(예/아니오 또는 둘 중 고르기) "
    "모국어로 딱 한 번만 다시 물어라."
)

SessionFactory = Callable[..., AsyncContextManager[LiveSessionProtocol]]

# 통화후 분석 task 강참조 보관소(GC 방지).
_analysis_tasks: set[asyncio.Task] = set()


def _new_turn_id() -> str:
    return uuid.uuid4().hex[:12]


async def _send_json(ws, message: ServerMessage) -> None:
    await ws.send_text(server_adapter.dump_json(message).decode("utf-8"))


def _resolve_target_language(settings: Settings, override: Optional[str]) -> tuple[str, bool]:
    """교육 대상 언어 결정 → (target_language, is_demo).

    prod 이거나 오버라이드가 없으면 항상 한국어(비데모). non-prod 에서 오버라이드가 오면
    그 언어로 데모 진행. prod 에서 오버라이드가 오면 무시하고 warning(오남용/버그 탐지).
    """
    if settings.ENV == "prod" or not override:
        if settings.ENV == "prod" and override:
            logger.warning("normalcall: prod 에서 target_language 오버라이드 무시(%s)", override)
        return DEFAULT_TARGET_LANGUAGE, False
    return override.strip(), True


# 데모/dev 통화 길이 override 범위(분). 사장님 요청: 레벨 데모에서 3~15분 선택.
DEMO_DURATION_MIN_MINUTES = 3
DEMO_DURATION_MAX_MINUTES = 15


def _resolve_call_duration(
    settings: Settings, duration_min: Optional[int], base: Optional[float] = None
) -> float:
    """통화 길이(초) 결정. 데모/dev 에서만 클라가 3~15분 override 가능. prod 는 무시(기본값).

    duration_min 없음 → base(콜타입 기본값). prod 에서 override 오면 무시+warning
    (실서비스는 통화 길이를 클라가 못 정한다 — 오남용/버그 방지). non-prod 는 3~15분 클램프.

    base: 콜타입별 기본 통화 길이. None(미지정)이면 일반 통화 기본값 CALL_DURATION_S 를
    **런타임에** 읽는다(테스트 monkeypatch 반영 — 리터럴 기본값으로 박으면 def-time 에
    고정돼 monkeypatch 가 안 먹는다). 레벨테스트는 base=LEVELTEST_MAX_S 로 3분 캡을 준다.
    하위호환: base 미지정 → 일반 경로 반환 바이트 동일.
    """
    if base is None:
        base = CALL_DURATION_S
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


class _CallState:
    """두 펌프가 공유하는 통화 상태(세그먼트 누적 + 시계 + 종료 플래그)."""

    __slots__ = (
        "turn_id", "call_start_ts", "should_close", "close_seed_sent", "close_reply_started",
        "seed_sent_ts",
        "playback_done_event", "segments", "persisted_count",
        "cur_user_pcm", "cur_user_text", "cur_beaver_pcm", "cur_beaver_text", "next_turn_index",
        "close_seed",
        "last_turn_id", "hint_ctx", "hint_task", "hint_tasks",
        "hinted_turn_ids", "hinted_next_turn_index",
        "last_activity_ts", "silence_stage", "call_duration_s",
        "idle_nudge1_s", "idle_nudge2_s", "idle_close_s", "nudge_seed_1",
        "reground_reminder", "reground_pending", "reground_injected", "user_turn_open",
        "band_observe", "band_client", "band_awaiting", "obs_count", "obs_max", "total_answers",
        "plateau_count", "last_beaver_question", "band_tasks",
    )

    def __init__(self) -> None:
        # 종료 시드 텍스트(콜타입별 — normal 기본, 레벨테스트는 run_call 이 교체).
        # 주입 시점·파이프(_inject_close_seed)는 불변, 문자열만 바뀐다(R4).
        self.close_seed: str = _CLOSE_SEED
        self.turn_id: Optional[str] = None
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
        self.cur_user_pcm = bytearray()
        self.cur_user_text: list[str] = []
        self.cur_beaver_pcm = bytearray()
        self.cur_beaver_text: list[str] = []
        self.next_turn_index = 0
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
        # 통화 길이(초). 기본 CALL_DURATION_S. 데모/dev 는 start.duration_min 으로 3~15분
        # override(run_call 에서 세팅). _watch_call_clock 이 모듈 상수 대신 이 값을 본다.
        self.call_duration_s: float = CALL_DURATION_S
        # 무음 3단 캐던스 + 1단 넛지 시드(콜타입별 — run_call 이 꽂는다). 기본은 일반 통화 값.
        # _watch_idle 은 모듈 상수 대신 이 필드를 본다(레벨테스트만 짧은 캐던스로 override).
        self.idle_nudge1_s: float = IDLE_NUDGE1_S
        self.idle_nudge2_s: float = IDLE_NUDGE2_S
        self.idle_close_s: float = IDLE_CLOSE_S
        self.nudge_seed_1: str = _NUDGE_SEED_1
        # 단발 재접지 리마인더(일반 통화만, run_call 에서 조립). None = 비활성.
        self.reground_reminder: Optional[str] = None
        # 재접지 상태기계(on_user_turn):
        #   reground_pending: arm 됨(fire_at 도달) — 다음 유저 발화 시작 시 얹는다.
        #   reground_injected: 이미 얹음(단일 소유권 가드, 통화당 1회).
        #   user_turn_open: 지금 유저 발화 턴이 열려 있나(첫 in_tr True → 비버 응답 시작 시 False).
        self.reground_pending: bool = False
        self.reground_injected: bool = False
        self.user_turn_open: bool = False
        # ── 레벨테스트 Phase 2: 조용한 밴드 관측(무주입) ──
        # band_observe: 관측 활성(레벨테스트만 run_call 이 True). False → 전 경로 무동작(일반 통화 무영향).
        # band_client: classify_leveltest_band 에 넘길 genai.Client(사이드카가 참조).
        # band_awaiting: 관측 사이드카 in-flight 가드(동시 1건만 — 다음 답변은 완료 후 관측).
        # obs_count: 관측 성공(band 값) 누계. obs_max: 관측된 최고 밴드(단조증가, -1=아직 없음).
        # plateau_count: 최고 밴드 갱신 없이 정체된 관측 연속 수(천장 판정 재료).
        # last_beaver_question: 직전 flush 된 비버 발화 스냅샷(관측 사이드카의 prior_question 문맥).
        # band_tasks: 관측 사이드카 강참조(GC 방지) — run_call finally 가 전량 취소.
        self.band_observe: bool = False
        self.band_client = None
        self.band_awaiting: bool = False
        self.obs_count: int = 0
        self.total_answers: int = 0  # None 포함 전체 답변 시도(비화자 조기종료 판정)
        self.obs_max: int = -1
        self.plateau_count: int = 0
        self.last_beaver_question: str = ""
        self.band_tasks: set[asyncio.Task] = set()


class _ClientDisconnect(Exception):
    """클라 WS 종료 내부 신호."""


class _CallFinished(Exception):
    """통화 정상 종료(작별 후/백스톱) 내부 신호."""


def _flush_user_segment(state: _CallState) -> None:
    if not state.cur_user_pcm and not state.cur_user_text:
        return
    text = "".join(state.cur_user_text).strip()
    logger.info("👤 USER[t%d]: %s", state.next_turn_index, text or "(무음/전사없음)")
    state.segments.append(
        {"turn_index": state.next_turn_index, "role": "user", "text": text, "pcm": bytes(state.cur_user_pcm)}
    )
    state.next_turn_index += 1
    state.cur_user_pcm = bytearray()
    state.cur_user_text = []


def _flush_beaver_segment(state: _CallState) -> None:
    if not state.cur_beaver_pcm and not state.cur_beaver_text:
        return
    text = "".join(state.cur_beaver_text).strip()
    logger.info("🦫 BEAVER[t%d]: %s", state.next_turn_index, text or "(전사없음)")
    # 레벨테스트 밴드 관측: 방금 끝난 비버 발화(직전 질문)를 스냅샷 — 다음 유저 답변 관측의
    # prior_question 문맥. band_observe=False(일반 통화)면 무동작.
    if state.band_observe and text:
        state.last_beaver_question = text
    state.segments.append(
        {"turn_index": state.next_turn_index, "role": "beaver", "text": text, "pcm": bytes(state.cur_beaver_pcm)}
    )
    state.next_turn_index += 1
    state.cur_beaver_pcm = bytearray()
    state.cur_beaver_text = []


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
    live_session_factory: SessionFactory | None = None,
) -> None:
    """노멀콜 단일 통화를 양방향 중계한다(인증은 ws_router 가 끝낸 뒤 호출).

    Args:
        client_ws: 이미 accept 된 FastAPI WebSocket.
        settings: 서버 설정.
        client: lifespan 의 genai.Client(app.state.genai_client).
        db_session_factory: app.state.session_factory(SQLAlchemy sessionmaker).
        member_id: 인증된 회원 id.
        live_session_factory: Live 세션 CM 팩토리(모킹 확장점). None 이면 호출 시점에
            모듈의 open_session 을 사용한다(기본 인자로 박지 않아 monkeypatch 가능).
    """
    # 기본값을 함수 정의 시점에 바인딩하지 않고 호출 시점에 해석 → 테스트에서
    # `open_session` 을 monkeypatch 하면 그대로 반영된다(운영은 실제 open_session).
    factory = live_session_factory or open_session
    # 1) 첫 start → character_id / locale / target_language / call_type / duration override.
    try:
        character_id, locale_override, target_override, call_type_override, duration_override = (
            await _read_initial_start(client_ws)
        )
    except _ClientDisconnect:
        logger.info("normalcall: start 수신 전 클라 종료")
        return

    # 2) 프롬프트 입력 조회(레벨 프로파일·페르소나·voice·locale) — 1회, 짧은 세션.
    #    needs_level_test(= korean_level 미확정)도 여기서 얻는다(추가 DB 비용 0, D11).
    setup = await svc.run_db(db_session_factory, lambda db: svc.load_call_setup(db, member_id, character_id))
    locale = locale_override or setup["locale"]

    # 교육 대상 언어(데모 전용 오버라이드; prod 는 한국어 고정). 데모면 레벨 프로파일을 비우고
    # 모국어 라벨을 데모용(ko→"한국어")으로. 전역 _LOCALE_LABEL 은 안 건드려 prod 무손상.
    target_language, is_demo_target = _resolve_target_language(settings, target_override)
    locale_label_override = _DEMO_LOCALE_EXTRA.get(locale) if is_demo_target else None

    # 콜타입 라우팅(D11): ① 클라 명시 — 단 아래 2건은 normal 로 강등 ② 서버 자동.
    #   강등 a) 데모(target_language 오버라이드): 비한국어 전사를 한국어 루브릭으로
    #          판정하면 korean_level 이 무의미한 값으로 오염 → 명시여도 level_test 금지.
    #   강등 b) prod && korean_level 보유자의 명시 재측정: 재측정은 미지원(후속 기능) —
    #          non-prod 는 개발 테스트 편의로 현행 허용.
    # 자동: 데모는 진입 금지(normal 고정), 그 외 korean_level 미확정 → level_test.
    if call_type_override is not None:
        call_type = call_type_override
        if call_type == "level_test" and is_demo_target:
            logger.warning(
                "normalcall: 데모 통화(target_language=%s)에서 call_type=level_test 명시 "
                "→ normal 강등(한국어 루브릭 판정 오염 방지) member=%s", target_language, member_id,
            )
            call_type = "normal"
        elif (
            call_type == "level_test"
            and settings.ENV == "prod"
            and not setup["needs_level_test"]
        ):
            logger.warning(
                "normalcall: prod 에서 korean_level 보유자의 level_test 재측정 명시 "
                "→ normal 강등(재측정은 미지원 — 후속 기능) member=%s", member_id,
            )
            call_type = "normal"
    elif is_demo_target:
        call_type = "normal"
    else:
        call_type = "level_test" if setup["needs_level_test"] else "normal"

    teaching_items: list[TeachingItem] = []  # P2.5 teaching_plan(normal + 재료 있을 때만)
    reground_reminder: str | None = None  # 일반 통화만 세팅(레벨테스트는 재접지 안 함)
    if call_type == "level_test":
        # 레벨테스트 대본 — 레벨/이력 슬롯 없는 전용 셋업(회원당 사실상 1회라 재조회 비용 수용).
        lt_setup = await svc.run_db(
            db_session_factory, lambda db: svc.load_level_test_setup(db, member_id, character_id)
        )
        system_instruction = build_leveltest_instruction(
            role=lt_setup["role"],
            personality=lt_setup["personality"],
            rules=lt_setup["rules"],
            locale=locale,
            interests=lt_setup["interests"],
            name=lt_setup["name"],
            target_language=target_language,
            locale_label=locale_label_override,
        )
        # Phase 1(주입 기계 제거): 서버가 질문을 주입하지 않는다. 비버가 첫 질문을 자유롭게
        # 시작하도록 오프닝 시드만 던진다(사다리 부트스트랩 없음 — 이중발화·마커낭독 소멸).
        seed_text = seed_leveltest_opening(target_language)
        voice = lt_setup["voice"]
    else:
        level_profile = "" if is_demo_target else setup["level_profile"]
        # P2-c2 체크판 재료(공부 10/대화 가이드/최근 소재/승급 멘트) — setup 이 선별해
        # 온 값을 그대로 꽂는다(전부 None/False 면 종전 프롬프트와 바이트 동일).
        # 데모(비한국어)는 한국어 커리큘럼이 무의미하므로 미주입.
        inject_materials = not is_demo_target
        system_instruction = build_system_instruction(
            role=setup["role"],
            personality=setup["personality"],
            rules=setup["rules"],
            level_profile=level_profile,
            locale=locale,
            interests=setup["interests"],
            name=setup["name"],
            history=setup["history"],
            target_language=target_language,
            locale_label=locale_label_override,
            study_items=setup.get("study_items") if inject_materials else None,
            known_items=setup.get("known_items") if inject_materials else None,
            recent_topics=setup.get("recent_topics") if inject_materials else None,
            promotion_notice=bool(setup.get("promotion_notice")) and inject_materials,
            lang_band=setup.get("lang_band", "beginner"),
        )
        seed_text = seed_opening(target_language)
        voice = setup["voice"]
        # 단발 재접지 리마인더(일반 통화 + REGROUND_MODE != "off"): DB 캐릭터 3필드를 중간에 1회 되박음.
        if REGROUND_MODE != "off":
            reground_reminder = build_reground_reminder(setup["role"], setup["personality"], setup["rules"])
        # P2.5: 학습 카드용 teaching_plan — 프롬프트 주입(study_items)과 단일 소스.
        if inject_materials and setup.get("study_items"):
            teaching_items = _teaching_plan_items(setup["study_items"])

    # 3) 통화 행 생성(call_type 기록).
    call_id = await svc.run_db(
        db_session_factory, lambda db: svc.create_call(db, member_id, character_id, call_type)
    )

    state = _CallState()
    # 통화 길이: 데모/dev 는 클라가 3~15분 지정 가능(prod 무시). _watch_call_clock 이 참조.
    state.reground_reminder = reground_reminder  # 일반 통화만 값 있음(중간 1회 재접지)
    # Phase 1: 레벨테스트도 in-band tool 을 쓰지 않는다(인-콜 판정 없음 — 종료는 3분캡/무음).
    # 따라서 tools=None(일반 통화와 동일 — 세션 팩토리 시그니처 무손상).
    live_tools = None
    if call_type == "level_test":
        # T1: 3분 하드캡(base=LEVELTEST_MAX_S). 데모가 duration_min 을 주면 3~15분 클램프가
        # 우선(데모의 명시 선택) — prod/일반 경로는 이 값에 못 닿아 무영향. 워처·리그라운드·
        # 넛지는 이 한 값(state.call_duration_s)으로 흡수한다(무수정).
        state.call_duration_s = _resolve_call_duration(
            settings, duration_override, base=LEVELTEST_MAX_S
        )
        state.close_seed = CLOSE_SEED_LEVELTEST  # 종료 시드 문자열만 교체(주입 파이프 불변)
        # T3: 무음 캐던스 단축 + 1단 넛지 내용 전환(질문 재출제 유지).
        state.idle_nudge1_s = LEVELTEST_IDLE_NUDGE1_S
        state.idle_nudge2_s = LEVELTEST_IDLE_NUDGE2_S
        state.idle_close_s = LEVELTEST_IDLE_CLOSE_S
        state.nudge_seed_1 = _NUDGE_SEED_1_LEVELTEST
        # Phase 2: 조용한 밴드 관측 활성 — 매 유저 답변을 사이드카로 밴드 분류만 하고(질문 주입 0)
        # 천장(obs_max) 도달 시 종료 시드만 주입한다. band_client = 분류 사이드카가 쓸 genai.Client.
        state.band_observe = True
        state.band_client = client
    else:
        state.call_duration_s = _resolve_call_duration(settings, duration_override)
        state.idle_nudge1_s = IDLE_NUDGE1_S
        state.idle_nudge2_s = IDLE_NUDGE2_S
        state.idle_close_s = IDLE_CLOSE_S
        state.nudge_seed_1 = _NUDGE_SEED_1

    # P2.5(D16) 동적 힌트 사이드카 활성 조건: 모든 통화(레벨테스트·일반, 레벨 무관)에 힌트 제공.
    # 데모(비한국어 target)만 제외 — 한국어 힌트가 무의미하므로(R5). 상세는 mechanics ⑬.
    # (구: 레벨테스트 또는 normal 레벨1만 → 사장님 결정으로 전 통화 확대. 비용: 비버 턴마다
    #  힌트 생성 LLM 호출이 늘지만 논블로킹 사이드카라 지연 은닉.)
    enable_hints = not is_demo_target
    if enable_hints:
        label = locale_label_override or _LOCALE_LABEL.get(locale) or _LOCALE_LABEL["en"]
        # 레벨테스트는 레벨을 모르는 상태 — 프로파일 대신 최저 난이도 요약으로 폴백.
        profile = setup["level_profile"] if call_type == "normal" else ""
        state.hint_ctx = {
            "client": client,
            "model": settings.JUDGE_MODEL,
            "instruction": _hint_instruction(profile, label),
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
    # ⚠ 10분 초과 선택은 Gemini 연결 한계(~10분)로 GoAway/연결종료가 먼저 올 수 있다(데모 한정 감수).
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
        logger.warning("normalcall 통화 상한(%.0fs) 초과 — 강제 종료", ABSOLUTE_CALL_TIMEOUT_S)
    except _ClientDisconnect:
        logger.info("normalcall 클라 연결 종료")
    except _CallFinished:
        logger.info("normalcall 통화 정상 종료")
        if state.band_observe:
            logger.info(
                "normalcall: 레벨테스트 밴드 관측 종료 obs_max=%d obs_count=%d plateau=%d "
                "(통화후 판정관이 전사로 최종 확정 — bracket 힌트 전달은 TODO)",
                state.obs_max, state.obs_count, state.plateau_count,
            )
    except Exception as exc:  # noqa: BLE001 - 최종 방어선
        logger.exception("normalcall 브리지 오류: %s", exc)
    finally:
        # D16: 미완 힌트 태스크 전량 취소 — 통화가 끝났는데 늦은 힌트가 나가는 것 방지.
        for t in list(state.hint_tasks):
            t.cancel()
        # Phase 2: 미완 밴드 관측 사이드카 전량 취소(통화 종료 후 뒤늦은 관측·종료 시도 방지).
        for t in list(state.band_tasks):
            t.cancel()
        _flush_user_segment(state)
        _flush_beaver_segment(state)
        # P2.6: 전사(텍스트) 선저장 — 오디오 MP3 변환·업로드(~9s)는 pending 으로 분리.
        pending_audio = await _persist_remaining(db_session_factory, state, call_id, member_id)
        # 분석 태스크를 먼저 생성(분석 우선 착수) → 오디오 업로드는 병렬 후행.
        _trigger_analysis(
            call_id, client, settings, db_session_factory, locale,
            target_language=target_language, locale_label=locale_label_override,
            call_type=call_type, member_id=member_id,
            candidates=setup.get("candidates") if call_type == "normal" else None,
            # D16: 힌트 열람 마커(in-memory) — 크래시 유실 시 과크레딧 1회 허용.
            hinted_from_turn_index=set(state.hinted_next_turn_index) or None,
        )
        _trigger_audio_upload(db_session_factory, call_id, member_id, pending_audio)
        await _finish_call(client_ws, state, call_id)


def _trigger_analysis(
    call_id, client, settings, db_session_factory, locale,
    *, target_language: str = DEFAULT_TARGET_LANGUAGE, locale_label: str | None = None,
    call_type: str = "normal", member_id: int | None = None,
    candidates: list[dict] | None = None,
    hinted_from_turn_index: set[int] | None = None,
) -> None:
    """통화후 분석을 백그라운드 task 로 띄운다(non-blocking, GC 방지 보관).

    call_type 디스패치: level_test → 레벨 판정(analyze_level_test_call, member_id 필수),
    normal → 기존 표현 추출 + 항목 검출(analyze_call). candidates 는 통화 시작 때
    선별한 검출 후보(주입 injected=True 포함, P2-c2) — None 이면 analyze_call 이
    기본 후보(practicing 18+introduced 12)로 폴백한다.
    hinted_from_turn_index(D16)는 항목 검출이 있는 analyze_call 에만 의미가 있다
    (레벨테스트 판정은 증거 적립이 없어 미전달).
    """
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
        )
    task = asyncio.create_task(coro, name=f"normalcall-analysis-{call_id}")
    _analysis_tasks.add(task)
    task.add_done_callback(_on_analysis_done)


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
    (analyze_call 이 기본 후보로 대체). target_language 는 서버 기본(비데모)으로 고정 —
    재분석은 이 배포의 대상 언어 루브릭으로 돈다. 증거 중복은 멱등 가드가 막는다.

    ⚠️ 이벤트루프 위에서 호출해야 한다(asyncio.create_task) — async 엔드포인트에서만.
    """
    _trigger_analysis(
        call_id, client, settings, db_session_factory, locale,
        target_language=DEFAULT_TARGET_LANGUAGE, locale_label=None,
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
    factory_kwargs = {"system_instruction": system_instruction, "voice": voice}
    if tools is not None:
        factory_kwargs["tools"] = tools
    async with live_session_factory(client, settings, **factory_kwargs) as session:
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(_pump_client_to_gemini(client_ws, session, state), name="nc-client->gemini")
                tg.create_task(_pump_gemini_to_client(client_ws, session, state), name="nc-gemini->client")
                tg.create_task(_watch_call_clock(state, session), name="nc-clock")
                tg.create_task(_watch_idle(session, state), name="nc-idle")
                tg.create_task(_reground_once(session, state), name="nc-reground")
                tg.create_task(
                    _periodic_flush(db_session_factory, state, call_id, member_id), name="nc-flush"
                )
                await session.send_text_turn(seed_text)  # 선톡 트리거
        except* _CallFinished:
            raise _CallFinished()
        except* _ClientDisconnect:
            raise _ClientDisconnect()


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
            logger.info("normalcall: 점진 flush %d개(누적 %d) call_id=%s", len(new), target, call_id)
        except Exception as exc:  # noqa: BLE001 - flush 실패는 다음 주기/종료시 재시도
            logger.warning("normalcall: 점진 flush 실패(무시): %s", exc)


async def _read_initial_start(
    client_ws,
) -> tuple[int, str | None, str | None, str | None, int | None]:
    """첫 start 에서 character_id / locale / target_language / call_type / duration_min 확보.

    call_type None = 서버 판단(D11 자동 라우팅), "normal"/"level_test" = 클라 명시(우선).
    duration_min None = 서버 기본 통화 길이, 값 있으면 데모/dev 에서 3~15분 override.
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
                    return (
                        int(getattr(cm, "character_id", DEFAULT_CHARACTER_ID)),
                        getattr(cm, "locale", None),
                        getattr(cm, "target_language", None),
                        getattr(cm, "call_type", None),
                        getattr(cm, "duration_min", None),
                    )
    except WebSocketDisconnect as exc:
        raise _ClientDisconnect() from exc
    return DEFAULT_CHARACTER_ID, None, None, None, None


# --------------------------------------------------------------------------- #
# P2.5: teaching_plan + 동적 힌트 사이드카 (D16 — mechanics ⑪·⑬)
# --------------------------------------------------------------------------- #
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


# 레벨테스트 통화용 힌트 난이도 폴백 — 레벨을 모르는 상태라 최저 난이도로 안전하게.
_HINT_PROFILE_FALLBACK = "아주 쉬운 기초 한국어(짧은 정형 표현과 5~10음절 단문)"
_HINT_PROFILE_MAX_CHARS = 400  # 레벨 프로파일 요약 상한 — 사이드카 입력 비대 방지


def _hint_instruction(level_profile: str, locale_label: str) -> str:
    """동적 힌트 사이드카 시스템 지시문(순수 문자열 조립 — LLM 생성 0)."""
    profile = (level_profile or "").strip()[:_HINT_PROFILE_MAX_CHARS] or _HINT_PROFILE_FALLBACK
    return (
        "너는 한국어 학습 힌트 생성기다. 선생님의 질문에 학습자가 할 만한 "
        "자연스러운 예시 답변을 examples 배열에 정확히 3개 만들어라(서로 조금씩 다른 답 — "
        "예: 짧은 답/조금 더 긴 답/다른 소재). 각 예시는 korean·roman·native 를 갖는다. "
        f"korean 은 다음 수준({profile}) 범위의 쉬운 한국어 1문장, "
        "roman 은 국어의 로마자 표기법(RR)에 따른 korean 의 로마자 표기, "
        f"native 는 {locale_label}로 옮긴 뜻."
    )


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
    """관측된 최고 밴드(obs_max)가 '천장'에 닿았는지 판정(순수 함수 — 부작용 0, 테스트 용이).

    시간 플로어(초반 표본으로 조기종료 방지) & 최소 관측수 충족 후 셋 중 하나면 천장:
      - obs_max >= 3        : advanced(최상위) 도달 — 더 잴 상단이 없다.
      - plateau_count >= N  : 최고 밴드가 갱신 안 된 정체 누적 — 더 못 올라간다(상한 확정).
      - obs_count >= MAX    : 관측 답변 안전 상한 — 무한 관측 방지.
    """
    if elapsed < LEVELTEST_BAND_TIME_FLOOR_S:
        return False
    # 비화자/완전초보: 여러 번 시도했는데 한국어 산출이 survival(0) 이하뿐 → 빨리 종료.
    # (한국어를 아예 못 하는 사람은 band=None 만 나와 obs_count 가 안 늘어 영영 안 끊기던 역설 차단.)
    if state.total_answers >= LEVELTEST_BAND_NONSPEAKER_MAX and state.obs_max <= 0:
        return True
    if state.obs_count < LEVELTEST_BAND_MIN_ANSWERS:
        return False
    return (
        state.obs_max >= 3
        or state.plateau_count >= LEVELTEST_BAND_PLATEAU_N
        or state.obs_count >= LEVELTEST_BAND_MAX_ANSWERS
    )


def _spawn_band_observe(session: LiveSessionProtocol, state: _CallState) -> None:
    """유저 답변 1건을 조용히 밴드 관측하는 사이드카를 띄운다(무주입 — should_close 만).

    ⛔ 격리(R4/R5): 2펌프 경로의 추가 비용은 create_task 1회뿐. 분류 LLM 콜은 백그라운드
    사이드카에서 일어나며, 느리거나 실패해도 통화 무영향(관측 1건 누락일 뿐 — 3분캡/무음이
    백스톱). ★ 질문 주입 코드 없음: 사이드카는 천장 도달 시 종료 시드만 주입한다.
    band_awaiting 1회 가드로 동시 1건만(진행중이면 이 답변은 관측 스킵 — 다음 답변에 재개).
    호출 시점은 _flush_user_segment **이전**이어야 한다(cur_user_text 가 비워지기 전 캡처).
    """
    if not state.band_observe or state.band_awaiting or state.should_close:
        return  # 종료 진행중이면 관측 불필요(m4: LLM 콜 낭비 방지)
    answer = "".join(state.cur_user_text).strip()
    if not answer:
        return  # 무발화 턴(오프닝 등) — 관측 대상 아님
    state.band_awaiting = True  # create_task 전 선점(동시 1건 가드)
    task = asyncio.create_task(
        _band_observe_sidecar(session, state, answer, state.last_beaver_question),
        name="normalcall-band-observe",
    )
    state.band_tasks.add(task)  # 강참조(GC 방지) — run_call finally 가 전량 취소
    task.add_done_callback(state.band_tasks.discard)


async def _band_observe_sidecar(
    session: LiveSessionProtocol, state: _CallState, answer: str, prior_question: str
) -> None:
    """답변 1건 밴드 분류 → obs_max/plateau 추적 → 천장이면 종료 시드 주입(백그라운드, R5).

    band None(무응답/판정불가) = 무변경(넛지·캡이 처리). band 값이면 obs_count++,
    obs_max 갱신 시 plateau=0, 아니면 plateau++. 천장(_band_ceiling_reached) 도달 시
    should_close 를 세우고, 비버 idle & 유저 응답 대기 없음이면 종료 시드를 직접 주입한다
    (발화중/유저턴 열림이면 주입 안 함 — 펌프가 다음 깨끗한 turn_end 에서 주입 + 시계워처
    백스톱). ★ 질문 주입 없음 — should_close/종료 시드만. 예외·CancelledError 처리는 힌트
    사이드카와 동일(취소는 재전파, 그 외는 흡수 → band None 취급).
    """
    try:
        band = await svc.classify_leveltest_band(
            state.band_client, answer_text=answer, prior_question=prior_question,
        )
    except asyncio.CancelledError:
        raise  # 취소(통화 종료)는 정상 경로 — 재전파
    except Exception as exc:  # noqa: BLE001 - 관측 실패는 관측 1건 누락일 뿐 통화 무영향
        logger.warning("normalcall: 밴드 관측 실패(무시 — 관측 1건 누락): %s", exc)
        band = None
    finally:
        state.band_awaiting = False  # in-flight 해제 → 다음 답변 관측 허용

    # total_answers 는 None(비화자·판정불가) 포함 전체 시도 — 비화자 조기종료 판정용.
    state.total_answers += 1
    if band is not None:
        state.obs_count += 1
        if band > state.obs_max:
            state.obs_max = band
            state.plateau_count = 0
        else:
            state.plateau_count += 1

    loop = asyncio.get_running_loop()
    elapsed = (
        loop.time() - state.call_start_ts if state.call_start_ts is not None else 0.0
    )
    reached = _band_ceiling_reached(state, elapsed)
    logger.info(
        "normalcall: 밴드 관측 band=%s obs_max=%d obs_count=%d total=%d plateau=%d elapsed=%.0fs 천장=%s",
        band, state.obs_max, state.obs_count, state.total_answers, state.plateau_count, elapsed, reached,
    )
    if not reached:
        return
    # 천장 도달 → 종료 파이프 합류(새 종료 경로 없음). 이미 종료 진행중이면 양보.
    if state.should_close or state.close_seed_sent:
        return
    state.should_close = True
    logger.info("normalcall: 밴드 천장 도달(obs_max=%d) → 종료 플래그", state.obs_max)
    # 종료 레이스 가드(시계워처와 동일): 비버 idle & 유저 응답 대기 없음이면 직접 주입,
    # 아니면 펌프 turn_end(should_close 경로)/시계워처 백스톱이 주입한다. ★ 질문 주입 아님.
    # M1(시니어): 세션 종료 레이스에 종료 시드 send_text_turn 이 던지면 미회수 태스크 예외로
    # 새어 "exception never retrieved" 로그가 남으므로 여기서 흡수(취소는 재전파).
    if state.turn_id is None and not state.user_turn_open:
        try:
            await _inject_close_seed(session, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 종료중 주입 실패는 백스톱이 마무리(R5)
            logger.warning("normalcall: 밴드 천장 종료 시드 주입 실패(무시): %s", exc)


# --------------------------------------------------------------------------- #
# 펌프: 클라 → Gemini
# --------------------------------------------------------------------------- #
async def _pump_client_to_gemini(client_ws, session: LiveSessionProtocol, state: _CallState) -> None:
    """클라 → Gemini. barge-in off: 비버 발화중이면 마이크 미전송. forward 먼저 후 누적."""
    from starlette.websockets import WebSocketDisconnect

    try:
        while True:
            message = await client_ws.receive()
            if message.get("type") == "websocket.disconnect":
                raise _ClientDisconnect()
            data = message.get("bytes")
            if data and state.turn_id is None:
                await session.send_audio(data)
                state.cur_user_pcm.extend(data)
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
        await _send_json(client_ws, ServerPong(t=getattr(msg, "t", None)))
    elif msg.type == "playback_done":
        state.playback_done_event.set()
    elif msg.type == "hint_used":
        _record_hint_used(state, msg)  # 적재만(응답 불요, D16)


# --------------------------------------------------------------------------- #
# 펌프: Gemini → 클라
# --------------------------------------------------------------------------- #
async def _pump_gemini_to_client(client_ws, session: LiveSessionProtocol, state: _CallState) -> None:
    """Gemini → 클라(상태기계). 턴 경계에서 세그먼트 확정 + 5분 종료 로직."""
    event_count = 0
    async for event in session.events():
        event_count += 1

        # A3 GoAway: 서버가 곧 연결을 닫겠다는 예고(연결 ~10분 한계, S2). 뚝 끊기기 전에
        # 우리가 먼저 우아하게 마무리한다 — 기존 종료 파이프에 합류: should_close 를 세우고,
        # idle 이면 즉시 짧은 작별 시드를 주입(발화중이면 펌프가 turn_end 에서 주입).
        if event.kind == "go_away":
            logger.warning("normalcall: GoAway 수신(time_left=%s) → 종료 절차", event.time_left)
            state.should_close = True
            if state.turn_id is None:
                await _inject_close_seed(session, state)
            continue

        # 재접지 얹기(on_user_turn): "첫 in_tr"(유저 발화 시작) 판별은 _forward_event 가
        # user_turn_open 을 True 로 바꾸기 전에 해야 한다.
        in_tr_first = event.kind == "in_tr" and not state.user_turn_open

        turn_started = await _forward_event(client_ws, event, state)

        if turn_started:
            # 레벨테스트 밴드 관측(무주입): 비버 응답 시작 = 직전 유저 답변 마침 → flush 로
            # cur_user_text 가 비워지기 전에 답변을 캡처해 관측 사이드카를 띄운다(논블로킹).
            _spawn_band_observe(session, state)
            _flush_user_segment(state)  # 비버 발화 시작 → 직전 사용자 세그먼트 확정
            state.user_turn_open = False  # 비버가 응답 시작 = 유저 발화 턴 종료
            if state.call_start_ts is None:
                state.call_start_ts = asyncio.get_running_loop().time()
                logger.info("normalcall: 통화 시계 시작(첫 turn_start)")
            if state.close_seed_sent:
                # 종료 시드 후 비버가 실제 작별 턴을 시작했다 — 이 턴 끝에서만 종료.
                state.close_reply_started = True

        # ── 재접지 "유저 발화 턴에 얹기"(on_user_turn) ──
        # arm 됐고(reground_pending) 아직 안 얹었으면, 유저 발화 턴에 리마인더를 turn_complete=False
        # 로 얹어 비버가 [유저발화+리마인더]에 1회 응답하게 한다(이중발화·잔류 제거 목표).
        # ⛔ 가드①(핵심 안전): should_close/close_seed_sent 면 절대 안 얹음 — 종료 근처 늦은
        #    in_tr 이 작별 턴을 오염(174/178 재발)하는 것을 원천 차단. ②1회만(reground_injected).
        if (REGROUND_MODE == "on_user_turn" and event.kind == "in_tr"
                and state.reground_pending and not state.reground_injected
                and not state.should_close and not state.close_seed_sent):
            attach_now = in_tr_first if REGROUND_ATTACH_AT == "first" else bool(event.is_final)
            if attach_now:
                state.reground_injected = True   # await 전 선점(단일 소유권)
                state.reground_pending = False
                try:
                    await session.send_reground(state.reground_reminder, turn_complete=False)
                    logger.info("normalcall: 재접지 얹기(유저 발화 턴, at=%s, tc=False)", REGROUND_ATTACH_AT)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 재접지 실패는 통화 무영향(R5)
                    logger.warning("normalcall: 재접지 얹기 실패(무시): %s", exc)

        if event.kind == "turn_end":
            _spawn_hint_task(client_ws, state)  # D16 힌트 사이드카 — 태스크 생성만(논블로킹)
            _flush_beaver_segment(state)
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

    logger.warning("normalcall: Live 이벤트 스트림 종료(서버측 close) events=%d", event_count)
    raise _CallFinished()


async def _forward_event(client_ws, event: LiveEvent, state: _CallState) -> bool:
    """단일 LiveEvent 를 즉시 forward 하며 진행중 세그먼트에 누적. 새 턴이면 True."""
    turn_started = False

    if event.kind == "audio":
        if state.turn_id is None:
            state.turn_id = _new_turn_id()
            await _send_json(client_ws, ServerTurnStart(turn_id=state.turn_id))
            turn_started = True
        if event.audio:
            await client_ws.send_bytes(event.audio)  # forward 먼저(반응성 우선)
            state.cur_beaver_pcm.extend(event.audio)

    elif event.kind == "in_tr":
        text = event.text or ""
        # A2: 입력 전사 = 학습자 활동 → 무음 시계 리셋 + 넛지 단계 원복(발화 재개).
        state.last_activity_ts = asyncio.get_running_loop().time()
        state.silence_stage = 0  # 발화 재개 → 넛지 단계 리셋
        state.user_turn_open = True  # 유저 발화 턴 열림(비버 turn_start 시 flush 에서 False)
        await _send_json(client_ws, ServerInputTranscript(text=text))
        if text:
            state.cur_user_text.append(text)
            logger.info("normalcall 👤 user: %s", text)

    elif event.kind == "out_tr":
        if state.turn_id is None:
            state.turn_id = _new_turn_id()
            await _send_json(client_ws, ServerTurnStart(turn_id=state.turn_id))
            turn_started = True
        text = event.text or ""
        await _send_json(client_ws, ServerOutputTranscript(text=text, turn_id=state.turn_id))
        if text:
            state.cur_beaver_text.append(text)
            logger.info("normalcall 🦫 beaver: %s", text)

    elif event.kind == "turn_end":
        turn_id = state.turn_id or _new_turn_id()
        await _send_json(client_ws, ServerTurnEnd(turn_id=turn_id))
        state.last_turn_id = turn_id  # D16: 방금 끝난 턴 id 보존(힌트 태스크 재료)
        state.turn_id = None
        # ⭐ 무음 시계 리셋: 비버가 방금 말을 멈췄다 = 여기서부터 무음이 시작된다.
        # (안 하면 시계가 통화 시작부터 흘러 비버의 긴 발화 직후 넛지가 즉시 터진다.)
        state.last_activity_ts = asyncio.get_running_loop().time()

    return turn_started


async def _inject_close_seed(session: LiveSessionProtocol, state: _CallState) -> None:
    """종료 시드를 정확히 1회만 주입한다(펌프·워처 공용, 단일 소유권 가드).

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
    """경과 감시: CALL_DURATION_S 경과 → should_close, 이후 종료 시드 주입을 보장하고 하드 백스톱.

    ⭐ RC1(소강 스타베이션) 방지: 5분 마크가 비버 발화중에 떨어지면 펌프가 그 턴 끝(turn_end)에서
    시드를 주입하지만, 소강(idle, turn_id None) 구간이면 turn_end 가 오지 않아 시드가 영영
    안 나간다. 그래서 워처가 idle 을 감지하면 직접 주입한다(작별 없는 무음 종료 방지).
    """
    loop = asyncio.get_running_loop()
    while state.call_start_ts is None:
        await asyncio.sleep(0.2)
    while loop.time() - state.call_start_ts < state.call_duration_s:
        # T2: 조기종료(tool 신호/GoAway/무음3단)가 캡 이전에 should_close 를 세우면 즉시
        # 백스톱 관리로 진입 — 안 그러면 조기 close 후에도 캡까지(최대 절대백스톱) 매달린다.
        if state.should_close:
            break
        await asyncio.sleep(0.2)
    state.should_close = True
    logger.info("normalcall: %.0fs 경과/조기신호 → 종료 플래그", state.call_duration_s)

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


async def _reground_once(session: LiveSessionProtocol, state: _CallState) -> None:
    """통화 중간(길이의 REGROUND_AT_FRACTION 지점)에 캐릭터를 딱 1회 되박는다(누적 드리프트 완화).

    REGROUND_MODE 로 방식이 갈린다:
      - "off": 아무것도 안 함(하드닝만 — 폴백).
      - "legacy_idle": fire_at 에 비버 idle & 무음 넛지 없음이면 send_reground(turn_complete=True).
                       비버가 별도 응답(이중발화). 회귀 대비 보존.
      - "on_user_turn": fire_at 에 **arm 만**(reground_pending=True). 실제 얹기는 펌프가 다음 유저
                        발화 턴에서 수행(turn_complete=False). 이 태스크는 send_reground 를 호출하지
                        않는다 → 시각 판정(태스크)과 얹기 실행(펌프) 관심사 분리.
    비활성(reground_reminder None = 레벨테스트 등)이면 즉시 종료. 종료 우선(should_close → arm 취소).
    """
    if REGROUND_MODE == "off" or not state.reground_reminder:
        return
    loop = asyncio.get_running_loop()
    while state.call_start_ts is None:
        await asyncio.sleep(0.2)
    fire_at = state.call_start_ts + state.call_duration_s * REGROUND_AT_FRACTION
    while loop.time() < fire_at:
        if state.should_close:  # 종료 우선 — arm 취소
            return
        await asyncio.sleep(0.2)
    if state.should_close:
        return

    if REGROUND_MODE == "on_user_turn":
        state.reground_pending = True  # 주입은 펌프가(유저 발화 턴에 얹기), 여기선 arm 만
        logger.info("normalcall: 재접지 arm(다음 유저 발화 턴에 얹음)")
        return

    # legacy_idle: 즉시 주입(비버 idle & 무음 넛지 없음일 때만), turn_complete=True.
    while True:
        await asyncio.sleep(0.2)
        if state.should_close:
            return
        if state.turn_id is not None or state.silence_stage > 0:
            continue
        try:
            await session.send_reground(state.reground_reminder, turn_complete=True)
            logger.info("normalcall: 캐릭터 재접지 1회 주입(legacy_idle, tc=True)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 재접지 실패는 통화 무영향(R5)
            logger.warning("normalcall: 재접지 주입 실패(무시): %s", exc)
        return


async def _finish_call(client_ws, state: _CallState, call_id: int | None) -> None:
    """call_ended 송신 → playback_done ack 대기 → WS close(전부 graceful)."""
    from starlette.websockets import WebSocketState

    with contextlib.suppress(Exception):
        if client_ws.client_state == WebSocketState.CONNECTED:
            await _send_json(client_ws, ServerCallEnded(call_id=str(call_id or ""), reason="done"))
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(state.playback_done_event.wait(), timeout=PLAYBACK_DONE_WAIT_S)
    with contextlib.suppress(Exception):
        if client_ws.client_state != WebSocketState.DISCONNECTED:
            await client_ws.close()
