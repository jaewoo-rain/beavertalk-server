"""캐스케이드 원가 계측 — 3구간(STT·LLM·TTS) 사용량 수집 → 요약 1건 → 로그 1줄.

🧒 왜 필요한가: 캐스케이드의 **유일한 동기가 원가**다("Live 와 같은 동작을 더 싸게").
  그런데 캐스케이드 세션은 지금껏 사용량을 한 톨도 안 남겼다 — 실사용에 들어가도 Live 와
  비교할 숫자가 안 생긴다. 목적 자체를 검증할 수 없다는 뜻이라, 배관보다 먼저 붙인다.

계약(bt-back 확정, 임의 변경 금지 — call15-polish 가 반대편 컬럼을 같은 계약으로 짠다):

    call.usage_engine = '<모드>:<구성요소를 + 로 연결>'
        예) 'cascade:google-stt-v2+gemini-2.5-flash+cloud-tts-chirp3-hd'
    usage_in_text / usage_out_text   = **LLM 토큰**
    usage_in_audio / usage_out_audio = 0   (캐스케이드 LLM 은 오디오를 안 받는다)
    usage_json.vendors = {"stt": {...audio_s}, "llm": {...토큰}, "tts": {...chars}}

STT·TTS 는 단위가 토큰이 아니라 **초·문자**라 컬럼에 섞으면 의미가 깨진다 → vendors 로 간다.

사용량을 어디서 얻나(1차 자료로 확인, 상세·근거는 docs/20260807_0030_캐스케이드-원가계측-설계.md):
  - **STT**: 벤더가 준다. `RecognitionResponseMetadata.total_billed_duration`
      "When available, billed audio seconds for the corresponding request."
      ⚠ v1 은 "for the stream / 마지막 응답에만" 이었다 → v2 가 증분인지 누적 반복인지
        원문만으로 못 정한다. sum·max 를 둘 다 들고 첫 실통화 로그로 확정한다.
  - **LLM**: 벤더가 준다(`usage_metadata`) — Live 경로가 이미 쓰는 것과 같은 필드.
  - **TTS**: **안 준다.** 설치된 SDK 응답 정의가 `audio_content` 하나뿐이라, 우리가 API 에
      실제로 넘긴 문자열 길이를 센다(core/tts.py 는 `text.strip()` 을 보낸다 → strip 후 길이).

⛔ R5 — 계측 실패가 통화를 죽이면 안 된다. 이 모듈의 모든 진입점은 예외를 전량 흡수하고,
  실패해도 세션 종료·클라 프로토콜은 그대로다. 오디오 펌프에는 계측 호출이 **없다**
  (스트림이 부수적으로 누적하고, 세션은 **끝난 뒤에만** 한 번 읽는다).

⛔ DB 영속화는 여기 없다. 캐스케이드를 통화 기록 파이프라인에 올릴지가 미정이고
  (run_cascade 에 call_id 가 없다), 컬럼·마이그레이션은 call15-polish 담당이다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MODE = "cascade"

# 구성요소 이름(engine 문자열에 그대로 들어간다). 벤더를 갈아끼우면 여기만 늘어난다 —
# 같은 컬럼에서 'cascade:whisper+...' 로 갈라져 보이게 하려는 것이 계약의 의도다.
# 서버→클라 오디오 규약(PCM16 / 24kHz mono) = 48,000 bytes/s. 바이트↔초는 산수다.
_TTS_BYTES_PER_S = 24000 * 2
STT_VENDOR_V2 = "google-stt-v2"
STT_VENDOR_FAKE = "fake-stt"


def stt_vendor_name(engine: str) -> str:
    """`core.stt.stt_v2_engine_name()` 의 반환('v2'|'fake') → 계약용 벤더 이름.

    페이크 세션이 실통화 원가에 섞이면 안 되므로 **엔진 이름이 다르게 남는다**.
    """
    return STT_VENDOR_V2 if engine == "v2" else STT_VENDOR_FAKE


def _num(value: Any) -> float:
    """숫자만 통과시킨다(미상 필드·None·문자열 내성 — 벤더 응답은 언제든 모양이 바뀐다)."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _attr(obj: Any, name: str) -> float:
    return _num(getattr(obj, name, None))


