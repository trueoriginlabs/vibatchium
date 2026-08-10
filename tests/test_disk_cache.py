"""0.18.13: the Chrome disk cache stops escaping the profile.

Chromium places a profile's cache by re-rooting the user-data-dir's path from
$XDG_CONFIG_HOME to $XDG_CACHE_HOME. Profiles live under ~/.config, so every
session grew a twin at ~/.cache/vibatchium/profiles/<name> that no code path
deleted — `close`, `clean` and the ephemeral reaper all work on the config side.

Three things are asserted here: the twin path is computed the way Chromium
computes it, `launch_session` pins the cache inside the profile so no twin is
created at all, and `delete_profile_dir` removes the twin left by any session
that ran before the pin existed.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from vibatchium.daemon import browser as B
from vibatchium.daemon import registry as R
from vibatchium.daemon.paths import cache_mirror_dir


# ─── the twin path ───────────────────────────────────────────────────────
def test_cache_mirror_mirrors_the_relative_path(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cch"))
    profile = tmp_path / "cfg" / "vibatchium" / "profiles" / "bot"
    assert cache_mirror_dir(profile) == tmp_path / "cch" / "vibatchium" / "profiles" / "bot"


def test_cache_mirror_none_outside_config_home(monkeypatch, tmp_path):
    """An attached or explicitly-placed profile gets no remap, so no twin.

    Returning a path here would point the sweeper at a directory Chromium never
    created — and, worse, one derived from a path we do not own.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cch"))
    assert cache_mirror_dir(tmp_path / "elsewhere" / "profile") is None


def test_cache_mirror_honours_xdg_over_home(monkeypatch, tmp_path):
    """CONFIG_DIR is hardcoded to ~/.config, but Chromium reads XDG_CONFIG_HOME.

    When they disagree the remap does not fire, so neither may we.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "somewhere-else"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cch"))
    assert cache_mirror_dir(Path.home() / ".config" / "vibatchium" / "profiles" / "bot") is None


def test_cache_mirror_defaults_when_xdg_unset(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    got = cache_mirror_dir(Path.home() / ".config" / "vibatchium" / "profiles" / "bot")
    assert got == Path.home() / ".cache" / "vibatchium" / "profiles" / "bot"


# ─── the size knob ───────────────────────────────────────────────────────
def test_disk_cache_mb_default_env_and_junk(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_DISK_CACHE_MB", raising=False)
    assert B.disk_cache_mb() == B.DISK_CACHE_MB_DEFAULT
    monkeypatch.setenv("VIBATCHIUM_DISK_CACHE_MB", "64")
    assert B.disk_cache_mb() == 64
    monkeypatch.setenv("VIBATCHIUM_DISK_CACHE_MB", "garbage")
    assert B.disk_cache_mb() == B.DISK_CACHE_MB_DEFAULT
    monkeypatch.setenv("VIBATCHIUM_DISK_CACHE_MB", "-5")
    assert B.disk_cache_mb() == 0


# ─── the launch flags ────────────────────────────────────────────────────
class _StubPW:
    """Captures launch kwargs and aborts before any Chrome starts."""

    def __init__(self):
        self.kwargs = None
        self.chromium = self

    async def launch_persistent_context(self, **kwargs):
        self.kwargs = kwargs
        raise RuntimeError("stop here — we only want the args")


def _launch_args(monkeypatch, tmp_path, headless=True):
    pw = _StubPW()
    monkeypatch.setattr(B, "coherent_headless_ua", lambda _pw: _noop_ua())
    with pytest.raises(RuntimeError, match="stop here"):
        asyncio.run(B.launch_session(tmp_path / "prof", headless=headless, pw=pw))
    return pw.kwargs["args"] or []


async def _noop_ua():
    return None


def test_launch_pins_cache_inside_the_profile(monkeypatch, tmp_path):
    monkeypatch.delenv("VIBATCHIUM_DISK_CACHE_MB", raising=False)
    args = _launch_args(monkeypatch, tmp_path)
    assert f"--disk-cache-dir={tmp_path / 'prof' / 'ChromeCache'}" in args
    assert f"--disk-cache-size={B.DISK_CACHE_MB_DEFAULT * 1024 * 1024}" in args


def test_launch_pins_cache_when_headed_too(monkeypatch, tmp_path):
    """Headed profiles live under ~/.config as well, so they leak identically."""
    args = _launch_args(monkeypatch, tmp_path, headless=False)
    assert any(a.startswith("--disk-cache-dir=") for a in args)


def test_disk_cache_size_omitted_when_zero(monkeypatch, tmp_path):
    """0 is the documented opt-out: pin the location, let Chrome size it."""
    monkeypatch.setenv("VIBATCHIUM_DISK_CACHE_MB", "0")
    args = _launch_args(monkeypatch, tmp_path)
    assert any(a.startswith("--disk-cache-dir=") for a in args)
    assert not any(a.startswith("--disk-cache-size=") for a in args)


async def test_nodriver_backend_pins_the_cache_too(monkeypatch, tmp_path):
    """nodriver launches Chrome itself and inherits nothing from launch_session
    — the same gap the file already documents for the de-Headless UA flag. A pin
    on only the patchright path leaves `--backend nodriver` leaking."""
    import sys
    import types as _t
    from vibatchium.daemon import backends as BK

    captured = {}

    async def fake_start(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop here — we only want the args")

    fake_uc = _t.ModuleType("nodriver")
    fake_uc.start = fake_start
    monkeypatch.setitem(sys.modules, "nodriver", fake_uc)
    monkeypatch.delenv("VIBATCHIUM_DISK_CACHE_MB", raising=False)

    with pytest.raises(RuntimeError, match="stop here"):
        await BK.launch_nodriver_session(tmp_path / "ndprof", headless=True)

    args = captured.get("browser_args") or []
    assert f"--disk-cache-dir={tmp_path / 'ndprof' / 'ChromeCache'}" in args, args
    assert f"--disk-cache-size={B.DISK_CACHE_MB_DEFAULT * 1024 * 1024}" in args, args


# ─── deleting a profile takes its twin ───────────────────────────────────
def _profiles_at(monkeypatch, tmp_path):
    """Point every module's PROFILES_DIR at a temp config tree."""
    cfg, cch = tmp_path / "cfg", tmp_path / "cch"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cch))
    profiles = cfg / "vibatchium" / "profiles"
    mirror = cch / "vibatchium" / "profiles"
    profiles.mkdir(parents=True)
    mirror.mkdir(parents=True)
    monkeypatch.setattr(R, "PROFILES_DIR", profiles)
    return profiles, mirror


