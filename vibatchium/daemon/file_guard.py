"""Browser-wide guard on ``file:`` loads (0.19.4).

``fspolicy.check_nav_url`` judges the URL a verb navigates to. But a ``file:``
page, once loaded, may itself navigate to, frame, or open a popup on any
other ``file:`` URL — Chrome lets file: reach file: — and none of those loads
pass through a verb. This guard closes that gap for every surface, CLI
included: it pauses every ``file:`` request the browser makes and lets it
through only if :func:`fspolicy.check_file_load` (deny list + the daemon's
``VIBATCHIUM_FILE_ROOTS``) allows the path; a denied one fails with
``net::ERR_ACCESS_DENIED``.

Why a BROWSER-level CDP ``Fetch.enable`` and not ``context.route``:

* ``context.route`` turns off Chrome's HTTP cache for the whole session (a
  stealth and speed cost); a Fetch interceptor whose only pattern is
  ``file://*`` never sees an http(s) request, and the cache stays on.
* On the browser target the interceptor sits in the network layer for every
  target of that browser — existing pages, new tabs, popups, iframes — so
  there is no window between a popup's creation and a per-page hook.
* It is installed by the launch/attach seam (``browser.launch_session`` /
  ``attach_session``, which the nodriver backend reuses), so a self-heal
  relaunch, a pre-warmed session and an attach all get it.

If the guard can't be installed (no browser handle, CDP refused) the launch
still succeeds and a warning is logged: agent surfaces don't depend on it —
they refuse ``file:`` navigation outright (``fspolicy.check_nav_url``).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from .. import fspolicy

log = logging.getLogger("vibatchium.file_guard")

_PATTERNS = [{"urlPattern": "file://*", "requestStage": "Request"}]


async def _decide(cdp, ev: dict) -> None:
    rid = ev.get("requestId")
    url = (ev.get("request") or {}).get("url") or ""
    try:
        reason = fspolicy.check_file_load(url)
    except Exception as exc:  # noqa: BLE001 — fail closed
        reason = f"{type(exc).__name__}: {exc}"
    try:
        if reason is None:
            await cdp.send("Fetch.continueRequest", {"requestId": rid})
        else:
            log.warning("file guard refused a file: load %s — %s", url, reason)
            await cdp.send("Fetch.failRequest",
                           {"requestId": rid, "errorReason": "AccessDenied"})
    except Exception as exc:  # noqa: BLE001 — target gone / request cancelled
        log.debug("file guard: could not settle %s: %s", url, exc)


async def install(session) -> bool:
    """Arm the guard on ``session``'s browser. Returns True if armed. Never
    raises: a failure is logged and the session keeps working."""
    try:
        browser = session.context.browser
        if browser is None:
            log.warning("file guard not installed: no browser handle")
            return False
        cdp = await browser.new_browser_cdp_session()
        cdp.on("Fetch.requestPaused",
               lambda ev: asyncio.ensure_future(_decide(cdp, ev)))
        await cdp.send("Fetch.enable", {"patterns": _PATTERNS})
    except Exception as exc:  # noqa: BLE001
        log.warning("file guard not installed (%s): %s", type(exc).__name__, exc)
        return False
    session.file_guard = cdp
    return True


async def uninstall(session) -> None:
    """Detach the guard (attach mode: the user's own Chrome outlives us)."""
    cdp = getattr(session, "file_guard", None)
    if cdp is None:
        return
    session.file_guard = None
    with contextlib.suppress(Exception):
        await cdp.send("Fetch.disable")
    with contextlib.suppress(Exception):
        await cdp.detach()
