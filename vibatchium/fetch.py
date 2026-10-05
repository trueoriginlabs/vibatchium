"""Pure helpers for the ``fetch`` verb — a curl_cffi-backed authenticated HTTP
lane that reuses a live browser session's identity (cookies + proxy + UA + a
JA3/HTTP2 impersonation target derived from the live Chrome) WITHOUT launching a
renderer or running JavaScript.

Why it exists (0.9.0, competitive-landscape lesson): anti-bot gates score the
TLS ClientHello (JA3) and HTTP/2 frame fingerprint *before any JS runs*. A plain
``requests``/``httpx`` call clears the JS checks but is killed at that layer
because its ClientHello is obviously not Chrome. ``curl_cffi`` (built on
curl-impersonate) reproduces Chrome's ClientHello byte-for-byte, so hitting a
JSON/API/static endpoint behind a login you already established in the browser
is fast, cheap, and fingerprint-correct.

HARD BOUNDARY: no JavaScript. This only defeats the *static* TLS+HTTP2
fingerprint gate — a DataDome/Kasada/Turnstile challenge that needs to *run* an
interrogation script will fail. It is a fast path, not a browser replacement.

This module is import-safe with ZERO optional deps (no ``curl_cffi`` import at
module top) so the helpers are unit-testable without the extra installed; the
handler imports ``curl_cffi`` lazily behind an ImportError guard.
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import re
import socket
from urllib.parse import quote, urlsplit, urlunsplit

log = logging.getLogger("vibatchium.fetch")

# curl_cffi-supported Chrome impersonation targets, ascending. Picked to match
# the live Chrome major as closely as possible; if the live Chrome is newer than
# any known token we fall back to the ``"chrome"`` alias (curl_cffi keeps it
# pointed at its newest Chrome target), so coherence degrades gracefully rather
# than pinning a stale fingerprint.
#
# Kept in step with the desktop-Chrome members of curl_cffi's ``BrowserType``
# enum for the floor in pyproject (0.16.3: chrome99 … chrome150). There is no
# free chrome152+ preset yet — see ``tls_coherence``.
_CHROME_TARGETS: list[tuple[int, str]] = [
    (99, "chrome99"), (100, "chrome100"), (101, "chrome101"), (104, "chrome104"),
    (107, "chrome107"), (110, "chrome110"), (116, "chrome116"), (119, "chrome119"),
    (120, "chrome120"), (123, "chrome123"), (124, "chrome124"), (131, "chrome131"),
    (133, "chrome133a"), (136, "chrome136"), (142, "chrome142"), (145, "chrome145"),
    (146, "chrome146"), (150, "chrome150"),
]
_LATEST_ALIAS = "chrome"

_CHROME_MAJOR_RE = re.compile(r"Chrome/(\d+)")
_TOKEN_MAJOR_RE = re.compile(r"^chrome(\d+)[a-z]?$")

# Chrome 152 added the TLS ``trust_anchors`` ClientHello extension. curl_cffi
# 0.16.3 can *send* it, but only via impersonate.pro or hand-set curlopts — no
# free preset carries it. So a 152+ browser fetched through any free preset
# presents a ClientHello missing an extension the real browser sends.
TRUST_ANCHORS_MAJOR = 152
# How far the live Chrome may run ahead of the impersonated preset before we
# say so. One major is routine release lag; TLS rarely moves per release.
_COHERENCE_GAP = 1
_COHERENCE_LOGGED: set[tuple[int, str]] = set()


def chrome_major(ua: str | None) -> int | None:
    """The Chrome major version in a UA string, or None if it isn't Chrome."""
    if not ua:
        return None
    m = _CHROME_MAJOR_RE.search(ua)
    return int(m.group(1)) if m else None


