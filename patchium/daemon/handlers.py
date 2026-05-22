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
from .browser import attach_session, close_session, launch_session
from .paths import DEFAULT_PROFILE_DIR

log = logging.getLogger("patchium.handlers")


def _need_session(daemon):
    if daemon.session is None:
        raise RuntimeError("no browser session — run `patchium start` or `patchium attach` first")
    return daemon.session


def _resolve_target(daemon, target: str):
    """Resolve `target` to a Locator. If it looks like a ref (`@eN`, `eN`, `[ref=eN]`)
    use the AX-snapshot resolver; otherwise treat as a raw CSS / Playwright selector."""
    s = _need_session(daemon)
    if target.startswith("@e") or elements.REF_RE.match(target):
        return elements.resolve(s.page, getattr(daemon, "_snapshot", None), target)
    if len(target) > 1 and target[0] == "e" and target[1:].isdigit():
        return elements.resolve(s.page, getattr(daemon, "_snapshot", None), target)
    return s.page.locator(target)


def register_all(daemon) -> None:
    @daemon.handler("ping")
    async def _ping(d, args):
        return {"pong": True, "session": d.session is not None}

    # ─── lifecycle ────────────────────────────────────────────────────────

    @daemon.handler("start")
    async def _start(d, args):
        if d.session is not None:
            return {"already_started": True, "mode": d.session.mode}
        profile = Path(args.get("profile") or DEFAULT_PROFILE_DIR)
        headless = bool(args.get("headless", False))
        d.session = await launch_session(profile, headless=headless)
        return {"started": True, "mode": "launch", "profile": str(profile)}

    @daemon.handler("attach")
    async def _attach(d, args):
        if d.session is not None:
            raise RuntimeError("session already active — stop first")
        cdp_url = args.get("cdp_url") or "http://localhost:9222"
        d.session = await attach_session(cdp_url)
        return {"attached": True, "mode": "attach", "cdp_url": cdp_url}

    @daemon.handler("stop")
    async def _stop(d, args):
        if d.session is None:
            return {"already_stopped": True}
        await close_session(d.session)
        d.session = None
        return {"stopped": True}

    @daemon.handler("status")
    async def _status(d, args):
        sess = d.session
        return {
            "running": sess is not None,
            "mode": sess.mode if sess else None,
            "pid": os.getpid(),
        }

    @daemon.handler("shutdown")
    async def _shutdown(d, args):
        # caller wants the whole daemon process to exit
        d._stopping.set()
        return {"shutting_down": True}

    # ─── navigation ───────────────────────────────────────────────────────

    @daemon.handler("go")
    async def _go(d, args):
        s = _need_session(d)
        url = args["url"]
        wait_until = args.get("wait_until", "domcontentloaded")
        timeout = int(args.get("timeout_ms", 60_000))
        resp = await s.page.goto(url, wait_until=wait_until, timeout=timeout)
        return {
            "url": s.page.url,
            "title": await s.page.title(),
            "status": resp.status if resp else None,
        }

    @daemon.handler("back")
    async def _back(d, args):
        s = _need_session(d)
        await s.page.go_back()
        return {"url": s.page.url}

    @daemon.handler("forward")
    async def _forward(d, args):
        s = _need_session(d)
        await s.page.go_forward()
        return {"url": s.page.url}

    @daemon.handler("reload")
    async def _reload(d, args):
        s = _need_session(d)
        await s.page.reload()
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
        snap = await elements.take_snapshot(s.page, depth=depth)
        d._prev_snapshot = getattr(d, "_snapshot", None)
        d._snapshot = snap
        return {
            "url": snap.url,
            "count": len(snap.refs),
            "text": snap.text(indent=bool(args.get("indent", True))),
        }

    @daemon.handler("diff_map")
    async def _diff_map(d, args):
        s = _need_session(d)
        prev = getattr(d, "_snapshot", None)
        new = await elements.take_snapshot(s.page)
        d._prev_snapshot = prev
        d._snapshot = new
        return {"text": elements.diff(prev, new)}

    @daemon.handler("click")
    async def _click(d, args):
        target = args["target"]
        timeout = int(args.get("timeout_ms", 30_000))
        loc = _resolve_target(d, target)
        await loc.click(timeout=timeout)
        return {"clicked": target}

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
        """Restore cookies + per-origin storage from a Playwright storage-state JSON.

        Playwright loads storage_state at context-creation time, so we apply
        cookies via add_cookies (no re-launch needed) and write localStorage /
        sessionStorage by navigating to each origin and replaying via JS. This
        matches Vibium's `storage restore` semantics while staying in-session.
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
        if origins:
            page = s.page
            original_url = page.url
            for origin in origins:
                url = origin.get("origin")
                items = origin.get("localStorage") or []
                if not url or not items:
                    continue
                await page.goto(url, wait_until="domcontentloaded")
                await page.evaluate(
                    """(items) => {
                        for (const {name, value} of items) {
                            try { localStorage.setItem(name, value); } catch(e) {}
                        }
                    }""",
                    items,
                )
            if original_url and original_url != "about:blank":
                await page.goto(original_url, wait_until="domcontentloaded")

        return {
            "cookies": len(state.get("cookies", [])),
            "origins": len(state.get("origins", [])),
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
