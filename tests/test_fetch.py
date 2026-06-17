"""0.9.0 — pure unit tests for the fetch-lane helpers (vibatchium/fetch.py).

No daemon, no curl_cffi, no network — these exercise the identity-translation
logic (impersonate target, proxy URL assembly, eTLD-safe cookie filtering, body
truncation) that the `fetch` handler composes.
"""
from __future__ import annotations

from vibatchium import fetch


# ─── pick_impersonate ────────────────────────────────────────────────────
def test_pick_impersonate_matches_nearest_target_at_or_below():
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36"
    assert fetch.pick_impersonate(ua) == "chrome136"   # nearest token <= 138


def test_pick_impersonate_newer_than_known_falls_back_to_latest_alias():
    assert fetch.pick_impersonate("Mozilla/5.0 Chrome/200.0.0.0") == "chrome"


def test_pick_impersonate_unparseable_ua_uses_latest_alias():
    assert fetch.pick_impersonate("not a browser") == "chrome"
    assert fetch.pick_impersonate(None) == "chrome"


def test_pick_impersonate_override_wins():
    assert fetch.pick_impersonate("Mozilla/5.0 Chrome/138", override="chrome131") == "chrome131"


def test_pick_impersonate_exact_known_major():
    assert fetch.pick_impersonate("X Chrome/131.0.0.0 Y") == "chrome131"


# ─── proxy_cfg_to_curl ────────────────────────────────────────────────────
def test_proxy_cfg_with_auth_embeds_userinfo():
    out = fetch.proxy_cfg_to_curl({"server": "http://h:8080", "username": "u", "password": "p"})
    assert out == {"http": "http://u:p@h:8080", "https": "http://u:p@h:8080"}


def test_proxy_cfg_without_auth_has_no_userinfo():
    out = fetch.proxy_cfg_to_curl({"server": "http://h:8080"})
    assert out == {"http": "http://h:8080", "https": "http://h:8080"}
    assert "@" not in out["http"]


def test_proxy_cfg_url_encodes_credentials():
    out = fetch.proxy_cfg_to_curl({"server": "http://h:1", "username": "u@x", "password": "p:w/d"})
    # special chars are percent-encoded so the URL stays well-formed
    assert "u%40x" in out["http"] and "p%3Aw%2Fd" in out["http"]


def test_proxy_cfg_none_returns_none():
    assert fetch.proxy_cfg_to_curl(None) is None
    assert fetch.proxy_cfg_to_curl({}) is None


# ─── cookies_for_url ──────────────────────────────────────────────────────
_COOKIES = [
    {"name": "sess", "value": "v1", "domain": ".ex.com", "path": "/", "secure": True},
    {"name": "scoped", "value": "v2", "domain": "ex.com", "path": "/app", "secure": False},
    {"name": "evil", "value": "x", "domain": "evil.com", "path": "/"},
    {"name": "tld", "value": "x", "domain": "com", "path": "/"},
]


def test_cookies_domain_suffix_match_https():
    out = fetch.cookies_for_url(_COOKIES, "https://www.ex.com/app/x")
    assert out == {"sess": "v1", "scoped": "v2"}


def test_cookies_secure_dropped_for_http():
    out = fetch.cookies_for_url(_COOKIES, "http://www.ex.com/app/x")
    assert "sess" not in out          # Secure cookie excluded over http
    assert out.get("scoped") == "v2"


def test_cookies_path_prefix_filters():
    out = fetch.cookies_for_url(_COOKIES, "https://ex.com/")
    assert "scoped" not in out        # /app cookie not sent to /
    assert out.get("sess") == "v1"


def test_cookies_bare_tld_does_not_leak_across_tld():
    # a malformed bare-label "com" cookie must NOT match arbitrary .com hosts
    assert "tld" not in fetch.cookies_for_url(_COOKIES, "https://anything.com/")
    assert "evil" not in fetch.cookies_for_url(_COOKIES, "https://www.ex.com/")


# ─── truncate_body ────────────────────────────────────────────────────────
def test_truncate_body_text():
    assert fetch.truncate_body(b"hello", 100) == ("hello", False, True)


def test_truncate_body_binary_is_base64():
    val, truncated, is_text = fetch.truncate_body(b"\xff\xfe\x00bin", 100)
    assert is_text is False and truncated is False
    import base64
    assert base64.b64decode(val) == b"\xff\xfe\x00bin"


def test_truncate_body_caps_before_decode():
    val, truncated, is_text = fetch.truncate_body(b"abcdef", 3)
    assert val == "abc" and truncated is True and is_text is True


def test_truncate_body_none():
    assert fetch.truncate_body(None, 100) == ("", False, True)


def test_truncate_body_cut_mid_multibyte_stays_text():
    # "☕" is 3 UTF-8 bytes; cutting at 1 byte must NOT flip the body to base64
    raw = "café ☕".encode()
    cut = len("café ".encode()) + 1   # lands inside the ☕ sequence
    val, truncated, is_text = fetch.truncate_body(raw, cut)
    assert is_text is True and truncated is True
    assert val == "café "                     # partial char dropped, not base64'd


# ─── host_is_internal (SSRF guard) ────────────────────────────────────────
def test_host_is_internal_blocks_loopback_linklocal_private():
    assert fetch.host_is_internal("127.0.0.1") is True
    assert fetch.host_is_internal("169.254.169.254") is True   # cloud metadata
    assert fetch.host_is_internal("10.0.0.5") is True
    assert fetch.host_is_internal("192.168.1.1") is True
    assert fetch.host_is_internal("::1") is True


def test_host_is_internal_allows_public_ip():
    assert fetch.host_is_internal("93.184.216.34") is False    # example.com (literal)
    assert fetch.host_is_internal("") is False


# ─── LIVE: the SSRF guard (fires before the curl_cffi import) ──────────────
def test_fetch_verb_refuses_internal_target(local_server):
    """The SSRF guard rejects a metadata/loopback target — and does so BEFORE
    requiring curl_cffi, so this runs on a base install too."""
    from vibatchium.client import call, DaemonError
    import pytest
    call("go", {"url": f"{local_server}/article.html", "wait_until": "load"})
    with pytest.raises(DaemonError) as ei:
        call("fetch", {"url": "http://169.254.169.254/latest/meta-data/"})
    assert "ssrf" in str(ei.value).lower() or "internal" in str(ei.value).lower()


# ─── LIVE: the fetch verb end-to-end (needs curl_cffi) ─────────────────────
def test_fetch_verb_end_to_end(local_server):
    """With curl_cffi installed, `fetch` hits a URL reusing the session identity
    and returns a shaped response. Skips on a base install (no curl_cffi)."""
    import pytest
    pytest.importorskip("curl_cffi")
    from vibatchium.client import call
    # seat a live session/context
    call("go", {"url": f"{local_server}/article.html", "wait_until": "load"})
    # local_server is 127.0.0.1 — legitimately internal, so opt past the SSRF guard
    r = call("fetch", {"url": f"{local_server}/article.html", "allow_internal": True})
    assert r["status"] == 200
    assert r["ok"] is True
    assert r["via"] == "curl_cffi"
    assert r["impersonate"].startswith("chrome")
    assert "one-way" in r["cookie_sync"]          # unidirectional caveat surfaced
    assert "The Main Title" in r.get("body", "")  # body returned as text
