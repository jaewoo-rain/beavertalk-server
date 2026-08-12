"""캐스케이드 P1 — LLM 응답을 **문장 단위로** TTS 에 흘려 비버 턴으로 송출한다.

    사용자 최종 전사 → [LLM 스트리밍] → [문장 분할] → [TTS 스트리밍] → BeaverOutput(페이서·원장)

왜 문장 단위인가: 첫 소리까지의 지연이 캐스케이드의 약점이다(설계 §4 — 1.5~2.5초). 문장이
완성되는 즉시 합성해 흘리면 LLM 이 아직 뒷문장을 쓰는 동안 앞문장이 이미 들린다. 반대로 너무
잘게 쪼개면 TTS 요청 수가 늘고 운율이 끊긴다 — 그래서 최소 길이를 둔다.

⛔ 불변식은 BeaverOutput 이 지킨다(I1~I5). 여기서는 **문장 텍스트를 조각의 마지막에 붙이는
  것**만 지키면 된다: 원장 절단이 "그 문장을 끝까지 들었을 때만 이력에 남긴다"이므로, 문장
  이름표는 그 문장의 **마지막 오디오 조각**에 달려야 걸친 문장이 자동으로 버려진다.

⛔ 취소(barge-in)는 이 태스크를 **밖에서 취소**해 이뤄진다. 여기서 CancelledError 를 잡아
  삼키지 않는다 — 세션이 잡아 audio_cancel 을 낸다(설계 §5).
"""

from __future__ import annotations

import logging
import re
from typing import Any, AsyncIterator

from core.audio import align_pcm16, trim_silence_edges

# ── 감정 태그 (2026-08-10 사장님 지시) ──────────────────────────────────────
# ① "llm 이 감정을 태그로 뱉어서 그걸 tts 에 넣어야 하잖아"
# ② ⭐ "미리 감정들 프롬프트를 정해놓고 **골라 쓰게** 하는 게 어때? **프롬프트가 일정해야
#    감정 표현도 그나마 일정할 거 아니야**"
# ⇒ **자유 서술 금지.** LLM 이 매번 스타일 문장을 지어내면 같은 감정도 통화마다 다르게 들린다.
#   여기 표가 유일한 출처이고, LLM 은 **이 태그 이름만** 고른다.
#
# ⛔ **개수를 늘리지 마라.** 많을수록 LLM 이 헷갈리고 표현이 흔들린다(②를 깬다). 6개는
#   어학 선생님이 실제로 하는 일 — 칭찬·격려·질문·설명·정정·인사 — 에서 뽑았다.
# ⛔ **속도 어휘를 넣지 마라**(천천히·또박또박·느리게·차분·차근…). 오늘 "또박또박" 한 낱말이
#   한국어 속도의 절반을 먹고 있었다(ko 1.3 → 침묵 정리 후 6.2~7.4자per초). 속도는
#   `speaking_rate` 파라미터가 맡는다 — 문구와 파라미터가 싸우면 어느 게 진짜인지 못 가린다.
# ⭐⭐ **클라 아바타 표정 5종과 같은 축**(2026-08-12 사장님 결정). 예전엔 화행 6종
#   (칭찬·격려·질문·설명·정정·인사)이었는데 클라는 `neutral/happy/surprised/sad/angry`
#   **표정**을 그린다 — 축이 달라 셋이 neutral 로 뭉개지고 sad·surprised·angry 는 영영 안 떴다.
#   ⇒ **매핑 계층을 만들지 않는다.** 서버가 처음부터 클라 어휘로 뱉는다.
#     매핑이 있으면 "서버는 A 를 보냈는데 클라는 B 를 그린다"가 생긴다 — 오늘만 그 계열
#     (감정 로그 거짓말·STT 언어 오표시)로 여러 번 당했다.
#   ⭐ 이름을 **영문 슬러그**로 쓰는 이유: 클라가 쓰는 낱말 그대로다. 한글로 두면 어딘가에서
#     번역이 필요해지고, 그 번역이 곧 매핑 계층이다.
#
# ⛔⛔ **캐릭터 색을 여기 넣지 마라**(2026-08-12 사장님 지적). 예전 문구는 "밝고 기쁘게",
#   "친절하게 알려 주는 **선생님**" 처럼 *다정한 선생님 하나*를 가정했다. 그런데 캐릭터는
#   DB 에 있고 제각각이다 — 츤데레도 "친절한 선생님 목소리"로 읽어 버린다.
#   ⇒ 여기는 **감정만**, 캐릭터 색은 페르소나(role·personality)가 넣는다. 얇은 층으로 얹는다.
# ⛔ 속도 어휘 금지(천천히·또박또박…) — 속도는 `speaking_rate` 파라미터가 맡는다.
EMOTION_STYLES: dict[str, str] = {
    # ⚠ neutral 이 **무표정한 낭독**이 되면 안 된다. 그렇다고 "친절하게"를 쓰면 캐릭터를
    #   덮어쓴다 — 그래서 "네 평소 말투"라고 쓴다. 평소 말투는 페르소나가 정한다.
    "neutral": "평소 말투 그대로, 생기 있게.",
    "happy": "기쁜 기분이 목소리에 드러나게.",
    "surprised": "놀란 기분이 목소리에 드러나게.",
    "sad": "가라앉고 안타까운 기분이 목소리에 드러나게.",
    "angry": "언짢은 기분이 목소리에 드러나게.",
}
# 태그 표기: 대사 맨 앞의 `<칭찬>`. ⚠ 어떤 위치에 나와도 **전부** 걷어낸다(소리로 나가면 안 된다).
# ⭐ **꺾쇠가 정식**이다(2026-08-12). 프롬프트가 대괄호를 구획 라벨로 쓰는 것과 충돌해
#   모델이 태그 자리에 `[공부 모드]` 를 넣었다 — 그래서 표기를 비켰다(persona_prompt 주석 참고).
# ⚠ **옛 대괄호도 계속 받는다.** 모델이 규약을 어기고 `[칭찬]` 을 뱉을 때, 안 받으면
#   감정을 잃고 그 글자가 소리로 나간다. 받는 쪽이 손해가 없다(어차피 걷어낸다).
_EMOTION_TAG_RE = re.compile(
    r"[\[<](%s)[\]>]" % "|".join(map(re.escape, EMOTION_STYLES))
)

