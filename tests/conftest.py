"""Pytest fixtures for vibatchium tests.

We start a fresh daemon per session, serve local HTML fixtures via http.server
on a random port, and reset to a known page between tests so each test starts
from a clean state.
"""
from __future__ import annotations

import http.server
import os
import socketserver
import tempfile
import threading
import time
from pathlib import Path

# ── Socket isolation: the whole pytest session runs against its own runtime
# dir. Without this, _daemon_lifecycle's "ensure no prior daemon" shutdown
# below goes to the USER'S default socket and kills a live shared daemon —
# on the shared box that tore down the running bot sessions (2026-07-15
# incident). paths.SOCK_PATH is frozen at import, so this MUST run before any
# vibatchium import. XDG_STATE_HOME is isolated too so test-daemon churn
# doesn't pollute the real persistent daemon.log. Kept short: AF_UNIX paths
# cap at ~107 bytes.
#
# The isolated runtime dir SYMLINKS the real one's entries (minus vibatchium/):
# a bare dir changes EGL/Wayland device discovery — on a dual-GPU host the
# un-pinned headless GPU session flips Intel→NVIDIA and the de-twin test
# breaks. Linking wayland/dbus/etc. back preserves graphics behavior while the
# vibatchium socket namespace stays private.
_iso_rt = tempfile.mkdtemp(prefix="vbtest-rt-")
_real_rt = os.environ.get("XDG_RUNTIME_DIR")
if _real_rt and os.path.isdir(_real_rt):
    for _ent in os.listdir(_real_rt):
        if _ent != "vibatchium":
            try:
                os.symlink(os.path.join(_real_rt, _ent),
                           os.path.join(_iso_rt, _ent))
            except OSError:
                pass
os.environ["XDG_RUNTIME_DIR"] = _iso_rt
os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="vbtest-st-")

# Vault isolation MUST run BEFORE any vibatchium import (same as SOCK_PATH above):
# secrets.py freezes VAULT_PATH from this env at import time, and the fixed test key
# re-keys the WHOLE vault file on every save. If VAULT_PATH froze to the real
# ~/.config/vibatchium/secrets.enc (because the env was set later, in a fixture body),
# an in-process test that imports secrets and calls set_secret would re-encrypt the
# user's real vault under the test key and make every existing entry permanently
# undecryptable. Setting it here — before pytest collects any test module — makes that
# impossible in the test PROCESS, not just the daemon subprocess.
import base64 as _b64
os.environ["VIBATCHIUM_SECRETS_KEY"] = _b64.b64encode(b"\x02" * 32).decode()
os.environ["VIBATCHIUM_VAULT_PATH"] = str(
    Path(tempfile.mkdtemp(prefix="vbtest-vault-")) / "secrets.enc"
)

import pytest

from vibatchium.client import call, daemon_is_running, spawn_daemon


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _daemon_lifecycle():
    """Spawn a fresh daemon for the whole test session; kill it at the end."""
    # Wave 6.1b: force VIBATCHIUM_WARM=off in tests so pre-warm doesn't spawn
    # extra Chromes and confuse process-counting assertions. Tests that need
    # warm behavior set the env explicitly inside the test.
    os.environ["VIBATCHIUM_WARM"] = "off"
    # The fixed test vault key + disposable vault path are set at MODULE TOP (before
    # any vibatchium import) so secrets.VAULT_PATH can't freeze to the real vault in
    # the test process. See the block above the `import pytest` line.
    # ensure no prior daemon is around
    if daemon_is_running():
        try:
            call("shutdown")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    spawn_daemon(wait=10)
    call("start", {"profile": "/tmp/vibatchium-test-profile", "headless": True})
    yield
    try:
        call("stop")
    except Exception:  # noqa: BLE001
        pass
    try:
        call("shutdown")
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(scope="session")
def local_server():
    """Serve tests/fixtures/ over http on a random port for the duration of the session."""
    FIXTURES_DIR.mkdir(exist_ok=True)
    # bind to 127.0.0.1 with port=0 to grab a free port
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(FIXTURES_DIR), **kw
    )
    # ThreadingTCPServer so multiple Chromes (multi-session tests) can fetch
    # fixtures concurrently — the single-threaded base class queues requests,
    # which manifests as Page.goto timeouts when wave5_sessions creates several
    # sessions and they all hit the local server at once.
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    yield base
    server.shutdown()


@pytest.fixture(autouse=True)
def _reset_between_tests():
    """Soft reset between tests. We deliberately don't navigate to about:blank
    because go_back across about:blank can hang on Chrome; each test re-navigates
    to its own fixture and the only cross-test state is page url, which the
    next `go` overrides."""
    yield
