# 회원가입 이메일 인증 제거 플랜

> 목적: 회원가입 시 이메일 인증(코드 발송→확인→소비)을 더 이상 요구하지 않는다.
> 가입은 **이메일 + 비밀번호만으로 즉시 완료**. 가입 전용 인증 코드 경로는 **완전 제거**한다.
> 비밀번호 재설정용 인증(PURPOSE_PWRESET)은 **그대로 유지**한다.

- 작성: 2026-06-24 20:03
- 상태: **승인 대기**
- 범위 결정: 가입 전용 인증 엔드포인트/코드/스키마/상수/테스트/스모크까지 **완전 제거**

---

## 1. 현재 흐름 → 변경 후 흐름

| | 현재 | 변경 후 |
|---|---|---|
| 가입 단계 | `POST /auth/email/send-code` → `POST /auth/email/verify-code` → `POST /auth/signup` | `POST /auth/signup` **단독** |
| 가입 게이트 | `MemberService.create()`가 `consume_verified(email, SIGNUP)` 실패 시 400 | 게이트 없음 — 이메일 중복(409)만 검사 후 즉시 생성 |
| 이메일 중복확인 | `GET /auth/email/available` | **유지** (인증과 무관) |
| 비밀번호 재설정 | send_reset_code → verify_code → confirm | **유지(무변경)** |

---

## 2. 변경 항목 (코드)

### ① `domains/account/service/member_service.py`
- `create()`에서 이메일 인증 소비 게이트 **삭제** (현재 89~95행):
  ```python
  # 삭제 대상
  if not EmailVerificationService(self.db).consume_verified(data.email, PURPOSE_SIGNUP):
      raise HTTPException(400, "이메일 인증이 필요합니다.")
  ```
- import 정리: `PURPOSE_SIGNUP` 제거 (→ `PURPOSE_PWRESET`만 유지).
- **주의:** `EmailVerificationService` import는 **유지** — `request_password_reset()`/`confirm_password_reset()`에서 계속 사용.

### ② `domains/account/routers/auth.py`
- 엔드포인트 **삭제**: `POST /auth/email/send-code`(`send_signup_code`), `POST /auth/email/verify-code`(`verify_signup_code`) (현재 42~53행).
- import 정리 **삭제**: `EmailSendCode`, `EmailVerifyCode`, `PURPOSE_SIGNUP`, `EmailVerificationService`.
  - `EmailVerificationService`는 auth.py에선 이 두 엔드포인트에서만 쓰임(비번재설정은 `MemberService` 경유) → 제거 안전.
- **유지**: `GET /auth/email/available`(`EmailAvailable`), `signup`, `login`, `social`, `password-reset/*`.

### ③ `domains/account/service/email_verification_service.py`
- `send_signup_code()` 메서드 **삭제** (현재 55~59행).
- import 정리: `PURPOSE_SIGNUP` 제거 (→ `PURPOSE_PWRESET`만 유지).
- **유지**: `_issue`, `verify_code`, `send_reset_code`, `consume_verified`, `generate_code` (모두 비번재설정에서 사용).

### ④ `domains/account/schemas/member.py`
- 스키마 **삭제**: `EmailSendCode`, `EmailVerifyCode` (현재 34~44행).
- `MemberCreate` docstring에서 "가입 전 이메일 인증이 완료돼 있어야 한다" 문구 **삭제**.
- **유지**: `EmailAvailable`.

### ⑤ `domains/account/models/email_verification.py`
- 상수 `PURPOSE_SIGNUP` **삭제** (현재 22행). `PURPOSE_PWRESET`만 유지.
- **DB 스키마 변경 없음** — `email_verification` 테이블·`purpose` 컬럼은 그대로(비번재설정이 사용). **Alembic 마이그레이션 불필요.**

---

## 3. 변경 항목 (테스트 / 스모크)

### ⑥ `tests/test_email_verification.py`  ⚠️ 재작성 필요
- 현재 5개 테스트가 `send_signup_code` + `PURPOSE_SIGNUP`으로 **인증 엔진(만료·시도제한·소비)을 검증** 중.
- 엔진 자체는 비번재설정용으로 살아있으므로, **테스트를 `send_reset_code` + `PURPOSE_PWRESET` 경로로 전환**해 동일 커버리지(코드 일치/만료/시도초과/소비 1회용)를 유지.
  - 단 `send_reset_code`는 "가입된 비번 회원"에게만 발송 → 픽스처에서 `Member`(password 보유) 1건 선생성 필요.
  - 또는 `_issue`/`verify_code`/`consume_verified`를 `PURPOSE_PWRESET`로 직접 호출해 단위 검증.

### ⑦ 스모크 스크립트 — 가입 단계에서 인증 호출 제거
| 파일 | 변경 |
|---|---|
| `scripts/smoke_signup_helper.py` | send-code/verify-code 2단계 제거 → `signup`만 호출 |
| `scripts/smoke_account_api.py` | `_verify_email` 헬퍼 제거, "인증 없이 가입 400"·인증 단계·"이미 가입 send-code 409" 케이스 제거 |
| `scripts/smoke_auth_api.py` | send-code/verify-code(61~67행) 제거 → `signup` 직접 |
| `scripts/smoke_live.py` | send-code/verify-code(32~35행) 제거 → `signup` 직접 |

---

## 4. 변경 없음 (확인)
- 비밀번호 재설정 전체 흐름(`request`/`confirm`, `send_reset_code`, `verify_code`, `consume_verified`).
- `email_verification` 테이블 / Alembic (마이그레이션 불필요).
- `GET /auth/email/available` 중복확인.
- 온보딩(`onboarding`), 소셜 로그인, 로그인.

---

## 5. 실행 순서
1. service/router/schema/model에서 가입 인증 코드 제거 (②①④⑤③ 순서로 — import 깨짐 방지 위해 사용처부터)
2. `tests/test_email_verification.py` 재작성(PWRESET 경로)
3. 스모크 4종 수정
4. 검증:
   - `python -c "import main"` (import 무결성)
   - `pytest` (회귀 — 특히 재작성한 인증 테스트)
   - `scripts/smoke_auth_api.py`, `scripts/smoke_account_api.py` (sqlite override, 가입이 인증 없이 통과하는지)

---

## 6. 리스크 / 주의
| 리스크 | 대응 |
|---|---|
| `EmailVerificationService`/`PURPOSE_PWRESET`를 실수로 같이 제거 | 비번재설정에서 사용 — **유지 대상** 명시(②①③) |
| 인증 테스트 재작성 시 커버리지 누락 | 만료·시도초과·1회용 소비 케이스를 PWRESET 경로로 1:1 이전 |
| 프론트/문서가 send-code·verify-code 호출 | 프론트(`front/`) 가입 플로우 점검 필요(별도). API 문서(API.md 등)에서 해당 엔드포인트 언급 시 갱신 |
| 기존 DB의 잔존 signup 인증 행 | 무해(가입에서 더 이상 참조 안 함). 정리는 선택 |

---

## 7. 후속(이번 범위 밖)
- 프론트엔드 가입 화면에서 인증 단계 UI 제거.
- API 문서(`API.md`/`API_DESIGN.md`)에 인증 엔드포인트가 기재돼 있으면 갱신.
