# 레벨 시스템 마스터 플랜 — 13레벨 · 레벨테스트 콜 · 체크판(숙달도) · 자동 레벨업

> 작성: 2026-07-09 12:31 (CEO 오케스트레이션 — product-designer / fastapi-architect / korean-linguist /
> prompt-persona-engineer / flutter-integration-engineer / mastery-system-engineer / db-architect 합동 설계)
> 상태: **사장님 승인 완료, 구현 착수 대기.** 이 문서가 /build 의 단일 기준이다.
> 동반 문서: **`20260709_1346_level-system-detailed-mechanics.md`** — 각 부품의 구현 직전 수준
> 동작 명세(선별 쿼리·검증 게이트 순서·상태 전이·판정 의사코드·파라미터 총괄표). 구현 시 필독.

---

## 0. 경영 확정 사항 (사장님 결정 — 변경 시 사장님 승인 필요)

| # | 결정 | 내용 |
|---|---|---|
| D1 | 레벨 구조 | **내부 13레벨**(L1 생존회화 + L2~L13 = 교재 12권). 4단계(초/중/고+생존)는 밴드 메타로만 |
| D2 | **레벨·체크판 UI 비노출** | 사용자는 자기 레벨 숫자·항목별 상태·진행률을 **보지 못한다**. 전부 서버 내부 기계장치 |
| D3 | 승급 통지 | UI 알림 없음. **승급 후 첫 통화에서 비버가 자연스럽게 언급**("저번에 잘했으니 오늘은 조금 어려운 것도 해볼까?") |
| D4 | 공부 모드 로드량 | **본편 5 + 예비 5 = 10개** 미리 로드(가르치는 건 한 번에 하나, 실소화 4~6개, 미소화분 다음 통화 이월) |
| D5 | 레벨테스트 방식 | 퀴즈("이 단어 알아요?") 금지. **자연스러운 대화로 위장한 계단식 프로빙** + 통화후 전사 판정 |
| D6 | 레벨업 게이트 | **AND 조건**: 배움(커버리지) 90% && 잘씀(숙달) 55%(초급 기준). "30%만 배우고 다 잘씀" 통과 불가 |
| D7 | 1단계 내용 | 자모 아님(원본 파일도 "(보류)"). **생존 회화 청크**(안녕하세요·반갑습니다·좋아요 류) 46개 초안 기반 |
| D8 | 데이터 마스터 | `level/한국어_4단계_통합.xlsx` 를 새 마스터로 승격(원본 대조 완료 — 무결). 기존 assets 12레벨은 legacy 이관 |
| D9 | '명02'→'병02' 교정 | 증거 확정(병01 존재·병02 부재·길잡이말 "병에 담다") — 오버라이드로 교정 진행 |
| D10 | ~~프론트 레벨 캐시~~ | **D11로 대체** — 서버 자동 라우팅 채택으로 프론트 캐시·게이트 자체가 불필요해짐 |
| D11 | **레벨테스트 진입 = 서버 자동** | 클라 무수정: start 수신 시 서버가 `korean_level is null` 이면 레벨테스트로 자동 진행(WS 핸들러가 어차피 member를 읽으므로 추가 비용 0). `ClientStart.call_type`은 선택 필드로 유지(미래 재측정용 명시 요청 통로). "시험 아님" 안내는 인터스티셜 대신 비버가 통화 첫마디로 |
| D12 | **승급 게이트 = 문법 전용** | 어휘를 승급 계산에서 제외(2026-07-09 사장님 결정): G1=문법 배움 90%, G2=문법 잘씀 55~60%(L1은 청크 기준). 근거: 문법이 이미 승급 페이스의 지배 변수(공부 모드 통화당 신규 문법 1개 = ~30통화)라 어휘 게이트는 실효 없이 복잡도만 추가. 어휘는 교습·체크판 추적·복습·grandfathering 전부 유지, is_core는 "가르치는 순서" 우선순위로만 사용 |
| D13 | **커리큘럼 최종본 = CEFR 12단계** | `level/04.CEFR 12단계 통합/`(문장 10,639행)이 최종 데이터(2026-07-09): 어휘→단계 배분 확정(cefr_v1, 매칭 100%), 어휘별 예문 확보(LLM 예문 생성 취소 — 다국어 뜻 번역만 잔존), 문법 인벤토리 동기화(손상 4건 제거·신규 4건 추가, 총 459 유지). 플로우 무변경 — 데이터 계층만 교체 |
| D14 | **잘씀 = 성공 산출 3회 이상 명시** | 점수식에 더해 (유도+자발) 성공 ≥3회를 명시 조건으로 추가(2026-07-09 사장님 결정). 따라말하기(E1)는 횟수 미산입. 기존 조건(2통화·2일 분산, 자발 1회 포함, 최근 증거≠F) 유지. D12(문법 전용 게이트)로 문법 판정의 비중이 커진 데 대한 엄격화 |
| D16 | **동적 힌트 = 질문별 예시 답변** (P2.5) | 사용자가 막히면 힌트 상자에 비버 질문에 대한 예시 답변 3줄(한국어/로마자/모국어) 표시(2026-07-09 사장님 지시). 생성은 통화 중 서버 사이드카 LLM 1콜(비버에게 시키지 않음 — 소리 내어 말해버림). **오염 방지**: 힌트 열람 시 클라가 기존 통화 WS로 `hint_used` 신호(별도 REST 불요) → 그 턴 증거를 따라말하기(E1) 수준으로 강등 — 힌트 보고 읽은 발화가 "자발"로 안 잡힘. 상세: §7 및 mechanics ⑬ |
| D15 | **체류 게이트(G3) 폐지 + 유효통화 컬럼 제거** | "충실한 통화 N회·N일" 게이트 폐지(2026-07-09 사장님 결정). 근거: G1(배움 90%)·G2(잘씀)를 채우려면 통화 수는 자연 충족 — 이중 규제였다. 전용 컬럼 call.is_valid_call/user_turn_count/user_char_count 도 함께 제거(Rev5), 통화 수 파생값(G4 창·브리지·버벅임·승급 멘트)은 **증거통화**(item_evidence 에 행이 있는 distinct call)로 계산(관통 원칙 2 — 파생). G5(승급 후 잠금)는 연쇄 승급 방지 안전핀으로 **일수 조건만** 유지(통화 수 조건 삭제). level.grammar_count/vocab_count/grammar_scope/vocab_sample 도 런타임 사용처 0(learning_item 이 단일 소스)이라 동반 제거 |

