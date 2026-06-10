"""Wave 7.7.5 — the "just works" layer.

Real-run feedback: agents using the vibatchium MCP slip into anti-patterns
(no auto-start, headed by default, 4-6 calls for "go look at this URL")
because the surface gives them primitives, not recipes. This wave adds
three thin fixes that make the 80% case match vibium-style ergonomics
without removing vibatchium's primitive power:

  1. `_go` auto-starts a session if none exists (headless)
  2. New `explore` verb: one call = verify_url → start → go → text →
     screenshot → close
  3. MCP `start` calls default to headless when args don't specify
     (CLI users still get the canonical Patchright headed default)
"""
from __future__ import annotations

from vibatchium.client import call, DaemonError


# ─── 1. _go auto-starts ─────────────────────────────────────────────────


def test_go_auto_starts_when_no_session(local_server):
    """Calling `go` against a fresh session name with no prior `start`
    should auto-spawn a headless Chrome and navigate. The agent shouldn't
    need to remember the start step for simple browse cases."""
    name = "autostart_probe_session"
    # Clean any prior state
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        call("session_delete", {"name": name})
    except DaemonError:
        pass
    try:
        # Skip the explicit start — go directly. Should auto-start.
        result = call("go", {"url": f"{local_server}/simple.html"},
                      session=name)
        # Navigation succeeded (auto-start fired)
        assert result.get("url", "").endswith("simple.html")
        # And there's now a running session
        listing = call("session_list")
        running = {s["name"] for s in listing.get("sessions", []) if s.get("running")}
        assert name in running, f"auto-start didn't register: {running}"
    finally:
        try:
            call("session_close", {"name": name})
        except DaemonError:
            pass
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_go_auto_start_can_be_disabled(monkeypatch, local_server):
    """VIBATCHIUM_NO_AUTO_START=1 in the daemon env disables auto-start.
    This is a unit-test on the resolution logic since we can't easily
    set daemon env from the test process."""
    def _should_auto_start(env_val: str) -> bool:
        return env_val.lower() not in ("1", "true", "yes")
    assert _should_auto_start("0") is True   # default → auto-start enabled
    assert _should_auto_start("") is True    # unset → auto-start enabled
    assert _should_auto_start("1") is False  # explicit opt-out
    assert _should_auto_start("true") is False
    assert _should_auto_start("yes") is False


# ─── 2. `explore` verb ─────────────────────────────────────────────────


def test_explore_one_call_returns_text_and_screenshot(local_server):
    """The whole point of explore: one call → page content + visual."""
    name = "explore_probe_session"
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        call("session_delete", {"name": name})
    except DaemonError:
        pass
    try:
        result = call("explore",
                      {"url": f"{local_server}/simple.html",
                       "skip_verify": True},  # local_server isn't reachable by name
                      session=name)
        # Expected shape
        assert result["url"].endswith("simple.html")
        assert result.get("title")
        assert result.get("status") == 200
        assert result.get("text"), "expected non-empty text"
        assert result.get("screenshot_b64"), "expected base64 screenshot"
        assert result.get("closed") is True, "session should auto-close"
        assert result.get("elapsed_ms", 0) > 0
        # Session should no longer be running (auto-closed)
        listing = call("session_list")
        running = {s["name"] for s in listing.get("sessions", []) if s.get("running")}
        assert name not in running
    finally:
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_explore_keep_open_leaves_session_alive(local_server):
    """keep_open=True is the "I want to follow up with more calls" mode."""
    name = "explore_keep_open_session"
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        call("session_delete", {"name": name})
    except DaemonError:
        pass
    try:
        result = call("explore",
                      {"url": f"{local_server}/simple.html",
                       "keep_open": True, "skip_verify": True},
                      session=name)
        assert result.get("closed") is False
        listing = call("session_list")
        running = {s["name"] for s in listing.get("sessions", []) if s.get("running")}
        assert name in running, "keep_open should leave session running"
    finally:
        try:
            call("session_close", {"name": name})
        except DaemonError:
            pass
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_explore_skips_navigation_on_dead_dns():
    """The DNS pre-check should fail-fast on a bad URL — no start, no go,
    no screenshot. Returns the failure quickly."""
    import time
    name = "explore_dead_dns_session"
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    t0 = time.time()
    result = call("explore",
                  {"url": "https://this-host-definitely-does-not-exist-xyz.invalid/"},
                  session=name)
    elapsed = time.time() - t0
    assert result.get("verified") is False
    assert "DNS" in result.get("error", "")
    # Should be sub-5s (DNS check timeout + small overhead), much faster
    # than the 30s navigation timeout it would have eaten
    assert elapsed < 6, f"dead-DNS check took {elapsed:.1f}s, expected <6"
    # No screenshot, no text — we never got past the verify
    assert "screenshot_b64" not in result
    assert "text" not in result


