"""Wave 6.1a — Live-view server.

Streams the active browser's frames over a WebSocket so you can watch what
an agent is doing in a normal browser tab. Two modes:

- **Read-only** (default): pure observation. Frames flow server → client.
- **Takeover** (`--takeover`): client → server `{type:click,x,y}` /
  `{type:key,code}` events go to `page.mouse.click(x,y)` / `page.keyboard.press(code)`.

Endpoints (default port 9223, bound to 127.0.0.1):
  GET  /                          → session list + viewer links
  GET  /viewer/<session_name>     → HTML viewer (canvas + JS WebSocket client)
  GET  /ws/<session_name>         → WebSocket: binary JPEG frames + JSON takeover

Security: binds 127.0.0.1 by default. `--bind 0.0.0.0` requires the explicit
`--insecure-public` flag because exposing this on a LAN gives whoever connects
full control of your browser. No auth — local-only by design.

Threading: the frame loop does NOT acquire `entry.lock` because Playwright's
`page.screenshot()` is safe to call concurrently with other CDP operations
(holding the lock for 50-200 ms every frame would gum up the session). Takeover
events DO acquire the lock — they're mutations on the page.

Cost: at 5 fps, JPEG quality 60, 1920×1080 viewport ≈ 8-15 KB per frame,
so ~40-75 KB/s per connected viewer. One frame-loop task per session
regardless of viewer count (broadcast model).
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from typing import TYPE_CHECKING

log = logging.getLogger("vibatchium.liveview")

if TYPE_CHECKING:
    from .daemon.registry import SessionRegistry


# Inlined viewer HTML — keeping it in-process means no MANIFEST changes /
# resource-path lookups / static-dir packaging hassles. Two templates:
# INDEX_HTML lists running sessions; VIEWER_HTML is the per-session canvas.

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>vibatchium — live-view</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0a0a0a; color: #e0e0e0; margin: 0; padding: 32px; }
  h1 { font-weight: 300; letter-spacing: -0.5px; }
  .meta { color: #777; font-size: 14px; }
  ul { list-style: none; padding: 0; }
  li { padding: 10px 14px; background: #1a1a1a; border-radius: 6px;
       margin-bottom: 8px; }
  a { color: #6ab7ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .tag { background: #2a4d3a; color: #7fe3a4; padding: 2px 8px; border-radius: 4px;
         font-size: 11px; font-family: monospace; margin-left: 8px; }
  .empty { color: #555; font-style: italic; }
</style>
</head>
<body>
<h1>vibatchium <span class="meta">live-view</span></h1>
<p class="meta">Running sessions (auto-refresh every 5s):</p>
<ul id="sessions"></ul>
<script>
async function refresh() {
  const r = await fetch('/sessions.json');
  const data = await r.json();
  const ul = document.getElementById('sessions');
  if (data.sessions.length === 0) {
    ul.innerHTML = '<li class="empty">no sessions running — try `vibatchium session new foo && vibatchium --session foo start`</li>';
    return;
  }
  ul.innerHTML = data.sessions.map(s =>
    `<li><a href="/viewer/${s.name}">${s.name}</a> <span class="tag">${s.backend || 'patchright'}</span> <span class="meta">${s.url || '(blank)'}</span></li>`
  ).join('');
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""


VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>vibatchium — {name}</title>
<style>
  body {{ margin: 0; background: #000; overflow: hidden;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  #frame {{ display: block; max-width: 100vw; max-height: 100vh;
            margin: 0 auto; cursor: {cursor}; image-rendering: auto; }}
  #status {{ position: fixed; bottom: 8px; left: 8px; padding: 4px 10px;
             background: rgba(0,0,0,0.6); color: #aaa; font-size: 11px;
             border-radius: 4px; font-family: monospace; }}
  #status.takeover {{ color: #7fe3a4; }}
  #status.error {{ color: #ff7777; }}
</style>
</head>
<body>
<img id="frame" alt="loading" />
<div id="status">connecting…</div>
<script>
const NAME = {name_json};
const TAKEOVER = {takeover};
const img = document.getElementById('frame');
const status = document.getElementById('status');
let ws;
let frameCount = 0;
let lastFrameTs = Date.now();
let viewportW = null, viewportH = null;
let currentBlobUrl = null;

function connect() {{
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${{proto}}://${{location.host}}/ws/${{NAME}}`);
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {{
    status.textContent = TAKEOVER ? 'takeover · streaming' : 'streaming';
    status.className = TAKEOVER ? 'takeover' : '';
  }};
  ws.onclose = () => {{
    status.textContent = 'disconnected — retrying in 2s';
    status.className = 'error';
    setTimeout(connect, 2000);
  }};
  ws.onerror = () => {{
    status.textContent = 'error';
    status.className = 'error';
  }};
  ws.onmessage = (e) => {{
    if (typeof e.data === 'string') {{
      // JSON envelope: hello or error
      try {{
        const m = JSON.parse(e.data);
        if (m.type === 'hello') {{
          viewportW = m.viewport_width;
          viewportH = m.viewport_height;
        }} else if (m.type === 'error') {{
          status.textContent = 'error: ' + m.error;
          status.className = 'error';
        }}
      }} catch (err) {{}}
      return;
    }}
    // Binary JPEG frame
    if (currentBlobUrl) URL.revokeObjectURL(currentBlobUrl);
    const blob = new Blob([e.data], {{type: 'image/jpeg'}});
    currentBlobUrl = URL.createObjectURL(blob);
    img.src = currentBlobUrl;
    frameCount++;
    const now = Date.now();
    if (now - lastFrameTs > 1000) {{
      const fps = (frameCount * 1000 / (now - lastFrameTs)).toFixed(1);
      status.textContent = `${{TAKEOVER ? 'takeover · ' : ''}}${{fps}} fps`;
      frameCount = 0;
      lastFrameTs = now;
    }}
  }};
}}

// Map a click on the canvas-rendered image back to page coordinates.
function imageCoords(ev) {{
  const rect = img.getBoundingClientRect();
  const scaleX = (viewportW || img.naturalWidth) / rect.width;
  const scaleY = (viewportH || img.naturalHeight) / rect.height;
  return {{
    x: Math.round((ev.clientX - rect.left) * scaleX),
    y: Math.round((ev.clientY - rect.top) * scaleY),
  }};
}}

function send(evt) {{
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify(evt));
}}

if (TAKEOVER) {{
  img.addEventListener('click', (ev) => {{
    const c = imageCoords(ev);
    send({{type: 'click', x: c.x, y: c.y, button: 'left'}});
  }});
  img.addEventListener('contextmenu', (ev) => {{
    ev.preventDefault();
    const c = imageCoords(ev);
    send({{type: 'click', x: c.x, y: c.y, button: 'right'}});
  }});
  window.addEventListener('keydown', (ev) => {{
    // Only forward when image area has focus-ish (not in dev tools etc).
    if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'TEXTAREA') return;
    if (ev.key.length === 1 && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {{
      send({{type: 'type', text: ev.key}});
    }} else {{
      // Translate to a Playwright-friendly key code.
      send({{type: 'key', code: ev.key, ctrl: ev.ctrlKey, shift: ev.shiftKey,
             alt: ev.altKey, meta: ev.metaKey}});
    }}
    ev.preventDefault();
  }});
  img.addEventListener('wheel', (ev) => {{
    ev.preventDefault();
    send({{type: 'scroll', dx: ev.deltaX, dy: ev.deltaY}});
  }}, {{passive: false}});
}}

connect();
</script>
</body>
</html>"""


