# docs 안내 (Claude·개발자용 인덱스)

`docs/` 는 R1(플랜 우선 + 기록)에 따라 쌓인 설계·플랜 문서다. 파일명 = `YYYYMMDD_HHmm_주제.md`(작업일시 접두).
아래는 **현재 유효한 정본(canonical)** 포인터. 나머지 날짜 파일은 그 시점의 플랜 기록(이력).

## 레벨 시스템 (2026-07 — 최신 대작업)
| 문서 | 용도 |
|---|---|
| `20260709_1231_level-system-master-plan.md` | **결정(D1~D16)·플로우·범위**. 무엇을/왜. 단일 기준 |
| `20260709_1346_level-system-detailed-mechanics.md` | **동작 명세 ①~⑬**(선별 쿼리·검증 게이트·상태 전이·게이트 의사코드·힌트). 구현 직전 수준 |
| `20260709_1621_level-system-overview-for-stakeholders.md` | 처음 보는 사람(대표)용 소개 — 코드 용어 없이 |
| `plans/2026-07-09-level-system-build.md` | 구현 내역·테스트 결과·TODO·**배포 트러블슈팅 노트** |

## 상시 참조
| 문서 | 용도 |
|---|---|
| `ERD.md` / `architecture.html` | 데이터 모델·아키텍처 다이어그램 (⚠ 체크판 신규 테이블 4종 미반영 — 갱신 필요) |
| `DEPLOY_CLOUD_RUN.md` | Cloud Run 배포 절차(원본). 실전 헬퍼는 `scripts/deploy_demo.sh` |
| `20260625_korean-level-12-curriculum.md` | 구 12단계 커리큘럼 근거(13레벨 재편 전 — 이력) |

## 관례
- 새 대작업은 여기 계획을 먼저 남기고 착수(R1). 완료 후 `plans/` 에 구현 요약.
- 이 인덱스는 정본이 바뀌면 갱신한다.
