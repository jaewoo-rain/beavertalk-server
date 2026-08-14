# 캐스케이드 WS 계약 (자동 생성)

> ⛔ **이 파일을 손으로 고치지 마라.** `scripts/dump_cascade_contract.py` 가
> `cascade_protocol.py` 의 모델에서 뽑는다. 고치려면 모델을 고쳐라.
> 사람이 옮겨 적은 표는 반드시 낡는다 — 2026-08-12 에 그걸로 오류 4건이 났다.

⚠ 필드 이름은 **wire 이름**이다(파이썬 속성명이 아니라 실제로 JSON 에 나가는 이름).

## 서버 → 클라

### `__test_cancel_report`  (ServerTestCancelReport)

[dev 훅] 취소 배관 **실측 리포트** — 데모 화면이 이걸 표로 띄운다.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `turn_id` | `str` | ✔ | `—` |
| `rtt_ms` | `int` |  | `0` |
| `client_stop_ms` | `int` |  | `-1` |
| `client_stop_is_lower_bound` | `bool` |  | `true` |
| `stop_measure` | `str` |  | `"clear_returned"` |
| `platform` | `str` |  | `""` |
| `audio_route` | `str` |  | `""` |
| `network_ms` | `int` |  | `-1` |
| `sent_bytes` | `int` |  | `0` |
| `played_server_bytes` | `int` |  | `0` |
| `unplayed_ms` | `int` |  | `0` |
| `spoken_text` | `str` |  | `""` |
| `source` | `str` |  | `""` |
| `sampled_at` | `str` |  | `""` |
| `accepted` | `bool` |  | `true` |
| `note` | `str` |  | `""` |

### `audio_cancel`  (ServerAudioCancel)

(P1) **지금 재생 중이거나 버퍼에 쌓인 이 턴 오디오를 즉시 버려라.**

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `turn_id` | `str` | ✔ | `—` |
| `epoch` | `int` | ✔ | `—` |
| `reason` | `str` |  | `"barge_in"` |
| `report_progress` | `bool` |  | `true` |

### `beaver_preparing`  (ServerBeaverPreparing)

비버가 **대답을 만드는 중**임을 알린다(배치 합성 전용).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `stage` | `str` | ✔ | `—` |
| `index` | `int` |  | `0` |
| `total` | `int` |  | `0` |
| `elapsed_ms` | `int` |  | `0` |

### `call_ended`  (CascadeCallEnded)

통화 종료 통지 — **캐스케이드 전용**(Live 의 `ServerCallEnded` 를 안 건드린다).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `call_id` | `str | null` |  | `null` |
| `reason` | `str` |  | `"client"` |

### `call_started`  (CascadeCallStarted)

서버가 **이 통화의 캐릭터를 정했다**는 통지 — 오디오가 흐르기 전 1회.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `character_id` | `int` | ✔ | `—` |
| `name` | `str | null` |  | `null` |

### `error`  (ServerError)

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `code` | `str` | ✔ | `—` |
| `message` | `str` | ✔ | `—` |
| `recoverable` | `bool` |  | `true` |

### `hint`  (ServerHint)

비버 질문에 대한 예시 답변 힌트(D16 — 서버 사이드카 LLM 생성, 예시 3개).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `turn_id` | `str` | ✔ | `—` |
| `examples` | `list[HintExample]` | ✔ | `—` |

### `input_transcript`  (ServerInputPartial)

사용자 발화 부분/최종 전사.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `text` | `str` | ✔ | `—` |
| `final` | `bool` |  | `false` |
| `turn_id` | `str | null` |  | `null` |

### `output_transcript`  (ServerOutputTranscript)

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `text` | `str` | ✔ | `—` |
| `turn_id` | `str` | ✔ | `—` |

### `pong`  (ServerPong)

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `t` | `int | null` |  | `null` |

### `ready`  (ServerCascadeReady)

세션 준비 완료 + **지금 어떤 엔진·정책으로 도는지**.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `engine` | `str` |  | `"fake"` |
| `turn_silence_ms` | `int` |  | `800` |
| `sample_rate` | `int` |  | `16000` |
| `language` | `str` |  | `""` |
| `bargein_confirm` | `str` |  | `"immediate"` |
| `mic_always_open` | `bool` |  | `false` |

### `sentence`  (CascadeSentenceMarker)

구간 마커 — **그 구간 오디오 바로 앞**에 인밴드로 끼운다(2026-08-12 프론트 합의).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `turn_id` | `str` | ✔ | `—` |
| `seq` | `int` | ✔ | `—` |
| `emotion` | `str` | ✔ | `—` |
| `text` | `str` | ✔ | `—` |
| `server_bytes` | `int` | ✔ | `—` |

