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
    log, cache, suffix = _resolve(
        ["LOG_PATH", "CACHE_DIR", "_LOG_SUFFIX"], env, tmp_path)
    # log lives under the persistent STATE dir, NOT the volatile runtime dir
    assert log == str(state / "vibatchium" / f"daemon{suffix}.log")
    assert (state / "vibatchium").is_dir()
    assert str(runtime) in cache          # sanity: runtime IS the cache dir
    assert not log.startswith(cache)
    # this runtime dir is NOT the canonical /run/user/<uid>, so the daemon gets a
    # per-daemon suffix (0.9.3) — the filename is no longer a bare daemon.log
    assert suffix.startswith("-run-") and log.endswith(".log")


def test_log_falls_back_to_home_local_state(tmp_path):
    runtime = tmp_path / "run"; runtime.mkdir()
    log, suffix = _resolve(
        ["LOG_PATH", "_LOG_SUFFIX"], {"XDG_RUNTIME_DIR": str(runtime)}, tmp_path)
    assert log == str(
        tmp_path / ".local" / "state" / "vibatchium" / f"daemon{suffix}.log")


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
        state_dir, cache_dir, log, suffix = _resolve(
            ["STATE_DIR", "CACHE_DIR", "LOG_PATH", "_LOG_SUFFIX"], env, tmp_path)
    finally:
        state.chmod(0o700)  # let tmp_path teardown remove it
    assert state_dir == cache_dir                    # fell back to the runtime dir
    assert log == str(runtime / "vibatchium" / f"daemon{suffix}.log")


# ─── _runtime_log_suffix: per-daemon log separation (0.9.3, pure unit) ─────────
#
# The bug this closes: STATE_DIR is HOME-derived (shared) while socket/pid/lock
# are XDG_RUNTIME_DIR-derived (per-daemon). Pre-0.9.3 the primary live daemon and
# an isolated daemon (e.g. project-scouter's own runtime dir) both opened the
# SAME state-dir daemon.log with independent RotatingFileHandlers → they raced on
# the rotation rename and shredded each other's history. The suffix gives each
# daemon its own file; the primary keeps the documented bare daemon.log.

def test_log_suffix_empty_for_canonical_runtime_dir():
    """The PRIMARY daemon (default XDG_RUNTIME_DIR=/run/user/<uid>) keeps the
    documented bare `daemon.log` — no suffix — for backward-compat."""
    from vibatchium.daemon.paths import _runtime_log_suffix
    if not hasattr(os, "getuid"):
        import pytest
        pytest.skip("no getuid on this platform")
    # Equals "" whether or not /run/user/<uid> exists on this host: a present
    # canonical dir matches the primary check; an absent one hits the not-a-dir
    # short-circuit. Either way the primary daemon never gets a suffix.
    assert _runtime_log_suffix(f"/run/user/{os.getuid()}") == ""


def test_log_suffix_empty_for_unset_or_missing_runtime_dir(tmp_path):
    """No runtime dir (the ~/.cache fallback) or a non-existent one → bare."""
    from vibatchium.daemon.paths import _runtime_log_suffix
    assert _runtime_log_suffix(None) == ""
    assert _runtime_log_suffix("") == ""
    assert _runtime_log_suffix(str(tmp_path / "does-not-exist")) == ""


def test_log_suffix_isolated_runtime_dir_is_stable_and_readable(tmp_path):
    """An intentionally-isolated runtime dir (scouter's scouter-vb) gets a
    readable, deterministic, collision-safe suffix → a SEPARATE log file."""
    import re
    from vibatchium.daemon.paths import _runtime_log_suffix
    rt = tmp_path / "scouter-vb"; rt.mkdir()
    suffix = _runtime_log_suffix(str(rt))
    assert re.fullmatch(r"-scouter-vb-[0-9a-f]{8}", suffix), suffix
    # pure function of the path → identical across calls (stable filename)
    assert _runtime_log_suffix(str(rt)) == suffix


def test_log_suffix_distinct_per_runtime_dir(tmp_path):
    """The core invariant: DIFFERENT runtime dirs → DIFFERENT log files, so two
    daemons' RotatingFileHandlers can never touch the same path (no rotation
    race). Even a same-basename dir at a different path stays distinct."""
    from vibatchium.daemon.paths import _runtime_log_suffix
    a = tmp_path / "scouter-vb"; a.mkdir()
    b = tmp_path / "test-sandbox"; b.mkdir()
    sa, sb = _runtime_log_suffix(str(a)), _runtime_log_suffix(str(b))
    assert sa and sb and sa != sb
    c = tmp_path / "nest" / "scouter-vb"; c.mkdir(parents=True)
    sc = _runtime_log_suffix(str(c))
    assert sc.startswith("-scouter-vb-") and sc != sa   # same name, different hash


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
