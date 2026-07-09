"""freetalking.json 문법↔미션 인덱스 — 대화 모드 유도 상황 힌트 조회 (외부 어댑터).

assets/level/freetalking.json 의 레코드(Grammer1/2/3 ↔ Misson1/Misson2/Mission3,
원본 철자 그대로 — 오타 아님)를 부팅 후 첫 호출 때 1회 lazy 로드해 정규화 인덱스로
들고, `hint_for(grammar_obj, ex)` 가 문법 표기에 맞는 미션 원문을 돌려준다.
매칭 실패·자산 부재 시 고정 폴백 템플릿(R5 graceful — 앱은 정상 동작).

core 규율: 도메인/DB 를 모른다 — 순수 자산 로드 + 문자열 반환.
설계 근거: docs/20260709_1346_level-system-detailed-mechanics.md ③.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# core/ 는 저장소 루트 바로 아래 — parents[1] = 루트.
_FREETALKING_JSON = (
    Path(__file__).resolve().parents[1] / "assets" / "level" / "freetalking.json"
)

# 레코드 필드 대응(위치 기반): Grammer{n} 의 유도 상황이 같은 자리의 미션이다.
# ⚠ 원본 철자: Misson1/Misson2 (오타), Mission3 (정타) — 자산을 고치지 말고 코드가 흡수.
_GRAMMAR_MISSION_PAIRS: tuple[tuple[str, str], ...] = (
    ("Grammer1", "Misson1"),
    ("Grammer2", "Misson2"),
    ("Grammer3", "Mission3"),
)

# lazy 싱글턴 인덱스: _norm_grammar(문법 표기) → 미션 원문. None = 미로드.
_index: dict[str, str] | None = None


def _norm_grammar(s: str) -> str:
    """문법 표기 정규화 — 공백 제거·하이픈 통일·casefold 로 표기 흔들림 흡수."""
    s = (s or "").strip().casefold()
    s = re.sub(r"\s+", "", s)
    return s.replace("‐", "-").replace("–", "-").replace("—", "-")


def _load_index() -> dict[str, str]:
    """freetalking.json → 정규화 인덱스(첫 호출 1회). 실패 시 빈 인덱스(R5)."""
    global _index
    if _index is not None:
        return _index
    idx: dict[str, str] = {}
    try:
        data = json.loads(_FREETALKING_JSON.read_text(encoding="utf-8"))
        for rec in data.get("records") or []:
            for g_key, m_key in _GRAMMAR_MISSION_PAIRS:
                grammar, mission = rec.get(g_key), rec.get(m_key)
                if grammar and mission:
                    # 같은 문법이 여러 과에 나오면 첫 등장(교재 순서상 도입 과) 우선.
                    idx.setdefault(_norm_grammar(str(grammar)), str(mission).strip())
    except Exception:  # noqa: BLE001 — 자산 부재/손상이 앱을 죽이면 안 된다(R5).
        idx = {}
    _index = idx
    return _index


def hint_for(grammar_obj: str, ex: str | None = None) -> str:
    """문법 항목의 대화 유도 상황 힌트를 돌려준다.

    freetalking 인덱스에 매칭되면 미션 원문, 아니면 폴백 템플릿(예문 우선, 없으면
    문법 표기로). 반환값은 대화 모드 가이드 블록의 `유도 상황:` 자리에 그대로 꽂힌다.
    """
    mission = _load_index().get(_norm_grammar(grammar_obj))
    if mission:
        return mission
    ref = (ex or "").strip() or (grammar_obj or "").strip()
    return f"학습자의 관심사 속에서 '{ref}'처럼 말하게 될 만한 순간을 만들어라"
