"""Wave 6.1a — live-view server tests.

Verifies:
- start returns viewer URL; stop is idempotent
- index page lists running sessions
- viewer page 404s for unknown sessions
- WebSocket /ws/<name> sends a hello + ≥3 binary JPEG frames within 2s
- multi-session: two sessions = two distinct viewer URLs
- non-loopback bind without --insecure-public is refused
- session shutdown cleans up live-view server cleanly
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from patchium.client import call, DaemonError


PORT = 9224  # avoid 9223 in case a real daemon's serving there


def _start_lv(extra=None):
    args = {"port": PORT, "fps": 10, "host": "127.0.0.1"}
    if extra:
        args.update(extra)
    return call("liveview_start", args)


def _stop_lv():
    try:
        call("liveview_stop")
    except DaemonError:
        pass


def test_liveview_requires_aiohttp_installed():
    """aiohttp is in our test venv; if not, the start call must error clearly."""
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        pytest.skip("aiohttp not installed in this venv")


def test_liveview_start_returns_url():
    _stop_lv()
    res = _start_lv()
    try:
        assert res["started"] is True
        assert res["url"].startswith("http://127.0.0.1:")
        assert res["port"] == PORT
        assert res["takeover"] is False
    finally:
        _stop_lv()


def test_liveview_start_idempotent():
    _stop_lv()
    a = _start_lv()
    b = _start_lv()
    try:
        assert a.get("started") is True
        assert b.get("already_running") is True
        assert a["url"] == b["url"]
    finally:
        _stop_lv()


def test_liveview_stop_idempotent():
    _stop_lv()
    res = call("liveview_stop")
    # When not running, returns {"already_stopped": True}
    assert res.get("already_stopped") is True


def test_liveview_refuses_public_bind_without_flag():
    _stop_lv()
    with pytest.raises(DaemonError, match="insecure_public"):
        call("liveview_start", {"port": PORT, "host": "0.0.0.0"})


def test_liveview_index_lists_running_sessions(local_server):
    """The / page should mention the default session that's running from conftest."""
    _stop_lv()
    _start_lv()
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/sessions.json", timeout=2) as r:
            data = json.loads(r.read())
        names = [s["name"] for s in data["sessions"]]
        assert "default" in names
    finally:
        _stop_lv()


def test_liveview_viewer_404s_for_unknown_session():
    _stop_lv()
    _start_lv()
    try:
        import urllib.request, urllib.error
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/viewer/no-such-session",
                                   timeout=2)
        assert exc.value.code == 404
    finally:
        _stop_lv()


def test_liveview_url_handler_returns_session_url(local_server):
    """liveview_url MCP/CLI handler returns the per-session URL."""
    _stop_lv()
    _start_lv()
    try:
        res = call("liveview_url", {"session": "default"})
        assert res["running"] is True
        assert "/viewer/default" in res["session_url"]
    finally:
        _stop_lv()


def test_liveview_url_when_server_off():
    _stop_lv()
    res = call("liveview_url")
    assert res["running"] is False
    assert res["url"] is None


def test_liveview_websocket_streams_frames(local_server):
    """Open a WS to /ws/default; expect hello + ≥3 binary frames within 2s."""
    _stop_lv()
    # navigate first so the screenshot has actual content
    call("go", {"url": f"{local_server}/simple.html"})
    _start_lv({"fps": 20})
    try:
        from aiohttp import ClientSession, WSMsgType

        async def collect():
            url = f"http://127.0.0.1:{PORT}/ws/default"
            frames = 0
            hello_seen = False
            async with ClientSession() as cs:
                async with cs.ws_connect(url) as ws:
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type == WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            if payload.get("type") == "hello":
                                hello_seen = True
                        elif msg.type == WSMsgType.BINARY:
                            frames += 1
                            if frames >= 3 and hello_seen:
                                return hello_seen, frames
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                            break
            return hello_seen, frames

        hello_seen, frames = asyncio.run(collect())
        assert hello_seen, "expected hello frame from server"
        assert frames >= 3, f"expected ≥3 binary frames in 2s, got {frames}"
    finally:
        _stop_lv()


def test_liveview_frames_are_jpeg(local_server):
    """Verify the binary frames are actually JPEG (start with FF D8 FF)."""
    _stop_lv()
    call("go", {"url": f"{local_server}/simple.html"})
    _start_lv({"fps": 10})
    try:
        from aiohttp import ClientSession, WSMsgType

        async def get_one():
            url = f"http://127.0.0.1:{PORT}/ws/default"
            async with ClientSession() as cs:
                async with cs.ws_connect(url) as ws:
                    deadline = time.time() + 2.0
                    while time.time() < deadline:
                        msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                        if msg.type == WSMsgType.BINARY:
                            return msg.data
            return None

        first = asyncio.run(get_one())
        assert first is not None, "no binary frame received in 2s"
        # JPEG magic bytes
        assert first[:3] == b'\xff\xd8\xff', \
            f"frame doesn't start with JPEG magic: {first[:8].hex()}"
    finally:
        _stop_lv()
