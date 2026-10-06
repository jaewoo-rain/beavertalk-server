# 통화 분석 「새로 배운 표현」 완성 문장 보장 (A6 · PM-DEC-398)

- 작성: 2026-10-06 · 브랜치 `fix/analysis-complete-sentence`(origin/dev c1bbb21 기준)
- 사용자 원문(10-06): 「지금 통화 분석 화면에서, 'V-(으)면서' 라는 문장이 나왔는데, 이렇게 나오면 발음 평가가 불가능함. 이에 따라 완성된 문장(커피 마시면서 얘기해요)형식으로 출력할 수 있도록 수정해야함.」
- 사용자 결정: 지시문 + 저장 안전장치 둘 다 · 기존 행 정리 안 함(새 통화부터)
- 조사: `C:\Users\hase0\claude_code\45_비버톡PM_하네스\_output\2026-10-03_계획_앱5건\A6_문법패턴\01_조사.md`

## 원인 경로

1. `call_session._trigger_analysis` — chat 통화만 검출 후보를 분석에 넘긴다
2. `normalcall_service._candidate_table` — 프롬프트 끝에 `ID|종류|항목|예문` 표. 문법 행의 「항목」 = 문형 표기
3. 분석 LLM 이 그 값을 `expressions[].korean` 에 옮긴다
4. `_save_analysis` 가 거르지 않고 `Sentence.korean_sentence` 로 저장
5. `review_service.py:49` — 발음 평가 기준 텍스트 = `korean_sentence` → 평가 불가

## 변경

| # | 위치 | 변경 |
|---|---|---|
| 1 | `domains/learning/service/normalcall_service.py` `_analysis_instruction` [규칙] | 1줄 추가 — 완성 문장만 · 문형 표기·후보 표 항목 값 금지 · 문법이면 예문 열 문장 |
| 2 | 같은 파일 `_PATTERN_NOTATION_RE` · `_is_pattern_notation` · `_complete_sentence_expressions` · `_lookup_grammar_examples` | 신설 — 표기 판정 · 예문 치환/제외 · 예문 사전(후보 → learning_item 문법 행) |
| 3 | 같은 파일 `analyze_call` | `_normalize_native_pair` 루프 뒤, `_save_analysis` 전에 안전장치 호출. 표기·문법 surface 일치가 없으면 DB 조회 없음 |
| 4 | `tests/test_analysis_complete_sentence.py` | 신규 22건 |
| 5 | `docs/prompts/README.md` §8 | 결정 로그 |

## 경계

- 이미 저장된 표기 행은 고치지 않는다
- 앱 변경 없음(빌드 48 그대로)
- translation 재번역 없음 · 치환 표현의 현지인 짝은 비움
- 어휘 surface 일치는 대상이 아니다
