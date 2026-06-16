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

import asyncio

import pytest

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
    """screenshot='always' forces the visual: one call → page content + PNG."""
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
                       "screenshot": "always",  # 0.7.0: opt in to the screenshot
                       "skip_verify": True},  # local_server isn't reachable by name
                      session=name)
        # Expected shape
        assert result["url"].endswith("simple.html")
        assert result.get("title")
        assert result.get("status") == 200
        assert result.get("text"), "expected non-empty text"
        assert result.get("screenshot_b64"), "expected base64 screenshot"
        assert "requested" in (result.get("screenshot_reason") or "")
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


def test_explore_auto_default_skips_screenshot_on_text_page(local_server):
    """0.7.0 TEXT-FIRST: a text-rich page returns NO screenshot by default —
    the common case is fast + cheap (no base64 in the agent's context)."""
    name = "explore_textfirst_session"
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        result = call("explore",
                      {"url": f"{local_server}/simple.html",
                       "skip_verify": True},   # no `screenshot` → default 'auto'
                      session=name)
        assert result.get("text"), "expected non-empty text"
        assert "screenshot_b64" not in result, "auto default must NOT screenshot a text page"
        assert "screenshot_reason" not in result
    finally:
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_explore_auto_fallback_screenshots_blank_page(local_server):
    """0.7.0: when the text path 'can't' (page yields no usable text), `auto`
    falls back to a screenshot so the agent still has something to look at.

    NOTE: blank.html has an empty <body>, so go's SPA-hydration wait can never
    see innerText>100 and deterministically burns its full ~5s render_timeout
    before this returns. That ~5s is expected here (not a hang) — it is the
    price of exercising the genuinely-empty fallback path."""
    name = "explore_fallback_session"
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        result = call("explore",
                      {"url": f"{local_server}/blank.html",
                       "skip_verify": True},   # default 'auto'
                      session=name)
        assert not (result.get("text") or "").strip(), "blank.html should yield no text"
        assert result.get("screenshot_b64"), "auto must fall back to a screenshot"
        assert "text-fallback" in (result.get("screenshot_reason") or "")
    finally:
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_explore_never_string_suppresses_even_the_fallback(local_server):
    """The 'never' STRING (what the CLI --no-screenshot and MCP clients emit —
    a distinct handler branch from the bool False) suppresses the screenshot
    even on a page where `auto` WOULD fall back (blank.html). Eats the ~5s
    hydration wait like the blank-fallback test above."""
    name = "explore_never_str_session"
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        result = call("explore",
                      {"url": f"{local_server}/blank.html",
                       "screenshot": "never", "skip_verify": True},
                      session=name)
        assert "screenshot_b64" not in result   # 'never' beats the auto fallback
        assert "screenshot_reason" not in result
    finally:
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_explore_auto_screenshots_walled_page(local_server):
    """0.7.0: auto mode falls back to a screenshot on a challenge/login wall
    even when the wall renders enough boilerplate to clear min_text_chars —
    seeing the wall pixels is exactly the case where a screenshot helps. (Pre-fix
    a walled page with >64 chars got NO screenshot.)"""
    name = "explore_walled_session"
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        result = call("explore",
                      {"url": f"{local_server}/walled.html",
                       "skip_verify": True},   # default 'auto'
                      session=name)
        assert result.get("walled"), "go should flag the cloudflare wall"
        assert len((result.get("text") or "").strip()) >= 64, "wall text clears the threshold"
        assert result.get("screenshot_b64"), "auto must still screenshot a walled page"
        assert "walled-fallback" in (result.get("screenshot_reason") or "")
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


# ─── 4. screenshots come back as viewable image blocks, not base64 text ──


def test_mcp_explore_default_screenshot_is_auto():
    """0.7.0: the explore tool no longer defaults to screenshot=true — the
    schema default is the text-first 'auto' policy, and the schema accepts BOTH
    the enum strings and the back-compat booleans the handler/docs promise."""
    from vibatchium.mcp_server import TOOLS
    explore_tool = next(t for t in TOOLS if t[0] == "explore")
    schema = explore_tool[2]
    shot = schema["properties"]["screenshot"]
    assert shot.get("default") == "auto"
    branches = shot["anyOf"]
    enums = next(b["enum"] for b in branches if "enum" in b)
    assert set(enums) == {"auto", "always", "never"}
    assert any(b.get("type") == "boolean" for b in branches)   # bools stay valid
    # full-page must no longer be the default (viewport is cheaper)
    assert schema["properties"]["full_page"].get("default") is False
    # the auto-fallback threshold is exposed so agents can tune it per call
    assert schema["properties"]["min_text_chars"].get("default") == 64