@dataclass
class SttUsage:
    """STT 구간. 단위는 ms 로 모으고 요약에서만 초로 바꾼다(반올림 오차 누적 방지)."""

    vendor: str = STT_VENDOR_V2
    collected: bool = False
    streams: int = 0
    sent_ms: float = 0.0        # 우리가 STT 로 흘린 오디오(무음 포함 — 마이크는 계속 열려 있다)
    replay_ms: float = 0.0      # 롤오버 때 **다시** 흘린 구간(이중 과금 후보)
    billed_sum_ms: float = 0.0  # 벤더가 준 값의 합
    billed_max_ms: float = 0.0  # 스트림별 최댓값의 합
    billed_msgs: int = 0        # 값이 실려 온 응답 수(0 = 벤더가 안 줬다)


@dataclass
class LlmUsage:
    vendor: str = ""
    calls: int = 0
    in_text: int = 0
    out_text: int = 0
    thoughts: int = 0    # ⚠ candidates 에 안 들어간다 — 출력 원가를 계산할 땐 더해야 한다
    cached: int = 0
    # ⭐ **토큰을 못 받은 호출 수**(2026-08-12). 스트림을 문장 상한에서 중간에 닫으면 벤더가
    #   usage 객체는 주는데 **필드가 전부 None** 인 회차가 있다(실측 4회 중 1회).
    #   ⛔ 그걸 0 으로 먹으면 원가가 조용히 과소 집계된다 — **모른다고 드러낸다.**
    unknown: int = 0
    total: int = 0

    # ⭐⭐ **추정 보정**(2026-08-13). 위 `unknown` 이 실통화에서 60~87% 였다 — 원인은 우리다:
    #   문장 상한을 채우면 `stream.aclose()` 로 끊는데(cascade_session), Gemini 는
    #   `usage_metadata` 를 **마지막 조각에만** 싣는다 ⇒ 상한이 잘 걸릴수록 장부가 더 빈다.
    #   ⛔ 끊는 걸 되돌리지 않는다 — 그건 상한의 목적(원가·지연 절감)을 없앤다.
    #   ⇒ 대신 **같은 통화의 실측 회차로** 메운다(추가 API 호출 0·비용 0).
    #   ⛔ 실측과 **절대 한 칸에 안 섞는다** — 아래 est_* 는 끝까지 따로 산다.
    est_in: int = 0
    est_out: int = 0
    est_calls: int = 0
    # 회차별 표본 {"in": 토큰|None, "out": 토큰|None, "out_chars": 생성 글자수}.
    # ⭐ **리스트 순서가 곧 턴 순서다** — 프롬프트는 이력이 쌓이며 단조 증가하므로,
    #   "가까운 회차"로 메우는 것이 평균으로 메우는 것보다 정확하다.
    samples: list = field(default_factory=list)


@dataclass
class TtsUsage:
    vendor: str = ""
    calls: int = 0
    calls_failed: int = 0     # 실패 호출의 과금 여부는 문서 미확인 → 문자에 안 더한다
    chars: int = 0
    chars_unheard: int = 0    # 합성했지만 barge-in 으로 못 들려준 문자(돈은 나갔다)
    # ⭐ 쿼터(429)로 **다른 엔진으로 내려간 횟수**. 사장님은 목소리가 바뀐 걸 소리로만 아신다 —
    #   통화가 끝난 뒤 "그 통화에서 몇 번 바뀌었나"를 숫자로 답할 수 있어야 한다.
    fallbacks: int = 0
    # ⭐ 내보낸 오디오 바이트(PCM16/24k mono). **Gemini-TTS 는 문자가 아니라 출력 오디오
    #   토큰으로 과금한다**(1초 = 25tok) — 같은 문장도 천천히 읽으면 더 비싸다. 문자↔초
    #   환산은 말하는 속도에 따라 배로 틀리므로 **초를 실측**해서 넘긴다.
    #   덤: 비버가 실제로 몇 초 말했는지도 이걸로 처음 확정된다(지금 원가 계산은 가정 위에 있다).
    audio_bytes: int = 0


