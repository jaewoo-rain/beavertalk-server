"""S5(2026-09-26, Play 심사 대비) — dev 전용 라우트 마운트 게이트.

`main.py:296` 이 `ENV != "prod"` 로 게이트돼 있어 실서비스(app-api, ENV="test")에도
`/__dev/signup`(service key 로 계정 생성+토큰 발급)·`/__levelcalldemo` 등 15개 dev 라우트가
그대로 열려 있었다. `DEV_ROUTES_ENABLED`(기본 False, ENV 와 무관)로 축을 분리했다.

⚠ `ENV` 를 건드리면 안 되는 이유(DAILY_LIMIT_ENFORCED 와 같은 이유)라, 여기서는 **마운트
여부가 DEV_ROUTES_ENABLED 하나에만 반응하고 ENV 값(dev/test/prod)엔 반응하지 않는지**를
직접 대조해 못박는다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.config import Settings
from main import create_app


def _client(*, dev_routes_enabled: bool, env: str) -> TestClient:
    # ⛔ `_env_file=None` — 아래 503 단정의 전제가 **「Supabase 미설정」**이다. 끊지 않으면
    #   개발자의 `.env.local` 에 실제 자격이 있어 client 가 살아나고 **401** 이 온다
    #   (2026-10-08 실측: env 파일 없는 트리에서만 통과해 「코드가 깨졌다」로 오진했다).
    settings = Settings(
        _env_file=None,
        DATABASE_URL_POOL="postgresql+psycopg2://u:p@localhost:5432/dummy",
        DEV_ROUTES_ENABLED=dev_routes_enabled,
        ENV=env,
    )
    return TestClient(create_app(settings))


@pytest.mark.parametrize("env", ["dev", "test", "prod"])
def test_dev_routes_closed_regardless_of_env_when_flag_off(env):
    """DEV_ROUTES_ENABLED=False(기본)면 ENV 가 뭐든 항상 404 — 안 넣으면 닫힌다."""
    c = _client(dev_routes_enabled=False, env=env)
    assert c.post("/__dev/signup", json={}).status_code == 404
    assert c.get("/__levelcalldemo").status_code == 404
    # GPT 통화 QA 콘솔(2026-10-05)도 같은 축 하나에만 반응한다 — 운영에 새면 안 된다.
    assert c.get("/__devgpt").status_code == 404


@pytest.mark.parametrize("env", ["dev", "test", "prod"])
def test_dev_routes_open_regardless_of_env_when_flag_on(env):
    """DEV_ROUTES_ENABLED=True 면 ENV 가 뭐든(prod 포함) 마운트된다 — 두 축이 분리됐다.

    /__dev/signup 은 Supabase 미설정이라 503(client None)이지 404 가 아니다 — 라우트
    자체가 존재한다는 뜻. /__levelcalldemo 는 정적 HTML 이라 200.
    """
    c = _client(dev_routes_enabled=True, env=env)
    assert c.post("/__dev/signup", json={"email": "x@x.com", "password": "pw"}).status_code == 503
    assert c.get("/__levelcalldemo").status_code == 200
    # /__devgpt = GPT 통화 QA 콘솔. 정적 HTML 이라 200 이고, 파일이 사라지면 여기서 잡힌다.
    r = c.get("/__devgpt")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "calls/stream" in r.text   # WS 계약이 페이지에 살아 있나
    assert "16000" in r.text          # 클라→서버 입력은 16k(서버가 24k 로 올린다)
