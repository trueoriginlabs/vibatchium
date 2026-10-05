"""0.19.4: `fill --use-secret` is origin-bound.

Before this, `fill @e3 --use-secret github.com:password` resolved the vault
value and wrote it into whatever element was targeted on whatever page was open
— so a prompt-injected agent could type a stored credential into evil.com and
read it back. Now the value is only written into a document whose origin
belongs to the secret's site (judged by the frame that OWNS the field), the
check runs before the secret is resolved, and a field swapped / focus
redirected mid-fill is cleared and refused.

Three layers here:
  * pure unit tests of the matching rules (no daemon);
  * the agent-surface guard (MCP refuses the operator-only knobs);
  * integration tests against the per-session test daemon + local server.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from vibatchium import secrets as S
from vibatchium.client import call


# ─── host matching ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("host,site", [
    ("github.com", "github.com"),
    ("gist.github.com", "github.com"),
    ("a.b.github.com", "github.com"),
    ("GitHub.COM", "github.com"),          # case-insensitive
    ("github.com", "GITHUB.com"),
    ("github.com.", "github.com"),         # trailing dot (FQDN form)
    ("github.com", "github.com."),
    ("login.example.com", "www.example.com"),   # leading www. dropped from the site
    ("example.com", "www.example.com"),
    ("www.example.com", "www.example.com"),
    ("xn--bcher-kva.de", "bücher.de"),     # IDN site vs punycode host (what Chrome reports)
    ("xn--bcher-kva.de", "Bücher.DE"),
    ("shop.xn--bcher-kva.de", "bücher.de"),
    ("127.0.0.1", "127.0.0.1"),
    ("localhost", "localhost"),
    ("::1", "::1"),
    ("[::1]", "::1"),
])
def test_host_matches_site_accepts(host, site):
    assert S.host_matches_site(host, site)


@pytest.mark.parametrize("host,site", [
    ("github.com.evil.com", "github.com"),     # suffix of the wrong label
    ("evilgithub.com", "github.com"),          # no label boundary
    ("github.co", "github.com"),
    ("github.com", "gist.github.com"),         # a subdomain site doesn't grant its parent
    ("api.github.com", "gist.github.com"),     # nor its siblings
    ("example.com.evil.com", "www.example.com"),
    ("www.example.com.evil.com", "www.example.com"),
    ("xn--gthub-n4a.com", "github.com"),       # homograph (punycode lookalike)
    ("bücher.de", "bucher.de"),
    ("foo.com", "com"),                        # single-label site: exact only
    ("foo.localhost", "localhost"),
    ("1.127.0.0.1", "127.0.0.1"),              # IPs: exact only
    ("127.0.0.2", "127.0.0.1"),
    ("", "github.com"),
    (None, "github.com"),
    ("github.com", ""),
    ("evil.com/github.com", "github.com"),     # junk that isn't a host
    ("github.com@evil.com", "github.com"),
])
def test_host_matches_site_rejects(host, site):
    assert not S.host_matches_site(host, site)


def test_www_strip_never_widens_to_a_bare_suffix():
    """`www.com` must not be stripped to `com` (which would allow all of .com)."""
    assert S.host_matches_site("www.com", "www.com")
    assert not S.host_matches_site("evil.com", "www.com")
    assert not S.host_matches_site("a.evil.com", "www.com")


# ─── url → origin ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/login", ("https", "github.com", 443)),
    ("https://github.com:443/x", ("https", "github.com", 443)),
    ("https://github.com:8443/x", ("https", "github.com", 8443)),
    ("http://127.0.0.1:5000/", ("http", "127.0.0.1", 5000)),
    ("HTTPS://GitHub.com/", ("https", "github.com", 443)),
    ("https://github.com@evil.com/", ("https", "evil.com", 443)),   # userinfo trick
    ("blob:https://github.com/1234-uuid", ("https", "github.com", 443)),
    ("https://[::1]:8080/", ("https", "::1", 8080)),
])
def test_url_origin(url, expected):
    assert S.url_origin(url) == expected


@pytest.mark.parametrize("url", [
    "about:blank", "about:srcdoc", "data:text/html,<input>", "file:///etc/passwd",
    "javascript:alert(1)", "", None, "chrome://settings", "https://", "https://:443/",
])
def test_url_origin_opaque_or_invalid(url):
    assert S.url_origin(url) is None


def test_format_origin_drops_default_port():
    assert S.format_origin(("https", "github.com", 443)) == "https://github.com"
    assert S.format_origin(("http", "127.0.0.1", 8080)) == "http://127.0.0.1:8080"
    assert S.format_origin(("https", "::1", 8443)) == "https://[::1]:8443"


# ─── the full policy (scheme + host + origins) ──────────────────────────────

@pytest.mark.parametrize("url,site", [
    ("https://github.com/login", "github.com"),
    ("https://gist.github.com/", "github.com"),
    ("https://github.com:8443/", "github.com"),      # port ignored by the site rule
    ("http://127.0.0.1:5000/", "127.0.0.1"),         # loopback may be plain http
    ("http://localhost:3000/", "localhost"),
    ("http://[::1]:3000/", "::1"),
    ("https://localhost/", "localhost"),
])
def test_check_secret_origin_allows(url, site):
    assert S.check_secret_origin(url, site)


@pytest.mark.parametrize("url,site,why", [
    ("http://github.com/login", "github.com", "plain http"),           # downgrade
    ("https://github.com.evil.com/", "github.com", "not github.com"),
    ("https://evilgithub.com/", "github.com", "not github.com"),
    ("https://evil.com/", "github.com", "not github.com"),
    ("about:blank", "github.com", r"no http\(s\) origin"),
    ("data:text/html,x", "github.com", r"no http\(s\) origin"),
    ("http://127.0.0.1:5000/", "github.com", "not github.com"),
    ("http://192.168.1.10/", "192.168.1.10", "plain http"),            # LAN is not loopback
])
def test_check_secret_origin_refuses(url, site, why):
    with pytest.raises(S.SecretOriginError, match=why):
        S.check_secret_origin(url, site)


def test_origins_replace_the_default_rule():
    origins = "https://github.com, https://gist.github.com"
    assert S.check_secret_origin("https://gist.github.com/x", "github.com", origins)
    assert S.check_secret_origin("https://github.com/", "github.com", origins)
    # Exact origins: a subdomain the default rule would allow is now refused.
    with pytest.raises(S.SecretOriginError, match="not in the entry's origins"):
        S.check_secret_origin("https://api.github.com/", "github.com", origins)
    # ...and so is a non-default port.
    with pytest.raises(S.SecretOriginError):
        S.check_secret_origin("https://github.com:8443/", "github.com", origins)


def test_origins_let_a_label_site_point_at_a_real_host():
    pol = ["https://login.microsoftonline.com"]
    assert S.check_secret_origin("https://login.microsoftonline.com/x", "work-sso", pol)
    with pytest.raises(S.SecretOriginError):
        S.check_secret_origin("https://microsoftonline.com/", "work-sso", pol)


def test_origins_wildcard_and_bare_entries():
    assert S.check_secret_origin("https://a.example.org/", "x", "https://*.example.org")
    assert S.check_secret_origin("https://a.b.example.org/", "x", "https://*.example.org")
    with pytest.raises(S.SecretOriginError):   # wildcard excludes the apex
        S.check_secret_origin("https://example.org/", "x", "https://*.example.org")
    with pytest.raises(S.SecretOriginError):
        S.check_secret_origin("https://badexample.org/", "x", "https://*.example.org")
    # Bare host entry = the default host-or-subdomain rule.
    assert S.check_secret_origin("https://example.org/", "x", "example.org")
    assert S.check_secret_origin("https://sso.example.org/", "x", "example.org")
    with pytest.raises(S.SecretOriginError):
        S.check_secret_origin("http://example.org/", "x", "example.org")


def test_origins_loopback_with_port_is_exact():
    pol = "http://127.0.0.1:5000"
    assert S.check_secret_origin("http://127.0.0.1:5000/p", "dev", pol)
    with pytest.raises(S.SecretOriginError):
        S.check_secret_origin("http://127.0.0.1:5001/p", "dev", pol)
    with pytest.raises(S.SecretOriginError):
        S.check_secret_origin("https://127.0.0.1:5000/p", "dev", pol)   # scheme is part of origin


@pytest.mark.parametrize("bad", [
    "http://github.com",          # plain http, not loopback
    "ftp://github.com",
    "https://github.com/login",   # a path is not an origin
    "https://",
    "bad/host",                   # not a host
    "https://*.",                 # wildcard with no base
])
def test_parse_origins_rejects_bad_entries(bad):
    with pytest.raises(ValueError):
        S.parse_origins(bad)


def test_parse_origins_accepts_list_and_separators():
    assert S.parse_origins("https://a.com,https://b.com  https://c.com/") == \
        ["https://a.com", "https://b.com", "https://c.com"]
    assert S.parse_origins(["https://a.com"]) == ["https://a.com"]
    assert S.parse_origins(None) == []
    assert S.parse_origins("") == []


def test_split_secret_reference_does_not_touch_the_vault(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("vault was read")
    monkeypatch.setattr(S, "load_vault", boom)
    assert S.split_secret_reference("github.com:password") == ("github.com", "password")
    assert S.split_secret_reference("github.com:totp") == ("github.com", "totp")
    for bad in ("nocolon", ":key", "site:", ""):
        with pytest.raises(ValueError):
            S.split_secret_reference(bad)


# ─── agent surfaces: MCP refuses the operator-only knobs ────────────────────

def test_agent_surface_violation():
    assert S.agent_surface_violation(
        "fill", {"use_secret": "a:b", "allow_cross_origin": True})
    assert S.agent_surface_violation(
        "secret_set", {"site": "github.com", "key": "origins", "value": "https://evil.com"})
    assert S.agent_surface_violation(
        "secret_set", {"site": "github.com", "key": " Origins ", "value": "x"})
    assert S.agent_surface_violation("fill", {"use_secret": "a:b"}) is None
    assert S.agent_surface_violation(
        "fill", {"use_secret": "a:b", "allow_cross_origin": False}) is None
    assert S.agent_surface_violation(
        "secret_set", {"site": "s", "key": "password", "value": "x"}) is None


def _mcp_call(monkeypatch, name, arguments):
    from vibatchium import mcp_server as M
    seen = []
    monkeypatch.setattr(M, "daemon_call",
                        lambda cmd, args=None, **kw: seen.append((cmd, args)) or {"ok": True})
    monkeypatch.setattr(M, "daemon_is_running", lambda: True)
    monkeypatch.setattr(M, "_ACTIVE_CAPS", None)
    return asyncio.run(M.call_tool(name, arguments)), seen


def test_mcp_refuses_allow_cross_origin(monkeypatch):
    """Not in the schema is not enough: call_tool forwards the arguments dict
    verbatim, so the knob must be refused explicitly — before the daemon."""
    out, seen = _mcp_call(monkeypatch, "fill", {
        "target": "#f", "use_secret": "github.com:password",
        "allow_cross_origin": True})
    assert seen == [], "the call reached the daemon"
    assert "operator-only" in json.dumps([getattr(b, "text", "") for b in
                                          getattr(out, "content", out)])


def test_mcp_refuses_rewriting_an_origins_policy(monkeypatch):
    out, seen = _mcp_call(monkeypatch, "secret_set", {
        "site": "github.com", "key": "origins", "value": "https://evil.com"})
    assert seen == []


def test_mcp_fill_schema_does_not_offer_the_escape_hatch():
    from vibatchium.mcp_server import TOOLS
    schema = next(t for t in TOOLS if t[0] == "fill")[2]
    assert "allow_cross_origin" not in schema["properties"]


def test_mcp_plain_secret_fill_still_forwards(monkeypatch):
    _out, seen = _mcp_call(monkeypatch, "fill", {
        "target": "#f", "use_secret": "github.com:password"})
    assert seen and seen[0][0] == "fill"
    assert "allow_cross_origin" not in seen[0][1]


# ─── safety-mode env: one function, one documented name ─────────────────────

@pytest.mark.parametrize("env,expected", [
    ({}, "flag-only"),
    ({"VIBATCHIUM_DEFAULT_SAFETY": "wrap"}, "wrap"),
    ({"VIBATCHIUM_SAFETY_MODE": "redact"}, "redact"),                # alias honoured
    ({"VIBATCHIUM_SAFETY_MODE": "off"}, "off"),
    ({"VIBATCHIUM_DEFAULT_SAFETY": "wrap",
      "VIBATCHIUM_SAFETY_MODE": "off"}, "wrap"),                       # documented name wins
    ({"VIBATCHIUM_DEFAULT_SAFETY": " OFF "}, "off"),
    ({"VIBATCHIUM_DEFAULT_SAFETY": "bogus"}, "flag-only"),           # typo falls back
    ({"VIBATCHIUM_DEFAULT_SAFETY": "bogus",
      "VIBATCHIUM_SAFETY_MODE": "redact"}, "redact"),
])
def test_safety_default_is_one_env_read(monkeypatch, env, expected):
    from vibatchium import safety
    from vibatchium.daemon import registry
    monkeypatch.delenv("VIBATCHIUM_DEFAULT_SAFETY", raising=False)
    monkeypatch.delenv("VIBATCHIUM_SAFETY_MODE", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # New sessions (registry) and mode-less sessions (safety) must agree.
    assert safety.default_mode() == expected
    assert registry._default_safety_mode() == expected


# ─── integration: the daemon refuses / allows real fills ────────────────────

FIELD = '<input id="f" autocomplete="off">'
SENTINEL = "ORIGIN-BOUND-SENTINEL-42"


def _build(markup):
    call("eval", {"expr": f"document.body.innerHTML = {markup!r}; 1"})


def _val(expr="document.getElementById('f').value"):
    return call("eval", {"expr": expr})["value"]


@pytest.fixture
def secret_site():
    """Seed vault entries; delete every site the test created on teardown."""
    made: list[str] = []

    def make(site=None, **kv):
        site = site or f"origin-test-{uuid.uuid4().hex[:8]}.example"
        for k, v in kv.items():
            call("secret_set", {"site": site, "key": k.replace("_", "-"), "value": v})
        made.append(site)
        return site

    yield make
    for site in made:
        try:
            call("secret_delete", {"site": site})
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def blank(local_server):
    call("go", {"url": f"{local_server}/blank.html"})
    return local_server


def test_fill_refused_on_a_foreign_origin(blank, secret_site):
    """The core bug: a github.com secret must not go into a 127.0.0.1 page."""
    site = secret_site(password=SENTINEL)
    _build(FIELD)
    with pytest.raises(Exception, match="refusing to fill") as ei:
        call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert SENTINEL not in str(ei.value)
    assert "127.0.0.1" in str(ei.value)          # names the page's origin
    assert _val() == ""                           # nothing written
    assert _val("document.getElementById('f').hasAttribute('data-vb-secret')") is False


def test_origin_is_checked_before_the_secret_is_resolved(blank, secret_site):
    """A broken totp-seed would raise a base32 error IF it were resolved; the
    origin refusal must win — proving nothing was decoded/computed first. Same
    for a key that doesn't exist (no existence oracle off-site)."""
    site = secret_site(totp_seed="!!!not-base32!!!")
    _build(FIELD)
    with pytest.raises(Exception, match="refusing to fill"):
        call("fill", {"target": "#f", "use_secret": f"{site}:totp"})
    with pytest.raises(Exception, match="refusing to fill"):
        call("fill", {"target": "#f", "use_secret": f"{site}:no-such-key"})


