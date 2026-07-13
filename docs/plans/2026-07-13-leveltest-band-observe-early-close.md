# 레벨테스트 Phase 2 = 조용한 밴드 관측 + 서버 조기종료

- 작성일: 2026-07-13
- 상태: **구현·테스트 완료 / 실통화 미검증** (Phase 1 위에 얹음, 미배포)
- 브랜치: `feat/leveltest-fast-probe` (Phase 1 = 커밋 c90cbb0, Phase 2 미커밋)
- 관련 파일: `domains/learning/service/normalcall_service.py`, `domains/learning/realtime/call_session.py`, `tests/test_normalcall_ws.py`

## 목표 & 범위
Phase 1(비버 자율 진행·무주입) 위에, 서버가 매 답변을 **조용히 밴드 관측**해 천장에 닿으면 조기종료 → **못하는 학습자 ~1분에 종료**(현재는 3분캡까지 감). ★ 질문 주입은 여전히 없음(관측은 should_close만) → 이중발화 재발 없음.
- 밴드 판정은 "노드 매칭"이 아니라 **절대 밴드 분류**(call 246 강등 오판 방지 — 낮은 답변이 천장을 못 내림).
- 최종 레벨은 통화후 판정관이 확정(라이브 밴드 = 조기종료 타이밍 자문).

## 아키텍처 & 데이터 흐름
```
비버 turn_end → _flush_beaver_segment: last_beaver_question 스냅샷
유저 답변 → 비버 응답 시작(turn_started) → _spawn_band_observe(답변, 직전질문) [논블로킹]
사이드카: classify_leveltest_band(answer, prior_question) → band(0~3)|None
  band None → 무변경 / 값 → obs_count++, obs_max 갱신 시 plateau=0 else plateau++
  천장(_band_ceiling_reached): 45s 플로어 & obs_count>=4 후 (obs_max==3 or plateau>=3 or obs_count>=10)
  → should_close → 기존 종료 파이프(_inject_close_seed=CLOSE_SEED_LEVELTEST). ★ 질문 주입 없음
백스톱(R5): 사이드카 실패/hang → 3분캡·무음3단이 종료
```

## 구현 내역
- **normalcall_service.py**: `classify_leveltest_band(client, *, answer_text, prior_question=None) -> int|None`(0 survival/1 beginner/2 intermediate/3 advanced, None=무응답/판정불가). `LeveltestBandRead` 스키마(인용→결정자질→밴드→자발성 CoT) + `_BAND_CLASSIFY_INSTRUCTION`(밴드별 결정자질: 간접화법→≥intermediate, 문어논증→≥advanced). ★ 인용검증(관통원칙3): ≥intermediate인데 heard_grammar 전사 부재면 한 밴드 강등. graceful.
- **call_session.py**: `_CallState` band_* 필드 8개, 상수(FLOOR 45s·MIN 4·PLATEAU 3·MAX 10), `_spawn_band_observe`(turn_started에 발사, band_awaiting 1회 가드, should_close면 스킵), `_band_observe_sidecar`(관측→추적→천장 종료, M1 종료시드 예외 흡수), `_band_ceiling_reached`(순수 함수), `_flush_beaver_segment` last_beaver_question 캡처. **질문 주입 없음** — 통화 중 send_text_turn = 선톡+무음+종료 시드만 유지.

## 테스트 결과 (실제 실행)
- **전체 179 passed, 0 failed** (M1/m4 전). M1/m4 후 test_normalcall_ws.py 39 passed.
- 신규 밴드추적 7건: advanced 즉시 천장, plateau 종료, band None 무천장, 무주입 유지("[다음]" 0건), 시간플로어 차단, 사이드카 실패→캡 백스톱, 일반통화 미관측.
- **시니어 리뷰(동시성)**: blocker 0. MAJOR 1(M1: 사이드카 종료시드 continuation try 밖 → 세션종료 레이스 미처리 예외) **수정**. minor(m4 종료후 관측 스킵 수정, m1 in-flight 답변 누락·m2 소표본 조기종료·m3 stale prior_question은 수용/통화후 흡수).

## 미해결 / 후속 작업 (TODO)
- **실통화 검증**: 못하는 학습자가 실제로 ~1분에 조기종료되는지, 잘하는 학습자는 계속 가는지 = 배포+실통화.
- 커밋(Phase 2, 미커밋)·배포.
- **다음(사장님 "1")**: 통화후 판정관 저평가 수정 — call 250에서 C급 학습자가 L5로 저평가됨. `analyze_level_test_call`/`_leveltest_instruction` 루브릭이 고급 한국어를 제대로 평가하게. (+ 라이브 obs_max를 bracket 힌트로 넘기는 정합 — 현재 로그만 TODO)
- minor: in-flight 중 답변 누락(직렬 관측), 소표본(4) 조기종료 공격성 튜닝 여지.

## 리스크 & 결정 사항
- **R-A**: 밴드 분류 정확도·조기종료 타이밍 미검증 → 실통화. 부정확해도 통화후 판정관+자동레벨업 자가치유(라이브는 자문).
- **결정**: Phase 2 조기종료는 obs_max 단조증가+plateau(call 246 강등 방지). 최종 레벨은 통화후 판정관 유지. 질문 주입 0 불변(이중발화 방지).
