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
import contextlib
import logging
import os
import shutil
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from patchright.async_api import Playwright, async_playwright

from . import lease as _lease
from .browser import BrowserSession, attach_session
from .paths import (
    DEFAULT_SESSION_NAME,
    PROFILES_DIR,
    list_session_names,
    session_dir,
)

log = logging.getLogger("vibatchium.registry")

# Sentinel so _launch_for can tell "caller passed proxy/geo (even None)" from
# "caller wants a fresh disk read" (the relaunch path) — None is a valid value.
_UNSET = object()

# 0.8.0 (Vibium lesson): one-time, opt-out Chrome auto-install on the first
# cold launch that fails because the browser binary is missing. Module-level so
# it fires AT MOST ONCE per daemon lifetime — a genuinely un-installable Chrome
# must not re-trigger a multi-minute `patchright install chrome` on every retry
# and every self-heal relaunch.
_chrome_install_attempted = False
_MISSING_CHROME_SIGNATURES = (
    "executable doesn't exist",
    "playwright install",
    "patchright install",
    "install chrome",
    "download new browsers",
)


async def _maybe_autoinstall_chrome(exc: BaseException) -> bool:
    """Return True (caller should retry the launch) iff a one-time Chrome
    auto-install just ran in response to a missing-executable launch error.
    Opt out with VIBATCHIUM_AUTO_INSTALL=0 (for sandboxed / offline CI)."""
    global _chrome_install_attempted
    if _chrome_install_attempted:
        return False
    if os.environ.get("VIBATCHIUM_AUTO_INSTALL", "1").lower() in ("0", "false", "no", "off"):
        return False
    msg = str(exc).lower()
    if not any(sig in msg for sig in _MISSING_CHROME_SIGNATURES):
        return False
    # Claim the one-shot BEFORE awaiting so concurrent cold starts don't each
    # spawn an install.
    _chrome_install_attempted = True
    log.warning("Chrome not installed — running one-time `patchright install "
                "chrome` (may take ~30-60s; set VIBATCHIUM_AUTO_INSTALL=0 to "
                "disable). One-shot per daemon.")
    import subprocess
    import sys
    try:
        # Bind to THIS interpreter's patchright (guaranteed importable) rather
        # than a bare `patchright` on PATH — robust under systemd's minimal PATH.
        await asyncio.to_thread(
            subprocess.run, [sys.executable, "-m", "patchright", "install", "chrome"],
            check=True, capture_output=True, timeout=600)
        log.info("Chrome installed; retrying launch.")
        return True
    except Exception as iexc:  # noqa: BLE001
        log.warning("auto-install of Chrome failed (%s) — run `vb install` "
                    "manually.", type(iexc).__name__)
        return False


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