def _make(profiles, mirror, name):
    (profiles / name).mkdir(parents=True)
    (profiles / name / "Cookies").write_text("x")
    (mirror / name / "Default" / "Cache").mkdir(parents=True)
    (mirror / name / "Default" / "Cache" / "data_0").write_text("y" * 64)


def test_delete_profile_dir_removes_the_twin(monkeypatch, tmp_path):
    profiles, mirror = _profiles_at(monkeypatch, tmp_path)
    _make(profiles, mirror, "gone")
    reg = SessionRegistryStub()
    assert asyncio.run(R.SessionRegistry.delete_profile_dir(reg, "gone")) is True
    assert not (profiles / "gone").exists()
    assert not (mirror / "gone").exists()


def test_delete_profile_dir_leaves_other_twins_alone(monkeypatch, tmp_path):
    profiles, mirror = _profiles_at(monkeypatch, tmp_path)
    _make(profiles, mirror, "gone")
    _make(profiles, mirror, "keep")
    reg = SessionRegistryStub()
    asyncio.run(R.SessionRegistry.delete_profile_dir(reg, "gone"))
    assert (mirror / "keep" / "Default" / "Cache" / "data_0").exists()


def test_delete_profile_dir_survives_a_missing_twin(monkeypatch, tmp_path):
    """Post-pin sessions have no twin at all — that must not be an error."""
    profiles, _mirror = _profiles_at(monkeypatch, tmp_path)
    (profiles / "fresh").mkdir(parents=True)
    reg = SessionRegistryStub()
    assert asyncio.run(R.SessionRegistry.delete_profile_dir(reg, "fresh")) is True


