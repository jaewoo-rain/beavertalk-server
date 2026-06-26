# 2026-06-25 07:46 · normalcall 모델 변경안 (DB 스키마 확정 플랜)

> normalcall(실시간 한국어 음성통화) 기능을 위한 **DB 모델/마이그레이션 변경안만** 확정한다.
> 상위 통합 설계: [`20260625_0726_normalcall-integration-plan.md`](20260625_0726_normalcall-integration-plan.md)
> **이 문서는 플랜. 코드/마이그레이션 작성·적용은 승인 후.**

## 확정된 결정 (사용자)
1. **personas = 캐릭터**(기존 `character` 테이블). **voices는 신규 테이블**로 만든다(Gemini 보이스 30종). 캐릭터가 voice를 참조.
2. **캐릭터 페르소나(`prompt`)를 `role` + `personality` + `rules` 3개로 분해**(prompt 제거). 공통 골격(언어정책·5분종료 등)은 캐릭터별이 아니라 코드 템플릿.
3. **`member.language` = 모국어(번역 target locale)**. 별도 컬럼 추가 없이 comment만 명확화.
4. **통화중 저장 = 1분마다 flush**(런타임 전략, 스키마 변경 없음 — `call_raw_data`가 이미 1:N).

기준 컨벤션: `Base + TimestampMixin`(created_at/updated_at), `BigInteger Identity` PK, FK `ondelete` 명시, `Mapped[Optional[...]]`=nullable / `Mapped[str]`=NOT NULL, 한글 `comment=`, 타입은 `Text`+앱 화이트리스트(DB Enum 회피), 데이터 시드는 `scripts/seed.py`.

---

## 변경 요약 (한눈에)

| 구분 | 테이블 | 변경 | 판정 |
|---|---|---|---|
| **CREATE** | `voice` (commerce) | 신규 테이블 + 30종 시드 | 필수 |
| **CREATE** | `level` (learning) | 신규 테이블 + **12단계 시드**(레벨별 발화 프로파일) | 필수 |
| **EDIT** | `character` (commerce) | +`voice_id`(FK) +`role` +`personality` +`rules`, **−`prompt`** | 필수 |
| **ADD** | `member` (account) | +`korean_level`(Int 1~12) +`interests` +`example_sentences`, `language` comment 변경 | 필수 |
| **EDIT** | `call` (learning) | +`status`(NOT NULL) +`mode` | 필수(status)/선택(mode) |
| **EDIT** | `sentence` (learning) | +`source_type` | 선택(권장) |
| — | `call_raw_data` (learning) | 변경 없음(1분 flush는 행 추가로 처리) | — |

---

## ① CREATE: `voice` (commerce 도메인)

`domains/commerce/models/voice.py` 신규. character 옆(같은 도메인)에 둬서 character→voice FK를 도메인 내부로 유지.

| 컬럼 | SQLAlchemy 타입 | nullable | 제약 | comment |
|---|---|---|---|---|
| `voice_id` | `BigInteger` Identity | NOT NULL | PK | |
| `name` | `Text` | NOT NULL | UNIQUE(`uq_voice_name`) | Gemini Live 프리빌트 보이스명(예: Charon, Aoede) |
| `description` | `Text` | nullable | | 음색 설명(밝은/차분한 등) |
| `gender` | `Text` | nullable | | 성별 느낌(male/female/neutral) |
| `sample_url` | `Text` | nullable | | 미리듣기 샘플 URL |
| `created_at`/`updated_at` | (TimestampMixin) | NOT NULL | | |

관계: `characters` (1:N, back_populates="voice").

### 시드 — Gemini Live 프리빌트 보이스 30종 (`scripts/seed.py`)
이름 + 음색 특성(출처: ai.google.dev speech-generation). gender는 공식 분류 없음 → NULL.

