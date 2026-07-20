# 실행 계획: 통화후 발음·피드백 확장 + 자동 국적 분석 (5기능)

- 작성일: 2026-07-16
- 상태: **구현 완료** (전체 pytest 245 passed · 시니어 리뷰 Blocker 0 · dev DB 마이그레이션 적용 · **미커밋**)
- 브랜치: `feat/leveltest-fast-probe`

## 구현 결과 (2026-07-16)
- **적용 마이그레이션**: Rev1 `32cc30f95a44`(call.feedback/pron_feedback/pron_feedback_n + review.counted) → Rev2 `f2cd7402bede`(nationality_prediction) → head 도달(dev DB).
- **신규 파일**: `core/nationality.py`, `domains/account/models/nationality_prediction.py`, `domains/account/{repository,service}/nationality_*.py`, `domains/learning/{schemas/pronunciation.py, repository/pronunciation_repository.py, service/pronunciation_service.py}`, 테스트 4종.
- **CEO 교정 1건**: SpeakCountry 평균 분모를 **고정5 → 보유 이력 개수(len, ≤5)**로 변경(통화 1건이면 top1 확률 그대로, 초기 % 희석 방지; 랭킹·top1 불변). percent [0,100] 클램프 추가(방어).
- **검증**: 전체 245 passed(normalcall 회귀 40 포함, R4 불변식 무변경 확인). 시니어 리뷰 Blocker 없음, 권고 3건은 "단일 writer 전제·계획 결정과 일치"로 수용.
- **미반영(권고·후속)**: 발음 comment 캐시가 country 미도착 시 첫 코멘트를 국가 없이 굳힐 수 있음(무효화키=counted수) — 실사용 레이스 극히 낮아 수용. 국적 서버는 `NATIONALITY_API_URL` 미설정 시 비활성 → 배포 시 env 주입 필요.
- 수립: CEO 오케스트레이션 + 전문 에이전트 4인(fastapi-architect / db-architect / api-integration-expert / prompt-persona-engineer) 종합

## CEO 교정(에이전트 상충 → 사장님 명세 기준 확정)
- 국적 추론 = **외부 국적 API**(Tailscale 서버, 실측 완료). ❌ Gemini 멀티모달 아님.
- 국적 갱신 = **매 통화 + 최근 5개 평균**. ❌ "speak_country null일 때 1회만" 아님.
- 최저 자모 = SpeechSuper 원응답 **`phonemes[].alpha` 직접 저장**. ❌ char 점수 NFD 분해 아님.

---

## 1. 목표 & 범위
**목표**: 통화 결과에 ①격려 한마디 ②발음 상세(문장별+소리별+국가맞춤 코칭) ③최근5 발음추이를 더하고, ④발음 재녹음을 "카운트/연습" 2모드로 나누며, ⑤매 통화 음성으로 국적을 자동 추론해 프로필(speak_country)을 채운다.

- **MVP 범위**: 요구1~5 백엔드 전부(엔드포인트·스키마·파이프라인·외부연동·LLM 2종).
- **비범위**: 프론트 구현, "N문장 중 M개 통과"·"변화(+N)" 계산(프론트), 발음 소리 의미라벨링(받침/혼동쌍 — 프론트), 일본어 데모, 발음평가 로직 자체 변경.

## 2. 아키텍처 & 데이터 흐름
```
[통화 종료] call_session.run_call finally (순수 가산, R4 불변식 무영향)
  ├ _trigger_analysis   → analyze_call: 요약+표현+★feedback(요구1) 한 콜 → call.feedback 저장
  ├ _trigger_audio_upload(기존)
  └ ★_trigger_nationality(신규): user 턴 in-memory PCM concat→WAV → (10s↑ 게이트)
       → core.nationality.predict_nationality(외부 API) → predictions
       → nationality_service: 예측 이력 5-FIFO 적재 + SpeakCountry 5평균 재계산 (요구5)

[복습 채점] POST /sentences/{id}/reviews[/audio] ?apply_score=T/F  (요구2)
  → speechsuper.assess (★phonemes[] 저장 추가) → Review(counted=apply_score, feedback+phonemes)
  → apply_score=T 만 Evaluation(공식점수) 갱신

[조회] GET /calls/{id}/pronunciation (요구3) → 문장별 점수 + 소리별 alpha 집계
        + PronunciationTip LLM(국가=speak_country.first_country + 최저 alpha, 모국어, 캐시)
      GET /calls/pronunciation-history (요구4) → 최근5 [날짜,문장수,total_score평균]
      GET /calls/{id}/result → +call.feedback 노출 (요구1)
```
**신규 파일**: `core/nationality.py`, `domains/account/models/nationality_prediction.py`, `domains/account/service/nationality_service.py`, `domains/learning/{schemas/pronunciation.py, repository/pronunciation_repository.py, service/pronunciation_service.py}`.

