"""URL → host key, and the go-time note lookup.

Matching is deliberately dumb by default (browser-use parity): strip the scheme,
lowercase, drop a leading ``www.``, and use the resulting hostname as the
directory key. No public-suffix-list dependency — ``docs.python.org`` keeps its
subdomain; ``www.amazon.com`` collapses to ``amazon.com``.

Opt-in **registrable-domain** mode (``VIBATCHIUM_SKILL_REGISTRABLE_DOMAIN=1``)
collapses every subdomain to the registrable domain so
``m.youtube.com`` / ``music.youtube.com`` / ``youtube.com`` share one bucket.
It uses a small bundled multi-label suffix list (no PSL dependency), so a few
exotic public suffixes may collapse one label too far — acceptable for a
field-notes key.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlparse

from . import store

# A usable hostname: dot-separated labels of [a-z0-9-]. Rejects junk that the
# lenient `//` parse below would otherwise accept (e.g. "not a url").
_HOSTNAME_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*$")

# Bundled two-label public suffixes (registrable domain = the label before
# these + the suffix). Covers the common country-code second-level domains; not
# exhaustive (that's what a PSL is for), but enough for a notes key.
_MULTI_SUFFIXES: frozenset[str] = frozenset({
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "gov.au", "edu.au", "id.au",
    "co.nz", "net.nz", "org.nz", "govt.nz",
    "co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp",
    "com.br", "net.br", "org.br", "gov.br",
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "co.in", "net.in", "org.in", "gov.in", "ac.in",
    "co.za", "org.za", "gov.za",
    "com.sg", "com.hk", "com.tw", "com.mx", "com.tr",
    "co.kr", "or.kr", "co.id", "co.th", "com.ph",
})


def _registrable_domain(host: str) -> str:
    """Collapse ``host`` to its registrable domain using the bundled suffix
    list. ``music.youtube.com`` → ``youtube.com``; ``foo.co.uk`` → ``foo.co.uk``."""
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _registrable_enabled() -> bool:
    return os.environ.get("VIBATCHIUM_SKILL_REGISTRABLE_DOMAIN", "0").lower() in (
        "1", "true", "yes", "on")


def host_key(url: str, *, registrable: bool | None = None) -> str | None:
    """Return the host key for a URL, or None if it has no usable hostname.

    ``registrable`` forces registrable-domain collapse on/off; ``None`` reads
    ``VIBATCHIUM_SKILL_REGISTRABLE_DOMAIN`` (default off — browser-use parity).
    """
    if not url or not isinstance(url, str):
        return None
    parsed = urlparse(url if "://" in url else f"//{url}", scheme="")
    host = (parsed.hostname or "").lower()
    if not host or not _HOSTNAME_RE.match(host):
        return None
    if registrable is None:
        registrable = _registrable_enabled()
    if registrable:
        return _registrable_domain(host) or None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def find_notes(url: str) -> tuple[str | None, list[str]]:
    """Return ``(host_key, [note_filenames])`` for a URL. Empty list if none."""
    host = host_key(url)
    if not host:
        return None, []
    try:
        return host, store.list_notes(host)
    except ValueError:
        # Host failed validation (exotic hostname) — no notes rather than error.
        return host, []