def test_fill_allowed_when_site_is_the_page_host(blank, secret_site):
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    _build(FIELD)
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert res["origin_check"] == "site"
    assert res["origin"] == blank                 # http://127.0.0.1:<port>
    assert SENTINEL not in json.dumps(res)
    assert _val() == SENTINEL


def test_totp_is_origin_bound_too(blank, secret_site):
    seed = "JBSWY3DPEHPK3PXP"
    foreign = secret_site(totp_seed=seed)
    _build(FIELD)
    with pytest.raises(Exception, match="refusing to fill"):
        call("fill", {"target": "#f", "use_secret": f"{foreign}:totp"})
    assert _val() == ""
    local = secret_site(site="127.0.0.1", totp_seed=seed)
    res = call("fill", {"target": "#f", "use_secret": f"{local}:totp"})
    assert res["origin_check"] == "site"
    code = _val()
    assert len(code) == 6 and code.isdigit()


def test_origins_key_allows_and_restricts(blank, secret_site, local_server):
    site = secret_site(password=SENTINEL, origins=local_server)
    _build(FIELD)
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert res["origin_check"] == "origins"
    assert _val() == SENTINEL
    # An origins list that names a DIFFERENT port refuses this page.
    port = int(local_server.rsplit(":", 1)[1])
    other = secret_site(site="127.0.0.1", password=SENTINEL,
                        origins=f"http://127.0.0.1:{port + 1}")
    _build(FIELD)
    with pytest.raises(Exception, match="not in the entry's origins"):
        call("fill", {"target": "#f", "use_secret": f"{other}:password"})
    assert _val() == ""


