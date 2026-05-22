"""Patchwright browser lifecycle — launch persistent context OR attach over CDP."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from patchright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

log = logging.getLogger("patchium.browser")


@dataclass
class BrowserSession:
    """Holds the live Patchwright handles for the daemon's lifetime."""

    pw: Playwright
    context: BrowserContext
    page: Page
    mode: str  # "launch" | "attach"
    profile_dir: Optional[Path] = None
    cdp_url: Optional[str] = None


async def launch_session(profile_dir: Path, headless: bool = False) -> BrowserSession:
    """Cold-launch real Chrome with persistent context (canonical Patchright config)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.info("launch persistent context profile=%s headless=%s", profile_dir, headless)

    pw = await async_playwright().start()
    context = await pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        channel="chrome",
        headless=headless,
        no_viewport=True,
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return BrowserSession(pw=pw, context=context, page=page, mode="launch", profile_dir=profile_dir)


async def attach_session(cdp_url: str) -> BrowserSession:
    """Attach to an already-running Chrome via `--remote-debugging-port=<port>`.

    The user's normal Chrome carries its real fingerprint (TLS, profile, cookies),
    which is the cleanest way past Cloudflare-class walls once a manual login has
    happened. Patchright's runtime-context isolation still applies on the client
    side regardless of how the browser was launched.
    """
    log.info("attach over CDP cdp_url=%s", cdp_url)
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    if not browser.contexts:
        raise RuntimeError("attached browser has no contexts — open a tab in Chrome first")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else await context.new_page()
    return BrowserSession(pw=pw, context=context, page=page, mode="attach", cdp_url=cdp_url)


async def close_session(session: BrowserSession) -> None:
    log.info("closing session mode=%s", session.mode)
    try:
        if session.mode == "launch":
            await session.context.close()
        # attach mode: don't close the user's Chrome, just disconnect
    finally:
        await session.pw.stop()
