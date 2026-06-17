"""Sync RPC client used by the CLI. Connects to the daemon over Unix socket,
sends one JSON-line request, reads one JSON-line response."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import uuid
from typing import Any

from .daemon.paths import SOCK_PATH


class DaemonNotRunning(RuntimeError):
    pass


class DaemonError(RuntimeError):
    pass


def _connect(timeout: float = 2.0) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(SOCK_PATH))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        s.close()
        raise DaemonNotRunning(f"daemon not running ({exc})") from exc
    return s


def daemon_is_running(timeout: float = 1.5, attempts: int = 2) -> bool:
    # 0.9.1: a momentarily-busy daemon (memory pressure, GC pause) must not be
    # misread as "not running" — that used to trigger a duplicate spawn. Retry
    # with a tolerant timeout before concluding it's down.
    if not SOCK_PATH.exists():
        return False
    for i in range(attempts):
        try:
            s = _connect(timeout=timeout)
            s.close()
            return True
        except DaemonNotRunning:
            if i + 1 < attempts:
                time.sleep(0.2)
    return False


def spawn_daemon(wait: float = 5.0) -> None:
    """Spawn the daemon process detached from this process. Returns once the
    socket is accepting.

    Uses subprocess.Popen with start_new_session=True instead of double-fork
    so we don't leak fds, signal handlers, or asyncio loop state from the
    calling process into the daemon. Safer when vibatchium is invoked from
    long-lived hosts (Claude Code, an MCP shell, a notebook).
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "vibatchium.daemon.server"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.time() + wait
    while time.time() < deadline:
        if daemon_is_running():
            return
        rc = proc.poll()
        if rc is not None and rc != 2:
            raise DaemonError(f"daemon exited immediately (rc={rc})")
        # rc == 2 (0.9.1 singleton): our spawn lost the race / found an incumbent
        # daemon already holding the lock — that's success, not failure. Keep
        # polling until the incumbent's socket answers (or the deadline).
        time.sleep(0.1)
    raise DaemonError(f"daemon did not come up within {wait}s")


def call(cmd: str, args: dict[str, Any] | None = None, *,
         session: str | None = None, lease: str | None = None,
         auto_spawn: bool = True, timeout: float = 120.0) -> Any:
    """RPC call. If daemon isn't running and auto_spawn=True, spawn it first.

    Args:
      cmd: daemon verb name.
      args: verb args dict.
      session: target session name. None = active session (server-side default).
               Sent as the special `_session` field — the daemon's dispatcher
               consumes it before invoking the handler.
      lease: lease token to present (0.7.0). None = read VIBATCHIUM_LEASE from
             this client's OWN env. Sent as the special `_lease` field. The
             token is resolved CLIENT-side and never read daemon-side (the
             daemon's env must not be a master token for every client).
      auto_spawn: spawn the daemon if it isn't running.
      timeout: socket read timeout.
    """
    if not daemon_is_running():
        if not auto_spawn:
            raise DaemonNotRunning("daemon not running (auto_spawn=False)")
        spawn_daemon()

    payload_args = dict(args or {})
    # Resolution: explicit kwarg → VIBATCHIUM_SESSION env → (omitted; daemon uses
    # active-session file → 'default'). This mirrors `kubectl --context` /
    # KUBECONFIG semantics.
    if session is None:
        session = os.environ.get("VIBATCHIUM_SESSION") or None
    if session:
        payload_args["_session"] = session
    # 0.7.0 lease token — client-side resolution only. The guard keeps default
    # (no-lease) callers byte-identical to pre-0.7.0.
    if lease is None:
        lease = os.environ.get("VIBATCHIUM_LEASE") or None
    if lease and "_lease" not in payload_args:
        payload_args["_lease"] = lease

    s = _connect(timeout=timeout)
    try:
        req = json.dumps({"id": uuid.uuid4().hex, "cmd": cmd, "args": payload_args}) + "\n"
        s.sendall(req.encode())
        # readline
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        resp = json.loads(buf.decode())
    finally:
        s.close()

    if not resp.get("ok"):
        raise DaemonError(resp.get("error", "unknown error"))
    return resp.get("result")
