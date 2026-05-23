"""Second batch of handlers: semantic locators, iframes, mouse, file ops,
dialogs, downloads, PDF, tracing, overrides, network capture, annotated
screenshots, content replacement.

Registered via `register_extra(daemon)` from server.py after the base set.
"""
from __future__ import annotations

import asyncio
import base64
import json as _json
import logging
import re
import re as _re_local
import time
from io import BytesIO
from pathlib import Path

from . import elements, observe as _observe_mod

log = logging.getLogger("patchium.handlers_extra")

# Pillow is optional — only required for `screenshot --annotate`. We import
# lazily to keep the base install thin.
try:  # pragma: no cover - depends on whether pillow is installed
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PILLOW = True
except Exception:  # noqa: BLE001
    _HAS_PILLOW = False


def _session(d):
    if d.session is None:
        raise RuntimeError("no browser session — run `patchium start` or `attach` first")
    return d.session


def _surface(d):
    """Active page-or-frame. All locator-class verbs use this so iframe
    switching is transparent."""
    return _session(d).target


def register_extra(daemon) -> None:
    # ─── find (semantic locators) ─────────────────────────────────────────

    @daemon.handler("find")
    async def _find(d, args):
        """Locate elements by one of several semantic strategies.
        kind: text | label | placeholder | role | testid | xpath | alt | title | css
        """
        surf = _surface(d)
        kind = args.get("kind", "text")
        query = args["query"]
        exact = bool(args.get("exact", False))

        if kind == "text":
            loc = surf.get_by_text(query, exact=exact)
        elif kind == "label":
            loc = surf.get_by_label(query, exact=exact)
        elif kind == "placeholder":
            loc = surf.get_by_placeholder(query, exact=exact)
        elif kind == "role":
            name = args.get("name")
            loc = surf.get_by_role(query, name=name, exact=exact) if name else surf.get_by_role(query)
        elif kind == "testid":
            loc = surf.get_by_test_id(query)
        elif kind == "xpath":
            loc = surf.locator(f"xpath={query}")
        elif kind == "alt":
            loc = surf.get_by_alt_text(query, exact=exact)
        elif kind == "title":
            loc = surf.get_by_title(query, exact=exact)
        elif kind == "css":
            loc = surf.locator(query)
        else:
            raise ValueError(f"unknown locator kind: {kind}")

        count = await loc.count()
        first_text = ""
        if count > 0:
            try:
                first_text = (await loc.first.inner_text(timeout=1_000))[:200]
            except Exception:  # noqa: BLE001
                pass
        return {"count": count, "kind": kind, "query": query, "first_text": first_text}

    @daemon.handler("count")
    async def _count(d, args):
        """Count elements matching a CSS selector or @eN."""
        surf = _surface(d)
        target = args["target"]
        if target.startswith("@e") or (len(target) > 1 and target[0] == "e" and target[1:].isdigit()):
            loc = elements.resolve(surf, getattr(d, "_snapshot", None), target)
        else:
            loc = surf.locator(target)
        return {"count": await loc.count()}

    @daemon.handler("content")
    async def _content(d, args):
        """Replace the page's HTML wholesale (Vibium parity)."""
        s = _session(d)
        html = args["html"]
        wait_until = args.get("wait_until", "domcontentloaded")
        await s.page.set_content(html, wait_until=wait_until)
        return {"set": True, "url": s.page.url}

    # ─── frames ───────────────────────────────────────────────────────────

    @daemon.handler("frames")
    async def _frames(d, args):
        """List live (non-detached) frames. Patchright keeps detached child
        frames in main_frame.child_frames until GC; filter them so stale
        iframe references from prior navigations don't show up."""
        s = _session(d)

        def is_live(frame):
            try:
                return not frame.is_detached()
            except Exception:  # noqa: BLE001
                return True

        def walk(frame, depth=0):
            if not is_live(frame):
                return
            yield (frame, depth)
            for child in frame.child_frames:
                yield from walk(child, depth + 1)

        seen = set()
        out = []
        for frame, depth in walk(s.page.main_frame):
            # de-dupe by url+name in case Patchright still reports duplicates
            key = (frame.name, frame.url)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "name": frame.name,
                "url": frame.url,
                "depth": depth,
                "is_main": frame is s.page.main_frame,
                "active": frame is (s.frame_ref or s.page.main_frame),
            })
        return {"frames": out}

    @daemon.handler("frame")
    async def _frame(d, args):
        """Switch the active iframe context for subsequent ops.
        Match by name (exact) or by URL substring; pass no name/url to clear."""
        s = _session(d)
        name = args.get("name")
        url = args.get("url")
        if not name and not url:
            s.frame_ref = None
            return {"active": "main", "cleared": True}

        def is_live(frame):
            try:
                return not frame.is_detached()
            except Exception:  # noqa: BLE001
                return True

        def walk(frame):
            if not is_live(frame):
                return
            yield frame
            for child in frame.child_frames:
                yield from walk(child)

        for f in walk(s.page.main_frame):
            if name and f.name == name:
                s.frame_ref = f
                return {"active": f.name or "(unnamed)", "url": f.url}
            if url and url in (f.url or ""):
                s.frame_ref = f
                return {"active": f.name or "(unnamed)", "url": f.url}
        raise RuntimeError(f"no frame matching name={name!r} url={url!r}")

    # ─── mouse (xy pixel control) ─────────────────────────────────────────

    @daemon.handler("mouse")
    async def _mouse(d, args):
        """Pixel-coord mouse control.

        Wave 6.2b: if the session has humanize enabled (via `humanize_on`),
        `click` and `wheel` actions route through the humanization layer —
        Bezier mouse trajectory, gaussian-sampled dwell, sinusoidal scroll
        inertia. `move`/`dblclick`/`down`/`up` stay direct (don't make sense
        to humanize a raw `mouse.down` event).
        """
        from .registry import current_session_ctx as _ctx
        s = _session(d)
        entry = d.registry.get(_ctx.get())
        humanize = bool(entry.flags.get("humanize")) if entry else False
        action = args["action"]
        x = float(args.get("x", 0))
        y = float(args.get("y", 0))
        button = args.get("button", "left")
        m = s.page.mouse
        if action == "click":
            if humanize:
                from ..humanize import humanized_click
                cursor = entry.flags.get("_cursor")
                new_cursor = await humanized_click(
                    s.page, x, y, button=button, cursor_pos=cursor,
                )
                entry.flags["_cursor"] = new_cursor
            else:
                await m.click(x, y, button=button)
        elif action == "dblclick":
            await m.dblclick(x, y, button=button)
        elif action == "move":
            if humanize:
                from ..humanize import humanized_move
                cursor = entry.flags.get("_cursor") if entry else None
                await humanized_move(s.page, x, y, start=cursor)
                if entry: entry.flags["_cursor"] = (x, y)
            else:
                await m.move(x, y, steps=int(args.get("steps", 1)))
        elif action == "down":
            await m.down(button=button)
        elif action == "up":
            await m.up(button=button)
        elif action == "wheel":
            dx = float(args.get("dx", 0))
            dy = float(args.get("dy", 0))
            if humanize:
                from ..humanize import humanized_scroll
                await humanized_scroll(s.page, dx, dy)
            else:
                await m.wheel(dx, dy)
        else:
            raise ValueError(f"unknown mouse action: {action}")
        return {"action": action, "x": x, "y": y, "humanized": humanize}

    # ─── Wave 6.2b: humanize per-session toggle ──────────────────────────

    @daemon.handler("humanize_on")
    async def _humanize_on(d, args):
        from .registry import current_session_ctx as _ctx
        entry = d.registry.get(_ctx.get())
        if entry is None:
            raise RuntimeError(
                "humanize requires a running session — start one first"
            )
        entry.flags["humanize"] = True
        return {"humanize": True, "session": entry.name}

    @daemon.handler("humanize_off")
    async def _humanize_off(d, args):
        from .registry import current_session_ctx as _ctx
        entry = d.registry.get(_ctx.get())
        if entry is None:
            return {"humanize": False, "note": "no running session"}
        entry.flags["humanize"] = False
        entry.flags.pop("_cursor", None)
        return {"humanize": False, "session": entry.name}

    @daemon.handler("humanize_status")
    async def _humanize_status(d, args):
        from .registry import current_session_ctx as _ctx
        entry = d.registry.get(_ctx.get())
        if entry is None:
            return {"humanize": False, "note": "no running session"}
        return {"humanize": bool(entry.flags.get("humanize")),
                "session": entry.name}

    # ─── upload ───────────────────────────────────────────────────────────

    @daemon.handler("upload")
    async def _upload(d, args):
        """Set files on an input[type=file] element."""
        target = args["target"]
        files = args["files"]
        if isinstance(files, str):
            files = [files]
        surf = _surface(d)
        if target.startswith("@e") or (len(target) > 1 and target[0] == "e" and target[1:].isdigit()):
            loc = elements.resolve(surf, getattr(d, "_snapshot", None), target)
        else:
            loc = surf.locator(target)
        await loc.set_input_files(files)
        return {"uploaded": files, "to": target}

    # ─── dialogs ──────────────────────────────────────────────────────────

    @daemon.handler("dialog_policy")
    async def _dialog_policy(d, args):
        """Set how the next dialog (alert/confirm/prompt) is handled.

        Idempotent: replaces any prior handler cleanly via `page.remove_listener`.
        Registered on the BrowserContext so popup-window dialogs also fire.
        Each new page in the context (existing + future) gets the same policy.
        """
        s = _session(d)
        action = args.get("action", "dismiss")  # accept | dismiss
        text = args.get("text")  # prompt-input text when accepting

        async def handle(dialog):
            try:
                if action == "accept":
                    if text is not None:
                        await dialog.accept(prompt_text=text)
                    else:
                        await dialog.accept()
                else:
                    await dialog.dismiss()
            except Exception:  # noqa: BLE001
                pass

        # Remove the prior handler (if any) from all live pages
        prior = s.dialog_policy.get("_handle") if isinstance(s.dialog_policy, dict) else None
        if prior is not None:
            for p in list(s.context.pages):
                try:
                    p.remove_listener("dialog", prior)
                except Exception:  # noqa: BLE001
                    pass

        # Register on existing pages AND on future ones (popups inherit the policy)
        for p in s.context.pages:
            p.on("dialog", handle)

        # Wire to future pages via context — install once, reuse for the
        # lifetime of this dialog policy
        prior_page_hook = s.dialog_policy.get("_page_hook") if isinstance(s.dialog_policy, dict) else None
        if prior_page_hook is not None:
            try:
                s.context.remove_listener("page", prior_page_hook)
            except Exception:  # noqa: BLE001
                pass

        def on_new_page_register_dialog(page):
            page.on("dialog", handle)
        s.context.on("page", on_new_page_register_dialog)

        s.dialog_policy = {
            "action": action, "text": text,
            "_handle": handle, "_page_hook": on_new_page_register_dialog,
        }
        return {"action": action, "text": text}

    # ─── downloads ────────────────────────────────────────────────────────

    @daemon.handler("download_arm")
    async def _download_arm(d, args):
        """Start collecting downloads. They appear in subsequent `download_list` calls."""
        s = _session(d)

        async def on_download(dl):
            entry = {
                "index": len(s.downloads),
                "url": dl.url,
                "suggested_filename": dl.suggested_filename,
                "download": dl,
            }
            s.downloads.append(entry)

        # idempotent: only register once
        if not getattr(d, "_download_armed", False):
            s.page.on("download", on_download)
            d._download_armed = True
        return {"armed": True, "count": len(s.downloads)}

    @daemon.handler("download_list")
    async def _download_list(d, args):
        s = _session(d)
        return {
            "downloads": [
                {"index": e["index"], "url": e["url"], "suggested_filename": e["suggested_filename"]}
                for e in s.downloads
            ]
        }

    @daemon.handler("download_save")
    async def _download_save(d, args):
        s = _session(d)
        i = int(args["index"])
        path = args["path"]
        entry = s.downloads[i]
        await entry["download"].save_as(path)
        return {"saved": path, "from_url": entry["url"]}

    # ─── pdf ──────────────────────────────────────────────────────────────

    @daemon.handler("pdf")
    async def _pdf(d, args):
        s = _session(d)
        path = args["path"]
        await s.page.pdf(path=path, format=args.get("format", "Letter"))
        return {"path": path}

    # ─── tracing (record) ─────────────────────────────────────────────────

    @daemon.handler("record_start")
    async def _record_start(d, args):
        s = _session(d)
        await s.context.tracing.start(
            screenshots=bool(args.get("screenshots", True)),
            snapshots=bool(args.get("snapshots", True)),
            sources=bool(args.get("sources", False)),
        )
        return {"recording": True}

    @daemon.handler("record_stop")
    async def _record_stop(d, args):
        s = _session(d)
        path = args["path"]
        await s.context.tracing.stop(path=path)
        return {"path": path}

    # ─── highlight (visual debug) ─────────────────────────────────────────

    @daemon.handler("highlight")
    async def _highlight(d, args):
        """Draw a 3-second red outline on an @eN or selector via injected JS."""
        target = args["target"]
        ms = int(args.get("ms", 3000))
        surf = _surface(d)
        if target.startswith("@e") or (len(target) > 1 and target[0] == "e" and target[1:].isdigit()):
            loc = elements.resolve(surf, getattr(d, "_snapshot", None), target)
        else:
            loc = surf.locator(target)
        await loc.evaluate(
            "(el, ms) => {"
            "const prev = el.style.outline;"
            "el.style.outline = '3px solid #ff2222';"
            "el.style.outlineOffset = '2px';"
            "setTimeout(() => { el.style.outline = prev; }, ms);"
            "}",
            ms,
        )
        return {"highlighted": target, "ms": ms}

    # ─── geolocation + media overrides ────────────────────────────────────

    @daemon.handler("geolocation")
    async def _geolocation(d, args):
        s = _session(d)
        if args.get("clear"):
            await s.context.clear_permissions()
            return {"cleared": True}
        lat = float(args["lat"])
        lng = float(args["lng"])
        accuracy = float(args.get("accuracy", 10))
        await s.context.set_geolocation({"latitude": lat, "longitude": lng, "accuracy": accuracy})
        await s.context.grant_permissions(["geolocation"])
        return {"lat": lat, "lng": lng, "accuracy": accuracy}

    @daemon.handler("media")
    async def _media(d, args):
        s = _session(d)
        kwargs = {}
        if "media" in args:        kwargs["media"] = args["media"]
        if "color_scheme" in args: kwargs["color_scheme"] = args["color_scheme"]
        if "reduced_motion" in args: kwargs["reduced_motion"] = args["reduced_motion"]
        if "forced_colors" in args: kwargs["forced_colors"] = args["forced_colors"]
        await s.page.emulate_media(**kwargs)
        return kwargs

    # ─── request interception (route) ─────────────────────────────────────

    @daemon.handler("route_add")
    async def _route_add(d, args):
        """Add a request-interception rule. Mode: abort | fulfill | passthrough.

        - abort:  request fails. Use to block heavy resources (images/css/fonts)
                  to save bandwidth, or to block third-party trackers during recon.
        - fulfill: return a synthetic response. Useful for API mocking and tests.
                   body / status / content_type / headers come from args.
        - passthrough (default): observe + log without altering. Use to record
                   that this pattern was matched (visible in `route_list`).

        Multi-page aware: route is registered on the context, so popups inherit it.
        """
        s = _session(d)
        pattern = args["pattern"]
        mode = args.get("mode", "passthrough")
        body = args.get("body", "")
        status = int(args.get("status", 200))
        content_type = args.get("content_type", "text/plain")
        headers_in = args.get("headers") or {}

        if not hasattr(s, "_routes"):
            s._routes = []  # list of {pattern, mode, body, status, content_type, headers, hits}
        # Allow updating an existing rule with the same pattern
        existing = next((r for r in s._routes if r["pattern"] == pattern), None)
        if existing is not None:
            existing.update(mode=mode, body=body, status=status,
                            content_type=content_type, headers=headers_in)
            existing.setdefault("hits", 0)
            return {"updated": pattern, "mode": mode}

        async def handler(route, request):
            rule = next((r for r in s._routes if r["pattern"] == pattern), None)
            if rule is None:
                await route.continue_()
                return
            rule["hits"] = rule.get("hits", 0) + 1
            if rule["mode"] == "abort":
                await route.abort()
            elif rule["mode"] == "fulfill":
                await route.fulfill(
                    status=rule["status"],
                    body=rule["body"],
                    content_type=rule["content_type"],
                    headers=rule.get("headers") or {},
                )
            else:
                await route.continue_()

        await s.context.route(pattern, handler)
        s._routes.append({
            "pattern": pattern, "mode": mode, "body": body, "status": status,
            "content_type": content_type, "headers": headers_in, "hits": 0,
            "_handler": handler,
        })
        return {"added": pattern, "mode": mode}

    @daemon.handler("route_list")
    async def _route_list(d, args):
        s = _session(d)
        rules = getattr(s, "_routes", [])
        return {
            "routes": [
                {"pattern": r["pattern"], "mode": r["mode"], "hits": r.get("hits", 0)}
                for r in rules
            ]
        }

    @daemon.handler("route_clear")
    async def _route_clear(d, args):
        s = _session(d)
        rules = getattr(s, "_routes", [])
        pattern = args.get("pattern")
        if pattern:
            rule = next((r for r in rules if r["pattern"] == pattern), None)
            if rule is None:
                return {"cleared": 0}
            try:
                await s.context.unroute(pattern, rule.get("_handler"))
            except Exception:  # noqa: BLE001
                pass
            rules.remove(rule)
            return {"cleared": 1, "pattern": pattern}
        # clear all
        n = len(rules)
        for r in rules:
            try:
                await s.context.unroute(r["pattern"], r.get("_handler"))
            except Exception:  # noqa: BLE001
                pass
        s._routes = []
        return {"cleared": n}

    @daemon.handler("wait_response")
    async def _wait_response(d, args):
        """Wait for a response matching URL pattern; optionally return its body."""
        s = _session(d)
        pattern = args["pattern"]
        timeout = int(args.get("timeout_ms", 30_000))
        capture_body = bool(args.get("body", False))
        max_body = int(args.get("max_body", 1_000_000))

        # page.wait_for_response accepts a string (substring) or regex source
        resp = await s.page.wait_for_response(pattern, timeout=timeout)
        out = {
            "url": resp.url,
            "status": resp.status,
            "ok": resp.ok,
            "headers": dict(resp.headers),
        }
        if capture_body:
            try:
                body_bytes = await resp.body()
                if len(body_bytes) > max_body:
                    body_bytes = body_bytes[:max_body]
                    out["truncated"] = True
                # text-ish bodies → utf-8; binary → base64
                try:
                    out["text"] = body_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    out["b64"] = base64.b64encode(body_bytes).decode()
            except Exception as exc:  # noqa: BLE001
                out["body_error"] = f"{type(exc).__name__}: {exc}"
        return out

    # ─── network capture ─────────────────────────────────────────────────

    @daemon.handler("network_start")
    async def _network_start(d, args):
        s = _session(d)
        if s.network.get("capturing"):
            return {"capturing": True, "already_on": True}
        s.network["events"].clear()
        s.network["max"] = int(args.get("max", 500))

        def on_request(req):
            if len(s.network["events"]) >= s.network["max"]:
                s.network["events"].pop(0)
            s.network["events"].append({
                "phase": "request",
                "url": req.url,
                "method": req.method,
                "resource_type": req.resource_type,
                "ts": time.time(),
            })

        def on_response(resp):
            if len(s.network["events"]) >= s.network["max"]:
                s.network["events"].pop(0)
            s.network["events"].append({
                "phase": "response",
                "url": resp.url,
                "status": resp.status,
                "ts": time.time(),
            })

        s.page.on("request", on_request)
        s.page.on("response", on_response)
        s.network["_handlers"] = (on_request, on_response)
        s.network["capturing"] = True
        return {"capturing": True, "max": s.network["max"]}

    @daemon.handler("network_stop")
    async def _network_stop(d, args):
        s = _session(d)
        if not s.network.get("capturing"):
            return {"capturing": False}
        h = s.network.get("_handlers")
        if h:
            try:
                s.page.remove_listener("request", h[0])
                s.page.remove_listener("response", h[1])
            except Exception:  # noqa: BLE001
                pass
        s.network["capturing"] = False
        return {"capturing": False, "events": len(s.network["events"])}

    @daemon.handler("network_dump")
    async def _network_dump(d, args):
        s = _session(d)
        path = args.get("path")
        events = s.network.get("events", [])
        if path:
            Path(path).write_text(_json.dumps(events, indent=2))
            return {"path": path, "events": len(events)}
        return {"events": events}

    # ─── HAR export (full network capture format) ────────────────────────

    @daemon.handler("har_start")
    async def _har_start(d, args):
        """Start recording a full HAR (HTTP Archive 1.2) to disk.

        Playwright's HAR mode captures request + response (with bodies),
        timings, redirects, content types — everything dev tools shows under
        the Network tab. Compatible with Wireshark, Chrome DevTools import,
        and HAR analysis tools (har-validator, har-tools-cli).

        Unlike `network_start` (in-memory ring buffer of summaries), HAR
        capture writes to disk and includes response bodies by default.

        Caveats: HAR recording is per-context — calling har_start re-creates
        the BrowserContext if one is already attached without HAR, which loses
        in-page state. We refuse to do that destructive thing automatically;
        require explicit har_start *before* any go/navigation if you want a
        full capture from session start.
        """
        s = _session(d)
        path = args["path"]
        content = args.get("content", "embed")  # embed | attach | omit
        url_filter = args.get("url_filter")     # glob

        # Tracing is the modern PW way; for true HAR we use context tracing's
        # HAR recording, but the simpler API is BrowserContext.set_har... oh wait
        # that doesn't exist. Use context.tracing or, more reliably,
        # the launch_persistent_context(record_har_path=...) at start time.
        # Since we're already started, the workable path is page-level via
        # `page.route` to log each event into a HAR-shaped JSON. Playwright
        # actually exposes context.record_har_path only at context-creation;
        # for in-session HAR capture, the simplest fallback is to do it
        # ourselves: subscribe to request/response and write a HAR-shaped
        # JSON manually on stop.

        if hasattr(s, "_har_state") and s._har_state.get("recording"):
            return {"recording": True, "already_on": True, "path": s._har_state["path"]}

        har_state = {
            "recording": True,
            "path": path,
            "url_filter": url_filter,
            "entries": [],
            "started_at": time.time(),
            "_handlers": [],
            "_pending": {},  # request -> partial entry
        }

        def on_request(req):
            if url_filter and url_filter not in req.url:
                return
            har_state["_pending"][req] = {
                "startedDateTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "time": 0,
                "request": {
                    "method": req.method,
                    "url": req.url,
                    "httpVersion": "HTTP/2",  # best-effort; PW doesn't expose
                    "headers": [{"name": k, "value": v} for k, v in req.headers.items()],
                    "queryString": [],
                    "headersSize": -1,
                    "bodySize": -1,
                },
                "_t0": time.time(),
            }

        async def on_response(resp):
            req = resp.request
            entry = har_state["_pending"].pop(req, None)
            if entry is None:
                return
            elapsed_ms = int((time.time() - entry.pop("_t0")) * 1000)
            try:
                body_bytes = await resp.body()
            except Exception:  # noqa: BLE001
                body_bytes = b""
            body_text = None
            body_b64 = None
            if content == "embed":
                try:
                    body_text = body_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    body_b64 = base64.b64encode(body_bytes).decode()
            entry["time"] = elapsed_ms
            entry["response"] = {
                "status": resp.status,
                "statusText": resp.status_text or "",
                "httpVersion": "HTTP/2",
                "headers": [{"name": k, "value": v} for k, v in resp.headers.items()],
                "cookies": [],
                "content": {
                    "size": len(body_bytes),
                    "mimeType": resp.headers.get("content-type", "application/octet-stream"),
                    **({"text": body_text} if body_text is not None else {}),
                    **({"encoding": "base64", "text": body_b64} if body_b64 is not None else {}),
                },
                "redirectURL": "",
                "headersSize": -1,
                "bodySize": len(body_bytes),
            }
            entry["cache"] = {}
            entry["timings"] = {"send": 0, "wait": elapsed_ms, "receive": 0}
            har_state["entries"].append(entry)

        # Wire on the active page + future pages
        page = s.page
        page.on("request", on_request)
        page.on("response", on_response)
        har_state["_handlers"].append((page, on_request, on_response))

        # Also re-wire on context for new pages
        def on_new_page(new_page):
            new_page.on("request", on_request)
            new_page.on("response", on_response)
            har_state["_handlers"].append((new_page, on_request, on_response))
        s.context.on("page", on_new_page)
        har_state["_context_hook"] = on_new_page

        s._har_state = har_state
        return {"recording": True, "path": path, "url_filter": url_filter}

    @daemon.handler("har_stop")
    async def _har_stop(d, args):
        """Stop HAR recording and flush entries to disk."""
        s = _session(d)
        har_state = getattr(s, "_har_state", None)
        if not har_state or not har_state.get("recording"):
            return {"recording": False, "note": "not recording"}

        # Detach handlers
        for page, on_req, on_resp in har_state["_handlers"]:
            try:
                page.remove_listener("request", on_req)
                page.remove_listener("response", on_resp)
            except Exception:  # noqa: BLE001
                pass
        try:
            s.context.remove_listener("page", har_state["_context_hook"])
        except Exception:  # noqa: BLE001
            pass

        # Write HAR file
        from .. import __version__ as _pv
        har_doc = {
            "log": {
                "version": "1.2",
                "creator": {"name": "patchium", "version": _pv},
                "browser": {"name": "Chrome", "version": ""},
                "pages": [],  # we don't track page boundaries here — could add
                "entries": [{k: v for k, v in e.items() if not k.startswith("_")}
                            for e in har_state["entries"]],
            }
        }
        Path(har_state["path"]).write_text(_json.dumps(har_doc, indent=2))
        s._har_state = {"recording": False, "path": har_state["path"]}
        return {
            "recording": False,
            "path": har_state["path"],
            "entries": len(har_state["entries"]),
            "elapsed_s": round(time.time() - har_state["started_at"], 2),
        }

    # ─── annotated screenshot ─────────────────────────────────────────────

    @daemon.handler("screenshot_annotate")
    async def _screenshot_annotate(d, args):
        """Take a screenshot and overlay @eN bounding boxes + ref labels.

        Uses `aria_snapshot(boxes=True)` to get bounding rects, parses them,
        renders via Pillow. Falls back to a plain screenshot if Pillow is missing.
        """
        s = _session(d)
        full_page = bool(args.get("full_page", False))
        path = args.get("path") or "screenshot.png"

        if not _HAS_PILLOW:
            # Fail loudly rather than silently fall back — caller asked for
            # annotation, doesn't get it without Pillow.
            raise RuntimeError(
                "screenshot --annotate requires Pillow. Install with "
                "`pip install pillow` (or omit --annotate)."
            )

        # boxes=True yields lines like:  [ref=e3] [box=L,T,W,H]
        snap_text = await s.page.aria_snapshot(mode="ai", boxes=True)
        box_re = re.compile(r"\[ref=(e\d+)\][^\n]*\[box=([\d.]+),([\d.]+),([\d.]+),([\d.]+)\]")
        boxes = []
        for m in box_re.finditer(snap_text):
            ref, L, T, W, H = m.group(1), *(float(v) for v in m.groups()[1:])
            boxes.append((ref, L, T, W, H))

        png_bytes = await s.page.screenshot(full_page=full_page)
        img = Image.open(BytesIO(png_bytes)).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
        except Exception:  # noqa: BLE001
            font = ImageFont.load_default()
        for ref, x, y, w, h in boxes:
            x, y, w, h = int(x), int(y), int(w), int(h)
            draw.rectangle([x, y, x + w, y + h], outline=(255, 32, 32, 220), width=2)
            label = f"@{ref}"
            tw = max(20, len(label) * 7)
            draw.rectangle([x, max(0, y - 14), x + tw, y], fill=(255, 32, 32, 220))
            draw.text((x + 2, max(0, y - 14)), label, fill=(255, 255, 255, 255), font=font)
        out = Image.alpha_composite(img, overlay).convert("RGB")
        out.save(path)
        return {"path": path, "annotated": True, "boxes": len(boxes)}

    # ─── cookie banner / consent dismissal ────────────────────────────────

    @daemon.handler("dismiss_banners")
    async def _dismiss_banners(d, args):
        """Heuristic: scan the current snapshot for common consent / cookie /
        newsletter dismiss buttons and click the most likely one.

        Args:
          dry_run: bool — return the candidate without clicking
          max_clicks: int (default 1) — how many banners to dismiss in one call

        Looks for buttons/links whose accessible name matches well-known consent
        verbs (Accept / Allow / Agree / Got it / Continue / Reject all / OK).
        Prefers shorter, more direct labels (single-button banners) over long
        verbose ones. Returns the ones it clicked (or would have) for transparency.
        """
        s = _session(d)
        # Common consent / dismissal labels, ordered by directness (most-direct first)
        ACCEPT_LABELS = [
            r"\baccept all\b", r"\baccept\b", r"\bagree to all\b", r"\bi agree\b",
            r"\bagree\b", r"\ballow all\b", r"\ballow\b", r"\baccept cookies\b",
            r"\bgot it\b", r"\bok\b", r"\bcontinue\b", r"\bunderstood\b",
            r"\bdismiss\b", r"\bclose\b",
        ]
        # Buttons-without-accept (last resort, reject preserves privacy more)
        REJECT_LABELS = [
            r"\breject all\b", r"\breject\b", r"\bdecline\b", r"\bno thanks\b",
            r"\bno, thanks\b",
        ]

        prefer = (args.get("prefer") or "reject").lower()  # accept | reject
        dry_run = bool(args.get("dry_run", False))
        max_clicks = int(args.get("max_clicks", 1))

        labels = REJECT_LABELS + ACCEPT_LABELS if prefer == "reject" else ACCEPT_LABELS + REJECT_LABELS

        # Snapshot the current page, parse for buttons/links matching the labels
        snap = await elements.take_snapshot(s.page)
        d._prev_snapshot = d._snapshot
        d._snapshot = snap
        yaml_text = snap.text(indent=True)

        # extract (role, name, ref) tuples from the snapshot
        # pattern: `- {role} "{name}" ... @eN`
        entry_re = _re_local.compile(
            r'^\s*-\s+(?P<role>button|link)\s+"(?P<name>[^"]+)"[^\n@]*@(?P<ref>e\d+)',
            _re_local.MULTILINE,
        )
        candidates = []
        for m in entry_re.finditer(yaml_text):
            name = m["name"].strip()
            for prio, pattern in enumerate(labels):
                if _re_local.search(pattern, name, _re_local.IGNORECASE):
                    candidates.append({
                        "ref": "@" + m["ref"],
                        "role": m["role"],
                        "name": name,
                        "priority": prio,
                    })
                    break

        # Sort: lowest priority number = highest preference
        candidates.sort(key=lambda c: c["priority"])

        if dry_run or not candidates:
            return {"dry_run": dry_run, "found": len(candidates),
                    "candidates": candidates[:5], "clicked": []}

        clicked = []
        for cand in candidates[:max_clicks]:
            try:
                loc = elements.resolve(s.page, snap, cand["ref"])
                await loc.click(timeout=5_000)
                clicked.append(cand)
            except Exception as exc:  # noqa: BLE001
                cand["error"] = f"{type(exc).__name__}: {exc}"
                clicked.append(cand)

        # post-click snapshot probably changed; invalidate
        d._prev_snapshot = d._snapshot
        d._snapshot = None
        return {"found": len(candidates), "clicked": clicked,
                "preference": prefer}

    # ─── eval_handle / handle table (DOM handle API) ──────────────────────

    @daemon.handler("eval_handle")
    async def _eval_handle(d, args):
        """Evaluate JS in the page and store the result as a JSHandle.

        Returns {"handle": "h_N", "preview": "<short JSON if serializable>"}.
        Use the handle with `handle_eval` to chain operations, or
        `handle_dispose` to release.

        Use cases: traverse shadow DOM (closed-only paths excluded), inspect
        a NodeList, hold a reference to an element across mutations, pass DOM
        objects between calls without re-querying.
        """
        s = _session(d)
        expr = args["expr"]
        handle = await s.page.evaluate_handle(expr)
        d._handle_counter += 1
        hid = f"h_{d._handle_counter}"
        d._handles[hid] = handle
        # Best-effort preview — JSON.stringify of the value when it's serializable
        try:
            preview = await handle.evaluate("(v) => { try { return JSON.stringify(v).slice(0, 500); } catch(e) { return String(v).slice(0, 500); } }")
        except Exception:  # noqa: BLE001
            preview = None
        return {"handle": hid, "preview": preview}

    @daemon.handler("handle_eval")
    async def _handle_eval(d, args):
        """Run a JS expression with a stored handle as `arg`.

        Example: handle = eval_handle("document.querySelectorAll('button')")
                 handle_eval(handle, "(buttons) => Array.from(buttons).map(b => b.textContent)")
        """
        hid = args["handle"]
        if hid not in d._handles:
            raise KeyError(f"unknown handle: {hid} (use eval_handle first)")
        expr = args["expr"]
        return {"value": await d._handles[hid].evaluate(expr)}

    @daemon.handler("handle_list")
    async def _handle_list(d, args):
        return {"handles": list(d._handles.keys()), "count": len(d._handles)}

    @daemon.handler("handle_dispose")
    async def _handle_dispose(d, args):
        hid = args["handle"]
        h = d._handles.pop(hid, None)
        if h is None:
            return {"disposed": False, "reason": "unknown handle"}
        try:
            await h.dispose()
        except Exception as exc:  # noqa: BLE001
            return {"disposed": True, "warning": f"{type(exc).__name__}: {exc}"}
        return {"disposed": True}

    @daemon.handler("handle_dispose_all")
    async def _handle_dispose_all(d, args):
        n = len(d._handles)
        for h in list(d._handles.values()):
            try:
                await h.dispose()
            except Exception:  # noqa: BLE001
                pass
        d._handles.clear()
        return {"disposed": n}

    # ─── observe → act ────────────────────────────────────────────────────

    @daemon.handler("observe")
    async def _observe(d, args):
        s = _session(d)
        intent = args["intent"]
        use_llm = bool(args.get("llm", False))
        force = bool(args.get("force", False))
        return await _observe_mod.observe(s.page, intent, use_llm=use_llm,
                                          force_refresh=force, daemon=d)

    @daemon.handler("act")
    async def _act(d, args):
        """Execute a previously-observed (or freshly-computed) plan.

        Wave 5.3 self-heal: on cache hit, prefer the step's `_durable` selector
        (role+name) over the snapshot-specific @eN. If the durable selector
        fails (page changed), invalidate the cache, re-observe, retry once
        with the fresh plan. Reduces LLM calls for repeated intents on stable
        pages; gracefully recovers when pages drift.

        Returns include `via: durable|ref|self_healed` per step so callers
        can see how each action resolved.
        """
        s = _session(d)
        intent = args["intent"]
        use_llm = bool(args.get("llm", False))

        result = await _observe_mod.observe(s.page, intent, use_llm=use_llm,
                                            force_refresh=False, daemon=d)
        plan = result.get("plan") or []
        if not plan:
            return {"executed": 0, "intent": intent, "reason": "empty plan"}

        async def _do_step(step: dict, prefer_durable: bool) -> tuple:
            """Run one step. Return (inner_result, via_label).

            On cache hit (`prefer_durable=True`):
              - Try the durable role+name selector with a TIGHT 3s timeout
                (cache is supposed to "just work"; slow = page changed).
              - On failure, raise — caller triggers self-heal (re-observe)
                rather than falling back to the snapshot-specific @eN, which
                is stale after navigation/mutation.

            On cache miss (`prefer_durable=False`):
              - Use the @eN ref directly with the verb's default timeout.
                The snapshot was just taken in observe(), so @eN is fresh.
            """
            verb = step["verb"]
            base_args = {"target": step["target"]}
            if verb == "fill" and "text" in step:
                base_args["text"] = step["text"]
            if prefer_durable:
                durable = step.get("_durable")
                if not durable:
                    # Cache hit but no durable info — happens for plans saved
                    # before Wave 5.3. Force self-heal.
                    raise RuntimeError("cached plan lacks durable selector")
                return (
                    await d._handlers[verb](d, {**base_args, "target": durable,
                                               "timeout_ms": 3_000}),
                    "durable",
                )
            return (await d._handlers[verb](d, base_args), "ref")

        executed = []
        self_healed = False
        cached = bool(result.get("cached"))
        i = 0
        while i < len(plan):
            step = plan[i]
            try:
                inner, via = await _do_step(step, prefer_durable=cached)
                executed.append({"step": step, "result": inner, "via": via})
                i += 1
            except Exception as exc:  # noqa: BLE001
                # First failure on a cached plan → invalidate + re-observe + retry once.
                if cached and not self_healed:
                    self_healed = True
                    _observe_mod.cache_invalidate(s.page.url, intent)
                    fresh = await _observe_mod.observe(
                        s.page, intent, use_llm=use_llm,
                        force_refresh=True, daemon=d,
                    )
                    fresh_plan = fresh.get("plan") or []
                    if not fresh_plan:
                        executed.append({"step": step, "error": str(exc),
                                         "self_heal": "no_fresh_plan"})
                        break
                    # Swap to fresh plan, retry from index i (re-aligning if length differs).
                    plan = fresh_plan
                    result = fresh
                    cached = False
                    if i >= len(plan):
                        i = 0  # short fresh plan — start over
                    continue
                # Already self-healed or never cached → terminal failure.
                executed.append({"step": step, "error": str(exc)})
                break

        return {
            "executed": len(executed),
            "intent": intent,
            "source": result.get("source"),
            "self_healed": self_healed,
            "steps": executed,
        }

    # ─── Wave 6.1a: live-view server ─────────────────────────────────────

    @daemon.handler("liveview_start")
    async def _liveview_start(d, args):
        """Start the live-view HTTP+WS server. One server per daemon.

        Returns the viewer URL. Idempotent — calling twice returns the
        existing server's URL without restart.
        """
        if getattr(d, "_liveview_server", None) is not None:
            srv = d._liveview_server
            return {"already_running": True,
                    "url": srv.url(),
                    "host": srv.host, "port": srv.port,
                    "takeover": srv.takeover, "fps": srv.fps}
        from ..liveview import LiveViewServer
        host = args.get("host", "127.0.0.1")
        port = int(args.get("port", 9223))
        fps = int(args.get("fps", 5))
        jpeg_quality = int(args.get("jpeg_quality", 60))
        takeover = bool(args.get("takeover", False))
        # Security: refuse non-loopback bind unless explicitly opted in
        if host not in ("127.0.0.1", "::1", "localhost") and not args.get("insecure_public"):
            raise RuntimeError(
                f"refusing to bind live-view to {host!r} without insecure_public=true — "
                f"public bind exposes full browser control to anyone who connects"
            )
        srv = LiveViewServer(d.registry, host=host, port=port, fps=fps,
                             jpeg_quality=jpeg_quality, takeover=takeover)
        await srv.start()
        d._liveview_server = srv
        return {"started": True, "url": srv.url(),
                "host": host, "port": port, "takeover": takeover, "fps": fps}

    @daemon.handler("liveview_stop")
    async def _liveview_stop(d, args):
        srv = getattr(d, "_liveview_server", None)
        if srv is None:
            return {"already_stopped": True}
        await srv.stop()
        d._liveview_server = None
        return {"stopped": True}

    @daemon.handler("liveview_url")
    async def _liveview_url(d, args):
        """Return the viewer URL for the current (or named) session.
        If the server isn't running, return null in `url`."""
        srv = getattr(d, "_liveview_server", None)
        if srv is None:
            return {"running": False, "url": None}
        from .registry import current_session_ctx as _ctx
        name = args.get("session") or _ctx.get()
        if not d.registry.has(name):
            return {"running": True, "url": srv.url(), "session_url": None,
                    "note": f"no session {name!r} — pass a name or start one"}
        return {"running": True, "url": srv.url(),
                "session_url": srv.url(name)}

    # ─── Wave 6.3d: vision-first primitive ───────────────────────────────

    def _bump_vision_stats(entry, result):
        """Wave 7.2: shared per-session vision accounting. Called by every
        vision_* handler so `vision stats` reflects all vision usage, not
        only vision_click."""
        stats = entry.flags.setdefault("vision_stats", {
            "calls": 0, "cache_hits": 0, "input_tokens": 0,
            "output_tokens": 0, "cost_usd": 0.0,
        })
        stats["calls"] += 1
        if result.get("via") == "cache":
            stats["cache_hits"] += 1
        else:
            tok = result.get("tokens", {}) or {}
            stats["input_tokens"] += tok.get("input", 0)
            stats["output_tokens"] += tok.get("output", 0)
            stats["cost_usd"] += result.get("cost_usd", 0)

    async def _vision_locate(d, args):
        """Shared vision_find/_click/_type body — returns (entry, result)."""
        from .. import vision as _vision
        from collections import deque as _deque
        s = _session(d)
        from .registry import current_session_ctx as _ctx
        entry = d.registry.get(_ctx.get())
        cache_log = entry.flags.setdefault("vision_rate", _deque())
        result = await _vision.find_element(
            s.page, args["intent"],
            min_confidence=float(args.get("min_confidence", 0.6)),
            cache_log=cache_log,
            max_per_minute=int(args.get("max_per_minute", 30)),
        )
        _bump_vision_stats(entry, result)
        return s, result

    @daemon.handler("vision_click")
    async def _vision_click(d, args):
        """Find a UI element matching the verbal description via Claude vision,
        then click it. Cache hit on identical (screenshot, intent) → no API call.
        """
        s, result = await _vision_locate(d, args)
        # devicePixelRatio scaling: Claude sees screenshot at the device px;
        # our screenshots are NOT scaled (they're the raw device pixels), so
        # mouse coords need to be in CSS pixels = device_px / dpr.
        dpr = result.get("devicePixelRatio", 1) or 1
        cx = result["x"] / dpr
        cy = result["y"] / dpr
        await s.page.mouse.click(cx, cy, button=args.get("button", "left"))
        return {
            "clicked": True, "x": cx, "y": cy,
            "confidence": result["confidence"],
            "via": result["via"], "rationale": result.get("rationale", ""),
        }

    @daemon.handler("vision_find")
    async def _vision_find(d, args):
        """Like vision_click but just return coords without clicking — useful
        for inspecting what the vision model sees."""
        _s, result = await _vision_locate(d, args)
        return result

    @daemon.handler("vision_type")
    async def _vision_type(d, args):
        """vision_click + type the given text into whatever was clicked."""
        s, result = await _vision_locate(d, args)
        dpr = result.get("devicePixelRatio", 1) or 1
        cx = result["x"] / dpr
        cy = result["y"] / dpr
        await s.page.mouse.click(cx, cy)
        await s.page.keyboard.type(args["text"])
        return {"typed": True, "x": cx, "y": cy,
                "confidence": result["confidence"], "via": result["via"]}

    @daemon.handler("vision_stats")
    async def _vision_stats(d, args):
        """Return cumulative vision API usage stats for the current session."""
        from .registry import current_session_ctx as _ctx
        entry = d.registry.get(_ctx.get())
        if entry is None:
            return {"calls": 0, "cache_hits": 0,
                    "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        return entry.flags.get("vision_stats", {
            "calls": 0, "cache_hits": 0,
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
        })

    @daemon.handler("vision_clear_cache")
    async def _vision_clear_cache(d, args):
        from .. import vision as _vision
        cleared = _vision.cache_clear()
        return {"cleared": cleared}

    @daemon.handler("vision_budget")
    async def _vision_budget(d, args):
        """Report today's + lifetime vision spend, configured caps, remaining.
        Also supports `reset: 'today' | 'lifetime' | 'all'` for ops use."""
        from .. import vision as _vision
        if args.get("reset"):
            return _vision.reset_spend(scope=args["reset"])
        snapshot = _vision.check_budget(estimate_usd=0)  # query, no consumption
        out = {
            "today_usd": round(snapshot["today"], 6),
            "lifetime_usd": round(snapshot["lifetime"], 6),
            "daily_cap_usd": snapshot["daily_cap"],
            "lifetime_cap_usd": snapshot["lifetime_cap"],
        }
        if snapshot["daily_cap"] is not None:
            out["daily_remaining_usd"] = max(
                0.0, round(snapshot["daily_cap"] - snapshot["today"], 6)
            )
        if snapshot["lifetime_cap"] is not None:
            out["lifetime_remaining_usd"] = max(
                0.0, round(snapshot["lifetime_cap"] - snapshot["lifetime"], 6)
            )
        return out

    # ─── Wave 6.3c: prompt-injection safety ──────────────────────────────

    @daemon.handler("safety_set")
    async def _safety_set(d, args):
        """Set safety mode for the current session.

        mode: 'off' (default) | 'flag-only' | 'wrap' | 'redact'
          - flag-only: add prompt_injection_risk + signals to responses
          - wrap: wrap suspicious regions in <UNTRUSTED_CONTENT> tags
          - redact: replace suspicious regions with [REDACTED-PROMPT-INJECTION-N]
        """
        from .registry import current_session_ctx as _ctx
        entry = d.registry.get(_ctx.get())
        if entry is None:
            raise RuntimeError("no running session")
        mode = args.get("mode", "off")
        if mode not in ("off", "flag-only", "wrap", "redact"):
            raise ValueError(
                f"unknown safety mode {mode!r}; "
                "valid: off | flag-only | wrap | redact"
            )
        entry.flags["safety_mode"] = mode
        return {"safety_mode": mode, "session": entry.name}

    @daemon.handler("safety_status")
    async def _safety_status(d, args):
        from .registry import current_session_ctx as _ctx
        entry = d.registry.get(_ctx.get())
        if entry is None:
            return {"safety_mode": "off", "note": "no running session"}
        return {"safety_mode": entry.flags.get("safety_mode", "off"),
                "session": entry.name}

    @daemon.handler("safety_scan")
    async def _safety_scan(d, args):
        """Scan an arbitrary string and return the classifier result without
        mutating any content. Useful for testing patterns."""
        from .. import safety as _safety
        text = args.get("text", "")
        return _safety.classify(text)

    # ─── Wave 5.4b: fingerprint scorer ───────────────────────────────────

    @daemon.handler("fingerprint")
    async def _fingerprint(d, args):
        """Open a bot-detection target and extract a numeric stealth score.

        Built-in targets:
          sannysoft  → bot.sannysoft.com (counts passed rows in the WebDriver table)
          creepjs    → abrahamjuliot.github.io/creepjs (trust score 0..100)
          brotector  → kaliiiiiiiiii.github.io/brotector (leak count, lower = better)

        Also accepts a raw URL via `--target https://...` plus an optional
        `--extract <js>` expression to override the score extraction.

        Returns:
          {target, url, backend, score, raw_signals}
        """
        s = _session(d)
        target = args.get("target", "sannysoft").lower()
        custom_url = args.get("url")
        custom_extract = args.get("extract")
        settle_ms = int(args.get("settle_ms", 5_000))

        BUILTINS = {
            "sannysoft": {
                "url": "https://bot.sannysoft.com/",
                "extract": """() => {
                    // Sannysoft has multiple test tables; count green vs red cells.
                    const cells = Array.from(document.querySelectorAll('td'));
                    const green = cells.filter(c => /rgb\\(0,\\s?255/.test(getComputedStyle(c).backgroundColor) || c.classList.contains('passed')).length;
                    const red = cells.filter(c => /rgb\\(255,\\s?0/.test(getComputedStyle(c).backgroundColor) || c.classList.contains('failed')).length;
                    const total = green + red;
                    return {
                        score: total ? Math.round(100 * green / total) : null,
                        passed: green, failed: red, total,
                    };
                }""",
            },
            "creepjs": {
                "url": "https://abrahamjuliot.github.io/creepjs/",
                "extract": """() => {
                    // CreepJS renders a trust score percent like "47.5%" near the top
                    const m = document.body.innerText.match(/(\\d{1,3}(?:\\.\\d+)?)\\s*%/);
                    const score = m ? parseFloat(m[1]) : null;
                    const lies_el = document.body.innerText.match(/(\\d+)\\s+lies/i);
                    return {score, lies: lies_el ? parseInt(lies_el[1]) : null};
                }""",
            },
            "brotector": {
                "url": "https://kaliiiiiiiiii-vinyzu.github.io/Brotector/",
                "extract": """() => {
                    // Brotector lists detection signals; count those that fired.
                    const fired = document.querySelectorAll('.detection-fired, [data-fired="true"]').length;
                    const all = document.querySelectorAll('.detection, [data-test]').length;
                    return {
                        score: all ? Math.round(100 * (1 - fired / all)) : null,
                        fired, total: all,
                    };
                }""",
            },
        }

        if custom_url:
            url = custom_url
            extract_js = custom_extract or "() => ({raw: document.title})"
        else:
            spec = BUILTINS.get(target)
            if spec is None:
                raise ValueError(
                    f"unknown fingerprint target {target!r}; "
                    f"built-ins: {sorted(BUILTINS)}; or pass `url`"
                )
            url = spec["url"]
            extract_js = custom_extract or spec["extract"]

        await s.page.goto(url, wait_until="networkidle", timeout=60_000)
        # Let JS detection scripts settle
        await asyncio.sleep(settle_ms / 1000)
        signals = await s.page.evaluate(extract_js)

        # Backend tag from session entry (set by registry.create)
        from .registry import current_session_ctx as _ctx
        entry = d.registry.get(_ctx.get())
        backend = entry.flags.get("backend") if entry else None

        return {
            "target": target,
            "url": url,
            "backend": backend,
            "score": signals.get("score") if isinstance(signals, dict) else None,
            "signals": signals,
        }

    # ─── compact map render ──────────────────────────────────────────────

    @daemon.handler("map_compact")
    async def _map_compact(d, args):
        """Map but rendered in browser-use-style one-liner per actionable element."""
        s = _session(d)
        snap = await elements.take_snapshot(s.page, depth=args.get("depth"))
        d._prev_snapshot = getattr(d, "_snapshot", None)
        d._snapshot = snap
        # Walk the YAML and pull just lines that have a ref + a quoted name
        lines = []
        ref_role_name = re.compile(
            r'^\s*-\s+(?P<role>\S+)\s+"(?P<name>[^"]+)"[^@]*@(?P<ref>e\d+)'
        )
        # also handle: `- link @eN`, no name
        ref_role_only = re.compile(r'^\s*-\s+(?P<role>\S+)\s+@(?P<ref>e\d+)')
        for line in snap.text(indent=True).splitlines():
            m = ref_role_name.search(line)
            if m:
                lines.append(f'@{m["ref"]} {m["role"]} "{m["name"]}"')
                continue
            m = ref_role_only.search(line)
            if m:
                lines.append(f'@{m["ref"]} {m["role"]}')
        return {
            "url": snap.url,
            "count": len(lines),
            "text": "\n".join(lines),
        }
