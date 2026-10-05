"""Guards around a vault secret that is live in a page (0.19.4).

Origin binding decides where `fill --use-secret` may write. These guards cover
what happens around the write: the agent that triggered the fill must not be
able to read the value back, nor to have prepared the page beforehand so it
reads the value for it.

* **Read-back refusal.** While a field we filled from the vault still holds a
  value, every verb that returns an input's value or runs caller-supplied JS
  (`value`, `eval`, `wait_fn`, `eval_handle`, `handle_eval`,
  `detect_forms values=true`) is refused. Scrubbing their output would not be
  enough: caller JS can re-encode the value however it likes.
* **Caller-JS taint.** Those same JS verbs (and `content`, which rewrites the
  document while keeping its origin; `go javascript:...`, which runs its body
  in the current document's main world; `fingerprint extract=...`; and a
  `route_add --mode fulfill` response, which is caller-written bytes served AS
  the requested origin) mark the page's current document. A secret
  fill into a marked document is refused: script the caller already ran there
  could be waiting to read or redirect the value, and the write itself runs in
  the isolated world that caller JS shares. A document is identified by its
  CDP loader id, so a navigation or reload clears the mark and an in-page
  `history.pushState` does not.

`VIBATCHIUM_SECRET_ALLOW_READBACK=1` in the daemon's env turns both off. It is
read from the daemon's environment only; no verb argument can set it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref

from .. import secrets as _secrets

log = logging.getLogger("vibatchium.secret_guard")

# The verbs refused while a secret is live. Kept here so the docs, the tests
# and the handlers agree on one list.
READBACK_VERBS = ("value", "eval", "wait_fn", "eval_handle", "handle_eval",
                  "detect_forms")

# page -> {main-frame loaderId in which caller JS ran}; "*" = could not tell
_TAINT: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# page -> CDPSession (cached; recreated if it dies)
_CDP: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
# page -> [ElementHandle] of fields we filled from the vault
_FILLED: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_MAX_TRACKED = 16

# Every element in the document (through OPEN shadow roots) that we tagged as a
# vault fill and that still holds a value. Isolated world, read-only.
_LIVE_SWEEP_JS = """() => {
  const seen = new Set();
  const walk = (root) => {
    for (const el of root.querySelectorAll('[data-vb-secret]')) {
      if (el.value) return true;
    }
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot && !seen.has(el.shadowRoot)) {
        seen.add(el.shadowRoot);
        if (walk(el.shadowRoot)) return true;
      }
    }
    return false;
  };
  return walk(document);
}"""

_HANDLE_LIVE_JS = """el => !!(el && el.isConnected
  && el.hasAttribute && el.hasAttribute('data-vb-secret') && el.value)"""


class SecretReadbackError(PermissionError):
    """A verb that could read a live vault secret was refused."""


class CallerJsTaintError(_secrets.SecretOriginError):
    """Caller JS ran in the document a secret was about to be written into."""


# ─── CDP: document identity ─────────────────────────────────────────────────

async def _cdp(page):
    sess = _CDP.get(page)
    if sess is None:
        sess = await page.context.new_cdp_session(page)
        _CDP[page] = sess
    return sess


async def main_loader_id(page) -> str | None:
    """The loader id of `page`'s current main-frame document, or None if it
    can't be read. A new document (navigation, reload) gets a new id; a
    same-document navigation keeps it."""
    for _attempt in range(2):
        try:
            sess = await _cdp(page)
            tree = await asyncio.wait_for(sess.send("Page.getFrameTree"), 5)
            return tree["frameTree"]["frame"].get("loaderId") or None
        except Exception:  # noqa: BLE001 — stale session: drop and retry once
            _CDP.pop(page, None)
    return None


# ─── caller-JS taint ────────────────────────────────────────────────────────

async def record_caller_js(page) -> None:
    """Mark `page`'s current document as having run caller-supplied JS. Call
    BEFORE running it, so a call that hangs or throws still marks."""
    if page is None:
        return
    lid = await main_loader_id(page)
    _TAINT.setdefault(page, set()).add(lid or "*")


def is_javascript_url(url) -> bool:
    """True if a browser would treat `url` as a `javascript:` URL. Navigating a
    page to one runs its body as script IN the current document's main world —
    caller JS by another name. The URL parser strips leading/trailing C0
    controls and spaces and drops tab/CR/LF anywhere, so `" java\\tscript:"`
    counts too."""
    if not isinstance(url, str):
        return False
    s = url.translate({9: None, 10: None, 13: None}).strip("".join(
        chr(c) for c in range(0x21)))
    return s[:11].lower() == "javascript:"


async def record_fulfilled(request) -> None:
    """A `route_add --mode fulfill` rule just answered `request` with caller-
    written bytes. Mark the document they end up in as touched by caller JS:
    a fulfilled main-frame navigation becomes the NEXT document of the page (so
    mark it once it commits), anything else (script, iframe, XHR) lands in the
    current one."""
    if _secrets.readback_env_enabled():
        return
    try:
        frame = request.frame
        page = frame.page
        nav = request.is_navigation_request() and frame.parent_frame is None
    except Exception:  # noqa: BLE001 — service-worker requests have no frame
        return
    if not nav:
        await record_caller_js(page)
        return
    # Until the fulfilled document commits we can't know its loader id, so the
    # whole page counts as touched; the framenavigated hook then pins the mark
    # to that document's id.
    _TAINT.setdefault(page, set()).add("pending")

    def _on_nav(fr):
        if fr is not frame:
            return
        with contextlib.suppress(Exception):
            page.remove_listener("framenavigated", _on_nav)

        async def _pin():
            await record_caller_js(page)
            marks = _TAINT.get(page)
            if marks is not None:
                marks.discard("pending")
        asyncio.get_running_loop().create_task(_pin())

    page.on("framenavigated", _on_nav)


async def _page_tainted(page) -> bool:
    marks = _TAINT.get(page)
    if not marks:
        return False
    if "*" in marks or "pending" in marks:
        return True
    lid = await main_loader_id(page)
    return lid is None or lid in marks


async def caller_js_violation(page, pre_origin, site: str) -> str | None:
    """Why a secret must not be written into `page` now, or None.

    Refuses when caller JS ran in the target page's current document, or in
    another page of the same context whose top document has the target's
    origin (it could reach the target through `window.opener` / a named
    window)."""
    if _secrets.readback_env_enabled():
        return None
    reason = (
        f"refusing to fill a {site!r} secret: caller-supplied JavaScript "
        f"(eval / wait_fn / eval_handle / handle_eval / content / "
        f"fingerprint extract / a `go javascript:` URL / a route_add fulfill "
        f"response) ran in {{where}} since it loaded, and could read or "
        f"redirect the value. "
        f"`reload` (or `go` to the login page again) and fill the secret "
        f"before running any eval there.")
    if await _page_tainted(page):
        return reason.format(where="this page")
    try:
        others = [p for p in page.context.pages if p is not page]
    except Exception:  # noqa: BLE001
        others = []
    for other in others:
        if other in _TAINT and \
                _secrets.url_origin(other.main_frame.url) == pre_origin and \
                await _page_tainted(other):
            return reason.format(where="another tab of this session on the "
                                       "same origin")
    return None


# ─── live-secret tracking + read-back refusal ───────────────────────────────

def track_secret_field(page, handle) -> bool:
    """Remember a field we filled from the vault. Returns True if the caller
    must NOT dispose `handle` (we kept it)."""
    handles = _FILLED.setdefault(page, [])
    handles.append(handle)
    while len(handles) > _MAX_TRACKED:
        old = handles.pop(0)
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().create_task(old.dispose())
    return True


async def secret_live(page) -> bool:
    """True if a field we filled from the vault still holds a value anywhere
    in `page` (any frame). Fails closed: a frame we can't read in time counts
    as live."""
    handles = _FILLED.get(page) or []
    keep = []
    live = False
    for h in handles:
        if live:
            keep.append(h)
            continue
        try:
            if await asyncio.wait_for(h.evaluate(_HANDLE_LIVE_JS), 3):
                live = True
                keep.append(h)
            else:
                with contextlib.suppress(Exception):
                    await h.dispose()
        except TimeoutError:
            live = True          # page wedged — can't prove it's gone
            keep.append(h)
        except Exception:  # noqa: BLE001 — context destroyed / detached
            with contextlib.suppress(Exception):
                await h.dispose()
    if handles:
        _FILLED[page] = keep
    if live:
        return True
    frames = [f for f in page.frames if not f.is_detached()]

    async def probe(frame) -> bool:
        for _attempt in range(2):
            try:
                return bool(await asyncio.wait_for(
                    frame.evaluate(_LIVE_SWEEP_JS), 3))
            except TimeoutError:
                return True
            except Exception:  # noqa: BLE001
                if frame.is_detached():
                    return False
                await asyncio.sleep(0.2)
        return True              # live frame we can't read: fail closed

    results = await asyncio.gather(*(probe(f) for f in frames))
    return any(results)


def readback_refusal(verb: str) -> str:
    return (f"refusing `{verb}`: a vault secret is live in a field on this "
            f"page (or another tab of this session) — submit or navigate first, or `fill <target> \"\"` to "
            f"clear it. (Operator override: "
            f"{_secrets.ALLOW_READBACK_ENV}=1 in the daemon's environment.)")


async def refuse_secret_readback(entry, verb: str) -> None:
    """Raise SecretReadbackError if `verb` could read a live vault secret in
    the session. Free when the session never filled one.

    Every tab of the session is checked, not only the current one: script in
    one tab can reach a same-origin tab it opened (or was opened by)."""
    if entry is None or not entry.flags.get("secret_filled"):
        return
    if _secrets.readback_env_enabled():
        return
    session = getattr(entry, "session", None)
    page = getattr(session, "page", None)
    if page is None:
        return
    try:
        pages = list(page.context.pages)
    except Exception:  # noqa: BLE001
        pages = [page]
    if page not in pages:
        pages.append(page)
    for p in pages:
        if await secret_live(p):
            raise SecretReadbackError(readback_refusal(verb))


def fulfill_route_violation(session, site: str) -> str | None:
    """Why a secret must not be written while the session has a
    `route_add --mode fulfill` rule installed, or None. Such a rule can answer
    any request — the login page itself, or one of its scripts — with caller
    bytes that the browser attributes to the real origin, so origin binding
    can't vouch for the document."""
    if _secrets.readback_env_enabled():
        return None
    rules = getattr(session, "_routes", None) or []
    live = [r.get("pattern") for r in rules if r.get("mode") == "fulfill"]
    if not live:
        return None
    return (f"refusing to fill a {site!r} secret: a `route_add --mode fulfill` "
            f"rule is active in this session ({', '.join(map(repr, live))}); it "
            f"can serve caller-written content as any origin. `route_clear`, "
            f"then `reload` the login page and fill again.")


async def guard_caller_js(entry, verb: str, page) -> None:
    """The one call a caller-JS verb makes before running caller code: refuse
    if a secret is live, else mark the document as touched."""
    await refuse_secret_readback(entry, verb)
    if not _secrets.readback_env_enabled():
        await record_caller_js(page)
