"""Wave 7.8 — profile-dir bloat prevention.

Two mechanisms, tested at the unit + integration level:

  1. `vb start --ephemeral` — the session's profile dir is removed when the
     session closes, so one-shot work leaves no cookies/login state on disk.
     The 'default' profile is never deleted.

  2. `vb session prune --older-than <dur>` — prune only profiles that have been
     idle (by on-disk mtime) at least the given duration, so a sweep reclaims
     stale per-run profiles without touching anything used recently.

Background: profiles are persistent by design; close() never deleted them, so
any caller that minted a fresh session name per run leaked a profile forever.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest


# ─── unit: duration parser (`--older-than`) ──────────────────────────────


def test_parse_age_seconds_units():
    from vibatchium.cli import _parse_age_seconds
    assert _parse_age_seconds("90s") == 90
    assert _parse_age_seconds("30m") == 1800
    assert _parse_age_seconds("12h") == 43200
    assert _parse_age_seconds("7d") == 604800
    assert _parse_age_seconds("2w") == 1209600
    # bare number = seconds
    assert _parse_age_seconds("3600") == 3600


def test_parse_age_seconds_rejects_garbage():
    import click
    from vibatchium.cli import _parse_age_seconds
    for bad in ("banana", "", "-5d", "5x", "1.5d", "d"):
        with pytest.raises(click.BadParameter):
            _parse_age_seconds(bad)


# ─── unit: profile last-active probe ─────────────────────────────────────


def test_profile_last_active_tracks_newest_child(tmp_path):
    from vibatchium.daemon.registry import _profile_last_active
    d = tmp_path / "p"
    d.mkdir()
    base = _profile_last_active(d)
    assert base is not None
    # A child newer than the dir wins.
    child = d / "Cookies"
    child.write_text("x")
    future = time.time() + 1000
    os.utime(child, (future, future))
    assert _profile_last_active(d) >= future - 1


def test_profile_last_active_missing_path_is_none(tmp_path):
    from vibatchium.daemon.registry import _profile_last_active
    assert _profile_last_active(tmp_path / "does-not-exist") is None


# ─── unit: ephemeral close behavior (registry, no real Chrome) ────────────


async def _make_registry_with_entry(tmp_path, monkeypatch, *, name, ephemeral,
                                    profile_dir=None):
    """Build a SessionRegistry holding one entry whose Chrome is stubbed out,
    so we can exercise close()'s ephemeral branch without launching a browser.

    PROFILES_DIR is redirected to a sandbox so the containment guard treats
    profiles created under it as in-tree (deletable). Pass an explicit
    profile_dir OUTSIDE that sandbox to test the out-of-tree refusal."""
    from vibatchium.daemon import backends, registry as regmod
    from vibatchium.daemon.registry import SessionRegistry, SessionEntry

    async def _noop_close(session):  # stub backends.close
        return None

    monkeypatch.setattr(backends, "close", _noop_close)
    profiles_root = tmp_path / "profiles"
    profiles_root.mkdir(exist_ok=True)
    monkeypatch.setattr(regmod, "PROFILES_DIR", profiles_root)
    pdir = profile_dir if profile_dir is not None else profiles_root / name
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "Cookies").write_text("secret-login-state")
    reg = SessionRegistry()
    entry = SessionEntry(name=name, profile_dir=pdir, session=object(),
                         ephemeral=ephemeral)
    reg._entries[name] = entry
    return reg, pdir


async def test_ephemeral_close_deletes_profile(tmp_path, monkeypatch):
    reg, pdir = await _make_registry_with_entry(
        tmp_path, monkeypatch, name="ephx", ephemeral=True)
    closed = await reg.close("ephx")
    assert closed is True
    assert not pdir.exists(), "ephemeral profile dir should be gone after close"


async def test_nonephemeral_close_keeps_profile(tmp_path, monkeypatch):
    reg, pdir = await _make_registry_with_entry(
        tmp_path, monkeypatch, name="keepx", ephemeral=False)
    closed = await reg.close("keepx")
    assert closed is True
    assert pdir.exists(), "non-ephemeral profile dir must be preserved on close"


async def test_ephemeral_never_deletes_default(tmp_path, monkeypatch):
    # Even if the default session is somehow flagged ephemeral, its dir survives.
    reg, pdir = await _make_registry_with_entry(
        tmp_path, monkeypatch, name="default", ephemeral=True)
    closed = await reg.close("default")
    assert closed is True
    assert pdir.exists(), "'default' profile must never be deleted"


async def test_ephemeral_refuses_out_of_tree_profile(tmp_path, monkeypatch):
    """SECURITY: an ephemeral session whose profile dir is OUTSIDE PROFILES_DIR
    (e.g. `start --session work --profile /home/me/Documents --ephemeral`) must
    NOT be rmtree'd on close — the guard validates the path, not just the name."""
    outside = tmp_path / "important_data"  # not under tmp_path/profiles
    reg, pdir = await _make_registry_with_entry(
        tmp_path, monkeypatch, name="work", ephemeral=True, profile_dir=outside)
    closed = await reg.close("work")
    assert closed is True
    assert pdir.exists(), "out-of-tree profile must NOT be deleted"


