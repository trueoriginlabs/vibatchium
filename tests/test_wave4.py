"""Tests for Wave 4: HAR export, eval_handle, stealth-mouse fallback."""
import json
from pathlib import Path

from patchium.client import call


def test_har_capture_writes_valid_har(local_server, tmp_path):
    """har start → navigate → har stop writes a valid HAR 1.2 doc."""
    har_path = tmp_path / "test.har"
    call("har_start", {"path": str(har_path)})
    call("go", {"url": f"{local_server}/simple.html"})
    call("sleep", {"ms": 300})
    res = call("har_stop")
    assert res["recording"] is False
    assert res["entries"] >= 1
    assert har_path.exists()
    doc = json.loads(har_path.read_text())
    assert doc["log"]["version"] == "1.2"
    assert doc["log"]["creator"]["name"] == "patchium"
    # at least one entry should be the simple.html request
    urls = [e["request"]["url"] for e in doc["log"]["entries"]]
    assert any("simple.html" in u for u in urls), f"no simple.html in HAR: {urls}"
    # first entry should have status + content-size
    e0 = doc["log"]["entries"][0]
    assert e0["response"]["status"] == 200
    assert e0["response"]["content"]["size"] > 0


def test_har_url_filter(local_server, tmp_path):
    """har_start with url_filter only records matching URLs."""
    har_path = tmp_path / "filtered.har"
    call("har_start", {"path": str(har_path), "url_filter": "api-test"})
    call("go", {"url": f"{local_server}/simple.html"})
    # trigger the api-test fetch
    call("click", {"target": "#trigger-fetch"})
    call("sleep", {"ms": 400})
    res = call("har_stop")
    doc = json.loads(har_path.read_text())
    # should only contain api-test entries, NOT simple.html
    urls = [e["request"]["url"] for e in doc["log"]["entries"]]
    assert all("api-test" in u for u in urls), f"non-filtered URL leaked: {urls}"


def test_eval_handle_lifecycle(local_server):
    """Create handle, eval against it, dispose."""
    call("go", {"url": f"{local_server}/simple.html"})
    # clear any prior handles from earlier tests
    call("handle_dispose_all")
    res = call("eval_handle", {"expr": "document.querySelectorAll('p')"})
    assert res["handle"].startswith("h_")
    hid = res["handle"]
    # count via the handle
    n = call("handle_eval", {"handle": hid, "expr": "(nl) => nl.length"})["value"]
    assert n == 1  # only one <p> in simple.html (the #lead)
    # list shows it
    lst = call("handle_list")
    assert hid in lst["handles"]
    # dispose
    call("handle_dispose", {"handle": hid})
    lst = call("handle_list")
    assert hid not in lst["handles"]


def test_handle_invalidates_on_navigation(local_server):
    """Handles are auto-disposed when the page navigates."""
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("eval_handle", {"expr": "document.body"})
    hid = res["handle"]
    # navigate
    call("go", {"url": f"{local_server}/second.html"})
    # handle should be gone (best-effort: list reports empty)
    lst = call("handle_list")
    assert hid not in lst["handles"], "handle survived navigation — should be invalidated"


def test_handle_eval_unknown_id_errors(local_server):
    """Using an unknown handle id gives a clear error."""
    import pytest
    from patchium.client import DaemonError
    with pytest.raises(DaemonError, match="unknown handle"):
        call("handle_eval", {"handle": "h_nonexistent", "expr": "(x) => x"})


def test_stealth_mouse_falls_back_cleanly():
    """Without cdp_patches installed, --stealth-mouse should report fallback,
    not crash. (Session fixture starts WITHOUT --stealth-mouse, so this is
    covered by direct CLI smoke in the README — here we just import-check.)
    """
    from patchium.stealth import humanize_mouse_available
    ok, info = humanize_mouse_available()
    # We don't assert ok=False because the test machine might have cdp_patches;
    # we just assert the function returns a (bool, str) tuple cleanly.
    assert isinstance(ok, bool)
    assert isinstance(info, str)
