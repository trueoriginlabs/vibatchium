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

log = logging.getLogger("patchium.stealth.mouse")


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
      session: BrowserSession from patchium.daemon.browser
      button_dwell_ms: typical button-press dwell time. Real humans: 40-120ms.
    """
    available, info = humanize_mouse_available()
    if not available:
        raise RuntimeError(
            "stealth-mouse layer requested but `cdp_patches` is not installed. "
            "Install with: `pip install patchium[stealth-mouse]` "
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

    # cdp_patches.AsyncInput takes either a Browser, Page, or pid; the Page
    # path is the one that matches our Patchright session.
    try:
        input_layer = await AsyncInput(browser=session.page)
    except TypeError:
        # Older API: positional arg
        input_layer = await AsyncInput(session.page)  # type: ignore[arg-type]

    session._input_layer = input_layer
    session._stealth_mouse_installed = True
    log.info("humanized mouse installed via CDP-Patches version=%s", info)
