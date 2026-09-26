"""애플리케이션 설정 (pydantic-settings).

Spring 의 application.yml 대응. `.env` 파일에서 값을 읽어온다.

- DATABASE_URL_POOL   : 런타임용 6543 Transaction Pooler 연결 (pgbouncer)
- DATABASE_URL_DIRECT : Alembic 마이그레이션용 5432 Direct 연결
"""

from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # ⭐⭐ **일일 통화 한도만 따로 켜는 스위치**(2026-08-20 사장님 지시).
    #   ⛔ 왜 ENV 로 안 하나: `ENV=prod` 는 한도만 켜는 스위치가 아니다. 같은 값이 dev
    #     데모 라우트(`/__levelcalldemo`·`/__enginedemo`, main.py:375)를 닫고 통화 세션의
    #     prod 가드(call_session.py:366)도 같이 켠다. 한도 하나 켜자고 개발용 데모 페이지를
    #     닫게 되는데, app-api 는 사장님 개발 서버라 그게 곤란하다.
    #   ⇒ 축을 나눈다. 이 값이 True 면 ENV 와 무관하게 한도가 돈다.
    #
    #   ⚠ **prod 는 이 값과 무관하게 계속 돈다**(`or settings.ENV == "prod"`). 회귀 없음 —
    #     실서비스에서 이 플래그를 안 켰다고 한도가 풀리면 그게 사고다.
    #   ⛔⛔ **켜면 Free 는 하루 1통화에서 잠긴다.** 원래 주석이 적어둔 그대로다:
    #     "테스트하다 하루가 잠기면 개발이 안 된다." 사장님 계정(member 20)은 플랜이
    #     없어 Free 이므로, app-api 에 켜면 **하루 한 통화 뒤 본인 테스트가 막힌다.**
    #     ⇒ 프론트 검증용으로 켤 곳은 demo-api 쪽이 맞다. 켤 위치를 정하고 켜라.
    DAILY_LIMIT_ENFORCED: bool = False

    # ⭐⭐ S5(2026-09-26, Play 심사 대비) — **dev 전용 라우트(main.py:296)만 따로 켜는
    #   스위치** — 위 DAILY_LIMIT_ENFORCED 와 같은 「축을 나눈다」 패턴이다.
    #   ⛔ 왜 ENV 로 안 하나: 실서비스(app-api)의 ENV 는 "prod" 가 아니라 "test" 다(2026-08-07
    #     실측) — `main.py:296` 이 `ENV != "prod"` 로 게이트돼 있어 `/__dev/signup`(service key
    #     로 계정 생성+토큰 발급, 이메일 확인 건너뜀)·`/__levelcalldemo` 등 dev 라우트 15개가
    #     실서비스에 그대로 열려 있었다(요청서 S5, bt-back 실측: 운영 `/__dev/signup` → 400,
    #     `/__levelcalldemo` → 200). `ENV` 를 prod 로 바로잡으면 근본 해결이지만, 같은 조건을
    #     쓰는 다른 블록(`call_session.py:366` 의 prod 가드)이 동시에 켜져 영향 범위가 커진다
    #     (`docs/20260807_0510_dev블록-노출-사실관계.md`) — 그 별건은 여전히 미착수다.
    #   ⇒ 이 값이 dev 라우트 마운트 여부를 **ENV 와 무관하게** 단독으로 결정한다.
    #   기본 False = **안 넣으면 닫힘**(운영은 env 를 안 넣으면 그대로 안전). demo-api·
    #   test-api 는 이 값을 true 로 넣어야 데모가 산다(Cloud Run env — bt-back 이 넣는다).
    DEV_ROUTES_ENABLED: bool = False

    # ⭐⭐ C4(2026-09-23, D4) — **하루 통화 총량(분) 예산**. Free 300s·premium 900s,
    #   콜타입 무관 합산(레벨테스트 제외). 옛 콜타입별 횟수 한도(DAILY_LIMIT_ENFORCED,
    #   DAILY_CALL_LIMIT)는 레벨테스트에만 남고 나머지는 이 예산이 대체한다.
    #   ⛔⛔ **기본값이 True 인 이유**: 옛 한도(DAILY_LIMIT_ENFORCED)가 운영에서 계속
    #     꺼진 채였던(사장님이 몰랐던) 문제가 있었다 — 그 구멍을 여기서 닫는다. prod 는
    #     이 값과 무관하게 계속 돈다(`or settings.ENV == "prod"`, DAILY_LIMIT_ENFORCED
    #     와 같은 이중 게이트).
    #   ⚠ 로컬 개발에서 5분/15분에 자꾸 막히면 `.env.local` 에 `DAILY_BUDGET_ENFORCED=false`
    #     를 넣어라(admin 롤 계정은 어차피 면제다 — 위 주석과 같은 탈출구).
    DAILY_BUDGET_ENFORCED: bool = True

    # 통화 대상 언어 기본값(멀티랭귀지). start.target_language 오버라이드가 없거나
    # 미지원 코드면 이 값으로 폴백. core.languages.DEFAULT_LANGUAGE 와 같은 값(ko).
    DEFAULT_TARGET_LANGUAGE: str = "ko"

    @property
    def direct_url(self) -> str:
        """마이그레이션용 URL. 미설정이면 런타임 URL 로 폴백."""
        return self.DATABASE_URL_DIRECT or self.DATABASE_URL_POOL

    # ⛔⛔ P2-6(2026-09-24, bt-back QA) — 옛 JWT 인증 설정 4개(JWT_SECRET·JWT_ALGORITHM·
    #   ACCESS_TOKEN_EXPIRE_MINUTES·PASSWORD_RESET_EXPIRE_MINUTES)를 지웠다. 자체 JWT
    #   발급·검증은 `core/security.py` 와 함께 이미 삭제됐다(Supabase GoTrue 로 이전,
    #   인증은 `core/supabase_auth.py`) — 읽는 코드가 0건이었다(grep 확인). 삭제는
    #   안전하다(bt-back 확인) — `extra="ignore"` 라 Cloud Run env 에 그 이름이 남아
    #   있어도 기동을 막지 않는다. 되살리지 마라 — 다시 자체 JWT 가 필요해지면 그때
    #   새로 설계한다.

    # ── SpeechSuper 발음평가 ──
    # 미설정이면 core.speechsuper 가 결정적 스텁으로 폴백한다(앱은 그대로 동작).
    SPEECH_SUPER_APP_KEY: str | None = None
    SPEECH_SUPER_SECRET_KEY: str | None = None
    SPEECH_SUPER_CORETYPE: str = "sent.eval.kr"  # 한국어 문장 평가

    # ── B2B 교실 서비스 (2026-09-02 분리) ──
    # 과제 통화의 회화 목표를 여기에 묻는다. 교실 테이블은 이 서버가 읽지 않는다.
    # ⛔ 둘 중 하나라도 비면 조회를 건너뛰고 **평소 선별로 통화가 진행된다** —
    #    막지 않는다. 회화 목표는 통화의 성립 조건이 아니라 재료다.
    B2B_API_BASE_URL: str | None = None
    B2B_SERVICE_TOKEN: str | None = None

    # ── 국적 분류 (외부 오디오 국적 추론 API) ──
    # 미설정이면 core.nationality 가 조용히 비활성(None 반환) — 통화·분석 무영향(R5).
    NATIONALITY_API_URL: str | None = None      # 예: https://<tailscale-host> (POST {URL}/predict)
    NATIONALITY_API_KEY: str | None = None      # X-API-Key(GPU 서버 앞단 인증 프록시). 비면 헤더 생략
    NATIONALITY_API_TIMEOUT_S: float = 20.0     # httpx read/write 타임아웃(초)
    NATIONALITY_MIN_SPEECH_S: float = 10.0      # 이 길이 미만 user 발화는 호출 스킵(호출측 게이트)

    # ⛔⛔ P2-6(2026-09-24, bt-back QA) — 옛 이메일 발송(Resend)·이메일 인증 코드·구글
    #   소셜 로그인 설정 7개(RESEND_API_KEY·MAIL_FROM·EMAIL_CODE_LENGTH·EMAIL_CODE_
    #   EXPIRE_MINUTES·EMAIL_CODE_MAX_ATTEMPTS·EMAIL_CODE_RESEND_SECONDS·GOOGLE_
    #   CLIENT_ID)를 지웠다. 읽는 코드가 0건이었다(grep 확인 — `core/email.py` 는
    #   이 파일 말고 어디에도 없다, 이미 삭제됐다). 삭제는 안전하다(bt-back 확인) —
    #   `extra="ignore"` 라 Cloud Run env 에 그 이름이 남아 있어도 기동을 막지 않는다.

    # ── normalcall (Gemini Live 음성통화 + 통화후 분석 + TTS + Storage) ──
    # 미설정이면 어댑터들이 graceful 폴백(통화 불가/분석 스킵/스텁). 앱은 그대로 뜬다.
    GEMINI_API_KEY: str | None = None              # AI Studio (USE_VERTEX=false 일 때)
    USE_VERTEX: bool = False                        # True 면 Vertex AI 사용
    GCP_PROJECT: str | None = None                 # Vertex 프로젝트 ID
    GCP_LOCATION: str = "us-central1"              # Vertex 리전
    GOOGLE_APPLICATION_CREDENTIALS: str | None = None  # 서비스계정 키(JSON) 경로
    GEMINI_LIVE_MODEL: str = "gemini-3.1-flash-live-preview"  # 통화(실시간 음성)
    # ⭐⭐ **C2(2026-09-22, D2) — 3.1 단일 모델.** 옛 2.5/3.1 이원화(플랜별 모델 분기,
    #   2026-09-04)를 걷어냈다 — Free 도 3.1 음성(표정 도구만 없음), Premium 은 3.1
    #   영상(+set_face). 두 값이 이제 **같은 모델**이다.
    #
    #   ⛔ `GEMINI_LIVE_MODEL` 은 **폐기하지 않는다** — 아래 둘이 비어 있을 때의 폴백이고,
    #     레벨테스트·캐스케이드 등 플랜을 모르는 호출부가 아직 그것을 본다. 두 값을 다
    #     비워 두면 종전 동작 그대로다(하위호환).
    #   ⚠ 값을 여기서 고르지 마라 — 고르는 곳은 `call_service.live_model_for()` 하나다.
    #     두 곳에서 고르면 언젠가 갈라진다.
    #
    #   ⛔ 역사(2.5 시절의 함정, 지금도 유효한 교훈 — **모델 이름과 `USE_VERTEX` 는
    #     반드시 같이 움직인다**): demo-api 리비전 `00265-br2`(2026-09-06)가 `USE_VERTEX`
    #     만 뒤집고 모델 이름을 안 바꿔 AI Studio 에 없는 이름으로 1008 이 났다 —
    #     **Free·Pro 통화가 2주 죽었다**(당시 Max 만 VIDEO=3.1 이라 멀쩡해서 아무도 몰랐다).
    #     지금은 3.1 이 Vertex 에 아예 없으므로(아래) 이 함정 자체가 구조적으로 막혔다 —
    #     Vertex 를 켜도 `live_engine_for` 가 빈 Vertex 모델을 보고 studio 로 되돌린다(R5).
    #   ⚠ 바꿀 땐 `tests/test_live_model_name.py` 가 형태를 잠근다. 그래도 **실제 존재
    #     여부는 배포 전에 API 로 확인해라** — 테스트는 오프라인이라 그것까진 못 본다.
    LIVE_MODEL_VOICE: str = "gemini-3.1-flash-live-preview"
    LIVE_MODEL_VIDEO: str = "gemini-3.1-flash-live-preview"

    # ⭐ 백엔드 분기 기반은 남겨 둔다(2026-09-08 도입) — **3.1 은 Vertex 에 없으므로**
    #   (2026-09-08 실측 1008) 지금은 Free·Premium 모두 사실상 studio 로 고정된다.
    #   Vertex 가 2.5 시절 냈던 속도 이득(중앙 1.15초 vs AI Studio 2.82초)은 3.1 에선
    #   못 받는다 — 구글이 3.1 을 Vertex 에 올리면 `LIVE_MODEL_*_VERTEX` 만 채워 되살린다.
    # ⚠ 비워 두면 위 `LIVE_MODEL_VOICE/VIDEO` + 전역 `USE_VERTEX` 로 떨어진다.
    LIVE_VOICE_BACKEND: str = "studio"  # "vertex" | "studio" | "" (=전역 USE_VERTEX 따름)
    LIVE_VIDEO_BACKEND: str = ""        # 〃 — 이미 Vertex 모델이 비어 있어 R5 로 studio 폴백
    LIVE_MODEL_VOICE_VERTEX: str = ""  # ⛔ 3.1 은 Vertex 에 없다 — 비워 둔다(실측 1008)
    LIVE_MODEL_VIDEO_VERTEX: str = ""  # ⛔ 3.1 은 Vertex 에 없다 — 비워 둔다(실측 1008)

    JUDGE_MODEL: str = "gemini-2.5-flash"          # 통화후 분석(generateContent)
    # ⭐ 표현학습 판정의 주인(2026-09-15 4차, 사장님): «가르쳤나»(비버 턴마다)·«맞혔나»(퀴즈 창 학습자 턴마다)를 JUDGE_MODEL 사이드카가 의미로 판정한다.
    #   False 면 종전 문자열 대조(quiz_judge) 경로만 — 시험 기본값(tests/conftest.py)이자 비상 스위치. 사이드카 실패 턴은 켜져 있어도 문자열로 폴백한다(R5).
    EXPR_LLM_JUDGE: bool = True
    # ⛔ 옛 EXPR_CUE_COMPLETED_TURN_25(10차, 2026-09-16)는 C2(2026-09-22, D2 3.1 단일화)에서
    #   삭제했다 — 2.5 계열 모델 자체가 더 이상 안 쓰인다. 퀴즈 큐·안내는 이제 항상
    #   재접지 얹기(send_reground turn_complete=False) 하나로만 간다.

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
    # 입력 전사에 **들릴 언어**를 힌트로 준다(학습 언어 + 모국어 2개). 끄면 종전처럼
    # 자동 감지 — 짧은 한국어가 통째로 다른 언어로 찍혔다(실측 call_id=1097:
    # "다"→`套`, "아주"→`और च`). 그 전사는 DB 에 저장돼 통화후 분석이 읽으므로 데이터 문제다.
    # ⛔ 킬스위치인 이유: 이 필드는 Live **세션 setup** 에 실린다. 백엔드가 거절하면
    #   통화가 열리지 않는다 — 재배포 없이 `gcloud run services update` 로 끌 수 있어야 한다(R5).
    LIVE_INPUT_LANGUAGE_CODES: bool = True
    LIVE_CTX_TRIGGER_TOKENS: int = 16000  # 압축 발동 임계
    LIVE_CTX_TARGET_TOKENS: int = 12000   # 압축 후 유지량(trigger 보다 작아야 한다)
    # ⛔ 3.1 Live 실측(2026-09-12 ctx-lab, docs/20260912_1630_3.1-압축-연구.md §4.2·§6): target 은 **턴 단위로 거칠게
    #   양자화**된다 — target 4000 은 매 압축이 «마지막 유저 턴 1개» 착지(쪽지·큐 전부 퇴출), target 5000 도 5분에 1회
    #   전면 퇴출(A→259). **7000/5000·6000/4000 은 사장님 결정으로 금지**(옵션으로도 안 연다). 운영은 8000/7000.
    #   검증으로 막지는 않는다(실험 서비스에서 재기 위해) — 값을 바꾸려면 설정별 1통 실측이 규칙.
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
    # ── 표정 툴 규칙 모드(2026-09-12 ctx-lab, docs/20260912_1630_3.1-압축-연구.md; 문구는 core/prompts/locked/face.py) ──
    # 3.1 Live 는 blocking 함수콜(set_face)이 낀 턴을 **2회 추론**해 그 턴의 컨텍스트를 2배로 과금한다(비동기 함수콜
    # 미지원 — 공식 문서). 표현학습은 24/25 턴이 툴 턴이라 5분 $0.56 이 나왔다. 호출 빈도가 곧 원가다.
    #   "qual"(운영 기본, 사장님 결정 2026-09-12): 규칙 2줄 «감정이 드러나는 턴마다 — 같은 감정이어도 매번» /
    #        «감정 없는 평범한 턴은 부르지 않는다(평소 얼굴 복귀는 자동)». enum 에 neutral 없음(앱이 클립 뒤 idle 복귀).
    #        숫자 예산·반복 금지 없음. 표현학습·프리토킹 대본에도 [표정] 블록이 붙는다. 실측 1469: 툴 턴 50%·$0.450(−22%)·표정 과소 0.
    #   "":   종전(매 변화·neutral 복귀 필수, 규칙은 일반 통화 대본에만) — 실측 96% 툴 턴·$0.576. 기록·되돌리기용.
    #   "sparse"/"budget": 실험 기록용(1463 68% 실패 / 1465 14%·$0.314 이나 핀잔 턴 5곳 표정 누락 → 폐기).
    LIVE_FACE_RULE_MODE: str = "qual"
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

    # ── 라이브 표정(영상통화 아바타) 스위치 ────────────────────────────────
    # ⚠ 이름이 SPIKE 인 건 **출신 때문**이다 — 2026-08-18 에 "모델이 tool 을 부르기는
    #   하는가"를 로그로만 재보려던 계측 스파이크였다. 다음날(08-19) 본편에서 마커 전송이
    #   붙어 **지금은 실험이 아니라 영상통화 표정의 실제 스위치**다. 이름만 남았다.
    #
    # 켜면 셋이 붙는다:
    #   ① Live 세션에 `set_face` tool 선언        (call_session.py `live_tools=[SET_FACE_TOOL]`)
    #   ② 프롬프트에 표정 지시 한 줄              (persona_prompt.py `face_tool=True`)
    #   ③ 모델이 부르면 **클라로 마커 전송**       (`ServerSentenceMarker` turn_id·seq·emotion)
    #
    # ⛔⛔ ③ 을 빠뜨리지 마라. 예전 이 자리에 *"클라로 아무것도 안 보내고 화면도 안 바뀐다"*
    #   라고 적혀 있었다(2026-08-20 에 고쳤다). 최초 스파이크 시점엔 참이었지만 본편이
    #   붙은 뒤로 **거짓**이었고, 그대로 두면 "계측이니 켜도 안전하다"고 읽힌다. 켜는 순간
    #   프론트로 실제 프레임이 나간다.
    # ⛔ 기본 False. 꺼져 있으면 Live config·프롬프트가 **바이트 동일**(회귀가 지킨다).
    # ⛔ `LIVE_CALL_END_OWNER` 는 **없다**(T23, 2026-09-12 사장님 결정). 통화 길이 만료는 **프론트가** 소켓을 닫아
    #   끝낸다(이어하기 설계 §8 «무음 컷 · 주입 0») — 서버 길이 시계로 작별 시드를 넣는 경로는 코드에서 지웠다.
    #   2026-08-19 에 «임시 스위치» 로 남겨 뒀는데 운영 env 가 "server" 로 남아 5분마다 작별이 나갔다. 스위치를 되살리지 마라.
    #   남는 종료 사유: 무음 3단 · GoAway · 사이드카 종료 요청 — 셋 다 같은 종료 시드 파이프를 쓴다. 절대 백스톱(540초)은 별개 축.
    #   그 경로에 걸려 있던 회귀(RC1 소강 스타베이션 · call 197 종료 레이스 · 무음 우선순위)는 남는 경로로 옮겨 잠갔다
    #   (tests/test_normalcall_ws.py — «_request_close»·«GoAway» 판).
    LIVE_FACE_SPIKE: bool = False
    # ⭐ 커리큘럼 2단계(2026-09-12, docs/plans/2026-09-12-cur-2단계-통화경로-이전.md) — 표현학습·프리토킹이 cur_* 체계로 재료를 뽑고
    #   진도를 쌓는다. false 면 옛 경로(learning_item 기반) 그대로 — 코드 배포 없이 되돌리기.
    #   ⛔ 경로는 **통화 단위로 고정**된다(§6 ③·§7 P1-6): 이 값은 통화 **시작 시점**에만 읽고, cur_call 행이 생긴 통화는 끝까지
    #     cur 경로(종료 저장·재분석·결과 조회가 «cur_call 존재 여부» 로 분기). 롤링 배포·되돌리기 중 한 통화가 두 경로에 갈라지지 않게.
    CUR_ENABLED: bool = True
    # 표현학습 한 통화에 싣는 항목 상한(§11: 안 배운 것 seq 순 → 부족분은 복습으로 채움). 옛 EXPRESSION_ITEMS_PER_CALL 과 별개.
    CUR_ITEMS_PER_CALL: int = 18
    # 차시 프리토킹(cur 경로)만 무음 1단 임계(초). 왕초보 침묵 = «못 알아들음» — 60s 는 5분의 20%(계획 D-a 채택). 다른 코스는 60 유지.
    FREETALK_IDLE_NUDGE1_S: float = 30.0
    # ⭐ 재접지 모드. "on_user_turn" | "legacy_idle" | "off"
    #   ⛔ **왜 env 로 뺐나**(2026-09-02) — `interrupted` 의 원인을 가르려면 재접지를 **끄고**
    #     통화해 봐야 하는데, 상수라 그 실험 한 번에 재빌드·재배포(5~6분)가 들었다.
    #   ⚠ 지금 상관만으로는 못 가른다: 재접지는 **우리가 넣어 시각이 정확**하고, 압축은
    #     usage 급감으로 **사후 추론**이라 시각이 부정확하다. 정확한 신호와 부정확한 신호를
    #     같은 잣대로 비교하면 정확한 쪽이 원인처럼 보인다.
    #     ⇒ 상관이 아니라 **대조**로 판정한다: 끄고도 interrupted 가 남으면 압축이 원인,
    #       사라지면 재접지가 원인이다.
    #   ⚠ 모드 뜻은 `domains/learning/realtime/call_session.REGROUND_MODE` 주석 참조.
    #
    # ⛔⛔ **이 스위치를 내리면 표현학습 진도 판정이 같이 줄어든다**(2026-09-10). 그 코스의
    #   «통화 중» 판정 사이드카는 재접지 arm 루프 안에서만 뜬다
    #   (`call_session._reground_watch` → `_spawn_expression_progress`).
    #   off·legacy_idle 로 내리면 통화 중 판정이 **0회**가 되어 판정이 조각 끝 1회로 줄고,
    #   재접지 쪽지의 «맞힌/틀린» 칸이 통화 내내 빈다(= 오답퀴즈 재료가 없다).
    #   ⚠ 재접지만 끄려고 내렸다가 학습 진도가 조용히 나빠지는 것을 막으려고 적어 둔다.
    LIVE_REGROUND_MODE: str = "on_user_turn"
    # ⭐⭐ 재접지를 **어느 통로로** 보낼까. "client_content" | "realtime"
    #
    #   ⛔ 왜 필요한가 — `client_content` 는 **설계상** 진행 중 생성을 끊는다.
    #     SDK 원문(types.py:20271, 벤더가 proto 에서 생성한 문서):
    #       "A message here will interrupt any current model generation."
    #     그리고 `interrupted` 필드(:19319): "a **client message** has interrupted..."
    #     ⇒ `turn_complete` 값과 무관하다. 우리 실측 43/43 은 버그가 아니라 스펙이다.
    #
    #   ⛔ 게다가 3.1 에서는 **금지된 용법**이다. 공식 문서가 모델별로 갈라 놨다:
    #       3.1: "send_client_content is only supported for seeding initial context
    #             history. To send text updates during the conversation,
    #             use send_realtime_input instead"
    #       2.5: 대화 중 사용 가능
    #     ⇒ 모델을 옮기며 규약이 바뀐 것을 우리가 못 따라갔다.
    #
    #   ⭐ "realtime" 은 `send_realtime_input(text=...)` 을 쓴다. RealtimeInput 은
    #     "can be sent continuously **without interruption to model generation**" 로
    #     규정된다. SDK 2.10.0 에 이미 있다(live.py:250) — 업그레이드 불필요.
    #     ⚠ `realtime_input_config`(setup config)와 **다른 축**이다 — 그건 안 건드린다.
    #       무음 버그 불변식(gemini_live.py:229)이 무사하다.
    #
    #   ⚠ **미검증**: realtime text 가 조용히 적재되는지, 즉시 응답을 촉발하는지 문서가
    #     침묵한다. 촉발하면 이중발화가 난다 — 그래서 스위치로 두고 실측한다.
    LIVE_REGROUND_TRANSPORT: str = "realtime"
    # ⭐⭐ 재접지를 **언제** 얹을까. "mic_open" | "first" | "final"
    #
    #   ⛔ 왜 바꿨나 — `in_tr`(입력 전사)은 **늦게 오는 보고서**다.
    #     실측(2026-09-02 통화, 82회 전수):
    #         재접지 얹기 82회 → 0.5초 안에 interrupted 43회(52%)
    #                          → 그중 비버 턴이 열려 있던 것 6회 = 말이 잘렸다
    #     한 건을 밀리초로 펼치면:
    #         09:50:57.387 얹기 → .390 비버 턴 시작(3ms) → .456 interrupted
    #     우리 로그가 매 턴 스스로 찍고 있던 문장이 원인이다:
    #         "응답지연: ⛔끝기준 무의미(전사가 응답과 동시 도착)"
    #     ⇒ Gemini 는 자기 전사를 우리한테 보내주는 걸 기다렸다 답을 만들지 않는다.
    #       **전사와 비버 답이 같이 온다.** 그래서 전사를 보고는 "사용자가 말하는 중"과
    #       "비버가 답하는 중"을 구분할 수 없다.
    #
    #   ⭐ "mic_open" — 업링크 마이크 프레임에 얹는다. 이게 더 나은 이유는 **지연이 없어서가
    #     아니라, 도착 자체가 비버 침묵의 증거이기 때문**이다:
    #         클라  `_micGated`          — 비버 발화중(+꼬리)엔 프레임을 아예 안 보낸다
    #         서버  `state.turn_id is None` — 그때만 Gemini 로 넘긴다
    #     ⇒ 프레임이 이 자리에 도착했다 = 양쪽이 각각 "비버는 안 말한다"를 보증했다.
    #       끊을 것이 없는 자리다.
    #   ⚠ 클라는 마이크를 **상시** 보낸다(목소리일 때만이 아니다 — 클라 VAD 는 계측 전용,
    #     `normalcall_controller.dart:2447` 주석). 그래서 서버가 RMS 로 발화를 직접 본다.
    #     안 그러면 침묵에 쪽지가 나가 비버가 혼자 말한다.
    LIVE_REGROUND_ATTACH_AT: str = "mic_open"
    # 서버측 발화 판정 임계(정규화 RMS). 클라 `_voicedRmsThreshold` 실측값과 **같은 수**다
    # (`normalcall_controller.dart:420`) — 두 쪽이 갈라지면 같은 소리를 두고 다르게 판정한다.
    # ⚠ 이 계산은 `reground_pending` 일 때만 돈다(핫패스 보호, R4).
    LIVE_REGROUND_VOICE_RMS: float = 0.02
    # ⭐ 같은 오디오 자리에 꽂히는 표정 마커를 **하나로 합칠까**(2026-09-01).
    #   프론트는 마커를 오디오 봉투 번호에 꽂으므로, 소리가 안 나간 사이 두 번 부르면
    #   자리가 같아져 뒤엣것이 앞엣것을 즉시 덮는다 — 그러면 표정이 안 보인다.
    #   ⚠ **기본 False(끔) 로 시작한다.** 씹힘을 관측한 통화(call 1252, 마커 8 → 화면 3)는
    #     `LIVE_PERSONA_INJECT=true` 구간이었고, 그때는 지시문 낭독·자문자답이 같이 나던
    #     **이미 망가진 상태**였다. 주입을 걷어낸 지금도 씹히는지 **다시 재야** 한다.
    #   ⇒ 재서 씹히면 True 로 켠다. env 한 줄이라 재빌드가 필요 없다.
    LIVE_FACE_SLOT_MERGE: bool = False
    # ⛔⛔ **`LIVE_PERSONA_INJECT` 를 되살리지 마라**(2026-09-01 제거).
    #   2.5 시절엔 «긴 지시문 + tool 을 setup 에 같이 넣으면 0/8 로 죽는다»(1011)가 있어
    #   setup 을 380자로 줄이고 나머지를 통화 중에 조각으로 밀어넣었다.
    #   ⭐ **3.1 에서는 그 제약이 없다** — 전문 9,094자 + tool 을 setup 에 넣어도
    #     안 죽는다(2026-08-27~09-01 나흘 실증, 1011 0건).
    #
    #   ⇒ 반대로 주입 쪽이 해가 됐다. 주입 시점을 **세 자리 옮겼는데 매번 다른 증상**이 났다:
    #       out_tr(비버 발화 중)   → 매 턴 interrupted, 대사가 8~19자로 토막      6/6
    #       in_tr(학습자 전사 시)  → 두 번째 턴 토막                              2/2
    #       turn_end(발화 종료 후) → 토막은 사라졌으나 **지시문 낭독 + 자문자답**
    #         자막에 `set_face(emotion='laugh')` 18건 · `만한 상황을 만들어라` 노출,
    #         비버가 자기가 시킨 것을 자기가 대답(call 1265)
    #   ⇒ 3.1 은 대화 턴으로 들어온 지시문을 **「상대가 한 말」로 취급한다.** 시점을 바꿔도
    #     안 풀린다. 지시문은 `system_instruction` 으로 들어가야 한다.
    #
    #   ⚠ 남은 대가는 **첫 인사 55% 무음**뿐이고, `_greeting_was_mute` 재시드가 막는다.
    #   ⚠ 되살리려면 위 세 실측을 먼저 반증해라 — 스위치를 남겨 두면 또 켜진다.

    # ── 클라 계측 레벨 ── "off" | "summary" | "full"
    #
    # ⭐ **주인이 서버인 이유**: 계측은 통화와 같은 소켓을 쓴다. 그게 문제를 일으켰을 때
    #   앱 재배포를 기다려야 한다면 탈출구가 없는 것이다. 여기서 "off" 로 두면 클라가
    #   `call_started.diag` 를 보고 **버퍼조차 채우지 않는다**.
    # ⚠ 구버전 앱은 이 값을 모른다 — 자기 기본값(summary)으로 돈다. 상한·전송 창은
    #   클라가 스스로 지키므로 폭주하지 않는다.
    #
    # "summary" = 뼈대만(턴·마커·언더런·타이밍). "full" = 거기에 주기 롤업·영상 내부 상태.
    # ⭐ 지금 기본이 "full" 인 것은 **의도한 것**이다 — 이 계기를 만든 이유가 「웃다가 갑자기
    #   멈춘다」이고, 그 증상은 전부 영상 내부(`vid_*`)에서 일어난다. summary 로 두면
    #   정작 보려던 것이 안 온다. 5분 통화 기준 수백 건이라 부담은 없다.
    #   조사가 끝나면 "summary" 로 내린다.
    LIVE_DIAG_LEVEL: str = "full"
    # ⭐⭐ **폭주 차단기**(2026-08-19 실측 사고). 소리 한 조각도 없이 `set_face` 가 이만큼
    #   연달아 오면 그 뒤로는 **마커를 안 보낸다**.
    #   ⛔⛔ **2026-08-20 블로킹 전환으로 이 차단기의 힘이 줄었다.** 예전엔 차단 뒤 응답을
    #     `SILENT` 로 돌려 "재개를 촉구하지 않는" 것이 루프를 실제로 끊었다. 지금은 표정
    #     응답에 scheduling 을 안 붙이므로(gemini_live.SET_FACE_TOOL 주석) 그 손잡이가
    #     없고, 남은 효과는 **마커 억제뿐**이다 — 클라는 안 흔들리지만 모델은 계속 부를 수
    #     있다.
    #     ⭐ 그래도 블로킹에선 폭주 자체가 구조적으로 어렵다: 모델이 우리 응답을 기다리므로
    #       "응답 없이 89회를 쏟아내는" 아래 경로가 성립하지 않는다. 실측으로 확인하기
    #       전까지 차단기는 **남겨 둔다.**
    #   ⛔ (아래는 NON_BLOCKING 시절의 사고 기록 — 왜 이 값이 생겼는지의 원본이다)
    #     `WHEN_IDLE` 은 "하던 일 끝나면 재개"인데 턴 사이에는 **할 일이 없어
    #     즉시 재개**한다. 그런데 모델이 재개하면서 또 `set_face` 를 부른다 ⇒ 무한 루프다.
    #     실측: 32초에 **89회**(초당 2.8회), 그동안 발화 0건. 사용자가 4번 말했는데 통화가
    #     통째로 죽어 있었다. 프롬프트로 눌러도 **모델이 지키지 않으면 그대로 재발**한다.
    #   ⚠ 3 인 이유: 정상 통화의 실측 최대는 **한 턴에 1회**였고(28호출/5세션), 연속 2회는
    #     감정이 빠르게 바뀔 때 있을 수 있다. 3부터는 정상 대화에서 설명이 안 된다.
    #   ⚠ 차단기가 걸려도 통화는 죽지 않는다 — 그 턴을 잃을 뿐이고, 다음 사용자 발화에서
    #     오디오가 흐르면 카운터가 0으로 돌아간다.
    LIVE_FACE_MAX_CONSECUTIVE: int = 3
    # ── TTS(core/tts.py `synthesize()` — 표현 오디오·발음 복습이 쓰는 합성기).
    #   "CASCADE_" 접두는 역사적 이름일 뿐, 지금은 캐스케이드와 무관하다.
    #   ⛔⛔ C14-c(2026-09-23) — CASCADE_TTS_LANGUAGE 는 삭제했다. 읽는 곳이 0건이었다
    #     (70e20e2 커밋 메시지의 "다섯 값 모두 읽힌다"는 부정확했다 — codex 발견).
    #   나머지 배치·침묵트림·OpenAI 보이스·에코 방어 등은 cascade_session.py 전용이라
    #   그 파일과 함께 삭제했다.
    #   ⛔⛔ P2-6(2026-09-24, bt-back QA) — `CASCADE_TTS_ENGINE`·`CASCADE_TTS_STYLE_
    #   PROMPT`·`CASCADE_TTS_SPEAKING_RATE` 도 같은 이유로 지웠다: 그 세 값은
    #   `core/tts.synthesize_stream`(캐스케이드 통화 중 스트리밍 합성) 전용이었는데
    #   그 함수 자체를 이번 정리에서 지웠다(호출부 0건 — grep 확인, 캐스케이드
    #   엔진 삭제로 유일한 소비처였던 cascade_session.py 가 먼저 없어졌다). 지금
    #   남은 `synthesize()`(표현 오디오·발음 복습)는 `CASCADE_TTS_GEMINI_MODEL`
    #   하나만 읽는다 — 나머지 셋은 이제 어디서도 안 읽힌다(extra="ignore" 라 Cloud
    #   Run env 에 남아 있어도 기동은 막지 않는다).
    #   ⭐⭐ 사장님 결정(2026-08-12): Gemini-TTS 로 간다("지금 좋아 잘돼"). 이 결정
    #   자체는 유효하다 — `synthesize()` 의 기본 엔진 선택(`engine=None`→Chirp3-HD,
    #   `engine="gemini-tts"`→Gemini)은 호출부(표현 추출·발음 복습)가 정한다.
    # ⭐ **Cloud TTS 의 model_name 문자열**이다(2026-08-07 공식 문서 확인). 유효값 4종:
    #     gemini-2.5-flash-tts / gemini-2.5-flash-lite-preview-tts /
    #     gemini-2.5-pro-tts / gemini-3.1-flash-tts-preview
    # ⚠ **Gemini API(ai.google.dev) 의 모델 ID 와 문자열이 다르다**(그쪽은
    #   gemini-2.5-flash-preview-tts 처럼 'preview' 위치가 다르다). 가격표를 그쪽 페이지에서
    #   읽어 왔다면 **이름을 그대로 가져다 쓰면 안 된다** — 우리는 Cloud TTS 를 호출한다.
    CASCADE_TTS_GEMINI_MODEL: str = "gemini-2.5-flash-tts"

    # Supabase (인증 주체 = GoTrue). Storage 는 GCS 로 이전 — 아래 URL/KEY 는 auth 검증용.
    SUPABASE_URL: str | None = None
    SUPABASE_SERVICE_KEY: str | None = None
    # 오디오 저장(통화 원본/표현 TTS/연습 녹음) = GCS 단일 비공개 버킷. 미설정/자격증명 부재면
    # voice_url=None(graceful). 아래 두 상수는 이제 버킷명이 아니라 **버킷 내 폴더 prefix**.
    GCS_AUDIO_BUCKET: str = "beavertalk-app-audio"       # bt-dev-web-01, asia-northeast3, 비공개
    GCS_SIGNED_URL_PUBLIC_TTL: int = 604800              # public_url 대체 signed URL 만료(7일)
    # 문장 TTS 재생 URL 만료(6시간). 매 요청 재서명하므로 길게 잡을 이유가 없다 —
    # 유출된 URL 이 살아 있는 시간을 줄인다. 캐릭터 프리뷰(PUBLIC_TTL)와 분리한다.
    GCS_SIGNED_URL_TTS_TTL: int = 21600
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
    # ⛔⛔ R3-a(2026-09-24, bt-back — 판매 개시 전 필수): 기본값을 **False 로 뒤집었다**.
    #   스텁은 서명을 안 본다 = 아무 문자열 영수증이나 통과해 premium 을 지급한다(무료
    #   기능이던 시절엔 무해했지만, 2단화로 유료 기능이 실제로 갈린 지금은 구멍이다).
    #   ⚠ ENV 가드(prod 에서만 막기)를 안 쓴 이유: 배포된 세 서비스(app-api·demo-api·
    #     test-api) 전부 `ENV=test` 다(2026-08-05 gcloud 실측, CLAUDE.md 참조) — ENV
    #     가드는 이 프로젝트에서 안 걸린다. 기본값 자체를 닫는 게 유일하게 확실하다.
    #   개발·QA 는 이 스위치를 Cloud Run 환경변수로 켜서(True) 그대로 쓴다(로컬은
    #   tests/test_iap.py 의 autouse 픽스처가 스스로 True 로 켠다 — 회귀 무영향).
    IAP_ALLOW_STUB: bool = False   # 스텁 허용(개발·QA 명시로 켜야 함). prod 는 절대 켜지 마라

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
    def _guard_reground_mode(self) -> "Settings":
        """재접지 모드 오타를 **기동 시점에** 잡는다.

        ⛔ 모르는 값을 조용히 두면 `REGROUND_MODE != "off"` 갈래가 전부 참이 되어
          「끈 줄 알았는데 도는」 상태가 된다. 그러면 대조 실험이 통째로 무의미해진다.
        """
        attach_ats = {"mic_open", "first", "final"}
        if self.LIVE_REGROUND_ATTACH_AT not in attach_ats:
            raise ValueError(
                f"LIVE_REGROUND_ATTACH_AT 는 {sorted(attach_ats)} 중 하나여야 합니다 "
                f"(받은 값: {self.LIVE_REGROUND_ATTACH_AT!r})."
            )
        transports = {"client_content", "realtime"}
        if self.LIVE_REGROUND_TRANSPORT not in transports:
            raise ValueError(
                f"LIVE_REGROUND_TRANSPORT 는 {sorted(transports)} 중 하나여야 합니다 "
                f"(받은 값: {self.LIVE_REGROUND_TRANSPORT!r})."
            )
        allowed = {"on_user_turn", "legacy_idle", "off"}
        if self.LIVE_REGROUND_MODE not in allowed:
            raise ValueError(
                f"LIVE_REGROUND_MODE 는 {sorted(allowed)} 중 하나여야 합니다 "
                f"(받은 값: {self.LIVE_REGROUND_MODE!r})."
            )
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