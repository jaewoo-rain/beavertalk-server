# 5장. 표준 수직 슬라이스 — account 도메인으로 배우는 정석

> 📘 **이 장을 읽고 나면**
> - BeaverTalk 의 레이어 패턴 정석(router → service → repository → model / schema)을 account 도메인 예제로 설명할 수 있어요.
> - 온보딩 요청(`POST /members/me/onboarding`) 하나가 각 레이어를 어떻게 통과하는지 코드로 따라갈 수 있어요.
> - **왜 repository 는 commit 하지 않고 service 가 트랜잭션 경계인지** 명확히 이해할 수 있어요.
> - Pydantic DTO(`from_attributes`, `field_validator`)로 ORM 엔티티를 안전하게 응답으로 바꾸는 법을 알 수 있어요.
> - Spring 의 Controller/Service/Repository/Entity/DTO 와 1:1로 매핑할 수 있어요.

---

## 레이어 패턴 정석 — account 를 견본으로

왜 이 장이 중요하냐면, account 도메인이 이 프로젝트에서 **레이어 규칙이 가장 깨끗하게 지켜진 견본**이기 때문이에요. 다른 도메인(commerce, learning …)을 짤 때 여기를 베끼면 됩니다. 각 레이어의 책임은 이렇게 나뉩니다.

```
HTTP 요청
   │
   ▼
[router]      얇게. 요청/응답 바인딩 + 의존성 주입만.        = @RestController
   │           (비즈니스 로직 없음)
   ▼
[service]     비즈니스 규칙 + 트랜잭션 경계(db.commit()).    = @Service + @Transactional
   │
   ▼
[repository]  쿼리만. commit 안 함.                          = @Repository (Spring Data JPA)
   │
   ▼
[model]       SQLAlchemy ORM 엔티티(테이블 매핑).            = @Entity
```

여기에 두 개가 옆에서 거듭니다.

- **schema** = Pydantic DTO. 입력 검증·출력 형태 정의. ORM 엔티티를 그대로 노출하지 않음. = Spring 의 Request/Response DTO.

### Spring 비유

Spring 의 3-tier 를 그대로 옮긴 구조라 낯설지 않아요. 다만 **결정적 차이 하나**가 있습니다: Spring 의 `@Transactional` 은 메서드가 끝나면 **자동 커밋**하지만, BeaverTalk 은 **service 에서 `db.commit()` 을 직접 호출**하는 명시적 커밋 전략입니다(2장 참고). 이 차이가 다음 절 전체를 관통해요.

> 한 줄 요약: account 는 router(얇게)→service(로직+커밋)→repository(쿼리만)→model 의 정석 견본입니다.

---

## 대표 워크스루 — 온보딩 요청이 레이어를 통과하는 길

`POST /members/me/onboarding` 요청 하나를 끝까지 따라가 보겠습니다. 사용자가 이름·학습이유·언어를 저장하는 온보딩이에요.

### ① router — 얇게 받아 넘기기만

```python
@router.post("/me/onboarding", response_model=MemberRead)
def onboarding(data: OnboardingIn, member: CurrentMember, db: DbSession) -> MemberRead:
    return MemberService(db).onboarding(
        member.member_id, data.name, data.reasons, data.language
    )
```

- `data: OnboardingIn` — 요청 본문을 Pydantic DTO 로 검증하며 파싱(= `@RequestBody`).
- `member: CurrentMember` — 3장의 인증 의존성. 토큰 → member 주입(= `@AuthenticationPrincipal`).
- `db: DbSession` — 요청 단위 세션 주입.
- 로직은 한 줄도 없이 service 로 위임합니다. 이게 "얇은 라우터" 예요.

