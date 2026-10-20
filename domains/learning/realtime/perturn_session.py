"""PM-DEC-864 — 턴마다 새 Live 세션(CONTEXT_MODE=perturn).

🧒 왜: Live 는 비버가 한 번 말할 때마다 세션에 쌓인 대화 전체(오디오 토큰)를 입력으로 다시 과금한다.
  15분 통화면 그 누적이 원가의 대부분이다. 1단계 직접 하네스 실측(call flow · ctx_harness.py)에서
  «비버 턴마다 새 세션 + 지시문 안에 줄거리·최근 대화» 가 운영 압축(8000/7000) 대비 −60%(US$0.476 → 0.192),
  지연 차이 없음이었다.

구조(대리 세션):
  - 이 객체가 LiveSessionProtocol 을 그대로 흉내 낸다 → 2펌프·워처·T22 재연결은 **한 줄도 안 바뀐다.**
  - 안쪽 세션은 «보관 태스크(_hold)» 가 하나씩 소유한다(연결 CM 의 진입·퇴장을 같은 태스크에서 — 다른 태스크에서 나가면
    websockets·anyio 가 깨질 수 있다). 수신은 공용 큐로 모이고 events() 는 **지금 세대**의 것만 내보낸다.
    지난 세대의 usage 만은 흘린다(돈은 이미 나갔다 — 계기판에서 빠지면 안 된다).
  - 다음 세션은 비버 턴이 끝나면(prepare) 미리 연다 — 앱이 그 턴 오디오를 재생하는 동안 연결 시간이 숨는다.
  - 교체는 마이크 자리(on_mic_frame)에서만 — 앱은 비버 재생 중 마이크를 안 보내므로 «프레임이 왔다 = 재생 끝» 이다.
⛔ 동시 연결은 통화당 2개(지금 + 다음)까지다. 다음 세션을 열기 전에 물러난 세션이 닫히기를 기다린다.
⛔ 교체하지 않고 지금 세션을 쓰는 경우 — 다음 세션이 아직 안 열렸는데 학습자가 말을 시작함 · 다음 세션을 연 뒤 제어 텍스트(시드·넛지·
  재접지 완결 턴)가 지금 세션에 들어감 · 비버 턴이 새로 시작됨. 미리 연 세션은 버린다(skipped).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class _Holder:
    gen: int
    ready: asyncio.Future
    close: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None
    session: Any = None
    connect_s: float = 0.0
    stale: str = ""          # 비어 있지 않으면 쓰지 않는다(사유)
    ctrl_at: int = 0         # 준비 시점의 제어 송신 수


class PerTurnSession:
    """턴마다 갈아끼우는 Live 세션 대리(LiveSessionProtocol 흉내 + async CM)."""

    def __init__(self, factory: Callable, client: Any, settings: Any, factory_kwargs: dict) -> None:
        self._factory = factory
        self._client = client
        self._settings = settings
        self._kwargs = dict(factory_kwargs)
        # 새 세션은 재개 핸들 없이 연다 — 핸들로 붙으면 지난 대화 전체가 되살아나 의미가 없다.
        self._kwargs.pop("resume_handle", None)
        self._q: asyncio.Queue = asyncio.Queue()
        self._gen = 0
        self._cur: Optional[_Holder] = None
        self._next: Optional[_Holder] = None
        self._retired: list[_Holder] = []
        self._opener: Optional[asyncio.Task] = None
        self.ctrl_sends = 0       # 오디오·마이크 자리 안내 말고 지금 세션에 들어간 텍스트·도구 응답 수
        self.swaps = 0
        self.skipped: dict[str, int] = {}
        self.open_failures = 0
        self.connect_s: list[float] = []
        self.max_open = 0

    # ── 수명 ──────────────────────────────────────────────────────────────── #
    async def __aenter__(self) -> "PerTurnSession":
        h = self._spawn(self._kwargs["system_instruction"])
        await h.ready        # 첫 세션이 안 열리면 종전처럼 예외로 올린다(T22·오류 경로 그대로)
        self._cur = h
        self.max_open = max(self.max_open, self._open_count())
        return self

    async def __aexit__(self, *exc) -> None:
        if self._opener is not None:
            self._opener.cancel()
        holders = [h for h in (self._cur, self._next, *self._retired) if h is not None]
        for h in holders:
            h.close.set()
        for h in holders:
            if h.task is not None:
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(h.task), 5.0)
                if not h.task.done():
                    h.task.cancel()

    def _open_count(self) -> int:
        return sum(1 for h in (self._cur, self._next, *self._retired) if h is not None and h.task and not h.task.done())

    def _spawn(self, system_instruction: str) -> _Holder:
        self._gen += 1
        h = _Holder(gen=self._gen, ready=asyncio.get_running_loop().create_future(), ctrl_at=self.ctrl_sends)
        h.task = asyncio.create_task(self._hold(h, system_instruction), name=f"nc-perturn-{h.gen}")
        return h

    async def _hold(self, h: _Holder, system_instruction: str) -> None:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        kw = {**self._kwargs, "system_instruction": system_instruction}
        try:
            async with self._factory(self._client, self._settings, **kw) as s:
                h.session = s
                h.connect_s = round(loop.time() - t0, 2)
                if not h.ready.done():
                    h.ready.set_result(True)
                reader = asyncio.create_task(self._read(h))
                closer = asyncio.create_task(h.close.wait())
                try:
                    await asyncio.wait({reader, closer}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for t in (reader, closer):
                        t.cancel()
                    with contextlib.suppress(BaseException):
                        await reader
        except asyncio.CancelledError:
            if not h.ready.done():
                h.ready.cancel()
            raise
        except BaseException as exc:  # noqa: BLE001 - 첫 세션이면 __aenter__ 가, 아니면 events() 가 올린다
            if not h.ready.done():
                h.ready.set_exception(exc)
            else:
                await self._q.put((h.gen, "error", exc))

    async def _read(self, h: _Holder) -> None:
        try:
            async for ev in h.session.events():
                await self._q.put((h.gen, "ev", ev))
            await self._q.put((h.gen, "end", None))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - 지금 세대면 events() 가 그대로 올린다
            await self._q.put((h.gen, "error", exc))

    # ── LiveSessionProtocol ─────────────────────────────────────────────── #
    async def events(self) -> AsyncIterator:
        while True:
            gen, kind, val = await self._q.get()
            if self._cur is None or gen != self._cur.gen:
                # 지난(또는 아직 안 쓴) 세대 — usage 만 흘린다. 나머지는 버린다(그 세션은 이미 물러났다).
                if kind == "ev" and getattr(val, "kind", "") == "usage" and gen < (self._cur.gen if self._cur else 0):
                    yield val
                elif kind == "error":
                    logger.info("normalcall perturn: 물러난 세션 %d 오류(무시): %r", gen, val)
                continue
            if kind == "ev":
                yield val
            elif kind == "end":
                return
            else:
                raise val

    def _s(self):
        return self._cur.session

    def _ctrl(self) -> None:
        self.ctrl_sends += 1
        if self._next is not None and not self._next.stale:
            self._next.stale = "control"

    async def send_audio(self, pcm16_16k: bytes) -> None:
        await self._s().send_audio(pcm16_16k)

    async def send_reground(self, text: str, *, turn_complete: bool = True) -> None:
        # 마이크 자리 얹기(turn_complete=False)는 학습자 발화에 붙는다 — 교체 뒤 새 세션으로 간다. 완결 턴만 제어로 센다.
        if turn_complete:
            self._ctrl()
        await self._s().send_reground(text, turn_complete=turn_complete)

    async def send_text_turn(self, text: str) -> None:
        self._ctrl()
        await self._s().send_text_turn(text)

    async def send_persona(self, text: str) -> None:
        self._ctrl()
        await self._s().send_persona(text)

    async def send_tool_response(self, fn_id, fn_name, **kw) -> None:
        await self._s().send_tool_response(fn_id, fn_name, **kw)

    # ── 교체 ──────────────────────────────────────────────────────────────── #
    @property
    def cur_gen(self) -> int:
        return self._cur.gen if self._cur is not None else 0

    @property
    def has_next(self) -> bool:
        return self._next is not None

    def _skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def _discard(self, why: str) -> None:
        h = self._next
        self._next = None
        if h is None:
            return
        self._skip(why)
        h.close.set()
        self._retired.append(h)
        logger.info("normalcall perturn: 다음 세션 %d 버림(%s)", h.gen, why)

    def prepare(self, system_instruction: str) -> None:
        """비버 턴 끝 — 다음 세션을 미리 연다(논블로킹). 이미 준비된 것이 있으면 버리고 새로."""
        if self._next is not None:
            self._discard("superseded")
        if self._opener is not None and not self._opener.done():
            self._opener.cancel()
        self._opener = asyncio.create_task(self._open_next(system_instruction, self.ctrl_sends), name="nc-perturn-open")

    async def _open_next(self, system_instruction: str, ctrl_at: int) -> None:
        # ⛔ 동시 2개 한도 — 물러난 세션이 다 닫힌 뒤에 연다.
        for h in list(self._retired):
            if h.task is not None and not h.task.done():
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(h.task), 5.0)
        self._retired = [h for h in self._retired if h.task is not None and not h.task.done()]
        if ctrl_at != self.ctrl_sends:
            self._skip("control")
            return
        h = self._spawn(system_instruction)
        h.ctrl_at = ctrl_at
        self._next = h
        self.max_open = max(self.max_open, self._open_count())

    def on_beaver_turn(self) -> None:
        """비버 턴이 새로 시작됐다 — 그 전에 준비한 세션은 이 턴을 모른다."""
        if self._next is not None:
            self._discard("beaver_turn")

    def on_mic_frame(self, voiced: bool) -> bool:
        """마이크 프레임 1개(비버 침묵 자리). 교체했으면 True."""
        h = self._next
        if h is None:
            return False
        if h.stale:
            self._discard(h.stale)
            return False
        if not h.ready.done():
            if voiced:
                self._discard("not_ready")   # 학습자가 이미 지금 세션에 말하기 시작했다 — 이 턴은 지금 세션으로
            return False
        if h.ready.cancelled() or h.ready.exception() is not None:
            self.open_failures += 1
            self._next = None
            self._skip("open_failed")
            logger.warning("normalcall perturn: 다음 세션 %d 연결 실패 — 지금 세션 유지: %r", h.gen,
                           None if h.ready.cancelled() else h.ready.exception())
            return False
        old = self._cur
        self._cur, self._next = h, None
        if old is not None:
            old.close.set()
            self._retired.append(old)
        self.swaps += 1
        self.connect_s.append(h.connect_s)
        logger.info("normalcall perturn: 세션 교체 %d→%d(연결 %.2fs · 교체 %d회)", old.gen if old else 0, h.gen,
                    h.connect_s, self.swaps)
        return True

    def stats(self) -> dict:
        cs = sorted(self.connect_s)
        return {
            "swaps": self.swaps,
            "skipped": dict(self.skipped),
            "open_failures": self.open_failures,
            "max_open": self.max_open,
            "connect_p50": cs[len(cs) // 2] if cs else None,
            "connect_max": cs[-1] if cs else None,
        }
