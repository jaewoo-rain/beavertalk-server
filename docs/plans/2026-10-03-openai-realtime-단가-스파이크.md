# OpenAI Realtime 단가 스파이크 — 15분 통화 $0.40 달성 가능성

작성 2026-10-03 · **1단계(공식 문서) + 2단계(실측) 완료**
실측 모델 `gpt-realtime-2.1-mini` · voice `marin` · 지시문 `normalcall_prompt.txt`
스크립트 `scripts/spike_openai_realtime_cost.py` (독립 실행, 앱 미통합, 기존 코드 무변경)
키는 `.env` 의 **`GPT_API_KEY`** 를 실행 시점에 `OPENAI_API_KEY` 로 매핑해 env 로만 주입
(⛔ 파일·로그·커밋에 값 없음)

---

## 0. 결론

| 질문 | 답 |
|---|---|
| 캐시가 오디오에 붙나 | **붙는다. 실측 확인** (12턴 누적 **80.4%**, 정상상태 **96%**) |
| 15분 환산 원가 (비버 45%) | **$0.2363** |
| 15분 환산 원가 (비버 65%) | **$0.3204** |
| 목표 $0.40 | **✓ 합격** (둘 다 달성) |
| Gemini 대비 | **3.7~4.1배 저렴** (45% 기준) |
| ⚠ 실제 블로커 | **가격이 아니라 TPM 40,000/분 한도** — org 티어 상향 필수 |

**합격.** 단 비용이 아닌 쪽에서 막는 게 둘 있다 — **TPM 한도**(§7)와 **Baba 음색 변경**(§8).

---

## 1. ⭐⭐ 캐시 적중률 — 실측

12턴, 학습자 발화를 실제 오디오(24kHz PCM16, 5초 조각)로 전송.
`set_face` 는 TPM 절약을 위해 off(툴콜은 §4 에서 따로 측정).

| 턴 | in_tok | in_text | **in_audio** | cached_total | cached_text | **cached_audio** | out_audio |
|---|---|---|---|---|---|---|---|
| 1 | 2,578 | 2,528 | 50 | 0 | 0 | **0** | 329 |
| 2 | 2,736 | 2,636 | 100 | 2,496 | 2,496 | **0** | 280 |
| 3 | 2,862 | 2,712 | 150 | 2,688 | 2,624 | **64** | 218 |
| 4 | 2,979 | 2,779 | 200 | 2,816 | 2,688 | **128** | 168 |
| 5 | 3,078 | 2,828 | 250 | 2,944 | 2,752 | **192** | 253 |
| 6 | 3,207 | 2,907 | 300 | 3,072 | 2,816 | **256** | 233 |
| 7 | 3,338 | 2,988 | 350 | 3,136 | 2,880 | **256** | 301 |
| 8 | 3,469 | 3,069 | 400 | 3,328 | 3,008 | **320** | 305 |
| 9 | 3,587 | 3,137 | 450 | 3,456 | 3,072 | **384** | 231 |
| 10 | 3,704 | 3,204 | 500 | 3,584 | 3,136 | **448** | 212 |
| 11 | 3,806 | 3,256 | 550 | 3,712 | 3,200 | **512** | 268 |
| 12 | 3,924 | 3,324 | 600 | 3,840 | 3,264 | **576** | 182 |

**적중률 (12턴 누적)**

| | 캐시/입력 | 비율 |
|---|---|---|
| **오디오 입력** | 3,136 / 3,900 | **80.4%** |
| 텍스트 입력 | 31,936 / 35,368 | **90.3%** |
| 전체 입력 | 35,072 / 39,268 | **89.3%** |

- **합격선 ≥80% 충족.** 그리고 이 80.4% 는 **워밍업이 섞인 12턴 평균**이다. 마지막 턴만 보면
  **576/600 = 96.0%** 다. 적중률은 턴이 쌓일수록 올라간다 — 이론값 (N−1)/(N+1):
  12턴 84.6% → 30턴 93.5% → 46턴 95.7%. **15분 통화(32~46턴)에서는 93~96% 대**다.
- **캐시는 1턴 지연 + 64토큰 블록 양자화**로 붙는다. 턴 1~2 의 오디오 캐시가 0 인 것은
  ①첫 턴은 캐시할 과거가 없고 ②오디오가 64토큰 블록을 못 채웠기 때문이다. 턴 3부터
  64, 128, 192 … 정확히 64의 배수로 증가한다.
- 텍스트 캐시도 같은 1턴 지연을 보인다(턴 1 = 0, 턴 2 = 2,496).

