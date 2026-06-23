"""Ergonomic Python SDK — guaranteed-teardown context managers (0.11.0).

Two context managers, both built so the resource is ALWAYS released on block
exit, including when the body raises:

    import vibatchium as vb

    # A throwaway session, closed + profile-deleted on exit:
    with vb.session(ephemeral=True) as s:
        s.go("https://example.com")
        print(s.text())

    # A private daemon on its OWN runtime dir AND HOME, fully removed on exit:
    with vb.isolated_daemon(home="/tmp/my-home") as d:
        with d.session() as s:
            s.go("https://example.com")

Why the isolated daemon overrides HOME and not just XDG_RUNTIME_DIR:
`daemon/paths.py` derives the socket/pid/lock from XDG_RUNTIME_DIR but derives
PROFILES_DIR / CONFIG_DIR / STATE_DIR from HOME. Isolating only the runtime dir
would still land leaked profiles in the *shared* ~/.config/vibatchium/profiles —
exactly the blast radius behind the 1540-profile leak incident. So
`IsolatedDaemon` overrides HOME too, giving the private daemon a wholly separate
on-disk footprint that we rmtree on teardown. This helper is the single
keystone the bench harness and the session wrapper both build on.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from collections.abc import Callable, Iterator

from . import client as _client

# Floor of available memory (MB) below which `IsolatedDaemon` refuses to spawn.
# The box this runs on is frequently memory-tight (live bots + heavy swap), and
# a private daemon that goes on to launch Chrome can tip it into OOM — which
# would endanger unrelated live processes. Override with VIBATCHIUM_SDK_RAM_FLOOR_MB
# (0 disables the check).
_DEFAULT_RAM_FLOOR_MB = int(os.environ.get("VIBATCHIUM_SDK_RAM_FLOOR_MB", "600"))


class RamFloorError(RuntimeError):
    """Raised when available memory is below the isolated-daemon floor."""


def _mem_available_mb() -> int | None:
    """Available memory in MB from /proc/meminfo, or None if unreadable
    (non-Linux, sandboxed /proc). None means 'can't tell' → don't block."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


# ─── the session wrapper (F2) ────────────────────────────────────────────────


class Session:
    """A live session handle. Thin, typed convenience over the daemon verbs —
    every method routes through the bound `call` so it targets THIS session by
    name, never the active-session default."""

    def __init__(self, call: Callable[..., Any], name: str, *,
                 ephemeral: bool = True):
        self._call = call
        self.name = name
        self.ephemeral = ephemeral

    def call(self, verb: str, args: dict[str, Any] | None = None) -> Any:
        """Invoke any daemon verb against this session."""
        return self._call(verb, args or {}, session=self.name)

    def go(self, url: str, **kw: Any) -> Any:
        return self.call("go", {"url": url, **kw})

    def text(self, **kw: Any) -> str:
        r = self.call("text", dict(kw))
        return r.get("text", "") if isinstance(r, dict) else r

    def html(self, **kw: Any) -> str:
        r = self.call("html", dict(kw))
        return r.get("html", "") if isinstance(r, dict) else r

    def click(self, target: str, **kw: Any) -> Any:
        return self.call("click", {"target": target, **kw})

    def screenshot(self, path: str | os.PathLike | None = None, **kw: Any) -> Any:
        a = dict(kw)
        if path is not None:
            a["path"] = str(path)
        return self.call("screenshot", a)


@contextmanager
def session(ephemeral: bool = True, *, name: str | None = None,
            headless: bool = True, backend: str | None = None,
            call: Callable[..., Any] | None = None) -> Iterator[Session]:
    """Open a session, yield a `Session`, and GUARANTEE teardown on exit.

    The default is an ephemeral throwaway: created, used, then closed and its
    profile dir removed — on normal exit AND on exception. The ephemeral path
    calls `start{ephemeral:true}` DIRECTLY rather than `session_new` first,
    because `session_new` defaults `prewarm=True` and would spawn a second,
    redundant warm Chrome on this memory-tight box.

    Pass `call=` to target a specific daemon (e.g. `IsolatedDaemon.call`);
    defaults to the ambient daemon via `client.call`. Note: an AMBIENT ephemeral
    session writes its throwaway profile under the shared
    ~/.config/vibatchium/profiles (cleaned by close); for a fully private
    footprint, open it on an `isolated_daemon(...)` instead.
    """
    _call = call or _client.call
    if name is not None:
        # A caller-supplied name reaches `start` → session_dir(), which does NOT
        # validate (only session_new/close/delete do) — guard against traversal
        # here, and so teardown's validate_name can't reject our own name.
        from .daemon.paths import validate_name
        validate_name(name, kind="session name")
    sname = name or f"sdk_{uuid.uuid4().hex[:8]}"
    try:
        if ephemeral:
            args: dict[str, Any] = {"ephemeral": True, "headless": headless}
            if backend:
                args["backend"] = backend
            # Direct start — NO session_new, so no prewarm Chrome (the correction).
            _call("start", args, session=sname)
        else:
            _call("session_new", {"name": sname, "prewarm": False})
            args = {"headless": headless}
            if backend:
                args["backend"] = backend
            _call("start", args, session=sname)
        yield Session(_call, sname, ephemeral=ephemeral)
    finally:
        # Close stops Chrome; for an ephemeral session the registry also rmtree's
        # the profile dir. Both are best-effort so teardown can never mask the
        # body's own exception. The extra delete on the ephemeral path is a
        # belt-and-suspenders guarantee of no on-disk leak.
        try:
            _call("session_close", {"name": sname})
        except Exception:  # noqa: BLE001
            pass
        if ephemeral:
            try:
                _call("session_delete", {"name": sname})
            except Exception:  # noqa: BLE001
                pass