## 3. 작업 분해 (담당 · 산출물 · 의존)

### [T1] 스키마+Alembic — db-architect · *선행(대부분 블로킹)*
- 모델: `call.feedback`(Text) / `call.pron_feedback`(Text)+`call.pron_feedback_n`(Int, LLM캐시 무효화키) / `review.counted`(Bool NOT NULL server_default true) / 신규 `nationality_prediction`(member_id CASCADE, call_id SET NULL, predictions JSON, top1, created_at; UNIQUE(call_id); idx(member_id,created_at)).
- Alembic 2분할(Rev1 컬럼4 / Rev2 테이블). autogenerate 검토(server_default·ondelete·JSON·명명). 모델+마이그레이션 같은 커밋(R2). registry import 1줄.

### [T2] SpeechSuper phoneme 추출 — fastapi-expert · *T1과 병렬*
- `core/speechsuper._map_result`에 `phonemes: [{phoneme,alpha,pronunciation}]`를 `result.words[].phonemes[]`에서 뽑아 반환 dict에 추가 → `review.feedback`에 저장. graceful(없으면 `[]`). 기존 evaluation/char_scores 불변. **신규 복습부터 축적.**

### [T3] 국적 어댑터+config — api-integration-expert · *T1과 병렬*
- `core/nationality.py`: `predict_nationality(audio_bytes, audio_type="wav")→dict|None`(httpx multipart `file`, `?top_k=3`, 예외 미전파, no_speech/불통/미설정=None, 재시도 1회). 실측 계약: `{predictions:[{country,iso,prob}], top1, ...}` / no_speech면 `{reason:"no_speech_detected"}`.
- config: `NATIONALITY_API_URL(None)`, `NATIONALITY_API_TOP_K=3`, `NATIONALITY_API_TIMEOUT_S=20`, `NATIONALITY_MIN_SPEECH_S=10`.

### [T4] 국적 저장·재계산 서비스 — db-architect · *T1·T3 이후*
- `nationality_service.record_and_recompute(db, member_id, call_id, predictions)`: 이력 insert(멱등 UNIQUE call_id) → 5-FIFO 정리 → 최근5 나라별 prob 평균(없는 회차 0)→top3→SpeakCountry upsert(country **이름** 저장) + member.speak_country_id 링크 → 단일 commit(R3).

### [T5] 국적 파이프라인 훅 — fastapi-expert · *T3·T4 이후*
- `call_session._trigger_nationality`(finally, `_trigger_audio_upload` 옆): user PCM concat→`pcm16_to_wav`→10s 게이트→`run_in_threadpool(predict_nationality)`→`run_db(record_and_recompute)`. GC강참조·예외흡수(R5). 매 통화(레벨테스트 포함).

### [T6] 요구1 피드백 한마디 — prompt-persona-engineer · *T1 이후*
- `_CallAnalysisBase`에 `feedback: str=Field(default="")` + `_analysis_instruction`에 지시(모국어 1문장, 격려, 숫자·레벨 금지=D2, 일반통화만). `_save_analysis`가 `call.feedback` 기록(기존 단일커밋 편승). `CallResult.feedback` 노출(/result만).

### [T7] 요구2 apply_score — fastapi-expert · *T1 이후*
- `ReviewCreate.apply_score: bool=True`, 오디오 엔드포인트 `Form(True)`. `review_service`: `counted=apply_score`, True만 `_apply_evaluation`. False는 Review+음성만 저장.

### [T8] 요구3 발음 상세(집계+LLM) — fastapi-expert + prompt-persona-engineer · *T1·T2·T7 이후*
- `pronunciation_repository`(순수SELECT): 소유통화, 활성문장+eval, 문장별 마지막 counted 복습(DISTINCT ON), first_country.
- `pronunciation_service`: `aggregate_sounds`(alpha별 attempts/passes(≥80)/pron평균, **counted만**), `build_sentence_scores`(모든 문장, 미복습 null), async comment(캐시B: `pron_feedback_n==현재 counted수`면 재사용).
- `PronunciationTip` LLM(JUDGE_MODEL, 국가 유/무·자모없음 3분기, 모국어). 라우터 `reanalyze` 동형(app.state+run_db).