# ⛔⛔ **집합 밖 대괄호 토큰**(2026-08-12 실통화 00147). 비버가 `[대화] ~~` 로 시작했고
#   위 정규식은 6개만 알아서 **안 지워졌다** → TTS 가 "대화"를 그대로 읽었다. 같은 원인으로
#   `detect_emotion` 도 None 을 내서 그 통화의 감정이 전부 '없음'이었다. **두 증상이 하나다.**
#
# ⭐ 왜 LLM 이 그걸 뱉나(가설, 프롬프트는 이번 범위 밖이라 안 고쳤다):
#   `core/persona_prompt.py` 가 `[공부 모드]` `[대화 모드]` `[학습자 수준]`
#   `[오늘의 공부 항목]` 처럼 **대괄호를 구획 라벨로** 쓴다. 같은 프롬프트가 "대사 맨 앞에
#   `[칭찬]` 을 붙여라"라고도 가르치니, 모델이 그 자리에 **모드 이름**을 넣는다.
#   ⇒ 프롬프트를 정제할 때 이 충돌을 같이 봐야 한다(라벨 표기를 바꾸든 태그를 다른 기호로 하든).
#
# ⚠ 범위를 **맨 앞 + 한글 토큰**으로 좁힌 이유(정상 대사를 지우면 안 된다):
#   · 위치 — 규약이 '맨 앞'이다. 문장 중간의 대괄호는 대사의 일부일 가능성이 높다.
#   · 한글 — 이 앱의 대사에서 대괄호가 정상적으로 쓰이는 자리는 **로마자 표기·영어 뜻**
#     (`[annyeonghaseyo]`)이다. 프롬프트가 흘리는 라벨은 전부 한글이다. 그래서 한글만 지운다.
#   그래도 지운 건 **반드시 로그로 드러낸다** — 조용히 지우면 프롬프트가 이상한 걸 뱉고
#   있다는 사실을 다시는 못 본다.
_STRAY_TAG_RE = re.compile(r"^\s*\[([가-힣][가-힣\s]{0,9})\]\s*")


# ⚠ **대괄호가 없는 경우**를 가르는 관측용(2026-08-12). 우리는 대사 원문을 로그에 안 찍기
#   때문에, 모델이 `[대화]` 를 뱉었는지 `대화.` 를 뱉었는지는 지금 **추론**이다. 전자는 위에서
#   막았지만 후자면 증상이 그대로 남는다 — 그래서 **지우지는 않고 세기만** 한다.
#   ⛔ 지우면 안 된다: "대화"는 정상 대사에도 나오는 낱말이다("대화 연습해 봐요").
#   ⛔ 임의 문자열을 찍지 않는다 — **프롬프트가 쓰는 라벨 낱말**에 걸릴 때만 남긴다.
_LABEL_WORDS = ("대화 모드", "공부 모드", "학습자 수준", "대화", "공부", "시스템")
_BARE_LABEL_RE = re.compile(
    r"^\s*(%s)\s*[.:,、·\-–]" % "|".join(map(re.escape, _LABEL_WORDS))
)


