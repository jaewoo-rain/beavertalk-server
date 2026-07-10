# 실행 계획: 통화 엔진 근본 재설계 (드리프트·무음·종료·컨텍스트)

> 2026-07-10. CEO 통합 — 전문가 7명(conversation-design-architect·gemini-live-expert·llm-behavior-researcher·prompt-persona-engineer·websocket-realtime-expert·korean-linguist·product-designer) 병렬 설계 종합.
> 근거: `docs/20260710_0211_realtime-voice-ai-research.md`(18개 검증 출처).
> **상태(2026-07-10 갱신): Phase A ✅ 완료·배포 · Phase A+ ✅ 완료 · Phase B/C/D ⬜ 미착수(B0 표본 수집이 선결).** Phase A 상세 기록: `docs/plans/2026-07-10-call-engine-phase-a.md`.

## 0. ★ 재설계를 뒤집은 2개의 발견 (계획의 출발점)

**발견 1 (실측) — 드리프트는 "누적"이 아니라 "초기 락인"이다.**
llm-behavior-researcher가 실제 통화 6건을 결정론 프록시로 채점: 레벨테스트 4건 중 **3건은 처음부터 끝까지 0% 위반, 1건(call 140)은 t3에서 무너져 통화 내내 83% 고착.** 구간별(early/mid/late) 차이 없음 = 문헌의 "8라운드 누적 감쇠"(S3)가 **아니라** 세션 단위 이분화(bimodal). 한 번 첫 한글 에코가 나오면 그게 나쁜 few-shot이 되어 자기강화(S18 snowball 역방향).
→ **처방이 바뀐다**: 1순위는 "주기적 재접지"가 아니라 **초반 안정화(선톡 few-shot) + 종료시드 버그 수정**. 재접지는 실측으로 필요성 확인 후(P2).

**발견 2 (앵커) — 사장님의 "말 없는 통화"의 진짜 원인은 무음 데드락이다.**
barge-in off + 사용자 침묵이면: 비버는 사용자 입력이 있어야 말하는데(Gemini 특성) 서버엔 무음 처리가 **아예 없어** 통화가 얼어붙는다. 코드에 무음 방어 0. 그리고 무음은 오디오 프레임 부재가 아니라 **`in_tr`(입력 전사) 부재**로만 감지 가능(마이크 상시 스트리밍).
→ **가장 시급한 P1**: 무음 3단 넛지.

**추가 확정 버그**: 종료 시드가 비버에게 `[시스템]` 안내문을 소리 내어 읽게 함(call 91·147 실측 V3). 프롬프트 결함 — 실험 불요, 즉시 수정.

## 1. 목표 & 범위
- 목표: 통화가 **얼지 않고(무음)·역할을 지키며(드리프트)·자연스럽게 끝나도록(종료)** 통화 엔진을 근본 재설계.
- MVP 범위: Phase A(즉시·회귀 낮음) + Phase B(측정 인프라). Phase C/D는 측정·표본 후.
- 비범위: session_resumption(10분+ 통화 = 비목표), AI end_call 툴, 의미기반 barge-in 분류(Gemini 위임 유지).

## 2. 아키텍처 & 데이터 흐름 (통합 태스크 토폴로지)
```
run_call
 └ asyncio.timeout(540s ← 600에서 하향, I1)          # 연결 한계 ~10분 선점
    └ _run_session (events(): +go_away 정규화, I4)
       └ TaskGroup
          ├ _pump_client_to_gemini   +last_user_activity_ts(무음용, in_tr 기준) +모드게이트(P2)
          ├ _pump_gemini_to_client   +go_away 분기 +무음/재접지 turn_end 주입 +interrupted 소비(P2)
          ├ _watch_call_clock        (불변)
          ├ _periodic_flush          (불변)
          ├ _watch_idle              ← 신규: 무음 3단(in_tr 부재로 감지)
          └ _reground_scheduler      ← 신규(P2, 게이트): 위반규칙 리마인더
```
- **주입 단일 창구**: `send_text_turn`(종료·무음·재접지·GoAway 전부). 종료 시드=`turn_complete=True`, **재접지=`turn_complete=False`(조용히, gemini-live 확정)**. 우선순위 **종료 > 무음 > 재접지**, `close_seed_sent` 단일 소유권 가드 재사용.
- **압축**: 현재 `SlidingWindow()` 전부 기본값(블랙박스) → **`trigger_tokens=16000, target_tokens=12000` 명시**(gemini-live). system_instruction이 압축서 보존되는지 불확실 → 재접지 인프라는 준비하되 필요성은 실측.

