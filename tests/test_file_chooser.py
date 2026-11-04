"""File-chooser interception: `upload --next` fills a native chooser that no
<input type=file> in the DOM could (daemon/file_chooser.py).

The page's buttons create a DETACHED input and click it, the shape of upload
buttons (Meta Business Suite's reel composer) that `vb upload TARGET` can't
reach. Clicks go through `mouse click x y`, the positional path callers use.

Interception is on only while armed. Unarmed, headless Chrome cancels the
dialog itself, which the page sees as the input's `cancel` event: that is how
these tests tell "native" from "intercepted" (an intercepted chooser nobody
fills fires neither `change` nor `cancel`).
"""
from __future__ import annotations

import time

import pytest

from vibatchium.client import DaemonError, call

_PAGE = """<!doctype html><html><body style="margin:0">
<button id="one" style="position:absolute;left:40px;top:40px;width:160px;height:50px">
  Add video</button>
<button id="many" style="position:absolute;left:40px;top:140px;width:160px;height:50px">
  Add photos</button>
<button id="two" style="position:absolute;left:40px;top:240px;width:160px;height:50px">
  Two inputs</button>
<div id="out"></div>
<script>
  const out = document.getElementById('out');
  function pick(tag, multiple) {
    const i = document.createElement('input');   // never attached
    i.type = 'file';
    i.multiple = multiple;
    i.onchange = () => {
      out.textContent += tag + [...i.files].map(f => f.name + ':' + f.size).join(',') + ';';
    };
    i.oncancel = () => { out.textContent += tag + 'cancel;'; };
    i.click();
  }
  document.getElementById('one').onclick = () => pick('', false);
  document.getElementById('many').onclick = () => pick('', true);
  document.getElementById('two').onclick = () => { pick('A=', false); pick('B=', false); };
</script></body></html>"""

# A tab that clicks a fresh file input every 50ms, with no user gesture.
_POLLER = """<!doctype html><html><body><div id="out"></div><script>
  const out = document.getElementById('out');
  setInterval(() => {
    const i = document.createElement('input');
    i.type = 'file';
    i.onchange = () => { out.textContent += [...i.files].map(f => f.name).join(',') + ';'; };
    i.click();
  }, 50);
</script></body></html>"""

_FRAMED = """<!doctype html><html><body style="margin:0">
<iframe src="inner.html" style="position:absolute;left:0;top:0;width:400px;height:200px;border:0">
</iframe><div id="out"></div><script>
  // file: frames are opaque origins to each other, so the frame posts its result.
  addEventListener('message', e => { document.getElementById('out').textContent += e.data; });
</script></body></html>"""

_INNER = """<!doctype html><html><body style="margin:0">
<input id="f" type="file" style="position:absolute;left:40px;top:40px;width:200px;height:50px">
<script>
  const f = document.getElementById('f');
  f.onchange = () => {
    parent.postMessage([...f.files].map(x => x.name + ':' + x.size).join(',') + ';', '*');
  };
</script></body></html>"""

ONE = (120, 65)
MANY = (120, 165)
TWO = (120, 265)
SESSION = "vibatchium_chooser_test"


@pytest.fixture
def chooser_page(_daemon_lifecycle, tmp_path):
    page = tmp_path / "upload.html"
    page.write_text(_PAGE)
    call("start", {"ephemeral": True, "headless": True}, session=SESSION)
    call("go", {"url": page.as_uri(), "wait_until": "load"}, session=SESSION)
    try:
        yield SESSION
    finally:
        call("session_close", {"name": SESSION})


def _click(s, xy):
    call("mouse", {"action": "click", "x": xy[0], "y": xy[1]}, session=s)


def _out(s):
    return call("eval", {"expr": "document.getElementById('out').textContent"},
                session=s)["value"]


def _wait_out(s, want, timeout=5.0):
    """Poll the page's #out until it equals ``want`` (or time runs out)."""
    deadline = time.monotonic() + timeout
    while (got := _out(s)) != want and time.monotonic() < deadline:
        time.sleep(0.05)
    return got


