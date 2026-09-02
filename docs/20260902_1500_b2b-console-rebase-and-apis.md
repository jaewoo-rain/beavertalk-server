# B2B 교사 콘솔 — 현행 main 위로 이식 + 집계·알림 API

작성 2026-09-02 · 브랜치 `feat/b2b-console` · 워크트리 `repos/beavertalk-server-b2b`

---

## 0. 착수 전에 알아야 할 것 — 실측

| 확인 대상 | 결과 |
|---|---|
| Cloud Run 4종의 `openapi.json` | `/console/*` 라우트 **0개** (app-api 41 · demo-api 41 · test-api 40) |
| 프로덕션 DB 테이블 | `classroom` · `classroom_member` · `assignment` · `submission` **전부 없음** |
| 저장소 `beavertalk-server` | `domains/classroom/` 전체가 **미추적**. HEAD 는 `2893876 wip:작업중` 에 detached |
| 그 베이스와 `origin/main` 차이 | 302파일 |
| `alembic_version` | `e2f3a4b5c6d7` — 옛 베이스에 **없는** 리비전 |

즉 B2B 백엔드는 커밋된 적도, 배포된 적도 없음. 이 문서는 그 이식 기록임.

⚠ 옛 작업본은 지우지 않았음. `beavertalk-server` 의 detached HEAD 와 미추적 파일은 그대로 있음.

---

## 1. `member.role` 충돌 — 축을 나눔

`origin/main` 은 이미 `member.role` 을 갖고 있음. **의미가 다름.**

| | 배포된 서버 (`a4c8e1d7b209`) | B2B 초안 (`d0e1f2a3b4c5`, 미적용) |
|---|---|---|
| 값 | `user` \| `admin` | `learner` \| `teacher` |
| 용도 | `/__dev` 운영 도구 접근 제어 | 교사 콘솔 접근 권한 |

프로덕션 실측 분포 = `user` 32 · `admin` 3.

- 같은 컬럼을 다시 `add_column` 하면 마이그레이션이 **실패**함.
- 값을 합치면 「관리자이면서 교사」를 표현할 수 없음.

**확정 — `member.is_teacher` 불리언 신설.** `role` 은 손대지 않음.
`require_teacher` 는 `not member.is_teacher` 한 줄임.

---

## 2. 리베이스가 드러낸 결함 — 커리큘럼 언어축

`learning_item` 이 그 사이 다국어가 됨(`language` ISO 639-1. 유일성·FK·인덱스가 전부 언어 프리픽스).
B2B 챕터 질의 **3곳이 `language` 를 안 걸고 있었음** — 챕터 40개 창에 다른 언어 어휘가 섞임.

B2B 과제는 TOPIK 급수로 챕터를 자르므로 정의상 한국어임 → `CURRICULUM_LANGUAGE = "ko"` 로 고정.

---

## 3. 회화 목표 산정 변경 (인수인계 §2)

### 3.1 무엇이 잘못됐나

- `is_core` 는 **급수 단위로 상위 100~120개**를 뽑는 축(`priority_score` 내림차순).
- 챕터는 **`seq_no` 순 40개씩** 자르는 다른 축.
- 두 축이 서로를 모름 → 챕터당 핵심 수가 **0~20 으로 흔들리고 0 이 나옴**.
  그 챕터에서 회화 과제는 `0 / 0` 이 됨.

### 3.2 확정안

`domains/classroom/service/conversation_goal.py` 신설 — **단일 출처**.

- 목표 = 그 항목 집합 안에서 `priority_rank` 앞선 **10개**. `min(N, 항목 수)` 라 0 이 없음.
- `is_core` 전역 플래그는 **그대로 둠**(게이트·복습 선별·grandfathering 이 계속 씀).
- 대상은 챕터 전체가 아니라 **과제의 목표 항목** — 교사가 뺀 문장을 회화 목표로 주지 않음.
- `create_assignment` · `chapter_preview` · `link_call` 셋이 같은 헬퍼를 부름.

### 3.3 🔴 이미 두 벌로 갈려 있었음

인수인계 문서가 못 잡은 것임.

| 자리 | 넣던 값 |
|---|---|
| `create_assignment` | `sum(1 for i in items if i.is_core)` — 0~20 |
| `submission_service.link_call` | `len(_target_ids(assignment))` — 40 |

