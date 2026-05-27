"""Optional CDP-Patches integration — humanized mouse trajectories.

Patchright defeats the `Runtime.enable` CDP leak (the main Cloudflare/DataDome
trap), but mouse-event entropy is a separate fingerprint surface. Brotector
and DataDome's aggressive mode (and to a lesser extent Akamai Bot Manager)
fingerprint:

- Mouse-movement velocity profile (Bezier curve vs straight line)
- Click→hold dwell time and movement-during-hold
- Cursor-on-element pre-click hover duration
- Scroll-event entropy
- Timing between event groups

CDP-Patches (https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches) is the
canonical Python lib for this — it injects events at the OS level (xdotool /
SendInput / CGEvent) rather than via CDP, which means real OS-level entropy.

We integrate it as an opt-in layer: if `cdp_patches` is importable, the
`--stealth-mouse` flag activates humanized trajectories on click/move/type
through Patchright's existing API surface.

Licensing note: CDP-Patches has no SPDX LICENSE file at time of writing
(2026-05). We pin the import to a specific commit hash to avoid surprise
license changes. See pyproject.toml `[project.optional-dependencies]`.
"""
from __future__ import annotations

import logging

log = logging.getLogger("vibatchium.stealth.mouse")


def _resolve_browser_pid(context) -> int | None:
    """Best-effort: pull the Chrome OS PID out of a Patchright BrowserContext.

    Walks `context._impl_obj._channel._connection._transport._proc.pid`. All
    underscored internals — fragile by design but the only path that exists
    in Playwright 1.x. Returns None if any link in the chain is missing so
    the caller can raise a useful error rather than AttributeError.
    """
    try:
        return context._impl_obj._channel._connection._transport._proc.pid
    except AttributeError:
        return None


def humanize_mouse_available() -> tuple[bool, str]:
    """Return (available, version_or_reason).

    Probes whether CDP-Patches is importable. We don't raise on miss; the
    caller decides whether the missing dep is fatal or just degraded.
    """
    try:
        import cdp_patches
        version = getattr(cdp_patches, "__version__", "unknown")
        return True, version
    except ImportError as exc:
        return False, str(exc)


async def install_humanized_mouse(session, *, button_dwell_ms: int = 60) -> None:
    """Wire CDP-Patches humanized input over the existing Patchright Page.

    After this call, subsequent session.page.mouse.* and keyboard.* operations
    route through CDP-Patches' OS-level injection layer. Bezier-curve mouse
    paths, jittered dwell, scroll inertia — all become indistinguishable from
    real human input as far as JS event listeners can tell.

    Idempotent: calling twice on the same session is a no-op.

    Args:
      session: BrowserSession from vibatchium.daemon.browser
      button_dwell_ms: typical button-press dwell time. Real humans: 40-120ms.
    """
    available, info = humanize_mouse_available()
    if not available:
        raise RuntimeError(
            "stealth-mouse layer requested but `cdp_patches` is not installed. "
            "Install with: `pip install git+https://github.com/Kaliiiiiiiiii-Vinyzu/CDP-Patches.git@main` "
            f"(import error: {info})"
        )
    if getattr(session, "_stealth_mouse_installed", False):
        log.info("humanized mouse already installed on session")
        return

    try:
        from cdp_patches.input.async_input import AsyncInput
    except ImportError as exc:
        raise RuntimeError(
            f"cdp_patches loaded but expected API not found ({exc}). "
            f"This may indicate a version mismatch — pin to the tested commit."
        ) from exc

    # CDP-Patches 1.1 has two bugs in its browser→pid dispatch:
    #   1) `browser=session.page` silently dies (it stringifies the Page repr
    #      as the PID, then can't find a window for it).
    #   2) `browser=context` hits `TypeError: isinstance() arg 2 must be a
    #      type` because cdp_patches.input.browsers exports `AsyncContext`
    #      etc. as bare strings, not classes.
    # We sidestep both by passing the Chrome PID directly — when `pid=` is
    # set, CDP-Patches's broken `get_async_browser_pid` is never called.
    # The PID lives at a Patchright/Playwright internal; resolution is
    # try/except so a future API rename doesn't kill stealth-mouse outright.
    pid = _resolve_browser_pid(session.page.context)
    if pid is None:
        raise RuntimeError(
            "could not resolve Chrome PID from Patchright context — the "
            "internal attribute chain `_impl_obj._channel._connection._"
            "transport._proc.pid` did not exist. Patchright/Playwright "
            "may have moved it; stealth-mouse cannot continue."
        )
    input_layer = await AsyncInput(pid=pid)

    session._input_layer = input_layer
    session._stealth_mouse_installed = True
    log.info("humanized mouse installed via CDP-Patches version=%s", info)
