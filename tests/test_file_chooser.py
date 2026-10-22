"""File-chooser interception: `upload --next` fills a native chooser that no
<input type=file> in the DOM could (daemon/file_chooser.py).

The page's buttons create a DETACHED input and click it, the shape of upload
buttons (Meta Business Suite's reel composer) that `vb upload TARGET` can't
reach. Clicks go through `mouse click x y`, the positional path callers use.
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
<div id="out"></div>
<script>
  for (const id of ['one', 'many']) {
    document.getElementById(id).addEventListener('click', () => {
      const i = document.createElement('input');   // never attached
      i.type = 'file';
      i.multiple = id === 'many';
      i.onchange = () => {
        document.getElementById('out').textContent +=
          [...i.files].map(f => f.name + ':' + f.size).join(',') + ';';
      };
      i.click();
    });
  }
</script></body></html>"""

ONE = (120, 65)
MANY = (120, 165)


@pytest.fixture
def chooser_page(_daemon_lifecycle, tmp_path):
    page = tmp_path / "upload.html"
    page.write_text(_PAGE)
    s = "vibatchium_chooser_test"
    call("start", {"ephemeral": True, "headless": True}, session=s)
    call("go", {"url": page.as_uri(), "wait_until": "load"}, session=s)
    try:
        yield s
    finally:
        call("session_close", {"name": s})


def _click(s, xy):
    call("mouse", {"action": "click", "x": xy[0], "y": xy[1]}, session=s)


def _out(s):
    return call("eval", {"expr": "document.getElementById('out').textContent"},
                session=s)["value"]


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
    assert _out(s) == "clip.mp4:1234;"
    # one shot: a second chooser gets nothing
    _click(s, ONE)
    time.sleep(0.5)
    assert _out(s) == "clip.mp4:1234;"
    assert call("upload_status", session=s)["swallowed"] == 1


def test_unarmed_chooser_is_swallowed_and_page_stays_live(chooser_page):
    s = chooser_page
    before = call("upload_status", session=s)["swallowed"]
    _click(s, ONE)
    time.sleep(0.5)
    st = call("upload_status", session=s)
    assert st["swallowed"] == before + 1 and not st["consumed"]
    assert _out(s) == ""
    assert call("eval", {"expr": "1 + 1"}, session=s)["value"] == 2


def test_expired_arm_delivers_nothing(chooser_page, tmp_path):
    s = chooser_page
    call("upload_next", {"files": [_file(tmp_path, "a.txt", "aa")], "timeout_s": 1},
         session=s)
    time.sleep(1.5)
    _click(s, ONE)
    time.sleep(0.5)
    assert _out(s) == ""
    st = call("upload_status", session=s)
    assert not st["armed"] and not st["consumed"]
    assert st["last"]["expired"] is True


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
    assert _out(s) == "a.jpg:1,b.jpg:2;"                     # nothing truncated in


def test_arm_refuses_secret_paths(chooser_page, tmp_path):
    s = chooser_page
    secret = _file(tmp_path, ".env", "API_KEY=1")
    with pytest.raises(DaemonError, match="file access denied"):
        call("upload_next", {"files": [secret]}, session=s)
    assert not call("upload_status", session=s)["armed"]


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


def test_native_switch_stops_and_restores_interception(chooser_page, tmp_path):
    s = chooser_page
    st = call("upload_native", {"state": "on"}, session=s)
    assert st["native"] and not st["intercepting"]
    before = st["swallowed"]
    _click(s, ONE)            # headless Chrome cancels the native dialog itself
    time.sleep(0.5)
    assert call("upload_status", session=s)["swallowed"] == before
    st = call("upload_native", {"state": "off"}, session=s)
    assert st["intercepting"]
    # arming while native also turns interception back on
    call("upload_native", {"state": "on"}, session=s)
    call("upload_next", {"files": [_file(tmp_path, "c.txt", "ccc")]}, session=s)
    _click(s, ONE)
    assert call("upload_status", {"wait_s": 5}, session=s)["consumed"]
    assert _out(s) == "c.txt:3;"


def test_popup_pages_are_intercepted_too(chooser_page, tmp_path):
    s = chooser_page
    page = tmp_path / "upload.html"
    assert call("page_new", session=s)["index"] == 1        # a tab opened after launch
    call("go", {"url": page.as_uri(), "wait_until": "load"}, session=s)
    call("upload_next", {"files": [_file(tmp_path, "d.txt", "dddd")]}, session=s)
    _click(s, ONE)
    assert call("upload_status", {"wait_s": 5}, session=s)["consumed"]
    assert _out(s) == "d.txt:4;"
