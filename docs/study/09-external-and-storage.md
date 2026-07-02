# 9장. 외부 연동과 스토리지 — graceful degradation

> 📘 **이 장을 읽고 나면**
> - 공개 버킷(voice-samples)과 비공개 버킷(voice-recordings)의 차이, 그리고 "DB 에는 object key 만 저장하고 URL 은 그때그때 조립한다"는 규약을 이해할 수 있어요.
> - Gemini/Vertex TTS/분석/SpeechSuper/Supabase 등 외부 서비스가 각각 실패했을 때 앱이 어떻게 버티는지 표로 파악할 수 있어요.
> - 이 프로젝트를 관통하는 **graceful degradation**(우아한 성능 저하) 패턴 — "키 없거나 실패하면 None/stub 을 돌려주고 앱은 계속 돈다" — 을 코드로 설명할 수 있어요.
> - 왜 Supabase Auth 만은 유일한 "하드 의존성" 인지 구분할 수 있어요.
> - lazy client 초기화 패턴이 무엇이고 왜 쓰는지 알 수 있어요.

---

## 9.1 왜 이런 장이 따로 필요한가

BeaverTalk 은 자기 혼자 다 못합니다. 음성 통화는 Google Gemini 에게, 발음 채점은 SpeechSuper 에게, 파일 저장은 Supabase 에게 맡깁니다. 이런 외부 서비스는 **언제든 없거나(키 미설정), 느리거나, 실패할 수 있습니다.**

Java 세계에서는 보통 이런 실패를 예외로 터뜨리고 `@ControllerAdvice` 로 500 을 내려주죠. 그런데 이 프로젝트의 철학은 다릅니다:

> **"핵심(인증) 하나만 빼면, 어떤 외부 서비스가 죽어도 앱은 계속 뜨고, 할 수 있는 일은 계속 한다."**

이걸 **graceful degradation**(우아한 성능 저하)이라고 부릅니다. 전등이 나가도 집이 무너지지 않고, 그냥 그 방만 어두워지는 것과 같아요.

이 장은 그 "전등들" 이 어떻게 배선돼 있는지, 그리고 하나 나갔을 때 무슨 일이 벌어지는지를 다룹니다.

---

## 9.2 스토리지 — 두 개의 버킷

### 왜 필요한가

통화 녹음, AI 음성(TTS), 복습 녹음 같은 **오디오 파일** 은 DB(PostgreSQL)에 넣기엔 너무 큽니다. 그래서 파일은 Supabase Storage(클라우드 파일 저장소)에 올리고, DB 에는 "그 파일이 어디 있는지" 만 적어둡니다.

### 비유

물품 보관소를 떠올리세요.

- **공개 보관함(voice-samples)** = 누구나 볼 수 있는 진열장. 캐릭터 미리듣기 목소리, 배운 표현의 TTS 음성처럼 "숨길 것 없는" 것들. 주소만 알면 바로 접근 → **public_url**.
- **비공개 보관함(voice-recordings)** = 잠긴 사물함. 사용자의 통화 원본, 연습 녹음처럼 개인적인 것들. 접근하려면 **1시간짜리 임시 열쇠** 가 필요 → **signed_url**(만료 3600초).

### 핵심 규약: DB 엔 key 만, URL 은 즉석 조립

DB 의 `voice_url` 컬럼에는 재생용 전체 URL 이 아니라 **object key**(버킷 안의 상대 경로)만 저장합니다. 재생이 필요할 때마다 그 key 로 URL 을 새로 만들어요.

왜 이렇게 할까요? signed_url 은 1시간 뒤 만료됩니다. 만약 DB 에 URL 을 통째로 저장해두면 1시간 뒤 그 URL 은 죽은 링크가 돼요. key 만 저장하면 필요할 때 **항상 새 URL** 을 뽑을 수 있습니다.

```
저장할 때:  파일 → storage.upload(bucket, path, ...) → object key 반환 → DB voice_url = key
읽을 때:    DB voice_url(key) → public_url(key)  또는  signed_url(key, 3600) → 재생 URL
```

