"""vision_click must honour humanize like every other click path.

It used to be the one click verb that bypassed the humanization layer: a bare
`page.mouse.click(cx, cy)` — a teleport, zero dwell, no trajectory. That is
precisely the interaction shape session-lifetime behavioural scoring is built
to catch, and vision clicking is the path most likely to be used on a hardened
target, so it was the worst possible verb to leave unhumanized.

No Anthropic API key is needed here: we prime vibatchium's vision cache with
the (screenshot, intent) pair the handler is about to look up, so find_element
returns a cache hit and never calls out. The page itself records the mouse
events, so the assertions are about what Chrome actually received rather than
what the daemon claims it sent.
"""
from __future__ import annotations


import pytest

from vibatchium import vision as _vision
from vibatchium.client import call

# A static page: no animation, so two successive screenshots are byte-identical
# and the cache key is stable.
MARKUP = ('<div id="t" style="position:absolute;left:100px;top:100px;'
          'width:200px;height:120px;background:#39c"></div>')

# Installed by its own eval: a <script> inserted via innerHTML never executes.
RECORDER = """
window.__ev = {moves: 0, down: null, up: null};
document.addEventListener('mousemove', () => { window.__ev.moves++; });
document.addEventListener('mousedown', e => { window.__ev.down = e.timeStamp; });
document.addEventListener('mouseup',   e => { window.__ev.up   = e.timeStamp; });
1
"""


def _shot_bytes():
    """The exact PNG bytes find_element will hash for its cache key.

    With no `path` the verb returns `png_b64`; with one it writes the file and
    returns the path. Both it and find_element take a default viewport PNG, so
    the bytes match on a static page.
    """
    import base64
    res = call("screenshot", {})
    if res.get("png_b64"):
        return base64.b64decode(res["png_b64"])
    path = res.get("path") or res.get("file")
    assert path, f"screenshot returned neither png_b64 nor path: {sorted(res)}"
    with open(path, "rb") as fh:
        return fh.read()


def _prime(intent, x, y):
    _vision.cache_put(_shot_bytes(), intent,
                      {"x": x, "y": y, "confidence": 0.99, "rationale": "test"})


def _events():
    return call("eval", {"expr": "window.__ev"})["value"]


@pytest.fixture
def page(local_server):
    call("go", {"url": f"{local_server}/blank.html"})
    call("eval", {"expr": f"document.body.innerHTML = {MARKUP!r}; 1"})
    call("eval", {"expr": RECORDER})
    return None


def _click(intent, x, y):
    _prime(intent, x, y)
    return call("vision_click", {"intent": intent})


def test_humanize_on_produces_a_trajectory_and_dwell(page):
    call("humanize_on", {})
    try:
        res = _click("the blue box", 200, 160)
        assert res["clicked"] is True
        assert res["humanized"] is True, "handler must report it humanized"
        ev = _events()
        assert ev["moves"] > 1, \
            f"humanized click must move along a path, saw {ev['moves']} mousemove(s)"
        assert ev["down"] is not None and ev["up"] is not None
        assert ev["up"] - ev["down"] > 0, \
            "humanized click must hold the button for a sampled dwell, not 0ms"
    finally:
        call("humanize_off", {})


def test_humanize_off_is_unchanged(page):
    call("humanize_off", {})
    res = _click("the blue box off", 200, 160)
    assert res["clicked"] is True
    assert res["humanized"] is False
    ev = _events()
    # A teleport: Chrome synthesises at most the one move implied by the click.
    assert ev["moves"] <= 1, \
        f"unhumanized click should not draw a path, saw {ev['moves']} moves"


def test_vision_click_lands_on_target(page):
    """Humanization must not cost accuracy — the click still hits the box."""
    call("humanize_on", {})
    try:
        call("eval", {"expr":
                      "document.getElementById('t').addEventListener("
                      "'click', () => { window.__hit = true; }); 1"})
        _click("the blue box hit", 200, 160)
        assert call("eval", {"expr": "window.__hit === true"})["value"] is True
    finally:
        call("humanize_off", {})
