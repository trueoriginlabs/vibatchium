"""0.19.4: the guards around a vault secret that is live in a page.

Origin binding (test_secret_origin_binding.py) decides WHERE `fill
--use-secret` may write. These tests cover what happens around the write, end
to end through the daemon:

  * read-back refusal — while a vault-filled field holds a value, `value`,
    `eval`, `wait_fn`, `eval_handle`, `handle_eval`, `detect_forms values=true`
    and `go javascript:` are refused; clearing the field or navigating away
    lifts it; `title` keeps working;
  * caller-JS taint — a document the caller ran JS in (`eval`, `content`,
    `go javascript:`, a `route_add` fulfill response) refuses a secret fill
    until it is reloaded;
  * opaque frames (about:blank / about:srcdoc) are refused;
  * the write is the native value setter + input/change events, so a
    React-style controlled input picks the value up;
  * only text-like <input> / <textarea> targets are accepted;
  * copy / cut on a vault-filled field yields nothing;
  * the pure helpers (IDNA, javascript: detection, the env override).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from vibatchium import secrets as S
from vibatchium.client import call
from vibatchium.daemon import secret_guard as G

from ._secret_pages import (
    IN_FRAME, SENTINEL, go_form, go_frame_host, page_url, probe, secret_site,
)

__all__ = ["secret_site"]   # pytest fixture, used by parameter name

LIVE = "vault secret is live"
TAINT = "caller-supplied JavaScript"


@pytest.fixture
def local_site(secret_site):
    """A secret bound to the local fixture server's host."""
    return secret_site(site="127.0.0.1", password=SENTINEL)


def _fill(site, target="#f", key="password"):
    return call("fill", {"target": target, "use_secret": f"{site}:{key}"})


# ─── read-back refusal ──────────────────────────────────────────────────────

READBACK_CALLS = [
    ("value", {"target": "#f"}),
    ("eval", {"expr": "document.getElementById('f').value"}),
    ("wait_fn", {"expr": "() => true", "timeout_ms": 2000}),
    ("eval_handle", {"expr": "document.getElementById('f')"}),
    ("detect_forms", {"values": True}),
    ("go", {"url": "javascript:void(document.title='x:'+"
                   "document.getElementById('f').value)",
            "wait_for_render": False, "timeout_ms": 5000}),
]


def test_readback_verbs_are_refused_while_a_secret_is_live(local_server, local_site):
    go_form(local_server)
    _fill(local_site)
    for verb, args in READBACK_CALLS:
        with pytest.raises(Exception, match=LIVE) as ei:
            call(verb, args)
        assert SENTINEL not in str(ei.value), verb
    # The page's own title is not a read-back verb — and the javascript: URL
    # above did not get to run (it would have rewritten the title).
    assert call("title", {})["title"] == "secret form"
    # detect_forms without values is fine.
    forms = call("detect_forms", {})
    assert SENTINEL not in json.dumps(forms)
    assert probe()["vals"]["f"] == SENTINEL      # the fill itself is intact


def test_clearing_the_field_lifts_the_refusal(local_server, local_site):
    go_form(local_server)
    _fill(local_site)
    with pytest.raises(Exception, match=LIVE):
        call("eval", {"expr": "1"})
    call("fill", {"target": "#f", "text": ""})
    assert call("eval", {"expr": "1 + 1"})["value"] == 2
    assert call("value", {"target": "#f"})["value"] == ""


def test_navigating_away_lifts_the_refusal(local_server, local_site):
    go_form(local_server)
    _fill(local_site)
    with pytest.raises(Exception, match=LIVE):
        call("value", {"target": "#f"})
    assert call("title", {})["title"] == "secret form"
    call("go", {"url": f"{local_server}/simple.html"})
    assert call("eval", {"expr": "1 + 1"})["value"] == 2
    assert call("title", {})["title"] == "Vibatchium Test Page"


def test_handle_eval_is_refused_while_a_secret_is_live(local_server, local_site):
    """A handle taken BEFORE the fill (navigation drops handles, so: in another
    tab) can't be used to run caller JS while the secret is live."""
    go_form(local_server)
    call("page_new", {})                        # tab 1: about:blank
    try:
        hid = call("eval_handle", {"expr": "window"})["handle"]
        call("page_switch", {"index": 0})
        _fill(local_site)
        with pytest.raises(Exception, match=LIVE):
            call("handle_eval", {"handle": hid, "expr": "w => w.document.title"})
    finally:
        call("page_switch", {"index": 1})
        call("page_close", {})
        call("page_switch", {"index": 0})