**미결(구현 중 확인)**: 레벨테스트 스킵 허용 여부(디폴트: 허용+홈 배너 회수), 어휘 급내 2분할 정밀 기준(vocab_split_v2), LLM 생성 대상 언어 목록.

---

## 1. 사용자 흐름 (전체)

```
가입 → 첫 전화 → [레벨테스트 통화 5분, 대화로 위장] → 서버가 전사 분석 → korean_level(1~13) 저장
→ 체크판 초기화(grandfathering) → 이후 매 통화: 공부(새 항목) / 대화(배운 것 유도)
→ 통화후 분석이 항목 사용 증거를 체크판에 기록 → 게이트 충족 시 자동 승급(무통지)
→ 승급 후 첫 통화에서 비버가 자연스럽게 난이도 상승 언급
```

사용자에게 보이는 것: 통화·자막·(L1 한정) 학습 카드·복습 문장. 보이지 않는 것: 레벨 숫자·체크판·게이지.

---

## 2. 레벨 체계 (13레벨)

| Lv | 밴드 | 소스 | 게이트 문법 | core 어휘 |
|---|---|---|---|---|
| 1 | 생존 | 신규 청크 46개(초안) | 0 (청크 ~40) | — |
| 2~5 | 초급 | Basic Korean A~D (TOPIK 1·2급) | 32~34 | 100 |
| 6~9 | 중급 | Intermediate Korean A~D (TOPIK 3·4급) | 30~45(상한) | 110 |
| 10~13 | 고급 | Advanced Korean A~D (TOPIK 5·6급) | 28~45(상한) | 120 |

