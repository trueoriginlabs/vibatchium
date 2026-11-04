"""File choosers: fill the native "Open file" dialog while an upload is armed.

``vb upload TARGET FILES`` needs an ``<input type=file>`` in the DOM. Many
upload buttons have none: they click a detached input (or one in a closed
shadow root), and Chrome opens the native OS dialog, which nothing in the page
can fill. A Playwright ``filechooser`` listener turns on CDP
``Page.setInterceptFileChooserDialog``: the native dialog is suppressed and the
page's chooser comes to us instead, and ``FileChooser.set_files`` completes it
as a trusted selection.

Interception is on only while an upload is armed:

* **armed** (``upload_next``): the listener goes on every page of the session
  (and pages opened meanwhile). The arm is bound to the page that was active
  when it was made: the first chooser from that page (any frame in it) gets
  the files, once. A chooser from any other page gets nothing and is counted
  as ``foreign``, so a background tab that clicks inputs on a timer can't
  harvest the files. Several files into a single-file chooser is refused, not
  truncated.
* **settled** (consumed, refused, failed, expired, or the page closed/crashed):
  the listener comes off again. An arm that no chooser takes expires on a
  timer, so interception never outlives it.
* **not armed**: no listener, Chrome behaves natively. Headless Chrome cancels
  the dialog itself; a headed ``vb show`` window shows the real one. Attach
  sessions follow the same rule, so the user's own tabs keep their uploads.

A chooser that arrives while intercepting but with no arm to give (a second
chooser from the same click, while the first is being filled) is swallowed
and counted. Choosers are logged at INFO once per page, then at DEBUG: a page
can open them in a loop, and the daemon log is shared.
"""
from __future__ import annotations

import asyncio
import logging
import time
import weakref

log = logging.getLogger("vibatchium.file_chooser")


def new_state() -> dict:
    return {"installed": False, "armed": None, "last": None,
            "swallowed": 0, "foreign": 0,
            # _current: the newest arm until it settles (pending or being
            # filled); an outcome for any other arm is stale and dropped.
            "_current": None, "_timer": None, "_watch": None,
            "_handler": None, "_page_hook": None,
            "_logged": weakref.WeakSet(), "_settled": asyncio.Event()}


def _note(state: dict, page, what: str, url) -> None:
    """INFO for a page's first unfilled chooser, DEBUG after that."""
    try:
        first = page not in state["_logged"]
        state["_logged"].add(page)
    except TypeError:
        first = True
    log.log(logging.INFO if first else logging.DEBUG,
            "file chooser %s on %s", what, url)


def _settle(session, arm: dict, outcome: dict) -> None:
    """Record ``arm``'s outcome, wake ``upload_status`` waiters and stop
    intercepting. A no-op if ``arm`` already settled or was replaced by a
    newer arm (a re-arm while the old one was being filled)."""
    state = session.file_chooser
    if state["_current"] is not arm:
        log.info("file chooser outcome for a replaced arm dropped: %s",
                 {k: outcome.get(k) for k in ("consumed", "error", "url")})
        return
    state["_current"] = None
    if state["armed"] is arm:
        state["armed"] = None
    state["last"] = outcome
    state["_settled"].set()
    uninstall(session)


def _expire(session, arm: dict) -> None:
    if session.file_chooser["armed"] is arm:
        _settle(session, arm, {"consumed": False, "expired": True,
                               "files": arm["files"], "at": time.time(),
                               "url": None})


def _expire_if_due(session) -> None:
    arm = session.file_chooser["armed"]
    if arm and time.time() > arm["expires_at"]:
        _expire(session, arm)


async def _on_chooser(session, chooser) -> None:
    state = session.file_chooser
    page = chooser.page
    page_url = page.url if page else None
    _expire_if_due(session)
    arm = state["armed"]
    if not arm:
        state["swallowed"] += 1
        _note(state, page, "swallowed (not armed)", page_url)
        return
    if page is not arm["page"]:
        state["foreign"] += 1
        _note(state, page, "ignored (armed for another page)", page_url)
        return
    # Take the arm before the first await: a second chooser arriving while
    # this one is being filled must find nothing to take.
    state["armed"] = None
    multiple = chooser.is_multiple()
    try:
        frame = await chooser.element.owner_frame()
        url = frame.url if frame else page_url
    except Exception:  # noqa: BLE001 — element/frame gone
        url = page_url
    base = {"files": arm["files"], "multiple": multiple, "url": url,
            "page_url": page_url}
    if len(arm["resolved"]) > 1 and not multiple:
        _settle(session, arm, {"consumed": False, **base,
                               "error": f"{len(arm['files'])} files armed but "
                                        f"this chooser takes one",
                               "at": time.time()})
        return
    try:
        await chooser.set_files(arm["resolved"])
    except Exception as exc:  # noqa: BLE001 — page closed / input gone
        _settle(session, arm, {"consumed": False, **base,
                               "error": f"{type(exc).__name__}: {exc}",
                               "at": time.time()})
        return
    _settle(session, arm, {"consumed": True, **base, "at": time.time()})


