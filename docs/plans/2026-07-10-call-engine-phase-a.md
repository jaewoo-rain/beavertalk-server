# 통화 엔진 근본 재설계 — Phase A (즉시·회귀 낮음)

- 작성일: 2026-07-10
- 상태: **완료(구현·시니어 리뷰·테스트 통과)** — 배포 대기(사장님 승인 필요)
- 상위 계획: `docs/20260710_0234_call-engine-redesign-plan.md`
- 근거 리서치: `docs/20260710_0211_realtime-voice-ai-research.md`(18개 검증 출처)
- 관련 파일:
  - `domains/learning/realtime/call_session.py` (무음 워처·GoAway·백스톱·종료 시드)
  - `core/gemini_live.py` (go_away 이벤트·압축 파라미터)
  - `core/persona_prompt.py` (초반 안정화·레벨테스트 에코 강화·종료 시드 낭독 수정)
  - `tests/test_normalcall_ws.py`, `tests/test_persona_prompt.py`

## 목표 & 범위
통화가 **얼지 않고(무음)·역할을 지키며(드리프트 초기락인)·자연스럽게 끝나도록(종료)** 하는
회귀 낮은 즉시 수정 묶음. 실험 불요 항목(무음 데드락·프롬프트 버그·플랫폼 위생)만.
측정 인프라(Phase B)·재접지/다이어트(Phase C)·프론트 신호(Phase D)는 별도.

## 아키텍처 & 데이터 흐름
```
run_call
 └ asyncio.timeout(540s ← 600 하향, 연결 ~10분 선점)
    └ _run_session (events(): +go_away 정규화)
       └ TaskGroup
          ├ _pump_client_to_gemini   +last_user_activity_ts(in_tr 기준)
          ├ _pump_gemini_to_client   +go_away 분기(should_close, idle면 종료시드)
          ├ _watch_call_clock        (5분 종료 시드 — 불변)
          ├ _periodic_flush          (불변)
          └ _watch_idle              ← 신규: 무음 3단(8s→+10s→+12s)
```
- 주입 단일 창구 `send_text_turn`. 우선순위 **종료 > 무음**.
- 종료 시드 단일 소유권: `_inject_close_seed` 가 `close_seed_sent` 를 await-전 선점 → 정확히 1회.
- 넛지는 `close_seed_sent` 를 건드리지 않고 새 턴만 생성. `silence_stage` 는 **실제 주입 성공 시에만** 전진.

## 구현 내역
- **A1 종료 시드 낭독 버그**: `_CLOSE_SEED`(call_session)·`CLOSE_SEED_LEVELTEST`/`seed_leveltest_opening`(persona_prompt)
  앞에 `(이 지시문 자체를 절대 소리 내어 읽거나 언급하지 마라 — 내용만 행동으로 반영하라.)` 삽입.
  실측 V3(call 91·147, 비버가 `[시스템]` 낭독) 대응.
- **A2 무음 3단 넛지**: `_watch_idle` 태스크(`nc-idle`) + `_inject_nudge` 헬퍼. `_CallState` 에
  `last_user_activity_ts`·`silence_stage` 추가. 무음은 오디오 부재가 아니라 **in_tr 부재**로만 감지
  (마이크 상시 스트리밍). 8s→새 화제 / +10s→"거기 있어?" / +12s→종료 경로 합류. in_tr 수신 시 stage 리셋.
- **A3 백스톱 540s + GoAway**: `ABSOLUTE_CALL_TIMEOUT_S 600→540`. `gemini_live.events()` 가
  `go_away` 를 server_content None-가드보다 먼저 검출·정규화(`LiveEvent.time_left`). 펌프가
  `go_away` 수신 시 `should_close`, idle이면 즉시 종료 시드(발화중이면 turn_end 재주입 경로).
- **A4 압축 파라미터 명시**: 블랙박스 기본값 → `trigger_tokens=16000` + `SlidingWindow(target_tokens=12000)`.
  주석·CLAUDE.md 의 잘못된 "~2분" → "오디오 15분/연결 ~10분(S2)" 정정.
- **A5 초반 안정화(락인 대응)**: `seed_leveltest_opening` 에 올바른 리액션 few-shot("미국이요"→
  "美国! Oh nice…" 모국어 리액션·한글 에코 금지) 삽입. 레벨테스트 규칙1 에코금지를 최상위 규칙으로 승격.

## 테스트 결과 (실제)
- 전체: **157 passed** (`conda run -n beavertalk-server pytest tests/ -q`, ≈162s).
- WS 스위트: **21 passed** (무음 3단·리셋·GoAway·넛지 게이팅 하드닝 포함).
- persona_prompt: 50 passed (에코 강화·few-shot·종료시드·일반통화 바이트동일 스냅샷).

## 시니어 리뷰 결과 (python-expert, 동시성)
- Q2~Q6(이중 주입·좀비 태스크·go_away 경로·첫발화전 무음·540 정합성): **전부 문제없음**.
- Q1(무음 넛지 `silence_stage`): 리뷰는 BLOCKER 로 보고. **CEO 판정: 현재 코드에선 발동 불가**
  (outer 가드와 `_inject_nudge` 사이에 await 경계 없음 → guard 가 다른 상태를 볼 수 없음).
  다만 향후 리팩터로 await 가 끼면 실제 버그가 되는 **취약성**이라, 방어적 하드닝 적용:
  `_inject_nudge` 가 실제 주입 성공 여부(bool) 반환 → 호출부가 그 값으로 `silence_stage` 전진 게이팅.
  회귀 잠금 테스트 `test_inject_nudge_gated_when_busy_or_closing` 추가.

## 미해결 / 후속 작업
- **배포**: demo-api(`beavertalk-app-demo-api`) 배포는 **사장님 승인 후**. 실통화로 무음·에코·종료 확인.
- **Phase B(측정 인프라)**: 표본 수집(다사용자·긴통화 40건 층화) 결정이 선결 — `drift_eval.py`·골든셋·replay.
- **Phase C(게이트)**: 프롬프트 다이어트·재접지 구현·모드별 barge-in — 측정 후.
- **Phase D(Flutter)**: hangup 버튼(P1)·mode/idle_hint 배지(P2).
- nit: 첫 turn_start 미도래 시 두 워처가 540s까지 대기(안전하나 회수 느림) — 짧은 조기 데드라인 고려.
- nit: `gemini_live.py` go_away `time_left` SDK 필드명(camelCase 가능) — 종료 로직 무영향, 로그 품질만.

## 리스크 & 결정 사항
- ⛔ R4 불변식 유지 확인: 2펌프·barge-in off·절대 백스톱·종료 규약(서버만 종료 결정) 무변경.
  회귀 테스트 `test_normalcall_ws.py` 통과.
- 540 하향은 정상 5분 통화(≈344s 마무리)에 무영향 — 200s+ 마진. 연결 ~10분(600s) 60s 선점 + GoAway 이중 안전.
- A2/A5 의 효과(무음 UX·초기락인 감소)는 Phase B 측정으로 사후 확증 예정.
