"""Wave 7.7.8 — fixes for friction observed in a real Codex run.

Codex tried "use vibatchium to make an aave forums account" and burned 3 retries:
  1. `verify_url X` → "No such command" (hyphenated alias missing)
  2. `verify-url --url X` → "No such option" (CLI was positional-only)
  3. `explore <url>` → returned 2,800 lines of base64 PNG in stdout

These tests pin the fixes so the next agent run doesn't trip on the same paths.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from vibatchium.cli import _rewrite_mcp_aliases, cli


# ─── Bug 1: argv rewrite handles top-level hyphenated commands ─────────


def test_argv_rewrite_verify_url_underscore_to_hyphen():
    """`vibatchium verify_url X` should rewrite to `vibatchium verify-url X`."""
    out = _rewrite_mcp_aliases(["vibatchium", "verify_url", "https://example.com"])
    assert out == ["vibatchium", "verify-url", "https://example.com"]


def test_argv_rewrite_leaves_existing_underscored_commands_alone():
    """Real underscored commands (if any) should pass through untouched."""
    # `setup` exists as-is (no underscore), this just confirms non-rewrite path
    out = _rewrite_mcp_aliases(["vibatchium", "setup", "--check"])
    assert out == ["vibatchium", "setup", "--check"]


def test_argv_rewrite_no_op_when_no_hyphenated_equivalent():
    """If neither underscored nor hyphenated form exists, don't invent one."""
    # `not_a_real_command` doesn't exist either way
    out = _rewrite_mcp_aliases(["vibatchium", "not_a_real_command", "X"])
    assert out == ["vibatchium", "not_a_real_command", "X"]


def test_argv_rewrite_session_prefix_still_works():
    """Regression: existing group-prefix rewrite (session_new → session new) still fires."""
    out = _rewrite_mcp_aliases(["vibatchium", "session_new", "foo"])
    assert out == ["vibatchium", "session", "new", "foo"]


def test_argv_rewrite_short_argv_safe():
    assert _rewrite_mcp_aliases(["vibatchium"]) == ["vibatchium"]
    assert _rewrite_mcp_aliases([]) == []


def test_verify_url_command_registered_with_hyphen():
    """Click registry should contain `verify-url`. The rewrite depends on it."""
    assert "verify-url" in cli.commands


# ─── Bug 2: verify-url accepts both positional and --url flag ──────────


def _vibatchium_bin() -> str:
    """Path to the `vb` binary, tolerant of both local-dev (.venv) and
    CI (PATH-installed via `pip install -e`) layouts."""
    import shutil
    venv_path = Path(__file__).parent.parent / ".venv" / "bin" / "vb"
    if venv_path.exists():
        return str(venv_path)
    which = shutil.which("vb")
    if which:
        return which
    return str(venv_path)  # fallback (will error at subprocess.run with a clear path)


def test_verify_url_positional_form_works():
    r = subprocess.run([_vibatchium_bin(), "verify-url", "https://example.com"],
                      capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    data = json.loads(r.stdout)
    assert data["ok"] is True


def test_verify_url_flag_form_works():
    """The form Codex tried first: `verify-url --url X`."""
    r = subprocess.run([_vibatchium_bin(), "verify-url", "--url", "https://example.com"],
                      capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    data = json.loads(r.stdout)
    assert data["ok"] is True


def test_verify_url_underscore_alias_via_argv_rewrite():
    """The other form Codex tried: `verify_url X` (MCP-style underscore)."""
    r = subprocess.run([_vibatchium_bin(), "verify_url", "https://example.com"],
                      capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    data = json.loads(r.stdout)
    assert data["ok"] is True


def test_verify_url_no_url_at_all_errors_clearly():
    r = subprocess.run([_vibatchium_bin(), "verify-url"],
                      capture_output=True, text=True, timeout=10)
    assert r.returncode != 0
    assert "URL required" in r.stderr or "URL required" in r.stdout


# ─── Bug 3: explore default no longer floods stdout with base64 ────────


def test_explore_default_writes_screenshot_to_cache(local_server, tmp_path):
    """Without -o or --inline-screenshot, CLI should write the PNG to
    a cache dir and return a screenshot_path instead of a base64 blob.
    Use a unique session so explore's auto-close doesn't affect the
    conftest-managed `default` session."""
    import os as _os
    # Unset XDG_RUNTIME_DIR so CACHE_DIR falls back to HOME/.cache —
    # otherwise the daemon-spawned-by-conftest path uses the host's
    # /run/user/UID and we can't redirect via HOME alone.
    env = {k: v for k, v in _os.environ.items() if k != "XDG_RUNTIME_DIR"}
    env["HOME"] = str(tmp_path)
    r = subprocess.run([_vibatchium_bin(),
                       "--session", "codex_friction_default_test",
                       "explore", f"{local_server}/simple.html", "--skip-verify"],
                      capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    data = json.loads(r.stdout)
    assert "screenshot_b64" not in data, \
        "default CLI should NOT inline screenshot_b64"
    assert "screenshot_path" in data
    shot = Path(data["screenshot_path"])
    assert shot.exists()
    assert shot.stat().st_size > 0
    # Path can be either XDG_RUNTIME_DIR/vibatchium/explores/ (if daemon
    # was started with XDG set) OR HOME/.cache/vibatchium/explores/ (if not).
    # Wave 7.7.10 routes through paths.CACHE_DIR which prefers XDG.
    assert "vibatchium/explores/" in str(shot)


def test_explore_inline_screenshot_flag_preserves_base64(local_server, tmp_path):
    """--inline-screenshot opts back into the old inline-base64 behavior."""
    import os as _os
    env = {**_os.environ, "HOME": str(tmp_path)}
    r = subprocess.run([_vibatchium_bin(),
                       "--session", "codex_friction_inline_test",
                       "explore", f"{local_server}/simple.html",
                       "--skip-verify", "--inline-screenshot"],
                      capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    data = json.loads(r.stdout)
    assert "screenshot_b64" in data
    assert len(data["screenshot_b64"]) > 100
    assert "screenshot_path" not in data


def test_explore_output_dir_still_writes_markdown(local_server, tmp_path):
    """Pre-existing -o behavior unchanged: writes landing.png + explore.md."""
    out = tmp_path / "scrape-out"
    r = subprocess.run([_vibatchium_bin(),
                       "--session", "codex_friction_outputdir_test",
                       "explore", f"{local_server}/simple.html",
                       "--skip-verify", "-o", str(out)],
                      capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    assert (out / "landing.png").exists()
    assert (out / "explore.md").exists()
    data = json.loads(r.stdout)
    assert "screenshot_b64" not in data
    assert data.get("markdown_path", "").endswith("explore.md")


def test_explore_auto_screenshot_skips_text_page(local_server, tmp_path):
    """0.7.0 --auto-screenshot is text-first: a text-rich page yields NO
    screenshot (no base64, no cache file) — the token-saving agent default."""
    import os as _os
    env = {**_os.environ, "HOME": str(tmp_path)}
    r = subprocess.run([_vibatchium_bin(),
                       "--session", "codex_friction_auto_test",
                       "explore", f"{local_server}/simple.html",
                       "--skip-verify", "--auto-screenshot"],
                      capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, f"stderr: {r.stderr}"
    data = json.loads(r.stdout)
    assert data.get("text")
    assert "screenshot_b64" not in data
    assert "screenshot_path" not in data