def test_delete_profile_dir_refuses_a_traversal_name(monkeypatch, tmp_path):
    """`session_delete`'s validation gate probed with `session_dir(name)`, which
    CREATES the directory it is asked about — so the gate was always true and
    validation never ran, letting `../../x` reach this rmtree. Both ends are
    fixed; this pins the deletion end, which is the one that does the damage."""
    profiles, _mirror = _profiles_at(monkeypatch, tmp_path)
    victim = tmp_path / "VICTIM"
    victim.mkdir()
    (victim / "important.txt").write_text("do not delete")
    rel = os.path.relpath(victim, profiles)
    reg = SessionRegistryStub()
    with pytest.raises(ValueError, match="outside PROFILES_DIR"):
        asyncio.run(R.SessionRegistry.delete_profile_dir(reg, rel))
    assert (victim / "important.txt").exists()


def test_delete_profile_dir_still_deletes_a_normal_name(monkeypatch, tmp_path):
    profiles, _mirror = _profiles_at(monkeypatch, tmp_path)
    (profiles / "ordinary").mkdir()
    reg = SessionRegistryStub()
    assert asyncio.run(R.SessionRegistry.delete_profile_dir(reg, "ordinary")) is True
    assert not (profiles / "ordinary").exists()


def test_session_delete_gate_does_not_create_what_it_tests(monkeypatch, tmp_path):
    """The root cause, pinned separately: a membership test must not have the
    side effect of making itself true."""
    from vibatchium.daemon import paths as pathsmod
    profiles, _mirror = _profiles_at(monkeypatch, tmp_path)
    monkeypatch.setattr(pathsmod, "PROFILES_DIR", profiles)
    (profiles / "real").mkdir()
    assert "real" in pathsmod.list_session_names()
    assert "../../escape" not in pathsmod.list_session_names()
    # session_dir is the helper that made the old gate useless — still true.
    assert pathsmod.session_dir("brand-new").exists(), \
        "session_dir no longer creates; the gate comment needs updating"


def test_delete_profile_dir_will_not_follow_a_symlinked_twin(monkeypatch, tmp_path):
    """A symlink in the mirror must not become a path out of it.

    Asserting only that the target survives is vacuous — `shutil.rmtree` refuses
    a symlink on its own, so the test would pass with the guard deleted. What
    the guard is FOR is that rmtree is never reached, so that is what we assert.
    """
    import shutil
    profiles, mirror = _profiles_at(monkeypatch, tmp_path)
    (profiles / "linked").mkdir(parents=True)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keepme").write_text("z")
    (mirror / "linked").symlink_to(outside, target_is_directory=True)

    calls = []
    real = shutil.rmtree
    monkeypatch.setattr(shutil, "rmtree",
                        lambda p, *a, **k: (calls.append(Path(p)), real(p, *a, **k))[1])
    reg = SessionRegistryStub()
    asyncio.run(R.SessionRegistry.delete_profile_dir(reg, "linked"))
    assert (mirror / "linked") not in calls, "rmtree was aimed at the symlink"
    assert (outside / "keepme").exists()
    assert (mirror / "linked").is_symlink()  # the link itself is left alone


class SessionRegistryStub:
    """Just enough registry for delete_profile_dir's guards."""

    _entries: dict = {}
    _warm_tasks: dict = {}
    _warm_sessions: dict = {}


# ─── the ephemeral close path deletes the twin too ───────────────────────
#
# close() rmtree's an ephemeral profile ITSELF rather than calling
# delete_profile_dir, so a twin sweep wired only into the latter misses the
# busiest path there is — the ephemeral lane is where the profile churn lives.
# A smoke test on a real daemon caught exactly that; these pin it down.

def _ephemeral_registry(monkeypatch, tmp_path):
    import types
    from vibatchium.daemon import paths as pathsmod
    profiles, mirror = _profiles_at(monkeypatch, tmp_path)
    monkeypatch.setattr(pathsmod, "PROFILES_DIR", profiles)
    monkeypatch.setenv("VIBATCHIUM_WARM", "off")
    monkeypatch.setenv("VIBATCHIUM_MAX_EPHEMERAL", "4")
    reg = R.SessionRegistry()

    async def fake_launch(name, *, profile_dir, headless, backend,
                          proxy_cfg=None, geo_cfg=None, gpu_on=None, gpu_node=None):
        profile_dir.mkdir(parents=True, exist_ok=True)
        return types.SimpleNamespace(mode="launch", headless=headless,
                                     gpu=False, gpu_node=None, flags={})
    monkeypatch.setattr(reg, "_launch_for", fake_launch)

    # close() tears down via `_backends.close`, NOT a registry attribute — a
    # stub on the wrong name is silently accepted and leaves every test running
    # the teardown-FAILURE branch instead of a successful close.
    from vibatchium.daemon import backends as backendsmod

    async def fake_close(_session):
        return None
    monkeypatch.setattr(backendsmod, "close", fake_close)
    return reg, profiles, mirror


