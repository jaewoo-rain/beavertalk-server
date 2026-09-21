"""취약 발음 학습 TTS 미리 굽기 — 고정 콘텐츠를 한 번만 합성해 저장한다.

실행:
    python scripts/pregen_sound_audio.py --dry-run    # 무엇을 구울지만 센다(합성 0)
    python scripts/pregen_sound_audio.py              # 빠진 것만 굽는다
    python scripts/pregen_sound_audio.py --force      # 이미 있어도 다시 굽는다

## 왜 필요한가

`POST /tts/speech` 는 아무것도 저장하지 않는다. 같은 문장을 열 번 요청하면 열 번 합성하고
열 번 과금된다. 앱 캐시는 메모리 32개라 앱을 끄면 사라진다 ⇒ **앱을 켤 때마다 전량 재합성**
이었다.

이 기능의 문장은 고정 콘텐츠다 — 30과 × 9개 = 고유 문장 233개, 1,070자.
한 번 구우면 그 뒤로는 0원이다.

## ⛔ 기본 음성 고정 — 캐릭터 음색이 아니다

발음 학습은 따라 할 **본보기**다. 사람마다 다른 목소리로 나오면 기준이 되지 않는다.
그래서 `voice=None`(언어 기본 음성, 한국어는 Aoede)으로 굽는다.
사장님 지시(2026-09-21) — 「발음 학습은 이전에 한 것처럼 기본적인 TTS로」.

## ⛔ 저장하는 것은 object key 다

서명 URL 을 DB 에 넣으면 7일 뒤 만료되고 그 과는 영구히 소리가 죽는다(2026-08-31 실사고).
`core/storage.playback_url()` 이 읽을 때마다 새로 서명한다.

## 멱등

`(text_hash, voice, engine)` 로 이미 있으면 건너뛴다. 중간에 죽어도 다시 돌리면
빠진 것만 채운다 — 한 문장 합성할 때마다 커밋한다.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

import db.registry  # noqa: F401  (전 모델 등록)
from core import storage, tts
from core.config import settings
from db.engine import build_engine
from db.session import build_session_factory
from domains.learning.models.sound_audio import (
    LESSON_ENGINE as ENGINE,
    LESSON_LANGUAGE as LANGUAGE,
    LESSON_VOICE as VOICE,
    SoundAudio,
    audio_text_hash as text_hash,
)

_ASSETS = Path(__file__).resolve().parent.parent / "assets" / "pronunciation"


def collect_texts() -> list[str]:
    """lessons.json 에서 **재생되는 문장 전량**을 순서대로, 중복 없이 모은다.

    단어 4 + 문장 1 + 청크 n + 평가 1. 청크는 끝에서부터 쌓기(백체이닝)라 마지막 청크가
    문장 본문과 같을 수 있다 — 중복은 여기서 접는다.
    """
    import json

    lessons = json.loads((_ASSETS / "lessons.json").read_text(encoding="utf-8"))["lessons"]
    seen: dict[str, None] = {}
    for l in lessons:
        for w in l.get("words") or []:
            t = (w.get("text") or "").strip()
            if t:
                seen.setdefault(t, None)
        s = l.get("sentence") or {}
        for t in [*(s.get("chunks") or []), s.get("text")]:
            t = (t or "").strip()
            if t:
                seen.setdefault(t, None)
        t = ((l.get("test") or {}).get("text") or "").strip()
        if t:
            seen.setdefault(t, None)
    return list(seen)


async def pregen(
    session, texts: list[str], force: bool, *, quiet: bool = False
) -> tuple[int, int, int]:
    """→ (구운 수, 건너뛴 수, 실패 수). 한 건마다 커밋해 중단에 견딘다.

    `quiet=True` 면 진행 출력을 하지 않는다 — 서버(`/__dev/pregen-sound-audio`)에서
    부를 때 쓴다. 로컬 자격증명이 없어 스크립트를 못 돌리는 환경이 있어서, 굽는 일은
    **배포된 서비스가 자기 자격증명으로** 할 수 있어야 한다.
    """
    made = skipped = failed = 0
    for i, text in enumerate(texts, start=1):
        h = text_hash(text)
        row = session.scalar(
            select(SoundAudio).where(
                SoundAudio.text_hash == h,
                SoundAudio.voice.is_(None) if VOICE is None else SoundAudio.voice == VOICE,
                SoundAudio.engine == ENGINE,
            )
        )
        if row is not None and not force:
            skipped += 1
            continue

        synthesized = await tts.synthesize(text, LANGUAGE, voice=VOICE, engine=ENGINE)
        if not synthesized:
            if not quiet:
                print(f"  [{i}/{len(texts)}] 합성 실패: {text}")
            failed += 1
            continue
        audio, content_type = synthesized

        ext = "mp3" if content_type == "audio/mpeg" else "wav"
        # key 에 해시를 쓴다 — 한글 문장을 경로에 넣으면 인코딩 문제가 따라온다.
        path = f"sound-lesson/{ENGINE}/{h}.{ext}"
        key = storage.upload(settings.SUPABASE_BUCKET_SAMPLES, path, audio, content_type)
        if not key:
            if not quiet:
                print(f"  [{i}/{len(texts)}] 업로드 실패: {text}")
            failed += 1
            continue

        if row is None:
            session.add(SoundAudio(
                text_hash=h, text=text, voice=VOICE, engine=ENGINE, object_key=key,
            ))
        else:
            row.text = text
            row.object_key = key
        session.commit()
        made += 1
        if not quiet:
            print(f"  [{i}/{len(texts)}] {text}  →  {key}")
    return made, skipped, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="셀 것만 세고 합성하지 않는다")
    ap.add_argument("--force", action="store_true", help="이미 있어도 다시 굽는다")
    args = ap.parse_args()

    texts = collect_texts()
    chars = sum(len(t.replace(" ", "")) for t in texts)
    print(f"대상 문장 {len(texts)}개 · 공백 제외 {chars}자 · 엔진 {ENGINE} · 음색 {VOICE or '(언어 기본)'}")
    if args.dry_run:
        return 0

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    with session_factory() as session:
        made, skipped, failed = asyncio.run(pregen(session, texts, args.force))
    print(f"구움 {made} · 건너뜀 {skipped} · 실패 {failed}")
    # 실패가 있으면 0 이 아니다 — CI·수동 실행 어느 쪽에서도 조용히 넘어가면 안 된다.
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
