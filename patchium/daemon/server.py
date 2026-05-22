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

from . import handlers
from .browser import BrowserSession, attach_session, close_session, launch_session
from .paths import DEFAULT_PROFILE_DIR, LOG_PATH, PID_PATH, SOCK_PATH

log = logging.getLogger("patchium.server")


class Daemon:
    def __init__(self) -> None:
        self.session: BrowserSession | None = None
        self._handlers: dict[str, Callable[[Daemon, dict], Awaitable[Any]]] = {}
        self._stopping = asyncio.Event()
        # populated by handlers.register_all() below
        handlers.register_all(self)

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
            # check if a live daemon is already using it
            try:
                _, w = await asyncio.open_unix_connection(str(SOCK_PATH))
                w.close()
                await w.wait_closed()
                print(f"[patchium] daemon already running at {SOCK_PATH}", file=sys.stderr)
                sys.exit(2)
            except (FileNotFoundError, ConnectionRefusedError):
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
            done, _ = await asyncio.wait(
                {stopper, serving}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                t.cancel()

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
