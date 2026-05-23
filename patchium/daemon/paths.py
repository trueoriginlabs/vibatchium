"""Filesystem locations for the daemon socket, pidfile, profiles, and session registry.

Patchium uses a **1:1 profile↔session** model: every named session has a
matching profile dir under `~/.config/patchium/profiles/<name>/`. The OS-level
lock on Chrome's `user-data-dir` enforces this — two sessions cannot share a
profile concurrently — so the 1:1 mapping is the path of least surprise.

- "session name" is the active identifier the CLI/MCP uses to address one
  concurrent browser (e.g. `patchium --session work click @e3`).
- "profile dir" is the on-disk Chrome user-data-dir that holds cookies,
  localStorage, IndexedDB, etc. It persists across `session close` so
  re-opening keeps you logged in.

`active-session` and `active-profile` files are kept in sync (1:1) — for
backwards-compat with the pre-multi-session API, `profile_*` verbs are aliases
for the corresponding `session_*` verbs.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# ─── name validation (Wave 7.5b — path-traversal hardening) ──────────────

# Single allowed-name regex shared by every verb that interpolates a
# caller-supplied identifier into a filesystem path. Rejects path separators,
# leading dot, parent-dir segments, and any control characters.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Hard cap so a 4 KB name can't blow out a path on filesystems with PATH_MAX.
_NAME_MAX_LEN = 64


def validate_name(name: str | None, *, kind: str = "name") -> str:
    """Validate a session/profile/checkpoint identifier.

    Raises ValueError if the name would be unsafe to splice into a path:
      - empty / non-string
      - longer than 64 characters
      - contains anything outside [A-Za-z0-9._-]
      - starts with a dot (would create a hidden file, or be `..`)
      - is exactly `.` or `..`

    Returns the name unchanged on success — caller-friendly chaining.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"bad {kind}: must be a non-empty string")
    if len(name) > _NAME_MAX_LEN:
        raise ValueError(
            f"bad {kind} {name!r}: max {_NAME_MAX_LEN} characters"
        )
    if name in {".", ".."}:
        raise ValueError(f"bad {kind} {name!r}: reserved")
    if not _NAME_RE.match(name):
        raise ValueError(
            f"bad {kind} {name!r}: only [A-Za-z0-9._-] allowed, "
            f"must not start with '.'"
        )
    return name

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
ACTIVE_SESSION_PATH = CONFIG_DIR / "active-session"

SOCK_PATH = CACHE_DIR / "daemon.sock"
PID_PATH = CACHE_DIR / "daemon.pid"
LOG_PATH = CACHE_DIR / "daemon.log"
DEFAULT_PROFILE_DIR = PROFILES_DIR / "default"
DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SESSION_NAME = "default"


# ─── session naming + registry ───────────────────────────────────────────────


def _read_name(path: Path, fallback: str) -> str:
    if path.exists():
        v = path.read_text().strip()
        if v:
            return v
    return fallback


def get_active_session_name() -> str:
    """Resolve the active session name.

    Order: ACTIVE_SESSION_PATH file → legacy ACTIVE_PROFILE_PATH file → 'default'.
    Reading the legacy path keeps pre-Wave-5 setups working without migration.
    """
    name = _read_name(ACTIVE_SESSION_PATH, "")
    if name:
        return name
    return _read_name(ACTIVE_PROFILE_PATH, DEFAULT_SESSION_NAME)


def set_active_session_name(name: str) -> None:
    """Persist the active session. Mirrors to ACTIVE_PROFILE_PATH so the legacy
    `profile_use` verb stays in sync (1:1 model)."""
    ACTIVE_SESSION_PATH.write_text(name)
    ACTIVE_PROFILE_PATH.write_text(name)


def session_dir(name: str) -> Path:
    """Return the on-disk dir for a session (creates if missing).

    Names are validated by the caller (`session_new` handler). Absolute paths
    are accepted as-is — useful for tests / ephemeral profiles.
    """
    if not name:
        name = DEFAULT_SESSION_NAME
    p = Path(name)
    if p.is_absolute():
        p.mkdir(parents=True, exist_ok=True)
        return p
    out = PROFILES_DIR / name
    out.mkdir(parents=True, exist_ok=True)
    return out


def list_session_names() -> list[str]:
    """List all on-disk session/profile names (sorted)."""
    if not PROFILES_DIR.exists():
        return []
    return sorted(p.name for p in PROFILES_DIR.iterdir() if p.is_dir())


# ─── legacy profile API (1:1 aliases) ────────────────────────────────────────


def get_active_profile_name() -> str:
    """Legacy alias — returns the active session name (1:1)."""
    return get_active_session_name()


def set_active_profile_name(name: str) -> None:
    """Legacy alias — sets the active session name."""
    set_active_session_name(name)


def get_active_profile_dir() -> Path:
    """Legacy alias — returns the dir of the active session."""
    return session_dir(get_active_session_name())


def list_profile_names() -> list[str]:
    """Legacy alias — same as list_session_names()."""
    return list_session_names()