def get_max_ephemeral() -> int:
    """Off-budget one-shot lane cap (0.7.0), separate from the persistent
    VIBATCHIUM_MAX_SESSIONS budget. Read on every call so it's testable.

    Unlike get_max_sessions (min 1), this floors at 0 — setting
    VIBATCHIUM_MAX_EPHEMERAL=0 HARD-DISABLES the lane, so `vb explore` without a
    pinned session and `vb start --ephemeral` then raise SessionLimitError.
    Default 2 bounds worst-case total Chromes to MAX_SESSIONS+MAX_EPHEMERAL.
    """
    try:
        return max(0, int(os.environ.get("VIBATCHIUM_MAX_EPHEMERAL", "2")))
    except ValueError:
        return 2


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
    # 0.7.0 self-heal: how many times this session auto-recovered from a Chrome
    # renderer crash / context death (page-revive OR full relaunch), and when.
    # Surfaced via `vb status` + `vb session list --json` for operator debugging.
    recovered: int = 0
    last_recovered_at: float | None = None
    # 0.7.0 lease: opt-in exclusive coordination. None = unleased (fully open,
    # today's behavior). Otherwise {'owner','token','expires_at','acquired_at'}.
    # Reaped lazily — see lease_active().
    lease: dict | None = None

    def touch(self) -> None:
        self.last_used_at = time.time()

    # ─── 0.7.0 lease helpers (advisory, TTL-bounded) ─────────────────────
    def lease_active(self, now: float | None = None) -> dict | None:
        """Return the live lease, lazily reaping an expired one. The ONLY place
        a stale lease is cleared — called by the dispatch gates AND every
        observability read (status / session_list / lease_info) so expiry
        semantics never diverge. Idempotent; takes no lock."""
        if self.lease is None:
            return None
        if _lease.is_expired(self.lease, now):
            log.info("lease expired session=%s owner=%s", self.name,
                     self.lease.get("owner"))
            self.lease = None
            return None
        return self.lease

    def lease_grant(self, owner: str, ttl_s, *, presented=None) -> dict:
        """Grant, renew, or steal the lease.

        A genuine same-holder RENEWAL (the caller presents the active token)
        keeps the existing token + original acquired_at; only expires_at slides
        forward. A STEAL (active lease, non-matching token) ROTATES the token —
        so the prior holder is revoked and never learns the new secret. A fresh
        grant on an unleased session mints a new token."""
        now = time.time()
        ttl = _lease.clamp_ttl(ttl_s)
        active = self.lease_active(now)
        renewal = active is not None and _lease._token_eq(presented, active["token"])
        token = active["token"] if renewal else _lease.mint_token()
        acquired = active["acquired_at"] if renewal else now
        self.lease = {"owner": owner or "anonymous", "token": token,
                      "acquired_at": acquired, "expires_at": now + ttl}
        return dict(self.lease)

    def lease_clear(self) -> None:
        self.lease = None


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
        # 0.7.0 ephemeral lane: monotonic counter for minting unique transient
        # session names (`_ex-<pid>-<seq>`). Single-process daemon → a plain
        # increment is race-free.
        self._ephemeral_seq = 0

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

    def is_idle(self) -> bool:
        """0.9.1: True when the daemon holds NO live sessions, NO warm-pooled
        sessions, and NO in-flight warm tasks — i.e. it's a candidate for the
        idle reaper. A daemon with any session (incl. attach-mode / bot sessions)
        is never idle, so the reaper can't touch a working daemon."""
        # An in-flight create()/close()/delete() holds mutate_lock and may not
        # have written _entries yet (Chrome still launching) — a cold start would
        # otherwise look idle and get reaped mid-launch. Never report idle then.
        if self.mutate_lock.locked():
            return False
        if self._entries or self._warm_sessions:
            return False
        return not any(not t.done() for t in self._warm_tasks.values())

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
        # Prewarms are persistent named sessions — count only the persistent
        # budget (ephemeral one-shot sessions are never pre-warmed).
        if self.count_persistent() + len(self._warm_sessions) + in_flight >= cap:
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

    # ─── 0.7.0 cap relief: two-budget accounting ─────────────────────────
    def count_persistent(self) -> int:
        """Running sessions that count against VIBATCHIUM_MAX_SESSIONS."""
        return sum(1 for e in self._entries.values() if not e.ephemeral)

    def count_ephemeral(self) -> int:
        """Running off-budget one-shot sessions (count against MAX_EPHEMERAL)."""
        return sum(1 for e in self._entries.values() if e.ephemeral)

    def budgets(self) -> dict:
        """Both budgets + current usage — surfaced in status / session_list."""
        return {
            "persistent": {"used": self.count_persistent(), "cap": get_max_sessions()},
            "ephemeral": {"used": self.count_ephemeral(), "cap": get_max_ephemeral()},
        }

    def mint_ephemeral_name(self) -> str:
        """A unique transient session name (`_ex-<pid>-<seq>`). The leading
        underscore is DELIBERATE: validate_name rejects it, so a minted name can
        never collide with a user-created session — yet session_dir/start/close
        don't validate, so it works end-to-end internally. The pid keeps it
        distinct across daemon restarts sharing PROFILES_DIR; the seq within one
        daemon. Single-process → the increment is race-free."""
        self._ephemeral_seq += 1
        return f"_ex-{os.getpid()}-{self._ephemeral_seq}"

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
                # 0.7.0: additive observability keys (self-heal + cap relief +
                # lease). All optional — pre-existing consumers ignore extras.
                row["ephemeral"] = entry.ephemeral
                row["recovered"] = entry.recovered
                row["last_recovered_at"] = entry.last_recovered_at
                row["lease"] = _lease.lease_public(entry.lease_active())
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
        # 0.7.0: two independent budgets. Ephemeral one-shot sessions never
        # compete with persistent/production sessions for slots.
        if ephemeral:
            ecap = get_max_ephemeral()
            used = self.count_ephemeral()
            if ecap <= 0 or used >= ecap:
                raise SessionLimitError(
                    f"VIBATCHIUM_MAX_EPHEMERAL={ecap} reached "
                    f"({used} one-shot sessions running). These auto-close on "
                    f"completion; wait for one to finish or raise the cap."
                )
        else:
            cap = get_max_sessions()
            used = self.count_persistent()
            if used >= cap:
                raise SessionLimitError(
                    f"VIBATCHIUM_MAX_SESSIONS={cap} reached "
                    f"({used} persistent sessions running). Close one with "
                    f"`vb session close <name>`, raise the cap, or run one-shot "
                    f"work via `vb explore` (off-budget ephemeral lane)."
                )
        pdir = profile_dir if profile_dir is not None else session_dir(name)
        pdir.mkdir(parents=True, exist_ok=True)
        from . import backends as _backends
        # Per-session proxy (proxy.json) + geo (geo.json). Loaded here so the
        # warm-claim guard can refuse a pre-warm that was launched WITHOUT these
        # overrides; passed through to _launch_for on the cold path to avoid a
        # second disk read. _launch_for re-loads from disk when called WITHOUT
        # them (the relaunch/self-heal path), so a mid-life `vb proxy set` is
        # honored on recovery.
        proxy_cfg, geo_cfg = self._load_proxy_geo(name, pdir)
        # Wave 6.1b: prefer a pre-warmed session if one is available for this
        # name AND the requested config matches (backend, headless, no proxy,
        # no geo). Proxy- or geo-configured sessions always launch fresh because
        # the warm session was launched without those overrides.
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
                and proxy_cfg is None and geo_cfg is None):
            sess = warm
            log.info("session %s claimed pre-warmed Chrome", name)
        else:
            if warm is not None:
                # Pre-warm doesn't match request — close it to free RAM
                try:
                    await _backends.close(warm)
                except Exception:  # noqa: BLE001
                    pass
            sess = await self._launch_for(name, profile_dir=pdir,
                                          headless=headless, backend=backend,
                                          proxy_cfg=proxy_cfg, geo_cfg=geo_cfg)
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

    def _load_proxy_geo(self, name: str, profile_dir: Path):
        """Resolve the persisted per-session proxy (proxy.json) + geo (geo.json)
        for a profile dir. Returns ``(proxy_cfg, geo_cfg)`` — either may be None.

        Shared by create() (warm-claim guard + cold launch) and _launch_for's
        relaunch path so the proxy/geo resolution + the proxy-without-geo
        coherence warning live in ONE place.
        """
        from ..proxy import load_session_proxy, parse as _parse_proxy
        proxy_cfg = None
        proxy_url = load_session_proxy(profile_dir)
        if proxy_url:
            try:
                proxy_cfg = _parse_proxy(proxy_url)
            except Exception as exc:  # noqa: BLE001
                # Wave 7.5d: proxy.parse's message can contain `user:pass@host`
                # — log only the exception class, never the raw URL.
                log.warning("ignoring malformed proxy for %s: %s",
                            name, type(exc).__name__)
        from ..geo import load_session_geo
        geo_cfg = load_session_geo(profile_dir)
        if proxy_cfg is not None and geo_cfg is None:
            log.warning(
                "session %s has a proxy but no geo override — the host "
                "timezone may not match the proxy's IP (a bot tell). "
                "Set `vb geo set --country <cc>` to cohere.", name)
        return proxy_cfg, geo_cfg

    async def _launch_for(self, name: str, *, profile_dir: Path,
                          headless: bool, backend: str,
                          proxy_cfg=_UNSET, geo_cfg=_UNSET,
                          allow_install: bool = True) -> BrowserSession:
        """Cold-launch a Chrome for a session with NO warm-claim — the single
        launch seam shared by create()'s cold path and relaunch().

        When proxy_cfg/geo_cfg are omitted (the relaunch/self-heal path) they
        are RE-READ from disk so a mid-life `vb proxy set` / `vb geo set` is
        honored on recovery. create() passes its already-loaded cfgs through to
        avoid a redundant read.
        """
        pw = await self._ensure_pw()
        from . import backends as _backends
        if proxy_cfg is _UNSET or geo_cfg is _UNSET:
            proxy_cfg, geo_cfg = self._load_proxy_geo(name, profile_dir)

        async def _do_launch():
            return await _backends.launch(
                backend, profile_dir, headless=headless, pw=pw, proxy=proxy_cfg,
                timezone_id=(geo_cfg or {}).get("timezone_id"))

        try:
            return await _do_launch()
        except Exception as exc:  # noqa: BLE001
            # 0.8.0: a missing Chrome binary is the #1 onboarding failure. Try a
            # one-time auto-install, then retry the launch exactly once. Any
            # other launch error (or a second failure) propagates unchanged.
            # NOT on the self-heal relaunch path (allow_install=False): that runs
            # under a wait_for(30) that would always cancel the ~minutes install
            # and burn the one-shot flag — and a missing binary mid-life is not a
            # cold-start onboarding case anyway.
            if not allow_install or not await _maybe_autoinstall_chrome(exc):
                raise
            return await _do_launch()

    async def relaunch(self, name: str) -> SessionEntry:
        """Tear down a dead/crashed Chrome and cold-launch a fresh one for the
        SAME session, preserving the SessionEntry identity (so a held entry.lock,
        entry.flags, lease, and recovered counter survive — only entry.session
        is swapped).

        Lock-ordering contract: the CALLER must hold entry.lock; relaunch must
        NOT take registry.mutate_lock (create()/close() take mutate_lock and
        never entry.lock — keeping the two orderings disjoint avoids deadlock).
        """
        entry = self._entries.get(name)
        if entry is None:
            raise RuntimeError(f"cannot relaunch unknown session {name!r}")
        old = entry.session
        if old.mode == "attach":
            # We don't own a foreign Chrome — tearing it down would kill the
            # user's real browser. Degrade to a clear, actionable error.
            raise RuntimeError(
                f"attach session {name!r} lost its browser — re-attach with "
                f"`vb attach` (auto-relaunch only applies to launched sessions)")
        from . import backends as _backends
        from .browser import ensure_nav_guard
        # Best-effort teardown; bound it so a hung close on a zombie-locked
        # profile dir can't wedge the session worse than the original crash.
        try:
            await asyncio.wait_for(_backends.close(old), timeout=10)
        except Exception:  # noqa: BLE001 — already dead / hung
            pass
        sess = await asyncio.wait_for(
            self._launch_for(name, profile_dir=entry.profile_dir,
                             headless=old.headless,
                             backend=entry.flags.get("backend", "patchright"),
                             allow_install=False),  # see _launch_for: no install under wait_for(30)
            timeout=30)
        # A concurrent session_close/session_delete (mutate_lock — disjoint from
        # the entry.lock we hold) may have popped this entry during the
        # multi-second launch above. Adopting sess into an orphaned entry would
        # leak an untracked Chrome that keeps the profile SingletonLock for the
        # daemon's lifetime (close_all/shutdown only iterate _entries). Re-check
        # ownership; if lost, tear the fresh Chrome down and surface a clean
        # error rather than orphan it. (No cross-lock acquisition — invariant
        # preserved.)
        if self._entries.get(name) is not entry:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(_backends.close(sess), timeout=10)
            raise RuntimeError(
                f"session {name!r} was closed during self-heal relaunch; "
                f"re-start it")
        sess.flags = getattr(sess, "flags", {}) if hasattr(sess, "flags") else {}
        # CRITICAL: carry the goal domain wall forward + re-arm the guard, or a
        # crashed goal-pinned session would resume with NO domain restriction.
        if old.nav_allowlist:
            sess.nav_allowlist = old.nav_allowlist
            await ensure_nav_guard(sess)
        entry.session = sess
        # Drop stale handles/snapshot — their execution contexts died with the
        # old renderer.
        for h in list(entry.handles.values()):
            try:
                await h.dispose()
            except Exception:  # noqa: BLE001
                pass
        entry.handles.clear()
        entry.handle_counter = 0
        entry.snapshot = None
        entry.prev_snapshot = None
        entry.recovered += 1
        entry.last_recovered_at = time.time()
        entry.touch()
        log.warning("self-heal: RELAUNCHED session %s (recovered=%d) "
                    "headless=%s backend=%s", name, entry.recovered,
                    old.headless, entry.flags.get("backend", "patchright"))
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
        if self.count_persistent() >= cap:
            raise SessionLimitError(f"VIBATCHIUM_MAX_SESSIONS={cap} reached")
        pw = await self._ensure_pw()
        sess = await attach_session(cdp_url, pw=pw)
        # 0.6.11: geo is a cold-launch (`start`) override applied at
        # launch_persistent_context time. Attach connects to an already-running
        # Chrome whose timezone is its own real one (coherent with its real IP);
        # forcing an override here could BREAK that coherence. So we don't apply
        # it — but we don't silently ignore a configured geo either: warn.
        from ..geo import load_session_geo
        if load_session_geo(session_dir(name)):
            log.warning("session %s has a geo timezone override but is ATTACHing "
                        "to an existing Chrome — geo applies only to cold-launch "
                        "(`start`); the attached browser keeps its own timezone.",
                        name)
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
            # Bound the teardown so a hung close on a zombie/SIGKILLed Chrome
            # (whose profile lock is wedged) can't park the caller's connection
            # coroutine indefinitely — the ephemeral-explore lane awaits this in
            # its finally, and handle_conn has no server-side timeout.
            await asyncio.wait_for(_backends.close(entry.session), timeout=10)
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
            # race us and re-create the dir (delete_profile_dir awaits the same
            # cancel for the same reason).
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
                # 0.6.11: recycle at the daemon default posture (honors
                # VIBATCHIUM_DEFAULT_HEADED) instead of hardcoding headless, so
                # a headed-default daemon's recycled warms are claimable. Lazy
                # import: handlers imports registry, so module-level would cycle.
                from .handlers import resolve_headless
                self.schedule_prewarm(name, profile_dir,
                                      headless=resolve_headless({}))
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
        # Wave 6.1b: drain pre-warms too. Union the two dicts — a completed
        # prewarm lives in BOTH _warm_sessions and _warm_tasks, so concatenating
        # lists would cancel it twice (harmless but wasteful/confusing).
        for name in set(self._warm_sessions) | set(self._warm_tasks):
            await self.cancel_prewarm(name)
        await self._maybe_stop_pw()
        return n

    async def delete_profile_dir(self, name: str) -> bool:
        """Remove the on-disk profile dir. Refuses to delete a running session
        or the special 'default' name. Cancels any in-flight pre-warm so we
        don't leak a Chrome whose profile dir was just removed."""
        if name == DEFAULT_SESSION_NAME:
            raise ValueError("cannot delete the 'default' profile")
        if name in self._entries:
            raise RuntimeError(
                f"session {name!r} is running — close it first"
            )
        # 0.6.11: AWAIT the cancel before rmtree. The old fire-and-forget
        # `create_task` raced the rmtree below — if a prewarm was mid-launch
        # (Chrome writing into the profile dir), rmtree could delete files out
        # from under it, leaving a corrupt/failed Chrome or an orphaned process.
        # Awaiting drains/cancels the in-flight launch first.
        if name in self._warm_tasks or name in self._warm_sessions:
            await self.cancel_prewarm(name)
        pdir = PROFILES_DIR / name
        if not pdir.exists():
            return False
        shutil.rmtree(pdir)
        log.info("profile dir deleted name=%s", name)
        return True
