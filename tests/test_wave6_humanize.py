"""Wave 6.2b — humanization tests.

Verifies:
- humanized_path returns ≥30 non-collinear points with measurable curviness
- sample_dwell_ms produces stdev > 15ms across 20 samples
- sinusoidal_scroll sums to total_dy exactly
- humanize_on/off/status handlers manage per-session flag
- Default mouse handler doesn't humanize unless flag set
- Humanized click on a real page completes within 2s and triggers click handler
"""
from __future__ import annotations

import statistics


from vibatchium.client import call
from vibatchium.humanize import (
    humanized_path, path_curviness, sample_dwell_ms, sinusoidal_scroll,
)


# ─── pure unit tests ────────────────────────────────────────────────────


def test_humanized_path_default_point_count():
    """Distance / 5 with floor 30, ceiling 200."""
    path = humanized_path((0, 0), (500, 0))  # distance 500 → 100 points
    assert 90 <= len(path) <= 110

    short_path = humanized_path((0, 0), (10, 0))  # short → floor at 30
    assert len(short_path) == 30

    long_path = humanized_path((0, 0), (2000, 0))  # long → ceiling at 200
    assert len(long_path) == 200


def test_humanized_path_starts_at_origin_ends_at_target():
    path = humanized_path((10, 20), (100, 200))
    # Bezier exactly hits start and end (cubic bezier with control points)
    assert abs(path[0][0] - 10) < 1e-6
    assert abs(path[0][1] - 20) < 1e-6
    assert abs(path[-1][0] - 100) < 1e-6
    assert abs(path[-1][1] - 200) < 1e-6


def test_humanized_path_curves():
    """The Bezier control points jitter perpendicular to the line, so path
    is NOT straight — curviness > 0.1 radians cumulative."""
    # Seed for determinism in test
    path = humanized_path((0, 0), (500, 0), seed=42)
    curv = path_curviness(path)
    assert curv > 0.1, f"path looks straight (curviness={curv:.3f} rad)"