def test_readback_is_refused_from_another_tab_too(local_server, local_site):
    """Script in one tab can reach a same-origin tab it opened, so every tab of
    the session counts."""
    go_form(local_server)
    _fill(local_site)
    call("page_new", {})
    try:
        call("go", {"url": f"{local_server}/simple.html"})
        with pytest.raises(Exception, match=LIVE):
            call("eval", {"expr": "1"})
    finally:
        call("page_close", {})
        call("page_switch", {"index": 0})


# ─── caller-JS taint ────────────────────────────────────────────────────────

def test_eval_then_secret_fill_is_refused_until_reload(local_server, local_site):
    go_form(local_server)
    call("eval", {"expr": "1"})
    with pytest.raises(Exception, match=TAINT) as ei:
        _fill(local_site)
    assert "reload" in str(ei.value)
    assert probe()["vals"]["f"] == ""
    call("reload", {})
    res = _fill(local_site)
    assert res["origin_check"] == "site"
    assert probe()["vals"]["f"] == SENTINEL


@pytest.mark.parametrize("verb,args", [
    ("wait_fn", {"expr": "() => true"}),
    ("eval_handle", {"expr": "document"}),
    ("go", {"url": "javascript:void(0)", "wait_for_render": False,
            "timeout_ms": 5000}),
])
def test_other_caller_js_verbs_taint_the_document(local_server, local_site, verb, args):
    go_form(local_server)
    try:
        call(verb, args)
    except Exception:  # noqa: BLE001 — goto(javascript:) reports ERR_ABORTED
        pass
    with pytest.raises(Exception, match=TAINT):
        _fill(local_site)


def test_content_taints_the_document(local_server, local_site):
    """`content` (set_content) keeps the document's origin, so caller markup and
    script would run AS the login page's origin."""
    go_form(local_server)
    call("content", {"html": '<p>caller page</p><input id="f">'})
    with pytest.raises(Exception, match=TAINT):
        _fill(local_site)


def test_route_fulfill_cannot_serve_a_fake_login_page(local_server, local_site):
    """`route_add --mode fulfill` serves caller bytes that the browser
    attributes to the real origin — a fake login page with an exfil script
    would pass origin binding. Refused while the rule is installed, refused for
    the document it served even after `route_clear`, allowed after a reload
    fetches the real page."""
    body = ('<!doctype html><title>fake</title><p>' + "x" * 120 +
            '</p><input id="f">')
    call("route_add", {"pattern": "**/secret_form.html", "mode": "fulfill",
                       "body": body, "content_type": "text/html"})
    try:
        call("go", {"url": f"{local_server}/secret_form.html"})
        assert call("title", {})["title"] == "fake"
        with pytest.raises(Exception, match="fulfill"):
            _fill(local_site)
    finally:
        call("route_clear", {})
    with pytest.raises(Exception, match=TAINT):
        _fill(local_site)
    call("reload", {})
    assert call("title", {})["title"] == "secret form"
    assert _fill(local_site)["origin_check"] == "site"


# ─── opaque frames ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["blank", "srcdoc"])
def test_secret_fill_into_an_opaque_frame_is_refused(local_server, local_site, kind):
    """An about:blank / about:srcdoc document's origin comes from whoever
    created or navigated it, not from its parent — never written into, even
    when the parent is an allowed origin."""
    go_frame_host(local_server, **{kind: 1})
    target = IN_FRAME + "#f"
    call("wait_selector", {"selector": target, "state": "attached"})
    with pytest.raises(Exception, match="no verifiable origin") as ei:
        _fill(local_site, target)
    assert SENTINEL not in str(ei.value)
    assert call("attr", {"target": target, "name": "data-vb-secret"})["value"] is None


# ─── the write itself ───────────────────────────────────────────────────────

def test_controlled_input_framework_sees_the_value(local_server, local_site):
    """The fixture emulates React's value tracker (the page shadows the node's
    own `value` setter and ignores `input` events whose value equals what it
    last set). The isolated-world native setter bypasses the shadow, so the
    `input` event carries a real change and the framework's state updates."""
    go_form(local_server, react=1)
    _fill(local_site)
    react = probe()["react"]
    assert react["state"] == SENTINEL, react
    assert react["setterCalls"] == 0         # never went through the page's setter
    assert react["events"] >= 1


