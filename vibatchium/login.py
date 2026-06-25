"""`vb login` — open a HEADED window for a manual login on a shared box.

The problem this bakes into one command: on a box where the default daemon is a
live bot's HEADLESS daemon, getting a visible window to log into a session's
profile is fiddly enough that two agents each burned ~10 minutes rediscovering
(and misdiagnosing) it. The four things that have to be true at once:

  1. A SEPARATE daemon — but on its OWN socket, so it can never disturb the live
     bots' default daemon. Achieved by moving only XDG_RUNTIME_DIR (the socket
     derives from it; see daemon/paths.py).
  2. The REAL profile. Unlike the SDK's `IsolatedDaemon` (which also isolates
     HOME to contain profile leaks), a login KEEPS the real HOME so the profile
     is PROFILES_DIR/<name> — the exact dir the headless bot reads. The cookies
     you type must land where the bot looks.
  3. A working display. We set DISPLAY + XAUTHORITY and DROP the Wayland hints so
     Chrome renders via X11/XWayland regardless of the isolated runtime dir (the
     X socket and XAUTHORITY are absolute paths, independent of XDG_RUNTIME_DIR).
     Forcing X11 also keeps the window visible to standard X tools — a native
     Wayland window is invisible to xwininfo/wmctrl, which misled an earlier
     debugging attempt into concluding "no window".
  4. A clean relaunch. A Chrome killed earlier leaves a stale SingletonLock in
     the profile; we clear it (only when its owner is dead / on another host) so
     `start --headed` actually cold-launches instead of refusing.

The login daemon is detached and PERSISTS after `vb login` returns, so the
window stays up while you log in. Tear it down with `vb login --close <name>`.

Most logic here is pure (env/path/lock computation) so it unit-tests without a
browser; the orchestrators (`run_login`, `close_login`) do the spawn + RPC.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import client as _client
from .daemon.paths import PROFILES_DIR, validate_name


class NoDisplayError(RuntimeError):
    """No usable X display for a headed login (e.g. a truly headless host)."""


# ─── pure helpers (unit-testable, no browser / no daemon) ────────────────────

def login_runtime_dir(base_runtime: str | os.PathLike, name: str) -> Path:
    """Isolated runtime dir for NAME's login daemon — a SIBLING of the default
    `<base>/vibatchium`, never that dir itself, so the live bots' socket is
    untouchable. `name` is validated by the caller; sanitized again here defensively."""
    # Drop '.' too so a name like "../x" can't leave a ".." in the dir name; the
    # whole thing is one path component under base regardless, but this keeps it
    # tidy and traversal-proof by construction.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", name).strip("_-") or "session"
    return Path(base_runtime) / f"vblogin-{safe}"


def sock_for_runtime(runtime_dir: str | os.PathLike) -> Path:
    """Mirror daemon/paths.py: SOCK_PATH = <XDG_RUNTIME_DIR>/vibatchium/daemon.sock."""
    return Path(runtime_dir) / "vibatchium" / "daemon.sock"


def resolve_display(environ: Mapping[str, str]) -> str | None:
    """The X display to use, or None if there is none (→ headed login impossible)."""
    return environ.get("DISPLAY") or None


def resolve_xauthority(environ: Mapping[str, str],
                       candidates: list[str | os.PathLike]) -> str | None:
    """Pick the X auth cookie file: an explicit, existing $XAUTHORITY wins; else
    the first existing candidate (caller passes them newest-first). None if none
    exist — X11 may still work without one (host-local), so this is advisory."""
    xa = environ.get("XAUTHORITY")
    if xa and Path(xa).exists():
        return xa
    for c in candidates:
        if Path(c).exists():
            return str(c)
    return None


def build_login_env(base_env: Mapping[str, str], runtime_dir: str | os.PathLike, *,
                    display: str, xauthority: str | None,
                    log_file: str | os.PathLike) -> dict[str, str]:
    """Child env for the login daemon: isolated SOCKET (XDG_RUNTIME_DIR moved),
    REAL home preserved (profile stays PROFILES_DIR/<name>), X11 forced."""
    env = dict(base_env)
    env["XDG_RUNTIME_DIR"] = str(runtime_dir)
    env["DISPLAY"] = display
    if xauthority:
        env["XAUTHORITY"] = xauthority
    # Force X11/XWayland: drop Wayland hints so ozone can't chase a wayland
    # socket that doesn't exist under our isolated runtime dir, and so the window
    # is a normal X toplevel (visible to xwininfo, not a Wayland-only surface).
    for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE"):
        env.pop(k, None)
    # This daemon exists to show windows — default it headed (belt-and-suspenders
    # alongside the explicit `start --headed`); never inherit a forced-headless.
    env.pop("VIBATCHIUM_DEFAULT_HEADLESS", None)
    env["VIBATCHIUM_DEFAULT_HEADED"] = "1"
    # Keep the daemon log inside the isolated runtime dir, not the shared state
    # log the live bots write to.
    env["VIBATCHIUM_LOG_FILE"] = str(log_file)
    # HOME is real, so the patchright browser cache (~/.cache/ms-playwright) is
    # found normally — no PLAYWRIGHT_BROWSERS_PATH juggling needed.
    return env


def parse_singleton_pid(link_target: str) -> int | None:
    """Chrome's SingletonLock symlinks to "<hostname>-<pid>"; pull the pid."""
    m = re.search(r"-(\d+)$", link_target or "")
    return int(m.group(1)) if m else None


