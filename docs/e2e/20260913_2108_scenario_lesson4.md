# 시나리오 lesson-cycle — 차시 4 (20260913_2108)

| 단계 | 기대 | 실측 | 판정 |
|---|---|---|---|
| reset | 차시 no=4 · drilled 0 · status learning | no=4 · drilled 0 · learning | PASS |
| 표현학습 1통 | 새 18 · 복습 0 · drilled 18/30 | 새 18 · 복습 0 · drilled 6/30 · 하네스 드릴 7 · 판정 ✔ | PASS |
| 표현학습 2통 | 새 12 · 복습 6 · status expression_done | 새 18 · 복습 0 · drilled 10/30 · status learning · 판정 ✖ | FAIL |
| 프리토킹 1통 | 차시 4 열림(잠금 아님) · 정상 종료 | locked=True · 종료 error:COURSE_LOCKED | FAIL |
| 차시 4 상태 | freetalk_done | learning | FAIL |
| /cur/me 다음 차시 | no=5 | no=4 · status learning | FAIL |

통화: 1574 · 1575 · None