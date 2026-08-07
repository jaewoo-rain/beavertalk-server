"""애플리케이션 설정 (pydantic-settings).

Spring 의 application.yml 대응. `.env` 파일에서 값을 읽어온다.

- DATABASE_URL_POOL   : 런타임용 6543 Transaction Pooler 연결 (pgbouncer)
- DATABASE_URL_DIRECT : Alembic 마이그레이션용 5432 Direct 연결
"""

from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_JWT_SECRET = "dev-secret-change-me-please-32bytes-minimum-0123456789"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # .env → .env.local 순서로 로드(뒤가 우선). .env.local(gitignore)이 있으면 그 값이
        # .env 를 오버라이드한다 — 로컬에서 도그푸딩 DB/Supabase 등으로 갈아끼울 때 사용.
        # 파일이 없으면 조용히 건너뛴다(없어도 무해).
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 런타임 연결(필수). 로컬은 이거 하나만 설정하면 된다.
    DATABASE_URL_POOL: str
    # 마이그레이션/관리용(선택). 안 주면 POOL 을 그대로 사용.
    # 운영에서 6543 풀러(POOL)와 5432 직결(DIRECT)을 분리할 때만 채운다.
    DATABASE_URL_DIRECT: str | None = None

    ENV: str = "dev"

    # 통화 대상 언어 기본값(멀티랭귀지). start.target_language 오버라이드가 없거나
    # 미지원 코드면 이 값으로 폴백. core.languages.DEFAULT_LANGUAGE 와 같은 값(ko).
    DEFAULT_TARGET_LANGUAGE: str = "ko"

    @property
    def direct_url(self) -> str:
        """마이그레이션용 URL. 미설정이면 런타임 URL 로 폴백."""
        return self.DATABASE_URL_DIRECT or self.DATABASE_URL_POOL

    # ── JWT 인증 ──
    # 운영에서는 반드시 강한 무작위 값으로 교체(.env). dev 기본값은 편의용.
    JWT_SECRET: str = _DEV_JWT_SECRET
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7일
    PASSWORD_RESET_EXPIRE_MINUTES: int = 30  # 비밀번호 재설정 토큰 만료

    # ── SpeechSuper 발음평가 ──
    # 미설정이면 core.speechsuper 가 결정적 스텁으로 폴백한다(앱은 그대로 동작).
    SPEECH_SUPER_APP_KEY: str | None = None
    SPEECH_SUPER_SECRET_KEY: str | None = None
    SPEECH_SUPER_CORETYPE: str = "sent.eval.kr"  # 한국어 문장 평가

    # ── 국적 분류 (외부 오디오 국적 추론 API) ──
    # 미설정이면 core.nationality 가 조용히 비활성(None 반환) — 통화·분석 무영향(R5).
    NATIONALITY_API_URL: str | None = None      # 예: http://<tailscale-host>:<port>
    NATIONALITY_API_TOP_K: int = 3              # predict?top_k= (상위 후보 수)
    NATIONALITY_API_TIMEOUT_S: float = 20.0     # httpx read/write 타임아웃(초)
    NATIONALITY_MIN_SPEECH_S: float = 10.0      # 이 길이 미만 user 발화는 호출 스킵(호출측 게이트)

    # ── 이메일 발송 (Resend) ──
    # 둘 다 있어야 실제 발송. 하나라도 없으면 core.email 이 콘솔 출력으로 폴백한다.
    RESEND_API_KEY: str | None = None
    MAIL_FROM: str | None = None  # 발신 주소 (예: onboarding@resend.dev)

    # ── 이메일 인증 코드 (회원가입 / 비밀번호 재설정 공용) ──
    EMAIL_CODE_LENGTH: int = 4          # 코드 자릿수
    EMAIL_CODE_EXPIRE_MINUTES: int = 30  # 코드 유효시간
    EMAIL_CODE_MAX_ATTEMPTS: int = 5     # 코드 입력 시도 제한
    EMAIL_CODE_RESEND_SECONDS: int = 60  # 재발송 최소 간격(레이트리밋)

    # ── 소셜 로그인 (Google) ──
    # 구글 ID 토큰 검증 시 허용할 audience(클라이언트 ID). 플랫폼별(Android/iOS/Web)로
    # 여러 개면 콤마로 구분해 넣는다. 미설정이면 google 검증은 500(서버 설정 오류).
    GOOGLE_CLIENT_ID: str | None = None

    # ── normalcall (Gemini Live 음성통화 + 통화후 분석 + TTS + Storage) ──
    # 미설정이면 어댑터들이 graceful 폴백(통화 불가/분석 스킵/스텁). 앱은 그대로 뜬다.
    GEMINI_API_KEY: str | None = None              # AI Studio (USE_VERTEX=false 일 때)
    USE_VERTEX: bool = False                        # True 면 Vertex AI 사용
    GCP_PROJECT: str | None = None                 # Vertex 프로젝트 ID
    GCP_LOCATION: str = "us-central1"              # Vertex 리전
    GOOGLE_APPLICATION_CREDENTIALS: str | None = None  # 서비스계정 키(JSON) 경로
    GEMINI_LIVE_MODEL: str = "gemini-live-2.5-flash-native-audio"  # 통화(실시간 음성)
    JUDGE_MODEL: str = "gemini-2.5-flash"          # 통화후 분석(generateContent)

    # Live 컨텍스트 압축(build_live_config). trigger 에 닿으면 target 만 남기고 오래된
    # 대화부터 버린다. 세션 수명(압축 無면 오디오 15분/연결 ~10분) 대비로 넣은 값이지만,
    # **통화 원가를 직접 결정하는 파라미터**이기도 하다 — Live 는 매 턴 컨텍스트 전체를
    # 입력으로 재처리하므로, 이 상한이 곧 턴당 입력 토큰의 상한이다.
    #
    # 실측(2026-08-02, call 880 / 5분 / 22턴): 입력 209,974 tok 중 오디오 성분 85%.
    # 통화 원가 ~$0.58 의 81%가 이 입력이다. 반면 5분 통화의 최대 컨텍스트는
    # 지시문 1,428 + 오디오 9,344 + 주입 ~300 ≈ 11,000 tok 이라 **16000 에 닿지 않는다**
    # — 즉 현재 값은 5분 통화에서 한 번도 발동하지 않는다.
    #
    # ⚠ 낮추면 비용은 줄지만 비버가 통화 초반을 실제로 잊는다("아까 그거 기억나?"가
    #   깨진다). 드리프트 완화를 위해 재접지를 넣었던 이력이 있으니, 값을 바꿀 때는
    #   반드시 실기기 통화 전사로 망각 여부를 확인할 것. env 로 뺀 이유가 그것이다 —
    #   재빌드 없이 gcloud run services update 로 바꿔가며 관측하라.
    LIVE_CTX_TRIGGER_TOKENS: int = 16000  # 압축 발동 임계
    LIVE_CTX_TARGET_TOKENS: int = 12000   # 압축 후 유지량(trigger 보다 작아야 한다)
    # 세션 재개(session_resumption). 15분 통화의 전제 — 압축은 **세션**(오디오 15분) 한계만
    # 풀고 **연결 수명(~10분)** 은 못 푼다. 연결을 이어붙이려면 서버가 주는 핸들이 필요하다.
    # ⚠ 이 플래그는 **핸들을 받아 로깅만** 한다(단계 0 계측). 재연결 자체는 아직 없다 —
    #   즉 켜도 통화 동작은 바뀌지 않는다. 스파이크에서 확인할 것: (1) native-audio 모델이
    #   핸들을 실제로 발급하는가 (2) resumable=False 가 얼마나 자주 오는가.
    #   안 나오면 재연결 설계 전체가 무효라, 큰 구현 전에 이걸로 먼저 잰다.
    # ⚠ transparent=True 는 **Vertex 전용**이다(AI Studio 경로에서 SDK 가 ValueError).
    #   USE_VERTEX 분기는 build_live_config 가 한다 — 안 그러면 api_key 폴백에서 연결
    #   자체가 터져 graceful degradation(R5)이 깨진다.
    LIVE_SESSION_RESUMPTION: bool = False  # 핸들 수집 활성(동작 변경 없음)
    # 일반 통화 길이(초)를 **전 회원에게 강제**하는 값. None(기본) 이면 강제하지 않고
    # 구독 플랜별 길이(call_service.CALL_DURATION_S_BY_PLAN — Free 5분 / Pro·Max 15분)가
    # 소스가 된다. prod 는 이 값을 주지 않는다(플랜이 결정해야 하므로).
    # ⚠ 이건 dev/demo 탈출구다: 구독 없는 개발 계정으로 15분 경로를 테스트해야 하는데,
    #   플랜만이 소스면 dev 에서 15분을 영영 못 밟는다. 900 을 주면 플랜 무관 15분.
    #   코드 기본값을 길게 박지 않는 이유는 그대로다 — 15분은 세션 재연결이 정상
    #   동작해야 성립하고, 재연결이 막히면 백스톱이 통화를 자른다.
    NORMAL_CALL_DURATION_S: Optional[float] = None
    # 통화 usage 시계열 상세 로그(원가 조사용). 기본 off = 통화 종료 시 요약 1줄만.
    # true 면 메시지별 (경과초, prompt, total) 시계열을 1줄 더 찍는다 — 압축이 실제로
    # 발동하는지(톱니 vs 단조증가)와 usage_metadata 가 증분인지 누적인지 판별하는 데 쓴다.
    # 조사 기간에만 gcloud run services update 로 켰다 끄는 값이라 env 로 뺐다.
    LIVE_USAGE_TRACE: bool = False
    # 표현 TTS = Google Cloud Text-to-Speech(Chirp3-HD, 다국어). Vertex(빌린 프로젝트)는 Cloud TTS 를
    # 못 켜므로, 우리 프로젝트(bt-dev-web-01) SA 키로 별도 호출한다. Cloud Run 은 /secrets 에 마운트.
    TTS_SA_KEY_FILE: str = "tts_key.json"          # bt-dev-web-01 서비스계정 키 경로(없으면 TTS 비활성)

    # ── 발음 챌린지 서버 STT (Google Cloud Speech-to-Text 스트리밍) ──
    # 키 없거나 STT_FAKE 면 core.stt 가 페이크 스트림으로 graceful(과금 0, 서버 정상 기동).
    STT_LANGUAGE: str = "ko-KR"                     # 인식 언어
    STT_MODEL: str = "latest_short"                 # 짧은 발화(단어) 최적. 빈 문자열이면 기본 모델
    STT_PHRASE_BOOST: float = 15.0                  # config.words(정답 단어) phrase hints 가중치
    STT_FAKE: bool = False                          # True 면 실제 STT 대신 페이크(테스트/크레덴셜 부재)
    STT_SA_KEY_FILE: str = ""                       # STT 전용 SA 키. 비우면 TTS_SA_KEY_FILE(bt-dev-web-01) 재사용

    # ── 캐스케이드 통화용 STT v2 (Speech-to-Text v2 스트리밍 + 음성활동 이벤트) ──
    # 발음챌린지의 v1 경로와 **나란히** 존재한다(v1 은 일부러 v1 — 건드리지 않는다).
    # v2 를 따로 쓰는 이유: v1 에는 음성활동 이벤트가 없어 턴 "시작"을 알 수 없다 = barge-in 불가.
    # 설계: docs/20260805_1720_캐스케이드-턴감지-최소루프-설계.md
    STT_V2_LANGUAGE: str = ""            # 비우면 STT_LANGUAGE 재사용
    STT_V2_MODEL: str = "long"           # 대화 길이 발화용(v1 의 latest_short 는 단어용)
    STT_V2_LOCATION: str = "global"      # global 이 아니면 리전 엔드포인트로 클라 생성
    STT_V2_PROJECT: str = ""             # 비우면 SA 키의 project_id
    STT_V2_RECOGNIZER: str = "_"         # 인라인 설정용 기본 recognizer
    STT_V2_STREAM_MAX_S: int = 240       # 선제 롤오버 시점(v2 스트림 하드 한도 5분보다 짧게)
    STT_V2_FAKE: bool = False            # True 면 실제 v2 대신 페이크(과금 0)
    # ⛔ voice_activity_timeout 은 **턴 감지 노브가 아니다.** proto 원문:
    #   "the server will automatically close the stream after the specified duration has
    #    elapsed after the last VOICE_ACTIVITY speech event has been sent."
    #   → 800ms 로 두면 사용자가 0.8초 쉴 때마다 **스트림이 통째로 닫힌다**. 턴 종료는
    #   아래 CASCADE_TURN_SILENCE_MS(서버 자체 타이머)가 판정한다. 이 두 값은 **스트림 보호
    #   상한**으로만 쓰고 기본은 미설정(0)이다. 0 초과로 줄 땐 문서상 범위 500ms~60s 를 지킬 것.
    STT_V2_VAD_START_GUARD_MS: int = 0   # 0=미설정. speech_start_timeout(스트림 보호)
    STT_V2_VAD_END_GUARD_MS: int = 0     # 0=미설정. speech_end_timeout(스트림 보호)

    # ── 캐스케이드 세션(턴 상태기계) ──
    # 턴 종료 = **서버 자체 타이머**. SPEECH_ACTIVITY_END(또는 마지막 인식 오디오) 이후
    # 이만큼 침묵이 이어지면 턴을 닫는다. 출발값 800ms = Live 기본값과 같은 값.
    CASCADE_TURN_SILENCE_MS: int = 800
    CASCADE_TURN_MAX_S: int = 60                 # END 가 영영 안 와도 턴을 강제 종료(안전망)
    # ⭐ 침묵 타이머의 **최소 대기**(2026-08-07 실통화 후 추가).
    #   타이머는 "이미 흘러간 침묵"을 오디오 시각으로 빼고 남은 만큼만 기다린다. 그 계산은
    #   파이프라인 지연 < 침묵 임계일 때만 성립하는데, 실측 지연이 810~914ms 로 임계(800ms)를
    #   **넘겼다.** 그러면 남은 대기가 0 이 되어 턴이 즉시 닫히고, 뒤늦게 도착한 최종 전사가
    #   **같은 발화로 턴을 하나 더 연다**(실통화 u2/u3·u16/u17…). 바닥을 둬서 최종 전사를
    #   기다릴 시간을 항상 남긴다.
    CASCADE_TURN_MIN_WAIT_MS: int = 250
    # 방금 닫은 턴의 **꼬리 전사**로 새 턴을 열지 않는 유예. 이 창 안에서 닫힌 턴의 끝보다
    # 앞선 오디오를 가리키는 최종 전사는 이미 낸 턴의 잔여물이다(유령 턴 차단).
    CASCADE_STALE_FINAL_MS: int = 1500
    CASCADE_ROLLOVER_BUFFER_MS: int = 3000       # 롤오버 갭 동안 보관할 오디오 상한
    # ── P1: 비버가 말한다(LLM → TTS) ──
    CASCADE_LLM_MODEL: str = "gemini-2.5-flash"  # 설계 §1-1(품질 비교 후 lite 전환 가능)
    # ⭐ 0 = 추론 끄기. 음성 대화에 사고 토큰은 대부분 불필요한데 **출력 단가로 과금되고
    #   첫 소리를 늦춘다** — 원가·체감속도 둘 다 손해다. None 이면 모델 기본값.
    CASCADE_LLM_THINKING_BUDGET: int | None = 0
    # ⛔ 캐스케이드 경로(WS + 데모 콘솔)를 여는 **전용 스위치**. 기본 False.
    #   ENV 게이트("prod 가 아니면 dev")에 기대지 않는 이유: 실서비스(app-api)의 ENV 가
    #   "prod" 가 아니라 **"test"** 라 그 게이트가 실서비스에서 열려 있다(2026-08-07 실측).
    #   깨진 게이트 위에 기능을 얹지 않는다 — 이 값이 True 인 곳에서만 열린다.
    #   ⚠ demo-api 에는 CASCADE_ENABLED=true 를 넣어야 사장님 데모가 산다(배포와 동시에).
    CASCADE_ENABLED: bool = False
    # ⭐ 선톡 — 비버가 먼저 인사한다(Live 와 같은 규약). 끄면 둘 다 서로 말하기를 기다린다.
    CASCADE_GREETING: bool = True
    # 이력 백스톱은 **글자 수**다(턴 수가 아니다 — 긴 발화 몇 개가 짧은 턴 12개보다 크다).
    # 15분 정상 통화(대략 1만 자)의 몇 배로 잡아 정상 통화는 절대 안 걸리게 하고, 병적으로
    # 긴 통화만 막는다. 넘으면 오래된 것부터 버리되 버린 사실을 로그로 남긴다.
    CASCADE_HISTORY_MAX_CHARS: int = 40000
    # ── TTS 엔진 A/B (사장님이 두 개를 번갈아 들으며 고르신다) ──
    # ⭐ **재빌드 없이 env 로 왔다갔다** 하는 게 요점이다. "chirp3-hd"(기본, 지금 동작) ↔
    #   "gemini-tts"(감정 지시 가능). 같은 통화 조건에서 양쪽 `첫소리=Xms` 를 뽑으면 지연
    #   비교가 실측으로 끝난다. 느리거나 이상하면 env 만 되돌린다.
    CASCADE_TTS_ENGINE: str = "chirp3-hd"
    # 속도가 목적이라 flash 계열부터. lite 가 더 빠를 수 있어 이것도 env 로 바꾼다.
    # ⭐ **Cloud TTS 의 model_name 문자열**이다(2026-08-07 공식 문서 확인). 유효값 4종:
    #     gemini-2.5-flash-tts / gemini-2.5-flash-lite-preview-tts /
    #     gemini-2.5-pro-tts / gemini-3.1-flash-tts-preview
    # ⚠ **Gemini API(ai.google.dev) 의 모델 ID 와 문자열이 다르다**(그쪽은
    #   gemini-2.5-flash-preview-tts 처럼 'preview' 위치가 다르다). 가격표를 그쪽 페이지에서
    #   읽어 왔다면 **이름을 그대로 가져다 쓰면 안 된다** — 우리는 Cloud TTS 를 호출한다.
    CASCADE_TTS_GEMINI_MODEL: str = "gemini-2.5-flash-tts"
    # 감정 지시(Style Instructions). ⚠ **짧게 유지해라** — 길어지면 지연 비교가 오염된다.
    # 사장님이 문구를 바꿔가며 들어보실 수 있게 env 로 뺐다.
    CASCADE_TTS_STYLE_PROMPT: str = "밝고 다정한 선생님 목소리로, 또박또박 조금 천천히."
    CASCADE_TTS_LANGUAGE: str = "ko"             # 비버가 말하는 언어(= 학습 대상 언어)
    CASCADE_TTS_VOICE: str = "Aoede"             # Chirp3-HD 음성명(Live 캐릭터 voice 와 같은 이름 체계)
    # 데모용 페르소나(캐스케이드는 아직 DB·캐릭터를 안 읽는다 — normalcall 과 같은 조립기를 쓴다)
    CASCADE_PERSONA_ROLE: str = "한국어를 가르치는 다정한 비버 선생님"
    CASCADE_PERSONA_PERSONALITY: str = "친근하고 밝다. 짧게 말하고 자주 되묻는다."
    CASCADE_PERSONA_LEVEL: str = "아주 쉬운 단어와 짧은 문장으로 천천히 말한다."
    CASCADE_PERSONA_LOCALE: str = "en"
    # ── 마이크 상시 개방 (barge-in 의 전제) ──
    # ⛔ 기본 OFF. 지금 클라의 '비버 발화 중 마이크 닫기' 게이팅이 **자기-대화 루프의 유일한
    #   방어선**이다. 안드로이드에서 플랫폼 AEC 가 사실상 안 걸리기 때문인데, 원인이 **세 곳**
    #   이다(2026-08-07 프론트 두 탭이 독립적으로 같은 결론):
    #     1. 재생 트랙이 USAGE_MEDIA / CONTENT_TYPE_MUSIC — 통화 경로 밖
    #     2. **녹음 소스가 AudioSource.DEFAULT** — startRecorder 가 audioSource 를 안 넘긴다
    #     3. **AudioManager.MODE_IN_COMMUNICATION 미설정** — android/app 전체에 호출 0건
    #   ⛔ 1번만 고치고 "AEC 켰다"고 판단하지 마라. 플랫폼 AEC 는 **통화 다운링크를 참조해
    #     업링크에서 빼는** 구조라, 재생만 옮기고 녹음이 DEFAULT 로 남으면 참조할 짝이 안
    #     생겨 효과가 0 이다. 셋은 한 묶음이다(프론트는 ANDROID_VOICE_AUDIO 플래그로 묶었다).
    #   실측: call_id=855 에서 게이팅이 켜져 있었는데도 타이밍 결함 하나로 **유저 턴의 절반이
    #   비버 대사**였다. 게이팅까지 빼면 스피커폰에서 무방비다.
    #   켜는 조건 2개가 모두 충족된 뒤에 켠다: (a) 위 **세 곳 전부** 정비 머지 (b) 에코 측정으로
    #   서버 2차 방어 파라미터 확정.
    # ⚠ 서버 상태기계는 **양쪽 모드를 모두 견딘다** — '비버 발화 중 입력이 온다/안 온다'를
    #   어느 쪽으로도 가정하지 않는다. 이 값은 barge-in 을 시도할지와 클라 통지에만 쓴다.
    CASCADE_MIC_ALWAYS_OPEN: bool = False
    # ── barge-in 에코 2차 방어 (AEC 가 부분적이라는 클라 조사 결론에 따른 필수 장치) ──
    # 기본값은 **보수적으로**(막는 쪽) 잡는다 — 클라 에코 측정 리그 실측이 나오면 그 값으로
    # 조인다. 전부 env 라 재빌드 없이 바뀐다.
    CASCADE_BARGEIN_CONFIRM: str = "transcript"  # 'immediate' | 'transcript'(세션값으로 덮임)
    CASCADE_BARGEIN_MIN_MS: int = 200
    # ⭐ **비버가 실제로 들리고 있을 때만** 끊는다(2026-08-07 45분 통화에서 나온 결함).
    #   사용자가 한 글자도 못 들었으면 끼어든 게 아니다 — 끊어봐야 멈출 소리가 없고(이득 0)
    #   준비한 대답만 사라진다(손실 큼). 관측: 취소 14건 중 7건이 '들린글자=0' 이었고 그 뒤가
    #   전부 빈 턴 → 침묵이었다. ⚠ 판정은 **오디오 시간**으로 한다 — 원장의 '들린 글자'는
    #   문장 단위라 2초를 들었어도 0 일 수 있다(그걸로 막으면 진짜 barge-in 을 막는다).
    CASCADE_BARGEIN_MIN_AUDIBLE_MS: int = 300
    # ③ 전사 확인 관문(bargein_confirm=="transcript"일 때). 잡음은 전사를 못 만든다.
    CASCADE_BARGEIN_MIN_CHARS: int = 2
    # 전사가 안 와도 **음성이 이 시간만큼 이어지면** 인정한다. 1.2초 연속 발성은 기침이 아니다.
    # STT 지연(실측 0.8~0.9초)으로 진짜 끼어들기가 영영 안 먹는 상황을 막는 구조적 폴백이다.
    CASCADE_BARGEIN_SUSTAIN_MS: int = 1200
    # 취소로 죽은 대답을 되살리는 유예. 이 안에서 사용자가 결국 아무 말도 안 했으면(빈 턴)
    # 하던 말을 이어서 한다 — 침묵으로 끝내지 않는다.
    CASCADE_RESUME_WINDOW_MS: int = 8000            # 비버 발화 중엔 이만큼 지속돼야 barge-in 인정
    CASCADE_BARGEIN_MIN_CHARS: int = 2           # transcript 확인 모드에서 요구할 최소 글자수
    # 비버 발화 구간 **에너지 임계 상향** — 잔여 에코는 대개 원음보다 작다. 0 = 비활성.
    CASCADE_BARGEIN_RMS: float = 0.05            # 0~1 정규화 RMS(보수적 = 높게 시작)
    CASCADE_ECHO_TAIL_MS: int = 300              # 마지막 TTS 바이트 이후 에코 경계 구간(P1)
    # ── 재생 진행도(이력 절단 근거) ──
    # ⚠ played_ms 는 **네이티브 카운터 값만** 신뢰한다(Android getPlaybackHeadPosition ±10~20ms).
    #   Dart 외삽값은 ±50~150ms 라 원장 절단의 '짧은 쪽 편향'을 무의미하게 만든다 → 버린다.
    CASCADE_TRUST_ESTIMATED_PROGRESS: bool = False
    CASCADE_CANCEL_STOP_MS: int = 120            # audio_cancel 수신 → 실제 무음까지 클라 지연(50~120ms)
    CASCADE_CLIENT_BUFFER_MS: int = 600          # progress 부재 시 서버 추정용 클라 버퍼(보수적=크게)
    # (P1) 비버 턴 페이서 — 클라가 250ms 공백(normalcall_controller.dart:1494)을 언더런으로
    # 오인하면 재생 쿠션이 상한까지 차오르고(1200ms — 동 파일 :455 의 _cushionMaxBytes)
    # 이후 모든 턴에 그 지연이 붙는다. 서버가 고정 간격(무음 패딩 포함)으로 흘려 와이어를
    # 굶기지 않는다. Cloud TTS 스트리밍 합성이 되므로 선행버퍼는 작게 잡는다.
    CASCADE_TTS_LEAD_MS: int = 200               # 송출 시작 전 확보할 합성 선행분

    # Supabase (인증 주체 = GoTrue). Storage 는 GCS 로 이전 — 아래 URL/KEY 는 auth 검증용.
    SUPABASE_URL: str | None = None
    SUPABASE_SERVICE_KEY: str | None = None
    # 오디오 저장(통화 원본/표현 TTS/연습 녹음) = GCS 단일 비공개 버킷. 미설정/자격증명 부재면
    # voice_url=None(graceful). 아래 두 상수는 이제 버킷명이 아니라 **버킷 내 폴더 prefix**.
    GCS_AUDIO_BUCKET: str = "beavertalk-app-audio"       # bt-dev-web-01, asia-northeast3, 비공개
    GCS_SIGNED_URL_PUBLIC_TTL: int = 604800              # public_url 대체 signed URL 만료(7일)
    SUPABASE_BUCKET_SAMPLES: str = "voice-samples"        # prefix: 캐릭터·TTS(장기 서명)
    SUPABASE_BUCKET_RECORDINGS: str = "voice-recordings"  # prefix: 통화·연습 녹음(단기 서명)

    # ── 예약전화 FCM 발송 ──
    # 서비스계정 미설정이면 core.fcm 이 graceful 비활성(등록/삭제 API 는 정상, 발송만 스킵).
    # JSON(문자열) 우선, 없으면 FILE(경로) 사용. 내부 디스패처는 시크릿 헤더로만 트리거.
    FIREBASE_PROJECT_ID: str | None = None
    FCM_SERVICE_ACCOUNT_FILE: str | None = None
    FCM_SERVICE_ACCOUNT_JSON: str | None = None
    # ── IAP(인앱결제) ────────────────────────────────────────────────── #
    # 영수증을 애플·구글에 **실제로 검증**할지. 자격증명(.p8 / 서비스계정)이 아직 없어
    # 지금은 전 환경 스텁이다 — 계약대로 응답하되 검증만 건너뛴다(프론트가 전 구간을
    # 돌려볼 수 있게). 자격증명이 들어오면 prod 부터 True 로 올린다.
    # ⛔ prod 에서 True 인데 키가 없으면 검증이 실패(503)한다 — 키 먼저, 스위치 나중.
    IAP_VERIFY_ENABLED: bool = False
    IAP_ALLOW_STUB: bool = True   # 스텁 허용(개발·QA). prod 전환 시 False 로 내린다

    INTERNAL_DISPATCH_SECRET: str | None = None  # 미설정이면 /internal/dispatch-calls 는 항상 403
    INTERNAL_DISPATCH_CATCHUP_MIN: int = 1        # 크론 지연 보정(과거 N분 버킷까지 재시도)

    # ── 예약전화 APNs VoIP 발송 (iOS) ──
    # 미설정이면 core.apns 가 graceful 비활성(등록/삭제·android 발송 정상, iOS 발송만 스킵).
    # 개인키는 PRIVATE_KEY(.p8 내용, Secret Manager) 우선, 없으면 PRIVATE_KEY_FILE(.p8 경로, 로컬).
    # FCM_SERVICE_ACCOUNT_JSON/_FILE 과 동일한 '내용 우선·파일 폴백' 규율.
    APNS_KEY_ID: str | None = None
    APNS_TEAM_ID: str | None = None              # 예: CTV7Z5BXL8
    APNS_BUNDLE_ID: str = "im.beavertalk.beavertalk"
    APNS_PRIVATE_KEY: str | None = None          # .p8 내용(Secret Manager 주입)
    APNS_PRIVATE_KEY_FILE: str | None = None     # .p8 경로(로컬 폴백 — fcm 패턴 미러)
    APNS_USE_SANDBOX: bool = False               # TestFlight/App Store = False(프로덕션)

    @property
    def google_client_ids(self) -> set[str]:
        """허용 audience 집합 (콤마 구분 파싱)."""
        if not self.GOOGLE_CLIENT_ID:
            return set()
        return {c.strip() for c in self.GOOGLE_CLIENT_ID.split(",") if c.strip()}

    @model_validator(mode="after")
    def _guard_prod_secret(self) -> "Settings":
        # 운영(prod)에서 기본 JWT 시크릿이면 기동 차단(시크릿 교체 누락 사고 방지)
        if self.ENV == "prod" and self.JWT_SECRET == _DEV_JWT_SECRET:
            raise ValueError("운영(ENV=prod)에서는 JWT_SECRET 을 반드시 교체해야 합니다.")
        return self

    @model_validator(mode="after")
    def _guard_live_ctx_window(self) -> "Settings":
        """압축 창 정합성 — 잘못된 조합은 조용히 이상하게 도니 기동 시 막는다.

        target >= trigger 면 압축이 아무것도 못 버리거나 매 턴 발동해 대화가 통째로
        날아간다. env 로 튜닝하는 값이라 오타 한 번이 통화 품질을 무너뜨릴 수 있어,
        런타임이 아니라 기동 시점에 잡는다.
        """
        if self.LIVE_CTX_TARGET_TOKENS >= self.LIVE_CTX_TRIGGER_TOKENS:
            raise ValueError(
                "LIVE_CTX_TARGET_TOKENS 는 LIVE_CTX_TRIGGER_TOKENS 보다 작아야 합니다 "
                f"(target={self.LIVE_CTX_TARGET_TOKENS}, "
                f"trigger={self.LIVE_CTX_TRIGGER_TOKENS})."
            )
        if self.LIVE_CTX_TARGET_TOKENS <= 0:
            raise ValueError("LIVE_CTX_TARGET_TOKENS 는 양수여야 합니다.")
        return self


settings = Settings()  # import 시점에 .env 로드