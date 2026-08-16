#!/usr/bin/env python
"""통화별 **컨텍스트·원가 계기판** — "몇 분 통화가 얼마인가"를 한 화면에.

왜 만들었나(2026-08-16): 계기판(usage_* 컬럼·usage_json)은 이미 다 쌓이고 있는데
**읽는 도구가 없어서** 매번 임시 스크립트를 썼다. 그래서 같은 질문에 매번 다른
숫자가 나왔다. 여기 하나로 모은다.

⛔ 원가는 **estimate_call_cost_usd 하나로만** 계산한다(직접 곱셈 금지 — CLAUDE.md).
   Live 와 캐스케이드는 같은 컬럼이 다른 단가라, 엔진을 모르고 계산하면 틀린 값이
   조용히 나온다.

⚠ **어느 DB 를 보는가** — 이 프로젝트의 함정이다.
   `.env` = 운영 / `.env.local` = 로컬 dev 로 **이름과 반대**이고, config.py 는
   ('.env','.env.local') 순으로 읽어 **뒤가 이긴다**. 그래서 그냥 import 하면
   로컬 dev DB 를 본다 — 그런데 **앱(app-api/demo-api)이 쓰는 건 .env 쪽**이다.
   ⇒ 기본을 운영(.env)으로 잡고, **어느 DB 를 봤는지 항상 출력한다.**
   로컬 dev DB 를 보려면 --local.

사용법:
    conda run -n beavertalk-server python scripts/dev_call_cost.py
    ... --email test@naver.com          # 그 계정만
    ... --member 4 --limit 30
    ... --local                          # .env.local(로컬 dev DB)
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
    """.env 를 찾을 곳 — 이 체크아웃, 그 다음 **본 체크아웃**.

    ⚠ git worktree 에는 `.env` 가 없다(gitignore 라 복제되지 않는다). 워크트리에서
      실행해도 돌게 하려고 `--git-common-dir` 로 본 체크아웃을 찾아 거기도 본다.
    """
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
        pass  # git 이 없어도 첫 후보로 돈다(R5 — 도구가 죽지 않는다)
    return out


PROD_SERVICE = "beavertalk-app-api"
GCP_PROJECT = "bt-dev-web-01"
GCP_REGION = "asia-northeast3"


def _live_ctx_settings(settings) -> tuple[int, int, str]:
    """압축 문턱의 **실제 소스**를 읽는다 — Cloud Run env 가 이긴다.

    ⛔ 이 함수가 없던 동안 이 스크립트는 `.env` 파일 값(16000/12000)을 찍었는데
       prod 는 Cloud Run env 로 8000/7000 을 돌고 있었다. **계기판이 거짓말을 했다.**
       원인은 우선순위를 잊은 것이다 — Cloud Run env > dotenv 파일.
    ⚠ gcloud 가 없거나 권한이 없으면 로컬 값으로 폴백하되, **출처를 반드시 밝힌다**(R5).
    """
    local = (settings.LIVE_CTX_TRIGGER_TOKENS, settings.LIVE_CTX_TARGET_TOKENS)
    try:
        import json
        import subprocess
        # ⚠ Windows 에서 gcloud 는 `gcloud.cmd` 다 — 확장자 없이 부르면 FileNotFound 로
        #   조용히 폴백해 **또 거짓 숫자를 찍는다**(이 함수를 만든 이유가 그거였다).
        exe = "gcloud"
        if sys.platform == "win32":
            import shutil
            exe = shutil.which("gcloud") or shutil.which("gcloud.cmd") or "gcloud.cmd"
        raw = subprocess.run(
            [exe, "run", "services", "describe", PROD_SERVICE,
             "--project", GCP_PROJECT, "--region", GCP_REGION, "--format", "json"],
            capture_output=True, text=True, timeout=60,
            shell=(sys.platform == "win32"),
        )
        if raw.returncode != 0:
            return (*local, "로컬 .env — ⚠ gcloud 실패, prod 실제값과 다를 수 있다")
        env = {
            e["name"]: e.get("value")
            for e in json.loads(raw.stdout)["spec"]["template"]["spec"]
            ["containers"][0].get("env", [])
        }
        t = env.get("LIVE_CTX_TRIGGER_TOKENS")
        g = env.get("LIVE_CTX_TARGET_TOKENS")
        if t and g:
            return int(t), int(g), f"Cloud Run {PROD_SERVICE} (실제 운영값)"
        return (*local, f"코드 기본값 — {PROD_SERVICE} 에 env 미설정")
    except Exception:
        return (*local, "로컬 .env — ⚠ gcloud 조회 불가, prod 실제값과 다를 수 있다")


def _force_env(local: bool) -> str:
    """읽을 DB 를 **명시적으로** 고정한다(환경변수가 dotenv 를 이긴다).

    ⛔ 이 한 걸음을 빼면 스크립트가 조용히 다른 DB 를 본다. 그 사고가 실제로 있었다.
    """
    name = ".env.local" if local else ".env"
    path = next((p for p in _env_candidates(name) if p.exists()), None)
    if path is None:
        tried = " / ".join(str(p) for p in _env_candidates(name))
        sys.exit(f"⛔ {name} 을 못 찾았다. 찾아본 곳: {tried}")
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*DATABASE_URL_(POOL|DIRECT)\s*=\s*(.+?)\s*$", line)
        if m:
            os.environ[f"DATABASE_URL_{m.group(1)}"] = m.group(2).strip().strip("\"'")
    url = os.environ.get("DATABASE_URL_POOL", "")
    host = re.sub(r"://[^@]*@", "://***@", url)
    return f"{path.name} → {host[:80]}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email")
    ap.add_argument("--member", type=int)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--local", action="store_true", help="로컬 dev DB(.env.local)")
    args = ap.parse_args()

    which = _force_env(args.local)

    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session

    import db.registry  # noqa: F401 — 관계 문자열 해석에 전 모델 등록이 필요하다
    from core.config import settings
    from domains.account.models.member import Member
    from domains.learning.models.call import Call
    from domains.learning.service.normalcall_service import estimate_call_cost_usd

    print(f"DB: {which}")
    trigger, target, src = _live_ctx_settings(settings)
    print(f"압축 설정: trigger={trigger} target={target}   [출처: {src}]\n")

    # 읽기 전용 조회 — dev_inspect_call.py 와 같은 관례(직결 5432).
    db = Session(create_engine(settings.direct_url, poolclass=None))
    try:
        q = select(Call).order_by(Call.call_id.desc())
        if args.email:
            mid = db.execute(
                select(Member.member_id).where(Member.email == args.email)
            ).scalar_one_or_none()
            if mid is None:
                sys.exit(f"⛔ {args.email} 없음")
            print(f"member {mid}  ({args.email})\n")
            q = q.where(Call.member_id == mid)
        elif args.member:
            q = q.where(Call.member_id == args.member)
        calls = list(db.execute(q.limit(args.limit)).scalars())

        if not calls:
            print("통화 없음")
            return

        hdr = (f"{'call':>5} {'날짜':<16} {'길이':>7} {'엔진':<9} "
               f"{'최대컨텍스트':>12} {'압축':>4} {'재연결':>6} "
               f"{'총토큰':>9} {'원가$':>9} {'분당$':>8}")
        print(hdr)
        print("-" * len(hdr))

        buckets: dict[int, list[float]] = {}
        for c in reversed(calls):
            uj = c.usage_json or {}
            # 길이: total_time(초) 우선, 없으면 usage_json.t_last(마지막 usage 수신 시각).
            # ⚠ t_last 는 '마지막 과금 메시지까지'라 실제 통화보다 **짧게** 나온다.
            secs = float(c.total_time or 0) or float(uj.get("t_last") or 0)
            cost, unknown = estimate_call_cost_usd(
                c.usage_engine,
                in_audio=c.usage_in_audio or 0, in_text=c.usage_in_text or 0,
                out_audio=c.usage_out_audio or 0, out_text=c.usage_out_text or 0,
                usage_json=uj,
            )
            engine = (c.usage_engine or "live?").split(":")[0]
            per_min = cost / (secs / 60) if secs else 0.0
            date = c.call_date.strftime("%m-%d %H:%M") if c.call_date else "-"
            # ⛔ 캐스케이드는 컨텍스트·총토큰 컬럼을 **안 쓴다**(LLM 토큰만 in_text/out_text).
            #   그런데 NULL 이 0 으로 찍히면 "컨텍스트가 0이다"라는 **거짓 숫자**가 된다.
            #   Live 만 이 칸을 채운다 — 나머지는 잰 적 없음을 '-' 로 드러낸다.
            is_live = not (c.usage_engine or "").startswith("cascade:")
            peak = (c.usage_peak_prompt or 0) if is_live else None
            # ⭐ 압축 문턱에 닿았는지를 눈으로 — 안 닿으면 압축은 영원히 안 돈다.
            flag = ("" if (peak or 0) < settings.LIVE_CTX_TRIGGER_TOKENS
                    else " ⚠트리거초과")
            # ⚠ 계기판 이전 통화는 이 값들이 NULL 이다 — 없는 걸 '-' 로 **보이게** 둔다
            #   (0 으로 채우면 "압축이 0번 돌았다"와 "잴 수 없다"가 구별되지 않는다).
            comp = uj.get("compressions")
            recon = uj.get("reconnects")
            peak_s = "-" if peak is None else f"{peak:,}"
            total_s = f"{c.usage_total:,}" if (is_live and c.usage_total) else "-"
            print(f"{c.call_id:>5} {date:<16} {secs:>6.0f}s {engine:<9} "
                  f"{peak_s:>12}{flag} "
                  f"{('-' if comp is None else str(comp)):>4} "
                  f"{('-' if recon is None else str(recon)):>6} "
                  f"{total_s:>9} {cost:>9.4f} {per_min:>8.4f}")
            if unknown:
                print(f"      ⚠ 단가 미상: {', '.join(unknown)}")
            if secs > 0:
                buckets.setdefault(max(1, round(secs / 60)), []).append(cost)

        print("\n=== 분 단위 집계 (반올림한 통화 길이별) ===")
        print(f"{'길이':>5} {'통화수':>6} {'평균원가$':>11} {'분당$':>9}")
        for m in sorted(buckets):
            v = buckets[m]
            avg = sum(v) / len(v)
            print(f"{m:>4}분 {len(v):>6} {avg:>11.4f} {avg / m:>9.4f}")

        print("\n⚠ 원가는 estimate_call_cost_usd 한 곳에서만 나온다(직접 곱셈 없음).")
        print("⚠ '최대컨텍스트'가 trigger 미만이면 압축은 **한 번도 안 돈다**"
              " — compressions=0 이 그 증거다.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
