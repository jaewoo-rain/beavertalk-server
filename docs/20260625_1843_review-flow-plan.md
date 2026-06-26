# 복습 플로우(분석→복습→점수받기) 개발 플랜

작성 2026-06-25. **이번에 개발 착수.** 목표: 분석화면에서 문장 선택 → 녹음 → 발음 채점 → 글자별 상/중/하 + 점수 표시 → **평균 게이지 프론트 재계산(실시간)**.

## 핵심 결정: 오디오 채점 경로 = 백엔드 멀티파트 (Option B)
현재 `POST /sentences/{id}/reviews` 는 JSON `{voice_url: 스토리지키}` 만 받음(클라가 선업로드 필요). 멀티파트 업로드+채점은 dev `/__dev/pron-eval` 에만 존재.

**채택: 백엔드에 프로덕션 멀티파트 채점 엔드포인트 추가.** 이유:
- 포맷 처리 일원화 — 백엔드 ffmpeg 가 클라가 보낸 무엇이든(web=webm/opus, mobile=wav/aac) MP3 변환 + 무손실 채점. SpeechSuper 포맷 리스크 제거.
- 클라 Storage RLS 정책 불필요(백엔드가 service_role 로 업로드). 대시보드 추가설정 0.
- 검증된 `/__dev/pron-eval` 로직 재사용.
- (반대안 A: 클라가 Supabase Storage 직접 업로드 → 키 전달. RLS 정책 + 포맷 호환 부담 커서 탈락.)

## 백엔드 변경 (작음)
1. **`ReviewService.add_review_from_audio(member_id, sentence_id, raw: bytes, content_type) -> ReviewFeedback`** 헬퍼:
   - `wav_to_mp3(raw)` 시도 → 성공 MP3, 실패 시 원본 그대로 저장.
   - `storage.upload(SUPABASE_BUCKET_RECORDINGS, "reviews/{member}/{sentence}/{uuid}.{ext}", payload, ctype)` → key(또는 None).
   - 채점은 **무손실 원본** 임시파일(audio_override)로: 기존 `add_review(..., ReviewCreate(voice_url=key), audio_override=tmp)` 호출.
   - tmp 파일 정리. (= 현재 `/__dev/pron-eval` 본문을 서비스로 이관)
2. **`POST /api/v1/sentences/{sentence_id}/reviews/audio`** (multipart `audio: UploadFile`) → `ReviewFeedback`. `CurrentMember` 인증. `sentence.py` 라우터에 추가.
3. `/__dev/pron-eval` 은 이 헬퍼 재사용하도록 정리(중복 제거). 기존 JSON `POST /sentences/{id}/reviews` 는 유지(키 기반 경로).

**응답 계약(ReviewFeedback) — 변경 없음:**
```
{ review_id, sentence_id, korean_sentence, native_sentence, voice_url,
  evaluation: { total_score, pronunciation, fluency, rhythm },   // 0~100
  char_scores: [ { char, score: 0~100, grade: '상'|'중'|'하' } ] }
```

## Flutter 변경
1. **모델**: `ReviewFeedback`, `CharScore`(char/score/grade), `PronScore`(total/pron/fluency/rhythm) 도메인 엔티티 + DTO(fromJson). `lib/features/review/` 신설(클린 아키텍처).
2. **데이터소스/리포지토리**: `ReviewRemoteDataSource.submitAudio(sentenceId, bytes, filename, contentType) -> ReviewFeedbackDto` — dio `FormData`(multipart) → `POST /sentences/{id}/reviews/audio`. 리포지토리 + Riverpod 프로바이더(`mapDioException`).
3. **녹음 캡처**: normalcall 패턴 재사용 — `flutter_sound` `Codec.pcm16` `toStream` 으로 단일 발화 녹음, 청크 누적(Uint8List) → 정지 시 **WAV(44B 헤더, 16k mono) 로 래핑** → 멀티파트 전송. **web/모바일 공통 동작**(normalcall 이 web 에서 pcm16 스트리밍 이미 됨). 백엔드가 WAV→MP3 변환 + 채점.
   - `learning_intro.dart`: mic 토글을 실제 녹음에 연결. 정지 → `submitAudio` → `ReviewFeedback` 수신 → 로딩 인디케이터 → `learning_next` 로 feedback 전달.
4. **결과 표시(실데이터 바인딩)**:
   - `learning_next.dart`: `char_scores` 의 **grade(상/중/하)** 로 글자 색칠(백엔드 grade 사용; score 폴백). native/me 재생 버튼.
   - `learning_main.dart`: `evaluation`(total/pron/fluency/rhythm) → 게이지.
   - `LearningArgs`/신규 `ReviewArgs` 가 `ReviewFeedback` 운반.
5. **평균 게이지 프론트 재계산(원칙: 집계는 프론트)**:
   - Riverpod `reviewScoresProvider`(StateNotifier `Map<int sentenceId, PronScore>`), 분석화면 진입 시 비움(점수 없음).
   - 복습에서 채점될 때마다 그 문장 점수 갱신 → **분석화면 게이지가 watch 해서 채점된 문장들 평균 재계산**(없으면 0/비활성). 서버 `average` 미사용.
6. **재생(best-effort, 2순위)**: "Me"=방금 녹음 로컬 바이트 재생(flutter_sound player). "Native"=문장 `voice_url` 재생(스토리지 키면 재생 URL 필요 → 우선 있으면 재생, 없으면 비활성). 핵심(녹음→채점→표시) 먼저, 재생 폴리시는 후순위.

## 단계(작업 순서)
1. 백엔드: `add_review_from_audio` 헬퍼 + `POST /sentences/{id}/reviews/audio` + dev 정리. import/스모크 검증.
2. Flutter: 모델/DTO → 데이터소스/리포지토리 → 녹음(pcm16→WAV) → intro 연결 → next/main 실데이터 → 게이지 재계산 프로바이더. `flutter analyze` 그린.

## 미해결/주의
- **web 녹음 권한**: 브라우저 mic 권한(이미 normalcall 에서 요청). https/localhost 필요.
- **재생 URL**: 문장 `voice_url`/내 녹음 키는 스토리지 키 → 재생하려면 signed/public URL 필요. 1차에선 로컬 녹음 바이트 재생만 확실히, TTS 재생은 voice_url 존재 시만.
- **SpeechSuper 포맷**: 백엔드가 무손실 WAV 로 채점하므로 클라는 pcm16/WAV 권장(웹 opus 도 백엔드 변환되지만 WAV 가 안전).
- **게이지 출처 일원화**: 분석화면은 서버 average 대신 `reviewScoresProvider` 기반 계산으로 통일(문장 진입 전엔 점수 0).
