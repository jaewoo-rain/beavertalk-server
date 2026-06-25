# 2026-06-25 09:45 · normalcall 프론트엔드(Flutter) WebSocket 연동 가이드

> **클라이언트 = Flutter 앱.** normalcall 실시간 음성통화 WS 에 어떻게 붙는지의 정본 계약 + Flutter 레시피.
> 백엔드 설계: [백엔드 구현 설계](20260625_0936_normalcall-backend-design.md). 메시지 원본: `domains/learning/realtime/protocol.py`.
> (이전 버전은 브라우저 기준이었음 — 본 문서는 Flutter 로 개정. 프로토콜 계약은 동일, 오디오/소켓 구현만 네이티브로.)

---

## 0. 클라 종류와 무관한 계약 (그대로)

| 항목 | 값 |
|---|---|
| WS URL | `wss://<host>/api/v1/calls/stream?token=<JWT access>` (로컬 `ws://host:8000/...`) |
| 인증 | JWT access 토큰. **쿼리 `?token=`** (Flutter 는 헤더도 가능 — 아래 §1) |
| 프레임 | **바이너리=raw PCM** / **텍스트=JSON 제어** |
| 입력 오디오(마이크→서버) | **PCM 16-bit / 16kHz / mono / LE** |
| 출력 오디오(서버→스피커) | **PCM 16-bit / 24kHz / mono / LE** |
| 시작 | 연결 직후 `{type:"start", character_id}` 1회 |
| 종료 | `call_ended` 수신 → 끝말 재생 후 `{type:"playback_done"}` → close |
| 결과 | `GET /api/v1/calls/{id}/status` 폴링 → `done` → `GET /api/v1/calls/{id}/result` |

→ 메시지 프로토콜·PCM 포맷·시퀀스는 §2·§3 그대로. **달라지는 건 §4(마이크)·§5(재생)·§8(생명주기)·§9(구현)** = 브라우저 API 대신 Flutter 플러그인.

---

## 1. 연결 (Flutter)

권장 패키지: `web_socket_channel`. 모바일에서 헤더가 필요하면 `IOWebSocketChannel.connect(url, headers: {...})` 도 가능하지만, **쿼리 `?token=` 으로 통일**하는 걸 권장(웹/앱 동일, 백엔드는 쿼리만 읽음).

```dart
import 'package:web_socket_channel/web_socket_channel.dart';

String wsUrl(String base, String token) {
  // base 예: https://api.example.com  또는  http://10.0.2.2:8000 (안드로이드 에뮬레이터)
  final ws = base.replaceFirst(RegExp(r'^http'), 'ws').replaceAll(RegExp(r'/$'), '');
  return '$ws/api/v1/calls/stream?token=${Uri.encodeComponent(token)}';
}

final channel = WebSocketChannel.connect(Uri.parse(wsUrl(base, token)));
channel.stream.listen(_onMessage, onDone: _cleanup, onError: (_) => _cleanup());
```

- 토큰 무효/없음 → 서버가 **1008 close** → `onDone`/`onError` 로 들어옴 → "로그인 필요" 처리.
- 보안: 네이티브 앱은 브라우저의 "HTTPS여야 마이크 동작" 제약이 **없다**. 단 운영은 TLS(`wss`) 권장. 평문 `ws` 는 `android:usesCleartextTraffic`/ATS 예외 설정 필요.
- **권한**: `permission_handler` 로 마이크 권한 먼저 요청.
  ```dart
  if (!await Permission.microphone.request().isGranted) { /* 안내 후 중단 */ }
  ```

---

## 2. 전체 시퀀스 (계약 — 그대로)

```
앱                                        서버
 │ WS connect ?token=JWT                   │ decode_token → member_id (실패 1008 close)
 │ {type:"start", character_id, locale?}   │ DB조회 → 프롬프트 → Call(ongoing) → Gemini Live open
 │                                          │ ← 선톡: turn_start → (binary PCM24k)×N + output_transcript×N → turn_end
 │ (binary PCM16k 마이크 스트림) ─────────▶ │ (비버 발화중 마이크 무시 = barge-in off)
 │                                          │ ← input_transcript / turn_start … turn_end (반복)
 │                                          │ … 서버 타이머 경과 → 비버 작별 …
 │                                          │ ← call_ended {call_id, reason}
 │ {type:"playback_done"} ────────────────▶ │ → WS close
 │ GET /calls/{id}/status → "done"          │
 │ GET /calls/{id}/result                   │ → 요약 + 배운 표현(+TTS voice_url)
```

