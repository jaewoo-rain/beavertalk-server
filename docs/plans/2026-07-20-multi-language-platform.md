# 실행 계획: 멀티랭귀지 플랫폼 (레벨테스트 + 통화 중심 · 발음 제외)

- 작성일: 2026-07-20
- 상태: **계획 확정 / 미구현** — `/build` 대기
- 브랜치: `feat/multi-language`
- 수립: CEO 오케스트레이션 + 전문 에이전트 5인 종합 → 사장님 기획 확정 반영(최종본)

## 확정된 기획 결정 (사장님)
- **목적 = 내부 도그푸딩**: 한국인(모국어 ko)이 **일본어 등 5개 언어**를 배우며 레벨 시스템을 우리가 직접 검증.
- **발음평가(SpeechSuper)는 제외** — 멀티랭귀지 통화는 **레벨테스트 + 대화 통화 + 체크판/레벨업 + 요약·한마디**만. 발음 리뷰/저장연습 흐름 없음.
- **5개 언어(cn/en/fr/jp/vi) 전부 적재** + **풀 레벨 시스템**(레벨테스트→체크판→자동 레벨업).
- **콘텐츠(레벨 프로파일·레벨테스트 앵커·생존청크)는 내가 데이터에서 초안 → 사장님 검수.**
- `learning_item`에 **읽기 필드**(일본어 かな 등) 추가.
- **통화마다 언어 선택**(call.target_language) — member 기본 학습언어 컬럼 없음. **기존 데모 콘솔에 언어 선택 UI 추가**로 도그푸딩.
- **골격 먼저(전부 'ko' 기본 → 한국어 바이트 불변) → 그 다음 콘텐츠·시드.**
- 라이선스: 내부 테스트라 도그푸딩 범위 OK(상용은 별건).

---

## 1. 목표 & 범위
**목표**: 단일언어(한국어) 학습 시스템을 **여러 target 언어(ko + en/ja/zh/fr/vi)** 로 확장. 한 DB의 **`language` 차원 + 사용자×언어별 레벨(`member_language_level`)**(여러 언어 동시 학습). 발음 제외, **레벨테스트+통화+체크판** 중심.

- **핵심 원칙**: 모든 통화(Call)는 정확히 하나의 target 언어(`call.target_language`)에 속하고, 그 값이 ①`member_language_level` 행 ②커리큘럼 선별 필터 ③증거·이력 집계 스코프를 전부 결정.
- **`member.language`(모국어=번역 locale)와 학습 대상 언어는 별개 축.** 모국어 무변경.
- **비범위**: 발음평가/리뷰 다국어화, 프론트 정식 구현(데모 콘솔만), 커리큘럼 라이선싱, 알람/복습 큐 언어축, 상용 출시.

## 2. 아키텍처 & 데이터 흐름
```
데모 콘솔 언어 선택 → start.target_language(코드) ─► _resolve_target_language ─► LanguageSpec
                                                          (code,label,level_count,has_curriculum,leveltest; is_demo 폐지)
                                                          ▼
 create_call(..., target_language=code)   → call.target_language 기록
 load_call_setup(db, member, char, lang)  → member_language_level[lang] 레벨 + lang 커리큘럼 선별 + needs_level_test(언어별)
 build_system_instruction(target_language=spec.label, locale_label=모국어)  ← 이미 파라미터화(무변경)
                                                          ▼ (통화 본체 _run_session: 언어 무관 — R4 무손상)
 _trigger_analysis(lang=call.target_language)
   ├ analyze_call: has_curriculum 게이트 + lang 스코프 검출/증거/레벨업(체크판)
   └ analyze_level_test_call: member_language_level[lang] 저장 + lang 루브릭
 (발음/리뷰 경로 = 이번 범위 제외)
```
**신규 파일**: `core/languages.py`(LanguageSpec), `domains/learning/models/member_language_level.py`, 언어별 커리큘럼 설정(band/gate/bucket/rubric) 레지스트리, 시드 일반화.

## 3. 작업 분해

### ▸ Phase 1 — 시스템 다국어化 (전부 'ko' 기본 → 한국어 불변)

