# __calldemo 대상 언어 변수화 + 데모 UI 개선 플랜

- 작성: 2026-07-06 01:26
- 상태: 방향 확정(구현 대기)
- 관련 코드: `core/persona_prompt.py`, `domains/learning/realtime/{protocol.py,call_session.py}`, `domains/learning/service/normalcall_service.py`, `main.py`, `scripts/call_demo.html`
- 사전 논의: `prompt-persona-engineer` + `fastapi-architect` 소집 판정(본 세션)

## 1. 목표
`__calldemo`(dev 전용 통화 데모)에서 **가르치는 대상 언어를 프랑스어로** 바꿔 시연하고 싶다. 프로덕션은 **한국어 그대로**(무손상). 더불어 데모 화면을 깔끔하게: 로그인 정보 숨김·자동 로그인·캐릭터/언어 선택.

## 2. 핵심 사실 (설계 근거)
- `locale`(학습자 모국어=설명 언어)와 "한국어"(가르치는 대상 언어)는 **코드상 이미 분리된 두 축** → 대상 언어만 변수로 빼도 서로 안 엉킨다.
- `build_system_instruction`·`SEED_OPENING` 은 **순수 문자열 조립**(LLM 생성 0). 대상 언어 변수화가 깨끗함.
- `build_system_instruction` 기본값을 `"한국어"`로 두면 **프로덕션 출력이 바이트 단위 동일** → 불변식(R4) 회귀 위험 0.
- WS `/api/v1/calls/stream` 는 **prod/dev 공용 단일 경로** → "데모 전용"은 라우트 분리가 아니라 **런타임 `settings.ENV` 게이트**로 막는다.

## 3. 확정된 데모 동작 (사용자 지시 반영)
| 항목 | 데모(프랑스어) | 프로덕션(한국어) |
|---|---|---|
| 대상 언어 | 선택(프랑스어 등) | 한국어 고정 |
| `level_profile` | **넣지 않음(빈값)** | 한국어 12단계 그대로 |
| 통화후 분석 → **문장 추출** | **한다**(대상 언어로 추출·번역해 보여줌) | 한다 |
| 발음 평가(SpeechSuper) | **안 한다**(스킵) | 한다 |
| 로그인 | 숨김 + 자동 로그인 | 해당 없음 |

> 아까 에이전트의 "데모는 분석 전체 스킵" 권고를 **수정**: 문장 추출은 유지(보여주기), 발음 평가만 스킵. 따라서 `_analysis_instruction` 도 대상 언어를 받아야 한다.

## 4. 변경 설계

### 4-1. 프롬프트 계층 — 클린 변수화 (`core/persona_prompt.py`)
- `build_system_instruction(..., target_language: str = "한국어")` 추가. `_INVARIANTS_TEMPLATE` 안의 대상언어 슬롯 "한국어" → `{target_language}` 보간.
  - 치환 대상(대상언어 슬롯): "한국어를 가르치는 선생님/한국어 수업", 공부/대화 모드의 "한국어 문장·표현", 규칙3 "한국어(10%)+모국어(90%)", 규칙4 "올바른 한국어".
  - **비치환**(구조/메타 라벨): `[학습자 수준]`·`[학습자 흥미·소재]` 라벨, `{locale_label}`(모국어 축)은 절대 안 건드림.
  - code-switching은 "대상 10% + 모국어 90%"로 자연 일반화(프랑스어에도 타당).
- `SEED_OPENING`(대상언어 포함 상수) → **함수화** `seed_opening(target_language="한국어")`. 기존 `SEED_OPENING = seed_opening()` 상수는 하위호환용 유지.
- **회귀 기준**: `target_language` 미전달(기본 "한국어") 시 출력 문자열이 변경 전과 **완전 동일**.

### 4-1b. 모국어(locale) 한국어 라벨 — **데모 전용 격리** (`core/persona_prompt.py` + realtime 게이트)
- **전역 `_LOCALE_LABEL` 은 건드리지 않는다.** → 프로덕션은 `locale="ko"` 여도 여전히 en(영어)로 폴백(현행 그대로 유지). 이게 사용자 요구: "실제 서버는 ko→영어, 데모에서만 ko→한국어".
- `build_system_instruction` 과 `_analysis_instruction` 에 optional `locale_label: str | None = None` 추가:
  ```python
  locale_label = locale_label or _LOCALE_LABEL.get(locale, _LOCALE_LABEL[_DEFAULT_LOCALE])
  ```
  None 이면 기존 동작 100% 동일(프로덕션 바이트 동일).
- **데모 게이트(`is_demo_target`)에서만** ko→한국어를 해석해 명시 전달:
  ```python
  _DEMO_LOCALE_EXTRA = {"ko": "한국어"}   # 데모 전용
  # is_demo_target 일 때만:
  locale_label = _DEMO_LOCALE_EXTRA.get(locale) or _LOCALE_LABEL.get(locale, _LOCALE_LABEL["en"])
  # → build_system_instruction(..., locale_label=locale_label)
  # → analyze_call(..., locale_label=locale_label) 로 전달
  ```
- 효과: 설명 언어(system_instruction) + 분석 요약/번역 언어(`_analysis_instruction`)가 데모에서만 한국어로. 기존 배선(`ClientStart.locale` → `run_call`)에 `locale:"ko"` 를 실어 보냄.
- 프로덕션 안전: prod 에서 클라가 `locale:"ko"` 를 보내도 게이트 밖이라 en 폴백. 데모만 한국어.
- 데모 조합: `target_language="프랑스어"` + `locale="ko"` = 한국인이 프랑스어 학습(설명·분석 한국어).

