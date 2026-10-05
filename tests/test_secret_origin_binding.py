"""0.19.4: `fill --use-secret` is origin-bound.

Before this, `fill @e3 --use-secret github.com:password` resolved the vault
value and wrote it into whatever element was targeted on whatever page was open
— so a prompt-injected agent could type a stored credential into evil.com and
read it back. Now the value is only written into a document whose origin
belongs to the secret's site (judged by the frame that OWNS the field), the
check runs before the secret is resolved, every frame above the field must be
an allowed origin too, and a field swapped mid-write is cleared and refused.
The read-back / caller-JS guards around a live secret are in
test_secret_guards.py.

Three layers here:
  * pure unit tests of the matching rules (no daemon);
  * the agent-surface guard (MCP refuses the operator-only knobs);
  * integration tests against the per-session test daemon + local server.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from vibatchium import secrets as S
from vibatchium.client import call

from ._secret_pages import (
    IN_EVIL, IN_FRAME, SENTINEL, go_form, go_frame_host, page_url, port_of,
    probe, secret_site,
)

__all__ = ["secret_site"]   # pytest fixture, used by parameter name


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
    assert S.agent_surface_violation("fill", {"use_secret": "a:b"}) is None
    assert S.agent_surface_violation(
        "fill", {"use_secret": "a:b", "allow_cross_origin": False}) is None
    assert S.agent_surface_violation("fill", {"target": "#q", "text": "x"}) is None


@pytest.mark.parametrize("cmd,args", [
    ("secret_init", {}),
    ("secret_init", {"force": True}),
    ("secret_set", {"site": "github.com", "key": "origins", "value": "https://evil.com"}),
    ("secret_set", {"site": "github.com", "key": " Origins ", "value": "x"}),
    ("secret_set", {"site": "s", "key": "password", "value": "x"}),     # any key
    ("secret_set", {"site": "s", "key": "email-poll", "value": "imap://evil"}),
    ("secret_delete", {"site": "github.com"}),
    ("secret_delete", {"site": "github.com", "key": "origins"}),
    ("secret_totp", {"site": "github.com"}),
    ("wait_email_code", {"site": "github.com"}),
])
def test_operator_only_verbs_are_refused_on_agent_surfaces(cmd, args):
    """Every vault mutation, and every verb that hands back a code without the
    origin check `fill --use-secret` applies, is operator-only."""
    msg = S.agent_surface_violation(cmd, args)
    assert msg and "operator-only" in msg
    assert cmd in S.OPERATOR_ONLY_VERBS


@pytest.mark.parametrize("cmd,args", [
    ("secret_list", {}),
    ("secret_list", {"site": "github.com"}),
    ("fill", {"target": "#f", "use_secret": "github.com:totp"}),
    ("go", {"url": "https://github.com/login"}),
])
def test_agent_surface_allows_masked_and_bound_verbs(cmd, args):
    assert S.agent_surface_violation(cmd, args) is None


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


@pytest.mark.parametrize("name,arguments", [
    ("secret_set", {"site": "github.com", "key": "origins", "value": "https://evil.com"}),
    ("secret_set", {"site": "github.com", "key": "password", "value": "x"}),
    ("secret_delete", {"site": "github.com"}),
    ("secret_init", {}),
    ("secret_totp", {"site": "github.com"}),
    ("wait_email_code", {"site": "github.com"}),
])
def test_mcp_refuses_operator_only_verbs(monkeypatch, name, arguments):
    """Whatever the cap set, MCP's call_tool refuses these before the daemon
    (with _ACTIVE_CAPS=None every tool is reachable, so the refusal must come
    from agent_surface_violation, not the cap filter)."""
    from vibatchium import mcp_server as M
    tool_names = {t[0] for t in M.TOOLS}
    if name not in tool_names:
        pytest.skip(f"{name} is not an MCP tool")
    out, seen = _mcp_call(monkeypatch, name, arguments)
    assert seen == [], "the call reached the daemon"
    assert "operator-only" in json.dumps([getattr(b, "text", "") for b in
                                          getattr(out, "content", out)])


def test_mcp_call_tool_consults_the_surface_guard_on_every_call(monkeypatch):
    """call_tool must hand EVERY call (cmd + mapped args) to
    agent_surface_violation, not only a hard-coded list."""
    from vibatchium import secrets as secrets_mod
    seen_checks = []
    real = secrets_mod.agent_surface_violation

    def spy(cmd, args):
        seen_checks.append(cmd)
        return real(cmd, args)
    monkeypatch.setattr(secrets_mod, "agent_surface_violation", spy)
    for name, arguments in [("go", {"url": "https://example.com"}),
                            ("title", {}),
                            ("fill", {"target": "#f", "text": "x"})]:
        _mcp_call(monkeypatch, name, arguments)
    assert seen_checks == ["go", "title", "fill"]


def _rest_client(monkeypatch, caps):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from vibatchium import client as C
    from vibatchium import rest as R
    seen = []
    # build_app imports these from vibatchium.client when it builds the app.
    monkeypatch.setattr(C, "call",
                        lambda cmd, args=None, **kw: seen.append((cmd, args)) or {"ok": True})
    monkeypatch.setattr(C, "daemon_is_running", lambda: True)
    app = R.build_app(require_auth=False, caps=caps)
    return TestClient(app), seen


def test_restricted_rest_refuses_operator_only_verbs(monkeypatch):
    """A caps-restricted REST shim serves untrusted clients: same refusals as
    MCP, consulted on every call, even when the verb's bucket is granted."""
    from vibatchium import secrets as secrets_mod
    checks = []
    real = secrets_mod.agent_surface_violation
    monkeypatch.setattr(secrets_mod, "agent_surface_violation",
                        lambda cmd, args: checks.append(cmd) or real(cmd, args))
    client, seen = _rest_client(monkeypatch, "nav,secrets")
    for verb, body in [("secret_set", {"site": "s", "key": "password", "value": "x"}),
                       ("secret_totp", {"site": "s"}),
                       ("secret_delete", {"site": "s"})]:
        r = client.post(f"/v1/{verb}", json=body)
        assert r.status_code == 403, (verb, r.status_code, r.text)
        assert "operator-only" in r.text
    r = client.post("/v1/title", json={})
    assert r.status_code == 200, r.text
    assert [c for c, _ in seen] == ["title"]
    assert checks == ["secret_set", "secret_totp", "secret_delete", "title"]


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
#
# Pages come from static fixtures configured by the query string, and what
# landed is read through the page's own probe (tests/_secret_pages.py): since
# 0.19.4 a secret fill refuses a document the caller ran `eval` in, and
# `value` / `eval` are refused while the secret is in a field.


