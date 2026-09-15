# 표기 내성 리플레이 — ko · 차시 4 A1-T01-1 (20260915_1538)

- 서버 LLM 판정 경로(call_session 가르침·정답 사이드카)에 전사 텍스트 직접 주입 · 모델 `gemini-2.5-flash` · 학습자 모국어 en · 통화 0
- 결과: 24/24 기대 일치 · 폴백/실패 턴 0
- 사례별: 원형 6/6 · roman 6/6 · 공개 뒤 복창 6/6 · 오답(다른 항목) 6/6

| # | 항목 | 사례 | 학습자 전사 | 기대 | 서버 판정 | 일치 | why(판정 로그) |
|---|---|---|---|---|---|---|---|
| 1 | 인사말 | 원형 | 안녕히 계세요. | passed | passed | ✔ | {1: 'U1이 정답을 말함'} |
| 1 | 인사말 | roman | annyeonghi gyeseyo. | passed | passed | ✔ | {1: 'U1이 정답을 말함'} |
| 1 | 인사말 | 공개 뒤 복창 | 안녕히 계세요. | failed | failed | ✔ | {1: '선생님이 정답을 알려준 후 학습자가 따라 말함'} |
| 1 | 인사말 | 오답(다른 항목) | 생일이 언제예요? | failed | failed | ✔ | {1: 'U1이 엉뚱한 답을 말했다.'} |
| 2 | N은/는 N이에요/예요 | 원형 | 생일이 언제예요? | passed | passed | ✔ | {2: 'U1이 정답 예문을 말함'} |
| 2 | N은/는 N이에요/예요 | roman | saengili eonjeyeyo? | passed | passed | ✔ | {2: 'U1이 정답 예문을 말함'} |
| 2 | N은/는 N이에요/예요 | 공개 뒤 복창 | 생일이 언제예요? | failed | failed | ✔ | {2: 'U3이 B2를 따라 말함'} |
| 2 | N은/는 N이에요/예요 | 오답(다른 항목) | 저는 회사원입니다. | failed | failed | ✔ | {2: 'U used formal polite, not informal polit'} |
| 3 | N입니까?, N입니다 | 원형 | 저는 회사원입니다. | passed | passed | ✔ | {3: 'U1이 정답을 말함'} |
| 3 | N입니까?, N입니다 | roman | jeoneun hoesawonipnida. | passed | passed | ✔ | {3: 'U1이 정답을 말함'} |
| 3 | N입니까?, N입니다 | 공개 뒤 복창 | 저는 회사원입니다. | failed | failed | ✔ | {3: '선생님이 정답을 알려준 뒤 학습자가 따라 말함'} |
| 3 | N입니까?, N입니다 | 오답(다른 항목) | 사람요 | failed | failed | ✔ | {3: '학습자가 틀리게 말함'} |
| 4 | 사람 | 원형 | 사람요 | passed | passed | ✔ | {4: 'U1이 정답을 말함'} |
| 4 | 사람 | roman | saramyo | passed | passed | ✔ | {4: 'U1이 정답을 말함'} |
| 4 | 사람 | 공개 뒤 복창 | 사람요 | failed | failed | ✔ | {4: '선생님이 정답을 먼저 알려줌'} |
| 4 | 사람 | 오답(다른 항목) | 나라요 | failed | failed | ✔ | {4: "U1이 '나라'라고 답해 틀림"} |
| 5 | 나라 | 원형 | 나라요 | passed | passed | ✔ | {5: 'U1 answered correctly before B spoke.'} |
| 5 | 나라 | roman | narayo | passed | passed | ✔ | {5: 'U answered correctly before B.'} |
| 5 | 나라 | 공개 뒤 복창 | 나라요 | failed | failed | ✔ | {5: 'U1이 몰라요 했고 B2가 정답을 알려줬다.'} |
| 5 | 나라 | 오답(다른 항목) | 고향요 | failed | failed | ✔ | {5: "U1 said '고향' (hometown) instead of '나라' "} |
| 6 | 고향 | 원형 | 고향요 | passed | passed | ✔ | {6: 'U1이 정답을 말함'} |
| 6 | 고향 | roman | gohyangyo | passed | passed | ✔ | {6: 'U1이 정답을 말함'} |
| 6 | 고향 | 공개 뒤 복창 | 고향요 | failed | failed | ✔ | {6: '선생님이 정답을 먼저 말함'} |
| 6 | 고향 | 오답(다른 항목) | 안녕히 계세요. | failed | failed | ✔ | {6: 'U1 said wrong expression'} |