def _render_viewer(name: str, takeover: bool) -> str:
    return VIEWER_HTML.format(
        name=name,
        name_json=json.dumps(name),
        takeover='true' if takeover else 'false',
        cursor='crosshair' if takeover else 'default',
    )


class LiveViewServer:
    """One server per daemon. Manages a frame-loop task per active session
    and broadcasts JPEG frames to any connected WebSocket clients."""

    def __init__(self, registry: SessionRegistry, *, host: str = "127.0.0.1",
                 port: int = 9223, fps: int = 5, jpeg_quality: int = 60,
                 takeover: bool = False) -> None:
        self.registry = registry
        self.host = host
        self.port = port
        self.fps = fps
        self.jpeg_quality = jpeg_quality
        self.takeover = takeover
        self.app = None
        self.runner = None
        self.site = None
        # Per-session: set of connected WSResponse objects; frame-loop task
        self._clients: dict[str, set] = defaultdict(set)
        self._frame_tasks: dict[str, asyncio.Task] = {}
        self._running = False

    async def start(self) -> None:
        try:
            from aiohttp import web
        except ImportError as exc:
            raise RuntimeError(
                "live-view requires `pip install vibatchium[liveview]` "
                f"(import error: {exc})"
            ) from exc

        self.app = web.Application()
        self.app.router.add_get('/', self._handle_index)
        self.app.router.add_get('/sessions.json', self._handle_sessions_json)
        self.app.router.add_get('/viewer/{name}', self._handle_viewer)
        self.app.router.add_get('/ws/{name}', self._handle_ws)

        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, self.host, self.port)
        await self.site.start()
        self._running = True
        log.info("live-view listening on http://%s:%d (takeover=%s, fps=%d)",
                 self.host, self.port, self.takeover, self.fps)

    async def stop(self) -> None:
        self._running = False
        # Cancel all frame-loop tasks
        for task in list(self._frame_tasks.values()):
            task.cancel()
        for task in list(self._frame_tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._frame_tasks.clear()
        # Close all WebSocket clients
        for clients in self._clients.values():
            for ws in list(clients):
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass
        self._clients.clear()
        # Stop HTTP server
        if self.site is not None:
            await self.site.stop()
        if self.runner is not None:
            await self.runner.cleanup()
        self.site = None
        self.runner = None
        self.app = None
        log.info("live-view stopped")

    def url(self, session_name: str | None = None) -> str:
        base = f"http://{self.host}:{self.port}"
        return f"{base}/viewer/{session_name}" if session_name else base

    # ─── HTTP handlers ──────────────────────────────────────────────

    async def _handle_index(self, request):
        from aiohttp import web
        return web.Response(text=INDEX_HTML, content_type='text/html')

    async def _handle_sessions_json(self, request):
        from aiohttp import web
        sessions = []
        for name in self.registry.list_running():
            entry = self.registry.get(name)
            if entry is None:
                continue
            try:
                url = entry.session.page.url
            except Exception:  # noqa: BLE001
                url = None
            sessions.append({
                "name": name,
                "url": url,
                "backend": entry.flags.get("backend", "patchright"),
            })
        return web.json_response({"sessions": sessions})

    async def _handle_viewer(self, request):
        from aiohttp import web
        name = request.match_info['name']
        if not self.registry.has(name):
            return web.Response(text=f"no session {name!r}", status=404)
        return web.Response(text=_render_viewer(name, self.takeover),
                            content_type='text/html')

    async def _handle_ws(self, request):
        from aiohttp import web, WSMsgType
        name = request.match_info['name']
        if not self.registry.has(name):
            return web.Response(text="no session", status=404)

        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self._clients[name].add(ws)

        # Send hello with viewport dimensions for client-side coord scaling
        try:
            entry = self.registry.get(name)
            page = entry.session.page
            vp = page.viewport_size or {}
            # Fallback: query window.innerWidth/Height if viewport not set
            if not vp:
                try:
                    vp = await page.evaluate(
                        "() => ({width: window.innerWidth, height: window.innerHeight})"
                    )
                except Exception:  # noqa: BLE001
                    vp = {"width": 1280, "height": 720}
            await ws.send_json({
                "type": "hello",
                "viewport_width": vp.get("width", 1280),
                "viewport_height": vp.get("height", 720),
                "takeover": self.takeover,
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("liveview hello failed for %s: %s", name, exc)

        # Start frame loop if not already running for this session
        if name not in self._frame_tasks or self._frame_tasks[name].done():
            self._frame_tasks[name] = asyncio.create_task(self._frame_loop(name))

        # Loop reading takeover events from the client
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if self.takeover:
                        await self._handle_takeover(name, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    log.warning("ws error for %s: %s", name, ws.exception())
                    break
        finally:
            self._clients[name].discard(ws)
            # If no more viewers, stop the frame loop to save CPU
            if not self._clients[name]:
                task = self._frame_tasks.pop(name, None)
                if task is not None and not task.done():
                    task.cancel()
        return ws

    # ─── frame loop + takeover ──────────────────────────────────────

    async def _frame_loop(self, name: str) -> None:
        """Capture screenshots at `self.fps` and broadcast to all WS clients
        for this session. Does NOT hold the session lock — Playwright
        `page.screenshot()` is safe to call concurrently with other CDP ops.
        """
        interval = 1.0 / max(1, self.fps)
        log.debug("frame loop started for %s @ %d fps", name, self.fps)
        try:
            while self._running:
                entry = self.registry.get(name)
                if entry is None:
                    log.debug("session %s gone, stopping frame loop", name)
                    return
                if not self._clients.get(name):
                    return  # no viewers
                try:
                    png = await entry.session.page.screenshot(
                        type='jpeg', quality=self.jpeg_quality,
                    )
                except Exception as exc:  # noqa: BLE001
                    # Page might be navigating or closing — skip this frame
                    log.debug("screenshot failed for %s: %s", name, exc)
                    await asyncio.sleep(interval)
                    continue
                # Broadcast (snapshot the client set; clients may disconnect mid-iter)
                for ws in list(self._clients.get(name, ())):
                    if ws.closed:
                        self._clients[name].discard(ws)
                        continue
                    try:
                        await ws.send_bytes(png)
                    except Exception:  # noqa: BLE001
                        self._clients[name].discard(ws)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def _handle_takeover(self, name: str, data: str) -> None:
        """Forward a client takeover event to the session's page. Acquires the
        session lock to serialize with regular handler calls."""
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            return
        entry = self.registry.get(name)
        if entry is None:
            return
        async with entry.lock:
            page = entry.session.page
            try:
                etype = ev.get('type')
                if etype == 'click':
                    await page.mouse.click(
                        float(ev['x']), float(ev['y']),
                        button=ev.get('button', 'left'),
                    )
                elif etype == 'type':
                    await page.keyboard.type(ev.get('text', ''))
                elif etype == 'key':
                    code = ev.get('code', '')
                    # Compose modifiers (Playwright uses '+' separator)
                    parts = []
                    if ev.get('ctrl'): parts.append('Control')
                    if ev.get('shift'): parts.append('Shift')
                    if ev.get('alt'): parts.append('Alt')
                    if ev.get('meta'): parts.append('Meta')
                    parts.append(code if code else 'Unidentified')
                    await page.keyboard.press('+'.join(parts))
                elif etype == 'scroll':
                    await page.mouse.wheel(
                        float(ev.get('dx', 0)), float(ev.get('dy', 0)),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("takeover %s failed: %s", etype, exc)
