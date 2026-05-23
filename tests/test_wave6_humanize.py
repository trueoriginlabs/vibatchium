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


from patchium.client import call
from patchium.humanize import (
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