def test_secret_set_validates_an_origins_value(secret_site):
    site = secret_site(password="x")
    with pytest.raises(Exception, match="plain http"):
        call("secret_set", {"site": site, "key": "origins", "value": "http://example.com"})


def test_allow_cross_origin_is_an_explicit_bypass(blank, secret_site):
    site = secret_site(password=SENTINEL)
    _build(FIELD)
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password",
                        "allow_cross_origin": True})
    assert res["origin_check"] == "bypassed"
    assert res["render_masked"] == "masked"
    assert _val() == SENTINEL


def _iframe_page(local_server, inner_host):
    """Top page on 127.0.0.1, an iframe on `inner_host` (a different ORIGIN on
    the same server) containing an input."""
    port = local_server.rsplit(":", 1)[1]
    src = f"http://{inner_host}:{port}/simple.html"
    call("go", {"url": f"{local_server}/blank.html"})
    call("eval", {"expr": (
        "new Promise(r => { const f = document.createElement('iframe');"
        f" f.id = 'fr'; f.src = {src!r}; f.onload = () => r(1);"
        " document.body.appendChild(f); })")})
    return "#fr >> internal:control=enter-frame >> #q"


def test_iframe_is_judged_by_its_own_origin_not_the_top_page(local_server, secret_site):
    """The top page is 127.0.0.1 — a 127.0.0.1 secret would pass a page.url
    check — but the field lives in a localhost frame, so it must be refused.
    And a localhost secret is allowed INTO that frame."""
    target = _iframe_page(local_server, "localhost")
    top = secret_site(site="127.0.0.1", password=SENTINEL)
    with pytest.raises(Exception, match="localhost"):
        call("fill", {"target": target, "use_secret": f"{top}:password"})
    inner = secret_site(site="localhost", password=SENTINEL)
    res = call("fill", {"target": target, "use_secret": f"{inner}:password"})
    assert res["origin"].startswith("http://localhost:")


