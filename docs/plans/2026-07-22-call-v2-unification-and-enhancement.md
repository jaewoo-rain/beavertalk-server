# 실행 계획: 통화 코어 통합(feat/call-v2) → 통화 고도화(드리프트 재접지·종료 타이밍·페이즈 통화)

- **작성일**: 2026-07-22
- **상태**: 계획 확정 · 미구현
- **브랜치**: `feat/call-v2` (feat/multi-language 토대 + feat/cloud-tts 얹기)
- **관련 파일**: core/persona_prompt.py, core/tts.py, core/stt.py, domains/learning/realtime/{call_session,ws_router,protocol}.py, domains/learning/service/normalcall_service.py, domains/learning/repository/mastery_repository.py, domains/learning/service/mastery_service.py, domains/learning/models/{call,member_language_level,item_evidence,member_level_history,learning_item,level}.py, alembic/versions/{71a272903bb6,f10b3c05cf8e}.py, tests/*
- **전문가 패널**: db-architect · fastapi-architect · conversation-design-architect · prompt-persona-engineer · mastery-system-engineer · test-engineer

---

## 1. 목표 & 범위

**목표(한 줄)**: `feat/multi-language`(통화 코어=언어차원 소유)를 토대로 `feat/cloud-tts`의 발음/STT/TTS/국적 기능을 얹은 통합 백엔드 `feat/call-v2`를 만들고, 그 위에서 통화 고도화 3종(페르소나 드리프트 재접지·종료 타이밍 개선·페이즈 통화)을 구현한다.

**MVP 범위**
- Phase 0: 두 브랜치 통합(1개 백엔드 + `target_language` 차원). "기능 동등" 회귀 그린이 완료선.
- Phase 1: 관측 우선(observation-first) — 행동 변경 0, 로그만. 실통화에서 위반율·이탈신호·페이즈 경계 실측 → 임계 튜닝.
- Phase 2: 페이즈 통화(교육 → 복습). 가장 안전한 행동 변경.
- Phase 3: 규칙 재접지(위반감지 게이팅, 레벨테스트 반전규칙 우선).
- Phase 4: 종료 이탈의도(confirm-before-close). 오검출 리스크 최대 → 맨 마지막, Phase 1 데이터로 임계 확정 후.

**비범위**
- 프론트 코드베이스 통합(설치본 flavor 분리 유지, 후속). 발음평가·국적분류의 다국어화(ko-only 유지, dogfood가 안 부르면 그만).
- 서버측 STT/발음 결과의 신규 테이블 영속화(필요 시 f10b 뒤 신규 리비전으로 별도).

**핵심 설계 원칙(관통)**: 세 고도화 모두 **이미 코드에 존재하는 단일 검증된 척추** 위에 얹는다 — "서버가 신호(시각/관측/전사)로 판단 → `should_close`를 세우거나 리마인더를 arm → 단일소유권 파이프(`_inject_close_seed` / `send_reground`)로 시드 주입". 레벨테스트 밴드천장 조기종료(`call_session._band_observe_sidecar`)가 이 패턴의 살아있는 증거다. **새 종료 경로·2펌프·백스톱·barge-in을 만들지 않는다(R4).**

---

## 2. 아키텍처 & 데이터 흐름

### 2-1. 통합 방향 (코드 근거로 확정)
`git checkout feat/multi-language → git checkout -b feat/call-v2 → git merge feat/cloud-tts`. multilang이 **상위집합**임이 4가지로 확정:
1. **DB 진부분집합**: multilang이 `call.target_language`·`member_language_level`·`{item_evidence,member_level_history,learning_item,level}.language`·복합키를 **가법**으로 얹음. cloud-tts는 모델·마이그레이션 **0 변경**(전부 코드).
2. **추상화가 hack 흡수**: multilang의 `LanguageSpec`(`spec.code/label/leveltest/has_curriculum`)이 cloud-tts의 `is_demo`/`_DEMO_LOCALE_EXTRA` hack을 상위 개념으로 포섭 → 통합은 hack **삭제** 방향.
3. **cloud-tts 어댑터가 이미 다국어 인지**: `core/tts.py:synthesize(text, language, voice)`가 (언어·음색) 두 축 모두 지원.
4. **cloud-tts 잔여 기능 직교**: 발음리포트·SpeechSuper·서버 STT는 신규 파일/라우터라 언어스코프 코드와 무교차. 국적추론은 공통(origin/main 유래).

### 2-2. 마이그레이션 = 이미 선형 (merge revision 불필요)
실 down_revision 추적 결과 단일 사슬: `… → f2cd7402bede(국적, =cloud-tts head) → 71a272903bb6(가법+백필) → f10b3c05cf8e(복합키)`. `71a`의 down_revision이 정확히 cloud-tts head → **멀티헤드 없음, down_revision 재작성 불필요.** git merge 후 사슬 그대로 유효, `alembic heads`는 `f10b3c05cf8e` 단일이어야 정상.

### 2-3. 충돌 화해 지도 (~20충돌, 대부분 자동/통째)

| 파일 | 방식 | 근거 |
|---|---|---|
| core/persona_prompt.py | **multilang 통째** | 순수 가법(`{ladder}` 슬롯·`ko` 라벨·확장 CLOSE_SEED_LEVELTEST). `_LEVELTEST_LADDER_KO`가 옛 인라인과 **바이트 동일** → 스냅샷 통과 |
| mastery_repository.py / mastery_service.py | **multilang 통째** | cloud-tts 무변경. 단 "2파일"이 아니라 4모델+2마이그레이션+normalcall 배선의 **원자 세트** |
| models/call.py·member_language_level.py·마이그레이션 2종 | **multilang 통째** | 스키마 진부분집합. call.py는 cloud-tts가 안 건드림 → **무충돌** |
| core/tts.py | **cloud-tts 채택** | 상위 어댑터(언어+음색), 충돌 0 |
| call_session.py | **multilang base + 확인** | spec 배선 채택, `is_demo`/`_DEMO_LOCALE_EXTRA`/`locale_label_override` 잔재 삭제. `_trigger_nationality`(공통) 확인 |
| **normalcall_service.py** | **수동 3-way (유일 의미 충돌)** | 아래 ★ |

**★ 유일한 진짜 충돌 — `analyze_call`의 TTS 합성** (multilang=언어축 `tts.synthesize(korean, target_language)` / cloud-tts=음색축 `tts.synthesize(korean, voice=call_voice)`). 어댑터 `_resolve_voice(language, voice)`가 이미 둘을 결합(`{lang}-Chirp3-HD-{voice}`) → **양쪽 다 전달로 화해**:
```
call_voice = await run_db(session_factory, lambda db: _voice_for_call(db, call_id))   # cloud-tts 함수 이식
synthesized = await tts.synthesize(korean, _lang_code(target_language), voice=call_voice)  # 두 의도 보존
```
→ 일본어 문장은 `ja` 음성풀에서 통화 캐릭터 음색으로. `_voice_for_call`이 None/미지원이면 어댑터가 언어 기본음성 폴백(R5, 크래시 없음).

### 2-4. 통화 페이즈 오케스트레이션 (교육 → 복습)
- **상태**: `_CallState`(`__slots__`) 로컬 권위 — 재접지/밴드관측/무음과 일관. 필드 추가: `phase("teaching"|"review")`, `phase_switch_pending`, `phase_switched`(단일소유 가드), `phase_seed`(persona 상수, None=비활성). protocol에 `ServerPhaseChange`(클라 UI 배너용, **load-bearing 아님**, 선택).
- **시드**: `persona_prompt.PHASE_REVIEW_SEED`(종료시드와 동형, 대본 소유=persona). "새 항목 금지·오늘 다룬 표현 회상·**아직 작별 금지(종료는 서버)**"를 못박아 종료규약과 충돌 방지. `run_call`이 학습통화(study_items 有)에만 꽂고 레벨테스트·회화전용언어(`has_curriculum=False`)엔 None.
- **타이밍**: `REVIEW_AT_FRACTION≈0.70`(300s→210s). 재접지 0.5(150s) < 페이즈 0.70(210s) < 종료(300s) — 순서 겹침 없음. 레벨테스트(180s)는 페이즈 비활성.
- **삽입(R4 보존, Alt A 권장)**: 전용 `_watch_phase` 태스크(=`_watch_idle` 동형, TaskGroup 7번째 가법). idle 경계(`turn_id is None and not user_turn_open and not should_close and not close_seed_sent and not phase_switched`)에서 `phase_switched=True`(await 전 선점) 후 `send_text_turn(phase_seed)`. 종료 근처면 **페이즈가 양보**(종료 우선). 2펌프·백스톱·barge-in·종료독점 전부 불변. 폴백: `PHASE_MODE="watch_idle"|"on_user_turn"|"off"` 상수화.
- **공부/대화 모드와 직교**: 서버는 모드 무추적(불변규칙 유지), 페이즈는 서버 시계 결정. 복습 시드를 모드-불가지 문구("오늘 나눈 이야기·다룬 표현")로 → 모드 분기 불필요.

### 2-5. 재접지·종료 훅 배치 (2펌프 무변경, 사이드카 가법)
- **드리프트 재접지 = 2트랙**:
  - *톤 트랙(현행 유지)*: 시간기반 arm @50%, `build_reground_reminder`, on_user_turn, 일반통화만.
  - *규칙 트랙(신규)*: **위반 감지 시에만 arm**(무조건 주기 아님 → 자기조절 → 잔소리 방지 = 캐릭터 납작화 방지). 감지는 결정론적 휴리스틱(LLM 사이드카 불필요): 에코(레벨테스트만), 목표어 과다, 모국어답 방치. 통화당 하드캡 2회. 얹기는 기존 `send_reground(turn_complete=False)` 재사용. **레벨테스트의 비어있는 `reground_reminder` 슬롯을 규칙 버전으로 활성**.
- **종료 이탈의도 = confirm-before-close**: `_pump_gemini_to_client`의 in_tr 분기(유저 전사 지점)에 `_spawn_exit_intent` 사이드카(=`_spawn_band_observe` 동형, 논블로킹 create_task, in-flight 가드). 상태기계: `NORMAL→(약한 이탈)HELD[비버 붙잡기]→(재차/강한 이탈, count≥2 또는 강신호 1발)should_close`. `elapsed < FLOOR(60~90s)`면 무시. 감지기는 **`should_close`만 세움**(시드 직접 주입 금지) → 실제 종료는 항상 `_inject_close_seed` 한 곳(서버 독점·단일소유 유지). 전용 시드 `_CLOSE_SEED_USER_INTENT`(시간 언급 금지, "이번엔 붙잡지 말고 보내줘라").

---

## 3. 작업 분해 (Task Breakdown)

### Phase 0 — 통합(feat/call-v2) · "기능 동등"까지 [순차, 최상류]
- [ ] **T0-1 브랜치·머지** (fastapi-expert) — `git checkout feat/multi-language && git checkout -b feat/call-v2 && git merge feat/cloud-tts`. 5/6 파일 multilang 통째 채택. 의존: 없음.
- [ ] **T0-2 normalcall_service 수동 3-way** (fastapi-expert) — multilang 언어배선 전량 채택 + `_voice_for_call` 이식 + `analyze_call` TTS를 `tts.synthesize(korean, _lang_code(target_language), voice=call_voice)`로 합성 + import 합집합. 의존: T0-1.
- [ ] **T0-3 call_session 정리** (fastapi-expert) — spec 배선 채택, `is_demo`/`_DEMO_LOCALE_EXTRA`/`locale_label_override` 삭제, `_trigger_nationality` 공통 확인, 서버 STT/pron 훅이 call_session 미접촉 확인(grep). 의존: T0-1.
- [ ] **T0-4 마이그레이션 정합(R2)** (db-architect) — dev DB `alembic current`/`heads` 실측 → `upgrade head`(71a 백필·member 이관 → f10b 복합키) → `alembic check`(모델 drift 0) → downgrade 왕복. **백필 UPDATE 4종+이관 INSERT가 머지 중 유실 안 됐는지 검토**. 의존: T0-1.
- [ ] **T0-5 mastery 원자세트 확인** (mastery-system-engineer) — base 4모델의 `.language` 필드 생존 + normalcall `call_language` 배선 생존 확인. 의존: T0-2,T0-4.
- [ ] **T0-6 conftest 추출**(선행 리팩터) (test-engineer) — `FakeWebSocket/FakeLiveSession/session_factory/seeded/_mock_external/_auth/…`를 `tests/conftest.py`로 이동, `test_normalcall_ws.py` 전량 그린으로 동치 확인. 의존: T0-1.
- [ ] **T0-7 통합 회귀 게이트(R4)** (test-engineer) — Stage0 정적(import·create_app 무키기동) → Stage1(normalcall_ws·persona 바이트스냅샷·어댑터 폴백 전량 그린). persona 스냅샷 깨지면 **진행 중단**(ko 오염). 의존: T0-2~T0-6. **← Phase 0 완료선**

### Phase 1 — 관측 우선(observation-first) [행동 변경 0, R4 무위험]
- [ ] **T1-1 관측 계측** (fastapi-expert + conversation-design-architect) — 위반 휴리스틱·이탈신호·페이즈 경계를 **로그만** 찍기(`seed_event=` 통일 태그, turn_index, elapsed). 실통화에서 빈도·오검출률 실측 → FLOOR·FRACTION·상한 파라미터 확정. 의존: Phase 0.

### Phase 2 — 페이즈 통화 [가장 안전한 행동 변경]
- [ ] **T2-1 시드 문자열** (prompt-persona-engineer) — `PHASE_REVIEW_SEED`(+ 선택 `seed_phase_teaching`), target/locale 파라미터화, None 하위호환, "캐릭터 톤 그대로/먼저 작별 금지" 명시. 회상질문 타당성 korean-linguist 검수.
- [ ] **T2-2 오케스트레이션** (fastapi-expert) — `_CallState` 4필드 + `_watch_phase`(Alt A) + TaskGroup 등록 + `PHASE_MODE` 스위치 + (선택)`ServerPhaseChange`. 의존: T2-1.
- [ ] **T2-3 테스트** (test-engineer) — `test_call_phase.py`(1회 주입·should_close 우선·중복 없음) + R4 회귀 재실행. 의존: T2-2.

### Phase 3 — 규칙 재접지 [위반감지 게이팅]
- [ ] **T3-1 문자열** (prompt-persona-engineer) — `build_reground_reminder(+target,+locale None하위호환)` 꼬리 앵커 + `build_reground_reminder_leveltest`(반전+에코 재주입). "정체성 나열 금지·캐릭터 목소리로 행동" 원칙.
- [ ] **T3-2 위반감지+배선** (fastapi-expert + llm-behavior-researcher) — 결정론 휴리스틱(에코=레벨테스트만), arm→on_user_turn 얹기, 하드캡 2회, 종료근처 금지 가드. 의존: T3-1, Phase 1 데이터.
- [ ] **T3-3 테스트** (test-engineer) — `test_leveltest_reground_gating.py`(위반→재접지1회, 정상→0회, 종료근처 금지). 의존: T3-2.

### Phase 4 — 종료 이탈의도 [오검출 리스크 최대, 맨 마지막]
- [ ] **T4-1 문자열** (prompt-persona-engineer) — `_CLOSE_SEED_USER_INTENT`(시간언급 금지) + 레벨테스트 변형.
- [ ] **T4-2 감지+배선** (fastapi-expert + llm-behavior-researcher) — `_spawn_exit_intent` 사이드카, confirm-before-close 상태기계, FLOOR 가드, `should_close`만 세움. 임계는 Phase 1 데이터로 확정. 의존: T4-1, Phase 1.
- [ ] **T4-3 테스트** (test-engineer) — `test_call_leave_intent.py`(단발 붙잡기/재차 종료/FLOOR 무시/오검출 억제) + R4 회귀. 의존: T4-2.

### 병렬 가능
- Phase 0 내부: T0-4(마이그)·T0-6(conftest)는 T0-2/T0-3와 병렬. 각 Phase의 시드(prompt)와 테스트 설계는 오케스트레이션 구현과 부분 병렬.
- Phase 2/3/4는 **각각 독립 커밋 + R4 게이트**. 순서는 리스크 오름차순(2→3→4) 권장이나 2·3은 병렬 가능(4는 Phase 1 데이터 의존).

---

## 4. 수용 기준 & 테스트 포인트 (3게이트, 전부 conda env)

`PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python -m pytest tests/ -q`

### 게이트 1 — 통합 회귀(R4, 최우선)
- normalcall WS 불변식 전량 그린(백스톱 540s·종료 서버독점·무음3단·GoAway·재접지 1회/종료근처 금지). **구멍 보강**: `test_periodic_flush_writes_partial_segments`(1분 flush), `test_genai_none_disables_call_keeps_app_up`.
- **persona_prompt 바이트 동일**: `target_language="ko"/default` 경로가 동결 원본과 바이트 동일(하위호환 계약). 비-ko는 신규 스냅샷으로 고정. **1건이라도 깨지면 ko 오염 → 진행 중단**.
- 어댑터 graceful: SpeechSuper/STT(`STT_FAKE`)/문장TTS(503)/발음리포트(404)/Cloud TTS 키부재 폴백.

### 게이트 2 — 다국어 정합
- `test_call_defaults_target_language_ko`(server_default 'ko'), `test_selection_scoped_to_target_language`(ko/en 혼재 회원 선별 격리).
- `test_levelup_isolated_per_language`(ko 증거 3회 → ko만 승급, en 불변), **언어 누수 회귀**(en 증거가 ko 게이트에 집계되면 실패), 진입시각 오염 차단(`get_latest_history(language)`), fast-track 격리.
- **마이그레이션 왕복**(`@pytest.mark.pg`, dev DIRECT 격리 스키마): `test_composite_key_downgrade_blocks_multilang_data` — `language!=ko` 행 있으면 f10b downgrade가 `RuntimeError`(비가역 가드), ko-only는 통과.

### 게이트 3 — 통화 고도화 신규 (구현 동반, FakeLiveSession/시간 모킹)
- 페이즈: 경계 idle에 복습시드 1회·should_close 우선·중복 없음.
- 종료 이탈: 단발 붙잡기 / 재차 종료 / FLOOR 이전 무시 / 오검출("배고파 가지고" 등) 억제.
- 규칙 재접지: 레벨테스트 반전규칙 위반→재접지1회 / 정상→0회 / 종료근처 금지.
- **전부 R4 불변식 회귀를 곁들여** 신규 로직이 2펌프·백스톱·barge-in·종료독점을 안 깨는지 확인.

### CI 순서
Stage0 정적(import/create_app 무키) → Stage1 통합회귀 → Stage2 다국어정합 → Stage3 고도화 → Stage4 전량 스위프(0 failed). asyncio는 명시 `@pytest.mark.asyncio`(레포 strict). smoke는 별개(수동, 실서버).

---

## 5. 리스크 & 결정 사항

### 리스크
- **R-A. `f10b3c05cf8e` 복합키 수술 — 준파괴적·prod 락·비가역 downgrade**. `ACCESS EXCLUSIVE` 짧은 락, `language!=ko` 데이터 있으면 downgrade가 `RuntimeError`(백업 복원으로만). → **prod 적용 전 스냅샷 백업(R6)·저트래픽 창**.
- **R-B. 공유 dev DB(demo-api/test-api 동일 DB)**. 한쪽에서 f10b 적용 시 다른 서비스 구코드가 복합키 스키마를 봄 → 런타임 드리프트. **DB 마이그레이션과 양 서비스 코드 배포를 한 창구에서 조율**.
- **R-C. 백필 UPDATE 유실**. `71a` 백필 4종+member 이관 INSERT는 autogenerate 미생성(수동) → 머지 중 축약/재생성되면 language=NULL→NOT NULL 승격 실패. **파일 그대로 보존 검토 후 커밋**.
- **R-D. `normalcall_service.py` 수동 3-way**. 자동 ours/theirs 금지 — multilang 언어배선 + cloud-tts TTS 합성 **둘 다 보존** 필수. 잘못하면 다국어 통화가 ko로 오집계 or 캐릭터 음색 소실.
- **R-E. Gemini 오디오턴+텍스트 병합 미보장(재접지 T7)**. `turn_complete=False` 얹기가 이중발화 유발 가능 → `REGROUND_MODE/PHASE_MODE="off"` 한 줄 폴백 확보. gemini-live-expert 실측 필요.
- **R-F. 이탈의도 오검출**. 화제전환/습관 bye를 종료로 오인 → confirm-before-close 1회 버퍼 + FLOOR + Phase 1 실측 임계로 방어. 그래서 **맨 마지막**.

### 결정 사항 (확정)
- **D1. 통합 방향** = multilang base + cloud-tts merge (상위집합 근거 4). 새 브랜치 `feat/call-v2`.
- **D2. 마이그레이션** = 선형 재접합, merge revision·down_revision 재작성 불필요.
- **D3. TTS 화해** = 언어+음색 둘 다 전달(`_resolve_voice`가 결합).
- **D4. 재접지** = 2트랙(톤 현행 + 규칙 위반감지 게이팅). 무조건 주기 금지(캐릭터 보존).
- **D5. 종료** = confirm-before-close + FLOOR. 감지기는 `should_close`만, 종료독점 유지(R4).
- **D6. 페이즈** = 서버시계 2페이즈, `_watch_phase`(Alt A), 레벨테스트 비활성.
- **D7. 순서** = 통합 → 관측우선 → 페이즈 → 규칙재접지 → 이탈의도(리스크 오름차순).
- **D8. 스냅샷 재동결** = cloud-tts 페르소나 변경이 동결본을 건드리면, diff를 사람이 검토·docs 기록 후 재동결(그 전 Stage1 레드는 정상 신호). **(구현 시 확인 포인트)**

### 미해결/가정
- dev DB의 정확한 현재 리비전은 T0-4에서 실측(가정 금지).
- `PHASE_MODE`/규칙재접지 하드캡·FLOOR 구체 수치는 Phase 1 실측으로 확정.
- 서버 STT/발음 결과 영속화 여부(현재 비영속) — 필요 시 별도 마이그레이션.
