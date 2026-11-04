"""UA client hints survive the headless `--user-agent` override.

Brave ships `UACHOverrideBlank` on, so the de-Headless UA flag used to blank
every client hint (empty `sec-ch-ua`, brands []). The launch now disables that
feature. Chrome keeps only the last `--disable-features`, so it joins whichever
switch is in effect and every other feature keeps its previous state. See the
module comment in daemon/browser.py.
"""
from __future__ import annotations

import glob
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibatchium.client import call
from vibatchium.daemon import browser as B


# ─── add_disabled_feature ───────────────────────────────────────────────


def _switches(args):
    return [a for a in args if a.startswith("--disable-features=")]


def _effective(args):
    """The feature list Chrome honours: Playwright's switch comes first on the
    real command line, so with none of ours it is Playwright's list."""
    sw = _switches(args)
    if not sw:
        return list(B.PLAYWRIGHT_DISABLED_FEATURES)
    return sw[-1].split("=", 1)[1].split(",")


def test_add_without_our_switch_keeps_playwrights_list_in_effect():
    out = B.add_disabled_feature(["--a"], "UACHOverrideBlank")
    assert out[0] == "--a" and len(_switches(out)) == 1
    assert _effective(out) == [*B.PLAYWRIGHT_DISABLED_FEATURES, "UACHOverrideBlank"]


def test_add_joins_the_last_existing_switch_and_adds_nothing_else():
    args = ["--disable-features=A", "--b", "--disable-features=WebRtcHideLocalIpsWithMdns"]
    out = B.add_disabled_feature(args, "UACHOverrideBlank")
    assert out[:2] == args[:2]
    assert out[2] == "--disable-features=WebRtcHideLocalIpsWithMdns,UACHOverrideBlank"
    assert B.add_disabled_feature(out, "UACHOverrideBlank") == out  # idempotent


def test_playwright_list_matches_the_installed_driver():
    """Tripwire: our copy must match what patchright actually passes, or the
    merged switch would re-enable a feature Playwright disables."""
    import patchright
    lib = Path(patchright.__file__).parent / "driver" / "package" / "lib"
    src = None
    for f in [lib / "coreBundle.js",
              *lib.glob("server/chromium/chromiumSwitches.js")]:
        if f.exists() and "disabledFeatures" in (txt := f.read_text(errors="replace")):
            src = txt
            break
    assert src, f"can't find Playwright's disabledFeatures under {lib}"
    m = re.search(r"disabledFeatures\s*=\s*\[(.*?)\]", src, re.S)
    assert m, "disabledFeatures array not found — driver layout changed"
    body = re.sub(r"//[^\n]*", "", m.group(1))
    assert tuple(re.findall(r'"([^"]+)"', body)) == B.PLAYWRIGHT_DISABLED_FEATURES


# ─── launch args ────────────────────────────────────────────────────────


class _Ctx:
    def __init__(self):
        self.pages = [SimpleNamespace(on=lambda *a, **k: None)]
        self.browser = SimpleNamespace(version="148.0.0.0")


async def _launch_args(monkeypatch, tmp_path, ua, **kw):
    sink = {}

    class _Chromium:
        async def launch_persistent_context(self, **k):
            sink.update(k)
            return _Ctx()

    async def _ua(pw, executable_path=None):
        return ua

    monkeypatch.setattr(B, "coherent_headless_ua", _ua)
    monkeypatch.setattr(B, "_wire_page_tracking", lambda s: None)
    await B.launch_session(tmp_path, headless=True,
                           pw=SimpleNamespace(chromium=_Chromium()), **kw)
    return sink["args"] or []


def _webrtc_feats():
    from vibatchium.proxy import webrtc_leak_guard_args
    return _effective(webrtc_leak_guard_args())


PROXY = {"server": "http://127.0.0.1:9"}
UA = "Mozilla/5.0 Chrome/148.0"


