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
    ServerCallEnded,
    ServerHint,
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
    "CascadeCallEnded",
    "CascadeCallStarted",
    "CascadeSentenceMarker",
    "CascadeTurnStart",
    "ServerCallEnded",
    "ServerHint",
    "ClientCascadeStart",
    "ClientCascadeStop",
    "ClientCascadeTiming",
    "ClientPing",
    "ClientPlaybackProgress",
    "ClientRouteChange",
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
    # ⭐ **채널 수**(2026-08-13). 서버는 여태 모노를 **가정만** 했고 클라는 선언할 방법이
    #   없었다 — 그래서 "가정이 틀렸나"를 물을 수조차 없었다. 기본 1 이라 지금 클라의 출력
    #   바이트는 그대로다(클라 변경 0). ⛔ 서버는 스테레오를 **처리하지 못한다**(다운믹스
    #   없음) — 값이 1 이 아니면 오디오 타임라인·STT 가 통째로 틀어지므로 경고를 남긴다.
    channels: int = Field(default=1)
    language: str | None = None  # 미전송이면 서버 설정(STT_V2_LANGUAGE→STT_LANGUAGE)
    aec: AecHint | None = None
    # ── TTS 선택 (⛔ **dev 데모 한정 편의**) ──
    # 사장님이 화면에서 엔진을 골라 A/B 하시려고 연 통로다. env 를 고치고 새 리비전을 띄우는
    # 왕복 없이 통화마다 바꿔 들으실 수 있다.
    # ⚠ 이건 **클라가 서버 기본값을 덮는 구조**다. 나중에 앱이 캐스케이드를 타게 되면
    #   이 통로를 그대로 열어 두면 안 된다 — **원가 통제가 클라로 넘어간다**(엔진마다 단가가
    #   다르고 쿼터도 다르다). 앱 연동 시에는 서버가 정하고 클라는 통보만 받아야 한다.
    # 미전송이면 서버 설정(env → 코드 기본값)을 쓴다. 알 수 없는 값은 서버가 거절한다.
    tts_engine: str | None = Field(default=None, alias="ttsEngine")
    speaking_rate: float | None = Field(default=None, alias="speakingRate")
    style_prompt: str | None = Field(default=None, alias="stylePrompt")

    model_config = {"populate_by_name": True}


class ClientCascadeStop(BaseModel):
    """세션 정상 종료 요청."""

    type: Literal["stop"] = "stop"


