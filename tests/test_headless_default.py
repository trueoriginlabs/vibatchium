"""0.6.4: headless-by-default for the daemon + CLI.

A background daemon owns no display, so programmatic callers (plugins, research
fan-out, the xscraper reader) default headless; only an interactive human TTY
running `vb start` gets a visible window. Env knobs and explicit flags override.
"""
from __future__ import annotations

import pytest

from vibatchium import cli
from vibatchium.daemon.handlers import resolve_headless


# ─── daemon-side resolver (governs plugins / research / direct start) ───────

def test_daemon_default_is_headless(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_DEFAULT_HEADLESS", raising=False)
    monkeypatch.delenv("VIBATCHIUM_DEFAULT_HEADED", raising=False)
    assert resolve_headless({}) is True                      # no args, no env


def test_daemon_explicit_arg_wins(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADLESS", "1")
    assert resolve_headless({"headless": False}) is False     # arg beats env
    monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADED", "1")
    assert resolve_headless({"headless": True}) is True


def test_daemon_env_headed_opts_out(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_DEFAULT_HEADLESS", raising=False)
    monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADED", "1")
    assert resolve_headless({}) is False


def test_daemon_env_headless_forces(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADLESS", "1")
    monkeypatch.delenv("VIBATCHIUM_DEFAULT_HEADED", raising=False)
    assert resolve_headless({}) is True


# ─── CLI-side resolver (governs `vb start`) ─────────────────────────────────

@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_DEFAULT_HEADLESS", raising=False)
    monkeypatch.delenv("VIBATCHIUM_DEFAULT_HEADED", raising=False)


def test_cli_agent_no_tty_is_headless():
    assert cli._cli_resolve_headless(None, isatty=False) is True


def test_cli_human_tty_is_headed():
    assert cli._cli_resolve_headless(None, isatty=True) is False


def test_cli_explicit_headed_wins_no_tty():
    assert cli._cli_resolve_headless(False, isatty=False) is False


def test_cli_explicit_headless_wins_tty():
    assert cli._cli_resolve_headless(True, isatty=True) is True


def test_cli_env_headless_overrides_tty(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADLESS", "1")
    assert cli._cli_resolve_headless(None, isatty=True) is True   # even at a TTY


def test_cli_env_headed_overrides_no_tty(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_DEFAULT_HEADED", "1")
    assert cli._cli_resolve_headless(None, isatty=False) is False
