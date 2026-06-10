"""Verb handlers. Each takes (daemon, args:dict) and returns a JSON-serializable value.

Handlers are registered via Daemon.handler(name). The daemon must have an active
BrowserSession for most verbs; lifecycle verbs (start/attach/stop/status) are exempt.
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from . import elements
from .paths import (
    DEFAULT_SESSION_NAME, PROFILES_DIR, get_active_session_name, list_session_names,
    secure_mkdir, secure_write, session_dir, set_active_session_name,
    validate_name,
)
from .registry import current_session_ctx

log = logging.getLogger("vibatchium.handlers")


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def resolve_headless(args: dict) -> bool:
    """Decide headless vs headed for a `start`/prewarm, daemon-side.

    Precedence (0.6.4): an explicit ``headless`` arg wins; then
    ``VIBATCHIUM_DEFAULT_HEADLESS`` forces headless; then
    ``VIBATCHIUM_DEFAULT_HEADED`` forces headed; otherwise **headless** — a
    background daemon has no display to own, so popping visible windows for
    programmatic callers (plugins, research fan-out, the xscraper reader) is the
    wrong default. Interactive `vb start` from a human TTY asks for headed
    explicitly (see cli.py), so humans still get a window.
    """
    if "headless" in args:
        return bool(args["headless"])
    if _env_truthy("VIBATCHIUM_DEFAULT_HEADLESS"):
        return True
    if _env_truthy("VIBATCHIUM_DEFAULT_HEADED"):
        return False
    return True


import re as _re

_REF_TARGET_RE = _re.compile(r"^@?(e\d+)$|^\[ref=e\d+\]$")


def _mask_url(url: str) -> str:
    """Return a credential-redacted version of a proxy URL safe for logging /
    returning to callers. `http://user:pass@host:port` → `http://***@host:port`."""
    if not url:
        return ""
    from urllib.parse import urlparse, urlunparse
    p = urlparse(url)
    if p.password or p.username:
        netloc = "***@" + (p.hostname or "")
        if p.port:
            netloc += f":{p.port}"
        return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))
    return url


class SessionNotStarted(Exception):
    """Sentinel raised by _need_session when no browser session exists.

    Server formats this without the `RuntimeError:` type prefix so the
    message matches the dispatcher-level "no session" rejection verbatim.
    """


def _need_session(daemon):
    if daemon.session is None:
        # Mirror the dispatcher's no-session message verbatim so the error
        # string is the same whether the verb is gated at dispatch time or
        # hits the handler-level check (UNLOCKED_VERBS path).
        from .paths import DEFAULT_SESSION_NAME
        from .registry import current_session_ctx
        try:
            name = current_session_ctx.get()
        except LookupError:
            name = DEFAULT_SESSION_NAME
        suffix = (f" --session {name}" if name != DEFAULT_SESSION_NAME else "")
        raise SessionNotStarted(f"no session {name!r} — run `vb start{suffix}` first")
    # Repair a stale session.page if the previously-active page has detached
    # (popup-then-close, navigation-cancelled, target-crashed). Pick the
    # newest live page from the context as the fallback.
    s = daemon.session
    try:
        if s.page.is_closed():
            live = [p for p in s.context.pages if not p.is_closed()]
            if live:
                s.page = live[-1]
    except Exception:  # noqa: BLE001
        # is_closed() not always available depending on attach mode; ignore
        pass
    return s


def _invalidate_snapshot(daemon) -> None:
    """Clear the cached AX snapshot AND any held JS handles — refs and handles
    from before navigation no longer apply."""
    daemon._prev_snapshot = getattr(daemon, "_snapshot", None)
    daemon._snapshot = None
    # Best-effort dispose of held handles
    handles = getattr(daemon, "_handles", {})
    for h in list(handles.values()):
        try:
            # JSHandle.dispose() returns a coroutine but we can't await here
            # — schedule a fire-and-forget. The handle becomes invalid anyway
            # since navigation tears down the execution context.
            import asyncio as _aio
            coro = h.dispose()
            if _aio.iscoroutine(coro):
                _aio.create_task(coro)
        except Exception:  # noqa: BLE001
            pass
    handles.clear()


def _is_ref_target(target: str) -> bool:
    """True iff target looks like a structured @eN ref, not a CSS/XPath selector.

    Strict: matches `@e3`, `e3`, `[ref=e3]` — refuses things like `[data-test="@e1"]`
    that a loose `startswith("@e")` check would falsely match.
    """
    return bool(_REF_TARGET_RE.match(target.strip()))


def _resolve_target(daemon, target: str):
    """Resolve `target` to a Locator.

    Recognized forms (full list in elements.resolve_target):
    - `@eN` / `eN` / `[ref=eN]`           — last-map aria-ref
    - `@text:Foo` / `@label:Email` etc.   — Playwright getBy* shortcuts
    - `"Multi word text"`                  — auto-routes to get_by_text
    - anything else                        — raw Playwright selector

    Refs need the cached snapshot; other forms resolve directly against
    the live AX tree.
    """
    s = _need_session(daemon)
    if _is_ref_target(target):
        if daemon._snapshot is None:
            raise RuntimeError(
                f"ref {target!r} cannot be resolved — last `map` was invalidated by "
                f"a navigation. Run `vb map` to refresh the snapshot first."
            )
        return elements.resolve(s.page, daemon._snapshot, target)
    return elements.resolve_target(s.page, daemon._snapshot, target)


# ─── Wave 7.8: housekeeping helpers (used by the `clean` handler) ─────────

def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _dir_size(p: Path) -> int:
    """Total size of a directory tree (follows no symlinks). 0 if missing."""
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


def _truncate_log_tail(path: Path, keep_bytes: int, *, apply: bool = True) -> int:
    """Compute (and, when apply=True, perform) keeping only the last
    `keep_bytes` of the daemon log. Returns the resulting size.

    With apply=False this is a pure dry-run: it computes the exact post-
    truncation size (including the line-alignment drop) WITHOUT touching the
    file, so the clean report's reclaimed bytes match what apply actually frees.

    The daemon holds this file open via a logging FileHandler, so a plain
    truncate would leave the handler appending past a sparse hole. We locate
    that handler, swap in a freshly-opened stream over the truncated file, and
    keep the log line-aligned by dropping any partial leading line. Best-effort:
    returns the resulting size (or the original size if anything goes wrong).
    """
    try:
        data = path.read_bytes()
    except OSError:
        return _file_size(path)
    if len(data) <= keep_bytes:
        return len(data)
    tail = data[-keep_bytes:]
    nl = tail.find(b"\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1:]
    if not apply:
        return len(tail)
    fh = None
    for h in list(logging.getLogger().handlers):
        if (isinstance(h, logging.FileHandler)
                and getattr(h, "baseFilename", None) == str(path)):
            fh = h
            break
    if fh is not None:
        fh.acquire()
    try:
        if fh is not None:
            try:
                fh.flush()
                if fh.stream:
                    fh.stream.close()
            except Exception:  # noqa: BLE001
                pass
            fh.stream = None
        with open(path, "wb") as f:
            f.write(tail)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if fh is not None:
            fh.stream = fh._open()
    except Exception as exc:  # noqa: BLE001
        log.warning("log truncation failed: %s", exc)
        return _file_size(path)
    finally:
        if fh is not None:
            fh.release()
    return len(tail)


def register_all(daemon) -> None:
    @daemon.handler("ping")
    async def _ping(d, args):
        from .. import __version__
        return {"pong": True, "session": d.session is not None,
                "version": __version__}

    # ─── Wave 7.6: utilities (no session required) ───────────────────────

    @daemon.handler("verify_url")
    async def _verify_url(d, args):
        """Fast pre-check that a URL is reachable, before `go` commits to a
        30s navigation timeout. Resolves DNS first (most common failure mode
        in agent dogfood: bad URL guesses like `docs.antigravity.google/`);
        optionally does an HTTP HEAD if `check_http=true`.

        Args:
          url            (str)  full URL to check
          check_http     (bool) also do HTTP HEAD (default false — DNS only)
          timeout_ms     (int)  per-stage timeout, default 3000

        Returns:
          {ok, url, host, dns_resolved, status, latency_ms, error}
        """
        import asyncio as _asyncio
        import socket as _socket
        import time as _time
        from urllib.parse import urlparse as _urlparse

        url = args.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("verify_url requires `url`")
        check_http = bool(args.get("check_http", False))
        timeout_ms = int(args.get("timeout_ms", 3000))
        timeout_s = timeout_ms / 1000.0
        parsed = _urlparse(url)
        if not parsed.hostname:
            return {"ok": False, "url": url, "host": None,
                    "dns_resolved": False, "status": None,
                    "latency_ms": 0, "error": "no hostname in URL"}
        t0 = _time.time()
        # DNS — getaddrinfo is sync; run in thread w/ timeout
        try:
            await _asyncio.wait_for(
                _asyncio.to_thread(_socket.getaddrinfo, parsed.hostname, None),
                timeout=timeout_s,
            )
            dns_ok = True
        except (TimeoutError, _socket.gaierror, OSError) as exc:
            return {
                "ok": False, "url": url, "host": parsed.hostname,
                "dns_resolved": False, "status": None,
                "latency_ms": int((_time.time() - t0) * 1000),
                "error": f"DNS: {type(exc).__name__}: {exc}",
            }
        # Optional HTTP HEAD
        status = None
        if check_http:
            import urllib.request as _ureq
            def _head():
                req = _ureq.Request(url, method="HEAD")
                req.add_header("User-Agent",
                               "Mozilla/5.0 (compatible; vibatchium-verify/1.0)")
                with _ureq.urlopen(req, timeout=timeout_s) as r:
                    return r.status
            try:
                status = await _asyncio.wait_for(
                    _asyncio.to_thread(_head), timeout=timeout_s,
                )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False, "url": url, "host": parsed.hostname,
                    "dns_resolved": dns_ok, "status": None,
                    "latency_ms": int((_time.time() - t0) * 1000),
                    "error": f"HTTP: {type(exc).__name__}: {exc}",
                }
        return {
            "ok": True, "url": url, "host": parsed.hostname,
            "dns_resolved": dns_ok, "status": status,
            "latency_ms": int((_time.time() - t0) * 1000), "error": None,
        }

    @daemon.handler("set_log_verbs")
    async def _set_log_verbs(d, args):
        """Toggle per-verb DEBUG audit logging at runtime. No daemon restart
        needed — for any non-trivial run where you want a full call trail.

        Args:
          on    (bool|str)  truthy enables, falsy disables. Accepts "on"/"off".

        Returns the new state.
        """
        on = args.get("on")
        if isinstance(on, str):
            on = on.strip().lower() in ("on", "1", "true", "yes")
        else:
            on = bool(on)
        d.flags["log_verbs"] = on
        log.info("log_verbs toggled to %s", on)
        return {"log_verbs": on,
                "note": ("per-verb DEBUG log enabled — set "
                         "VIBATCHIUM_LOG_LEVEL=DEBUG and tail "
                         "$XDG_RUNTIME_DIR/vibatchium/daemon.log "
                         "to see verb traffic") if on else "off"}

    # ─── lifecycle ────────────────────────────────────────────────────────

    @daemon.handler("start")
    async def _start(d, args):
        """Launch Chrome for a session.

        Session resolution: dispatcher already set `current_session_ctx` from
        the request's `_session` field (or active-session file → 'default').

        Profile resolution:
          - `profile=<abs-path>`  → use that dir as user-data-dir (test/ephemeral)
          - `profile=<bare-name>` → PROFILES_DIR/<name>; also adopts that as session name
                                    if the caller didn't pass `_session` explicitly
          - neither               → session_dir(<session_name>)
        """
        name = current_session_ctx.get()
        raw = args.get("profile")
        if raw:
            p = Path(raw)
            if p.is_absolute():
                profile_dir = p
            else:
                # bare name → also makes that the session name (so the user can do
                # `vb start --profile work` and address it later as `--session work`)
                if name == DEFAULT_SESSION_NAME:
                    name = raw
                profile_dir = PROFILES_DIR / raw
        else:
            profile_dir = session_dir(name)

        # 'default' is never deletable, so never mark/report it ephemeral
        # (close() guards it anyway — this keeps the response honest).
        want_ephemeral = bool(args.get("ephemeral")) and name != DEFAULT_SESSION_NAME

        if d.registry.has(name):
            entry = d.registry.get(name)
            # Allow `start --ephemeral` to mark an already-running session for
            # cleanup-on-close.
            if want_ephemeral:
                entry.ephemeral = True
            return {"already_started": True, "mode": entry.session.mode,
                    "session": name, "profile": str(entry.profile_dir),
                    "ephemeral": entry.ephemeral}

        # 0.6.4: headless by default (a background daemon owns no display).
        # Explicit headless=true|false wins; VIBATCHIUM_DEFAULT_HEADED=1 opts a
        # whole daemon back into headed. Interactive `vb start` sends headed
        # explicitly so humans at a terminal still get a window.
        headless = resolve_headless(args)
        backend = args.get("backend") or "patchright"
        ephemeral = want_ephemeral
        entry = await d.registry.create(
            name, profile_dir=profile_dir, headless=headless,
            backend=backend, ephemeral=ephemeral,
        )

        out = {"started": True, "mode": "launch",
               "session": name, "profile": str(entry.profile_dir),
               "profile_name": entry.profile_dir.name,
               "backend": backend, "ephemeral": ephemeral}
        return out

    # ─── session management ────────────────────────────────────────────

    @daemon.handler("session_new")
    async def _session_new(d, args):
        """Create a new on-disk session/profile dir without launching Chrome.

        Use `start --session NAME` (or `session_start`) to actually launch.
        Idempotent: re-creating an existing session is a no-op that reports
        `created=false, exists=true`.

        Wave 6.1b: if VIBATCHIUM_WARM ∈ {opportunistic, both} (default both),
        also schedules a background Chrome pre-spawn at this profile dir so
        a subsequent `start` call finds it warm. Pass `prewarm=false` to opt
        out per-call.
        """
        name = validate_name(args.get("name"), kind="session name")
        p = PROFILES_DIR / name
        existed = p.exists()
        p.mkdir(parents=True, exist_ok=True)
        prewarm_requested = args.get("prewarm", True)
        if prewarm_requested and not d.registry.has(name):
            # Prewarm matches what `start` will resolve to (0.6.4: headless
            # default), so the pre-spawned Chrome claims cleanly.
            d.registry.schedule_prewarm(name, p, headless=resolve_headless(args))
        return {
            "created": not existed, "exists": existed, "name": name,
            "path": str(p), "profile_dir": str(p),
            "running": d.registry.has(name),
            "prewarm_scheduled": prewarm_requested,
        }

    @daemon.handler("session_list")
    async def _session_list(d, args):
        """List every on-disk session + which are currently running."""
        return {
            "active": get_active_session_name(),
            "sessions": d.registry.list_all(),
        }

    @daemon.handler("session_use")
    async def _session_use(d, args):
        name = validate_name(args.get("name"), kind="session name")
        if name not in list_session_names():
            raise ValueError(
                f"unknown session: {name!r} — create with `vb session new {name}`"
            )
        set_active_session_name(name)
        return {"active": name}

    @daemon.handler("session_switch")
    async def _session_switch(d, args):
        """Alias for session_use (familiar to users of other automation tools)."""
        return await d._handlers["session_use"](d, args)

    @daemon.handler("session_close")
    async def _session_close(d, args):
        """Stop Chrome for one session; profile dir is preserved on disk."""
        name = args.get("name") or current_session_ctx.get()
        name = validate_name(name, kind="session name")
        closed = await d.registry.close(name)
        return {"closed": closed, "name": name}

    @daemon.handler("session_close_all")
    async def _session_close_all(d, args):
        n = await d.registry.close_all()
        return {"closed": n}

    # ─── Wave 6.3a: credential vault + TOTP ────────────────────────────

    @daemon.handler("secret_init")
    async def _secret_init(d, args):
        """Provision the vault key in the OS keyring (or print to stdout for
        env-var setups). Returns the base64 key only if `print_key=true` is
        passed — by default just confirms storage.

        Refuses to run against an existing vault unless `force=true` — a
        fresh key would orphan the existing ciphertext silently.
        """
        from .. import secrets as _secrets
        info = _secrets.init_vault_key(
            prefer=args.get("prefer", "keyring"),
            force=bool(args.get("force", False)),
        )
        if not args.get("print_key"):
            info = {"stored_in": info["stored_in"]}
        else:
            info["warning"] = "key in response — store securely + redact from logs"
        return info

    @daemon.handler("secret_set")
    async def _secret_set(d, args):
        """Store a secret. Logs site+key but never the value."""
        from .. import secrets as _secrets
        site = args["site"]
        key = args["key"]
        value = args["value"]
        if not (site and key and value):
            raise ValueError("secret_set requires site, key, value")
        _secrets.set_secret(site, key, value)
        # NEVER include value in response
        return {"set": True, "site": site, "key": key}

    @daemon.handler("secret_list")
    async def _secret_list(d, args):
        """List secrets (MASKED). Returns {site: {key: '<set>'}}."""
        from .. import secrets as _secrets
        return {"sites": _secrets.list_secrets(args.get("site"))}

    @daemon.handler("secret_delete")
    async def _secret_delete(d, args):
        from .. import secrets as _secrets
        site = args["site"]
        key = args.get("key")
        deleted = _secrets.delete_secret(site, key)
        return {"deleted": deleted, "site": site, "key": key}

    @daemon.handler("secret_totp")
    async def _secret_totp(d, args):
        """Compute the current TOTP for a site's stored totp-seed.

        Used internally by `fill --use-secret site:totp`. Also exposed so
        callers can verify a TOTP setup without filling a form.
        """
        from .. import secrets as _secrets
        site = args["site"]
        seed = _secrets.get_secret(site, "totp-seed")
        if not seed:
            raise KeyError(f"no totp-seed set for site {site!r}")
        return {"site": site, "code": _secrets.totp(seed)}

    @daemon.handler("wait_email_code")
    async def _wait_email_code(d, args):
        """Poll the IMAP mailbox configured in site's `email-poll` secret
        and return the matched code.

        `timeout`: total seconds to wait (default 60).
        `max_age`: skip messages older than this (default 300s).
        `mark_read`: consume the message after extracting the code.
        """
        from .. import secrets as _secrets
        site = args["site"]
        url = _secrets.get_secret(site, "email-poll")
        if not url:
            raise KeyError(f"no email-poll configured for site {site!r}")
        cfg = _secrets.parse_email_poll_url(url)
        # Run the blocking IMAP poller in a worker thread so we don't block
        # the daemon event loop.
        import asyncio as _aio
        code = await _aio.to_thread(
            _secrets.wait_for_email_code, cfg,
            timeout=int(args.get("timeout", 60)),
            max_age_s=int(args.get("max_age", 300)),
            mark_read=bool(args.get("mark_read", False)),
        )
        if code is None:
            raise TimeoutError(f"no matching email for {site!r} within timeout")
        return {"site": site, "code": code}

    # ─── Wave 6.2a: per-session proxy ──────────────────────────────────

    @daemon.handler("proxy_set")
    async def _proxy_set(d, args):
        """Persist a proxy URL for the current session. Takes effect on next start.

        URL forms:
          http://user:pass@host:port
          socks5://user:pass@host:port
          brightdata://customer-id:password@zone-name?country=us&session-id=X
          iproyal://user:pass@geo.iproyal.com:12321?country=us&sticky=7d
          decodo://user:pass@gate.decodo.com:7000?country=us
        """
        from ..proxy import save_session_proxy, parse as _parse, load_proxy_file
        from .registry import current_session_ctx as _ctx
        url = args.get("url")
        path = args.get("path")
        if path:
            url = load_proxy_file(path)
        if not url:
            raise ValueError("proxy_set requires url= or path=")
        # Validate before persisting (raises ProxyParseError on bad URL)
        _parse(url)
        sname = _ctx.get()
        from .paths import session_dir as _sd
        # Use the in-memory profile_dir if session is running, else resolve from disk
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        save_session_proxy(pdir, url)
        return {"set": True, "session": sname, "url_preview": _mask_url(url),
                "note": "takes effect on next `start` (close session first if running)"}

    @daemon.handler("proxy_clear")
    async def _proxy_clear(d, args):
        from ..proxy import save_session_proxy
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        save_session_proxy(pdir, None)
        return {"cleared": True, "session": sname}

    @daemon.handler("proxy_info")
    async def _proxy_info(d, args):
        """Report what proxy is configured + (if session running) the exit IP."""
        from ..proxy import load_session_proxy, parse as _parse
        from .registry import current_session_ctx as _ctx
        from .paths import session_dir as _sd
        sname = _ctx.get()
        entry = d.registry.get(sname)
        pdir = entry.profile_dir if entry else _sd(sname)
        url = load_session_proxy(pdir)
        out = {"session": sname, "configured": bool(url),
               "url_preview": _mask_url(url) if url else None}
        if url:
            try:
                cfg = _parse(url)
                out["server"] = cfg.get("server")
                out["has_auth"] = bool(cfg.get("username"))
            except Exception as exc:  # noqa: BLE001
                out["parse_error"] = str(exc)
        # If session is running, probe ipify.org through the browser
        if entry is not None:
            try:
                import time as _t
                t0 = _t.time()
                # Use a fresh page to avoid polluting the user's active page
                page = await entry.session.context.new_page()
                try:
                    await page.goto("https://api.ipify.org?format=json",
                                     timeout=10_000, wait_until="domcontentloaded")
                    body = await page.evaluate("() => document.body.innerText")
                    import json as _json
                    parsed = _json.loads(body)
                    out["exit_ip"] = parsed.get("ip")
                    out["latency_ms"] = int((_t.time() - t0) * 1000)
                finally:
                    await page.close()
            except Exception as exc:  # noqa: BLE001
                out["exit_ip_error"] = str(exc)
        return out

    # ─── Wave 6.1c: session checkpoint / restore ───────────────────────

    @daemon.handler("checkpoint_save")
    async def _checkpoint_save(d, args):
        """Snapshot the current session: tabs (url + scroll_y + title) +
        storage_state (cookies + LS + SS) + viewport. Writes to
        <profile_dir>/checkpoints/<name>.json.

        For a session that's currently logged in, a checkpoint captures
        everything needed to recreate the same logged-in state later, even
        in a different session.
        """
        import json as _json
        import time as _time
        from .registry import current_session_ctx as _ctx
        name = validate_name(args.get("name") or "default", kind="checkpoint name")
        sname = _ctx.get()
        entry = d.registry.get(sname)
        if entry is None:
            raise RuntimeError(f"no running session {sname!r}")
        s = entry.session
        # Capture tabs
        tabs = []
        for p in s.context.pages:
            if p.is_closed():
                continue
            try:
                title = await p.title()
            except Exception:  # noqa: BLE001
                title = ""
            try:
                scroll_y = await p.evaluate("() => window.scrollY")
            except Exception:  # noqa: BLE001
                scroll_y = 0
            tabs.append({"url": p.url, "title": title, "scroll_y": scroll_y})
        # Capture storage state (cookies + per-origin LS/SS)
        storage_state = await s.context.storage_state()
        # Viewport
        viewport = s.page.viewport_size or {}
        doc = {
            "version": 1,
            "ts": _time.time(),
            "session_source": sname,
            "viewport": viewport,
            "tabs": tabs,
            "storage_state": storage_state,
        }
        cp_dir = secure_mkdir(entry.profile_dir / "checkpoints")
        path = cp_dir / f"{name}.json"
        secure_write(path, _json.dumps(doc, indent=2))
        return {"saved": True, "name": name, "path": str(path),
                "tabs": len(tabs), "cookies": len(storage_state.get("cookies", [])),
                "bytes": path.stat().st_size}

    @daemon.handler("checkpoint_load")
    async def _checkpoint_load(d, args):
        """Restore a checkpoint into the current session.

        Apply storage_state, re-open tabs, restore viewport. The checkpoint's
        source session is recorded so cross-session loads can be detected
        (e.g. loading a 'work' checkpoint into 'work-2' for a clone).

        Cross-tab restoration strategy:
          - Close all currently-open tabs except the first
          - Navigate the first to the saved tab[0].url
          - For tabs[1:], open new pages and navigate them
          - Apply scroll position last (per tab)
        """
        import json as _json
        from .registry import current_session_ctx as _ctx
        name = validate_name(args.get("name") or "default", kind="checkpoint name")
        from_session = args.get("from_session")  # optional: load from another session's profile
        if from_session is not None:
            from_session = validate_name(from_session, kind="from_session")

        sname = _ctx.get()
        entry = d.registry.get(sname)
        if entry is None:
            raise RuntimeError(f"no running session {sname!r}")
        s = entry.session

        # Resolve checkpoint path: by default look in current session's profile;
        # `from_session` reads from a different session's profile dir.
        if from_session:
            src_entry = d.registry.get(from_session)
            if src_entry is None:
                from .paths import PROFILES_DIR
                src_dir = PROFILES_DIR / from_session
            else:
                src_dir = src_entry.profile_dir
        else:
            src_dir = entry.profile_dir
        cp_path = src_dir / "checkpoints" / f"{name}.json"
        if not cp_path.exists():
            raise FileNotFoundError(f"no checkpoint {name!r} at {cp_path}")
        doc = _json.loads(cp_path.read_text())

        # 1. Apply storage_state via the existing restore handler logic.
        await d._handlers["storage_restore"](d, {"state": doc.get("storage_state", {})})

        # 2. Tabs
        tabs = doc.get("tabs", [])
        if tabs:
            # Close all tabs except the first one
            for p in list(s.context.pages)[1:]:
                try:
                    await p.close()
                except Exception:  # noqa: BLE001
                    pass
            # Navigate the first tab
            first = s.context.pages[0] if s.context.pages else await s.context.new_page()
            s.page = first
            try:
                await first.goto(tabs[0]["url"], wait_until="domcontentloaded",
                                  timeout=30_000)
                if tabs[0].get("scroll_y"):
                    await first.evaluate(f"() => window.scrollTo(0, {int(tabs[0]['scroll_y'])})")
            except Exception:  # noqa: BLE001
                pass
            # Open additional tabs
            for tab in tabs[1:]:
                np = await s.context.new_page()
                try:
                    await np.goto(tab["url"], wait_until="domcontentloaded",
                                   timeout=30_000)
                    if tab.get("scroll_y"):
                        await np.evaluate(f"() => window.scrollTo(0, {int(tab['scroll_y'])})")
                except Exception:  # noqa: BLE001
                    pass

        # 3. Viewport
        vp = doc.get("viewport") or {}
        if vp.get("width") and vp.get("height"):
            try:
                await s.page.set_viewport_size({"width": int(vp["width"]),
                                                  "height": int(vp["height"])})
            except Exception:  # noqa: BLE001
                pass

        _invalidate_snapshot(d)
        return {
            "loaded": True, "name": name,
            "tabs_restored": len(tabs),
            "from_session": doc.get("session_source", from_session or sname),
            "to_session": sname,
        }

    @daemon.handler("checkpoint_list")
    async def _checkpoint_list(d, args):
        """List checkpoints for the current (or named) session."""
        import json as _json
        from_session = args.get("from_session")
        if from_session is not None:
            from_session = validate_name(from_session, kind="from_session")
        from .registry import current_session_ctx as _ctx
        sname = from_session or _ctx.get()
        if from_session:
            from .paths import PROFILES_DIR
            cp_dir = PROFILES_DIR / from_session / "checkpoints"
        else:
            entry = d.registry.get(sname)
            if entry is None:
                from .paths import PROFILES_DIR
                cp_dir = PROFILES_DIR / sname / "checkpoints"
            else:
                cp_dir = entry.profile_dir / "checkpoints"
        if not cp_dir.exists():
            return {"session": sname, "checkpoints": []}
        out = []
        for f in sorted(cp_dir.glob("*.json")):
            try:
                doc = _json.loads(f.read_text())
                out.append({
                    "name": f.stem, "ts": doc.get("ts"),
                    "tabs": len(doc.get("tabs", [])),
                    "cookies": len(doc.get("storage_state", {}).get("cookies", [])),
                    "bytes": f.stat().st_size,
                })
            except Exception:  # noqa: BLE001
                pass
        return {"session": sname, "checkpoints": out}

    @daemon.handler("checkpoint_delete")
    async def _checkpoint_delete(d, args):
        name = validate_name(args.get("name"), kind="checkpoint name")
        from .registry import current_session_ctx as _ctx
        sname = _ctx.get()
        entry = d.registry.get(sname)
        if entry is None:
            from .paths import PROFILES_DIR
            cp = PROFILES_DIR / sname / "checkpoints" / f"{name}.json"
        else:
            cp = entry.profile_dir / "checkpoints" / f"{name}.json"
        if not cp.exists():
            return {"deleted": False, "name": name}
        cp.unlink()
        return {"deleted": True, "name": name}

    @daemon.handler("session_delete")
    async def _session_delete(d, args):
        """Delete a profile dir on disk. Refuses if the session is running,
        active, or is the special 'default'."""
        name = validate_name(args.get("name"), kind="session name")
        if name == get_active_session_name():
            raise ValueError(f"session {name!r} is active — switch first")
        deleted = d.registry.delete_profile_dir(name)
        return {"deleted": deleted, "name": name}

    # ─── profile management (legacy aliases for session_* ─ 1:1 model) ──

    @daemon.handler("profile_list")
    async def _profile_list(d, args):
        return {
            "active": get_active_session_name(),
            "profiles": list_session_names(),
        }

    @daemon.handler("profile_new")
    async def _profile_new(d, args):
        return await d._handlers["session_new"](d, args)

    @daemon.handler("profile_use")
    async def _profile_use(d, args):
        res = await d._handlers["session_use"](d, args)
        return {**res, "note": "takes effect on next `start`"}

    @daemon.handler("profile_delete")
    async def _profile_delete(d, args):
        name = args.get("name")
        if name == get_active_session_name():
            raise ValueError(f"profile {name!r} is active — switch first")
        return await d._handlers["session_delete"](d, args)

    @daemon.handler("clean")
    async def _clean(d, args):
        """One-shot housekeeping — reclaim disk from four sources:

          profiles : stale per-run profile dirs (idle ≥ older_than seconds)
          locks    : leftover Chrome SingletonLock/Socket/Cookie files in
                     non-running profiles (these cause 'profile already in
                     use' launch failures)
          cache    : regenerable caches (vision-cache, observe-cache,
                     screenshots/, explores/) — NOT the vision-spend ledger
          logs     : truncate daemon.log to its last log_keep_bytes

        Dry-run by default — pass apply=true to actually delete. Never touches
        the 'default' profile, the active session, or anything running in this
        daemon. HAR/network captures go to user-chosen paths, so they're out of
        scope. Returns a per-category {count, bytes} report.
        """
        import shutil as _shutil
        import time as _time
        from .paths import CACHE_DIR, LOG_PATH

        apply = bool(args.get("apply"))
        ot = args.get("older_than")
        older_than = float(ot) if ot is not None else 14 * 86400.0
        keep = set(args.get("keep") or [])
        log_keep_bytes = int(args.get("log_keep_bytes", 256 * 1024))
        now = _time.time()
        active = get_active_session_name()
        # "in use" = running sessions PLUS pre-warming Chromes (not in the
        # registry's _entries but holding a live lock on their profile dir).
        # Both must be off-limits to lock removal AND profile pruning.
        in_use = set(d.registry.list_running()) | d.registry.warming_names()
        protected = {DEFAULT_SESSION_NAME, active, *keep}
        cats: dict = {}

        # ── stale profile dirs (by idle age) ──
        if args.get("profiles", True):
            names, total = [], 0
            for row in d.registry.list_all():
                name = row["name"]
                if name in protected or name in in_use or row.get("running"):
                    continue
                la = row.get("last_active")
                if la is None or (now - la) < older_than:
                    continue
                total += _dir_size(PROFILES_DIR / name)
                names.append(name)
                if apply:
                    try:
                        d.registry.delete_profile_dir(name)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("clean: profile %s: %s", name, exc)
            cats["profiles"] = {"count": len(names), "bytes": total,
                                "names": sorted(names)}

        # ── stale Chrome lock files (non-running profiles only) ──
        if args.get("locks", True):
            lock_names = ("SingletonLock", "SingletonSocket", "SingletonCookie")
            count, total = 0, 0
            for name in list_session_names():
                if name in in_use:
                    continue  # a live/warming profile legitimately holds its lock
                pdir = PROFILES_DIR / name
                for lk in lock_names:
                    f = pdir / lk
                    try:
                        if not (f.is_symlink() or f.exists()):
                            continue
                        try:
                            total += f.lstat().st_size
                        except OSError:
                            pass
                        count += 1
                        if apply:
                            f.unlink(missing_ok=True)
                    except OSError:
                        continue
            cats["locks"] = {"count": count, "bytes": total}

        # ── regenerable caches (never the vision-spend ledger) ──
        if args.get("cache", True):
            targets = [CACHE_DIR / "vision-cache.json",
                       CACHE_DIR / "observe-cache.json",
                       CACHE_DIR / "explores",
                       CACHE_DIR / "screenshots"]
            items, total = [], 0
            for t in targets:
                if not t.exists():
                    continue
                total += _dir_size(t) if t.is_dir() else _file_size(t)
                items.append(t.name)
                if apply:
                    try:
                        if t.is_dir():
                            _shutil.rmtree(t)
                        else:
                            t.unlink(missing_ok=True)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("clean: cache %s: %s", t, exc)
            cats["cache"] = {"count": len(items), "bytes": total, "items": items}

        # ── daemon log ──
        if args.get("logs", True):
            before = _file_size(LOG_PATH)
            # apply=False computes the exact post-truncation size without
            # writing, so the dry-run report matches what --apply reclaims.
            after = _truncate_log_tail(LOG_PATH, log_keep_bytes, apply=apply)
            cats["logs"] = {"bytes_before": before, "bytes_after": after,
                            "reclaimed": max(0, before - after)}

        total_bytes = sum(cats.get(k, {}).get("bytes", 0)
                          for k in ("profiles", "locks", "cache"))
        total_bytes += cats.get("logs", {}).get("reclaimed", 0)
        return {"dry_run": not apply, "categories": cats,
                "total_bytes": total_bytes}

    @daemon.handler("attach")
    async def _attach(d, args):
        """Attach to an existing Chrome via CDP and register it as a session."""
        name = current_session_ctx.get()
        if d.registry.has(name):
            raise RuntimeError(f"session {name!r} already active — stop first")
        cdp_url = args.get("cdp_url") or "http://localhost:9222"
        await d.registry.attach(name, cdp_url)
        return {"attached": True, "mode": "attach", "cdp_url": cdp_url, "session": name}

    @daemon.handler("stop")
    async def _stop(d, args):
        """Stop Chrome for the current session. Daemon stays up.

        Use `session_close NAME` to stop a non-active session, or
        `session_close_all` to stop everything.
        """
        name = current_session_ctx.get()
        closed = await d.registry.close(name)
        if not closed:
            return {"already_stopped": True, "session": name}
        return {"stopped": True, "session": name}

    @daemon.handler("status")
    async def _status(d, args):
        """Report on the active session + the registry's running set."""
        from .. import __version__
        name = current_session_ctx.get()
        entry = d.registry.get(name)
        return {
            "running": entry is not None,
            "session": name,
            "mode": entry.session.mode if entry else None,
            "pid": os.getpid(),
            "version": __version__,
            "running_sessions": d.registry.list_running(),
        }

    # ─── Wave 7.7.5: high-level "just works" verbs ───────────────────────

    @daemon.handler("explore")
    async def _explore(d, args):
        """One-call "go look at this URL and tell me what's there."

        Does: verify_url (DNS pre-check) → auto-start session if needed
        → go → extract text + screenshot → optionally close session.
        Closes the most common "I just want to look at a page" workflow
        into a single MCP call instead of 4-6.

        Args:
          url           (str)  target URL — required
          intent        (str)  optional natural-language description of
                               what to look for (not used in v0, future
                               hook for `act` integration)
          keep_open     (bool) leave session open for follow-up calls
                               (default: false — auto-close after extract)
          screenshot    (bool) include a base64 PNG of the landing page
                               (default: true)
          full_page     (bool) full-page screenshot vs viewport
                               (default: true)
          skip_verify   (bool) skip the verify_url DNS pre-check
                               (default: false; turn on for trusted URLs)

        Returns:
          {url, title, status, text, walled?, screenshot_b64?, session,
           closed (bool), elapsed_ms}
        """
        import time as _time
        t0 = _time.time()
        url = args.get("url")
        if not isinstance(url, str) or not url:
            raise ValueError("explore requires `url`")
        keep_open = bool(args.get("keep_open", False))
        want_screenshot = bool(args.get("screenshot", True))
        full_page = bool(args.get("full_page", True))
        skip_verify = bool(args.get("skip_verify", False))
        # DNS pre-check unless skipped — saves 30s on dead-DNS guesses
        if not skip_verify:
            v = await d._handlers["verify_url"](d, {"url": url})
            if not v.get("ok"):
                return {
                    "url": url, "verified": False,
                    "error": v.get("error"),
                    "elapsed_ms": int((_time.time() - t0) * 1000),
                }
        # Navigate (auto-start happens inside _go thanks to 7.7.5)
        go_res = await d._handlers["go"](d, {"url": url})
        # Extract text
        text_res = await d._handlers["text"](d, {})
        text = text_res.get("text", "") if isinstance(text_res, dict) else str(text_res)
        out = {
            "url": go_res.get("url", url),
            "title": go_res.get("title"),
            "status": go_res.get("status"),
            "text": text,
            "session": current_session_ctx.get(),
        }
        if go_res.get("walled"):
            out["walled"] = go_res["walled"]
            out["advice"] = go_res.get("advice")
        # Skills surfaced during the inner `go` (opt-in via VIBATCHIUM_SKILLS).
        if go_res.get("skills"):
            out["skills"] = go_res["skills"]
        # Screenshot
        if want_screenshot:
            try:
                shot = await d._handlers["screenshot"](
                    d, {"full_page": full_page})
                if isinstance(shot, dict) and shot.get("png_b64"):
                    out["screenshot_b64"] = shot["png_b64"]
            except Exception as exc:  # noqa: BLE001
                out["screenshot_error"] = f"{type(exc).__name__}: {exc}"
        # Optional auto-close
        out["closed"] = False
        if not keep_open:
            try:
                await d._handlers["stop"](d, {})
                out["closed"] = True
            except Exception:  # noqa: BLE001
                pass
        out["elapsed_ms"] = int((_time.time() - t0) * 1000)
        return out

    @daemon.handler("shutdown")
    async def _shutdown(d, args):
        # caller wants the whole daemon process to exit
        d._stopping.set()
        return {"shutting_down": True}

    # ─── navigation ───────────────────────────────────────────────────────

    @daemon.handler("go")
    async def _go(d, args):
        """Navigate. Wave 5.4: detect Cloudflare/DataDome walls and surface
        an advisory in the response so callers can switch backends.

        Wave 7.7.5 ergonomic: if no session is running for the current
        session-name, auto-spawn one (headless by default for `go`-first
        callers — the assumption is they're using vibatchium for content
        extraction, not visual debugging). Set VIBATCHIUM_NO_AUTO_START=1
        to disable and require explicit `start` first.
        """
        from . import backends as _backends
        # Wave 7.7.5: auto-start if no session present
        name = current_session_ctx.get()
        if (d.registry.get(name) is None
                and os.environ.get("VIBATCHIUM_NO_AUTO_START", "0").lower()
                    not in ("1", "true", "yes")):
            log.info("auto-start session=%s (no existing session for `go`)", name)
            await d._handlers["start"](d, {"headless": True})
        s = _need_session(d)
        url = args["url"]
        # Per-goal domain allowlist enforcement: while a goal owns this session
        # it pins an allowlist via entry.flags['goal_domains']. Navigations to a
        # host that is neither an allowed host nor a subdomain of one are
        # REFUSED here — the safety boundary the goal declared. Mirrors the
        # per-goal caps gate (which lives in the dispatcher because it keys off
        # the verb name; this one lives here because it keys off the URL arg).
        entry = d.registry.get(name)
        allow_csv = entry.flags.get("goal_domains") if entry else None
        if allow_csv:
            from ..goals.allowlist import origin_allowed, parse_allowlist
            if not origin_allowed(url, parse_allowlist(allow_csv)):
                log.warning("navigation to %r blocked by goal domain allowlist "
                            "(%s) on session %s", url, allow_csv, name)
                raise ValueError(
                    f"navigation to {url!r} blocked by goal domain allowlist "
                    f"({allow_csv})")
        wait_until = args.get("wait_until", "domcontentloaded")
        timeout = int(args.get("timeout_ms", 60_000))
        resp = await s.page.goto(url, wait_until=wait_until, timeout=timeout)
        # Wave 7.7.12: bounded SPA-hydration wait. `domcontentloaded` fires
        # before client-side render on heavy SPAs (Immunefi, HackerOne,
        # bughunters.google.com) — without this wait, `text` returned ""
        # and screenshots came back uniform-blank-white. SSR sites satisfy
        # the predicate in <50ms (no cost); SPAs get up to 5s to hydrate.
        # Opt out per-call with `wait_for_render=False` for raw DCL behavior.
        if args.get("wait_for_render", True):
            min_chars = int(args.get("min_chars", 100))
            render_timeout = int(args.get("render_timeout_ms", 5000))
            try:
                await s.page.wait_for_function(
                    "(n) => document.body && "
                    "document.body.innerText.trim().length > n",
                    arg=min_chars,
                    timeout=render_timeout,
                )
            except Exception:  # noqa: BLE001
                # Bounded — proceed even if page is genuinely empty
                # (about:blank, error pages, asset URLs, etc.).
                pass
        _invalidate_snapshot(d)
        s.frame_ref = None
        status = resp.status if resp else None
        title = await s.page.title()
        out = {"url": s.page.url, "title": title, "status": status}
        wall = _backends.is_walled(title, status)
        if wall:
            out["walled"] = wall
            # Wave 7.7.3 observability: walled-page detection now logs at
            # INFO level so post-run forensics (`vb logs --since 1h`)
            # can find "Reddit blocked at 21:24" without needing
            # VIBATCHIUM_LOG_VERBS=1 to have been on. Closes the gap the
            # second dogfood run surfaced (operator hit Reddit block;
            # detection happened but left no trace in the daemon log).
            name = current_session_ctx.get()
            log.info("walled-page detected session=%s defender=%s url=%s "
                     "status=%s title=%r",
                     name, wall, s.page.url, status, title[:80])
            # Hint backend swap if we're still on patchright (nodriver beats
            # patchright on hardest Cloudflare gates per 2026 benchmark).
            entry = d.registry.get(name)
            current_backend = entry.flags.get("backend") if entry else None
            if current_backend in (None, "patchright"):
                # Two escalation levers, both keep cookies (same persistent
                # profile): (1) posture — headed drops the headless screen/
                # GPU/scrollbar tells the UA override can't reach, often enough
                # on its own and cheaper than a backend swap; (2) backend —
                # nodriver beats patchright on the hardest Cloudflare gates
                # (2026 benchmark). Suggest headed first, nodriver if it holds.
                out["advice"] = (
                    f"page looks {wall}-walled. Try headed first (stealthier "
                    f"than headless, keeps cookies): `vb session close {name} "
                    f"&& vb --session {name} start --headed`. If it still "
                    f"walls, swap backend: `vb --session {name} start "
                    f"--backend nodriver` (after close)."
                )
        # Skills: surface per-host field-notes (opt-in via VIBATCHIUM_SKILLS).
        # Best-effort — a skills-store hiccup must never break navigation.
        try:
            from ..skills.handlers import surface_for_url
            sk = surface_for_url(out["url"])
            if sk:
                out["skills"] = sk
        except Exception:  # noqa: BLE001
            log.debug("skill surfacing failed", exc_info=True)
        return out

    @daemon.handler("back")
    async def _back(d, args):
        s = _need_session(d)
        wait_until = args.get("wait_until", "domcontentloaded")
        timeout = int(args.get("timeout_ms", 15_000))
        try:
            await s.page.go_back(wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.info("go_back soft-failed: %s", exc)
        _invalidate_snapshot(d)
        s.frame_ref = None
        return {"url": s.page.url}

    @daemon.handler("forward")
    async def _forward(d, args):
        s = _need_session(d)
        wait_until = args.get("wait_until", "domcontentloaded")
        timeout = int(args.get("timeout_ms", 15_000))
        try:
            await s.page.go_forward(wait_until=wait_until, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            log.info("go_forward soft-failed: %s", exc)
        _invalidate_snapshot(d)
        s.frame_ref = None
        return {"url": s.page.url}

    @daemon.handler("reload")
    async def _reload(d, args):
        s = _need_session(d)
        wait_until = args.get("wait_until", "domcontentloaded")
        await s.page.reload(wait_until=wait_until)
        _invalidate_snapshot(d)
        return {"url": s.page.url}

    @daemon.handler("url")
    async def _url(d, args):
        s = _need_session(d)
        return {"url": s.page.url}

    @daemon.handler("title")
    async def _title(d, args):
        s = _need_session(d)
        return {"title": await s.page.title()}

    # ─── content extraction ───────────────────────────────────────────────

    @daemon.handler("text")
    async def _text(d, args):
        # Accept both `selector` (legacy MCP param) and `target` (modern,
        # matches click/fill/hover). Route through _resolve_target so @eN
        # and @text:/@label: prefixes work consistently.
        sel = args.get("target") or args.get("selector")
        if sel:
            loc = _resolve_target(d, sel)
            return {"text": await loc.inner_text()}
        s = _need_session(d)
        return {"text": await s.page.inner_text("body")}

    @daemon.handler("html")
    async def _html(d, args):
        sel = args.get("target") or args.get("selector")
        if sel:
            loc = _resolve_target(d, sel)
            return {"html": await loc.inner_html()}
        s = _need_session(d)
        return {"html": await s.page.content()}

    @daemon.handler("eval")
    async def _eval(d, args):
        s = _need_session(d)
        expr = args["expr"]
        # Patchright's isolated-context default is what we want for stealth.
        return {"value": await s.page.evaluate(expr)}

    @daemon.handler("attr")
    async def _attr(d, args):
        target = args.get("target") or args.get("selector")
        if not target:
            raise ValueError("attr requires `target` (or legacy `selector`)")
        loc = _resolve_target(d, target)
        return {"value": await loc.get_attribute(args["name"])}

    @daemon.handler("value")
    async def _value(d, args):
        target = args.get("target") or args.get("selector")
        if not target:
            raise ValueError("value requires `target` (or legacy `selector`)")
        loc = _resolve_target(d, target)
        return {"value": await loc.input_value()}

    # ─── input ────────────────────────────────────────────────────────────

    @daemon.handler("keys")
    async def _keys(d, args):
        s = _need_session(d)
        await s.page.keyboard.press(args["keys"])
        return {"pressed": args["keys"]}

    # ─── screenshot ───────────────────────────────────────────────────────

    @daemon.handler("screenshot")
    async def _screenshot(d, args):
        s = _need_session(d)
        full_page = bool(args.get("full_page", False))
        path = args.get("path")
        png = await s.page.screenshot(full_page=full_page)
        if path:
            # Always secure-write — page captures may include authenticated
            # sessions (banking, dashboards) and must not leak via umask.
            secure_write(Path(path), png)
            return {"path": path}
        return {"png_b64": base64.b64encode(png).decode()}

    # ─── element model: map + interactive verbs ───────────────────────────

    @daemon.handler("map")
    async def _map(d, args):
        s = _need_session(d)
        depth = args.get("depth")
        # Map operates against the active frame if one is set, else page.
        surface = s.frame_ref if s.frame_ref is not None else s.page
        snap = await elements.take_snapshot(surface, depth=depth)
        d._prev_snapshot = d._snapshot
        d._snapshot = snap
        return {
            "url": snap.url,
            "count": len(snap.refs),
            "text": snap.text(indent=bool(args.get("indent", True))),
        }

    @daemon.handler("diff_map")
    async def _diff_map(d, args):
        s = _need_session(d)
        prev = d._snapshot
        surface = s.frame_ref if s.frame_ref is not None else s.page
        new = await elements.take_snapshot(surface)
        d._prev_snapshot = prev
        d._snapshot = new
        return {"text": elements.diff(prev, new)}

    @daemon.handler("click")
    async def _click(d, args):
        """Click an @eN ref or selector.

        With auto_dismiss_banners=True (default off): on an "intercepted by another
        element" failure, try dismiss_banners once and retry. This is the
        productionization fix for the common case where a consent banner covers
        the target — Playwright's auto-wait reports the element as not actionable.
        """
        target = args["target"]
        timeout = int(args.get("timeout_ms", 30_000))
        auto_dismiss = bool(args.get("auto_dismiss_banners", False))
        entry = d.registry.get(current_session_ctx.get())
        humanize = bool(entry.flags.get("humanize")) if entry else False
        loc = _resolve_target(d, target)

        async def _do_click(loc):
            # When humanize is on, add a Bezier approach + dwell, but the actual
            # click stays Playwright's verified click (hit-tested at dispatch),
            # so we can never click the wrong element.
            if humanize:
                from ..humanize import humanized_locator_approach, sample_dwell_ms
                pos = await humanized_locator_approach(
                    entry.session.page, loc, cursor=entry.flags.get("_cursor"))
                if pos is not None:
                    entry.flags["_cursor"] = pos
                await loc.click(timeout=timeout, delay=sample_dwell_ms())
            else:
                await loc.click(timeout=timeout)

        try:
            await _do_click(loc)
            return {"clicked": target, "humanized": humanize}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc).lower()
            intercepted = "intercepts pointer events" in msg or "subtree intercepts" in msg \
                          or "element is not stable" in msg or "is not visible" in msg
            if not auto_dismiss or not intercepted:
                raise
            # Try to dismiss banners and retry once
            log.info("click intercepted — attempting auto-dismiss + retry")
            # We need the dismiss_banners handler — call via the daemon's table
            try:
                await d._handlers["dismiss_banners"](d, {"prefer": "reject", "max_clicks": 1})
            except Exception:  # noqa: BLE001
                pass
            # Re-resolve (snapshot may have been invalidated by the banner click)
            loc = _resolve_target(d, target)
            await _do_click(loc)
            return {"clicked": target, "auto_dismissed": True, "humanized": humanize}

    @daemon.handler("dblclick")
    async def _dblclick(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.dblclick(timeout=int(args.get("timeout_ms", 30_000)))
        return {"dblclicked": args["target"]}

    @daemon.handler("fill")
    async def _fill(d, args):
        """Fill an input. Wave 6.3a: `use_secret: 'site:key'` resolves the
        secret from the vault at fill time. The resolved value NEVER appears
        in the response, the daemon log, or any cache."""
        loc = _resolve_target(d, args["target"])
        if args.get("use_secret"):
            from .. import secrets as _secrets
            ref = args["use_secret"]
            value = _secrets.resolve_secret_reference(ref)
            # Use a plain fill but mask the value from any logging
            await loc.fill(value, timeout=int(args.get("timeout_ms", 30_000)))
            return {"filled": args["target"], "from_secret": ref}
        await loc.fill(args["text"], timeout=int(args.get("timeout_ms", 30_000)))
        return {"filled": args["target"]}

    @daemon.handler("type")
    async def _type(d, args):
        loc = _resolve_target(d, args["target"])
        timeout = int(args.get("timeout_ms", 30_000))
        entry = d.registry.get(current_session_ctx.get())
        humanize = bool(entry.flags.get("humanize")) if entry else False
        # Humanize only when on AND the caller didn't pin an explicit delay.
        if humanize and not args.get("delay_ms"):
            from ..humanize import humanized_type
            await humanized_type(entry.session.page, loc, args["text"],
                                 timeout_ms=timeout)
            return {"typed": args["target"], "humanized": True}
        await loc.press_sequentially(args["text"], delay=int(args.get("delay_ms", 0)),
                                     timeout=timeout)
        return {"typed": args["target"]}

    @daemon.handler("hover")
    async def _hover(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.hover(timeout=int(args.get("timeout_ms", 30_000)))
        return {"hovered": args["target"]}

    @daemon.handler("focus")
    async def _focus(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.focus(timeout=int(args.get("timeout_ms", 30_000)))
        return {"focused": args["target"]}

    @daemon.handler("press")
    async def _press(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.press(args["keys"], timeout=int(args.get("timeout_ms", 30_000)))
        return {"pressed": args["keys"], "on": args["target"]}

    @daemon.handler("check")
    async def _check(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.check(timeout=int(args.get("timeout_ms", 30_000)))
        return {"checked": args["target"]}

    @daemon.handler("uncheck")
    async def _uncheck(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.uncheck(timeout=int(args.get("timeout_ms", 30_000)))
        return {"unchecked": args["target"]}

    @daemon.handler("select")
    async def _select(d, args):
        loc = _resolve_target(d, args["target"])
        value = args.get("value")
        label = args.get("label")
        index = args.get("index")
        kwargs = {}
        if value is not None: kwargs["value"] = value
        if label is not None: kwargs["label"] = label
        if index is not None: kwargs["index"] = index
        result = await loc.select_option(**kwargs)
        return {"selected": result}

    @daemon.handler("scroll")
    async def _scroll(d, args):
        s = _need_session(d)
        if "target" in args and args["target"]:
            loc = _resolve_target(d, args["target"])
            await loc.scroll_into_view_if_needed()
            return {"scrolled_to": args["target"]}
        dx = int(args.get("dx", 0))
        dy = int(args.get("dy", 0))
        await s.page.mouse.wheel(dx, dy)
        return {"scrolled": [dx, dy]}

    @daemon.handler("is")
    async def _is(d, args):
        """Element state check: visible / enabled / checked / hidden."""
        loc = _resolve_target(d, args["target"])
        state = args.get("state", "visible")
        method = {
            "visible": loc.is_visible,
            "hidden": loc.is_hidden,
            "enabled": loc.is_enabled,
            "disabled": loc.is_disabled,
            "checked": loc.is_checked,
            "editable": loc.is_editable,
        }.get(state)
        if method is None:
            raise ValueError(f"unknown state: {state}")
        return {"state": state, "value": await method()}

    # ─── viewport ─────────────────────────────────────────────────────────

    @daemon.handler("viewport")
    async def _viewport(d, args):
        s = _need_session(d)
        if "width" in args and "height" in args:
            await s.page.set_viewport_size(
                {"width": int(args["width"]), "height": int(args["height"])}
            )
        size = s.page.viewport_size or {}
        return {"width": size.get("width"), "height": size.get("height")}

    # ─── storage (cookies + localStorage + sessionStorage) ────────────────

    @daemon.handler("storage_export")
    async def _storage_export(d, args):
        s = _need_session(d)
        path = args.get("path")
        if path:
            await s.context.storage_state(path=path)
            return {"path": path}
        state = await s.context.storage_state()
        return {"state": state}

    @daemon.handler("storage_restore")
    async def _storage_restore(d, args):
        """Restore cookies + per-origin localStorage + sessionStorage from a
        Playwright storage-state JSON.

        Cookies apply context-wide via add_cookies. Per-origin localStorage and
        sessionStorage are replayed by navigating to each origin's URL once
        and writing via JS — this is how Playwright itself rehydrates state at
        context creation, but we do it in-session.

        We RESTORE the original page URL afterwards unless it was about:blank
        OR the current URL is already one of the origins (in which case we
        don't navigate at all — caller is on a real page and we leave them there).
        """
        import json as _json
        path = args.get("path")
        if path:
            state = _json.loads(Path(path).read_text())
        else:
            state = args.get("state") or {}

        s = _need_session(d)
        await s.context.add_cookies(state.get("cookies", []))

        origins = state.get("origins") or []
        origin_urls = {o.get("origin") for o in origins if o.get("origin")}
        original_url = s.page.url
        # Restore-in-place when current URL is one of the origins
        in_place = any(original_url.startswith(u) for u in origin_urls if u)

        if origins and not in_place:
            page = s.page
            for origin in origins:
                url = origin.get("origin")
                ls = origin.get("localStorage") or []
                ss = origin.get("sessionStorage") or []
                if not url or (not ls and not ss):
                    continue
                await page.goto(url, wait_until="domcontentloaded")
                if ls:
                    await page.evaluate(
                        """(items) => {
                            for (const {name, value} of items) {
                                try { localStorage.setItem(name, value); } catch(e) {}
                            }
                        }""",
                        ls,
                    )
                if ss:
                    await page.evaluate(
                        """(items) => {
                            for (const {name, value} of items) {
                                try { sessionStorage.setItem(name, value); } catch(e) {}
                            }
                        }""",
                        ss,
                    )
            # Restore caller's original URL if it was meaningful
            if original_url and original_url not in ("about:blank", ""):
                await page.goto(original_url, wait_until="domcontentloaded")
                _invalidate_snapshot(d)
        elif origins and in_place:
            # Already on a relevant origin — write directly without navigation
            page = s.page
            for origin in origins:
                if not (original_url.startswith(origin.get("origin", "") or "_")):
                    continue
                for store_key, payload in (("localStorage", origin.get("localStorage") or []),
                                           ("sessionStorage", origin.get("sessionStorage") or [])):
                    if payload:
                        await page.evaluate(
                            f"""(items) => {{
                                for (const {{name, value}} of items) {{
                                    try {{ {store_key}.setItem(name, value); }} catch(e) {{}}
                                }}
                            }}""",
                            payload,
                        )

        # Count what we actually restored
        ls_total = sum(len(o.get("localStorage") or []) for o in origins)
        ss_total = sum(len(o.get("sessionStorage") or []) for o in origins)
        return {
            "cookies": len(state.get("cookies", [])),
            "origins": len(origins),
            "localStorage_items": ls_total,
            "sessionStorage_items": ss_total,
            "in_place": in_place,
        }

    @daemon.handler("cookies")
    async def _cookies(d, args):
        s = _need_session(d)
        return {"cookies": await s.context.cookies()}

    # ─── waits ────────────────────────────────────────────────────────────

    @daemon.handler("wait_selector")
    async def _wait_selector(d, args):
        s = _need_session(d)
        sel = args["selector"]
        state = args.get("state", "visible")
        timeout = int(args.get("timeout_ms", 30_000))
        await s.page.wait_for_selector(sel, state=state, timeout=timeout)
        return {"matched": sel, "state": state}

    @daemon.handler("wait_ref")
    async def _wait_ref(d, args):
        s = _need_session(d)
        ref = args["ref"]
        state = args.get("state", "visible")
        timeout = int(args.get("timeout_ms", 30_000))
        loc = elements.resolve(s.page, getattr(d, "_snapshot", None), ref)
        await loc.wait_for(state=state, timeout=timeout)
        return {"matched": ref, "state": state}

    @daemon.handler("wait_url")
    async def _wait_url(d, args):
        s = _need_session(d)
        pattern = args["pattern"]
        timeout = int(args.get("timeout_ms", 30_000))
        await s.page.wait_for_url(pattern, timeout=timeout)
        return {"url": s.page.url}

    @daemon.handler("wait_load")
    async def _wait_load(d, args):
        s = _need_session(d)
        state = args.get("state", "load")
        timeout = int(args.get("timeout_ms", 30_000))
        await s.page.wait_for_load_state(state, timeout=timeout)
        return {"state": state}

    @daemon.handler("wait_fn")
    async def _wait_fn(d, args):
        s = _need_session(d)
        expr = args["expr"]
        timeout = int(args.get("timeout_ms", 30_000))
        await s.page.wait_for_function(expr, timeout=timeout)
        return {"satisfied": True}

    @daemon.handler("sleep")
    async def _sleep(d, args):
        import asyncio as _asyncio
        ms = int(args.get("ms", 1000))
        await _asyncio.sleep(ms / 1000)
        return {"slept_ms": ms}

    # ─── pages ────────────────────────────────────────────────────────────

    @daemon.handler("pages")
    async def _pages(d, args):
        s = _need_session(d)
        out = []
        for i, p in enumerate(s.context.pages):
            out.append({"index": i, "url": p.url, "title": await p.title(),
                        "active": p is s.page})
        return {"pages": out}

    @daemon.handler("page_new")
    async def _page_new(d, args):
        s = _need_session(d)
        page = await s.context.new_page()
        s.page = page
        return {"url": page.url, "index": len(s.context.pages) - 1}

    @daemon.handler("page_switch")
    async def _page_switch(d, args):
        s = _need_session(d)
        i = int(args["index"])
        s.page = s.context.pages[i]
        await s.page.bring_to_front()
        return {"url": s.page.url, "index": i}

    @daemon.handler("page_close")
    async def _page_close(d, args):
        s = _need_session(d)
        await s.page.close()
        if s.context.pages:
            s.page = s.context.pages[0]
        return {"remaining": len(s.context.pages)}
