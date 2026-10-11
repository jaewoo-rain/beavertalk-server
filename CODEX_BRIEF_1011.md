# 과제: Gemini Live 2.5 native-audio 에서 "긴 지시문 + function tool" 이 1011 로 죽는다

## 목표
`gemini-2.5-flash-native-audio-preview-09-2025` (AI Studio, api_key) 에서
**긴 시스템 지시문(3,695자 한국어) + function calling + 실시간 오디오**를
**안정적으로(성공률 95%+)** 동시에 돌리는 방법을 찾아라.

3.1 로 갈아타는 답은 이미 검토했다. **2.5 에서 되는 길**을 찾는 게 과제다.

## 재현 환경
- SDK: google-genai 2.10.0 (`C:\Users\jaewoo\miniconda3\envs\beavertalk-server\Lib\site-packages\google\genai`)
- 파이썬 실행: `PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python <파일>`
- 키: `core.config.settings.GEMINI_API_KEY` (`.env` 에 있음. AQ. 형식 Auth key)
- 지시문 생성: `core.persona_prompt.build_system_instruction(role=..., personality=..., level_profile=..., locale="en", interests=[...], name="jaewoo", lang_band="beginner", face_tool=True)` → 3,695자
- 툴 선언: `core.gemini_live.SET_FACE_TOOL`
- 테스트 음성: `C:\Users\jaewoo\AppData\Local\Temp\claude\C--Users-jaewoo-Desktop-beavertalkweb-beavertalk-server\f9ca9d1b-1b83-4d40-82ac-c4c7624a0c89\scratchpad\u1.pcm`
  (PCM16/16kHz/mono, 13초, 실제 통화 녹음)
- 기존 하네스들이 같은 scratchpad 에 있다: `axes.py` `modelsweep.py` `decl25.py` `final25.py` `diag.py` 등. 재사용해라.

## 실패 모양
`client.aio.live.connect()` 는 성공한다(setup_complete 옴).
**`receive()` 의 0번째 메시지에서** `APIError: 1011 None. Internal error occurred.`

## 이미 실측한 것 (전부 각 5~20회. 되풀이하지 마라)

### 죽는 조합
| 조합 | 결과 |
|---|---|
| 긴 지시문 + SET_FACE_TOOL | 0/18 (세 번 나눠 측정, 전부 1011) |
| + `output_audio_transcription` 끔 | 0/6 |
| + `thinking_config(thinking_budget=0)` | 0/6 ⇒ thinking 은 원인 아님 |
| 지시문 1,000자로 줄이고 SET_FACE_TOOL | 0/6 ⇒ 지시문 다이어트로 안 됨 |
| `history_config(initial_history_in_client_content=True)` | 0/6 — **1007** (AI Studio 가 historyConfig 를 모른다) |
| 툴 선언에 `parameters` 가 있으면 (문자열/enum/한글/영문 무관) | 0/5 전부 |
| 툴 설명이 **한국어**면 (parameters 없어도) | 0/5 |
| 모델 `-latest`, `-preview-12-2025` 로 바꿔도 | 0/4 |

### 사는 조합
| 조합 | 결과 |
|---|---|
| 긴 지시문 + 툴 **없음** | 6/6 |
| 짧은 지시문(81자) + SET_FACE_TOOL | 20/20 |
| 긴 지시문 + `FunctionDeclaration(name="set_face", description="Set the avatar face.")` (인자 없음) | 5/5 — 단 **모델이 호출을 안 한다** |
| 긴 지시문 + 영문설명 무인자 5개(`face_neutral`/`face_happy`/`face_surprised`/`face_sad`/`face_angry`, 각 `"Show a X face."`) | ⚠ **불안정** — 1차 9/10, 2차 5/12. 뭉텅이로 성공/실패 |

### ⭐ 지금 가장 유력한 후보가 그 마지막 줄이다
표정 호출도 되고 발화도 된다. **문제는 1011 발생률이 40~60% 로 출렁인다는 것.**

## 찾아야 할 것 (우선순위)
1. **영문 무인자 5툴 조합의 1011 을 없애거나, 재시도로 실효 100%를 만드는 법.**
   - 1011 이 언제 나는지 규칙이 있나? (시간대? 연속 호출? 세션 간 간격? 키 rate?)
   - 재연결 재시도로 덮으면 몇 회 만에 붙나? 붙은 세션은 끝까지 안정적인가?
2. 아직 아무도 안 밟은 축:
   - `media_resolution`, `seed`, `max_output_tokens`, `top_p`/`top_k` 명시
   - `realtime_input_config` (auto VAD 파라미터) 명시
   - `generation_config` 를 통째로 넘기는 경로 vs 개별 필드
   - `http_options(api_version="v1alpha")`
   - 툴 설명 길이/언어 조합의 정확한 경계 (영문 몇 자까지 되나)
   - 툴 개수 (1개 vs 5개 vs 10개)
   - `setup` 페이로드 총 바이트 임계가 있나 — 지시문+툴선언 합산으로 재라
3. 인터넷/GitHub 이슈/공식문서에서 **09-2025 모델 + function calling + 1011** 사례

## 규칙
- ⛔ **Vertex 는 쓰지 마라.** 사장님이 AI Studio 로 못박았다.
- ⛔ `core/`, `domains/` 코드를 **고치지 마라.** 실험은 scratchpad 에서만.
- ⛔ 커밋·배포 금지.
- 결과는 `C:\Users\jaewoo\Desktop\beavertalkweb\beavertalk-server\CODEX_RESULT_1011.md` 에 써라.
- 측정값은 반드시 **횟수와 함께**(예: 8/10). 1~2회로 결론 내지 마라.
