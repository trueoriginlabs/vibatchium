"""Wave 7.7.10 — fixes from the post-runs full-stack audit.

After three real agent runs (Codex × 2 + Nemotron on opencode), an audit
identified 9 high/critical issues:

  C1 - explore screenshot perms leak (umask 0664 instead of 0600)
  C2 - argv rewrite only checks argv[1] — breaks `--session X session_close`
  C3 - text/html/attr/value bypass resolver — reject @eN refs
  H1 - attr/value not exposed via MCP
  H2 - --url flag only on verify-url, not go/wait-url/wait-response
  H3 - tabs/snapshot/goto/etc aliases missing — bad "Did you mean"
  H4 - screenshot defaults to ./screenshot.png in CWD
  H5 - no-session error inconsistent (RuntimeError: prefix on handler path)
  H6 - cli docstring referenced non-existent set-log-verbs command

These tests pin each fix.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

from vibatchium.cli import _find_verb_index, _rewrite_mcp_aliases


def _vibatchium_bin() -> str:
    return str(Path(__file__).parent.parent / ".venv" / "bin" / "vibatchium")


# ─── C2: argv rewrite handles --session NAME verb_form ────────────────


def test_find_verb_index_skips_session_flag():
    assert _find_verb_index(["vibatchium", "--session", "work", "session_close"]) == 3


def test_find_verb_index_skips_json_flag():
    assert _find_verb_index(["vibatchium", "--json", "status"]) == 2


def test_find_verb_index_skips_combined_global_flags():
    assert _find_verb_index(["vibatchium", "--json", "--session", "work", "go"]) == 4


def test_find_verb_index_no_verb_returns_minus_one():
    assert _find_verb_index(["vibatchium"]) == -1
    assert _find_verb_index(["vibatchium", "--help"]) == -1
    assert _find_verb_index(["vibatchium", "--json"]) == -1


def test_argv_rewrite_handles_session_prefix():
    """Repro for the original bug: --session NAME session_close BROKE."""
    out = _rewrite_mcp_aliases(["vibatchium", "--session", "work", "session_close"])
    assert out == ["vibatchium", "--session", "work", "session", "close"]


def test_argv_rewrite_handles_session_prefix_with_underscored_top_level():
    out = _rewrite_mcp_aliases(["vibatchium", "--session", "work", "verify_url", "https://x"])
    assert out == ["vibatchium", "--session", "work", "verify-url", "https://x"]


# ─── H3: top-level aliases (tabs/snapshot/goto/etc.) ──────────────────


def test_alias_tabs_to_pages():
    assert _rewrite_mcp_aliases(["vibatchium", "tabs"]) == ["vibatchium", "pages"]


def test_alias_snapshot_to_map():
    assert _rewrite_mcp_aliases(["vibatchium", "snapshot"]) == ["vibatchium", "map"]


def test_alias_goto_to_go():
    assert _rewrite_mcp_aliases(["vibatchium", "goto", "https://x"]) == \
        ["vibatchium", "go", "https://x"]


def test_alias_navigate_open_visit_to_go():
    for verb in ("navigate", "open", "visit"):
        assert _rewrite_mcp_aliases(["vibatchium", verb, "https://x"]) == \
            ["vibatchium", "go", "https://x"]


def test_alias_dom_to_html():
    assert _rewrite_mcp_aliases(["vibatchium", "dom"]) == ["vibatchium", "html"]


def test_alias_get_text_to_text():
    assert _rewrite_mcp_aliases(["vibatchium", "get-text"]) == ["vibatchium", "text"]
    assert _rewrite_mcp_aliases(["vibatchium", "get_text"]) == ["vibatchium", "text"]


def test_alias_is_state_to_is():
    assert _rewrite_mcp_aliases(["vibatchium", "is_state"]) == ["vibatchium", "is"]


def test_alias_tab_to_page_group_subcommand_passthrough():
    """`tab new myname` → `page new myname` (group + subcommand preserved)."""
    assert _rewrite_mcp_aliases(["vibatchium", "tab", "new", "myname"]) == \
        ["vibatchium", "page", "new", "myname"]


def test_alias_works_after_session_flag():
    out = _rewrite_mcp_aliases(["vibatchium", "--session", "work", "tabs"])
    assert out == ["vibatchium", "--session", "work", "pages"]


# ─── H6: set-log-verbs CLI command exists ─────────────────────────────


def test_set_log_verbs_cli_command_exists():
    from vibatchium.cli import cli
    assert "set-log-verbs" in cli.commands


def test_set_log_verbs_underscored_alias_works_via_help():
    r = subprocess.run([_vibatchium_bin(), "set_log_verbs", "--help"],
                      capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    assert "set-log-verbs" in r.stdout
    assert "Toggle per-verb" in r.stdout


# ─── H1: attr/value in MCP TOOLS ──────────────────────────────────────


def test_attr_in_mcp_tools():
    from vibatchium.mcp_server import TOOLS
    names = {t[0] for t in TOOLS}
    assert "attr" in names
    attr_tool = next(t for t in TOOLS if t[0] == "attr")
    # Schema requires target + name
    props = attr_tool[2]["properties"]
    assert "target" in props
    assert "name" in props


def test_value_in_mcp_tools():
    from vibatchium.mcp_server import TOOLS
    names = {t[0] for t in TOOLS}
    assert "value" in names
    value_tool = next(t for t in TOOLS if t[0] == "value")
    assert "target" in value_tool[2]["properties"]


def test_text_html_now_use_target_param():
    """text/html schemas should advertise `target` (not `selector`) to match
    click/fill/hover naming. Both names accepted at runtime."""
    from vibatchium.mcp_server import TOOLS
    for verb in ("text", "html"):
        tool = next(t for t in TOOLS if t[0] == verb)
        assert "target" in tool[2]["properties"]


# ─── C3: text/html/attr/value route through resolver ──────────────────


def test_text_accepts_target_param(local_server):
    """The fix: text() should accept `target` kwarg (modern) AND `selector`
    (legacy). Both should route through _resolve_target."""
    from vibatchium.client import call
    call("go", {"url": f"{local_server}/simple.html"})
    # Use `target` (modern)
    r1 = call("text", {"target": "h1"})
    # Use `selector` (legacy back-compat)
    r2 = call("text", {"selector": "h1"})
    assert r1.get("text") == r2.get("text")
    assert r1.get("text")  # non-empty


def test_attr_routes_through_resolver(local_server):
    from vibatchium.client import call
    call("go", {"url": f"{local_server}/simple.html"})
    # Plain CSS still works
    r = call("attr", {"target": "h1", "name": "id"})
    # Doesn't matter what the id is — just that the call doesn't error
    assert "value" in r


def test_attr_requires_target_or_selector():
    from vibatchium.client import call, DaemonError
    import pytest
    with pytest.raises(DaemonError):
        call("attr", {"name": "href"})


# ─── H2: --url flag on go/wait-url/wait-response ──────────────────────


def test_go_accepts_url_flag(local_server):
    r = subprocess.run([_vibatchium_bin(), "go", "--url", f"{local_server}/simple.html"],
                      capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert "simple.html" in r.stdout


def test_go_positional_still_works(local_server):
    r = subprocess.run([_vibatchium_bin(), "go", f"{local_server}/simple.html"],
                      capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"stderr: {r.stderr}"


def test_go_with_no_url_errors_clearly():
    r = subprocess.run([_vibatchium_bin(), "go"],
                      capture_output=True, text=True, timeout=10)
    assert r.returncode != 0


# ─── C1 + H4: secure cache paths for explore + screenshot ─────────────


def test_screenshot_default_lands_in_cache_with_0600(local_server):
    """The fix for both the screenshot default (CWD → cache) and the
    perms leak (umask → 0600). Daemon-side change pins the bytes-then-
    secure_write order."""
    name = "audit_screenshot_perms"
    from vibatchium.client import call, DaemonError
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        call("session_delete", {"name": name})
    except DaemonError:
        pass
    try:
        # Use the daemon directly so we control session naming
        call("start", {"headless": True}, session=name)
        call("go", {"url": f"{local_server}/simple.html"}, session=name)
        # CLI default: no -o
        r = subprocess.run([_vibatchium_bin(), "--session", name, "screenshot"],
                          capture_output=True, text=True, timeout=15)
        assert r.returncode == 0, f"stderr: {r.stderr}"
        # Path printed to stdout
        path = r.stdout.strip()
        assert path.endswith(".png")
        assert "vibatchium" in path  # under CACHE_DIR/vibatchium/
        # The file exists and has 0600 perms
        p = Path(path)
        assert p.exists()
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)} for {p}"
    finally:
        try:
            call("session_close", {"name": name})
        except DaemonError:
            pass
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_explore_default_uses_cache_dir_with_0700(local_server, tmp_path):
    """Explore should land in CACHE_DIR/explores (resolves XDG_RUNTIME_DIR),
    parent dir 0700, PNG 0600."""
    name = "audit_explore_perms"
    env = {**os.environ, "HOME": str(tmp_path)}
    r = subprocess.run([_vibatchium_bin(),
                       "--session", name,
                       "explore", f"{local_server}/simple.html",
                       "--skip-verify"],
                      capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    data = json.loads(r.stdout)
    shot_path = Path(data["screenshot_path"])
    assert shot_path.exists()
    # PNG itself: 0600
    file_mode = stat.S_IMODE(shot_path.stat().st_mode)
    assert file_mode == 0o600, f"PNG expected 0600, got {oct(file_mode)}"
    # Parent dir: 0700
    dir_mode = stat.S_IMODE(shot_path.parent.stat().st_mode)
    assert dir_mode == 0o700, f"dir expected 0700, got {oct(dir_mode)}"


# ─── H5: no-session error string normalized across paths ──────────────


def test_no_session_error_dispatcher_path():
    """Verb gated at dispatcher (click) — clean no-session message."""
    r = subprocess.run([_vibatchium_bin(), "--session", "nonexistent_xyz",
                       "click", "@e1"],
                      capture_output=True, text=True, timeout=10)
    assert r.returncode != 0
    assert "no session" in r.stderr
    assert "run `vibatchium start" in r.stderr
    assert "RuntimeError:" not in r.stderr  # ← THE fix


def test_no_session_error_handler_path():
    """UNLOCKED_VERBS (wait_url) hit handler-level check — message should
    now MATCH the dispatcher path exactly."""
    r = subprocess.run([_vibatchium_bin(), "--session", "nonexistent_xyz",
                       "wait", "url", "*example*", "--timeout", "500"],
                      capture_output=True, text=True, timeout=10)
    assert r.returncode != 0
    assert "no session" in r.stderr
    assert "run `vibatchium start" in r.stderr
    assert "RuntimeError:" not in r.stderr  # ← THE fix


def test_session_not_started_exception_class_exists():
    from vibatchium.daemon.handlers import SessionNotStarted
    assert issubclass(SessionNotStarted, Exception)
