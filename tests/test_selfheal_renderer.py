"""0.7.0 self-healing renderer (Chrome crash auto-recovery).

The PURE + IN-PROCESS tests (no real Chrome) are the load-bearing gate — they
deterministically exercise the dispatch recovery state machine via a fake
session whose ``context.new_page()`` succeeds. The LIVE relaunch test launches
a real headless Chrome through an in-process registry; the ``chrome://crash``
and last-page-death tests are best-effort and self-skip if the platform won't
crash on cue.
"""
from __future__ import annotations

import asyncio
import os
import time
import types
from pathlib import Path

import pytest

from vibatchium.client import call, DaemonError
from vibatchium.daemon.browser import is_crash_error


# ─── PURE: crash-signature detection ─────────────────────────────────────
def test_is_crash_error_matches_signatures():
    crashy = [
        "Page.goto: Page crashed",
        "Target crashed",
        "Target closed",
        "Target page, context or browser has been closed",
        "Browser has been closed",
        "Navigation failed because page crashed",
        "Connection closed while reading from the driver",
    ]
    for msg in crashy:
        assert is_crash_error(RuntimeError(msg)) is True, msg
    benign = [
        "element not found",
        "Timeout 30000ms exceeded",
        "net::ERR_NAME_NOT_RESOLVED",
        "strict mode violation: locator resolved to 2 elements",
    ]
    for msg in benign:
        assert is_crash_error(RuntimeError(msg)) is False, msg


def test_is_crash_error_ignores_benign_url_and_timeout():
    # The driver embeds the navigated URL / JS message verbatim — a benign
    # string containing 'crashed'/'closed' must NOT be misread as a crash
    # (else a non-retried verb silently swaps the user's live page).
    from patchright.async_api import TimeoutError as PWTimeout
    assert is_crash_error(RuntimeError(
        "Page.goto: net::ERR_ABORTED at https://x.com/?q=crashed")) is False
    assert is_crash_error(RuntimeError(
        "Evaluation failed: WebSocket connection closed")) is False
    assert is_crash_error(PWTimeout("Timeout 30000ms exceeded")) is False
    # …but the anchored driver phrases still match a real crash:
    assert is_crash_error(RuntimeError(
        "Target page, context or browser has been closed")) is True


def test_retry_safe_verbs_excludes_mutating_and_phantoms():
    from vibatchium.daemon.server import Daemon
    rs = Daemon.RETRY_SAFE_VERBS
    assert "content" not in rs   # `content` == set_content (mutating, NOT a read)
    for phantom in ("read", "snapshot", "links", "accessibility"):
        assert phantom not in rs, f"{phantom} is not a real verb"
    assert {"go", "text", "html", "screenshot"} <= rs


# ─── IN-PROCESS: dispatch recovery state machine ─────────────────────────
class _FakeContext:
    def __init__(self):
        self.pages = []
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        p = types.SimpleNamespace(url="about:blank", is_closed=lambda: False)
        self.pages.append(p)
        return p


def _make_daemon_entry():
    """A real Daemon + a SessionEntry whose fake session can revive a page
    (tier-1) without a real Chrome. Registered in the registry so the recovery
    machine can find it."""
    os.environ["VIBATCHIUM_PLUGINS"] = "0"  # don't cold-load plugins in-process
    from vibatchium.daemon.server import Daemon
    from vibatchium.daemon.registry import SessionEntry
    d = Daemon()
    ctx = _FakeContext()
    sess = types.SimpleNamespace(
        context=ctx, page=types.SimpleNamespace(url="about:blank"),
        frame_ref=None, mode="launch", headless=True, nav_allowlist=None,
        flags={"backend": "patchright"})
    entry = SessionEntry(name="t", profile_dir=Path("/tmp/vbtest-t"), session=sess)
    d.registry._entries["t"] = entry
    return d, entry


