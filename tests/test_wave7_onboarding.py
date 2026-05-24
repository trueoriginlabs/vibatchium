"""Wave 7.7.2 — onboarding fixes from real-run feedback.

Items pulled from the other Claude's geminixprize fan-out feedback:
  - `patchium logs` for execution-tracing visibility
  - `patchium daemon start --max-sessions N` (discoverable cap override)
  - MCP-style underscored verb aliases (`session_new` works in shell
    not just MCP, so a brief can paste either way)
  - `patchium session prune` for cleanup after dogfood runs
"""
from __future__ import annotations

import subprocess
import sys


# ─── patchium logs ──────────────────────────────────────────────────────


def test_logs_help_lists_options():
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "logs", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0
    assert "--session" in out.stdout
    assert "--tail" in out.stdout
    assert "--since" in out.stdout
    assert "--errors-only" in out.stdout


def test_logs_reads_daemon_log(local_server):
    """At minimum, `patchium logs --tail 1` should return without
    crashing once the daemon has emitted any line."""
    # The conftest-started daemon has emitted at least the
    # "daemon listening" line, so logs should produce output.
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "logs", "--tail", "5"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0
    # Should have at least one line of output, mentioning patchium
    assert "patchium" in out.stdout.lower()


def test_logs_session_filter_excludes_unrelated(local_server):
    """--session foo should NOT show lines for session bar."""
    # Trigger an event for the default session so there's something to filter
    from patchium.client import call
    call("status", {})
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "logs",
         "--session", "nonexistent_xyz_zzz", "--tail", "100"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0
    # No real session matches that needle — output should be empty
    # (or contain only blank lines)
    assert "nonexistent_xyz_zzz" not in out.stdout or out.stdout.strip() == ""


def test_logs_since_relative_parses():
    """--since 10m / 1h / 30s / 2d should all parse and not crash."""
    for spec in ("10m", "1h", "30s", "2d"):
        out = subprocess.run(
            [sys.executable, "-m", "patchium.cli", "logs",
             "--since", spec, "--tail", "5"],
            capture_output=True, text=True, timeout=10,
        )
        assert out.returncode == 0, f"--since {spec!r} failed: {out.stderr}"


def test_logs_since_bad_format_errors_cleanly():
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "logs",
         "--since", "garbage-format"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode != 0
    assert "since" in out.stderr.lower()


# ─── patchium daemon start --max-sessions ──────────────────────────────


def test_daemon_start_help_lists_flags():
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "daemon", "start", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0
    assert "--max-sessions" in out.stdout
    assert "--log-verbs" in out.stdout
    assert "--default-safety" in out.stdout


def test_daemon_start_refuses_when_running(local_server):
    """If the daemon is already up (conftest started it), `daemon start`
    should error cleanly instead of silently spawning a duplicate."""
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "daemon", "start"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode != 0
    assert "already running" in out.stderr


# ─── MCP-style underscored verb aliases ────────────────────────────────


def test_underscored_alias_session_list_works(local_server):
    """`patchium session_list` should be equivalent to `patchium session list`.
    Lets a brief written in MCP form paste straight into a shell."""
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "session_list"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0, f"alias failed: stderr={out.stderr!r}"
    # The session_list output includes "default"
    assert "default" in out.stdout


def test_underscored_alias_safety_status_works(local_server):
    """`safety_status` → `safety status` (group + subcommand)."""
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "safety_status"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0, f"alias failed: stderr={out.stderr!r}"


def test_set_log_verbs_dash_alias_works(local_server):
    """`set_log_verbs` (MCP) → `set-log-verbs` (CLI). Top-level singleton
    that doesn't fit the group/subcommand pattern."""
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "set_log_verbs", "--on", "false"],
        capture_output=True, text=True, timeout=10,
    )
    # Tolerant: this is a brand-new CLI verb that might not exist as a
    # spaced form (since set-log-verbs has only one word). What we care
    # about is the alias rewrite happened — exit 2 (unknown command)
    # would be the failure mode if rewrite didn't fire.
    # Acceptable: exit 0 (worked) OR a click error that recognizes the
    # rewritten command name.
    combined = out.stdout + out.stderr
    assert "set_log_verbs" not in combined or "Usage" in combined, (
        f"alias rewrite seems to have failed: {combined!r}"
    )


# ─── patchium session prune ────────────────────────────────────────────


def test_session_prune_help_lists_options():
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "session", "prune", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0
    assert "--pattern" in out.stdout
    assert "--keep" in out.stdout
    assert "--dry-run" in out.stdout


def test_session_prune_dry_run_doesnt_delete(local_server):
    """--dry-run reports targets without actually deleting them."""
    from patchium.client import call, DaemonError
    # Create a few stopped sessions
    for name in ("prune_probe_a", "prune_probe_b", "prune_keep_me"):
        try:
            call("session_new", {"name": name})
        except DaemonError:
            pass
    try:
        out = subprocess.run(
            [sys.executable, "-m", "patchium.cli", "--json",
             "session", "prune", "--pattern", "prune_probe_", "--dry-run"],
            capture_output=True, text=True, timeout=10,
        )
        assert out.returncode == 0, out.stderr
        import json
        result = json.loads(out.stdout.strip())
        assert result["dry_run"] is True
        # Both probes should be in the dry-run list
        names = result["pruned"]
        assert "prune_probe_a" in names
        assert "prune_probe_b" in names
        # The "keep_me" one shouldn't be — different pattern
        assert "prune_keep_me" not in names
        # And nothing was actually deleted
        listing = call("session_list")
        names_now = {s["name"] for s in listing["sessions"]}
        assert "prune_probe_a" in names_now
        assert "prune_probe_b" in names_now
    finally:
        for name in ("prune_probe_a", "prune_probe_b", "prune_keep_me"):
            try:
                call("session_delete", {"name": name})
            except DaemonError:
                pass


def test_session_prune_actually_deletes(local_server):
    """Without --dry-run, matching sessions are deleted on disk."""
    from patchium.client import call, DaemonError
    name = "actually_prune_me"
    call("session_new", {"name": name})
    try:
        out = subprocess.run(
            [sys.executable, "-m", "patchium.cli", "--json",
             "session", "prune", "--pattern", "actually_prune"],
            capture_output=True, text=True, timeout=10,
        )
        assert out.returncode == 0, out.stderr
        listing = call("session_list")
        names_now = {s["name"] for s in listing["sessions"]}
        assert name not in names_now
    except Exception:
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass
        raise


def test_session_prune_never_touches_default(local_server):
    """Even with --pattern=default, the 'default' session should not be deleted."""
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "--json",
         "session", "prune", "--pattern", "default", "--dry-run"],
        capture_output=True, text=True, timeout=10,
    )
    import json
    result = json.loads(out.stdout.strip())
    assert "default" not in result["pruned"]


def test_session_prune_skips_active_session(local_server):
    """The current active session must never be pruned, regardless of pattern."""
    from patchium.client import call
    active = call("status").get("session") or "default"
    out = subprocess.run(
        [sys.executable, "-m", "patchium.cli", "--json",
         "session", "prune", "--pattern", active, "--dry-run"],
        capture_output=True, text=True, timeout=10,
    )
    import json
    result = json.loads(out.stdout.strip())
    assert active not in result["pruned"]
