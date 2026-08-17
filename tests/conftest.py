"""테스트 공통 설정 — **시험이 재는 것을 바꾸지 않고, 기다리기만 하는 시간을 없앤다.**

## 왜 (2026-08-17)
전 스위트가 7분 38초였다. 그 대부분이 **검증이 아니라 대기**였다:

    tests/test_level_test_call.py       25개 / 64.9s   — 9개가 각각 정확히 7.0s
    tests/test_normalcall_ws.py + hint  126개 / 348.5s — 다수가 각각 정확히 7.0s

`7.0s` 는 우연한 숫자가 아니라 `call_session.PLAYBACK_DONE_WAIT_S` 그대로다.
`_graceful_close` 는 `call_ended` 를 보낸 뒤 클라의 `playback_done` ack 를 그만큼 기다린다
(작별 오디오 꼬리가 잘리지 않게 — 실서비스에선 꼭 필요한 창이다). 그런데 **가짜 WS 는 그
ack 를 영영 보내지 않으므로** 통화를 끝까지 도는 시험은 전부 상한 7초를 통째로 태운다.

## ⛔ 커버리지는 1도 줄지 않는다
· `playback_done` · `PLAYBACK_DONE_WAIT_S` 를 **참조하는 시험이 0건**이다(grep 확인) —
  이 상한값 자체를 검증하는 시험은 없다. 시험이 보는 것은 그 대기 **전에** 끝난 일
  (call_ended 전송 · status 전이 · 세그먼트 저장 · 종료 규약)이고, 그건 그대로 돈다.
· 0 이 아니라 0.05s 로 둔다 — `wait_for` / TimeoutError 경로를 **여전히 통과**시켜서
  "기다렸다가 닫는다"는 배관 자체는 계속 시험 대상으로 남긴다.
· 프로덕션 값(7.0)은 **안 건드린다.** 여기서 바꾸는 건 테스트 프로세스 안뿐이다.

⚠ 이 상한의 *값*을 검증하는 시험을 새로 쓴다면, 그 시험에서만
  `monkeypatch.setattr(call_session, "PLAYBACK_DONE_WAIT_S", 7.0)` 으로 되돌려 쓴다.
"""

from __future__ import annotations

import pytest

# 실서비스 상한(7.0s)을 대신할 테스트용 값 — 대기 경로는 살리고 시간만 없앤다.
_TEST_PLAYBACK_DONE_WAIT_S = 0.05


@pytest.fixture(autouse=True)
def _fast_playback_done_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """가짜 클라가 절대 보내지 않는 ack 를 7초씩 기다리지 않는다.

    call_session 을 import 하지 못하는 환경(모듈 구조 변경 등)에서도 조용히 넘어간다 —
    이 파일 때문에 전 스위트가 collect 조차 못 하는 일은 없어야 한다(R5 와 같은 규율).
    """
    try:
        from domains.learning.realtime import call_session
    except Exception:  # noqa: BLE001 — 여기서 죽으면 전 스위트가 죽는다
        return
    monkeypatch.setattr(
        call_session, "PLAYBACK_DONE_WAIT_S", _TEST_PLAYBACK_DONE_WAIT_S, raising=False
    )
