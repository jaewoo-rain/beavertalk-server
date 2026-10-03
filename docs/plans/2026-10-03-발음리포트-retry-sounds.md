# 발음 리포트 `retry_sounds` — 다시 해볼 소리 카드 (PM-DEC-333/337/341)

작업: 2026-10-03 22:32 · 브랜치 `feat/retry-sounds` (기준 `origin/main` = `bc114da`)

## 무엇을 한다
`GET /api/v1/calls/{call_id}/pronunciation-report` 응답에 `retry_sounds: list[RetrySoundOut] = []`
를 더한다. **읽기 전용 · 필드 1개 추가 · 마이그레이션 없음.** 영향 API 1개.

기본값이 빈 배열이라 구버전 앱은 이 키를 무시한다(하위호환). 0개면 서버가 `[]` 를
보내고 **카드 숨김은 앱이 한다**(규칙 8).

## 어디를 고치나 (3파일 + 시험 1파일)

| 파일 | 무엇 |
|---|---|
| `domains/learning/schemas/pronunciation_report.py` | `RetrySoundOut` 신규 + `LearningSummaryOut.retry_sounds` |
| `domains/learning/service/weak_sound_service.py` | **공개** `get_sound_cards(db, member_id, sound_keys)` 신규 |
| `domains/learning/service/pronunciation_report_service.py` | 상수 2개 + `_retry_candidates`·`_retry_cards` + 조립 |
| `tests/test_pronunciation_report.py` | 선정 규칙 1~7 · 점수 동일성 · 하위호환 시험 |

라우터·리포지토리·모델은 **손대지 않는다.**

## 설계 판단 2개

### 1) 점수 산식을 복제하지 않는다 — `weak_sound_service` 에 공개 함수 하나
요구는 「취약 발음 목록(`/pronunciation/weak-sounds`)과 **같은 점수**」다. `_score_view`
는 `WeakSoundItem` 을 돌려주는 private 함수라 리포트가 직접 부르면 ①private 침범
②언젠가 두 산식이 갈라진다.

→ `weak_sound_service.get_sound_cards(db, member_id, keys) -> dict[str, WeakSoundItem]`
**공개 함수 하나**를 둔다. 안에서 목록 화면과 **똑같이** `_locale_of` → `get_lessons` →
`get_scores` → `get_i18n` → `_translated` → `_score_view` 를 통과시킨다. 점수가 같은
것은 비교로 지키는 게 아니라 **같은 코드를 지나기 때문**이다(구성상 동일).

레이어: `routers → service → repository` 를 지킨다. 리포트 서비스는 `weak_sound_repository`
를 직접 부르지 않고 **서비스에게 묻는다.** 서비스→서비스 선례 있음 —
`weak_sound_service.py:51` 이 `pronunciation_service.aggregate_sounds` 를,
`pronunciation_report_service.py:27` 이 `pronunciation_service` 를 이미 import 한다.
순환 없음(weak_sound_service 는 report service 를 모른다).

### 2) 기준값은 **서비스 모듈 상수**(설정값 아님)
`RETRY_MIN_MISSES = 2` · `RETRY_LIMIT = 3` 을 `pronunciation_report_service` 모듈 상수로
둔다. 같은 파일의 `_PASS_THRESHOLD = 80`, `weak_sound_service` 의 `LIST_SIZE`/
`RECOMMEND_BELOW`/`RECENT_CALLS` 와 같은 자리다.
`core/config.Settings` 로 올리지 않는 이유: ①배포 환경마다 달라야 할 값이 아니다(화면
규칙이라 dev/prod 가 갈리면 QA 가 재현 못 한다) ②비밀이 아니다 ③시험이 이 숫자를
고정한다 — env 로 흔들리면 시험이 환경 의존이 된다.

## 선정 규칙 → 구현 자리

| # | 규칙 | 어디서 걸러지나 |
|---|---|---|
| 1 | 이 통화 문장별 마지막 counted 복습만 | **현행 그대로** — `report.sounds` = `aggregate_sounds(get_last_counted_reviews(call_id))` (`pronunciation_service.py:272,275`) |
| 2 | `sound_key is None` 제외 | `_retry_candidates` (모음·위치 미부착 옛 복습) |
| 3 | `sound_lesson` 에 과가 있는 키만 | `get_sound_cards` 가 과 없는 키를 빼고, `_retry_cards` 가 `cards` 에 없는 키를 건너뛴다 |
| 4 | `misses >= 2` | `_retry_candidates` — `RETRY_MIN_MISSES` |
| 5 | `misses`↓ → 정확도(`passes/attempts`)↑ → `sound_key` | `_retry_candidates` 정렬 키 |
| 6 | 최대 3개 | `_retry_cards` — `RETRY_LIMIT` |
| 7 | 스텁 채점 제외 | **현행 충족** — 스텁은 `counted=False` 라 입력에 없다 |
| 8 | 0개면 카드 숨김 | **앱 몫.** 서버는 `[]` |