async def test_the_close_stub_exercises_the_success_path(monkeypatch, tmp_path):
    """Guards the fixture itself: if the teardown stub stops being wired to the
    real call site, every test below quietly changes which branch it covers."""
    import logging
    reg, _profiles, _mirror = _ephemeral_registry(monkeypatch, tmp_path)
    await reg.create("_ex-9-9", headless=True, ephemeral=True)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    logging.getLogger("vibatchium.registry").addHandler(handler)
    try:
        await reg.close("_ex-9-9")
    finally:
        logging.getLogger("vibatchium.registry").removeHandler(handler)
    assert not [r for r in records if "close_session" in r.getMessage()], \
        "teardown raised — the stub is not patching the real call site"


async def test_ephemeral_close_removes_the_cache_twin(monkeypatch, tmp_path):
    reg, profiles, mirror = _ephemeral_registry(monkeypatch, tmp_path)
    await reg.create("_ex-1-1", headless=True, ephemeral=True)
    (mirror / "_ex-1-1" / "Default").mkdir(parents=True)
    (mirror / "_ex-1-1" / "Default" / "data_0").write_text("cached")
    await reg.close("_ex-1-1")
    assert not (profiles / "_ex-1-1").exists()
    assert not (mirror / "_ex-1-1").exists(), "the busiest deletion path leaked its twin"


async def test_persistent_close_keeps_its_twin(monkeypatch, tmp_path):
    """A non-ephemeral close keeps the profile, so its cache must survive too —
    the session is stopped, not deleted, and will reopen on the same profile."""
    reg, profiles, mirror = _ephemeral_registry(monkeypatch, tmp_path)
    await reg.create("keeper", headless=True)
    (mirror / "keeper").mkdir(parents=True)
    await reg.close("keeper")
    assert (profiles / "keeper").exists()
    assert (mirror / "keeper").exists()


