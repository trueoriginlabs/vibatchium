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
import fcntl
import json
import logging
import os
import signal
import sys
from logging.handlers import RotatingFileHandler
import time
from typing import Any
from collections.abc import Awaitable, Callable

from . import handlers, handlers_extra
from . import lease as _lease
from ..caps import resolve_caps as _resolve_caps, verb_in_caps
from .paths import (
    CACHE_DIR, DEFAULT_SESSION_NAME, LOCK_PATH, LOG_PATH, PID_PATH, SOCK_PATH,
    get_active_session_name,
)
from . import freeze as _freeze
from .registry import SessionEntry, SessionRegistry, current_session_ctx

log = logging.getLogger("vibatchium.server")


# Wave 7.5e: fields that must be redacted from per-verb DEBUG logs.
# Maps verb name → set of arg keys whose values should be replaced with
# `<redacted>` before logging. Conservative — when in doubt, redact.
_REDACTED_ARG_FIELDS: dict[str, set[str]] = {
    "secret_set":        {"value"},          # the secret material itself
    "fill":              {"text"},           # may be a password / secret value
    "type":              {"text"},           # same
    "vision_type":       {"text"},           # typed via the vision verb — same
    "keys":              {"keys"},           # may be typed password
    "press":             {"keys"},
    "proxy_set":         {"url"},            # contains user:pass@host
    "eval":              {"expr"},           # may include inline credentials
    "eval_handle":       {"expr"},
    "handle_eval":       {"expr"},
    "route_add":         {"body", "headers"},  # mock body + headers may carry auth
    "fetch":             {"headers", "json", "data", "params"},  # may carry Authorization / login payloads / API keys
    # NOTE: secret_init has no sensitive *args* — only prefer/force/print_key.
    # The generated key (`key_b64`) is in the *response*, gated behind
    # `print_key`, and responses are never logged through this path. A redaction
    # entry here would protect a field that never reaches this arg-only redactor
    # (the previous `{"key"}` did nothing), so there deliberately isn't one.
}

# 0.6.11: verbs whose args are URLs/patterns that MAY embed `user:pass@host`
# (HTTP basic-auth form). Rather than nuke the whole field (losing the host,
# which is useful in a debug log), mask only the userinfo — keeping the
# "credentials never appear in logs" guarantee while staying debuggable.
# (proxy_set stays in the whole-redact map above: proxy URLs can also carry
# secrets in query params, so the entire URL is replaced.)
_URL_ARG_FIELDS: dict[str, set[str]] = {
    "go":         {"url"},
    "verify_url": {"url"},
    "wait_url":   {"pattern"},
    "fetch":      {"url"},   # may embed user:pass@host basic-auth
}


def _redact_for_log(cmd: str, args: dict) -> dict:
    """Strip sensitive fields from args before they hit the log file.

    Returns a SHALLOW COPY of `args` with sensitive values replaced by
    `<redacted>`. Caller-supplied free-text fields (eval expressions,
    type / fill text) are conservatively redacted because they're the
    most likely vector for accidentally logging passwords / tokens.
    """
    redact = _REDACTED_ARG_FIELDS.get(cmd)
    url_fields = _URL_ARG_FIELDS.get(cmd)
    # Always return a shallow COPY (docstring contract) — never the caller's
    # dict, even when there's nothing to strip.
    out = dict(args)
    for k in (redact or ()):
        if k in out:
            out[k] = "<redacted>"
    if url_fields:
        from ..proxy import mask_userinfo
        for k in url_fields:
            if isinstance(out.get(k), str):
                out[k] = mask_userinfo(out[k])
    # 0.7.0: a lease token can ride ANY verb as args['_lease'] — never log it.
    if "_lease" in out:
        out["_lease"] = "<redacted>"
    return out


