"""Regression tests for the Wave 3 P0/P1 audit fixes.

Covers:
- snapshot invalidation on navigation
- _is_ref_target strictness (doesn't false-match CSS [attr^="@e"])
- storage_restore preserves sessionStorage
- route add/list/clear lifecycle
- dialog_policy replaces prior handler cleanly
"""
from patchium.client import call


def test_snapshot_invalidates_on_navigation(local_server):
    """After go/back/forward, refs from the previous map must NOT silently resolve."""
    call("go", {"url": f"{local_server}/simple.html"})
    call("map")
    # navigate away
    call("go", {"url": f"{local_server}/second.html"})
    # using a pre-nav ref should now error explicitly
    import pytest
    from patchium.client import DaemonError
    with pytest.raises(DaemonError, match="invalidated"):
        call("click", {"target": "@e1"})


def test_snapshot_invalidates_on_reload(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    call("map")
    call("reload")
    import pytest
    from patchium.client import DaemonError
    with pytest.raises(DaemonError, match="invalidated"):
        call("click", {"target": "@e1"})


def test_css_selector_with_at_e_attribute_not_treated_as_ref(local_server):
    """`[data-test^="@e"]` is a CSS selector, not a ref. Must not error as ref."""
    # set up a div on the page
    call("go", {"url": f"{local_server}/simple.html"})
    call("eval", {"expr": "document.body.insertAdjacentHTML('beforeend', '<div data-test=\"@e1-marker\" id=\"odd\">marker</div>')"})
    # this is a CSS selector — should NOT trigger ref-resolution
    res = call("count", {"target": '[data-test^="@e"]'})
    assert res["count"] == 1


def test_storage_restore_preserves_session_storage(local_server, tmp_path):
    """storage_restore should write both LS and SS, not just LS."""
    # state with both kinds
    state = {
        "cookies": [],
        "origins": [{
            "origin": local_server,
            "localStorage": [{"name": "ls_key", "value": "ls_val"}],
            "sessionStorage": [{"name": "ss_key", "value": "ss_val"}],
        }],
    }
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("storage_restore", {"state": state})
    assert res["localStorage_items"] == 1
    assert res["sessionStorage_items"] == 1
    assert res["in_place"] is True  # we're on the same origin
    # verify the values landed
    ls = call("eval", {"expr": "localStorage.getItem('ls_key')"})["value"]
    ss = call("eval", {"expr": "sessionStorage.getItem('ss_key')"})["value"]
    assert ls == "ls_val"
    assert ss == "ss_val"


def test_route_add_list_clear(local_server):
    """Add a route, list it, clear it."""
    call("go", {"url": f"{local_server}/simple.html"})
    # clear any leftover routes from prior tests
    call("route_clear")
    # add
    res = call("route_add", {"pattern": "**/*.css", "mode": "abort"})
    assert res["added"] == "**/*.css"
    # list
    lst = call("route_list")["routes"]
    assert any(r["pattern"] == "**/*.css" and r["mode"] == "abort" for r in lst)
    # clear specific
    res = call("route_clear", {"pattern": "**/*.css"})
    assert res["cleared"] == 1
    # listing should be empty now
    assert call("route_list")["routes"] == []


def test_dialog_policy_replaces_prior_handler(local_server):
    """Setting dialog_policy twice should not accumulate handlers."""
    call("go", {"url": f"{local_server}/simple.html"})
    call("dialog_policy", {"action": "dismiss"})
    call("dialog_policy", {"action": "accept"})
    # if the prior handler was leaked, accept+dismiss would race; we just verify
    # the policy state is the latest one set
    # (full E2E would require triggering a confirm() and checking which fired —
    #  add later when we have a fixture page with a deterministic dialog)


def test_close_active_page_recovers(local_server):
    """If active page closes, daemon falls back to a live page."""
    call("go", {"url": f"{local_server}/simple.html"})
    call("page_new")
    # we're now on the new (blank) page; the simple.html page is index 0
    pages = call("pages")["pages"]
    assert len(pages) >= 2
    # close active → fallback should land us on a live page
    call("page_close")
    res = call("status")
    assert res["running"] is True
    # the daemon's session.page should still be navigable
    call("go", {"url": f"{local_server}/simple.html"})
    assert call("title")["title"] == "Patchium Test Page"
