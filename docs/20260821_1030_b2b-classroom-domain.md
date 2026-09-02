# B2B 교사 콘솔 — classroom 도메인 구축

작성 2026-08-21 · 기획 정본 `claude_code/23_제품기획_하네스/_output/2026-08-19_비버톡_B2B숙제/` 10문서

---

## 0. 무엇을 만들었나

한국어 교육기관이 **TOPIK 챕터를 숙제로 내고 결과를 보는** 서버 레일.

| 층 | 산출물 |
|---|---|
| 모델 | `classroom` · `classroom_member` · `assignment` · `submission` + `member.role` |
| 마이그레이션 | `d0e1f2a3b4c5_b2b_classroom_tables.py` (down_revision `c9d0e1f2a3b4`) |
| 서비스 | `domains/classroom/service/classroom_service.py` |
| 라우터 | `/console/*` (교사) · `/classrooms/*` (학습자) |
| 테스트 | `tests/test_classroom.py` — **19건 통과** |

---

## 1. 설계 결정

### 1.1 조직 3단을 만들지 않는다
조직→반→좌석이 아니라 **반 1단**이다. `classroom.teacher_member_id` 로 교사에 직결한다.
테이블 1개와 권한 분기 로직이 통째로 사라진다.

### 1.2 교사 전용 테이블·인증을 만들지 않는다
`member.role` 컬럼 하나(`learner`|`teacher`)다. 인증은 기존 Supabase 토큰(`CurrentMember`)을 그대로 쓴다.
기본값 `learner` + `server_default` 라 기존 행 전부가 한 번에 채워진다.

### 1.3 남의 반은 404 다
403 은 "있긴 있다"를 알려준다. `owned()` 는 소유자가 아니면 존재 자체를 부정한다.

### 1.4 스냅샷을 굳힌다
`assignment.target_item_ids` 는 **출제 시점** 항목 id 배열이다.
커리큘럼이 나중에 바뀌어도 이미 낸 과제는 그대로여야 한다.

### 1.5 미수행 행을 미리 깐다
과제를 만들 때 명단 전원에게 `submission(status='not_started')` 를 선깔기한다.
**"누가 안 했나"가 이 제품의 값어치**다 — LEFT JOIN 으로 매번 유도하지 않는다.

### 1.6 내보내기는 소프트다
`classroom_member.left_at` 을 세운다. 개인 결과 표시는 사라지지만 반 평균 집계에는 남는다.
하드 삭제하면 이미 산출한 평균이 소급해서 바뀐다.

### 1.7 참여코드 charset
`ABCDEFGHJKLMNPQRSTUVWXYZ23456789` — **I·O·0·1 제외**.
교사가 칠판에 적고 학습자가 옮겨 적는다. 손글씨에서 서로 오인되는 글자를 뺀다.

---

## 2. 🔴 발견 — `learning_item.examples` 는 `kind` 에 따라 권리가 갈린다

컬럼 주석은 `"교재 예문(JSON 배열 문자열)"` 한 줄뿐이라 **오인하기 쉽다.**
`scripts/seed.py` 실측 결과:

| kind | `examples` 의 실제 내용 | 근거 | 표시 |
|---|---|---|---|
| `vocab` | **자체 LLM 생성 문장** | `seed.py:172` `[e["example"]]` ← `CEFR_문장_통합.xlsx` | 가능 |
| `grammar` | **서울대 한국어 교재 예문** | `한국어_단계별_문법_12단계.xlsx` `Read Me` 시트 | 🔴 금지 |

**조치** — `vocab_example(item)` 헬퍼를 통과해야만 예문이 나간다. `kind != 'vocab'` 이면 `None`.
`assignment.grammar_items` 스냅샷에도 **표제(`surface`)만** 담는다.
회귀 테스트 `test_vocab_example_refuses_grammar_items` 가 이를 고정한다.

☞ 같은 컬럼에 권리가 다른 두 자산이 섞여 있다. 컬럼 주석을 고치는 것이 근본 해법이나,
   그건 learning 도메인 소유라 이 작업 범위 밖이다. **소비 측에서 막았다.**

---

## 3. API

### 교사 (`/console`)
```
GET    /console/classrooms
POST   /console/classrooms
GET    /console/classrooms/{id}
PATCH  /console/classrooms/{id}
POST   /console/classrooms/{id}/join-code      새 코드 발급
POST   /console/classrooms/{id}/archive
GET    /console/classrooms/{id}/learners
PATCH  /console/classrooms/{id}/learners/{cm_id}
DELETE /console/classrooms/{id}/learners/{cm_id}
GET    /console/classrooms/{id}/assignments
POST   /console/classrooms/{id}/assignments
GET    /console/classrooms/{id}/assignments/{aid}   결과(제출·취약문장)
GET    /console/curriculum/{grade}/chapters/{ch}    챕터 미리보기
```

### 학습자 (`/classrooms`)
```
GET    /classrooms/preview?join_code=XXXXXX   인증 불요 — A2 반 확인
POST   /classrooms/join                        A3 이름·동의
GET    /classrooms/my/assignments              A5 숙제 목록
DELETE /classrooms/{id}/leave                  DA1 반 나가기
```

**응답에 문안을 넣지 않는다.** 앱은 30개 로케일이다 — 서버가 문장을 만들면 30개 로케일이
서버로 넘어온다. 데이터만 내려보내고 조립은 클라이언트가 한다.

---

## 4. 개인정보 경계 (`04_학습자관리.md` §5)

`RosterMemberOut` 에 **`member_id` 조차 넣지 않았다.** 콘솔이 다른 API 로 학습자를 조회할
통로를 애초에 열지 않기 위해서다.

| 교사가 본다 | 교사가 못 본다 |
|---|---|
| `roster_name` · `student_no` · 이 반 과제의 수행 여부·결과 | 이메일 · 앱에서 쓰는 이름 · 국적 · 모국어 · 이 반 밖의 통화·학습 기록 |

---

## 5. 검증

```
PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python -m pytest tests/test_classroom.py -q
```

| 항목 | 결과 |
|---|---|
| 테스트 | **19 passed** |
| 모델 ↔ 마이그레이션 컬럼 대조 | **불일치 0** (4테이블 49컬럼 + `member.role`) |
| sqlite 인메모리 `create_all` | 27테이블 정상 |

☞ 이 PC 에 `beavertalk-server` conda env 가 없어 **일회용 venv(`.venv-check`)** 로 돌렸다.
   `.gitignore` 대상이며 CI·운영과 무관하다.

---

## 6. 남은 것

| 항목 | 사유 |
|---|---|
| dev DB 에 `alembic upgrade head` 적용 | 파괴적 작업 — R6 대로 사용자 확인 후 |
| 제출 기록 배선 (`submission` 을 실제로 채우는 쪽) | `learning_intro` 완료 훅 + `normalcall` 통화후 분석에서 과제 id 로 묶어야 함 |
| 마감 전날 알림 | 기존 FCM 배선 재사용 · `assignment.reminder_sent_at` 컬럼은 준비됨 |
| 반 단위 '덜 쓰인 표현' 집계 | `item_evidence` 를 접어야 한다. 지금은 빈 배열을 반환한다(지어내지 않음) |
