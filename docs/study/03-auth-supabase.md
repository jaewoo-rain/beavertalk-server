# 3장. 인증 — Supabase Auth 위임

> 📘 **이 장을 읽고 나면**
> - BeaverTalk 이 자체 JWT 를 발급하지 않고 **Supabase Auth 에 인증을 위임**한다는 사실과, 왜 README/openapi 의 `/auth/signup·login` 이 구버전 잔재인지 구분할 수 있어요.
> - `Authorization: Bearer <토큰>` 이 우리 서버에 도착한 뒤 `verify_token()` → `get_current_member()` → `find_or_create_by_auth()` 로 흐르는 경로를 그림처럼 떠올릴 수 있어요.
> - "최초 접속 시 회원 자동 프로비저닝"(별도 signup 엔드포인트 없음)이 어떤 코드로 일어나는지 설명할 수 있어요.
> - Spring 의 OAuth2 Resource Server + JWT introspection 과 1:1로 매핑해서 이해할 수 있어요.
> - 회원 탈퇴가 왜 "Supabase 먼저 삭제 → 로컬 소프트 삭제" 순서인지, 그리고 아직 남아 있는 인증 관련 잔재(bcrypt/pyjwt/smoke_common)를 알아챌 수 있어요.

---

## ⚠️ 먼저 정정하고 갑니다 — "자체 JWT" 는 옛날 이야기

프로젝트를 처음 열면 헷갈리는 지점이 하나 있어요.

- `README.md` 에는 "자체 JWT 인증" 이라고 적혀 있고,
- `openapi.json` 에는 `/api/v1/auth/signup`, `/api/v1/auth/login` 같은 엔드포인트가 보입니다.

**둘 다 구버전 잔재(드리프트)입니다.** 실제 현재 코드는 우리가 직접 비밀번호를 받거나 JWT 를 발급하지 않아요. 인증은 통째로 **Supabase Auth(GoTrue)** 에 맡깁니다. 문서와 코드가 어긋나 있을 뿐, "진실은 코드" 라는 원칙으로 코드를 따라가면 됩니다.

Spring 으로 비유하면 이런 전환이에요.

- **옛날(README 가 말하는 것)**: 우리가 `UserDetailsService` + `PasswordEncoder` 로 로그인 처리하고, 우리 서버가 JWT 를 서명·발급하는 **Authorization Server** 역할.
- **지금(실제 코드)**: 우리는 로그인/발급을 안 하고, 외부 IdP(Supabase)가 발급한 토큰을 **검증만** 하는 **OAuth2 Resource Server** 역할.

> 한 줄 요약: README/openapi 의 signup·login 은 무시하세요. 실제 인증 주체는 Supabase 입니다.

---

## 실제 인증 흐름 — 토큰 한 개가 지나가는 길

왜 이렇게 하냐면, 비밀번호 저장·소셜 로그인·비밀번호 재설정·이메일 인증 같은 "귀찮고 위험한" 일을 전부 Supabase 에 떠넘길 수 있기 때문이에요. 우리 서버는 "이 토큰이 진짜냐?" 만 물어보면 됩니다.

전체 그림은 이렇습니다.

```
[Flutter 앱]
   │  1) supabase SDK 로 가입/로그인 (우리 서버 안 거침)
   ▼
[Supabase Auth] ── 2) access token(JWT) 발급 ──▶ [앱이 토큰 보관]
                                                     │
   3) Authorization: Bearer <JWT> 로 우리 API 호출    │
   ▼                                                 ▼
[우리 FastAPI]
   4) verify_token(token): client.auth.get_user(token)  ← Supabase 에 검증 위임
   5) get_current_member(): 토큰의 uuid 로 member 찾기, 없으면 자동 생성
   ▼
[핸들러 실행]
```

핵심은 **4번**입니다. 우리 코드는 JWT 를 직접 열어보지(decode) 않아요. 대신 Supabase 에 "이 토큰 주인이 누구냐" 를 물어봅니다.

