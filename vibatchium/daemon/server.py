"""Async Unix-socket JSON-RPC server holding the live Patchwright session(s).

Protocol: one JSON line per direction.
  request : {"id": "<str>", "cmd": "<verb>", "args": {<verb-specific>}}
  response: {"id": "<str>", "ok": true,  "result": <any>}
         OR {"id": "<str>", "ok": false, "error": "<str>"}

Multi-session (Wave 5+): requests may include `"args": {"_session": "<name>"}`
to address a specific session. Without the field, the request hits the active
session (`~/.config/vibatchium/active-session` → 'default'). The daemon holds
multiple BrowserSessions concurrently via SessionRegistry, with per-session
locks so verbs on DIFFERENT sessions don't serialize.

Each session runs in its own Chrome process (separate `launch_persistent_context`)
giving real fingerprint isolation — independent TLS/GPU/audio, independent
ephemeral ports. ~200-400 MB RAM per session; cap via VIBATCHIUM_MAX_SESSIONS.

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
from typing import Any
from collections.abc import Awaitable, Callable

from . import handlers, handlers_extra
from ..caps import resolve_caps as _resolve_caps, verb_in_caps
from .paths import DEFAULT_SESSION_NAME, LOG_PATH, PID_PATH, SOCK_PATH, get_active_session_name
from .registry import SessionEntry, SessionRegistry, current_session_ctx

log = logging.getLogger("vibatchium.server")


# Wave 7.5e: fields that must be redacted from per-verb DEBUG logs.
# Maps verb name → set of arg keys whose values should be replaced with
# `<redacted>` before logging. Conservative — when in doubt, redact.
_REDACTED_ARG_FIELDS: dict[str, set[str]] = {
    "secret_set":        {"value"},          # the secret material itself
    "fill":              {"text"},           # may be a password / secret value
    "type":              {"text"},           # same
    "keys":              {"keys"},           # may be typed password
    "press":             {"keys"},
    "proxy_set":         {"url"},            # contains user:pass@host
    "eval":              {"expr"},           # may include inline credentials
    "eval_handle":       {"expr"},
    "handle_eval":       {"expr"},
    "route_add":         {"body", "json"},   # mock content may contain secrets
    "secret_init":       {"key"},            # base64 key material
}


def _redact_for_log(cmd: str, args: dict) -> dict:
    """Strip sensitive fields from args before they hit the log file.

    Returns a SHALLOW COPY of `args` with sensitive values replaced by
    `<redacted>`. Caller-supplied free-text fields (eval expressions,
    type / fill text) are conservatively redacted because they're the
    most likely vector for accidentally logging passwords / tokens.
    """
    redact = _REDACTED_ARG_FIELDS.get(cmd)
    if not redact:
        return args
    out = dict(args)
    for k in redact:
        if k in out:
            out[k] = "<redacted>"
    return out


class Daemon:
    # Verbs whose handlers DON'T acquire a per-session lock. These either
    # block on external events (waits) and need to coexist with the action
    # that triggers the event, or they're cheap read-only state queries.
    UNLOCKED_VERBS = frozenset({
        "ping", "status",
        "wait_selector", "wait_ref", "wait_url", "wait_load", "wait_fn",
        "wait_response", "sleep",
        # Wave 6.3b: email-code polling is a long-running wait — don't hold
        # the registry mutate lock.
        "wait_email_code",
        # Wave 7.6: pure utilities — no session, no registry mutation
        "verify_url",      # DNS / HTTP pre-check before committing to `go`
        "set_log_verbs",   # runtime toggle for the per-verb DEBUG audit log
        # Wave 7.7.5: high-level orchestration verbs that manage their own
        # session lifecycle — they call into other handlers explicitly
        # (auto-start, go, text, stop) so they don't need the session lock
        # held at this layer.
        "explore",
    })

    # Wave 7.7.5: verbs that can auto-start a session when one isn't
    # running yet. The dispatcher's "no session" rejection is bypassed for
    # these — they handle the missing-session case themselves (typically
    # by calling into `start` first). The per-session lock IS still
    # acquired after auto-start completes, so concurrent same-session
    # mutations stay safe.
    SESSION_AUTOSTART_VERBS = frozenset({
        "go",  # auto-starts headless when called without a prior `start`
    })

    # Verbs that mutate the registry itself (create/destroy sessions, switch
    # active session, daemon-level queries that don't need a session). These
    # acquire the registry.mutate_lock instead of a per-session lock.
    # Wave 6.2a: proxy_* verbs touch the profile dir and don't require a
    # running session; checkpoint_list/delete are file ops on the profile.
    REGISTRY_VERBS = frozenset({
        "start", "attach", "stop", "shutdown",
        "session_new", "session_list", "session_use", "session_switch",
        "session_close", "session_close_all", "session_delete",
        "profile_list", "profile_new", "profile_use", "profile_delete",
        # Wave 7.8: housekeeping — prunes profile dirs / caches, no session.
        "clean",
        "proxy_set", "proxy_clear", "proxy_info",
        "checkpoint_list", "checkpoint_delete",
        # Wave 6.3a: secrets are not session-scoped
        "secret_init", "secret_set", "secret_list", "secret_delete", "secret_totp",
    })

    def __init__(self) -> None:
        self.registry = SessionRegistry()
        self._handlers: dict[str, Callable[[Daemon, dict], Awaitable[Any]]] = {}
        self._stopping = asyncio.Event()
        # ─── plugin state (see vibatchium/plugins/) ──────────────────────
        # _verb_meta: name → VerbSpec for every add_verb-registered verb.
        # _verb_lock_class: name → "session"|"registry"|"unlocked" override
        #   consulted by dispatch() before the built-in *_VERBS frozensets.
        # _plugin_verbs: the subset of verbs that came from plugins (so
        #   plugin_reload can drop them without touching built-ins).
        # _plugins: name → metadata dict (source, version, verbs, error).
        # _loading_plugin: the plugin name currently being register()ed, so
        #   add_verb can attribute verbs to their source plugin.
        from ..plugins.api import VerbSpec as _VerbSpec  # noqa: F401  (typing/ref)
        self._verb_meta: dict[str, Any] = {}
        self._verb_lock_class: dict[str, str] = {}
        self._plugin_verbs: set[str] = set()
        self._plugins: dict[str, dict] = {}
        self._loading_plugin: str | None = None
        # Wave 7.6: daemon-level flags (runtime-mutable). `log_verbs` controls
        # per-verb DEBUG logging; initial value from env so existing scripts
        # that set VIBATCHIUM_LOG_VERBS=1 keep working without a daemon restart
        # being needed to change it.
        self.flags: dict[str, Any] = {
            "log_verbs": os.environ.get("VIBATCHIUM_LOG_VERBS", "0") in ("1", "true", "yes"),
        }
        handlers.register_all(self)
        handlers_extra.register_extra(self)
        # Built-in plugin-admin verbs (plugin_list/show/reload, list_verbs).
        from ..plugins.handlers import register_admin_verbs
        register_admin_verbs(self)
        # Built-in Skills verbs (skill_list/show/write/rm/import).
        from ..skills.handlers import register_skill_verbs
        register_skill_verbs(self)
        # Built-in Goals verbs (goal_new/list/show/next/step/...).
        from ..goals.handlers import register_goal_verbs
        register_goal_verbs(self)
        # Discover + load plugins. Opt out with VIBATCHIUM_PLUGINS=0. Isolated
        # per plugin — a broken plugin is logged and skipped, never fatal.
        if os.environ.get("VIBATCHIUM_PLUGINS", "1").lower() not in ("0", "false", "no", "off"):
            try:
                from ..plugins import registry as _plugin_registry
                _plugin_registry.load_into(self)
            except Exception:  # noqa: BLE001
                log.exception("plugin loading failed (continuing without plugins)")

    # ─── handler registration ────────────────────────────────────────────

    def handler(self, name: str):
        def deco(fn):
            self._handlers[name] = fn
            return fn
        return deco

    # ─── plugin verb registration (the add_verb contract) ─────────────────

    def add_verb(
        self,
        name: str,
        handler,
        *,
        inputs_schema: dict | None = None,
        outputs_schema: dict | None = None,
        caps_required: list[str] | None = None,
        secrets_required: list[str] | None = None,
        description: str = "",
        lock: str = "session",
    ) -> None:
        """Register a plugin verb. Called from a plugin's ``register(daemon)``.

        ``handler`` is ``async def(daemon, args: dict) -> JSON-serializable`` —
        the same signature as built-in handlers, so the plugin can drive the
        live session via ``daemon.session`` in-process.

        ``caps_required`` / ``secrets_required`` are *descriptive only*; the
        daemon cannot enforce them against in-process plugin code (which runs
        as your user). See ``vibatchium/plugins/__init__.py``.

        ``lock`` picks the dispatch lock class: ``"session"`` (default — needs
        a running session + per-session lock), ``"registry"`` (serialized by
        the registry mutate lock, no session), or ``"unlocked"`` (cheap/no
        session). Raises ``PluginError`` on a bad name / collision.
        """
        from ..plugins.api import VerbSpec, PluginError
        spec = VerbSpec(
            name=name,
            handler=handler,
            inputs_schema=inputs_schema or {},
            outputs_schema=outputs_schema or {},
            caps_required=list(caps_required or []),
            secrets_required=list(secrets_required or []),
            description=description,
            lock=lock,
            plugin=self._loading_plugin,
        )
        if name in self._handlers and name not in self._plugin_verbs:
            raise PluginError(
                f"verb {name!r} would shadow a built-in verb — refused"
            )
        if name in self._plugin_verbs:
            raise PluginError(
                f"verb {name!r} already registered by another plugin — refused"
            )
        self._handlers[name] = handler
        self._verb_meta[name] = spec
        self._verb_lock_class[name] = lock
        self._plugin_verbs.add(name)
        if self._loading_plugin and self._loading_plugin in self._plugins:
            self._plugins[self._loading_plugin]["verbs"].append(name)

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

        # Wave 7.5e + 7.6: opt-in per-verb DEBUG log. Off by default because
        # (a) noisy and (b) args may contain large strings (eval scripts).
        # Read from daemon.flags so `set_log_verbs` can toggle at runtime
        # without a daemon restart. Sensitive fields are redacted before
        # logging.
        if self.flags.get("log_verbs"):
            log.debug("verb session=%s cmd=%s args=%s",
                      session_name, cmd, _redact_for_log(cmd, args))

        # Resolve the dispatch lock class. Plugin verbs (and the plugin-admin
        # built-ins) carry an explicit class in _verb_lock_class; everything
        # else falls back to the built-in *_VERBS frozensets, defaulting to
        # session-scoped.
        lock_class = self._verb_lock_class.get(cmd)
        if lock_class is None:
            if cmd in self.REGISTRY_VERBS:
                lock_class = "registry"
            elif cmd in self.UNLOCKED_VERBS:
                lock_class = "unlocked"
            else:
                lock_class = "session"

        # Push the selected session into the contextvar so handlers (via the
        # session-routed properties above) operate on the right SessionEntry.
        tok = current_session_ctx.set(session_name)
        try:
            if lock_class == "registry":
                # Registry mutation — serialized by the registry's mutate_lock
                # so concurrent session_new / start can't race on the dict.
                async with self.registry.mutate_lock:
                    result = await self._handlers[cmd](self, args)
            elif lock_class == "unlocked":
                # Cheap reads + waits — no lock.
                result = await self._handlers[cmd](self, args)
            else:
                # Session-scoped verb — needs the per-session lock so concurrent
                # mutations on the SAME session don't trash session.page / snapshot.
                # Different-session mutations run in parallel because each has its own lock.
                entry = self.registry.get(session_name)
                if entry is None:
                    # Wave 7.7.5: auto-start verbs handle the missing session
                    # themselves by calling into `start` from within the
                    # handler. Skip the dispatcher-level rejection so the
                    # handler can fire; the registry lookup will succeed
                    # on a follow-up dispatcher call once `start` returns.
                    if cmd in self.SESSION_AUTOSTART_VERBS:
                        result = await self._handlers[cmd](self, args)
                    else:
                        return {
                            "id": req_id, "ok": False,
                            "error": f"no session {session_name!r} — "
                                     f"run `vb start"
                                     f"{' --session ' + session_name if session_name != DEFAULT_SESSION_NAME else ''}` first",
                        }
                else:
                    # Per-goal caps enforcement (D5): while a goal owns this
                    # session it pins it to a cap set via entry.flags. Reject
                    # out-of-bucket verbs at the socket boundary. (Plugin
                    # in-process code remains the documented trust boundary.)
                    goal_caps = entry.flags.get("goal_caps")
                    if goal_caps:
                        try:
                            allowed = verb_in_caps(cmd, _resolve_caps(goal_caps))
                        except Exception:  # noqa: BLE001
                            allowed = True  # bad caps string → don't lock out
                        if not allowed:
                            return {
                                "id": req_id, "ok": False,
                                "error": f"verb {cmd!r} blocked by goal caps "
                                         f"({goal_caps}) on session "
                                         f"{session_name!r}",
                            }
                    async with entry.lock:
                        result = await self._handlers[cmd](self, args)
            # Wave 6.3c: prompt-injection middleware. Off by default; per-session
            # flag controls activation. Mutates content fields in-place.
            entry = self.registry.get(session_name)
            if entry is not None and isinstance(result, dict):
                mode = entry.flags.get("safety_mode")
                if mode and mode != "off":
                    from .. import safety as _safety
                    result = _safety.scan_response(cmd, result, mode)
            return {"id": req_id, "ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001
            log.exception("handler %s failed (session=%s)", cmd, session_name)
            from .handlers import SessionNotStarted
            # SessionNotStarted carries a user-facing message that already
            # matches the dispatcher-level format — emit it bare so wait_*
            # and other UNLOCKED_VERBS see the same error string as click/fill.
            if isinstance(exc, SessionNotStarted):
                return {"id": req_id, "ok": False, "error": str(exc)}
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
                print(f"[vibatchium] daemon already running at {SOCK_PATH}", file=sys.stderr)
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

        # Wave 6.1b: eager Playwright driver pre-start (if enabled).
        # Non-blocking from the user's perspective; happens before first conn.
        await self.registry.warmup()

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
        # Wave 6.1a: shut down live-view server first so frame loops stop
        # before sessions tear down (avoids "page closed" exceptions in flight).
        lv = getattr(self, "_liveview_server", None)
        if lv is not None:
            with contextlib.suppress(Exception):
                await lv.stop()
            self._liveview_server = None
        with contextlib.suppress(Exception):
            await self.registry.close_all()
        with contextlib.suppress(Exception):
            SOCK_PATH.unlink()
        with contextlib.suppress(Exception):
            PID_PATH.unlink()


def main() -> None:
    # Wave 7.5e: level controlled by VIBATCHIUM_LOG_LEVEL (default INFO).
    # Setting it to DEBUG together with VIBATCHIUM_LOG_VERBS=1 produces a
    # full audit trail of every verb dispatched.
    level_name = os.environ.get("VIBATCHIUM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        filename=str(LOG_PATH),
        level=level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Wave 7.5d/e fix: basicConfig opens the file with mode inherited from
    # umask (typically 0664). The daemon log can include site names from
    # `secret set` and other low-but-real-sensitivity metadata. Force 0600.
    try:
        os.chmod(LOG_PATH, 0o600)
    except OSError:
        pass
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()
