"""Opt-in, TTL-bounded session leases (0.7.0).

A *pure* module — no daemon, asyncio, or I/O — so the lease decision logic is
trivially unit-testable and has one source of truth. The registry holds the
live lease dict on each `SessionEntry`; the dispatcher consults the helpers
here to decide whether a caller may operate on a leased session.

Model
-----
A lease is advisory coordination, NOT a hard mutex. While a session is leased,
session-scoped verbs (and the disruptive registry verbs: stop / session_close /
session_delete / proxy_set / proxy_clear / geo_set / geo_clear) from a caller
that does not present the matching token are refused with a clean ``busy``
error *before* the per-session lock is taken — so a non-holder returns
instantly instead of blocking behind the holder. The operator sledgehammers
(``session_close_all`` / ``shutdown`` / ``clean``) and the UNLOCKED verbs
(waits, ``explore``) are deliberately NOT gated.

Security note: the presented token always arrives in the request as
``args['_lease']``. The client reads ``VIBATCHIUM_LEASE`` from its *own* env and
injects it; the daemon must NEVER read the env itself (its env would otherwise
be a master token for every client). See ``holder_token_from_args``.
"""
from __future__ import annotations

import hmac
import secrets
import time

LEASE_DEFAULT_TTL_S = 60
LEASE_MIN_TTL_S = 1
LEASE_MAX_TTL_S = 3600  # a forgotten lease self-heals within 1h


def clamp_ttl(ttl_s) -> int:
    """Clamp a requested TTL into [MIN, MAX]; junk → the default."""
    try:
        v = int(ttl_s)
    except (TypeError, ValueError):
        v = LEASE_DEFAULT_TTL_S
    return max(LEASE_MIN_TTL_S, min(LEASE_MAX_TTL_S, v))


def mint_token() -> str:
    return secrets.token_urlsafe(16)


def is_expired(lease: dict, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return now >= lease["expires_at"]


def holder_token_from_args(args: dict) -> str | None:
    """The presented token — ONLY from ``args['_lease']``.

    The env (``VIBATCHIUM_LEASE``) is read CLIENT-side in ``client.call()`` and
    injected here as ``_lease``. Reading ``os.environ`` on the DAEMON side would
    make the daemon's own env a master token for every client — a
    privilege-escalation bug. Do NOT add an env read here.
    """
    t = args.get("_lease")
    return str(t) if t else None


def _token_eq(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(str(a), str(b))  # constant-time


def check_access(lease: dict | None, presented: str | None, name: str,
                 now: float | None = None):
    """Decide whether ``presented`` may operate on a (possibly) leased session.

    ``lease`` must be the ALREADY-lazily-expired active lease (callers pass
    ``entry.lease_active()``). Returns ``(allowed: bool, reason: str | None)``.
    """
    if lease is None:
        return True, None
    if _token_eq(presented, lease["token"]):
        return True, None
    return False, busy_message(lease, name, now)


def busy_message(lease: dict, name: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    remaining = max(0, int(lease["expires_at"] - now))
    when = time.strftime("%H:%M:%S", time.localtime(lease["expires_at"]))
    return (f"session {name!r} busy: leased by {lease['owner']!r} until {when} "
            f"({remaining}s left); present the lease token "
            f"(--lease-token / VIBATCHIUM_LEASE), wait, or "
            f"`vb session release {name} --force` to break it")


def lease_public(lease: dict | None, now: float | None = None) -> dict | None:
    """Token-free projection for status / session_list / lease-info.

    NEVER includes ``token`` — observability must not leak the holder's key.
    """
    if lease is None:
        return None
    now = time.time() if now is None else now
    return {
        "owner": lease["owner"],
        "expires_at": lease["expires_at"],
        "expires_in_s": max(0, int(lease["expires_at"] - now)),
        "acquired_at": lease["acquired_at"],
    }
