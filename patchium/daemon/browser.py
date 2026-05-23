"""Patchwright browser lifecycle — launch persistent context OR attach over CDP."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from patchright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

log = logging.getLogger("patchium.browser")


@dataclass
class BrowserSession:
    """Holds the live Patchwright handles for the daemon's lifetime.

    `frame_ref` (when non-None) makes subsequent ops target that frame instead
    of the top-level page — used by the frame/frames verbs to switch iframe
    context. `dialog_handler` registers a one-shot dialog response when set.
    `downloads` records page.on('download') events keyed by index. `network`
    holds the network-capture ring buffer when capture is on.
    """

    pw: Playwright
    context: BrowserContext
    page: Page
    mode: str  # "launch" | "attach"
    profile_dir: Path | None = None
    cdp_url: str | None = None
    frame_ref: object = None         # patchright.Frame | None
    dialog_policy: dict = field(default_factory=lambda: {"action": "dismiss"})
    downloads: list = field(default_factory=list)
    network: dict = field(default_factory=lambda: {"capturing": False, "events": [], "max": 500})
    # Wave 5: when False, this session does NOT own its Playwright driver
    # subprocess — the daemon does (shared). close_session won't `.stop()` it.
    owns_pw: bool = True

    @property
    def target(self):
        """Return the active page-or-frame for verbs that should respect a
        currently-switched frame."""
        return self.frame_ref if self.frame_ref is not None else self.page


def _wire_page_tracking(session: BrowserSession) -> None:
    """Auto-track new pages (popups, target=_blank) and recover from page close.

    Without this, `s.page` becomes stale when:
    - JS opens a popup via `window.open` (new tab appears, session.page still old)
    - The active page closes (session.page points at a dead Page object)
    - target=_blank link navigates (new tab is now where the action is)

    We listen on the BrowserContext for new pages and bring them to the front
    of `s.page` if the caller hasn't explicitly switched tabs recently. Page
    close events trigger fallback to the newest remaining live page.
    """
    def on_new_page(page):
        # Make the new page the active one. Callers that want the old page
        # back can use `pages` + `page switch <index>`.
        session.page = page
        log.info("new page opened — switched session.page to %s", page.url or "(blank)")

        def on_close(_):
            try:
                live = [p for p in session.context.pages if not p.is_closed()]
                if live:
                    session.page = live[-1]
                    log.info("active page closed — fell back to %s", session.page.url)
            except Exception:  # noqa: BLE001
                pass
        page.on("close", on_close)

    session.context.on("page", on_new_page)
    # Hook close on existing pages too
    for p in session.context.pages:
        def make_handler(pg):
            def on_close(_):
                try:
                    live = [x for x in session.context.pages if not x.is_closed()]
                    if live:
                        session.page = live[-1]
                except Exception:  # noqa: BLE001
                    pass
            return on_close
        p.on("close", make_handler(p))


async def launch_session(profile_dir: Path, headless: bool = False,
                         *, pw: Playwright | None = None,
                         proxy: dict | None = None) -> BrowserSession:
    """Cold-launch real Chrome with persistent context (canonical Patchright config).

    The Playwright driver (Node.js subprocess) can be shared across multiple
    sessions when `pw` is supplied — Wave 5 multi-session passes one driver
    instance to N sessions to avoid spawning a Node.js subprocess per Chrome
    (which can exhaust file descriptors on long-running daemons with frequent
    session churn).

    `proxy`: optional Playwright proxy config dict
    `{server, username, password}`. When set, also injects WebRTC leak-guard
    Chrome flags so STUN can't bypass the proxy to expose the real IP.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    log.info("launch persistent context profile=%s headless=%s proxy=%s",
             profile_dir, headless, bool(proxy))

    owns_pw = pw is None
    if pw is None:
        pw = await async_playwright().start()
    # Chrome args for multi-session reliability:
    #   --disable-dev-shm-usage: write shared-memory files to /tmp instead of
    #     /dev/shm. /dev/shm defaults to 64MB in many containers/distros and
    #     is exhausted quickly when running ≥2 headless Chromes concurrently,
    #     manifesting as `Page.goto` timeouts on later-spawned sessions even
    #     though `launch_persistent_context` returned cleanly.
    extra_args = ["--disable-dev-shm-usage"] if headless else []
    # Wave 6.2a: WebRTC leak guard when a proxy is configured.
    if proxy:
        from ..proxy import webrtc_leak_guard_args
        extra_args = list(extra_args) + webrtc_leak_guard_args()
    launch_kwargs = {
        "user_data_dir": str(profile_dir),
        "channel": "chrome",
        "headless": headless,
        "no_viewport": True,
        "args": extra_args if extra_args else None,
    }
    if proxy:
        launch_kwargs["proxy"] = proxy
    context = await pw.chromium.launch_persistent_context(**launch_kwargs)
    page = context.pages[0] if context.pages else await context.new_page()
    sess = BrowserSession(pw=pw, context=context, page=page, mode="launch",
                          profile_dir=profile_dir, owns_pw=owns_pw)
    _wire_page_tracking(sess)
    return sess


async def attach_session(cdp_url: str, *, pw: Playwright | None = None) -> BrowserSession:
    """Attach to an already-running Chrome via `--remote-debugging-port=<port>`.

    The user's normal Chrome carries its real fingerprint (TLS, profile, cookies),
    which is the cleanest way past Cloudflare-class walls once a manual login has
    happened. Patchright's runtime-context isolation still applies on the client
    side regardless of how the browser was launched.
    """
    log.info("attach over CDP cdp_url=%s", cdp_url)
    owns_pw = pw is None
    if pw is None:
        pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp_url)
    if not browser.contexts:
        raise RuntimeError("attached browser has no contexts — open a tab in Chrome first")
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else await context.new_page()
    sess = BrowserSession(pw=pw, context=context, page=page, mode="attach",
                          cdp_url=cdp_url, owns_pw=owns_pw)
    _wire_page_tracking(sess)
    return sess


async def close_session(session: BrowserSession) -> None:
    log.info("closing session mode=%s owns_pw=%s", session.mode, session.owns_pw)
    try:
        if session.mode == "launch":
            await session.context.close()
        # attach mode: don't close the user's Chrome, just disconnect
    finally:
        # Only stop the Playwright driver if THIS session owns it.
        # In multi-session mode, the daemon owns a shared driver and stops it
        # on full daemon shutdown.
        if session.owns_pw:
            await session.pw.stop()
