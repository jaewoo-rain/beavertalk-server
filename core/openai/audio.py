"""OpenAI Realtime 전용 오디오 변환 — 업링크 16kHz → 24kHz.

## 왜 이 파일이 따로 있나
OpenAI Realtime 은 **24kHz 만** 받는다(공식: "Only a 24kHz sample rate is supported,
and the audio format type is audio/pcm" — 2026-10-03 실측에서 16k 세션은 거부됐다).
그런데 앱은 16kHz PCM16 을 보낸다. 그 간극은 **이 엔진만의 사정**이므로 어댑터가 자기
안에서 메꾼다 — 앱도, 공용 오디오 상수도 건드리지 않는다.

## ⛔ `audioop` 을 쓰지 않는다
이 저장소는 이미 같은 문제를 만나 순수 파이썬으로 내려간 선례가 있다(RMS 계산).
`audioop` 은 파이썬 3.13 에서 제거됐고, 지금 런타임이 3.12 라서 "아직 되는" 상태일
뿐이다. 되는 동안 쓰면 3.13 올라가는 날 통화가 죽는다. `scipy` 도 안 쓴다 — 15분에
몇 초짜리 일에 의존성을 늘릴 이유가 없다.

## 변환 방식 — 2:3 선형보간
16000:24000 = 2:3 이라 **입력 2샘플마다 출력 3샘플**이 나오고, 출력 j 번째 샘플의
입력 좌표는 `j × 2/3` 이다. 그 좌표 양옆 두 입력 샘플을 선형보간한다.

- **클리핑이 원리적으로 불가능하다**: 선형보간은 두 값의 볼록결합이라 결과가 항상
  `[min(x_i, x_i+1), max(...)]` 안에 있다. int16 범위를 벗어날 수 없다.
- **프레임 경계에서 클릭이 안 난다**: 직전 프레임의 **마지막 샘플 1개**와 위상(phase)을
  들고 간다. 보간 보폭이 2/3 < 1 이라 필요한 과거 샘플은 항상 1개뿐이다.
- **위상 드리프트가 누적되지 않는다**: 매 프레임 끝에서 위상을 새 프레임 좌표계로
  다시 재어 `[0, 2/3)` 로 되돌린다 — float 오차가 통화 전체로 쌓이지 않는다.
"""

from __future__ import annotations

from array import array

SOURCE_SAMPLE_RATE = 16_000
TARGET_SAMPLE_RATE = 24_000
SAMPLE_WIDTH_BYTES = 2
CHANNELS = 1

# session.update 의 audio.input.format / audio.output.format 에 그대로 들어가는 값.
# ⛔ 입·출력이 같아야 한다(세션 중 변경 불가 — 공식 문서).
AUDIO_FORMAT: dict = {"type": "audio/pcm", "rate": TARGET_SAMPLE_RATE}

# 출력 PCM24k 의 초당 바이트 — 앱 재생 파이프라인과 같은 값이라 **출력은 변환이 없다**.
TARGET_BYTES_PER_S = TARGET_SAMPLE_RATE * SAMPLE_WIDTH_BYTES * CHANNELS

_STEP = SOURCE_SAMPLE_RATE / TARGET_SAMPLE_RATE   # 2/3 — 출력 1샘플당 입력 좌표 증가분
_EPS = 1e-9


class Upsampler16kTo24k:
    """스트리밍 16k→24k 업샘플러. 통화 하나에 **인스턴스 하나**(상태를 들고 간다).

    ⛔ 인스턴스를 여러 펌프가 공유하면 위상이 섞인다 — 업링크 펌프 하나만 호출한다.
    """

    __slots__ = ("_prev", "_phase", "_tail")

    def __init__(self) -> None:
        self._prev: int | None = None   # 직전 프레임의 마지막 샘플(경계 보간용)
        self._phase: float = 0.0        # 다음 출력 샘플의 입력 좌표(현재 버퍼 기준)
        self._tail: bytes = b""         # 홀수 바이트로 잘려 온 샘플의 앞바이트

    def feed(self, pcm16_16k: bytes) -> bytes:
        """16kHz PCM16(LE) 청크 → 24kHz PCM16(LE) 청크. 입력이 비면 빈 바이트.

        ⚠ 반환 길이는 입력의 정확히 1.5배가 아니다 — 위상이 프레임을 넘어가므로
          프레임마다 출력 샘플 수가 1개 안팎으로 달라지고, 긴 구간에서 평균 1.5배에
          수렴한다. 길이로 동기를 맞추는 호출부가 있으면 안 된다(없다 — 스트리밍이다).
        """
        if not pcm16_16k:
            return b""
        raw = self._tail + pcm16_16k
        if len(raw) % SAMPLE_WIDTH_BYTES:
            # 샘플 중간에서 끊긴 바이트는 다음 호출로 넘긴다(버리면 그 자리에 클릭이 난다).
            self._tail = raw[-1:]
            raw = raw[:-1]
        else:
            self._tail = b""
        if not raw:
            return b""

        src = array("h")
        src.frombytes(raw)   # LE 가정: 우리 파이프라인 전체가 PCM16 LE 다(Windows/Linux 공통 little-endian)

        if self._prev is None:
            buf = src
        else:
            buf = array("h", (self._prev,))
            buf.extend(src)

        n = len(buf)
        out = array("h")
        p = self._phase
        last = n - 1
        while p < last + _EPS:
            i = int(p)
            if i >= last:
                out.append(buf[last])          # 정확히 마지막 샘플 위 — 보간할 다음 샘플이 없다
                p += _STEP
                continue
            frac = p - i
            a = buf[i]
            # ⛔ `int(x + 0.5)` 를 쓰지 마라 — 음수에서 0 쪽으로 잘려 **DC 오프셋**이 생긴다
            #   (-3.2 → -2). 오디오는 절반이 음수다. `round` 로 양쪽을 같게 깎는다.
            out.append(round(a + (buf[i + 1] - a) * frac) if frac else a)
            p += _STEP

        # 다음 프레임의 buf[0] = 이 프레임의 buf[last] ⇒ 위상을 그 좌표계로 옮긴다.
        self._prev = buf[last]
        self._phase = p - last
        return out.tobytes()

    def reset(self) -> None:
        """경계 상태를 버린다(세션을 새로 열 때). 통화 중에는 부르지 않는다."""
        self._prev = None
        self._phase = 0.0
        self._tail = b""
