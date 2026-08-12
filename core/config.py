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
    # ── STT 엔진 선택 (2026-08-10) ──
    # ⭐ `openai` 는 code-switching 실측에서 **6/6** 을 맞춘 유일한 후보다(구글은 1~2/6).
    #   ⭐ **기본이 openai 다**(2026-08-10 사장님 지시). 실측이 근거다 — 6개 언어쌍 같은
    #     오디오·같은 실시간 경로에서 OpenAI **6/6**, Google 1~2/6, ElevenLabs 2/6.
    #   ⚠ 키가 없거나 연결이 실패하면 google 로 폴백하고 WARNING 을 남긴다(R5). 조용한 폴백
    #     금지 — 어느 엔진이 실제로 돌았는지 로그와 **원가 벤더**에 남아야 한다.
    #     ⇒ 폴백은 **안전망으로 그대로 산다.** 키가 빠지면 통화가 죽는 게 아니라 구글로 돈다.
    CASCADE_STT_ENGINE: str = "openai"        # 'openai' | 'google'
    OPENAI_STT_MODEL: str = "gpt-4o-mini-transcribe"   # $0.003/분(구글의 1/5.3)
    # ⭐ 통화를 끊기 전에 흘릴 무음 길이. **없으면 마지막 발화가 사라진다**(2026-08-10 실측:
    #   그냥 끊으면 전사 2건, 꼬리 무음 1.5초를 붙이면 3건째가 온다 — server VAD 가 발화 끝을
    #   못 봐서 마지막 구간을 커밋하지 않는다). 통화 중에는 마이크가 상시 열려 문제가 없다.
    OPENAI_STT_TAIL_SILENCE_MS: int = 1500
    # ⚠ 이름이 관례와 다르다(`OPENAI_API_KEY` 가 아니다) — .env 에 이렇게 들어 있다.
    GPT_API_KEY: str = ""
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
    # ⭐⭐ **벤더 VAD 가 쪼갠 한 발화를 도로 잇는 간격**(2026-08-12).
    #   실측(내가 실제 벤더에 붙여 이벤트 전량을 받음): 한영 혼합 한 문장이 **두 item** 으로
    #   갈렸다 — item1 audio 52~1728ms 'Study Korean Today.' / item2 1812~3808ms
    #   '영 한국어 공부하자.' → **오디오 간격 84ms**. 실통화 u1/u2 와 같은 모양이다.
    #   그 결과 비버가 첫 조각에 대답을 만들다 **버리고** 두 번째에 다시 만든다("응답이 느리다").
    #   ⚠ 값 300 의 근거 — 실측 갈림 84ms 의 3.5배이고, **사람이 일부러 두는 쉼(≥500ms)**
    #     보다는 확실히 작다. 우리 턴 침묵 임계(800ms)의 절반 아래라 진짜 두 번째 발화를
    #     삼키지 않는다. 0 이면 잇지 않는다(예전 동작).
    CASCADE_SPEECH_MERGE_GAP_MS: int = 300
    CASCADE_TURN_SILENCE_MS: int = 800
    # ⭐ **전사 정지 기준**(2026-08-08). 글자가 한 번 나온 뒤부터는 VAD 가 아니라 **전사가
    #   멎었는지**로 턴을 닫는다 — 차·카페에서는 VAD 가 영영 조용해지지 않기 때문이다.
    #   왜 800ms(VAD 기준)보다 큰가: 이 경로는 VAD 의 "아직 발성 중" 보호막을 뗀 것이라
    #   **생각하느라 멈추면 잘린다.** 외국인 학습자는 한국어 문장을 만드느라 자주 멈추고,
    #   우리 사용자층에서 특히 아픈 자리다. 실측 파이프라인 지연이 723~914ms 라 그보다
    #   확실히 큰 값이어야 "지연 때문에 잘리는" 일이 안 생긴다. 1.5초로 잡되 env 로 조절한다
    #   (지금은 최악이 60초라, 1.5초여도 압도적 개선이다).
    CASCADE_TURN_TRANSCRIPT_SILENCE_MS: int = 1500
    # 안전망. ⚠ 전사 기준 판정이 대개 먼저 닫지만, **글자가 한 번도 안 나온 잡음 전용 턴**은
    #   여기까지 열려 있다(전사가 없으니 정지 판정 대상이 아니다). LLM 은 안 부르므로 원가는
    #   없고 상태만 오래 굳는다 — 그래서 60초는 과하다.
    #   ⛔ 그렇다고 짧게 깎으면 **진짜로 길게 말하는 학습자를 자른다**(어학 앱이다).
    #   30초는 한 사람이 쉬지 않고 말하는 상한으로 넉넉하고, 잡음 턴은 그 안에 정리된다.
    CASCADE_TURN_MAX_S: int = 30                 # END 가 영영 안 와도 턴을 강제 종료(안전망)
    # ⭐ **STT 가 통째로 조용해지면 이만큼 뒤에 턴을 닫는다**(2026-08-10 실통화).
    #   그날 STT 는 `speech_begin` **하나만** 보내고 전사도 speech_end 도 안 보냈다. 그러면
    #   침묵 타이머(speech_end 가 걸어야 한다)도, 전사 정지 타이머(전사가 걸어야 한다)도
    #   **아무도 안 걸린다** — 남는 건 30초 상한뿐이라 화면은 "내 턴"인데 30초를 굳었고,
    #   사장님은 통화를 끊으셨다("내 턴이라면서 시작 안 해").
    #   ⛔ 그래서 턴은 **열리는 순간부터** 마감 시계를 갖는다. STT 이벤트가 하나라도 오면
    #     그때마다 갱신되므로(=말하는 중엔 계속 미뤄진다) 정상 대화에는 닿지 않는다.
    #   5초 근거: 실측 파이프라인 지연 최댓값 914ms 의 5배 여유 — 정상 경로는 못 닿는다.
    #     그러면서도 "몇 초 안에 닫힌다"는 조건을 만족한다(30초는 통화가 죽은 것이다).
    #   ⚠ 빈 턴으로 닫혀도 손해가 없다: LLM 을 안 부르고(원가 0), 끊겼던 대답은
    #     `_resume_interrupted` 가 이어 준다.
    CASCADE_TURN_IDLE_S: float = 5.0
    # ⭐⭐ **세션 절대 백스톱**(2026-08-11 승인). Live 불변식(540초)의 캐스케이드 대응물이다.
    #   왜 두나: 턴 시계 **넷이 전부 "이벤트가 정상적으로 흐른다"를 전제**한다. 그 전제가 깨지면
    #     (펌프 정지·반열림 소켓) **아무 시계도 안 걸리고 세션이 무기한 산다.** STT 가 통화
    #     원가의 53% 이고 마이크는 상시개방이라, 끝나지 않는 세션 하나가 계속 과금된다.
    #   ⛔ 오늘 하루에만 그 계열 결함이 **다섯 건**이었다(30초 굳음 · CANCELLING 미해제 ·
    #     reason=max 오표기 · 이어가기 배수 누락 · 두 판정 사이 낙하).
    #     이건 **"못 본 게 또 있다"는 전제의 마지막 방어선**이다.
    #   1200초 근거: 제품 상한이 15분(900초) 통화다 — 개시·정리 여유를 더했다.
    #     ⚠ Live 의 540초를 그대로 못 쓴다. 그건 **연결 ~10분 한계**에 맞춘 값인데 캐스케이드엔
    #       그 한계가 없다(OpenAI Realtime 세션 상한은 60분 — 1차 자료 확인).
    #   ⭐ 정상 통화는 여기 **절대 안 닿는다.** 닿았다면 그 자체가 결함 신호다(로그로 남는다).
    CASCADE_SESSION_MAX_S: float = 1200.0
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
    # ⭐⭐ **대답 길이 상한**(2026-08-12). 프롬프트 문구가 아니라 **호출 파라미터**다.
    #   왜: 실통화(00146)에서 한 대답이 **18.7초(196자)** 였고, 그동안 사장님 발화가
    #     대기열에서 **14.2초** 기다렸다. 어학 대화에서 선생님이 19초를 혼자 말하는 것
    #     자체가 제품 문제다.
    #   값 40 의 근거 — 같은 모델·같은 대본으로 실측(**꼬리를 버린 뒤 실제 발화 길이**):
    #       상한 없음 평균 15.4초(최악 19.2)  ·  36 → 8.2초  ·  **40 → 9.1초**
    #       44 → 11.5초(최악 15.0)  ·  64 → 12.6초(최악 19.0)
    #     ⇒ 40 이 9초대로 들어오면서 문장 3~5개(설명+예시+질문)를 유지한다.
    #   ⚠ **너무 짧아도 안 된다**(사장님). 40 에서도 8회 중 1회는 문장 1개(5.0초)로 얇아졌다 —
    #     얇다고 느껴지면 44~52 로 올려라. 0 이면 상한 없음(벤더 기본값).
    #   ⚠ 토큰↔글자 비는 **0.34~0.45 토큰per자**로 실측했다(count_tokens 로 대조 확인).
    #     한국어·영어 비율에 따라 흔들리므로 글자 수로 환산해 쓰지 마라.
    CASCADE_LLM_MAX_OUTPUT_TOKENS: int = 40
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
    # ── TTS 엔진 ──
    # ⭐⭐ **사장님 결정(2026-08-12): Gemini-TTS 로 간다**("지금 좋아 잘돼"). 화면에서 골라
    #   들으시던 값을 **서버 기본값**으로 올린다 — 앱이 붙으면 이 값이 곧 실서비스 소리다.
    #   ⚠ 대가를 알고 쓴다: Gemini-TTS 는 **쿼터가 있는 유일한 엔진**이고, 넘치면 그 통화는
    #     Chirp 으로 폴백한다 = **통화 중에 목소리가 바뀐다**(사장님은 소리로만 아신다).
    #     그래서 폴백 횟수를 `cascade usage:` 요약에 남긴다(tts_fallbacks).
    #   ⚠ 실측(7일 로그): 분당 요청 최대 **42회**(08-10), 최근 통화 **12회/분**. 문서상 상한은
    #     분당 10회인데 그 두 경우 모두 429 가 **안** 났다 — 실효 상한이 문서와 다르다는 뜻이다.
    #     실제 폴백은 08-07(묶음 개선 전 무더기)과 08-10 08:34 뿐이고 그 뒤로는 없다.
    #   ⛔ 그래도 위험은 실재한다. 되돌리려면 env 로 "chirp3-hd" 를 넣으면 끝이다.
    CASCADE_TTS_ENGINE: str = "gemini-tts"
    # 속도가 목적이라 flash 계열부터. lite 가 더 빠를 수 있어 이것도 env 로 바꾼다.
    # ⭐ **Cloud TTS 의 model_name 문자열**이다(2026-08-07 공식 문서 확인). 유효값 4종:
    #     gemini-2.5-flash-tts / gemini-2.5-flash-lite-preview-tts /
    #     gemini-2.5-pro-tts / gemini-3.1-flash-tts-preview
    # ⚠ **Gemini API(ai.google.dev) 의 모델 ID 와 문자열이 다르다**(그쪽은
    #   gemini-2.5-flash-preview-tts 처럼 'preview' 위치가 다르다). 가격표를 그쪽 페이지에서
    #   읽어 왔다면 **이름을 그대로 가져다 쓰면 안 된다** — 우리는 Cloud TTS 를 호출한다.
    CASCADE_TTS_GEMINI_MODEL: str = "gemini-2.5-flash-tts"
    # 감정 지시(Style Instructions). ⚠ **짧게 유지해라** — 길어지면 지연 비교가 오염된다.
    # ⛔ **속도 얘기를 여기 쓰지 마라.** 속도는 아래 speaking_rate(파라미터)가 맡는다. 프롬프트로
    #   "천천히/자연스럽게"를 부탁하면 둘이 싸우고, 부탁 쪽은 실측 편차가 1.5배까지 났다
    #   (2.4 ~ 10.0 자/초). 프롬프트는 **감정·톤만** 맡는다.
    #   ⭐ **"또박또박"도 뺐다**(2026-08-10). 예전에 "그건 속도가 아니라 또렷함"이라며 남겼는데,
    #     실측이 그 구분을 지지하지 않았다: 같은 문장을 Gemini-TTS 가 **한국어 1.3자per초**로
    #     읽는다(Chirp 은 같은 언어에서 4.5~5.6). 스타일 프롬프트는 **Gemini 에만** 전달되고
    #     ⚠ **그 1.3 은 이 커밋 시점의 값이다.** 낱말을 빼고 구간 침묵까지 잘라낸 뒤 실측은
    #       **6.2~7.4자per초**다(목표 = Live 실측 7.7). 지금 값으로 읽지 마라.
    #     (Chirp 가지는 빈 문자열을 넘긴다) 두 엔진의 속도 차가 정확히 거기서 갈린다.
    #     그리고 또렷함은 프롬프트 없이도 확보된다 — **프롬프트를 하나도 안 받는 Chirp**
    #     경로에서 "뭉개져서 못 알아듣겠다"는 말이 나온 적이 없다.
    #   ⛔ **normalcall 의 교수법 문장("천천히 또박또박 들려주고 2번 따라 말하게")과 혼동하지
    #     마라.** 그건 LLM 에게 주는 **가르치는 방식**이고 여기는 TTS 목소리 스타일이다.
    #     거기를 같이 지우면 학습 설계가 무너진다(에코 결함도 그 문장이 근거였다).
    CASCADE_TTS_STYLE_PROMPT: str = "밝고 다정한 선생님 목소리로."
    # ⭐ 말하는 속도. proto 원문 범위 [0.25, 2.0], **1.0 = 그 목소리의 정상 속도**.
    #   1.0 이면 필드를 아예 안 넘긴다 = 지금 동작 그대로(배포만으로는 아무것도 안 바뀐다).
    #   엔진 공통 필드라 Chirp3-HD 경로에도 같이 걸린다.
    #   ⭐ 원가와 같은 방향이다 — Gemini-TTS 는 **출력 오디오 초**로 과금되므로 빨리 읽으면
    #     오디오가 짧아져 그만큼 싸진다.
    CASCADE_TTS_SPEAKING_RATE: float = 1.0
    # ⭐ **Gemini 전용 배속.** 엔진 공통 값을 올리면 Chirp 까지 빨라지는데, Chirp 은 이미
    #   14~22자per초로 충분하다(사장님: "빠르게 잘 나온다"). 올릴 곳은 Gemini 뿐이다.
    #   실측 근거: 같은 조건에서 Chirp en 14.4자per초 vs Gemini en 11.1 = **약 1.3배** 차이.
    #     → 후보값은 1.3 이다. ⛔ **지금은 1.0(무변경)으로 둔다.**
    #   ⚠ 앞 커밋(구간 침묵 정리)이 체감을 바꾸므로, **먼저 재고 나서** 이 값을 정한다.
    #     둘을 한꺼번에 올리면 어느 쪽이 얼마를 기여했는지 못 가린다. 레버만 미리 달아 둔다
    #     (env 로 통화마다 바꿔 들어볼 수 있다 — 재빌드 없이).
    #   문서 범위 [0.25, 2.0].
    CASCADE_TTS_SPEAKING_RATE_GEMINI: float = 1.0
    # ⭐⭐ **언어별 배속.** `"en:1.4,ko:1.0"` 처럼 적는다(빈 값 = 언어별 지정 없음).
    #   ⛔ 하나의 값으로는 둘을 못 맞춘다. 같은 언어끼리 본 실측(2026-08-10):
    #       한국어  Live 7.7  vs Gemini 6.2~7.4   → 거의 맞았다
    #       영어    Chirp 19.6 vs Gemini 12.0     → 1.6배 느리다
    #     하나를 1.6 으로 올리면 영어는 맞지만 **한국어가 11.8** 이 되어 Live 를 한참 넘긴다.
    #     한국어는 **학습자가 따라 말하는 부분**이라 빨라지면 안 된다.
    #   ⚠ `자per초` 는 언어 간 직접 비교가 **안 된다** — 한국어 1글자(음절 덩어리)가 영어
    #     3~4글자만큼 소리를 낸다. 반드시 **같은 언어끼리** 비교해라.
    #   ⛔ **기본은 비워 둔다.** 값은 사장님이 귀로 찾으실 것이지 우리가 고를 값이 아니다
    #     (오늘 "1.3 이 맞겠지"로 두 번 어긋났다). 데모 화면에서 통화마다 바꿔 시험한다.
    CASCADE_TTS_SPEAKING_RATE_BY_LANG: str = ""
    # ── OpenAI TTS (`/v1/audio/speech`) ──
    # ⭐ `response_format:"pcm"` 이 **24kHz 16-bit mono** 라 우리 규약과 그대로 맞는다(1차 자료).
    CASCADE_TTS_OPENAI_MODEL: str = "gpt-4o-mini-tts"
    # ⚠⚠ **임시 기본값 — 청취 후 교체 예정.** 사장님이 "일단 아무 캐릭터나 넣어"라고 하셨고,
    #   13종을 들으신 뒤 바꾸실 값이다. env 로 재배포 없이 바꾼다.
    #   고른 근거(약하다): 여성·따뜻한 톤이라 비버(다정한 선생님) 결에 맞는다.
    #   ⭐ 13종 실측(같은 한국어 문장·비스트리밍, TTFB ms / 읽기 자per초, **목표 7.7**):
    #     fable 514/6.3 · cedar 546/6.4 · onyx 561/6.8 · alloy 641/6.3 · shimmer 672/6.2
    #     echo 688/7.1 · verse 718/7.0 · nova 734/6.3 · marin 750/6.3 · ballad 797/6.5
    #     sage 875/5.2 · ash 969/6.1 · coral 968/5.5
    #   ⇒ **13종 전부 한국어를 읽는다**(실패 0). 속도만 보면 echo·verse 가 목표에 더 가깝지만,
    #     목소리는 **귀로 고를 값**이라 숫자로 정하지 않았다.
    CASCADE_TTS_OPENAI_VOICE: str = "nova"
    # ⚠ **미측정이라 크게 잡는다.** 선행버퍼가 작으면 합성이 재생을 못 따라갈 때 언더런이 난다
    #   (Gemini 에서 그게 '끊긴다'의 정체였다). 실측 뒤 줄여라 — 이 값은 첫소리를 늦추지 않는다.
    CASCADE_TTS_LEAD_MS_OPENAI: int = 1500
    # ⭐ **한 음성이 두 언어를 다 읽는 엔진**(콤마 구분). 여기 적히면 `__마커__` 분할을 건너뛴다.
    #   구간이 안 쪼개지므로 **요청 수와 구간 침묵이 같이 준다**(429 에도 유리하다).
    #   ⛔ 기본은 비움 = 지금처럼 분할한다. 안 나눴을 때 **한국어 발음이 어떻게 되는지 미확인**이라
    #     내가 기본을 정하지 않는다 — 양쪽을 다 들어보고 사장님이 고르신다.
    CASCADE_TTS_SINGLE_VOICE_ENGINES: str = ""
    # ── 구간 앞뒤 침묵 잘라내기 (2026-08-10) ──
    # ⭐ **어느 엔진에 적용할지**를 이름으로 적는다(콤마 구분). 비우면 아무 데도 안 한다.
    #   ⛔ Chirp 은 **일부러 뺐다.** 사장님이 "빠르게 잘 나온다"고 하신 상태이고, 실측도
    #     ko 6자/1.1초=5.3자per초 로 같은 조건에서 Gemini(2.0)보다 훨씬 낫다. **멀쩡한 걸
    #     건드려 망가뜨리지 않는다.** 나중에 Chirp 에서도 패딩이 관측되면 여기 이름만 더한다.
    CASCADE_TTS_TRIM_ENGINES: str = "gemini-tts,gemini-batch"
    # 잘라낸 뒤 **남길 틈**. 0 으로 두지 마라 — 구간이 딱 붙으면 기계처럼 들린다(우리가
    # 고치려는 게 "AI 티"다). 사람의 자연스러운 절 사이 쉼이 150~250ms 라, 앞뒤 120ms 씩
    # 남기면 구간 경계가 240ms 가 되어 그 대역에 들어온다.
    # ⚠ 앞쪽 값은 **첫소리 보호 여유**이기도 하다 — 소리가 시작된 지점에서 이만큼 되돌아가
    #   자르므로, 파열음처럼 시작이 작은 자음이 날아가지 않는다.
    CASCADE_TTS_TRIM_KEEP_MS: int = 120
    # ⭐ 첫 문장 뒤의 문장들을 **이만큼 모아서 한 번에** 합성한다(요청 수 ↓, 억양 이어짐).
    #   2026-08-07 실통화에서 문장마다 스트림을 여느라 턴당 7회(57 calls/8턴)까지 갔고
    #   **분당 요청 쿼터에 걸려 429** 가 났다. 429 는 곧 폴백(다른 엔진 재합성)이라 한 대답
    #   안에서 목소리가 섞인다. ⛔ 너무 키우면 뒤쪽 문장의 첫 소리가 늦어진다 — 첫 문장은
    #   어차피 단독 즉시 송출이라 이 값은 **뒤쪽 지연과 요청 수의 교환**이다.
    CASCADE_TTS_BATCH_CHARS: int = 160
    # ⭐ Gemini 배치 모드(전체 합성 후 재생)의 **합성 상한**. 넘으면 거기까지 만든 것만
    #   들려주고 그 사실을 로그로 남긴다 — 조용히 멈추면 통화가 죽은 것처럼 보인다.
    #   163자 ≈ 오디오 27초 ≈ 합성 21초(실측 배속 1.3x)라 그보다 넉넉하게 잡는다.
    CASCADE_TTS_BATCH_TIMEOUT_S: int = 90
    # ── 언어 두 개 ──
    # 비버는 **설명은 모국어, 배울 표현은 타깃 언어**로 말한다(code-switching). 타깃 부분을
    # __이렇게__ 감싸 오면 서버가 그 경계로 잘라 **구간마다 그 언어로** 읽는다.
    # ⚠ 데모엔 회원이 없어 둘 다 env 다(실서비스는 member.language / target_language 가 준다).
    # 기본값은 둘 다 ko → 마커가 있어도 같은 언어라 **지금 동작과 같다**(안전한 기본값).
    # ⭐⭐ **학습자 모국어 = 비버가 설명·리액션에 쓰는 언어**(2026-08-12 단일화).
    #   예전엔 같은 뜻이 `CASCADE_PERSONA_LOCALE`(기본 en)과 여기(기본 ko) **두 곳에 다른
    #   기본값**으로 있었다 — 페르소나는 영어로 설명한다면서 TTS 는 한국어 음성으로 읽는
    #   조합이 만들어진다. DB 를 붙이면서 **이 값 하나로 모은다**(세션이 `_locale` 로 확정).
    #   ⚠ 기본을 en 으로 바꾼 이유: 배포 env 가 이미 en 이고(demo-api), 페르소나 기본도 en 이라
    #     **둘 중 en 쪽이 실제로 돌던 값**이다. 학습자는 외국인이다.
    CASCADE_TTS_LANGUAGE: str = "en"             # 모국어 구간을 읽을 언어(= 페르소나 locale)
    CASCADE_TTS_TARGET_LANGUAGE: str = "ko"      # __마커__ 안쪽을 읽을 언어
    CASCADE_TTS_TARGET_LANGUAGE_LABEL: str = "한국어"   # 프롬프트에 넣을 이름(예: "영어")
    CASCADE_TTS_VOICE: str = "Aoede"             # Chirp3-HD 음성명(Live 캐릭터 voice 와 같은 이름 체계)
    # 데모용 페르소나(캐스케이드는 아직 DB·캐릭터를 안 읽는다 — normalcall 과 같은 조립기를 쓴다)
    CASCADE_PERSONA_ROLE: str = "한국어를 가르치는 다정한 비버 선생님"
    CASCADE_PERSONA_PERSONALITY: str = "친근하고 밝다. 짧게 말하고 자주 되묻는다."
    # ⛔ 여기에 속도 지시를 쓰지 마라. 프롬프트가 "천천히"를 시키고 파라미터
    #   (CASCADE_TTS_SPEAKING_RATE)가 빠르게 잡으면 **다음 사람이 어느 게 진짜인지 못 가린다.**
    #   난이도(쉬운 단어·짧은 문장)만 맡는다.
    CASCADE_PERSONA_LEVEL: str = "아주 쉬운 단어와 짧은 문장으로 말한다."
    # ⚠ **더 이상 안 쓴다**(2026-08-12). 모국어는 `CASCADE_TTS_LANGUAGE` 하나로 모았다 —
    #   env 호환을 위해 남겨 두지만 코드가 읽지 않는다. 값을 바꿔도 아무 일도 안 일어난다.
    CASCADE_PERSONA_LOCALE: str = "en"
    # ── DB 연결(2026-08-12): 데모 전용 **덮어쓰기**. 비어 있으면 DB 값이 이긴다 ──
    # ⛔ 위 세 값(`CASCADE_TTS_VOICE`·`CASCADE_TTS_LANGUAGE`·`CASCADE_PERSONA_LOCALE`)을
    #   덮어쓰기로 쓰면 안 된다 — **배포 env 에 이미 값이 들어 있어서**(demo-api:
    #   CASCADE_TTS_VOICE=Sulafat, CASCADE_TTS_LANGUAGE=en …) DB 캐릭터·언어가 **영영 안 먹는다.**
    #   그 셋은 **DB 가 없을 때의 기본값**이고, 실험용 덮어쓰기는 아래 세 개다(기본 빈 값).
    #   ⚠ 데모 화면에는 음색·언어 선택 UI 가 없다(2026-08-12 확인) — 실험은 env 로만 한다.
    # 통화중 세그먼트 **점진 저장** 주기(초). Live 와 같은 1분 — 긴 통화·크래시 내성이 목적이다
    # (통화가 죽어도 그때까지의 전사·오디오가 남는다). ⛔ 크게 잡으면 그만큼 잃는다.
    CASCADE_SEGMENT_FLUSH_S: float = 60.0
    CASCADE_TTS_VOICE_OVERRIDE: str = ""          # 캐릭터 음색을 무시하고 이 음성으로
    CASCADE_LOCALE_OVERRIDE: str = ""             # 회원 모국어를 무시하고 이 언어로
    CASCADE_TARGET_LANGUAGE_OVERRIDE: str = ""    # 학습 대상 언어를 무시하고 이 언어로
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
    # ⭐ **보류가 유효한 시간** — 이 안에 전사가 오면 확정, 안 오면 **기각**이다.
    #   ⛔ 예전엔 이 시간이 차면 "전사가 없어도 끊는" **안전망**이었다. 2026-08-10 사장님 지시로
    #     없앴다. 실측이 이유다: rms=**0.0077**(침묵 0.0000~0.0030 과 발화 0.011~0.44 사이의
    #     회색지대)에서 안전망이 비버를 죽였고, 그 뒤 턴까지 굳어 **30초 완전 침묵**이 됐다.
    #     막으려던 것(사용자 말이 묻히는 것)보다 더 나쁜 결과를 만들었다.
    #   ⇒ **전사 없이는 절대 끊지 않는다.** 여기 남은 역할은 하나뿐이다: 오래된 보류가
    #     영영 살아 있어서 **한참 뒤 엉뚱한 전사에 확정되는 것**을 막는 유효기간.
    #   3500ms 근거: 전사 확정 실측 최댓값 620ms · 파이프라인 지연 최댓값 914ms 의 ~4배.
    #     넉넉해도 안전하다 — 이 시간이 하는 일은 이제 **기각**뿐이다.
    CASCADE_BARGEIN_PENDING_MS: int = 3500
    # 취소로 죽은 대답을 되살리는 유예. 이 안에서 사용자가 결국 아무 말도 안 했으면(빈 턴)
    # 하던 말을 이어서 한다 — 침묵으로 끝내지 않는다.
    # ⚠ 주석이 **다른 설정을 설명하고 있었다**(2026-08-11 QA 발견7). 실제 용도는 이것이다:
    #   barge-in 으로 **끊긴 대답을 이어 말할 유예**. 이 시간을 넘기면 포기한다
    #   (`_resume_interrupted`). 잘못된 주석이 설명하던 값은 `CASCADE_BARGEIN_MIN_MS` 다 —
    #   그대로 뒀으면 barge-in 을 튜닝하려는 사람이 **이어 말하기를 끄게** 된다.
    CASCADE_RESUME_WINDOW_MS: int = 8000
    CASCADE_BARGEIN_MIN_CHARS: int = 2           # transcript 확인 모드에서 요구할 최소 글자수
    # ⭐⭐ **이 게이트의 일은 '사용자가 말했나'가 아니라 '이게 비버 자기 목소리인가'다**
    #   (2026-08-08 사장님 판단으로 역할이 재정의됐다). 그러면 임계는 **발화 분포가 아니라
    #   에코 잔여 분포 위**에 긋는 값이다 — 발화 쪽에 맞춰 올리면 에코 필터가 아니라
    #   **발화 필터**가 되고, 그게 08-08 오전 기각 17건을 만든 상태였다.
    #   실측 두 덩어리:  재생 중 잔여 에코 0.0000~0.0030 / 실제 발화 0.0110~0.0443
    #   → 0.007 = 잔여 상단의 2배 이상 위, 발화 하단보다 확실히 아래.
    #   ⚠ AEC 를 선언한 세션에서는 이 관문을 **아예 돌리지 않는다**(막을 대상이 없다 —
    #     08-08 로그에서 비버 재생 중 전사 0건). protocol.AEC_MODES_WITH_CANCEL 참고.
    # 0.05 → 0.010 → 0.007 (2026-08-08 실측으로 근거가 두 번 바뀌었다).
    #   비버 발화 중 마이크 에너지가 **두 덩어리로 깨끗하게 갈린다**:
    #     진짜 침묵   0.0000 ~ 0.0030
    #     사장님 발화 0.0110 ~ 0.0443     ← 10~20배 차이
    #   그런데 임계선이 **두 덩어리 위에** 있어서, 말을 끊으려 해도 17건이 기각됐다
    #   ("말 끊는 게 될 때 있고 안 될 때 있다"의 정체다. 클라는 0~16ms 안에 버퍼를 비웠으니
    #    프론트 문제가 아니라 **서버가 취소를 안 보낸 것**이었다).
    #   원인: 브라우저 AEC 가 double-talk 구간에서 사용자 목소리까지 1/3 수준으로 누른다.
    #   "보수적 = 높게" 라는 옛 주석의 전제(잔여 에코가 크다)는 실측으로 깨졌다 — 잔여
    #   에코는 0.003 이하다. 0.010 이면 그 3배 여유이고 실제 발화(0.011~)는 전부 통과한다.
    #   ⛔ env 로만 때우지 않는다 — 코드 기본값이 낡으면 env 없는 환경이 옛 동작으로 남는다.
    #   안전망: 임계를 넘어도 전사 확인 게이트가 한 겹 더 있다(CASCADE_BARGEIN_CONFIRM).
    CASCADE_BARGEIN_RMS: float = 0.007            # 0~1 정규화 RMS(0 이면 이 관문 비활성)
    # (CASCADE_ECHO_TAIL_MS 는 2026-08-11 제거 — 에코 분류기를 걷어낸 뒤 아무도 안 읽는다.
    #  설정 50개 전수 확인에서 유일한 죽은 값이었다.)
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
    CASCADE_TTS_LEAD_MS: int = 200               # 송출 시작 전 확보할 합성 선행분(Chirp 기준)
    # ⭐ **엔진마다 필요한 버퍼가 다르다.** 상수 하나로 쓰다가 Gemini 가 끊겼다(2026-08-08).
    #   재측정(한국어, 스트리밍): Gemini 는 합성이 재생보다 **최대 1.16~1.48초 뒤처진다.**
    #   그런데 200ms 만 모으고 재생을 시작했으니 언더런이 나는 게 당연했다.
    #   ⚠ 배속 자체는 1.68~1.94x 로 **실시간보다 빠르다** — 초반 1.5초만 견디면 그 뒤로는
    #     격차가 계속 벌어져 안 끊긴다. 즉 문제는 속도가 아니라 **출발 버퍼**였다.
    #   대가는 첫 소리가 그만큼 늦는 것이다(배치 모드의 20초+ 보다는 훨씬 낫다).
    CASCADE_TTS_LEAD_MS_GEMINI: int = 1500
    # 짧은 요청은 Gemini 에 특히 불리하다(연결·모델 로딩 고정 오버헤드 ≈1.3초가 통째로 붙는다).
    # 그래서 Gemini 는 더 크게 묶는다. ⚠ TTFB 는 길이와 거의 무관했다(49자 1,328ms /
    # 196자 1,188ms) — 묶어도 첫 소리가 그만큼 늦지는 않는다.
    CASCADE_TTS_BATCH_CHARS_GEMINI: int = 400
    # ⭐ OpenAI 도 **큰 묶음** 쪽이다(2026-08-11). 실측 TTFB 를 나란히 놓으면 이유가 보인다:
    #     Chirp  165~212ms  → 요청이 많아도 왕복이 짧아 안 끊긴다(그래서 160)
    #     Gemini 805~1271ms → 왕복은 긴데 요청이 적어 안 끊긴다(그래서 400)
    #     OpenAI 545~953ms  → ⛔ 그런데 **160** 을 쓰고 있었다 = 요청도 많고 왕복도 길다
    #   실통화에서 99자에 요청 6회, 내 실측에서도 4회에 벤더 대기 합계 2.90초가 나왔다.
    #   400 근거: 실측 대답이 99~120자라 **대부분 한 번**에 들어간다(왕복 1회). 그리고 TTFB 는
    #   길이와 거의 무관하므로(Gemini 실측 49자 1,328ms / 196자 1,188ms) 크게 묶어도 손해가 없다.
    #   ⚠ Gemini 보다 왕복이 짧으니 더 작게 잡을 여지는 있다 — 실측 뒤 조정해라(그래서 별도 값이다).
    CASCADE_TTS_BATCH_CHARS_OPENAI: int = 400
    # (ElevenLabs 설정 5종은 2026-08-10 제거했다 — 실측 전에 접었다. git 이력에 남아 있다.)

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