```
Zephyr(밝은) Puck(경쾌한) Charon(정보전달형) Kore(단단한) Fenrir(활기찬)
Leda(젊은) Orus(단단한) Aoede(산뜻한) Callirrhoe(느긋한) Autonoe(밝은)
Enceladus(숨소리섞인) Iapetus(맑은) Umbriel(느긋한) Algieba(매끄러운) Despina(매끄러운)
Erinome(맑은) Algenib(허스키) Rasalgethi(정보전달형) Laomedeia(경쾌한) Achernar(부드러운)
Alnilam(단단한) Schedar(고른) Gacrux(성숙한) Pulcherrima(적극적인) Achird(친근한)
Zubenelgenubi(캐주얼한) Vindemiatrix(온화한) Sadachbia(생기있는) Sadaltager(박식한) Sulafat(따뜻한)
```

---

## ①-2 CREATE: `level` (learning 도메인) — 12단계 레벨 마스터

> **왜 DB 테이블인가:** 프롬프트 빌더가 `member.korean_level`(1~12)로 **레벨별 발화 프로파일**을 꺼내 `system_instruction`의 [학습자 수준] 슬롯에 주입해야 한다. 큰 MD 1개로는 런타임에 못 쓰므로 **12행 테이블**로 분리(프로젝트 DB-first 컨벤션, 캐릭터·voice와 동일). 12개 파일로 쪼개는 대신 DB 1테이블이 일관적·조회 1회.
> **무엇을 담나:** 프롬프트엔 압축 프로파일만 주입(원본 어휘 5,965개 통째 주입 금지). 따라서 레벨당 **프로파일 + 핵심 문법 + 대표 어휘 + 메타**만. 원본 전체는 `assets/level/{grammar_12levels,vocab_12levels}.json` 레퍼런스.

`domains/learning/models/level.py` 신규.

| 컬럼 | 타입 | nullable | 제약 | comment |
|---|---|---|---|---|
| `level_id` | `BigInteger` Identity | NOT NULL | PK | |
| `level_no` | `Integer` | NOT NULL | UNIQUE(`uq_level_no`) | 레벨 번호(1~12) |
| `band` | `Text` | nullable | | 밴드(초급/중급/고급) |
| `grade` | `Text` | nullable | | 어휘 등급(A/B/C) |
| `stage_name` | `Text` | nullable | | 단계명(초급 1 …) |
| `textbook` | `Text` | nullable | | 교재명(Basic Korean A …) |
| `grammar_count` | `Integer` | nullable | | 문법 포인트 수 |
| `vocab_count` | `Integer` | nullable | | 어휘 수 |
| `grammar_scope` | `Text` | nullable | | 핵심 문법(JSON 배열 문자열, ~15개) |
| `vocab_sample` | `Text` | nullable | | 고빈도 대표 어휘(JSON 배열 문자열, ~40개) |
| `profile` | `Text` | nullable | | **발화 프로파일(프롬프트 [학습자 수준] 슬롯 주입)** |
| `created_at`/`updated_at` | (TimestampMixin) | NOT NULL | | |

- **시드 소스: `assets/level/level_profiles_12.json`**(이미 생성 — 원본 엑셀 추출본). `seed.py`의 `seed_levels`가 이 파일을 읽어 12행 upsert.
- `member.korean_level`(Int 1~12)는 `level.level_no`를 가리킴. **FK는 두지 않음**(고정 enum 성격 — `member_reason`처럼 앱 검증; FK가 비-PK 컬럼을 향하는 복잡도 회피). 무결성은 `1 ≤ level_no ≤ 12` 앱 검증.
- 프롬프트 빌더: `SELECT profile, grammar_scope, vocab_sample FROM level WHERE level_no = member.korean_level` 1회.

---

## ② EDIT: `character` (페르소나 분해 + voice 연결)

`domains/commerce/models/character.py`.

**ADD**
| 컬럼 | 타입 | nullable | 제약 | comment | 왜 |
|---|---|---|---|---|---|
| `voice_id` | `BigInteger` | nullable | FK→`voice.voice_id` `ondelete=SET NULL`, index | 실시간 통화 음성 | 캐릭터=페르소나, voice=음성 분리 참조 |
| `role` | `Text` | nullable | | 역할/정체성 | 페르소나 분해(prompt→구조화) |
| `personality` | `Text` | nullable | | 성격·말투·톤 | 〃 |
| `rules` | `Text` | nullable | | 캐릭터별 추가 규칙/금기 | 캐릭터별 특수규칙(예: 거친 말투)을 DB로 |