---

## 3. 메시지 프로토콜 (계약 — 그대로)

- **바이너리 프레임** = raw PCM (헤더 없음). 앱→서버 16kHz, 서버→앱 24kHz, 둘 다 16-bit signed **LE**, mono. Flutter 에선 `Uint8List`.
- **텍스트 프레임** = JSON 제어.

**앱 → 서버 (텍스트):** `start{character_id:int, locale?:string}` · `playback_done{turn_id?}` · `ping{t?}`
**서버 → 앱 (텍스트):**
| type | 필드 | 동작 |
|---|---|---|
| `turn_start` | turn_id | 비버 발화 시작(말하는 중 UI) |
| `output_transcript` | text, turn_id | 비버 자막 조각 → turn_id 별 누적 |
| `input_transcript` | text | 내 말 자막 조각 → 누적 |
| `turn_end` | turn_id | 비버 턴 끝(자막 문단 확정) |
| `call_ended` | call_id, reason | 종료 → 끝말 재생 후 close + 결과 폴링 |
| `error` | code, message, recoverable | 오류 |
| `pong` | t? | ping 응답 |

> 자막은 **partial 조각**으로 여러 번 온다. turn_id 별로 이어 붙이고 turn_end 에서 확정. `interrupted` 는 barge-in off 라 정상 흐름에 없음.

수신 분기:
```dart
void _onMessage(dynamic data) {
  if (data is String) {                  // 텍스트 = 제어 JSON
    _handleControl(jsonDecode(data) as Map<String, dynamic>);
  } else if (data is List<int>) {        // 바이너리 = 비버 PCM24k
    _player.feed(Uint8List.fromList(data));
  }
}
void _send(Map<String, dynamic> m) => channel.sink.add(jsonEncode(m));
```

---

## 4. 마이크 입력 (PCM 16kHz) — Flutter

**브라우저의 AudioWorklet 다운샘플이 불필요하다.** 네이티브 레코더에 `sampleRate: 16000, mono, pcm16` 을 지정하면 **OS가 16kHz로 리샘플한 PCM 바이트 스트림**을 그대로 준다 → WS 바이너리로 흘려보내면 끝.

권장: `flutter_sound`(레코드+재생 한 플러그인). 대안: `record`(startStream), `mic_stream`.

```dart
import 'package:flutter_sound/flutter_sound.dart';

final _recorder = FlutterSoundRecorder();
StreamSubscription? _micSub;

Future<void> startMic(WebSocketChannel channel) async {
  await _recorder.openRecorder();
  // 통화용 오디오 세션(에코 제거/이어피스) — call_ended 후 close.
  await _recorder.setSubscriptionDuration(const Duration(milliseconds: 100));
  final controller = StreamController<Uint8List>();
  _micSub = controller.stream.listen((bytes) {
    if (bytes.isNotEmpty) channel.sink.add(bytes);   // PCM16k 바이너리 전송
  });
  await _recorder.startRecorder(
    toStream: controller.sink,
    codec: Codec.pcm16,        // raw PCM 16-bit
    sampleRate: 16000,         // ⭐ 서버 입력 포맷
    numChannels: 1,            // mono
    // iOS 에코제거: enableVoiceProcessing(가능 버전) 또는 AVAudioSession voiceChat 모드.
  );
}
```

- **barge-in off**: 비버 발화중 서버가 마이크를 무시하므로 계속 보내도 안전. 대역폭을 아끼려면 `turn_start`~`turn_end` 사이 전송을 멈춰도 됨(선택).
- **에코 제거(중요)**: 스피커로 비버 음성이 나오므로, AEC 없으면 비버가 자기 소리를 듣는다. barge-in off 가 1차 방어지만, iOS 는 `AVAudioSession` `.voiceChat`/`.playAndRecord`, Android 는 `AcousticEchoCanceler`(record 패키지 옵션/네이티브)로 AEC 활성 권장.
- 정리: `await _recorder.stopRecorder(); await _recorder.closeRecorder(); _micSub?.cancel();`

---

## 5. 서버 오디오 재생 (PCM 24kHz) — Flutter