class ClientRouteChange(BaseModel):
    """통화 **도중** 출력 장치가 바뀌었다(2026-08-12 프론트 구현 완료).

    ⭐ 무엇을 푸나: `start.aec` 는 **세션 시작 스냅샷**이고 그것도 재생 개통 전 값이다.
      통화 중 이어폰을 뽑으면 서버는 계속 `headset` 을 믿는데 실제 출력은 **스피커폰**
      (에코 최악)이다 — 정확히 그 전이가 안 잡혔다.

    ⛔⛔ **이 프레임은 안 올 수 있다.** 클라가 콜백 등록에 실패하면 **조용히 통지 없이**
      통화가 계속 돈다(진단이 통화를 죽이면 안 된다는 그쪽 원칙). 그래서:
        · `route_change` 가 안 온다 ≠ 라우트가 안 바뀌었다. **보조 신호**다.
        · ⭐ **한 번도 안 와도 동작이 예전과 완전히 같아야 한다**(회귀로 박았다).
          필수로 가정하면 콜백 등록에 실패한 기기에서 **정책이 굳는다**.

    ⚠ 정밀도 한계(프론트 명시):
      · 보고가 **5~15ms(≈480바이트) 늦다** — 플랫폼 콜백은 "바뀌었다"만 주고 결과 라우트는
        한 번 더 조회해야 한다. 실제 전환 지점은 보고값보다 그만큼 **앞**이다.
        ⇒ 경계에서 딱 잘라 판단하지 마라.
      · iOS 는 **통화 시작 직후 짧은 창**을 못 잡는다 — 그 구간은 `start.aec` 가 유일한 근거다.

    Attributes:
        aec: **`start.aec` 와 같은 객체**다. ⛔ 새 필드를 만들지 말고 같은 처리를 다시 태운다 —
            두 곳으로 갈라지면 시작과 도중이 어긋난다.
        uplink_bytes: 이 변경 **직전까지** 클라가 실제로 소켓에 쓴 마이크 PCM 누적.
            PCM16/16k mono = **32,000 B/s 고정**이라 바이트↔ms 는 산수다(`ms = bytes / 32`).
            ⚠ 게이팅으로 **버린 프레임은 안 센다** — 서버가 받지 못한 것이라 포함하면 어긋난다.
    """

    type: Literal["route_change"] = "route_change"
    aec: AecHint | None = None
    uplink_bytes: int = 0


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
    # ⛔ Literal(화이트리스트)을 쓰지 않는다. 모르는 값이 오면 pydantic 이 **메시지 전체를
    #   거부**해 리포트도 원장 절단도 통째로 날아간다 — 필드 하나 때문에 측정이 사라진다.
    #   대신 평문 str 로 받고 **모르는 값은 보수적인 쪽으로** 해석한다(native 가 아니면 안 믿고,
    #   cancel 이 아니면 보정 없음). 클라가 나중에 값을 늘려도 서버는 안 깨진다.
    source: str = "estimate"        # 'native' 만 신뢰. 그 외/누락 = 안 믿는다
    sampled_at: str = "stop"        # 'cancel' 이면 정지 지연 보정. 그 외/누락 = 보정 없음
    # ⭐⭐ **불변식 I6 의 유일한 외부 증인**(2026-08-12 프론트 추가). 클라가 받은
    #   **홀수 길이 바이너리 프레임**의 누적 횟수다. 0 이 정상이다.
    #   ⚠ 왜 클라가 세 주나: 클라 큐는 홀수가 와도 이어붙어 재생이 안 깨진다 — 즉 **자연 신호가
    #     없다.** 우리가 정렬을 놓치면 아무도 모르게 소리만 조금씩 상한다(그 사고를 이미 겪었다).
    #   ⚠ 구버전 클라는 안 보낸다 → 기본 0 으로 두고 **없으면 조용히 넘어간다**(R5).
    odd_frames: int = 0
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
    stop_measure: str = "clear_returned"   # 'hal_drained' 만 실측. 그 외/누락 = 하한
    # ⭐ 이 측정이 **어떤 환경에서 나왔는지**. 강등률(clear_returned 비율)만 보면 오독한다 —
    #   iOS 나 타임스탬프 미지원 라우트는 HAL 잔량을 잴 방법이 아예 없어 **100% 강등이 정상**
    #   이다. 맥락 없이 숫자만 보면 결함으로 읽힌다. 라우트는 통화 중에도 바뀌므로(이어폰을
    #   뽑으면) 세션이 아니라 **측정마다** 싣는다. 빈 문자열 = 미보고.
    #   ⭐ 둘 다 **자유 문자열**이다(화이트리스트 없음). 서버는 이 값을 분류표에 넣고 조회하는
    #   게 아니라 **그룹 키**로만 쓴다 — "같은 라우트에서 hal_drained 가 한 번이라도 나왔나",
    #   "라우트가 바뀌었는데도 계속 0건인가". 그래서 클라가 값을 늘려도(예: receiver) 그대로 돈다.
    platform: str = ""      # 'android' | 'ios' | …
    audio_route: str = ""   # 'speaker' | 'headset' | 'receiver' | 'bt_a2dp' | … (자유)


class ClientTestSay(BaseModel):
    """[dev 훅] 페이크 STT 에 최종 전사를 주입(크레덴셜 0 으로 상태기계 검증)."""

    type: Literal["__test_say"] = "__test_say"
    text: str = ""


