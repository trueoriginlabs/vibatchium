"""Multi-session registry — the heart of Wave 5.

Each entry corresponds to one running BrowserSession (one persistent-context
Chrome instance, one profile dir). The daemon holds a `SessionRegistry`
mapping `name → SessionEntry`. The current-call's session is selected via a
`ContextVar` (set in the dispatcher) so individual handlers can read/write
state via convenience properties on the daemon without taking explicit
session arguments — the existing `(d, args)` handler signature still works.

### Design choice: arch B (multi persistent-context, one daemon process)

Patchright's stealth is documented only for `launch_persistent_context()`
(per its README + Issue #46). Each persistent context is a separate Chrome
process → independent TLS/GPU/audio fingerprint, independent ephemeral
ports, real OS-level isolation. Multiple persistent contexts in one daemon
process is the sweet spot:

- Single daemon = simple IPC + simple MCP routing
- N Chrome processes = real fingerprint isolation per session
- Profile↔session 1:1 enforced by OS user-data-dir lock
- ~200-400 MB RAM per Chrome; default cap 4 sessions (PATCHIUM_MAX_SESSIONS)

The interface is shaped so a future "remote session" (process-per-session,
arch C) could be added behind the same `SessionEntry` surface without
breaking handlers — `entry.session` would just point at a proxy object.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from patchright.async_api import Playwright, async_playwright

from .browser import BrowserSession, attach_session, close_session, launch_session
from .paths import (
    DEFAULT_SESSION_NAME,
    PROFILES_DIR,
    get_active_session_name,
    list_session_names,
    session_dir,
    set_active_session_name,
)

log = logging.getLogger("patchium.registry")


# The contextvar that carries the current-call's session name through the
# async task. Set by the dispatcher before invoking a handler; read by the
# daemon's session-resolving properties.
current_session_ctx: ContextVar[str] = ContextVar(
    "patchium_current_session", default=DEFAULT_SESSION_NAME
)


def get_max_sessions() -> int:
    """Concurrent-session cap — read at every call so it's testable."""
    try:
        return max(1, int(os.environ.get("PATCHIUM_MAX_SESSIONS", "4")))
    except ValueError:
        return 4


@dataclass
class SessionEntry:
    """All per-session state lives here.

    What used to live on `Daemon` (session, _snapshot, _handles, _lock) moves
    onto the entry; the daemon resolves which entry to operate on via the
    `current_session_ctx` ContextVar. Handlers can keep using `d.session`,
    `d._snapshot`, etc. — those are now properties on Daemon that route to
    the active entry transparently.
    """
    name: str
    profile_dir: Path
    session: BrowserSession
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # AX snapshot cache (was Daemon._snapshot / _prev_snapshot)
    snapshot: object = None
    prev_snapshot: object = None
    # JSHandle table (was Daemon._handles / _handle_counter)
    handles: dict = field(default_factory=dict)
    handle_counter: int = 0
    # Misc per-session flags (extensible)
    flags: dict = field(default_factory=dict)
    # Bookkeeping
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_used_at = time.time()


class SessionLimitError(RuntimeError):
    """Raised when PATCHIUM_MAX_SESSIONS would be exceeded."""