서버 바이너리(PCM24k)를 raw PCM 스트림 플레이어로 순차 재생. `flutter_sound` `startPlayerFromStream` 사용.

```dart
final _player = FlutterSoundPlayer();

Future<void> openPlayer() async {
  await _player.openPlayer();
  await _player.startPlayerFromStream(
    codec: Codec.pcm16,
    sampleRate: 24000,    // ⭐ 서버 출력 포맷
    numChannels: 1,
    interleaved: true,    // mono 라 무관하지만 명시
  );
}

// 수신한 PCM24k 청크 공급(버전에 따라 feedUint8FromStream / foodSink 등):
void feed(Uint8List pcm) {
  _player.feedUint8FromStream(pcm);   // flutter_sound 버전별 API 명칭 다름 — 설치 버전 확인
}
```

> ⚠️ **flutter_sound 버전별로 stream API 명이 다르다**(`foodSink.add(FoodData(bytes))` / `feedFromStream` / `feedUint8FromStream`). 설치 버전 문서 확인 필수. raw PCM 스트리밍이 까다로우면 대안: 청크를 모아 짧은 WAV 헤더를 붙여 순차 재생, 또는 `flutter_pcm_player`/플랫폼 채널(AudioTrack/AVAudioEngine)로 직접 PCM push.
- 컨텍스트/플레이어는 **세션 간 유지**하고 종료 시 stop 만(재오픈 비용·끊김 회피). 통화 완전 종료 시 `stopPlayer/closePlayer`.
- **끝말 보장**: `call_ended` 후 곧장 닫지 말고, 공급한 PCM 이 다 재생될 때까지(스트림 drain) 기다린 뒤 `playback_done` 전송 + WS close. flutter_sound 의 재생 완료/큐 상태로 판정하거나, 마지막 청크 후 짧은 지연(예: 남은 버퍼 길이)을 둔다.

---

## 6. 제어 메시지 처리

```dart
String? _curTurn;
String? _callId;

void _handleControl(Map<String, dynamic> m) {
  switch (m['type']) {
    case 'turn_start':        _curTurn = m['turn_id']; setBeaverSpeaking(true); break;
    case 'output_transcript': appendBeaverSubtitle(m['turn_id'], m['text']); break;
    case 'input_transcript':  appendUserSubtitle(m['text']); break;
    case 'turn_end':          setBeaverSpeaking(false); finalizeSubtitle(m['turn_id']); break;
    case 'call_ended':
      _callId = m['call_id'];
      _onPlaybackDrained(() { _send({'type': 'playback_done'}); channel.sink.close(); pollResult(_callId!); });
      break;
    case 'error':             showError(m); if (m['recoverable'] == false) cleanup(); break;
    case 'pong':              break;
  }
}
```

---

## 7. 통화 종료 후 — 결과 조회 (REST)

분석은 비동기. status 폴링 후 결과. REST 는 **`Authorization: Bearer <JWT>` 헤더** 사용(WS 와 달리 헤더 가능).

```dart
Future<void> pollResult(String callId) async {
  for (var i = 0; i < 30; i++) {                              // ~60s
    final s = await api.get('/api/v1/calls/$callId/status');  // {status: ongoing|analyzing|done|failed|unknown}
    final st = s['status'];
    if (st == 'done')   { await loadResult(callId); return; }
    if (st == 'failed') { showAnalysisFailed(); return; }
    await Future.delayed(const Duration(seconds: 2));
  }
  showTimeout();
}
// result = { call_id, summary, rating, average, sentences:[{sentence_id, korean_sentence, native_sentence, voice_url, is_bookmarked}] }
// voice_url = 표현 TTS 재생 URL(public). just_audio 등으로 재생. (TTS 미활성 구간엔 null → 버튼 비활성)
```
- 연습/지표 화면은 기존 `POST /api/v1/sentences/{id}/reviews`(발음 채점) 흐름(Pass 2).

---

## 8. 생명주기 & 엣지 케이스 (Flutter)

