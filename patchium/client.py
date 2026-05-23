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


def daemon_is_running() -> bool:
    if not SOCK_PATH.exists():
        return False
    try:
        s = _connect(timeout=0.5)
        s.close()
        return True
    except DaemonNotRunning:
        return False


def spawn_daemon(wait: float = 5.0) -> None:
    """Spawn the daemon process detached from this process. Returns once the
    socket is accepting.

    Uses subprocess.Popen with start_new_session=True instead of double-fork
    so we don't leak fds, signal handlers, or asyncio loop state from the
    calling process into the daemon. Safer when patchium is invoked from
    long-lived hosts (Claude Code, an MCP shell, a notebook).
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "patchium.daemon.server"],
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
        if proc.poll() is not None:
            raise DaemonError(f"daemon exited immediately (rc={proc.returncode})")
        time.sleep(0.1)
    raise DaemonError(f"daemon did not come up within {wait}s")


def call(cmd: str, args: dict[str, Any] | None = None, *,
         session: str | None = None,
         auto_spawn: bool = True, timeout: float = 120.0) -> Any:
    """RPC call. If daemon isn't running and auto_spawn=True, spawn it first.

    Args:
      cmd: daemon verb name.
      args: verb args dict.
      session: target session name. None = active session (server-side default).
               Sent as the special `_session` field — the daemon's dispatcher
               consumes it before invoking the handler.
      auto_spawn: spawn the daemon if it isn't running.
      timeout: socket read timeout.
    """
    if not daemon_is_running():
        if not auto_spawn:
            raise DaemonNotRunning("daemon not running (auto_spawn=False)")
        spawn_daemon()

    payload_args = dict(args or {})
    # Resolution: explicit kwarg → PATCHIUM_SESSION env → (omitted; daemon uses
    # active-session file → 'default'). This mirrors `kubectl --context` /
    # KUBECONFIG semantics.
    if session is None:
        session = os.environ.get("PATCHIUM_SESSION") or None
    if session:
        payload_args["_session"] = session

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