class ClientCascadeTiming(BaseModel):
    """클라가 **실제로 들린 시각**을 알려준다 — 비버 턴 하나당 1건(2026-08-15).

    ⭐⭐ 목적은 평균이 아니라 **뺄셈**이다. 서버는 자기가 첫 소리를 **언제 보냈는지** 알고
      (`첫소리=2270ms`), 클라는 그게 **언제 실제로 났는지** 안다. 둘을 빼면 지금까지
      추정만 하고 **한 번도 못 잰** 값이 나온다:

          클라 재생 몫 = audible_ms − 서버 첫소리

    ⛔ 그래서 **턴 단위**여야 한다. 통화 끝에 평균만 받으면 짝을 못 맞춰 뺄셈이 성립하지 않는다.
    ⛔ `turn_id` 는 **비버 턴 id**(`b58`)다 — 사용자 턴이 아니다. 서버가 `첫소리` 를 잰 턴이
      그것이라, 그 키로 조인해야 두 값이 같은 사건을 가리킨다.
    ⚠ R5: 이 메시지가 **안 와도, 필드가 없어도** 통화는 그대로 돈다(구버전 클라가 붙는다).
      계측이 통화를 죽이면 안 된다 — 전부 기본값을 두고 처리 실패는 흡수한다.
    """

    type: Literal["client_timing"] = "client_timing"
    turn_id: str = ""
    # user_turn_end 수신 → **첫 소리가 실제로 난 시각**.
    # ⚠ 원점이 **서버가 "말 끝났다"고 알린 시각**이라, 그 앞의 침묵판정·전송·전사·우리대기
    #   (합쳐 ~1.1초)가 통째로 빠져 있다. 사용자 체감과 이 값이 갈리는 이유가 그것이다.
    audible_ms: int = -1
    # ⭐⭐ **사용자가 입을 연 순간부터** 첫 소리까지(2026-08-15 사장님 지시: "마이크 인식되는
    #   순간부터 시간 재도록"). 클라 로컬 VAD 의 첫 유성 프레임이 원점이다.
    #   ⇒ 이게 **사장님이 실제로 기다리는 시간**이고, 위 `audible_ms` 는 그 부분집합이다.
    #   ⚠ -1 = 못 쟀음. ⛔ 0 으로 채우지 마라 — 0 은 "즉시 났다"로 읽힌다.
    speech_to_sound_ms: int = -1
    # user_turn_end 수신 → turn_start 도착(클라가 이미 쓰던 자 — 같이 받아 맞춰 본다).
    turn_start_ms: int = -1
    cushion_ms: int = -1
    # ⚠ True = 네이티브 잔량을 못 받아 **추정**한 값. 실측과 같은 표에 섞으면 안 된다.
    estimated: bool = False


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
        ClientRouteChange,
        ClientCascadeTiming,
        ClientTestSay,
        ClientTestEvent,
        ClientTestBeaver,
        ClientTestCancel,
    ],
    Field(discriminator="type"),
]


# ────────────────────────────── 서버 → 클라이언트 ──────────────────────────────
class CascadeCallStarted(BaseModel):
    """서버가 **이 통화의 캐릭터를 정했다**는 통지 — 오디오가 흐르기 전 1회.

    ⛔ 왜 필요한가(지금 안 깨지는데도): 클라는 이 값으로 **어느 캐릭터 얼굴을 띄울지** 정하고,
      없으면 앱이 고른 캐릭터로 폴백한다 — 그래서 지금은 정상으로 보인다. 깨지는 조건은
      **서버가 앱 선택과 다른 캐릭터를 고를 때**다. 캐릭터는 우리가 DB 에서 읽으므로
      (`resolve_call_character`: 수신통화면 알람 캐릭터, 그 외엔 member.character_id)
      앱은 그걸 모른 채 자기 얼굴을 띄운다 ⇒ **목소리와 얼굴이 다른 캐릭터**가 된다.
      에러도 안 난다 — 조용히 어긋난다.

    ⚠ **캐스케이드 전용 모델**인 이유는 `name` 한 칸이다. Live 의 `ServerCallStarted` 에
      필드를 더하면 Live 가 내보내는 JSON 이 바뀐다(`CascadeCallEnded` 와 같은 판단).
      ⛔ 같은 `type` 을 가진 모델이 union 에 둘이면 판별이 깨진다 — Live 것은 안 넣는다.
    ⭐ 이름을 같이 싣는 이유(프론트 사정): 프론트는 id 가 아니라 **이름**으로 자산을 고른다 —
      **서버 캐릭터 id 가 환경마다 다르다**(prod 9=Popo / dev 3=Popo). id 만 주면 이름으로
      되짚어야 하고, 그 매핑이 틀리면 또 조용히 어긋난다.
    ⚠ `name` 은 못 읽을 수 있다(캐릭터 행이 없거나 조회 실패 — R5). 그때는 null 이고,
      클라는 예전처럼 id 로 되짚으면 된다. **통화는 계속된다.**
    """

    type: Literal["call_started"] = "call_started"
    character_id: int
    name: str | None = None


