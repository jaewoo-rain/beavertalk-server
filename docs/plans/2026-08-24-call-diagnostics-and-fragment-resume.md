# 실행 계획 2건: ① 통화 계측 로그 시스템 ② 5분 조각 이어하기

- **작성일**: 2026-08-24
- **상태**: 계획 확정 / 미구현
- **브랜치(예정)**: 서버 `feat/call-diag`, 프론트 `feat/fragment-resume`
- **관련 리포**: `beavertalk-server` · `beavertalk-flutter`

---

## 0. 왜 지금 이걸 하는가 — 조사에서 드러난 사실

### ⛔ 발견 1. 프론트 응답시간 계기가 **라이브에서 한 번도 안 돌았다**

```
Live 프로토콜(protocol.py)에  user_turn_start / user_turn_end / client_timing  →  전부 0건
                                                (캐스케이드 cascade_protocol.py 에만 있다)

프론트  _userTurnEndForAudioMs 는 `case 'user_turn_end'` 에서만 세워진다 (:3866)
        _recordResponseTime:  if (endedAt == null) return;   ← 라이브에선 매번 즉시 반환
```

⇒ 응답시간 계기·응답시간 카드·`client_timing` 전송이 **전부 죽은 코드**였다.
사장님이 "응답이 느리다"고 하셨을 때 **앱에도 서버에도 숫자가 없었다.**

그리고 설령 보냈어도 서버가 버린다 — `client_timing` 이 Live 유니온에 없어
`normalcall 제어 메시지 무시` 로 삼켜진다(`call_session.py:2396-2399`).

### ⛔ 발견 2. 릴리스 빌드에서 프론트 로그가 통째로 사라진다

```dart
void _log(...) { if (!kDebugMode) return; }   // normalcall_controller.dart:943
```
사장님 폰에서 아무것도 안 보이던 진짜 이유. **새 계측을 여기 뒤에 두면 만드는 순간 무의미하다.**

### ⛔ 발견 3. 프론트가 이어하기를 **아예 안 쓴다**

`continues_call_id` / `resumeFrom` → Flutter 전체 grep **0건**. 서버엔 계약이 다 있는데
프론트가 한 번도 안 보낸다.

### ⛔ 발견 4. 서버에 누적 통화 시간이 없다

```python
call.total_time = total_time      # normalcall_service.py:1013 — 대입이지 += 가 아니다
```
조각 3개짜리 체인의 `total_time` 은 **마지막 조각 길이만** 남는다. `usage_*` 원가 컬럼도 같다.

### ⛔ 발견 5. 한도 게이트가 이어하기보다 **먼저** 돈다 (잠복 버그)

```
call_session.py:1144  is_daily_limit_reached(...)   ← 여기서 거절
call_session.py:1237  resume_call(...)              ← 이어하기 판정은 그 다음
```
조각1이 끝나면 그 행이 `has_call_in_window` 를 참으로 만들고, `DAILY_CALL_LIMIT_BY_PLAN` 은
pro/max 도 `normal:1` 이다 ⇒ **한도를 켜는 순간 Pro/Max 조각2가 "오늘 이미 통화함"으로 거절된다.**
지금은 `ENV=test` + `DAILY_LIMIT_ENFORCED` 미설정이라 한도가 꺼져 있어 잠복 중이다(실측 확인).

### ⭐ 발견 6. 배포 실값 (에이전트 추측을 정정)

```
ENV = test                      DAILY_LIMIT_ENFORCED 없음 → 한도 OFF (확정)
LIVE_CALL_END_OWNER = server    ⇒ 지금은 서버가 종료를 소유한다
```

---

## 1. 목표 & 범위

### 계획 ① 통화 계측 로그
프론트가 **영상·감정·응답시간·재생 대조**를 재서 **서버로 보낸다**. 릴리스에서도 돈다.

### 계획 ② 5분 조각 이어하기
```
5분 도달 → 조각 종료 → 시트
  · Free    구독 유도 → 결제 → (홈이 아니라) 통화 화면으로 복귀 → 이어하기
  · Pro/Max 바로 이어하기 시트 → 이어하기
시간은 누적된다 — 5분 → 10분 → 15분
```

