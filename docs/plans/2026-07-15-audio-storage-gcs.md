# 오디오 저장 Supabase Storage → GCS 이전

- 작성일: 2026-07-15
- 상태: 구현 완료(로컬 테스트 통과) / 배포 대기
- 관련 파일: `core/storage.py`(재작성), `core/supabase_client.py`(신규), `core/supabase_auth.py`,
  `core/config.py`, `requirements.txt`, `tests/test_storage_gcs.py`(신규)

## 목표 & 범위
통화 원본 음성·표현 TTS·연습 녹음의 **저장소를 Supabase Storage → GCS**로 옮긴다.
- 버킷: `beavertalk-app-audio` (프로젝트 `bt-dev-web-01`, 리전 `asia-northeast3`, **비공개**).
- 인증(로그인 토큰 검증)은 **그대로 Supabase(GoTrue)** — 오디오만 GCS.
- 비범위: 기존 Supabase 에 있던 오디오 마이그레이션(앞으로 저장분만 GCS). DB 스키마 변경 없음.

## 아키텍처 & 데이터 흐름
- 저장 어댑터는 여전히 `core/storage.py` **단일 진입점**, 공개 API 시그니처 동일
  (`upload(bucket, path, data, content_type)` / `public_url(bucket, path)` / `signed_url(bucket, path, expires_in)`).
  → 호출부(normalcall/sentence/review service) **무수정**, 테스트 스텁(monkeypatch) 그대로 유효.
- 과거 2버킷(voice-samples/voice-recordings)은 **단일 버킷 내 폴더 prefix**로 강등.
  최종 object key = `"{bucket}/{path}"` (예: `voice-recordings/calls/4/279/0001_user.mp3`).
- DB(`voice_url`)에는 **object key(path)만** 저장(기존과 동일) → 재생 URL 은 **V4 signed URL**로
  매 요청 재발급. 공개/비공개 구분 없이 전부 서명(단일 비공개 버킷).
  - `public_url`(캐릭터·TTS): 장기 서명(기본 7일, `GCS_SIGNED_URL_PUBLIC_TTL`).
  - `signed_url`(통화 원본·연습 녹음): 단기 서명(기본 1시간).
- **자격증명**: ADC(`google.auth.default`, cloud-platform 스코프). Cloud Run 런타임 SA 는
  private key 가 없어 **IAM signBlob** 로 서명 → `service_account_email` + `access_token`
  (compute 토큰 ~1h 만료 시 refresh) 을 `generate_signed_url` 에 전달.

## ⚠️ 실제 앱 신원 = vertex-ai-user (compute SA 아님)
Cloud Run 서비스엔 `GOOGLE_APPLICATION_CREDENTIALS=/secrets/gcp_key.json` 이 설정돼 있어
`google.auth.default()` 가 **런타임 compute SA 가 아니라 그 키파일의 SA 로 인증**한다:
- 신원: `vertex-ai-user@tta-lingko-rookie.iam.gserviceaccount.com` (**private key 보유**).
- 따라서 (1) 버킷 권한은 이 SA 에 줘야 하고, (2) private key 로 **로컬 서명** → signBlob/tokenCreator 불필요.
- `storage._signed` 는 **로컬 키 서명 1순위 → 실패 시 signBlob 폴백**(compute SA 등 무키 환경 대비) 2단 전략.

## 인프라 (완료)
- 버킷 생성: `gcloud storage buckets create gs://beavertalk-app-audio --location asia-northeast3
  --uniform-bucket-level-access --public-access-prevention` ✅
- 버킷 IAM `roles/storage.objectAdmin`:
  - `vertex-ai-user@tta-lingko-rookie.iam.gserviceaccount.com` (실제 앱 신원) ✅
  - `333511894671-compute@developer.gserviceaccount.com` (무해한 폴백) ✅
- IAM Credentials API 활성(signBlob 폴백 대비) ✅. compute SA tokenCreator 는 불필요 판명 → 회수.

## 신규 서비스 배포 (beavertalk-app-api)
- demo-api 설정 그대로 복제 + 신 이미지(GCS 코드). 리전 asia-northeast3, ENV=test(추후 prod).
- JWT: `beavertalk-app-jwt-secret` 는 실제 64자 랜덤값(dev 기본값 아님) → 추후 ENV=prod 전환 시 부팅 OK.

## 구현 내역
- **`core/supabase_client.py`(신규)**: service_role 클라이언트 팩토리 `get_client()`.
  과거 `storage._get_client()` 가 auth 에 재사용되던 커플링을 분리 — auth 안전.
- **`core/supabase_auth.py`**: `storage._get_client()` → `supabase_client.get_client()`.
- **`core/storage.py`(재작성)**: Supabase → GCS. `_init` lazy(자격증명/버킷/서명 이메일),
  `_blob_name`(prefix 합성), `upload`(upload_from_string), `_signed`(V4·signBlob),
  `public_url`/`signed_url` 위임. 미설정·예외 전부 graceful `None`(R5).
- **`core/config.py`**: `GCS_AUDIO_BUCKET`(기본 `beavertalk-app-audio`),
  `GCS_SIGNED_URL_PUBLIC_TTL`(604800). `SUPABASE_BUCKET_*` 상수는 이제 **prefix** 로 재해석.
- **`requirements.txt`**: `google-cloud-storage>=2.16` 추가(supabase 는 auth 용으로 유지).

## 테스트 결과
- `tests/test_storage_gcs.py`(신규 12케이스): `_blob_name` 합성, upload 계약·graceful,
  V4 서명 인자, signBlob 자격증명 전달, 예외 흡수 — 통과.
- 회귀: `test_sentence_tts`(storage 스텁 소비), 전체 스위트 — (실행 로그 참조).
- 로컬 실업로드 스모크: 로컬은 ADC 부재 → graceful `None`(정상). gcloud 로 버킷 쓰기/삭제 확인 ✅.
  **실 업로드+서명 end-to-end 검증은 Cloud Run(SA 자격증명) 배포 후.**

## 미해결 / 후속
- [ ] demo-api 배포 후 실통화 1건으로 GCS 저장 + signed URL 재생 end-to-end 확인
      (`scripts/dev_inspect_call.py`로 voice_url key 확인 → GCS 객체 존재/서명 재생).
- [ ] (선택) Flutter 재생 측 signed URL 만료(1h) 대응 — 필요 시 재발급 엔드포인트.
- [ ] (선택) 기존 Supabase 오디오 마이그레이션 여부 결정(현재 미이전).

## 리스크 & 결정 사항
- **결정: 단일 비공개 버킷 + 전부 signed URL**(공개/비공개 혼합 대신). 단순·유출위험↓,
  기존 "key 저장·URL 매번 조립" 설계와 정합.
- **리스크: Cloud Run signBlob** 미설정 시 서명 실패 → graceful `None`(업로드는 되나 재생 URL null).
  → tokenCreator 부여로 해소(완료). 배포 후 실검증 필수.
- **graceful degradation 유지(R5)**: 자격증명/버킷 부재 시 앱은 정상, `voice_url=None`.
- 인증은 Supabase 유지 — 이번 변경으로 로그인/토큰 검증 경로 불변.