### [T9] 요구4 최근5 이력 — fastapi-expert · *T1 이후*
- `GET /calls/pronunciation-history`(sync): normal·done 최근5 `[call_date, 활성문장수, counted문장 total_score 평균]`.

### [T10] 테스트·검증 — test-engineer · *전체 이후*
- 회귀: 통화 WS·분석 스냅샷(LevelAssessment 바이트 불변). 신규 단위: apply_score T/F, 소리집계(counted만·마지막복습), history, feedback 파싱 양경로, PronunciationTip 3분기(LLM목), 국적 어댑터 실패모드표·5평균. **전체 pytest 통과 + normalcall 회귀(R4)**.

**병렬 순서**: T1·T2·T3 동시 착수 → T4~T9 → T10.

## 4. 수용 기준 & 테스트 포인트
- **요구1**: 일반통화 분석 후 `call.feedback` 채워짐(모국어 1문장), `/result` 노출. 레벨테스트·빈통화 null. 추가 LLM콜 0.
- **요구2**: `apply_score=false` 복습 → Review·음성 저장, `Evaluation`·`/result` average 불변, 소리집계 제외. `true`는 기존대로.
- **요구3**: 문장별=모든 활성문장(미복습 null). 소리별=문장당 마지막 counted 복습 phonemes를 alpha로 묶어 attempts/passes(≥80)/평균. comment=국가+최저alpha 모국어 1문장(국가 null이면 소리만, 자모없으면 생략). 캐시 적중 시 LLM 미호출.
- **요구4**: 최근5 [날짜,문장수,점수]; 점수=counted 문장 total_score 평균(없으면 null).
- **요구5**: 매 통화 후 비동기(프론트 무감지), <10s·no_speech·불통·미설정=조용히 skip. 성공 시 이력 최대5 유지, SpeakCountry=5평균 top3. 통화·분석 무손상.
- **엣지**: 복습0 통화(소리집계 [], comment 생략), speak_country null(국가없이), 외부API 다운(통화 정상), phonemes 없는 옛 복습(집계 제외).

## 5. 리스크 & 결정 사항
**CEO 확정 결정**: 국적=외부API(❌Gemini) · 매통화 5평균(❌null1회) · alpha=phonemes 직저장(❌NFD) · 오디오=in-memory user PCM · comment 캐시=counted수 무효화(B) · call.feedback=/result만 · SpeakCountry country **이름** 저장 · sentence_count=활성문장 전체 · speak_country 쓰기는 account 도메인 서비스 소유.

**리스크**:
1. Tailscale API 도달성(사장님 "가능"·env 교체 가능 확보).
2. `_trigger_nationality`가 `state.segments`의 user PCM 메모리 잔존에 의존(현재 유지됨 — 향후 flush 최적화 시 재검토).
3. phonemes는 speechsuper 확장 후 **신규 복습부터만** 축적(옛 데이터 없음, dev 수용).
4. downgrade는 파괴적(R6, 신규 컬럼/테이블이라 prod 백필 불필요).

**미결(기본값 진행)**: member↔speak_country 1:1 UNIQUE 강제는 범위 밖(단일 writer 전제). PronunciationTip 캐시 컬럼 2개 추가 수용.

## 관련 파일(구현 대상)
- 모델: `domains/learning/models/{call,review}.py`, 신규 `domains/account/models/nationality_prediction.py`, `db/registry.py`
- 어댑터/설정: `core/nationality.py`, `core/speechsuper.py`, `core/config.py`
- 서비스: `domains/learning/service/{normalcall_service,review_service}.py`, 신규 `domains/learning/service/pronunciation_service.py`, `domains/account/service/nationality_service.py`
- 리포지토리: 신규 `domains/learning/repository/pronunciation_repository.py`
- 스키마/라우터: `domains/learning/schemas/{call,review}.py`, 신규 `schemas/pronunciation.py`, `domains/learning/routers/{call,sentence}.py`
- 파이프라인: `domains/learning/realtime/call_session.py`
- Alembic: `alembic/versions/`(Rev1·Rev2, head `c9d0e1f2a3b4` 뒤 체인)