### ⛔ 비범위
- **UI 신규 제작 금지**(사장님 지시). 기존 시트·페이월·결제 화면을 **연결만** 한다.
- 서버 누적 `total_time` 정정(발견 4) — 별건 백로그
- 마지막 조각의 작별(현재 아무도 안 함) — 별건

---

## 2. ⭐ 종료 소유권 — `server` 를 유지한다 (설계 변경)

사장님은 "5분 끊김은 프론트가"라고 하셨지만, 조사가 **반대를 가리킨다**:

```
CallState.callId 는 `call_ended` 에서만 세팅된다 (controller:3882)
call_started 는 character_id 와 name 만 읽는다 (:3709-3716)

⇒ 프론트가 먼저 끊으면 call_ended 가 안 와서 **그 조각의 call_id 를 모른다**
  = 이어할 대상을 잃는다. 지금 있는 우회는 GET /calls 폴링(600ms×5회, best-effort)뿐
```

⇒ **서버가 끊어주면 `call_id` 가 공짜로 오고, 작별 인사도 안 잘린다.**
`LIVE_CALL_END_OWNER=server` 를 그대로 둔다.

⭐ **더 나은 대안(서버 3줄)**: `call_started` 에 `call_id` 를 실으면 조각 시작 0초부터 번호를
알아 **어떤 종료 경로에서도** 체인이 안 끊긴다. 프론트는 `:3709` 에 한 줄 추가로 끝.
⇒ **이걸 권한다.** 종료 소유권과 무관하게 견고해진다.

---

## 3. 계획 ① — 통화 계측 로그

### 3-1. 무엇을 재나 (이벤트 그룹)

| 그룹 | 이벤트 | 답하는 질문 |
|---|---|---|
| **체감 지연** | `voice_on` · `voice_off` · `turn_start_rx` · `audio_rx1` · `audible1` · `underrun` · `mic_gate` | "응답이 느린가" — 원점은 **클라 로컬 VAD**(`_markVoicedIfLoud`), 서버 통지에 의존하지 않는다 |
| **감정** | `mk_rx` · `mk_fire` · `mk_drop` · `mk_unknown` | "감정이 도착했나 / **화면에 언제 떴나**" — 봉투 큐가 최대 1.2~2.5초 미룬다 |
| **영상** | `vid_ready` · `vid_talk` · `vid_emo` · `vid_dec` · `vid_swap` · `vid_stall` | "영상이 도는가" — 디코더 수(`vid_dec`)가 얼어붙음의 실제 원인 |
| **대조** | `turn_done`(rx_bytes vs played_bytes) · `win`(5초 롤업) · `ws` · `summary` | ⭐ 사장님 요구 핵심: "음성 플레이 시간이 서버가 보낸 것과 다르지 않은지" (48,000 B/s 고정 산수) |

⭐ **조인 키가 공짜다**: 서버 `face_seq` 는 통화 스코프 단조 증가고 로그에 `전송 seq=N` 으로
이미 남는다. 클라 `_onSentenceMarker` 가 그 `seq` 를 읽는다 ⇒ **서버 송신 ↔ 클라 수신 ↔ 클라
발화가 seq 하나로 조인된다. 서버 변경 0.**

### 3-2. 전송 — WS 배치 (REST 기각)

**REST 를 안 쓰는 이유**: REST 는 `call_id` 가 필요한데 **정확히 고장난 통화에서 그 id 가 없다**.
그리고 인증이 둘로 갈린다(WS=Supabase 토큰, REST=앱 JWT).

**오디오와 같은 소켓인데 위험하지 않나** — CPU 가 아니라 head-of-line blocking 이 문제다.
규율 셋으로 막는다:

```
R-a  프레임 상한 2KB          최악 업링크(128kbps)에서도 125ms
R-b  _micGated == true 일 때만 flush
     ⭐ 비버가 말하는 동안 업링크는 완전히 비어 있다(controller:1962)
        그 창에 보내면 마이크 프레임을 단 하나도 밀지 않는다
R-c  핫패스에서 인코딩 금지    링버퍼 append 만. 직렬화는 이미 도는 5초 타이머에 얹는다
```

**원가**: 통화당 27KB(full). 하루 1,000통 = 0.8GB/월 → Cloud Logging 무료구간 안, 사실상 0원.
⛔ **DB 에는 배치를 넣지 마라** — `summary` 1행(약 300B)만.