def pick_impersonate(ua: str | None, override: str | None = None) -> str:
    """Choose the curl_cffi ``impersonate`` token for a live Chrome UA.

    An explicit ``override`` always wins. Otherwise parse the Chrome major out
    of ``ua`` and map it to the nearest supported token at or below it; if the
    UA is newer than any known target (or unparseable) return the ``"chrome"``
    latest-alias so the JA3 tracks the freshest available Chrome.
    """
    if override:
        return override
    major = chrome_major(ua)
    if major is None:
        return _LATEST_ALIAS
    if major >= _CHROME_TARGETS[-1][0]:
        return _LATEST_ALIAS
    best = _CHROME_TARGETS[0][1]
    for tmajor, token in _CHROME_TARGETS:
        if tmajor <= major:
            best = token
        else:
            break
    return best


def _token_major(token: str | None) -> int | None:
    m = _TOKEN_MAJOR_RE.match(token or "")
    return int(m.group(1)) if m else None


def tls_coherence(ua: str | None, impersonate: str,
                  latest: str | None = None) -> dict | None:
    """Describe a TLS-fingerprint gap between the live Chrome and the preset.

    Returns ``{ua_major, impersonate, gap, note}`` when the browser whose
    cookies/UA we're presenting is newer than the ClientHello we can forge —
    by more than ``_COHERENCE_GAP`` majors, or across the Chrome 152
    ``trust_anchors`` boundary — else None. ``latest`` is what the installed
    curl_cffi resolves the ``"chrome"`` alias to (``DEFAULT_CHROME``); without
    it the alias is assumed to mean the newest entry in ``_CHROME_TARGETS``.

    Non-Chrome UAs and non-Chrome presets return None: there is no version
    relationship to compare. Logs once per (ua_major, preset) per process.
    """
    ua_major = chrome_major(ua)
    if ua_major is None:
        return None
    resolved = impersonate
    if impersonate == _LATEST_ALIAS:
        resolved = latest or _CHROME_TARGETS[-1][1]
    preset_major = _token_major(resolved)
    if preset_major is None:
        return None
    gap = ua_major - preset_major
    missing_trust_anchors = preset_major < TRUST_ANCHORS_MAJOR <= ua_major
    if gap <= _COHERENCE_GAP and not missing_trust_anchors:
        return None
    parts = [f"session UA is Chrome {ua_major} but the newest TLS preset "
             f"available is {resolved} ({gap} major{'s' if gap != 1 else ''} behind)"]
    if missing_trust_anchors:
        parts.append(f"Chrome {TRUST_ANCHORS_MAJOR}+ sends a TLS trust_anchors "
                     "extension no free curl_cffi preset reproduces, so the "
                     "ClientHello will not match the browser's")
    parts.append("a JA3/JA4-scoring wall can see the mismatch; use the browser "
                 "(`go`) for those targets")
    note = "; ".join(parts)
    key = (ua_major, resolved)
    if key not in _COHERENCE_LOGGED:
        _COHERENCE_LOGGED.add(key)
        log.warning("fetch TLS coherence: %s", note)
    return {"ua_major": ua_major, "impersonate": resolved, "gap": gap, "note": note}


def _sf_string(s: str) -> str:
    """Serialize ``s`` as an RFC 8941 structured-field string."""
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def client_hint_headers(uad: dict | None) -> dict:
    """Build the low-entropy ``Sec-CH-UA*`` request headers from a page's
    ``navigator.userAgentData`` (``{brands, mobile, platform}``).

    curl_cffi's preset stamps its OWN client hints — chrome150 sends
    ``sec-ch-ua: …v="150"`` and ``sec-ch-ua-platform: "macOS"`` — even when the
    User-Agent is overridden to the session's Linux Chrome 153. The browser on
    the same cookies sends v="153" / "Linux", so a server comparing the two sees
    the platform flip between requests. Taking the values from the live page
    makes the fetch lane say what the browser says. Brand order is preserved:
    Chrome serializes the header from the same list ``brands`` exposes.

    Returns {} when ``uad`` is missing or malformed (``userAgentData`` only
    exists in secure contexts) — the caller then keeps the preset's hints.
    """
    if not isinstance(uad, dict):
        return {}
    brands = uad.get("brands")
    if not isinstance(brands, list) or not brands:
        return {}
    items = []
    for b in brands:
        if not isinstance(b, dict) or not b.get("brand") or b.get("version") is None:
            return {}
        items.append(f"{_sf_string(b['brand'])};v={_sf_string(b['version'])}")
    out = {"sec-ch-ua": ", ".join(items),
           "sec-ch-ua-mobile": "?1" if uad.get("mobile") else "?0"}
    if uad.get("platform"):
        out["sec-ch-ua-platform"] = _sf_string(uad["platform"])
    return out


