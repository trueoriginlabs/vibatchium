"""Filesystem locations for the daemon socket, pidfile, and persistent profile."""
from __future__ import annotations

import os
from pathlib import Path

_xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
if _xdg_runtime and Path(_xdg_runtime).is_dir():
    CACHE_DIR = Path(_xdg_runtime) / "patchium"
else:
    CACHE_DIR = Path.home() / ".cache" / "patchium"

CACHE_DIR.mkdir(parents=True, exist_ok=True)

SOCK_PATH = CACHE_DIR / "daemon.sock"
PID_PATH = CACHE_DIR / "daemon.pid"
LOG_PATH = CACHE_DIR / "daemon.log"
DEFAULT_PROFILE_DIR = CACHE_DIR / "chrome-profile"
