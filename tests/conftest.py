"""Pytest fixtures for patchium tests.

We start a fresh daemon per session, serve local HTML fixtures via http.server
on a random port, and reset to a known page between tests so each test starts
from a clean state.
"""
from __future__ import annotations

import http.server
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from patchium.client import call, daemon_is_running, spawn_daemon


FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _daemon_lifecycle():
    """Spawn a fresh daemon for the whole test session; kill it at the end."""
    # ensure no prior daemon is around
    if daemon_is_running():
        try:
            call("shutdown")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    spawn_daemon(wait=10)
    call("start", {"profile": "/tmp/patchium-test-profile", "headless": True})
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