### Spring 비유: JWT introspection(자체 검증 대신 외부에 물어보기)

Spring Security 로 옮기면 이렇게 됩니다.

```yaml
# 우리가 지금 하는 것과 같은 개념 (opaque token introspection)
spring:
  security:
    oauth2:
      resourceserver:
        opaque-token:
          introspection-uri: https://<supabase>/auth/v1/...  # 외부 IdP 에 "이 토큰 유효?" 질의
```

- Spring 의 `opaque-token`(introspection) 방식 = 우리의 `client.auth.get_user(token)`. **서명 키를 우리가 안 갖고 있어도 됩니다.** 검증은 발급자(Supabase)가 합니다.
- 반대로 `jwt.jwk-set-uri` 로 우리가 직접 서명 검증하는 방식과는 다릅니다. BeaverTalk 은 "직접 decode 안 함" 을 의도적으로 택했어요(서명 방식·키 교체에 무관해지려고).

### 작은 코드 예시

```python
def verify_token(token: str) -> Optional[AuthUser]:
    client = storage._get_client()          # service_role 클라이언트
    resp = client.auth.get_user(token)      # ← Supabase 에 검증 위임 (우리가 decode 안 함)
    user = getattr(resp, "user", None)
    return AuthUser(uid=str(user.id), email=getattr(user, "email", None))
```