def test_field_swapped_on_focus_is_cleared_and_refused(blank, secret_site):
    """The page replaces the input the moment it's focused (so the text lands
    in a node we never checked or masked). Must refuse and leave no value."""
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    _build(FIELD)
    call("eval", {"expr": (
        "const el = document.getElementById('f');"
        "el.addEventListener('focus', () => {"
        "  const n = document.createElement('input'); n.id = 'f';"
        "  el.replaceWith(n); n.focus(); }, {once: true}); 1")})
    with pytest.raises(Exception, match="refusing|detached|replaced|not attached"):
        call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    vals = call("eval", {"expr":
        "JSON.stringify([...document.querySelectorAll('input')].map(i => i.value))"})
    assert SENTINEL not in vals["value"], "secret left behind in a swapped node"


def _page_with_foreign_iframe(local_server, event):
    port = local_server.rsplit(":", 1)[1]
    call("go", {"url": f"{local_server}/blank.html"})
    call("eval", {"expr": (
        "new Promise(r => {"
        " document.body.innerHTML = '<input id=\"f\">';"
        " const f = document.createElement('iframe'); f.id = 'evil';"
        f" f.src = 'http://localhost:{port}/simple.html'; f.onload = () => r(1);"
        " document.body.appendChild(f);"
        f" document.getElementById('f').addEventListener({event!r},"
        "   () => { f.focus(); }, {once: true}); })")})