# ⛔ **잘린 태그**(2026-08-12 실측). 길이 상한이 태그 중간을 자른 원문을 봤다:
#     "… we can still have fun with Korean today. <happy"
#   지금은 '미완성 문장 버리기'가 우연히 이걸 같이 버려 소리로는 안 나간다. 그 방어가
#   **유일**하면 언젠가 새는 자리다 — 대괄호 사고와 같은 계열이라 여기서도 막는다
#   (안 지우면 TTS 가 "해피"라고 읽는다).
# ⚠ 끝에 붙은 것만 지운다. 문장 중간의 `<` 는 대사의 일부일 수 있다(부등호·이모티콘).
_DANGLING_TAG_RE = re.compile(r"<[A-Za-z]{0,12}$")


def read_bare_label(text: str) -> str | None:
    """대괄호 **없이** 라벨 낱말로 시작하나(`대화. ~~`). 관측 전용 — 지우지 않는다."""
    match = _BARE_LABEL_RE.match(text or "")
    return match.group(1) if match else None


def read_stray_tag(text: str) -> str | None:
    """대사 **맨 앞**의 집합 밖 한글 대괄호 토큰. 없으면(또는 아직 안 닫혔으면) None."""
    match = _STRAY_TAG_RE.match(text or "")
    if not match:
        return None
    tag = match.group(1).strip()
    return None if tag in EMOTION_STYLES else tag


def detect_emotion(text: str) -> str | None:
    """대사에서 **첫 번째** 감정 태그를 읽는다. 없거나 집합 밖이면 None(=기본 스타일).

    ⛔ 대답 1건에 **하나만** 쓴다 — 문장마다 바꾸면 구간이 더 쪼개져 TTS 호출이 늘고,
      지금 분당 상한이 10 인데 대답 하나가 이미 3~6회다(429 위험이 이 설계의 1순위 제약).
    """
    m = _EMOTION_TAG_RE.search(text or "")
    return m.group(1) if m else None


def strip_emotion_tags(text: str) -> str:
    """TTS 로 넘어가기 전에 태그를 **전부** 걷어낸다.

    ⛔ 태그가 소리로 나가면 안 된다 — `__마커__` 와 같은 급의 요구다. 오늘 `?` 를
      "쿼스천마크"로 읽던 사고가 정확히 이 계열이다.
    ⛔ **집합 밖 토큰도 맨 앞이면 걷어낸다**(`[대화]` 가 소리로 나갔다). 위 주석 참고 —
      감정으로 쓰지는 않되 소리로도 내보내지 않는다.
    """
    out = _EMOTION_TAG_RE.sub("", text or "")      # 집합 안: 어디에 있든
    out = _DANGLING_TAG_RE.sub("", out)            # 잘린 태그(`<happy`) — 끝에 붙은 것만
    while _STRAY_TAG_RE.match(out):                # 집합 밖: 맨 앞만(`[대화] [공부 모드] …`)
        out = _STRAY_TAG_RE.sub("", out, count=1)
    return out


def emotion_style(emotion: str | None) -> str | None:
    """감정 → 스타일 문구. 집합 밖·None 이면 None(호출부가 기본 스타일을 쓴다 — R5)."""
    return EMOTION_STYLES.get(emotion or "")


logger = logging.getLogger(__name__)

# 문장 분할 규칙(설계 §1-4). 종결부호·줄바꿈에서 끊고, 너무 짧으면 붙이고, 너무 길면 자른다.
_SENTENCE_END = "?!.。！？…"
# ⚠ 설계 초안은 12자였는데 **한국어 회화에는 길다** — "안녕하세요 반가워요."(11자) 같은
# 정상 문장이 다음 문장과 묶여 첫 소리가 그만큼 늦어진다. 초보 학습자에게 하는 말은 원래
# 짧으므로 8자로 낮춘다(그보다 짧은 "네." 류만 뒤와 붙인다).
_MIN_SENTENCE_CHARS = 8
_MAX_SENTENCE_CHARS = 120


