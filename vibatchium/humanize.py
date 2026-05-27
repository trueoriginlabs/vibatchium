"""Wave 6.2b — humanization budget.

Bezier mouse trajectories, gaussian-sampled dwell times, sinusoidal scroll
inertia. Opt-in per session via `vb humanize on`. Off by default
because:
  1. Bad humanization is worse than none (perfectly-symmetric Bezier curves
     are more detectable than a straight line).
  2. Existing tests assume instantaneous clicks; default-on would slow CI.
  3. Defenders that fingerprint mouse behavior (DataDome, PerimeterX) are
     opt-in worth-it targets, not always-on.

Pure functions (no Playwright dependency) — the orchestration in
`_mouse` handler awaits these in a coroutine that drives Playwright APIs.

Acceptance:
- Click trajectory: ≥30 intermediate mouse.move events with non-linear path
  (sum of segment angles deviates from straight line by >0.1 rad)
- Dwell-time stdev > 15 ms across 20 clicks (proves it's not constant)
- sannysoft score unchanged with humanize on (humanization shouldn't break
  property-based detectors)
- Per-click latency ≤ 1.5 s for typical distances (≤500px)
"""
from __future__ import annotations

import asyncio
import math
import random


def humanized_path(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    n_points: int | None = None,
    seed: int | None = None,
) -> list[tuple[float, float]]:
    """Cubic-Bezier mouse path from `start` to `end` with jittered control points.

    Generates an organic, non-straight trajectory by placing two control
    points off-axis from the line between start and end. Jitter direction
    and magnitude are randomized per call so successive moves don't look
    identical.

    `n_points` defaults to `max(30, min(200, distance/5))` so short moves
    don't get noisily oversampled and long ones stay smooth.

    `seed` is for deterministic tests.
    """
    if seed is not None:
        rng = random.Random(seed)
    else:
        rng = random
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    distance = math.hypot(dx, dy)
    if n_points is None:
        n_points = max(30, min(200, int(distance / 5) or 30))

    # Perpendicular unit vector for control-point bias
    if distance > 0:
        perp_x, perp_y = -dy / distance, dx / distance
    else:
        perp_x = perp_y = 0
    # Jitter control points ~5-15% of distance off-axis
    jitter = distance * rng.uniform(0.05, 0.15)
    bias = rng.choice([-1, 1])
    # Place control points at ~30% and ~70% along the line, biased perpendicular
    cp1_t = rng.uniform(0.2, 0.4)
    cp2_t = rng.uniform(0.6, 0.8)
    cp1 = (sx + dx * cp1_t + perp_x * jitter * bias,
           sy + dy * cp1_t + perp_y * jitter * bias)
    cp2 = (sx + dx * cp2_t + perp_x * jitter * bias * 0.7,
           sy + dy * cp2_t + perp_y * jitter * bias * 0.7)

    pts = []
    for i in range(n_points):
        t = i / (n_points - 1) if n_points > 1 else 0.5
        ot = 1 - t
        x = (ot ** 3 * sx + 3 * ot * ot * t * cp1[0]
             + 3 * ot * t * t * cp2[0] + t ** 3 * ex)
        y = (ot ** 3 * sy + 3 * ot * ot * t * cp1[1]
             + 3 * ot * t * t * cp2[1] + t ** 3 * ey)
        pts.append((x, y))
    return pts


def path_curviness(path: list[tuple[float, float]]) -> float:
    """Sum of absolute angle changes along the path (radians). 0 = straight line.

    Used by tests to verify the Bezier path actually curves (catches bugs
    where the path would degenerate to straight).
    """
    if len(path) < 3:
        return 0.0
    total = 0.0
    prev_angle = None
    for i in range(len(path) - 1):
        dx = path[i + 1][0] - path[i][0]
        dy = path[i + 1][1] - path[i][1]
        if dx == 0 and dy == 0:
            continue
        angle = math.atan2(dy, dx)
        if prev_angle is not None:
            diff = abs(angle - prev_angle)
            # Wrap to [-π, π]
            if diff > math.pi:
                diff = 2 * math.pi - diff
            total += diff
        prev_angle = angle
    return total


def sample_dwell_ms(mean_ms: float = 100, stdev_ms: float = 25,
                    floor_ms: float = 40, ceiling_ms: float = 250) -> int:
    """Gaussian-sampled mouse-button-down dwell time, clamped to a realistic
    human range (~40-250 ms; mean ~100, stdev ~25)."""
    val = random.gauss(mean_ms, stdev_ms)
    return int(max(floor_ms, min(ceiling_ms, val)))


def sinusoidal_scroll(total_dy: float, *, duration_ms: float = 300,
                       tick_ms: float = 16) -> list[tuple[float, float]]:
    """Break a single scroll-wheel into stepped events whose deltas follow a
    sin-curve — mimics a real mouse-wheel flick (fast onset → peak → decay).

    Returns list of (dt_ms, dy_px) tuples. Cumulative sum of dy_px equals
    `total_dy` (correction applied to the last step).
    """
    n_steps = max(1, int(duration_ms / tick_ms))
    if n_steps == 1:
        return [(duration_ms, total_dy)]
    steps = []
    cumulative = 0.0
    for i in range(n_steps):
        t = (i + 0.5) / n_steps
        amplitude = math.sin(t * math.pi)  # peaks at t=0.5
        step_dy = total_dy * amplitude * (2 / n_steps)
        steps.append((tick_ms, step_dy))
        cumulative += step_dy
    # Correct any rounding drift onto the last step
    drift = total_dy - cumulative
    if abs(drift) > 1e-6:
        last_ms, last_dy = steps[-1]
        steps[-1] = (last_ms, last_dy + drift)
    return steps


# ─── Playwright orchestration ──────────────────────────────────────────


async def humanized_move(page, end_x: float, end_y: float,
                          *, start: tuple[float, float] | None = None,
                          step_delay_ms: float = 5.0) -> None:
    """Move the mouse along a Bezier path to (end_x, end_y).

    `start` defaults to a small offset from (0,0) — Playwright doesn't expose
    cursor position, so we fake a starting point. After the first move, the
    real cursor is at the destination; subsequent moves originate from there
    (caller should track and pass `start`).
    """
    if start is None:
        start = (random.uniform(10, 100), random.uniform(10, 100))
    path = humanized_path(start, (end_x, end_y))
    for px, py in path:
        await page.mouse.move(px, py)
        # Tiny inter-step delay; total move time scales with distance + n_points
        await asyncio.sleep(step_delay_ms / 1000)


async def humanized_click(page, x: float, y: float, *,
                           button: str = "left",
                           cursor_pos: tuple[float, float] | None = None
                           ) -> tuple[float, float]:
    """Move humanlike to (x,y), pause briefly, mouse-down, dwell, mouse-up.
    Returns the new cursor position."""
    await humanized_move(page, x, y, start=cursor_pos)
    # Pre-click hover
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.down(button=button)
    await asyncio.sleep(sample_dwell_ms() / 1000)
    await page.mouse.up(button=button)
    return (x, y)


async def humanized_scroll(page, dx: float, dy: float, *,
                            duration_ms: float = 300) -> None:
    """Send multiple scroll-wheel events that follow a sin curve — looks like
    a real mouse-wheel flick, not a single jump."""
    if dx == 0 and dy == 0:
        return
    steps = sinusoidal_scroll(dy, duration_ms=duration_ms) if dy else [(duration_ms, 0)]
    for dt_ms, step_dy in steps:
        await page.mouse.wheel(dx / max(1, len(steps)), step_dy)
        await asyncio.sleep(dt_ms / 1000)