def singleton_is_stale(link_target: str, *, hostname: str,
                       pid_alive: Callable[[int], bool]) -> bool:
    """A SingletonLock is stale (safe to remove) when it was made on another host
    or its owning Chrome pid is dead. A live owner on THIS host means the profile
    is in use — never clear that (would risk double-use / corruption)."""
    if not link_target:
        return True
    host = link_target.rsplit("-", 1)[0]
    if host != hostname:
        return True
    pid = parse_singleton_pid(link_target)
    if pid is None:
        return True
    return not pid_alive(pid)


# ─── IO helpers ──────────────────────────────────────────────────────────────

def xauth_candidates(uid: int | None = None, home: str | None = None,
                     run_base: str = "/run/user") -> list[Path]:
    """X auth files to try, newest-first: the mutter Xwayland auth (random
    per-login suffix, so it MUST be discovered not hardcoded) then ~/.Xauthority."""
    uid = os.getuid() if uid is None else uid
    base = Path(run_base) / str(uid)
    out: list[Path] = []
    try:
        out = sorted(base.glob(".mutter-Xwaylandauth.*"),
                     key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        out = []
    home = home or os.environ.get("HOME")
    if home:
        xa = Path(home) / ".Xauthority"
        if xa.exists():
            out.append(xa)
    return out


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return False


def clear_stale_singletons(profile_dir: str | os.PathLike, *,
                           pid_alive: Callable[[int], bool] = _pid_alive,
                           hostname: str | None = None) -> bool:
    """Remove a STALE Chrome Singleton{Lock,Cookie,Socket} from the profile so a
    relaunch isn't refused. No-op when the profile has no lock or the lock's
    owner is alive on this host. Returns True if it cleared something."""
    prof = Path(profile_dir)
    lock = prof / "SingletonLock"
    if not (lock.is_symlink() or lock.exists()):
        return False
    try:
        target = os.readlink(lock)
    except OSError:
        target = ""
    host = hostname if hostname is not None else socket.gethostname()
    if not singleton_is_stale(target, hostname=host, pid_alive=pid_alive):
        return False
    cleared = False
    for n in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            (prof / n).unlink()
            cleared = True
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return cleared


def _sock_alive(sock_path: Path, timeout: float = 1.5) -> bool:
    try:
        _client.call_on(sock_path, "status", timeout=timeout)
        return True
    except _client.DaemonError:
        return True  # answered (even an error) → socket is live
    except (_client.DaemonNotRunning, OSError):
        return False


def _spawn_login_daemon(env: dict[str, str], sock_path: Path,
                        ready_timeout: float = 15.0) -> subprocess.Popen:
    """Spawn a DETACHED daemon (survives this process so the window persists) and
    wait for its socket to answer. Mirrors client.spawn_daemon but with a custom
    env and an explicit socket."""
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "vibatchium.daemon.server"],
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
    )
    deadline = time.time() + ready_timeout
    while time.time() < deadline:
        if _sock_alive(Path(sock_path), timeout=2.0):
            return proc
        rc = proc.poll()
        if rc is not None and rc != 2:  # rc==2 = 0.9.1 singleton found incumbent
            raise _client.DaemonError(f"login daemon exited before ready (rc={rc})")
        time.sleep(0.1)
    raise _client.DaemonError(f"login daemon did not come up within {ready_timeout}s")