class SentenceBuffer:
    """스트리밍 텍스트를 문장으로 끊어 내보낸다(순수 — LLM/TTS 를 모른다)."""

    __slots__ = ("_buf",)

    def __init__(self) -> None:
        self._buf = ""

    def push(self, piece: str) -> list[str]:
        """조각을 넣고, 끊긴 문장들을 순서대로 돌려준다."""
        out: list[str] = []
        self._buf += piece or ""
        while True:
            cut = self._find_cut()
            if cut <= 0:
                break
            sentence, self._buf = self._buf[:cut].strip(), self._buf[cut:].lstrip()
            if sentence:
                out.append(sentence)
        return out

    def flush(self) -> str:
        """남은 것(마지막 문장). 종결부호가 없어도 내보낸다 — 안 내보내면 말이 잘린다."""
        rest, self._buf = self._buf.strip(), ""
        return rest

    def _find_cut(self) -> int:
        for i, ch in enumerate(self._buf):
            if i + 1 < _MIN_SENTENCE_CHARS:
                continue
            if ch in _SENTENCE_END or ch == "\n":
                return i + 1
        if len(self._buf) >= _MAX_SENTENCE_CHARS:
            # 폭주 출력 방어 — 공백에서 끊어 운율이 덜 깨지게.
            space = self._buf.rfind(" ", 0, _MAX_SENTENCE_CHARS)
            return space + 1 if space >= _MIN_SENTENCE_CHARS else _MAX_SENTENCE_CHARS
        return 0


# 언어 마커 — 비버가 **타깃 언어로 말하는 부분**을 감싼다: "오늘은 __How are you?__ 를 배울까?"
# 따옴표를 안 쓰는 이유는 persona_prompt 가 이미 따옴표를 두 용도로 쓰기 때문이다(대사 인용 /
# 특정 표현만 타깃으로 들려주기). __ 는 자연 문장에 안 나오고 마크다운 굵게 문법이라 모델이 잘 지킨다.
MARKER = "__"


def split_by_language(text: str, base_lang: str, target_lang: str) -> list[tuple[str, str]]:
    """마커 경계로 잘라 [(문자열, 언어)] 로 만든다. 마커는 **여기서 사라진다**.

    폴백이 중요하다: 모델이 마커를 안 쓰면(또는 짝이 안 맞으면) 잘리지 않고 통째로
    base_lang 으로 나간다 — **마커 준수에 전부를 걸지 않는다.** 그 경우 문자 체계로
    고르는 판정을 얹을 수 있는데(후보가 둘뿐이라 판정이 아니라 고르기다), 지금은 그
    자리만 열어 두고 단순 폴백을 쓴다.
    """
    if not text:
        return []
    chunks = text.split(MARKER)
    if len(chunks) % 2 == 0:
        # 짝이 안 맞는다 = 모델이 규칙을 반만 지켰다. 자르지 않고 통째로 낸다(말이 사라지는
        # 것보다 낫다). 마커만 지운다.
        return [(text.replace(MARKER, "").strip(), base_lang)]
    out: list[tuple[str, str]] = []
    orphan = ""      # 아직 붙일 앞 구간이 없는 조각(첫 조각이 구두점일 때)
    for i, chunk in enumerate(chunks):
        piece = (orphan + chunk).strip()
        orphan = ""
        if not piece:
            continue
        if not _has_speech(piece):
            # ⛔ **구두점만 남은 조각을 단독 구간으로 만들지 않는다**(2026-08-08 실통화).
            #   "That's right! __맞아요__?" 를 쪼개면 마지막 조각이 "?" 하나가 되는데,
            #   그것만 TTS 에 보내면 문맥이 없어 **기호를 단어로 읽는다**("쿼스천 마크").
            #   앞 구간에 붙인다 — 앞이 없으면 다음 조각 앞에 붙인다.
            if out:
                prev_text, prev_lang = out[-1]
                # ⚠ 이러면 그 구두점은 **앞 구간의 언어로** 읽힌다. 구두점은 언어색이 옅어
                #   대개 무해하고, 단독으로 읽히는 것보다는 확실히 낫다.
                out[-1] = (f"{prev_text}{piece}", prev_lang)
            else:
                orphan = piece
            continue
        out.append((piece, target_lang if i % 2 else base_lang))
    if orphan and out:
        # 끝까지 붙일 곳을 못 찾은 조각(입력이 구두점으로 시작해 그 뒤가 전부 비었던 경우)
        out[0] = (f"{orphan}{out[0][0]}", out[0][1])
    return out


def _has_speech(text: str) -> bool:
    """소리 내어 읽을 **말**이 들어 있나 — 글자·숫자가 하나라도 있으면 발화다.

    ⚠ 길이로 자르면 안 된다. "네", "응", "Oh" 는 짧지만 **진짜 발화**다. 반대로 "?" 는
    길이가 같아도 발화가 아니다. 그래서 **문자 종류**로 가른다(isalnum 은 한글·라틴·숫자를
    모두 참으로 본다).
    """
    return any(ch.isalnum() for ch in text)