**REMOVE**
- `prompt` (분해되어 role/personality/rules로 대체). 기존 4종 시드값은 `seed.py`에서 새 컬럼으로 재작성(데이터 유실 없음).

**KEEP**
- `voice_url`(comment를 "캐릭터 프리뷰 샘플 음성 URL"로 명확화 — `voice` 테이블의 실시간 합성과 역할 분리), `price`, `name`, `description`, `image_url`.

관계: `voice` (N:1, back_populates="characters").

### 기존 4종 캐릭터 재시드 매핑 (`seed.py`)
| name | role | personality | rules | voice |
|---|---|---|---|---|
| 비비 | 거칠지만 정 있는 트래시토커 파트너(기본 무료) | 직설적·도발적, 모국어로 거친 농담·면박 | 모국어 거친 표현·욕설 허용(과하지 않게) | **Fenrir** |
| 주디 | 발음을 짚어주는 선생님형 파트너 | 차분·똑부러짐 | 발음·문법 오류를 친절히 교정 | Kore |
| 레오 | 활기차게 이끄는 친구형 파트너 | 에너지·장난스러움 | — | Puck |
| 미나 | 공감하며 들어주는 파트너 | 감성·따뜻함 | — | Leda |

(비비=Fenrir 고정[기본 무료·욕쟁이 트래시토커]. 주디/레오/미나 voice는 임의 배정 — 미리듣기 후 조정.)

---

## ③ ADD: `member` (학습 프로파일)

`domains/account/models/member.py`.

| 컬럼 | 타입 | nullable | comment | 왜 |
|---|---|---|---|---|
| `korean_level` | `Integer` | nullable | 한국어 레벨(**1~12** → `level.level_no`) | 통화 난이도·교정 강도·code-switching 비율 입력. 프롬프트 빌더가 이 값으로 `level` 조회 |
| `interests` | `Text` | nullable | 관심사(콤마구분 코드) | 대화 주제 시드(프롬프트 주입). 통계 대상 아니라 직접 컬럼으로 충분 |
| `example_sentences` | `Text` | nullable | 통화 프롬프트용 예시 문장(개행 구분) | 사용자 표현 수준 시드 |

**EDIT**: `language` comment `"사용 언어"` → `"모국어(번역 target locale)"`. (값/타입 변경 없음 — `Sentence.locale`·`native_sentence` 번역 타깃의 단일 출처임을 명시.)

> **`korean_level` = 1~12** (한국어 12단계 커리큘럼). 원본 엑셀(문법·어휘 12단계)을 추출한 전문 레퍼런스: [`20260625_korean-level-12-curriculum.md`](20260625_korean-level-12-curriculum.md). 레벨별 **발화 프로파일**을 통화 프롬프트 [학습자 수준] 슬롯에 주입. 머신용 전체 데이터는 `assets/level/{grammar_12levels,vocab_12levels,freetalking}.json`. (초급 1~4=등급A / 중급 5~8=B / 고급 9~12=C.)

> 흥미를 별도 테이블(`member_interest`)로 정규화하지 않음 — `member_reason`(학습 이유)과 달리 통화 프롬프트 주입용 보조 텍스트라 정규화 이득 작음. 예시 '단어' 별 컬럼은 YAGNI로 생략(`interests`/`example_sentences`에 흡수).

---

## ④ EDIT: `call` (분석 상태 + 모드)

`domains/learning/models/call.py`.

| 컬럼 | 타입 | nullable | 기본값 | comment | 판정 |
|---|---|---|---|---|---|
| `status` | `Text` | NOT NULL | `server_default text("'ongoing'")` | 분석 상태(ongoing/analyzing/done/failed) | **필수** |
| `mode` | `Text` | nullable | | 감지된 통화 모드(conversation/study/unknown) | 선택 |

- `status`: 비동기 분석 폴링 신호. NOT NULL + server_default라 **기존 행 자동 백필(ongoing)** → 무중단.
- `mode`: UI 뱃지/통계용. 분석이 detected_mode를 산출하므로 같이 저장. (불필요하면 생략 가능.)
- import에 `text` 추가 필요(server_default).

