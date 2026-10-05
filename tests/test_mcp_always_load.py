"""Core MCP tools carry `_meta["anthropic/alwaysLoad"]` so Claude Code keeps
them loaded while the rest of the server is deferred behind tool search
(code.claude.com/docs/en/mcp, "Exempt a server from deferral").
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from vibatchium import mcp_server
from vibatchium.caps import CAP_PROFILES

KEY = "anthropic/alwaysLoad"
REPO = Path(__file__).resolve().parent.parent


def _list(caps, monkeypatch):
    monkeypatch.setattr(mcp_server, "_ACTIVE_CAPS", caps)
    monkeypatch.setattr(mcp_server, "_plugin_tools", lambda: [])
    return asyncio.run(mcp_server.list_tools())


def test_core_set_is_small():
    """Each always-loaded schema costs context on every turn."""
    assert 1 <= len(mcp_server._ALWAYS_LOAD) <= 6
    assert set(mcp_server._ALWAYS_LOAD) == {"explore", "go", "extract", "screenshot", "act"}


def test_core_set_exists_in_the_lean_default(monkeypatch):
    """A name that isn't exposed by default can't be always-loaded by default."""
    names = {t.name for t in _list(CAP_PROFILES["lean"], monkeypatch)}
    assert set(mcp_server._ALWAYS_LOAD) <= names


def test_only_core_tools_are_marked(monkeypatch):
    tools = _list(None, monkeypatch)  # full surface
    marked = {t.name for t in tools if (t.meta or {}).get(KEY) is True}
    assert marked == set(mcp_server._ALWAYS_LOAD)


def test_serializes_as_underscore_meta(monkeypatch):
    """The SDK field is `meta` with wire alias `_meta`; passing `meta=` to
    types.Tool is silently dropped, so pin the wire shape."""
    tools = {t.name: t for t in _list(CAP_PROFILES["lean"], monkeypatch)}
    wire = tools["explore"].model_dump(by_alias=True, exclude_none=True, mode="json")
    assert wire["_meta"] == {KEY: True}
    assert "meta" not in wire
    plain = tools["click"].model_dump(by_alias=True, exclude_none=True, mode="json")
    assert "_meta" not in plain


def test_meta_dict_is_not_shared():
    a, b = mcp_server._meta_for("go"), mcp_server._meta_for("explore")
    a["x"] = 1
    assert "x" not in b and "x" not in mcp_server._ALWAYS_LOAD_META


@pytest.mark.timeout(60)
def test_stdio_handshake_carries_meta(tmp_path):
    """End to end over the real stdio transport: initialize ->
    notifications/initialized -> tools/list. A private XDG_RUNTIME_DIR keeps
    the plugin-verb lookup away from any live daemon."""
    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path),
           "PYTHONPATH": str(REPO) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    p = subprocess.Popen(
        [sys.executable, "-c", "from vibatchium.cli import main; main()", "mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, text=True)
    try:
        def send(msg):
            p.stdin.write(json.dumps(msg) + "\n")
            p.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})
        assert json.loads(p.stdout.readline())["result"]["serverInfo"]["name"] == "vibatchium"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = json.loads(p.stdout.readline())["result"]["tools"]
    finally:
        p.stdin.close()
        p.terminate()
        p.wait(timeout=10)
    marked = {t["name"] for t in tools if t.get("_meta", {}).get(KEY) is True}
    assert marked == set(mcp_server._ALWAYS_LOAD)
