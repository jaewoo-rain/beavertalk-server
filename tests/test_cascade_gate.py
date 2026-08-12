"""캐스케이드 노출 게이트 — **깨진 ENV 게이트에 기대지 않는다**(2026-08-07).

사실관계:
  실서비스(app-api) /health → {"status":"ok","env":"test"} — ENV 가 "prod" 가 **아니다.**
  그래서 main.py 의 `if settings.ENV != "prod":` 는 **실서비스에서도 참**이고, 그 블록은
  운영에 마운트된다. 옛 주석("prod 에는 마운트조차 하지 않는다")은 사실과 반대였고, 그걸
  1차 자료로 읽은 클라 쪽이 "prod 백엔드면 소켓이 안 열린다"를 안전장치로 세고 있었다.

그래서 캐스케이드는 **전용 스위치(CASCADE_ENABLED)** 로 막는다. 여기서 못박는 것:
  ① 기본값(꺼짐)에서는 dev 라도 열리지 않는다 — 데모 HTML 도 WS 도
  ② 켠 곳에서만 열린다
  ③ ⭐ ENV 가 "test"(=실서비스인데 dev 로 보이는 값)여도 스위치가 닫으면 닫힌다

⚠ 라우트 목록을 뒤지지 않고 **실제 응답**으로 검사한다 — 도메인 라우터가 서브앱으로 붙어
  app.routes 에는 안 보이기 때문이다(그걸 모르고 검사하면 "안 열렸다"를 잘못 통과시킨다).
"""

import pytest
from starlette.websockets import WebSocketDisconnect

import main as main_mod
from core.config import settings as base_settings
from fastapi.testclient import TestClient

WS_PATH = "/api/v1/cascade/stream"
_POLICY_VIOLATION = 1008   # 라우터가 인증 실패로 닫는 코드 = **라우트가 존재한다**는 증거
_NO_ROUTE = 1000           # 그런 경로가 없을 때 스타렛이 그냥 닫는다


def _client(env: str, cascade_enabled: bool) -> TestClient:
    return TestClient(
        main_mod.create_app(
            base_settings.model_copy(update={"ENV": env, "CASCADE_ENABLED": cascade_enabled})
        )
    )


def _ws_close_code(client: TestClient) -> int:
    with pytest.raises(WebSocketDisconnect) as caught:
        with client.websocket_connect(WS_PATH):
            pass
    return caught.value.code


def test_disabled_by_default_even_in_dev():
    """⛔ 스위치가 꺼져 있으면 dev 에서도 노출 0 — 기본값이 안전해야 한다."""
    with _client("dev", False) as client:
        assert client.get("/__cascadedemo").status_code == 404
        assert _ws_close_code(client) == _NO_ROUTE


def test_enabled_only_where_switched_on():
    """켠 곳에서는 열린다. WS 는 토큰이 없으면 1008 로 닫힌다(= 라우트가 살아 있다)."""
    with _client("dev", True) as client:
        assert client.get("/__cascadedemo").status_code == 200
        assert _ws_close_code(client) == _POLICY_VIOLATION


def test_switch_off_closes_it_even_when_env_gate_is_wrong():
    """⭐ 요점: 실서비스와 같은 ENV="test" 여도 스위치가 닫으면 닫힌다.

    깨진 게이트 위에 기능을 얹지 않는다 — 이게 이 스위치의 존재 이유다.
    """
    with _client("test", False) as client:
        assert client.get("/__cascadedemo").status_code == 404
        assert _ws_close_code(client) == _NO_ROUTE


def test_prod_never_mounts_even_if_switch_is_on():
    """ENV 가 제대로 "prod" 인 환경에서는 스위치가 켜져 있어도 안 열린다(두 겹)."""
    with _client("prod", True) as client:
        assert client.get("/__cascadedemo").status_code == 404
        assert _ws_close_code(client) == _NO_ROUTE


def test_demo_page_is_actually_served_when_enabled():
    """라우트만 있고 파일이 없던 /__calldemo 전례(지금도 500)를 반복하지 않는다."""
    with _client("dev", True) as client:
        response = client.get("/__cascadedemo")
    assert response.status_code == 200
    assert "cascade" in response.text.lower()
