"""0.19.1 — does an upgraded install actually REACH the agent + the daemon?

Two failure modes shipped in 0.19.0, both silent:

  1. On a git checkout (editable install) `git pull` changes the source while
     __version__ stands still, so the daemon-vs-client version compare reports
     a clean bill of health for a daemon executing old code.
  2. `vb update` refreshed the package but not the generated agent skill, so a
     new verb (`vb search`) existed that no installed agent was ever told about.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from vibatchium import cli


# ─── _newest_source_mtime ───────────────────────────────────────────────

def test_newest_source_mtime_ignores_pycache(tmp_path):
    """A .pyc is written when the daemon IMPORTS a module. Counting it would
    stamp the tree with the daemon's own boot time — every daemon fresh forever."""
    (tmp_path / "a.py").write_text("x = 1")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    pyc = cache / "a.cpython-311.py"     # .py so rglob("*.py") would catch it
    pyc.write_text("compiled")
    old = time.time() - 10_000
    import os
    os.utime(tmp_path / "a.py", (old, old))
    newest = cli._newest_source_mtime(str(tmp_path))
    assert newest == pytest.approx(old, abs=2)


def test_newest_source_mtime_none_on_missing_tree(tmp_path):
    assert cli._newest_source_mtime(str(tmp_path / "nope")) is None


# ─── _daemon_code_is_stale ──────────────────────────────────────────────

@pytest.fixture
def editable(monkeypatch):
    monkeypatch.setattr(cli, "_is_editable_install", lambda: True)


def test_stale_when_daemon_predates_source(monkeypatch, editable):
    here = str(Path(cli.__file__).resolve().parent)
    monkeypatch.setattr(cli, "_newest_source_mtime", lambda root=None: 5_000.0)
    assert cli._daemon_code_is_stale(4_000.0, here) is True


def test_fresh_when_daemon_booted_after_source(monkeypatch, editable):
    here = str(Path(cli.__file__).resolve().parent)
    monkeypatch.setattr(cli, "_newest_source_mtime", lambda root=None: 4_000.0)
    assert cli._daemon_code_is_stale(5_000.0, here) is False


def test_slack_absorbs_filesystem_granularity(monkeypatch, editable):
    """A write landing during the daemon's own import must not read as stale."""
    here = str(Path(cli.__file__).resolve().parent)
    monkeypatch.setattr(cli, "_newest_source_mtime", lambda root=None: 5_001.0)
    assert cli._daemon_code_is_stale(5_000.0, here) is False


def test_not_stale_for_released_install(monkeypatch):
    """Non-editable source is immutable after install — the version compare is
    the right signal there, and this one must stay silent."""
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_newest_source_mtime", lambda root=None: 9e9)
    assert cli._daemon_code_is_stale(1.0, None) is False


def test_not_stale_when_daemon_serves_a_different_checkout(monkeypatch, editable):
    """A daemon spawned from another venv is not answerable to OUR mtimes —
    comparing them is comparing unrelated timelines."""
    monkeypatch.setattr(cli, "_newest_source_mtime", lambda root=None: 9e9)
    assert cli._daemon_code_is_stale(1.0, "/some/other/venv/vibatchium") is False


def test_not_stale_when_daemon_too_old_to_report(monkeypatch, editable):
    """A pre-0.19.1 daemon has no started_at. Silence beats a false alarm that
    drops live sessions."""
    monkeypatch.setattr(cli, "_newest_source_mtime", lambda root=None: 9e9)
    assert cli._daemon_code_is_stale(None, None) is False


# ─── the update bounce decision ─────────────────────────────────────────

def test_editable_bounce_skipped_when_daemon_is_current(monkeypatch):
    calls: list = []
    monkeypatch.setattr(cli, "daemon_is_running", lambda: True)
    monkeypatch.setattr(cli, "call", lambda c, **k: calls.append(c) or {})
    monkeypatch.setattr(cli, "_daemon_code_is_stale", lambda *a: False)
    cli._bounce_daemon_for_update(editable=True)
    assert "shutdown" not in calls


def test_editable_bounce_fires_when_daemon_is_stale(monkeypatch):
    calls: list = []
    monkeypatch.setattr(cli, "daemon_is_running", lambda: True)
    monkeypatch.setattr(cli, "call", lambda c, **k: calls.append(c) or {})
    monkeypatch.setattr(cli, "_daemon_code_is_stale", lambda *a: True)
    cli._bounce_daemon_for_update(editable=True)
    assert "shutdown" in calls


def test_released_install_always_bounces(monkeypatch):
    """Unchanged pre-0.19.1 behaviour: a real install replaced the code, so the
    running daemon is stale by construction."""
    calls: list = []
    monkeypatch.setattr(cli, "daemon_is_running", lambda: True)
    monkeypatch.setattr(cli, "call", lambda c, **k: calls.append(c) or {})
    cli._bounce_daemon_for_update(editable=False)
    assert calls == ["shutdown"]


def test_bounce_is_noop_when_daemon_down(monkeypatch):
    monkeypatch.setattr(cli, "daemon_is_running", lambda: False)
    monkeypatch.setattr(cli, "call",
                        lambda *a, **k: pytest.fail("must not call a dead daemon"))
    cli._bounce_daemon_for_update(editable=True)


def test_bounce_survives_a_wedged_daemon(monkeypatch):
    """`vb update` already upgraded the package by this point — a daemon that
    won't answer must not make the whole command look like it failed."""
    monkeypatch.setattr(cli, "daemon_is_running", lambda: True)
    def _boom(*a, **k):
        raise OSError("socket wedged")
    monkeypatch.setattr(cli, "call", _boom)
    cli._bounce_daemon_for_update(editable=False)   # must not raise


# ─── the agent refresh ──────────────────────────────────────────────────

def test_refresh_never_raises_when_setup_fails(monkeypatch):
    import vibatchium.setup_cmd as sc
    monkeypatch.setattr(sc, "run_setup",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no home")))
    cli._refresh_agent_integration()   # must not raise


def test_refresh_surfaces_caps_drift(monkeypatch, capsys):
    import vibatchium.setup_cmd as sc
    monkeypatch.setattr(sc, "run_setup", lambda *a, **k: {"results": [
        {"agent": "claude", "docs": "unchanged", "skill": "unchanged",
         "notes": ["registered with `--caps lean`; this version installs "
                   "`--caps lean,search`. Re-register ... `vb setup --force`"]},
    ]})
    cli._refresh_agent_integration()
    err = capsys.readouterr().err
    assert "claude" in err and "--caps" in err
