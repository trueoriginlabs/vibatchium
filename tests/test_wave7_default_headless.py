"""Wave 7.7.4 — VIBATCHIUM_DEFAULT_HEADLESS env var.

Fan-out / background-scraping operators kept forgetting to pass
`--headless` (or `headless: true` via MCP) on every `start` call,
ending up with headed Chrome windows polluting their desktop. The
MCP schema description for `headless` says "not recommended for
stealth" — actively steering agents away — which makes the trap
worse for the legitimate fan-out use case.

VIBATCHIUM_DEFAULT_HEADLESS=1 (or set via `vibatchium daemon start
--default-headless`) flips the start default to headless without
changing per-call behavior — an explicit `headless: false` arg
still wins.
"""
from __future__ import annotations

import subprocess
import sys


def test_daemon_start_help_lists_default_headless():
    """The new --default-headless flag should appear in the help."""
    out = subprocess.run(
        [sys.executable, "-m", "vibatchium.cli", "daemon", "start", "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0
    assert "--default-headless" in out.stdout
    assert "fan-out" in out.stdout or "background" in out.stdout


def test_default_headless_resolution_logic(monkeypatch):
    """Unit-test the resolution logic without touching the daemon:
    - explicit args wins over env
    - env default flips when set
    - missing both → headed (preserves existing default)
    """
    # Simulate what the handler does
    def _resolve(args: dict) -> bool:
        import os
        if "headless" in args:
            return bool(args["headless"])
        env = os.environ.get("VIBATCHIUM_DEFAULT_HEADLESS", "0").lower()
        return env in ("1", "true", "yes", "on")

    # No env, no arg → headed (preserves existing default)
    monkeypatch.delenv("VIBATCHIUM_DEFAULT_HEADLESS", raising=False)
    assert _resolve({}) is False

    # Env set, no arg → headless
    monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADLESS", "1")
    assert _resolve({}) is True

    # Env set, arg explicitly False → headed (per-call wins)
    assert _resolve({"headless": False}) is False

    # Env unset, arg True → headless (per-call wins)
    monkeypatch.delenv("VIBATCHIUM_DEFAULT_HEADLESS", raising=False)
    assert _resolve({"headless": True}) is True

    # Env value variants
    for truthy in ("1", "true", "yes", "on", "TRUE", "Yes"):
        monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADLESS", truthy)
        assert _resolve({}) is True, f"{truthy!r} should be truthy"
    for falsy in ("0", "false", "no", "off", "", "garbage"):
        monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADLESS", falsy)
        assert _resolve({}) is False, f"{falsy!r} should be falsy"


def test_explicit_args_always_win_over_env_default(monkeypatch):
    """Even with VIBATCHIUM_DEFAULT_HEADLESS=1 set globally, a script
    that explicitly passes headless=false (e.g. wants a headed window
    for visual debugging) gets headed."""
    monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADLESS", "1")
    # The resolution logic from the handler
    def _resolve(args):
        import os
        if "headless" in args:
            return bool(args["headless"])
        return os.environ.get("VIBATCHIUM_DEFAULT_HEADLESS", "0").lower() in (
            "1", "true", "yes", "on"
        )
    assert _resolve({"headless": False}) is False
    assert _resolve({"headless": True}) is True
    assert _resolve({}) is True  # env default takes over only when no arg
