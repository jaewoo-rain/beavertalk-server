# 1장. FastAPI 골격 — Spring 개발자를 위한 안내서

> 📘 **이 장을 읽고 나면**
> - FastAPI의 핵심 구성요소가 Spring의 어떤 어노테이션/빈에 대응하는지 표로 매핑할 수 있습니다.
> - `create_app()` 팩토리와 `lifespan`이 어떻게 스프링의 `ApplicationContext` 부팅을 대신하는지 이해합니다.
> - 라우터가 `/api/v1` 아래 도메인별로 등록되는 방식과 표준 에러 바디 형태를 설명할 수 있습니다.
> - 도메인 수직 슬라이스(`domains/*/{models,schemas,repository,service,routers}`)와 레이어 의존 방향을 그릴 수 있습니다.

---

## 1.1 FastAPI ↔ Spring 대응표

### (1) 왜 중요한가
Spring 개발자가 FastAPI 코드를 처음 보면 "어노테이션이 어디 갔지?" 하고 당황합니다. FastAPI는 어노테이션 대신 **함수 인자와 타입 힌트**로 같은 일을 합니다. 개념 대응만 머릿속에 넣으면 코드가 갑자기 읽히기 시작합니다.

### (2) Spring 비유 — 한눈에 보는 매핑표

| FastAPI | Spring | 한 줄 설명 |
|---|---|---|
| `APIRouter(prefix=...)` | `@RestController` + `@RequestMapping` | URL 그룹을 묶는 컨트롤러 단위 |
| `@router.get/post/...` | `@GetMapping / @PostMapping` | 핸들러 메서드 매핑 |
| `Depends(...)` | `@Autowired` / 생성자 주입 | 의존성 주입(요청 스코프까지 포함) |
| Pydantic `BaseModel` | DTO + `@Valid` | 요청/응답 검증 + 직렬화 |
| `app.state` | `ApplicationContext` | 앱 수명 동안 공유되는 싱글턴 보관소 |
| pydantic-settings `Settings` | `@ConfigurationProperties` / `application.yml` | 환경변수·`.env` 바인딩 |
| Service 클래스 | `@Service` | 비즈니스 로직 + 트랜잭션 경계 |
| Repository 클래스 | `@Repository` / Spring Data JPA | 순수 DB 접근 |
| **명시적 `db.commit()`** | `@Transactional` (자동 커밋) | ⚠️ **여기가 핵심 차이** (아래 참고) |

### (3) 가장 큰 함정 미리보기 — 트랜잭션
Spring에서는 `@Transactional`이 붙은 메서드가 정상 리턴하면 프레임워크가 **자동으로 커밋**합니다. 그런데 이 프로젝트는 그렇지 않습니다. Service가 **직접 `self.db.commit()`을 호출**해야만 저장됩니다. 잊으면 "에러도 없는데 DB에 안 들어감" 현상이 생깁니다.

```python
# Spring 감각: 리턴하면 저장되겠지? → ❌ 이 프로젝트에선 저장 안 됨
def update(self, member_id, data):
    m = self.repo.get(member_id)
    m.language = data.language
    # self.db.commit()  ← 이 줄이 없으면 롤백됨(close 시 폐기)
```

