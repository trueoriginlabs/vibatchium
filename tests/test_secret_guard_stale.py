"""secret_guard never evaluates a tracked handle whose document is gone.

On Patchright 1.60, ElementHandle.evaluate on a handle whose execution context
died in a navigation kills the shared Playwright driver (every session on the
daemon goes with it); 1.61+ raises an ordinary error. The pin now requires
1.61.2, but the guard must not depend on that: each tracked handle carries a
token stamped into its frame's document, and the handle is only touched while
the frame still holds that token.
"""
import asyncio

from vibatchium.daemon import secret_guard as sg


class _Frame:
    def __init__(self):
        self.tokens: set[str] = set()
        self.detached = False

    def is_detached(self):
        return self.detached

    async def evaluate(self, js, token):
        if js == sg._DOC_STAMP_JS:
            self.tokens.add(token)
            return True
        assert js == sg._DOC_HAS_JS
        return token in self.tokens

    def navigate(self):
        self.tokens.clear()          # a new document has no stamp


class _Handle:
    def __init__(self, live=True):
        self.live = live
        self.evaluated = 0
        self.disposed = 0

    async def evaluate(self, js):
        self.evaluated += 1
        return self.live

    async def dispose(self):
        self.disposed += 1


class _Page:
    frames: list = []


def _run(coro):
    return asyncio.run(coro)


def _live(page):
    # Only the tracked-handle half; the DOM sweep needs a real browser.
    async def go():
        page.frames = []
        return await sg.secret_live(page)
    return _run(go())


def test_same_document_handle_is_checked():
    page, frame, h = _Page(), _Frame(), _Handle(live=True)
    _run(sg.track_secret_field(page, h, frame))
    assert _live(page) is True
    assert h.evaluated == 1


def test_navigated_document_handle_is_never_touched():
    page, frame, h = _Page(), _Frame(), _Handle(live=True)
    _run(sg.track_secret_field(page, h, frame))
    frame.navigate()
    assert _live(page) is False
    assert h.evaluated == 0 and h.disposed == 0
    assert not sg._FILLED.get(page)          # dropped, not kept for later


def test_detached_frame_handle_is_never_touched():
    page, frame, h = _Page(), _Frame(), _Handle(live=True)
    _run(sg.track_secret_field(page, h, frame))
    frame.detached = True
    assert _live(page) is False
    assert h.evaluated == 0


def test_eviction_drops_without_disposing():
    page, frame = _Page(), _Frame()
    handles = [_Handle() for _ in range(sg._MAX_TRACKED + 3)]
    for h in handles:
        _run(sg.track_secret_field(page, h, frame))
    assert len(sg._FILLED[page]) == sg._MAX_TRACKED
    assert all(h.disposed == 0 for h in handles)
