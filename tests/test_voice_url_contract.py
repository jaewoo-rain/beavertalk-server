"""voice_url 계약 — **저장은 object key, 응답은 지금 서명한 URL**.

왜 이 파일이 있나(2026-08-31 실측):
    `core/storage.py` 의 계약은 「DB 엔 key 만 담고 재생 URL 은 매 요청 조립」이다.
    응답을 만드는 자리가 여러 곳인데 **한 곳만 빠져도 그 화면만 조용히 죽는다.**
    실제로 `SentenceOut(` 생성자를 고치고도 `model_validate` 계열을 놓쳐,
    학습 결과 화면의 「원어민」이 08-14 서명 URL 을 그대로 받고 있었다.
    flutter_sound 는 만료 URL 에서 완료도 예외도 내지 않아 **무음·무반응**이었다 —
    스낵바조차 안 떠서 눈으로는 「버튼이 죽은 것」과 구분되지 않는다.

    그래서 화면별 테스트가 아니라 **소스 전수 검사**를 방어선으로 둔다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SERVICE_DIR = Path(__file__).resolve().parent.parent / "domains" / "learning" / "service"
_FILES = sorted(p.name for p in _SERVICE_DIR.glob("*.py"))

# voice_url 을 담아 **응답으로 나가는** 스키마. ORM 모델은 제외한다 —
# 그쪽은 key 를 그대로 들고 있는 것이 정상이다.
_RESPONSE_SCHEMAS = (
    "SentenceOut",
    "CallResultSentence",
    "RawDataOut",
    "ReviewOut",
    "ReviewFeedback",
    "SentenceTtsOut",
)

# 조립을 거쳤다고 인정하는 호출.
_ASSEMBLERS = ("playback_url", "_playback_url", "_sample_url", "_recording_url")

# 저장값을 그대로 넘기는 것이 **옳은** 자리(ORM 행 복사·DB 쓰기). 다른 곳에서
# 같은 모양이 나오면 실패한다.
_ALLOWED_RAW = {
    # call_service: 재분석용 통화 복제 — 새 ORM 행에 key 를 그대로 옮긴다.
    "voice_url=s.voice_url,",
    "CallRawData(content=r.content, voice_url=r.voice_url, total_time=r.total_time)",
    # review_service: DB 에 저장하는 값(=key). 응답이 아니다.
    "voice_url=data.voice_url,  # object key(또는 폴백 경로)",
}


def _src(name: str) -> str:
    return (_SERVICE_DIR / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _FILES)
def test_stored_voice_url_is_not_emitted_verbatim(name: str) -> None:
    """`voice_url=<orm>.voice_url` 을 조립 없이 쓰는 자리를 막는다."""
    offenders = []
    for lineno, line in enumerate(_src(name).splitlines(), 1):
        stripped = line.strip()
        if not re.search(r"voice_url\s*=\s*[A-Za-z_][\w.]*\.voice_url", stripped):
            continue
        if any(a in stripped for a in _ASSEMBLERS) or stripped in _ALLOWED_RAW:
            continue
        offenders.append(f"{name}:{lineno} {stripped}")
    assert not offenders, (
        "저장값(object key)을 그대로 응답에 넣고 있다 — storage.playback_url 로 "
        f"조립해라. 의도한 ORM 복사면 _ALLOWED_RAW 에 올려라: {offenders}"
    )


@pytest.mark.parametrize("name", _FILES)
def test_model_validate_overrides_voice_url(name: str) -> None:
    """`Schema.model_validate(orm)` 은 저장값을 그대로 옮긴다 — 반드시 갈아 끼운다.

    이걸 빠뜨린 것이 2026-08-31 결함의 직접 원인이었다.
    """
    src = _src(name)
    for schema in _RESPONSE_SCHEMAS:
        for m in re.finditer(rf"\b{schema}\.model_validate\(", src):
            window = src[m.start(): m.start() + 300]
            assert any(a in window for a in _ASSEMBLERS), (
                f"{name}: {schema}.model_validate 뒤에 voice_url 조립이 없다 — "
                "저장된 서명 URL 이 만료된 채로 나간다."
            )
