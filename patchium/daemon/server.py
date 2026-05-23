"""Async Unix-socket JSON-RPC server holding the live Patchwright session(s).

Protocol: one JSON line per direction.
  request : {"id": "<str>", "cmd": "<verb>", "args": {<verb-specific>}}
  response: {"id": "<str>", "ok": true,  "result": <any>}
         OR {"id": "<str>", "ok": false, "error": "<str>"}

Multi-session (Wave 5+): requests may include `"args": {"_session": "<name>"}`
to address a specific session. Without the field, the request hits the active
session (`~/.config/patchium/active-session` → 'default'). The daemon holds
multiple BrowserSessions concurrently via SessionRegistry, with per-session
locks so verbs on DIFFERENT sessions don't serialize.

Each session runs in its own Chrome process (separate `launch_persistent_context`)
giving real fingerprint isolation — independent TLS/GPU/audio, independent
ephemeral ports. ~200-400 MB RAM per session; cap via PATCHIUM_MAX_SESSIONS.

Clients (CLI, MCP server) connect, send one request, read one response, close.
Sessions are long-lived across many such connections.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from typing import Any, Awaitable, Callable

from . import handlers, handlers_extra
from .paths import DEFAULT_SESSION_NAME, LOG_PATH, PID_PATH, SOCK_PATH, get_active_session_name
from .registry import SessionEntry, SessionRegistry, current_session_ctx

log = logging.getLogger("patchium.server")


class Daemon:
    # Verbs whose handlers DON'T acquire a per-session lock. These either
    # block on external events (waits) and need to coexist with the action
    # that triggers the event, or they're cheap read-only state queries.
    UNLOCKED_VERBS = frozenset({
        "ping", "status",
        "wait_selector", "wait_ref", "wait_url", "wait_load", "wait_fn",
        "wait_response", "sleep",
    })

    # Verbs that mutate the registry itself (create/destroy sessions, switch
    # active session, daemon-level queries that don't need a session). These
    # acquire the registry.mutate_lock instead of a per-session lock.
    REGISTRY_VERBS = frozenset({
        "start", "attach", "stop", "shutdown",
        "session_new", "session_list", "session_use", "session_switch",
        "session_close", "session_close_all", "session_delete",
        "profile_list", "profile_new", "profile_use", "profile_delete",
    })

    def __init__(self) -> None:
        self.registry = SessionRegistry()
        self._handlers: dict[str, Callable[[Daemon, dict], Awaitable[Any]]] = {}
        self._stopping = asyncio.Event()
        handlers.register_all(self)
        handlers_extra.register_extra(self)

    # ─── handler registration ────────────────────────────────────────────

    def handler(self, name: str):
        def deco(fn):
            self._handlers[name] = fn
            return fn
        return deco

    # ─── session-routed properties (drop-in replacements for the old single-
    #     session attributes that handlers still write to)
    #
    # The dispatcher sets `current_session_ctx` to the current call's session
    # name before invoking the handler. These properties read/write the
    # corresponding SessionEntry's state, so handlers keep using `d.session`,
    # `d._snapshot`, etc., unchanged.

    def _current_entry(self) -> SessionEntry | None:
        return self.registry.get(current_session_ctx.get())

    @property
    def session(self):
        entry = self._current_entry()
        return entry.session if entry else None

    @session.setter
    def session(self, value):
        # The only writer in legacy code was lifecycle handlers (`start`/`attach`/
        # `stop`). Those now go through the registry; this setter exists only
        # to satisfy any remaining attribute writes (notably `d.session = None`
        # in the old _stop handler — now a no-op).
        if value is None:
            name = current_session_ctx.get()
            entry = self.registry.get(name)
            if entry is not None:
                # Caller wanted to "stop" — actually close via the registry.
                # Schedule and return; sync setter can't await, but
                # SessionRegistry.close is the explicit path now.
                pass
        # Non-None assignments are unused in the new code path.

    @property
    def _snapshot(self):
        entry = self._current_entry()
        return entry.snapshot if entry else None

    @_snapshot.setter
    def _snapshot(self, value):
        entry = self._current_entry()
        if entry is not None:
            entry.snapshot = value

    @property
    def _prev_snapshot(self):
        entry = self._current_entry()
        return entry.prev_snapshot if entry else None

    @_prev_snapshot.setter
    def _prev_snapshot(self, value):
        entry = self._current_entry()
        if entry is not None:
            entry.prev_snapshot = value

    @property
    def _handles(self) -> dict:
        entry = self._current_entry()
        if entry is None:
            # Return a throwaway dict so handlers that do `d._handles[hid] = h`
            # don't blow up when there's no session — the write will simply be
            # lost (which matches the "no session" precondition error we'd
            # raise anyway in the session-needing handler).
            return {}
        return entry.handles

    @property
    def _handle_counter(self) -> int:
        entry = self._current_entry()
        return entry.handle_counter if entry else 0

    @_handle_counter.setter
    def _handle_counter(self, value: int) -> None:
        entry = self._current_entry()
        if entry is not None:
            entry.handle_counter = value

    # ─── dispatch ────────────────────────────────────────────────────────

    async def dispatch(self, req: dict) -> dict:
        req_id = req.get("id", "")
        cmd = req.get("cmd")
        args = req.get("args") or {}
        # Extract + consume the session selector; default to active session.
        session_name = args.pop("_session", None) or get_active_session_name()
        if cmd not in self._handlers:
            return {"id": req_id, "ok": False, "error": f"unknown command: {cmd}"}

        # Push the selected session into the contextvar so handlers (via the
        # session-routed properties above) operate on the right SessionEntry.
        tok = current_session_ctx.set(session_name)
        try:
            if cmd in self.REGISTRY_VERBS:
                # Registry mutation — serialized by the registry's mutate_lock
                # so concurrent session_new / start can't race on the dict.
                async with self.registry.mutate_lock:
                    result = await self._handlers[cmd](self, args)
            elif cmd in self.UNLOCKED_VERBS:
                # Cheap reads + waits — no lock.
                result = await self._handlers[cmd](self, args)
            else:
                # Session-scoped verb — needs the per-session lock so concurrent
                # mutations on the SAME session don't trash session.page / snapshot.
                # Different-session mutations run in parallel because each has its own lock.
                entry = self.registry.get(session_name)
                if entry is None:
                    return {
                        "id": req_id, "ok": False,
                        "error": f"no session {session_name!r} — "
                                 f"run `patchium start"
                                 f"{' --session ' + session_name if session_name != DEFAULT_SESSION_NAME else ''}` first",
                    }
                async with entry.lock:
                    result = await self._handlers[cmd](self, args)
            return {"id": req_id, "ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001
            log.exception("handler %s failed (session=%s)", cmd, session_name)
            return {"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            current_session_ctx.reset(tok)

    # ─── socket plumbing ─────────────────────────────────────────────────

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
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

        await self.shutdown()

    async def shutdown(self) -> None:
        log.info("daemon shutting down — closing %d sessions",
                 len(self.registry.list_running()))
        with contextlib.suppress(Exception):
            await self.registry.close_all()
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
