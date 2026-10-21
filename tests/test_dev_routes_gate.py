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


def _client(*, dev_routes_enabled: bool, env: str, **extra) -> TestClient:
    settings = Settings(
        DATABASE_URL_POOL="postgresql+psycopg2://u:p@localhost:5432/dummy",
        DEV_ROUTES_ENABLED=dev_routes_enabled,
        ENV=env,
        **extra,
    )
    return TestClient(create_app(settings))


@pytest.mark.parametrize("env", ["dev", "test", "prod"])
def test_dev_routes_closed_regardless_of_env_when_flag_off(env):
    """DEV_ROUTES_ENABLED=False(기본)면 ENV 가 뭐든 항상 404 — 안 넣으면 닫힌다."""
    c = _client(dev_routes_enabled=False, env=env)
    assert c.post("/__dev/signup", json={}).status_code == 404
    assert c.get("/__levelcalldemo").status_code == 404


@pytest.mark.parametrize("env", ["dev", "test", "prod"])
def test_dev_routes_open_regardless_of_env_when_flag_on(env):
    """DEV_ROUTES_ENABLED=True 면 ENV 가 뭐든(prod 포함) 마운트된다 — 두 축이 분리됐다.

    /__dev/signup 은 Supabase 미설정이라 503(client None)이지 404 가 아니다 — 라우트
    자체가 존재한다는 뜻. /__levelcalldemo 는 정적 HTML 이라 200.
    """
    c = _client(dev_routes_enabled=True, env=env, DEV_AUTH_ROUTES_ENABLED=True, DEV_AUTH_KEY="k1")
    assert c.post("/__dev/signup", json={"email": "x@test.com", "password": "pw"}, headers={"X-Dev-Key": "k1"}).status_code == 503
    assert c.get("/__levelcalldemo").status_code == 200



def test_dev_auth_routes_closed_by_default_even_when_dev_routes_on():
    """보안(2026-10-21) — DEV_ROUTES_ENABLED=true(운영 demo)여도 login·signup 은 DEV_AUTH_ROUTES_ENABLED 없이는 404."""
    c = _client(dev_routes_enabled=True, env="test")
    assert c.post("/__dev/login", json={"email": "a@test.com", "password": "pw"}).status_code == 404
    assert c.post("/__dev/signup", json={"email": "a@test.com", "password": "pw"}).status_code == 404
    assert c.get("/__levelcalldemo").status_code == 200          # 다른 dev 라우트는 그대로


def test_dev_auth_requires_key_and_signup_requires_allowed_domain():
    c = _client(dev_routes_enabled=True, env="test", DEV_AUTH_ROUTES_ENABLED=True, DEV_AUTH_KEY="k1")
    body = {"email": "a@test.com", "password": "pw"}
    assert c.post("/__dev/login", json=body).status_code == 403                                  # 키 없음
    assert c.post("/__dev/login", json=body, headers={"X-Dev-Key": "nope"}).status_code == 403   # 키 틀림
    assert c.post("/__dev/login", json=body, headers={"X-Dev-Key": "k1"}).status_code == 503     # 통과(Supabase 미설정)
    assert c.post("/__dev/signup", json={"email": "victim@gmail.com", "password": "pw"}, headers={"X-Dev-Key": "k1"}).status_code == 403
    assert c.post("/__dev/signup", json=body, headers={"X-Dev-Key": "k1"}).status_code == 503


def test_dev_auth_empty_key_always_denies():
    c = _client(dev_routes_enabled=True, env="test", DEV_AUTH_ROUTES_ENABLED=True, DEV_AUTH_KEY="")
    assert c.post("/__dev/login", json={"email": "a@test.com", "password": "pw"}, headers={"X-Dev-Key": ""}).status_code == 403