def install(session) -> bool:
    """Listen for choosers on every page of ``session``'s context. Idempotent.
    Never raises: a failure is logged and Chrome keeps its native dialogs.
    Returns whether it is installed."""
    state = session.file_chooser
    if state["installed"]:
        return True

    def handler(chooser):
        asyncio.ensure_future(_on_chooser(session, chooser))

    def on_page(page):
        page.on("filechooser", handler)

    try:
        session.context.on("page", on_page)
        for p in session.context.pages:
            p.on("filechooser", handler)
    except Exception as exc:  # noqa: BLE001
        log.warning("file chooser interception not installed (%s): %s",
                    type(exc).__name__, exc)
        state.update(_handler=handler, _page_hook=on_page)
        uninstall(session, force=True)
        return False
    state.update(installed=True, _handler=handler, _page_hook=on_page)
    return True


def _drop_arm_hooks(state: dict) -> None:
    """Cancel the expiry timer and the armed page's close/crash watch."""
    if state["_timer"] is not None:
        state["_timer"].cancel()
        state["_timer"] = None
    watch, state["_watch"] = state["_watch"], None
    if watch is not None:
        page, hooks = watch
        for ev, fn in hooks:
            try:
                page.remove_listener(ev, fn)
            except Exception:  # noqa: BLE001
                pass


def uninstall(session, *, force: bool = False) -> None:
    """Drop the listener everywhere (Chrome shows native dialogs again), the
    armed page's close/crash watch and the expiry timer."""
    state = session.file_chooser
    _drop_arm_hooks(state)
    if not (state["installed"] or force):
        return
    try:
        pages = list(session.context.pages)
    except Exception:  # noqa: BLE001 — context already gone
        pages = []
    for p in pages:
        try:
            p.remove_listener("filechooser", state["_handler"])
        except Exception:  # noqa: BLE001
            pass
    try:
        session.context.remove_listener("page", state["_page_hook"])
    except Exception:  # noqa: BLE001
        pass
    state.update(installed=False, _handler=None, _page_hook=None)


def arm(session, files: list, resolved: list, timeout_s: float) -> dict:
    """Arm the next chooser of the active page with ``resolved`` (already
    fspolicy-checked). Replaces any earlier arm."""
    state = session.file_chooser
    page = session.page
    _drop_arm_hooks(state)        # an earlier arm's timer and watch
    if not install(session):
        raise RuntimeError("file chooser interception could not be installed on "
                           "this session (see the daemon log)")
    new = {"files": files, "resolved": resolved, "page": page,
           "expires_at": time.time() + timeout_s}
    state["_settled"].set()       # wake anyone waiting on the replaced arm
    state.update(armed=new, _current=new, last=None, _settled=asyncio.Event())

    def gone(what):
        def on_gone(_):
            if state["armed"] is new:
                _settle(session, new, {"consumed": False, "files": files,
                                       "error": f"the armed page {what} before "
                                                f"a chooser opened; re-arm",
                                       "at": time.time(), "url": None})
        return on_gone

    hooks = [("close", gone("closed")), ("crash", gone("crashed"))]
    try:
        for ev, fn in hooks:
            page.on(ev, fn)
        state["_watch"] = (page, hooks)
    except Exception:  # noqa: BLE001
        pass
    state["_timer"] = asyncio.get_running_loop().call_later(
        timeout_s, _expire, session, new)
    return status(session)


def abandon(session, reason: str) -> None:
    """Settle a pending arm with ``reason`` as its error and stop intercepting:
    the session is closing, or a self-heal relaunch is replacing its browser
    (the caller then hands this state to the new session, so ``upload_status``
    there reports why the arm is gone)."""
    state = session.file_chooser
    arm = state["_current"]
    if arm is not None:
        _settle(session, arm, {"consumed": False, "files": arm["files"],
                               "error": reason, "at": time.time(), "url": None})
    else:
        uninstall(session)


async def wait_settled(session, wait_s: float) -> None:
    state = session.file_chooser
    if state["_current"] is not None and wait_s > 0:
        try:
            await asyncio.wait_for(state["_settled"].wait(), wait_s)
        except TimeoutError:
            pass


def status(session) -> dict:
    state = session.file_chooser
    _expire_if_due(session)
    arm = state["armed"]
    out = {"armed": bool(arm), "intercepting": state["installed"],
           "native": not state["installed"], "swallowed": state["swallowed"],
           "foreign": state["foreign"],
           "consumed": bool(state["last"] and state["last"].get("consumed"))}
    if arm:
        out["files"] = arm["files"]
        out["expires_in_s"] = round(arm["expires_at"] - time.time(), 1)
    if state["last"]:
        out["last"] = state["last"]
    return out