### object key 패턴

key 는 호출부가 규칙에 맞게 만듭니다(storage.py 는 받은 path 를 그대로 저장/반환만 함).

| 용도 | key 패턴 | 버킷 | 만드는 곳 |
|---|---|---|---|
| 통화 원본 턴 | `calls/{member}/{call}/{turn:04d}_{role}.mp3` | recordings(비공개) | [normalcall_service.py:165](../../domains/learning/service/normalcall_service.py#L165) |
| 표현 TTS | `tts/{call}/{sentence}.mp3` | samples(공개) | [normalcall_service.py:367](../../domains/learning/service/normalcall_service.py#L367) |
| 복습 녹음 | `reviews/{member}/{sentence}/{uuid}.mp3` | recordings(비공개) | [review_service.py:78](../../domains/learning/service/review_service.py#L78) |

### 실제 코드

- 업로드(성공 시 key 반환, 실패/미설정 시 None): [core/storage.py:51](../../core/storage.py#L51)
- 공개 URL 조립: [core/storage.py:74](../../core/storage.py#L74)
- 서명 URL 조립(기본 1시간): [core/storage.py:88](../../core/storage.py#L88)
- 버킷 이름 설정값: [core/config.py:78](../../core/config.py#L78) (`voice-samples` 공개), [core/config.py:79](../../core/config.py#L79) (`voice-recordings` 비공개)

### 흔한 함정

`upload` 은 `upsert="true"` 로 같은 경로 덮어쓰기를 허용합니다([storage.py:65](../../core/storage.py#L65)). 그래서 재시도해도 파일이 중복 안 생겨요. 반대로 말하면 "같은 key 로 두 번 올리면 앞엣것을 덮어쓴다" 는 점을 알아두세요.

> 한 줄 요약: 공개(voice-samples/public_url)와 비공개(voice-recordings/signed_url 1h) 두 버킷을 쓰고, DB 엔 object key 만 저장해 재생 URL 은 매번 새로 조립합니다.

---

## 9.3 lazy client 초기화 패턴

### 왜 필요한가

Supabase 클라이언트를 만들려면 URL·키가 있어야 하고 네트워크 준비도 필요합니다. 앱 뜰 때 무조건 만들면, 키가 없는 개발 환경에서는 부팅 자체가 실패하겠죠. 그래서 **"처음 진짜 필요해질 때 한 번만 만든다"** — 이게 lazy init 입니다.

### 비유

카페의 원두 그라인더를 생각하세요. 손님이 커피를 처음 주문하기 전엔 안 켭니다. 첫 주문 때 켜서(초기화) 그다음부턴 계속 그걸 씁니다. 키가 없으면? "그라인더 없음(None)" 이라고 한 번만 알려주고, 이후엔 조용히 커피 없이 운영합니다.

### 코드 모양

```
_client = None
_client_ready = False        # "한 번 시도했나" 표시

def _get_client():
    if _client_ready:        # 이미 시도함 → 캐시된 결과(클라이언트 또는 None) 반환
        return _client
    _client_ready = True      # 이제 시도한다고 표시
    url, key = settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY
    if not url or not key:    # 키 없음 → None (경고 1회)
        return None
    try:
        _client = create_client(url, key)   # 진짜 생성
    except Exception:
        _client = None        # 실패해도 None
    return _client
```

`_client_ready` 플래그 덕분에 키가 없어도 경고는 **딱 한 번** 만 찍고, 이후 호출은 조용히 None 을 돌려줍니다. 실제 코드: [core/storage.py:28](../../core/storage.py#L28)

### 흔한 함정

이 클라이언트는 **service_role 키** 로 만듭니다(관리자 권한). 그래서 Storage 뿐 아니라 Auth 검증(9.6)도 **같은 클라이언트를 재사용** 해요. `supabase_auth.verify_token` 안에서 `storage._get_client()` 를 부르는 게 그 이유입니다([supabase_auth.py:32](../../core/supabase_auth.py#L32)). 즉 Storage 설정과 Auth 설정이 한 몸입니다.

> 한 줄 요약: 클라이언트는 처음 필요할 때 한 번만 만들고(lazy) 결과를 캐시하며, 키가 없으면 None 을 반환해 앱을 죽이지 않습니다.

---

## 9.4 graceful degradation 패턴 — 이 프로젝트의 핵심 철학

### 왜 필요한가

외부 서비스는 통제 밖입니다. 하나 실패했다고 전체가 500 이 나면 사용자 경험이 최악이죠. 그래서 이 프로젝트의 core 어댑터들은 **"예외를 던지는 대신 None 이나 stub 을 돌려준다"** 는 공통 규율을 지킵니다.

### 비유

식당에서 "오늘 새우가 없어요" 라고 손님을 쫓아내지 않고, "새우 대신 다른 걸로 드릴까요?" 하는 것과 같아요. 재료(외부 서비스) 하나 빠져도 손님은 밥을 먹고 갑니다.

### 각 서비스별 degradation 동작

| 무엇이 없거나 실패하면 | 어떻게 되나 | 실제 코드 |
|---|---|---|
| genai 클라이언트 없음(키 미설정) | `app.state.genai_client = None`. 통화 WS 는 `server_not_ready` 에러 후 close, 단건 TTS 엔드포인트는 503 | [main.py:109](../../main.py#L109), [ws_router.py:52](../../domains/learning/realtime/ws_router.py#L52), [sentence_service.py:67](../../domains/learning/service/sentence_service.py#L67) |
| Storage 미설정/실패 | `upload`/`public_url`/`signed_url` 이 None → `voice_url = None`(전사·텍스트는 저장됨, 오디오만 없음) | [storage.py:58](../../core/storage.py#L58) |
| SpeechSuper 키 없음/호출 실패 | 예외 대신 결정적 stub 채점 반환(가짜지만 항상 같은 점수) | [speechsuper.py:336](../../core/speechsuper.py#L336) |
| TTS 실패(client None/합성 실패) | `synthesize_korean` 이 None → 표현(Sentence)은 저장되되 음성 없음 | [tts.py:37](../../core/tts.py#L37) |
| ffmpeg 없음 | MP3 인코딩 None → WAV 로 폴백 | [audio.py:81](../../core/audio.py#L81) |
| 분석(gemini_analysis) 호출 실패 | `generate_structured` 이 None → 통화 status=failed(통화 자체엔 영향 없음) | [gemini_analysis.py:61](../../core/gemini_analysis.py#L61), [normalcall_service.py:346](../../domains/learning/service/normalcall_service.py#L346) |

### 패턴의 세 가지 모양

이 프로젝트의 degradation 은 크게 세 형태입니다.

1. **None 반환(대부분)** — Storage, TTS, 분석. 호출부는 "None 이면 건너뛴다".
2. **결정적 stub(SpeechSuper 만)** — 진짜처럼 생긴 가짜 결과를 만들어 반환. 그래서 키 없이도 채점 UI 전체를 테스트할 수 있음. 계약(반환 형태)은 실제 호출과 100% 동일.
3. **폴백 전환(ffmpeg)** — MP3 안 되면 WAV 로. 기능은 유지하되 형식만 낮춤.

### "결정적(deterministic)" 이 왜 중요한가

SpeechSuper stub 은 `score = 60 + (ord(글자) % 41)` 처럼 **글자의 유니코드 값** 으로 점수를 만듭니다([speechsuper.py:343](../../core/speechsuper.py#L343)). 무작위가 아니라 "같은 글자는 항상 같은 점수" 예요. 덕분에 테스트가 매번 같은 결과를 내서 안정적입니다. 무작위였다면 테스트가 들쭉날쭉했겠죠.

### 흔한 함정

degradation 이 "조용히" 일어나기 때문에, 개발 중 "왜 오디오가 안 나오지?" 할 때 진짜 원인은 대개 "키 미설정 → None" 입니다. 로그에 찍히는 `voice_url=None`, `genai 비활성`, `스텁 폴백` 같은 경고 한 줄이 단서예요. 예외가 안 터지니 로그를 봐야 합니다.

> 한 줄 요약: core 어댑터들은 실패 시 예외 대신 None(또는 결정적 stub, 또는 WAV 폴백)을 돌려줘서, 외부 서비스가 죽어도 앱은 할 수 있는 일을 계속합니다.

---

## 9.5 외부 서비스 상태표

각 서비스가 "실연동" 인지 "stub 존재" 인지, 실패 시 무엇이 되는지 정리합니다.

| 서비스 | 성격 | 실패/미설정 시 | 설정 위치 |
|---|---|---|---|
| Gemini Live(실시간 통화) | 실연동 | 통화 기능만 비활성(앱은 뜸) | [config.py:71](../../core/config.py#L71) |
| Vertex/AI Studio genai 클라이언트 | 실연동 | None → 통화·TTS 비활성 | [main.py:60](../../main.py#L60) |
| generateContent(통화후 분석) | 실연동 | None → 해당 통화 status=failed | [gemini_analysis.py:25](../../core/gemini_analysis.py#L25) |
| Vertex Gemini-TTS | 실연동 | None → 표현은 저장, 오디오만 없음 | [tts.py:30](../../core/tts.py#L30) |
| SpeechSuper(발음 채점) | 실연동 + **stub** | 결정적 stub 채점 | [speechsuper.py:68](../../core/speechsuper.py#L68) |
| Supabase Storage | 실연동 | None → voice_url=None | [storage.py:51](../../core/storage.py#L51) |
| **Supabase Auth** | 실연동 | **인증 자체 불가 → 401 (하드 의존성)** | [supabase_auth.py:28](../../core/supabase_auth.py#L28) |

### genai 클라이언트 만들기 — 두 갈래

`_create_genai_client` 는 설정에 따라 두 경로로 클라이언트를 만듭니다. 실제 코드: [main.py:60](../../main.py#L60)

```
USE_VERTEX == True  → 서비스계정 키(GOOGLE_APPLICATION_CREDENTIALS
                       → 없으면 프로젝트 루트 gcp_key.json 폴백)로 Vertex 클라이언트
USE_VERTEX == False → GEMINI_API_KEY 로 AI Studio 클라이언트
어느 쪽이든 키 없음/인증 실패/패키지 없음 → None (앱은 정상 부팅)
```

이 None 이 `app.state.genai_client` 에 담기고([main.py:109](../../main.py#L109)), 통화/TTS 를 쓰는 엔드포인트가 읽어서 각자 503/skip 으로 처리합니다. main.py 자체는 절대 안 죽어요.

> 한 줄 요약: 인증 빼고 모든 외부 서비스는 optional 이며, genai 클라이언트가 None 이면 통화·TTS 만 꺼지고 앱은 정상 부팅됩니다.

---

## 9.6 유일한 예외 — Supabase Auth 는 하드 의존성

### 왜 인증만 다른가

지금까지 본 서비스는 다 "없어도 대체(None/stub)" 가 가능했어요. 그런데 **인증은 대체할 게 없습니다.** "이 요청이 누구 것인지" 를 모르면, 그 사람의 통화·문장·복습에 접근시킬 수가 없어요. 신원을 확인 못하면 fall back 할 대상 자체가 존재하지 않는 겁니다.

### 비유

호텔에서 수건(TTS)이 없으면 그냥 안 주면 됩니다. 그런데 투숙객 신원 확인(Auth)이 안 되면? 방 열쇠를 줄 수가 없어요. "대충 아무 방이나 드릴게요" 는 불가능합니다.

### 어떻게 동작하나

프론트(Flutter/supabase-js)가 Supabase 로 로그인/가입을 끝내고 받은 access token(JWT)을 우리 API 에 `Bearer` 로 보냅니다. 우리는 그 토큰을 **Supabase 에 되물어(get_user)** 검증합니다. 실제 코드: [core/supabase_auth.py:28](../../core/supabase_auth.py#L28)

```
verify_token(token):
    client = storage._get_client()        # Auth 도 같은 service_role 클라이언트 재사용
    if client is None:                     # Supabase 미설정 → 인증 불가
        return None                        # → 호출부가 401
    resp = client.auth.get_user(token)     # 검증을 Supabase 에 위임
    return AuthUser(uid, email)            # 성공
```

여기서 client 가 None 이면(= Supabase 설정이 없으면) `verify_token` 이 None 을 돌려주고, 그러면 **아무도 인증할 수 없습니다.** 이게 "graceful 하게 저하될 수 없는" 유일한 지점이에요.

WS 통화에서도 마찬가지로, `verify_token` 이 None 이면 `accept()` 없이 1008 로 끊습니다([ws_router.py:45](../../domains/learning/realtime/ws_router.py#L45)).

### 인증과 Storage 가 한 몸인 이유

`verify_token` 이 `storage._get_client()` 를 재사용하므로([supabase_auth.py:32](../../core/supabase_auth.py#L32)), 둘 다 **같은 `SUPABASE_URL` + `SUPABASE_SERVICE_KEY`** 에 의존합니다. 즉 Storage 의 "파일 업로드" 기능은 없어도 되지만, 그 밑의 Supabase 클라이언트는 Auth 가 반드시 필요로 합니다. Storage 는 optional, 그 클라이언트가 떠받치는 Auth 는 mandatory 인 미묘한 관계예요.

(참고로 회원 탈퇴 시엔 `delete_auth_user` 로 Supabase 의 auth.users 행까지 지워야 합니다. 안 지우면 남은 토큰으로 요청이 오면 member 가 되살아나요. 실제 코드: [supabase_auth.py:47](../../core/supabase_auth.py#L47))

### 흔한 함정

`config.py` 를 보면 JWT_SECRET/JWT_ALGORITHM 같은 **자체 JWT** 설정이 남아 있습니다([config.py:37](../../core/config.py#L37)). 이건 자체 인증을 하던 시절의 잔재예요. 지금 실제 인증은 위처럼 Supabase 에 위임하므로 헷갈리지 마세요(3장에서 자세히 다룹니다).

> 한 줄 요약: 다른 외부 서비스는 다 optional 이지만 Supabase Auth 는 신원 해석의 유일한 통로라 대체 불가 — 미설정 시 모든 보호 엔드포인트가 401 이 됩니다.

---

## 9.7 관련 파일 지도

| 역할 | 파일 |
|---|---|
| Storage 업로드/URL 조립 + lazy client | [core/storage.py](../../core/storage.py) |
| 발음 채점 + 결정적 stub | [core/speechsuper.py](../../core/speechsuper.py) |
| 표현 TTS(실패 시 None) | [core/tts.py](../../core/tts.py) |
| 오디오 포맷/변환(MP3→WAV 폴백) | [core/audio.py](../../core/audio.py) |
| 외부 서비스 설정값 | [core/config.py](../../core/config.py) |
| genai 클라이언트 생성(graceful) | [main.py](../../main.py) |
| Supabase Auth(하드 의존성) | [core/supabase_auth.py](../../core/supabase_auth.py) |

---

## ✍️ 스스로 점검

1. DB 의 `voice_url` 컬럼에 재생용 전체 URL 이 아니라 object key 만 저장하는 이유는 무엇인가요? 특히 비공개 버킷의 signed_url 특성과 연결해서 설명해 보세요.
2. SpeechSuper 는 다른 외부 서비스와 달리 실패 시 None 이 아니라 "결정적 stub" 을 돌려줍니다. 이 stub 이 "결정적(무작위 아님)" 이어야 하는 이유, 그리고 stub 의 반환 형태가 실제 API 응답과 같아야 하는 이유는 각각 무엇인가요?
3. genai 클라이언트가 없으면(None) 통화 기능만 꺼지고 앱은 뜨는데, Supabase 가 없으면 앱은 떠도 사실상 못 씁니다. 이 차이가 나는 근본 이유를 "무엇으로 대체(fall back)할 수 있는가" 관점에서 설명해 보세요.

⟵ [이전: 실시간 한국어 회화 통화](08-learning-realtime.md) ・ [📚 목차](README.md) ⟶