⇒ **§1 의 문서 판정(「오디오에 캐시가 붙는다」)이 실측으로 확정됐다.**

### ⭐ 실측이 뒤집은 구조 — 비버 음성은 오디오로 재청구되지 않는다
`in_audio` 가 **정확히 내가 보낸 학습자 오디오와 일치한다**(5초 조각당 +50토큰 = 10 tok/s).
비버 자신의 오디오 출력(`out_audio` 168~329토큰)은 **다음 턴 `in_audio` 에 전혀 안 들어간다.**
비버 발화는 **전사 «텍스트»** 로 컨텍스트에 들어온다(`in_text` 가 턴당 +72.4 증가).

⇒ 1차 추정($0.27)은 「비버 오디오도 오디오로 재청구된다」고 가정해 **과대추정**이었다.
실측 구조로 다시 계산하면 **$0.2363** 이다(§3).

---

## 2. 실측에서 얻은 구조 상수 (추정 대체)

| 항목 | 실측값 | 근거 |
|---|---|---|
| 지시문 토큰 | **~2,500 tok** (4,273자) | 턴1 `in_text` 2,528 |
| 입력 오디오 | **10 tok/초** (학습자 발화만) | 5초 조각당 정확히 +50 |
| 출력 오디오 | **20 tok/초** | 공식값과 일치 |
| 비버 1턴 길이 | **12.7초** | out_audio 평균 254tok ÷ 20 |
| 비버 발화 1초당 컨텍스트 텍스트 | **5.7 tok** | 턴당 +72.4 ÷ 12.7초 |
| 툴콜 2번째 Response 캐시율 | **98.8%** | 2,880 / 2,914 |

※ 1차 추정의 지시문 6,000토큰은 **틀렸다** — `wc -c` 가 바이트를 센 것을 글자로 오독했다
(한글 1자 = UTF-8 3바이트). 실제 4,273자 → 2,500토큰.

---

## 3. 15분 환산 원가 (실측 구조 기반)

`set_face` 매 턴 호출 + 입력 전사 ON(통화후 분석에 필요) 포함.

### 비버 발화 45% (405초) / 학습자 225초 / 32턴

| 항목 | 원가 | 비중 |
|---|---|---|
| **out_audio** | **$0.1620** | **68.6%** |
| in_audio | $0.0333 | 14.1% |
| tool_2nd_response | $0.0240 | 10.1% |
| in_text (지시문) | $0.0062 | 2.6% |
| out_text | $0.0055 | 2.3% |
| in_text (전사) | $0.0054 | 2.3% |
| **합계** | **$0.2363** | ($0.0158/분) |
| 캐시 OFF 였다면 | $1.0740 | **캐시가 78% 를 깎는다** |

### 비버 발화 65% (585초) / 학습자 180초 / 46턴

| 항목 | 원가 | 비중 |
|---|---|---|
| **out_audio** | **$0.2340** | **73.0%** |
| in_audio | $0.0304 | 9.5% |
| tool_2nd_response | $0.0314 | 9.8% |
| in_text (지시문) | $0.0083 | 2.6% |
| out_text | $0.0080 | 2.5% |
| in_text (전사) | $0.0084 | 2.6% |
| **합계** | **$0.3204** | ($0.0214/분) |
| 캐시 OFF 였다면 | $1.3454 | **캐시가 76% 를 깎는다** |

**둘 다 목표 $0.40 달성.** 발화 축소 결정이 없어도 65% 까지는 여유가 있다.

### 민감도 (45% 기준)
| 시나리오 | 15분 원가 |
|---|---|
| 기준 | $0.2363 |
| `set_face` 끄면 | $0.2123 |
| 입력 전사 끄면 | $0.2334 |
| 지시문 2배(5k tok) | $0.2478 |
| **비버가 100% 말함** | **$0.4222** ⚠ 초과 |

⇒ **out_audio(비버 발화 시간)가 유일한 실질 레버**다. 다른 건 다 잔돈이다.
비버 발화가 **75% 를 넘어가면** 목표를 위협한다(100% = $0.42).

### full(`gpt-realtime-2.1`) — 비용 이득 없음
비버 45% 기준 **$0.8248** ($0.0550/분) = Gemini($0.87~0.97) 동급. 원가 해법이 아니다.

### Gemini 대비
| | Gemini Live 3.1 | **2.1-mini (45%)** | 2.1-mini (65%) | 2.1 full |
|---|---|---|---|---|
| $/15분 | $0.87~0.97 (실측) | **$0.2363** | $0.3204 | $0.8248 |
| $/분 | $0.058~0.065 | **$0.0158** | $0.0214 | $0.0550 |
| 배수 | 1× | **3.7~4.1배 저렴** | 2.7~3.0배 | 1.1배 |

