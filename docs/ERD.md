# BeaverTalk 엔티티 관계도 (ERD)

전체 도메인(account / commerce / alarm / learning)의 ORM 모델과 관계.
소프트 삭제 필드(`deleted_at`)는 `member`, `sentence` 두 곳에 있음.

> GitHub·VS Code(Mermaid 확장)·Obsidian 등에서 아래 다이어그램이 그려집니다.

```mermaid
erDiagram
    %% ───────── account ─────────
    speak_country ||--o{ member          : "억양 참조(1:N, SET NULL)"
    member        ||--o{ member_reason   : "학습이유(1:N, CASCADE)"

    %% ───────── commerce ─────────
    voice     ||--o{ character           : "통화 음성(1:N, SET NULL)"
    character ||--o{ discount_event      : "할인행사(1:N, CASCADE)"
    member    ||--o{ member_character    : "보유(CASCADE)"
    character ||--o{ member_character    : "보유됨(RESTRICT)"
    character ||--o{ member              : "대표 캐릭터(1:N, SET NULL)"
    member    ||--o{ subscribe           : "구독(1:N, CASCADE)"
    member    ||--o{ payment             : "결제(1:N, CASCADE)"

    %% ───────── alarm ─────────
    member    ||--o{ alarm               : "알람(1:N, CASCADE)"
    character ||--o{ alarm               : "알람 캐릭터(RESTRICT)"
    alarm     ||--o{ schedule            : "반복 요일(1:N, CASCADE)"

    %% ───────── learning ─────────
    member    ||--o{ call                : "통화(1:N, CASCADE)"
    character ||--o{ call                : "통화 상대(RESTRICT)"
    call      ||--o{ call_raw_data       : "원본 턴(1:N, CASCADE)"
    call      ||--o{ sentence            : "발화(1:N, CASCADE)"
    sentence  ||--|| evaluation          : "평가(1:1, CASCADE)"
    sentence  ||--o{ review              : "복습(1:N, CASCADE)"

    %% level 은 member.korean_level(=level_no) 로 논리 참조(FK 없음)
    level }o..o{ member : "korean_level ↔ level_no (논리 참조, FK 없음)"


    member {
        bigint member_id PK "NN"
        text auth_user_id UK "null · uq · idx · Supabase UUID"
        bigint speak_country_id FK "null · idx"
        bigint character_id FK "null · idx · 대표캐릭터"
        text name "null"
        text language "null"
        text email UK "null · uq · 탈퇴시 NULL"
        bool is_auto_payment "null"
        bool onboarding_completed "NN · def"
        datetime deleted_at "null · idx · 소프트삭제"
        int korean_level "null · →level.level_no"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    member_reason {
        bigint member_reason_id PK "NN"
        bigint member_id FK "NN · uq(member_id,reason) · idx"
        text reason "NN · uq(member_id,reason)"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    speak_country {
        bigint speak_country_id PK "NN"
        text first_country "null"
        text second_country "null"
        text third_country "null"
        int first_percent "null"
        int second_percent "null"
        int third_percent "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    character {
        bigint character_id PK "NN"
        bigint voice_id FK "null · idx"
        text role "null"
        text personality "null"
        text rules "null"
        text voice_url "null"
        numeric price "NN"
        text name "NN"
        text description "null"
        text image_url "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    voice {
        bigint voice_id PK "NN"
        text name UK "NN · uq"
        text description "null"
        text gender "null"
        text sample_url "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    member_character {
        bigint member_id PK,FK "NN"
        bigint character_id PK,FK "NN · idx"
        numeric purchase_price "null"
        datetime purchase_date "null"
    }
    discount_event {
        bigint discount_event_id PK "NN"
        bigint character_id FK "NN · idx"
        numeric discount_price "null"
        datetime start_time "null"
        datetime end_time "null"
        bool activate "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    subscribe {
        bigint subscribe_id PK "NN"
        bigint member_id FK "NN · idx"
        datetime start_date "null"
        datetime end_date "null"
        numeric price "null"
        bool is_activate "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    payment {
        bigint payment_id PK "NN"
        bigint member_id FK "NN · idx"
        datetime payment_date "null"
        numeric price "null"
        text description "null"
        text category "null · idx"
        text card_info "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    alarm {
        bigint alarm_id PK "NN"
        bigint member_id FK "NN · idx"
        bigint character_id FK "NN · idx"
        datetime time "null"
        bool is_activate "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    schedule {
        bigint schedule_id PK "NN"
        bigint alarm_id FK "NN · idx"
        text day_of_week "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    call {
        bigint call_id PK "NN"
        bigint member_id FK "NN · idx"
        bigint character_id FK "NN · idx"
        datetime call_date "null · idx"
        int total_time "null"
        text summary "null"
        int rating "null"
        text status "NN · def"
        text mode "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    call_raw_data {
        bigint call_raw_data_id PK "NN"
        bigint call_id FK "NN · idx"
        text role "null"
        int turn_index "null"
        text content "null"
        text voice_url "null"
        int total_time "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    sentence {
        bigint sentence_id PK "NN"
        bigint call_id FK "NN · idx"
        text korean_sentence "null"
        text native_sentence "null"
        text locale "null"
        text source_type "null"
        text voice_url "null"
        bool is_bookmarked "null"
        datetime deleted_at "null · idx · 소프트삭제"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    evaluation {
        bigint evaluation_id PK "NN"
        bigint sentence_id FK,UK "NN · uq"
        int total_score "null"
        int pronunciation "null"
        int fluency "null"
        int rhythm "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    review {
        bigint review_id PK "NN"
        bigint sentence_id FK "NN · idx"
        text voice_url "null"
        json feedback "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
    level {
        bigint level_id PK "NN"
        int level_no UK "NN · uq"
        text band "null"
        text grade "null"
        text stage_name "null"
        text textbook "null"
        int grammar_count "null"
        int vocab_count "null"
        text grammar_scope "null"
        text vocab_sample "null"
        text profile "null"
        datetime created_at "NN · def=now()"
        datetime updated_at "NN · def=now() · onupd"
    }
```