⚠ 규칙 3 은 규칙 6 **앞**이다 — 먼저 3개로 자르고 과 없는 것을 버리면 과가 있는 4순위가
억울하게 빠진다. 그래서 후보 **전체**를 `get_sound_cards` 에 넘기고, 과 있는 것만 세며
3개에서 멈춘다. 키 개수가 왕복 수를 늘리지 않으므로(아래) 비용은 같다.

## DB 왕복 (요청서 확인요청 ①) — **실측**
새 세션·새 threadpool 홉을 **만들지 않는다.** 이미 있는 `_call_date` 의 `run_db` 안에서
같이 읽는다(한 세션 = 한 커넥션).

`before_cursor_execute` 로 실제 실행된 SQL 문을 센 값(조건만 바꿔 같은 요청 5회):

| 조건 | SQL | 기준선 대비 | 세션 |
|---|---|---|---|
| A 자격 소리 0개(= misses≥2 가 없다) | 5 | **+0** | 3 |
| B 자격 2개 · 전부 미학습 | 13 | **+8** | 3 |
| C 자격 2개 · 전부 학습함 | 11 | **+6** | 3 |
| D 키 없는 소리만(모음만 틀림) | 5 | **+0** | 3 |
| E 자격 2개 · 과가 하나도 없다 | 6 | **+1** | 3 |

⚠ 내가 처음 **+4/+6 으로 예측했는데 실측은 +6/+8** 이었다. 차이 2개의 정체 —
`_locale_of` → `get_member_language` 의 `db.get(Member, …)` 가 **1번이 아니라 3번**이다.
`Member.reasons`·`Member.owned_characters` 가 `lazy="selectin"`(`member.py:115,131`)
이라 Member 를 읽을 때마다 `member_reason`·`member_character` 를 같이 긁는다.
→ 내역: 과 1 + 점수 1 + **회원 3** + 번역 1 (= +6) + 미학습이 있으면 복습 집계 2 (= +8).

- 전부 회원/로케일 단위 조회라 **후보 키 개수와 무관**하다(3개든 20개든 같다).
- 세션은 **3개 그대로**(늘지 않았다).
- 이 쿼리 세트는 `/pronunciation/weak-sounds` 가 **이미 운영에서 매번 내는 것과 같다**
  (그 화면이 Member selectin 3번도 같이 낸다). 전부 PK/단일컬럼 인덱스 조회이고
  `sound_lesson` 30행·`sound_lesson_i18n` 로케일당 30행·`member_sound_score` 회원당
  수십 행이라 전송량도 무시 가능. → **허용 범위.**
- 📌 **범위 밖 발견(건드리지 않았다)**: `get_member_language` 는 `language` 한 칸만
  필요한데 Member 전체 + selectin 2개를 긁는다. 컬럼 1개만 `select` 하면 리포트·취약
  발음·LLM 코칭 경로가 **각각 2쿼리** 줄지만, 그건 다른 API 들이 공유하는 함수라 이
  작업 범위(「이것만」)가 아니다. 별건으로 보고만 한다.

## 시험 (tests/test_pronunciation_report.py)
규칙마다 1개씩. sqlite 인메모리 + `sound_lesson`/`sound_lesson_i18n`/`member_sound_score`
시딩, `get_pronunciation_report` 는 기존 방식대로 가짜 주입(외부 의존 0).

- 규칙 2·3·4·5·6 각각, 0개 → `[]`, 점수가 `/weak-sounds` 와 **같은 값**(두 API 를 실제로
  호출해 대조), 번역 라벨 우선, 기존 응답 회귀(필드 추가가 기존 키를 안 바꾼다).

### 돌연변이 검증(시험이 진짜 붙잡고 있나)
구현을 13가지로 **일부러 틀리게** 바꿔 어느 시험이 죽는지 확인했다(기준값·정렬 3단 각
단계·규칙3/6 순서 뒤집기·attempts 바꿔치기·점수 산식 복제·번역 무시·학습점수 무시 …).
처음엔 **3개가 안 잡혔다** — 그 3개가 시험의 구멍이었고, 다음과 같이 메꿨다.
1. 규칙 2 필터 삭제가 응답을 안 바꾼다(과 조회가 `""` 키를 못 찾아 규칙 3 이 뒤에서
   잡는다). → 후보 단계를 직접 보는 순수 함수 시험을 추가(비용 차이를 고정).
2. 정렬 1단(misses) 엔드포인트 시험의 데이터가 misses 전부 동률이라 1단을 뒤집어도
   답이 같았다. → misses 가 다른 소리를 넣어 1단·2단을 함께 쓰게 고쳤다.
3. 정확도 반올림 시험이 **못한 쪽을 사전순으로도 앞에** 둬서 두 구현이 같은 답을 냈다.
   → 못한 쪽을 사전순 뒤로 바꿨다.
최종: **13/13 전부 시험에 잡힌다.**

## 하지 않는 것
- 모델 변경·마이그레이션 **없음**(요청서 명시). 필요해지면 구현 않고 보고.
- 라우터 시그니처·통화 경로·프롬프트 **무수정**(R4 무관).
