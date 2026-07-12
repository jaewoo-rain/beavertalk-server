# 레벨테스트 = 비버 자율 진행(OPI) + 서버 무주입 (Phase 1)

- 작성일: 2026-07-13
- 상태: **구현·테스트 완료 / T0'(실통화) 미검증, 미커밋·미배포**
- 브랜치: `feat/leveltest-fast-probe`
- 관련 파일: `domains/learning/realtime/call_session.py`, `core/persona_prompt.py`, `tests/test_normalcall_ws.py`, `tests/test_persona_prompt.py`, `tests/test_level_test_call.py`

## 목표 & 범위
레벨테스트에서 **통화 중 서버 질문 주입을 코드에서 제거** → 비버가 OPI(구술능력인터뷰)식으로 자유 진행(질문·반응을 자기 턴 하나), 서버는 뒤에서 종료만 결정. **이중발화·마커낭독 원인 소멸.**
- 배경(실측 call 248): 서버가 판정 후 다음 질문을 별도 턴(`send_text_turn`, turn_complete=True)으로 주입 → 비버가 (a)자율 응답+(b)주입 응답 2턴 + "[다음]" 마커 낭독. **폐기했던 legacy_idle 재접지 버그가 사다리에서 재발.**
- 진단(gemini-live): 주입을 없애면 VAD가 유저 턴당 1생성만 하므로 이중발화·마커낭독 **구조적 소멸**. product: 인-콜 사다리의 band/anchor는 통화후로 안 넘어가 로그로만 버려짐 → 주입 기계 전체가 가치 0 → 제거는 **과설계가 아니라 정리**.
- **Phase 1(이번)**: 주입 기계 삭제 + 비버 OPI 프롬프트. 종료=3분캡/무음(기존). 인-콜 판정 없음.
- **Phase 2(fast-follow)**: 밴드 분류 사이드카(조용한 관측) + 서버 천장 검출 → 못하는 학습자 조기종료 + bracket 힌트.

## 아키텍처 & 데이터 흐름 (비버=인터뷰어, 서버=심판)
```
비버(자유): 인사+쉬운 질문 시작 → 답할 때마다 [짧은 반응+다음 질문] 한 턴 →
           잘하면 난도↑(현재→과거→계획+이유→간접화법→비교/가정→의견논증), 못하면 쉽게 낮춤, 절대 스스로 안 끝냄
서버: 통화 중 send_text_turn = 선톡 시드 1 + (무음 넛지) + 종료 시드 1 뿐 (질문 주입 0)
종료: 3분캡(LEVELTEST_MAX_S) / 무음 3단(25/8/10) → should_close → 기존 종료 파이프(CLOSE_SEED_LEVELTEST)
레벨: 통화후 판정관이 전사로 최종 확정(단일 권한)
```

## 구현 내역
### domains/learning/realtime/call_session.py (1527→1237줄, 290줄 삭제)
- **삭제**: `_inject_tree_question`·`_resolve_tree_verdict`·`_watch_tree`·`_maybe_spawn_tree_judge`·`_tree_judge_sidecar` 함수, `_CallState` tree 필드 13개, 상수 `TREE_SIGNAL_TIMEOUT_S`·`TREE_MAX_FORCED_ADVANCES`·`NOATTEMPT_MAX`·`LEVELTEST_MIN_S`, 펌프 turn_started/turn_end 훅, ceiling tool_call 블록, `_watch_tree` TaskGroup, finally의 tree_judge_tasks, import(LEVELTEST_LADDER·build_leveltest_question_seed).
- **변경**: run_call 레벨테스트 분기 = `seed_leveltest_opening(target)` 무인자(비버 자유 시작), tools=None, close_seed=CLOSE_SEED_LEVELTEST, 무음 25/8/10 유지. 무음 1단 시드 순화("방금 한 질문을 더 쉽게/선택지로 다시").
- **유지**: 종료 파이프·3분캡·무음3단·절대백스톱·종료 레이스 가드·2펌프·barge-in off·재접지·일반 통화 무변경. 통화 중 send_text_turn = 선톡+무음+종료 시드 3곳만.

### core/persona_prompt.py (레벨테스트 슬롯만, 일반 통화 바이트 동일)
- `build_leveltest_question_seed` 삭제. `seed_leveltest_opening(target)` 무인자 복원.
- `_LEVELTEST_TEMPLATE` OPI 개정: [진행 방식—네가 이끈다](기본 상승·제자리걸음/건너뛰기 금지·충분판단 금지·절대 자기종료 금지) + [난이도 사다리 6단] + [막히면—인내심](발판 2회·되묻기≠실패) + [답 직후—한 턴에 반응+질문].
- `CLOSE_SEED_LEVELTEST` 보강("어려운 질문 중이었어도 자연스럽게 마무리").

## 테스트 결과 (실제 실행)
- **전체 172 passed, 0 failed** (`pytest tests/ -q`).
- 신규/교체(test_normalcall_ws.py): 무주입(여러 답변에도 "[다음]" 0건)·오프닝 무주입 부트스트랩·3분캡 종료·무음 캐던스 신 시드·일반통화 무영향. 구식 사다리 테스트 4 삭제+1교체+4갱신.
- persona 스냅샷 31 OK(일반 통화 무변).
- **시니어 리뷰(동시성)**: blocker 0, major 0. 삭제가 dangling 참조·slots/init 불일치 없이 깨끗, 종료 파이프 온전. minor 3(주석 잔재·gemini_live 고아 심볼 LEVELTEST_DONE_TOOL·기존 send_reground 타이핑) — 동작 무영향.

## 미해결 / 후속 작업 (TODO)
- **T0' 실측(최우선)**: 비버가 자유 진행 시 실제로 (a)답변당 1턴만 (b)난도를 올려가며 (c)스스로 안 끝내는지 = 배포+실통화. 오프닝/종료 시드 마커(1회씩)가 낭독 안 되는지도.
- 커밋(미커밋)·배포(사장님 확인 후).
- **Phase 2**: 밴드 분류 사이드카(`classify_leveltest_band` 절대밴드+인용검증) + 서버 천장검출(obs_max 단조증가·plateau·reach-fail, call 246 강등 방지) + 통화후 bracket 힌트 → 못하는 학습자 ~60~90초 종료.
- 정리: `leveltest_ladder.py`·`judge_leveltest_answer`(Phase 1 미사용, Phase 2용) 존치. gemini_live의 LEVELTEST_DONE_TOOL 등 고아 심볼(Phase 2 tool 재사용 여부에 따라).

## 리스크 & 결정 사항
- **R-A(관문)**: 비버 자유 진행 시 난도 상승·자기종료 안 함이 native-audio에서 안정적인지 미검증 → T0'. 프롬프트로 안 되면 escalation 넛지(on_user_turn 병합, 이중발화 없는 검증된 방식)로 보강.
- **R-B**: 오프닝/종료 시드 마커는 여전히 1회씩 주입 — 낭독 최소화됐으나 실측.
- **결정**: D1 Phase 1(주입 제거)부터, 조기종료는 Phase 2 fast-follow. D2 최종 레벨은 통화후 판정관 유지.
- native-audio: 주입 없으면 VAD 유저턴당 1생성 → 이중발화 구조적 부재(gemini-live 판정). turn_complete=True 주입 = 별도 생성 필연(재접지 legacy_idle 교훈 동일).
