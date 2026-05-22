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
        s = _session(d)
        action = args["action"]
        x = float(args.get("x", 0))
        y = float(args.get("y", 0))
        button = args.get("button", "left")
        m = s.page.mouse
        if action == "click":
            await m.click(x, y, button=button)
        elif action == "dblclick":
            await m.dblclick(x, y, button=button)
        elif action == "move":
            await m.move(x, y, steps=int(args.get("steps", 1)))
        elif action == "down":
            await m.down(button=button)
        elif action == "up":
            await m.up(button=button)
        elif action == "wheel":
            await m.wheel(float(args.get("dx", 0)), float(args.get("dy", 0)))
        else:
            raise ValueError(f"unknown mouse action: {action}")
        return {"action": action, "x": x, "y": y}

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
        """Set how the next dialog (alert/confirm/prompt) is handled."""
        s = _session(d)
        action = args.get("action", "dismiss")  # accept | dismiss
        text = args.get("text")  # prompt-input text when accepting

        # Replace the current handler (Patchright doesn't expose remove)
        async def handle(dialog):
            try:
                if action == "accept":
                    await dialog.accept(prompt_text=text) if text else await dialog.accept()
                else:
                    await dialog.dismiss()
            except Exception:  # noqa: BLE001
                pass

        # off+on idempotent re-registration
        try:
            s.page.remove_listener("dialog", s.dialog_policy.get("_handle"))
        except Exception:  # noqa: BLE001
            pass
        s.page.on("dialog", handle)
        s.dialog_policy = {"action": action, "text": text, "_handle": handle}
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
            await s.page.screenshot(path=path, full_page=full_page)
            return {"path": path, "annotated": False,
                    "note": "Pillow not installed — install patchium[annotate]"}

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

    # ─── observe → act ────────────────────────────────────────────────────

    @daemon.handler("observe")
    async def _observe(d, args):
        s = _session(d)
        intent = args["intent"]
        use_llm = bool(args.get("llm", False))
        force = bool(args.get("force", False))
        return await _observe_mod.observe(s.page, intent, use_llm=use_llm, force_refresh=force)

    @daemon.handler("act")
    async def _act(d, args):
        """Execute a previously-observed (or freshly-computed) plan."""
        s = _session(d)
        intent = args["intent"]
        use_llm = bool(args.get("llm", False))
        result = await _observe_mod.observe(s.page, intent, use_llm=use_llm, force_refresh=False)
        plan = result.get("plan") or []
        if not plan:
            return {"executed": 0, "intent": intent, "reason": "empty plan"}

        executed = []
        for step in plan:
            verb = step["verb"]
            target = step["target"]
            handler_name = verb
            args_for = {"target": target}
            if verb == "fill" and "text" in step:
                args_for["text"] = step["text"]
            # dispatch via the daemon's already-registered handlers
            inner = await d._handlers[handler_name](d, args_for)
            executed.append({"step": step, "result": inner})
        return {"executed": len(executed), "intent": intent,
                "source": result.get("source"), "steps": executed}

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
