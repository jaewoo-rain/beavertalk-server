# 문장별 TTS: Cloud TTS(Chirp) → Vertex Gemini-TTS 교체 플랜

작성 2026-06-26. gemini-expert(모델·포맷) + Explore(배선·리플) 분석 기반.

## 한 줄 요약
`core/tts.py`가 쓰는 **Cloud Text-to-Speech(Chirp 3 HD)는 프로젝트 권한이 없어 못 켠다** → 항상 None → 문장 음성 미생성. 대신 **이미 동작하는 Vertex genai 클라이언트(`app.state.genai_client`)로 `gemini-2.5-flash-tts`를 호출**하도록 `core/tts.py`만 교체한다. **새 엔드포인트·스키마·마이그레이션 없음.** 저장(storage→voice_url)·조회(GET 응답의 voice_url) 경로는 이미 완비.

## 사실 확인 (gemini-expert + 웹검증)
- Vertex 모델 ID: **`gemini-2.5-flash-tts`** (GA). ⚠️ AI Studio용 `gemini-2.5-flash-preview-tts` 아님.
- 권한: `roles/aiplatform.user`만 필요 → 현재 SA(`vertex-ai-user@tta-lingko-rookie`) 보유. **Cloud TTS API 활성화 불필요.**
- 리전: **us-central1 지원** → 통화용 클라이언트(`app.state.genai_client`, location=us-central1) 그대로 재사용.
- 한국어: ko-KR GA. 출력: **헤더 없는 PCM s16le / 24kHz / mono**. `resp.candidates[0].content.parts[0].inline_data.data`.

## 배선 확인 (Explore)
- **호출부 1곳뿐**: [normalcall_service.py:327](../domains/learning/service/normalcall_service.py#L327) — `audio = tts.synthesize_korean(korean)` (320~339 루프).
- **완전 async 컨텍스트**: `analyze_call`(async) ← `asyncio.create_task`(call_session `_trigger_analysis`). 통화 종료 후 백그라운드 분석.
- **genai client 이미 전달됨**: `run_call → _trigger_analysis → analyze_call(call_id, client, ...)`. `analyze_call`이 `client: genai.Client`를 이미 받음(분석에 사용). TTS 단계에서 같은 `client` 접근 가능.
- **storage.upload(bucket, path, data, content_type)**: 현재 `SUPABASE_BUCKET_SAMPLES`(public), `tts/{call_id}/{sentence_id}.mp3`, `audio/mpeg`. 시그니처 불변.
- **import 2곳뿐**: `normalcall_service.py`(from core import tts), `tests/test_normalcall_ws.py`(모킹).
- `core/audio.py`에 `wav_to_mp3()` 존재(복습 오디오에서 이미 prod 사용 중) → PCM→WAV→MP3 변환에 재사용 가능.

## 변경 대상 (5개 파일)
| 파일 | 변경 |
|---|---|
| `core/tts.py` | Cloud TTS 경로 폐기 → Vertex genai `gemini-2.5-flash-tts`. 시그니처 `async def synthesize_korean(text, client) -> bytes|None`. PCM 추출 → WAV 래핑 → (기본)wav_to_mp3 로 MP3. graceful None 규율 유지. |
| `domains/learning/service/normalcall_service.py` | L327: `audio = await tts.synthesize_korean(korean, client)` (await + client 인자). 상위 `analyze_call`은 이미 `client` 보유 → 추가 전달만. |
| `core/config.py` | `TTS_MODEL: str = "gemini-2.5-flash-tts"` 추가(JUDGE_MODEL 부근). |
| `requirements.txt` | `google-cloud-texttospeech` 제거(다른 import 없음). `google-genai` 유지. |
| `tests/test_normalcall_ws.py` | TTS 모킹을 async 함수로 변경(`async def _fake(text, client): return None`). |

## 호출 방식 (async, 스레드풀 불필요)
genai 클라이언트는 `.aio`(비동기) 지원 → 이벤트 루프 블록 없이:
```python
async def synthesize_korean(text: str, client) -> bytes | None:
    if not text or not text.strip() or client is None:
        return None
    try:
        resp = await client.aio.models.generate_content(
            model=settings.TTS_MODEL,                      # gemini-2.5-flash-tts
            contents=text.strip(),
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    language_code="ko-KR",
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon"),
                    ),
                ),
            ),
        )
        pcm = resp.candidates[0].content.parts[0].inline_data.data  # PCM s16le 24k mono
        if not pcm:
            return None
        wav = _pcm_to_wav(pcm, rate=24000)                 # stdlib wave
        return audio_mod.wav_to_mp3(wav) or wav            # mp3 변환 실패 시 wav 폴백
    except Exception as exc:
        logger.warning("tts: 합성 실패(무시, None) — %s", exc)
        return None
```

## 출력 포맷 결정 (확인 필요)
- **권장(기본): MP3** — PCM→WAV(`wave`)→`audio.wav_to_mp3`. 저장 경로 `tts/{...}.mp3`·`audio/mpeg` **그대로 유지**(리플 0), 복습 오디오와 일관. 단 `wav_to_mp3`(ffmpeg/pydub) 동작 전제 — 이미 prod 복습에서 사용 중이라 검증됨.
- **대안: WAV** — stdlib만 사용(의존성 0). 경로를 `tts/{...}.wav`·`audio/wav`로 변경. ffmpeg 불확실할 때 안전.
- → 기본 MP3, `wav_to_mp3` 실패 시 WAV bytes 폴백(위 코드처럼 `or wav`)로 두면 둘 다 커버. (단 폴백 시 확장자/content_type은 호출부에서 분기 필요 → 단순하게 가려면 둘 중 하나로 고정)

## 영향 없음 확인
- 조회 API(`/calls/{id}/result`, `/calls/{id}`, `/members/me/bookmarks` 등): `voice_url`만 채워질 뿐 스키마/엔드포인트 불변.
- 통화·분석·복습 채점: 무관(같은 genai client 재사용, 별 영향 없음).
- DB: `sentence.voice_url` 기존 컬럼 사용 → 마이그레이션 없음.

## 검증
- `pytest tests/test_normalcall_ws.py` (모킹 async 수정 후) 통과.
- 로컬/배포 스모크: 실제 통화 → 분석 완료 → `GET /calls/{id}/result`의 sentence `voice_url` 채워짐 → 앱에서 재생.
- 배포 시 `core/tts.py`만 바뀌므로 기존 통화/분석 회귀 위험 낮음.

## 미해결/주의
- 동일 문장 중복 합성 방지(텍스트 해시 캐시)는 후속 최적화(필수 아님).
- 문장 N개 순차 합성(현재 for 루프) — 통화당 수~수십개면 OK. 과도하면 세마포어 3~5 동시화는 후속.
- voice 선택(`Charon` 등 prebuilt) 후속 조정 가능.
