"""[dev] 다조각 통화의 total_time·usage 를 **서버 로그**로 백필한다(2026-09-14 ①, 실통화 1602·1604).

왜: 2026-09-14 이전 서버는 조각마다 finalize_call/save_call_usage 가 **덮어써** 마지막(또는 첫) 조각 값만 남았다. 로그에는 조각마다
  «normalcall: 저장 완료 call_id=N segments=… duration=Ds» 와 «normalcall usage: call_id=N … msgs=… sum_total=… sum_in=AUDIO=a,TEXT=t sum_out=…»
  가 한 줄씩 남아 있으므로 그걸 합쳐 되돌려 놓는다. 로그가 없으면(보존 기간 지남·조회 실패) 그 통화는 **건너뛴다**.

사용법(운영 체크아웃 .env = 운영 DB, DATABASE_URL_DIRECT):
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_backfill_fragment_totals.py 1602 1604            # dry-run(기본): 계산만 출력
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dev_backfill_fragment_totals.py 1602 1604 --apply    # 실제 UPDATE(사장님 허락 뒤 bt-back)
    --log-file PATH : gcloud 대신 미리 받아 둔 로그(텍스트, 한 줄 = 한 로그) 를 쓴다. --project / --freshness(기본 7d) 는 gcloud logging read 인자.

⛔ 새 서버(2026-09-14 ①)가 저장한 통화(usage_json.fragments 있음)는 이미 누적이라 건너뛴다 — 두 번 더하지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from core.config import settings  # noqa: E402

_SAVED_RE = re.compile(r"normalcall: 저장 완료 call_id=(\d+) segments=(\d+) duration=(\d+)s")
_USAGE_RE = re.compile(
    r"normalcall usage: call_id=(\d+) type=\S+ msgs=(\d+) dropped=(\d+) t=\[[^\]]*\] sum_prompt=(\d+) sum_resp=(\d+) sum_thoughts=(\d+) "
    r"sum_total=(\d+) .*?sum_in=(\S+) sum_out=(\S+)"
)


def _mods(s: str) -> dict[str, int]:
    """«AUDIO=12,TEXT=3» → {"AUDIO": 12, "TEXT": 3} (- 면 빈 dict)."""
    out: dict[str, int] = {}
    if s == "-":
        return out
    for part in s.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            try:
                out[k] = int(v)
            except ValueError:
                pass
    return out


def fetch_log_lines(call_ids: list[int], *, project: str | None, freshness: str, log_file: str | None) -> list[str]:
    if log_file:
        return Path(log_file).read_text(encoding="utf-8").splitlines()
    ids = " OR ".join(f'"call_id={c} "' for c in call_ids) + " OR " + " OR ".join(f'"call_id={c}"' for c in call_ids)
    cmd = ["gcloud", "logging", "read", f'(textPayload:"normalcall: 저장 완료" OR textPayload:"normalcall usage:") AND ({ids})',
           "--format=value(textPayload)", f"--freshness={freshness}", "--limit=2000"]
    if project:
        cmd.append(f"--project={project}")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=120, shell=(sys.platform == "win32"))
    except Exception as exc:  # noqa: BLE001
        print(f"⚠ gcloud 조회 실패({exc}) — 로그 없음으로 처리(전부 건너뜀)")
        return []
    if res.returncode != 0:
        print(f"⚠ gcloud 조회 실패(rc={res.returncode}): {res.stderr.strip()[:300]} — 전부 건너뜀")
        return []
    return res.stdout.splitlines()


def compute(call_id: int, lines: list[str]) -> dict | None:
    """로그 줄에서 이 통화의 조각별 duration·usage 를 뽑아 합친다. 조각을 하나도 못 찾으면 None."""
    durations: list[int] = []
    usages: list[dict] = []
    for ln in lines:
        m = _SAVED_RE.search(ln)
        if m and int(m.group(1)) == call_id:
            durations.append(int(m.group(3)))
            continue
        m = _USAGE_RE.search(ln)
        if m and int(m.group(1)) == call_id:
            in_mod, out_mod = _mods(m.group(8)), _mods(m.group(9))
            usages.append({
                "msgs": int(m.group(2)), "dropped": int(m.group(3)), "sum_prompt": int(m.group(4)), "sum_resp": int(m.group(5)),
                "sum_thoughts": int(m.group(6)), "total": int(m.group(7)),
                "in_audio": in_mod.get("AUDIO", 0), "in_text": in_mod.get("TEXT", 0), "out_audio": out_mod.get("AUDIO", 0), "out_text": out_mod.get("TEXT", 0),
            })
    if not durations and not usages:
        return None
    # gcloud 는 최신부터 준다 — 시간순으로 뒤집는다(조각 번호는 순서일 뿐 계산엔 안 쓴다)
    durations.reverse()
    usages.reverse()
    return {
        "durations": durations, "total_time": sum(durations),
        "usages": usages,
        "usage_sum": {k: sum(u[k] for u in usages) for k in ("msgs", "dropped", "sum_prompt", "sum_resp", "sum_thoughts", "total", "in_audio", "in_text", "out_audio", "out_text")} if usages else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("call_ids", nargs="+", type=int)
    ap.add_argument("--apply", action="store_true", help="실제 UPDATE(기본은 dry-run)")
    ap.add_argument("--project", default=None)
    ap.add_argument("--freshness", default="7d")
    ap.add_argument("--log-file", default=None)
    args = ap.parse_args()

    lines = fetch_log_lines(args.call_ids, project=args.project, freshness=args.freshness, log_file=args.log_file)
    print(f"로그 {len(lines)}줄")
    engine = create_engine(settings.direct_url, poolclass=None)
    with engine.begin() as c:
        for cid in args.call_ids:
            row = c.execute(text("SELECT total_time, fragment_count, usage_json, usage_msgs, usage_in_audio, usage_in_text, usage_out_audio, usage_out_text, usage_total FROM call WHERE call_id=:c"), {"c": cid}).first()
            if row is None:
                print(f"[{cid}] 없는 통화 — 건너뜀")
                continue
            uj = row[2] if isinstance(row[2], dict) else (json.loads(row[2]) if row[2] else {})
            if uj and uj.get("fragments"):
                print(f"[{cid}] 이미 누적 저장(usage_json.fragments 있음) — 건너뜀")
                continue
            comp = compute(cid, lines)
            if comp is None:
                print(f"[{cid}] 로그에 조각 기록 없음 — 건너뜀 (DB total_time={row[0]}s fragment_count={row[1]})")
                continue
            print(f"[{cid}] DB total_time={row[0]}s fragment_count={row[1]} → 로그 조각 {len(comp['durations'])}개 {comp['durations']} 합 {comp['total_time']}s")
            if comp["usage_sum"]:
                s = comp["usage_sum"]
                print(f"       usage DB(msgs={row[3]} in_audio={row[4]} in_text={row[5]} out_audio={row[6]} out_text={row[7]} total={row[8]}) → 로그 {len(comp['usages'])}조각 합 "
                      f"msgs={s['msgs']} in_audio={s['in_audio']} in_text={s['in_text']} out_audio={s['out_audio']} out_text={s['out_text']} total={s['total']}")
            if not args.apply:
                continue
            params = {"c": cid, "tt": comp["total_time"]}
            sets = ["total_time=:tt"]
            if comp["usage_sum"]:
                s = comp["usage_sum"]
                frags = [{"fragment": i + 1, **u, "backfilled": True} for i, u in enumerate(comp["usages"])]
                new_json = {**uj, "sum_prompt": s["sum_prompt"], "sum_resp": s["sum_resp"], "sum_thoughts": s["sum_thoughts"], "dropped": s["dropped"],
                            "fragments": frags, "backfilled_from_logs": True}
                params.update({"m": s["msgs"], "ia": s["in_audio"], "it": s["in_text"], "oa": s["out_audio"], "ot": s["out_text"], "tot": s["total"],
                               "uj": json.dumps(new_json, ensure_ascii=False)})
                sets += ["usage_msgs=:m", "usage_in_audio=:ia", "usage_in_text=:it", "usage_out_audio=:oa", "usage_out_text=:ot", "usage_total=:tot",
                         "usage_json=CAST(:uj AS JSONB)"]
            c.execute(text(f"UPDATE call SET {', '.join(sets)} WHERE call_id=:c"), params)
            print(f"       ✔ UPDATE 적용")
    print("dry-run 끝 — 적용은 --apply" if not args.apply else "적용 끝")


if __name__ == "__main__":
    main()