**[T1] 스키마 + Alembic** — db-architect · *선행·블로킹*
- `learning_item.language`(Text, 백필'ko'), **UNIQUE(language,source_key)**, **FK(language,level_no)→level**, 인덱스 language 프리픽스. **읽기 필드 추가**(`reading` 또는 언어별 표기: jp かな·zh 병음 등 — nullable, 언어별 사용).
- `level.language`, **UNIQUE(language,level_no)**, 대리 level_id 유지.
- **신규 `member_language_level`**(member_id, language, level_no nullable; UNIQUE(member,language); 복합 FK).
- `member_level_history.language`(백필'ko', 인덱스 (member,language,created_at)).
- **`item_evidence.language`**(백필'ko', 인덱스 (member,language,call_id)) — member-only 집계 오염 방지.
- `call.target_language`(Text, server_default'ko'). `member_item_progress` language 컬럼 없음(learning_item 스코프).
- `sentence.korean_sentence`·`member.korean_level` 유지+주석/폴백(rename·drop 후속). **member 기본 학습언어 컬럼 없음**(통화마다 선택).
- Alembic Rev A(가법+백필, 비파괴) → Rev B(제약 수술·DDL 락·DIRECT·downgrade 가드). prod 백업(R6).

**[T2] 언어 레지스트리 + target_language 승격** — fastapi-expert · *T1 병렬(코드부)*
- `core/languages.py`: `LanguageSpec(code,label,level_count,has_curriculum,leveltest)` + `SUPPORTED_LANGUAGES`(ko/en/ja/zh/fr/vi) + `resolve_language` + `DEFAULT_LANGUAGE="ko"`.
- `_resolve_target_language`→LanguageSpec, **`is_demo` 폐지**. `inject_materials/enable_hints=spec.has_curriculum`, 레벨테스트=`spec.leveltest and needs_level_test`, `_LOCALE_LABEL["ko"]="한국어"` 추가.
- `settings.DEFAULT_TARGET_LANGUAGE="ko"`. target 출처: **start(코드) → 'ko'**(member 기본 없음). start `target_language`=언어코드. `create_call(...target_language)`. `trigger_reanalysis`가 call.target_language 사용.

**[T3] 레벨/체크판 language 스코프化** — mastery 설계→fastapi-expert 구현 · *T1 이후*
- 레벨 접근자 `get/upsert_language_level`(member.korean_level 대체, ko dual-read/write 폴백).
- 선별 쿼리 전부 `language` 필터. **member-only 집계(치명) 필터**: `get_latest_history`·`list_recent_evidence_call_ids`·`count_evidence_calls_since`·`_sel3_cooldown`·`evidence_grade_counts`·`list_unconfirmed_fast_track`.
- `evaluate_level_up(language)`·`apply_grandfathering(language)`·`_save_level_assessment(language)`·history(language). `band_of(level_no, lang)` + **언어별 설정 레지스트리**(max_level/band/gate/bucket/rubric; ko=현재값). needs_level_test 언어별·콜드스타트.

### ▸ Phase 2 — 5개 언어 적재 + 콘텐츠 + 데모 (Phase 1 검증 후)

**[T4] 시드 일반화 + 커리큘럼 적재** — mastery/데이터
- `parse_xlsx.py`·`seed.py` **language 인자** 일반화. `level/05.다른 언어 CEFR/{cn,en,fr,jp,vi}/` → learning_item(language·읽기 포함)·level 적재. source_key 언어 접두. `meanings`=locale 키 dict(ko 커버리지).
- 매핑 주의: **jp 사전형/표면/읽기 3필드, cn 한자+병음, fr (surface,POS), vi 빈도기반, en 최선.**

**[T5] 콘텐츠 저작(내가 초안→검수)** — prompt-persona-engineer + korean-linguist
- **`level.profile` 5언어×12단계**(grammar_12에서 문법 앵커 추출·저작).
- **레벨테스트**: 사다리를 기능 축(현재→과거→계획·이유→간접→가정·비교→의견) 추상화 + **언어별 문법 앵커**. 4버킷(입문/초급/중급/고급→level) 매핑 언어별.
- **생존청크 46×5 저작**(level 1 채움 — 인사·숫자·정형표현, surface+읽기+한국어뜻).
- **발음표기 조항**(zh 병음/jp かな/vi 성조부호 함께 제시) — 프롬프트 규칙.
- 전부 **내가 데이터에서 초안 → 사장님 검수**.