async def test_ephemeral_close_outside_profiles_dir_touches_nothing(monkeypatch, tmp_path):
    """The `--profile /somewhere/else --ephemeral` guard bails BEFORE the rmtree.

    Asserting only that the directory survives is not enough — it survives under
    the WRONG ordering too, because an out-of-tree profile has no twin to find.
    Assert the twin drop is never reached, which is the ordering the guard buys.
    """
    from vibatchium.daemon import registry as regmod
    reg, _profiles, _mirror = _ephemeral_registry(monkeypatch, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keepme").write_text("x")
    reached = []
    monkeypatch.setattr(regmod, "drop_cache_mirror",
                        lambda p: reached.append(p) or False)
    await reg.create("stray", headless=True, ephemeral=True, profile_dir=outside)
    await reg.close("stray")
    assert reached == [], "ran past the outside-PROFILES_DIR guard"
    assert (outside / "keepme").exists()


# ─── the shared helper ───────────────────────────────────────────────────
def test_drop_cache_mirror_reports_whether_it_found_one(monkeypatch, tmp_path):
    from vibatchium.daemon import registry as regmod
    from vibatchium.daemon.paths import drop_cache_mirror
    _profiles, mirror = _profiles_at(monkeypatch, tmp_path)
    (mirror / "present").mkdir(parents=True)
    assert drop_cache_mirror(regmod.PROFILES_DIR / "present") is True
    assert not (mirror / "present").exists()
    assert drop_cache_mirror(regmod.PROFILES_DIR / "absent") is False


# ─── every consumer of clean's report knows the new category ─────────────
#
# A new category is not done when the handler emits it. `_clean_item_count`
# gates the whole --apply path, so a category missing from it makes
# `vb clean --apply` print "nothing to reclaim" and silently do nothing whenever
# it is the only work there is — which is exactly what a smoke test caught.

def _report(**counts):
    cats = {k: {"count": c, "bytes": c * 1000, "names": []} for k, c in counts.items()}
    return {"categories": cats, "total_bytes": sum(c * 1000 for c in counts.values())}


def test_clean_item_count_counts_cache_mirror():
    from vibatchium.cli import _clean_item_count
    assert _clean_item_count(_report(cache_mirror=3)) == 3, \
        "--apply would report 'nothing to reclaim' and no-op"
    assert _clean_item_count(_report(profiles=1, cache_mirror=2, locks=1, cache=1)) == 5


def test_clean_item_count_zero_when_genuinely_empty():
    from vibatchium.cli import _clean_item_count
    assert _clean_item_count(_report(profiles=0, cache_mirror=0)) == 0


def test_clean_report_renders_cache_mirror(capsys):
    from vibatchium.cli import _print_clean_report
    _print_clean_report(_report(profiles=0, cache_mirror=2), applied=False)
    err = capsys.readouterr().err
    assert "cachedirs" in err and "2" in err


def test_clean_total_bytes_includes_cache_mirror(monkeypatch, tmp_path):
    """The handler's own total, not the CLI's — it drives the confirm prompt."""
    d, _profiles, mirror = _sandboxed_daemon(tmp_path, monkeypatch)
    _twin(mirror, "orphan")
    r = await_sync(d.dispatch({"id": "1", "cmd": "clean",
                               "args": {"older_than": 1, "profiles": False,
                                        "locks": False, "cache": False,
                                        "logs": False, "apply": False}}))
    assert r["result"]["total_bytes"] == r["result"]["categories"]["cache_mirror"]["bytes"]
    assert r["result"]["total_bytes"] > 0


def await_sync(coro):
    return asyncio.run(coro)


def test_drop_cache_mirror_refuses_a_symlink(monkeypatch, tmp_path):
    """Same point as above at the helper level: rmtree must never be aimed at
    a symlink, not merely survive being aimed at one."""
    import shutil
    from vibatchium.daemon import registry as regmod
    from vibatchium.daemon.paths import drop_cache_mirror
    _profiles, mirror = _profiles_at(monkeypatch, tmp_path)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keepme").write_text("x")
    (mirror / "linked").symlink_to(outside, target_is_directory=True)

    calls = []
    monkeypatch.setattr(shutil, "rmtree", lambda p, *a, **k: calls.append(Path(p)))
    assert drop_cache_mirror(regmod.PROFILES_DIR / "linked") is False
    assert calls == [], f"rmtree reached: {calls}"
    assert (outside / "keepme").exists()


# ─── `clean` sweeps twins whose profile is already gone ──────────────────
#
# In-process Daemon with PROFILES_DIR redirected under a temp XDG_CONFIG_HOME,
# so cache_mirror_dir resolves into the sandbox and `apply` can never reach the
# real store. Mirrors the harness in test_wave8_bloat.

def _sandboxed_daemon(tmp_path, monkeypatch):
    from vibatchium.daemon import paths, registry as regmod, handlers as hmod
    from vibatchium.daemon.server import Daemon
    cfg, cch = tmp_path / "cfg", tmp_path / "cch"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cch))
    profiles = cfg / "vibatchium" / "profiles"
    mirror = cch / "vibatchium" / "profiles"
    profiles.mkdir(parents=True)
    mirror.mkdir(parents=True)
    cache = tmp_path / "runtime"
    cache.mkdir()
    for mod in (paths, regmod, hmod):
        monkeypatch.setattr(mod, "PROFILES_DIR", profiles)
    monkeypatch.setattr(paths, "CACHE_DIR", cache)
    monkeypatch.setattr(paths, "LOG_PATH", cache / "daemon.log")
    monkeypatch.setattr(paths, "ACTIVE_SESSION_PATH", tmp_path / "active-session")
    monkeypatch.setattr(paths, "ACTIVE_PROFILE_PATH", tmp_path / "active-profile")
    paths.set_active_session_name("default")
    return Daemon(), profiles, mirror


def _twin(mirror, name, *, age_days=30.0):
    import os
    blocks = mirror / name / "Default" / "Cache" / "Cache_Data"
    blocks.mkdir(parents=True)
    (blocks / "data_0").write_text("z" * 128)
    stamp = __import__("time").time() - age_days * 86400
    os.utime(blocks, (stamp, stamp))
    return mirror / name


async def _clean(d, **args):
    r = await d.dispatch({"id": "1", "cmd": "clean",
                          "args": {"profiles": False, "locks": False,
                                   "cache": False, "logs": False, **args}})
    assert r["ok"], r.get("error")
    return r["result"]["categories"]["cache_mirror"]