### 4-2. 분석 계층 — 대상언어 인지 (`normalcall_service.py`)
- `_analysis_instruction(locale, target_language="한국어")` 로 확장. "배운 한국어 표현" → "배운 {target_language} 표현", "○○를 한국어로" → "{target_language}로" 등 대상언어 슬롯 치환.
- 출력 스키마의 `korean` 필드는 **데모에서 대상언어 문장을 담는 용도로 재사용**(스키마/DB 변경 없음 — 데모 목적).
- `analyze_call(...)` 이 `target_language` 를 받아 `_analysis_instruction` 에 전달. 기본 "한국어" → 프로덕션 무변경.
- 발음 평가는 분석과 별개(복습 단계) → 데모에서는 **호출 자체를 안 함**(UI에서 제거).

### 4-3. 배선 — 데모 전용 오버라이드 (realtime 오케스트레이션)
- `protocol.ClientStart` 에 optional `target_language: str | None = None` 추가(하위호환: 없으면 None→한국어).
- `_read_initial_start` 반환에 `target_language` 추가(튜플 arity 확장).
- `run_call` 에 정책 헬퍼:
  ```python
  def _resolve_target_language(settings, override):
      # prod 또는 override 없음 → 한국어(비데모). non-prod + override → 데모.
      if settings.ENV == "prod" or not override:
          if settings.ENV == "prod" and override:
              logger.warning("normalcall: prod 에서 target_language 오버라이드 무시(%s)", override)
          return "한국어", False
      return override.strip(), True
  ```
  - `is_demo_target` 이면: `setup["level_profile"] = ""`(빈값), `seed_opening(target)`/`build_system_instruction(..., target_language=target)` 사용, `analyze_call(..., target_language=target)`.
  - **분석은 데모에서도 돈다(문장 추출).** 발음 평가만 UI에서 뺀다.
- 레이어 규율: 대상언어 결정은 realtime(전송 오버라이드 + ENV 정책의 합성). `persona_prompt`는 순수 문자열만 받는 어댑터 유지, `load_call_setup` **시그니처 무변경**.

### 4-4. 프로덕션 안전장치
- ENV 게이트가 `run_call` 런타임에 있어, prod 클라가 실수/악의로 `target_language` 를 보내도 **무조건 폐기→한국어** + warning 로그. 방어 2중(None 기본 + ENV=prod 무시).

### 4-5. 데모 UI (`scripts/call_demo.html`)
- **STEP 1 로그인 카드 숨김** + 페이지 로드 시 **자동 로그인**(하드코딩 test 계정 + 공개 anon 키). "누구세요" 정보 표시 최소화.
- **캐릭터 ID 선택**(기존 유지) + **대상 언어 드롭다운**(한국어/프랑스어…) 추가 → `start{character_id, target_language}` 전송.
- **발음 연습(녹음→채점) UI 제거**(`wireRecord`/`/__dev/pron-eval` 호출부). **문장 목록 표시 + "🔊 들어보기(TTS)" 버튼은 유지**(확정). Gemini-TTS가 프랑스어 문장도 읽어줌.
- `/__dev/call-prompt` 프리뷰에 optional `target_language` 쿼리 추가(dev 게이트라 안전) — 변경 전후 확인용.

## 5. 건드리는 파일
- `core/persona_prompt.py` — `target_language` 파라미터 + `seed_opening()` 함수화
- `domains/learning/realtime/protocol.py` — `ClientStart.target_language`
- `domains/learning/realtime/call_session.py` — `_read_initial_start` arity, `_resolve_target_language`, setup 조립, `analyze_call` 인자
- `domains/learning/service/normalcall_service.py` — `_analysis_instruction`/`analyze_call` 에 `target_language`
- `main.py` — `/__dev/call-prompt` 에 `target_language` 프리뷰
- `scripts/call_demo.html` — 로그인 숨김·자동로그인·언어 선택·발음평가 UI 제거
- **DB/마이그레이션 변경 없음.**

## 6. 검증 / 회귀
- `/__dev/call-prompt`: (1) 기본(한국어) 경로 = 변경 전후 **문자열 diff 0**(R4), (2) `target_language=프랑스어`+`level_profile=""` = 규칙3/모드/시드에 "프랑스어"만 박히고 `{locale_label}`(모국어)는 유지되는지 눈으로 확인.
- `tests/test_normalcall_ws.py`: `_read_initial_start` arity 변경 반영, 기본 경로 회귀.
- 수동: `__calldemo`에서 프랑스어 선택 → 통화 → 문장 추출 표시(발음평가 없음) 확인.

## 7. 범위 밖 (지금 안 함)
- 정식 다국어 학습 기능(대상언어별 커리큘럼/level_profile, 대상언어별 발음평가 엔진, persona 다국어 rewrite). 지금은 **데모 전용(ENV 게이트)**. 계약 모양은 정식기능과 동일하게 둬, 승격 시 게이트 제거 + 인프라 추가만으로 전환.

## 8. 다음 단계
`fastapi-expert`(배선·서비스), `prompt-persona-engineer`(템플릿/분석 문구), 그리고 데모 HTML 구현. 구현 후 위 6번 검증.
