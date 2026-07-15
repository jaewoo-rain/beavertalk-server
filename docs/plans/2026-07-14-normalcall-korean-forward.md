# 일반통화 한국어 위주(레벨 적응) 전환

- 작성일: 2026-07-14
- 상태: **구현·시니어리뷰·테스트 완료 / 실통화 미검증 / 미커밋·미배포**
- 브랜치: `feat/leveltest-fast-probe`
- 관련 파일: `core/persona_prompt.py`, `domains/learning/service/normalcall_service.py`, `domains/learning/realtime/call_session.py`, `main.py`, `tests/test_persona_prompt.py`

## 목표 & 범위
일반 수업 통화(normalcall)를 "모국어 위주 → 한국어 위주(레벨 적응)"로 전환해 **학습자가 한국어를 실제로 산출**하게 만든다(그래야 통화후 문장추출·발음평가·검출 = 복습 재료가 나옴). 기존 `한국어 10%+모국어 90%, 레벨 무관`이 학습자 산출을 굶기고 있었음.

**핵심 원리(전문가 7인 합의):**
- 두 축 분리: 비버 한국어 input % ≠ 학습자 한국어 output. 목표는 output.
- 끝-한국어(end-loaded): 모국어 발판 먼저 → 한국어 질문 맨 끝 → 멈춤. 상대는 마지막 언어에 정렬.
- 퍼센트 아니라 행동 규칙(LLM은 % 자기검증 불가 — 실측상 규칙기반 10%는 정확히 지켜짐).
- 밴드 무차별 flip 금지(입문 과유도 → 오류↑ → G4 승급지연 역설).

**비범위:** 파이프라인(mastery/analyze_call/추출·검출·게이트) 무변경(추출 필터가 이미 보수적). 레벨테스트 통화 무변경(별 함수·별 대본). 재접지 주기주입 안 함(실측상 5분 드리프트 비유의). R4 불변식 보호.

## 아키텍처 & 데이터 흐름
```
member.korean_level(1~13) [load_call_setup]
  → level_no(미확정 폴백 2) → band_of() → lang_band ∈ {survival,beginner,intermediate,advanced}
  → setup["lang_band"]  [call_session.run_call]
  → build_system_instruction(..., lang_band=…)   ← 순수 어댑터(band_of 임포트 안 함)
       규칙3 = _LANG_POLICY[lang_band](.format 선처리) + 전 밴드 공통 규칙
  → Gemini Live system_instruction(1회)
재접지(50% on_user_turn 몰래 주입): 캐릭터 톤만, 언어규칙은 처음 지시대로 유지(문구만 정합)
```

## 밴드별 언어 정책 (행동 규칙 — 퍼센트 없음)
| 밴드 | 비버 한국어 | 모국어 | 학습자 유도 |
|---|---|---|---|
| survival(입문) | 가르치는 표현·청크·짧은 칭찬만 | 설명·지시·리액션 | 따라 말하기, 새 표현 하나만 |
| beginner(초급) | 짧은 질문·리액션·예문 | 새 단어 뜻풀이 | 짧은 문장, 막히면 선택지→모국어힌트→재시도 |
| intermediate(중급) | 질문·리액션 대부분 | 새 문법·복잡한 개념 | 온전한 문장, 모국어답→재유도 |
| advanced(고급) | 기본 전부 | 막힐 때 구제용 | 복문·긴 담화·의견 자유 |

**전 밴드 공통 불변(규칙 3):** 모국어 발판→한국어 질문 착지→멈춤(자문자답 금지) / 학습자 턴 한국어 0 금지 / **몰이해·질문시 즉시 모국어 설명(비율보다 우선)** / 절·문장단위 코드스위칭 / 이해>비율.

## 구현 내역
- **persona_prompt.py**: `_LANG_POLICY` 4밴드 상수 신설. `_INVARIANTS_TEMPLATE` 규칙3 전면 교체(옛 10%/90% 삭제, `{lang_policy}` 슬롯 + 공통 규칙). `[모국어]` 헤더 "모국어 위주" 삭제. `build_system_instruction(..., lang_band="beginner")` kwarg + lang_policy 선처리 주입. `build_reground_reminder` 끝 문구 정합(메커니즘·시그니처 무변경).
- **normalcall_service.py**: `load_call_setup` 반환에 `"lang_band": band_of(level_no)` 추가(신규 계산 0).
- **call_session.py**: 일반통화 `build_system_instruction` 호출에 `lang_band=setup.get("lang_band","beginner")` 전달.
- **main.py**: `/__preview` 데모도 lang_band 전달(미리보기 충실성 — 시니어리뷰 M1).

## 테스트 결과 (실제 실행)
- **`pytest tests/` → 187 passed / 0 failed**(경고 1: 무관한 Starlette deprecation).
- `test_persona_prompt.py` 38 passed: 스냅샷 3종 의도적 재기준화 + 밴드 4케이스·공통규칙·폴백·[모국어]헤더·reground 문구 신규 테스트.
- **회귀 게이트 무변경 통과**: `test_normalcall_ws.py`·`test_level_test_call.py`·레벨테스트 프롬프트 테스트 전부.

## 시니어 리뷰 (python-expert)
- **blocker 0.** `.format` 이중포맷 안전(치환값 재스캔 안 함), replace×규칙3 독립, 밴드 폴백 정합, 하위호환(kwarg-only), 레벨테스트 격리 확인, R4 무영향, 어댑터 순수성 유지.
- M1(`/__preview` lang_band 미전달) **수정 완료**. m1(비한국어 데모 밴드 무의미 주입)·m2(폴백밴드 미소비)는 데모 한정 minor·후속.

## 미해결 / 후속 작업 (TODO)
- **실통화 검증**: 밴드별로 학습자 한국어 산출이 실제로 느는지, 입문자 이탈 안 하는지 = 배포+실통화.
- **측정 하니스 편입**: llm-behavior의 `measure_drift.py`(학습자 한국어 글자수 = 1차 지표) 편입 — 이번엔 안 함.
- **Phase 2(측정 후 조건부)**: 무음 1단 넛지(`_NUDGE_SEED_1`) repair화(R4 인접), 재접지에 언어규율 추가(드리프트 실측시).
- 커밋·배포 미실행.

## 리스크 & 결정 사항
- **결정**: 밴드 적응 행동규칙(무차별 flip 금지). 퍼센트 프롬프트 미기재. 끝-한국어+자문자답금지가 핵심 지렛대. 몰이해시 모국어 설명은 하드 규칙(비율보다 우선).
- **결정**: 파이프라인·레벨테스트 무변경. 재접지 메커니즘 무변경(문구만).
- **미검증 가정(실측 필요)**: "끝-한국어가 학습자 산출을 늘린다"는 정렬이론 예측이나 BeaverTalk 미확인. 밴드 비율 수치 잠정(극단 밴드 이해붕괴 감시). 저밴드 과유도→G4 승급지연 리스크(입문은 현행+부드러운 유도만으로 완화).
