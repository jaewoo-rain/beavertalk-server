# 예약전화 Android FCM 자동 발송 (server-side dispatch)

- **작성일**: 2026-07-06
- **상태**: ✅ **완료·운영 배포** — 코드/리뷰/테스트 + DB 마이그레이션 + Cloud Run 배포(test-api `00028`, demo-api `00005`) + Cloud Scheduler(매분) + 기기 수신까지 엔드투엔드 검증 완료.
- **배포 메모**: 서비스명은 `beavertalk-app-test-api` / `beavertalk-app-demo-api`(둘 다 같은 DB `beavertalk-app-db-pool`). 시크릿: `beavertalk-app-fcm-sa`(FCM SA JSON), `beavertalk-internal-dispatch-secret`. Scheduler `beavertalk-dispatch-calls` → test-api. Firebase 프로젝트는 `bt-dev-web-01`(초기 `beavertalk-e962f`에서 교체 — 프로젝트 교체 시 기존 기기토큰 무효화, 앱 `google-services.json`도 동일 프로젝트로 맞춰야 함).
- **관련 파일**:
  - 신규: `domains/push/**` (models·schemas·service·routers), `core/fcm.py`, `alembic/versions/d4e5f6a7b8c9_add_push_device_token_dispatch_log.py`, `tests/test_push_dispatch.py`
  - 수정: `db/registry.py`, `domains/account/models/member.py`, `core/config.py`, `main.py`, `requirements.txt`, `.dockerignore`

## 목표 & 범위
매분 Cloud Scheduler → `POST /api/v1/internal/dispatch-calls`(공유 시크릿) → 도래한 활성 알람 조회 → 회원 기기 토큰으로 **data-only FCM** 발송 → 앱이 CallKit 수신 화면 표시.

- **범위**: `domains/push/` 슬라이스, FCM 어댑터, dispatch 엔드포인트, 지터/중복 방지(캐치업 윈도우+멱등 로그), Alembic 리비전, 설정 5개, 로깅/requirements/.dockerignore, 결정적 pytest.
- **비범위**: iOS APNs VoIP(`platform`에 `ios_voip` 자리만 마련), normalcall WS 변경(불필요), Flutter 클라 배선(별도), 키 발급·Secret 주입·Scheduler 잡 생성(운영자).

## 아키텍처 & 데이터 흐름
```
Cloud Scheduler(매분, KST) ──POST /api/v1/internal/dispatch-calls (X-Internal-Secret)──▶
  routers/internal.py  [hmac.compare_digest, 미설정이면 403]
    └─▶ service/dispatch_service.py  [도메인 오케스트레이션 + 명시적 commit]
         ├─ 활성 알람 로드: selectinload(schedules) + joinedload(character), is_activate.is_(True)
         ├─ 캐치업 버킷(now, now-CATCHUP분) × _wall_hm(시:분) × 요일(버킷 기준) 매칭
         ├─ _claim(alarm_id, "YYYY-MM-DD HH:MM")  ← push_dispatch_log UNIQUE, on_conflict_do_nothing, commit-before-send
         └─ core/fcm.py send_incoming_call(멀티캐스트)  [순수 어댑터, DB 무지]
              └─ 폐기 토큰(UNREGISTERED/SenderIdMismatch) → is_valid=False (service commit)
         └─ _purge(): created_at 2일 초과 로그 삭제(예외 삼킴)
```

### 핵심 설계 결정 (제안서 대비 수정)
1. **모델 등록은 `db/registry.py`** (제안서의 `models/__init__.py` 아님 — Alembic env.py 가 보는 곳).
2. **FCM 발송기는 `core/fcm.py`** (도메인/DB 무지 어댑터, `storage.py`/`speechsuper.py` 규율). `dispatch_service`가 도메인·트랜잭션.
3. **firebase-admin**: 모듈 레벨 lazy 싱글턴 + `threading.Lock`(threadpool 동시 init 방지) + lifespan `fcm.warmup()`(콜드스타트 첫 링 지연 방지). 키 없으면 graceful 비활성.
4. **발송은 `send_each_for_multicast`** + per-token 결과 역매핑(폐기 토큰만 무효화, 일시 실패는 유지).
5. **분 매칭 = 캐치업 윈도우 + 멱등 로그**: at-least-once/지터로 인한 유실·이중발송 방지. 버킷의 요일로 판정(자정 경계 안전). claim-commit → send(at-most-once 편향: 전화는 이중 링이 미스보다 나쁨).
6. **upsert 원자화**: `pg_insert(...).on_conflict_do_update(token)` (SELECT-then-INSERT 레이스 제거).

