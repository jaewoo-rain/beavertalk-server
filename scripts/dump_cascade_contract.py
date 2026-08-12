"""캐스케이드 WS 계약을 **모델에서 직접** 뽑는다(마크다운 + JSON).

⛔⛔ **손으로 옮겨 적지 마라.** 2026-08-12 에 프론트가 우리 문서에서 오류 4건을 잡았고,
  넷 다 사람이 손으로 옮겨서 생겼다:
    ① `input_partial` → 실제 wire 값은 `input_transcript`(클래스명을 wire 로 착각)
    ② "클라에 지터버퍼 없음" → 앱엔 900ms 가 있었다(데모 화면을 앱으로 착각)
    ③ `output_transcript` 누락 → 캐스케이드가 **아예 안 보내고 있었다**
    ④ `ready` 필드가 camelCase 라고 적음 → 실제는 snake_case(alias 없음)
  ⭐ ④ 가 특히 나쁘다 — **파싱이 안 터지고 조용히 기본값으로 돈다.** 양쪽 다 "적용됐다"고 믿는다.

⇒ 이 스크립트는 `cascade_protocol.py` 의 **pydantic 모델**에서 wire `type` 값·필드명·타입·
  기본값을 읽는다. 모델이 바뀌면 문서도 바뀐다. 회귀가 "문서 == 현행 모델"을 지킨다.

사용:
    PYTHONIOENCODING=utf-8 conda run -n beavertalk-server python scripts/dump_cascade_contract.py
    → docs/cascade-contract.md · docs/cascade-contract.json
"""

from __future__ import annotations

import json
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from domains.learning.realtime import cascade_protocol as proto  # noqa: E402


def _wire_type(model) -> str:
    """이 모델이 실제로 내보내는 `type` 값. ⛔ 클래스명이 아니다(오류 ①이 그것이었다)."""
    field = model.model_fields.get("type")
    if field is None:
        return ""
    args = typing.get_args(field.annotation)
    return str(args[0]) if args else str(field.default or "")


def _type_name(annotation) -> str:
    origin = typing.get_origin(annotation)
    if origin is typing.Union or str(origin) == "typing.Union" or origin is type(int | str):
        parts = [_type_name(a) for a in typing.get_args(annotation)]
        return " | ".join(p for p in parts if p != "NoneType") + (
            " | null" if type(None) in typing.get_args(annotation) else ""
        )
    if origin is not None:
        inner = ", ".join(_type_name(a) for a in typing.get_args(annotation))
        return f"{getattr(origin, '__name__', str(origin))}[{inner}]"
    return getattr(annotation, "__name__", str(annotation))


def _fields(model) -> list[dict]:
    rows = []
    for name, field in model.model_fields.items():
        if name == "type":
            continue
        required = field.is_required()
        default = None if required else field.default
        rows.append({
            "name": name,                       # ⚠ wire 이름 그대로(별칭이 있으면 아래에서 덮는다)
            "type": _type_name(field.annotation),
            "required": required,
            "default": default if isinstance(default, (str, int, float, bool, type(None))) else str(default),
            "alias": field.alias,
        })
        if field.alias:
            rows[-1]["name"] = field.alias     # 실제로 나가는 이름은 별칭이다
    return rows


def collect() -> dict:
    def _members(union) -> list:
        return list(typing.get_args(typing.get_args(union)[0]))

    out: dict = {"server_to_client": [], "client_to_server": []}
    for key, union in (("server_to_client", proto.CascadeServerMessage),
                       ("client_to_server", proto.CascadeClientMessage)):
        for model in _members(union):
            out[key].append({
                "wire_type": _wire_type(model),
                "model": model.__name__,
                "doc": (model.__doc__ or "").strip().split("\n")[0],
                "fields": _fields(model),
            })
        out[key].sort(key=lambda m: m["wire_type"])
    return out


def to_markdown(data: dict) -> str:
    lines = [
        "# 캐스케이드 WS 계약 (자동 생성)",
        "",
        "> ⛔ **이 파일을 손으로 고치지 마라.** `scripts/dump_cascade_contract.py` 가",
        "> `cascade_protocol.py` 의 모델에서 뽑는다. 고치려면 모델을 고쳐라.",
        "> 사람이 옮겨 적은 표는 반드시 낡는다 — 2026-08-12 에 그걸로 오류 4건이 났다.",
        "",
        "⚠ 필드 이름은 **wire 이름**이다(파이썬 속성명이 아니라 실제로 JSON 에 나가는 이름).",
        "",
    ]
    for key, title in (("server_to_client", "서버 → 클라"), ("client_to_server", "클라 → 서버")):
        lines += [f"## {title}", ""]
        for msg in data[key]:
            lines += [f"### `{msg['wire_type']}`  ({msg['model']})", ""]
            if msg["doc"]:
                lines += [msg["doc"], ""]
            lines += ["| 필드 | 타입 | 필수 | 기본값 |", "|---|---|---|---|"]
            for f in msg["fields"]:
                default = "—" if f["required"] else json.dumps(f["default"], ensure_ascii=False)
                lines.append(f"| `{f['name']}` | `{f['type']}` | {'✔' if f['required'] else ''} | `{default}` |")
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    data = collect()
    docs = ROOT / "docs"
    (docs / "cascade-contract.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (docs / "cascade-contract.md").write_text(to_markdown(data) + "\n", encoding="utf-8")
    print("서버→클라 %d종 · 클라→서버 %d종 를 docs/cascade-contract.{md,json} 에 썼다"
          % (len(data["server_to_client"]), len(data["client_to_server"])))


if __name__ == "__main__":
    main()