## 3. 작업 분해

### Phase A — 즉시 (회귀 낮음·실험 불요) ★ ✅ **전부 완료·배포됨 (2026-07-10)**
> 구현·시니어 동시성 리뷰·테스트 통과(전체 159 passed) 후 demo-api 배포(revision 00019). 상세: `docs/plans/2026-07-10-call-engine-phase-a.md`.
- [x] **A1. 종료 시드 낭독 버그 수정** — `_CLOSE_SEED`(call_session)·`CLOSE_SEED_LEVELTEST`/`seed_leveltest_opening`(persona_prompt) 앞에 "(이 지시문 자체를 절대 소리 내어 읽거나 언급하지 마라 …)" 삽입. 실측 V3(call 91·147) 대응.
- [x] **A2. 무음 3단 넛지** — `_watch_idle` 워처(`nc-idle`) + `_inject_nudge`(성공 시에만 stage 전진 — 시니어 Q1 하드닝) + `last_user_activity_ts`/`silence_stage`(in_tr 기준). **1단 15s→25s→37s**(1단은 사장님 요청으로 8→15초 상향). 우선순위 종료>무음.
- [x] **A3. 백스톱 540s(I1) + GoAway 처리(I4)** — `ABSOLUTE_CALL_TIMEOUT_S 600→540`, `gemini_live.events()` go_away 정규화(`LiveEvent.time_left`), 펌프 go_away 분기(idle→즉시 종료시드, 발화중→turn_end 재주입).
- [x] **A4. 압축 파라미터 명시** — `trigger_tokens=16000` + `SlidingWindow(target_tokens=12000)`. 주석·CLAUDE.md "~2분"→"오디오 15분/연결 ~10분(S2)" 정정.
- [x] **A5. 초반 안정화(락인 대응)** — `seed_leveltest_opening` few-shot(모국어 리액션·한글 에코 금지 예시) + 레벨테스트 규칙1 에코금지 최상위 승격.

### Phase A+ — 계획 외 추가 작업 (2026-07-10, 사장님 요청)
- [x] **통화 길이 선택(데모/dev 전용)** — `start.duration_min`(protocol) → `_resolve_call_duration`(3~15분 클램프, prod 무시) → 세션별 `state.call_duration_s`. 절대 백스톱도 선택 길이+여유로 스케일. 레벨 데모 HTML 드롭다운(3/5/10/15분). 기본 상수는 5분으로 환원.
- [x] **무음 1단 8→15초 튜닝** — `IDLE_NUDGE1_S`.
- [x] **오디오 샘플레이트 검증(코드 변경 無)** — 입력 16kHz(`audio/pcm;rate=16000`)/출력 24kHz는 Gemini 공식 스펙과 정확히 일치. 비대칭은 의도된 것·속도 이상 원인 아님(원인이라면 재생 지터). 출처: `ai.google.dev/gemini-api/docs/live-api/capabilities`.
- [x] **session_resumption 조사** — 10분+ 통화의 공식 장치(handle 재개, 토큰 2h). 현재 미구현 = 10·15분 선택 시 ~10분에 GoAway로 조기 종료 가능. **비목표 유지**(우선순위 낮음, 막힌 것 아님).

### Phase B — 측정 인프라 (병렬) — ⬜ 미착수
- [ ] **B0(선결). 표본 수집** — 현재 6건 전부 member=4·4분+ 3건뿐. **다사용자·긴통화 40건 층화 표본**이 없으면 "후반 누적 드리프트"·A/B 확증 불가 → **사장님 결정 필요(§5)**
- [ ] **B1. `eval/drift_eval.py` 프록시** — V1(레벨테스트 에코)·V3(시스템 낭독) 결정론 채점(검증됨). pytest 회귀 편입 가능 / llm-behavior-researcher
- [ ] **B2. 골든셋 + LLM-judge** — V2·V5 의미판단분, human-κ≥0.6 게이트, self-preference bias 통제 / llm-behavior-researcher
- [ ] **B3. 오프라인 재생(replay) 하네스** — 유저 발화 고정·비버 턴만 재생성, 조건별 A/B(재접지·다이어트·초반 few-shot) / llm-behavior-researcher