class CascadeCallEnded(BaseModel):
    """통화 종료 통지 — **캐스케이드 전용**(Live 의 `ServerCallEnded` 를 안 건드린다).

    ⛔ 두 가지가 Live 와 다르다. 둘 다 실사용에서 사고가 났던 자리다:
      ① `call_id` 가 **null 을 허용**한다. Live 모델은 `str` 필수라 우리가 `str(... or "")` 로
         빈 문자열을 보내고 있었다 — **클라가 `""` 를 유효한 id 로 착각한다.** 없으면 null 이다.
      ② `reason` 이 종료 경로를 가른다: `duration`(시간 만료) · `client`(사용자 종료) ·
         `backstop`(세션 절대 백스톱) · `stream_end`/`error`(STT 쪽).

    ⭐ 왜 필요한가(프론트 보고): 지금 캐스케이드는 **시간 만료 때만** call_ended 를 냈다.
      사용자가 종료 버튼을 누르면 안 나가서, 클라가 `GET /calls` 를 **5회×600ms 폴링**해
      call_id 를 되짚고 있었다 — **3초를 헛돌고, 그 사이 다른 통화가 생기면 엉뚱한 id 를 집는다.**
    """

    type: Literal["call_ended"] = "call_ended"
    call_id: str | None = None
    reason: str = "client"


class CascadeSentenceMarker(BaseModel):
    """구간 마커 — **그 구간 오디오 바로 앞**에 인밴드로 끼운다(2026-08-12 프론트 합의).

    ⭐ 하는 일이 둘이다. 하나로 합친 이유는 **같은 사실의 출처가 둘이면 어긋나기** 때문이다:
      ① 표정: 클라가 문장 단위로 아바타를 바꾼다(`SentenceEmotion`)
      ② 자막: ⛔ 캐스케이드는 지금까지 **비버 자막을 한 번도 안 보냈다**(Live 는 보낸다) —
         사용자가 비버 말을 글로 못 봤다. `text` 가 그 후퇴를 같이 메운다.

    ⚠ **미리 보내지 않는다.** 프론트 재생 파이프는 '도착 시점'이 아니라 '들리는 시점'으로
      얼굴을 움직인다(마커를 오디오 큐에 **위치로** 꽂는다). 그래서 선행 여유가 필요 없고
      **순서만** 맞으면 된다 — 그 구간 첫 오디오 프레임보다 먼저 나가기만 하면 된다.
    ⚠ `seq` 는 **구간 순번**이지 문장 순번이 아니다. 코드스위칭 문장 하나가 언어 구간 2개면
      마커도 2개이고 `text` 는 각각 조각이다(프론트가 이어 붙인다).
    ⚠ `server_bytes` 는 주 키가 **아니다**(순서가 주 키다). 프론트 원장(바이트 단위)과
      **정수로 대조**해 어긋남을 잡는 교차검증용이다 — ms 로 주면 반올림 오차가 조용히 쌓인다.
    ⛔⛔ **이 값은 "그 턴에서" 이 구간 직전까지 보낸 바이트다 — 통화 누적이 아니다.**
      턴이 바뀌면 **0 부터 다시 센다**(취소가 잦아 통화 누적은 기준이 무너진다).
      실기기에서 실제로 어긋났다: `서버=0 우리원장=435840 차이=-435840B` — 클라는 통화
      누적으로 읽고 있었다. 이름이 `server_bytes` 라 그렇게 읽히는 것이 자연스럽다.
      ⇒ **`turn_bytes` 로 바꾸기로 결정**(2026-08-13). 클라와 **동시에** 바꿔야 해서 아직
        안 바꿨다 — 신호가 오면 이름만 갈면 된다(뜻·계산은 그대로다).
      ⚠ 그 전까지의 임시 우회: 프론트가 차분을 쓰되 **`diff < 0` 이면 새 턴으로 보고 리셋**
        하면 된다. 같은 턴 안에서는 지금도 맞고, 깨지는 건 **턴 경계 하나**뿐이다.
    ⛔ `emotion` 은 **이미 이어붙인 결과값**이다(carry-forward 계산은 서버가 끝낸다).
      클라가 상태를 들면 `audio_cancel` 로 마커를 버릴 때 감정이 어긋난다 — 상태 없는 쪽이 안 깨진다.
    ⛔ 값을 화이트리스트로 막지 않는다 — 클라가 모르는 값을 neutral 로 떨어뜨린다.
    """

    type: Literal["sentence"] = "sentence"
    turn_id: str
    seq: int
    emotion: str
    text: str
    server_bytes: int


