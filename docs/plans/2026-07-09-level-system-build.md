# 레벨 시스템 구축 (레벨테스트 콜 · 체크판 · 자동 레벨업) — /build 최종 플랜

- 작성일: 2026-07-09 · 상태: **서버 구현 완료(P0~P2), DB 적용 대기**
- 기준 문서: `docs/20260709_1231_level-system-master-plan.md`(결정 D1~D14), `docs/20260709_1346_level-system-detailed-mechanics.md`(동작 명세), `docs/20260709_1621_level-system-overview-for-stakeholders.md`(대외 소개)
- 관련 파일: 아래 "구현 내역" 참조

## 목표 & 범위
- 첫 통화 = 대화로 위장한 레벨테스트(서버 자동 진입) → korean_level(1~13) 배정
- 커리큘럼(문법 459·어휘 10,636·생존청크 46)을 DB 항목화, 사용자별 체크판(미학습→배움→연습중→잘씀) 추적
- 통화 프롬프트에 공부(본편5+예비5)/대화(아는문법≤40+유도5) 블록 주입, 통화후 분석이 증거 기록
- 게이트(문법 전용 D12, 잘씀=성공 3회 D14) 충족 시 자동 승급, UI 비노출(승급은 비버 대사 D2·D3)
- 비범위: P2.5(레벨1 카드 UI — Flutter), P3(감쇠·리텐션 프로브·정체 구제·open-set 검출·다국어 뜻 생성)

## 아키텍처 & 데이터 흐름
```
xlsx(CEFR 최종본) → parse_xlsx.py → curriculum_v2/*.json → seed.py → learning_item(11,141행)
통화 시작: member 조회 → [레벨null? → 레벨테스트 대본] / [일반: 선별쿼리 → 프롬프트 블록 주입]
통화 종료: 분석 1콜(요약+표현+detections) → 서버 검증 게이트 5단 → item_evidence(append-only)
         → 상태 전이·fast-track → 유효통화 산출 → evaluate_level_up(멱등 3중) → 단일 commit
레벨테스트: 판정 1콜(밴드→단계 CoT) → 클램프 → korean_level + grandfathering 단일 commit
⛔ 불변식(R4): _run_session·2펌프·10분 백스톱·barge-in off 코드 경로 무변경(시니어 리뷰 3회 확인)
```

## 구현 내역 (파일/모듈별)
**데이터 파이프라인 (P0 + CEFR 개정)**
- `scripts/curriculum/parse_xlsx.py`·`assign_levels.py` — xlsx→JSON, 동형이의어 파서, 오버라이드 11건(오타 7+문법 정리 4), 어휘 배분 cefr_v1(매칭 100%)·예문 인라인 100%, core 선정(빈도40+품사35+교재25, 쿼터), 문법 45 상한, 검증 내장·멱등
- `assets/level/curriculum_v2/{grammar,vocab,overrides,jamo,survival_chunks}.json`, `level_profiles_13.json`(실측 재산출·CEFR 라벨), `source/`(원본 2종)
- `scripts/seed.py` — seed_levels(13) + seed_learning_items(source_key 멱등 upsert, stale 리포트+--prune)

**모델·마이그레이션 (R2: 같은 커밋 필수)**
- 모델: `learning_item`(29컬럼, kind grammar/vocab/chunk), `member_item_progress`(희소 체크판), `item_evidence`(append-only), `member_level_history`(trigger_call UNIQUE 멱등), `call` +6컬럼(call_type/assessed_level/assessment_note/is_valid_call/user_turn_count/user_char_count)
- Alembic 체인: `e5f6a7b8c9d0`(13레벨 shift — **파괴적, prod 백업+사장님 확인 R6, 사전상태 가드**) → `f6a7b8c9d0e1`(learning_item) → `a7b8c9d0e1f2`(call 판정 컬럼) → `b8c9d0e1f2a3`(체크판 3테이블+call 유효통화)

