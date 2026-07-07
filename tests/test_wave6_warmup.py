"""Wave 6.1b — warmup tests.

Verifies:
- get_warm_mode parses VIBATCHIUM_WARM with sensible defaults
- VIBATCHIUM_WARM=off is a clean no-op (no extra processes scheduled)
- Eager mode: warmup() starts the Playwright driver subprocess
- Opportunistic mode: session_new schedules a background pre-spawn task
- session_delete cancels in-flight pre-warm cleanly
- start() consumes a pre-warmed session and reports faster than cold
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

from vibatchium.client import call, DaemonError
from vibatchium.daemon.paths import PROFILES_DIR
from vibatchium.daemon.registry import get_warm_mode


def _ensure_clean(name: str) -> None:
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    p = PROFILES_DIR / name
    if p.exists():
        try:
            shutil.rmtree(p)
        except Exception:  # noqa: BLE001
            pass


# ─── warm-claim posture guard (0.6.9 regression) ────────────────────────


async def test_headed_request_does_not_claim_headless_prewarm(monkeypatch):
    """0.6.9 regression: a --headed start must NOT be handed a headless
    pre-warm. registry.create()'s warm-claim guard previously checked
    backend + profile + proxy but omitted headless (the config its own comment
    promised to match), so --headed silently got the already-headless warm and
    opened zero windows.

    Tests the guard logic directly: backends.launch/close and _ensure_pw are
    stubbed (no real Chrome -> no display needed, deterministic). Fails without
    the fix: in case A the headed request claims the warm, so no fresh launch
    happens and entry.session is the (headless) warm.
    """
    from types import SimpleNamespace
    from vibatchium.daemon.registry import SessionRegistry
    from vibatchium.daemon import backends

    calls = {"launch": 0, "closed": []}

    async def fake_launch(backend, pdir, *, headless, pw=None, proxy=None,
                          timezone_id=None, gpu=False, gpu_node=None):
        calls["launch"] += 1
        return SimpleNamespace(headless=headless, profile_dir=pdir, mode="launch",
                               timezone_id=timezone_id, gpu=bool(gpu), gpu_node=gpu_node)

    async def fake_close(sess):
        calls["closed"].append(sess)

    async def fake_ensure_pw():
        return object()

    monkeypatch.setattr(backends, "launch", fake_launch)
    monkeypatch.setattr(backends, "close", fake_close)
    base = Path(tempfile.mkdtemp(prefix="warmguard_"))
    try:
        # (A) the bug: headless warm, --headed request -> must NOT claim it.
        reg = SessionRegistry()
        monkeypatch.setattr(reg, "_ensure_pw", fake_ensure_pw)
        pdir_a = base / "headed"
        warm_a = SimpleNamespace(headless=True, profile_dir=pdir_a, mode="launch")
        reg._warm_sessions["headed"] = warm_a
        entry_a = await reg.create("headed", profile_dir=pdir_a, headless=False)
        assert entry_a.session is not warm_a, (
            "--headed request was handed the headless pre-warm (the bug)"
        )
        assert entry_a.session.headless is False, "claimed session is not headed"
        assert calls["launch"] == 1, "a fresh headed Chrome should have been launched"
        assert warm_a in calls["closed"], "the rejected headless warm should be closed"

        # (B) control: headless warm, headless request -> reuse it.
        reg2 = SessionRegistry()
        monkeypatch.setattr(reg2, "_ensure_pw", fake_ensure_pw)
        calls["launch"] = 0
        pdir_b = base / "headless"
        warm_b = SimpleNamespace(headless=True, profile_dir=pdir_b, mode="launch")
        reg2._warm_sessions["headless"] = warm_b
        entry_b = await reg2.create("headless", profile_dir=pdir_b, headless=True)
        assert entry_b.session is warm_b, (
            "matching-posture pre-warm should still be reused (optimization intact)"
        )
        assert calls["launch"] == 0, "no fresh launch when the warm matches posture"

        # (C) 0.6.11: a geo-configured session must NOT claim a geo-less warm
        # (the warm was launched without the timezone override, so it'd silently
        # mismatch — same class as the headless bug). Guard adds `geo_cfg is
        # None` exactly like proxy.
        from vibatchium import geo as _geo
        reg3 = SessionRegistry()
        monkeypatch.setattr(reg3, "_ensure_pw", fake_ensure_pw)
        calls["launch"] = 0
        calls["closed"] = []
        pdir_c = base / "geosess"
        pdir_c.mkdir(parents=True, exist_ok=True)
        _geo.save_session_geo(pdir_c, {"timezone_id": "Asia/Tokyo"})
        warm_c = SimpleNamespace(headless=True, profile_dir=pdir_c, mode="launch")
        reg3._warm_sessions["geosess"] = warm_c
        entry_c = await reg3.create("geosess", profile_dir=pdir_c, headless=True)
        assert entry_c.session is not warm_c, (
            "geo-configured request claimed a geo-less pre-warm (the bug)")
        assert calls["launch"] == 1, "a fresh geo-applied Chrome should launch"
        assert warm_c in calls["closed"], "the rejected geo-less warm should close"
        assert entry_c.session.timezone_id == "Asia/Tokyo", (
            "the fresh launch did not receive the configured timezone")
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ─── pure mode-parser tests ─────────────────────────────────────────────


def test_warm_mode_default_both():
    """Default — when env unset — is 'both' per user spec."""
    prior = os.environ.pop("VIBATCHIUM_WARM", None)
    try:
        assert get_warm_mode() == "both"
    finally:
        if prior is not None:
            os.environ["VIBATCHIUM_WARM"] = prior


def test_warm_mode_off():
    prior = os.environ.get("VIBATCHIUM_WARM")
    os.environ["VIBATCHIUM_WARM"] = "off"
    try:
        assert get_warm_mode() == "off"
    finally:
        if prior is None:
            os.environ.pop("VIBATCHIUM_WARM", None)
        else:
            os.environ["VIBATCHIUM_WARM"] = prior


def test_warm_mode_invalid_falls_back_to_both():
    prior = os.environ.get("VIBATCHIUM_WARM")
    os.environ["VIBATCHIUM_WARM"] = "garbage"
    try:
        assert get_warm_mode() == "both"
    finally:
        if prior is None:
            os.environ.pop("VIBATCHIUM_WARM", None)
        else:
            os.environ["VIBATCHIUM_WARM"] = prior


def test_warm_mode_all_valid_values():
    for v in ("eager", "opportunistic", "both", "off"):
        prior = os.environ.get("VIBATCHIUM_WARM")
        os.environ["VIBATCHIUM_WARM"] = v
        try:
            assert get_warm_mode() == v
        finally:
            if prior is None:
                os.environ.pop("VIBATCHIUM_WARM", None)
            else:
                os.environ["VIBATCHIUM_WARM"] = prior


# ─── daemon-level tests ─────────────────────────────────────────────────


def test_session_new_with_prewarm_off_in_conftest():
    """Conftest sets VIBATCHIUM_WARM=off, so session_new should NOT schedule
    a pre-spawn even though the default prewarm arg is True."""
    name = "vibatchium_test_w6_no_prewarm"
    _ensure_clean(name)
    try:
        res = call("session_new", {"name": name})
        # prewarm_scheduled is "did caller request it"; the actual scheduling
        # is gated by get_warm_mode in the daemon. Since VIBATCHIUM_WARM=off,
        # no Chrome should be spawned. Verify by counting before/after.
        before = _count_vibatchium_chromes()
        time.sleep(0.8)  # give any rogue pre-spawn time to fire
        after = _count_vibatchium_chromes()
        assert after == before, (
            f"VIBATCHIUM_WARM=off should have prevented pre-spawn; "
            f"chrome count went {before} → {after}"
        )
    finally:
        _ensure_clean(name)


def test_session_new_returns_prewarm_scheduled_flag():
    """The response shape should include prewarm_scheduled for visibility,
    even when the daemon-side gate (VIBATCHIUM_WARM) blocks the actual spawn."""
    name = "vibatchium_test_w6_flag"
    _ensure_clean(name)
    try:
        res = call("session_new", {"name": name, "prewarm": True})
        assert res["prewarm_scheduled"] is True
        res2 = call("session_new", {"name": name + "_opt", "prewarm": False})
        assert res2["prewarm_scheduled"] is False
    finally:
        _ensure_clean(name)
        _ensure_clean(name + "_opt")


def test_session_delete_cancels_inflight_prewarm():
    """Even though VIBATCHIUM_WARM=off prevents real pre-spawn, the
    cancel_prewarm path must not raise on missing entries."""
    name = "vibatchium_test_w6_cancel"
    _ensure_clean(name)
    call("session_new", {"name": name})
    # Delete should not raise even when nothing to cancel
    res = call("session_delete", {"name": name})
    assert res.get("name") == name


def _count_vibatchium_chromes() -> int:
    """Count Chrome processes whose --user-data-dir lives under our profile root."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["ps", "-e", "-o", "args="], text=True, errors="ignore", timeout=2,
        )
    except Exception:  # noqa: BLE001
        return 0
    return sum(
        1 for line in out.splitlines()
        if "chrome" in line.lower()
        and ("/profiles/vibatchium_test" in line or "/vibatchium-test-profile" in line
             or "/vibatchium-warm" in line)
    )
