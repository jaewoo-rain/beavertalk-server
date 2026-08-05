"""캐스케이드 통화(STT→LLM→TTS) WS 프로토콜.

⛔ 왜 normalcall 의 `protocol.py` 에 메시지를 더하지 않고 파일을 새로 팠나:
  캐스케이드는 **별도 경로**다(인수인계 D5). 신규 메시지 추가 자체는 무해하지만, 통화의 계약
  파일을 실험이 흔들면 캐스케이드를 접을 때 되돌릴 것이 생긴다. 대신 겹치는 메시지는 여기서
  **import 해 재사용**한다(ServerInputTranscript·ServerError·ServerPong·ClientPing) —
  클라 입장에선 여전히 한 벌의 계약이고 중복 정의가 없다.

프레임 규약(**normalcall 과 100% 동일** — 앱 무수정이 목표다, 클라 제약 #4):
  바이너리 = 오디오, 텍스트 = JSON 제어(discriminated union).
  클라→서버 오디오: PCM16 / 16kHz mono, 헤더 없음.
  서버→클라 오디오: PCM16 / 24kHz mono, **헤더 없음**(P1/TTS).

⛔ 이름 주의 — `turn_start` / `turn_end` 는 **비버(서버 출력) 턴**이다(normalcall 계약 그대로).
   특히 `turn_end` 는 **TTS 오디오 마지막 바이트를 보낸 뒤**에 낸다. LLM 텍스트 완료 시점이
   아니다: 현행 클라의 `_turnEnded` 가 "이 턴 오디오가 전부 큐에 들어왔다"를 전제로 지터
   프리버퍼를 우회하기 때문에, 텍스트 완료 시점에 보내면 짧은 대사가 잘린다(클라 제약 #1).
   사용자 턴은 이름을 겹치지 않게 `user_turn_start` / `user_turn_end` 로 따로 둔다.

barge-in 취소 배관 ③(클라 폐기)에 **오디오 프레임 헤더를 쓰지 않는 이유**:
  WebSocket 은 한 연결 안에서 메시지 순서를 보장한다(텍스트/바이너리 혼재여도). 그래서
  "audio_cancel 이후 ~ 다음 turn_start 이전에 도착하는 바이너리 프레임"은 **전부 취소된
  턴의 뒤늦은 조각**으로 유일하게 식별된다. 클라는 그 구간의 프레임을 디코드 없이 버리면
  된다 — 프레임 포맷을 바꿀 필요가 없다(앱 재생 경로 무수정). epoch 는 JSON 메시지에만
  실어 상관관계·진단용으로 쓴다. 설계:
  docs/20260805_1720_캐스케이드-턴감지-최소루프-설계.md §4.

P0(현재 구현): start/stop/ping + 오디오 → ready/user_turn_start/input_transcript/
  user_turn_end/stt_rollover/error/pong. **턴 감지만 본다.**
P1(다음): turn_start/turn_end(비버) + audio_cancel / playback_progress + 서버→클라 오디오.
  계약은 지금 확정해 둔다.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, TypeAdapter

# normalcall 과 의미가 같은 메시지는 재사용한다(중복 정의 금지).
from domains.learning.realtime.protocol import (
    ClientPing,
    ServerError,
    ServerOutputTranscript,
    ServerPong,
    ServerTurnEnd,
    ServerTurnStart,
)

__all__ = [
    "AecHint",
    "BEAVER_FRAME_INTERVAL_MS",
    "CascadeClientMessage",
    "CascadeServerMessage",
    "ClientCascadeStart",
    "ClientCascadeStop",
    "ClientPing",
    "ClientPlaybackProgress",
    "ClientTestBeaver",
    "ClientTestCancel",
    "ClientTestEvent",
    "ClientTestSay",
    "ServerAudioCancel",
    "ServerCascadeReady",
    "ServerInputPartial",
    "ServerSttRollover",
    "ServerTestCancelReport",
    "ServerUserTurnEnd",
    "ServerUserTurnStart",
    "ServerError",
    "ServerOutputTranscript",
    "ServerPong",
    "ServerTurnEnd",
    "ServerTurnStart",
    "cascade_client_adapter",
    "cascade_server_adapter",
]

# 비버 턴 중 서버가 프레임을 내보내는 간격 상한(P1 페이서).
# 클라는 250ms 이상 공백을 "mid-utterance 서버 공백"으로 판정하고, 언더런으로 오인하면
# 재생 쿠션을 상한(1200ms — flutter normalcall_controller.dart:455)까지 올린다 —
# 한 번 올라가면 이후 모든 턴에 그 지연이 붙는다.
# 캐스케이드는 문장 단위 버스트라 그 조건을 상시로 만든다 → 페이서가 무음이라도 이 간격마다
# 내보내 **와이어를 굶기지 않는다**(클라 제약 #2·#3, 설계 §8).
BEAVER_FRAME_INTERVAL_MS = 100


# ────────────────────────────── 클라이언트 → 서버 ──────────────────────────────
class AecHint(BaseModel):
    """클라의 AEC 자기진단 — **세션별** barge-in 정책의 입력.

    AEC 는 기기·라우트마다 다르다. 이어폰은 음향 결합이 사실상 없어 즉시 끊어도 되지만,
    스피커폰 + AEC 미적용이면 비버가 **자기 목소리에** 끊긴다. 전역 설정 하나로는 이 차이를
    표현할 수 없어서 클라가 알려주고 서버가 세션마다 정책을 고른다.

    mode: headset(유선/HFP) | hw(플랫폼 AEC 확인됨) | vpio(iOS VPIO) | none | unknown
    route: 'speaker' | 'headset' | 'bt_a2dp' … (통화 중 바뀌면 재통보 필요 — P1)
    """

    mode: str = "unknown"
    route: str | None = None


class ClientCascadeStart(BaseModel):
    """세션 시작(오디오 전에 1회). 마이크 규격 + AEC 힌트 — 인증은 소켓 쿼리 토큰이 한다."""

    type: Literal["start"] = "start"
    sample_rate: int = Field(default=16000, alias="sampleRate")
    language: str | None = None  # 미전송이면 서버 설정(STT_V2_LANGUAGE→STT_LANGUAGE)
    aec: AecHint | None = None

    model_config = {"populate_by_name": True}


class ClientCascadeStop(BaseModel):
    """세션 정상 종료 요청."""

    type: Literal["stop"] = "stop"


class ClientPlaybackProgress(BaseModel):
    """(P1) **실제로 재생한 양.** audio_cancel 직후 1회, 또는 턴 재생 완료 시.

    이 값이 대화 이력 정합성의 근거다 — 비버가 중간에 잘렸을 때 LLM 이력에는 `played_server_bytes`
    까지 실제로 들린 부분만 남긴다(설계 §5). 안 오면 서버가 추정으로 폴백하되 **짧은 쪽으로
    편향**한다(못 들은 말을 들었다고 치는 것이 그 반대보다 훨씬 나쁘다).

    ⭐ **왜 ms 가 아니라 `played_server_bytes` 인가.** 클라는 서버가 끼운 **무음 패딩과 실제
    대사를 구분할 수 없다** — 둘 다 서버발 바이트로 똑같이 도착한다. 클라가 '무음 제외'를
    시도하면 자기가 만든 필러만 걸러낼 뿐 서버 패딩은 대사로 계산돼 **원장 절단이 틀어진다.**
    그래서 클라는 "서버에서 받은 오디오 중 실제로 스피커로 나간 **바이트 수**"만 보고하고
    (자기 생성 필러는 세지 않는다), **대사/무음 분리는 서버가 원장으로** 한다.
    PCM24k·16bit = 48,000 B/s 고정이라 바이트↔ms 는 산수다.

    Attributes:
        played_server_bytes: 위 값. 원장 절단의 **유일한 기준**.
        source: `native` = 네이티브 재생 카운터(Android getPlaybackHeadPosition ±10~20ms).
            `estimate` = Dart/JS 외삽(±50~150ms). ⛔ **서버는 estimate 를 기본적으로 버린다**
            — 오차가 원장 절단 단위보다 커서 '짧은 쪽 편향' 원칙이 무의미해지기 때문이다
            (CASCADE_TRUST_ESTIMATED_PROGRESS 로 강제 허용은 가능).
        sampled_at: `stop` = **실제로 소리가 멎은 뒤** 샘플(권장). `cancel` = 취소 메시지를
            받은 순간 샘플. 클라는 audio_cancel 수신 후 실제 무음까지 50~120ms 걸리므로,
            `cancel` 이면 서버가 CASCADE_CANCEL_STOP_MS 를 더해 보정한다.
    """

    type: Literal["playback_progress"] = "playback_progress"
    turn_id: str
    epoch: int = 0
    played_server_bytes: int = 0        # ⭐ 주 계약값(아래 설명)
    played_ms: int = 0                  # 진단용 참고치(원장 절단에 쓰지 않는다)
    discarded_ms: int = 0
    source: Literal["native", "estimate"] = "estimate"
    sampled_at: Literal["stop", "cancel"] = "stop"
    # 클라가 **자기 안에서** 잰 시간: audio_cancel 수신 → 소리가 멎기까지(ms). 미보고면 -1.
    client_stop_ms: int = -1
    # ⭐ 그 숫자가 **무엇까지 포함하는가**. client_stop_ms 는 값이 항상 오지만 의미가 둘이라,
    #   구분이 없으면 서버가 **가장 낙관적인 값을 실측으로 믿게 된다**(우리가 재려는 게 정확히
    #   "50~120ms 목표에 드는가"인데, 낙관 편향된 값으로 합격을 내면 실기기에서 뒤집힌다).
    #     hal_drained    = 하드웨어 잔량까지 빠져 **실제로 조용해진 시각**. 판정에 그대로 쓴다
    #     clear_returned = 네이티브 clear() 반환까지만. 회수 경로(재생스레드가 세대변화를 보고
    #                      스스로 flush)가 그 뒤에 올 수 있어 **실제 무음은 이보다 늦다** = 하한
    #   ⛔ 기본값이 clear_returned 인 이유: **누락 = 안 믿는다**(침묵을 실측으로 오해하지 않는다).
    #   ※ source 와 달리 값을 **버리지는 않는다** — 쓸모 있는 하한이므로 성격만 표시한다.
    stop_measure: Literal["hal_drained", "clear_returned"] = "clear_returned"


class ClientTestSay(BaseModel):
    """[dev 훅] 페이크 STT 에 최종 전사를 주입(크레덴셜 0 으로 상태기계 검증)."""

    type: Literal["__test_say"] = "__test_say"
    text: str = ""


class ClientTestEvent(BaseModel):
    """[dev 훅] 페이크 STT 에 음성활동 이벤트를 주입."""

    type: Literal["__test_event"] = "__test_event"
    event: Literal["speech_begin", "speech_end"]


class ClientTestBeaver(BaseModel):
    """[dev 훅] **가짜 비버 오디오**를 실시간 레이트로 흘려 달라(취소 배관 검증용).

    P1(LLM·TTS)이 붙기 전에 **클라의 취소 배관을 실기기에서 검증**하려고 만든 통로다.
    지금은 서버가 오디오를 낼 일이 없어 audio_cancel 을 보낼 수가 없고, 그래서 클라가
    만들어 둔 네이티브 clear() 를 한 줄도 못 돌린다.

    ⛔ 이건 P1 착수가 아니다. 진짜 TTS·LLM 은 붙이지 않는다 — 톤/무음 PCM24k 를 흘릴 뿐이다.
    그래도 **서버 불변식은 그대로 지킨다**(오디오 전에 turn_start, 실시간 페이싱,
    audio_cancel 이 턴 종결 겸함). 훅이 불변식을 어기면 클라 판별식이 깨진다.
    """

    type: Literal["__test_beaver"] = "__test_beaver"
    seconds: float = 5.0            # 흘릴 길이(초)
    tone: bool = True               # True=440Hz 톤(귀로 확인) / False=무음
    sentence_ms: int = 1000         # 이 간격마다 "문장" 하나가 끝난 것으로 원장에 표시


class ClientTestCancel(BaseModel):
    """[dev 훅] 지금 흐르는 가짜 비버 턴을 **끊어라**(= barge-in 과 같은 취소 배관)."""

    type: Literal["__test_cancel"] = "__test_cancel"
    reason: str = "barge_in"


CascadeClientMessage = Annotated[
    Union[
        ClientCascadeStart,
        ClientCascadeStop,
        ClientPing,
        ClientPlaybackProgress,
        ClientTestSay,
        ClientTestEvent,
        ClientTestBeaver,
        ClientTestCancel,
    ],
    Field(discriminator="type"),
]


# ────────────────────────────── 서버 → 클라이언트 ──────────────────────────────
class ServerCascadeReady(BaseModel):
    """세션 준비 완료 + **지금 어떤 엔진·정책으로 도는지**.

    engine="fake" 면 실제 인식이 아니다 — 데모 화면이 배너를 띄워 사람이 착각하지 않게 한다.
    turn_silence_ms 는 **서버 자체 타이머**의 침묵 임계다(STT 설정값이 아니다 — v2 의
    voice_activity_timeout 은 스트림을 닫는 필드라 턴 노브로 못 쓴다).
    측정된 감지지연에서 이 값을 빼면 파이프라인(리전 왕복+인식) 비용이 보인다.
    """

    type: Literal["ready"] = "ready"
    engine: str = "fake"
    turn_silence_ms: int = 800
    sample_rate: int = 16000
    language: str = ""
    bargein_confirm: str = "immediate"  # 이 세션의 barge-in 확인 정책(AEC 힌트로 결정)
    bargein_min_ms: int = 200           # 에코 2차 방어 — 최소 지속
    # 마이크 상시 개방 여부. False 면 클라가 비버 발화 중 마이크를 닫으므로 barge-in 이
    # 사실상 발동하지 않는다(그 상태에서도 통화는 정상 성립해야 한다). 자세한 이유는
    # core/config.py 의 CASCADE_MIC_ALWAYS_OPEN 주석.
    mic_always_open: bool = False


class ServerUserTurnStart(BaseModel):
    """**사용자** 턴 시작(SPEECH_ACTIVITY_BEGIN). barge-in 은 이 신호가 트리거다.

    ⛔ `turn_start`(비버 턴)와 이름을 겹치지 않게 한다 — 클라의 기존 turn_start/turn_end
    핸들러는 비버 오디오 재생 상태기계에 묶여 있다(클라 제약 #1).
    """

    type: Literal["user_turn_start"] = "user_turn_start"
    turn_id: str
    at_ms: int  # 세션 시작 기준 경과(ms) — 서버 monotonic


class ServerInputPartial(BaseModel):
    """사용자 발화 부분/최종 전사.

    normalcall 의 ServerInputTranscript 와 같은 뜻이지만 `final` 플래그가 더 붙는다
    (캐스케이드는 부분 전사도 턴 판정 재료라 클라가 구분해 표시한다).
    """

    type: Literal["input_transcript"] = "input_transcript"
    text: str
    final: bool = False
    turn_id: str | None = None


class ServerUserTurnEnd(BaseModel):
    """**사용자** 턴 종료(SPEECH_ACTIVITY_END + 최종 전사 대기). 비버 턴의 turn_end 와 다르다.

    Attributes:
        speech_ms: 턴 시작 → 마지막 음성 활동 길이.
        silence_ms: 이 판정에 쓴 침묵 임계(서버 타이머 값).
        pipeline_lag_ms: `보낸 오디오 총량 − 마지막 이벤트의 오디오 오프셋`.
            = **리전 왕복 + STT 인식 지연**. STT v2 는 서울·도쿄 리전이 없어 global/us 로
            나가므로 이 항이 고정비로 붙는다. 감지지연을 이 값과 분리해 봐야 무엇을 고칠지
            알 수 있다(임계를 줄일 문제인지, 리전을 옮길 문제인지).
        end_lag_ms: 마지막 음성 활동 → 이 메시지 발신까지의 벽시계 시간.
        reason: 'silence'(침묵 타이머) | 'max'(턴 상한 안전망).
    """

    type: Literal["user_turn_end"] = "user_turn_end"
    turn_id: str
    text: str = ""
    at_ms: int = 0
    speech_ms: int = 0
    silence_ms: int = 0
    pipeline_lag_ms: int = 0
    end_lag_ms: int = 0
    reason: str = "silence"


class ServerSttRollover(BaseModel):
    """내부 STT 스트림 교체 통지(진단 전용 — 턴 판정과 무관).

    gap_ms 동안의 오디오는 버퍼에 담겼다가 새 스트림에 그대로 흘러간다(유실 0, 감지만 지연).
    reason: 'vad_close'(엔진이 닫음) | 'limit'(수명 만료로 우리가 굴림) | 'error'.
    """

    type: Literal["stt_rollover"] = "stt_rollover"
    reason: str = ""
    gap_ms: int = 0


class ServerTestCancelReport(BaseModel):
    """[dev 훅] 취소 배관 **실측 리포트** — 데모 화면이 이걸 표로 띄운다.

    Attributes:
        rtt_ms: `audio_cancel` 을 소켓에 쓴 시각 → `playback_progress` 가 도착한 시각.
            ⚠ **네트워크 왕복이 섞인 값**이다(화면에도 그렇게 표기한다).
        client_stop_ms: 클라가 자기 안에서 잰 값 — audio_cancel 수신 → 소리가 멎기까지.
            **"폐기 실효지연 50~120ms 목표"의 실측치**다. 미보고면 -1.
        client_stop_is_lower_bound: True 면 위 값은 **하한**이다(실제 무음은 더 늦다).
            stop_measure != "hal_drained" 일 때 참. **합격 판정에 그대로 쓰면 안 된다.**
        stop_measure: 그 값이 무엇까지 포함하는지(hal_drained / clear_returned).
        network_ms: rtt_ms − client_stop_ms = 네트워크 왕복 추정(둘 다 있을 때만).
            ⚠ client_stop 이 하한이면 이 값은 **상한**이 된다(빼는 값이 작으므로).
        sent_bytes: 서버가 그 턴에서 보낸 총 바이트.
        played_server_bytes: 클라가 실제로 재생했다고 보고한 바이트.
        unplayed_ms: (sent − played) 를 ms 로 — **버려진 양**. 이만큼이 안 들렸다.
        spoken_text: 원장 절단 결과(실제로 들린 데까지의 대사). 절단이 도는지 눈으로 본다.
    """

    type: Literal["__test_cancel_report"] = "__test_cancel_report"
    turn_id: str
    rtt_ms: int = 0                 # ⚠ 왕복 포함
    client_stop_ms: int = -1        # 클라 자체 소요(-1 = 미보고)
    client_stop_is_lower_bound: bool = True   # True = 실제 무음은 이보다 늦다
    stop_measure: str = "clear_returned"      # 그 값이 무엇까지 포함하는가
    network_ms: int = -1            # rtt − client_stop (둘 다 있을 때만)
    sent_bytes: int = 0
    played_server_bytes: int = 0
    unplayed_ms: int = 0
    spoken_text: str = ""
    source: str = ""
    sampled_at: str = ""
    accepted: bool = True           # False = 서버가 그 진행도를 버렸다(사유는 note)
    note: str = ""


class ServerAudioCancel(BaseModel):
    """(P1) **지금 재생 중이거나 버퍼에 쌓인 이 턴 오디오를 즉시 버려라.**

    barge-in 취소 배관의 ③(클라 폐기). ①TTS 합성 중단 ②서버 송신 중단과 **동시에** 나간다.
    이게 없으면 서버가 멈춰도 클라는 이미 받은 수백 ms~수 초를 끝까지 재생한다 = 비버가
    몇 초 더 말한다.

    클라 규칙: 이 메시지를 받으면 (a) 재생 중지 + 큐 폐기, (b) **다음 turn_start 전까지
    도착하는 바이너리 프레임을 전부 버린다**(WS 순서 보장으로 그 구간 = 취소된 턴의 잔여).
    report_progress=True 면 played_server_bytes 를 playback_progress 로 되보낸다(이력 절단 근거).
    epoch 는 요청/응답 상관관계와 로그 대조용이다 — 오디오 프레임에는 싣지 않는다.
    """

    type: Literal["audio_cancel"] = "audio_cancel"
    turn_id: str
    epoch: int
    reason: str = "barge_in"  # 'barge_in' | 'abort' | 'call_end'
    report_progress: bool = True


CascadeServerMessage = Annotated[
    Union[
        ServerCascadeReady,
        ServerUserTurnStart,
        ServerInputPartial,
        ServerUserTurnEnd,
        ServerSttRollover,
        ServerAudioCancel,
        ServerTestCancelReport,
        # 비버(서버 출력) 턴 — normalcall 과 **같은 모델을 재사용**한다. 앱의 재생 상태기계가
        # 이 두 메시지에 묶여 있어 의미를 바꾸면 안 된다(클라 제약 #1).
        ServerTurnStart,
        ServerTurnEnd,
        ServerOutputTranscript,
        ServerError,
        ServerPong,
    ],
    Field(discriminator="type"),
]

cascade_client_adapter: TypeAdapter[CascadeClientMessage] = TypeAdapter(CascadeClientMessage)
cascade_server_adapter: TypeAdapter[CascadeServerMessage] = TypeAdapter(CascadeServerMessage)