async def test_goal_ownership_clears_ephemeral_flag(tmp_path):
    """A session flagged ephemeral that becomes goal-owned must have the flag
    cleared — its checkpoints live in the profile dir, so a goal needs it to
    survive close/resume rather than be deleted."""
    from vibatchium.daemon.server import Daemon
    from vibatchium.daemon.registry import SessionEntry
    from vibatchium.goals.handlers import _make_caps_cb
    d = Daemon()
    entry = SessionEntry(name="goalsess", profile_dir=tmp_path / "goalsess",
                         session=object(), ephemeral=True)
    d.registry._entries["goalsess"] = entry
    caps_cb = _make_caps_cb(d)
    caps_cb("goalsess", None)  # goal claims the session (no caps pinned)
    assert entry.ephemeral is False, "goal ownership must clear the ephemeral flag"


async def test_warming_names_includes_parked_and_inflight():
    """clean must treat pre-warming Chromes as in-use (they hold a live lock)
    even though they're not in the registry's _entries."""
    import asyncio
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    reg._warm_sessions["parked"] = object()

    async def _never():
        await asyncio.sleep(60)

    t = asyncio.ensure_future(_never())
    reg._warm_tasks["inflight"] = t
    try:
        names = reg.warming_names()
        assert "parked" in names
        assert "inflight" in names
    finally:
        t.cancel()


# ─── integration: `vb start --ephemeral` end-to-end ──────────────────────


def _run_cli(*argv, timeout=60):
    return subprocess.run(
        [sys.executable, "-m", "vibatchium.cli", *argv],
        capture_output=True, text=True, timeout=timeout,
    )


def test_start_help_lists_ephemeral():
    out = _run_cli("start", "--help", timeout=10)
    assert out.returncode == 0
    assert "--ephemeral" in out.stdout


def test_start_ephemeral_deletes_profile_on_close(local_server):
    """A real headless session started with --ephemeral has its profile dir
    removed when it closes."""
    from vibatchium.client import call, DaemonError
    from vibatchium.daemon.paths import PROFILES_DIR
    name = "eph_probe_close"
    pdir = PROFILES_DIR / name
    # clean slate
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    try:
        call("session_delete", {"name": name})
    except DaemonError:
        pass
    try:
        out = _run_cli("--json", "--session", name, "start",
                       "--ephemeral", "--headless")
        assert out.returncode == 0, out.stderr
        res = json.loads(out.stdout)
        assert res.get("ephemeral") is True
        assert pdir.exists(), "profile dir should exist while the session runs"
        # Closing the ephemeral session deletes the dir synchronously.
        call("session_close", {"name": name})
        assert not pdir.exists(), "ephemeral profile should be deleted on close"
    finally:
        # Belt-and-suspenders: make sure nothing is left running or on disk.
        try:
            call("session_close", {"name": name})
        except DaemonError:
            pass
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_start_without_ephemeral_keeps_profile(local_server):
    """Control: a normal session's profile dir survives close()."""
    from vibatchium.client import call, DaemonError
    from vibatchium.daemon.paths import PROFILES_DIR
    name = "noneph_probe_close"
    pdir = PROFILES_DIR / name
    try:
        out = _run_cli("--json", "--session", name, "start", "--headless")
        assert out.returncode == 0, out.stderr
        res = json.loads(out.stdout)
        assert res.get("ephemeral") is False
        call("session_close", {"name": name})
        assert pdir.exists(), "non-ephemeral profile must persist after close"
    finally:
        try:
            call("session_close", {"name": name})
        except DaemonError:
            pass
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


