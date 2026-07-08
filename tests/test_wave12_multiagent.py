"""0.12.0 — multi-agent honesty: front door, concurrency fixes, sessionless fetch,
resource floor, uv-aware packaging. Pure/unit tests (no daemon, no Chrome) plus a
couple of async dispatch-routing tests on a fresh in-process Daemon.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from vibatchium import sdk
from vibatchium import cli
from vibatchium.daemon import registry as _registry
from vibatchium.daemon.server import Daemon


# ─── dispatch routing: sessionless fetch fallback (G4) ────────────────────────

def _fresh_daemon(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_PLUGINS", "0")
    return Daemon()


def test_sessionless_fallback_verbs_contains_fetch_only():
    assert Daemon.SESSIONLESS_FALLBACK_VERBS == frozenset({"fetch"})
    # fetch is NOT auto-start (no Chrome) and NOT unlocked-by-default.
    assert "fetch" not in Daemon.SESSION_AUTOSTART_VERBS
    assert "fetch" not in Daemon.UNLOCKED_VERBS


def test_dispatch_runs_fetch_with_no_session(monkeypatch):
    """With no session, dispatch must ROUTE fetch to its handler (which then does
    the sessionless decision) instead of rejecting with 'no session'."""
    d = _fresh_daemon(monkeypatch)

    async def fake_fetch(daemon, args):
        return {"sentinel": True, "url": args.get("url")}

    d._handlers["fetch"] = fake_fetch
    resp = asyncio.run(d.dispatch({"id": "1", "cmd": "fetch",
                                   "args": {"url": "http://example.com"}}))
    assert resp["ok"] is True
    assert resp["result"]["sentinel"] is True


def test_dispatch_still_rejects_session_verb_with_no_session(monkeypatch):
    """Control: a normal session verb with no session is still rejected."""
    d = _fresh_daemon(monkeypatch)
    resp = asyncio.run(d.dispatch({"id": "1", "cmd": "click",
                                   "args": {"target": "@e1"}}))
    assert resp["ok"] is False
    assert "no session" in resp["error"]


def test_fetch_sessionless_preconditions(monkeypatch):
    """The REAL fetch handler, no session: a cookie-wanting call gets actionable
    'start a session / --no-cookies' guidance BEFORE the curl_cffi dep is
    required; a --no-cookies call passes the precondition (and, absent curl_cffi,
    surfaces the dep error — proving it routed past the no-session gate)."""
    d = _fresh_daemon(monkeypatch)
    # 8.8.8.8 is a public IP literal → SSRF guard passes with no DNS/network.
    r1 = asyncio.run(d.dispatch({"id": "1", "cmd": "fetch",
                                 "args": {"url": "http://8.8.8.8/x"}}))
    assert r1["ok"] is False
    assert "session" in r1["error"] and "--no-cookies" in r1["error"]

    try:
        import curl_cffi  # noqa: F401
        has_curl = True
    except ImportError:
        has_curl = False
    if not has_curl:
        r2 = asyncio.run(d.dispatch({"id": "2", "cmd": "fetch",
                                     "args": {"url": "http://8.8.8.8/x",
                                              "cookies": False}}))
        assert r2["ok"] is False
        # Routed past the no-session gate into the handler; dep is the blocker now.
        assert "curl_cffi" in r2["error"]
        assert "no session" not in r2["error"]


# ─── go auto-start now runs under the registry mutate_lock (A1 / go-race) ─────

def test_go_autostart_takes_mutate_lock_and_double_checks(monkeypatch):
    d = _fresh_daemon(monkeypatch)
    from vibatchium.daemon import handlers as H

    calls = {"start": 0, "lock_held": []}

    async def fake_start(daemon, args):
        calls["start"] += 1
        calls["lock_held"].append(daemon.registry.mutate_lock.locked())
        # Simulate create() inserting an entry so the double-check sees it.
        import types
        daemon.registry._entries["default"] = types.SimpleNamespace(
            touch=lambda: None)

    class _Stop(Exception):
        pass

    def boom(daemon):
        raise _Stop()

    d._handlers["start"] = fake_start
    monkeypatch.setattr(H, "_need_session", boom)

    async def one():
        try:
            await d._handlers["go"](d, {"url": "http://example.com"})
        except _Stop:
            pass

    async def run():
        await asyncio.gather(one(), one())

    asyncio.run(run())
    # Double-checked locking → start ran exactly once even with two go-firsts.
    assert calls["start"] == 1
    # …and it ran while holding the registry mutate_lock.
    assert calls["lock_held"] == [True]


# ─── memory admission floor (E1) ──────────────────────────────────────────────

def test_session_ram_floor_env(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_SESSION_RAM_FLOOR_MB", raising=False)
    assert _registry.get_session_ram_floor_mb() == 0          # default off
    monkeypatch.setenv("VIBATCHIUM_SESSION_RAM_FLOOR_MB", "512")
    assert _registry.get_session_ram_floor_mb() == 512
    monkeypatch.setenv("VIBATCHIUM_SESSION_RAM_FLOOR_MB", "garbage")
    assert _registry.get_session_ram_floor_mb() == 0          # bad value → off


def test_ram_floor_refuses_cold_launch(monkeypatch):
    """When the floor is set and memory is below it, a cold create() raises
    SessionLimitError (so callers that degrade on a full cap handle it)."""
    reg = _registry.SessionRegistry()
    monkeypatch.setenv("VIBATCHIUM_SESSION_RAM_FLOOR_MB", "1000")
    monkeypatch.setattr(_registry, "_mem_available_mb", lambda: 200)

    async def go():
        with pytest.raises(_registry.SessionLimitError) as ei:
            await reg.create("probe", headless=True)
        assert "memory admission floor" in str(ei.value)

    asyncio.run(go())


def test_ram_floor_none_available_never_blocks(monkeypatch):
    """Unreadable /proc (None) must never block — 'can't tell' is not 'too low'.
    We stop just before the real Chrome launch by faking _launch_for."""
    reg = _registry.SessionRegistry()
    monkeypatch.setenv("VIBATCHIUM_SESSION_RAM_FLOOR_MB", "1000")
    monkeypatch.setattr(_registry, "_mem_available_mb", lambda: None)

    launched = {"n": 0}

    async def fake_launch(self, name, **kw):
        launched["n"] += 1
        raise RuntimeError("stop-after-floor-check")

    monkeypatch.setattr(_registry.SessionRegistry, "_launch_for", fake_launch)

    async def go():
        with pytest.raises(RuntimeError) as ei:
            await reg.create("probe", headless=True)
        assert "stop-after-floor-check" in str(ei.value)   # floor did NOT block

    asyncio.run(go())
    assert launched["n"] == 1


# ─── isolated-env builder + detached registry + reap (B / D) ──────────────────

def test_build_isolated_env_forces_isolation():
    env = sdk.build_isolated_env(
        "/run/priv", "/home/priv",
        base_env={"XDG_STATE_HOME": "/shared/state", "FOO": "bar",
                  "PLAYWRIGHT_BROWSERS_PATH": "/pb"},
        extra_env={"HOME": "/evil"},          # forced HOME must override this
        max_sessions=3)
    assert env["XDG_RUNTIME_DIR"] == "/run/priv"
    assert env["HOME"] == "/home/priv"        # forced wins over extra_env
    assert "XDG_STATE_HOME" not in env        # dropped so HOME governs
    assert env["FOO"] == "bar"                # unrelated vars pass through
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/pb"   # honored (Chrome findable)
    assert env["VIBATCHIUM_MAX_SESSIONS"] == "3"
    assert env["VIBATCHIUM_WARM"] == "off"


def test_isolated_registry_reap(monkeypatch, tmp_path):
    reg_file = tmp_path / "isolated-daemons.json"
    monkeypatch.setattr(sdk, "isolated_registry_path", lambda: reg_file)

    live_home = tmp_path / "live-home"; live_home.mkdir()
    live_rt = tmp_path / "live-rt"; live_rt.mkdir()
    orphan_home = tmp_path / "orphan-home"; orphan_home.mkdir()
    orphan_rt = tmp_path / "orphan-rt"; orphan_rt.mkdir()

    sdk.register_isolated_daemon({
        "sock_path": "/run/live.sock", "home": str(live_home),
        "runtime_dir": str(live_rt), "owns_home": True, "owns_runtime": True})
    sdk.register_isolated_daemon({
        "sock_path": "/run/orphan.sock", "home": str(orphan_home),
        "runtime_dir": str(orphan_rt), "owns_home": True, "owns_runtime": True})

    # live.sock answers; orphan.sock does not.
    monkeypatch.setattr(
        sdk, "_daemon_socket_alive",
        lambda sp: str(sp) == "/run/live.sock")

    rep = sdk.reap_isolated_daemons(kill_live=False)

    assert [e["sock_path"] for e in rep["reaped"]] == ["/run/orphan.sock"]
    assert [e["sock_path"] for e in rep["kept"]] == ["/run/live.sock"]
    # Orphan's dirs removed; live daemon's dirs left intact.
    assert not orphan_home.exists() and not orphan_rt.exists()
    assert live_home.exists() and live_rt.exists()
    # Registry now holds only the live daemon.
    remaining = json.loads(reg_file.read_text())
    assert [e["sock_path"] for e in remaining] == ["/run/live.sock"]


def test_reap_all_kills_live(monkeypatch, tmp_path):
    reg_file = tmp_path / "reg.json"
    monkeypatch.setattr(sdk, "isolated_registry_path", lambda: reg_file)
    home = tmp_path / "h"; home.mkdir()
    rt = tmp_path / "r"; rt.mkdir()
    sdk.register_isolated_daemon({
        "sock_path": "/run/x.sock", "home": str(home), "runtime_dir": str(rt),
        "owns_home": True, "owns_runtime": True})
    # Alive until shutdown is RPC'd, then dead — so reap's re-poll sees it go
    # down and proceeds to reclaim its dirs.
    state = {"alive": True}
    shutdowns = []

    def _fake_alive(sp, *a, **k):
        return state["alive"]

    def _fake_call_on(sp, cmd, *a, **k):
        shutdowns.append((str(sp), cmd))
        if cmd == "shutdown":
            state["alive"] = False

    monkeypatch.setattr(sdk, "_daemon_socket_alive", _fake_alive)
    monkeypatch.setattr(sdk._client, "call_on", _fake_call_on)

    rep = sdk.reap_isolated_daemons(kill_live=True)
    assert ("/run/x.sock", "shutdown") in shutdowns
    assert [e["sock_path"] for e in rep["killed"]] == ["/run/x.sock"]
    assert not home.exists() and not rt.exists()
    assert json.loads(reg_file.read_text()) == []


# ─── uv / editable-aware packaging (G2 / G3) ──────────────────────────────────

def test_pkg_install_cmd_matches_install_model(monkeypatch):
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: False)
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)

    monkeypatch.setattr(cli, "_is_uv_venv", lambda: True)
    assert cli._pkg_install_cmd("curl_cffi").startswith("uv pip install --python ")
    assert cli._pkg_install_cmd("curl_cffi").endswith("curl_cffi")

    monkeypatch.setattr(cli, "_is_uv_venv", lambda: False)
    assert cli._pkg_install_cmd("curl_cffi") == "pip install curl_cffi"

    monkeypatch.setattr(cli, "_is_pipx_install", lambda: True)
    assert cli._pkg_install_cmd("curl_cffi") == "pipx inject vibatchium curl_cffi"


def test_update_dist_editable_is_noop(monkeypatch):
    monkeypatch.setattr(cli, "_is_editable_install", lambda: True)

    def _fail(*a, **k):
        raise AssertionError("must not run an installer on an editable tree")

    monkeypatch.setattr(cli, "_run", _fail)
    rc, note = cli._update_dist(None)
    assert rc == 0
    assert "editable" in note.lower()


def test_update_dist_uv_branch(monkeypatch):
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: False)
    monkeypatch.setattr(cli, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(cli, "_is_uv_venv", lambda: True)
    seen = {}

    class _CP:
        returncode = 0

    def _run(cmd, *, capture):
        seen["cmd"] = cmd
        return _CP()

    monkeypatch.setattr(cli, "_run", _run)
    rc, note = cli._update_dist(None)
    assert rc == 0
    assert seen["cmd"][:3] == ["uv", "pip", "install"]
    assert "uv venv detected" in note


def test_update_dist_uv_tool_branch(monkeypatch):
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: False)
    monkeypatch.setattr(cli, "_is_uv_tool_install", lambda: True)
    seen = {}

    class _CP:
        returncode = 0

    def _run(cmd, *, capture):
        seen["cmd"] = cmd
        return _CP()

    monkeypatch.setattr(cli, "_run", _run)
    rc, note = cli._update_dist(None)
    assert rc == 0
    assert seen["cmd"] == ["uv", "tool", "upgrade", "vibatchium"]
    assert "uv tool install detected" in note

    rc, note = cli._update_dist("0.6.2")
    assert rc == 0
    assert seen["cmd"] == ["uv", "tool", "install", "--force", "vibatchium==0.6.2"]


def test_update_dist_uv_missing_binary(monkeypatch):
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: False)
    monkeypatch.setattr(cli, "_is_uv_tool_install", lambda: True)

    def _run(cmd, *, capture):
        raise FileNotFoundError("uv")

    monkeypatch.setattr(cli, "_run", _run)
    rc, note = cli._update_dist(None)
    assert rc == 127
    assert "uv tool upgrade vibatchium" in note


def test_is_uv_tool_install_detects_tools_prefix(monkeypatch, tmp_path):
    prefix = tmp_path / ".local" / "share" / "uv" / "tools" / "vibatchium"
    prefix.mkdir(parents=True)
    monkeypatch.setattr(cli.sys, "prefix", str(prefix))
    assert cli._is_uv_tool_install() is True
    monkeypatch.setattr(cli.sys, "prefix", str(tmp_path / "plain-venv"))
    assert cli._is_uv_tool_install() is False


# ─── MCP instructions: concurrency guidance (C1) ──────────────────────────────

def test_mcp_instructions_have_concurrency_guidance():
    from vibatchium.mcp_server import _build_instructions
    s = _build_instructions(None)
    assert s is not None
    assert "Concurrency" in s
    assert "session" in s
    assert "--isolated" in s


# ─── research: no whole-run abort + session_new dropped (A2) ──────────────────

def _research_fake_call(behavior):
    """Build a fake `call` for research. `behavior` maps verb→callable(args)."""
    seen = {"session_new": 0, "start": 0}

    def fake_call(cmd, args=None, session=None, **kw):
        args = args or {}
        if cmd == "status":
            return {"budgets": {"persistent": {"cap": 2, "used": 0}}}
        if cmd == "verify_url":
            return {"ok": True, "latency_ms": 1}
        if cmd == "session_new":
            seen["session_new"] += 1
            return {}
        if cmd == "start":
            seen["start"] += 1
            return behavior["start"](args)
        return {}

    return fake_call, seen


def test_research_degrades_on_start_error_without_aborting(monkeypatch, tmp_path):
    """A start failure must degrade THAT thread, not crash the whole run."""
    from click.testing import CliRunner

    def _boom(args):
        raise cli.DaemonError("boom (non-capacity → fast fail)")

    fake_call, seen = _research_fake_call({"start": _boom})
    monkeypatch.setattr(cli, "call", fake_call)

    out = tmp_path / "out"
    res = CliRunner().invoke(cli.cli, [
        "research", "--target", "http://example.com",
        "--intent", "a", "--intent", "b", "--intent", "c",
        "--output-dir", str(out), "--no-verify-urls"])
    assert res.exit_code == 0, res.output
    assert (out / "index.md").exists()           # run completed despite failures
    assert seen["session_new"] == 0              # redundant session_new dropped
    assert seen["start"] >= 1                     # each thread tried to start


def test_research_happy_path_drops_session_new(monkeypatch, tmp_path):
    from click.testing import CliRunner

    fake_call, seen = _research_fake_call({"start": lambda args: {}})
    monkeypatch.setattr(cli, "call", fake_call)

    out = tmp_path / "out"
    res = CliRunner().invoke(cli.cli, [
        "research", "--target", "http://example.com",
        "--intent", "a", "--intent", "b", "--intent", "c",
        "--output-dir", str(out)])
    assert res.exit_code == 0, res.output
    assert (out / "index.md").exists()
    assert seen["session_new"] == 0
    # cap=2 < 3 intents → the recycle note is shown.
    assert "running 2 at a time" in res.output
