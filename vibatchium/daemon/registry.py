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
- ~200-400 MB RAM per Chrome; default cap 4 sessions (VIBATCHIUM_MAX_SESSIONS)

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

from patchright.async_api import Playwright, async_playwright

from .browser import BrowserSession, attach_session
from .paths import (
    DEFAULT_SESSION_NAME,
    PROFILES_DIR,
    list_session_names,
    session_dir,
)

log = logging.getLogger("vibatchium.registry")


# The contextvar that carries the current-call's session name through the
# async task. Set by the dispatcher before invoking a handler; read by the
# daemon's session-resolving properties.
current_session_ctx: ContextVar[str] = ContextVar(
    "vibatchium_current_session", default=DEFAULT_SESSION_NAME
)


def get_max_sessions() -> int:
    """Concurrent-session cap — read at every call so it's testable."""
    try:
        return max(1, int(os.environ.get("VIBATCHIUM_MAX_SESSIONS", "4")))
    except ValueError:
        return 4


def _profile_last_active(path: Path) -> float | None:
    """Best-effort 'last touched' epoch for a profile dir — the newest mtime
    among the dir itself and its immediate children.

    Used by `vb session prune --older-than` to skip recently-used profiles.
    A single-level scan (not a deep walk) keeps it cheap: Chrome rewrites
    lock files (SingletonLock/SingletonSocket) and top-level state on every
    launch/exit, so the newest top-level mtime tracks real use closely.
    Returns None if the path is gone or unreadable.
    """
    try:
        newest = path.stat().st_mtime
    except OSError:
        return None
    try:
        for child in path.iterdir():
            try:
                m = child.stat().st_mtime
                if m > newest:
                    newest = m
            except OSError:
                continue
    except OSError:
        pass
    return newest


def _within_profiles_dir(path: Path) -> bool:
    """True iff `path` is strictly contained in PROFILES_DIR.

    The deletion guard for ephemeral sessions: an absolute `--profile` path
    (which `session_dir` accepts as-is) must NEVER be rmtree'd just because the
    session is flagged ephemeral — otherwise `start --session x --profile
    /home/me/Documents --ephemeral` would delete an arbitrary directory on
    close. Only profile dirs that actually live under PROFILES_DIR are eligible.
    """
    try:
        rp = path.resolve()
        base = PROFILES_DIR.resolve()
    except OSError:
        return False
    return rp != base and base in rp.parents


def _default_safety_mode() -> str:
    """Wave 7.7.1: default safety mode for new sessions. Reads VIBATCHIUM_DEFAULT_SAFETY
    (off | flag-only | wrap | redact). Defaults to `flag-only` — every scraped
    content field gets risk metadata, no content mutation, ~1ms overhead. To
    silence entirely set VIBATCHIUM_DEFAULT_SAFETY=off."""
    val = os.environ.get("VIBATCHIUM_DEFAULT_SAFETY", "flag-only").lower()
    if val in ("off", "flag-only", "wrap", "redact"):
        return val
    return "flag-only"


def get_warm_mode() -> str:
    """Wave 6.1b: warmup strategy. Returns 'eager' | 'opportunistic' | 'both' | 'off'.

    - eager: pre-start the Playwright driver at daemon init (saves ~100-150ms
      on first session_create). Negligible RAM cost.
    - opportunistic: on session_new <name>, spawn Chrome at that profile dir
      in the background so a subsequent start finds it warm. ~250 MB per
      pre-warmed session.
    - both (default): apply both. Best end-to-end latency.
    - off: do neither; pure on-demand (the pre-Wave-6 behavior).
    """
    val = os.environ.get("VIBATCHIUM_WARM", "both").lower()
    if val not in {"eager", "opportunistic", "both", "off"}:
        return "both"
    return val


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
    # Misc per-session flags (extensible).
    # Wave 7.7.1: default safety_mode is `flag-only` — every scraped
    # content field gets `prompt_injection_risk` + `signals` metadata
    # without any content mutation. ~1ms per scraped paragraph; no
    # behavioral change for agents that don't read the metadata. To
    # disable: `vb safety set off` per session, or set the
    # VIBATCHIUM_DEFAULT_SAFETY env var.
    flags: dict = field(default_factory=lambda: {
        "safety_mode": _default_safety_mode(),
    })
    # Bookkeeping
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)
    # Wave 7.8: ephemeral session — when True, close() removes the profile
    # dir after Chrome teardown so one-shot work leaves no cookies/login
    # state on disk. Prevents profile-dir bloat from callers that mint a
    # fresh session name per run. The 'default' session is NEVER deleted,
    # regardless of this flag (guarded in close()).
    ephemeral: bool = False

    def touch(self) -> None:
        self.last_used_at = time.time()


