"""File choosers: intercept the native "Open file" dialog, fill it when armed.

``vb upload TARGET FILES`` needs an ``<input type=file>`` in the DOM. Many
upload buttons have none: they click a detached input (or one in a closed
shadow root), and Chrome opens the native OS dialog, which nothing in the page
can fill. A Playwright ``filechooser`` listener turns on CDP
``Page.setInterceptFileChooserDialog``: the native dialog is suppressed and the
page's chooser comes to us instead, and ``FileChooser.set_files`` completes it
as a trusted selection.

So every launched session listens, on every page (popups and new tabs
included):

* **armed** (``upload_next``): the next chooser gets the armed files, once. An
  arm expires after its timeout, so a chooser the page opens later on its own
  can't harvest them. Several files into a single-file chooser is refused,
  not truncated.
* **not armed**: the chooser is swallowed and counted. No native dialog
  appears, so a click can't leave a modal window stalling an unattended or
  headed session (or sitting on the operator's screen).
* **native** (``upload_native on``): no listener, Chrome shows its own dialog,
  for a person driving a ``vb show`` window by hand. Arming turns the
  listener back on.

Not installed on attach sessions (the browser is the user's own; their upload
buttons must keep working) until an ``upload_next`` asks for it.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("vibatchium.file_chooser")


def new_state() -> dict:
    return {"installed": False, "armed": None, "last": None,
            "swallowed": 0, "_handler": None, "_page_hook": None,
            "_settled": asyncio.Event()}


def _settle(state: dict, outcome: dict) -> None:
    state["armed"] = None
    state["last"] = outcome
    state["_settled"].set()


async def _on_chooser(state: dict, chooser) -> None:
    arm = state["armed"]
    url = chooser.page.url if chooser.page else None
    multiple = chooser.is_multiple()
    if arm and time.time() > arm["expires_at"]:
        _settle(state, {"consumed": False, "expired": True, "files": arm["files"],
                        "at": time.time(), "url": url})
        arm = None
    if not arm:
        state["swallowed"] += 1
        log.info("file chooser swallowed (not armed) on %s", url)
        return
    if len(arm["resolved"]) > 1 and not multiple:
        _settle(state, {"consumed": False, "files": arm["files"], "multiple": False,
                        "error": f"{len(arm['files'])} files armed but this chooser "
                                 f"takes one", "at": time.time(), "url": url})
        return
    try:
        await chooser.set_files(arm["resolved"])
    except Exception as exc:  # noqa: BLE001 — page closed / input gone
        _settle(state, {"consumed": False, "files": arm["files"], "multiple": multiple,
                        "error": f"{type(exc).__name__}: {exc}",
                        "at": time.time(), "url": url})
        return
    _settle(state, {"consumed": True, "files": arm["files"], "multiple": multiple,
                    "at": time.time(), "url": url})


def install(session) -> bool:
    """Listen for choosers on every page of ``session``'s context. Idempotent.
    Never raises, like the file: guard: a failure is logged, the launch goes
    on, and Chrome keeps its native dialogs. Returns whether it is installed."""
    state = session.file_chooser
    if state["installed"]:
        return True

    def handler(chooser):
        asyncio.ensure_future(_on_chooser(state, chooser))

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


def uninstall(session, *, force: bool = False) -> None:
    """Drop the listener everywhere; Chrome shows native dialogs again."""
    state = session.file_chooser
    if not (state["installed"] or force):
        return
    for p in session.context.pages:
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
    """Arm the next chooser with ``resolved`` (already fspolicy-checked)."""
    state = session.file_chooser
    if not install(session):
        raise RuntimeError("file chooser interception could not be installed on "
                           "this session (see the daemon log)")
    state["armed"] = {"files": files, "resolved": resolved,
                      "expires_at": time.time() + timeout_s}
    state["last"] = None
    state["_settled"] = asyncio.Event()
    return status(session)


async def wait_settled(session, wait_s: float) -> None:
    state = session.file_chooser
    if state["armed"] and wait_s > 0:
        try:
            await asyncio.wait_for(state["_settled"].wait(), wait_s)
        except TimeoutError:
            pass


def status(session) -> dict:
    state = session.file_chooser
    arm = state["armed"]
    if arm and time.time() > arm["expires_at"]:
        _settle(state, {"consumed": False, "expired": True, "files": arm["files"],
                        "at": time.time(), "url": None})
        arm = None
    out = {"armed": bool(arm), "intercepting": state["installed"],
           "native": not state["installed"], "swallowed": state["swallowed"],
           "consumed": bool(state["last"] and state["last"].get("consumed"))}
    if arm:
        out["files"] = arm["files"]
        out["expires_in_s"] = round(arm["expires_at"] - time.time(), 1)
    if state["last"]:
        out["last"] = state["last"]
    return out
