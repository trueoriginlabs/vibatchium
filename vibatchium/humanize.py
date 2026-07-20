"""Wave 6.2b — humanization budget.

Bezier mouse trajectories, gaussian-sampled dwell times, sinusoidal scroll
inertia. Opt-in per session via `vb humanize on`. Off by default
because:
  1. Bad humanization is worse than none (perfectly-symmetric Bezier curves
     are more detectable than a straight line).
  2. Existing tests assume instantaneous clicks; default-on would slow CI.
  3. Defenders that fingerprint mouse *behavior* — trajectory shape and
     inter-event timing (DataDome, PerimeterX) — are opt-in worth-it targets,
     not always-on.

SCOPE (be honest): this improves trajectory + timing biometrics only. It still
dispatches through `page.mouse`/`page.keyboard` over CDP `Input.*`, so every
injected event lacks the raw pointer stream real hardware produces: no
`pointerrawupdate` events and no `CoalescedEvents` batched into each `pointermove`
(both measured absent by the behavioural oracle — vibatchium/oracle.py). It does
NOT close that per-event gap — for walls that fingerprint it, the answer is
attach-mode against a real headful Chrome (see README "Honest limits"), not
synthetic input. (The `pageX==screenX` coordinate tell once assumed here was
measured NOT to fire: CDP input carries a real screen offset — see oracle.py
`screen_eq_client`.)

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
        # Tiny inter-step delay; total move time scales with distance + n_points.
        # NB: jittering this does NOT change the timing a page observes — Chrome
        # re-emits injected moves on its ~60Hz compositor clock, so `pointermove`
        # inter-event dt is fixed at the refresh interval regardless (measured via
        # the behavioural oracle). The real synthetic tell is upstream: CDP input
        # produces no `pointerrawupdate`/coalesced raw stream at all (see oracle.py).
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


# ─── element-aware humanization (safe: motion only, click stays verified) ──
#
# The cardinal rule for clicking by ELEMENT (vs raw coords): humanization must
# only add the *approach* (movement) and *timing* (dwell). The actual click is
# always Playwright's `locator.click()`, which re-resolves + hit-tests the
# target the instant before dispatch. So a page that reflows mid-animation can
# never make us click the wrong element — the worst case is the cursor ends a
# few px off and Playwright clicks the correct element anyway. We NEVER dispatch
# a bare mouse.down at a coordinate that hasn't been hit-tested against the
# resolved element.


def point_in_box(box: dict, *, rng: random.Random | None = None
                 ) -> tuple[float, float]:
    """A jittered click point inside the element's inner ~60% — gaussian around
    center, hard-clamped to ±30% of each half-extent so we stay well clear of
    the edges (and never stray onto an adjacent element)."""
    r = rng or random
    cx = box["x"] + box["width"] / 2
    cy = box["y"] + box["height"] / 2
    # Jitter ~12% of each extent, clamped to ±30% of the half-extent AND an
    # absolute 64px — so on a huge hit-area we still aim near the centre, not
    # 1000px off (which neither looks human nor helps).
    cap_x = min(box["width"] * 0.3, 64)
    cap_y = min(box["height"] * 0.3, 64)
    ox = max(-cap_x, min(cap_x, r.gauss(0, box["width"] * 0.12)))
    oy = max(-cap_y, min(cap_y, r.gauss(0, box["height"] * 0.12)))
    return (cx + ox, cy + oy)


async def humanized_locator_approach(page, loc, *,
                                     cursor: tuple[float, float] | None = None
                                     ) -> tuple[float, float] | None:
    """Humanize the APPROACH to a Playwright locator — without clicking it.

    Scrolls the element into view, reads its CURRENT bounding box, moves the
    mouse along a Bezier path to a jittered interior point, and pauses briefly
    (pre-click hover). Returns the new cursor position, or None if the element
    has no usable box (caller should then just click directly).

    Deliberately does NOT click: the caller dispatches Playwright's verified
    `locator.click()` afterward, so correctness is identical to a normal click.
    """
    try:
        await loc.scroll_into_view_if_needed(timeout=5000)
    except Exception:  # noqa: BLE001
        pass  # non-fatal — loc.click() will scroll again if it needs to
    box = await loc.bounding_box()
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        return None
    tx, ty = point_in_box(box)
    await humanized_move(page, tx, ty, start=cursor)
    await asyncio.sleep(random.uniform(0.05, 0.15))  # settle on the element
    return (tx, ty)


def humanized_type_delays(n: int, *, median_ms: float = 105, sigma: float = 0.5,
                          floor_ms: float = 22, ceiling_ms: float = 1200,
                          pause_prob: float = 0.09,
                          seed: int | None = None) -> list[int]:
    """Per-keystroke inter-key delays (ms), RIGHT-SKEWED like real keystroke
    dynamics — a log-normal body plus occasional long hesitations — not a tight
    gaussian.

    Real inter-key timing is heavy-tailed: quick bursts punctuated by
    word-boundary / thinking / hard-reach pauses, so the within-phrase stdev is
    on the order of the mean. A symmetric gaussian (the old model, stdev≈45ms)
    reads as metronomic to a behavioural scorer — the self-hosted oracle measured
    humanize at stdev~45ms against a real operator's 90-240ms and flagged it. This
    draws a log-normal around `median_ms` (spread `sigma`) and gives `pause_prob`
    of the keys a 2.5-5x hesitation, so the distribution matches (stdev ≈ mean,
    long right tail). Pure + deterministic with `seed`.
    """
    r = random.Random(seed) if seed is not None else random
    mu = math.log(max(1.0, median_ms))
    out = []
    for _ in range(n):
        d = r.lognormvariate(mu, sigma)
        if r.random() < pause_prob:
            d *= r.uniform(2.5, 5.0)          # word-boundary / thinking pause
        out.append(int(max(floor_ms, min(ceiling_ms, d))))
    return out


def budgeted_type_delays(n: int, *, max_total_ms: float = 20000,
                         base_median_ms: float = 105, seed: int | None = None
                         ) -> list[int]:
    """Heavy-tailed inter-key delays for `n` chars whose total stays within
    `max_total_ms`.

    Short text types at natural cadence. For a big field the median shrinks so it
    fits the budget, and if the sampled tail still overshoots the whole sequence
    is scaled down proportionally (preserving the shape) — otherwise a long type
    could exceed the daemon's RPC timeout and surface as an opaque socket error
    mid-type.
    """
    if n <= 0:
        return []
    # Shrink the median for very long fields; leave ~30% headroom under the budget
    # for the heavy tail before the proportional scale-to-fit kicks in.
    median = max(1.0, min(base_median_ms, max_total_ms / n * 0.7))
    delays = humanized_type_delays(n, median_ms=median, seed=seed)
    total = sum(delays)
    if total <= max_total_ms:
        return delays
    # Overshoot → scale to fit with a hard 1ms/key floor. Scale only the mass ABOVE
    # the floor, so the total lands ON the budget and int() (truncating down) keeps
    # it there. The earlier `max(5, int(d*scale))` re-floored sub-5ms keys back UP
    # after scaling, so once 5*n exceeded the budget (n≳4000) no single pass fit and
    # the sum grew unbounded — a >4000-char type could sleep 100s+, past the RPC
    # timeout the budget exists to respect. If even 1ms/key can't fit (absurdly long
    # field), that floor is the best achievable.
    floor = 1
    if n * floor >= max_total_ms:
        return [floor] * n
    scale = (max_total_ms - n * floor) / (total - n * floor)
    return [floor + int((d - floor) * scale) for d in delays]


async def humanized_type(page, loc, text: str, *, timeout_ms: int = 30000) -> None:
    """Type `text` into `loc` with heavy-tailed inter-key timing.

    Focuses the validated element first, then dispatches one character at a
    time, with the total time bounded (see `budgeted_type_delays`). Keys are
    sent to the focused element — same targeting as `press_sequentially`, so
    the typed value is unchanged; on a page that actively steals focus
    mid-type they could land elsewhere (same exposure as `press_sequentially`,
    marginally widened by the inter-key pauses).
    """
    await loc.focus(timeout=timeout_ms)
    for ch, delay_ms in zip(text, budgeted_type_delays(len(text)), strict=True):
        await page.keyboard.type(ch)
        await asyncio.sleep(delay_ms / 1000)