class CascadeTurnStart(BaseModel):
    """비버 턴 시작 — **캐스케이드 전용**(Live 의 `ServerTurnStart` 를 안 건드린다).

    ⭐ 왜 따로 두나(2026-08-12): 프론트가 이 프레임에 **표정 한 칸**을 원한다. 그런데
      `ServerTurnStart` 는 Live 와 공용이라, 거기에 필드를 더하면 **Live 가 보내는 JSON 도**
      바뀐다(`"emotion":null` 이 붙는다). Live 출력 무변경은 이 프로젝트의 규율이므로
      캐스케이드만 자기 모델을 갖는다 — `type` 은 같아서 클라 입장에선 같은 프레임이다.

    ⚠ 프론트 요구: 별도 프레임이 아니라 **turn_start 한 칸**. 그쪽 선행버퍼가 900ms(적응형
      최대 1200)라 turn_start 부터 첫 소리까지 여유가 있고, 별도 프레임이면 **순서 보장을
      또 따져야** 하기 때문이다.
    ⛔ 값을 **화이트리스트로 막지 않는다**(프론트 요청). "모르는 값이 와도 우리는 안 깨진다
      (neutral 로 떨어뜨린다). 나중에 집합을 늘려도 구버전 앱이 안 죽는다."
      ⇒ 서버가 새 감정을 추가할 때 **클라 배포를 기다릴 필요가 없다**.
    """

    type: Literal["turn_start"] = "turn_start"
    turn_id: str
    # 이 턴의 표정(클라 아바타 어휘: neutral·happy·surprised·sad·angry). 모르면 None.
    emotion: str | None = None


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
    # ⛔ `bargein_min_ms` 는 **2026-08-14 에 뺐다** — 서버에서 그 관문 자체를 삭제했기 때문이다
    #   (사유는 `cascade_session._bargein_allowed` 주석). 안 쓰는 값을 계약에 남겨 두면
    #   클라가 그걸 보고 정책을 추측한다. ⚠ 플러터는 이 필드를 읽지 않았다(`lib/` 0건 확인).
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
        reason: 'silence'(침묵/전사정지 타이머) | 'stt_idle'(STT 가 통째로 조용해서 — 열린
            순간부터 걸리는 무활동 감시) | 'max'(턴 상한 안전망).
            ⚠ 값이 늘면 여기도 같이 늘려라. 클라가 이 문자열로 화면을 가른다.
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


class ServerBeaverPreparing(BaseModel):
    """비버가 **대답을 만드는 중**임을 알린다(배치 합성 전용).

    왜 필요한가: 배치 모드는 전체를 합성한 뒤에 한 번에 들려준다 — 163자면 합성만 20초가
    넘는다. 화면에 아무 표시가 없으면 사용자는 **"끊겼나?" 하고 통화를 끊는다.**
    지연 자체는 이 모드에서 비용이 아니지만 **침묵이 설명되지 않는 것**은 비용이다.
    """

    type: Literal["beaver_preparing"] = "beaver_preparing"
    stage: str                 # "llm" | "tts"
    index: int = 0             # 지금 몇 번째 구간을 합성 중인가(1부터)
    total: int = 0             # 전체 구간 수(llm 단계에선 0)
    elapsed_ms: int = 0


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
    platform: str = ""                        # 측정 맥락(강등률 해석에 필수)
    audio_route: str = ""
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
        ServerBeaverPreparing,
        ServerAudioCancel,
        ServerTestCancelReport,
        # 비버(서버 출력) 턴. ⚠ turn_start 만 캐스케이드 전용이다(표정 한 칸 때문 —
        # Live 모델에 필드를 더하면 Live 출력이 바뀐다). 나머지는 normalcall 과 같은 모델을
        # 재사용한다 — 앱의 재생 상태기계가 묶여 있어 의미를 바꾸면 안 된다(클라 제약 #1).
        CascadeCallEnded,
        CascadeCallStarted,
        CascadeSentenceMarker,
        CascadeTurnStart,
        ServerTurnEnd,
        ServerOutputTranscript,
        # 힌트(D16) — **normalcall 과 같은 모델**을 재사용한다(클라가 이미 처리한다).
        # ⚠ union 에 없으면 직렬화가 다른 타입으로 폴백해 경고가 난다(call_ended 에서 겪었다).
        ServerHint,
        # 통화 종료 — 캐스케이드 전용 모델이다(위 CascadeCallEnded 주석 참고).
        # ⚠ 같은 `type` 을 가진 모델이 union 에 둘이면 판별이 깨진다 — Live 것은 안 넣는다.
        ServerError,
        ServerPong,
    ],
    Field(discriminator="type"),
]

cascade_client_adapter: TypeAdapter[CascadeClientMessage] = TypeAdapter(CascadeClientMessage)
cascade_server_adapter: TypeAdapter[CascadeServerMessage] = TypeAdapter(CascadeServerMessage)
