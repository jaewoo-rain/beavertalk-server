# 발음평가·국적분류 자체 NPU 서버 전환 (2026-09-28)

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

## 2. 국적분류

- 코드 변경 없이 주소만 바꾸면 된다. 기존 클라이언트가 같은 `POST /predict`·`top5` 형태를 읽는다
- 새 설정 `NATIONALITY_API_CONSENT`(기본 `"0"`)를 추가했다
  - NPU 는 `consent=0` 이 없으면 **녹음을 저장한다**. 통화 녹음이라 기본은 저장하지 않는다
  - `None` 이면 필드를 보내지 않는다(옛 GPU 서버 호환)

## 3. 검증

- 단위: `tests/test_pron_npu.py` 6건 · `tests/test_nationality.py` consent 1건 추가
- 전체 회귀: 2144 passed · 1 skipped (consent 추가 전 실행) → 추가 후 관련 54건 통과
- 실호출(이 PC → NPU, gTTS 한국어 「독립문 앞에서 사진을 찍었어요」)
  - 발음: `is_stub` false · 종합 96 · 글자 13개 · 음소 28개 · 0.73초
  - 국적: top1 Korea 0.921 · 서버 처리 238 ms
- NPU 주소는 공인 IP 로 풀린다(Tailscale Funnel). Cloud Run 에서 닿는지는 배포 후 확인한다

## 4. 배포에 필요한 것 (미실행)

- Cloud Run `beavertalk-app-api` 에 시크릿 `PRON_NPU_TOKEN` 추가
- `NATIONALITY_API_URL` 을 `https://npu.tail428c00.ts.net` 으로 변경
- 이 브랜치를 배포 대상 브랜치에 병합 후 배포

---

- 출처 표기 의무: 두 모델 모두 AI 허브 데이터로 학습했다. 앱·웹 안내 화면에 지정 문구를 넣어야 한다(API 안내 §4). 이 브랜치 범위 밖이다
- 발음평가 NPU 는 현재 모든 요청 녹음을 저장한다(API 안내 §1). 끄는 옵션이 없다
