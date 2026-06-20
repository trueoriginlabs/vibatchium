"""0.9.2 — the daemon log is PERSISTENT (a state dir, not the volatile runtime
dir), bounded (RotatingFileHandler), overridable, and fail-safe.

Path-resolution cases run in a FRESH interpreter with a hermetic env (HOME
isolated to tmp_path AND the log knobs cleared) so paths.py — which computes the
path at import — never reads the real box's env or touches the real ~/.local/state.
The handler cases unit-test _SecureRotatingFileHandler directly (no daemon/browser).
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys


def _clean_env(home, env_extra: dict) -> dict:
    """Parent env + isolated HOME, with every log knob CLEARED so each case
    starts hermetic and only env_extra is active (a host that exports
    XDG_STATE_HOME / VIBATCHIUM_LOG_FILE must not leak in)."""
    env = {**os.environ, "HOME": str(home)}
    for k in ("XDG_STATE_HOME", "VIBATCHIUM_LOG_FILE",
              "VIBATCHIUM_LOG_MAX_BYTES", "VIBATCHIUM_LOG_BACKUPS"):
        env.pop(k, None)
    env.update(env_extra)
    return env


def _resolve(attrs: list[str], env_extra: dict, home) -> list[str]:
    code = "from vibatchium.daemon import paths\n" + \
        "\n".join(f"print(paths.{a})" for a in attrs)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=_clean_env(home, env_extra), check=True,
    )
    return out.stdout.strip().splitlines()


def test_log_defaults_to_persistent_state_dir(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    runtime = tmp_path / "run"; runtime.mkdir()
    env = {"XDG_STATE_HOME": str(state), "XDG_RUNTIME_DIR": str(runtime)}
    log, cache = _resolve(["LOG_PATH", "CACHE_DIR"], env, tmp_path)
    # log lives under the persistent STATE dir, NOT the volatile runtime dir
    assert log == str(state / "vibatchium" / "daemon.log")
    assert str(runtime) in cache          # sanity: runtime IS the cache dir
    assert not log.startswith(cache)
    assert (state / "vibatchium").is_dir()


def test_log_falls_back_to_home_local_state(tmp_path):
    runtime = tmp_path / "run"; runtime.mkdir()
    (log,) = _resolve(["LOG_PATH"], {"XDG_RUNTIME_DIR": str(runtime)}, tmp_path)
    assert log == str(tmp_path / ".local" / "state" / "vibatchium" / "daemon.log")


def test_log_file_override_wins_and_creates_parent(tmp_path):
    custom = tmp_path / "logs" / "custom-daemon.log"
    (log,) = _resolve(["LOG_PATH"], {"VIBATCHIUM_LOG_FILE": str(custom)}, tmp_path)
    assert log == str(custom)
    assert custom.parent.is_dir()


def test_unwritable_state_dir_falls_back_to_cache(tmp_path):
    """The fail-safe: if the persistent state dir can't be created, LOG_PATH
    falls back to the volatile runtime CACHE_DIR so the daemon STILL starts
    (pre-0.9.2 behaviour) instead of crashing the import."""
    if os.geteuid() == 0:
        import pytest
        pytest.skip("root bypasses directory permissions")
    state = tmp_path / "ro-state"; state.mkdir(); state.chmod(0o500)  # read-only
    runtime = tmp_path / "run"; runtime.mkdir()
    env = {"XDG_STATE_HOME": str(state), "XDG_RUNTIME_DIR": str(runtime)}
    try:
        state_dir, cache_dir, log = _resolve(
            ["STATE_DIR", "CACHE_DIR", "LOG_PATH"], env, tmp_path)
    finally:
        state.chmod(0o700)  # let tmp_path teardown remove it
    assert state_dir == cache_dir                    # fell back to the runtime dir
    assert log == str(runtime / "vibatchium" / "daemon.log")


# ─── _SecureRotatingFileHandler: rotation + 0600 + maxBytes=0 (pure unit) ──────

def test_secure_rotating_handler_rotates_and_chmods_backups(tmp_path):
    """Rotation produces a .1 backup, and BOTH the active log and the rotated
    backup are 0600 even under a permissive umask (the leak class the secure
    handler exists to prevent)."""
    import logging
    from vibatchium.daemon.server import _SecureRotatingFileHandler
    logp = tmp_path / "d.log"
    old_umask = os.umask(0o022)
    try:
        h = _SecureRotatingFileHandler(str(logp), maxBytes=200, backupCount=2)
        lg = logging.getLogger("vb-test-rotate"); lg.setLevel(logging.INFO)
        lg.addHandler(h)
        try:
            for i in range(200):
                lg.info("%s line %d", "x" * 50, i)
        finally:
            lg.removeHandler(h); h.close()
    finally:
        os.umask(old_umask)
    backup = tmp_path / "d.log.1"
    assert backup.exists(), "expected a rotated backup"
    for f in (logp, backup):
        mode = stat.S_IMODE(f.stat().st_mode)
        assert mode == 0o600, f"{f} is 0o{mode:03o}, expected 0o600"


def test_secure_rotating_handler_maxbytes_zero_never_rotates(tmp_path):
    """maxBytes=0 is the documented 'never rotate' (append-only) contract."""
    import logging
    from vibatchium.daemon.server import _SecureRotatingFileHandler
    logp = tmp_path / "d.log"
    h = _SecureRotatingFileHandler(str(logp), maxBytes=0, backupCount=2)
    lg = logging.getLogger("vb-test-norotate"); lg.setLevel(logging.INFO)
    lg.addHandler(h)
    try:
        for i in range(500):
            lg.info("%s %d", "y" * 80, i)
    finally:
        lg.removeHandler(h); h.close()
    assert not (tmp_path / "d.log.1").exists()
