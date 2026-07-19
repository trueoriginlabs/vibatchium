"""Live-view auth — the cross-site WebSocket hijacking (CSWSH) gate.

Loopback binding is not authentication. WebSockets are exempt from the
same-origin policy and from CORS preflight, so before this gate existed ANY
page the operator happened to load in their ordinary browser could open
`ws://127.0.0.1:<port>/ws/<session>` — session names are guessable — and read
frames from a session logged into their real accounts, or drive clicks and
keystrokes into it whenever the daemon had been started with takeover.

These tests are the regression guard. Each one FAILS against the pre-fix code:
there, an unauthenticated connect returned frames instead of 403.

The tests below deliberately avoid the browser wherever possible — only the
frame-flow test needs a real page. That keeps this file cheap to run on a
memory-tight box.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request

import pytest

from vibatchium.client import call, DaemonError

PORT = 9226  # distinct from test_wave6_liveview's 9224 and any real daemon


def _start(extra=None):
    args = {"port": PORT, "fps": 10, "host": "127.0.0.1"}
    if extra:
        args.update(extra)
    return call("liveview_start", args)


def _stop():
    try:
        call("liveview_stop")
    except DaemonError:
        pass


def _tok(res, key="url"):
    from urllib.parse import urlparse, parse_qs
    return parse_qs(urlparse(res[key]).query)["token"][0]


@pytest.fixture(autouse=True)
def _clean_server():
    _stop()
    yield
    _stop()


def _get(path):
    """GET returning (status, body). Non-2xx comes back as a status, not a raise."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=2) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# ─── the token is minted and surfaced ────────────────────────────────────


def test_start_returns_tokened_url():
    res = _start()
    assert "token=" in res["url"], "viewer link must carry the credential"
    # Watch-only server mints no control token, so no control link.
    assert "control_url" not in res


def test_takeover_start_returns_a_separate_control_url():
    res = _start({"takeover": True})
    assert "control_url" in res, "takeover server must expose a control link"
    assert _tok(res) != _tok(res, "control_url"), \
        "control token must differ from the watch token, else watch-only is a lie"


def test_tokens_are_fresh_per_server():
    a = _start()
    _stop()
    b = _start()
    assert _tok(a) != _tok(b), "tokens must not be stable across restarts"


# ─── HTTP endpoints reject anonymous callers ─────────────────────────────


def test_index_requires_token():
    _start()
    assert _get("/")[0] == 403


def test_sessions_json_does_not_enumerate_sessions_anonymously():
    """Session names are the guessable half of a /ws/<name> URL."""
    _start()
    status, body = _get("/sessions.json")
    assert status == 403
    assert b"default" not in body


def test_viewer_requires_token():
    _start()
    assert _get("/viewer/default")[0] == 403


def test_bad_token_is_rejected():
    _start()
    assert _get("/sessions.json?token=not-the-token")[0] == 403


def test_valid_token_is_accepted():
    res = _start()
    assert _get(f"/sessions.json?token={_tok(res)}")[0] == 200


# ─── the WebSocket itself ────────────────────────────────────────────────


def _ws_connect(query="", origin=None):
    """Attempt a WS upgrade. Returns (ok, status_or_none)."""
    from aiohttp import ClientSession

    async def go():
        headers = {"Origin": origin} if origin else None
        url = f"http://127.0.0.1:{PORT}/ws/default{query}"
        async with ClientSession() as cs:
            try:
                async with cs.ws_connect(url, headers=headers, timeout=3):
                    return True, None
            except Exception as exc:  # noqa: BLE001
                return False, getattr(exc, "status", None)

    return asyncio.run(go())


def test_ws_without_token_is_rejected():
    """THE hole: this returned a live frame stream before the fix."""
    _start()
    ok, status = _ws_connect()
    assert not ok, "unauthenticated WebSocket must not connect"
    assert status == 403


def test_ws_with_bad_token_is_rejected():
    _start()
    ok, status = _ws_connect("?token=wrong")
    assert not ok
    assert status == 403


def test_ws_rejects_foreign_origin_even_with_a_valid_token():
    """The drive-by shape: a malicious page that somehow learned the token
    still cannot open the socket from its own origin."""
    res = _start()
    ok, status = _ws_connect(f"?token={_tok(res)}", origin="https://evil.example")
    assert not ok, "foreign-Origin WebSocket must be refused"
    assert status == 403


def test_ws_accepts_own_origin():
    res = _start()
    ok, _ = _ws_connect(f"?token={_tok(res)}", origin=f"http://127.0.0.1:{PORT}")
    assert ok, "the viewer page's own origin must still work"


def test_ws_with_valid_token_and_no_origin_connects():
    """Non-browser clients (our CLI, scripts) send no Origin and must work —
    they cannot be a CSWSH vector, which requires a victim's browser."""
    res = _start()
    ok, _ = _ws_connect(f"?token={_tok(res)}")
    assert ok


# ─── takeover is a separate grant ────────────────────────────────────────


def _hello_takeover(token):
    """Connect and read the server's hello frame; return its takeover flag."""
    from aiohttp import ClientSession, WSMsgType

    async def go():
        url = f"http://127.0.0.1:{PORT}/ws/default?token={token}"
        async with ClientSession() as cs:
            async with cs.ws_connect(url, timeout=3) as ws:
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                    if msg.type == WSMsgType.TEXT:
                        payload = json.loads(msg.data)
                        if payload.get("type") == "hello":
                            return payload.get("takeover")
        return None

    return asyncio.run(go())


def test_watch_token_gets_no_takeover_on_a_takeover_server():
    """The grant split: the server is in takeover mode, but a watch-token
    connection is told it has no input rights."""
    res = _start({"takeover": True})
    assert _hello_takeover(_tok(res)) is False


def test_control_token_gets_takeover():
    res = _start({"takeover": True})
    assert _hello_takeover(_tok(res, "control_url")) is True


def test_watch_token_input_events_are_ignored(local_server):
    """A watch-only connection can send takeover JSON all it likes; the page
    must not move. Proven by URL, not by trusting the hello flag."""
    from aiohttp import ClientSession

    call("go", {"url": f"{local_server}/simple.html"})
    res = _start({"takeover": True})
    before = call("url")["url"]

    async def send_clicks(token):
        url = f"http://127.0.0.1:{PORT}/ws/default?token={token}"
        async with ClientSession() as cs:
            async with cs.ws_connect(url, timeout=3) as ws:
                for _ in range(5):
                    await ws.send_json({"type": "click", "x": 20, "y": 20,
                                        "button": "left"})
                await asyncio.sleep(0.5)

    asyncio.run(send_clicks(_tok(res)))
    assert call("url")["url"] == before, \
        "watch-only connection must not be able to drive the page"