첫 통화 뒤에 **분모가 튀고 있었음**. 교사 화면에서 `회화 2 / 11` 이 `2 / 40` 이 됨.
테스트가 그 40 을 고정하고 있어 안 드러났음.

귀속과 점수를 나눔 — 통화 귀속은 목표 문장 전체로 판단하고 점수만 상위 N 으로 셈.
좁혀서 귀속까지 막으면 실제로 한 통화가 「안 했다」로 남음.

---

## 4. 신설 API

### 4.1 `GET /console/classrooms/{classroom_id}/overview`

홈 한 판을 1콜로 채움. 지금 콘솔은 상태 분포·최근 활동을 **과제 수만큼** 호출함.

```json
{
  "assignment_count": 12,
  "status_totals": { "not_started": 34, "in_progress": 11, "done": 129 },
  "per_assignment": [{ "assignment_id": 41, "completed": 16, "total": 18, "due_at": "..." }],
  "recent": [{ "classroom_member_id": 7, "roster_name": "...", "assignment_id": 41,
               "status": "done", "completed_at": "..." }],
  "learner_totals": [{ "classroom_member_id": 7, "done": 9, "missed": 3, "last_seen_at": null }]
}
```

- `recent` 는 완료된 것만 최신순 20건.
- 🔴 `last_seen_at` 은 **null 을 그대로 내림.** 0 이나 현재 시각으로 채우지 말 것 —
  「한 번도 안 들어온 사람」과 「오늘 들어온 사람」이 구별돼야 함. 콘솔에서 실제로 났던 버그임.
- `last_seen_at` 의 출처는 **마지막 통화 시각**(`max(call.created_at)`)임. 과제 수행이 아니라 앱 사용이 기준임.
- `missed` 는 **마감이 지난 미수행만** 셈. 안 그러면 과제 낸 그날 전원이 미수행자로 보임.
- 추이·증감·진도는 넣지 않았음 — `list_assignments` 1콜로 이미 됨.

### 4.2 `POST /console/classrooms/{classroom_id}/assignments/{assignment_id}/remind`

요청 본문 없음. 교사 권한 + 소유 검사.

응답 `200`
```json
{ "sent": 4, "skipped_no_device": 1, "skipped_unreachable_platform": 2, "sent_at": "..." }
```

응답 `409`
```json
{ "detail": "already_sent_today", "sent_at": "..." }
```

**하루 1회** — 멱등 키 `(assignment_id, 발송일)`. 조건부 `UPDATE … RETURNING` 으로 원자적으로 잡음.
읽고 나서 쓰면 다른 브라우저·다른 교사가 그 사이를 비집음.
`push_dispatch_log` 를 재사용하지 않은 이유는 그 테이블이 `alarm_id` NOT NULL 이라서임.

클레임을 **발송보다 먼저** 함(`dispatch_service._claim()` 과 같은 순서).
발송이 통째로 실패해도 그날 칸은 소모됨 — 「두 번 울리는 것」이 「오늘 못 보내는 것」보다 나쁨.

하루 경계는 `CLASSROOM_TZ = Asia/Seoul`. 🔴 해외 기관이 붙으면 `classroom` 에 시간대 칸을 만들 것.

---

## 5. 🔴🔴 발송 레일이 없었음

인수인계 §4.1 은 「발송 레일은 이미 있음」이라 적었으나 **사실이 아님.**

| 확인 | 결과 |
|---|---|
| 푸시 어댑터 함수 | `fcm.send_incoming_call` · `apns.send_incoming_call_voip` — **착신 전화 전용** |
| `device_token.platform` 실측 | `android_fcm`(유효 8) · `ios_voip`(유효 4) — **둘뿐** |

- 착신 경로로 숙제 알림을 보내면 학습자 폰이 **울림**.
- VoIP 토큰으로는 알림을 못 띄움 — iOS 는 VoIP 푸시를 받으면 즉시 CallKit 으로 착신을
  보고하도록 강제하고, 안 하면 앱을 죽임.

**한 것** — `core/fcm.send_notification()` 신설(표시형 알림, priority normal, TTL 12h).
문구는 `core/push_copy.py` 에 두고 `member.language` 로 고름(ko·en, 나머지는 영어).

