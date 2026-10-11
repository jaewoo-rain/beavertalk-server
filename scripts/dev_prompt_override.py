"""[dev] 프롬프트 DB override 편집 — 배포 없이 통화 대본을 바꾼다(2026-10-10 사장님 지시).

`prompt_override` 테이블의 글자가 통화 대본을 덮어쓴다. 설계·검사 정본은
`docs/20261010_2050_프롬프트-DB-override.md`, 프롬프트 규율은 `docs/prompts/README.md`.

⛔⛔ **이 DB 는 실서비스와 공유다**(`app-api`·`demo-api`·`gpt-api` 가 같은 Supabase).
  ⇒ `on` 으로 올린 글자는 **그 시점부터 실제 통화**가 쓴다. 통화 중인 세션은 시작 때 떠놓은
    스냅샷으로 끝까지 가므로 말을 바꾸진 않는다 — 다음 통화부터 바뀐다.
⛔ 서버의 `PROMPT_OVERRIDE_ENABLED` 가 꺼져 있으면 **행을 올려도 안 읽는다**(그게 기본값).
  `status` 가 그 스위치까지 같이 찍어 준다.

사용법(한글 콘솔이 깨지면 `--out` 으로 파일에 쓰고 Read 로 확인):

    # 지금 뭐가 현역인가 + 스위치 상태
    python scripts/dev_prompt_override.py status [--out PATH]

    # 코드 기본값을 파일로 뽑는다 → 편집기로 고친다
    python scripts/dev_prompt_override.py dump expression --out expr.txt

    # 고친 글자를 **검사만** 해본다(DB 안 건드린다)
    python scripts/dev_prompt_override.py check expression --file expr.txt

    # 검사를 통과하면 새 행으로 올리고 현역으로 세운다(이전 현역은 자동 내림)
    python scripts/dev_prompt_override.py put expression --file expr.txt --note "되묻기 표현 수정"

    # 코드 기본값으로 되돌린다(행은 지우지 않는다 — append-only)
    python scripts/dev_prompt_override.py off expression

⚠ `put` 은 검사(`core.openai.prompts.override.vet`)를 통과하지 못하면 **아무것도 안 쓴다.**
  검사가 막는 건 배관(슬롯·라벨·종료 어휘·길이)이고, **말의 뜻은 못 본다** — 고친 뒤
  통화 한 번은 들어봐야 한다.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text  # noqa: E402

from core.config import settings  # noqa: E402
from core.openai.prompts.override import vet  # noqa: E402
from domains.learning.service import prompt_override_service as po_svc  # noqa: E402

# ⭐ 기본값 표를 **두 군데 두지 않는다** — 서비스가 든다(`po_svc.DEFAULTS`). dev 편집기
#   `/__devprompt` 와 이 스크립트가 같은 표를 보므로 둘이 어긋날 수 없다.
# ⚠ 거기 없는 이름은 아직 **대본을 떼지 않은** 것이다(`freetalk`·`chat`·`leveltest`).
DEFAULTS = po_svc.DEFAULTS


def _engine():
    # ⚠ `.env`·`.env.local` 둘 다 **운영 프로젝트**를 가리킨다(2026-10-10 실측). 대상을
    #   눈으로 확인할 수 있게 호스트를 같이 찍는다.
    return create_engine(settings.direct_url, poolclass=None)


def _emit(lines: list[str], out: str | None, *, raw: bool = False) -> None:
    """`raw=True` 면 끝에 줄바꿈을 **덧붙이지 않는다** — `dump` → 편집 → `put` 왕복에서
    글자가 1자씩 자라면 길이 검사와 전사 비교가 어긋난다."""
    text_ = "\n".join(lines) + ("" if raw else "\n")
    if out:
        io.open(out, "w", encoding="utf-8", newline="").write(text_)
        print("→ %s (%d자)" % (out, len(text_)))
    else:
        print(text_)


def _default_of(name: str) -> str:
    if name not in DEFAULTS:
        raise SystemExit(
            "모르는 대본 이름: %s (아는 것: %s)\n"
            "— 아직 대본을 떼지 않은 대본은 DB 로 못 바꾼다(모듈 주석 참고)."
            % (name, ", ".join(sorted(DEFAULTS)))
        )
    return DEFAULTS[name]


def cmd_status(args) -> None:
    lines = [
        "스위치 PROMPT_OVERRIDE_ENABLED = %s%s" % (
            getattr(settings, "PROMPT_OVERRIDE_ENABLED", False),
            "" if getattr(settings, "PROMPT_OVERRIDE_ENABLED", False)
            else "   ⛔ 꺼져 있다 — 행을 올려도 통화는 코드 기본값으로 간다",
        ),
        "DB = %s" % settings.direct_url.split("@")[-1].split("/")[0],
        "떼어낸 대본 = %s" % ", ".join(sorted(DEFAULTS)),
        "",
    ]
    with _engine().connect() as c:
        rows = list(c.execute(text(
            "SELECT override_id, name, enabled, length(body) AS n, "
            "       coalesce(note, '') AS note, created_at "
            "FROM prompt_override ORDER BY override_id DESC LIMIT 30")))
        used = c.execute(text(
            "SELECT count(*) FROM call WHERE prompt_override_id IS NOT NULL")).scalar()
    if not rows:
        lines.append("(행 없음 — 전부 코드 기본값)")
    for r in rows:
        lines.append("%s id=%-4s %-10s %5d자  %s  %s" % (
            "★현역" if r.enabled else "   　 ", r.override_id, r.name, r.n,
            str(r.created_at)[:19], r.note[:50]))
    lines.append("")
    lines.append("override 로 돌았다고 적힌 통화 = %s건" % used)
    _emit(lines, args.out)


def cmd_dump(args) -> None:
    """현역 행이 있으면 **그 글자**를, 없으면 코드 기본값을 뽑는다(고칠 출발점)."""
    default = _default_of(args.name)
    with _engine().connect() as c:
        row = c.execute(text(
            "SELECT override_id, body FROM prompt_override "
            "WHERE name = :n AND enabled IS TRUE ORDER BY override_id DESC LIMIT 1"),
            {"n": args.name}).first()
    body, src = (row.body, "현역 id=%s" % row.override_id) if row else (default, "코드 기본값")
    print("출처: %s (%d자)" % (src, len(body)))
    _emit([body], args.out, raw=True)


def _read_body(path: str, default: str) -> str:
    """파일을 읽되 **편집기가 붙인 꼬리 줄바꿈**을 기본값 모양에 맞춘다.

    ⚠ 거의 모든 편집기가 저장할 때 끝에 줄바꿈을 넣는다. 그대로 올리면 대본이 매번 1자씩
      자라고(길이 검사·전사 비교가 어긋난다) 조립 결과에 빈 줄이 하나 더 생긴다.
      기본값이 줄바꿈으로 끝나지 않으면 **꼬리 줄바꿈을 전부 깎는다.**
    """
    body = io.open(path, encoding="utf-8").read()
    if not default.endswith("\n"):
        body = body.rstrip("\n")
    return body


def cmd_check(args) -> tuple[bool, str]:
    default = _default_of(args.name)
    body = _read_body(args.file, default)
    ok, why = vet(args.name, body, default)
    print("%s — %s (%d자, 기본값 %d자)" % (
        "통과" if ok else "거부", why or "이상 없음", len(body), len(default)))
    return ok, why


def cmd_put(args) -> None:
    ok, why = cmd_check(args)
    if not ok:
        raise SystemExit("⛔ 검사를 통과하지 못해 **아무것도 쓰지 않았다**: %s" % why)
    body = _read_body(args.file, _default_of(args.name))
    with _engine().begin() as c:
        # 이전 현역을 내리고(append-only — 지우지 않는다) 새 행을 현역으로 세운다.
        c.execute(text("UPDATE prompt_override SET enabled = FALSE "
                       "WHERE name = :n AND enabled IS TRUE"), {"n": args.name})
        new_id = c.execute(text(
            "INSERT INTO prompt_override (name, body, enabled, note) "
            "VALUES (:n, :b, TRUE, :note) RETURNING override_id"),
            {"n": args.name, "b": body, "note": args.note or ""}).scalar()
    print("★ 현역 = id %s (%s, %d자)" % (new_id, args.name, len(body)))
    if not getattr(settings, "PROMPT_OVERRIDE_ENABLED", False):
        print("⛔ 다만 스위치가 꺼져 있다 — 서버에 PROMPT_OVERRIDE_ENABLED=true 를 켜야 읽는다.")


def cmd_off(args) -> None:
    with _engine().begin() as c:
        n = c.execute(text("UPDATE prompt_override SET enabled = FALSE "
                           "WHERE name = :n AND enabled IS TRUE"), {"n": args.name}).rowcount
    print("내린 행 %d개 — %s 는 이제 코드 기본값으로 간다." % (n, args.name))


def main() -> None:
    ap = argparse.ArgumentParser(description="프롬프트 DB override 편집(dev)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="현역 행 + 스위치 상태")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("dump", help="현역(없으면 코드 기본값) 글자를 뽑는다")
    p.add_argument("name")
    p.add_argument("--out")
    p.set_defaults(fn=cmd_dump)

    p = sub.add_parser("check", help="파일의 글자를 검사만 한다(DB 안 건드림)")
    p.add_argument("name")
    p.add_argument("--file", required=True)
    p.set_defaults(fn=lambda a: cmd_check(a))

    p = sub.add_parser("put", help="검사 통과 시 새 행으로 올려 현역으로 세운다")
    p.add_argument("name")
    p.add_argument("--file", required=True)
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_put)

    p = sub.add_parser("off", help="현역을 내려 코드 기본값으로 되돌린다")
    p.add_argument("name")
    p.set_defaults(fn=cmd_off)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