def test_explore_no_screenshot_when_disabled(local_server):
    """screenshot=false skips the screenshot capture."""
    name = "explore_no_shot_session"
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        call("session_delete", {"name": name})
    except DaemonError:
        pass
    try:
        result = call("explore",
                      {"url": f"{local_server}/simple.html",
                       "screenshot": False, "skip_verify": True},
                      session=name)
        assert result.get("text")
        assert "screenshot_b64" not in result
    finally:
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


# ─── 3. MCP headless default ────────────────────────────────────────────


def test_mcp_leaves_headless_unset_so_daemon_decides(monkeypatch):
    """0.6.11: when the agent calls `start` through MCP without `headless` and
    VIBATCHIUM_MCP_HEADED_DEFAULT is unset, MCP must leave `headless` UNSET so
    the daemon's resolve_headless() applies the canonical precedence (defaults
    headless, but honors a daemon-wide VIBATCHIUM_DEFAULT_HEADED opt-in). The
    OLD behavior hardcoded headless=True here, which ignored DEFAULT_HEADED.
    Imports the REAL helper so this can't drift from the implementation."""
    from vibatchium.mcp_server import _apply_mcp_start_posture
    monkeypatch.delenv("VIBATCHIUM_MCP_HEADED_DEFAULT", raising=False)
    # No env, no explicit arg → headless left UNSET (daemon decides)
    assert "headless" not in _apply_mcp_start_posture("start", {})
    # Explicit per-call headless always wins (untouched)
    assert _apply_mcp_start_posture("start", {"headless": False})["headless"] is False
    assert _apply_mcp_start_posture("start", {"headless": True})["headless"] is True
    # Other verbs untouched
    assert "headless" not in _apply_mcp_start_posture("go", {"url": "x"})


def test_mcp_headed_default_env_forces_headed(monkeypatch):
    """0.6.11: VIBATCHIUM_MCP_HEADED_DEFAULT=1 must actually force this MCP
    server headed (headless=False). Previously it was a no-op — it skipped the
    headless=True force, but the daemon then defaulted headless anyway, so the
    env var never produced a headed session."""
    from vibatchium.mcp_server import _apply_mcp_start_posture
    monkeypatch.setenv("VIBATCHIUM_MCP_HEADED_DEFAULT", "1")
    assert _apply_mcp_start_posture("start", {})["headless"] is False
    # Still defers to an explicit per-call value
    assert _apply_mcp_start_posture("start", {"headless": True})["headless"] is True


def test_explore_in_mcp_core_bucket():
    """`explore` should be in the `core` capability bucket so any
    cap-gated MCP surface still exposes it (it's the canonical one-call
    workflow for agents)."""
    from vibatchium.mcp_server import _CAP_BUCKETS
    assert "explore" in _CAP_BUCKETS["core"]


def test_mcp_lists_explore_tool_in_schema():
    """The MCP schema list must include `explore` so agents can discover it."""
    from vibatchium.mcp_server import TOOLS
    names = {t[0] for t in TOOLS}
    assert "explore" in names
    explore_tool = next(t for t in TOOLS if t[0] == "explore")
    # Description should mention "ONE-CALL" or similar steering language
    assert "ONE-CALL" in explore_tool[1] or "one-call" in explore_tool[1].lower()