def test_dispatch_recovers_and_retries_read_verb(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_SELF_HEAL", raising=False)
    d, entry = _make_daemon_entry()
    calls = {"n": 0}

    async def flaky_text(daemon, args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Page.goto: Page crashed")
        return {"text": "ok", "attempt": calls["n"]}

    d._handlers["text"] = flaky_text
    out = asyncio.run(d._run_session_verb_with_recovery("text", {}, entry, "t"))
    assert out["attempt"] == 2
    assert calls["n"] == 2                       # original + one retry
    assert entry.recovered == 1
    assert entry.session.context.new_page_calls == 1  # tier-1 fresh page


def test_mutating_verb_recovers_not_retried(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_SELF_HEAL", raising=False)
    d, entry = _make_daemon_entry()
    calls = {"n": 0}

    async def flaky_click(daemon, args):
        calls["n"] += 1
        raise RuntimeError("Target crashed")

    d._handlers["click"] = flaky_click
    out = asyncio.run(d._run_session_verb_with_recovery("click", {}, entry, "t"))
    assert calls["n"] == 1                        # NOT retried (mutating verb)
    assert out["ok"] is False
    assert out["recovered"] is True
    assert "re-issue" in out["error"]
    assert entry.recovered == 1                   # but the session WAS recovered


def test_second_crash_surfaces(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_SELF_HEAL", raising=False)
    d, entry = _make_daemon_entry()
    calls = {"n": 0}

    async def always_crash(daemon, args):
        calls["n"] += 1
        raise RuntimeError("Page crashed")

    d._handlers["text"] = always_crash
    with pytest.raises(RuntimeError, match="vb session close"):
        asyncio.run(d._run_session_verb_with_recovery("text", {}, entry, "t"))
    assert calls["n"] == 2                        # original + exactly one retry


def test_non_crash_error_not_retried(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_SELF_HEAL", raising=False)
    d, entry = _make_daemon_entry()
    calls = {"n": 0}

    async def boom(daemon, args):
        calls["n"] += 1
        raise ValueError("not a crash")

    d._handlers["text"] = boom
    with pytest.raises(ValueError, match="not a crash"):
        asyncio.run(d._run_session_verb_with_recovery("text", {}, entry, "t"))
    assert calls["n"] == 1
    assert entry.recovered == 0                   # no recovery for a plain error


def test_self_heal_kill_switch_off(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_SELF_HEAL", "0")
    d, entry = _make_daemon_entry()
    calls = {"n": 0}

    async def crash(daemon, args):
        calls["n"] += 1
        raise RuntimeError("Page crashed")

    d._handlers["text"] = crash
    with pytest.raises(RuntimeError, match="Page crashed"):
        asyncio.run(d._run_session_verb_with_recovery("text", {}, entry, "t"))
    assert calls["n"] == 1
    assert entry.recovered == 0                   # kill-switch disables recovery


# ─── UNIT: relaunch refuses attach mode (don't kill a foreign Chrome) ─────
def test_attach_relaunch_refuses():
    from vibatchium.daemon.registry import SessionRegistry, SessionEntry
    reg = SessionRegistry()
    fake = types.SimpleNamespace(mode="attach")
    reg._entries["att"] = SessionEntry(
        name="att", profile_dir=Path("/tmp/x"), session=fake)
    with pytest.raises(RuntimeError, match="re-attach"):
        asyncio.run(reg.relaunch("att"))


# ─── LIVE: real relaunch preserves entry identity + flags + nav guard ────
def test_relaunch_preserves_flags_and_navguard():
    from vibatchium.daemon.registry import SessionRegistry

    async def go():
        reg = SessionRegistry()
        try:
            entry = await reg.create("vbtest-relaunch", headless=True,
                                     ephemeral=True)
            entry.flags["goal_caps"] = "read"
            entry.session.nav_allowlist = {"example.com"}
            old_session = entry.session
            e2 = await reg.relaunch("vbtest-relaunch")
            assert e2 is entry                       # SAME entry object
            assert e2.session is not old_session     # NEW browser session
            assert e2.flags.get("goal_caps") == "read"   # flags preserved
            assert e2.session.nav_allowlist == {"example.com"}  # wall carried fwd
            assert e2.session._nav_guard_installed is True       # guard re-armed
            assert e2.snapshot is None and e2.handles == {}
            assert e2.recovered == 1
            await e2.session.page.goto("about:blank")  # fresh page is usable
        finally:
            await reg.close_all()

    asyncio.run(go())


# ─── LIVE (observability): status + session_list expose recovery state ───
def test_status_and_list_expose_recovered_and_budgets():
    st = call("status")
    assert st.get("recovered", 0) >= 0
    assert "last_recovered_at" in st
    assert "budgets" in st and "persistent" in st["budgets"]
    sl = call("session_list")
    assert "budgets" in sl
    for row in sl["sessions"]:
        if row.get("running"):
            assert "recovered" in row
            assert "ephemeral" in row


# ─── LIVE (best-effort): chrome://crash auto-recovers the session ────────
def test_renderer_crash_auto_recovers_live(local_server):
    sess = "vbtest-crashheal"
    try:
        call("start", {"headless": True, "ephemeral": True}, session=sess)
        call("go", {"url": f"{local_server}/simple.html"}, session=sess)
        # Navigating to chrome://crash kills the renderer. The `go` surfaces an
        # error (it re-crashes on its single retry), but the session is revived
        # under us — that's what we assert via the recovered counter.
        try:
            call("go", {"url": "chrome://crash"}, session=sess)
        except DaemonError:
            pass
        st = call("status", session=sess)   # status is UNLOCKED — always answers
        if st.get("recovered", 0) < 1:
            pytest.skip("chrome://crash did not crash the renderer on this platform")
        assert st["recovered"] >= 1          # PROOF of self-heal
        # And the session becomes usable again. The first nav right after a
        # crash can transiently ERR_ABORTED while the fresh page settles —
        # self-heal recovers that too, so retry a few times.
        ok = False
        for _ in range(6):
            try:
                r = call("go", {"url": f"{local_server}/simple.html"}, session=sess)
                if "simple" in (r.get("url") or ""):
                    ok = True
                    break
            except DaemonError:
                time.sleep(0.3)
        assert ok, "session never became usable again after crash recovery"
    finally:
        try:
            call("session_close", {"name": sess})
        except DaemonError:
            pass