### `stt_rollover`  (ServerSttRollover)

내부 STT 스트림 교체 통지(진단 전용 — 턴 판정과 무관).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `reason` | `str` |  | `""` |
| `gap_ms` | `int` |  | `0` |

### `turn_end`  (ServerTurnEnd)

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `turn_id` | `str` | ✔ | `—` |

### `turn_start`  (CascadeTurnStart)

비버 턴 시작 — **캐스케이드 전용**(Live 의 `ServerTurnStart` 를 안 건드린다).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `turn_id` | `str` | ✔ | `—` |
| `emotion` | `str | null` |  | `null` |

### `user_turn_end`  (ServerUserTurnEnd)

**사용자** 턴 종료(SPEECH_ACTIVITY_END + 최종 전사 대기). 비버 턴의 turn_end 와 다르다.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `turn_id` | `str` | ✔ | `—` |
| `text` | `str` |  | `""` |
| `at_ms` | `int` |  | `0` |
| `speech_ms` | `int` |  | `0` |
| `silence_ms` | `int` |  | `0` |
| `pipeline_lag_ms` | `int` |  | `0` |
| `end_lag_ms` | `int` |  | `0` |
| `reason` | `str` |  | `"silence"` |

### `user_turn_start`  (ServerUserTurnStart)

**사용자** 턴 시작(SPEECH_ACTIVITY_BEGIN). barge-in 은 이 신호가 트리거다.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `turn_id` | `str` | ✔ | `—` |
| `at_ms` | `int` | ✔ | `—` |

## 클라 → 서버

### `__test_beaver`  (ClientTestBeaver)

[dev 훅] **가짜 비버 오디오**를 실시간 레이트로 흘려 달라(취소 배관 검증용).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `seconds` | `float` |  | `5.0` |
| `tone` | `bool` |  | `true` |
| `sentence_ms` | `int` |  | `1000` |

### `__test_cancel`  (ClientTestCancel)

[dev 훅] 지금 흐르는 가짜 비버 턴을 **끊어라**(= barge-in 과 같은 취소 배관).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `reason` | `str` |  | `"barge_in"` |

### `__test_event`  (ClientTestEvent)

[dev 훅] 페이크 STT 에 음성활동 이벤트를 주입.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `event` | `Literal[speech_begin, speech_end]` | ✔ | `—` |

### `__test_say`  (ClientTestSay)

[dev 훅] 페이크 STT 에 최종 전사를 주입(크레덴셜 0 으로 상태기계 검증).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `text` | `str` |  | `""` |

### `ping`  (ClientPing)

keepalive 핑(서버는 pong 응답).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `t` | `int | null` |  | `null` |

### `playback_progress`  (ClientPlaybackProgress)

(P1) **실제로 재생한 양.** audio_cancel 직후 1회, 또는 턴 재생 완료 시.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `turn_id` | `str` | ✔ | `—` |
| `epoch` | `int` |  | `0` |
| `played_server_bytes` | `int` |  | `0` |
| `played_ms` | `int` |  | `0` |
| `discarded_ms` | `int` |  | `0` |
| `source` | `str` |  | `"estimate"` |
| `sampled_at` | `str` |  | `"stop"` |
| `odd_frames` | `int` |  | `0` |
| `client_stop_ms` | `int` |  | `-1` |
| `stop_measure` | `str` |  | `"clear_returned"` |
| `platform` | `str` |  | `""` |
| `audio_route` | `str` |  | `""` |

### `route_change`  (ClientRouteChange)

통화 **도중** 출력 장치가 바뀌었다(2026-08-12 프론트 구현 완료).

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `aec` | `AecHint | null` |  | `null` |
| `uplink_bytes` | `int` |  | `0` |

### `start`  (ClientCascadeStart)

세션 시작(오디오 전에 1회). 마이크 규격 + AEC 힌트 — 인증은 소켓 쿼리 토큰이 한다.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|
| `sampleRate` | `int` |  | `16000` |
| `channels` | `int` |  | `1` |
| `language` | `str | null` |  | `null` |
| `aec` | `AecHint | null` |  | `null` |
| `ttsEngine` | `str | null` |  | `null` |
| `speakingRate` | `float | null` |  | `null` |
| `stylePrompt` | `str | null` |  | `null` |

### `stop`  (ClientCascadeStop)

세션 정상 종료 요청.

| 필드 | 타입 | 필수 | 기본값 |
|---|---|---|---|

