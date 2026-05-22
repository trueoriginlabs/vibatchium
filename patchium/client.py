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
from pathlib import Path
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
        raise DaemonNotRunning(f"daemon not running ({exc})")
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
    """Fork+detach the daemon process. Returns once the socket is accepting."""
    # double-fork to detach from controlling terminal
    pid = os.fork()
    if pid > 0:
        # parent: wait for sock to appear
        deadline = time.time() + wait
        while time.time() < deadline:
            if daemon_is_running():
                return
            time.sleep(0.1)
        raise DaemonError(f"daemon did not come up within {wait}s")

    # child: detach session, double-fork
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)
    # grandchild: become the daemon
    sys.stdin = open(os.devnull)
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    from .daemon import server
    server.main()
    os._exit(0)


def call(cmd: str, args: dict[str, Any] | None = None, *, auto_spawn: bool = True, timeout: float = 60.0) -> Any:
    """RPC call. If daemon isn't running and auto_spawn=True, spawn it first."""
    if not daemon_is_running():
        if not auto_spawn:
            raise DaemonNotRunning("daemon not running (auto_spawn=False)")
        spawn_daemon()

    s = _connect(timeout=timeout)
    try:
        req = json.dumps({"id": uuid.uuid4().hex, "cmd": cmd, "args": args or {}}) + "\n"
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