### 3-3. 서버 계약

```python
# protocol.py 에 추가 (유니온에 '더하기만' — 서버→클라는 한 글자도 안 바뀐다)
class ClientDiag(BaseModel):
    type: Literal["client_diag"] = "client_diag"
    seq: int = 0                  # 배치 번호(통화 스코프). 구멍 = 유실
    anchor_epoch_ms: int = 0      # 첫 배치에만 의미. t 의 원점
    level: str = "summary"        # 'summary' | 'full'
    dropped: int = 0              # 상한으로 버린 건수(0이 정상)
    events: list[dict[str, Any]] = Field(default_factory=list)
```

⛔ **`events` 를 discriminated union 으로 만들지 마라** — 클라가 필드 하나를 늘리면
pydantic 이 **배치 전체를 거부**해 그 통화 계측이 통째로 사라진다.

- `_handle_client_control` 에 `elif` 1줄 + `_record_client_diag`(상한·절단·try/except)
- 적재: 배치당 `logger.info` 1줄(JSON) + `summary` 만 DB 1행
- **`client_timing` 도 Live 유니온에 합류**(발견 1의 BUG-2 수정)
- (권장) `ServerPong.s` 에 서버 epoch ms → 클라 전 이벤트가 서버 시계에 얹힌다(3줄)

### 3-4. 켜고 끄기

```
⛔ kDebugMode 뒤에 두지 마라 — 그게 지금 아무것도 안 보이는 이유다
레벨      summary(≈13KB) / full(≈27KB)
주인      call_started 에 additive `diag: "off"|"summary"|"full"` → 앱 배포 없이 끈다
클라 기본  summary. --dart-define=DIAG=full|off 가 이긴다
배포 순서  ⛔ 서버 먼저 → 앱 나중 (반대면 서버가 배치마다 '제어 메시지 무시' 를 찍는다)
```

### 3-5. 개인정보

**숫자와 열거값만.** 전사 원문 금지 — "이미 DB 에 있으니 괜찮다"는 성립하지 않는다.
**DB 와 로그는 보존기간·접근권한·유출경로가 다르다.** 조인 키(`turn_id`·`seq`)만 있으면
DB 원문과 언제든 붙으므로 정보 이득도 0이다.

---

## 4. 계획 ② — 5분 조각 이어하기

### 4-1. 이어하기 계약 (서버 조사 결과)

```
보낼 것   start 프레임에 continues_call_id (str|int) 한 필드
          ⛔ 첫 조각엔 필드 자체를 안 넣는다 (null 은 의도가 흐려진다)
          ⛔ duration_min 금지 — ENV=test 라 override 가 먹어 서버 백스톱이 클라 손에 넘어간다

받을 것   call_started.call_id  ← 이어할 번호 (⛔ 프론트가 지금 버린다)
          GET /calls/{id}/resume-status  → ready · can_resume · fragment_count · max_fragments
          GET /calls/daily-status        → can_call_normal · max_fragments

거절하면  에러 프레임이 안 나간다. 조용히 새 통화로 폴백
          ⇒ "이어졌나"는 call_started.call_id == 내가 보낸 값 으로만 안다

TTL       RESUME_TTL_S(300) + CALL_FRAGMENT_S(360) = 660초
          300초에 끊으면 결제 왕복에 남는 창이 약 360초  ⚠
```

### 4-2. ⭐ 시간 누적 — 프론트가 든다

**근거**: 누적 시간은 **판정 축이 아니라 표시 축**이다. "더 이어갈 수 있나"의 진짜 판정은
서버가 **조각 수**(`max_fragments`)로 이미 소유한다. 앱이 죽으면 시트도 사라지고 TTL 도
6분이라 어차피 못 잇는다 ⇒ 프론트 보관의 약점이 이 흐름에선 안 드러난다.

```dart
// CallState 에 필드 1개 추가 — elapsedSec 의 의미는 바꾸지 않는다
final int carriedSec;                              // 앞선 조각들이 쌓은 초. 첫 조각 0
int get totalElapsedSec => carriedSec + elapsedSec;
```

