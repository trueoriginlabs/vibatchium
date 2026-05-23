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
# Wave 7.5d: enforce 0700 on the cache root so other system users can't
# browse session names / cached data. (mkdir respects umask, which is
# typically 0022 → 0755; we narrow it explicitly.)
try:
    os.chmod(CACHE_DIR, 0o700)
except OSError:
    pass

# Profiles live OUTSIDE the runtime dir so they survive reboots. Sock+pid stay
# in the runtime dir so a stale socket doesn't persist across reboots.
CONFIG_DIR = Path.home() / ".config" / "patchium"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(CONFIG_DIR, 0o700)
except OSError:
    pass
PROFILES_DIR = CONFIG_DIR / "profiles"
PROFILES_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(PROFILES_DIR, 0o700)
except OSError:
    pass
ACTIVE_PROFILE_PATH = CONFIG_DIR / "active-profile"
ACTIVE_SESSION_PATH = CONFIG_DIR / "active-session"

SOCK_PATH = CACHE_DIR / "daemon.sock"
PID_PATH = CACHE_DIR / "daemon.pid"
LOG_PATH = CACHE_DIR / "daemon.log"
DEFAULT_PROFILE_DIR = PROFILES_DIR / "default"
DEFAULT_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(DEFAULT_PROFILE_DIR, 0o700)
except OSError:
    pass

DEFAULT_SESSION_NAME = "default"


# ─── Wave 7.5d: secure write helper ──────────────────────────────────────

def secure_write(path: Path, content: str | bytes) -> None:
    """Write a file with 0600 perms regardless of umask.

    Use this for every file patchium produces that could carry sensitive
    data (cookies, auth headers, request bodies, cached intents, secrets).
    Caller-controlled output paths (`patchium screenshot -o foo.png`) are
    explicitly NOT routed through this — the user picked the path and
    may want to share the artifact; for those, document the perms model
    instead of forcing it.

    Atomic: writes to a temp file in the same directory, fchmods to 0600
    *before* the rename, then renames. Eliminates the brief window where
    the file could be world-readable after creation but before chmod.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Tempfile in same dir so rename is atomic on the same fs.
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    mode = "wb" if isinstance(content, (bytes, bytearray)) else "w"
    try:
        # os.open with mode lets us chmod-on-open atomically.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(str(tmp), flags, 0o600)
        with os.fdopen(fd, mode) as f:
            f.write(content)
        os.replace(str(tmp), str(path))
    except Exception:
        # Best-effort cleanup
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def secure_mkdir(path: Path) -> Path:
    """mkdir -p with 0700 perms — for any dir patchium creates that may
    hold sensitive children (checkpoints, network dumps, vision cache)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


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
    secure_write(ACTIVE_SESSION_PATH, name)
    secure_write(ACTIVE_PROFILE_PATH, name)


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