# ─── integration: `vb session prune --older-than` ────────────────────────


def test_prune_help_lists_older_than():
    out = _run_cli("session", "prune", "--help", timeout=10)
    assert out.returncode == 0
    assert "--older-than" in out.stdout


def test_prune_older_than_skips_fresh_profile(local_server):
    """A just-created profile is newer than the cutoff, so it's not pruned."""
    from vibatchium.client import call, DaemonError
    name = "fresh_age_probe"
    call("session_new", {"name": name})
    try:
        out = _run_cli("--json", "session", "prune",
                       "--pattern", name, "--older-than", "1d", "--dry-run")
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout)
        assert name not in result["pruned"], "fresh profile must be skipped"
    finally:
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_prune_older_than_includes_stale_profile(local_server):
    """Backdating a profile's mtime past the cutoff makes it prunable."""
    from vibatchium.client import call, DaemonError
    from vibatchium.daemon.paths import PROFILES_DIR
    name = "stale_age_probe"
    call("session_new", {"name": name})
    pdir = PROFILES_DIR / name
    old = time.time() - 10 * 86400  # 10 days ago
    os.utime(pdir, (old, old))
    try:
        out = _run_cli("--json", "session", "prune",
                       "--pattern", name, "--older-than", "1d", "--dry-run")
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout)
        assert name in result["pruned"], "stale profile should be pruned"
    finally:
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_prune_older_than_rejects_bad_duration(local_server):
    out = _run_cli("--json", "session", "prune",
                   "--older-than", "banana", "--dry-run", timeout=10)
    assert out.returncode != 0
    assert "older-than" in (out.stdout + out.stderr).lower()


def test_prune_keep_protects_named_session(local_server):
    """--keep excludes a session that would otherwise match the pattern."""
    from vibatchium.client import call, DaemonError
    a, b = "keeptest_drop", "keeptest_save"
    for n in (a, b):
        try:
            call("session_new", {"name": n})
        except DaemonError:
            pass
    try:
        out = _run_cli("--json", "session", "prune", "--pattern", "keeptest_",
                       "--keep", b, "--dry-run")
        assert out.returncode == 0, out.stderr
        pruned = json.loads(out.stdout)["pruned"]
        assert a in pruned          # matches pattern, not kept
        assert b not in pruned      # protected by --keep
    finally:
        for n in (a, b):
            try:
                call("session_delete", {"name": n})
            except DaemonError:
                pass


# ─── `vb clean` housekeeping ─────────────────────────────────────────────


def test_clean_help_lists_categories():
    out = _run_cli("clean", "--help", timeout=10)
    assert out.returncode == 0
    for opt in ("--apply", "--older-than", "--no-profiles", "--no-locks",
                "--no-cache", "--no-logs"):
        assert opt in out.stdout


def test_clean_dry_run_reports_without_deleting(local_server):
    """A stale profile shows up in the dry-run report but is NOT deleted."""
    from vibatchium.client import call, DaemonError
    from vibatchium.daemon.paths import PROFILES_DIR
    name = "clean_stale_probe"
    call("session_new", {"name": name})
    pdir = PROFILES_DIR / name
    old = time.time() - 30 * 86400
    os.utime(pdir, (old, old))
    try:
        out = _run_cli("--json", "clean", "--older-than", "7d")  # no --apply
        assert out.returncode == 0, out.stderr
        report = json.loads(out.stdout)
        assert report["dry_run"] is True
        assert name in report["categories"]["profiles"]["names"]
        # Still present on disk — dry-run must not delete.
        assert pdir.exists()
    finally:
        try:
            call("session_delete", {"name": name})
        except DaemonError:
            pass


def test_clean_apply_json_requires_yes(local_server):
    """--apply in --json mode without -y is refused (no interactive confirm)."""
    out = _run_cli("--json", "clean", "--apply", timeout=15)
    assert out.returncode != 0
    assert "yes" in (out.stdout + out.stderr).lower()


def test_clean_is_in_session_cap_bucket():
    """`clean` must be reachable under the `session` cap (so cap-gated REST /
    Goals can use it) and denied otherwise."""
    from vibatchium.caps import verb_in_caps, resolve_caps
    assert verb_in_caps("clean", resolve_caps("session"))
    assert not verb_in_caps("clean", resolve_caps("nav"))