**[T6] 데모 콘솔 언어 선택** — fastapi-expert
- `level_call_demo.html`에 **target 언어 드롭다운**(ja/en/zh/fr/vi/ko) → start 메시지 target_language 전송. 언어별 레벨테스트·통화·결과 확인 가능하게.

### ▸ [T7] 테스트·검증 — test-engineer · *전체 이후*
- **회귀(최우선)**: 기존 ko 시나리오(증거→전이→승급→grandfathering, 통화 WS) language='ko' 명시 **바이트 동일**. R4 통화 불변식.
- 신규: 다언어 셋업/분석 라우팅, member-only 집계 언어 격리(ko/ja 안 섞임), member_language_level 읽기/쓰기, has_curriculum 게이트, 콜드스타트(언어별 레벨테스트).

**순서**: Phase 1(T1→T2·T3) → 검증 → Phase 2(T4→T5→T6) → T7.

## 4. 수용 기준 & 테스트 포인트
- **하위호환(필수)**: 기존 한국어 사용자·통화·체크판·승급 무손상, ko 통화 프롬프트·분석·선별 바이트 동일.
- **언어 격리**: 한 회원이 ko·ja 동시 학습 시 진도/증거/이력/레벨업 언어별 독립(교차 오염 0, 특히 진입시각/G4).
- **콜드스타트**: 새 언어 첫 통화=그 언어 레벨테스트, 기존 언어 무영향.
- **풀 시스템**: 일본어 통화로 레벨테스트→체크판 증거 적립→자동 레벨업이 실제로 도는지(도그푸딩 검증).
- **데모**: 콘솔에서 언어 선택→일본어 레벨테스트·통화·요약·한마디 확인.
- **레지스트리**: 새 언어 = LanguageSpec 1행 + DB 시드 + 콘텐츠, 코드 분기 없음.

## 5. 리스크 & 결정 사항
**CEO 확정 결정**:
- 발음(SpeechSuper) **범위 제외** · `item_evidence.language` 추가(집계 오염 방지) · `is_demo` 폐지→`has_curriculum` · level_no 전역 고정(2~13≡A1~C4, 1≡생존) · `_LANG_POLICY` 공유/profile·앵커·생존청크 언어별 저작 · **통화마다 언어 선택**(member 기본 컬럼 없음) · 골격→콘텐츠 단계 빌드.

**리스크**:
1. **member-only 집계 오염(치명)**: language 필터 누락 시 ja 통화가 ko 진입시각 오염 → evidence/history.language + 필터 필수(T3).
2. **Rev B 제약 수술**: DDL 락·downgrade는 다국어 데이터 전까지만. prod 백업.
3. **콘텐츠 저작량**: profile 5×12 + 생존청크 46×5 + 레벨테스트 앵커 5 = 언어학 수기 자산(초안 내가·검수 사장님). Phase 2 병목.
4. **밴드 비등가성**: level_no 절대 CEFR가 언어마다 다름(vi 빈도축). 언어 간 비교 금지(비노출이라 안전). 내부 테스트라 수용.
5. **커리큘럼 매핑 정밀도**: 문법-예문 매칭 낮음 → examples 검수 or 비버 즉석 생성. cn 뜻 76.5% 검수.
6. **라이선스**: 내부 도그푸딩 OK, 상용은 데이터 교체 선결(범위 밖).

## 관련 파일(구현 대상)
- 모델: `domains/learning/models/{learning_item,level,call,sentence,member_level_history,item_evidence}.py`, 신규 `member_language_level.py`, `domains/account/models/member.py`, `db/registry.py`
- 코드: 신규 `core/languages.py`, `core/config.py`, `core/persona_prompt.py`, `domains/learning/realtime/{call_session,protocol}.py`, `domains/learning/service/{normalcall_service,mastery_service}.py`, `domains/learning/repository/mastery_repository.py`
- 시드/콘텐츠: `scripts/curriculum/parse_xlsx.py`, `scripts/seed.py`, `assets/level/`(profile·생존청크 저작), 데이터 `level/05.다른 언어 CEFR/`
- 데모: `scripts/level_call_demo.html`(언어 선택)
- Alembic: `alembic/versions/`(Rev A→Rev B, head `f2cd7402bede` 뒤)