async def test_override_launch_keeps_playwrights_list_plus_uach(monkeypatch, tmp_path):
    args = await _launch_args(monkeypatch, tmp_path, UA)
    assert _effective(args) == [*B.PLAYWRIGHT_DISABLED_FEATURES, "UACHOverrideBlank"]
    assert f"--user-agent={UA}" in args


async def test_proxied_override_launch_keeps_its_old_posture_plus_uach(monkeypatch, tmp_path):
    # Proxied launches have always run without Playwright's list (the WebRTC
    # guard's switch replaces it). The UA-CH fix must not change that.
    args = await _launch_args(monkeypatch, tmp_path, UA, proxy=PROXY)
    assert _effective(args) == [*_webrtc_feats(), "UACHOverrideBlank"]


async def test_no_override_launches_are_unchanged(monkeypatch, tmp_path):
    assert _switches(await _launch_args(monkeypatch, tmp_path, None)) == []
    args = await _launch_args(monkeypatch, tmp_path, None, proxy=PROXY)
    assert _effective(args) == _webrtc_feats()


# ─── live: every launchable binary keeps its brands ─────────────────────


class _Echo(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        _Echo.seen.append({**{k.lower(): v for k, v in self.headers.items()},
                           ":path": self.path})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body>" + b"client hints probe " * 10 + b"</body></html>")

    def log_message(self, *a):
        pass


def _binaries() -> list[str]:
    home = Path.home()
    builds = glob.glob(str(home / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    out = sorted(builds, key=lambda p: int(re.search(r"chromium-(\d+)", p).group(1)),
                 reverse=True)[:1]
    out += ["/opt/google/chrome/chrome", "/opt/brave.com/brave/brave",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]
    return [p for p in out if os.access(p, os.X_OK)]


def _cleanup(name):
    for verb in ("session_close", "session_delete"):
        try:
            call(verb, {"name": name})
        except Exception:  # noqa: BLE001
            pass


@pytest.mark.parametrize("proxied", [False, True], ids=["direct", "proxied"])
@pytest.mark.parametrize("binary", _binaries() or [None])
def test_live_headless_override_keeps_brands(binary, proxied):
    """`proxied` adds the WebRTC guard's switch after Playwright's. The echo
    server doubles as the proxy: a plain-http proxy request is a GET with an
    absolute URL, which it answers the same way."""
    if binary is None:
        pytest.skip("no launchable Chromium executable on this host")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"  # loopback = secure context
    name = "uach_live"
    _cleanup(name)
    try:
        call("session_new", {"name": name, "prewarm": False})
        if proxied:
            call("proxy_set", {"url": url.rstrip("/")}, session=name)
        try:
            call("start", {"headless": True, "browser_binary": binary}, session=name)
        except Exception as exc:  # noqa: BLE001
            if "sandbox" in str(exc):
                pytest.skip(f"{binary} can't sandbox here: {str(exc)[:120]}")
            raise
        _Echo.seen.clear()
        call("go", {"url": url}, session=name)
        r = call("eval", {"expr": "JSON.stringify({ua: navigator.userAgent, "
                                  "b: navigator.userAgentData.brands, "
                                  "p: navigator.userAgentData.platform})"},
                 session=name)
        js = json.loads(r.get("value") if isinstance(r, dict) else r)
        assert "HeadlessChrome" not in js["ua"], js
        assert js["b"], f"{binary}: brands empty under the UA override: {js}"
        assert js["p"], js
        if "brave" in binary.lower():
            assert "Brave" in [b["brand"] for b in js["b"]], js
        hdr = _Echo.seen[0]
        # A proxied request names the absolute URL; a direct one only the path.
        assert hdr[":path"].startswith("http://") == proxied, hdr[":path"]
        assert hdr.get("sec-ch-ua"), f"{binary}: empty sec-ch-ua: {hdr}"
        assert hdr.get("sec-ch-ua-platform") not in (None, '""'), hdr
    finally:
        _cleanup(name)
        srv.shutdown()
        srv.server_close()