실제 코드 링크:
- [core/supabase_auth.py:28](../../core/supabase_auth.py#L28) — `verify_token()`, `client.auth.get_user(token)` 로 위임하는 자리.
- [core/deps.py:34](../../core/deps.py#L34) — `get_current_member()`, Bearer 를 받아 검증 → member 반환.
- [core/deps.py:45](../../core/deps.py#L45) — `auth_user = verify_token(...)` 호출 지점.

### 흔한 함정

- **"JWT 라이브러리로 직접 열어보면 되지 않나?" (X)** 이 프로젝트는 일부러 decode 하지 않습니다. 직접 열면 서명 키 관리·키 교체(rotation) 대응을 우리가 떠안게 돼요. `get_user()` 위임이 그 부담을 없앱니다.
- **검증 실패 시 예외를 던지지 않고 `None` 을 돌려줍니다.** 토큰 무효·네트워크 오류·Supabase 미설정 모두 `None` 으로 뭉뚱그리고, 401 매핑은 호출부([core/deps.py:43](../../core/deps.py#L43))가 합니다. 로그만 남기고 조용히 실패하는 설계예요.

> 한 줄 요약: 우리는 JWT 를 decode 하지 않고 Supabase 에 검증을 위임합니다(= Spring opaque-token introspection).

---

## 최초 접속 시 회원 자동 프로비저닝 — signup 이 없는 이유

왜 필요하냐면, 인증은 Supabase 가 다 끝냈는데 우리 DB 의 `member` 행은 아직 없기 때문이에요. "언제 우리 회원 레코드를 만들지?" 라는 문제가 생깁니다. BeaverTalk 은 **별도 signup 엔드포인트를 두지 않고**, 인증된 사용자가 **처음 API 를 호출하는 그 순간** member 를 자동으로 만듭니다(lazy provisioning).

### Spring 비유

Spring 에서 소셜 로그인 시 `OAuth2UserService` 안에서 "우리 DB 에 이 사용자가 있으면 로드, 없으면 INSERT" 하는 패턴과 똑같아요. 인증 성공 직후, 애플리케이션 사용자 레코드를 지연 생성하는 것이죠.

### 작은 코드 예시

```python
def find_or_create_by_auth(self, auth_user_id: str, email: Optional[str]) -> Member:
    member = self.repo.get_by_auth(auth_user_id)     # 이미 있으면 그대로
    if member is not None:
        ... # 이메일 바뀌었으면 동기화
        return member
    character_id, owned = self._resolve_default_character(None)  # 무료 기본 캐릭터 지급
    member = Member(auth_user_id=auth_user_id, email=email,
                    character_id=character_id, owned_characters=owned)
    self.repo.add(member); self.db.commit(); self.db.refresh(member)
    return member
```

신규 회원이면 첫 무료 캐릭터(price 0)를 자동 보유시키고 `onboarding_completed=False` 로 시작합니다. 즉 "가입" 이라는 단계가 코드상 존재하지 않고, `GET /members/me` 한 번이 곧 가입입니다.

실제 코드 링크:
- [domains/account/service/member_service.py:75](../../domains/account/service/member_service.py#L75) — `find_or_create_by_auth()`.
- [domains/account/service/member_service.py:90](../../domains/account/service/member_service.py#L90) — 무료 기본 캐릭터 지급 + member 생성.
- [domains/account/service/member_service.py:124](../../domains/account/service/member_service.py#L124) — `_resolve_default_character()`(스타터 캐릭터 결정).

### 흔한 함정

- **`account` 라우터에 auth 라우터가 없습니다.** `domains/account/routers/` 에는 `member.py` 하나뿐이고, `signup`/`login` 엔드포인트는 없어요. 인증이 필요한 엔드포인트는 전부 `CurrentMember` 의존성으로 표시합니다. openapi 의 auth 경로를 찾다가 코드에서 못 찾아 당황하지 마세요.
- **이메일 동기화 부작용.** Supabase 에서 이메일을 바꾸면 다음 요청 때 `member.email` 이 조용히 갱신되고 `commit` 됩니다([member_service.py:85](../../domains/account/service/member_service.py#L85)). "읽기인 줄 알았는데 쓰기가 일어난다" 는 점을 기억하세요.

실제 코드 링크(라우터 쪽):
- [domains/account/routers/member.py:19](../../domains/account/routers/member.py#L19) — `prefix="/members"`, auth 라우터 없음.
- [domains/account/routers/member.py:22](../../domains/account/routers/member.py#L22) — `GET /members/me` 가 `CurrentMember` 로 인증 요구.
- [core/deps.py:60](../../core/deps.py#L60) — `CurrentMember = Annotated[Member, Depends(get_current_member)]`(= Spring `@AuthenticationPrincipal`).

> 한 줄 요약: 별도 회원가입 API 가 없습니다. 첫 인증 요청이 member 를 자동 생성(+ 무료 캐릭터)합니다.

---

## 회원 탈퇴 — Supabase 먼저, 그다음 소프트 삭제

왜 순서가 중요하냐면, 순서를 바꾸면 **삭제한 계정이 부활**할 수 있기 때문이에요. 우리 DB 만 지우고 Supabase 인증 주체(auth.users)를 남겨두면, 앱에 남아 있던 토큰으로 다시 요청이 올 때 위의 `find_or_create_by_auth()` 가 member 를 다시 만들어 버립니다.

그래서 탈퇴 로직은 이렇게 방어합니다.

1. **Supabase auth 사용자를 먼저 삭제**(admin API). 실패하면(미설정·네트워크·권한) **탈퇴 자체를 502 로 중단** — 로컬만 바뀌는 불일치를 막습니다.
2. 성공하면 로컬 `member` 는 **하드 삭제하지 않고** `deleted_at` 만 찍는 **소프트 삭제**. 학습·구독 등 데이터는 보존합니다.
3. 대신 유니크 키인 `email`, `auth_user_id` 를 **NULL 로 비웁니다.** → (a) 같은 이메일 재가입 가능, (b) 남은 토큰으로 이 행을 다시 못 찾아 부활 불가.

### 작은 코드 예시

```python
def delete(self, member_id: int) -> None:
    member = self.get(member_id)
    if member.auth_user_id and not delete_auth_user(member.auth_user_id):
        raise HTTPException(502, "인증 서버에서 계정을 삭제하지 못했습니다. ...")  # 부활 방지
    member.deleted_at = datetime.now(timezone.utc)
    member.email = None
    member.auth_user_id = None
    self.db.commit()
```

실제 코드 링크:
- [domains/account/service/member_service.py:175](../../domains/account/service/member_service.py#L175) — `delete()`, 순서·소프트 삭제 전체.
- [core/supabase_auth.py:47](../../core/supabase_auth.py#L47) — `delete_auth_user()`(admin `delete_user`).
- [domains/account/routers/member.py:47](../../domains/account/routers/member.py#L47) — `DELETE /members/me` (204).

### 흔한 함정

- **소프트 삭제 회원은 목록에서 빠집니다.** `MemberRepository.list()` 가 `deleted_at IS NULL` 로 거릅니다([member_repository.py:41](../../domains/account/repository/member_repository.py#L41)). 탈퇴 후 "회원이 사라졌다" 가 아니라 "숨겨졌다" 입니다.
- **Supabase 미설정 개발 환경에서는 탈퇴가 502 로 막힙니다.** `auth_user_id` 가 있는데 admin 삭제가 안 되면 중단하도록 설계돼 있어요. 로컬에서 테스트할 땐 이 점을 감안하세요.

> 한 줄 요약: 탈퇴는 "Supabase 삭제 성공" 을 전제로만 진행하고, 로컬은 재식별 키를 끊은 소프트 삭제로 부활을 막습니다.

---

## 남아 있는 잔재(정직하게 기록) — 아직 안 치운 인증 흔적

문서만 어긋난 게 아니라, 코드/의존성에도 옛 자체-JWT 시절의 흔적이 남아 있습니다. 온보딩하는 사람이 "이거 쓰이나?" 하고 헤매지 않도록 정직하게 적어둡니다.

- **`requirements.txt` 의 `bcrypt`, `pyjwt[crypto]` 는 현재 안 쓰입니다.** 비밀번호 해싱·자체 JWT 발급이 사라졌으니 죽은 의존성이에요.
  - [requirements.txt:9](../../requirements.txt#L9) — `bcrypt`, [requirements.txt:10](../../requirements.txt#L10) — `pyjwt[crypto]`.
- **`core/config.py` 의 `JWT_SECRET`/`JWT_ALGORITHM` 등도 잔재입니다.** 우리가 서명을 안 하므로 실제 인증에 쓰이지 않아요([core/config.py:37](../../core/config.py#L37) 부근).
- **`scripts/smoke_common.py` 는 지금 깨져 있습니다.** 이미 삭제된 `core.security` 를 import 하려고 해서 실행하면 `ImportError` 가 납니다.
  - [scripts/smoke_common.py:13](../../scripts/smoke_common.py#L13) — `from core.security import ...`(존재하지 않는 모듈).
- **`core/deps.py` 의 주석도 옛말입니다.** "순수 암호화/토큰은 core/security.py" 라고 적혀 있지만 그 파일은 없습니다([core/deps.py:8](../../core/deps.py#L8)).

> 한 줄 요약: bcrypt/pyjwt/JWT_SECRET/smoke_common 은 자체-JWT 시절 잔재입니다 — 쓰이지 않거나(깨져 있으니) 손대지 마세요.

---

## ✍️ 스스로 점검

1. 우리 서버는 들어온 JWT 를 직접 decode 하나요, 아니면 어디에 검증을 위임하나요? 그 코드 한 줄은 무엇인가요?
2. `POST /auth/signup` 같은 엔드포인트가 없는데도 신규 사용자의 `member` 행은 언제·어느 함수에서 만들어지나요?
3. 회원 탈퇴에서 "Supabase 삭제" 를 "로컬 소프트 삭제" 보다 먼저 하고, 실패 시 중단하는 이유는 무엇인가요?

⟵ [이전: 요청 라이프사이클과 DB 세션](02-lifecycle-db-session.md) ・ [📚 목차](README.md) ・ [다음: Alembic 스키마 마이그레이션](04-alembic-migrations.md) ⟶
