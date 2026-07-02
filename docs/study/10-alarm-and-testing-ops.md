# 10장. alarm 도메인 · 테스트 · 배포

> 📘 **이 장을 읽고 나면**
> - `Alarm` 1 : N `Schedule` 구조와, 요일을 통째로 갈아끼울 때 옛 요일이 자동 삭제되는 **orphan removal 패턴**(JPA `orphanRemoval` 대응)을 설명할 수 있어요.
> - 소유권 체크(`_get_owned` → 404)와 N+1 방지(`selectinload` / `joinedload`)가 왜 필요한지 알 수 있어요.
> - pytest 단위 테스트와 `scripts/smoke_*.py` 수동 스모크의 역할 차이를 Spring 비유로 구분할 수 있어요.
> - Dockerfile(왜 ffmpeg? 왜 `$PORT`?)과 Cloud Run 배포·환경변수의 큰 그림을 그릴 수 있어요.
> - ⚠️ 지금 `smoke_common.py`가 왜 깨져 있는지, 어떤 리팩터링의 잔재인지 알 수 있어요.

---

## PART A — alarm 도메인

### 1. 두 테이블: Alarm 과 Schedule

**왜 필요한가요?**
"평일 오전 8시 30분에 비버 캐릭터가 깨워주세요" 같은 알람은 두 조각으로 나뉩니다. 알람 자체(시간·캐릭터·켜짐 여부)와, "무슨 요일마다 반복?"이라는 요일 목록이에요. 요일은 여러 개(월·수·금)일 수 있으니 **1 : N** 관계가 자연스럽습니다.

**Spring 비유**
`@Entity Alarm` 하나에 `@OneToMany List<Schedule> schedules` 가 달린 구조 그대로예요. `Schedule` 쪽은 `@ManyToOne Alarm alarm` 으로 되돌아오는 양방향 관계고요.

**작은 코드 예시**
```python
class Alarm(Base, TimestampMixin):
    member_id: Mapped[int] = mapped_column(ForeignKey("member.member_id", ondelete="CASCADE"))
    character_id: Mapped[int] = mapped_column(ForeignKey("character.character_id", ondelete="RESTRICT"))
    time: Mapped[Optional[datetime]]
    is_activate: Mapped[Optional[bool]]

    schedules: Mapped[list["Schedule"]] = relationship(
        back_populates="alarm", cascade="all, delete-orphan",
        passive_deletes=True, lazy="selectin",
    )
```

FK 두 개의 삭제 정책이 다른 점을 눈여겨보세요. `member` 는 `ondelete="CASCADE"`(회원이 사라지면 알람도 같이), `character` 는 `ondelete="RESTRICT"`(캐릭터가 알람에 물려 있으면 캐릭터 삭제를 막음)입니다.