실제 코드 링크:
- [domains/account/routers/member.py:28](../../domains/account/routers/member.py#L28) — `onboarding` 라우터.
- [domains/account/schemas/member.py:16](../../domains/account/schemas/member.py#L16) — `OnboardingIn` 입력 DTO.

### ② service — 비즈니스 규칙 + 트랜잭션 경계

여기가 알맹이입니다.

```python
def onboarding(self, member_id, name, reasons, language) -> Member:
    member = self.get(member_id)                       # 없으면 404
    if name is not None:     member.name = name        # 전달된 것만 반영
    if language is not None:  member.language = language
    if reasons is not None:
        codes = self._validate_reasons(reasons)        # ← 화이트리스트 검증(비즈니스 규칙)
        member.reasons = [MemberReason(reason=c) for c in codes]  # 재할당 = 옛 행 orphan 제거
    member.onboarding_completed = True
    self.db.commit()                                   # ← 트랜잭션 경계
    self.db.refresh(member)                            # DB 최신값 다시 로드
    return member
```

세 가지 정석 포인트를 보세요.

1. **비즈니스 규칙은 service 에.** `_validate_reasons()` 가 `reason` 코드를 화이트리스트(`ALLOWED_REASONS`)로 검증하고 중복을 제거합니다. 이런 "도메인 규칙" 은 라우터도 repository 도 아닌 service 의 몫이에요.
2. **`member.reasons = [...]` 재할당 = orphan 제거.** 관계에 `cascade="all, delete-orphan"` 이 걸려 있어서, 리스트를 통째로 새 것으로 바꾸면 **옛 `member_reason` 행들이 자동으로 DELETE** 됩니다. "기존 이유를 교체" 라는 요구사항이 코드 한 줄로 표현돼요(= JPA 의 `orphanRemoval = true`).
3. **`commit()` + `refresh()`.** 명시적으로 커밋하고, DB 가 채운 값(기본값·트리거 등)을 다시 읽어옵니다.

실제 코드 링크:
- [domains/account/service/member_service.py:102](../../domains/account/service/member_service.py#L102) — `onboarding()`.
- [domains/account/service/member_service.py:118](../../domains/account/service/member_service.py#L118) — `member.reasons` 재할당(orphan 제거).
- [domains/account/service/member_service.py:151](../../domains/account/service/member_service.py#L151) — `_validate_reasons()`(화이트리스트 + 중복 제거).
- [domains/account/models/member_reason.py:21](../../domains/account/models/member_reason.py#L21) — `ALLOWED_REASONS` 화이트리스트.

### ③ repository — 쿼리만, 커밋 없음

service 가 부르는 조회는 repository 로 내려갑니다.

```python
def get(self, member_id: int) -> Optional[Member]:
    return self.db.get(Member, member_id)   # PK 조회, commit 안 함
```

repository 는 **오직 쿼리**만 합니다. `add()` 조차 세션에 넣기만 하고 flush/commit 은 안 해요("flush/commit 은 service 책임" 이라고 주석에 못 박혀 있습니다).

실제 코드 링크:
- [domains/account/repository/member_repository.py:22](../../domains/account/repository/member_repository.py#L22) — `get()`.
- [domains/account/repository/member_repository.py:51](../../domains/account/repository/member_repository.py#L51) — `add()`(세션에 추가만).
- [domains/account/repository/member_repository.py:36](../../domains/account/repository/member_repository.py#L36) — `get_by_auth()`(3장 프로비저닝이 쓰는 조회).

### ④ 응답 — ORM → DTO 자동 변환

service 가 `Member`(ORM 객체)를 돌려주면, 라우터의 `response_model=MemberRead` 가 이를 DTO 로 바꿔 직렬화합니다. 자세한 건 다음 절에서.

> 한 줄 요약: 라우터는 위임만, service 가 규칙 검증·관계 재할당·commit 을 책임지고, repository 는 쿼리만 합니다.

---

## 왜 repository 는 커밋 안 하고 service 가 트랜잭션 경계인가

왜 이 규칙이 중요하냐면, **하나의 요청 = 하나의 트랜잭션** 을 지키기 위해서예요.

repository 가 각자 commit 해버리면, 한 요청 안에서 여러 쓰기를 할 때 중간에 실패해도 앞부분이 이미 커밋돼 **데이터가 반쪽만 저장**되는 사태가 납니다. 예를 들어 온보딩에서 "옛 이유 삭제 → 새 이유 추가 → 완료 플래그" 중간에 터지면, 삭제만 되고 추가는 안 된 상태로 남을 수 있어요.

그래서 규칙은 명확합니다.

- **repository**: 쿼리·`add`·`delete` 로 세션에 변경을 **쌓기만** 함. 커밋 금지.
- **service**: 하나의 논리적 작업이 다 끝난 뒤 **한 번** `db.commit()`. 실패하면 아무것도 반영 안 됨(원자성).
- **`get_db`**: 세션 생성/`close` 만 담당하고 commit 하지 않음(명시적 커밋 전략, [db/session.py:32](../../db/session.py#L32)).

### Spring 비유

Spring `@Transactional` 을 service 메서드에 붙이는 것과 목적이 똑같습니다("트랜잭션 경계는 service"). 차이는 **커밋을 누가 부르냐** 뿐이에요.

| | Spring | BeaverTalk |
|---|---|---|
| 트랜잭션 경계 | `@Transactional` 붙은 service 메서드 | `db.commit()` 부르는 service 메서드 |
| 커밋 시점 | 메서드 정상 종료 시 자동 | 코드에서 명시적으로 호출 |
| repository 커밋 | 안 함(경계는 service) | 안 함(경계는 service) |

실제 코드 링크:
- [domains/account/service/member_service.py:1](../../domains/account/service/member_service.py#L1) — "@Service + @Transactional 에 해당, 여기서 db.commit() 명시 호출" 주석.
- [domains/account/repository/member_repository.py:1](../../domains/account/repository/member_repository.py#L1) — "여기서는 commit 하지 않는다(경계는 service)" 주석.
- [db/session.py:6](../../db/session.py#L6) — 명시적 커밋 전략 설명.

### 흔한 함정

- **repository 에서 무심코 `self.db.commit()` 을 부르면** service 의 트랜잭션 원자성이 깨집니다. repository 에는 절대 commit 을 넣지 마세요.
- **service 에서 commit 을 깜빡하면** 변경이 세션에만 쌓이고 DB 엔 반영 안 된 채 `get_db` 가 close 해버립니다(조용히 롤백). "저장했는데 안 남아요" 의 흔한 원인이에요.

> 한 줄 요약: 한 요청 = 한 트랜잭션. 그래서 커밋은 오직 service 가, 그것도 작업이 다 끝난 뒤 한 번만 합니다.

---

## 모델과 DTO — 소프트 삭제, from_attributes, reasons 변환

### 모델 3형제 요지

account 도메인의 핵심 모델입니다.

- **`Member`** — 회원 본체. `member_id`(PK), `auth_user_id`(Supabase UUID, unique), `email`, `onboarding_completed`, 그리고 소프트 삭제용 `deleted_at`. 자식 관계 `reasons`, `owned_characters` 는 `cascade="all, delete-orphan"`.
- **`MemberReason`** — 회원별 학습 이유(1:N). `(member_id, reason)` UniqueConstraint 로 같은 이유 중복 저장 방지. 이유는 마스터 테이블 없이 코드 문자열로 보관.
- **`SpeakCountry`** — 억양(M:1의 부모). member 여러 명이 같은 억양 행을 참조 가능.

**소프트 삭제**: `Member.deleted_at` 이 NULL 이면 활성, 값이 있으면 탈퇴(3장 참고). `repository.list()` 가 `deleted_at IS NULL` 로 걸러 탈퇴 회원을 숨깁니다.

실제 코드 링크:
- [domains/account/models/member.py:33](../../domains/account/models/member.py#L33) — `Member` 모델.
- [domains/account/models/member.py:65](../../domains/account/models/member.py#L65) — `deleted_at`(소프트 삭제).
- [domains/account/models/member.py:84](../../domains/account/models/member.py#L84) — `reasons` 관계(`delete-orphan`).
- [domains/account/models/member_reason.py:36](../../domains/account/models/member_reason.py#L36) — `MemberReason` + UniqueConstraint.
- [domains/account/models/speak_country.py:20](../../domains/account/models/speak_country.py#L20) — `SpeakCountry`.

### DTO — `from_attributes` 로 ORM 을 응답으로

왜 DTO 가 필요하냐면, ORM 엔티티를 그대로 응답에 노출하면 내부 컬럼·관계가 새어 나가고 지연 로딩이 엉키기 때문이에요. Pydantic DTO 가 "밖으로 내보낼 모양" 을 따로 정의합니다.

```python
class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # ORM 객체 → DTO 자동 변환 허용
    member_id: int
    email: Optional[str]
    reasons: list[str] = []
    ...
    @field_validator("reasons", mode="before")
    @classmethod
    def _reason_codes(cls, v):
        # ORM 의 list[MemberReason] → list[str](코드)로 변환
        return [getattr(r, "reason", r) for r in v] if v else []
```

- `from_attributes=True` = Pydantic 이 dict 뿐 아니라 **ORM 객체의 속성**에서도 값을 읽게 해줍니다(Spring 의 `Entity → DTO` 매핑 자리).
- `reasons` 의 `field_validator` 가 **ORM 객체 리스트(`list[MemberReason]`)를 순수 코드 문자열 리스트(`list[str]`)로** 바꿔줍니다. 응답 JSON 은 `["travel", "career"]` 처럼 깔끔해져요.

실제 코드 링크:
- [domains/account/schemas/member.py:60](../../domains/account/schemas/member.py#L60) — `MemberRead` DTO.
- [domains/account/schemas/member.py:63](../../domains/account/schemas/member.py#L63) — `from_attributes=True`.
- [domains/account/schemas/member.py:77](../../domains/account/schemas/member.py#L77) — `reasons` `field_validator`(ORM→코드 변환).

### 흔한 함정

- **`from_attributes` 를 빼먹으면** `response_model` 이 ORM 객체를 못 읽어 검증 에러가 납니다. ORM 을 그대로 반환하는 DTO 엔 반드시 필요해요.
- **`field_validator` 없이 `reasons` 를 내보내면** `MemberReason` 객체가 그대로 직렬화되려다 실패합니다. "관계 객체 리스트 → 스칼라 리스트" 변환이 이 validator 의 존재 이유예요.

> 한 줄 요약: DTO 는 `from_attributes` 로 ORM 을 받아들이고, `field_validator` 로 관계 객체를 깔끔한 응답 형태로 다듬습니다.

---

## ✍️ 스스로 점검

1. 온보딩에서 "기존 학습 이유를 새 것으로 교체" 가 코드상 어떻게 구현되나요? 옛 `member_reason` 행은 누가 지우나요?
2. repository 가 절대 `db.commit()` 을 부르면 안 되는 이유를, "반쪽 저장" 시나리오로 설명해 보세요.
3. `MemberRead` 의 `from_attributes=True` 와 `reasons` `field_validator` 는 각각 무슨 문제를 해결하나요?

⟵ [이전: Alembic 스키마 마이그레이션](04-alembic-migrations.md) ・ [📚 목차](README.md) ・ [다음: 준비 중](06-next.md) ⟶