---

## 4. 툴콜 과금 — 2번 청구되지만 2번째는 98.8% 캐시

`set_face` 를 선언하고 호출시켜 측정. **Gemini 3.1 과 구조는 같고 비용은 다르다.**

| 턴 | Response | input | cached | 캐시율 | out_audio |
|---|---|---|---|---|---|
| 1 | main (툴콜만) | 2,883 | 0 | **0%** | 0 |
| 1 | after_tool | 2,914 | 2,880 | **98.8%** | 350 |
| 2 | main (툴콜만) | 3,010 | 2,880 | 95.7% | 0 |
| 2 | after_tool | 3,041 | 3,008 | 98.9% | 395 |
| 3 | main | 3,153 | 3,008 | 95.4% | 0 |
| 3 | after_tool | 3,184 | 3,136 | 98.5% | 197 |

- 툴콜이 있는 턴은 **Response 가 2개**가 되고 **각각 프롬프트 전체를 청구한다** —
  구조적으로 Gemini 3.1 의 「프롬프트 2배 청구」와 같다.
- **그런데 2번째는 98.8% 가 캐시**라 실제 추가 비용은 작다. 15분 통화 영향분
  **$0.0240 (전체의 10.1%)**.
- ⚠ **첫 턴만 예외**: turn 1 after_tool 이 캐시 0% 로 2,914 전액 재청구됐다
  (캐시 write 가 아직 안 내려앉음). 통화 1건당 1회뿐이라 영향 미미.
- 툴콜 Response 는 `out_audio` 가 0 이고 음성은 2번째 Response 에서 나온다 — 출력은
  2배가 되지 않는다.

---

## 5. 16kHz 입력 — **거부된다 (실측으로 문서 모순 해결)**

