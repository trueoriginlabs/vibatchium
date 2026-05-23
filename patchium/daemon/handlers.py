"""Verb handlers. Each takes (daemon, args:dict) and returns a JSON-serializable value.

Handlers are registered via Daemon.handler(name). The daemon must have an active
BrowserSession for most verbs; lifecycle verbs (start/attach/stop/status) are exempt.
"""
from __future__ import annotations

import base64
import logging
import os
import tempfile
from pathlib import Path

from . import elements
from .paths import (
    DEFAULT_PROFILE_DIR, DEFAULT_SESSION_NAME, PROFILES_DIR, ACTIVE_PROFILE_PATH,
    get_active_profile_dir, get_active_profile_name,
    get_active_session_name, list_profile_names, list_session_names,
    session_dir, set_active_profile_name, set_active_session_name,
)
from .registry import current_session_ctx

log = logging.getLogger("patchium.handlers")


import re as _re

_REF_TARGET_RE = _re.compile(r"^@?(e\d+)$|^\[ref=e\d+\]$")


def _need_session(daemon):
    if daemon.session is None:
        raise RuntimeError("no browser session — run `patchium start` or `patchium attach` first")
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

    If it's a structured ref (`@eN`, `eN`, `[ref=eN]`) use Playwright's
    `aria-ref=` selector engine. Otherwise treat as raw CSS / Playwright
    selector. The daemon-side `_snapshot` cache exists only to log staleness
    diagnostics; the actual resolution goes through Playwright's selector
    engine against the live AX tree.
    """
    s = _need_session(daemon)
    if _is_ref_target(target):
        if daemon._snapshot is None:
            raise RuntimeError(
                f"ref {target!r} cannot be resolved — last `map` was invalidated by "
                f"a navigation. Run `patchium map` to refresh the snapshot first."
            )
        return elements.resolve(s.page, daemon._snapshot, target)
    return s.page.locator(target)


def register_all(daemon) -> None:
    @daemon.handler("ping")
    async def _ping(d, args):
        return {"pong": True, "session": d.session is not None}

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
                # `patchium start --profile work` and address it later as `--session work`)
                if name == DEFAULT_SESSION_NAME:
                    name = raw
                profile_dir = PROFILES_DIR / raw
        else:
            profile_dir = session_dir(name)

        if d.registry.has(name):
            entry = d.registry.get(name)
            return {"already_started": True, "mode": entry.session.mode,
                    "session": name, "profile": str(entry.profile_dir)}

        headless = bool(args.get("headless", False))
        stealth_mouse = bool(args.get("stealth_mouse"))
        backend = args.get("backend") or "patchright"
        try:
            entry = await d.registry.create(
                name, profile_dir=profile_dir, headless=headless,
                stealth_mouse=stealth_mouse, backend=backend,
            )
        except Exception as exc:
            # Surface stealth_mouse failures non-fatally, matching the prior
            # opt-in behavior — if Chrome itself launched OK but stealth_mouse
            # install failed (e.g. missing CDP-Patches), retry without it.
            if stealth_mouse and "stealth" in str(exc).lower():
                entry = await d.registry.create(
                    name, profile_dir=profile_dir, headless=headless,
                    stealth_mouse=False, backend=backend,
                )
                return {"started": True, "mode": "launch",
                        "session": name, "profile": str(entry.profile_dir),
                        "profile_name": entry.profile_dir.name,
                        "backend": backend,
                        "stealth_mouse": False, "stealth_mouse_error": str(exc)}
            raise

        out = {"started": True, "mode": "launch",
               "session": name, "profile": str(entry.profile_dir),
               "profile_name": entry.profile_dir.name,
               "backend": backend}
        if stealth_mouse:
            out["stealth_mouse"] = True
        return out

    # ─── session management ────────────────────────────────────────────

    @daemon.handler("session_new")
    async def _session_new(d, args):
        """Create a new on-disk session/profile dir without launching Chrome.

        Use `start --session NAME` (or `session_start`) to actually launch.
        Idempotent: re-creating an existing session is a no-op that reports
        `created=false, exists=true`.
        """
        name = args.get("name")
        if not name or "/" in name or name.startswith("."):
            raise ValueError(f"bad session name: {name!r}")
        p = PROFILES_DIR / name
        existed = p.exists()
        p.mkdir(parents=True, exist_ok=True)
        return {
            "created": not existed, "exists": existed, "name": name,
            "path": str(p), "profile_dir": str(p),
            "running": d.registry.has(name),
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
        name = args.get("name")
        if not name:
            raise ValueError("session_use requires a name")
        if name not in list_session_names():
            raise ValueError(
                f"unknown session: {name!r} — create with `patchium session new {name}`"
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
        closed = await d.registry.close(name)
        return {"closed": closed, "name": name}

    @daemon.handler("session_close_all")
    async def _session_close_all(d, args):
        n = await d.registry.close_all()
        return {"closed": n}

    @daemon.handler("session_delete")
    async def _session_delete(d, args):
        """Delete a profile dir on disk. Refuses if the session is running,
        active, or is the special 'default'."""
        name = args.get("name")
        if not name:
            raise ValueError("session_delete requires a name")
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
        name = current_session_ctx.get()
        entry = d.registry.get(name)
        return {
            "running": entry is not None,
            "session": name,
            "mode": entry.session.mode if entry else None,
            "pid": os.getpid(),
            "running_sessions": d.registry.list_running(),
        }

    @daemon.handler("shutdown")
    async def _shutdown(d, args):
        # caller wants the whole daemon process to exit
        d._stopping.set()
        return {"shutting_down": True}

    # ─── navigation ───────────────────────────────────────────────────────

    @daemon.handler("go")
    async def _go(d, args):
        """Navigate. Wave 5.4: detect Cloudflare/DataDome walls and surface
        an advisory in the response so callers can switch backends."""
        from . import backends as _backends
        s = _need_session(d)
        url = args["url"]
        wait_until = args.get("wait_until", "domcontentloaded")
        timeout = int(args.get("timeout_ms", 60_000))
        resp = await s.page.goto(url, wait_until=wait_until, timeout=timeout)
        _invalidate_snapshot(d)
        s.frame_ref = None
        status = resp.status if resp else None
        title = await s.page.title()
        out = {"url": s.page.url, "title": title, "status": status}
        wall = _backends.is_walled(title, status)
        if wall:
            out["walled"] = wall
            # Hint backend swap if we're still on patchright (nodriver beats
            # patchright on hardest Cloudflare gates per 2026 benchmark).
            name = current_session_ctx.get()
            entry = d.registry.get(name)
            current_backend = entry.flags.get("backend") if entry else None
            if current_backend in (None, "patchright"):
                out["advice"] = (
                    f"page looks {wall}-walled; try "
                    f"`patchium session close {name} && "
                    f"patchium --session {name} start --backend nodriver`"
                )
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
        s = _need_session(d)
        sel = args.get("selector")
        if sel:
            return {"text": await s.page.locator(sel).inner_text()}
        return {"text": await s.page.inner_text("body")}

    @daemon.handler("html")
    async def _html(d, args):
        s = _need_session(d)
        sel = args.get("selector")
        if sel:
            return {"html": await s.page.locator(sel).inner_html()}
        return {"html": await s.page.content()}

    @daemon.handler("eval")
    async def _eval(d, args):
        s = _need_session(d)
        expr = args["expr"]
        # Patchright's isolated-context default is what we want for stealth.
        return {"value": await s.page.evaluate(expr)}

    @daemon.handler("attr")
    async def _attr(d, args):
        s = _need_session(d)
        return {"value": await s.page.locator(args["selector"]).get_attribute(args["name"])}

    @daemon.handler("value")
    async def _value(d, args):
        s = _need_session(d)
        return {"value": await s.page.locator(args["selector"]).input_value()}

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
        if path:
            await s.page.screenshot(path=path, full_page=full_page)
            return {"path": path}
        # return base64 if no path given
        png = await s.page.screenshot(full_page=full_page)
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
        loc = _resolve_target(d, target)
        try:
            await loc.click(timeout=timeout)
            return {"clicked": target}
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
            loc = _resolve_target(d, target) if _is_ref_target(target) else _need_session(d).page.locator(target)
            await loc.click(timeout=timeout)
            return {"clicked": target, "auto_dismissed": True}

    @daemon.handler("dblclick")
    async def _dblclick(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.dblclick(timeout=int(args.get("timeout_ms", 30_000)))
        return {"dblclicked": args["target"]}

    @daemon.handler("fill")
    async def _fill(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.fill(args["text"], timeout=int(args.get("timeout_ms", 30_000)))
        return {"filled": args["target"]}

    @daemon.handler("type")
    async def _type(d, args):
        loc = _resolve_target(d, args["target"])
        await loc.press_sequentially(args["text"], delay=int(args.get("delay_ms", 0)),
                                     timeout=int(args.get("timeout_ms", 30_000)))
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
