"""Frames, storage round-trip, waits, network capture, multi-page."""

from vibatchium.client import call


def test_frames_list(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    call("sleep", {"ms": 200})  # let iframe load
    res = call("frames")
    frames = res["frames"]
    # main + iframe
    assert len(frames) == 2
    assert any(f["is_main"] for f in frames)
    assert any("iframe.html" in (f["url"] or "") for f in frames)


def test_frame_switch_and_interact(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    call("sleep", {"ms": 300})
    call("frame", {"url": "iframe.html"})
    # inside iframe, count its button
    n = call("count", {"target": "#inner-btn"})["count"]
    assert n == 1
    # back to main
    call("frame", {})
    n = call("count", {"target": "#inner-btn"})["count"]
    assert n == 0  # not in main frame's DOM


def test_storage_roundtrip(local_server, tmp_path):
    call("go", {"url": f"{local_server}/simple.html"})
    # write a localStorage value via eval
    call("eval", {"expr": "localStorage.setItem('vibatchium_test_key', 'roundtrip_ok'); 'set'"})
    state_path = tmp_path / "state.json"
    call("storage_export", {"path": str(state_path)})
    assert state_path.exists()
    # state json should contain the value
    contents = state_path.read_text()
    assert "vibatchium_test_key" in contents
    assert "roundtrip_ok" in contents


def test_wait_load_networkidle(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("wait_load", {"state": "networkidle"})
    assert res["state"] == "networkidle"


def test_wait_fn(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    res = call("wait_fn", {"expr": "document.title.length > 0"})
    assert res["satisfied"] is True


def test_network_capture(local_server):
    call("network_start", {"max": 100})
    call("go", {"url": f"{local_server}/simple.html"})
    call("sleep", {"ms": 200})
    res = call("network_dump")
    events = res["events"]
    assert any(ev.get("url", "").endswith("/simple.html") for ev in events), \
        f"expected /simple.html in {events[:3]}"
    call("network_stop")


def test_network_capture_with_url_filter(local_server):
    """url_filter discards events whose URL does not match the substring."""
    call("network_start", {"max": 100, "url_filter": "no-such-url-segment-xyz"})
    call("go", {"url": f"{local_server}/simple.html"})
    call("sleep", {"ms": 200})
    res = call("network_dump")
    assert res["events"] == [], "filter should have rejected every event"
    call("network_stop")


def test_network_capture_with_response_headers(local_server):
    """capture_response_headers=True populates events[].headers (response only)."""
    call(
        "network_start",
        {
            "max": 100,
            "url_filter": "/simple.html",
            "capture_response_headers": True,
        },
    )
    call("go", {"url": f"{local_server}/simple.html"})
    call("sleep", {"ms": 200})
    res = call("network_dump")
    responses = [
        ev for ev in res["events"]
        if ev.get("phase") == "response" and ev.get("url", "").endswith("/simple.html")
    ]
    assert responses, "expected at least one filtered response event"
    # headers field present + dict-shaped
    assert "headers" in responses[0]
    assert isinstance(responses[0]["headers"], dict)
    call("network_stop")


def test_network_capture_response_bodies(local_server):
    """0.9.1: capture_response_bodies grabs the response body into the ring
    buffer. This is the race-free CreateTweet-capture primitive — arm capture,
    fire a SEPARATE click that triggers an XHR, then dump and read the body (no
    session-lock deadlock like wait_response). simple.html's #trigger-fetch
    button does fetch('/api-test') → JSON, the same shape as a GraphQL submit
    response carrying a new id."""
    call("network_start", {
        "max": 100,
        "url_filter": "/api-test",
        "capture_response_bodies": True,
    })
    call("go", {"url": f"{local_server}/simple.html"})
    call("click", {"target": "#trigger-fetch"})
    call("sleep", {"ms": 300})
    res = call("network_dump")
    bodies = [
        ev for ev in res["events"]
        if ev.get("phase") == "response" and "/api-test" in ev.get("url", "")
    ]
    assert bodies, f"expected an /api-test response event, got {res['events'][:3]}"
    ev = bodies[0]
    # network_dump must have drained the async body fetch before returning
    assert "body_pending" not in ev
    assert '"hello":"world"' in ev.get("text", ""), \
        f"expected captured JSON body, got {ev.get('text')!r}"
    call("network_stop")


def test_network_capture_bodies_opt_in(local_server):
    """Body capture is opt-in: without capture_response_bodies, the matching
    response event carries no `text`/`b64` (only the cheap metadata)."""
    call("network_start", {"max": 100, "url_filter": "/api-test"})
    call("go", {"url": f"{local_server}/simple.html"})
    call("click", {"target": "#trigger-fetch"})
    call("sleep", {"ms": 300})
    res = call("network_dump")
    for ev in res["events"]:
        if "/api-test" in ev.get("url", ""):
            assert "text" not in ev and "b64" not in ev
    call("network_stop")


def test_pages_lifecycle():
    res = call("pages")
    initial = len(res["pages"])
    call("page_new")
    res = call("pages")
    assert len(res["pages"]) == initial + 1
    call("page_close")
    res = call("pages")
    assert len(res["pages"]) == initial


def test_viewport_set_get():
    call("viewport", {"width": 800, "height": 600})
    res = call("viewport")
    assert res["width"] == 800
    assert res["height"] == 600
