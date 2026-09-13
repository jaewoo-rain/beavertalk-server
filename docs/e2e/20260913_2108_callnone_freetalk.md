# 프리토킹 E2E — call None (20260913_2108, run 3) · 차시 no=4 A1-T01-1

**결과: ⛔ 잠김 — 프리토킹이 열려 있어야 했다** — ServerError COURSE_LOCKED 로 끊김 · 통화 전 status=learning
- 통화 길이 요청 5분 · 비버 턴 0 · 학습자 턴 0 · 첫 비버 발화 nans · 오류 ['COURSE_LOCKED: 이 차시(A1-T01-1)의 표현학습이 아직 끝나지 않았어요(status=learning).']
- DB call: {}

## ① 비버 턴별 언어 (한글 비율: ko ≥0.7 · native ≤0.3 · mixed)
- 비버 턴 0 = ko 0 · **native 0** · mixed 0
- 

## ② 모국어 턴 **다음** 비버 턴이 한국어인가 (복귀 성공률)
- 모국어 턴 없음(마지막 턴 제외)
- 하네스가 모국어 턴을 유도한 학습자 턴 0: 

## ③ 문형 이름을 비버가 말한 턴 (기대 0)
- 문형 이름 발화 턴 **0** ✔

## ④ /cur/me 전이 — status · next_course · 다음 차시
- 전: status=learning · next_course=expression · 차시 no=4
- 후: status=learning · next_course=expression · 차시 no=4
- /cur/lessons 차시 4 status=learning 

## ⑤ 비버가 자기 이름 아닌 인물로 답한 턴 (연기 · 기대 0)
- 캐릭터 이름 «?» · 다른 이름으로 자기소개한 턴 **0**

## ⑥ 턴당 문장 수 (max_sentences=2)
- 비버 턴 없음

## ⑦ 과제 수 — 질문/요청으로 끝난 비버 턴
- 과제 턴 0/0 · 과제 없는 턴: 없음

## ⑧ 무음 넛지 — 의도적 침묵 프로브 · 서버 로그
- 침묵 프로브 미실행(학습자 턴이 7개에 못 미침)
- 서버 로그 넛지 0줄 — 발동 0
- ⚠ 1단/2단 시드 **문구**는 서버가 로그에 남기지 않는다 — 넛지 뒤 비버 턴(위 문구)으로 내용을 본다

## ⑨ 차시 소재 등장 (항목 수 · 변형=라벨 조각·예문 포함)
- 비버 0/30: 
- 학습자(하네스 대본) 0/30: 

## 7. 서버 로그 (gcloud)
```
2026-09-13T12:08:17.090140Z	INFO:domains.learning.realtime.call_session:normalcall cur 표현학습 저장: {'lesson_completed': False, 'status': 'learning', 'drilled': 4, 'passed': 0, 'failed': 2, 'fragment': 1}
```

## 전사
```
```