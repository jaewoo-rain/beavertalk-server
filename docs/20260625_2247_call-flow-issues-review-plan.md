# 통화→분석→복습→점수 플로우: 문제 검토 + 수정 플랜

작성 2026-06-25 22:47. 빠른 땜질을 멈추고 **전수 검토 후 정리**한 문서. (Flutter 전수 감사 + 서버 채점 설정 검토 기반. 감사의 과잉 플래그는 걸러냄.)

## 0. 현재 정상 동작 (확인됨)
- Supabase 인증(로그인/가입) — 배포 서버 정상.
- 통화 WS + Vertex genai — **call_demo.html(웹)에서 문제없이 동작**(사용자 확인) → 서버/오디오 파이프라인 자체는 정상.
- 백엔드 복습 오디오 엔드포인트(`POST /sentences/{id}/reviews/audio`), 발화 로그(`👤/🦫`) 추가됨.
- Cloud Run: timeout 1800s, Vertex env/키 마운트 적용.

## 1. 확정된 문제 (근거 명확)
| # | 문제 | 위치 | 근거 | 영향 |
|---|---|---|---|---|
| C1 | **SpeechSuper 키 미설정 → 복습 점수가 전부 스텁(가짜)** | 서버 env | 배포 env에 `SPEECH_SUPER_APP_KEY/SECRET_KEY` 없음. `speechsuper.py` 폴백=결정적 스텁 | "점수받기"가 진짜 발음평가가 아님 (핵심 기능 무의미) |
| C2 | **Cloud TTS API 비활성 → 문장 voice_url=None** | tta-lingko-rookie | 로그 403 `texttospeech ... disabled` | 복습 "Native(표준발음)" 오디오 없음 + 로그 도배 |
| C3 | **provider 빌드 중 수정 크래시**(분석화면) | `analysis.dart` didChangeDependencies | 스크린샷 빨간 에러 | 분석화면 진입 시 크래시 → **이미 수정**(post-frame 지연), 기기 검증 필요 |
| C4 | **목소리 2배속**(안드) | `normalcall_controller` 재생 | AudioTrack 네이티브 48k에 24k 공급 | → **이미 수정**(24k→48k 업샘플), 기기 검증 필요 |
| C5 | **test/prod 같은 DB 공유** | Cloud Run env | 둘 다 `beavertalk-app-db-pool`. prod는 구코드+신스키마라 깨짐 | 데이터 비격리 + prod 지뢰 |
| C6 | **기기 minSdk 29 미만 설치 불가** | Android | S8=API28 설치거부, flutter_sound 스트림재생=API29+ | Android 10+ 기기/에뮬 필요 |

## 2. 검증 필요 (Flutter 감사가 플래그 — 확정 아님, 확인 후 대응)
우선순위 순. 대부분 빠른 변경의 통합 리스크.
- **A1 (오디오 레이트 견고성)**: 재생 레이트 48k **하드코딩**. 기기 네이티브가 44.1k면 ~9% 빠름. → 우선 현재 기기에서 C4 수정이 실제로 정상인지 확인. 비48k 기기 대응은 네이티브 레이트 쿼리(플랫폼 채널)로 후속.
- **A2 (녹음 중 화면 pop 레이스)** `learning_intro.dart`: 녹음 중 뒤로가기 → recorder dispose/제출 async가 사라진 화면에서 진행 → 점수 엉뚱한 문장에 기록/누수 가능. → pop 시 stop+submit 취소 가드.
- **A3 (방어적 파싱)** `call_result_dto.dart` `json['call_id'] as int` / `call_finish.dart` 파싱: null/형식오류 시 조용히 홈 이동 or 크래시. → 사용자 보이는 에러 + 안전 파싱.
- **A4 (재생 큐 엣지)** `_pendingChunks` 감소/`_forceFinishClosing` 타이머/`_maybeFinishClosing`: 가드 존재하나 빈 청크·드롭 시 종료 hang 여지. → 코드 확인 후 빈 청크는 카운터 영향 없게, force-timer는 `_finishClosing`에서 취소.
- **A5 (재진입 staleness)** `analysis.dart` `_scoresReset`: 같은 통화 재진입 시 게이지 상태 유지 — 의도와 일치하나 확인.

> 감사가 "CRITICAL"로 표기한 다수(carry 원자성, poll 더블내비 등)는 기존 가드(`_navigated`, `_drainScheduled`)로 대부분 방어됨 → **확인 후 실제 재현되는 것만** 수정. 추측성 리팩터 금지.

## 3. 수정 플랜 (우선순위 + 순서)

### P0 — 핵심 기능 정확성
1. **C3·C4 기기 검증 먼저.** 핫리스타트한 Flutter로 Android 10+ 기기에서: 목소리 속도 정상? 분석화면 크래시 없음? → 결과에 따라 A1 추가 대응 여부 결정. *(코드 수정 전에 현 상태 확인)*
2. **C1 실제 채점 활성화**: SpeechSuper 키를 Secret으로 추가 + 재배포 → 복습 점수가 진짜가 됨. (키 보유 시)
3. (선택) **C2 TTS 활성화**: `gcloud services enable texttospeech.googleapis.com --project=tta-lingko-rookie` → 문장 발음 오디오 + 로그 정리.

### P1 — 실제 버그(검증 후 수정)
4. A2 녹음 중 pop 가드, A3 방어적 파싱(call_id), A4 재생 큐 엣지. (재현/확정된 것만)

### P2 — 정리/하드닝
5. A1 비48k 기기 대응(네이티브 레이트 쿼리) — 필요 기기 나오면.
6. A5 등 폴리시.

### 인프라/운영
7. **C5 prod 정리**: (a) prod도 동일 코드+env로 맞추거나 (b) prod 트래픽 0/삭제 — 혼동 방지.

## 4. 검증 체크리스트(기기, Android 10+)
- [ ] 통화: 연결·내 목소리 송신·상대 목소리 **정상 속도**·끊김 없음
- [ ] 통화 종료 → 평점 3개 → 로딩(폴링) → 분석화면 **크래시 없음**
- [ ] 분석 게이지: 처음 0/비활성 → 복습 후 갱신
- [ ] 복습: 녹음 → 채점 → 글자별 상/중/하 + 점수(**실점수=SpeechSuper 켠 뒤**)
- [ ] 서버 로그 `👤 user`/`🦫 beaver` 확인(인식/응답)

## 5. 결정 필요 (사용자)
- SpeechSuper 키 있나요? 있으면 바로 넣고 재배포(P0-2).
- TTS API 지금 켤까요?(P0-3)
- prod는 어떻게(맞추기/내리기/방치)?
- 위 검증 체크리스트부터 한 번 돌려서 **실제로 뭐가 깨지는지** 확정한 뒤 P1 손댈까요?
