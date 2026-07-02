# 7장. commerce 도메인 — 캐릭터·구매·결제·구독

> 📘 **이 장을 읽고 나면**
> - commerce 도메인의 6개 모델(Character / Voice / MemberCharacter / Payment / Subscribe / DiscountEvent)이 각각 무슨 역할인지 설명할 수 있어요.
> - 캐릭터 구매가 왜 **한 트랜잭션**으로 소유+결제를 묶는지, 그리고 **가격을 왜 서버가 계산**하는지(클라 조작 방지) 이해할 수 있어요.
> - 결제 이력의 `size+1` 페이지네이션과 이달 합계, 구독의 소프트 취소 패턴을 코드로 따라갈 수 있어요.
> - `selectinload`와 "소유 id 집합 단일 쿼리"로 N+1을 어떻게 막는지 알 수 있어요.
> - Repository → Service → Router 3계층이 JPA/Spring의 Repository → Service → Controller와 어떻게 대응되는지 감을 잡을 수 있어요.

---

## 7.1 도메인 개요 — 무엇을 파는가

commerce는 "회화 캐릭터를 팔고, 결제/구독을 기록하는" 도메인입니다. 모델은 [domains/commerce/models/](../../domains/commerce/models/)에 있습니다.

| 모델 | 역할 | 핵심 포인트 |
|---|---|---|
| **Character** | 회화 페르소나(통화 상대) | `price`(Numeric), `voice_id`로 음성 연결, 마스터 데이터 |
| **Voice** | Gemini Live 음성 카탈로그 | `name` UNIQUE(프리빌트 30종), 캐릭터와 N:1 |
| **MemberCharacter** | 소유(구매) 조인 | 복합 PK, `purchase_price` **스냅샷** |
| **Payment** | 결제 로그 | `category = "character" \| "subscribe"` 로 공용 |
| **Subscribe** | 구독 | `is_activate=False` 로 **소프트 취소** |
| **DiscountEvent** | 기간 할인 | 캐릭터당 N개, 활성+기간 내 1건이 유효가 |

> **JPA/Spring 비유**: `models/`가 `@Entity`, `repository/`가 `JpaRepository`, `service/`가 `@Service`, `routers/`가 `@RestController`입니다. 계층 이름만 다르고 책임 분리는 똑같아요.