# ─── the isolated-daemon keystone (shared by bench + session wrapper) ─────────


class IsolatedDaemon:
    """A private daemon on its OWN XDG_RUNTIME_DIR *and* HOME.

    Spawns `python -m vibatchium.daemon.server` as a detached subprocess with
    both env vars overridden, so its socket/lock/pid AND its profiles/config/
    state live under temp dirs we own — zero contact with the shared daemon the
    live bots run on. On `stop()` the daemon is shut down and any temp dirs we
    created are removed. Reach it with `.call(...)` or open sessions on it with
    `.session(...)`.
    """

    def __init__(self, *, home: str | os.PathLike | None = None,
                 runtime_dir: str | os.PathLike | None = None,
                 max_sessions: int | None = None,
                 max_ephemeral: int | None = None,
                 warm: bool = False,
                 ram_floor_mb: int | None = None,
                 ready_timeout: float = 15.0,
                 extra_env: dict[str, str] | None = None):
        self._home_arg = home
        self._runtime_arg = runtime_dir
        self._max_sessions = max_sessions
        self._max_ephemeral = max_ephemeral
        self._warm = warm
        self._ram_floor_mb = (_DEFAULT_RAM_FLOOR_MB if ram_floor_mb is None
                              else ram_floor_mb)
        self._ready_timeout = ready_timeout
        self._extra_env = extra_env or {}

        self._home: Path | None = None
        self._runtime_dir: Path | None = None
        self._owns_home = home is None
        self._owns_runtime = runtime_dir is None
        self._sock_path: Path | None = None
        self._proc: subprocess.Popen | None = None

    # — lifecycle —

    def start(self) -> IsolatedDaemon:
        if self._proc is not None:
            return self  # idempotent
        if self._ram_floor_mb and self._ram_floor_mb > 0:
            avail = _mem_available_mb()
            if avail is not None and avail < self._ram_floor_mb:
                raise RamFloorError(
                    f"only {avail}MB available, isolated daemon needs "
                    f">= {self._ram_floor_mb}MB (set VIBATCHIUM_SDK_RAM_FLOOR_MB=0 "
                    f"or ram_floor_mb=0 to override)")

        self._runtime_dir = self._prep_dir(self._runtime_arg, "vbiso-rt-")
        self._home = self._prep_dir(self._home_arg, "vbiso-home-")
        # Mirror paths.py: SOCK_PATH = <XDG_RUNTIME_DIR>/vibatchium/daemon.sock
        self._sock_path = self._runtime_dir / "vibatchium" / "daemon.sock"

        env = self._child_env()
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "vibatchium.daemon.server"],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            self._await_ready()
        except BaseException:
            # Startup failed (RAM already checked above). __enter__ never
            # returned, so the `with` protocol will NOT call __exit__/stop() —
            # without this we'd leak the temp runtime+home dirs AND a daemon that
            # came up slowly past ready_timeout (still holding its flock). That's
            # exactly the leak class this keystone exists to kill. stop() is
            # idempotent and None-guards every field, so it's safe here.
            self.stop()
            raise
        return self

    def _prep_dir(self, given: str | os.PathLike | None, prefix: str) -> Path:
        if given is not None:
            p = Path(given)
            p.mkdir(parents=True, exist_ok=True)
        else:
            p = Path(tempfile.mkdtemp(prefix=prefix))
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
        return p

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        # Caller extras first, so the forced isolation vars below ALWAYS win — a
        # stray extra_env={"HOME":...} / {"XDG_RUNTIME_DIR":...} can never
        # silently un-isolate the private daemon.
        env.update(self._extra_env)
        env["XDG_RUNTIME_DIR"] = str(self._runtime_dir)
        env["HOME"] = str(self._home)
        # Don't let inherited XDG_*_HOME vars defeat the HOME override (STATE_DIR
        # in particular keys off XDG_STATE_HOME first). Drop them so the private
        # HOME fully governs the on-disk footprint.
        for k in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                  "XDG_DATA_HOME"):
            env.pop(k, None)
        # Overriding HOME hides the patchright/Playwright browser cache (which is
        # HOME-derived: ~/.cache/ms-playwright) — so a session launched on this
        # daemon couldn't find Chrome. Preserve it explicitly so the on-disk
        # ISOLATION (profiles/config/state under the private HOME) doesn't cost us
        # Chrome discovery. Honor an inherited PLAYWRIGHT_BROWSERS_PATH first.
        if not env.get("PLAYWRIGHT_BROWSERS_PATH"):
            real_home = os.environ.get("HOME")
            if real_home:
                cache = Path(real_home) / ".cache" / "ms-playwright"
                if cache.is_dir():
                    env["PLAYWRIGHT_BROWSERS_PATH"] = str(cache)
        # Isolate the daemon log under the private runtime dir as well — belt and
        # suspenders alongside the HOME override.
        env["VIBATCHIUM_LOG_FILE"] = str(self._runtime_dir / "daemon.log")
        env["VIBATCHIUM_WARM"] = "both" if self._warm else "off"
        if self._max_sessions is not None:
            env["VIBATCHIUM_MAX_SESSIONS"] = str(self._max_sessions)
        if self._max_ephemeral is not None:
            env["VIBATCHIUM_MAX_EPHEMERAL"] = str(self._max_ephemeral)
        return env

    def _await_ready(self) -> None:
        deadline = time.time() + self._ready_timeout
        last_exc: Exception | None = None
        while time.time() < deadline:
            try:
                _client.call_on(self._sock_path, "status", timeout=2.0)
                return
            except _client.DaemonNotRunning as exc:
                last_exc = exc
            except (json.JSONDecodeError, OSError) as exc:
                # Socket appeared mid-bind, or a partial/empty response during
                # startup — not ready yet, keep polling (don't let it escape and
                # kill start()).
                last_exc = exc
            except _client.DaemonError:
                return  # server answered (even an error) → the socket is live
            rc = self._proc.poll() if self._proc else None
            if rc is not None:
                raise _client.DaemonError(
                    f"isolated daemon exited before becoming ready (rc={rc})")
            time.sleep(0.1)
        raise _client.DaemonError(
            f"isolated daemon did not come up within {self._ready_timeout}s "
            f"(last: {last_exc})")

    def stop(self) -> None:
        if self._sock_path is not None:
            try:
                _client.call_on(self._sock_path, "shutdown", timeout=5.0)
            except Exception:  # noqa: BLE001
                pass
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass
            self._proc = None
        if self._owns_runtime and self._runtime_dir is not None:
            shutil.rmtree(self._runtime_dir, ignore_errors=True)
        if self._owns_home and self._home is not None:
            shutil.rmtree(self._home, ignore_errors=True)

    # — access —

    @property
    def sock_path(self) -> Path | None:
        return self._sock_path

    @property
    def home(self) -> Path | None:
        return self._home

    @property
    def runtime_dir(self) -> Path | None:
        return self._runtime_dir

    def call(self, cmd: str, args: dict[str, Any] | None = None, *,
             session: str | None = None, timeout: float = 120.0) -> Any:
        """RPC a verb on THIS daemon (not the ambient one)."""
        return _client.call_on(self._sock_path, cmd, args, session=session,
                               timeout=timeout)

    def session(self, ephemeral: bool = True, **kw: Any):
        """Open a guaranteed-teardown session on THIS daemon."""
        return session(ephemeral=ephemeral, call=self.call, **kw)

    def __enter__(self) -> IsolatedDaemon:
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def isolated_daemon(**kw: Any) -> IsolatedDaemon:
    """Return an `IsolatedDaemon` context manager (always isolated).

    Use as `with vb.isolated_daemon(home=...) as d:`. Named `isolated_daemon`
    rather than `daemon` because `vibatchium.daemon` is a core subpackage —
    exporting a top-level `daemon` would collide with it (and resolve
    order-dependently to the module). To drive the AMBIENT daemon instead, call
    `vb.session()` without an explicit `call=`, or use the CLI.

    Equivalent to constructing `IsolatedDaemon(**kw)` directly (the class is a
    context manager too).
    """
    return IsolatedDaemon(**kw)