**실제 코드 링크**
- [domains/alarm/models/alarm.py:19](../../domains/alarm/models/alarm.py#L19) — `Alarm` 엔티티, FK·relationship 정의
- [domains/alarm/models/schedule.py:16](../../domains/alarm/models/schedule.py#L16) — `Schedule`(요일 1개 = 1 row), `day_of_week` 는 `Text`

**흔한 함정**
`day_of_week` 는 DB 상 자유 문자열(`Text`)이에요. "MON/TUE/…" 같은 값 검증은 DB가 아니라 **스키마 계층**의 `Literal` 이 담당합니다([schemas/alarm.py:11](../../domains/alarm/schemas/alarm.py#L11)). 잘못된 요일은 422로 거부돼요.

**한 줄 요약**
알람 = `Alarm`(시간·캐릭터·활성) 1 : N `Schedule`(반복 요일 한 개씩).

---

### 2. 요일 교체 = orphan removal 패턴 ⭐

**왜 필요한가요?**
알람 수정 화면에서 반복 요일을 "월·수·금" → "화·목" 으로 바꾸면, 옛 요일(월·수·금) 3개 row는 **DB에서 사라져야** 합니다. 그런데 서비스 코드에는 `DELETE schedule` 문이 한 줄도 없어요. 비결이 `cascade="all, delete-orphan"` 입니다.

**Spring 비유**
JPA의 `@OneToMany(orphanRemoval = true)` 와 정확히 같습니다. 부모의 컬렉션에서 자식을 떼어내면(리스트에서 빠지면), JPA가 그 자식을 "고아(orphan)"로 보고 `DELETE` 를 자동 발행하죠. SQLAlchemy의 `delete-orphan` 도 동일하게, **컬렉션에서 빠진 자식**을 flush 시점에 삭제합니다.

**작은 코드 예시**
```python
if data.days_of_week is not None:
    # 기존 요일 통째 교체: clear() → delete-orphan 이 옛 schedule 삭제
    alarm.schedules.clear()
    alarm.schedules.extend(Schedule(day_of_week=d) for d in data.days_of_week)
self.db.commit()
```

`clear()` 로 옛 `Schedule` 들이 컬렉션에서 빠지는 순간 고아가 됩니다. `commit()` 때 SQLAlchemy가 옛 row들에 `DELETE`, 새 row들에 `INSERT` 를 알아서 발행해요. 개발자는 "원하는 최종 상태"만 컬렉션에 세팅하면 됩니다.

**실제 코드 링크**
- [domains/alarm/service/alarm_service.py:58](../../domains/alarm/service/alarm_service.py#L58) — `update()` 의 요일 교체 블록
- [domains/alarm/models/alarm.py:34](../../domains/alarm/models/alarm.py#L34) — `cascade="all, delete-orphan"` 가 걸린 relationship

**흔한 함정**
- `cascade` 에 `delete-orphan` 이 없으면 `clear()` 는 자식의 FK 를 `NULL` 로 만들려다 실패하거나 고아 row가 DB에 남습니다. "컬렉션에서 뺐는데 왜 안 지워지지?"의 90%가 이 옵션 누락이에요.
- `delete()`(알람 전체 삭제)는 이 orphan 규칙 **더하기** FK `ondelete="CASCADE"` 로 이중 안전망을 갖습니다([alarm_service.py:73](../../domains/alarm/service/alarm_service.py#L73)).

**한 줄 요약**
컬렉션을 `clear()` 후 재구성하면 `delete-orphan` 이 옛 자식을 알아서 `DELETE` — JPA `orphanRemoval` 과 똑같아요.

---

### 3. 소유권 체크 → 404

**왜 필요한가요?**
남의 알람을 URL의 `alarm_id` 만 바꿔서 조회·수정·삭제하면 안 되겠죠. 이걸 막는 게 `_get_owned` 입니다.

**Spring 비유**
Spring Security의 메서드 보안(`@PreAuthorize("#alarm.memberId == authentication.principal.id")`)을 서비스 안에서 손으로 구현한 형태예요. 단, 여기선 403이 아니라 **404** 를 던집니다 — "권한 없음"이라 알려주면 "그 ID의 알람이 존재한다"는 정보까지 새어나가므로, 존재 자체를 숨기는 전략입니다.

**작은 코드 예시**
```python
def _get_owned(self, member_id: int, alarm_id: int) -> Alarm:
    alarm = self.repo.get(alarm_id)
    if alarm is None or alarm.member_id != member_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "알람을 찾을 수 없습니다.")
    return alarm
```

`get`/`update`/`set_active`/`delete` 모든 단건 작업이 이 함수를 먼저 통과합니다.

**실제 코드 링크**
- [domains/alarm/service/alarm_service.py:79](../../domains/alarm/service/alarm_service.py#L79) — `_get_owned`(없거나 남의 것이면 404)

#### 엔드포인트 표

라우터 prefix 는 `/alarms`(전역 prefix `/api/v1` 아래)입니다([routers/alarm.py:11](../../domains/alarm/routers/alarm.py#L11)).

| 메서드 | 경로 | 하는 일 | 성공 코드 |
|---|---|---|---|
| `GET` | `/alarms` | 내 알람 목록 | 200 |
| `POST` | `/alarms` | 알람+요일 생성 | 201 |
| `GET` | `/alarms/{id}` | 단건 조회(소유 체크) | 200 |
| `PUT` | `/alarms/{id}` | 부분 수정 + 요일 교체 | 200 |
| `DELETE` | `/alarms/{id}` | 삭제(자식 함께) | 204 |
| `POST` | `/alarms/{id}/activate` | 켜기 | 200 |
| `POST` | `/alarms/{id}/deactivate` | 끄기 | 200 |

**실제 코드 링크**
- [domains/alarm/routers/alarm.py:14](../../domains/alarm/routers/alarm.py#L14) — 7개 엔드포인트 전부

**흔한 함정**
activate/deactivate 를 `PUT` 의 `is_activate` 로도 바꿀 수 있는데, 왜 별도 엔드포인트를 뒀을까요? 토글은 요일·시간을 건드리지 않는 **단순·잦은 동작**이라 얇은 전용 API가 클라이언트에 편하기 때문입니다. 내부적으로는 둘 다 `set_active` 를 호출해요([alarm_service.py:66](../../domains/alarm/service/alarm_service.py#L66)).

**한 줄 요약**
단건 작업은 전부 `_get_owned` 를 거쳐 "없거나 남의 것"이면 404.

---

### 4. N+1 방지: selectinload + joinedload

**왜 필요한가요?**
알람 10개를 목록으로 뿌릴 때, 각 알람마다 "요일들"과 "캐릭터"를 다시 쿼리하면 1(목록) + 10(요일) + 10(캐릭터) = 21번 쿼리가 나갑니다. 이게 그 악명 높은 **N+1** 이에요.

**Spring 비유**
JPA에서 `LAZY` 컬렉션을 루프에서 건드릴 때 터지는 N+1과 똑같은 문제입니다. 해법도 대응돼요 — JPA `fetch join`(`JOIN FETCH`) ≈ SQLAlchemy `joinedload`, JPA `@BatchSize`/배치 IN 로딩 ≈ SQLAlchemy `selectinload`.

**작은 코드 예시**
```python
@staticmethod
def _load_opts() -> list:
    # 컬렉션(schedules)=selectinload, 스칼라(character)=joinedload → N+1 방지.
    return [selectinload(Alarm.schedules), joinedload(Alarm.character)]
```

- `schedules`(1 : N 컬렉션) → `selectinload`: 부모 id들을 모아 `... WHERE alarm_id IN (...)` 한 방으로 자식을 긁어옵니다. 컬렉션에 `JOIN` 을 쓰면 부모 row가 자식 수만큼 뻥튀기(cartesian)되므로 컬렉션엔 selectin 이 안전해요.
- `character`(N : 1 스칼라) → `joinedload`: 한 건이라 `JOIN` 한 번으로 끝. row 뻥튀기 걱정이 없습니다.

**실제 코드 링크**
- [domains/alarm/repository/alarm_repository.py:17](../../domains/alarm/repository/alarm_repository.py#L17) — `_load_opts()`(로딩 전략)
- [domains/alarm/repository/alarm_repository.py:26](../../domains/alarm/repository/alarm_repository.py#L26) — `list_by_member` 가 `.options(*self._load_opts())` 적용

**흔한 함정**
`_load_opts` 가 왜 상수가 아니라 **메서드**일까요? 주석대로, 관계 표현식(`Alarm.schedules`)은 모든 모델이 registry 에 등록된 **뒤**에야 해석됩니다. 모듈 로딩 시점에 상수로 만들면 import 순서에 따라 관계 해석이 깨질 수 있어, 호출 시점에 만들어요.

**한 줄 요약**
컬렉션엔 `selectinload`, 스칼라엔 `joinedload` — JPA fetch join / batch 로딩의 파이썬 판.

---

## PART B — 테스트

BeaverTalk의 검증은 두 겹입니다. **자동 단위 테스트**(pytest)와 **수동 스모크 스크립트**(`scripts/smoke_*.py`)예요.

### 5. pytest 단위 테스트

**왜 필요한가요?**
외부 발음평가 API(SpeechSuper)는 키·오디오·네트워크가 있어야 실제로 돌아갑니다. 그런데 테스트는 그런 것 없이도 빨리·항상 통과해야 하죠. 그래서 `test_speechsuper.py` 는 **폴백·매핑·계약** 세 가지를 네트워크 없이 검증합니다.

**Spring 비유**
`@ExtendWith(MockitoExtension.class)` 로 외부 클라이언트를 목킹하고 순수 함수만 검증하는 JUnit 단위 테스트예요. pytest의 `monkeypatch` 가 Mockito의 `when(...).thenThrow(...)` 역할을 합니다.

**작은 코드 예시**
```python
def test_call_failure_falls_back(monkeypatch):
    """실호출 경로에서 예외가 나면 스텁으로 폴백(예외 전파 안 됨)."""
    monkeypatch.setattr(ss.settings, "SPEECH_SUPER_APP_KEY", "x", raising=False)
    def boom(*_a, **_k): raise RuntimeError("network down")
    monkeypatch.setattr(ss, "_load_audio", boom)
    out = ss.assess_pronunciation("안녕", "https://example.com/a.wav")
    _assert_contract(out)  # 스텁 결과
```

세 가지 초점:
- **폴백**: 키·오디오가 없거나 실호출이 터지면 결정적 스텁 결과를 반환(앱이 죽지 않음).
- **매핑**: SpeechSuper 응답의 `words[]`/`overall` 을 도메인 형태(글자별 점수)로 정확히 변환.
- **계약(contract)**: 반환 dict의 키·타입·등급("상/중/하")이 항상 일정.

**실제 코드 링크**
- [tests/test_speechsuper.py:15](../../tests/test_speechsuper.py#L15) — `_assert_contract`(반환 계약 검증 헬퍼)
- [tests/test_speechsuper.py:96](../../tests/test_speechsuper.py#L96) — 실호출 실패 → 폴백 테스트

**흔한 함정**
계약 테스트는 "값이 맞나"가 아니라 "**모양이 안 깨졌나**"를 봅니다. 스텁 점수가 바뀌어도 통과하지만, 키를 하나 빼먹으면 잡히죠. 리팩터링 안전망으로서 이게 핵심이에요.

**한 줄 요약**
`test_speechsuper.py` 는 네트워크 없이 폴백·매핑·계약을 지키는 pytest 단위 테스트.

---

### 6. scripts/smoke_*.py — 수동 end-to-end 스모크

**왜 필요한가요?**
단위 테스트가 "부품이 맞나"라면, 스모크는 "**조립품이 실제 HTTP로 도나**"를 봅니다. 각 스크립트는 in-memory SQLite로 앱을 띄우고 `get_db` 를 오버라이드한 뒤, `TestClient` 로 실제 요청을 순서대로 보내 `assert` 합니다.

**Spring 비유**
`@SpringBootTest(webEnvironment=RANDOM_PORT)` + `TestRestTemplate` + H2 인메모리 DB 조합과 판박이예요. `main.app.dependency_overrides[get_db]` 가 스프링의 `@TestConfiguration` 으로 DataSource 를 H2로 바꾸는 것에 대응합니다.

**작은 코드 예시** (모든 smoke API 스크립트의 공통 골격)
```python
main.app.dependency_overrides[get_db] = override_get_db   # 인메모리 sqlite 주입
client = TestClient(main.app)
r = client.put(f"/api/v1/alarms/{aid}", headers=H, json={"days_of_week": ["TUE", "THU"]})
assert set(r.json()["days_of_week"]) == {"TUE", "THU"}     # orphan removal 검증
```

**실제 코드 링크**
- [scripts/smoke_alarm_api.py:41](../../scripts/smoke_alarm_api.py#L41) — `get_db` 오버라이드 + `TestClient`
- [scripts/smoke_alarm_api.py:90](../../scripts/smoke_alarm_api.py#L90) — 요일 전체 교체(orphan removal)를 실제 HTTP로 검증

#### 스모크 스크립트 카탈로그 (각 한 줄)

| 스크립트 | 역할 |
|---|---|
| `smoke_common` | ⚠️ DB 없이 security/JWT/스키마/`/health` 검증 — **현재 깨짐**(아래 6-1) |
| `smoke_infra` | config 로드 → engine → session 팩토리 import 만 확인(DB 미접속) |
| `smoke_models` | 14개 모델 import + `configure_mappers()` 정합성 + 인메모리 `create_all` |
| `smoke_connect` | 실제 Supabase 연결(비밀번호 필요) — DB URL 살아있는지 확인 |
| `smoke_auth_api` | 소셜 로그인 + 비밀번호 재설정 흐름(sqlite) |
| `smoke_account_api` | 회원가입 → 로그인(JWT) → `/me` 보호 라우터 → 미인증 차단 |
| `smoke_commerce_api` | 캐릭터 목록(할인·소유) → 구매 → 중복 409 → 소유 목록 |
| `smoke_payment_api` | 구독+구매 → 결제 내역 탭(전체/구독/캐릭터) + 이번 달 합계 |
| `smoke_learning_api` | 통화 일괄 저장 → 목록/상세/원본/평점 → 북마크 → 복습 → 소유 격리 |
| `smoke_review_api` | 복습 발음 채점 end-to-end |
| `smoke_alarm_api` | 알람 생성(요일 중첩) → 조회 → 요일 교체 → 비활성 → 삭제 → 소유 격리 |
| `smoke_live` | 실제 Supabase로 가입/로그인/조회 라이브 검증 후 테스트 회원 삭제 |

**한 줄 요약**
`smoke_*.py` 는 `TestClient` + 인메모리 SQLite로 실제 API 흐름을 손으로 돌려보는 수동 통합 점검이에요.

---

### 6-1. ⚠️ 정직한 기록: `smoke_common.py` 는 지금 깨져 있습니다

**무슨 일이 있었나요?**
`smoke_common.py` 는 첫 줄부터 이렇게 import 합니다.

```python
from core.security import create_access_token, decode_token, hash_password, verify_password
```

그런데 **`core/security.py` 파일이 지금 존재하지 않습니다.** 인증이 자체 JWT/bcrypt(`core.security`)에서 **Supabase Auth**(`core/supabase_auth.py`)로 이전되면서 옛 모듈이 삭제됐고, 이 스모크만 업데이트를 못 받은 잔재예요. 실행하면 13번째 줄에서 곧장 `ModuleNotFoundError: No module named 'core.security'` 가 납니다.

**어떻게 확인했나요?**
- `core/` 디렉터리에 `security.py` 없음 — 대신 `supabase_auth.py` 존재.
- 프로젝트 전체에서 `core.security` 를 import 하는 파일은 `scripts/smoke_common.py` **단 하나**.

**실제 코드 링크**
- [scripts/smoke_common.py:13](../../scripts/smoke_common.py#L13) — 삭제된 `core.security` 를 import 하는 깨진 줄
- [core/supabase_auth.py](../../core/supabase_auth.py) — 현재 인증을 담당하는 대체 모듈

**흔한 함정 / 교훈**
"모듈을 지웠으면, 그 모듈을 부르는 곳을 전부 따라가라." 도메인 서비스·라우터는 `core.deps` 를 거쳐 이미 Supabase Auth 로 옮겨졌지만, 개발자용 보조 스크립트는 CI에 안 걸려 방치되기 쉽습니다. 이 스크립트는 (1) 삭제하거나 (2) Supabase 토큰 검증 기준으로 다시 쓰는 게 맞아요. **다른 `smoke_*_api.py` 들은 `core.security` 를 쓰지 않으므로 정상 동작합니다** — 이 파일 하나만 고립되어 깨진 상태입니다.

**한 줄 요약**
`smoke_common.py` 는 Supabase Auth 이전 후 삭제된 `core.security` 를 아직 import 해 지금 실행하면 즉시 깨지는, 정리 안 된 잔재예요.

---

## PART C — 배포 / 운영

### 7. Dockerfile — 왜 ffmpeg? 왜 $PORT?

**왜 필요한가요?**
Cloud Run은 컨테이너 이미지를 받아 돌립니다. 그래서 앱을 이미지로 굽는 레시피(Dockerfile)가 필요해요.

**Spring 비유**
Spring Boot의 `Dockerfile`(또는 buildpack)로 fat-jar 를 이미지화하는 것과 같습니다. `CMD uvicorn ...` 이 `ENTRYPOINT java -jar app.jar` 자리예요.

**작은 코드 예시** (핵심 두 줄)
```dockerfile
RUN apt-get install -y --no-install-recommends ffmpeg   # TTS/녹음 인코딩용
...
ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
```

- **ffmpeg**: 표현 TTS(PCM→MP3), 복습 녹음(WAV→MP3) 인코딩에 씁니다. 없으면 WAV로 폴백하지만, MP3 저장하려면 필요해요. 캐시 효율을 위해 의존성 설치보다 앞 레이어에 둡니다.
- **`--host 0.0.0.0`**: Cloud Run은 컨테이너의 `$PORT`(기본 8080)로 트래픽을 보냅니다. `127.0.0.1` 에만 바인딩하면 외부 요청을 못 받아 startup 타임아웃이 나요. 반드시 `0.0.0.0`.
- 의존성(`requirements.txt`)을 코드보다 먼저 `COPY` → 레이어 캐시로 재빌드가 빨라집니다.

**실제 코드 링크**
- [Dockerfile:11](../../Dockerfile#L11) — ffmpeg 설치(이유 주석 포함)
- [Dockerfile:23](../../Dockerfile#L23) — `ENV PORT=8080` + `0.0.0.0` 바인딩 CMD
- [requirements.txt](../../requirements.txt) — psycopg2/supabase/google-genai 등 런타임 의존성
- [docs/DEPLOY_CLOUD_RUN.md](../../docs/DEPLOY_CLOUD_RUN.md) — 배포 단계별 따라하기(구성 생성 → 시크릿 → 마이그레이션 → deploy)

**흔한 함정**
`.env` 를 이미지에 굽지 마세요. `.dockerignore` 로 제외하고, 비밀값은 **Secret Manager** 로 주입합니다(`--set-secrets`). 배포 문서 2·3장 참고.

**한 줄 요약**
ffmpeg는 오디오 인코딩용, `0.0.0.0:$PORT` 는 Cloud Run 필수, 비밀값은 이미지가 아니라 Secret Manager로.

---

### 8. 환경변수 & dev/prod 차이

**왜 필요한가요?**
설정을 코드에 하드코딩하면 환경(로컬/운영)마다 다시 빌드해야 합니다. BeaverTalk은 `core/config.py` 의 `Settings`(pydantic-settings)가 `.env`·환경변수를 읽어 한곳에 모읍니다.

**Spring 비유**
Spring의 `application.yml` + `@ConfigurationProperties` 조합이에요. `ENV=dev/prod` 가 스프링 프로파일(`spring.profiles.active`)에 대응합니다.

**핵심 환경변수 요약** (전체는 README 5장 + `config.py`)

| 변수 | 필수 | 역할 |
|---|---|---|
| `DATABASE_URL_POOL` | ✅ | 런타임 DB (Supabase 6543 풀러) |
| `DATABASE_URL_DIRECT` | 권장 | 마이그레이션용(5432 직결). 미설정 시 POOL 폴백 |
| `ENV` | | `dev`/`prod` (기본 `dev`) |
| `JWT_SECRET` | prod 필수 | JWT 서명 키. prod에서 기본값이면 **기동 차단** |
| `SPEECH_SUPER_*` | 선택 | 없으면 발음평가가 결정적 스텁으로 폴백 |
| `GEMINI_API_KEY` / `USE_VERTEX` | 선택 | 없으면 통화·분석·TTS가 graceful 폴백 |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | 선택 | 없으면 Storage 업로드 생략(`voice_url=None`) |

**dev / prod 차이**
- **dev**: `JWT_SECRET` 기본값 허용, SQL 로깅·테스트 콘솔(`/__console`) 활성. 외부 키 없이도 스텁 폴백으로 앱이 뜹니다.
- **prod**: `ENV=prod` + 기본 `JWT_SECRET` 이면 `_guard_prod_secret` 이 **기동을 막습니다**(시크릿 교체 누락 사고 방지). `/__console` 자동 숨김.

**실제 코드 링크**
- [core/config.py:15](../../core/config.py#L15) — `Settings`(모든 `FGPU`… 아님, `.env` 기반 설정)
- [core/config.py:88](../../core/config.py#L88) — `_guard_prod_secret`(prod에서 기본 시크릿 기동 차단)

**흔한 함정**
DB 비밀번호의 특수문자 `@` 는 URL에서 `%40` 으로 인코딩해야 합니다(배포 문서 트러블슈팅 표). 또 스키마를 바꾼 배포는 **`alembic upgrade head` 를 먼저** 돌리지 않으면 `column does not exist` 500이 납니다.

**한 줄 요약**
설정은 `config.py`(=application.yml)에 모으고, prod는 `JWT_SECRET` 미교체 시 기동을 막는 안전장치가 있어요.

---

## ✍️ 스스로 점검

1. `AlarmUpdate` 로 요일을 "월·수·금" → "화·목" 으로 바꿀 때, 서비스 코드에 `DELETE` 문이 없는데도 옛 요일 row가 사라지는 이유는 무엇인가요? (힌트: `cascade` 옵션과 JPA 대응 개념)
2. 알람 목록을 컬렉션(`schedules`)은 `selectinload`, 캐릭터(`character`)는 `joinedload` 로 로딩하는 이유를 각각 설명해 보세요. 컬렉션에 `joinedload` 를 쓰면 어떤 문제가 생기나요?
3. `scripts/smoke_common.py` 를 지금 실행하면 왜 즉시 실패하나요? 무슨 리팩터링의 잔재이며, 어떻게 고쳐야 할까요?

---

⟵ [이전: 9장 ..](09-external-and-storage.md) ・ [📚 목차](README.md) ・ [다음: 11장 종합](11-putting-it-together.md) ⟶
