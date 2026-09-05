# -*- coding: utf-8 -*-
"""alembic autogenerate 가 **남의 테이블을 건드리지 않는가**.

## ⛔ 이게 깨지면 조용히 데이터가 날아간다

같은 Postgres 를 여러 소유자가 쓴다:

    alembic(서버)   call · sentence · member · item_evidence …
    앱 클라이언트    content_report   ← Supabase 마이그레이션으로 앱팀이 만든다
    (기타)          waitlist

autogenerate 는 «모델에 없는 테이블» 을 **삭제 대상으로 제안**한다. 검토에서 못 걸러내면
운영 DB 의 남의 테이블이 DROP 된다 — 신고 데이터는 Google Play 정책 대응 자료라
없어지면 되돌릴 방법이 없다.

`env.py` 의 `include_name` 이 그 방어인데, 그 함수는 마이그레이션을 돌릴 때만 실행되므로
**평소에는 아무도 안 본다.** 여기서 못박는다.
"""

from __future__ import annotations

import pytest

from db.registry import Base


def _include_name(name: str, type_: str = "table") -> bool:
    """`alembic/env.py` 의 include_name 과 **같은 규칙**.

    ⚠ 그쪽을 import 하지 않는다 — env.py 는 alembic 컨텍스트 없이는 못 읽는다.
      대신 규칙을 여기 복제하고, 아래 test_the_rule_matches_env_py 가 두 곳이 갈라지면
      잡는다.
    """
    owned = set(Base.metadata.tables.keys())
    if type_ == "table":
        return name in owned
    return True


def test_the_clients_report_table_is_invisible_to_autogenerate():
    """⛔ `content_report` 는 **앱 클라이언트 소유**다. autogenerate 가 보면 안 된다.

    마이그레이션 파일이 직접 경고해 두었다:
      "이 테이블은 앱 클라이언트 소유다. 같은 Postgres를 서버의 alembic이 관리하므로,
       autogenerate 가 이 테이블을 모르는 객체로 보고 DROP 을 제안할 수 있다."
    (`beavertalk-flutter/supabase/migrations/2026-09-01_content_report.sql`)

    ⭐ 지금은 «모델에 있는 것만 관리» 라는 일반 규칙으로 이미 막혀 있다. 이 시험은
      그 규칙이 나중에 «전부 관리» 로 느슨해지는 것을 막는다.
    """
    assert _include_name("content_report") is False


def test_other_foreign_tables_stay_invisible_too():
    """같은 이유로 보호되는 것들. `waitlist` 는 env.py 주석이 직접 지목한 예다."""
    for name in ("waitlist", "content_report"):
        assert _include_name(name) is False, f"{name} 이 autogenerate 에 노출됐다"


@pytest.mark.parametrize("name", ["call", "sentence", "member", "item_evidence"])
def test_our_own_tables_stay_managed(name):
    """⚠ 반대 방향도 못박는다 — 보호가 지나쳐 **우리 테이블까지 가리면** 스키마 변경이
    autogenerate 에 안 잡히고, 그건 «마이그레이션을 만들었는데 비어 있다» 로 나타난다.
    """
    assert _include_name(name) is True, f"{name} 이 관리 대상에서 빠졌다"


def test_non_table_objects_pass_through():
    """인덱스·제약 등은 테이블 필터를 타지 않는다(그쪽은 부모 테이블이 이미 걸러졌다)."""
    assert _include_name("anything", type_="index") is True


def test_the_rule_matches_env_py():
    """⛔ 이 파일의 규칙 복제본이 `env.py` 와 **갈라지지 않았는가**.

    복제는 언젠가 어긋난다. 원문을 문자열로 읽어 핵심 두 줄이 그대로인지 본다 —
    깨지면 이 시험이 «둘이 갈라졌다» 고 알려 준다(무엇이 옳은지는 사람이 판단한다).
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "env.py"
    text = src.read_text(encoding="utf-8")
    assert "_OWNED_TABLES = set(Base.metadata.tables.keys())" in text, \
        "env.py 의 소유 테이블 산출 방식이 바뀌었다 — 이 파일의 복제본도 맞춰라"
    assert "return name in _OWNED_TABLES" in text, \
        "env.py 의 필터 규칙이 바뀌었다 — 이 파일의 복제본도 맞춰라"
    assert "include_name=include_name" in text, \
        "⛔ include_name 이 context.configure 에서 빠졌다 — 방어가 통째로 꺼졌다"