def test_focus_redirected_into_a_foreign_iframe_is_refused(local_server, secret_site):
    """At `beforeinput` — i.e. as the text is being inserted, after Playwright's
    own focus handling — the page moves focus into a cross-origin iframe, so
    the text may go to ANOTHER document. Must refuse and clear the field."""
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    _page_with_foreign_iframe(local_server, "beforeinput")
    with pytest.raises(Exception, match="focus was redirected into a nested frame"):
        call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert _val() == ""


def test_focus_stolen_on_focus_event_is_already_harmless(local_server, secret_site):
    """Control: a steal on the `focus` event is undone by Playwright's fill,
    which re-focuses the target before inserting — the value lands in the
    checked field and the guard (correctly) sees focus on it. Pinned so a
    Playwright change in that behaviour shows up here."""
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    _page_with_foreign_iframe(local_server, "focus")
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert res["origin_check"] == "site"
    assert _val() == SENTINEL


def test_same_document_focus_move_is_not_a_false_positive(blank, secret_site):
    """A login page that auto-advances focus within ITS OWN document (OTP
    boxes, 'next field') is trusted with the value already — must not refuse."""
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    _build('<input id="f"><input id="g">')
    call("eval", {"expr": (
        "document.getElementById('f').addEventListener('input',"
        " () => document.getElementById('g').focus()); 1")})
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert res["origin_check"] == "site"
    assert _val() == SENTINEL