@pytest.mark.parametrize("field", ["select", "checkbox", "contenteditable"])
def test_unsupported_targets_are_refused_with_nothing_written(
        local_server, local_site, field):
    go_form(local_server, field=field)
    with pytest.raises(Exception, match="unsupported") as ei:
        _fill(local_site)
    assert SENTINEL not in str(ei.value)
    snap = probe()
    assert SENTINEL not in json.dumps(snap)
    assert snap["sec"] is False                # not even masked
    # Nothing live: read verbs still work on this page.
    call("eval", {"expr": "1"})


def test_copy_from_a_secret_field_yields_nothing(local_server, local_site):
    """`press Control+A` / `Control+C` on a text-type secret field, then a paste
    elsewhere, would read the value back without any read verb."""
    go_form(local_server, other=1, plain=1)
    # Control: headless clipboard copy/paste works here at all.
    call("fill", {"target": "#plain", "text": "CONTROL-COPY"})
    call("press", {"target": "#plain", "keys": "Control+A"})
    call("press", {"target": "#plain", "keys": "Control+C"})
    call("press", {"target": "#other", "keys": "Control+V"})
    if probe()["vals"]["other"] != "CONTROL-COPY":
        pytest.skip("keyboard copy/paste doesn't reach the clipboard in this "
                    "headless Chrome — the copy guard can't be exercised")
    call("fill", {"target": "#other", "text": ""})
    _fill(local_site)
    call("press", {"target": "#f", "keys": "Control+A"})
    call("press", {"target": "#f", "keys": "Control+C"})
    call("press", {"target": "#other", "keys": "Control+V"})
    call("press", {"target": "#f", "keys": "Control+A"})
    call("press", {"target": "#f", "keys": "Control+X"})
    call("press", {"target": "#other", "keys": "Control+V"})
    snap = probe()
    assert SENTINEL not in snap["vals"]["other"], "secret copied out via the clipboard"
    assert snap["vals"]["f"] == SENTINEL         # cut was blocked too


# ─── pure helpers ───────────────────────────────────────────────────────────

def test_idna_is_uts46_not_idna2003():
    """Browsers resolve straße.de to xn--strae-oqa.de; Python's built-in idna
    codec (IDNA2003) maps it to strasse.de, a different registrable domain."""
    assert S.normalize_host("straße.de") == "xn--strae-oqa.de"
    assert S.host_matches_site("xn--strae-oqa.de", "straße.de")
    assert not S.host_matches_site("strasse.de", "straße.de")
    assert S.normalize_host("Bücher.DE") == "xn--bcher-kva.de"


@pytest.mark.parametrize("url,expected", [
    ("javascript:alert(1)", True),
    ("JavaScript:void(0)", True),
    ("  javascript:x", True),
    ("\x01javascript:x", True),
    ("java\tscript:x", True),
    ("java\nscript:x", True),
    ("https://example.com/javascript:x", False),
    ("about:blank", False),
    ("", False),
    (None, False),
])
def test_is_javascript_url(url, expected):
    assert G.is_javascript_url(url) is expected


def test_readback_env_override_disables_both_guards(monkeypatch):
    """VIBATCHIUM_SECRET_ALLOW_READBACK=1 (daemon env only) turns off the
    read-back refusal and the caller-JS taint check."""
    monkeypatch.delenv(S.ALLOW_READBACK_ENV, raising=False)
    assert not S.readback_env_enabled()
    monkeypatch.setenv(S.ALLOW_READBACK_ENV, "1")
    assert S.readback_env_enabled()

    class Entry:
        flags = {"secret_filled": True}
        session = None
    asyncio.run(G.refuse_secret_readback(Entry(), "value"))   # no raise
    assert asyncio.run(G.caller_js_violation(None, None, "s")) is None
    assert G.fulfill_route_violation(
        type("Sess", (), {"_routes": [{"mode": "fulfill", "pattern": "*"}]})(),
        "s") is None


def test_page_url_helper_builds_cross_origin_urls(local_server):
    """Guard for the helper the frame tests rely on."""
    port = local_server.rsplit(":", 1)[1]
    assert page_url(local_server, host="localhost", a=1) == \
        f"http://localhost:{port}/secret_form.html?a=1"