class SessionRegistry:
    """Holds all live sessions; serializes registry mutations with `mutate_lock`.

    Per-session locks live ON the entry (`entry.lock`) so concurrent operations
    on DIFFERENT sessions don't block each other — `patchium --session A click @e1`
    and `patchium --session B fill @e2 hello` run truly in parallel.

    The `mutate_lock` only serializes session create/close/delete events.
    """

    def __init__(self) -> None:
        self._entries: dict[str, SessionEntry] = {}
        self.mutate_lock = asyncio.Lock()
        # Wave 5: one Playwright driver subprocess shared across all sessions.
        # Spawned lazily on the first create/attach and torn down on full
        # daemon shutdown. Per-session driver subprocess would saturate fds
        # on long-running daemons with frequent session churn.
        self._pw: Playwright | None = None

    async def _ensure_pw(self) -> Playwright:
        if self._pw is None:
            self._pw = await async_playwright().start()
            log.info("started shared Playwright driver")
        return self._pw

    async def _maybe_stop_pw(self) -> None:
        """Stop the shared Playwright driver when no sessions are running."""
        if self._pw is not None and not self._entries:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            log.info("stopped shared Playwright driver (no sessions)")

    # ─── lookups ─────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[SessionEntry]:
        entry = self._entries.get(name)
        if entry is not None:
            entry.touch()
        return entry

    def has(self, name: str) -> bool:
        return name in self._entries

    def list_running(self) -> list[str]:
        return sorted(self._entries.keys())

    def list_all(self) -> list[dict]:
        """List every on-disk session plus its running state.

        Returns rows of `{"name", "running", "profile_dir", "url", "title", "mode"}`.
        Profiles that have never been started appear with `running: false`.
        """
        running = set(self._entries.keys())
        on_disk = set(list_session_names())
        names = sorted(running | on_disk)
        out = []
        for name in names:
            entry = self._entries.get(name)
            row = {
                "name": name,
                "running": entry is not None,
                "profile_dir": str(session_dir(name)) if name in on_disk else None,
            }
            if entry is not None:
                row["mode"] = entry.session.mode
                try:
                    row["url"] = entry.session.page.url
                except Exception:  # noqa: BLE001
                    row["url"] = None
                try:
                    row["pages"] = len(entry.session.context.pages)
                except Exception:  # noqa: BLE001
                    row["pages"] = None
            out.append(row)
        return out

    # ─── lifecycle ───────────────────────────────────────────────────────

    async def create(
        self,
        name: str,
        *,
        profile_dir: Path | None = None,
        headless: bool = False,
        stealth_mouse: bool = False,
        backend: str = "patchright",
    ) -> SessionEntry:
        """Launch Chrome for a new session.

        Args:
          name: session identifier (also used as profile dir basename when
                profile_dir is None).
          profile_dir: explicit user-data-dir; defaults to PROFILES_DIR/<name>.
          headless: opt out of headed mode (NOT recommended for stealth).
          stealth_mouse: layer CDP-Patches humanized input.
          backend: 'patchright' (default), 'nodriver', or 'auto'.
                   nodriver requires `pip install patchium[nodriver]` and
                   uses its hardened launch flags + Patchright connect_over_cdp.

        Raises:
          SessionLimitError if PATCHIUM_MAX_SESSIONS would be exceeded.
          RuntimeError if a session with this name is already running.
        """
        if name in self._entries:
            raise RuntimeError(
                f"session {name!r} already running — "
                f"use `patchium --session {name} stop` first"
            )
        cap = get_max_sessions()
        if len(self._entries) >= cap:
            raise SessionLimitError(
                f"PATCHIUM_MAX_SESSIONS={cap} reached "
                f"({len(self._entries)} sessions running). "
                f"Close one with `patchium session close <name>` or raise the cap."
            )
        pdir = profile_dir if profile_dir is not None else session_dir(name)
        pdir.mkdir(parents=True, exist_ok=True)
        pw = await self._ensure_pw()
        from . import backends as _backends
        sess = await _backends.launch(backend, pdir, headless=headless, pw=pw)
        sess.flags = getattr(sess, "flags", {}) if hasattr(sess, "flags") else {}
        if stealth_mouse:
            # If the optional stealth-mouse layer can't install, tear down the
            # Chrome we just spawned so we don't leak a process. The caller's
            # exception propagates up.
            from ..stealth import install_humanized_mouse
            try:
                await install_humanized_mouse(sess)
            except Exception:
                try:
                    await _backends.close(sess)
                except Exception:  # noqa: BLE001
                    pass
                raise
        entry = SessionEntry(name=name, profile_dir=pdir, session=sess)
        # Stash backend choice on the entry so observability tools and the
        # status handler can report which stealth stack is in use per session.
        entry.flags["backend"] = backend
        self._entries[name] = entry
        log.info("session created name=%s profile=%s mode=%s backend=%s",
                 name, pdir, sess.mode, backend)
        return entry

    async def attach(self, name: str, cdp_url: str) -> SessionEntry:
        """Register a session by attaching to an existing Chrome over CDP.

        The profile_dir is recorded as the session-name path even though
        Chrome's actual profile lives wherever the foreign process pointed
        `--user-data-dir`. We don't try to sync — `attach` is for manual-login
        flows where the user already controls the profile location.
        """
        if name in self._entries:
            raise RuntimeError(f"session {name!r} already attached")
        cap = get_max_sessions()
        if len(self._entries) >= cap:
            raise SessionLimitError(f"PATCHIUM_MAX_SESSIONS={cap} reached")
        pw = await self._ensure_pw()
        sess = await attach_session(cdp_url, pw=pw)
        entry = SessionEntry(name=name, profile_dir=session_dir(name), session=sess)
        self._entries[name] = entry
        log.info("session attached name=%s cdp_url=%s", name, cdp_url)
        return entry

    async def close(self, name: str) -> bool:
        """Stop Chrome for a session; profile dir is preserved on disk.

        Returns True if the session was running and is now closed.
        Idempotent — closing an absent session returns False without error.
        """
        entry = self._entries.pop(name, None)
        if entry is None:
            return False
        # Best-effort dispose handles before closing the browser
        for h in list(entry.handles.values()):
            try:
                await h.dispose()
            except Exception:  # noqa: BLE001
                pass
        entry.handles.clear()
        from . import backends as _backends
        try:
            await _backends.close(entry.session)
        except Exception as exc:  # noqa: BLE001
            log.warning("close_session(%s) failed: %s", name, exc)
        log.info("session closed name=%s", name)
        return True

    async def close_all(self) -> int:
        """Stop every running session. Returns the count closed.

        Also tears down the shared Playwright driver when the last session
        exits — frees its Node.js subprocess.
        """
        names = list(self._entries.keys())
        n = 0
        for name in names:
            if await self.close(name):
                n += 1
        await self._maybe_stop_pw()
        return n

    def delete_profile_dir(self, name: str) -> bool:
        """Remove the on-disk profile dir. Refuses to delete a running session
        or the special 'default' name."""
        if name == DEFAULT_SESSION_NAME:
            raise ValueError("cannot delete the 'default' profile")
        if name in self._entries:
            raise RuntimeError(
                f"session {name!r} is running — close it first"
            )
        pdir = PROFILES_DIR / name
        if not pdir.exists():
            return False
        shutil.rmtree(pdir)
        log.info("profile dir deleted name=%s", name)
        return True