@dataclass
class CascadeUsage:
    """세션 1건의 사용량 누계. 요약은 순수 함수 1개(summary)가 만든다 — 로그와 (훗날) DB 의 단일 소스."""

    stt: SttUsage = field(default_factory=SttUsage)
    llm: LlmUsage = field(default_factory=LlmUsage)
    tts: TtsUsage = field(default_factory=TtsUsage)
    errors: int = 0          # 계측 자체가 실패한 횟수(조용히 비는 것보다 세는 게 낫다)

    # ── 수집 ──
    def record_stt(self, stream: Any, engine: str = "v2") -> None:
        """세션 종료 시 STT 스트림에서 누계를 **한 번** 걷는다.

        `usage()` 가 없는 객체(다른 엔진·목)도 그냥 통과한다 — 계측 부재는 오류가 아니다.
        """
        try:
            # ⭐ 스트림이 자기 벤더를 알면 **그게 진실이다** — 폴백이 일어나면 엔진 이름과
            #   실제로 돈 엔진이 갈린다(TTS 폴백에서 배운 그대로).
            self.stt.vendor = getattr(stream, "vendor", "") or stt_vendor_name(engine)
            usage = stream.usage() if hasattr(stream, "usage") else None
            if not usage:
                return
            self.stt.collected = True
            self.stt.streams = int(_num(usage.get("streams")))
            self.stt.sent_ms = _num(usage.get("sent_audio_ms"))
            self.stt.replay_ms = _num(usage.get("replay_audio_ms"))
            self.stt.billed_sum_ms = _num(usage.get("billed_sum_ms"))
            self.stt.billed_max_ms = _num(usage.get("billed_max_ms"))
            self.stt.billed_msgs = int(_num(usage.get("billed_msgs")))
        except Exception as exc:  # noqa: BLE001 - R5
            self.errors += 1
            logger.warning("cascade usage: STT 계측 수집 실패(무시) — %s", exc)

    def record_llm(self, usage_metadata: Any, vendor: str = "", out_chars: int = 0) -> None:
        """LLM 응답 1건의 usage_metadata 를 누적한다(대답 배관이 호출한다).

        필드는 Live 경로와 같다. 없는 필드는 0 으로 흡수한다 — 모델·SDK 가 바뀌어도
        **한 필드가 사라졌다고 통화가 죽지는 않게**.

        Args:
            out_chars: 이 회차가 **실제로 생성한 글자수**(`chat.text`). 벤더가 토큰을 안 준
                회차를 나중에 메우는 유일한 단서다 — 토큰은 못 받아도 글자는 우리가 받았다.
        """
        try:
            if vendor:
                self.llm.vendor = vendor
            self.llm.calls += 1
            in_tok = int(_attr(usage_metadata, "prompt_token_count"))
            out_tok = int(_attr(usage_metadata, "candidates_token_count"))
            # ⭐ 표본은 **성공·실패 모두** 남긴다 — 빈 회차가 어디 있었는지 알아야 메운다.
            self.llm.samples.append({
                "in": in_tok or None, "out": out_tok or None,
                "out_chars": max(0, int(out_chars or 0)),
            })
            if usage_metadata is None or getattr(usage_metadata, "total_token_count", None) is None:
                # ⚠ 이 호출의 토큰은 **모른다**(스트림 조기 종료 등). 세어서 드러낸다.
                #   ⭐ 요약에서 메울 수 있으면 메우고, 그때 `unknown` 은 **못 메운 것만** 남는다.
                self.llm.unknown += 1
                logger.info(
                    "cascade usage: LLM 토큰 미수신(usage=%s, 생성 %d자) — 요약에서 추정으로 메운다",
                    "없음" if usage_metadata is None else "필드 없음", out_chars or 0,
                )
            self.llm.in_text += in_tok
            self.llm.out_text += out_tok
            self.llm.thoughts += int(_attr(usage_metadata, "thoughts_token_count"))
            self.llm.cached += int(_attr(usage_metadata, "cached_content_token_count"))
            self.llm.total += int(_attr(usage_metadata, "total_token_count"))
        except Exception as exc:  # noqa: BLE001 - R5
            self.errors += 1
            logger.warning("cascade usage: LLM 계측 실패(무시) — %s", exc)

    def record_tts_fallback(self) -> None:
        """쿼터 등으로 **엔진이 바뀌었다**(목소리가 바뀐다). 통화당 횟수를 센다."""
        self.tts.fallbacks += 1

    def record_tts(self, text: str, vendor: str = "", failed: bool = False) -> None:
        """TTS 합성 1건. **API 에 실제로 넘긴 문자열**의 길이를 센다(core/tts.py 는 strip 해서 보낸다).

        실패 호출은 문자에 더하지 않고 건수만 센다 — 실패가 과금되는지 문서로 확인하지 못했다.
        확인되면 여기 한 곳만 바꾸면 된다(설계 §2-5).
        """
        try:
            if vendor:
                self.tts.vendor = vendor
            if failed:
                self.tts.calls_failed += 1
                return
            self.tts.calls += 1
            self.tts.chars += len((text or "").strip())
        except Exception as exc:  # noqa: BLE001 - R5
            self.errors += 1
            logger.warning("cascade usage: TTS 계측 실패(무시) — %s", exc)

    def retag_tts(self, vendor: str) -> None:
        """벤더 이름만 바꾼다 — ⛔ **문자를 다시 세지 않는다.**

        폴백(의도한 엔진이 실패해 다른 엔진이 소리를 냈다)에서 필요하다. 예전엔 호출부가
        `record_tts(문장, vendor=실제엔진)` 을 한 번 더 불러서 **같은 문장을 두 번 셌다** —
        원가가 두 배로 잡힌다. 이 프로젝트의 유일한 동기가 원가라, 그 숫자가 틀리면
        "캐스케이드가 싼가"라는 질문 자체가 무의미해진다.
        """
        try:
            if vendor:
                self.tts.vendor = vendor
        except Exception as exc:  # noqa: BLE001 - R5
            self.errors += 1
            logger.warning("cascade usage: TTS 벤더 갱신 실패(무시) — %s", exc)

    def record_tts_audio(self, audio_bytes: int) -> None:
        """실제로 내보낸 TTS 오디오 바이트(PCM16/24k). 초 환산은 요약에서 한다.

        ⚠ 문자 수로 대신할 수 없다 — Gemini-TTS 는 **출력 오디오 토큰**으로 과금하고
        (1초 = 25tok), 같은 문장도 읽는 속도에 따라 초가 달라진다. 그래서 재는 쪽을 바꾼다.
        """
        try:
            self.tts.audio_bytes += max(0, int(audio_bytes))
        except Exception as exc:  # noqa: BLE001 - R5
            self.errors += 1
            logger.warning("cascade usage: TTS 오디오 계측 실패(무시) — %s", exc)

    def record_tts_unheard(self, chars: int) -> None:
        """합성은 했지만 barge-in 취소로 못 들려준 문자 수(이미 과금된 몫)."""
        try:
            self.tts.chars_unheard += max(0, int(chars))
        except Exception as exc:  # noqa: BLE001 - R5
            self.errors += 1
            logger.warning("cascade usage: TTS 취소분 계측 실패(무시) — %s", exc)

    # ── 요약(순수) ──
    def engine(self) -> str:
        """실제로 **돈 구성요소만** 잇는다. 안 돈 구간을 적으면 원가 비교가 거짓말이 된다."""
        parts = []
        if self.stt.collected:
            parts.append(self.stt.vendor)
        if self.llm.calls:
            parts.append(self.llm.vendor or "llm")
        if self.tts.calls or self.tts.calls_failed:
            parts.append(self.tts.vendor or "tts")
        return f"{MODE}:" + ("+".join(parts) if parts else "none")

    def _estimate_missing(self) -> None:
        """토큰을 못 받은 회차를 **같은 통화의 실측 회차로** 메운다(추정 열은 따로 둔다).

        왜 필요한가 — 실통화에서 LLM 회차의 60~87% 가 토큰 없이 끝났다. 원인은 벤더가 아니라
        우리다: 문장 상한을 채우면 스트림을 끊는데 Gemini 는 usage 를 **마지막 조각에만** 준다.
        그대로 두면 원가 장부가 약 2.5배 과소집계된다(실측 23콜 중 9콜만 세고 있었다).

        어떻게 메우나
          입력  **가장 가까운 실측 회차의 prompt_token_count**. 평균이 아니라 근접값을 쓰는
                이유: 프롬프트는 대화 이력이 쌓이며 **단조 증가**한다 — 평균은 초반을 과대,
                후반을 과소 추정한다. 동점이면 **앞쪽**(더 작은 값)을 택한다: 과대계상보다
                과소계상이 덜 위험하냐가 아니라, **추정이 실측을 넘어서지 않게** 하는 쪽이다.
          출력  같은 통화의 **글자→토큰 비율** × 그 회차가 생성한 글자수. 비율을 통화 안에서
                뽑는 이유: 언어(한국어/영어)마다 글자당 토큰이 배로 다르다.

        ⛔ 실측이 **한 건도 없으면 아무것도 추정하지 않는다.** 근거가 없는 숫자를 만드느니
          `unknown` 으로 비워 두는 게 낫다 — 이 함수의 존재 이유가 "조용히 0으로 먹지 않기"다.
        ⛔ 멱등이다(요약을 두 번 만들어도 값이 안 불어난다) — 로그와 DB 가 같은 객체를 본다.
        """
        self.llm.est_in = self.llm.est_out = self.llm.est_calls = 0
        samples = self.llm.samples
        measured = [(i, s["in"]) for i, s in enumerate(samples) if s["in"]]
        if not measured:
            return
        # 글자→토큰 비율: **입출력 둘 다 실린 회차**에서만 뽑는다(짝이 안 맞으면 비율이 아니다).
        chars = sum(s["out_chars"] for s in samples if s["out"] and s["out_chars"])
        toks = sum(s["out"] for s in samples if s["out"] and s["out_chars"])
        per_char = (toks / chars) if chars > 0 else 0.0
        for idx, s in enumerate(samples):
            if s["in"]:
                continue
            # min 은 동점에서 **먼저 온 것**을 남긴다 = 앞쪽 회차(더 작은 프롬프트).
            _, near_in = min(measured, key=lambda p: abs(p[0] - idx))
            self.llm.est_in += near_in
            self.llm.est_out += int(round(s["out_chars"] * per_char))
            self.llm.est_calls += 1

    def summary(self, duration_s: float | None = None, turns: int | None = None) -> dict | None:
        """계약 모양의 요약 1건. 아무것도 못 모았으면 None(호출부가 '미수집'으로 분기).

        `audio_s`(계약 키)에 무엇을 넣었는지는 **항상** `audio_s_source` 가 말한다:
          - "vendor_billed_max" : 벤더가 준 과금 초(스트림별 최댓값의 합 — 실통화로 확정된 기본)
          - "sent_audio"        : 우리가 흘린 오디오 길이(벤더 값이 안 실렸을 때의 폴백)
        """
        try:
            if not (self.stt.collected or self.llm.calls or self.tts.calls or self.tts.calls_failed):
                return None
            # ⭐ 2026-08-07 실통화로 **판정 완료**: total_billed_duration 은 응답마다의 증분이
            #   아니라 **누적값이 반복해 실린다.** 실측(통화 104초): max=102.0s 가 실제 오디오와
            #   맞고 sum=419.0s 는 4배다. → 원가 산식은 **max** 를 쓴다. sum 을 썼으면 STT
            #   원가를 4배로 과대계상했다(설계 §1-1 의 판정표대로 sum·max 를 둘 다 든 덕에 갈렸다).
            #   벤더 값이 안 실린 세션(페이크·필드 미제공)에서는 우리 카운터로 폴백한다.
            billed_s = round(self.stt.billed_max_ms / 1000.0, 1)
            sent_s = round(self.stt.sent_ms / 1000.0, 1)
            use_vendor = self.stt.billed_msgs > 0 and self.stt.billed_max_ms > 0
            stt = {
                "vendor": self.stt.vendor,
                "audio_s": billed_s if use_vendor else sent_s,
                "audio_s_source": "vendor_billed_max" if use_vendor else "sent_audio",
                "sent_audio_s": round(self.stt.sent_ms / 1000.0, 1),
                "replay_audio_s": round(self.stt.replay_ms / 1000.0, 1),
                "billed_sum_s": round(self.stt.billed_sum_ms / 1000.0, 1),
                "billed_max_s": round(self.stt.billed_max_ms / 1000.0, 1),
                "billed_msgs": self.stt.billed_msgs,
                "streams": self.stt.streams,
            }
            # ⭐ 빈 회차를 메운다. **여기 한 번만** 부른다 — 요약이 원가의 단일 소스이므로.
            self._estimate_missing()
            in_total = self.llm.in_text + self.llm.est_in
            out_total = self.llm.out_text + self.llm.est_out
            llm = {
                "vendor": self.llm.vendor, "calls": self.llm.calls,
                # ⚠ 여기 `in_text`/`out_text` 는 **실측+추정**이다(원가는 이걸 써야 맞다).
                #   ⛔ 그래도 실측은 잃지 않는다 — `*_measured`/`*_est` 로 언제든 되가른다.
                "in_text": in_total, "out_text": out_total,
                "in_text_measured": self.llm.in_text, "out_text_measured": self.llm.out_text,
                "in_text_est": self.llm.est_in, "out_text_est": self.llm.est_out,
                "est_calls": self.llm.est_calls,
                "thoughts": self.llm.thoughts, "cached": self.llm.cached,
                # ⭐ **끝내 못 메운** 호출 수 — 0 이 아니면 그만큼은 여전히 과소집계다.
                #   조용히 비는 것보다 세는 게 낫다(errors 와 같은 규율).
                "unknown": max(0, self.llm.unknown - self.llm.est_calls),
            }
            tts = {
                "vendor": self.tts.vendor, "calls": self.tts.calls,
                "calls_failed": self.tts.calls_failed,
                "fallbacks": self.tts.fallbacks,
                "chars": self.tts.chars, "chars_unheard": self.tts.chars_unheard,
                # ⭐ 오디오 초 — Gemini-TTS 단가의 기준이다(문자 아님). PCM16/24k mono 라
                #   바이트 ÷ 48,000 이고, 이건 추정이 아니라 우리가 내보낸 실측이다.
                "audio_s": round(self.tts.audio_bytes / _TTS_BYTES_PER_S, 1),
            }
            return {
                "engine": self.engine(),
                # 컬럼 대응 4항 — 캐스케이드 LLM 은 오디오를 안 받는다(계약).
                "in_text": in_total, "out_text": out_total,
                "in_audio": 0, "out_audio": 0,
                # ⚠ total 도 추정을 얹는다 — 위 두 값과 다른 근거로 계산하면 장부가 갈린다.
                "total": self.llm.total + self.llm.est_in + self.llm.est_out,
                "vendors": {"stt": stt, "llm": llm, "tts": tts},
                "dur_s": round(duration_s, 1) if duration_s is not None else None,
                "turns": turns,
                "errors": self.errors,
            }
        except Exception as exc:  # noqa: BLE001 - R5
            logger.warning("cascade usage: 요약 실패(무시) — %s", exc)
            return None


