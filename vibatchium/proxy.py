"""Wave 6.2a — proxy abstraction.

Pluggable proxy adapters that turn `provider://user:pass@host?params` URLs
into the Playwright proxy config dict accepted by `launch_persistent_context`.

Built-in providers:
  - http / https / socks5            generic: passed through with no rewrite
  - brightdata                       Bright Data residential/datacenter zones
  - iproyal                          IPRoyal residential pool + sticky sessions
  - decodo                           Decodo (formerly Smartproxy) residential

Per-session model: the proxy is stored at <profile_dir>/proxy.json and applied
at launch time (Playwright accepts proxy config only at launch, not runtime).
To switch a session's proxy: `vibatchium proxy set/clear`, then restart the
session (close + start).

WebRTC leak guard: when a proxy is configured, the session's Chrome flags
include `--disable-features=WebRtcHideLocalIpsWithMdns,WebRtcAllowInputVolumeAdjustment`
to suppress real-IP leakage via STUN/RTC.

Credential hygiene: support `--proxy-file PATH` (file must be 0600) so the
URL never appears in `ps`/shell history.

Acceptance: see tests/test_wave6_proxy.py.
"""
from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from collections.abc import Callable
from urllib.parse import parse_qsl, urlparse

log = logging.getLogger("vibatchium.proxy")


class ProxyParseError(ValueError):
    """Raised when a proxy URL is malformed or addresses an unknown adapter."""


# ─── adapter registry ──────────────────────────────────────────────────


def _generic(url: str, parsed) -> dict:
    """http://, https://, socks5:// — pass through unchanged."""
    if not parsed.hostname:
        raise ProxyParseError(f"missing host in proxy URL: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    cfg = {"server": f"{parsed.scheme}://{parsed.hostname}:{port}"}
    if parsed.username:
        cfg["username"] = parsed.username
    if parsed.password:
        cfg["password"] = parsed.password
    return cfg


def _brightdata(url: str, parsed) -> dict:
    """Bright Data: brightdata://customer-id:password@zone-name?country=us&session=X

    Maps to:
      server   = brd.superproxy.io:33335 (residential gateway)
      username = brd-customer-<id>-zone-<zone>[-country-<cc>][-session-<id>]
      password = <password>
    """
    customer = parsed.username
    password = parsed.password
    zone = parsed.hostname
    if not (customer and password and zone):
        raise ProxyParseError(
            "brightdata URL must be brightdata://customer-id:password@zone-name"
        )
    params = dict(parse_qsl(parsed.query))
    parts = [f"brd-customer-{customer}", f"zone-{zone}"]
    if "country" in params:
        parts.append(f"country-{params['country'].lower()}")
    if "session-id" in params:
        parts.append(f"session-{params['session-id']}")
    # rotation: omit session-* params → rotates per request
    return {
        "server": "http://brd.superproxy.io:33335",
        "username": "-".join(parts),
        "password": password,
    }


def _iproyal(url: str, parsed) -> dict:
    """IPRoyal: iproyal://user:pass@geo.iproyal.com:12321?country=us&sticky=7d

    Maps to:
      server   = geo.iproyal.com:12321
      username = <user>[_country-<cc>][_lifetime-<sticky>]
      password = <password>
    """
    user = parsed.username
    password = parsed.password
    if not (user and password):
        raise ProxyParseError(
            "iproyal URL must be iproyal://user:pass@host:port"
        )
    host = parsed.hostname or "geo.iproyal.com"
    port = parsed.port or 12321
    params = dict(parse_qsl(parsed.query))
    suffixes = []
    if "country" in params:
        suffixes.append(f"country-{params['country'].lower()}")
    if "sticky" in params:
        # IPRoyal expects lifetime like "7d", "300s"
        suffixes.append(f"lifetime-{params['sticky']}")
    elif "session" in params:
        suffixes.append(f"session-{params['session']}")
    return {
        "server": f"http://{host}:{port}",
        "username": user if not suffixes else user + "_" + "_".join(suffixes),
        "password": password,
    }


def _decodo(url: str, parsed) -> dict:
    """Decodo (Smartproxy): decodo://user:pass@gate.decodo.com:7000?country=us

    Maps to:
      server   = gate.decodo.com:7000
      username = user-<base-user>[-country-<cc>][-session-<id>][-sessionduration-<sec>]
      password = <password>
    """
    user = parsed.username
    password = parsed.password
    if not (user and password):
        raise ProxyParseError(
            "decodo URL must be decodo://user:pass@host:port"
        )
    host = parsed.hostname or "gate.decodo.com"
    port = parsed.port or 7000
    params = dict(parse_qsl(parsed.query))
    parts = [f"user-{user}"]
    if "country" in params:
        parts.append(f"country-{params['country'].lower()}")
    if "session" in params:
        parts.append(f"session-{params['session']}")
    if "duration" in params:
        parts.append(f"sessionduration-{params['duration']}")
    return {
        "server": f"http://{host}:{port}",
        "username": "-".join(parts),
        "password": password,
    }


_ADAPTERS: dict[str, Callable] = {
    "http": _generic, "https": _generic, "socks5": _generic, "socks": _generic,
    "brightdata": _brightdata,
    "iproyal": _iproyal,
    "decodo": _decodo,
}


def list_providers() -> list[str]:
    return sorted({k for k in _ADAPTERS if k not in ("http", "https", "socks")} | {"http", "socks5"})


def parse(url: str) -> dict:
    """Turn a `provider://user:pass@host?params` URL into a Playwright proxy
    config dict suitable for `launch_persistent_context(proxy=...)`.

    Raises ProxyParseError on invalid URL or unknown provider.
    """
    if not url or "://" not in url:
        raise ProxyParseError(f"invalid proxy URL: {url!r}")
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    adapter = _ADAPTERS.get(scheme)
    if adapter is None:
        raise ProxyParseError(
            f"unknown proxy provider {scheme!r}. "
            f"Built-ins: {sorted(_ADAPTERS.keys())}"
        )
    cfg = adapter(url, parsed)
    return cfg


def load_proxy_file(path: str | Path) -> str:
    """Read a proxy URL from a 0600 file. Used to keep creds out of `ps` /
    shell history."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"proxy file not found: {p}")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(
            f"proxy file {p} has mode {oct(mode)} — must be 0600 "
            f"(chmod 600 {p})"
        )
    return p.read_text().strip()


# ─── per-session storage ───────────────────────────────────────────────


def session_proxy_path(profile_dir: Path) -> Path:
    return profile_dir / "proxy.json"


def save_session_proxy(profile_dir: Path, url: str | None) -> None:
    """Persist proxy URL on the session's profile dir. Takes effect at next start.
    Passing url=None removes the file."""
    p = session_proxy_path(profile_dir)
    if url is None:
        if p.exists():
            p.unlink()
        return
    # Validate before saving so a bad URL doesn't get persisted
    parse(url)
    p.write_text(json.dumps({"url": url}))
    # 0600 — contains proxy credentials
    os.chmod(p, 0o600)


def load_session_proxy(profile_dir: Path) -> str | None:
    p = session_proxy_path(profile_dir)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text()).get("url")
    except Exception:  # noqa: BLE001
        return None


# ─── stealth flags (WebRTC leak guard) ────────────────────────────────


def webrtc_leak_guard_args() -> list[str]:
    """Chrome flags that suppress real-IP leakage through WebRTC STUN.

    Apply alongside proxy config — without these, a malicious page can
    discover the real IP via STUN handshake even though all HTTP requests
    are tunneled through the proxy.
    """
    return [
        "--disable-features=WebRtcHideLocalIpsWithMdns",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]