def proxy_cfg_to_curl(cfg: dict | None) -> dict | None:
    """Translate a Playwright proxy dict (``{server, username?, password?}``,
    as returned by ``vibatchium.proxy.parse``) into a curl_cffi ``proxies``
    dict. Returns None when there's no proxy.

    The assembled URL embeds ``user:pass`` userinfo — callers must NEVER log it.
    """
    if not cfg:
        return None
    server = cfg.get("server")
    if not server:
        return None
    parts = urlsplit(server if "://" in server else "http://" + server)
    user = cfg.get("username")
    if user:
        pw = cfg.get("password") or ""
        netloc = f"{quote(user, safe='')}:{quote(pw, safe='')}@{parts.hostname or ''}"
        if parts.port:
            netloc += f":{parts.port}"
        url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    else:
        url = server if "://" in server else "http://" + server
    return {"http": url, "https": url}


def cookies_for_url(cookies: list[dict], url: str) -> dict:
    """Filter a Playwright ``context.cookies()`` list down to the cookies that
    apply to ``url`` and collapse to a ``{name: value}`` dict for curl_cffi.

    Domain matching is eTLD-conservative: a cookie domain with no dot (e.g. a
    bare TLD) only matches that exact host — never a suffix — so a malformed
    ``.com`` cookie can't leak across an entire TLD. Honors path-prefix and the
    Secure attribute (a Secure cookie is dropped for an ``http://`` URL).
    """
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    scheme = (u.scheme or "").lower()
    path = u.path or "/"
    out: dict[str, str] = {}
    for c in cookies:
        dom = (c.get("domain") or "").lower().lstrip(".")
        if not dom:
            continue
        if "." in dom:
            if not (host == dom or host.endswith("." + dom)):
                continue
        elif host != dom:                       # bare label → exact host only
            continue
        cpath = c.get("path") or "/"
        if not (path == cpath or path.startswith(cpath.rstrip("/") + "/") or cpath == "/"):
            continue
        if c.get("secure") and scheme != "https":
            continue
        name = c.get("name")
        if name:
            out[name] = c.get("value", "")
    return out


def _ip_is_internal(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_loopback or ip.is_link_local or ip.is_private
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def host_is_internal(host: str) -> bool:
    """True if ``host`` is (or resolves to) a loopback / link-local / private /
    reserved address — the SSRF-sensitive ranges, including the cloud metadata
    endpoint 169.254.169.254. An unresolvable host returns False (let the
    request fail naturally rather than masking a DNS error as an SSRF block).
    """
    if not host:
        return False
    try:
        return _ip_is_internal(ipaddress.ip_address(host))   # literal IP
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        addr = info[4][0].split("%")[0]                      # strip scope id
        try:
            if _ip_is_internal(ipaddress.ip_address(addr)):
                return True
        except ValueError:
            continue
    return False


def truncate_body(raw: bytes | None, max_body: int) -> tuple[str, bool, bool]:
    """Return ``(value, truncated, is_text)`` for a response body.

    UTF-8-decodable bytes come back as text; binary comes back base64-encoded
    (the same convention as ``wait_response``). ``max_body`` caps the bytes
    BEFORE decoding so a huge response can't blow up the agent's context.
    """
    if raw is None:
        raw = b""
    truncated = False
    if max_body and len(raw) > max_body:
        raw = raw[:max_body]
        truncated = True
    try:
        return raw.decode("utf-8"), truncated, True
    except UnicodeDecodeError:
        # A size cut can land mid multi-byte char; don't mislabel otherwise-text
        # as binary — back off up to 3 bytes to the last valid UTF-8 boundary.
        if truncated:
            for back in range(1, 4):
                try:
                    return raw[:-back].decode("utf-8"), True, True
                except UnicodeDecodeError:
                    continue
        return base64.b64encode(raw).decode(), truncated, False
