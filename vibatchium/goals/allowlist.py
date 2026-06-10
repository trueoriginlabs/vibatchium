"""Goal domain allowlist — parse + enforce the per-goal ``domain_allowlist``.

A goal may pin its owned session to a set of allowed origins (CLI
``--allow-domains`` / MCP ``allow_domains`` — "CSV of allowed origins"). While
the goal owns the session, navigations to a host that is neither an allowed
host nor a subdomain of one are REFUSED at the navigation chokepoint (the ``go``
verb). This module holds the (dependency-free, testable) parse + match logic so
the engine, the daemon dispatcher path, and tests can all share one definition.

Matching policy (chosen — see the bug fix note):
  * Host-only, scheme- and port-agnostic. An allowlist entry may be written as
    a bare host (``example.com``), a full origin (``https://example.com``), or a
    wildcard (``*.example.com``); all normalize to the bare host.
  * A target host is permitted iff it equals an allowed host OR is a subdomain
    of one (``a.b.example.com`` matches ``example.com``). Suffix-confusion
    attacks (``example.com.evil.com``) are NOT matched.
  * A URL with no resolvable host (``about:blank``, ``data:``/``chrome:`` URIs,
    schemeless input) is REFUSED whenever the allowlist is non-empty — the
    boundary refuses rather than silently proceeding.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def _host_of_entry(token: str) -> str:
    """Normalize one allowlist token to a bare lowercase host (no scheme, port,
    path, or leading ``*.``). Returns '' for an unusable token."""
    token = (token or "").strip().lower()
    if not token:
        return ""
    if "://" in token:
        return urlsplit(token).hostname or ""
    # Bare host[:port][/path] — keep only the host label run.
    host = token.split("/", 1)[0].split(":", 1)[0]
    if host.startswith("*."):
        host = host[2:]
    return host


def parse_allowlist(csv: str | None) -> set[str]:
    """Parse a ``a,b,c`` CSV of origins into a set of normalized bare hosts.
    Empty/None → empty set (meaning: no restriction)."""
    if not csv:
        return set()
    out: set[str] = set()
    for part in csv.split(","):
        h = _host_of_entry(part)
        if h:
            out.add(h)
    return out


def host_of_url(url: str) -> str:
    """Extract the lowercase host of a URL, or '' if none (about:/data:/relative)."""
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def origin_allowed(url: str, allowed: set[str]) -> bool:
    """True iff navigating to ``url`` is permitted under ``allowed`` (a set of
    normalized bare hosts). An empty ``allowed`` set means no restriction → True.
    """
    if not allowed:
        return True
    host = host_of_url(url)
    if not host:
        return False  # no resolvable host while a restriction is active → refuse
    for a in allowed:
        if host == a or host.endswith("." + a):
            return True
    return False
