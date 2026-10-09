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


@pytest.fixture(autouse=True)
def _expr_llm_judge_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """표현학습 LLM 판정(2026-09-15 4차)은 시험에서 **기본 꺼짐** — 기존 시험은 문자열 대조 경로(동기 판정)를 검증한다.
    LLM 판정 시험(tests/test_expr_llm_judge.py)은 state.expr_llm_judge 를 직접 켜고 가짜 generate_structured 를 쓴다."""
    try:
        from core.config import settings
    except Exception:  # noqa: BLE001
        return
    monkeypatch.setattr(settings, "EXPR_LLM_JUDGE", False, raising=False)


@pytest.fixture(autouse=True)
def _serialize_run_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """⛔ **시험 전용** — `svc.run_db` 동시 호출을 줄 세운다. 프로덕션 코드는 안 건드린다.

    ## 왜 (2026-10-04, 제목 선생성 PM-DEC-362 를 넣다가 드러났다)
    시험의 DB 는 `sqlite+pysqlite:///:memory:` + **`StaticPool`** 이다 — 즉 전 세션이
    **DBAPI 연결 하나**를 공유한다(`:memory:` 는 연결마다 DB 가 따로 생겨서 공유가 유일한
    선택이다). 그런데 `run_db` 는 `run_in_threadpool` 로 **스레드마다 새 세션**을 연다.
    통화가 끝나면 백그라운드 태스크가 동시에 여러 개 돈다:

        분석(analyze_call) · 이어하기 요약(build_resume_context) ·
        자유대화 기억(extract_and_merge_chat_memory) · **제목(build_call_title)**

    둘 이상이 같은 순간에 그 **한 연결**에서 트랜잭션을 열면 sqlite 가 터진다 —
        `(sqlite3.DatabaseError) cannot start a transaction within a transaction`
    실측: 전 스위트 부하에서 `test_run_call_persists_segments_and_status` 1건이
    그 예외로 떨어졌다(파일 단독 실행 6/6 통과 — **부하 의존 flake**다).
    CLAUDE.md 가 경고하는 «부하에 흔들려 실패 목록이 매번 바뀜» 이 이 종류다.

    ⭐ **프로덕션엔 없는 문제다.** 거기선 `run_db` 가 세션마다 pgbouncer 풀에서
      **자기 연결**을 받는다 — 동시 실행이 정상 경로다(분석·요약·기억이 이미 그렇게 돈다).
      그래서 고칠 자리는 프로덕션 코드가 아니라 **시험 하네스**다.
    ⛔ 태스크를 끄지 않았다 — 끄면 그 시험들이 통화 종료 배관을 더 이상 안 본다.
      **순서는 그대로**(락은 FIFO) 두고 겹침만 막는다.
    ⚠ 락을 **이벤트 루프별로** 만든다. 한 시험이 `asyncio.run` 을 여러 번 부르면
      루프가 갈리고, 하나의 `asyncio.Lock` 을 두 루프에서 쓰면
      "bound to a different event loop" 로 죽는다.
    """
    try:
        import asyncio as _asyncio

        from domains.learning.service import normalcall_service as _svc
    except Exception:  # noqa: BLE001 — 여기서 죽으면 전 스위트가 죽는다
        return

    original = _svc.run_db
    locks: dict = {}

    async def _serialized(session_factory, fn):
        loop = _asyncio.get_running_loop()
        lock = locks.get(loop)
        if lock is None:
            lock = locks[loop] = _asyncio.Lock()
        async with lock:
            return await original(session_factory, fn)

    monkeypatch.setattr(_svc, "run_db", _serialized)


@pytest.fixture(autouse=True)
def _developer_env_must_not_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    """⛔⛔ 시험을 **개발자의 `.env` 에서 떼어낸다**(2026-10-08 실측 사고).

    `core/config.py` 는 import 시점에 `.env`·`.env.local` 을 읽는다. 그 파일에
    `OPENAI_REALTIME_COURSES=expression` + 실제 `GPT_API_KEY` 가 들어 있으면
    `live_openai_for("expression")` 이 **True** 가 되어 통화 시험이 OpenAI 경로를 탄다 —
    그 경로는 조각 3값(`fragment_index`·`max_fragments`·`remaining_s`)을 **일부러 비우므로**
    Gemini 조각 계약을 단정하는 시험 20개가 `KeyError: 'fragment_index'` 로 깨졌다.
    ⚠ env 파일이 없는 트리(워크트리 등)에서는 통과하고 있는 트리에서만 깨져서,
      「코드가 깨졌다」로 오진하기 쉽다.

    ⇒ **기본은 꺼짐**으로 고정한다. 엔진 라우팅을 보는 시험은
      `tests/test_openai_routing.py` 처럼 **자기가 monkeypatch 로 켠다**(그쪽이 이긴다 —
      이 fixture 가 먼저 돌고 테스트 본문이 뒤에 덮는다).
    """
    try:
        from core.config import settings
    except Exception:  # noqa: BLE001
        return
    # ① 엔진 라우팅 — 켜지면 조각 3값이 비어 Gemini 조각 계약 시험이 깨진다.
    monkeypatch.setattr(settings, "OPENAI_REALTIME_COURSES", "", raising=False)
    monkeypatch.setattr(settings, "GPT_API_KEY", "", raising=False)
    # ② 압축 임계 — 단계 0 계측의 기준선이 **코드 기본값 16k/12k** 위에서 수집됐다.
    #   클라우드는 8000/7000 로 낮춰 두었고 그 값이 `.env.local` 로 새어 들면
    #   「압축 임박」 판정 시험 3개가 다른 설정의 결과를 본다.
    #   ⚠ `test_env_override_reaches_the_wire` 처럼 **일부러 낮추는** 시험은 자기가
    #     monkeypatch 하므로 그쪽이 이긴다(이 fixture 가 먼저, 테스트 본문이 뒤).
    monkeypatch.setattr(settings, "LIVE_CTX_TRIGGER_TOKENS", 16000, raising=False)
    monkeypatch.setattr(settings, "LIVE_CTX_TARGET_TOKENS", 12000, raising=False)
    # ③ Supabase — 전 스위트가 **「미설정」**을 전제한다(예: `/__dev/signup` 이 503 이지
    #   401 이 아니라는 단정). `.env.local` 에 실제 자격이 있으면 client 가 살아나 401 이 온다.
    #   ⚠ `core/supabase_client.get_client()` 는 모듈 레벨 settings 를 보고 **성공 시 캐시**
    #     하므로(`_ready`) 자격만 비우면 앞 시험이 만든 캐시가 그대로 쓰인다 — 캐시도 끊는다.
    monkeypatch.setattr(settings, "SUPABASE_URL", "", raising=False)
    monkeypatch.setattr(settings, "SUPABASE_SERVICE_KEY", "", raising=False)
    try:
        from core import supabase_client as _sc
        monkeypatch.setattr(_sc, "_ready", False, raising=False)
        monkeypatch.setattr(_sc, "_client", None, raising=False)
    except Exception:  # noqa: BLE001
        pass