def test_mcp_explore_schema_validates_string_and_bool_screenshot():
    """The published inputSchema must accept BOTH the enum strings and the
    back-compat booleans — the MCP SDK jsonschema-validates args BEFORE the
    handler runs, so a string-only schema would reject screenshot=true and the
    handler's bool-coercion (and the 'booleans still accepted' docs) would be a
    lie over MCP."""
    jsonschema = pytest.importorskip("jsonschema")
    from vibatchium.mcp_server import TOOLS
    schema = next(t for t in TOOLS if t[0] == "explore")[2]
    for val in ("auto", "always", "never", True, False):
        jsonschema.validate({"url": "https://x", "screenshot": val}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"url": "https://x", "screenshot": "sometimes"}, schema)


def test_mcp_explore_omitted_screenshot_forwards_no_key(monkeypatch):
    """When the model omits screenshot, call_tool must NOT inject the schema
    default — the key never reaches the daemon and the handler default ('auto')
    is the single source of truth for text-first behavior. Pins the schema
    default to the handler default so a one-sided edit can't silently diverge."""
    from vibatchium import mcp_server as M
    from vibatchium.mcp_server import TOOLS
    rec = {}
    monkeypatch.setattr(M, "daemon_call",
                        lambda cmd, args=None, **kw: rec.update(args=args) or {"text": "ok"})
    monkeypatch.setattr(M, "daemon_is_running", lambda: True)
    monkeypatch.setattr(M, "_ACTIVE_CAPS", None)
    asyncio.run(M.call_tool("explore", {"url": "https://x"}))
    assert "screenshot" not in rec["args"]   # default applied by handler, not the wire
    schema = next(t for t in TOOLS if t[0] == "explore")[2]
    assert schema["properties"]["screenshot"]["default"] == "auto"   # == handler default


def test_mcp_screenshot_returns_image_block_not_base64_text(monkeypatch):
    """A base64 PNG must come back as a VIEWABLE MCP image block — never inlined
    as base64 text, which would flood the model's context with useless tokens."""
    import mcp.types as types
    from vibatchium import mcp_server as M
    b64 = "iVBORw0KGgoAAAANSUhEUgAA"  # stand-in base64 PNG
    monkeypatch.setattr(M, "daemon_call",
                        lambda cmd, args=None, **kw: {"text": "hello",
                                                      "screenshot_b64": b64})
    monkeypatch.setattr(M, "daemon_is_running", lambda: True)
    monkeypatch.setattr(M, "_ACTIVE_CAPS", None)
    blocks = asyncio.run(M.call_tool("explore",
                                     {"url": "https://x", "screenshot": "always"}))
    assert isinstance(blocks[0], types.TextContent)
    assert b64 not in blocks[0].text, "base64 must NOT be inlined as text"
    assert "hello" in blocks[0].text
    imgs = [b for b in blocks if isinstance(b, types.ImageContent)]
    assert len(imgs) == 1
    assert imgs[0].data == b64 and imgs[0].mimeType == "image/png"


def test_mcp_screenshot_verb_png_b64_becomes_image_block(monkeypatch):
    """The standalone `screenshot` verb's png_b64 is also returned as an image."""
    import mcp.types as types
    from vibatchium import mcp_server as M
    b64 = "QQQQ"
    monkeypatch.setattr(M, "daemon_call", lambda cmd, args=None, **kw: {"png_b64": b64})
    monkeypatch.setattr(M, "daemon_is_running", lambda: True)
    monkeypatch.setattr(M, "_ACTIVE_CAPS", None)
    blocks = asyncio.run(M.call_tool("screenshot", {}))
    assert b64 not in blocks[0].text
    imgs = [b for b in blocks if isinstance(b, types.ImageContent)]
    assert len(imgs) == 1 and imgs[0].data == b64


def test_mcp_non_image_result_is_single_text_block(monkeypatch):
    """A plain result (no image field) stays a single text block at index 0 —
    JSON-parsing callers must be unaffected by the image-block change."""
    import json
    import mcp.types as types
    from vibatchium import mcp_server as M
    monkeypatch.setattr(M, "daemon_call", lambda cmd, args=None, **kw: {"url": "u", "ok": True})
    monkeypatch.setattr(M, "daemon_is_running", lambda: True)
    monkeypatch.setattr(M, "_ACTIVE_CAPS", None)
    blocks = asyncio.run(M.call_tool("url", {}))
    assert len(blocks) == 1 and isinstance(blocks[0], types.TextContent)
    assert json.loads(blocks[0].text) == {"url": "u", "ok": True}