class SessionLimitError(RuntimeError):
    """Raised when VIBATCHIUM_MAX_SESSIONS would be exceeded."""


class SessionRegistry:
    """Holds all live sessions; serializes registry mutations with `mutate_lock`.

    Per-session locks live ON the entry (`entry.lock`) so concurrent operations
    on DIFFERENT sessions don't block each other — `vb --session A click @e1`
    and `vb --session B fill @e2 hello` run truly in parallel.

    The `mutate_lock` only serializes session create/close/delete events.
    """

    def __init__(self) -> None:
        self._entries: dict[str, SessionEntry] = {}
        self.mutate_lock = asyncio.Lock()
        # Wave 5: one Playwright driver subprocess shared across all sessions.
        # Spawned lazily on the first create/attach OR eagerly at daemon init
        # if VIBATCHIUM_WARM in {eager,both}. Per-session driver subprocess would
        # saturate fds on long-running daemons with frequent session churn.
        self._pw: Playwright | None = None
        # Wave 6.1b: opportunistic per-session pre-warm.
        # Map name → (BrowserSession, task) of Chromes spawned via session_new
        # in the background. start() pops from here if the warm session matches.
        self._warm_sessions: dict[str, BrowserSession] = {}
        self._warm_tasks: dict[str, asyncio.Task] = {}

    async def _ensure_pw(self) -> Playwright:
        if self._pw is None:
            self._pw = await async_playwright().start()
            log.info("started shared Playwright driver")
        return self._pw

    async def _maybe_stop_pw(self) -> None:
        """Stop the shared Playwright driver when no sessions are running."""
        if self._pw is not None and not self._entries and not self._warm_sessions:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
            log.info("stopped shared Playwright driver (no sessions)")

    # ─── Wave 6.1b: warmup ──────────────────────────────────────────────

    async def warmup(self) -> None:
        """Eager Playwright driver pre-start. Called from daemon.run() at
        startup if VIBATCHIUM_WARM ∈ {eager, both}. Fast — ~150ms — and
        non-blocking by virtue of being awaited once before serving traffic."""
        mode = get_warm_mode()
        if mode in {"eager", "both"}:
            await self._ensure_pw()
            log.info("pre-warmed Playwright driver (VIBATCHIUM_WARM=%s)", mode)

    def schedule_prewarm(self, name: str, profile_dir: Path,
                          headless: bool = False) -> None:
        """Wave 6.1b opportunistic: spawn a Chrome at `profile_dir` in the
        background so a subsequent create(name=...) finds it warm.

        Cheap to call — returns immediately, work happens in a background task.
        Idempotent: re-scheduling for the same name is a no-op while a prior
        warm is in-flight or already done.
        """
        if get_warm_mode() not in {"opportunistic", "both"}:
            return
        if name in self._warm_sessions:
            return  # already warm
        if name in self._warm_tasks and not self._warm_tasks[name].done():
            return  # already in-flight
        if name in self._entries:
            return  # already running for real
        # Wave 7 fix: warm pre-spawns must count toward VIBATCHIUM_MAX_SESSIONS
        # (each one is a real Chrome holding ~200-400 MB). The cap is enforced
        # in create() but opportunistic prewarms previously slipped past it.
        cap = get_max_sessions()
        in_flight = sum(1 for t in self._warm_tasks.values() if not t.done())
        if len(self._entries) + len(self._warm_sessions) + in_flight >= cap:
            log.debug("skipping prewarm of %s — at VIBATCHIUM_MAX_SESSIONS=%d",
                      name, cap)
            return

        async def _do_prewarm():
            try:
                pw = await self._ensure_pw()
                from . import backends as _backends
                sess = await _backends.launch("patchright", profile_dir,
                                              headless=headless, pw=pw)
                # Only stash if still unclaimed (might race with real create)
                if name not in self._entries and name not in self._warm_sessions:
                    self._warm_sessions[name] = sess
                    log.info("pre-warmed session name=%s profile=%s", name, profile_dir)
                else:
                    # Lost the race; discard
                    from . import backends as _b
                    await _b.close(sess)
            except Exception as exc:  # noqa: BLE001
                log.debug("prewarm %s failed: %s", name, exc)

        self._warm_tasks[name] = asyncio.create_task(_do_prewarm())

    async def cancel_prewarm(self, name: str) -> bool:
        """Cancel an in-flight prewarm AND/OR discard an already-warm Chrome
        for this name. Called by session_delete so we don't leak a warm Chrome
        whose profile dir was just removed."""
        cancelled = False
        task = self._warm_tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            cancelled = True
        sess = self._warm_sessions.pop(name, None)
        if sess is not None:
            from . import backends as _backends
            try:
                await _backends.close(sess)
            except Exception:  # noqa: BLE001
                pass
            cancelled = True
        return cancelled

    # ─── lookups ─────────────────────────────────────────────────────────

    def get(self, name: str) -> SessionEntry | None:
        entry = self._entries.get(name)
        if entry is not None:
            entry.touch()
        return entry

    def has(self, name: str) -> bool:
        return name in self._entries

    def list_running(self) -> list[str]:
        return sorted(self._entries.keys())

    def warming_names(self) -> set[str]:
        """Names with a parked or in-flight pre-warm Chrome. These are NOT in
        `_entries` yet hold a live OS lock on their profile dir, so housekeeping
        (lock removal / profile pruning) must treat them as 'in use' alongside
        `list_running()`."""
        names = set(self._warm_sessions.keys())
        names |= {n for n, t in self._warm_tasks.items() if not t.done()}
        return names

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
            if name in on_disk:
                # Epoch of last on-disk activity — lets `session prune
                # --older-than` skip recently-used profiles. None if unreadable.
                row["last_active"] = _profile_last_active(PROFILES_DIR / name)
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
        backend: str = "patchright",
        ephemeral: bool = False,
    ) -> SessionEntry:
        """Launch Chrome for a new session.

        Args:
          name: session identifier (also used as profile dir basename when
                profile_dir is None).
          profile_dir: explicit user-data-dir; defaults to PROFILES_DIR/<name>.
          headless: run without a visible window — the default for daemon/agent
                paths (a background daemon owns no display). The UA *string* is
                de-Headless'd automatically (browser.coherent_headless_ua, via a
                browser-wide --user-agent flag that also covers SharedWorkers),
                so the `HeadlessChrome` leak is closed on every context. The
                Sec-CH-UA client hints don't leak in new-headless mode (already
                report `Google Chrome`). Residual headless tells (SwiftShader
                WebGL, 800x600 screen, 0px scrollbar) remain — headed clears those.
          backend: 'patchright' (default), 'nodriver', or 'auto'.
                   nodriver requires `pip install vibatchium[nodriver]` and
                   uses its hardened launch flags + Patchright connect_over_cdp.
          ephemeral: delete the profile dir when this session closes (one-shot
                   work that should leave no state on disk). Never deletes the
                   'default' profile.

        Raises:
          SessionLimitError if VIBATCHIUM_MAX_SESSIONS would be exceeded.
          RuntimeError if a session with this name is already running.
        """
        if name in self._entries:
            raise RuntimeError(
                f"session {name!r} already running — "
                f"use `vb --session {name} stop` first"
            )
        cap = get_max_sessions()
        if len(self._entries) >= cap:
            raise SessionLimitError(
                f"VIBATCHIUM_MAX_SESSIONS={cap} reached "
                f"({len(self._entries)} sessions running). "
                f"Close one with `vb session close <name>` or raise the cap."
            )
        pdir = profile_dir if profile_dir is not None else session_dir(name)
        pdir.mkdir(parents=True, exist_ok=True)
        pw = await self._ensure_pw()
        from . import backends as _backends
        # Wave 6.2a: resolve persisted per-session proxy (if any). A proxy
        # configured via `vb proxy set` lives in <profile_dir>/proxy.json.
        from ..proxy import load_session_proxy, parse as _parse_proxy
        proxy_cfg = None
        proxy_url = load_session_proxy(pdir)
        if proxy_url:
            try:
                proxy_cfg = _parse_proxy(proxy_url)
            except Exception as exc:  # noqa: BLE001
                # Wave 7.5d: exception message from proxy.parse contains the
                # raw URL, which may include `user:pass@host` credentials.
                # Log only the exception class — don't leak creds into logs.
                log.warning("ignoring malformed proxy for %s: %s",
                            name, type(exc).__name__)
        # Wave 6.1b: prefer a pre-warmed session if one is available for this
        # name AND the requested config matches (backend, headless, no proxy).
        # Proxy-configured sessions always launch fresh because the warm
        # session was launched without the proxy.
        #
        # If a prewarm is in-flight (task started but not done), await it
        # first — both that task and a fresh launch would race for the
        # OS-level user-data-dir lock.
        task = self._warm_tasks.pop(name, None)
        if task is not None and not task.done():
            try:
                await task
            except Exception:  # noqa: BLE001
                pass
        warm = self._warm_sessions.pop(name, None)
        if (warm is not None and backend == "patchright"
                and warm.profile_dir == pdir and warm.headless == headless
                and proxy_cfg is None):
            sess = warm
            log.info("session %s claimed pre-warmed Chrome", name)
        else:
            if warm is not None:
                # Pre-warm doesn't match request — close it to free RAM
                try:
                    await _backends.close(warm)
                except Exception:  # noqa: BLE001
                    pass
            sess = await _backends.launch(backend, pdir, headless=headless,
                                           pw=pw, proxy=proxy_cfg)
        sess.flags = getattr(sess, "flags", {}) if hasattr(sess, "flags") else {}
        entry = SessionEntry(name=name, profile_dir=pdir, session=sess,
                             ephemeral=ephemeral)
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
            raise SessionLimitError(f"VIBATCHIUM_MAX_SESSIONS={cap} reached")
        pw = await self._ensure_pw()
        sess = await attach_session(cdp_url, pw=pw)
        entry = SessionEntry(name=name, profile_dir=session_dir(name), session=sess)
        self._entries[name] = entry
        log.info("session attached name=%s cdp_url=%s", name, cdp_url)
        return entry

    async def close(self, name: str) -> bool:
        """Stop Chrome for a session; profile dir is preserved on disk
        (unless the session was started `ephemeral`, in which case the dir is
        removed after teardown — see Wave 7.8 below; 'default' is never removed).

        Returns True if the session was running and is now closed.
        Idempotent — closing an absent session returns False without error.

        Wave 7.7.3: when VIBATCHIUM_WARM_RECYCLE=1, also re-prewarm this
        session's profile in the background after teardown so a
        subsequent `start` with the same name finds it warm. Helps the
        "sequential sessions under the same name" workflow (competitor
        scan / batch processing) where the same profile gets repeatedly
        opened-and-closed. Default OFF (costs RAM for sessions you may
        not reopen).
        """
        entry = self._entries.pop(name, None)
        if entry is None:
            return False
        profile_dir = entry.profile_dir
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
        # Wave 7.8: ephemeral session — remove the profile dir now that Chrome
        # is down so one-shot work leaves nothing on disk. The 'default'
        # profile is never deleted. We return early so an ephemeral session is
        # never warm-recycled (there'd be no dir left to reopen).
        if entry.ephemeral and name != DEFAULT_SESSION_NAME:
            if not _within_profiles_dir(profile_dir):
                # Safety guard: refuse to delete a dir outside PROFILES_DIR (e.g.
                # an absolute `--profile` path). Ephemeral means "throw away the
                # managed profile", never "rmtree an arbitrary directory".
                log.warning("ephemeral session %s: profile %s is outside "
                            "PROFILES_DIR — not deleting", name, profile_dir)
                return True
            # Cancel any in-flight/parked prewarm BEFORE deleting so it can't
            # race us and re-create the dir. close() is async, so await it
            # (unlike the sync delete_profile_dir, which can only fire-and-forget).
            if name in self._warm_tasks or name in self._warm_sessions:
                await self.cancel_prewarm(name)
            try:
                if profile_dir.exists():
                    shutil.rmtree(profile_dir)
                    log.info("ephemeral profile deleted name=%s profile=%s",
                             name, profile_dir)
            except Exception as exc:  # noqa: BLE001
                log.warning("ephemeral cleanup failed for %s: %s", name, exc)
            return True
        # Wave 7.7.3 warm recycle
        recycle = os.environ.get("VIBATCHIUM_WARM_RECYCLE", "0").lower() in (
            "1", "true", "yes", "on"
        )
        if recycle and get_warm_mode() in {"opportunistic", "both"}:
            try:
                self.schedule_prewarm(name, profile_dir, headless=True)
                log.info("warm-recycle scheduled for name=%s", name)
            except Exception as exc:  # noqa: BLE001
                log.warning("warm-recycle schedule failed for %s: %s",
                             name, exc)
        return True

    async def close_all(self) -> int:
        """Stop every running session. Returns the count closed.

        Also discards any pre-warmed sessions and tears down the shared
        Playwright driver when nothing is left.
        """
        names = list(self._entries.keys())
        n = 0
        for name in names:
            if await self.close(name):
                n += 1
        # Wave 6.1b: drain pre-warms too
        for name in list(self._warm_sessions.keys()) + list(self._warm_tasks.keys()):
            await self.cancel_prewarm(name)
        await self._maybe_stop_pw()
        return n

    def delete_profile_dir(self, name: str) -> bool:
        """Remove the on-disk profile dir. Refuses to delete a running session
        or the special 'default' name. Cancels any in-flight pre-warm so we
        don't leak a Chrome whose profile dir was just removed."""
        if name == DEFAULT_SESSION_NAME:
            raise ValueError("cannot delete the 'default' profile")
        if name in self._entries:
            raise RuntimeError(
                f"session {name!r} is running — close it first"
            )
        # Wave 6.1b: cancel any pre-warm (cooperative; the task will see the
        # missing dir and drop). We can't `await` here since this is sync.
        if name in self._warm_tasks or name in self._warm_sessions:
            asyncio.create_task(self.cancel_prewarm(name))
        pdir = PROFILES_DIR / name
        if not pdir.exists():
            return False
        shutil.rmtree(pdir)
        log.info("profile dir deleted name=%s", name)
        return True