def _wait_status(s, pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred(st := call("upload_status", session=s)) and time.monotonic() < deadline:
        time.sleep(0.05)
    return st


def _file(tmp_path, name, body):
    f = tmp_path / name
    f.write_text(body)
    return str(f)


def test_armed_chooser_gets_the_file(chooser_page, tmp_path):
    s = chooser_page
    clip = _file(tmp_path, "clip.mp4", "x" * 1234)
    armed = call("upload_next", {"files": [clip]}, session=s)
    assert armed["armed"] and armed["intercepting"]
    _click(s, ONE)
    st = call("upload_status", {"wait_s": 5}, session=s)
    assert st["consumed"] and not st["armed"]
    assert st["last"]["multiple"] is False
    assert st["last"]["url"].endswith("/upload.html")
    assert _out(s) == "clip.mp4:1234;"
    # one shot, and interception is off again: the next chooser is native
    assert not st["intercepting"] and st["native"]
    _click(s, ONE)
    assert _wait_out(s, "clip.mp4:1234;cancel;") == "clip.mp4:1234;cancel;"


def test_unarmed_chooser_is_native(chooser_page):
    s = chooser_page
    st = call("upload_status", session=s)
    assert not st["intercepting"] and st["native"]
    _click(s, ONE)            # headless Chrome cancels the native dialog itself
    assert _wait_out(s, "cancel;") == "cancel;"
    st = call("upload_status", session=s)
    assert st["swallowed"] == 0 and not st["consumed"]


def test_one_click_two_choosers_only_the_first_gets_the_file(chooser_page, tmp_path):
    s = chooser_page
    call("upload_next", {"files": [_file(tmp_path, "a.txt", "aa")]}, session=s)
    _click(s, TWO)
    st = call("upload_status", {"wait_s": 5}, session=s)
    assert st["consumed"]
    assert _out(s) == "A=a.txt:2;"        # the second input never got the file
    # Chrome 147 opens both choosers and the second finds no arm (swallowed);
    # newer Chrome opens one chooser per click, so the second may never arrive.
    assert st["swallowed"] in (0, 1)
    assert not st["intercepting"]


def test_expired_arm_stops_intercepting_without_a_chooser(chooser_page, tmp_path):
    s = chooser_page
    call("upload_next", {"files": [_file(tmp_path, "a.txt", "aa")], "timeout_s": 1},
         session=s)
    # The expiry timer settles the arm and wakes this waiter; no chooser needed.
    t0 = time.monotonic()
    st = call("upload_status", {"wait_s": 10}, session=s)
    assert time.monotonic() - t0 < 5
    assert not st["armed"] and not st["consumed"]
    assert st["last"]["expired"] is True
    assert not st["intercepting"]
    _click(s, ONE)
    assert _wait_out(s, "cancel;") == "cancel;"      # native, nothing delivered


def test_multiple_chooser_gets_all_and_single_refuses_two(chooser_page, tmp_path):
    s = chooser_page
    a, b = _file(tmp_path, "a.jpg", "a"), _file(tmp_path, "b.jpg", "bb")
    call("upload_next", {"files": [a, b]}, session=s)
    _click(s, MANY)
    st = call("upload_status", {"wait_s": 5}, session=s)
    assert st["consumed"] and st["last"]["multiple"] is True
    assert _out(s) == "a.jpg:1,b.jpg:2;"
    call("upload_next", {"files": [a, b]}, session=s)
    _click(s, ONE)
    st = call("upload_status", {"wait_s": 5}, session=s)
    assert not st["consumed"] and "takes one" in st["last"]["error"]
    assert not st["intercepting"]
    assert _out(s) == "a.jpg:1,b.jpg:2;"                     # nothing truncated in


def test_rearm_replaces_the_pending_arm(chooser_page, tmp_path):
    s = chooser_page
    call("upload_next", {"files": [_file(tmp_path, "old.txt", "o")]}, session=s)
    call("upload_next", {"files": [_file(tmp_path, "new.txt", "nn")]}, session=s)
    _click(s, ONE)
    st = call("upload_status", {"wait_s": 5}, session=s)
    assert st["consumed"] and st["last"]["files"][0].endswith("new.txt")
    assert _out(s) == "new.txt:2;"


def test_arm_refuses_secret_paths(chooser_page, tmp_path):
    s = chooser_page
    secret = _file(tmp_path, ".env", "API_KEY=1")
    with pytest.raises(DaemonError, match="file access denied"):
        call("upload_next", {"files": [secret]}, session=s)
    st = call("upload_status", session=s)
    assert not st["armed"] and not st["intercepting"]


def test_agent_surface_arm_is_confined_to_its_roots(chooser_page, tmp_path):
    from vibatchium import fspolicy
    s = chooser_page
    inside, outside = tmp_path / "in", tmp_path / "out"
    inside.mkdir(), outside.mkdir()
    scope = {fspolicy.SCOPE_ARG: {"roots": [str(inside)], "cwd": str(inside)}}
    with pytest.raises(DaemonError, match="file access denied"):
        call("upload_next", {"files": [_file(outside, "x.txt", "x")], **scope}, session=s)
    ok = call("upload_next", {"files": [_file(inside, "y.txt", "y")], **scope}, session=s)
    assert ok["armed"]


def test_arm_is_bound_to_the_active_tab(chooser_page, tmp_path):
    """A background tab clicking file inputs on a timer must not take an arm
    made for the active tab, and must not flood the shared daemon log."""
    from vibatchium.daemon.paths import LOG_PATH
    s = chooser_page
    poller = tmp_path / "poller.html"
    poller.write_text(_POLLER)
    assert call("page_new", session=s)["index"] == 1
    call("go", {"url": poller.as_uri(), "wait_until": "load"}, session=s)
    call("page_switch", {"index": 0}, session=s)            # poller is in the background
    log_start = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0

    call("upload_next", {"files": [_file(tmp_path, "mine.txt", "mmm")]}, session=s)
    st = _wait_status(s, lambda st: st["foreign"] >= 5)
    assert st["foreign"] >= 5, st
    assert st["armed"] and not st["consumed"]
    _click(s, ONE)
    st = call("upload_status", {"wait_s": 5}, session=s)
    assert st["consumed"] and st["last"]["url"].endswith("/upload.html")
    assert _out(s) == "mine.txt:3;"
    assert not st["intercepting"]

    with LOG_PATH.open("rb") as fh:
        fh.seek(log_start)
        lines = [ln for ln in fh.read().decode(errors="replace").splitlines()
                 if "file chooser ignored" in ln and " INFO " in ln]
    assert len(lines) == 1, lines                            # first per page only

    call("page_switch", {"index": 1}, session=s)
    assert _out(s) == ""                                     # the poller got nothing


def test_chooser_in_an_iframe_reports_the_frame_url(chooser_page, tmp_path):
    s = chooser_page
    (tmp_path / "inner.html").write_text(_INNER)
    framed = tmp_path / "framed.html"
    framed.write_text(_FRAMED)
    call("go", {"url": framed.as_uri(), "wait_until": "load"}, session=s)
    call("upload_next", {"files": [_file(tmp_path, "f.txt", "ffff")]}, session=s)
    _click(s, (140, 65))
    st = call("upload_status", {"wait_s": 5}, session=s)
    assert st["consumed"]
    assert st["last"]["url"].endswith("/inner.html")
    assert st["last"]["page_url"].endswith("/framed.html")
    assert _wait_out(s, "f.txt:4;") == "f.txt:4;"


def test_closing_the_armed_tab_settles_the_arm(chooser_page, tmp_path):
    s = chooser_page
    call("page_new", session=s)
    call("upload_next", {"files": [_file(tmp_path, "z.txt", "z")]}, session=s)
    call("page_close", session=s)
    st = call("upload_status", {"wait_s": 5}, session=s)
    assert not st["armed"] and not st["consumed"]
    assert "closed" in st["last"]["error"]
    assert not st["intercepting"]


def test_arm_on_a_second_tab(chooser_page, tmp_path):
    s = chooser_page
    page = tmp_path / "upload.html"
    assert call("page_new", session=s)["index"] == 1        # a tab opened after launch
    call("go", {"url": page.as_uri(), "wait_until": "load"}, session=s)
    call("upload_next", {"files": [_file(tmp_path, "d.txt", "dddd")]}, session=s)
    _click(s, ONE)
    assert call("upload_status", {"wait_s": 5}, session=s)["consumed"]
    assert _out(s) == "d.txt:4;"


def test_renderer_crash_settles_the_arm(tmp_path):
    """A pending arm can't outlive the page it is bound to: a renderer crash
    settles it. Best-effort like the other live crash tests: skips if this
    Chrome doesn't report CDP Page.crash as a page crash."""
    import asyncio

    from vibatchium.daemon import file_chooser
    from vibatchium.daemon.registry import SessionRegistry
    f = _file(tmp_path, "r.txt", "r")
    crashed = {}

    async def go():
        reg = SessionRegistry()
        try:
            entry = await reg.create("vbtest-chooser-crash", headless=True,
                                     ephemeral=True)
            s = entry.session
            file_chooser.arm(s, [f], [f], 30)
            cdp = await s.context.new_cdp_session(s.page)
            # Page.crash never answers (the renderer dies), so don't await it.
            crash = asyncio.ensure_future(cdp.send("Page.crash"))
            try:
                await asyncio.wait_for(s.file_chooser["_settled"].wait(), 10)
            except TimeoutError:
                return
            finally:
                crash.cancel()
            crashed["status"] = file_chooser.status(s)
        finally:
            await reg.close_all()

    asyncio.run(go())
    if "status" not in crashed:
        pytest.skip("this Chrome did not report Page.crash as a page crash")
    st = crashed["status"]
    assert "crashed" in st["last"]["error"] and not st["intercepting"]


def test_self_heal_relaunch_settles_the_arm(tmp_path):
    """A self-heal relaunch settles a pending arm and hands the outcome to the
    new session, so `upload_status` there says why; a waiter is woken."""
    import asyncio

    from vibatchium.daemon import file_chooser
    from vibatchium.daemon.registry import SessionRegistry
    f = _file(tmp_path, "r.txt", "r")

    async def go():
        reg = SessionRegistry()
        try:
            entry = await reg.create("vbtest-chooser-heal", headless=True,
                                     ephemeral=True)
            s = entry.session
            file_chooser.arm(s, [f], [f], 30)
            waiter = asyncio.ensure_future(file_chooser.wait_settled(s, 10))
            e2 = await reg.relaunch("vbtest-chooser-heal")
            await asyncio.wait_for(waiter, 5)                 # woken, not timed out
            st = file_chooser.status(e2.session)
            assert not st["armed"] and not st["consumed"]
            assert "relaunched" in st["last"]["error"]
            assert not st["intercepting"]
            assert e2.session.file_chooser["_timer"] is None  # expiry timer gone
        finally:
            await reg.close_all()

    asyncio.run(go())
