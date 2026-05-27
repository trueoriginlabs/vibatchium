"""Vibatchium daemon — long-lived browser process exposing JSON-RPC over Unix socket."""
from .paths import (
    ACTIVE_PROFILE_PATH, ACTIVE_SESSION_PATH,
    CACHE_DIR, CONFIG_DIR,
    DEFAULT_PROFILE_DIR, DEFAULT_SESSION_NAME,
    PID_PATH, PROFILES_DIR, SOCK_PATH,
    get_active_profile_dir, get_active_profile_name,
    get_active_session_name, list_profile_names, list_session_names,
    session_dir, set_active_profile_name, set_active_session_name,
)
from .registry import SessionEntry, SessionLimitError, SessionRegistry, current_session_ctx, get_max_sessions

__all__ = [
    # paths
    "SOCK_PATH", "PID_PATH", "CACHE_DIR", "CONFIG_DIR", "PROFILES_DIR",
    "DEFAULT_PROFILE_DIR", "DEFAULT_SESSION_NAME",
    "ACTIVE_PROFILE_PATH", "ACTIVE_SESSION_PATH",
    "get_active_profile_name", "get_active_profile_dir", "list_profile_names",
    "set_active_profile_name",
    "get_active_session_name", "set_active_session_name",
    "session_dir", "list_session_names",
    # registry (multi-session)
    "SessionRegistry", "SessionEntry", "SessionLimitError",
    "current_session_ctx", "get_max_sessions",
]