def format_usage_line(summary: dict) -> str:
    """요약 → `key=value` 한 줄.

    ⛔ 형식을 함부로 바꾸지 마라. Live 쪽과 같은 규율이다 — Cloud Logging 에서 grep 과
      로그 기반 메트릭이 이 줄을 그대로 읽는다(`normalcall usage:` 와 짝).
    """
    stt = summary["vendors"]["stt"]
    llm = summary["vendors"]["llm"]
    tts = summary["vendors"]["tts"]
    return (
        f"engine={summary['engine']} dur_s={summary.get('dur_s')} turns={summary.get('turns')} "
        f"stt_audio_s={stt['audio_s']} stt_src={stt['audio_s_source']} "
        f"stt_replay_s={stt['replay_audio_s']} stt_streams={stt['streams']} "
        f"stt_billed_sum_s={stt['billed_sum_s']} stt_billed_max_s={stt['billed_max_s']} "
        f"stt_billed_msgs={stt['billed_msgs']} "
        f"llm_in={llm['in_text']} llm_out={llm['out_text']} llm_thoughts={llm['thoughts']} "
        f"llm_calls={llm['calls']} "
        # ⚠ 2026-08-13 부터 위 llm_in/llm_out 은 **실측+추정**이다(그 전 값은 과소집계였다).
        #   추정이 섞였으면 **항상 이 두 칸이 함께 뜬다** — 숫자만 보고 실측으로 읽지 않게.
        + (f"llm_est={llm.get('est_calls', 0)}콜(in+{llm.get('in_text_est', 0)} "
           f"out+{llm.get('out_text_est', 0)}) " if llm.get("est_calls") else "")
        + (f"⚠llm_unknown={llm['unknown']} " if llm.get("unknown") else "")
        + 
        f"tts_chars={tts['chars']} tts_audio_s={tts['audio_s']} "
        f"tts_unheard={tts['chars_unheard']} tts_calls={tts['calls']} "
        f"tts_failed={tts['calls_failed']} tts_fallbacks={tts.get('fallbacks', 0)} "
        f"err={summary.get('errors', 0)}"
    )


def log_usage_summary(
    usage: CascadeUsage, duration_s: float | None = None, turns: int | None = None
) -> dict | None:
    """세션 종료 시 사용량 한 줄을 방출한다. 못 모았으면 **그 사실을 한 줄로** 남긴다.

    ⭐ 조용히 비우지 않는 것이 요점이다. Live 경로는 call_id 부재·usage 0건·예외를 전부
      조용히 삼켜서, 원가가 안 남아도 아무도 몰랐다. 같은 실수를 반복하지 않는다.
    """
    try:
        summary = usage.summary(duration_s=duration_s, turns=turns)
        if summary is None:
            logger.info(
                "cascade usage: engine=%s collected=0 reason=no_usage_recorded err=%d",
                usage.engine(), usage.errors,
            )
            return None
        logger.info("cascade usage: %s", format_usage_line(summary))
        return summary
    except Exception as exc:  # noqa: BLE001 - R5
        logger.warning("cascade usage: 방출 실패(무시) — %s", exc)
        return None
