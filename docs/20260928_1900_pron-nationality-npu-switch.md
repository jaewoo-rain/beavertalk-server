# 발음평가 자체 NPU 서버 전환 (2026-09-28)

- 요청: 사용자 직접 지시 「SpeechSuper 발음평가 API를 자체 NPU 서버로 바꿔줘」 · 「국적분류는 바꾸면 안된다?」
- 근거 문서: `NPU_서버_API_안내.md`(2026-09-27) · `NPU_GPU_성능비교.md`(2026-09-27)
- 브랜치: `feat/pron-npu` (기준 `origin/main` `2573a5b`)

## 1. 발음평가

- 1순위를 자체 NPU `POST {PRON_NPU_URL}/sent.eval.kr` 로 바꿨다
- `PRON_NPU_TOKEN` 이 있을 때만 NPU 를 부른다. 토큰은 `Authorization: Bearer` 헤더로만 보낸다
- NPU 실패 시 순서: SpeechSuper(키가 있을 때) → 결정적 스텁. 앱이 깨지지 않는다(R5)
- 응답 매핑은 `_map_npu_result` 로 따로 둔다. SpeechSuper 와 다른 점 4가지를 흡수한다
  - 글자 점수: `words[].score` (SpeechSuper 는 `scores.overall`)
  - 음소 자모: `alpha_jamo` 를 도메인 `alpha` 로, KoG2P 기호는 `phoneme` 으로
  - 위치: 서버가 준 `position`(실제 발음 기준, 연음이면 받침이 초성)을 그대로 쓴다
  - 틀린 음소: `errorType` 이 있고 `uncertain` 이 아닌 것만 `phoneme_misses` 에 넣는다
- `readType` 3(오독)은 글자 점수가 null 로 온다. 뜻이 「20 미만」이라 0 으로 본다
- 빠뜨린 소리(`pronunciation: null`)는 음소 점수 0 이다
- 반환 계약(`evaluation`·`char_scores`·`phonemes`·`phoneme_misses`·`is_stub`)은 바뀌지 않는다

## 2. 국적분류 — 바꾸지 않음

- 사용자 결정(2026-09-28): 「국적분류는 기존 모델 연동 그대로 해줘」
- 코드·설정 모두 `origin/main` 그대로다. 한때 넣었던 `consent` 필드는 되돌렸다
- 참고: NPU 국적 서버로 옮길 때는 주소(`NATIONALITY_API_URL`)만 바꾸면 되지만, NPU 는
  `consent=0` 이 없으면 녹음을 저장한다. 옮기게 되면 그때 함께 다룬다

## 3. 검증

- 단위: `tests/test_pron_npu.py` 6건
- 전체 회귀: 2144 passed · 1 skipped
- 실호출(이 PC → NPU, gTTS 한국어 「독립문 앞에서 사진을 찍었어요」)
  - 발음: `is_stub` false · 종합 96 · 글자 13개 · 음소 28개 · 0.73초
- NPU 주소는 공인 IP 로 풀린다(Tailscale Funnel). Cloud Run 에서 닿는지는 배포 후 확인한다

## 4. 배포에 필요한 것 (미실행)

- Cloud Run `beavertalk-app-api` 에 시크릿 `PRON_NPU_TOKEN` 추가
- 이 브랜치를 배포 대상 브랜치에 병합 후 배포

---

- 출처 표기 의무: 발음평가 모델은 AI 허브 데이터로 학습했다. 앱·웹 안내 화면에 지정 문구를 넣어야 한다(API 안내 §4). 이 브랜치 범위 밖이다(사용자: 나중에)
- 발음평가 NPU 는 현재 모든 요청 녹음을 저장한다(API 안내 §1). 끄는 옵션이 없다
