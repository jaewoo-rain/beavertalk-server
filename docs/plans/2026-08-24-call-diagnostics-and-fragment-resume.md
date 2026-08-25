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

---

# 부록 A — 계측 구현 결과 (2026-08-25, 완료)

⛔ **조각 이어하기(§4)는 이번 범위가 아니다**(사장님 지시: "조각 이어하기말고 통화 계측
로그만 빌드해줘"). 아래는 계측만이다.

## A-1. 계획과 달라진 것 셋 — 이유와 함께

| 계획 | 실제 | 왜 |
|---|---|---|
| 링버퍼 | **쿼터 절단**(`diagClassRank`) | 링은 **오래된 것부터** 버려 통화 **초반**을 지운다. 우리가 제일 자주 보는 것이 「붙자마자 이상하다」라 초반이 가장 값지다. 대신 롤업·영상 내부가 먼저 죽고 턴·마커·언더런·요약은 안 죽는다 |
| `summary`/`full` = 이름만 | **실제로 거른다**(`rank < 3` 차단) | 반쯤 만든 스위치는 없는 것보다 나쁘다 — 「낮췄다」고 믿으면서 그대로 보낸다 |
| 서버 기본 `summary` | **`full`** | 이 계기를 만든 이유가 「웃다가 갑자기 멈춘다」이고 그 증상은 전부 `vid_*` 다. summary 로 두면 정작 보려던 것이 안 온다. 조사 끝나면 내린다 |

## A-2. ⛔ 짓다가 찾은 「조용한 부재」 넷 — 전부 이번에 닫았다

계측을 붙이려고 배관을 따라가 보니, **계측 자체가 여러 겹으로 죽어 있었다.** 넷 다
에러를 내지 않고 정상처럼 보였다는 점이 같다.

```
① Live 응답시간이 한 번도 안 돌았다
   _recordResponseTime 의 원점 _userTurnEndForAudioMs 는 `user_turn_end` 프레임에서만
   채워지는데 ⛔ 그 프레임은 캐스케이드에만 있다. 라이브에선 영원히 null → 조용히 반환.
   ⇒ 로컬 VAD 의 마지막 유성 프레임을 원점으로 쓴다(localOrigin).

② 서버 ClientTiming 이 **지어낸 필드 이름**이었다
   first_audio_ms · measured 라는 없는 이름을 써서, 유니온에 넣어 살렸다고 생각한 값이
   pydantic extra=ignore 에 걸려 **한 번 더** 버려졌다. 서버 로그는 `first_audio=None` 을
   찍으며 정상처럼 보였다.
   ⇒ 이름의 주인은 이미 보내고 있는 클라와 ClientCascadeTiming 이다. 글자까지 맞췄다.
   ⇒ 회귀: test_live_and_cascade_timing_never_drift_apart (필드 집합 동일성)

③ ServerCallStarted.diag 를 만들어 놓고 **아무도 안 채웠다**
   "끄는 스위치는 서버에 있다"가 거짓이었다 — 계측이 통화를 방해할 때 탈출구가
   앱 재배포뿐이었다.  ⇒ settings.LIVE_DIAG_LEVEL 신설 + 회귀로 못박음

④ dropped 를 누계로 보내는데 서버는 배치마다 **더한다**
   손실 수가 부풀어 「손실 많음」 경고가 상시가 되고, 상시 경고는 진짜 손실을 덮는다.
   ⇒ 클라가 델타로 보낸다.
```

⚠ `call_started.call_id` 는 §7-1 의 「서버 3줄」이 아니었다 — **서버는 처음부터 싣고
있었고 클라가 읽지도 않고 버리고 있었다.** 클라 2줄로 끝났다.

## A-3. 앵커의 진실 — 원점이 다르면 같은 이름으로 보내지 않는다

```
audible_ms          서버가 「말 끝났다」고 알린 시각 → 첫 소리   (캐스케이드에만 존재)
speech_to_sound_ms  ⭐ 학습자가 입을 연 시각 → 첫 소리          (두 엔진 정의 동일)
```

라이브는 원점 ①이 없다. 그렇다고 로컬 VAD 값을 `audible_ms` 로 실으면 **서버는 그것을
모른 채** 자기 `첫소리` 에서 빼서 틀린 줄 모르는 숫자를 만든다 — 없는 것보다 나쁘다.
⇒ 라이브는 `audible_ms = -1`(못 쟀음), 잰 값은 `speech_to_sound_ms` 로 간다.
그게 **사장님이 실제로 기다리는 시간**이기도 하다.

## A-4. 마이크 앵커 오염도 같이 막았다

`_markVoicedIfLoud` 는 반이중 게이트 **앞**에서 돌고 있었다 = 비버가 말하는 동안 마이크로
새어 들어온 비버 목소리(AEC 누설)가 「학습자가 입을 열었다」로 기록될 수 있었다 — 응답시간
원점이 통째로 앞당겨진다. `gated:` 를 넘겨 **앵커는 열린 마이크에서만** 잡는다.
닫힌 쪽은 버리지 않고 `gated_loud` 로 센다 — 그 수가 크면 「비버가 말하는 동안 학습자가
말을 걸고 있었다」는 뜻이고, 「비버가 너무 길게 말한다」의 직접 증거다.

## A-5. 전송 규율 — 위험은 CPU 가 아니라 **순서**다

```
R-a  프레임 ≤ 2KB          넘으면 건수를 줄여 다시 만든다. 잘라낸 것은 다음 배치로(안 버린다)
R-b  _micGated == true 일 때만 전송   비버 발화중엔 업링크가 완전히 비어 있다
                                     ⇒ 마이크 프레임을 **단 하나도** 밀지 않는다
R-c  핫패스에서 인코딩 금지  직렬화는 이미 도는 5초 타이머(_startInflateLog)에 얹는다
예외 둘  버퍼 절반 초과 / 통화 종료(finish) — 그때는 마이크 창을 안 기다린다
```

⚠ 업링크 128kbps 실기기에서 4KB 프레임 하나 = **약 250ms 마이크 공백**. 서버 VAD 가 그
구멍을 「말이 끝났다」로 읽는다 — **계측이 재려던 지연을 계측이 만든다.**

## A-6. 영상 계측이 답할 질문

사장님 증상: 「웃다가 웃다가 반복 → 갑자기 멈춤 → 말할 차례에 듣는 영상」

```
vid_talk {on:false}   ⇒ _stopTalking 이 _emoOpacity 를 **페이드 없이 0** 으로 떨군다.
                        loop:true 인 감정 클립이 잘리는 그 순간이 「갑자기 멈춤」이다
vid_emo_drop          ⇒ ⛔ 앞 감정을 여는 중(_emoLoading)에 다음 마커가 오면 그냥 반환하고
                        **재시도 경로가 없다.** 그 감정은 영영 안 뜬다
mk_rx vs vid_emo      ⇒ 서버가 보냈는데 화면이 안 바뀌었나를 실기기에서 가른다
```

⛔ 셋 다 **고치지 않았다**(사장님 지시: 원인만). 시각만 남긴다 — 증상과 시각이 붙어야
처방을 고를 수 있다.

## A-7. 산출물

```
서버   protocol.py       ClientTiming 필드를 실제 계약으로 교정
       call_session.py   타이밍 로그 재작성 · call_started 에 diag 레벨 탑재
       core/config.py    LIVE_DIAG_LEVEL (off|summary|full)
       tests             +2 회귀(필드 드리프트 · 레벨 실제 전송)

프론트 domain/entities/call_diag_event.dart      쿼터 버퍼
       data/datasources/call_diag_sink.dart      배치·상한·전송 창·레벨
       presentation/normalcall_controller.dart   후크 ~14곳 · 라이브 원점 복구 · VAD 게이팅
       presentation/sync_avatar.dart             onDiag 콜백 1개 (UI 불변)
       screens/home/call.dart                    배선 1줄 (화면 불변)
       test/call_diag_sink_test.dart             10건

검증   flutter analyze 무결 · flutter test 464 통과
       서버 tests/test_normalcall_ws.py 148 통과
       서버 tests/ 전체 = 1375 통과 / 8 실패
         ⚠ 8건은 **전부 이 작업 전부터 깨져 있던 캐스케이드 전용 시험**이다
           (내 변경을 stash 하고 돌려 같은 결과를 확인했다). 그중 2~3건은 실제
           OpenAI STT 웹소켓을 물어 타임아웃하는 flaky 다 — 실행마다 수가 바뀐다.
         ⛔ 내가 깬 것은 1건뿐이었다: test_live_model_is_not_changed_by_cascade_work.
           `ServerCallStarted` 스냅샷에 `diag` 가 늘어난 것으로, 그 시험이 스스로
           "지우지 말고 기대값을 갱신하라(의도한 Live 변경이면)"고 적어 둔 대로 갱신했다.
```

⚠ **아직 실기기에서 한 번도 안 돌았다.** 배포 + 실통화 1건으로 `normalcall 계측:` 과
`normalcall 클라타이밍:` 두 줄이 실제로 오는지 확인해야 완료다.
