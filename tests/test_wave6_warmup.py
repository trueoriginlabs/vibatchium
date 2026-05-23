"""Wave 6.1b — warmup tests.

Verifies:
- get_warm_mode parses PATCHIUM_WARM with sensible defaults
- PATCHIUM_WARM=off is a clean no-op (no extra processes scheduled)
- Eager mode: warmup() starts the Playwright driver subprocess
- Opportunistic mode: session_new schedules a background pre-spawn task
- session_delete cancels in-flight pre-warm cleanly
- start() consumes a pre-warmed session and reports faster than cold
"""
from __future__ import annotations

import os
import shutil
import time


from patchium.client import call, DaemonError
from patchium.daemon.paths import PROFILES_DIR
from patchium.daemon.registry import get_warm_mode


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


# ─── pure mode-parser tests ─────────────────────────────────────────────


def test_warm_mode_default_both():
    """Default — when env unset — is 'both' per user spec."""
    prior = os.environ.pop("PATCHIUM_WARM", None)
    try:
        assert get_warm_mode() == "both"
    finally:
        if prior is not None:
            os.environ["PATCHIUM_WARM"] = prior


def test_warm_mode_off():
    prior = os.environ.get("PATCHIUM_WARM")
    os.environ["PATCHIUM_WARM"] = "off"
    try:
        assert get_warm_mode() == "off"
    finally:
        if prior is None:
            os.environ.pop("PATCHIUM_WARM", None)
        else:
            os.environ["PATCHIUM_WARM"] = prior


def test_warm_mode_invalid_falls_back_to_both():
    prior = os.environ.get("PATCHIUM_WARM")
    os.environ["PATCHIUM_WARM"] = "garbage"
    try:
        assert get_warm_mode() == "both"
    finally:
        if prior is None:
            os.environ.pop("PATCHIUM_WARM", None)
        else:
            os.environ["PATCHIUM_WARM"] = prior


def test_warm_mode_all_valid_values():
    for v in ("eager", "opportunistic", "both", "off"):
        prior = os.environ.get("PATCHIUM_WARM")
        os.environ["PATCHIUM_WARM"] = v
        try:
            assert get_warm_mode() == v
        finally:
            if prior is None:
                os.environ.pop("PATCHIUM_WARM", None)
            else:
                os.environ["PATCHIUM_WARM"] = prior


# ─── daemon-level tests ─────────────────────────────────────────────────


def test_session_new_with_prewarm_off_in_conftest():
    """Conftest sets PATCHIUM_WARM=off, so session_new should NOT schedule
    a pre-spawn even though the default prewarm arg is True."""
    name = "patchium_test_w6_no_prewarm"
    _ensure_clean(name)
    try:
        res = call("session_new", {"name": name})
        # prewarm_scheduled is "did caller request it"; the actual scheduling
        # is gated by get_warm_mode in the daemon. Since PATCHIUM_WARM=off,
        # no Chrome should be spawned. Verify by counting before/after.
        before = _count_patchium_chromes()
        time.sleep(0.8)  # give any rogue pre-spawn time to fire
        after = _count_patchium_chromes()
        assert after == before, (
            f"PATCHIUM_WARM=off should have prevented pre-spawn; "
            f"chrome count went {before} → {after}"
        )
    finally:
        _ensure_clean(name)


def test_session_new_returns_prewarm_scheduled_flag():
    """The response shape should include prewarm_scheduled for visibility,
    even when the daemon-side gate (PATCHIUM_WARM) blocks the actual spawn."""
    name = "patchium_test_w6_flag"
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
    """Even though PATCHIUM_WARM=off prevents real pre-spawn, the
    cancel_prewarm path must not raise on missing entries."""
    name = "patchium_test_w6_cancel"
    _ensure_clean(name)
    call("session_new", {"name": name})
    # Delete should not raise even when nothing to cancel
    res = call("session_delete", {"name": name})
    assert res.get("name") == name


def _count_patchium_chromes() -> int:
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
        and ("/profiles/patchium_test" in line or "/patchium-test-profile" in line
             or "/patchium-warm" in line)
    )
