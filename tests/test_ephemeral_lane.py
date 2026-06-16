"""0.7.0 cap relief: off-budget ephemeral one-shot lane.

UNIT tests exercise the two-budget accounting in the registry with a stubbed
launch (no real Chrome). LIVE tests drive `vb explore` through a real daemon and
assert it runs off-budget without touching `default`.
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from vibatchium.client import call, DaemonError
from vibatchium.daemon import registry as R
from vibatchium.daemon.registry import SessionRegistry, SessionEntry, SessionLimitError


@pytest.fixture(autouse=True)
def _clean_session_env(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_SESSION", raising=False)
    monkeypatch.delenv("VIBATCHIUM_LEASE", raising=False)


# ─── UNIT: env reader + count helpers + naming ───────────────────────────
def test_get_max_ephemeral_default_and_env(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_MAX_EPHEMERAL", raising=False)
    assert R.get_max_ephemeral() == 2
    monkeypatch.setenv("VIBATCHIUM_MAX_EPHEMERAL", "5")
    assert R.get_max_ephemeral() == 5
    monkeypatch.setenv("VIBATCHIUM_MAX_EPHEMERAL", "garbage")
    assert R.get_max_ephemeral() == 2
    monkeypatch.setenv("VIBATCHIUM_MAX_EPHEMERAL", "0")
    assert R.get_max_ephemeral() == 0
    monkeypatch.setenv("VIBATCHIUM_MAX_EPHEMERAL", "-3")
    assert R.get_max_ephemeral() == 0


def _stub_entry(reg, name, ephemeral):
    e = SessionEntry(name=name, profile_dir=Path("/tmp/" + name),
                     session=types.SimpleNamespace(mode="launch"),
                     ephemeral=ephemeral)
    reg._entries[name] = e
    return e


def test_count_helpers_partition():
    reg = SessionRegistry()
    _stub_entry(reg, "p1", False)
    _stub_entry(reg, "p2", False)
    e3 = _stub_entry(reg, "e1", True)
    assert reg.count_persistent() == 2
    assert reg.count_ephemeral() == 1
    b = reg.budgets()
    assert b["persistent"]["used"] == 2 and b["ephemeral"]["used"] == 1
    # classification is dynamic — a goal-claim that flips .ephemeral is reflected
    e3.ephemeral = False
    assert reg.count_persistent() == 3 and reg.count_ephemeral() == 0


def test_mint_ephemeral_name_unique_and_internal():
    from vibatchium.daemon.paths import validate_name
    reg = SessionRegistry()
    a, b = reg.mint_ephemeral_name(), reg.mint_ephemeral_name()
    assert a != b and a.startswith("_ex-")
    # Deliberately OUTSIDE the user-validatable namespace (leading underscore)
    # so a transient name can NEVER collide with a user-created session.
    with pytest.raises(ValueError):
        validate_name(a, kind="session name")


# ─── UNIT: create() splits the two budgets (stubbed launch) ──────────────
def _patch_launch(reg, monkeypatch):
    async def fake(name, *, profile_dir, headless, backend, proxy_cfg=None, geo_cfg=None):
        return types.SimpleNamespace(mode="launch", headless=headless, flags={})
    monkeypatch.setattr(reg, "_launch_for", fake)


def test_create_splits_budgets(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_MAX_SESSIONS", "1")
    monkeypatch.setenv("VIBATCHIUM_MAX_EPHEMERAL", "2")
    monkeypatch.setenv("VIBATCHIUM_WARM", "off")
    reg = SessionRegistry()
    _patch_launch(reg, monkeypatch)

    async def go():
        await reg.create("p1", headless=True)
        with pytest.raises(SessionLimitError, match="vb explore"):
            await reg.create("p2", headless=True)            # persistent full
        await reg.create("e1", headless=True, ephemeral=True)
        await reg.create("e2", headless=True, ephemeral=True)
        with pytest.raises(SessionLimitError, match="MAX_EPHEMERAL"):
            await reg.create("e3", headless=True, ephemeral=True)  # ephemeral full
        assert reg.count_persistent() == 1
        assert reg.count_ephemeral() == 2

    asyncio.run(go())


def test_ephemeral_full_does_not_block_persistent(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_MAX_SESSIONS", "2")
    monkeypatch.setenv("VIBATCHIUM_MAX_EPHEMERAL", "1")
    monkeypatch.setenv("VIBATCHIUM_WARM", "off")
    reg = SessionRegistry()
    _patch_launch(reg, monkeypatch)

    async def go():
        await reg.create("e1", headless=True, ephemeral=True)   # ephemeral full
        await reg.create("p1", headless=True)                   # persistent OK
        await reg.create("p2", headless=True)
        assert reg.count_persistent() == 2 and reg.count_ephemeral() == 1

    asyncio.run(go())


def test_max_ephemeral_zero_hard_disables(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_MAX_SESSIONS", "2")
    monkeypatch.setenv("VIBATCHIUM_MAX_EPHEMERAL", "0")
    monkeypatch.setenv("VIBATCHIUM_WARM", "off")
    reg = SessionRegistry()
    _patch_launch(reg, monkeypatch)

    async def go():
        with pytest.raises(SessionLimitError, match="MAX_EPHEMERAL"):
            await reg.create("e1", headless=True, ephemeral=True)
        await reg.create("p1", headless=True)                   # persistent unaffected
        assert reg.count_persistent() == 1 and reg.count_ephemeral() == 0

    asyncio.run(go())


# ─── LIVE: explore off-budget lane ───────────────────────────────────────
def _make_default_active():
    """Make `default` the active session so a no-pin explore takes the lane."""
    try:
        call("session_new", {"name": "default"})
    except DaemonError:
        pass
    try:
        call("session_use", {"name": "default"})
    except DaemonError:
        pass


def test_explore_runs_off_budget_no_pinned_session(local_server):
    _make_default_active()
    before = call("session_list")["budgets"]["persistent"]["used"]
    r = call("explore", {"url": f"{local_server}/simple.html"})
    assert r.get("lane") == "ephemeral"
    assert r["session"].startswith("_ex-")
    assert r["closed"] is True
    running = [s["name"] for s in call("session_list")["sessions"] if s["running"]]
    assert r["session"] not in running                       # auto-closed
    after = call("session_list")["budgets"]["persistent"]["used"]
    assert after == before                                   # never touched persistent


def test_explore_ephemeral_lane_captures_screenshot(local_server):
    """The off-budget no-pin lane funnels through the same _run_body, so an
    always-mode explore there returns a screenshot AND still auto-closes +
    reclaims the minted off-budget slot (capture-then-ephemeral-teardown clean)."""
    _make_default_active()
    before = call("session_list")["budgets"]["persistent"]["used"]
    r = call("explore", {"url": f"{local_server}/simple.html", "screenshot": "always"})
    assert r.get("lane") == "ephemeral"
    assert r["session"].startswith("_ex-")
    assert r.get("screenshot_b64"), "always-mode must capture in the ephemeral lane"
    assert "requested" in (r.get("screenshot_reason") or "")
    assert r["closed"] is True
    running = [s["name"] for s in call("session_list")["sessions"] if s["running"]]
    assert r["session"] not in running                       # minted slot reclaimed
    after = call("session_list")["budgets"]["persistent"]["used"]
    assert after == before                                   # never touched persistent


def test_explore_explicit_session_unchanged(local_server):
    name = "vbtest-explore-explicit"
    try:
        r = call("explore", {"url": f"{local_server}/simple.html"}, session=name)
        assert r["session"] == name
        assert "lane" not in r                               # legacy path
        assert r["closed"] is True
    finally:
        try:
            call("session_close", {"name": name})
        except DaemonError:
            pass


def test_explore_does_not_touch_default(local_server):
    _make_default_active()
    call("go", {"url": f"{local_server}/simple.html"})       # default → simple
    before = call("url")["url"]
    call("explore", {"url": f"{local_server}/simple.html?explore=1"})  # no pin
    after = call("url")["url"]
    assert after == before                                   # default untouched
    assert "explore=1" not in after


def test_status_and_session_list_report_budgets():
    st = call("status")
    assert st["budgets"]["persistent"]["cap"] >= 1
    sl = call("session_list")
    assert "persistent" in sl["budgets"] and "ephemeral" in sl["budgets"]


# ─── UNIT: MCP explore description advertises the off-budget lane ────────
def test_mcp_explore_description_off_budget():
    from vibatchium import mcp_server as M
    explore = next(t for t in M.TOOLS if t[0] == "explore")
    desc = explore[1]
    assert "ONE-CALL" in desc                                # existing assert preserved
    assert "ephemeral" in desc.lower() or "off-budget" in desc.lower()