- 문법 분모 상한 45(Int A 69개 등 편차 대응) — 초과분은 '선택 문법'(추적하되 게이트 제외, 대화 재활용 풀 잔류).
- 어휘(교재 구분 없음): TOPIK급 → 교재 2권 지그재그 배분(우선순위 점수 내림차순 홀짝). `assign_rule` 버전으로 재배분 가능.
- core 선정 점수 = 빈도 40%(legacy vocab_12levels.json 순위 조인) + 품사 가중 35%(동사 1.0 최우선 — 동사 시트 신호) + 교재 예문 등장 25%. 품사 쿼터(동사 30/형용사 15/명사 40/부사 10/기타 5%). 접사·단독 의존명사·줄어든꼴은 core 금지.
- non-core 어휘 = "노출 풀": 추적만, 게이트 제외, 대화 모드 통화당 최대 3개 주입.
- 자모(40행): learning_item에 **넣지 않음**. `assets/level/curriculum_v2/jamo.json` 자산 → 추후 앱 화면 모듈.
- 승급 주기 설계치: L1 ~14통화(≈2주, 의도적 단기), L2+ 44~51통화(≈6~7주). 전 커리큘럼 이론 최소 ≈584통화.

---

## 3. 데이터 파이프라인 (P0)

### 3.1 소스·검증 결과
- 마스터: `level/한국어_4단계_통합.xlsx` (문법 459 / 어휘 10,636 / 자모 40). **원본 10개 파일과 전수 대조 완료 — 일치**(유일 차이: '사01/사02' 등급 결측을 1급으로 올바르게 보정).
- 오류 7건 전부 **원본 유래**(AI 정리 훼손 아님) → 오버라이드 교정:
  1) 1급 '명02'(길잡이말 "병에 담다") → **'병02'** (D9 확정)
  2) '대전01 명사' → '대전01' 3) '공 적02' 내부 공백 4) '고소하다 01' 5) '자아실현 00' 6) '윗글0' → '윗글00' 7) 고급 문법 `-(으)ㄹ라치면` 예문 3개 비문 → "낼라치면/시작할라치면/출력할라치면" 교체
- 동사 시트 1,468 = 어휘 완전 부분집합 → 별도 시드 금지, `is_verb_priority` 플래그만.

### 3.2 구조 (xlsx → 정규화 JSON 커밋 → 시드 조인 2단)
```
assets/level/
  source/한국어_4단계_통합.xlsx      # 원본 불변(level/ 에서 이동·커밋)
  curriculum_v2/{grammar,vocab,overrides,jamo}.json   # 정규화 산출물(diff 리뷰 가능)
  curriculum_v2/survival_chunks.json                  # L1 생존 청크 46개(linguist 초안→검수)
  generated/vocab_gen_v1.json        # LLM 배치 생성물(다국어 뜻·예문, source_key 조인)
  legacy/                            # 구 12레벨 자산 이관
scripts/curriculum/{parse_xlsx,assign_levels,generate_vocab_gen}.py
scripts/seed.py                      # seed_levels(13) + seed_learning_items 추가(멱등 upsert)
```
- 표기 파서: 공백 전제거 → 오버라이드 → 구분자 `[/∙·‧・]` 분해 → `surface`(접미 제거) + `homograph_refs` 보존. 동형이의어는 표면형 병합(전사 판정은 표면형이라 sense 분리 불가).
- 품사: 40여 변형 → 정규 11종 화이트리스트(미지 토큰=파싱 에러). `pos_raw` 무손실 보존.
- 어휘 뜻·예문 생성: krdict(한국어기초사전) API 대역어 우선 매칭 → 잔여만 LLM 배치(길잡이말을 sense 힌트로 필수 주입, 레벨 문법 scope 내 예문 제약, `--only-missing` 멱등).

