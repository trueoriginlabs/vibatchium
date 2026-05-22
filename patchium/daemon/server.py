"""Async Unix-socket JSON-RPC server holding the live Patchwright session.

Protocol: one JSON line per direction.
  request : {"id": "<str>", "cmd": "<verb>", "args": {<verb-specific>}}
  response: {"id": "<str>", "ok": true,  "result": <any>}
         OR {"id": "<str>", "ok": false, "error": "<str>"}

Clients (CLI, MCP server) connect, send one request, read one response, close.
The browser session itself is long-lived across many such connections.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Awaitable

from . import handlers, handlers_extra
from .browser import BrowserSession, attach_session, close_session, launch_session
from .paths import DEFAULT_PROFILE_DIR, LOG_PATH, PID_PATH, SOCK_PATH

log = logging.getLogger("patchium.server")


class Daemon:
    # Verbs whose handlers DON'T acquire the global RPC lock. These either
    # block on external events (waits) and need to coexist with the action
    # that triggers the event, or they're cheap read-only state queries.
    # Running them outside the lock means: `patchium wait-response /api/foo` in
    # one shell while `patchium click @e3` in another shell can fire the request
    # — neither blocks on the other.
    UNLOCKED_VERBS = frozenset({
        "ping", "status",
        "wait_selector", "wait_ref", "wait_url", "wait_load", "wait_fn",
        "wait_response", "sleep",
    })

    def __init__(self) -> None:
        self.session: BrowserSession | None = None
        self._handlers: dict[str, Callable[[Daemon, dict], Awaitable[Any]]] = {}
        self._stopping = asyncio.Event()
        # Global RPC lock — serializes mutating verbs so concurrent MCP/CLI clients
        # don't race on session.page, session.frame_ref, _snapshot, etc.
        # Patchright/Playwright operations aren't safe to interleave on the same
        # Page anyway. Wait verbs (see UNLOCKED_VERBS) skip the lock so they
        # can block on events fired by lock-holding verbs running in parallel.
        self._lock = asyncio.Lock()
        # `_snapshot` is the most recent aria_snapshot result; resolves @eN refs.
        # Cleared on navigation (see _invalidate_snapshot() in handlers.py).
        self._snapshot = None
        self._prev_snapshot = None
        # populated by handlers.register_all() + handlers_extra.register_extra() below
        handlers.register_all(self)
        handlers_extra.register_extra(self)

    def handler(self, name: str):
        def deco(fn):
            self._handlers[name] = fn
            return fn
        return deco

    async def dispatch(self, req: dict) -> dict:
        req_id = req.get("id", "")
        cmd = req.get("cmd")
        args = req.get("args") or {}
        if cmd not in self._handlers:
            return {"id": req_id, "ok": False, "error": f"unknown command: {cmd}"}
        try:
            if cmd in self.UNLOCKED_VERBS:
                # Wait verbs and read-only state queries run without the lock so
                # they can block on events triggered by lock-holding verbs.
                result = await self._handlers[cmd](self, args)
            else:
                async with self._lock:
                    result = await self._handlers[cmd](self, args)
            return {"id": req_id, "ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001
            log.exception("handler %s failed", cmd)
            return {"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

    async def handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                resp = {"id": "", "ok": False, "error": f"bad json: {exc}"}
            else:
                resp = await self.dispatch(req)
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def run(self) -> None:
        if SOCK_PATH.exists():
            # check if a live daemon is already using it; catch broadly because
            # OSError (and subclasses) can arrive when the socket file is stale
            # but readable, e.g. orphaned across reboots
            try:
                _, w = await asyncio.open_unix_connection(str(SOCK_PATH))
                w.close()
                await w.wait_closed()
                print(f"[patchium] daemon already running at {SOCK_PATH}", file=sys.stderr)
                sys.exit(2)
            except (OSError, ConnectionRefusedError):
                SOCK_PATH.unlink(missing_ok=True)

        server = await asyncio.start_unix_server(self.handle_conn, path=str(SOCK_PATH))
        os.chmod(SOCK_PATH, 0o600)
        PID_PATH.write_text(str(os.getpid()))

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._stopping.set())

        log.info("daemon listening on %s pid=%s", SOCK_PATH, os.getpid())

        async with server:
            stopper = asyncio.create_task(self._stopping.wait())
            serving = asyncio.create_task(server.serve_forever())
            done, pending = await asyncio.wait(
                {stopper, serving}, return_when=asyncio.FIRST_COMPLETED
            )
            # cancel any still-pending task and await its cancellation cleanly
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

        await self.shutdown()

    async def shutdown(self) -> None:
        log.info("daemon shutting down")
        if self.session is not None:
            with contextlib.suppress(Exception):
                await close_session(self.session)
            self.session = None
        with contextlib.suppress(Exception):
            SOCK_PATH.unlink()
        with contextlib.suppress(Exception):
            PID_PATH.unlink()


def main() -> None:
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()
