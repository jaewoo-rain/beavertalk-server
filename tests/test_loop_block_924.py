"""PM-DEC-924 — 이벤트 루프를 막는 호출(동기 GCS 업로드 · 비동기 gRPC 의 루프 안 토큰 갱신) 회귀 시험."""
import ast
import asyncio
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core import tts

ROOT = Path(__file__).resolve().parents[1]


def _sync_uploads_in_async(rel: str) -> list[int]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    bad = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_AsyncFunctionDef(self, n):
            self.stack.append("async"); self.generic_visit(n); self.stack.pop()

        def visit_FunctionDef(self, n):
            self.stack.append("def"); self.generic_visit(n); self.stack.pop()

        def visit_Lambda(self, n):
            self.stack.append("lambda"); self.generic_visit(n); self.stack.pop()

        def visit_Call(self, n):
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr in ("upload", "download") and isinstance(f.value, ast.Name)
                    and f.value.id == "storage" and self.stack and self.stack[-1] == "async"):
                bad.append(n.lineno)
            self.generic_visit(n)

    V().visit(tree)
    return bad


@pytest.mark.parametrize("rel", [
    "domains/learning/service/normalcall_service.py",
    "domains/learning/service/cur_item_tts_service.py",
    "domains/learning/service/sentence_service.py",
])
def test_no_sync_storage_call_inside_async_functions(rel):
    assert _sync_uploads_in_async(rel) == [], f"{rel}: async 함수 안 동기 storage 호출(루프 정지)"


class _Creds:
    def __init__(self, token=None, expiry=None, delay=0.3):
        self.token, self.expiry, self.delay, self.refreshed = token, expiry, delay, 0

    def refresh(self, request):
        time.sleep(self.delay)
        self.token = "t"
        self.expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        self.refreshed += 1


@pytest.mark.asyncio
async def test_token_refresh_runs_off_the_event_loop():
    c = _Creds()
    ticks = []

    async def ticker():
        for _ in range(5):
            ticks.append(1)
            await asyncio.sleep(0.02)

    await asyncio.gather(tts.refresh_creds_off_loop(c), ticker())
    assert c.refreshed == 1 and len(ticks) == 5


@pytest.mark.asyncio
async def test_valid_token_is_not_refreshed():
    c = _Creds(token="t", expiry=datetime.now(timezone.utc) + timedelta(minutes=30))
    await tts.refresh_creds_off_loop(c)
    assert c.refreshed == 0
    soon = _Creds(token="t", expiry=datetime.utcnow() + timedelta(minutes=2))   # tz 없는 만료(google-auth 형식)
    await tts.refresh_creds_off_loop(soon)
    assert soon.refreshed == 1
    await tts.refresh_creds_off_loop(None)