**못 한 것 — iOS 학습자에게는 안 감.** 응답에 `skipped_unreachable_platform` 으로 정직하게 셈.
「기기 없음」으로 뭉치면 교사가 「앱을 안 깐 학생」으로 오해함 — 앱은 깔았고 우리가 못 보내는 것임.

☞ **앱 작업이 선행돼야 함**: iOS 가 일반 APNs 토큰을 등록하고(`platform='ios_apns'` 같은 값),
   서버가 그 토큰으로 alert 푸시를 보내야 함. 그때 이 칸이 0 이 됨.

---

## 6. 마이그레이션

`d0e1f2a3b4c5` 하나. **아직 어디에도 적용 안 됨**이라 초안을 그대로 고쳐 씀.

- `down_revision` 을 `c9d0e1f2a3b4` → **`e2f3a4b5c6d7`**(현행 배포 헤드)로 옮김.
- `member.role` 추가를 빼고 `member.is_teacher` 로 교체. `ix_member_role` 인덱스는 뺌 —
  교사는 극소수라 부분 인덱스가 아니면 이득이 없음.
- `assignment.manual_reminder_sent_at` 추가(자동 알림 칸과 별개).

### 🔴 적용 전에 알아야 할 것 — main 의 마이그레이션 그래프가 갈라져 있음

`origin/main` 에 **헤드가 둘**임.

| 헤드 | 파일 | 부모 |
|---|---|---|
| `b7e2c5a91d34` | `iap_receipt` | `a4c8e1d7b209` |
| `e2f3a4b5c6d7` | `call_resume_context` | `d1e2f3a4b5c6` |

- 프로덕션 `alembic_version` 은 **`e2f3a4b5c6d7` 한 행뿐**인데 `iap_receipt` 테이블은 **존재함.**
- 그러므로 `alembic upgrade head` 는 (a) 다중 헤드로 거부되거나 (b) `iap_receipt` 를 다시 만들려다 실패함.
- **이 브랜치가 만든 문제가 아님.** 손대지 않았음.

☞ 적용은 리비전을 명시할 것: `alembic upgrade d0e1f2a3b4c5`
☞ 그래프 정리(merge revision)는 서버 담당자 몫임.

---

## 7. 이 브랜치에 **넣지 않은** 것

| 항목 | 왜 |
|---|---|
| `f2a3b4c5d6e7_anonymize_on_leave` 마이그레이션 | 탈퇴 익명화는 별건임(법무 고지 쪽). 옛 작업본에 그대로 있음 |
| 커리큘럼 `seq_no` 정본 교체(인수인계 §3) | G드라이브 정본을 읽는 데이터 작업임. 코드 변경이 아님 |
| 자동 알림 크론(인수인계 §4.3) | §5 가 풀려야 의미가 있음. `reminder_sent_at` 칸과 응답 필드는 준비해 둠 |

---

## 8. 검증

```
PYTHONIOENCODING=utf-8 python -m pytest tests/ -q
```

- `tests/test_classroom.py` **48 통과**(이식 전 36 → 회화 목표 3 + 집계 4 + 알림 6 신설, 1건 정정).
- 전체 **1329 통과 / 3 스킵**.
- 실패 5건은 `tests/test_cascade_gate.py` — `psycopg2` 미설치인 로컬 venv 문제로 이 변경과 무관함.
  CI(`.github/workflows/deploy.yml`)는 requirements 전부를 깔고 돎.
- 라우트 실측 — `app.openapi()` 에 `/console/*` **12개**(신설 2 포함) + 학습자용 `/classrooms/*` 5개.
  ⚠ 이 FastAPI(0.141)는 `app.routes` 에 `_IncludedRouter` 지연 객체를 담음. 라우트 확인은
  `app.routes` 순회가 아니라 **`app.openapi()`** 로 할 것.

---

## 9. 다음

1. 서버 담당자와 브랜치 합의 → `origin/main` 병합.
2. `alembic upgrade d0e1f2a3b4c5` (⚠ §6 의 다중 헤드 주의).
3. 교사 계정에 `is_teacher = true` 데이터 작업.
4. 콘솔에 `VITE_API_BASE` 등록 → 목데이터 모드 해제.
5. 콘솔 한시 코드 제거(인수인계 §5) — 핵심 0 잠금 · 발송 버튼 · 알림 배지 · `faq4A`.