async def test_clean_reports_orphaned_twins(tmp_path, monkeypatch):
    d, _profiles, mirror = _sandboxed_daemon(tmp_path, monkeypatch)
    _twin(mirror, "orphan")
    res = await _clean(d, older_than=1, apply=False)
    assert res["names"] == ["orphan"]
    assert res["bytes"] > 0
    assert (mirror / "orphan").exists()  # dry run deletes nothing


async def test_clean_spares_a_twin_whose_profile_has_not_relaunched(tmp_path, monkeypatch):
    """No ChromeCache/ inside the profile means we cannot tell a dead cache from
    one Chrome is still writing, so the twin stays. This is the conservative
    half of the rule and the reason the sweep is safe to run unattended."""
    d, profiles, mirror = _sandboxed_daemon(tmp_path, monkeypatch)
    (profiles / "alive").mkdir()
    _twin(mirror, "alive")
    assert await _clean(d, older_than=1, apply=False) == {
        "count": 0, "bytes": 0, "names": []}


async def test_clean_reclaims_a_twin_the_profile_has_outgrown(tmp_path, monkeypatch):
    """The measured majority of the leak: the biggest twins belong to sessions
    that RUN, so their profiles still exist. Once the profile has relaunched
    under the pin (ChromeCache/ present) Chrome cannot reach the twin at all,
    and a rule keyed on 'profile is gone' would strand it forever — 72 dirs and
    8.4 GB of it on the box this was measured against."""
    d, profiles, mirror = _sandboxed_daemon(tmp_path, monkeypatch)
    (profiles / "bot" / "ChromeCache").mkdir(parents=True)
    _twin(mirror, "bot")
    res = await _clean(d, older_than=1, apply=True)
    assert res["names"] == ["bot"]
    assert not (mirror / "bot").exists()
    assert (profiles / "bot" / "ChromeCache").exists()  # the live cache is untouched


async def test_clean_never_touches_a_running_session_even_when_relaunched(tmp_path, monkeypatch):
    d, profiles, mirror = _sandboxed_daemon(tmp_path, monkeypatch)
    (profiles / "bot" / "ChromeCache").mkdir(parents=True)
    _twin(mirror, "bot")
    d.registry._warm_sessions["bot"] = object()
    assert (await _clean(d, older_than=1, apply=True))["count"] == 0
    assert (mirror / "bot").exists()


async def test_clean_ages_off_the_block_file_not_the_directory(tmp_path, monkeypatch):
    """The twin's own mtime is a creation stamp — Chrome writes into Cache_Data
    without touching the parent, so a directory in daily use can look years
    idle. Ageing off the parent would delete a cache still being written."""
    import os
    d, _profiles, mirror = _sandboxed_daemon(tmp_path, monkeypatch)
    twin = _twin(mirror, "busy", age_days=0)   # blocks written just now...
    old = __import__("time").time() - 400 * 86400
    os.utime(twin, (old, old))                 # ...parent stamped long ago
    assert (await _clean(d, older_than=86400, apply=False))["count"] == 0


async def test_clean_apply_removes_the_orphan(tmp_path, monkeypatch):
    d, _profiles, mirror = _sandboxed_daemon(tmp_path, monkeypatch)
    _twin(mirror, "orphan")
    _twin(mirror, "fresh", age_days=0)
    res = await _clean(d, older_than=1, apply=True)
    assert res["names"] == ["orphan"]
    assert not (mirror / "orphan").exists()
    assert (mirror / "fresh").exists()


async def test_clean_never_sweeps_a_running_session(tmp_path, monkeypatch):
    d, _profiles, mirror = _sandboxed_daemon(tmp_path, monkeypatch)
    _twin(mirror, "running")
    d.registry._warm_sessions["running"] = object()
    assert (await _clean(d, older_than=1, apply=True))["count"] == 0
    assert (mirror / "running").exists()


async def test_clean_can_be_switched_off(tmp_path, monkeypatch):
    d, _profiles, mirror = _sandboxed_daemon(tmp_path, monkeypatch)
    _twin(mirror, "orphan")
    r = await d.dispatch({"id": "1", "cmd": "clean",
                          "args": {"profiles": False, "locks": False,
                                   "cache": False, "logs": False,
                                   "cache_mirror": False, "apply": True}})
    assert "cache_mirror" not in r["result"]["categories"]
    assert (mirror / "orphan").exists()
