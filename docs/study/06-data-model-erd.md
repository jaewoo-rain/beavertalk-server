# 6장. 데이터 모델 전체 지도 (ERD)

> 📘 **이 장을 읽고 나면**
> - BeaverTalk의 14개 테이블이 4개 도메인(account / commerce / learning / alarm)으로 어떻게 나뉘고 서로 어떻게 연결되는지 한눈에 그릴 수 있어요.
> - JPA에서 익숙한 관계 4종(1:1, 1:N 양방향, N:1 단방향, N:M+추가컬럼)이 SQLAlchemy 2.0에서 각각 어떤 코드로 표현되는지 대응시킬 수 있어요.
> - `BigInteger + Identity()` PK, `Numeric(10,2)` 금액, `TimestampMixin`, cascade 정책, 소프트 삭제 같은 **공통 규칙**을 이해하고, 새 테이블을 만들 때 그대로 따라 할 수 있어요.
> - "이 캐릭터를 지우면 뭐가 같이 지워지고, 뭐가 막히는가?"를 삭제 정책(CASCADE vs RESTRICT vs SET NULL)으로 설명할 수 있어요.

---

## 6.1 왜 전체 지도를 먼저 보나요?

각 도메인 챕터로 바로 들어가기 전에 **테이블 지도**를 먼저 익히면, 개별 코드가 "전체 그림의 어느 조각인지"가 보입니다. Spring 프로젝트에 처음 투입될 때 ERD 한 장을 벽에 붙여 놓고 시작하는 것과 같아요.

> **JPA 비유**: JPA에서 `@Entity` 클래스들과 그 사이의 `@OneToMany` / `@ManyToOne` 연관을 모아 놓은 도메인 모델 다이어그램이 바로 이 장입니다. SQLAlchemy에서는 `@Entity` 대신 `Base`를 상속한 클래스, `@Column` 대신 `mapped_column(...)`, 연관은 `relationship(...)`으로 표현합니다.

이 저장소에는 이미 사람이 관리하는 두 개의 원본이 있으니 **교차 확인**하세요.
- [docs/ERD.md](../ERD.md) — Mermaid ERD + 관계 요약 표 (이 장의 기반)
- `docs/BeaverTalk.vuerd.json` — vuerd 편집기용 ERD 원본

한 줄 요약: **개별 테이블을 외우기 전에, 도메인 4개와 관계 4종부터 머리에 넣으세요.**

---

## 6.2 14개 테이블 한눈에 — 4개 도메인

테이블은 `domains/<도메인>/models/*.py` 에 도메인별로 모여 있습니다.

| 도메인 | 테이블 | 한 줄 설명 | 모델 파일 |
|---|---|---|---|
| **account** | `member` | 회원(Supabase Auth 연결, 소프트 삭제) | [member.py](../../domains/account/models/member.py) |
| | `speak_country` | 억양(모국 억양 비율) 마스터 | [speak_country.py](../../domains/account/models/speak_country.py) |
| | `member_reason` | 온보딩 학습 이유(회원당 N개) | [member_reason.py](../../domains/account/models/member_reason.py) |
| **commerce** | `character` | 회화 페르소나(가격 보유) 마스터 | [character.py](../../domains/commerce/models/character.py) |
| | `voice` | Gemini Live 음성 카탈로그 마스터 | [voice.py](../../domains/commerce/models/voice.py) |
| | `member_character` | 캐릭터 소유(구매) 조인 테이블(복합 PK) | [member_character.py](../../domains/commerce/models/member_character.py) |
| | `discount_event` | 캐릭터별 기간 할인 | [discount_event.py](../../domains/commerce/models/discount_event.py) |
| | `payment` | 결제 로그(캐릭터/구독 공용) | [payment.py](../../domains/commerce/models/payment.py) |
| | `subscribe` | 구독(소프트 취소) | [subscribe.py](../../domains/commerce/models/subscribe.py) |
| **learning** | `call` | 통화 세션 | [call.py](../../domains/learning/models/call.py) |
| | `call_raw_data` | 통화의 원본 턴(발화 순서) | [call_raw_data.py](../../domains/learning/models/call_raw_data.py) |
| | `sentence` | 발화(문장, 소프트 삭제·북마크) | [sentence.py](../../domains/learning/models/sentence.py) |
| | `evaluation` | 발화 평가(1:1) | [evaluation.py](../../domains/learning/models/evaluation.py) |
| | `review` | 발화 복습 기록(1:N) | [review.py](../../domains/learning/models/review.py) |
| | `level` | 한국어 레벨(1~12) 마스터 (FK 없이 숫자 참조) | [level.py](../../domains/learning/models/level.py) |
| **alarm** | `alarm` | 알람(회원+캐릭터) | [alarm.py](../../domains/alarm/models/alarm.py) |
| | `schedule` | 알람 반복 요일(알람당 N개) | [schedule.py](../../domains/alarm/models/schedule.py) |

