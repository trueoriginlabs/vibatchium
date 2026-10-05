"""0.19.4: follow-on file: loads.

`fspolicy.check_nav_url` judges the URL a verb navigates to, but a loaded
file: page can itself redirect to, frame, or open a popup on another file:
URL. Two layers close that:

  * agent surfaces (a call carrying `_fs_scope`) refuse file: navigation
    outright, before any Chrome is touched;
  * every surface: the browser-wide file guard (daemon/file_guard.py, a CDP
    Fetch interceptor on `file://*` at the browser target) judges every file:
    request the browser makes, so a page in an allowed dir can't pull in a
    denied file.

The "denied" target is a tmp file with a secret-shaped NAME (`id_rsa_*`) and
harmless content, so nothing real is at stake if the policy regressed.
"""
from __future__ import annotations

import time

import pytest

from vibatchium import fspolicy
from vibatchium.client import DaemonError, call

SENTINEL = "FAKE-KEY-MATERIAL-7731"
ALLOWED = "ALLOWED-FOLLOW-ON-OK"


@pytest.fixture
def pages(tmp_path):
    key = tmp_path / "id_rsa_vbtest"
    key.write_text(SENTINEL + "\n")
    ok = tmp_path / "ok.html"
    ok.write_text(f"<html><body><p>{ALLOWED}</p></body></html>")
    pad = "<p>" + "padding text " * 12 + "</p>"
    hop = tmp_path / "hop.html"
    hop.write_text(f"<html><body>{pad}<script>setTimeout(() => "
                   f"{{location.href = {key.as_uri()!r}}}, 50)</script></body></html>")
    hop_ok = tmp_path / "hop_ok.html"
    hop_ok.write_text(f"<html><body>{pad}<script>setTimeout(() => "
                      f"{{location.href = {ok.as_uri()!r}}}, 50)</script></body></html>")
    frames = tmp_path / "frames.html"
    frames.write_text(f"<html><body>{pad}<iframe name=bad src={key.as_uri()!r}></iframe>"
                      f"<iframe name=good src={ok.as_uri()!r}></iframe></body></html>")
    popup = tmp_path / "popup.html"
    popup.write_text(f"<html><body>{pad}<a id=pop target=_blank "
                     f"href={key.as_uri()!r}>open</a></body></html>")
    return {"key": key, "ok": ok, "hop": hop, "hop_ok": hop_ok,
            "frames": frames, "popup": popup}


def _wait_url_change(start: str, timeout: float = 5.0) -> str:
    end = time.time() + timeout
    url = start
    while time.time() < end:
        url = call("url", {})["url"]
        if url != start:
            return url
        time.sleep(0.1)
    return url


def test_redirect_to_a_denied_file_is_blocked(local_server, pages):
    call("go", {"url": pages["hop"].as_uri()})
    url = _wait_url_change(pages["hop"].as_uri())
    assert not url.endswith("id_rsa_vbtest"), url
    assert SENTINEL not in call("text", {})["text"]


def test_redirect_to_an_allowed_file_still_works(local_server, pages):
    """Positive control: the guard judges paths, it doesn't kill file:."""
    call("go", {"url": pages["hop_ok"].as_uri()})
    _wait_url_change(pages["hop_ok"].as_uri())
    deadline = time.time() + 5
    while ALLOWED not in call("text", {})["text"] and time.time() < deadline:
        time.sleep(0.1)
    assert ALLOWED in call("text", {})["text"]


def test_iframe_of_a_denied_file_is_blocked(local_server, pages):
    call("go", {"url": pages["frames"].as_uri()})
    time.sleep(0.5)
    frames = {f["name"]: f["url"] for f in call("frames", {})["frames"]}
    assert frames.get("good") == pages["ok"].as_uri()          # control
    assert frames.get("bad") != pages["key"].as_uri(), frames
    call("frame", {"name": "bad"})
    try:
        assert SENTINEL not in call("text", {})["text"]
    finally:
        call("frame", {})


def test_popup_of_a_denied_file_is_blocked(local_server, pages):
    call("go", {"url": pages["popup"].as_uri()})
    before = len(call("pages", {})["pages"])
    call("click", {"target": "#pop"})
    deadline = time.time() + 5
    while len(call("pages", {})["pages"]) == before and time.time() < deadline:
        time.sleep(0.1)
    try:
        listing = call("pages", {})["pages"]
        assert len(listing) == before + 1
        assert listing[-1]["url"] != pages["key"].as_uri(), listing
        call("page_switch", {"index": len(listing) - 1})
        time.sleep(0.3)
        assert SENTINEL not in call("text", {})["text"]
    finally:
        listing = call("pages", {})["pages"]
        if len(listing) > before:
            call("page_close", {"index": len(listing) - 1})
        call("page_switch", {"index": 0})


def test_agent_surface_refuses_file_navigation(local_server, pages):
    """A call carrying `_fs_scope` (what MCP / a caps-restricted REST shim
    attach) refuses file: even for an allowed path, and even when its roots
    are opted out (`roots: None`); the same call without a scope goes."""
    for scope in ({"roots": None, "cwd": None},
                  {"roots": [str(pages["ok"].parent)], "cwd": str(pages["ok"].parent)}):
        with pytest.raises(DaemonError, match="agent surface"):
            call("go", {"url": pages["ok"].as_uri(), fspolicy.SCOPE_ARG: scope})
    call("go", {"url": pages["ok"].as_uri()})
    assert ALLOWED in call("text", {})["text"]


def test_file_guard_is_armed_on_the_session():
    from vibatchium.daemon import file_guard
    assert file_guard._PATTERNS == [{"urlPattern": "file://*",
                                     "requestStage": "Request"}]
