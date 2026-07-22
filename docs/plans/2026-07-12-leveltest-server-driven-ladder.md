# 레벨테스트 = 서버 주도 적응형 사다리 (O/X는 사이드카 판정)

- 작성일: 2026-07-12
- 상태: **구현·테스트 완료 / T0' 실측·배포 미완** (미커밋)
- 브랜치: `feat/leveltest-fast-probe`
- 관련 파일: `domains/learning/leveltest_ladder.py`(신규), `domains/learning/service/normalcall_service.py`(판정기), `core/persona_prompt.py`(프롬프트), `domains/learning/realtime/call_session.py`(엔진), `tests/test_normalcall_ws.py`·`tests/test_persona_prompt.py`·`tests/test_level_test_call.py`

## 목표 & 범위
레벨테스트를 **서버가 문항 사다리를 소유하는 적응형 결정 구조**로 재설계. "다음 질문·언제 끝낼지"를 LLM이 아니라 서버가 결정. 비버는 서버가 준 질문 낭독 + 따뜻한 리액션만. O/X 판정은 **서버 사이드카**(전사 기반 `generate_structured`).
- 배경(실측): LLM이 `leveltest_ceiling_reached` 함수를 과호출(유창한 학습자를 6초에 조기종료 → L8 저평가). 프롬프트로 못 막음 → "종료 결정권"을 LLM에서 제거.
- MVP: 선형 6계단 사다리(+교차확인) + 사이드카 판정 + 문항뱅크 랜덤. 완전 분기트리는 2차.

## 아키텍처 & 데이터 흐름
```
비버 = tool 없음(tools=None). 서버 질문 낭독(모국어) + 답 직후 리액션 한 마디만.
매 노드: ① 서버가 질문 주입(build_leveltest_question_seed → send_text_turn) → 비버 발화
        ② 유저 답(in_tr) → 비버 응답 시작(turn_started)에 판정 사이드카 발사(병렬)
        ③ judge_leveltest_answer(전사, 목표문법) → pass/fail/unclear (인용검증)
        ④ LEVELTEST_LADDER.advance(cursor, verdict) → 다음 노드 질문 주입 or leaf(None)
        ⑤ leaf → should_close → CLOSE_SEED_LEVELTEST → 작별 → _CallFinished
레벨: leaf history → band_and_anchor(밴드+앵커) → 통화후 판정관이 밴드 내 refine
백스톱(R5): _watch_tree(판정 미도달 시 강제전진), 3분캡, 무음 넛지, 절대백스톱
지연: 사이드카(~1s) 도는 동안 비버 자율 리액션이 TRP 은폐
```

## 구현 내역
- **`leveltest_ladder.py`**: `LadderNode`(node_id·stage·target_desc·question_bank 5문항·korean_cue) + `LevelLadder`(root_id/pick_question 랜덤+명사치환/target_desc/advance 그래프/band_and_anchor). 12노드(N0~N5 + N0e~N5e 교차확인). advance 무상태 그래프(pass→상위/fail→교차확인→leaf). 밴드 앵커(survival 1/beginner 3/intermediate 6/advanced 10)는 `_BAND_RANGE`·`_clamp_assessed_level`과 정합.
- **판정기(`normalcall_service.judge_leveltest_answer`)**: `generate_structured`로 `LeveltestVerdict{result, heard_grammar}` 1콜. ★ 인용검증(관통원칙3): pass인데 heard_grammar가 전사에 없으면 unclear 강등. client None/실패/빈입력 → unclear(graceful R5). 순수 인용검증은 LLM 없이 테스트 가능.
- **프롬프트(`persona_prompt.py`)**: `_LEVELTEST_TEMPLATE`에서 `_DEFAULT_PROBE_PLAN`·[천장 신호] 삭제 → [진행 방식](서버가 질문 준다·비버는 낭독+리액션만·질문/종료 스스로 안 함) + [리액션 규칙](물음표·새질문 금지, 정답 여부 누출 금지, 에코 금지). `seed_leveltest_opening(node0_q, cue)`·신규 `build_leveltest_question_seed(q, cue)`. `CLOSE_SEED_LEVELTEST` 시험 냄새 제거("오늘 대화는 여기까지").
- **엔진(`call_session.py`)**: `_CallState` tree 필드 12개. `run_call` 부트스트랩(cursor=root, 오프닝에 root 질문, tools=None). `_maybe_spawn_tree_judge`(turn_started에 판정 발사, cursor==asked 가드), `_tree_judge_sidecar`(판정→전진, 예외흡수), `_resolve_tree_verdict`(단일소유권, advance→주입/종료), `_inject_tree_question`(_inject_close_seed 미러·idle 가드), `_watch_tree`(백스톱). 일반통화 tree=None → 전 경로 무동작.

## 테스트 결과 (실제 실행)
- **전체 스위트: 175 passed, 0 failed** (`pytest tests/ -q`).
- 신규 사다리 흐름 테스트(test_normalcall_ws.py): 부트스트랩 질문·판정→전진→주입·all-pass N0→N5→leaf·all-fail 빠른 leaf·백스톱 강제전진·일반통화 무영향. 구식 ceiling tool 테스트 5개 교체.
- 유닛: 사다리 self-check(그래프 정합·경로·밴드) OK, 판정기 인용검증 8케이스 OK, persona 스냅샷 31 OK.
- **시니어 리뷰(동시성)**: blocker 0. MAJOR 1건(cursor/asked 디싱크 — 판정 지연 갭에 유저 추가발화가 안 물어본 노드로 오판정→계단 밀림) **수정 완료**(asked==cursor 가드). MINOR A(resolve try 밖) 수정. MINOR B/C/D는 감사로그/문서 수준(수용).

## 미해결 / 후속 작업 (TODO)
- **T0' 실측(최우선·미완)**: 사이드카 판정기가 한국어 문법 O/X를 실제로 정확히 판정하는지 + 매 턴 지연이 대화 자연스러움을 해치지 않는지 = **배포+실통화로만 검증**. (기존 native-audio function-call 관문은 사이드카 방식으로 소멸.)
- 커밋(미커밋)·배포(사장님 확인 후).
- 통화후 판정관 bracket 주입(leaf band→scorer)은 현재 로그만 — 필요시 refine 연결.
- 2차: 완전 분기 트리(밴드 내부 자질 분기), locale별(zh/vi) 낭독 검증 AC.
- 시니어 MINOR B(강제전진 노드 history 미기록) 후속 하드닝 여지.

## 리스크 & 결정 사항
- **R-A(신규 관문)**: 사이드카 판정 정확도 미검증 → T0' 실측이 관문. 부정확해도 통화후 판정관+캡이 흡수(레벨은 나옴, R5).
- **R-B**: 매 턴 사이드카 지연 → 자율 리액션 + `_watch_tree` 백스톱으로 관리(실측 필요).
- **결정**: D1 O/X=사이드카(native-audio tool 과호출 회피), D2 MVP=선형 사다리+문항뱅크 랜덤, 질문=모국어 낭독(뱅크는 영어 소스).
- 관통 원칙: 판정은 인용검증 통과해야 데이터, 최종 레벨은 통화후 판정관이 전사로 확정, 일반통화 바이트 동일, R4 불변식 유지.
