"""Patchium daemon — long-lived browser process exposing JSON-RPC over Unix socket."""
from .paths import (
    SOCK_PATH, PID_PATH, CACHE_DIR, CONFIG_DIR, PROFILES_DIR,
    DEFAULT_PROFILE_DIR, ACTIVE_PROFILE_PATH,
    get_active_profile_name, get_active_profile_dir,
    set_active_profile_name, list_profile_names,
)

__all__ = [
    "SOCK_PATH", "PID_PATH", "CACHE_DIR", "CONFIG_DIR", "PROFILES_DIR",
    "DEFAULT_PROFILE_DIR", "ACTIVE_PROFILE_PATH",
    "get_active_profile_name", "get_active_profile_dir",
    "set_active_profile_name", "list_profile_names",
]