1. **단일 통화 세션**: 통화 로직을 위젯이 아니라 **싱글톤 서비스/Provider**(Riverpod/GetIt)에 두고, 화면 재빌드·핫리로드로 WS/레코더가 중복 생성되지 않게. 진입 시 "이미 활성" 가드.
2. **권한 우선**: 마이크 권한 거부 시 통화 진입 차단 + 설정 유도.
3. **오디오 세션 모드**: 통화이므로 `playAndRecord` + 스피커/이어피스 라우팅 + AEC. (flutter_sound `setAudioSession`/네이티브.)
4. **정리(dispose)**: 화면 이탈/통화 종료 시 — recorder stop+close, player stop+close, mic 구독 cancel, `channel.sink.close()`, 세션 플래그 해제.
5. **백그라운드/인터럽트**: 전화 수신·앱 백그라운드 시 일시정지/종료 처리(AudioSession interruption 콜백).
6. **재연결**: 통화는 단발성. `error.recoverable==true`/비정상 close 시 1~2회만. 남은 시간은 서버 주도이니 표시하지 말고 `call_ended` 만 신뢰.
7. **엔디안**: PCM16 **LE**. 네이티브 플러그인은 LE 로 주고받으므로 보통 변환 불필요. 수동 변환 시 `ByteData ... Endian.little`.

---

## 9. 레퍼런스 서비스 골격 (Dart)

```dart
class NormalCallService {
  WebSocketChannel? _ch;
  final _recorder = FlutterSoundRecorder();
  final _player = FlutterSoundPlayer();
  bool _active = false;

  Future<void> start({required String base, required String token, required int characterId}) async {
    if (_active) return;                      // 중복 방지
    if (!await Permission.microphone.request().isGranted) return;
    _active = true;

    await _player.openPlayer();
    await _player.startPlayerFromStream(codec: Codec.pcm16, sampleRate: 24000, numChannels: 1);

    _ch = WebSocketChannel.connect(Uri.parse(wsUrl(base, token)));
    _ch!.stream.listen(_onMessage, onDone: _cleanup, onError: (_) => _cleanup());

    _send({'type': 'start', 'character_id': characterId});   // open 직후
    await _startMic();
  }

  void _onMessage(dynamic d) {
    if (d is String) _handleControl(jsonDecode(d));
    else if (d is List<int>) _player.feedUint8FromStream(Uint8List.fromList(d));  // 버전별 API 확인
  }

  void _send(Map<String, dynamic> m) => _ch?.sink.add(jsonEncode(m));

  Future<void> _cleanup() async {
    await _recorder.stopRecorder(); await _recorder.closeRecorder();
    await _player.stopPlayer();     await _player.closePlayer();
    await _ch?.sink.close();
    _ch = null; _active = false;
  }
  // _startMic / _handleControl / pollResult 는 §4·§6·§7
}
```

---

## 10. 권장 패키지

| 용도 | 패키지 | 비고 |
|---|---|---|
| WebSocket | `web_socket_channel` | 바이너리=Uint8List, 텍스트=String. 헤더 필요시 `IOWebSocketChannel` |
| 마이크(PCM16k 스트림) | `flutter_sound` (또는 `record`/`mic_stream`) | `Codec.pcm16, sampleRate:16000, numChannels:1` |
| 재생(PCM24k 스트림) | `flutter_sound` `startPlayerFromStream` | 버전별 feed API 확인. 대안: 플랫폼 채널 AudioTrack/AVAudioEngine |
| 권한 | `permission_handler` | 마이크 |
| TTS/결과 음성 재생 | `just_audio` | result `voice_url`(http) 재생 |
| REST | `dio`/`http` | `Authorization: Bearer` |

---

## 11. 체크리스트 (Flutter 구현)

- [ ] 마이크 권한 요청 → 거부 시 차단
- [ ] `wss://<host>/api/v1/calls/stream?token=JWT` 연결, 1008(onDone) 처리
- [ ] open 직후 `{type:"start", character_id}` 전송
- [ ] 레코더 `pcm16/16000/mono` → 바이너리 청크 그대로 `channel.sink.add`
- [ ] 수신: 바이너리=PCM24k 플레이어 feed, 텍스트=제어 JSON
- [ ] 자막 turn_id 별 누적, turn_end 확정
- [ ] `call_ended` → 끝말 재생 drain → `playback_done` → close → status 폴링 → result
- [ ] AEC/오디오 세션(playAndRecord, voiceChat) 설정으로 에코 방지
- [ ] 싱글톤 세션 + dispose 정리(recorder/player/ws/플래그)
- [ ] result `voice_url` 표현 음성 재생(just_audio)
```
