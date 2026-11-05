"""Wait for a page's text to stop changing, within a bounded time.

`domcontentloaded` fires long before a client-rendered page has its content,
and a fixed "more than N characters" check passes on the site header alone:
measured on 2026-11-05, Myer's search page read 126 characters at that point
and 5,069 three seconds later, Officeworks 1,046 then 8,076. Text that stays
level is not enough either: Officeworks sat at 2,275 characters for a full
second with nothing in flight, then jumped to 7,988. So a short page has to
hold still for longer than a long one.
"""
from __future__ import annotations

import asyncio
import time

_TEXT_LEN_JS = "document.body ? document.body.innerText.trim().length : 0"

QUIET_S_LONG = 0.8       # page already has plenty of text
QUIET_S_SHORT = 1.5      # short page: more likely to be a shell still loading
LONG_PAGE_CHARS = 3000
POLL_S = 0.2
# A request older than this stops counting as "still loading". Analytics and
# long polls stay open forever, and would otherwise hold every page to the cap.
YOUNG_REQUEST_S = 1.5
_DATA_REQUESTS = ("xhr", "fetch", "document")


class RequestWatch:
    """Tracks data requests still in flight on a page, as a context manager."""

    def __init__(self, page) -> None:
        self._page = page
        self._live: dict = {}

    def _started(self, request) -> None:
        if request.resource_type in _DATA_REQUESTS:
            self._live[request] = time.monotonic()

    def _ended(self, request) -> None:
        self._live.pop(request, None)

    def __enter__(self) -> RequestWatch:
        self._page.on("request", self._started)
        self._page.on("requestfinished", self._ended)
        self._page.on("requestfailed", self._ended)
        return self

    def __exit__(self, *_exc) -> None:
        self._page.remove_listener("request", self._started)
        self._page.remove_listener("requestfinished", self._ended)
        self._page.remove_listener("requestfailed", self._ended)

    def busy(self) -> bool:
        now = time.monotonic()
        return any(now - t < YOUNG_REQUEST_S for t in self._live.values())


async def wait_settled(page, *, cap_ms: int = 5000, min_chars: int = 100,
                       watch: RequestWatch | None = None) -> dict:
    """Return once the page has at least `min_chars` of text and it has held
    still, or at `cap_ms`. Never raises: an empty page, a navigation in
    progress or a starved renderer just use up the time.

    Returns {"settled": bool, "chars": int}. `settled` False means the cap was
    reached, so the caller should say the page may still be rendering.
    """
    deadline = time.monotonic() + cap_ms / 1000
    last: int | None = None
    still_since = time.monotonic()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"settled": False, "chars": last or 0}
        try:
            # evaluate has no timeout of its own, and a busy page can starve it.
            chars = await asyncio.wait_for(page.evaluate(_TEXT_LEN_JS),
                                           timeout=min(remaining, 1.0))
        except Exception:  # noqa: BLE001 — navigating, closed or starved
            chars = None
        now = time.monotonic()
        moved = (chars is None or last is None
                 or abs(chars - last) > max(20, last * 0.01))
        if chars is not None and moved:
            last = chars
        if moved or (watch is not None and watch.busy()):
            still_since = now
        quiet = QUIET_S_LONG if (last or 0) >= LONG_PAGE_CHARS else QUIET_S_SHORT
        if (last or 0) > min_chars and now - still_since >= quiet:
            return {"settled": True, "chars": last}
        await asyncio.sleep(POLL_S)
