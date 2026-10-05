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


def test_pick_impersonate_uses_chrome150_between_146_and_150():
    # 0.16.x added chrome150; a 148 browser must not be rounded down to 146.
    assert fetch.pick_impersonate("X Chrome/148.0.0.0 Y") == "chrome146"
    assert fetch.pick_impersonate("X Chrome/149.0.0.0 Y") == "chrome146"
    assert fetch.pick_impersonate("X Chrome/150.0.0.0 Y") == "chrome"
    assert ("chrome150" in dict(fetch._CHROME_TARGETS).values())


def test_chrome_targets_match_installed_curl_cffi():
    """Every token in the table must be a real preset in the installed build,
    and every desktop-Chrome preset the build ships should be in the table —
    otherwise a curl_cffi bump silently leaves the newest preset unused."""
    import pytest
    imp = pytest.importorskip("curl_cffi.requests.impersonate")
    shipped = {b.value for b in imp.BrowserType
               if b.value.startswith("chrome") and "android" not in b.value}
    table = {tok for _, tok in fetch._CHROME_TARGETS}
    assert table <= shipped, f"unknown tokens: {table - shipped}"
    assert shipped <= table, f"presets missing from _CHROME_TARGETS: {shipped - table}"
    # the alias we fall back to must resolve to the newest entry in the table
    assert imp.DEFAULT_CHROME == fetch._CHROME_TARGETS[-1][1]


# ─── tls_coherence ────────────────────────────────────────────────────────
_UA153 = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36")


def test_tls_coherence_flags_ua_ahead_of_newest_preset():
    c = fetch.tls_coherence(_UA153, "chrome", latest="chrome150")
    assert c is not None
    assert c["ua_major"] == 153
    assert c["impersonate"] == "chrome150"      # the alias, resolved
    assert c["gap"] == 3
    assert "trust_anchors" in c["note"]


def test_tls_coherence_alias_defaults_to_table_head_without_latest():
    c = fetch.tls_coherence(_UA153, "chrome")
    assert c["impersonate"] == fetch._CHROME_TARGETS[-1][1]


def test_tls_coherence_silent_within_one_major():
    assert fetch.tls_coherence("X Chrome/151.0 Y", "chrome", latest="chrome150") is None
    assert fetch.tls_coherence("X Chrome/150.0 Y", "chrome", latest="chrome150") is None
    assert fetch.tls_coherence("X Chrome/136.0 Y", "chrome136") is None


def test_tls_coherence_trust_anchors_boundary_flags_even_a_one_major_gap():
    # A hypothetical chrome151 preset vs a 152 browser: gap 1, but 152 added
    # an extension the preset lacks.
    c = fetch.tls_coherence("X Chrome/152.0 Y", "chrome151")
    assert c is not None and c["gap"] == 1 and "trust_anchors" in c["note"]
    # ...and a preset that already carries it is fine.
    assert fetch.tls_coherence("X Chrome/153.0 Y", "chrome152") is None


def test_tls_coherence_gap_without_trust_anchors():
    c = fetch.tls_coherence("X Chrome/149.0 Y", "chrome146")
    assert c is not None and c["gap"] == 3 and "trust_anchors" not in c["note"]


def test_tls_coherence_ignores_non_chrome():
    assert fetch.tls_coherence("Mozilla/5.0 Firefox/140.0", "chrome") is None
    assert fetch.tls_coherence(None, "chrome") is None
    assert fetch.tls_coherence(_UA153, "safari260") is None


def test_tls_coherence_logs_once(caplog):
    import logging
    fetch._COHERENCE_LOGGED.discard((160, "chrome150"))
    ua = "X Chrome/160.0 Y"
    with caplog.at_level(logging.WARNING, logger="vibatchium.fetch"):
        fetch.tls_coherence(ua, "chrome", latest="chrome150")
        fetch.tls_coherence(ua, "chrome", latest="chrome150")
    assert sum("TLS coherence" in r.getMessage() for r in caplog.records) == 1


# ─── client_hint_headers ──────────────────────────────────────────────────
def test_client_hint_headers_from_user_agent_data():
    uad = {"brands": [{"brand": "Google Chrome", "version": "153"},
                      {"brand": "Not_A Brand", "version": "8"},
                      {"brand": "Chromium", "version": "153"}],
           "mobile": False, "platform": "Linux"}
    h = fetch.client_hint_headers(uad)
    # byte-for-byte what Chrome 153 itself sent for this userAgentData
    assert h == {
        "sec-ch-ua": '"Google Chrome";v="153", "Not_A Brand";v="8", "Chromium";v="153"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }


def test_client_hint_headers_escapes_and_mobile():
    h = fetch.client_hint_headers({"brands": [{"brand": 'A"B\\C', "version": "1"}],
                                   "mobile": True, "platform": ""})
    assert h["sec-ch-ua"] == '"A\\"B\\\\C";v="1"'
    assert h["sec-ch-ua-mobile"] == "?1"
    assert "sec-ch-ua-platform" not in h


def test_client_hint_headers_missing_or_malformed_is_empty():
    assert fetch.client_hint_headers(None) == {}
    assert fetch.client_hint_headers({}) == {}
    assert fetch.client_hint_headers({"brands": []}) == {}
    assert fetch.client_hint_headers({"brands": [{"brand": "X"}]}) == {}
    assert fetch.client_hint_headers({"brands": "nope"}) == {}


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


# ─── LIVE: the fetch lane presents the browser's own client hints ──────────
def test_fetch_verb_sends_session_client_hints(local_server):
    """curl_cffi's preset stamps its own Sec-CH-UA (its Chrome major, macOS).
    With a live session the fetch lane must send what the browser sends, so the
    platform and version don't flip between browser and fetch requests on the
    same cookies. Also checks `tls_coherence` agrees with the pure helper."""
    import http.server
    import socketserver
    import threading

    import pytest
    pytest.importorskip("curl_cffi")
    from vibatchium.client import call

    seen: dict = {}

    class Echo(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.update({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        # 127.0.0.1 is a secure context, so userAgentData exists on this page.
        call("go", {"url": f"{local_server}/article.html", "wait_until": "load"})
        ident = call("eval", {"expr": (
            "({ua: navigator.userAgent, "
            "brands: navigator.userAgentData.brands.map(b => ({brand: b.brand, version: b.version})), "
            "platform: navigator.userAgentData.platform})")})["value"]
        port = srv.server_address[1]
        r = call("fetch", {"url": f"http://127.0.0.1:{port}/", "allow_internal": True})
    finally:
        srv.shutdown()
        srv.server_close()

    assert r["status"] == 200
    assert seen["user-agent"] == ident["ua"]
    expected = fetch.client_hint_headers(
        {"brands": ident["brands"], "mobile": False, "platform": ident["platform"]})
    assert seen["sec-ch-ua"] == expected["sec-ch-ua"]
    assert seen["sec-ch-ua-platform"] == expected["sec-ch-ua-platform"]

    want = fetch.tls_coherence(ident["ua"], r["impersonate"])
    if want is None:
        assert "tls_coherence" not in r
    else:
        tc = r["tls_coherence"]
        assert tc["ua_major"] == want["ua_major"]
        assert tc["gap"] == want["gap"]
        assert tc["client_hints"] == "session"
