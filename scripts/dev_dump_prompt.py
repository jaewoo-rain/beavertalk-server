#!/usr/bin/env python
"""통화를 걸지 않고 **비버에게 실제로 갈 지시문을 그대로 찍는다**.

왜 만들었나(2026-08-16): "오늘 항목이 몇 개 들어가나 / 프롬프트가 제대로 조립되나"를
확인하려고 매번 **실제 통화를 걸었다.** 통화는 돈이 들고(라이브 5분 ≈ $0.25),
마이크가 필요하고, 로그를 뒤져야 한다. 그런데 이 값들은 전부 **통화 시작 전에**
서버가 DB 만 보고 정하는 것이라, 통화 없이 그대로 재현할 수 있다.

⛔ 통화 경로와 **같은 함수**를 쓴다(`load_call_setup` → `build_system_instruction`).
   여기서 따로 조립하면 "덤프는 맞는데 실제는 다른" 상태가 되고, 그게 제일 나쁘다.

⚠ 읽기 전용이다 — 통화 행을 만들지 않고 commit 하지 않는다.
⚠ 어느 DB 를 보는지 항상 출력한다(.env=운영 / .env.local=로컬, 이름과 반대다).

사용법:
    conda run -n beavertalk-server python scripts/dev_dump_prompt.py --email test@naver.com
    ... --member 4 --language en
    ... --full            # 지시문 전문(기본은 오늘의 공부 항목 블록만)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _env_candidates(name: str) -> list[Path]:
    """이 체크아웃 → 본 체크아웃. worktree 에는 .env 가 없다(gitignore)."""
    out = [ROOT / name]
    try:
        import subprocess
        common = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--path-format=absolute",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if common:
            out.append(Path(common).parent / name)
    except Exception:
        pass
    return out


def _force_env(local: bool) -> str:
    name = ".env.local" if local else ".env"
    path = next((p for p in _env_candidates(name) if p.exists()), None)
    if path is None:
        sys.exit(f"⛔ {name} 을 못 찾았다: {' / '.join(map(str, _env_candidates(name)))}")
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*DATABASE_URL_(POOL|DIRECT)\s*=\s*(.+?)\s*$", line)
        if m:
            os.environ[f"DATABASE_URL_{m.group(1)}"] = m.group(2).strip().strip("\"'")
    url = os.environ.get("DATABASE_URL_POOL", "")
    return f"{path.name} → {re.sub(r'://[^@]*@', '://***@', url)[:80]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email")
    ap.add_argument("--member", type=int)
    ap.add_argument("--character", type=int, help="미지정이면 아무 캐릭터 1개")
    ap.add_argument("--language", default=None,
                    help="미지정이면 member.target_language(통화의 단일 소스)")
    ap.add_argument("--full", action="store_true", help="지시문 전문")
    ap.add_argument("--local", action="store_true")
    args = ap.parse_args()

    which = _force_env(args.local)

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    import db.registry  # noqa: F401 — 관계 문자열 해석
    from core.config import settings
    from core.persona_prompt import build_system_instruction
    from domains.account.models.member import Member
    from domains.commerce.models.character import Character
    from domains.learning.service import normalcall_service as svc

    print(f"DB: {which}")
    sess = Session(create_engine(settings.direct_url, poolclass=None))
    try:
        if args.email:
            m = sess.scalar(select(Member).where(Member.email == args.email))
        elif args.member:
            m = sess.get(Member, args.member)
        else:
            sys.exit("⛔ --email 또는 --member 가 필요하다")
        if m is None:
            sys.exit("⛔ 회원 없음")

        # ⛔ **`member.target_language` 가 통화 언어의 단일 소스다**(call_session.py:972).
        #   `member.language` 는 다른 컬럼이다 — 이걸 쓰면 덤프가 실제 통화와 어긋난다.
        #   실제로 한 번 어긋났다: language=en / target_language=ko 인 계정을 영어로 뽑아
        #   "레벨 3"으로 보고했는데, 실제 통화는 한국어 레벨 1이었다. 기본값을 못박는다.
        language = args.language or (m.target_language or "ko")
        char_id = args.character or sess.scalar(select(Character.character_id).limit(1))
        print(f"member={m.member_id} email={m.email} character={char_id}")
        print(f"  target_language={m.target_language!r}  ← 통화가 쓰는 값 (단일 소스)")
        print(f"  language={m.language!r}  (별개 컬럼 — 통화 언어가 아니다)")
        if args.language and args.language != m.target_language:
            print(f"  ⚠ --language {args.language} 로 덮어썼다 — 실제 통화는 "
                  f"{m.target_language!r} 로 돈다")

        setup = svc.load_call_setup(sess, m.member_id, char_id, language)

        items = setup.get("study_items") or []
        main_n = sum(1 for it in items if it.get("slot") != "reserve")
        res_n = len(items) - main_n
        print(f"\n=== 오늘의 항목 {len(items)}개  (본편 {main_n} · 예비 {res_n}) ===")
        if not items:
            # ⚠ 0 은 정상일 수도(레벨 미확정) 사고일 수도(재료 소진) 있다 — 구별해 준다.
            reason = ("korean_level 미확정 — 레벨테스트로 라우팅된다"
                      if setup.get("needs_level_test")
                      else "⛔ 레벨은 있는데 재료가 0 — 갇힘 사고를 의심해라")
            print(f"  (없음) {reason}")
        for i, it in enumerate(items, 1):
            print(f"  {i:>2}. [{it.get('slot'):<7}][{it.get('kind'):<7}] {it.get('obj')}")

        instruction = build_system_instruction(
            role=setup["role"], personality=setup["personality"],
            level_profile=setup["level_profile"], locale=setup["locale"],
            interests=setup["interests"], name=setup["name"],
            history=setup["history"], target_language=language,
            study_items=setup.get("study_items"),
            known_items=setup.get("known_items"),
            recent_topics=setup.get("recent_topics"),
            promotion_notice=bool(setup.get("promotion_notice")),
            lang_band=setup.get("lang_band", "beginner"),
        )
        print(f"\n지시문 {len(instruction):,}자")
        if args.full:
            print("\n" + "=" * 70 + "\n" + instruction)
        else:
            # 공부 항목 블록만 — 대개 이것만 보면 된다.
            # ⛔ 그냥 "[오늘의 공부 항목" 을 찾으면 **불변식 템플릿이 그 블록을 언급하는
            #   문장**("블록이 있으면 …")에 먼저 걸린다. 실제 블록은 헤더 전문으로 찾는다.
            start = instruction.find("[오늘의 공부 항목 — 공부 모드일 때만")
            if start < 0:
                print("\n⚠ [오늘의 공부 항목] 블록이 지시문에 없다.")
            else:
                end = instruction.find("\n[", start + 1)
                print("\n" + "=" * 70)
                print(instruction[start: end if end > 0 else len(instruction)])
        print("\n⚠ 읽기 전용 — 통화 행을 만들지 않았고 원가도 0이다.")
    finally:
        sess.close()


if __name__ == "__main__":
    main()