### (4) 실제 코드
- 트랜잭션 경계를 Service가 명시적으로 잡는 예: [domains/account/service/member_service.py:87](../../domains/account/service/member_service.py#L87), [member_service.py:98](../../domains/account/service/member_service.py#L98)
- 커밋 전략 설명 주석: [domains/account/service/member_service.py:1](../../domains/account/service/member_service.py#L1)

### (5) 흔한 함정
- **`@Transactional` 감각으로 커밋을 생략** → 저장 안 됨. 2장에서 자세히 다룹니다.
- 어노테이션을 찾으려 하지 말 것. FastAPI는 **함수 시그니처가 곧 설정**입니다.

### (6) 한 줄 요약
FastAPI는 어노테이션 대신 타입 힌트로 Spring의 DI·검증·라우팅을 하고, **트랜잭션만은 자동이 아니라 수동**입니다.

---

## 1.2 앱 부트스트랩 — `create_app()` 팩토리와 `lifespan`

### (1) 왜 필요한가
스프링은 `SpringApplication.run()`이 컨텍스트를 띄우고 빈을 조립합니다. FastAPI에는 그런 마법이 없어서 **우리가 직접 앱을 조립하는 팩토리 함수**를 만듭니다. 팩토리로 만들면 테스트에서 다른 설정을 주입하기도 쉽습니다.

### (2) Spring 비유
- `create_app(settings)` = `@SpringBootApplication` 클래스 + 수동 빈 등록.
- `lifespan` = `ApplicationRunner` / `@PostConstruct`(시작) + `@PreDestroy`(종료)를 하나로 묶은 것.
- `app.state.engine`, `app.state.session_factory` = `ApplicationContext`에 등록된 싱글턴 빈.

### (3) 작은 코드 예시
```python
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    engine = build_engine(settings)
    app.state.engine = engine                       # 싱글턴 빈 등록
    app.state.session_factory = build_session_factory(engine)
    app.state.genai_client = _create_genai_client(settings)  # 없으면 None(graceful)
    try:
        yield          # ← 이 지점에서 앱이 요청을 받는 동안 살아있음
    finally:
        engine.dispose()   # @PreDestroy: 종료 시 커넥션 정리
```

### (4) 실제 코드
- `lifespan` 정의(엔진/세션팩토리/genai_client를 `app.state`에 보관): [main.py:102](../../main.py#L102)
- `create_app()` 팩토리(설정 주입 + 미들웨어 + 라우터 등록): [main.py:133](../../main.py#L133)
- `app.state.settings` 를 먼저 심어 lifespan이 읽게 함(이중 해석 방지): [main.py:143](../../main.py#L143)
- `app = create_app()` 모듈 레벨 인스턴스(`uvicorn main:app` 호환): [main.py:223](../../main.py#L223)

### (5) 흔한 함정
- **전역 엔진을 모듈 top에 두지 않는다.** 예전에는 그렇게 했지만, 전역을 없애고 `lifespan`에서 만들어 `app.state`에 담도록 리팩터했습니다. 배경은 [REFACTOR_LIFESPAN_PLAN.md](../../REFACTOR_LIFESPAN_PLAN.md) 참고(자세한 이유는 2장).
- `genai_client`는 실패해도 `None`을 반환합니다 → 통화 기능만 비활성, 앱은 정상 기동. "필수 빈이 없으면 컨텍스트 로딩 실패"인 스프링과 다른 graceful 전략입니다.

### (6) 한 줄 요약
`create_app()`은 수동 부팅 함수, `lifespan`은 시작/종료 훅이며 공유 자원은 전역이 아니라 `app.state`에 담습니다.

---

## 1.3 라우터 등록 · 표준 에러 · 헬스체크 · dev 전용 엔드포인트

### (1) 왜 중요한가
컨트롤러(라우터)를 한 곳에서 일관된 규칙으로 등록해야, URL 체계와 에러 응답 형식이 흐트러지지 않습니다.

### (2) Spring 비유
- `app.include_router(..., prefix="/api/v1")` = `server.servlet.context-path` 또는 컨트롤러 공통 `@RequestMapping("/api/v1")`.
- `http_exception_handler` = `@RestControllerAdvice` + `@ExceptionHandler`로 에러 바디를 통일하는 것.
- `if settings.ENV != "prod":` 로 감싼 엔드포인트 = 스프링 프로필(`@Profile("!prod")`).

### (3) 작은 코드 예시
```python
# 도메인 라우터 4개를 /api/v1 아래 등록
app.include_router(account_router,  prefix="/api/v1")
app.include_router(commerce_router, prefix="/api/v1")
app.include_router(alarm_router,    prefix="/api/v1")
app.include_router(learning_router, prefix="/api/v1")

# HTTPException(409, "이미 가입됨") → {"detail": {"code": "HTTP_409", "message": "..."}}
```

### (4) 실제 코드
- 도메인 라우터 등록(account/commerce/alarm/learning, `/api/v1` prefix): [main.py:161](../../main.py#L161)
- 표준 에러 핸들러(문자열 detail → `{code, message}` 로 래핑): [main.py:116](../../main.py#L116)
- 등록 시 사용하는 `API_PREFIX = "/api/v1"` 상수: [main.py:35](../../main.py#L35)
- `/health` 헬스체크(DB 연결은 확인 안 함): [main.py:155](../../main.py#L155)
- dev 전용 엔드포인트(`ENV != "prod"` 일 때만 노출): [main.py:168](../../main.py#L168)

### (5) 흔한 함정
- `/health`는 **DB 연결을 확인하지 않습니다.** "health 200인데 쿼리는 다 죽음"이 가능하니 진짜 준비 상태 체크와 혼동하지 마세요.
- 에러 바디는 **항상 `detail` 키 아래에 중첩**됩니다. 프런트/테스트가 `body["detail"]["message"]`를 기대하도록 맞추세요.
- dev 엔드포인트는 `openapi.json`(`/docs`)에 안 뜨도록 `include_in_schema=False`로 숨겨져 있습니다.

### (6) 한 줄 요약
라우터는 `/api/v1` 아래 도메인별로 등록되고, 모든 `HTTPException`은 `{detail:{code,message}}` 형태로 통일됩니다.

---

## 1.4 도메인 수직 슬라이스 구조와 레이어 의존 방향

### (1) 왜 필요한가
기능이 늘어날수록 "컨트롤러 폴더 / 서비스 폴더 / 리포 폴더"로 수평 분할하면 하나의 기능을 고칠 때 폴더 4~5개를 오가야 합니다. 이 프로젝트는 **도메인별 수직 슬라이스**를 씁니다. `account`, `commerce`, `alarm`, `learning` 각각이 자기만의 `models / schemas / repository / service / routers`를 갖습니다.

### (2) Spring 비유
패키지를 `com.app.controller / service / repository`(레이어 우선)로 나누는 대신, `com.app.account / commerce / learning`(도메인 우선, package-by-feature)으로 나눈 것과 같습니다. 각 도메인이 미니 모듈입니다.

### (3) 구조와 의존 방향
```
domains/
  account/   commerce/   alarm/   learning/
    models/       ← SQLAlchemy 엔티티 (@Entity)
    schemas/      ← Pydantic DTO (@Valid)
    repository/   ← DB 접근 (@Repository), commit 안 함
    service/      ← 비즈니스 로직 (@Service), db.commit() 여기서
    routers/      ← 컨트롤러 (@RestController)

  의존 방향(한 방향으로만):
  routers ──▶ service ──▶ repository ──▶ models
     └── routers 가 repository 를 직접 부르는 것은 금지 ──┘
```

### (4) 실제 코드
- 라우터가 Service만 부르고 Repository를 직접 안 부르는 예: [domains/account/routers/member.py:29](../../domains/account/routers/member.py#L29)
- 도메인 라우터 집합을 하나로 묶는 `__init__.py`(sub-router 조립): [domains/account/routers/__init__.py:1](../../domains/account/routers/__init__.py#L1), [domains/learning/routers/__init__.py:1](../../domains/learning/routers/__init__.py#L1)
- Service = 트랜잭션 경계 + repo 생성자 주입: [domains/account/service/member_service.py:32](../../domains/account/service/member_service.py#L32)
- Repository = 순수 DB 접근, "여기서는 commit 하지 않는다": [domains/account/repository/member_repository.py:1](../../domains/account/repository/member_repository.py#L1)
- DTO(Pydantic) 정의 — 엔티티를 그대로 노출하지 않음: [domains/account/schemas/member.py:1](../../domains/account/schemas/member.py#L1)
- `Depends`로 세션·인증 주체를 주입하는 배선(`DbSession`, `CurrentMember`): [core/deps.py:60](../../core/deps.py#L60)

### (5) 흔한 함정
- **라우터에서 Repository를 직접 호출하지 마세요.** 트랜잭션/도메인 규칙이 Service를 건너뛰어 커밋 누락·검증 누락이 생깁니다.
- Repository에서 `commit()` 호출 금지 — 트랜잭션 경계는 오직 Service가 관리합니다.
- 도메인 간 참조는 가능하지만(예: account service가 commerce 모델을 조회), **다른 도메인의 repository/service를 통해** 접근하는 것이 원칙입니다.

### (6) 한 줄 요약
도메인별 수직 슬라이스에서 데이터는 `routers → service → repository → models` 한 방향으로만 흐르고, 라우터의 repo 직접 호출은 금지입니다.

---

## ✍️ 스스로 점검
1. Spring의 `@Transactional` 자동 커밋과 이 프로젝트의 커밋 방식은 무엇이 다른가요? 저장이 누락되는 시나리오를 하나 말해보세요.
2. 엔진과 세션 팩토리는 왜 모듈 전역이 아니라 `lifespan`에서 만들어 `app.state`에 담을까요?
3. 라우터가 Repository를 직접 호출하면 안 되는 이유 두 가지는 무엇인가요?

⟵ [이전: 목차](README.md) ・ [📚 목차](README.md) ・ [다음: 2장. 설정과 DB 인프라](02-config-and-db.md) ⟶
