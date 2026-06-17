"""0.9.1 — daemon singleton (flock) + idle reaper + `vb daemon list`.

These guard the daemon-leak fix: a non-isolated `vb` call used to spawn a second
daemon that could supersede and orphan the first (each parenting Chromes), until
the box OOM-thrashed. The flock singleton makes that structurally impossible.

All tests are isolated — the singleton/list tests use the conftest daemon in its
isolated XDG runtime dir; the reaper test spins a daemon in a FRESH temp runtime
dir — so nothing here can touch a real/live daemon.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


# ─── is_idle() — pure, no Chrome ──────────────────────────────────────────
def test_registry_is_idle_pure():
    from vibatchium.daemon.registry import SessionRegistry
    r = SessionRegistry()
    assert r.is_idle() is True
    r._entries["x"] = object()              # a live session → not idle
    assert r.is_idle() is False
    r._entries.clear()
    r._warm_sessions["y"] = object()        # a warm-pooled session → not idle
    assert r.is_idle() is False
    r._warm_sessions.clear()

    class _FakeTask:                        # an in-flight warm task → not idle
        def __init__(self, is_done):
            self._done = is_done

        def done(self):
            return self._done

    r._warm_tasks["z"] = _FakeTask(False)
    assert r.is_idle() is False
    r._warm_tasks["z"] = _FakeTask(True)    # finished task → idle again
    assert r.is_idle() is True


# ─── flock singleton ──────────────────────────────────────────────────────
def test_second_daemon_refuses_to_start(local_server):
    """The conftest daemon already owns this isolated XDG's lock+socket; a second
    `python -m vibatchium.daemon.server` must exit cleanly (rc=2), never bind a
    second socket (which is what orphaned daemons under the old code)."""
    proc = subprocess.run(
        [sys.executable, "-m", "vibatchium.daemon.server"],
        env=os.environ.copy(), capture_output=True, text=True, timeout=25,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stderr[-600:])
    # must be stopped by the FLOCK ("already holds {lock}"), not only the
    # socket-serving fallback ("already serving") — the lock is what we're testing.
    assert "holds" in proc.stderr.lower(), proc.stderr[-600:]


# ─── vb daemon list (read-only) ───────────────────────────────────────────
def test_daemon_list_reports_live(local_server):
    from click.testing import CliRunner
    from vibatchium import cli as cli_mod
    res = CliRunner().invoke(cli_mod.cli, ["--json", "daemon", "list"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["live_pid"] is not None
    assert any(d["live"] for d in data["daemons"]), data


# ─── idle reaper ──────────────────────────────────────────────────────────
def test_idle_reaper_self_shuts_down(tmp_path):
    """A daemon in a FRESH runtime dir with a short idle timeout and no sessions
    must self-exit cleanly. Isolated, so it can't reach any other daemon."""
    rt = tmp_path / "rt"
    home = tmp_path / "home"
    rt.mkdir()
    home.mkdir()
    env = os.environ.copy()
    env.update({
        "XDG_RUNTIME_DIR": str(rt),
        "HOME": str(home),
        "VIBATCHIUM_WARM": "off",
        "VIBATCHIUM_DAEMON_IDLE_TIMEOUT": "2",
        "VIBATCHIUM_PLUGINS": "0",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "vibatchium.daemon.server"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        # reaper polls on a ~5s floor; idle_for >= 2s ⇒ self-shutdown on first poll
        rc = proc.wait(timeout=25)
        assert rc == 0, f"expected clean idle self-shutdown, got rc={rc}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_idle_reaper_disabled_by_default(tmp_path):
    """With the timeout unset (the default), the reaper must NOT fire — a daemon
    stays up so long-lived bot daemons are never surprise-killed."""
    rt = tmp_path / "rt2"
    home = tmp_path / "home2"
    rt.mkdir()
    home.mkdir()
    env = os.environ.copy()
    env.update({
        "XDG_RUNTIME_DIR": str(rt),
        "HOME": str(home),
        "VIBATCHIUM_WARM": "off",
        "VIBATCHIUM_PLUGINS": "0",
    })
    env.pop("VIBATCHIUM_DAEMON_IDLE_TIMEOUT", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "vibatchium.daemon.server"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            proc.wait(timeout=8)            # must still be alive — no reaping
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
