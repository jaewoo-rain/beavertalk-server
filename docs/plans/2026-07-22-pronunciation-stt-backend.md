# 발음 챌린지 서버 STT 백엔드 이식 (beavertalk-server)

- **작성일**: 2026-07-22
- **상태**: 구현 완료 (테스트 그린 · 미배포 · 미커밋)
- **브랜치**: feat/cloud-tts
- **참조**: `web/beavertalkweb/server/app/{routers/pron_stt.py, services/stt/*}` (웹 홈페이지에서 검증된 서버 STT)
- **관련 파일(신규/변경)**:
  - `core/stt.py` (신규) — Google Speech 비동기 클라이언트 + 스트림 래퍼(실제/페이크)
  - `domains/learning/realtime/stt_session.py` (신규) — WS↔STT 2펌프 브리지
  - `domains/learning/realtime/ws_router.py` (변경) — `WS /api/v1/pron/stt/ws` 추가
  - `core/config.py` (변경) — STT 설정 필드
  - `requirements.txt` (변경) — `google-cloud-speech`
  - `tests/test_pron_stt.py` (신규) — 페이크 스트림 계약 테스트

## 목표 & 범위
모바일 앱 발음 챌린지가 마이크 PCM(LINEAR16)을 서버로 흘리면 **Google Cloud
Speech-to-Text 스트리밍**으로 인식해 부분/최종 전사를 돌려주는 **WS 게이트웨이**를
`beavertalk-server`에 구축한다. 웹(`web/beavertalkweb`)의 검증된 구현을 **우리 코드
컨벤션**(core 어댑터 = 도메인/DB 무지, 2펌프+TaskGroup, app.state, graceful degradation,
UPPERCASE 설정)으로 이식한다.

**비범위**: 프론트(완성본, 별도). 발음 채점(SpeechSuper)은 기존 파이프라인 유지 — 이건
"전사(STT)"만 담당.

## 계약 (프론트 ↔ 서버) — 웹과 동일하게 유지
- **경로**: `WS /api/v1/pron/stt/ws` (realtime_router 가 `/api/v1`에 물림 → 웹과 동일 경로)
- **client→server**: 첫 텍스트 `{"type":"config","words":[...],"sampleRate":N}` → 이후 바이너리
  PCM 청크 → 선택 `{"type":"stop"}`. (테스트 훅 `{"type":"__test_say","text":...}`)
- **server→client**: `{"type":"ready"}` / `{"type":"partial","text"}` / `{"type":"final","text"}` /
  `{"type":"error","error"}`

## 아키텍처 & 데이터 흐름
```
앱 마이크 PCM16/Nk ──WS bytes──▶ ws_router(/pron/stt/ws)
                                    │ (선택 ?token= verify)
                                    ▼
                         PronSttSession.run()  ── 2펌프 + TaskGroup
                          ├ pump_in : WS bytes → stream.push_audio()
                          └ pump_out: stream.results() → WS {partial|final}
                                    ▼
                         core.stt.make_stt_stream()
                          ├ 실제: GoogleSttStream(streaming_recognize)
                          └ 페이크: FakeSttStream (STT_FAKE=1, 크레덴셜/과금 0)
```
- **core/stt.py**: `get_speech_client()`(lru_cache, `STT_SA_KEY_FILE`→없으면 `TTS_SA_KEY_FILE`
  재사용 = bt-dev-web-01 SA) + `GoogleSttStream` + `FakeSttStream` + `make_stt_stream`.
  키 없거나 `STT_FAKE`면 클라 미생성(graceful). google-cloud-speech 는 사용 시점 import.
- **stt_session.py**: 웹 `PronSttSession`을 우리 realtime 로 이식(normalcall 과 동일한
  2펌프/TaskGroup/`except*` 패턴). `SttInbound`(audio/control/disconnect) + `_WsSttTransport`.
- **ws_router.py**: `@router.websocket("/pron/stt/ws")` — 토큰 있으면 verify(재사용), 없으면
  허용(발음챌린지=저민감·본인 음성·DB 무기록). accept 후 세션 위임. app.state.settings 사용.

## 작업 분해
- [x] 참조(웹 STT 4파일 + 계약테스트) 정독
- [x] `core/config.py` — STT_LANGUAGE/STT_MODEL/STT_PHRASE_BOOST/STT_FAKE/STT_SA_KEY_FILE
- [x] `core/stt.py` — 클라이언트 팩토리 + 스트림 2종 + make_stt_stream
- [x] `domains/learning/realtime/stt_session.py` — 세션 브리지 + transport
- [x] `ws_router.py` — WS 라우트 추가
- [x] `requirements.txt` — google-cloud-speech>=2.27
- [x] `tests/test_pron_stt.py` — 페이크 계약 테스트(ready→__test_say→final)
- [x] 테스트 통과: STT 계약 3 green + normalcall 회귀 40 green(R4) + WS end-to-end(TestClient) OK

## 수용 기준 & 테스트 포인트
- STT_FAKE=1 로 `config → __test_say → final 에코` 경로가 돈다(ready 이벤트 → final "머리").
- config 없이 오디오 먼저 와도 죽지 않고 기본값으로 동작.
- 키/크레덴셜 없이 서버 기동 정상(graceful) + normalcall WS 회귀 그린(R4).
- 프론트가 기존 웹 계약 그대로 붙으면 동작(경로·메시지 동일).

## 리스크 & 결정 사항
- **D1. 인증**: 웹은 Origin 체크만(토큰 없음). 우리 표준 WS(normalcall)는 `?token=` 필수.
  → **결정**: 토큰 있으면 verify, 없으면 허용(발음챌린지 저민감). 과금 남용 우려 시 한 줄로
  필수화 가능. 프론트 완성본 계약을 깨지 않기 위함. **(사장 확인 포인트)**
- **D2. GCP 프로젝트**: STT 는 bt-dev-web-01(TTS/GCS 와 동일 SA=tts_key.json) 재사용. 배포 시
  그 SA 에 `roles/speech.client` + Speech-to-Text API 활성화 필요.
- **D3. 스트림 ~5분 한도**: 발음챌린지는 단발(~60s)이라 롤오버 미구현(웹과 동일). 장문 필요 시
  스트림 재시작 로직 추가.
- **R4 준수**: 신규 WS 엔드포인트라 normalcall 2펌프/백스톱/종료규약 불변 — 회귀 테스트로 확인.