> ⚠️ 위 표는 4 도메인 기준으로 정확히 **14개 핵심 테이블**을 담고 있어요(`level`은 마스터 데이터로 함께 셈). 실제 파일 목록은 각 도메인의 `models/` 디렉터리를 열어 다시 확인하세요.

### ASCII 관계 지도

화살표 방향은 "자식 → 부모(FK가 가리키는 쪽)"이고, 괄호 안은 **부모 삭제 시 정책**입니다.

```
account 도메인                         commerce 도메인
──────────────                        ────────────────
speak_country ◄──(SET NULL)── member       voice ◄──(SET NULL)── character
                                │             character ◄──(CASCADE)── discount_event
member ──(1:N,CASCADE)──► member_reason      character ◄──(SET NULL)── member (대표 캐릭터)
                                │
                                │  ┌──────────── member_character (복합 PK) ──────────┐
                                └──┤  member ──(CASCADE)──►  ◄──(RESTRICT)── character │
                                   └───────────────────────────────────────────────────┘
                                │
member ──(1:N,CASCADE)──► subscribe,  payment

learning 도메인                                     alarm 도메인
────────────────                                    ─────────────
member ──(CASCADE)──► call ◄──(RESTRICT)── character   member ──(CASCADE)──► alarm ◄──(RESTRICT)── character
                       │                                                        │
        ┌──────────────┼──────────────┐                        alarm ──(1:N,CASCADE)──► schedule
        ▼              ▼              ▼
   call_raw_data    sentence      (모두 CASCADE)
                       │
        ┌──────────────┼──────────────┐
        ▼(1:1)                        ▼(1:N)
    evaluation                     review

level  ······(FK 없음)······  member.korean_level = level.level_no  (논리 참조만)
```

한 줄 요약: **member와 character가 두 개의 허브(hub)입니다 — 대부분의 테이블이 이 둘 중 하나(또는 둘 다)에 매달려 있어요.**

---

## 6.3 관계 패턴 4종 (JPA 비유로)

BeaverTalk의 모든 관계는 아래 4개 패턴 중 하나입니다. JPA를 알면 거의 그대로 대응됩니다.

### 패턴 1 — 1:1 (unique FK): `sentence ↔ evaluation`

**왜 필요한가**: 발화(문장) 하나에 평가(점수)는 정확히 한 건만 있어야 합니다.

**JPA 비유**: `@OneToOne` 에서 자식 쪽 FK에 `@Column(unique = true)`를 거는 방식과 동일합니다. "1:1은 결국 FK에 UNIQUE를 얹은 1:N"이라는 감각이 그대로 통해요.

```python
# evaluation 이 자식. sentence_id FK 에 unique=True → "발화당 평가 1건" 보장
sentence_id: Mapped[int] = mapped_column(
    ForeignKey("sentence.sentence_id", ondelete="CASCADE"), unique=True, comment="발화",
)
sentence: Mapped["Sentence"] = relationship(back_populates="evaluation")
```