# ─── `vb clean` guards — in-process, SANDBOXED PROFILES_DIR ───────────────
#
# These build a real Daemon in-process and redirect PROFILES_DIR/CACHE_DIR/
# LOG_PATH into a tmp sandbox, so `clean --apply` deletes ONLY test dirs (the
# CLI integration path has no --pattern, so applying it against the real store
# would touch the user's actual profiles). Sandboxing also lets us control which
# sessions are running / warming / active, so each safety guard is exercised
# INDEPENDENTLY — a regression that removed a guard makes one of these fail.


def _inprocess_daemon(tmp_path, monkeypatch, *, active="default"):
    """A Daemon() whose profile/cache/log paths live under tmp_path. Returns
    (daemon, profiles_dir, cache_dir)."""
    from vibatchium.daemon import paths, registry as regmod, handlers as hmod
    from vibatchium.daemon.server import Daemon
    profiles = tmp_path / "profiles"
    cache = tmp_path / "cache"
    profiles.mkdir(exist_ok=True)
    cache.mkdir(exist_ok=True)
    # handlers.py + registry.py each did `from .paths import PROFILES_DIR`, so
    # both bindings (and the source in paths) must be redirected.
    monkeypatch.setattr(paths, "PROFILES_DIR", profiles)
    monkeypatch.setattr(regmod, "PROFILES_DIR", profiles)
    monkeypatch.setattr(hmod, "PROFILES_DIR", profiles)
    monkeypatch.setattr(paths, "CACHE_DIR", cache)
    monkeypatch.setattr(paths, "LOG_PATH", cache / "daemon.log")
    monkeypatch.setattr(paths, "ACTIVE_SESSION_PATH", tmp_path / "active-session")
    monkeypatch.setattr(paths, "ACTIVE_PROFILE_PATH", tmp_path / "active-profile")
    paths.set_active_session_name(active)
    return Daemon(), profiles, cache


async def _dispatch(d, args):
    r = await d.dispatch({"id": "1", "cmd": "clean", "args": args})
    assert r["ok"], r.get("error")
    return r["result"]


async def test_clean_protects_default_active_keep_and_warming(tmp_path, monkeypatch):
    """Every protected category is excluded from pruning even when stopped AND
    backdated — so dropping any one guard makes the exact-set assertion fail."""
    d, profiles, _ = _inprocess_daemon(tmp_path, monkeypatch, active="activeone")
    old = time.time() - 30 * 86400
    for n in ("default", "activeone", "keepme", "warm1", "stale1", "stale2"):
        p = profiles / n
        p.mkdir()
        os.utime(p, (old, old))
    # warm1 holds a live pre-warm Chrome — not in _entries, but must be spared.
    d.registry._warm_sessions["warm1"] = object()
    res = await _dispatch(d, {"older_than": 1, "keep": ["keepme"],
                              "locks": False, "cache": False, "logs": False,
                              "apply": False})
    names = set(res["categories"]["profiles"]["names"])
    # Only the two unprotected, stale, stopped profiles are eligible.
    assert names == {"stale1", "stale2"}, names
    assert "default" not in names    # default guard
    assert "activeone" not in names  # active-session guard
    assert "keepme" not in names     # --keep guard
    assert "warm1" not in names      # warming-session guard


async def test_clean_dry_run_deletes_nothing_in_any_category(tmp_path, monkeypatch):
    """dry-run (apply=False) must not delete profiles, locks, caches, OR truncate
    the log — guards the `if apply:` gate in every category."""
    d, profiles, cache = _inprocess_daemon(tmp_path, monkeypatch)
    old = time.time() - 30 * 86400
    # A stale (empty) profile → eligible for the profiles category.
    stale = profiles / "staleprof"
    stale.mkdir()
    os.utime(stale, (old, old))  # empty dir → mtime governs last_active
    # A separate profile holding a lock → eligible for the locks category
    # (lock removal is age-independent; this one is left fresh on purpose).
    lockp = profiles / "lockprof"
    lockp.mkdir()
    lock = lockp / "SingletonLock"
    lock.write_text("x")
    (cache / "observe-cache.json").write_text("{}")
    logp = cache / "daemon.log"
    logp.write_text("L" * (300 * 1024))
    res = await _dispatch(d, {"older_than": 1, "apply": False})
    assert res["dry_run"] is True
    # Report shows reclaimable work...
    assert res["categories"]["profiles"]["count"] == 1
    assert res["categories"]["locks"]["count"] == 1
    # ...but NOTHING is gone.
    assert stale.exists() and lock.exists()
    assert (cache / "observe-cache.json").exists()
    assert logp.stat().st_size == 300 * 1024


