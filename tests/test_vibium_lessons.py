"""0.8.0 — optimizations distilled from the Vibium deep-dive.

Covers four features:
  1. console_start/stop/dump — passive console + page-error capture
  2. expect — one-call verification gate
  3. lean default MCP surface (CAP_PROFILES / `vb mcp` default)
  4. one-time Chrome auto-install on first launch (gating logic)

PURE/UNIT tests need no Chrome. LIVE tests drive the conftest daemon and clean
up every session in finally — run with an isolated HOME+XDG_RUNTIME_DIR so the
shared production daemon is never touched.
"""
from __future__ import annotations

import asyncio
import time

from vibatchium.client import call, DaemonError


def _cleanup(name):
    for verb in ("console_stop",):
        try:
            call(verb, {}, session=name)
        except DaemonError:
            pass
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        call("session_delete", {"name": name})
    except DaemonError:
        pass


# ─── 1. console + browser-log capture (LIVE) ─────────────────────────────
def test_console_default_captures_browser_log_entries(local_server):
    """The stealth-safe DEFAULT (Log domain only, no Runtime) captures
    browser-level warnings — e.g. a CSP violation — without enabling the
    Runtime detection vector."""
    name = "vbtest-console-log"
    try:
        call("start", {"headless": True, "ephemeral": True}, session=name)
        r = call("console_start", {}, session=name)            # default: Log only
        assert r["capturing"] is True and r["include_page_console"] is False
        call("go", {"url": f"{local_server}/csp.html"}, session=name)
        time.sleep(0.6)
        events = call("console_dump", {}, session=name)["events"]
        assert any(e["kind"] == "log" and "violates" in (e.get("text") or "")
                   for e in events), events
        assert call("console_stop", {}, session=name)["capturing"] is False
    finally:
        _cleanup(name)


def test_console_include_page_records_console_and_pageerror(local_server):
    """With include_page_console (Runtime on), page console.* + uncaught errors
    are captured."""
    name = "vbtest-console-page"
    try:
        call("start", {"headless": True, "ephemeral": True}, session=name)
        call("console_start", {"include_page_console": True}, session=name)
        call("go", {"url": f"{local_server}/console.html"}, session=name)
        time.sleep(0.7)  # let the setTimeout pageerror fire
        events = call("console_dump", {}, session=name)["events"]
        texts = " ".join(e.get("text", "") for e in events)
        assert "hello-log" in texts
        assert "err-msg" in texts
        assert any(e["kind"] == "pageerror" and "boom" in e["text"] for e in events), events
        errs = call("console_dump", {"errors_only": True}, session=name)["events"]
        assert errs and all(e["level"] == "error" for e in errs)
        assert any(e["kind"] == "pageerror" for e in errs)
    finally:
        _cleanup(name)


def test_console_level_filter_suppresses_logs(local_server):
    name = "vbtest-console-filter"
    try:
        call("start", {"headless": True, "ephemeral": True}, session=name)
        call("console_start", {"levels": "error", "include_page_console": True},
             session=name)
        call("go", {"url": f"{local_server}/console.html"}, session=name)
        time.sleep(0.7)
        events = call("console_dump", {}, session=name)["events"]
        texts = " ".join(e.get("text", "") for e in events)
        assert "hello-log" not in texts, "level=error must suppress console.log"
        assert "warn-msg" not in texts, "level=error must suppress console.warn"
        # the error + the uncaught pageerror still come through
        assert "err-msg" in texts or any(e["kind"] == "pageerror" for e in events)
    finally:
        _cleanup(name)


# ─── 2. expect verification gate (LIVE) ──────────────────────────────────
def test_expect_target_state_passes(local_server):
    """A supported element state (visible) on a real element passes."""
    name = "vbtest-expect-target"
    try:
        call("start", {"headless": True, "ephemeral": True}, session=name)
        call("go", {"url": f"{local_server}/simple.html"}, session=name)
        r = call("expect", {"target": "@text:Hello, Vibatchium", "state": "visible"},
                 session=name)
        assert r["passed"] is True, r["failures"]
    finally:
        _cleanup(name)


def test_console_restart_on_rerun(local_server):
    """Re-running console_start RESTARTS (no silent already_on no-op) so it can
    rebind after a tab swap or change options."""
    name = "vbtest-console-restart"
    try:
        call("start", {"headless": True, "ephemeral": True}, session=name)
        call("console_start", {}, session=name)
        r2 = call("console_start", {"levels": "error"}, session=name)
        assert r2["capturing"] is True
        assert r2.get("already_on") is None      # restart, not a no-op
        assert r2["levels"] == "error"           # new args applied
        call("console_stop", {}, session=name)
    finally:
        _cleanup(name)


def test_expect_passes_on_matching_state(local_server):
    name = "vbtest-expect-ok"
    try:
        call("start", {"headless": True, "ephemeral": True}, session=name)
        call("go", {"url": f"{local_server}/simple.html"}, session=name)
        r = call("expect", {"text_contains": "Hello, Vibatchium",
                            "url_contains": "simple"}, session=name)
        assert r["passed"] is True
        assert r["failures"] == []
        assert "screenshot_b64" not in r   # auto: no shot on success
    finally:
        _cleanup(name)


def test_expect_fails_and_screenshots_on_failure(local_server):
    name = "vbtest-expect-fail"
    try:
        call("start", {"headless": True, "ephemeral": True}, session=name)
        call("go", {"url": f"{local_server}/simple.html"}, session=name)
        r = call("expect", {"text_contains": "DEFINITELY-NOT-ON-THIS-PAGE"},
                 session=name)
        assert r["passed"] is False
        assert any(f["check"] == "text_contains" for f in r["failures"])
        assert r.get("screenshot_b64"), "auto must screenshot the failure"
        assert r.get("screenshot_reason") == "failure-evidence"
    finally:
        _cleanup(name)


