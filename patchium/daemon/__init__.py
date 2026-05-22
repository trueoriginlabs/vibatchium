"""Patchium daemon — long-lived browser process exposing JSON-RPC over Unix socket."""
from .paths import SOCK_PATH, PID_PATH, CACHE_DIR

__all__ = ["SOCK_PATH", "PID_PATH", "CACHE_DIR"]
