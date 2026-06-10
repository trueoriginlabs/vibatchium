"""Pluggable stealth backends (Wave 5.4).

Two backends ship today:

- **patchright** (default): canonical Patchright stack — `launch_persistent_context`
  + `channel='chrome'` + headed + no-viewport. The 2026 Cloudflare benchmark
  (Paterson) puts it at 25 OK / 3 gated / 3 blocked across 31 targets.

- **nodriver** (optional, opt-in via `pip install vibatchium[nodriver]`): uses
  the `nodriver` library to spawn Chrome with its hardened launch flags
  (no chromedriver injection, expert-mode CDP), then vibatchium connects via
  Patchright `connect_over_cdp` for the action layer. Same 2026 benchmark:
  28 OK / 3 gated / 0 blocked — the only tool with zero hard blocks. Useful
  when Patchright hits Cloudflare Turnstile interactive challenges.

The two backends ALWAYS produce the same `BrowserSession` object, so every
existing handler keeps working unchanged — the difference is in *how* Chrome
was launched, not the API surface.

`Backend.auto` picks `patchright` by default and surfaces an advisory in the
response when a Cloudflare wall is detected (status 403 / "Just a moment"
title) suggesting the user re-launch with `--backend nodriver`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from patchright.async_api import Playwright, async_playwright

from .browser import (
    BrowserSession,
    attach_session,
    coherent_headless_ua,
    launch_session,
)

log = logging.getLogger("vibatchium.backends")


VALID_BACKENDS = {"patchright", "nodriver", "auto"}
DEFAULT_BACKEND = "patchright"


# Page titles / status codes Cloudflare and DataDome serve when walling a
# scraper. Used by the `_go` handler's auto-escalation hint.
CLOUDFLARE_TITLES = (
    "just a moment",
    "attention required",
    "verifying you are human",
    "checking your browser",
)
DATADOME_TITLES = (
    "blocked - datadome",
    "you've been blocked",
)
WALL_TITLES = CLOUDFLARE_TITLES + DATADOME_TITLES


def is_walled(title: str, status: int | None) -> str | None:
    """Return a defender name if the response looks like a bot wall, else None.

    Used by `_go` to surface `cloudflare_walled: <defender>` in the result
    when a navigation seems blocked. Caller can then advise switching to the
    nodriver backend (which sometimes gets through where patchright doesn't).
    """
    if status == 403 or status == 429:
        # status alone isn't conclusive — many legit 403/429 responses exist
        pass
    tl = (title or "").lower()
    for needle in CLOUDFLARE_TITLES:
        if needle in tl:
            return "cloudflare"
    for needle in DATADOME_TITLES:
        if needle in tl:
            return "datadome"
    return None


async def launch_patchright_session(
    profile_dir: Path,
    *,
    headless: bool = False,
    pw: Playwright | None = None,
    proxy: dict | None = None,
) -> BrowserSession:
    """Canonical Patchright launch (current default)."""
    return await launch_session(profile_dir, headless=headless, pw=pw, proxy=proxy)


async def launch_nodriver_session(
    profile_dir: Path,
    *,
    headless: bool = False,
    pw: Playwright | None = None,
    proxy: dict | None = None,
) -> BrowserSession:
    """Launch Chrome via nodriver, then connect Patchright over CDP.

    Why two-layer: nodriver's launcher avoids the chromedriver injection
    surface entirely and sets stealth flags that Patchright doesn't (or sets
    differently). Patchright then connects via `connect_over_cdp` so the
    daemon's existing handlers (which call Playwright APIs) keep working.
    Patchright's CDP-message patches still apply over CDP (per the project
    README — they're at the client protocol layer, not the launch layer).

    Requires `pip install vibatchium[nodriver]` (which pulls the `nodriver` lib).
    """
    try:
        import nodriver as uc  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "nodriver backend requires `pip install vibatchium[nodriver]`. "
            f"(import error: {exc})"
        ) from exc

    profile_dir.mkdir(parents=True, exist_ok=True)
    log.info("nodriver launch persistent context profile=%s headless=%s",
             profile_dir, headless)

    # Pick a free port via OS — nodriver wants a concrete port
    import socket as _sk
    sk = _sk.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()

    # nodriver supports proxy via browser_args; passing through.
    extra_args = []
    if proxy:
        extra_args.append(f"--proxy-server={proxy['server']}")
        # nodriver doesn't auto-inject Basic-Auth — caller must pass an
        # auth-handling extension or use a proxy URL that includes inline creds.
        # For now, warn if username/password present.
        if proxy.get("username") or proxy.get("password"):
            log.warning("nodriver proxy doesn't support inline auth; "
                        "use a Chrome extension or IP-allowlisted proxy")
    # De-Headless the UA on the nodriver path too — nodriver launches Chrome
    # itself (bypasses launch_session), so it doesn't inherit the patchright
    # path's `--user-agent` flag. Same browser-wide flag via browser_args.
    # The clean-UA probe needs a live patchright driver; reuse the shared one,
    # or spin a throwaway just for the probe on a cold nodriver-first launch.
    if headless:
        probe_pw, own_probe_pw = pw, False
        if probe_pw is None:
            probe_pw = await async_playwright().start()
            own_probe_pw = True
        try:
            clean_ua = await coherent_headless_ua(probe_pw)
        finally:
            if own_probe_pw:
                await probe_pw.stop()
        if clean_ua:
            extra_args.append(f"--user-agent={clean_ua}")
    browser = await uc.start(
        user_data_dir=str(profile_dir),
        headless=headless,
        port=port,
        no_sandbox=False,
        browser_args=extra_args if extra_args else None,
    )
    cdp_url = f"http://127.0.0.1:{port}"

    # Now connect Patchright over CDP and wrap as a BrowserSession.
    # We use attach_session for the Patchright side, but the underlying
    # Chrome process is owned by nodriver and must be closed via the
    # nodriver Browser handle on session teardown.
    sess = await attach_session(cdp_url, pw=pw)
    sess.mode = "launch"          # treat as launch (we own the Chrome process)
    sess.profile_dir = profile_dir
    sess._nodriver_browser = browser  # keep handle for cleanup
    return sess


async def launch(
    backend: str,
    profile_dir: Path,
    *,
    headless: bool = False,
    pw: Playwright | None = None,
    proxy: dict | None = None,
) -> BrowserSession:
    """Dispatch to the requested backend's launcher."""
    if backend not in VALID_BACKENDS:
        raise ValueError(
            f"unknown backend {backend!r}; valid: {sorted(VALID_BACKENDS)}"
        )
    if backend in ("patchright", "auto"):
        return await launch_patchright_session(profile_dir, headless=headless,
                                                pw=pw, proxy=proxy)
    if backend == "nodriver":
        return await launch_nodriver_session(profile_dir, headless=headless,
                                              pw=pw, proxy=proxy)
    raise AssertionError(f"unreachable backend: {backend}")


async def close(session: BrowserSession) -> None:
    """Tear down the Chrome process appropriately for its backend.

    For the patchright backend, defer to `browser.close_session`. For the
    nodriver backend, also stop the underlying `nodriver.Browser` to actually
    kill the Chrome process (Patchright's CDP disconnect alone doesn't).
    """
    from .browser import close_session as _close

    nd = getattr(session, "_nodriver_browser", None)
    try:
        await _close(session)
    finally:
        if nd is not None:
            try:
                nd.stop()  # nodriver Browser.stop is sync in current versions
            except Exception:  # noqa: BLE001
                pass