def test_fill_refused_on_a_foreign_origin(local_server, secret_site):
    """The core bug: a github.com secret must not go into a 127.0.0.1 page."""
    site = secret_site(password=SENTINEL)
    go_form(local_server)
    with pytest.raises(Exception, match="refusing to fill") as ei:
        call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert SENTINEL not in str(ei.value)
    assert "127.0.0.1" in str(ei.value)          # names the page's origin
    snap = probe()
    assert snap["vals"]["f"] == ""               # nothing written
    assert snap["sec"] is False                  # not even masked


def test_origin_is_checked_before_the_secret_is_resolved(local_server, secret_site):
    """A broken totp-seed would raise a base32 error IF it were resolved; the
    origin refusal must win — proving nothing was decoded/computed first. Same
    for a key that doesn't exist (no existence oracle off-site)."""
    site = secret_site(totp_seed="!!!not-base32!!!")
    go_form(local_server)
    with pytest.raises(Exception, match="refusing to fill"):
        call("fill", {"target": "#f", "use_secret": f"{site}:totp"})
    with pytest.raises(Exception, match="refusing to fill"):
        call("fill", {"target": "#f", "use_secret": f"{site}:no-such-key"})


def test_fill_allowed_when_site_is_the_page_host(local_server, secret_site):
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    go_form(local_server)
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert res["origin_check"] == "site"
    assert res["origin"] == local_server         # http://127.0.0.1:<port>
    assert SENTINEL not in json.dumps(res)
    assert probe()["vals"]["f"] == SENTINEL


def test_totp_is_origin_bound_too(local_server, secret_site):
    seed = "JBSWY3DPEHPK3PXP"
    foreign = secret_site(totp_seed=seed)
    go_form(local_server)
    with pytest.raises(Exception, match="refusing to fill"):
        call("fill", {"target": "#f", "use_secret": f"{foreign}:totp"})
    assert probe()["vals"]["f"] == ""
    local = secret_site(site="127.0.0.1", totp_seed=seed)
    res = call("fill", {"target": "#f", "use_secret": f"{local}:totp"})
    assert res["origin_check"] == "site"
    code = probe()["vals"]["f"]
    assert len(code) == 6 and code.isdigit()


def test_origins_key_allows_and_restricts(local_server, secret_site):
    site = secret_site(password=SENTINEL, origins=local_server)
    go_form(local_server)
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert res["origin_check"] == "origins"
    assert probe()["vals"]["f"] == SENTINEL
    # An origins list that names a DIFFERENT port refuses this page.
    port = int(port_of(local_server))
    other = secret_site(site="127.0.0.1", password=SENTINEL,
                        origins=f"http://127.0.0.1:{port + 1}")
    go_form(local_server)
    with pytest.raises(Exception, match="not in the entry's origins"):
        call("fill", {"target": "#f", "use_secret": f"{other}:password"})
    assert probe()["vals"]["f"] == ""


def test_secret_set_validates_an_origins_value(secret_site):
    site = secret_site(password="x")
    with pytest.raises(Exception, match="plain http"):
        call("secret_set", {"site": site, "key": "origins", "value": "http://example.com"})


def test_allow_cross_origin_is_an_explicit_bypass(local_server, secret_site):
    site = secret_site(password=SENTINEL)
    go_form(local_server)
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password",
                        "allow_cross_origin": True})
    assert res["origin_check"] == "bypassed"
    assert res["render_masked"] == "masked"
    assert probe()["vals"]["f"] == SENTINEL


