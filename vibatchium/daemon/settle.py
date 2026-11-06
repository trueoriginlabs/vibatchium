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

QUIET_MS_LONG = 800      # page already has plenty of text
QUIET_MS_SHORT = 1500    # short page: more likely to be a shell still loading
LONG_PAGE_CHARS = 3000
POLL_MS = 200
# A request older than this stops counting as "still loading". Analytics and
# long polls stay open forever, and would otherwise hold every page to the cap.
YOUNG_REQUEST_S = 1.5
_DATA_REQUESTS = ("xhr", "fetch", "document")

# Polls inside the page, in one call. Every evaluate from the driver hands the
# page a user activation (Playwright sends userGesture), so polling from Python
# would keep pages "activated" for the whole wait, letting them open popups and
# file dialogs. `s` keeps its state between polls: Playwright reuses the arg.
_STILL_JS = """(s) => {
  if (document.body && document.body.tagName === "FRAMESET") return true;  // text is in its frames
  const n = document.body ? document.body.innerText.trim().length : 0;
  const now = performance.now();
  if (s.last == null || Math.abs(n - s.last) > Math.max(20, s.last * 0.01)) {
    s.last = n;
    s.since = now;
  }
  const quiet = s.last >= s.longPage ? s.quietLong : s.quietShort;
  return s.last > s.minChars && now - s.since >= quiet;
}"""


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
                       watch: RequestWatch | None = None) -> bool:
    """True once the page has more than `min_chars` of text and it has held
    still. False at `cap_ms`, so the caller should say the page may still be
    rendering. Never raises: an empty page, a navigation in progress or a
    starved renderer just use up the time.
    """
    deadline = time.monotonic() + cap_ms / 1000
    while (remaining := deadline - time.monotonic()) > 0:
        state = {"minChars": min_chars, "longPage": LONG_PAGE_CHARS,
                 "quietLong": QUIET_MS_LONG, "quietShort": QUIET_MS_SHORT}
        try:
            await page.wait_for_function(
                _STILL_JS, arg=state, polling=POLL_MS, timeout=remaining * 1000)
        except Exception:  # noqa: BLE001 — timed out, navigating or closed
            await asyncio.sleep(min(POLL_MS / 1000, max(0.0, remaining)))
            continue
        if watch is None or not watch.busy():
            return True
        await asyncio.sleep(POLL_MS / 1000)
    return False