---

## ⑤ EDIT: `sentence` (표현 출처)

`domains/learning/models/sentence.py`.

| 컬럼 | 타입 | nullable | comment | 판정 |
|---|---|---|---|---|
| `source_type` | `Text` | nullable | 표현 출처(asked/corrected/drilled) | 선택(권장) |

- 결과/복습 화면에서 "내가 물어본 표현 / 교정된 표현 / 발음연습"을 구분. 통화후 분석이 이 값을 산출하므로 지금 넣어두는 걸 권장. nullable이라 무중단.

---

## 통화중 저장 전략 (런타임 — 스키마 변경 없음)

`call_raw_data`가 이미 `call`과 1:N이라 **1분마다 누적 전사/음성을 행으로 append**하면 됨:
```
시작:   Call(status=ongoing) INSERT
1분마다: 직전 flush 이후 누적된 턴 → CallRawData 행 append
종료:   finalize(status=analyzing) → 분석 → status=done
```
- 5→10→15분으로 늘어나도 **행 수만 증가**, 스키마 그대로.
- 크래시 시 마지막 flush(≤1분)까지 보존.
- 1분 간격은 코드 상수(추후 조정 가능).

---

## Alembic 마이그레이션 전략

- **head = `a7b8c9d0e1f2`**(member_name) → 신규 리비전 1개 `<rev>_normalcall_schema_delta`, `down_revision='a7b8c9d0e1f2'`.
- **DDL 전부 한 리비전**(create **voice** + create **level** + character 컬럼 +/− + member 3컬럼 + language comment + call 2컬럼 + sentence 1컬럼).
- **데이터 시드는 마이그레이션이 아니라 `scripts/seed.py`**(프로젝트 기존 컨벤션). seed.py에 추가: `seed_voices`(30종) + `seed_levels`(`assets/level/level_profiles_12.json` 읽어 12행) + `seed_characters` 재작성. → migration은 순수 스키마, seed는 데이터로 분리.
- **무중단**: 신규 컬럼 전부 nullable, 유일한 NOT NULL인 `call.status`만 server_default `'ongoing'`(기존 행 자동 백필). `prompt` drop은 데이터 손실이나 값이 seed.py에 있어 안전.
- **registry.py / 각 도메인 `__init__.py`에 `Voice`·`Level` import 추가**(Alembic autogenerate·관계 해석용).
- downgrade: 역순(level drop, voice drop, character 컬럼 원복 + prompt 재생성, member/call/sentence 컬럼 drop, language comment 원복).

### env.py 주의 (확인됨)
- `include_name`이 "모델에 정의된 테이블만 관리" → `voice`·`level`을 모델+registry에 등록해야 autogenerate가 인식/생성. (미등록 테이블 보호 로직과 충돌 없음.)
- 마이그레이션은 `settings.direct_url`(5432 Direct) 사용.

---

## 구현 순서 (승인 후)
1. `voice.py`·`level.py` 신규 + `character.py` 수정(분해/FK/prompt제거) + `member`(korean_level Int 등)·`call`·`sentence` 컬럼 추가.
2. `registry.py`·각 도메인 `__init__.py`에 `Voice`·`Level` 등록.
3. `scripts/seed.py`: `seed_voices`(30종) + `seed_levels`(`assets/level/level_profiles_12.json` → 12행) + `seed_characters` 재작성(role/personality/rules/voice_id, 비비=Fenrir, 기존 행 update).
4. Alembic 리비전 1개 생성(DDL: voice+level 테이블 + 컬럼 델타). **적용은 사용자가 `alembic upgrade head` 실행 또는 승인 후.**
5. (다음 단계) core 어댑터·realtime WS·분석 서비스 — 별도 플랜대로.

## 열린 항목
- 캐릭터 voice: **비비=Fenrir 고정**(기본 무료·욕쟁이 트래시토커), 주디/레오/미나는 임의 배정 — 미리듣기 후 조정.
- ~~`korean_level` 값 체계~~ → **확정: 1~12 (한국어 12단계 커리큘럼).** [`20260625_korean-level-12-curriculum.md`](20260625_korean-level-12-curriculum.md) 참조.