## 관계 요약

| 부모 | 자식 | 카디널리티 | 삭제 정책 | 비고 |
|---|---|---|---|---|
| speak_country | member | 1:N | SET NULL | 억양 |
| character | member | 1:N | SET NULL | 대표 캐릭터 |
| member | member_reason | 1:N | CASCADE | 온보딩 학습이유 |
| member ↔ character | member_character | M:N | member=CASCADE, character=RESTRICT | 보유 캐릭터(복합 PK) |
| voice | character | 1:N | SET NULL | 통화 음성 |
| character | discount_event | 1:N | CASCADE | 할인행사 |
| member | subscribe | 1:N | CASCADE | 구독 |
| member | payment | 1:N | CASCADE | 결제 |
| member | alarm | 1:N | CASCADE | 알람 |
| character | alarm | 1:N | RESTRICT | 알람 캐릭터 |
| alarm | schedule | 1:N | CASCADE | 반복 요일 |
| member | call | 1:N | CASCADE | 통화 |
| character | call | 1:N | RESTRICT | 통화 상대 |
| call | call_raw_data | 1:N | CASCADE | 원본 턴 |
| call | sentence | 1:N | CASCADE | 발화 |
| sentence | evaluation | 1:1 | CASCADE | UNIQUE FK |
| sentence | review | 1:N | CASCADE | 복습 |
| level | member | (논리) | — | member.korean_level = level.level_no, **DB FK 없음** |

## 참고

- **소프트 삭제**: `member.deleted_at`, `sentence.deleted_at`. 값이 있으면 탈퇴/삭제된 행. 회원 탈퇴 시 `member`는 하드 삭제하지 않고 `deleted_at`을 찍으며 `email`·`auth_user_id`를 NULL로 비운다(같은 이메일 재가입 허용). 자식 데이터(call·subscribe 등)는 CASCADE로 지워지지 않고 보존된다.
- **RESTRICT** 관계(character→alarm/call/member_character)는 참조가 남아 있으면 캐릭터를 못 지운다(마스터 데이터 보호).
- `level`은 FK 없이 `korean_level` 숫자값으로만 연결되는 마스터 데이터.