**레벨테스트 (P1)**
- `core/persona_prompt.py` — `_RULE_CLOSE_PROTOCOL` 상수화(기존 출력 바이트 동일, 스냅샷 테스트), `build_leveltest_instruction`(4계단 프로빙·교정 금지·code-switching 역전), 시드 2종
- `normalcall_service.py` — `LevelAssessment`(CoT 필드 순서), 클램프(밴드 재계산·낮은 쪽·모순=failed), <20자 스킵, `_save_level_assessment`(+grandfathering, FOR UPDATE, 단일 commit), `get_status_detail`
- `call_session.py` — 라우팅(명시 우선, 단 데모/prod재측정 강등 → 자동: korean_level null), `protocol.py` call_type 선택 필드, `MemberRead.korean_level`, status 응답 확장

**체크판·레벨업 (P2)**
- `mastery_service.py` — 증거 적립(점수 Δ·상한: 순증2.0/승격 문법2·어휘6·청크4/신규8/FT2), 상태 전이(D14: 성공 산출 3회·2통화·2일·E3≥1, chunk 2회 특례), fast-track 5조건+확정/복귀, `evaluate_level_up`(D12: 문법+L1청크 분모, GATE_PARAMS 밴드별, 멱등 3중, gate_snapshot), `apply_grandfathering`(게이트 대상만 행 생성)
- `mastery_repository.py` — 선별(pick_study_items 밴드 구성+SEL2/SEL3, pick_chat_targets, known_grammar), 브리지/버벅임 파생 계산, promotion_pending, 게이트 집계, `has_call_evidence`(M4 멱등 가드)
- `normalcall_service.py` — detections 병합(closed-set ≤30·후보 0이면 스키마 스위치), `_verify_detections` 5단(인용 검증·에코 강등·dedup), `_apply_call_mastery`(M1 선두 FOR UPDATE + M4 가드 + 단일 commit), `load_call_setup` 확장(study/known/topics/promotion/candidates — 전부 R5 폴백)
- `core/persona_prompt.py` — 공부/대화 블록·L1 변형·승급 알림(신 인자 None이면 바이트 동일), `core/curriculum_hints.py`(freetalking 유도 힌트)

## 테스트 결과 (실제 실행)
- **전체 `pytest tests/` : 137 passed, 0 failed** (conda env beavertalk-server)
- 구성: 기존 45 + P0 커리큘럼 20 + persona 25 + 레벨테스트 22 + mastery 11 + 선별 13 (+기타)
- 시니어 리뷰 3회(P0/P1/P2): **blocker 0**. major 전건 수정 완료 — P0 4건(프로파일 실측·문법 상한·Rev1 가드·stale 리포트), P1 6건(데모 오염·prod 재측정 강등·클램프·20자 가드·원자성·로그), P2 2건 수정(M1 동시성 증거 유실 → 선두 FOR UPDATE / M4 재실행 이중 적립 → call 단위 가드) + 2건 이연 문서화(아래)
- 스모크(sqlite): 시드 멱등(11,141행), mastery 5파트, 선별 30/30, 레벨테스트 라우팅 4케이스