def _pidfile_daemon_alive() -> bool:
    """0.9.1: True if PID_PATH names a live vibatchium daemon process. A
    latency-independent liveness signal used when the socket connect-probe times
    out (a live-but-slow incumbent under memory pressure must not be orphaned)."""
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True            # exists (owned by another user) — assume alive
    # Guard against PID reuse: confirm it's actually a vibatchium daemon.
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"vibatchium.daemon.server" in fh.read()
    except OSError:
        return True            # pid is alive but cmdline unreadable — be safe


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

    # 0.18.6 idle-freeze fix: the UNLOCKED verbs that DRIVE this session's
    # renderer (wait on a selector/ref/url/load-state/function, or the explore
    # orchestrator). They need the renderer RUNNING, but the idle-freezer only
    # thaws on the LOCKED verb path — so a wait issued against a parked
    # (SIGSTOPped) session, or one that crosses the idle threshold mid-wait,
    # would stall on a stopped renderer. For these, the dispatcher thaws first
    # and marks the session in-flight (see below). Deliberately EXCLUDED:
    # `sleep` (asyncio.sleep — no renderer), `wait_email_code` (IMAP poll on a
    # worker thread — no renderer), `wait_response` (coexists with the LOCKED
    # action that triggers the response, which thaws) — freezing a renderer
    # during any of those is harmless and keeps CPU-relief working; and
    # `ping`/`status`/`verify_url`/`set_log_verbs` (no session, or `status`
    # must report `idle_frozen` truthfully without thawing).
    PAGE_WAIT_VERBS = frozenset({
        "wait_selector", "wait_ref", "wait_url", "wait_load", "wait_fn",
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

    # 0.12.0: verbs that CAN run with no session at all (no auto-start, no
    # session lock) when one isn't present — the dispatcher lets them through
    # instead of rejecting with "no session", and the handler decides whether
    # it can proceed sessionless or must raise a precondition error. `fetch` is
    # the case: an anonymous --no-cookies GET carries no session state (no
    # cookies/UA/proxy to reuse), so forcing a full Chrome session for it was
    # pure ceremony. When a session DOES exist, fetch still runs under the
    # per-session lock and reuses its identity exactly as before.
    SESSIONLESS_FALLBACK_VERBS = frozenset({
        "fetch",
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
        # 0.6.11: timezone/locale coherence — profile-dir config, no session.
        "geo_set", "geo_clear", "geo_info",
        # 0.13.0: headless GPU WebGL — profile-dir config; gpu_info touches the live
        # page but takes entry.lock ITSELF (like geo_info), so it must be a registry
        # verb — otherwise the dispatcher pre-holds entry.lock and the internal
        # acquire deadlocks (asyncio.Lock is non-reentrant).
        "gpu_set", "gpu_clear", "gpu_info",
        # 0.7.0: session leases (registry-class — they mutate the entry's lease
        # field under the contextvar; no per-session page lock needed).
        "session_lease", "session_release", "session_lease_info",
        "checkpoint_list", "checkpoint_delete",
        # Wave 6.3a: secrets are not session-scoped
        "secret_init", "secret_set", "secret_list", "secret_delete", "secret_totp",
    })

    # 0.7.0 self-heal: verbs safe to auto-retry ONCE after a renderer-crash
    # recovery — navigation + pure reads only. A mutating verb may have
    # committed a side-effect server-side before the renderer died, so it is
    # recovered but NOT retried (the caller re-issues it).
    RETRY_SAFE_VERBS = frozenset({
        "go", "reload", "back", "forward",      # navigation (didn't commit on crash)
        "text", "html", "extract", "extract_fields", "pdf",  # pure reads (NOT `content` = set_content)
        "screenshot", "map", "observe",         # read-only page analysis
        "detect_forms", "candidates",            # read-only DOM inspection (0.15.0)
        "url", "title", "frames",
        "console_dump",                          # reads the in-memory ring buffer
    })

    # 0.7.0 lease: registry verbs that DISRUPT a leased session (tear it down or
    # reconfigure it) — a non-holder is refused these while a lease is active.
    # session_close_all / shutdown / clean are deliberately ABSENT: those are
    # operator sledgehammers and must always work.
    LEASE_GUARDED_REGISTRY_VERBS = frozenset({
        "stop", "session_close", "session_delete",
        "proxy_set", "proxy_clear", "geo_set", "geo_clear",
        # 0.13.0: gpu_set/gpu_clear mutate persisted per-session config (gpu_info is
        # a read, so it's absent — mirrors geo_info/proxy_info).
        "gpu_set", "gpu_clear",
    })

    def __init__(self) -> None:
        self.registry = SessionRegistry()
        self._handlers: dict[str, Callable[[Daemon, dict], Awaitable[Any]]] = {}
        self._stopping = asyncio.Event()
        # 0.9.1 singleton + idle reaper: the held flock fd (None until acquired)
        # and a monotonic activity stamp the reaper uses to decide idleness.
        self._lock_fd: int | None = None
        self._last_activity: float = time.monotonic()
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
        self._last_activity = time.monotonic()   # 0.9.1: feeds the idle reaper
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
                # 0.7.0 lease gate: refuse the DISRUPTIVE registry verbs (those
                # that tear down or reconfigure a session) from a non-holder
                # while a lease is active. Non-disruptive registry verbs
                # (session_new/list/use, secrets, the lease verbs themselves)
                # are never gated.
                if cmd in self.LEASE_GUARDED_REGISTRY_VERBS:
                    target = (args.get("name") or session_name
                              if cmd in ("session_close", "session_delete")
                              else session_name)
                    g_entry = self.registry.get(target)
                    if g_entry is not None:
                        active = g_entry.lease_active()
                        if active is not None:
                            ok_lease, reason = _lease.check_access(
                                active, _lease.holder_token_from_args(args), target)
                            if not ok_lease:
                                return {"id": req_id, "ok": False, "error": reason}
                # Registry mutation — serialized by the registry's mutate_lock
                # so concurrent session_new / start can't race on the dict.
                async with self.registry.mutate_lock:
                    result = await self._handlers[cmd](self, args)
            elif lock_class == "unlocked":
                # Cheap reads + waits — no lock. But the page-driving waits
                # (and explore) need this session's renderer RUNNING: the
                # idle-freezer only thaws on the LOCKED verb path
                # (_run_session_verb_with_recovery), so a wait against a parked
                # session — or one that crosses the idle threshold mid-wait —
                # would stall on a SIGSTOPped renderer (0.18.6 fix).
                if cmd in self.PAGE_WAIT_VERBS:
                    # get() also stamps activity, so the wait's own start
                    # resets the idle clock (a bare wait used to inherit the
                    # PRIOR verb's timestamp and freeze mid-wait).
                    entry = self.registry.get(session_name)
                    if entry is not None:
                        # Bump BEFORE the frozen check so the freezer — which
                        # also consults inflight — can't slip a SIGSTOP in.
                        entry.inflight += 1
                        try:
                            if entry.frozen:
                                # Uncontended when frozen (no verb holds the
                                # lock — that's WHY it froze), so this acquires
                                # instantly and never serializes the wait
                                # behind a concurrent locked action.
                                async with entry.lock:
                                    await _freeze.lift(entry)
                            result = await self._handlers[cmd](self, args)
                        finally:
                            entry.inflight -= 1
                    else:
                        result = await self._handlers[cmd](self, args)
                else:
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
                    elif cmd in self.SESSIONLESS_FALLBACK_VERBS:
                        # No session, no lock — run the handler and let it decide
                        # whether it can proceed sessionless (e.g. an anonymous
                        # fetch) or must raise its own precondition error.
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
                    # 0.7.0 exclusive-lease gate — a PURE read with no await
                    # before the early return, placed BEFORE `async with
                    # entry.lock` so a denied caller returns instantly instead
                    # of blocking on the very lock the holder is using (the
                    # whole point of a clean "busy" error).
                    active = entry.lease_active()
                    if active is not None:
                        ok_lease, reason = _lease.check_access(
                            active, _lease.holder_token_from_args(args),
                            session_name)
                        if not ok_lease:
                            log.info("lease-denied session=%s cmd=%s owner=%s",
                                     session_name, cmd, active["owner"])
                            return {"id": req_id, "ok": False, "error": reason}
                    # 0.7.0 self-heal: run the verb under the per-session lock
                    # with transparent Chrome renderer-crash recovery.
                    result = await self._run_session_verb_with_recovery(
                        cmd, args, entry, session_name)
            # 0.7.0 self-heal: a mutating verb that crashed was RECOVERED but
            # deliberately not retried — surface it as a top-level failure (with
            # the recovered flag) so the caller re-issues, rather than ok:true
            # wrapping a nested error. No existing handler returns a top-level
            # `recovered` key, so this shape is unambiguous.
            if (isinstance(result, dict) and result.get("recovered") is True
                    and result.get("ok") is False):
                return {"id": req_id, "ok": False, "recovered": True,
                        "error": result.get("error",
                                            "session recovered after a crash")}
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
            from .handlers import HeadedNoDisplayError, SessionNotStarted
            # These carry a user-facing message that already matches the
            # dispatcher-level format — emit it bare (no `ClassName:` prefix) so
            # the caller sees the actionable text verbatim. SessionNotStarted so
            # wait_*/UNLOCKED_VERBS match click/fill; HeadedNoDisplayError so the
            # "use vb show" guidance isn't buried behind a type name.
            if isinstance(exc, (SessionNotStarted, HeadedNoDisplayError)):
                return {"id": req_id, "ok": False, "error": str(exc)}
            return {"id": req_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            current_session_ctx.reset(tok)

    async def _run_session_verb_with_recovery(self, cmd, args, entry, name):
        """Run a session-scoped verb under ``entry.lock`` with transparent
        Chrome renderer-crash recovery.

        On a crash signature: revive the page (tier-1, context still alive) or
        relaunch the dead context (tier-2), then retry ONCE iff the verb is in
        ``RETRY_SAFE_VERBS``; otherwise recover the session but return a
        structured ``{ok:False, recovered:True}`` re-issue error so a mutating
        verb's side-effect is never double-applied. Recovery runs while holding
        ``entry.lock`` so no interleaving verb can touch a half-recovered
        context; ``registry.relaunch`` deliberately does NOT take the registry
        mutate_lock (disjoint lock orderings → no deadlock).

        Kill-switch: ``VIBATCHIUM_SELF_HEAL=0`` disables recovery entirely and
        re-raises the original crash (today's wedge behavior, for operators who
        want a crash to fail loudly and stop a bot).
        """
        handler = self._handlers[cmd]
        if os.environ.get("VIBATCHIUM_SELF_HEAL", "1").lower() in (
                "0", "false", "no", "off"):
            async with entry.lock:
                await _freeze.lift(entry)
                return await handler(self, args)
        from .browser import is_crash_error, revive_page
        async with entry.lock:
            # 0.16.0: a parked (idle-frozen) session thaws before its verb
            # runs — under the same lock, so the freezer can't re-apply
            # mid-verb.
            await _freeze.lift(entry)
            try:
                return await handler(self, args)
            except Exception as exc:  # noqa: BLE001
                if not is_crash_error(exc):
                    raise
                log.warning("self-heal: crash on %s (session=%s): %s — recovering",
                            cmd, name, type(exc).__name__)
                # Tier 1: context alive → fresh page. Tier 2: context/browser
                # dead → full relaunch.
                try:
                    inflight = getattr(entry.session, "_revive_task", None)
                    if inflight is not None:
                        # The context.on_close reviver is already opening a fresh
                        # page for an actually-CLOSED page — let it finish and
                        # reuse that page instead of racing it to a second one.
                        with contextlib.suppress(Exception):
                            await inflight
                        live = [p for p in entry.session.context.pages
                                if not p.is_closed()]
                        if live:
                            entry.session.page = live[-1]
                            entry.session.frame_ref = None
                        else:
                            await revive_page(entry.session, force_new=True)
                    else:
                        # Renderer crash: the page reports is_closed()==False but
                        # is dead, so a FRESH page is mandatory — reusing it would
                        # retry straight back into the crash.
                        await revive_page(entry.session, force_new=True)
                    entry.recovered += 1
                    entry.last_recovered_at = time.time()
                    entry.snapshot = None
                    entry.prev_snapshot = None
                except Exception:  # noqa: BLE001
                    await self.registry.relaunch(name)  # bumps its own counter
                # Idempotency gate: only re-run side-effect-free verbs.
                if cmd not in self.RETRY_SAFE_VERBS:
                    return {"ok": False, "recovered": True,
                            "error": (f"session {name!r} recovered after a "
                                      f"renderer crash during {cmd!r}; re-issue "
                                      f"the command")}
                try:
                    return await handler(self, args)  # retry ONCE
                except Exception as exc2:  # noqa: BLE001
                    if is_crash_error(exc2):
                        raise RuntimeError(
                            f"session {name!r} crashed again immediately after "
                            f"auto-recovery (relaunch #{entry.recovered}); the "
                            f"page may be crash-looping — inspect or reset with "
                            f"`vb session close {name}` then re-`vb start`"
                        ) from exc2
                    raise

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

    def _acquire_singleton_lock(self) -> bool:
        """0.9.1: take the exclusive daemon lock (held for the process lifetime).
        Returns False if another daemon already holds it. This is the race-free
        replacement for the old connect-probe — two daemons can never both bind
        the socket, so the supersede-and-orphan leak is structurally impossible."""
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
        # Non-inheritable (Python's default, asserted explicitly): the lock fd
        # must NOT leak into a child Chrome via exec, or an orphaned Chrome could
        # keep the flock held after the daemon dies and block the next daemon.
        os.set_inheritable(fd, False)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        with contextlib.suppress(OSError):
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
        return True

    def _release_singleton_lock(self) -> None:
        if self._lock_fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
            self._lock_fd = None
            # DELIBERATELY do NOT unlink LOCK_PATH. flock binds to the inode, not
            # the path — removing the file would let daemon B flock the soon-to-be
            # -unlinked inode while daemon C O_CREATs a NEW inode at the same path
            # and flocks that uncontended, so both "win" the singleton. A
            # persistent empty lockfile (pidfile model) keeps flock single-inode;
            # _acquire_singleton_lock O_CREAT-reuses it and rewrites the pid.

    async def _idle_reaper(self) -> None:
        """0.9.1 (opt-in): self-shutdown after VIBATCHIUM_DAEMON_IDLE_TIMEOUT
        seconds with ZERO sessions/warm. Disabled by default (0/unset) so the
        long-lived bot daemon is never surprise-killed; recommended for dogfood /
        isolated daemons. Gated on registry.is_idle(), so a daemon with any open
        session (incl. attach-mode) is never reaped."""
        try:
            timeout = float(os.environ.get("VIBATCHIUM_DAEMON_IDLE_TIMEOUT", "0") or 0)
        except ValueError:
            timeout = 0.0
        if timeout <= 0:
            return
        poll = min(30.0, max(5.0, timeout / 4))
        while not self._stopping.is_set():
            await asyncio.sleep(poll)
            if not self.registry.is_idle():
                # observed busy → restart the idle clock, so the grace window is
                # measured from when the daemon last had work (covers a long verb
                # AND the warm-pool-drain path that never hits dispatch()).
                self._last_activity = time.monotonic()
                continue
            idle_for = time.monotonic() - self._last_activity
            if idle_for >= timeout:
                log.info("idle reaper: 0 sessions for %.0fs (>= %.0fs) — self-shutdown",
                         idle_for, timeout)
                self._stopping.set()
                return

    async def _idle_freezer(self) -> None:
        """0.16.0 (default-on): lifecycle-freeze sessions that have served no
        verb for VIBATCHIUM_IDLE_FREEZE_AFTER seconds, so a page parked on
        WebGL / CSS-animation / rAF content can't burn cores indefinitely (see
        freeze.py). The next verb on a session thaws it in the dispatcher.
        Like the reaper, this task is kept OUT of the FIRST_COMPLETED wait
        set — a disabled freezer returns immediately and must not end the
        daemon."""
        if not _freeze.freeze_enabled():
            log.info("idle-freeze disabled (VIBATCHIUM_IDLE_FREEZE)")
            return
        after = _freeze.freeze_after()
        poll = min(30.0, max(5.0, after / 4))
        log.info("idle-freeze armed: after=%.0fs poll=%.0fs", after, poll)
        while not self._stopping.is_set():
            await asyncio.sleep(poll)
            for name in self.registry.list_running():
                await self._freeze_if_idle(name, after)

    async def _freeze_if_idle(self, name: str, after: float) -> int:
        """One session's idle-freeze decision, factored out of the poll loop so
        it's unit-testable. Returns renderers newly SIGSTOPped (0 = skipped or
        none found). Never raises."""
        # peek(), NOT get() — get() stamps activity and would reset the idle
        # clock this loop is measuring.
        entry = self.registry.peek(name)
        if entry is None or not _freeze.eligible(entry):
            return 0
        if time.time() - entry.last_used_at < after:
            return 0
        if entry.lock.locked():
            return 0  # locked verb in flight — not idle
        if entry.inflight > 0:
            return 0  # unlocked page-wait in flight (0.18.6) — not idle
        async with entry.lock:
            # Re-check under the lock: a verb may have just finished (fresh
            # activity) or be the reason the lock was held.
            idle_for = time.time() - entry.last_used_at
            if idle_for < after:
                return 0
            if entry.inflight > 0:
                # Raced a page-wait dispatch that bumped inflight after our
                # guard above — leave the renderer running (0.18.6).
                return 0
            try:
                fresh = await _freeze.apply(entry)
            except Exception:  # noqa: BLE001 — never kill the loop
                log.exception("idle-freeze: apply failed session=%s", name)
                return 0
            if fresh:
                log.info("idle-freeze: froze session=%s "
                         "(%d renderer(s), idle %.0fs)", name, fresh, idle_for)
            return fresh

    async def run(self) -> None:
        # 0.9.1: race-free singleton — hold an exclusive flock for life.
        if not self._acquire_singleton_lock():
            print(f"[vibatchium] another daemon already holds {LOCK_PATH} — "
                  f"not starting a second", file=sys.stderr)
            sys.exit(2)
        # We own the lock, but a pre-0.9.1 daemon (no flock) might still be
        # serving the socket — never supersede a LIVE daemon, regardless of its
        # code version. A bounded connect decides; on a TIMEOUT (a live-but-slow
        # daemon under memory pressure — exactly the leak scenario), fall back to
        # the pidfile so we don't orphan it. Only reclaim a genuinely dead socket.
        if SOCK_PATH.exists():
            incumbent_alive = False
            try:
                _, w = await asyncio.wait_for(
                    asyncio.open_unix_connection(str(SOCK_PATH)), timeout=2.0)
                w.close()
                await w.wait_closed()
                incumbent_alive = True
            except TimeoutError:
                incumbent_alive = _pidfile_daemon_alive()
            except (OSError, ConnectionRefusedError):
                incumbent_alive = False
            if incumbent_alive:
                print(f"[vibatchium] daemon already serving at {SOCK_PATH} — "
                      f"not superseding it", file=sys.stderr)
                self._release_singleton_lock()
                sys.exit(2)
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
            # The reaper runs alongside but is NOT itself a completion trigger:
            # when it decides to reap it sets `_stopping`, which `stopper`
            # observes. (A disabled reaper returns immediately — that must NOT
            # end the daemon, so it's kept out of the wait set.)
            reaper = asyncio.create_task(self._idle_reaper())
            # 0.16.0: idle-freeze poll loop — same out-of-wait-set contract
            # as the reaper (returns immediately when disabled).
            freezer = asyncio.create_task(self._idle_freezer())
            try:
                await asyncio.wait(
                    {stopper, serving}, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                for t in (stopper, serving, reaper, freezer):
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
        # 0.9.1: release the flock LAST (after sock/pid are gone) so the next
        # daemon takes over cleanly. The lockfile itself is intentionally never
        # unlinked — flock is inode-bound, so a stable path keeps it the
        # authoritative single-inode gate.
        self._release_singleton_lock()


class _SecureRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that keeps the active log AND every rotated backup at
    0600 — the daemon log can carry site names / URLs (low-but-real
    sensitivity). _open() runs on construction and after each rollover, so every
    file this handler creates is chmod'd; a rotated backup keeps the 0600 it had
    while it was the active baseFilename."""

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def main() -> None:
    # Wave 7.5e: level controlled by VIBATCHIUM_LOG_LEVEL (default INFO).
    # Setting it to DEBUG together with VIBATCHIUM_LOG_VERBS=1 produces a
    # full audit trail of every verb dispatched.
    level_name = os.environ.get("VIBATCHIUM_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    # 0.9.2: persistent + bounded daemon log. LOG_PATH now resolves to a state
    # dir (see paths.py) so the forensic trail survives reboots/daemon bounces;
    # a RotatingFileHandler caps on-disk size. maxBytes=0 disables rotation.
    # The daemon MUST always start, so neither a malformed size/backup env value
    # nor an unwritable log path may crash it: bad numbers fall back to the
    # default, and an un-openable path falls back to the runtime CACHE_DIR
    # (guaranteed-writable, volatile — the pre-0.9.2 location).
    try:
        max_bytes = int(os.environ.get("VIBATCHIUM_LOG_MAX_BYTES") or 10 * 1024 * 1024)
    except ValueError:
        max_bytes = 10 * 1024 * 1024
    try:
        backups = max(0, int(os.environ.get("VIBATCHIUM_LOG_BACKUPS") or 5))
    except ValueError:
        backups = 5
    try:
        handler = _SecureRotatingFileHandler(
            str(LOG_PATH), maxBytes=max_bytes, backupCount=backups)
    except OSError:
        handler = _SecureRotatingFileHandler(
            str(CACHE_DIR / "daemon.log"), maxBytes=max_bytes, backupCount=backups)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.basicConfig(level=level, handlers=[handler])
    asyncio.run(Daemon().run())


if __name__ == "__main__":
    main()