실제 코드: [domains/learning/models/evaluation.py:21](../../domains/learning/models/evaluation.py#L21)

**흔한 함정**: `unique=True`를 빠뜨리면 조용히 1:N이 되어 "한 문장에 평가 2건"이 저장되어도 DB가 막지 못합니다. 1:1의 핵심은 relationship이 아니라 **컬럼의 UNIQUE 제약**입니다.

한 줄 요약: **1:1 = 자식 FK에 `unique=True`. relationship은 편의일 뿐, 보장은 UNIQUE가 합니다.**

### 패턴 2 — 1:N 양방향 (`back_populates` + `cascade`): `member ↔ call`

**왜 필요한가**: 회원 한 명이 통화를 여러 번 하고(부모→자식 목록), 통화에서 회원을 거꾸로 참조(자식→부모)해야 합니다. 회원이 지워지면 그 통화들도 함께 정리되어야 하죠.

**JPA 비유**: `@OneToMany(mappedBy="member", cascade = REMOVE, orphanRemoval = true)` + 자식의 `@ManyToOne`. SQLAlchemy의 `back_populates`가 JPA의 `mappedBy`, `cascade="all, delete-orphan"`이 `orphanRemoval=true`에 대응합니다.

```python
# 부모(member) 쪽
calls: Mapped[list["Call"]] = relationship(
    back_populates="member", cascade="all, delete-orphan", passive_deletes=True,
)
```
```python
# 자식(call) 쪽
member: Mapped["Member"] = relationship(back_populates="calls")
```

실제 코드: 부모 [domains/account/models/member.py:97](../../domains/account/models/member.py#L97), 자식 [domains/learning/models/call.py:45](../../domains/learning/models/call.py#L45)

**흔한 함정**: `passive_deletes=True`를 빼면 SQLAlchemy가 자식을 하나씩 `SELECT` 후 `DELETE`하려고 해서 성능이 나빠집니다. 이 프로젝트는 FK에 `ondelete="CASCADE"`(DB가 직접 지움)를 걸고 `passive_deletes=True`로 "DB에 맡겨"라고 알려줍니다. **양쪽 정책이 짝을 이뤄야** 해요.

한 줄 요약: **`back_populates`(=mappedBy) + `cascade="all, delete-orphan"`(=orphanRemoval) + FK `ondelete="CASCADE"` + `passive_deletes=True`가 한 세트입니다.**

### 패턴 3 — N:1 단방향 (RESTRICT): `call → character`

**왜 필요한가**: 통화는 어떤 캐릭터와 했는지 알아야 하지만(자식→부모), 캐릭터가 "나와 통화한 목록"을 들고 다닐 필요는 없습니다(역방향 불필요). 또 통화 이력이 남아 있는 캐릭터를 실수로 지우면 안 됩니다.

**JPA 비유**: 역참조 컬렉션 없이 자식에만 `@ManyToOne`을 둔 **단방향** 매핑입니다. `back_populates` 없이 relationship 한쪽만 선언하면 단방향이 됩니다.

```python
character_id: Mapped[int] = mapped_column(
    ForeignKey("character.character_id", ondelete="RESTRICT"), index=True, comment="캐릭터",
)
# 단방향(필요 시 쿼리에서 joinedload) — back_populates 없음
character: Mapped["Character"] = relationship(lazy="select")
```

실제 코드: [domains/learning/models/call.py:30](../../domains/learning/models/call.py#L30), relationship은 [call.py:46](../../domains/learning/models/call.py#L46)

**흔한 함정**: `ondelete="RESTRICT"`는 "참조가 남아 있으면 부모 삭제를 **거부**"합니다. character는 마스터 데이터이므로 이게 맞아요. 반대로 소유 계층(member의 자식들)은 CASCADE입니다. **"마스터 데이터 = RESTRICT/SET NULL, 소유 데이터 = CASCADE"** 라는 감을 잡으세요.

한 줄 요약: **N:1 단방향 = 자식에만 relationship. 마스터를 보호하려면 FK를 RESTRICT로.**

### 패턴 4 — N:M + 추가 컬럼 (Association Object, 복합 PK): `member ↔ character`

**왜 필요한가**: 회원은 여러 캐릭터를 사고, 캐릭터는 여러 회원이 삽니다(N:M). 그런데 "**구매가 스냅샷**"과 "구매 날짜"라는 추가 정보가 필요해서, 단순 조인 테이블이 아니라 **엔티티가 있는 조인 테이블**이 됩니다.

**JPA 비유**: `@ManyToMany`로 끝내지 않고 `@Entity`로 만든 **연결 엔티티** + `@EmbeddedId`(복합 키) 패턴과 똑같습니다. JPA에서 "조인 테이블에 컬럼 추가가 필요하면 @ManyToMany를 버리고 연결 엔티티로 승격하라"는 그 규칙이에요.

```python
class MemberCharacter(Base):
    __tablename__ = "member_character"

    member_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("member.member_id", ondelete="CASCADE"), primary_key=True,
    )
    character_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("character.character_id", ondelete="RESTRICT"),
        primary_key=True, index=True,
    )
    purchase_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))  # 구매가 스냅샷
    purchase_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
```

실제 코드: [domains/commerce/models/member_character.py:23](../../domains/commerce/models/member_character.py#L23)

여기서 **복합 PK `(member_id, character_id)` 자체가 "같은 캐릭터 중복 구매 불가"를 보장**합니다(같은 조합을 두 번 넣으면 PK 충돌). 그리고 두 FK의 정책이 **비대칭**이라는 점을 눈여겨보세요.
- `member_id` → **CASCADE**: 회원이 지워지면 소유 기록도 정리(소유는 회원 소유물).
- `character_id` → **RESTRICT**: 누군가 소유 중인 캐릭터는 못 지움(마스터 보호).

한 줄 요약: **조인 테이블에 컬럼이 필요하면 Association Object로 승격하고, 복합 PK로 중복을 막으세요.**

---

## 6.4 모든 테이블이 따르는 공통 규칙

### PK: `BigInteger + Identity()`

```python
character_id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
```
실제 코드: [domains/commerce/models/character.py:26](../../domains/commerce/models/character.py#L26)

**JPA 비유**: `@GeneratedValue(strategy = GenerationType.IDENTITY)`와 같습니다. DB가 시퀀스/identity로 PK를 채워 줍니다. `member_character`처럼 복합 PK인 경우만 `Identity()`를 안 씁니다(둘 다 FK로 구성).

### 금액: `Numeric(10, 2)`

```python
price: Mapped[Decimal] = mapped_column(Numeric(10, 2), comment="가격(달러)")
```
실제 코드: [domains/commerce/models/character.py:35](../../domains/commerce/models/character.py#L35)

**왜**: 돈을 `float`로 다루면 반올림 오차가 생깁니다. `Numeric(10,2)`(정수부 최대 8자리 + 소수 2자리)로 Python `Decimal`과 매핑됩니다. **JPA 비유**: `BigDecimal` + `@Column(precision=10, scale=2)`.

### 타임스탬프: `TimestampMixin`

```python
class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
```
실제 코드: [db/base.py:20](../../db/base.py#L20)

**JPA 비유**: `@MappedSuperclass` + `@CreationTimestamp` / `@UpdateTimestamp`. 클래스가 `class Payment(Base, TimestampMixin)`처럼 믹스인을 **상속만** 하면 두 컬럼이 자동으로 붙습니다. `server_default=func.now()`는 앱이 아니라 **DB 서버 시각**으로 채웁니다. 단, `member_character`처럼 TimestampMixin을 안 쓰는 조인 테이블도 있으니 상속 목록을 확인하세요.

### cascade: 소유 계층 CASCADE vs 마스터 데이터 RESTRICT/SET NULL

| 상황 | 정책 | 예 |
|---|---|---|
| 부모가 자식을 **소유** | `CASCADE` | member → call / payment / subscribe |
| **마스터**를 참조 중이라 보호 | `RESTRICT` | character ← call / alarm / member_character |
| 참조는 선택적, 끊어도 됨 | `SET NULL` | speak_country ← member, voice ← character |

**감 잡는 법**: "이 부모를 지웠을 때, 자식은 **같이 죽어야 하나(CASCADE)** / **부모를 못 죽게 막아야 하나(RESTRICT)** / **혼자 살아남되 연결만 끊나(SET NULL)**?"를 물어보세요.

### 소프트 삭제: `member.deleted_at`, `sentence.deleted_at`

```python
deleted_at: Mapped[Optional[datetime]] = mapped_column(
    DateTime(timezone=True), index=True, comment="탈퇴 시각(소프트 삭제, NULL=활성)",
)
```
실제 코드: [domains/account/models/member.py:65](../../domains/account/models/member.py#L65)

**왜**: 회원 탈퇴 시 `member` 행을 진짜로 지우면 통화·구독 이력까지 CASCADE로 날아갑니다. 대신 `deleted_at`에 시각을 찍고 `email`·`auth_user_id`만 NULL로 비워 **재가입은 허용하되 데이터는 보존**합니다. **JPA 비유**: Hibernate `@SQLDelete` + `@Where(clause = "deleted_at IS NULL")` 패턴을 애플리케이션 레벨에서 손으로 하는 것.

**흔한 함정**: 소프트 삭제 테이블을 조회할 때 `WHERE deleted_at IS NULL` 필터를 빠뜨리면 탈퇴 회원/삭제 문장이 섞여 나옵니다.

### 인덱스: FK 인덱스 + 복합/부분 인덱스

- **모든 FK에 `index=True`** — 조인·필터 성능(예: [payment.py:23](../../domains/commerce/models/payment.py#L23)의 `member_id`, [payment.py:28](../../domains/commerce/models/payment.py#L28)의 `category`).
- **복합 인덱스** — "내 통화 최신순" 같은 조합 조회용:
  ```python
  __table_args__ = (Index("ix_call_member_date", "member_id", "call_date"),)
  ```
  실제 코드: [domains/learning/models/call.py:23](../../domains/learning/models/call.py#L23)

**JPA 비유**: `@Table(indexes = @Index(columnList = "member_id, call_date"))`. JPA는 FK 인덱스를 자동으로 만들어 주지 않는 경우가 많아 여기서도 **명시적으로** 붙입니다.

한 줄 요약: **PK는 Identity, 돈은 Numeric(10,2), 시각은 TimestampMixin, 삭제는 소유=CASCADE·마스터=RESTRICT·소프트삭제 2곳, FK엔 항상 인덱스 — 이 6개가 골격입니다.**

---

## ✍️ 스스로 점검

1. `evaluation`이 `sentence`와 1:1임을 **DB 레벨에서 보장**하는 것은 relationship 선언일까요, 아니면 컬럼의 어떤 제약일까요? (힌트: 6.3 패턴 1)
2. 회원(member)을 삭제하면 그 회원의 `call`은 어떻게 되고, 그 회원이 소유했던 `member_character`가 가리키는 `character`는 왜 삭제되지 않나요? 두 FK의 정책 차이로 설명해 보세요.
3. 새 금액 컬럼과 생성/수정 시각이 필요한 테이블을 만든다면, PK 타입·금액 타입·타임스탬프를 각각 어떤 코드로 선언해야 이 프로젝트의 공통 규칙을 따르는 걸까요?

---

⟵ 이전 ・ [📚 목차](./README.md) ・ [7장. commerce 도메인 ⟶](./07-commerce.md)
