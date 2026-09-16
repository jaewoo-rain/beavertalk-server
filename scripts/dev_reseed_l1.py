"""L1 3차시 청크 배치 재시드 — 배포된 서버의 `/__dev/cur-reseed-l1` 을 부른다(기본 **dry-run**).

왜 있나: 배치 수정(2026-09-16)이 DB 에 닿으려면 재적재가 필요한데 `load_cur_seed.py` 는
`DATABASE_URL_DIRECT` 를 요구한다. 그 비밀값 없이도 되게, 이미 DB 를 들고 있는 **배포된 서버**에
시킨다(계획 docs/20260916_1620_L1-청크-차시-재배치-플랜.md §4).

사용:
    # 1) 무엇이 바뀌는지만 본다 — 쓰기 0
    E2E_PASSWORD=... PYTHONIOENCODING=utf-8 conda run -n beavertalk-server \
        python scripts/dev_reseed_l1.py --email you@example.com

    # 2) 실제로 쓴다
    ... python scripts/dev_reseed_l1.py --email you@example.com --apply

    # 3) 되돌린다(옛 15/15/16 배치)
    ... python scripts/dev_reseed_l1.py --email you@example.com --layout legacy --apply

⚠ demo-api 와 실서비스는 **같은 Supabase 프로젝트**다(CLAUDE.md 운영 §). --apply 는 실제 커리큘럼을 바꾼다.
⛔ 비밀번호 리터럴 금지(공개 저장소) — E2E_PASSWORD env 또는 --password.
⛔ 403 이 오면 그 계정이 admin 이 아니다(`member.role`). 승격 자체가 DB 쓰기라 최초 1회는 사람이 해야 한다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

DEFAULT_BASE = "https://beavertalk-app-demo-api-333511894671.asia-northeast3.run.app"


def _token(base: str, email: str, password: str) -> str:
    import httpx

    r = httpx.post(f"{base}/__dev/signup", json={"email": email, "password": password}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"⛔ 로그인 실패 {r.status_code}: {r.text[:200]}")
    return r.json()["access_token"]


def _diff(before: list[str], after: list[str]) -> tuple[list[str], list[str]]:
    """빠진 것 / 들어온 것(순서 변경은 세지 않는다)."""
    b, a = set(before), set(after)
    return sorted(b - a), sorted(a - b)


def main() -> int:
    import httpx

    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BT_BASE") or DEFAULT_BASE)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", default=os.environ.get("E2E_PASSWORD"))
    ap.add_argument("--layout", default="new", choices=["new", "legacy"])
    ap.add_argument("--apply", action="store_true", help="실제로 쓴다(기본은 dry-run·쓰기 0)")
    a = ap.parse_args()
    if not a.password:
        sys.stderr.write("⛔ 비밀번호가 없다 — E2E_PASSWORD env 또는 --password 로 줘라(코드에 리터럴 금지).\n")
        return 1

    token = _token(a.base, a.email, a.password)
    r = httpx.post(
        f"{a.base}/__dev/cur-reseed-l1",
        headers={"Authorization": f"Bearer {token}"},
        json={"layout": a.layout, "dry_run": not a.apply},
        timeout=120,
    )
    if r.status_code == 403:
        print("⛔ 403 — 이 계정은 admin 이 아니다(member.role). 최초 승격은 DB 작업이라 사람이 해야 한다.")
        return 1
    if r.status_code != 200:
        print(f"⛔ {r.status_code}: {r.text[:500]}")
        return 1

    out = r.json()
    print(f"배치={out['layout']}  dry_run={out['dry_run']}  커밋={out['committed']}")
    for w in out.get("warnings") or []:
        print("  ⚠ " + w)
    for l in out["lessons"]:
        gone, came = _diff(l["items"]["before"], l["items"]["after"])
        print(f"\n[{l['code']}] 바뀜={l['changed']}  {l['count']['before']}개 → {l['count']['after']}개")
        print(f"  상황: {l['situation']['before']} → {l['situation']['after']}")
        print(f"  상대: {l['partner']['before']} → {l['partner']['after']}")
        print("  빠짐: " + (" · ".join(gone) if gone else "없음"))
        print("  들어옴: " + (" · ".join(came) if came else "없음"))
        print("  최종 순서: " + " · ".join(l["items"]["after"]))
    if out["dry_run"]:
        print("\n— dry-run 이라 아무것도 쓰지 않았다. 실제로 쓰려면 --apply.")
    else:
        print("\n✅ 커밋됨. 되돌리려면 --layout legacy --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