**Character** — 페르소나. 역할/성격/규칙으로 프롬프트를 만들고, 실시간 통화 음성은 `Voice`를 참조합니다. `price`를 들고 있는 상품이기도 합니다. 실제 코드: [character.py:23](../../domains/commerce/models/character.py#L23).

**Voice** — Gemini Live 프리빌트 음성 목록(고정). `name`이 유니크 식별자입니다. 실제 코드: [voice.py:21](../../domains/commerce/models/voice.py#L21).

**MemberCharacter** — "누가 어떤 캐릭터를 샀는가". 복합 PK로 중복 구매를 막고, **구매 당시 가격을 `purchase_price`에 스냅샷**으로 남깁니다(나중에 정가가 바뀌어도 구매 기록은 그대로). 실제 코드: [member_character.py:23](../../domains/commerce/models/member_character.py#L23).

**Payment** — 결제 한 건. `category`로 캐릭터 구매인지 구독 결제인지 구분해 **한 테이블을 공용**합니다. 실제 코드: [payment.py:18](../../domains/commerce/models/payment.py#L18).

**Subscribe** — 구독. 취소해도 행을 지우지 않고 `is_activate=False`로 **소프트 취소**해 이력을 보존합니다. 실제 코드: [subscribe.py:18](../../domains/commerce/models/subscribe.py#L18).

**DiscountEvent** — 캐릭터별 기간 할인. `activate`이고 지금이 `start_time~end_time` 사이면 유효합니다. 실제 코드: [discount_event.py:18](../../domains/commerce/models/discount_event.py#L18).

한 줄 요약: **Character(상품) + Voice(음성) + MemberCharacter(소유) + Payment(결제) + Subscribe(구독) + DiscountEvent(할인), 여섯 조각이 commerce 전부입니다.**

---

## 7.2 구매 흐름 — 한 트랜잭션 + 서버측 가격

**왜 중요한가**: 구매는 "소유 기록(MemberCharacter)"과 "결제 기록(Payment)"이 **둘 다 생기거나 둘 다 안 생겨야** 합니다. 하나만 생기면 "돈은 냈는데 캐릭터가 없음" 같은 사고가 납니다. 또 가격을 클라이언트가 보내는 대로 믿으면 조작당합니다.

**JPA/Spring 비유**: Spring의 `@Transactional` 메서드 안에서 두 엔티티를 `save`하고 커밋하는 것과 같아요. 여기서는 데코레이터 대신 서비스가 직접 `self.db.commit()`을 호출해 트랜잭션 경계를 만듭니다.

전체 로직: [domains/commerce/service/purchase_service.py:37](../../domains/commerce/service/purchase_service.py#L37)

### 1) 캐릭터 조회 + 존재 확인

```python
character = self.char_repo.get(character_id)
if character is None:
    raise HTTPException(status.HTTP_404_NOT_FOUND, "캐릭터를 찾을 수 없습니다.")
```
[purchase_service.py:40](../../domains/commerce/service/purchase_service.py#L40)

### 2) 중복 구매 방지 (복합 PK + 사전 체크 409)

```python
if self.mc_repo.get(member_id, character_id) is not None:
    raise HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"code": "ALREADY_OWNED", "message": "이미 보유한 캐릭터입니다."},
    )
```
[purchase_service.py:45](../../domains/commerce/service/purchase_service.py#L45)

복합 PK가 **최종 방어선**(중복이면 INSERT가 PK 충돌로 실패)이지만, 그 전에 친절한 409를 먼저 돌려줍니다. "DB 예외가 500으로 새어 나가기 전에 도메인 언어(`ALREADY_OWNED`)로 막는다"는 감각이에요.

### 3) 서버측 유효 가격 계산 (클라 조작 방지)

```python
price = self.char_service.effective_price(character)  # 서버가 가격 결정
```
[purchase_service.py:51](../../domains/commerce/service/purchase_service.py#L51)

`effective_price`는 **활성 할인이 있으면 할인가, 없으면 정가**를 돌려줍니다. 클라가 보낸 금액은 쓰지 않아요.

```python
def active_discount(self, character):
    now = datetime.now(timezone.utc)
    for d in character.discount_events:
        if d.activate and d.discount_price is not None \
           and d.start_time is not None and d.end_time is not None \
           and _as_utc(d.start_time) <= now <= _as_utc(d.end_time):
            return d
    return None
```
[character_service.py:84](../../domains/commerce/service/character_service.py#L84), `effective_price`는 [character_service.py:98](../../domains/commerce/service/character_service.py#L98)

> **함정 주의**: Postgres는 timezone-aware datetime을 주지만 SQLite는 naive로 줄 수 있어서, `_as_utc()`로 통일한 뒤 비교합니다([character_service.py:29](../../domains/commerce/service/character_service.py#L29)). aware-vs-naive 비교는 `TypeError`를 냅니다.

### 4) MemberCharacter + Payment 를 한 트랜잭션으로 원자 생성

```python
mc = MemberCharacter(member_id=member_id, character_id=character_id,
                     purchase_price=price, purchase_date=now)
payment = Payment(member_id=member_id, price=price, payment_date=now,
                  description=f"캐릭터 구매: {character.name}",
                  category="character", card_info=card_info)
self.mc_repo.add(mc)
self.payment_repo.add(payment)
self.db.commit()   # ← 둘을 한 트랜잭션으로. 중간 실패 시 전부 롤백
```
[purchase_service.py:54](../../domains/commerce/service/purchase_service.py#L54)

`add()`는 세션에 담기만 하고, **`commit()` 한 번**이 둘을 함께 확정합니다. 중간에 실패하면 둘 다 롤백됩니다. 구매 시점의 `price`를 `purchase_price`에 넣어 스냅샷을 남기는 것도 여기서 일어나요.

**흔한 함정**: repository의 `add()` 안에서 커밋하지 않는 것이 중요합니다. 커밋을 **서비스가 소유**해야 여러 repository 호출을 한 트랜잭션으로 묶을 수 있어요(레포마다 커밋하면 원자성이 깨집니다).

한 줄 요약: **조회 → 중복 409 → 서버가 가격 계산 → 소유+결제를 `commit()` 한 번으로 원자 생성.**

---

## 7.3 결제 이력 — 필터 + size+1 페이지네이션 + 이달 합계

로직: [domains/commerce/service/payment_service.py:18](../../domains/commerce/service/payment_service.py#L18)

```python
category = None if type_ == "all" else type_          # subscribe/character 필터
offset = (page - 1) * size
rows = self.repo.list_by_member(member_id, category, limit=size + 1, offset=offset)
has_more = len(rows) > size                            # 한 개 더 받아서 다음 페이지 유무 판단
items = [PaymentItem.model_validate(p) for p in rows[:size]]
```
[payment_service.py:21](../../domains/commerce/service/payment_service.py#L21)

**`size+1` 트릭**: 페이지 크기가 10이면 **11개를 요청**해서, 11개가 오면 "다음 페이지 있음(`has_more=True`)"으로 판단하고 실제로는 10개만 보여줍니다. 총 개수를 세는 `COUNT(*)` 쿼리 없이 "더 있나?"만 알아내는 값싼 방법이에요.

**이달 합계**는 별도 집계 쿼리:
```python
stmt = select(func.coalesce(func.sum(Payment.price), 0)).where(
    Payment.member_id == member_id, Payment.payment_date >= since)
```
[payment_repository.py:40](../../domains/commerce/repository/payment_repository.py#L40)

정렬은 `payment_date DESC NULLS LAST, payment_id DESC`로 최신 우선 + 날짜 없는 행은 뒤로 밀립니다([payment_repository.py:34](../../domains/commerce/repository/payment_repository.py#L34)).

> **JPA 비유**: Spring Data의 `Pageable`을 쓰지 않고 손으로 `limit/offset`을 다루는 방식이에요. `func.sum`/`func.coalesce`는 JPQL의 `SUM` + `COALESCE`와 같습니다.

한 줄 요약: **`size+1`로 다음 페이지 유무를 싸게 판단하고, 이달 합계는 `SUM` 집계 쿼리로 따로 구합니다.**

---

## 7.4 구독 — start(원자) / list / cancel(소프트)

로직: [domains/commerce/service/subscription_service.py:23](../../domains/commerce/service/subscription_service.py#L23)

**start** — 구매와 같은 패턴입니다. Subscribe + Payment(`category="subscribe"`)를 만들고 `commit()` 한 번으로 원자 생성:
```python
self.repo.add(sub)
self.payment_repo.add(payment)
self.db.commit()   # 구독 + 결제 한 트랜잭션
```
[subscription_service.py:40](../../domains/commerce/service/subscription_service.py#L40)

**cancel** — 행을 지우지 않고 `is_activate`만 끕니다:
```python
sub.is_activate = False   # 삭제 아님 — 이력 보존
self.db.commit()
```
[subscription_service.py:53](../../domains/commerce/service/subscription_service.py#L53)

> **JPA 비유**: 조회한 엔티티의 필드만 바꾸고 커밋하는 **dirty checking**(변경 감지)과 동일합니다. SQLAlchemy도 세션이 추적 중인 객체의 변경을 커밋 시 자동 `UPDATE`로 반영해요. 소프트 취소는 6장의 소프트 삭제와 같은 철학입니다(이력 보존).

**cancel의 소유권 체크**: `sub.member_id != member_id`면 404를 던져 남의 구독을 못 건드리게 합니다([subscription_service.py:51](../../domains/commerce/service/subscription_service.py#L51)).

한 줄 요약: **start는 원자 생성, cancel은 `is_activate=False` 소프트 취소(변경 감지로 UPDATE).**

---

## 7.5 엔드포인트 표

라우터 3개는 `domains/commerce/routers/__init__.py`에서 묶여 `main.py`에서 `/api/v1` 프리픽스로 등록됩니다. 아래 경로는 **`/api/v1` + 라우터 프리픽스 + 라우트**를 합친 최종 경로입니다.

| 메서드 | 경로 | 설명 | 코드 |
|---|---|---|---|
| GET | `/api/v1/characters` | 캐릭터 목록(소유여부·유효가 포함) | [character.py:20](../../domains/commerce/routers/character.py#L20) |
| GET | `/api/v1/characters/{character_id}` | 캐릭터 상세(활성 할인 포함) | [character.py:27](../../domains/commerce/routers/character.py#L27) |
| POST | `/api/v1/characters/{character_id}/purchase` | 캐릭터 구매(201) | [character.py:34](../../domains/commerce/routers/character.py#L34) |
| GET | `/api/v1/members/me/characters` | 내가 소유한 캐릭터 | [character.py:49](../../domains/commerce/routers/character.py#L49) |
| GET | `/api/v1/payments` | 결제 이력(`type` 탭, `page`) | [payment.py:14](../../domains/commerce/routers/payment.py#L14) |
| POST | `/api/v1/subscriptions` | 구독 시작(201) | [subscription.py:14](../../domains/commerce/routers/subscription.py#L14) |
| GET | `/api/v1/subscriptions` | 내 구독 목록 | [subscription.py:21](../../domains/commerce/routers/subscription.py#L21) |
| POST | `/api/v1/subscriptions/{subscribe_id}/cancel` | 구독 취소(소프트) | [subscription.py:26](../../domains/commerce/routers/subscription.py#L26) |

라우터 프리픽스: payment는 `prefix="/payments"`([payment.py:11](../../domains/commerce/routers/payment.py#L11)), subscription은 `prefix="/subscriptions"`([subscription.py:11](../../domains/commerce/routers/subscription.py#L11)), character는 프리픽스 없이 경로에 직접 씁니다.

> ⚠️ 위 경로/메서드/줄번호는 실제 라우터 파일에서 확인한 값입니다. `openapi.json`은 구버전일 수 있으니 항상 라우터 소스를 기준으로 삼으세요.

**JPA/Spring 비유**: `CurrentMember`/`DbSession`은 FastAPI의 의존성 주입(`Depends`)으로, Spring의 `@AuthenticationPrincipal` + 트랜잭션 스코프 `EntityManager` 주입과 같은 역할입니다. 라우터는 얇게 두고 서비스에 위임합니다.

한 줄 요약: **얇은 라우터가 서비스로 위임하고, 모든 경로는 `/api/v1` 아래에 놓입니다.**

---

## 7.6 N+1 방지

**왜 중요한가**: 목록 화면에서 캐릭터마다 할인을 따로 조회하면 "1(목록) + N(할인)" 쿼리가 터집니다. Spring/JPA를 해봤다면 지긋지긋한 그 N+1이에요.

### selectinload(discount_events)

```python
stmt = (select(Character).order_by(Character.character_id)
        .limit(limit).offset(offset)
        .options(selectinload(Character.discount_events)))  # 목록 할인 N+1 방지
```
[character_repository.py:26](../../domains/commerce/repository/character_repository.py#L26)

`selectinload`는 캐릭터를 먼저 다 가져온 뒤, 그 id들로 할인을 **`IN (...)` 한 방**에 로드합니다(총 2쿼리). 단건 `get()`에도 붙여 세션 종료 후 접근 오류를 예방합니다([character_repository.py:20](../../domains/commerce/repository/character_repository.py#L20)).

> **JPA 비유**: `@BatchSize` 또는 `join fetch`와 목적이 같습니다. 특히 `selectinload`는 컬렉션을 별도 `IN` 쿼리로 묶는 방식이라, 카티전 곱이 생기는 `join fetch`보다 컬렉션에 안전한 선택이에요.

### owned_character_ids 단일 쿼리

목록에서 "이 캐릭터를 내가 샀나?"를 캐릭터마다 조회하는 대신, **소유 id를 집합으로 한 번에** 가져와 메모리에서 판정합니다.

```python
def owned_character_ids(self, member_id: int) -> set[int]:
    stmt = select(MemberCharacter.character_id).where(
        MemberCharacter.member_id == member_id)
    return set(self.db.scalars(stmt).all())
```
[member_character_repository.py:21](../../domains/commerce/repository/member_character_repository.py#L21)

서비스는 이 집합으로 `c.character_id in owned`를 O(1) 판정합니다([character_service.py:49](../../domains/commerce/service/character_service.py#L49)).

**흔한 함정**: `selectinload`는 **relationship에만** 씁니다. 스칼라 컬럼(예: 소유 여부 boolean)은 relationship이 아니므로, 이렇게 "id 집합 단일 쿼리 + 파이썬 멤버십 검사"로 푸는 게 더 깔끔합니다.

한 줄 요약: **컬렉션은 `selectinload`로, "소유했나" 판정은 id 집합 단일 쿼리로 — 둘 다 목록당 쿼리를 상수로 고정합니다.**

---

## ✍️ 스스로 점검

1. 캐릭터 구매에서 소유(MemberCharacter)와 결제(Payment)가 "둘 다 or 둘 다 안"을 보장하는 코드 한 줄은 무엇인가요? 그리고 왜 repository가 아니라 service가 그 줄을 소유해야 하나요?
2. 구독을 취소할 때 행을 삭제하지 않고 `is_activate=False`로 두는 이유는 무엇이며, 이 변경이 별도 `UPDATE` 문 없이 DB에 반영되는 메커니즘(변경 감지)을 SQLAlchemy 관점에서 설명해 보세요.
3. 캐릭터 목록에서 "각 캐릭터의 할인"과 "내 소유 여부"를 각각 N+1 없이 가져오는 두 기법은 무엇인가요? 왜 하나는 `selectinload`, 다른 하나는 id 집합 쿼리를 쓰나요?

---

[⟵ 6장. 데이터 모델 전체 지도](./06-data-model-erd.md) ・ [📚 목차](./README.md) ・ 다음 ⟶