def strip_markers(text: str) -> str:
    """⭐ **마커를 지우는 유일한 지점.**

    비버 대사는 TTS 로만 가지 않는다:
      TTS      마커로 언어를 정하고 **지우고** 합성한다(split_by_language 가 한다)
      _history **남긴다** — LLM 이 자기 형식을 봐야 관행이 유지된다
      그 외    전사 저장·문장 추출·복습·화면 등 **전부 지운다**
    지금 캐스케이드는 DB 를 안 타지만 곧 탄다. 영속화를 붙이는 사람은 **이 함수를 쓰면 된다** —
    찾아 헤매지 않게 한 곳에 모아 둔다.
    """
    return (text or "").replace(MARKER, "")


async def sample_aligned(pcm_stream: AsyncIterator[bytes],
                         stats: dict | None = None) -> AsyncIterator[bytes]:
    """조각 경계를 **표본 경계로** 맞춘다 — 홀수 꼬리는 다음 조각으로 이월한다.

    ⭐ **왜 여기인가**(2026-08-11). 이월은 "한 합성 요청 안에서"만 옳다:
      · 어댑터 안에서 고치면 **다음 어댑터가 또 홀수를 낸다**(같은 사고를 세 번째로 겪는다).
      · 송출 경계(`BeaverOutput.send`)는 조각의 **연속성을 모른다** — 거기서 이월하면 문장
        A 의 남은 반 표본이 문장 B(다른 요청)의 첫 조각에 붙어 **B 전체가 1바이트 밀린다.**
      · `speak_stream` 은 **요청 하나의 시작과 끝**을 아는 유일한 자리다. 그래서 이월도,
        끝에 남은 1바이트를 버리는 것도 여기서만 안전하다.
    ⚠ 그래도 송출 경계에 불변식(I6)을 하나 더 둔다 — 이 함수를 안 타는 경로가 생겨도
      클라로는 홀수가 못 나가게. 두 겹인 이유는 **조용한 실패를 다시는 만들지 않기** 위해서다.
    """
    carry = b""
    async for chunk in pcm_stream:
        if not chunk:
            continue
        if len(chunk) % 2 and stats is not None:
            # ⭐ **몇 개나 홀수였나**를 세어 둔다. 이 숫자가 0 이 아니면 벤더가 표본 경계를
            #   안 지킨다는 뜻이고, 그게 보여야 다음에 같은 증상을 **듣기 전에** 안다.
            stats["odd"] = stats.get("odd", 0) + 1
        out, carry = align_pcm16(chunk, carry)
        if out:
            yield out
    if carry:
        # 스트림이 끝났는데 반 표본이 남았다 = 벤더가 홀수 길이를 냈다. 여기서는 버려도 된다
        # (0.02ms, 뒤에 이어질 바이트가 없다). ⚠ 흔한 일이 아니므로 흔적은 남긴다.
        logger.info("tts 스트림 끝에 반 표본 1바이트 — 버린다(길이 홀수)")


async def speak_stream(beaver: Any, pcm_stream: AsyncIterator[bytes], text: str,
                       trim_tail: bool = False) -> int:
    """한 문장의 오디오를 송출한다. **이름표는 마지막 조각에** 붙는다. 보낸 바이트 수 반환.

    한 조각 앞서 보내는 이유: 마지막 조각이 어느 것인지는 다음 조각이 와 봐야 안다.

    ⭐ `trim_tail` — **마지막 조각의 뒤쪽 침묵**을 잘라낸다(2026-08-10). 어차피 그 조각은
      위 이유로 이미 손에 들고 있으므로 **지연이 0** 이다. 새로 버퍼를 만들지 않는다.
      ⚠ 한계: 꼬리 침묵이 마지막 조각보다 길면 그 앞 조각의 몫은 못 자른다. 더 자르려면
        조각을 더 붙들어야 하는데 그건 **첫소리를 늦추는 대가**라 하지 않는다.
    """
    sent = 0
    pending: bytes | None = None
    async for chunk in sample_aligned(pcm_stream):
        if not chunk:
            continue
        if pending is not None:
            await beaver.send(pending, "")
            sent += len(pending)
        pending = chunk
    if pending is not None:
        if trim_tail:
            pending = trim_silence_edges(pending, head=False)
        await beaver.send(pending, text)   # 이 문장을 끝까지 들었으면 이력에 남는다
        sent += len(pending)
    return sent
