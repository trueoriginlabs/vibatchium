"""0.16.0 idle-freeze: a parked session must not burn the box.

A headless page is never "hidden" and the launch defaults keep Chrome's
anti-throttle flags on (stealth posture), so a session parked on a page with
WebGL / CSS animations / rAF loops renders at full speed forever. Under
software GL (SwiftShader) that pegs multiple CPU cores per parked session —
the 2026-07-13 and 2026-07-15 shared-box incidents were both a THREE.js
background costing 4+ cores while nobody was driving the session.

The fix lives daemon-side so no caller has to remember a flag: a session that
hasn't served a verb for VIBATCHIUM_IDLE_FREEZE_AFTER seconds gets its
RENDERER processes SIGSTOPped; the next verb SIGCONTs them — under the same
per-session lock — before running, so an actively-driven session is never
frozen. Kernel-level stop is the ONLY mechanism that measurably works
(2026-07-15 probes on chromium-1217):

  rAF/JS burn   156 t/4s → CDP setScriptExecutionDisabled 1, SIGSTOP 0
  CSS-anim burn 198 t/4s → setScriptExecutionDisabled 190,
                            Page.setWebLifecycleState frozen 206, SIGSTOP 0
  Emulation.setCPUThrottlingRate(10) made it WORSE (27% → 105% of a core:
  the suspend/resume machinery burns more than the page).

Only renderers are stopped — the browser process, GPU process, and CDP stay
live, so registry ops, `vb status`, and self-heal keep working; a stopped
renderer submits no frames, so GPU-process burn stops with it. Renderers are
found by /proc cmdline (`--type=renderer` + `--user-data-dir=<profile>`), and
recorded as (pid, starttime) pairs so a recycled pid is never signalled.
SIGKILL (teardown) works on stopped processes, but close() still thaws first
so Chrome's graceful shutdown IPC isn't left waiting on a stopped child.

Eligibility (`eligible`): launched sessions only (attach-mode can be a human's
real browser — and its renderers may not carry our profile dir anyway),
headless only (a headed window may be human-driven with zero daemon traffic —
e.g. a manual login), patchright backend only (posture parity with the other
maintenance paths).

Knobs:
  VIBATCHIUM_IDLE_FREEZE        default on; 0/false/no/off disables
  VIBATCHIUM_IDLE_FREEZE_AFTER  idle seconds before freezing (default 90;
                                clamped to >=5)
"""
from __future__ import annotations

import logging
import os
import signal

log = logging.getLogger("vibatchium.daemon")

DEFAULT_AFTER = 90.0


def freeze_enabled() -> bool:
    return os.environ.get("VIBATCHIUM_IDLE_FREEZE", "1").lower() not in (
        "0", "false", "no", "off")


def freeze_after() -> float:
    """Idle seconds before a session's renderers are frozen."""
    raw = os.environ.get("VIBATCHIUM_IDLE_FREEZE_AFTER", "")
    if raw.strip() == "":
        return DEFAULT_AFTER
    try:
        after = float(raw)
    except ValueError:
        log.warning("idle-freeze: bad VIBATCHIUM_IDLE_FREEZE_AFTER=%r — "
                    "using default %s", raw, DEFAULT_AFTER)
        return DEFAULT_AFTER
    return max(after, 5.0)


def eligible(entry) -> bool:
    """Whether this session may ever be idle-frozen. See module docstring."""
    sess = entry.session
    if sess is None or getattr(sess, "mode", None) != "launch":
        return False
    if not getattr(sess, "headless", False):
        return False
    if entry.flags.get("backend", "patchright") != "patchright":
        return False
    return True


def _starttime(pid: int) -> int | None:
    """Kernel starttime of `pid` (clock ticks since boot) — the pid-reuse
    guard: a recycled pid has a different starttime. None if gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _find_renderers(profile_dir: str) -> list[int]:
    """Renderer pids of the Chrome serving `profile_dir`. Chrome propagates
    --user-data-dir onto its renderer processes, so a plain cmdline match is
    session-exact. Empty result = fail-safe (nothing gets frozen)."""
    pids = []
    needle = f"--user-data-dir={profile_dir}"
    for ent in os.listdir("/proc"):
        if not ent.isdigit():
            continue
        try:
            with open(f"/proc/{ent}/cmdline") as f:
                cmd = f.read().replace("\0", " ")
        except OSError:
            continue
        if "--type=renderer" in cmd and needle in cmd:
            pids.append(int(ent))
    return pids


def _signal_checked(pid: int, starttime: int, sig: int) -> bool:
    """Send `sig` to `pid` iff its starttime still matches (not recycled)."""
    if _starttime(pid) != starttime:
        return False
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


async def apply(entry) -> int:
    """SIGSTOP every not-yet-frozen renderer of the session. Returns the
    number newly stopped (0 = already fully covered / none found). Re-invoked
    each poll while the session stays idle. Caller holds ``entry.lock``."""
    have = {pid for pid, _ in entry.freeze_pids}
    fresh = 0
    for pid in _find_renderers(str(entry.profile_dir)):
        if pid in have:
            continue
        st = _starttime(pid)
        if st is None:
            continue
        if not _signal_checked(pid, st, signal.SIGSTOP):
            continue
        entry.freeze_pids.append((pid, st))
        fresh += 1
    entry.frozen = bool(entry.freeze_pids)
    return fresh


async def lift(entry) -> None:
    """Thaw: SIGCONT every recorded renderer (starttime-checked, so a recycled
    pid is never signalled), then clear state. Never raises. Caller holds
    ``entry.lock``."""
    if not entry.freeze_pids and not entry.frozen:
        return
    for pid, st in entry.freeze_pids:
        _signal_checked(pid, st, signal.SIGCONT)
    entry.freeze_pids.clear()
    entry.frozen = False
    log.info("idle-freeze: thawed session=%s", entry.name)
