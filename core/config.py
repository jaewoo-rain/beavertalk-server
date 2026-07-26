"""애플리케이션 설정 (pydantic-settings).

Spring 의 application.yml 대응. `.env` 파일에서 값을 읽어온다.

- DATABASE_URL_POOL   : 런타임용 6543 Transaction Pooler 연결 (pgbouncer)
- DATABASE_URL_DIRECT : Alembic 마이그레이션용 5432 Direct 연결
"""

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


settings = Settings()  # import 시점에 .env 로드