def test_iframe_is_judged_by_its_own_origin_not_the_top_page(local_server, secret_site):
    """Top page on 127.0.0.1, the field in a localhost iframe (two origins on
    one server). A 127.0.0.1 secret would pass a page.url check, but the field
    is localhost's, so it's refused. A localhost secret matches the field but
    the frame is embedded by 127.0.0.1 — refused by the ancestor rule — until
    the entry's `origins` lists both."""
    port = port_of(local_server)
    go_frame_host(local_server, src=page_url(local_server, host="localhost"))
    target = IN_FRAME + "#f"
    top = secret_site(site="127.0.0.1", password=SENTINEL)
    with pytest.raises(Exception, match="localhost"):
        call("fill", {"target": target, "use_secret": f"{top}:password"})
    inner = secret_site(site="localhost", password=SENTINEL)
    with pytest.raises(Exception, match="embedded by http://127.0.0.1"):
        call("fill", {"target": target, "use_secret": f"{inner}:password"})
    assert probe(IN_FRAME)["vals"]["f"] == ""
    both = secret_site(password=SENTINEL, origins=(
        f"http://localhost:{port},http://127.0.0.1:{port}"))
    res = call("fill", {"target": target, "use_secret": f"{both}:password"})
    assert res["origin"].startswith("http://localhost:")
    assert probe(IN_FRAME)["vals"]["f"] == SENTINEL


def test_login_page_framed_by_a_foreign_origin_is_refused(local_server, secret_site):
    """The setup for every focus / overlay / navigation game: a foreign top page
    (localhost) frames the real login page (127.0.0.1). The field's own origin
    is right, but its embedder isn't — refused, nothing written."""
    port = port_of(local_server)
    go_frame_host(local_server, host="localhost", src=page_url(local_server))
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    with pytest.raises(Exception, match="embedded by http://localhost") as ei:
        call("fill", {"target": IN_FRAME + "#f", "use_secret": f"{site}:password"})
    assert SENTINEL not in str(ei.value)
    snap = probe(IN_FRAME)
    assert snap["vals"]["f"] == "" and snap["sec"] is False


def test_field_swapped_during_the_write_is_cleared_and_refused(local_server, secret_site):
    """The page replaces the input the moment the value arrives (so it would
    sit in a node we never checked or masked). Must refuse and leave no value
    anywhere."""
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    go_form(local_server, swap="input")
    with pytest.raises(Exception, match="refusing|detached|replaced|swapped"):
        call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert SENTINEL not in json.dumps(probe()["inputs"]), \
        "secret left behind in a swapped node"


@pytest.mark.parametrize("events", ["focus", "beforeinput", "blur",
                                    "focus,beforeinput,blur,input,keydown"])
def test_focus_moved_into_a_foreign_iframe_cannot_redirect_the_value(
        local_server, secret_site, events):
    """The old keyboard-insert fill could be redirected: Playwright focused the
    field, then inserted text into WHATEVER was focused, so a page that moved
    focus into a cross-origin iframe in that gap got the value. The write now
    goes through the pinned node's value setter, with no focus or keyboard
    involved — the value lands only in the checked field, and the foreign frame
    (which autofocuses its own field and would receive any typed text) gets
    nothing."""
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    evil = page_url(local_server, host="localhost", autofocus=1)
    go_form(local_server, evil=evil, steal=events)
    call("wait_selector", {"selector": IN_EVIL + "#probe"})
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert res["origin_check"] == "site"
    assert probe()["vals"]["f"] == SENTINEL
    inner = probe(IN_EVIL)
    assert SENTINEL not in json.dumps(inner["vals"]), \
        "the value reached the foreign iframe"


def test_same_document_focus_move_is_not_a_false_positive(local_server, secret_site):
    """A login page that auto-advances focus within ITS OWN document (OTP
    boxes, 'next field') is trusted with the value already — must not refuse."""
    site = secret_site(site="127.0.0.1", password=SENTINEL)
    go_form(local_server, advance=1)
    res = call("fill", {"target": "#f", "use_secret": f"{site}:password"})
    assert res["origin_check"] == "site"
    snap = probe()
    assert snap["vals"]["f"] == SENTINEL
    assert snap["vals"]["g"] == ""


def test_control_the_steal_does_redirect_keyboard_text(local_server):
    """Control for the test above: the same page really does pull KEYBOARD
    input into the foreign frame (what the old focus + insertText fill did), so
    the clean result above is the write path's doing, not a dud fixture."""
    evil = page_url(local_server, host="localhost", autofocus=1)
    go_form(local_server, evil=evil, steal="beforeinput")
    call("wait_selector", {"selector": IN_EVIL + "#probe"})
    call("type", {"target": "#f", "text": "KEYS"})
    inner = probe(IN_EVIL)
    assert inner["vals"]["f"], f"steal had no effect on keyboard input: {inner}"