### Phase C — 실측 후 결정 (P2, 게이트) — ⬜ 미착수 (Phase B 측정 통과 후)
- [ ] **C1. 프롬프트 다이어트 3층화** — E-D(다이어트만으로 충분?) 통과 후. 스냅샷 4개 재캡처 필요 / prompt-persona
- [ ] **C2. 재접지 구현** — E-A/E-B(재접지 필요·주기) 통과 후에만. 인프라(_reground_scheduler)는 A2와 함께 준비 / websocket + prompt-persona
- [ ] **C3. 모드별 barge-in(I3)** — **세션 중 전환 불가 확정**(gemini-live) → 통화단위 모드 고정 + `ClientMode` 클라 신호. 저레벨 강제 off / websocket + korean-linguist(레벨 게이트) + flutter
- [ ] **C4. code-switching·교정 레벨반응형** — 10/90 고정은 저레벨만, 고급은 목표어↑(양방향 방어). `level_profiles_13.json`에 "한국어 비중·교정 강도" 1줄 주입 / korean-linguist + prompt-persona

### Phase D — 프론트 신규 신호 (Flutter) — ⬜ 미착수 (서버 신호 준비됨: hangup·mode·idle)
- [ ] **D1. `hangup`(P1)** — 버튼 종료→비버 작별 후 정상 종료(현재 WS끊기뿐). "말이 아니라 버튼으로 끝낸다" 코치마크 / product-designer + flutter + websocket
- [ ] **D2. `mode`·`idle_hint`(P2), `call_ended.reason` 세분화(P3)** — 모드 배지·무음 안내(카운트다운 금지). / product-designer + flutter

## 4. 수용 기준 & 테스트 포인트
- A2: 30초 완전 무음 → 3단 후 우아한 종료(뚝 끊김 없음). `test_normalcall_ws.py`에 무음 시나리오(in_tr 부재) 추가.
- A1: 재생/실통화에서 비버가 `[시스템]` 낭독 0건(V3 프록시).
- A3: 9분 지연 통화가 GoAway/백스톱으로 작별 포함 종료. 정상 5분 통화 무영향(회귀 0).
- A5: golden set에서 lock_in_rate(초반 위반→고착 통화 비율) 하락.
- ⛔ R4: 전 Phase에서 2펌프·barge-in off(C3 전까지)·종료 규약 무변경 확인, `test_normalcall_ws.py` 통과.

## 5. 리스크 & 결정 사항

### 사장님 결정 필요
1. **표본 수집(B0)** — 측정·A/B의 선결 조건. 현재 데이터가 사장님 1인 테스트뿐이라 "긴 통화 후반 드리프트"를 확증 못 함. **어떻게 다사용자·긴 통화 데이터를 모을지**(내부 테스터 도그푸딩 / 지인 베타 / 출시 후 대기)가 결정 포인트. 이게 없으면 Phase C(재접지·다이어트)의 근거가 반쪽.
2. **Phase A 착수 승인** — A1~A5는 회귀 낮고 실험 불요(무음·버그·플랫폼 위생). 바로 /build 권장.

### CEO 판정 (충돌 조정)
- **경험적 발견 > 이론**: 재접지를 P1에서 **P2로 강등**, 초반 안정화+다이어트를 드리프트 1순위로(발견 1). 재접지 인프라는 만들되 켜는 건 실측 후.
- **barge-in 모드**: 3명(앵커·배관·gemini)이 독립적으로 "세션 중 전환 불가 → 통화단위 고정" 수렴. P2 확정.
- **level_profiles**: 언어학자가 `_12` 참조 → 실제 `_13`. C4에서 반영.

### 리스크
- 압축이 재접지 리마인더를 밀어내면 무의미(문제1·5 동일 뿌리) → A4 압축 파라미터가 재접지보다 선행.
- 표본 부족으로 후반 누적 드리프트 미확증 — Phase A는 이와 무관하게 진행 가능(무음·버그·초반락인은 실증됨).