async def test_clean_apply_deletes_only_stale_unprotected(tmp_path, monkeypatch):
    d, profiles, _ = _inprocess_daemon(tmp_path, monkeypatch)
    stale = profiles / "stale_apply"
    stale.mkdir()
    os.utime(stale, (time.time() - 30 * 86400,) * 2)
    fresh = profiles / "fresh_apply"
    fresh.mkdir()  # just now
    await _dispatch(d, {"older_than": 7 * 86400, "locks": False,
                        "cache": False, "logs": False, "apply": True})
    assert not stale.exists(), "stale profile should be deleted"
    assert fresh.exists(), "fresh profile must survive the cutoff"


async def test_clean_apply_lock_removal_spares_warming(tmp_path, monkeypatch):
    """The lock loop also keys off the in-use set: a stale lock in a stopped
    profile is removed, but a warming profile's live lock is left alone."""
    d, profiles, _ = _inprocess_daemon(tmp_path, monkeypatch)
    stopped = profiles / "stopped_lock"
    stopped.mkdir()
    stale_lock = stopped / "SingletonLock"
    stale_lock.write_text("x")
    warm = profiles / "warm_lock"
    warm.mkdir()
    warm_lock = warm / "SingletonLock"
    warm_lock.write_text("y")
    d.registry._warm_sessions["warm_lock"] = object()
    await _dispatch(d, {"profiles": False, "cache": False, "logs": False,
                        "apply": True})
    assert not stale_lock.exists(), "stale lock in stopped profile removed"
    assert warm_lock.exists(), "warming profile's live lock must be spared"


async def test_clean_apply_truncates_live_daemon_log(tmp_path, monkeypatch):
    """The real apply path finds the daemon's FileHandler by baseFilename and
    reopens it — exercise that against a live handler over the sandboxed log."""
    import logging
    d, _profiles, cache = _inprocess_daemon(tmp_path, monkeypatch)
    logp = cache / "daemon.log"
    logp.write_text("".join(f"line {i}\n" for i in range(5000)))
    big = logp.stat().st_size
    root = logging.getLogger()
    fh = logging.FileHandler(str(logp))
    root.addHandler(fh)
    try:
        res = await _dispatch(d, {"profiles": False, "locks": False,
                                  "cache": False, "logs": True,
                                  "log_keep_bytes": 4096, "apply": True})
        assert res["categories"]["logs"]["reclaimed"] > 0
        assert logp.stat().st_size < big
        assert logp.stat().st_size <= 4096
        # Handler must keep appending to the truncated file, not a sparse offset.
        logging.getLogger("vibatchium").error("POST-CLEAN-MARKER")
        fh.flush()
        assert "POST-CLEAN-MARKER" in logp.read_text()
    finally:
        root.removeHandler(fh)
        fh.close()


def test_truncate_log_tail_keeps_tail_and_reopens_handler(tmp_path):
    """The log truncator keeps the tail line-aligned AND reopens the live
    FileHandler so subsequent writes append to the truncated file rather than
    leaving a sparse hole at the old offset."""
    import logging
    from vibatchium.daemon.handlers import _truncate_log_tail
    logp = tmp_path / "daemon.log"
    logp.write_text("".join(f"line {i}\n" for i in range(2000)))
    root = logging.getLogger()
    fh = logging.FileHandler(str(logp))
    root.addHandler(fh)
    try:
        new_size = _truncate_log_tail(logp, 300)
        assert new_size <= 300
        assert logp.stat().st_size == new_size
        assert "line 1999" in logp.read_text()          # tail preserved
        # The handler must keep working after truncation — and not at a stale
        # offset that would balloon the file back up.
        logging.getLogger("vibatchium").error("MARKER-XYZ")
        fh.flush()
        assert "MARKER-XYZ" in logp.read_text()
        assert logp.stat().st_size < new_size + 200      # appended, not sparse
    finally:
        root.removeHandler(fh)
        fh.close()