### 타임존 검증 (실데이터)
실제 DB `alarm.time` = 센티넬 날짜 `2000-01-01` + 사용자 벽시각을 `+00`(UTC) 라벨로 저장(예 `2000-01-01 08:00:00+00`). 세션 tz UTC. `_wall_hm()`(astimezone(utc)→시:분)가 정확히 08:00 복원 → **교정 불필요**. 요일: `_DAY_CODES[Mon=0..Sun=6]` ↔ `schedule.day_of_week`(MON..SUN) 일치. **MVP는 전원 Asia/Seoul 가정**.

## 구현 내역 (모듈별)
- `domains/push/models/device_token.py` — `DeviceToken`(member 1:N CASCADE, token unique+index, is_valid server_default true).
- `domains/push/models/push_dispatch_log.py` — `PushDispatchLog`(UNIQUE(alarm_id, intended_fire_minute), created_at index).
- `domains/push/schemas/device.py` — `DeviceRegisterIn`/`DeviceOut`.
- `domains/push/service/device_service.py` — 원자 upsert / 소유자 스코프 delete.
- `domains/push/service/dispatch_service.py` — run()/_claim()/_ring()/_purge().
- `domains/push/routers/{device,internal}.py` + `__init__.py` — `POST /devices`(201), `DELETE /devices/{token}`(204), `POST /internal/dispatch-calls`(hidden, hmac).
- `core/fcm.py` — lazy init(Lock)+warmup()+send_incoming_call(멀티캐스트).
- 배선: `db/registry.py`(모델 2개), `member.py`(device_tokens 관계), `core/config.py`(5키), `main.py`(라우터·warmup·로깅), `requirements.txt`(firebase-admin>=6.5,<7), `.dockerignore`(키 제외).
- 마이그레이션: `d4e5f6a7b8c9`(down_revision `c2d3e4f5a6b7`) — device_token + push_dispatch_log, 비파괴/무중단.

### 시니어 리뷰 반영 (blocker 0)
- `_ring`: 폐기 토큰 없을 때 SELECT 가 연 트랜잭션을 `rollback()`으로 닫음(pgbouncer idle-in-transaction 방지).
- `_purge`: 예외를 삼켜 성공한 디스패치가 500 으로 가려지지 않게 함.

## 테스트 결과 (실제 실행)
- 신규 `tests/test_push_dispatch.py`: **18 passed**. (`uv run` 임시 env, firebase-admin·Postgres·네트워크 전부 목킹)
- 전체 스위트: **45 passed** (신규 18 + 기존 27; `pytest-asyncio` 포함 시). 회귀 없음.
- 커버: fcm 페이로드(data-only·전부 문자열·priority high·ttl 60s)·폐기/일시 실패 분류·graceful None / 정시·캐치업·요일·is_activate·무토큰·폐기토큰 무효화 / dedup 게이트 / 시크릿 403 / 자정 요일 엣지.

## 미해결 / 후속 작업 (TODO)
- **[운영자] DB 마이그레이션 적용**: 코드 리뷰만 완료, **라이브 DB 미적용**. `.env`에 `DATABASE_URL_DIRECT`(5432 직결) 추가 후 `alembic upgrade head` 권장(현재는 POOL 6543 만 존재). 또는 Supabase SQL Editor 수동 DDL(플랜 채팅 참고) 후 `alembic stamp d4e5f6a7b8c9`.
- **[운영자] Secret/env**: `FIREBASE_PROJECT_ID`, `FCM_SERVICE_ACCOUNT_JSON`(또는 `_FILE`), `INTERNAL_DISPATCH_SECRET`. 배포 시 `--update-secrets`.
- **[운영자] Cloud Scheduler 잡**: `* * * * *`, Asia/Seoul, asia-northeast3, `X-Internal-Secret` 헤더.
- **[테스트 갭]** Postgres 전용 경로(`_claim`/`_purge`/`upsert` 실제 SQL)와 `_ensure_app` 자격증명 파싱은 SQLite 목킹으로 미검증 → Postgres 통합테스트 또는 `scripts/smoke_*.py` 로 보강 권장.
- **[데이터]** `alarm.is_activate` NULL 은 발사 안 됨(`.is_(True)`). 생성 경로가 항상 True 세팅하는지 확인(현재 `AlarmCreate.is_activate` 기본 True).
- iOS APNs VoIP(후속).

## 리스크 & 결정 사항
- **타임존**: MVP Asia/Seoul 고정(§검증). 글로벌 확장 시 `member.timezone`.
- **캐치업 깊이**: `INTERNAL_DISPATCH_CATCHUP_MIN=1`(<2분 지각/1회 드롭 회복). FCM ttl=60s 가 유령전화 방지.
- **내부 엔드포인트 공개**: 서비스가 `--allow-unauthenticated`(앱 호출) → 시크릿이 유일 방어 + 멱등 로그가 블라스트 반경 흡수. 강화는 후속 OIDC.
- **at-most-once 편향**: claim-commit 후 크래시 시 그 분 1회 미스(재링 없음) — 전화 UX 상 이중 링보다 미스가 나음.