### 3.3 스키마 (신규 모델 + Alembic)
- `learning_item`: 단일 테이블 + kind(grammar/vocab). canonical 좌표(band/topik_grade/textbook_code/seq_no) + 파생 level_no(+assign_rule) 병기 — 배분 규칙 변경 시 UPDATE 재계산만. source_key UNIQUE(`g:{교재코드}:{surface}` / `v:{surface}{접미|00}`). JSONB(examples/meanings/gen_examples). CHECK·부분 인덱스는 db-architect 설계 v1 그대로.
- `member_item_progress`(체크판, 희소 — 행 부재=미학습): status + 카운터 4(repeat/prompted/spontaneous/miss) + score + provenance(observed/placement/**fast_track**) + fast_track_confirmed_at + 시각들 + first/last_call_id. UNIQUE(member_id,item_id).
- `item_evidence`(append-only 감사 로그): grade_raw/grade_final/learner_quote/verified/turn_index/normalized_text_hash. **P0에서도 못 뺌**(튜닝·리플레이 재계산의 원본).
- `member_level_history`: from/to_level, reason(placement/gate_promotion/remeasure_up/down/manual), **trigger_call_id UNIQUE(멱등 키)**, gate_snapshot JSONB. member.level_entered_at 컬럼 금지(최신 행 파생).
- `call` 확장: call_type(normal/level_test), assessed_level, assessment_note. ~~is_valid_call, user_turn_count, user_char_count~~ — D15 로 제거(Rev5), 통화 수 파생값은 증거통화(item_evidence distinct call) 계산.
- `jamo`: 테이블 없음(정적 자산).

### 3.4 마이그레이션 순서 (⚠ 순서 고정)
1. **Rev1 `level_13_shift`** (수동, 파괴적 — 실행 전 prod 백업 + 사장님 확인, R6): level_no +100/−99 2단 shift(1~12→2~13) + member.korean_level +1. downgrade는 RuntimeError 차단. **레벨테스트 기능보다 먼저 배포**(판정이 처음부터 1~13).
2. **Rev2** learning_item 등 신규 테이블(autogenerate → 검토 체크리스트: JSONB·partial index·server_default·CHECK·비PK FK).
3. `seed_levels`(13행 — D15 이후 텍스트 자산만: profile·band·grade·stage_name·textbook) → `seed_learning_items`.
4. 커밋 단위: 모델+Rev1+Rev2+curriculum_v2 JSON+seed 확장 = 같은 커밋(R2). generated/*.json 은 후속 커밋 허용(결손 시 NULL graceful).
- ⚠ `normalcall_service.py:84` 기본 레벨 폴백: shift 후 1=생존. **미설정 회원 기본은 2(Basic A)로 변경**(생존 레벨은 완전 초보 전용 — 레벨테스트가 배정).

---

## 4. 레벨테스트 콜 (P1)

- 진입(D11, **서버 자동**): 클라는 평소처럼 start 전송 → 서버가 `korean_level is null` 이면 레벨테스트 페르소나로 자동 진행(추가 DB 비용 0 — 핸들러가 어차피 member 로드). 안내 인터스티셜 없음 — "시험 아님" 프레이밍은 비버의 선톡이 담당.
- 프로토콜: `ClientStart.call_type: Literal["normal","level_test"] | None = None` 선택 필드(명시 시 우선 — 미래 재측정 통로, 미지정 시 서버 판단). status 폴링 응답에 call_type/assessed_level 추가(신규 엔드포인트 없음) — 클라 결과 화면 분기는 이 값으로(MVP 생략 가능).
- 통화: 기존 run_call 재사용(⛔ 2펌프·10분 백스톱·barge-in off·종료 시드 무변경). 페르소나는 사용자가 고른 캐릭터 유지(voice·말투), "통화 목적" 템플릿만 교체. **전용 캐릭터 행 만들지 않음.**
- 프롬프트(D5): `build_leveltest_instruction` 별도 함수(종료 규약 문단은 `_RULE_CLOSE_PROTOCOL` 상수 공유 — 기존 출력 바이트 동일 스냅샷 테스트). code-switching 역전(안내=모국어/측정 질문=한국어), 교정 금지, 4단계 프로빙 사다리(인사→과거·계획→경험·의견→추상 논증), 레벨·점수 발설 금지. 전용 선톡/종료 시드.
- 판정: 통화중 판정 0(표본 수집만) → 통화후 `generate_structured` 1콜(temperature 0). 스키마 필드 순서 = evidence→reasoning→band→level_in_band→level_no→confidence→sample_quality→summary→feedback(CoT 강제). 밴드(3지선다)→단계(4지선다) 2단 판정 + 앱 측 정합성 클램프. 루브릭은 `{rubric}` 텍스트 슬롯(assets 파일 → 상수 폴백).
- 가드: 발화 ~20자 미만 → 판정 스킵·미저장(다음 통화 자동 재테스트) / 발화 있으나 전부 모국어 → L1 저장 / sparse+low confidence → 1단계 하향 / LLM 실패 → failed·미저장. 애매하면 항상 낮게.
- 저장: member.korean_level + call(assessed_level/note/summary) + status=done **단일 commit** + **grandfathering**: k−2 이하 항목 일괄 MASTERED(placement, score 3.0), k−1 INTRODUCED, k 이상 UNSEEN. placement는 실증거(observed)가 오면 즉시 굴복(F 1건에도 강등).
- 결과 화면(D2): 숫자 발표 없음 — "딱 맞는 난이도를 찾았어요" 톤.

---

## 5. 체크판 · 증거 파이프라인 (P2)

### 5.1 상태기계
`UNSEEN(행 부재) → INTRODUCED(배운적 있음) → PRACTICING → MASTERED(잘 사용함)` + provenance + score(0~6).
- 증거 등급: E0 노출+0.25 / E1 모방+0.5 / E2 유도+1.0 / E3 자발+1.5 / F 오류−1.0.
- MASTERED 정식 경로: score≥3.0 && 성공증거 서로 다른 통화 2회·다른 날 2일 && E2+ 2건 중 E3≥1 && 최근 증거≠F.
- **fast-track(미학습→잘씀 직행, 1통화)**: 같은 통화에서 ①E3 2건 ②서로 다른 문장(해시·Jaccard<0.5) ③사이 USER 턴≥2 ④≥1건이 비버 최초 언급보다 선발화(미주입 항목은 자동 충족) ⑤F 0건 — 전부 충족 시 승격(통화당 ≤2). 다음 대화 통화 우선 재확인: 실패 시 PRACTICING 복귀(유일한 강등 예외), 성공/14일 무F 시 확정. **확정 전엔 게이트 미산입.**
- 선별 제외(이미 아는 것 재교육 방지): MASTERED 공부 큐 제외, 최근 자발 사용 항목은 대화 풀로, 재주입 쿨다운 2통화(F는 즉시 재주입), 선발화 관측 항목 우선순위 최하위.
- 항목 유형별 "잘씀" 조작화(linguist): 청크=상황 적합 산출 1~2회 / 어휘=에코 배제(직전 3턴) 자발 2통화 / 문법=토큰 3+·**결합 어휘 2종+**·2통화 / 문형=슬롯 충전물 2종+.
- L1(생존) 특례: INTRODUCED에 따라말하기 인정, MASTERED=서로 다른 통화 2회 사용(유도 인정, 모방만 배제).

### 5.2 검출(통화후 분석 — 기존 1콜에 병합, 추가 콜 없음)
- 후보 closed-set ≤30(이번 주입 ~12 + practicing 18). 항목당 1건, 성공 최고 등급 우선.
- 스키마: `CallAnalysis` + `detections: list[ItemDetection{item_id, evidence(E1/E2/E3/F), quote, note}]` (기본값 [] — 구응답 graceful).
- **서버 검증 게이트(LLM은 증인, 심판은 코드)**: item_id가 후보 밖 → 폐기 / quote가 실제 USER 전사에 부재 → 폐기 / E3인데 직전 2 비버 턴에 동일 표현 → E1 강등 / 4음절 미만·항목 단독 발화 → E1 강등 / 동일 정규화 인용 중복 → 1건.
- ASR 왜곡 대응: F는 "오류가 전사에 명시적으로 남았을 때만" 보수 판정(지시문 명문화). 정확률보다 자발성·결합 다양성·통화 분산 가중.
- 상한: 통화당 항목당 순증 +2.0, MASTERED 승격 문법 2/어휘 6/청크 4, INTRODUCED 8, fast-track 2.
- 힌트(P2.5): `hint_used` 수신분 call 귀속 저장(판정 반영은 P3 — stage2 후 발화=모방 취급 예정).

### 5.3 레벨업 게이트
- G1 커버리지: 문법+core 어휘 INTRODUCED+ — 초급 90% / 중급 85% / 고급 80%
- G2 숙달: MASTERED(확정분만) — 초급 55% / 중급 50% / 고급 45%
- ~~G3 체류~~ — **D15 로 폐지**(G1/G2 와 이중 규제 — 배움·잘씀을 채우면 통화 수는 자연 충족)
- G4 품질: 최근 5 **증거통화**(item_evidence 에 행이 있는 distinct call, 최신순) F비율 < 30/25/20%(분모<10이면 pass)
- G5 잠금: 승급 후 3~10일(밴드별 — D15 로 **일수만**, 통화 수 조건 삭제)
- 판정: 통화 분석 커밋 직후 `evaluate_level_up`(member FOR UPDATE + trigger_call_id UNIQUE + 항상 +1 = 멱등 3중). 승급 시 korean_level+1 + history 행 + gate_snapshot. **강등 없음** — 승급 후 부진(신규 F≥50% 3연속)이면 콘텐츠만 "이전 레벨 복습 70%" 소프트 브리지.
- 정체 탈출: 30통화/45일 초과 && G1 충족 && G2 미충족 → 15통화마다 G2 −5%p(하한 45/35%) + 승급 프로브 콜(미숙달 하위 10개 강제 편성, 7/10 성공 시 면제 승급).
- 튜닝 불가 항목(게이밍 방어의 심장): k=2·서로 다른 통화 조건. 튜닝 레버 순서: 대화 증거 수율(5→6) → G2 −5%p → core −20.

### 5.4 승급 통지 (D3)
- UI 없음. 승급 후 첫 통화(=history 최신 행 이후 증거통화 0회) 프롬프트에 1줄 주입:
  `[승급 알림] 학습자가 최근 실력이 늘어 오늘부터 조금 더 어려운 내용을 다룬다. 통화 초반에 {locale_label}로 "저번에 정말 잘했으니까 오늘은 조금 어려운 것도 해볼까?"처럼 자연스럽게 한 번만 언급하고, 레벨·점수·단계 같은 단어는 쓰지 마라.`

---

## 6. 통화 프롬프트 (P2 — prompt-persona-engineer 전문 완성본 보유)

- 구성 공식(D4 반영): **본편 5 + 예비 5 = 10 로드.** 본편 = 복습 0~2 + 신규 문법 1(2 금지 — 타임버짓) + 신규 어휘 2~4. 밴드별 차등(L1: 청크 3+어휘 1, 문법 0). 예비는 전원 어휘(절단 위험 최소). 미소화분 다음 통화 이월.
- `build_system_instruction` 시그니처 변경: `history` 제거 → `study_items` / `known_items{grammar≤40, targets 3~5}` / `recent_topics≤5`. 호출부 2곳(call_session.py, main.py 프리뷰) 동시 수정.
- 불변식 규칙 1 교체: "[공부/대화 모드] 블록이 있으면 따르고 없으면 기존 즉석 방식" 포인터 + 블록 헤더 상호배제 이중 선언. 블록 미주입 시 기존 동작(R5 폴백).
- 공부 블록 핵심 규칙: 한 번에 한 항목·항목당 절차(문법: 설명→예문 따라→즉석 예문→응용 질문→교정 / 단어: 뜻→따라→예문 / 청크: 통반복 2회) · "다 못 가르쳐도 괜찮다"(서두름 방지) · 진행률 발설 금지 · "[시스템]" 시드 오면 즉시 종료 규약. L1 변형 블록(문법 용어 금지·분해 설명 금지).
- 대화 블록: 아는 문법 ≤40(` · ` join, soft constraint) + 유도 표현 3~5(freetalking.json 문법↔미션 인덱스를 힌트로 기계 조회 — 필드 철자 `Misson1/Misson2/Mission3` 주의) + 유도 시도 2회 상한·포기 규칙 · 어휘 whitelist 미주입.
- 검출 지시문: E1/E2/E3/F 정의 + 후보 테이블 + quote 원문 인용 강제 + ASR 보수 판정. (전문은 prompt-persona-engineer 산출물 — /build 시 그대로 사용)
- 토큰: system_instruction ~3,900tok(예비 5로 +200), 검출 콜 ~5,000~7,500tok — 무해.

---

## 7. 레벨 1 학습 카드 UI (P2.5 — Flutter)

- **기존 call.dart에 조건부 위젯**(별도 화면 금지 — 종료·복구 로직 이중화 위험). teaching_plan 있으면 카드, 없으면 기존 자막.
- 서버: start 직후 `ServerTeachingPlan{items:[{item_id, ko, romanization, meaning, example…}]}` 1회 push(핫패스 밖) — 프롬프트 주입과 단일 소스. `ClientHintUsed{item_id, stage}` 수신·저장.
- 카드 상태기계(클라 로컬): UPCOMING → TEACH(비버 자막 문자열 매칭, 풀 노출) → RECALL(내 차례: 한국어 `●●●●` 마스킹+뜻만) → SAID ✓(input_transcript 매칭). 힌트 2단계(첫 음절 → 전체, "힌트 봄" 중립 마킹).
- 프롬프트 보강: "가르칠 표현은 표기 그대로 말하라(변형 금지)" — 매칭 신뢰도. L1은 모드 질문 생략(공부 고정).
- 로마자: L1 기본 ON(시각 위계 3순위·회색), L2 기본 OFF, L3+ 토글 제거. RR 표기 서버 데이터 고정.
- 카드 수: L1 본편 4(청크 3+어휘 1) 시작, 실측 튜닝. 적용 범위는 "데이터 조건"(teaching_plan 유무) — 클라는 레벨 모름(D2 정합).

### 동적 힌트 v2 — 질문별 예시 답변 (D16, 2026-07-09 사장님 지시)
- 비버 질문이 끝날 때(turn_end)마다 서버가 **사이드카 LLM 1콜**(flash, 구조화 출력)로 예시 답변 생성
  → WS `hint`{turn_id, korean, roman, native} push → 힌트 상자에 3줄 표시(예: "화장실에 가요 / hwajangsire gayo / I'm going to the bathroom").
- 입력 = 비버의 직전 질문 + 레벨 프로파일(그 사람이 아는 범위의 표현으로) + 모국어. 로마자는 서버 RR 규칙 변환 우선.
- Live 모델에게 시키지 않는 이유: 만든 걸 소리 내어 말해버림(화면 전용 데이터 통로 없음). barge-in off 라 turn_end 후 0.5~1.5s 생성이 사용자 "생각하는 틈"과 정합.
- ⛔ 격리: 2펌프 경로 밖 fire-and-forget 태스크(강참조) — 실패 시 힌트만 미표시(R5), 통화 무영향.
- 체크판 정합: 힌트 열람 후 발화는 자발(E3) 아님 — `hint_used` 신호로 해당 턴 증거를 모방(E1) 수준 강등(정적 카드 힌트와 동일 규칙).
- 비용: 힌트당 ~$0.0005, 통화당 10~15개 ≈ 1센트 미만. 적용 범위: L1·레벨테스트 우선, 전 통화 확장은 플래그로.

---

## 8. 프론트 (P1~P2.5 공통)

- D11 반영: **레벨 캐시·진입 게이트·인터스티셜 전부 제거**(서버 자동 라우팅). 클라는 기존 start 그대로.
- 남는 프론트 작업(P1): 통화 종료 후 status 폴링 응답의 `call_type`으로 결과 화면 분기(레벨테스트면 배운 표현 목록 대신 "딱 맞는 난이도를 찾았어요" 톤 + 평점 화면 생략) — **MVP 생략 가능**(생략 시 기존 결과 화면이 빈 표현 목록으로 표시되는 것 허용).
- D2 반영: 진행률 게이지·체크판 화면·승급 알림·레벨 숫자 표시 전부 **프론트 범위 제외**(백엔드 데이터·진행도 API는 유지 — 추후 노출 결정 대비).

---

## 9. 로드맵 · 작업 분해

### P0. 13레벨 재편 + 데이터 적재
| # | 작업 | 담당 |
|---|---|---|
| P0-1 | xlsx → curriculum_v2 JSON (parse_xlsx.py, 표기 파서, 오버라이드 7건, 품사 정규화) | fastapi-expert(+db-architect 설계) |
| P0-2 | survival_chunks.json 46개 확정 + `-(으)ㄹ라치면` 예문 교정 + core 목록·Int A 선택문법 절단 검수 | korean-linguist |
| P0-3 | Rev1 shift 마이그레이션(⚠ 사장님 확인 후 실행) + Rev2 테이블 + 모델 + registry | db-architect |
| P0-4 | seed_levels(13)/seed_learning_items + assign_levels(textbook_v1/vocab_split_v1) | fastapi-expert |
| P0-5 | generate_vocab_gen.py (krdict 매칭 + LLM 잔여 생성) — 후속 커밋 가능 | api-integration-expert |

### P1. 레벨테스트 콜
protocol(call_type 선택 필드) → persona_prompt(leveltest 함수+스냅샷 테스트) → call_session 배선(**서버 자동 라우팅**: member.korean_level null → 레벨테스트) → analyze_level_test_call(판정+grandfathering 단일 commit) → MemberRead/status 확장 → Flutter(결과 화면 분기 1건 — MVP 생략 가능) → 회귀+신규 테스트

### P2. 체크판 + 레벨업
progress/evidence/history 테이블 → 검출 병합(CallAnalysis+detections+검증 게이트) → 선별 쿼리(공부 10/대화 유도) → build_system_instruction 개편(블록 2종+승급 알림 1줄) → evaluate_level_up → 테스트(골든셋 전사 10~20건 포함)

### P2.6. 결과 페이지 체감 속도 — ✅ 구현 완료 (2026-07-09, 사장님 지시)
통화 결과 화면의 핵심(요약 + 공부한 문장 한국어 재생)이 **바로** 나와야 한다. 분석 파이프라인 재정렬:
1. **전사(텍스트) 선저장** — 통화 종료 시 오디오 MP3 변환·업로드(~9s)와 분리, 전사 행 먼저 커밋 → 분석 즉시 시작(오디오 업로드는 병렬)
2. **요약+표현 저장 즉시 status=done** — 결과 페이지 폴링이 여기서 풀림(현재는 TTS·체크판까지 대기)
3. **문장 TTS는 done 이후 백그라운드** — 재생 시점에 voice_url 없으면 기존 온디맨드 합성(`POST /sentences/{id}/tts`)이 이미 폴백으로 동작
4. **체크판·승급 판정도 done 이후 같은 백그라운드 태스크에서 계속** — 사용자 노출과 무관(D2)하므로 결과 표시를 막지 않게. 단 "증거→상태→승급 단일 commit" 원자성은 유지(done 커밋과 분리 — status 는 이미 done 이므로 부분 실패해도 결과 화면 무손상, 체크판만 재시도 대상)
- 기대 효과: 통화 종료 → 결과 표시까지 현행 ~18s+TTS×N → **LLM 1콜(~7s) 수준**으로 단축
- 구현(2026-07-09): `save_segments(upload_audio=False)` 전사 선커밋 + `upload_segment_audio` 후행 병렬 업로드(call_session finally, 분석 태스크 먼저 생성) / `_save_analysis` 에 status=done 합류(단일 커밋) → TTS 루프 → 체크판(`_apply_call_mastery` 단일 commit 유지) 순 후행, done 이후 실패는 status 무변경. 테스트 140 통과(신규 3: done 선커밋·체크판 실패 격리·세그먼트 분리). mechanics ⑤ 갱신.

### P2.5. L1 카드 UI
teaching_plan/hint_used 프로토콜 → Flutter 카드 패널·힌트 → 저장

### P3. 고도화
감쇠·리텐션 프로브·소프트 브리지 자동화·힌트→판정 반영·fast-track 파라미터 튜닝·진행도 노출 재검토

### 불변 준수 (전 페이즈)
- ⛔ normalcall 2펌프·TaskGroup·10분 백스톱·barge-in off·종료 시드 규약 무변경(분기는 전부 세션 루프 밖). R4: `tests/test_normalcall_ws.py` 회귀 필수.
- R3 명시적 commit(서비스 계층) / R5 graceful(블록 미주입·생성물 결손·krdict 실패 시 폴백) / R2 모델+마이그레이션 동일 커밋.

---

## 10. 리스크

| 리스크 | 대응 |
|---|---|
| ASR이 학습자 오류를 교정해 전사 | F 보수 판정 + 정확률 대신 자발성·결합·분산 가중 |
| LLM 검출 환각 | closed-set + quote 존재 검증 + 상한(코드 게이트) |
| 한 통화 몰아치기/문장 반복 게이밍 | 2통화·2일 분산 + 해시 dedup + 카테고리 승격 상한 |
| 5분 1회 레벨테스트의 정밀도 한계 | 밴드 고신뢰·단계 잠정 + (P3) 이후 통화 보정 루프 |
| Rev1 shift 비가역 | prod 백업 + downgrade 차단 + 사장님 확인 후 실행 |
| 데이터 재수령 | parse_xlsx 재실행 → JSON diff = 변경 명세, source_key 불변으로 멱등 재시드 |
