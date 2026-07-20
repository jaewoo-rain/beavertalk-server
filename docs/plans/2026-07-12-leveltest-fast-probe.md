# 레벨테스트 3분 test-like 프로빙 + 천장 조기종료

- 작성일: 2026-07-12
- 상태: **부분완료** (코드·테스트 완료 / T0 실측·배포 미완)
- 브랜치: `feat/leveltest-fast-probe` (미커밋·미배포)
- 관련 파일: `core/gemini_live.py`, `core/persona_prompt.py`, `domains/learning/realtime/call_session.py`, `domains/learning/service/normalcall_service.py`, `tests/test_normalcall_ws.py`, `tests/test_persona_prompt.py`, `tests/test_level_test_call.py`

## 목표 & 범위
레벨테스트 통화를 "빠른 test-like 프로빙"으로 재설계. **3분 하드캡 + 비버가 천장 찾으면 조기종료**. 오프닝 한마디 대폭 단축. 레벨 계산은 통화후 분석 유지.
- 범위: 180초 캡 · 빠른 프롬프트 · 짧은 오프닝 · 무음 캐던스 단축 · 비대칭 채점 · 조기종료(tool-use 신호).
- 비범위: 클라 신규 신호(불필요), 통화중 정밀 13레벨 배치(밴드배치+자동레벨업이 자가치유).

## 아키텍처 & 데이터 흐름
- 길이: `call_type==level_test` → `call_duration_s = LEVELTEST_MAX_S(180)`. 워처·리그라운드·넛지는 이 한 값으로 흡수(무수정).
- 조기종료: 비버가 천장 확정 시 `leveltest_ceiling_reached()`(NON_BLOCKING function call, 무음) 호출 → `events()`가 `LiveEvent(kind="tool_call")` 방출 → 펌프가 감지(45초 플로어·1회성) → `should_close` → 기존 종료 파이프 합류(`_inject_close_seed`=`CLOSE_SEED_LEVELTEST` → 작별 → `_CallFinished`). 신호 없으면 180초 캡.
- 낭독 방지: out_tr sentinel은 native-audio가 소리 내어 읽어 불가(gemini-live 판정) → tool-use(별도 구조화 필드)로 근본 회피.
- graceful degradation(R5): tool 미발동·ack 실패해도 180캡 + SEED_TO_HANGUP(22s) + 540s 절대백스톱이 종료 보장.

## 구현 내역
### core/gemini_live.py
- `LEVELTEST_DONE_TOOL`(`leveltest_ceiling_reached`, `Behavior.NON_BLOCKING`). `build_live_config(tools=None)`/`open_session(tools=None)` — None이면 일반 통화 바이트 동일.
- `events()`에 `tool_call` 정규화(`LiveEvent(kind="tool_call", fn_name, fn_id)`, None-safe, 최상위 필드). `LiveSessionProtocol.send_tool_response(fn_id, fn_name)` + 구현(SDK 검증: `FunctionResponse.scheduling=SILENT`는 최상위 필드).

### core/persona_prompt.py (레벨테스트 전용 — 일반 경로 바이트 동일)
- **오프닝 단축**(`seed_leveltest_opening`): 긴 안심 멘트 제거 → "맞춤 수업 준비하게 몇 개만 물어볼게" + 1계단 질문을 한 호흡에. "긴 안심 멘트 금지" 명시.
- **빠른 프로빙**(`_DEFAULT_PROBE_PLAN`): 밴드별 4계단(과거 -았/었- → 미래+이유 → 간접화법 → 문어논증) + 상황단서 유도(번역투 금지). 이동규칙 "1회 자발성공→상승 / 애매하면 교차확인 / 2회 실패→천장".
- **천장 신호 규칙**: "천장 확정 시 `leveltest_ceiling_reached` 호출(소리 X). 실제 답 3회 이상 뒤에만. 신호≠작별(서버가 마무리)".

### domains/learning/service/normalcall_service.py
- `_leveltest_instruction`(통화후 채점): 계단별 목표자질 검출 + 비대칭 가중(자발=강한양성/유도=약한양성+청크 교차확인/유도실패=한밴드 하향) + 밴드→하단레벨 기본배치(A2→L3,B→L6,C→L10) + scorable<2면 L1~2 바닥.

### domains/learning/realtime/call_session.py
- 상수 `LEVELTEST_MAX_S=180`, `LEVELTEST_MIN_S=45`(조기종료 플로어), 무음 `25/8/10` + 레벨테스트 넛지 시드.
- `_resolve_call_duration(base=None)` — 런타임 CALL_DURATION_S 기본. level_test는 base=180.
- `run_call` level_test 분기: 캡·tools=[LEVELTEST_DONE_TOOL]·무음 필드 세팅.
- `_watch_call_clock` 첫 루프 `if should_close: break`(조기 close 하드닝 — GoAway/무음3단 잠복버그도 동시 수정).
- `_watch_idle` state 필드 기반 캐던스(레벨테스트 25/8/10 + "새 화제 말고 더 쉬운 발판" 시드).
- 펌프 `tool_call` 감지 → ack(SILENT) → 45초 플로어 → should_close → 종료 파이프 합류. M1(종료중 중복신호 무시)·M4(fn_id None 방어) 하드닝 반영. call-197 `user_turn_open` 레이스 가드 재사용.

## 테스트 결과 (실제 실행)
- **전체 스위트: 170 passed, 0 failed** (`pytest tests/ -q`, 2026-07-12).
- 신규 회귀(test_normalcall_ws.py 6건): 조기 천장신호→작별→종료, 45초 플로어 폐기, 신호없음→180캡, 무음 단축 분기, tools 전달 분기, 일반통화 무영향.
- 신규(test_persona_prompt.py): 천장 신호 블록 스냅샷.
- 수정: test_level_test_call.py 가짜 factory에 `tools=None` 추가(신 계약 반영), 시드 assertion "실력"→"맞춤 수업"(오프닝 단축 반영).
- 시니어 리뷰(동시성): **blocker 0, major 0**. minor M1·M4 반영, M2·M3는 기존 성질(이 브랜치 회귀 아님).

## 미해결 / 후속 작업 (TODO)
- **T0 실측 (최우선·미완)**: `gemini-live-2.5-flash-native-audio`에서 NON_BLOCKING function call이 **실제로 발동**하는지 1~2콜 실측 = 조기종료 go/no-go. **미검증** — 배포+실통화 필요.
  - 실패 시 폴백: 자연 마무리 문구 NLU 또는 서버 규칙기반 천장판정.
- 커밋(브랜치 `feat/leveltest-fast-probe`, 미커밋) / 배포(미배포 — 사장님 확인 후).
- minor M2(user_turn_open 고착 시 작별 스킵 — 희귀, 백스톱이 잡음), M3(call_start 전 신호 — 도달 불가) 후속 하드닝 여지.

## 리스크 & 결정 사항
- **R-A(관문)**: native-audio function calling 실발동 미검증 → T0가 가름. 미검증 상태로 배포 금지.
- **R-B**: 조기종료 오탐 → 45초 플로어 + "실제 답 3회" + 통화후 표본게이트(자가치유) 3중 방어.
- **결정**: 조기종료+3분캡 둘 다(D1), 신호=tool-use 1순위 T0 후(D2), 오프닝 단축(D3).
- 관통 원칙 준수: 레벨은 통화후 계산(신호는 "그만"만 전달), 일반 통화 경로 바이트/행동 동일, R4 불변식(2펌프·백스톱·barge-in off·종료규약) 유지.