## 미해결 / 후속 작업 (TODO)
0. **⚡ P2.6 결과 페이지 체감 속도 (다음 최우선 — 2026-07-09 사장님 지시)**: 요약+공부한 문장이 결과 화면에 바로 나오도록 분석 파이프라인 재정렬 — ①전사 선저장(오디오 업로드 병렬 분리) ②요약·표현 커밋 즉시 status=done ③TTS 는 done 후 백그라운드(온디맨드 폴백 기존재) ④체크판·승급도 done 후 계속(원자성 유지). 목표: 종료→결과 ~7초. 상세: 마스터 플랜 §9 P2.6
1. **DB 적용(사장님 확인 필요, R6)**: prod 백업 → `DATABASE_URL_DIRECT` 로 `alembic upgrade head`(4개 리비전) → `python scripts/seed.py` → 사후 검증(level 13행·learning_item 11,141행) → 회귀 pytest + 통화 스모크
2. **실 Gemini 라이브 스모크**: 레벨테스트 판정(nullable 필드·CoT 순서)·검출 1콜 — 모킹 아닌 실호출 1회 (P1 리뷰 #9)
3. **R2 커밋**: 모델+마이그레이션+curriculum_v2+seed 같은 커밋으로 묶기 (전부 미커밋 워킹트리 상태)
4. **P2.5**: teaching_plan/hint_used 프로토콜 + Flutter 카드 UI (유일한 클라 작업)
5. **P3 이연분(리뷰 기록)**: 정체 구제(30통화/45일 G2 완화·프로브 콜 — M3), SEL4 선발화 우선순위 강등(m1), 통화 간 hash 다양성(m4), 망각 감쇠·리텐션 프로브, 힌트→판정 반영, 어휘 다국어 뜻 배치 생성(krdict+LLM), 재측정 기능(쿨다운)
6. 관찰 항목: fast-track 발생률(m3 표면형 의존), user_char_count 모국어 포함(m5), survival 밴드 게이트 값(m6)

## 배포·운영 노트 (2026-07-09 dev 배포에서 실제 발생 — 재발 시 참조)

| 증상 | 원인 | 해결 |
|---|---|---|
| test-api 통화가 Gemini Live 1008 "invalid authentication credentials" | 장수명 Cloud Run 인스턴스가 **만료된 액세스 토큰을 Live WS 핸드셰이크에 재사용**(HTTP 호출은 자동 갱신되나 구버전 google-genai 의 Live 경로가 갱신 안 함). 키·프로젝트·권한은 정상(로컬 동일 키로 Live 연결 성공, 시크릿 키=로컬 키 동일 확인) | 인스턴스 재시작(`--revision-suffix` 무변경 롤아웃)으로 즉시 복구. 근본: 새 이미지 빌드 시 최신 google-genai 포함 |
| `gcloud run deploy --source` → "Container import failed" | 소스 배포가 만든 `:latest` 태그가 **멀티 매니페스트(이미지+증명)**를 가리켜 Cloud Run 이 임포트 거부 | `gcloud builds submit --tag`(클래식 단일 매니페스트)로 빌드 후 이미지/다이제스트 지정 배포 |
| 배포 후 `/__levelcalldemo` 500 (파일 부재) | `.gitignore` 에 미커밋 `scripts/` 줄이 추가돼 있었고, **`.gcloudignore` 부재 시 gcloud 가 `.gitignore` 를 그대로 따라** scripts 전체(seed.py 포함)가 빌드 업로드에서 제외됨 | `.gitignore` 의 `scripts/` 제거 + **빌드 전용 `.gcloudignore` 신설**(.dockerignore 동일 규칙 — gitignore 변경이 빌드에 영향 못 주게 분리) |

- dev 데모: `/__calldemo`(기존 통화·언어 데모 — 레벨 시스템 완전 격리) / `/__levelcalldemo`(레벨테스트 체험 — STEP0 계정별 로그인·신규 가입, "재측정 강제" 토글로 명시/자동 경로 모두 검증, 판정 레벨 표시. **테스트 계정 레벨을 실제로 덮어씀**)
- 캐릭터 정정(사장님 확정): 정식 캐릭터는 **BABA(1)·BIBI(2)** 뿐 — seed 의 구 4종(비비·주디·레오·미나)은 잘못된 시드였음. dev DB 에서 5~8 삭제, seed_characters 는 존재 확인 전용으로 교체.

## 리스크 & 결정 사항
- 결정 D1~D14는 마스터 플랜 §0 참조. 핵심: 레벨·체크판 UI 비노출 / 서버 자동 라우팅 / 승급=문법 전용 / 잘씀=성공 3회 / CEFR 최종본 / 강등 없음
- 리스크: Rev1 비가역(가드+백업으로 완화) / ASR 교정 왜곡(보수 판정) / LLM 검출 환각(인용 검증 코드 게이트) / 판정 품질은 출시 후 골든셋 튜닝(증거 로그 리플레이 가능)