def test_humanized_path_variance_across_seeds():
    """Two paths from same endpoints with different seeds should differ."""
    a = humanized_path((0, 0), (300, 0), seed=1)
    b = humanized_path((0, 0), (300, 0), seed=2)
    # Compare midpoints
    mid_a = a[len(a) // 2]
    mid_b = b[len(b) // 2]
    assert mid_a != mid_b, "different seeds produce identical paths"


def test_humanized_path_deterministic_with_seed():
    a = humanized_path((0, 0), (300, 0), seed=42)
    b = humanized_path((0, 0), (300, 0), seed=42)
    assert a == b


def test_sample_dwell_distribution():
    """20 samples should have stdev > 15ms (proves it's not constant)."""
    samples = [sample_dwell_ms() for _ in range(20)]
    stdev = statistics.stdev(samples)
    assert stdev > 15, f"dwell stdev too low: {stdev:.1f}ms"
    # All within the clamp range
    for s in samples:
        assert 40 <= s <= 250


def test_sample_dwell_clamps_to_range():
    """Even with extreme params, output is clamped."""
    s = sample_dwell_ms(mean_ms=10000, stdev_ms=1, floor_ms=40, ceiling_ms=250)
    assert s <= 250
    s = sample_dwell_ms(mean_ms=-100, stdev_ms=1, floor_ms=40, ceiling_ms=250)
    assert s >= 40


def test_sinusoidal_scroll_sums_to_total():
    """Cumulative dy matches input dy regardless of duration."""
    steps = sinusoidal_scroll(500, duration_ms=300)
    total = sum(dy for _, dy in steps)
    assert abs(total - 500) < 0.001, f"sum {total} ≠ 500"

    # Negative direction
    steps = sinusoidal_scroll(-200, duration_ms=200)
    total = sum(dy for _, dy in steps)
    assert abs(total - (-200)) < 0.001


def test_sinusoidal_scroll_has_multiple_steps():
    """For typical scroll, expect ~20 steps at 16ms tick / 300ms duration."""
    steps = sinusoidal_scroll(500, duration_ms=300, tick_ms=16)
    assert 15 <= len(steps) <= 25


def test_sinusoidal_scroll_short_duration_collapses_to_one_step():
    steps = sinusoidal_scroll(500, duration_ms=10, tick_ms=16)
    assert len(steps) == 1


# ─── handler-level tests ────────────────────────────────────────────────


def test_humanize_off_by_default():
    """Default session should report humanize=False."""
    res = call("humanize_status")
    assert res["humanize"] is False


def test_humanize_toggle_persists_in_session():
    call("humanize_on")
    try:
        assert call("humanize_status")["humanize"] is True
    finally:
        call("humanize_off")
    assert call("humanize_status")["humanize"] is False


def test_mouse_click_reports_humanized_flag(local_server):
    """The `mouse` handler response includes `humanized: true|false` so
    callers can verify the path was actually humanized."""
    call("go", {"url": f"{local_server}/simple.html"})
    # off
    call("humanize_off")
    r1 = call("mouse", {"action": "click", "x": 50, "y": 50})
    assert r1["humanized"] is False
    # on
    call("humanize_on")
    try:
        r2 = call("mouse", {"action": "click", "x": 60, "y": 60})
        assert r2["humanized"] is True
    finally:
        call("humanize_off")


def test_humanized_click_latency_acceptable(local_server):
    """Humanized click for a typical short distance should complete in <2s."""
    import time
    call("go", {"url": f"{local_server}/simple.html"})
    call("humanize_on")
    try:
        t0 = time.time()
        call("mouse", {"action": "click", "x": 100, "y": 100})
        elapsed = time.time() - t0
        assert elapsed < 2.0, f"humanized click took {elapsed:.2f}s (>2s)"
    finally:
        call("humanize_off")


# ─── element-level humanization (Wave 7.8.1) ────────────────────────────
# The point of these is correctness: with humanize ON, the semantic
# click/type verbs must still hit the RIGHT element and produce the RIGHT
# value (the click stays Playwright's verified click; we only add motion).


def test_humanized_type_delays_shape_and_clamp():
    from vibatchium.humanize import humanized_type_delays
    d = humanized_type_delays(40, seed=7)
    assert len(d) == 40
    assert all(22 <= x <= 1200 for x in d)             # clamped to the new range
    assert humanized_type_delays(5, seed=7) == humanized_type_delays(5, seed=7)


def test_humanized_type_delays_is_heavy_tailed():
    # Real keystroke timing is right-skewed with occasional long pauses, NOT a
    # tight gaussian — the behavioural oracle flagged the old stdev~45ms as
    # metronomic against a real operator's 90-240ms within-phrase spread.
    from vibatchium.humanize import humanized_type_delays
    d = humanized_type_delays(400, seed=1)
    med = statistics.median(d)
    assert statistics.pstdev(d) / statistics.fmean(d) > 0.65   # heavy-tailed (CV≈0.8)
    assert max(d) > 400                                        # long hesitations occur
    # the FAR tail (the pause mechanism), not merely a wider lognormal: many keys
    # land beyond 3x the median. A plain/higher-sigma lognormal without the pause
    # gives ~6 here; the pauses push it to ~25 — this is the load-bearing guard.
    assert sum(1 for x in d if x > 3 * med) >= 15


def test_humanized_click_hits_correct_element(local_server):
    """humanize ON: clicking #counter-btn repeatedly increments the counter —
    proving the verified click lands on the intended element every time, not a
    neighbor."""
    call("go", {"url": f"{local_server}/simple.html"})
    assert call("text", {"selector": "#counter"})["text"] == "0"
    call("humanize_on")
    try:
        r = call("click", {"target": "#counter-btn"})
        assert r["humanized"] is True
        call("click", {"target": "#counter-btn"})
        call("click", {"target": "#counter-btn"})
        assert call("text", {"selector": "#counter"})["text"] == "3"
    finally:
        call("humanize_off")


def test_humanized_type_produces_correct_value(local_server):
    """humanize ON: typed text lands in the right field with the right value."""
    call("go", {"url": f"{local_server}/simple.html"})
    call("humanize_on")
    try:
        r = call("type", {"target": "#q", "text": "hello world"})
        assert r.get("humanized") is True
        assert call("value", {"selector": "#q"})["value"] == "hello world"
        call("click", {"target": "#submit"})
        call("sleep", {"ms": 200})
        assert call("text", {"selector": "#result"})["text"] == "hello world"
    finally:
        call("humanize_off")


def test_humanize_off_click_unchanged(local_server):
    """Regression guard: with humanize OFF the click path is the plain one."""
    call("go", {"url": f"{local_server}/simple.html"})
    call("humanize_off")
    r = call("click", {"target": "#counter-btn"})
    assert r["humanized"] is False
    assert call("text", {"selector": "#counter"})["text"] == "1"


# ─── live-DOM proof (real Chrome, not the _FakePage) ────────────────────
# The ≥30-point assertions above run against _FakePage. These confirm the path
# and timing survive to a REAL browser DOM — closing the gap where a no-op could
# pass the fake-page count yet teleport on a live page.


def _val(res):
    return res.get("value", res) if isinstance(res, dict) else res


def test_humanized_click_traces_a_real_path_on_live_dom(local_server):
    """A humanized click dispatches MANY intermediate mousemove events on the
    actual page (a Bezier path), where a teleport would dispatch ~1. Compares
    humanize ON vs OFF on the same page so the delta is the proof."""
    call("go", {"url": f"{local_server}/simple.html"})
    install = """(() => {
        window.__moves = [];
        document.addEventListener('mousemove',
            e => window.__moves.push([e.clientX, e.clientY]), {passive: true});
        return true;
    })()"""
    count = "window.__moves.length"
    distinct = "new Set(window.__moves.map(p => p.join(','))).size"

    # humanize OFF baseline — plain Playwright click is at most a couple moves.
    call("humanize_off")
    call("eval", {"expr": install})
    call("click", {"target": "#counter-btn"})
    off_n = _val(call("eval", {"expr": count}))

    # humanize ON — Bezier path.
    call("humanize_on")
    try:
        call("eval", {"expr": install})  # reset recorder
        r = call("click", {"target": "#counter-btn"})
        assert r["humanized"] is True
        on_n = _val(call("eval", {"expr": count}))
        on_distinct = _val(call("eval", {"expr": distinct}))
    finally:
        call("humanize_off")

    assert on_n >= 8, (
        f"humanized click dispatched only {on_n} mousemove events on the live "
        f"DOM — a real Bezier path should produce many (teleport regression?)")
    assert on_distinct >= 5, f"only {on_distinct} distinct points — not a path"
    assert on_n > off_n, (
        f"humanize ON ({on_n}) did not produce more moves than OFF ({off_n}) — "
        f"the humanized path isn't reaching the real browser")


def test_humanized_typing_has_varied_interkey_timing_on_live_dom(local_server):
    """Humanized typing produces non-zero, non-constant inter-keystroke delays
    on the real DOM — heavy-tailed cadence, not an instant bulk-set."""
    import statistics
    call("go", {"url": f"{local_server}/simple.html"})
    call("eval", {"expr": """(() => {
        window.__keys = [];
        document.querySelector('#q').addEventListener(
            'keydown', () => window.__keys.push(performance.now()));
        return true;
    })()"""})
    call("humanize_on")
    try:
        call("type", {"target": "#q", "text": "hello world typing"})
    finally:
        call("humanize_off")
    deltas = _val(call("eval", {"expr": """(() => {
        const t = window.__keys, d = [];
        for (let i = 1; i < t.length; i++) d.push(t[i] - t[i - 1]);
        return d;
    })()"""}))
    assert isinstance(deltas, list) and len(deltas) >= 10, (
        f"too few keydown intervals ({len(deltas) if isinstance(deltas, list) else deltas}) "
        f"— per-char dispatch may not be reaching the DOM")
    assert min(deltas) > 0, "inter-key delays are 0 — not humanized timing (bulk set?)"
    assert statistics.pstdev(deltas) > 1, (
        f"inter-key timing is ~constant (stdev={statistics.pstdev(deltas):.2f}ms) "
        f"— humanized cadence isn't being applied")


# ─── motion/typing actually runs (stub page — no browser) ───────────────
# These mutation-catch a humanize path that silently no-ops: the integration
# tests above would stay green (a plain click also increments the counter), so
# we assert the Bezier moves / per-char keystrokes really happen.

class _FakeMouse:
    def __init__(self):
        self.moves = 0

    async def move(self, x, y):
        self.moves += 1


class _FakeKeyboard:
    def __init__(self):
        self.typed = []

    async def type(self, ch):
        self.typed.append(ch)


class _FakePage:
    def __init__(self):
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()


class _FakeLocator:
    def __init__(self, box):
        self._box = box
        self.focused = False

    async def scroll_into_view_if_needed(self, timeout=None):
        pass

    async def bounding_box(self):
        return self._box

    async def focus(self, timeout=None):
        self.focused = True


async def test_approach_actually_moves_and_targets_inside_box():
    from vibatchium.humanize import humanized_locator_approach
    page = _FakePage()
    loc = _FakeLocator({"x": 100, "y": 100, "width": 80, "height": 30})
    pos = await humanized_locator_approach(page, loc)
    assert page.mouse.moves >= 30            # the Bezier path really ran
    assert pos is not None
    x, y = pos
    assert 100 <= x <= 180 and 100 <= y <= 130   # landed inside the element


async def test_approach_no_box_falls_back_to_none():
    from vibatchium.humanize import humanized_locator_approach
    page = _FakePage()
    pos = await humanized_locator_approach(page, _FakeLocator(None))
    assert pos is None
    assert page.mouse.moves == 0             # nothing to approach → caller just clicks


async def test_humanized_type_dispatches_each_char_incl_unicode():
    from vibatchium.humanize import humanized_type
    page = _FakePage()
    loc = _FakeLocator({"x": 0, "y": 0, "width": 10, "height": 10})
    await humanized_type(page, loc, "aé🎉")
    assert loc.focused is True
    assert page.keyboard.typed == ["a", "é", "🎉"]


async def test_humanized_type_empty_is_noop():
    from vibatchium.humanize import humanized_type
    page = _FakePage()
    loc = _FakeLocator(None)
    await humanized_type(page, loc, "")
    assert loc.focused is True
    assert page.keyboard.typed == []


def test_budgeted_type_delays_bounds_long_text():
    from vibatchium.humanize import budgeted_type_delays
    assert budgeted_type_delays(0) == []
    assert 600 < sum(budgeted_type_delays(10, seed=1)) < 2500     # ~full cadence + tail
    long = budgeted_type_delays(1000, seed=1)
    assert len(long) == 1000
    assert sum(long) <= 20000          # bounded to the RPC budget
    # the overshoot regime the naive max(5, int(d*scale)) re-floor blew past: a
    # >4000-char type once summed 100s+ (n=20000 -> 100000ms). The total must stay
    # within budget across the whole feasible range (n <= max_total_ms).
    for n in (5000, 20000):
        d = budgeted_type_delays(n, seed=1)
        assert len(d) == n and sum(d) <= 20000, f"n={n} sum={sum(d)}"
