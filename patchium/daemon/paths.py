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

# Profiles live OUTSIDE the runtime dir so they survive reboots. Sock+pid stay
# in the runtime dir so a stale socket doesn't persist across reboots.
CONFIG_DIR = Path.home() / ".config" / "patchium"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
PROFILES_DIR = CONFIG_DIR / "profiles"
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
ACTIVE_PROFILE_PATH = CONFIG_DIR / "active-profile"

SOCK_PATH = CACHE_DIR / "daemon.sock"
PID_PATH = CACHE_DIR / "daemon.pid"
LOG_PATH = CACHE_DIR / "daemon.log"
DEFAULT_PROFILE_DIR = PROFILES_DIR / "default"
DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def get_active_profile_name() -> str:
    """Return the active profile name (default 'default')."""
    if ACTIVE_PROFILE_PATH.exists():
        return ACTIVE_PROFILE_PATH.read_text().strip() or "default"
    return "default"


def set_active_profile_name(name: str) -> None:
    ACTIVE_PROFILE_PATH.write_text(name)


def get_active_profile_dir() -> Path:
    """Return the active profile's user-data-dir path, creating it if missing."""
    p = PROFILES_DIR / get_active_profile_name()
    p.mkdir(parents=True, exist_ok=True)
    return p


def list_profile_names() -> list[str]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir())