공식 문서 2곳이 모순했다. **가이드**([voice-websockets](https://developers.openai.com/api/docs/guides/voice-websockets?api=realtime))는
`{"type":"audio/pcm","rate":16000}` 을 지원 목록에 올리고, **API 레퍼런스**는
"Only a 24kHz sample rate is supported" 라고 한다. 실제로 16000 을 보냈다:

```
{"type": "invalid_request_error", "code": "integer_below_min_value",
 "message": "Invalid 'session.audio.input.format.rate': integer below minimum value.
             Expected a value >= 24000, but got 16000 instead.",
 "param": "session.audio.input.format.rate"}
```

⇒ **API 레퍼런스가 맞다. 16kHz 입력은 불가.** 우리 클라는 PCM16/16k 를 보내므로
**서버에서 24k 업샘플이 필수**다.

**업샘플 비용 실측** (`audioop.ratecv`, 20ms 프레임 스트리밍, 15분 분량):
- **133.5 ms CPU / 통화 1건** = 코어 점유율 **0.0148%**
- 프레임당 **3.0 µs** 추가 지연
⇒ **사실상 무비용.** 리스크 아님. (단 `audioop` 은 Python 3.13 에서 제거 예정 —
`scipy`/직접 선형보간으로 대체 필요.)

출력은 24k 그대로 나오므로 **우리 재생 파이프라인(PCM24k)과 그대로 맞는다.**
※ "One format applies to both input and output and cannot change during the session" —
24k 통일이 유일하게 가능한 선택이고, 그게 우리에게 유리하다.

---

## 6. 한국어 음성 품질 · 코드스위칭 — 쓸 수 있다, 일관성은 튜닝 필요

실측 전사(우리 실제 `normalcall_prompt.txt` 투입, voice `marin`). 공식 언급이 0건이라
이게 유일한 근거다.

**한국어: 자연스럽고 정확하다.** 문법·조사·어미 오류 없음. 반말체 페르소나도 살아 있다
("들렸어", "해볼래?", "네 발음은 계속 체크할 거야").

**코드스위칭: 우리 설계대로 동작한다.** 안내·설명 = 영어 / 타깃 문장 = 한국어:

> [2] Okay, you said, "Sorry, I didn't catch that." That means you didn't understand...
> In my example it was: "오늘은 여행 이야기 할까, 케이팝 이야기 할까?" which means "Today,
> shall we talk about travel or K-pop?"
> Now, your turn: 한국어로 "오늘은 여행 이야기 할까, 케팝 이야기 할까?"라고 말해보자.

> [4] Sure. The basic Korean is: "먹고 싶어요." It means "I want to eat."
> Now try saying it out loud: "먹고 싶어요."

공식 가이드가 **기본 정책으로는 언어 전환을 억제**하지만("Switch languages only when the
user explicitly asks…"), 같은 가이드가 **언어 튜터 code-switch 예시를 공인 패턴으로 제시**
한다("Use English when explaining grammar… Speak in French when conducting practice").
우리 지시문이 그 오버라이드로 먹혔다.

**⚠ 약점 2개 (튜닝 과제, 탈락 사유는 아님)**
1. **턴별 일관성이 흔들린다.** 어떤 턴은 전부 한국어(「오케이 Sam, 들렸어…」), 어떤 턴은
   전부 영어. 「한국어 10% + 모국어 90%」 비율이 엄격히 지켜지지 않는다.
2. **페르소나 드리프트.** 「까칠한 상남자 반말」로 시작해 평범한 영어 선생님 톤으로 샌다.
   (Gemini 에서도 겪은 문제 — 재접지로 대응하던 그 축이다.)
3. 「케이팝」을 「케팝」으로 쓴 오타성 산출 1건.

음색은 `marin` 으로 측정. **Baba 목소리와는 다르다**(§8).

---

## 7. ⚠⚠ 실제 블로커 — TPM 40,000/분 (가격이 아니다)

12턴 연속 실행 중 4턴째부터 전부 실패했다. 공식 에러:

```
status=failed {"type":"failed","error":{"type":"tokens","code":"rate_limit_exceeded",
 "message":"Rate limit reached for gpt-realtime-2.1-mini (for limit gpt-4o-mini-realtime)
            in organization ... on tokens per min (TPM):
            Limit 40000, Used 40000, Requested 3188. Please try again in 4..."}}
```

- **현재 org 한도: TPM 40,000** (버킷 이름은 `gpt-4o-mini-realtime`).
- ⭐ **캐시된 토큰도 TPM 에 계산된다.** 캐시는 *돈* 은 깎아주지만 *한도* 는 안 깎아준다.
- 턴당 청구 입력 ≈ 3.2k, `set_face` 로 Response 2개 → **턴당 ~6.4k**.
  ⇒ **분당 약 6턴**에서 막힌다.
- 우리 통화는 비버 1턴이 12.7초 = **분당 ~4.7턴**. 즉 **15분 통화 1건이 혼자서 한도의
  대부분을 먹고, 통화가 길어질수록(입력이 커질수록) 1건조차 못 버틴다.**
  턴 46 시점 입력 ~6k × 2 = 12k/턴 → 분당 3턴만 가능.
- **동시 통화는 현 한도로 불가능하다.**

⇒ **org 티어 상향(TPM 증설)이 이식의 선행 조건이다.** 이건 가격보다 먼저 풀어야 한다.
(이번 실측은 턴 사이 11초 페이싱으로 회피했다 — 실서비스에선 쓸 수 없는 수단이다.)

### 참고: 세션 한계는 넉넉하다
> "The maximum duration of a Realtime session is **60 minutes**."
> — [realtime-conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)

Gemini 는 연결이 ~10분에 선점돼 **15분 통화를 한 연결로 애초에 못 한다.** OpenAI 는 60분이라
재연결 없이 들어간다. 컨텍스트도 128k(실효 ~96k)인데 15분 통화는 **~4k 토큰**에서 끝나
(실측 턴12 = 3,924) **truncation 이 한 번도 안 걸린다 = 캐시가 안 깨진다.**

---

## 8. 남은 미지수 · 결정 필요

### 사장님 결정 필요
1. **⚠ TPM 티어 상향** (§7) — OpenAI 계정 한도 증설 요청. **이식의 선행 조건.**
2. **Baba 음색 변경 수용 여부** — voice 10종 고정
   (`alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar`, 공식 권장
   `marin`/`cedar`). Custom Voices 는 ①영업 승인 게이트 ②**지원 모델 목록에 mini 가 없다**
   ([custom-voices](https://developers.openai.com/api/docs/guides/custom-voices)).
   ⇒ **Baba 목소리는 바뀐다.**
3. **비버 발화 축소** — 65%($0.3204)까지는 불필요. **75% 를 넘기면 필요**(100% = $0.4222).

### 못 쟀거나 미확정
- **한국어 발음의 «실청» 평가** — 전사는 정확하지만 음성 자체의 억양·자연스러움은 사람이
  들어야 한다. 음성 파일 저장해 뒀다(§9).
- **실제 통화 지연(latency)** — 이번 실측은 대본 구동(수동 commit)이라 server VAD 기반
  실시간 턴테이킹 지연을 재지 않았다. **못 쟀다.**
- **barge-in·무음 넛지·종료 규약** 이식 가능성 — 이벤트 모델이 달라 재설계 필요.
  이번 스파이크 범위 밖.
- **장기 통화(30분+) 캐시 유지** — 15분까지만 검증. TTL 30분이라 통화 중엔 안전하나
  미실측.
- **`audioop` 대체** — Python 3.13 에서 제거 예정(실행 시 DeprecationWarning 확인).
- 공식 블로그 2건(openai.com/index/*)은 WebFetch 403 — 벤치마크 수치는 직접 확인 못 했다.

### 설계 제약 (이식 시 반드시)
1. **재접지·힌트·종료 시드를 `instructions` 교체로 넣지 마라.** 프리픽스가 바뀌어 캐시가
   전부 깨진다("changing these mid-session will reduce the cache rate"). **대화 끝에
   conversation item 으로 append** 해야 §3 원가가 성립한다.
2. **truncation 을 켜지 마라.** "Truncation busts the cache near the beginning of the
   conversation" — 128k 안에 다 들어가므로 필요도 없다.
3. **입력 24k 업샘플** 필수(§5, 무비용).

---

## 9. 산출물 · 재현

```bash
# 추정 모델 (키 불필요)
PYTHONIOENCODING=utf-8 conda run -n beavertalk-server \
  python scripts/spike_openai_realtime_cost.py

# 실측 (키는 .env 의 GPT_API_KEY 를 실행 시점에 매핑)
K="$(grep -m1 '^GPT_API_KEY=' .env | cut -d= -f2- | tr -d '"'\''\r' | xargs)"
OPENAI_API_KEY="$K" PYTHONIOENCODING=utf-8 conda run -n beavertalk-server \
  python scripts/spike_openai_realtime_cost.py --mode live \
    --turns 12 --no-tools --pace 11 --user-audio <wav> --out <dir>

# 16kHz 수용 probe
... --mode live --turns 2 --no-tools --pace 6 --probe16 --out <dir>
```

주요 옵션: `--turns` `--no-tools`(TPM 절약) `--pace`(TPM 회피 대기초) `--user-audio`
`--slice-s` `--probe16` `--rate` `--instructions` `--out`

실측 산출물(세션 scratchpad, 커밋 안 함): 턴별 usage `live_usage.json`,
전사 `transcripts.txt`, 비버 음성 `beaver_24000.wav`(실청용).

**총 실측 비용: $0.15 미만** (4세션 합산 — $0.018 + $0.032 + $0.0805 + $0.015).

---

## 부록 A. 공식 단가 (2026-10-03 확인)

| | gpt-realtime-2.1-mini | gpt-realtime-2.1 |
|---|---|---|
| Text 입력 | $0.60 | $4.00 |
| Text 캐시 입력 | $0.06 | $0.40 |
| Text 출력 | $2.40 | $24.00 |
| **Audio 입력** | **$10.00** | $32.00 |
| **Audio 캐시 입력** | **$0.30** | $0.40 |
| **Audio 출력** | **$20.00** | $64.00 |
| Image 입력 / 캐시 | $0.80 / $0.08 | — |
| 컨텍스트 / 최대출력 | 128,000 / 32,000 | 128,000 / 32,000 |

출처: [models/gpt-realtime-2.1-mini](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini) ·
[models/gpt-realtime-2.1](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)

⚠ `gpt-realtime` / `gpt-realtime-mini` 는 **deprecated (2027-01-20 종료)**, 권장 대체가
각각 `gpt-realtime-2.1` / `gpt-realtime-2.1-mini`
([deprecations](https://developers.openai.com/api/docs/deprecations)). 구 mini 는
컨텍스트도 32k 뿐이다.

## 부록 B. 토큰화율
- 입력 오디오 **1 토큰 / 100ms** (10 tok/s) — 실측 일치
- 출력 오디오 **1 토큰 / 50ms** (20 tok/s)
- 출처: [voice-latency-cost](https://developers.openai.com/api/docs/guides/voice-latency-cost?api=realtime)
- 비교: Gemini 오디오 **32 토큰/초** ([ai.google.dev/gemini-api/docs/tokens](https://ai.google.dev/gemini-api/docs/tokens))
  → 초당 환산 uncached 는 OpenAI($0.0001/초) ≈ Gemini($0.000096/초) **거의 동일**.
  차이는 전부 **재청구분을 캐시가 깎는 데서** 난다.