- 화면 타이머 = `totalElapsedSec`
- **시트 판정도 `totalElapsedSec`** ⛔ 조각 경과를 넘기면 무료가 5분마다 무한히 이어간다
- ⛔ `_connect` 는 `state = CallState(...)` **생성자**를 쓴다(:1330) — `carriedSec` 을
  **인자로 받아 teardown 뒤에** 실어야 한다. 안 그러면 에러 없이 0부터 다시 센다

### 4-3. ⛔ Free → 구독 → 복귀가 지금은 **안 된다** (둘 다 고쳐야)

```
① call.dart:196   페이월 push 전에 hangUp()
                  → phase=ended → ref.listen 이 _goFinish 를 태워
                    pushReplacementNamed(callFinish) 로 CallScreen 을 교체해 버린다
                    (페이월 아래에서 조용히 일어난다)

② purchase_flow.dart:207   popUntil((r) => r.isFirst)
                  → 남아 있었어도 홈으로 날아간다
```

**고치는 법 (UI 신규 제작 0)**

```
(1) hangUp() 대신 CallPhase.segmentEnded (신규) — _goFinish 는 `ended` 만 보므로 안 걸린다
    소켓은 닫되 phase 를 보존한다 (_teardown(keepError:) 와 같은 형태의 플래그)

(2) returnTo 라우트 인자를 3파일에 통과
    call.dart:198 → pushNamed(paywallPro, arguments: (returnTo: Routes.call))
    paywall.dart:384-391 → purchase_flow.dart:45-50 파싱
    purchase_flow.dart:207 → returnTo == null ? popUntil(isFirst)
                                              : popUntil((r) => r.settings.name == returnTo)
    ⇒ returnTo 미지정(기존 모든 진입점)은 한 글자도 안 바뀐다

(3) 구독 성공 감지 — 별도 배선 불필요
    purchase_flow.dart:69 가 sessionEntitlementProvider 를 세우고 캐시를 invalidate
    → callQuotaProvider 가 자동으로 maxDurationSec:900 으로 바뀐다
    ⇒ 복귀 후 _showLimitSheet 를 다시 띄우기만 하면
      isCeiling(300)==false 라 **같은 코드가 자동으로 "이어하기" 시트가 된다**
```

### 4-4. 재연결 방식 — 두 경로

```
A 소프트(_swapSocket)   유료 check-in 경로. 오디오 파이프라인 유지, 소켓만 교체
   근거: 마이크 업링크가 프레임마다 `final ch = _channel;` 을 그때그때 읽는다(:1963)
        재생 큐·AEC·세션은 소켓과 무관
   비용: 소켓 RTT 만
   ⚠ R4 위험 — `_teardown` 이 소켓의 유일한 출구라는 규율에 두 번째 문이 생긴다
     게이트(phase==inCall && _channel!=null && !_starting) + 실패 시 hangUp() 폴백 필수

B 하드(hangUp → start(resume:))   무료→구독 복귀 경로 (이미 파이프라인을 내렸으므로)
   ⚠ 알려진 지뢰 2건이 코드에 기록돼 있다:
     :811-813 엔진 재init 레이스 · :1611-1617 `_needsStart` 스테일 → 2번째 통화 무음
   ⇒ 실기기에서 "구독 후 이어하기" 를 **최소 3회 연속** 눌러 봐야 한다. 에뮬로는 안 잡힌다
```

### 4-5. 목 → 실제 API

`callQuotaProvider` 가 지금 `call_quota_mock.dart` 다. 갈아끼우기 전까지 틀리는 것:

```
usedToday 항상 0        "오늘 통화 다 썼다"가 절대 재현 안 된다
유료 900초 상한          클라의 상상이다 — 서버는 조각당 360초 + max_fragments
                        ⇒ 지금 유료 15분은 어디에도 구현돼 있지 않다
subscriptions/status    서버보다 앞서 작성됨 ⇒ 유료 판정이 세션 엔타이틀먼트 하나에 의존
                        ⇒ 앱을 껐다 켜면 유료 상태가 날아간다
```
⛔ 폴백은 **반드시 제한 쪽**(로딩/실패 = 무료 300).

---

## 5. 작업 분해

### 계획 ① 계측 (서버 먼저, 앱 나중)