# ─── orchestrators ───────────────────────────────────────────────────────────

def _base_runtime(base_env: Mapping[str, str]) -> str:
    return base_env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"


def run_login(name: str, *, url: str | None = None,
              base_env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Open (or reuse) a headed login window for session NAME. Returns a summary
    dict. Raises NoDisplayError if there's no X display to render into."""
    base_env = os.environ if base_env is None else base_env
    validate_name(name, kind="session name")
    rt = login_runtime_dir(_base_runtime(base_env), name)
    sock = sock_for_runtime(rt)
    profile_dir = PROFILES_DIR / name

    daemon_up = _sock_alive(sock)
    if not daemon_up:
        display = resolve_display(base_env)
        if not display:
            raise NoDisplayError(
                "no DISPLAY available — a headed login needs an X display. On a "
                "headless host, log in via the cookie-import path instead.")
        xauth = resolve_xauthority(base_env, list(xauth_candidates()))
        rt.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(rt, 0o700)
        except OSError:
            pass
        env = build_login_env(base_env, rt, display=display, xauthority=xauth,
                              log_file=rt / "daemon.log")
        _spawn_login_daemon(env, sock)
    else:
        # Daemon already up, but the previous window may have been CLOSED (its
        # browser context is dead while the daemon lingers) — reusing it would
        # show no window. Drop any existing session for this name so the start
        # below relaunches a guaranteed-fresh, visible one.
        try:
            _client.call_on(sock, "session_close", {"name": name}, timeout=15.0)
        except Exception:  # noqa: BLE001
            pass

    # Clear a stale Chrome SingletonLock (from a previously-closed window) so the
    # launch isn't refused.
    clear_stale_singletons(profile_dir)
    _client.call_on(sock, "start", {"headless": False}, session=name)
    if url:
        # MUST pass session=name. Without it the daemon routes `go` to its
        # active/default session, which auto-starts a SECOND Chrome on the WRONG
        # profile (profiles/default) — giving two windows AND, fatally, writing
        # the login cookies into profiles/default instead of this login's
        # profile, so the bot never sees the login. (This was the reported bug.)
        _client.call_on(sock, "go", {"url": url}, session=name)

    return {
        "session": name,
        "headed": True,
        "daemon_reused": daemon_up,
        "profile": str(profile_dir),
        "runtime_dir": str(rt),
        "sock": str(sock),
        "display": base_env.get("DISPLAY"),
        "url": url,
    }


def close_login(name: str, *,
                base_env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Tear down NAME's login daemon (closing the window) and remove its runtime
    dir. Best-effort: a missing daemon is fine."""
    base_env = os.environ if base_env is None else base_env
    rt = login_runtime_dir(_base_runtime(base_env), name)
    sock = sock_for_runtime(rt)
    closed = False
    try:
        _client.call_on(sock, "shutdown", timeout=5.0)
        closed = True
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(rt, ignore_errors=True)
    return {"session": name, "closed": closed, "runtime_dir": str(rt)}