def test_expect_treats_wall_as_failure(local_server):
    name = "vbtest-expect-wall"
    try:
        call("start", {"headless": True, "ephemeral": True}, session=name)
        call("go", {"url": f"{local_server}/walled.html"}, session=name)
        r = call("expect", {}, session=name)
        assert r["passed"] is False
        assert r.get("walled")
        assert any(f["check"] == "not_walled" for f in r["failures"])
        # allow_walled bypasses the wall check
        r2 = call("expect", {"allow_walled": True}, session=name)
        assert r2["passed"] is True
    finally:
        _cleanup(name)


# ─── 3. lean default MCP surface (PURE) ──────────────────────────────────
def test_cap_profiles_resolve():
    from vibatchium.caps import resolve_caps, CAP_PROFILES, LEAN_CAPS
    lean = resolve_caps("lean")
    assert lean == set(LEAN_CAPS.split(","))
    assert "lean" in CAP_PROFILES and "full" in CAP_PROFILES
    assert resolve_caps("full") is None       # full = no filter
    assert resolve_caps("all") is None
    mixed = resolve_caps("lean,network")      # profile + extra bucket
    assert "network" in mixed and "core" in mixed
    assert "devtools" not in lean             # console_* not in the lean surface


def test_lean_caps_single_source_of_truth():
    from vibatchium.caps import LEAN_CAPS as caps_val
    from vibatchium.setup_cmd import LEAN_CAPS as setup_val
    assert caps_val == setup_val              # setup re-exports caps.py's value


def test_console_in_devtools_not_lean_expect_in_lean():
    from vibatchium.caps import verb_in_caps, resolve_caps
    lean = resolve_caps("lean")
    assert verb_in_caps("console_start", lean) is False
    assert verb_in_caps("console_start", resolve_caps("devtools")) is True
    assert verb_in_caps("console_start", None) is True   # full surface
    assert verb_in_caps("expect", lean) is True          # expect IS 80%-case


def test_vb_mcp_defaults_to_lean(monkeypatch):
    from click.testing import CliRunner
    from vibatchium import cli as cli_mod
    from vibatchium import mcp_server
    rec = {}
    monkeypatch.setattr(mcp_server, "_entrypoint",
                        lambda caps=None: rec.update(caps=caps))
    res = CliRunner().invoke(cli_mod.cli, ["mcp"])
    assert res.exit_code == 0, res.output
    assert rec["caps"] == "lean"              # unset → lean
    rec.clear()
    res = CliRunner().invoke(cli_mod.cli, ["mcp", "--caps", "full"])
    assert rec["caps"] == "full"              # explicit full wins


def test_console_and_expect_registered_as_mcp_tools():
    from vibatchium.mcp_server import TOOLS
    names = {t[0] for t in TOOLS}
    assert {"console_start", "console_stop", "console_dump", "expect"} <= names


def test_entrypoint_and_python_m_default_to_lean(monkeypatch):
    """`python -m vibatchium.mcp_server` (and an empty --caps) default to lean too,
    not just the cli `vb mcp` path; explicit full restores the full surface."""
    from vibatchium import mcp_server as M
    from vibatchium.caps import resolve_caps

    async def _noop():
        return None

    monkeypatch.setattr(M, "main", _noop)
    prev = M._ACTIVE_CAPS
    try:
        M._entrypoint(None)              # the python -m path
        assert M._ACTIVE_CAPS == resolve_caps("lean")
        M._entrypoint("")                # empty also -> lean
        assert M._ACTIVE_CAPS == resolve_caps("lean")
        M._entrypoint("full")            # explicit full -> no filter
        assert M._ACTIVE_CAPS is None
    finally:
        M._ACTIVE_CAPS = prev


# ─── 4. one-time Chrome auto-install gating (PURE) ───────────────────────
def test_autoinstall_gating(monkeypatch):
    import subprocess
    from vibatchium.daemon import registry as R
    calls = {"n": 0}

    def fake_run(cmd, **kw):
        calls["n"] += 1
        class _CP:
            returncode = 0
        return _CP()

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        # opt-out via env → never installs
        monkeypatch.setenv("VIBATCHIUM_AUTO_INSTALL", "0")
        R._chrome_install_attempted = False
        assert asyncio.run(R._maybe_autoinstall_chrome(
            RuntimeError("Executable doesn't exist; run patchright install"))) is False
        assert calls["n"] == 0

        # non-matching error → not our problem, no install
        monkeypatch.delenv("VIBATCHIUM_AUTO_INSTALL", raising=False)
        R._chrome_install_attempted = False
        assert asyncio.run(R._maybe_autoinstall_chrome(
            RuntimeError("net::ERR_NAME_NOT_RESOLVED"))) is False
        assert calls["n"] == 0

        # matching missing-Chrome error → installs once, returns True (retry)
        R._chrome_install_attempted = False
        assert asyncio.run(R._maybe_autoinstall_chrome(
            RuntimeError("Executable doesn't exist at /x; run patchright install chrome"))) is True
        assert calls["n"] == 1

        # one-shot guard: a second failure does NOT re-install
        assert asyncio.run(R._maybe_autoinstall_chrome(
            RuntimeError("Executable doesn't exist"))) is False
        assert calls["n"] == 1
    finally:
        R._chrome_install_attempted = False   # reset module flag for other tests