```
S1  protocol.py: ClientDiag 추가 + 유니온 등록 + ClientTiming 합류
S2  call_session.py: _handle_client_control elif 2줄 + _record_client_diag
S3  (권장) ServerPong.s · ServerCallStarted.diag · ServerCallStarted.call_id
S4  회귀: "미지 이벤트가 섞인 배치가 와도 통화가 안 죽는다" 1건 (R4 통화 회귀 필수)
F1  call_diag_event.dart (레코드·링버퍼·클래스 쿼터)  ← 신규
F2  call_diag_sink.dart (배치·상한·flush 스케줄·직렬화) ← 신규
F3  컨트롤러에 _diag.add(...) 호출 ~20곳. 로직 변경 0 (_log 는 지우지 말고 나란히)
F4  sync_avatar.dart 에 onDiag 콜백 1개 (UI 불변)
F5  ⛔ _log 의 kDebugMode 게이트를 계측에는 적용하지 않는다
```

### 계획 ② 이어하기 (0 이 선행)

```
0   서버 계약 확정 — call_started 에 call_id(+total_elapsed_sec) 실을 수 있나   ← 1~6 선행
1   CallState.carriedSec + totalElapsedSec + copyWith
2   start({resume}) → _connect(resume), teardown 뒤 대입, start 에 continues_call_id
3   CallPhase.segmentEnded + phase 보존 teardown
4   _swapSocket() 소프트 재연결 (유료 경로)
5   call.dart 배선 — 판정·타이머를 totalElapsedSec 로, 시트 1차버튼이 재개를 부르게
6   결제 퍼널 returnTo 통과 + _exitToRoot 조건화
7   목 → 실제 API (독립 병행 가능)
8   회귀 — 누적 300/600/900 에서 isCheckIn·isCeiling (현재 quota 테스트 0건)
S5  ⛔ 한도 게이트를 resume_call 뒤로 이동 (발견 5) — 한도를 켜기 전 필수
```

---

## 6. 검증

```
계획 ①
  조인 실증(핵심)  실기기 5분 통화 1건 → 서버 `전송 seq=N` 과 클라 mk_rx.seq 전량 대조
                  유실 0 · (mk_fire − mk_rx) 중앙값 ≈ cushion_ms 면 봉투 배관이 설계대로
  재생 대조        turn_done.rx_bytes vs 서버가 그 턴에 보낸 바이트. 차이 0 이 정상
  간섭 확인        DIAG=off vs full 각 1통 — PUMP:min · loop_lag.max · MIC 평균간격
                  ⛔ 나빠지면 계측이 실패한 것이다

계획 ②
  실기기           "구독 후 이어하기" 3회 연속 (하드 재시작 지뢰 2건이 여기 있다)
  누적             5분 → 10분 → 15분 이 화면에 맞게 뜨는가
  이어짐 확인       call_started.call_id == 내가 보낸 값
```

---

## 7. 사장님 결정이 필요한 것

```
1  call_started 에 call_id 를 실을까 (서버 3줄)
   ⇒ 권장. 조각 시작 0초부터 번호를 알아 어떤 종료 경로에서도 체인이 안 끊긴다

2  계측을 릴리스 빌드에서 켤까
   ⇒ 권장. 안 켜면 지금과 똑같이 아무것도 안 보인다. 원가는 사실상 0

3  무료 사용자가 구독 직후 이어할 때 — 남은 10분인가, 새로 15분인가
   ⇒ 지금 설계는 "남은 10분"(carriedSec 유지)

4  유료 15분 도달 시 문구
   ⚠ 현재 canContinue 하나로만 갈라서 `callFreeEndedTitle`("무료 통화가 끝났어요")이
     유료 15분 종료에도 뜬다 — 실제 버그다

5  종료 화면의 분석 대상
   ⚠ 체인 마지막 조각의 call_id 로 분석을 치는데 화면엔 누적 15분이 뜬다
     서버가 체인을 합쳐주지 않으면 사용자는 15분 분석을 기대하고 마지막 5분만 받는다
```

## 8. 미확인으로 남는 것

```
⚠ 조각 체인의 GET /calls/{id}/result 가 합쳐지는지 미확인
⚠ _swapSocket 이 R4 불변식(2펌프·barge-in·종료 규약)을 실제로 안 깨는지는 실기기 검증 필요
⚠ 재연결 중(0.5~3초) 표시할 자리가 통화 화면에 없다 — 헤더는 l10n.connected 고정
  없으면 "먹통인가?" 로 읽힌다
```
