"""Ambient behaviour — low-rate, human-plausible pointer activity BETWEEN verbs.

WHY. `humanize` shapes input DURING a verb (Bezier approach, dwell, cadence). But
between verbs a vibatchium page sees nothing at all: no pointer moves, no wheel,
a perfectly still cursor — for as long as the agent thinks. That is exactly the
signal 2026's session-lifetime behavioural scorers key on:

  * Akamai (2026, "Identifying agentic automation with behavioral telemetry"):
    "63.2% of agentic autopost requests contained 0 mouse events", a further
    35.8% fell below their minimum event count; agents don't "move the cursor
    idly while reading", don't "hover before clicking", don't "scroll to
    explore". Their mouse-telemetry classifier reports ROC-AUC 0.981.
  * Cloudflare Precursor (2026): scores "pointer movement, keyboard activity,
    focus changes, and visibility", session-scoped ("a bot cannot reset its
    behavioral signature by refreshing"); human paths form "an arc, limited by
    the range of the wrist" with "small corrections and overshoots", where bots
    show "linear interpolations or mathematically ideal Bézier curves".

WHAT. Opt-in per session (`vb --session X humanize ambient on`, default OFF).
While the session is idle between verbs, a per-session asyncio task emits
occasional bursts through the SAME channel humanize uses (Playwright
`page.mouse` → CDP `Input.dispatchMouseEvent`):

  micro   1-3 px resting-hand corrections
  drift   short wrist-arc drifts near the cursor, horizontally biased (reading)
  reposition  a move onto another visible text block
  scroll  1-3 wheel notches in reading rhythm (mostly down), document only

Paths are minimum-jerk (Flash & Hogan 1985 bell-shaped velocity → decelerates
into the landing point), with an ASYMMETRIC bow (not a symmetric Bezier),
8-12 Hz physiological tremor, occasional overshoot + corrective sub-move,
integer CSS-pixel coordinates (a DPR-1 mouse never reports 150.3999) and
~60 Hz sample spacing. Shapes are built to land inside the oracle's literature
bands (vibatchium/oracle.py HUMAN_BASELINE: ≥8 samples per deliberate move,
straightness 0.55-0.999, decel_ratio < 0.95). The RATES (burst gaps, drift
size, scroll odds) are NOT from a published human distribution — none exists
for idle-between-actions pointer activity — they are heuristic and seeded per
session; `vb oracle ambient` measures what they produce.

SAFETY CONTRACT (each enforced in code below, and tested):
  * Never clicks, presses a button, types, selects, or navigates — the only
    calls are `page.mouse.move` and `page.mouse.wheel`.
  * Never acts during a verb. The dispatcher calls `verb_begin()` before ANY
    session-scoped verb, the page waits and every non-registry plugin verb
    (an `unlocked` one included — it holds no lock), once the lease/goal-caps
    gates have let it through; that cancels an in-flight burst
    and parks the loop. Ambient NEVER takes `entry.lock`; instead, before every
    single CDP dispatch, `_blocked()` re-checks (synchronously — no await
    between check and dispatch) that no verb is in flight, `entry.lock` is free,
    no unlocked page-wait is running, the session isn't idle-frozen, no liveview
    takeover is attached, and no mouse button is held (a move with a button held
    would be a drag/selection). At most one already-dispatched move can land
    after a verb begins, and it lands BEFORE the verb's own input (CDP order).
  * Never keeps a parked session awake: ambient never stamps `last_used_at`
    (it uses registry.peek, never get), so the idle-freezer still freezes at
    VIBATCHIUM_IDLE_FREEZE_AFTER; a frozen session is a hard stop. Independently
    ambient stops after an idle HORIZON (default 180 s since the last verb,
    `VIBATCHIUM_AMBIENT_HORIZON`) — the bound when idle-freeze is off/ineligible.
  * Safe targets only: every path point is hit-tested in the page (isolated
    world — invisible to page JS) with elementFromPoint, piercing open shadow
    roots, and rejected if it or any ancestor is interactive (links, buttons,
    form controls, labels, summary/details, nav, iframes, media, canvas, svg),
    has an interactive ARIA role, inline mouse/pointer handlers, tabindex,
    contenteditable, aria-haspopup/expanded, a pointer/grab cursor, or sits
    within 40 px of the top edge (exit-intent) / 8 px of the others. A leading
    run of points still on the element the pointer already rests on (a plain
    click left it on a button) is allowed — that element is already hovered.
    Wheel is only sent when the wheel point is safe, nothing between it and the
    document is an inner scroller (so it can never scroll a <select>, textarea,
    map or chat pane), the document has room, AND every point that will slide
    under the still pointer during the scroll (sampled every 8 px) is safe —
    Chrome re-runs hover after a scroll, so content arriving under the cursor
    gets a real mouseover. If the column under the cursor isn't clean, the hand
    first moves onto a text block, then scrolls; otherwise no scroll.
  * No motion at all after a pointer-parking verb (`hover`, `focus`, `mouse
    move`) until the next verb that navigates or acts: the agent left the
    pointer there on purpose — on a hover-opened menu or card — and any drift
    or scroll could close it before the click that was coming.
  * Scroll is suppressed after a viewport-pinning verb (screenshot, candidates,
    vision_find, mouse …) until the next verb that navigates or acts — reads in
    between (text, extract, eval, url, waits, `humanize_ambient status`) keep the
    pin — so an agent that read coordinates off a screenshot never clicks into a
    page ambient scrolled. And because a wheel's scroll lands ~10 ms after its
    CDP call returns, coordinate verbs (screenshot, mouse, candidates, vision_*)
    wait until the last notch is >= 50 ms old before running.
  * No teleports: every burst starts from where the DOCUMENT last saw the
    pointer — an isolated-world listener (invisible to page JS) records the
    last pointer position from any source, including a plain Playwright click
    that parked the pointer on an element centre without telling us. (`:hover`
    can't answer this: measured empty in headless Chrome after plain moves and
    locator clicks.) Humanize and ambient share one per-page cursor.
  * Visible + focused pages only; only the session's ACTIVE page; headless
    launch-mode patchright only (never attach — that browser is the user's
    own — and never a headed window, which a human may be using).

HONEST LIMIT. This is CDP-synthesised input: it fires NO `pointerrawupdate`
events and carries no coalesced samples (`getCoalescedEvents`) — the raw-pointer
gap the oracle measures as `raw_pointer_events` / `coalesced_max` is unchanged
by ambient. It fills the *silence* between actions; it does not make the events
it emits indistinguishable from hardware.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import random
import time
import weakref
from dataclasses import asdict, dataclass, field

log = logging.getLogger("vibatchium.daemon")

DEFAULT_HORIZON_S = 180.0
MIN_HORIZON_S = 5.0
MAX_HORIZON_S = 1800.0
FRAME_MS = 1000.0 / 60.0          # ~60 Hz pointer sampling (oracle n_moves source)
CDP_TIMEOUT_S = 2.0               # cap on any single probe / input call
TOP_MARGIN_PX = 40                # stay clear of the exit-intent top edge
EDGE_MARGIN_PX = 8

# Verbs after which the agent may hold viewport coordinates it intends to click
# into (it read a screenshot / bbox). Ambient keeps pointer drift but suppresses
# SCROLL until the next verb outside this set, so those coordinates stay valid.
VIEWPORT_PINNING_VERBS = frozenset({
    "screenshot", "screenshot_annotate", "candidates", "vision_find",
    "mouse", "highlight", "map", "map_compact", "diff_map",
})

# A CDP wheel's scroll is applied by the compositor AFTER Input.dispatchMouseEvent
# returns (~9-10 ms measured in the 0.20.0 review), so a notch cancelled by
# verb_begin can still move the page under a verb that has already started.
# Verbs that read or act on viewport COORDINATES wait until the last notch is
# at least this old before they run.
WHEEL_SETTLE_S = 0.05
COORDINATE_VERBS = frozenset({
    "screenshot", "screenshot_annotate", "mouse", "candidates", "highlight",
    "vision_click", "vision_find", "vision_type",
})

# Verbs whose PURPOSE is to leave the pointer (or focus) where it is: a hover
# that opened a CSS/JS menu or hover card, a `mouse move` that parked the
# pointer, a focus that opened a :focus-within dropdown. Any ambient motion
# after one of these can close what the agent just opened before its next
# click, so ambient makes NO pointer motion or scroll at all until the next
# verb that navigates or acts (reads in between keep the pointer parked).
# `mouse` parks only for action=move (see pointer_parks()).
POINTER_PARKING_VERBS = frozenset({"hover", "focus"})


def pointer_parks(cmd: str, args: dict | None = None) -> bool:
    if cmd in POINTER_PARKING_VERBS:
        return True
    return cmd == "mouse" and str((args or {}).get("action") or "").lower() == "move"


# Verbs that only READ (the page, or daemon/session state). They leave
# ambient's suppression state exactly as they found it: the usual agent loop is
# screenshot → extract/text/eval → coordinate click, and a read in the middle
# must not hand the viewport back to ambient scroll between the screenshot and
# the click. Everything else — navigation, input, page/frame switches, plugin
# verbs — clears the scroll pin.
READ_ONLY_VERBS = frozenset({
    # page content / DOM reads
    "text", "html", "extract", "extract_fields", "eval", "attr", "value",
    "count", "find", "is_state", "url", "title", "frames", "pages",
    "detect_forms", "observe", "pdf", "expect", "fingerprint",
    # the pinning reads (they set the pin; they must not clear a park either)
    "screenshot", "screenshot_annotate", "candidates", "vision_find",
    "highlight", "map", "map_compact", "diff_map",
    # waits
    "wait_selector", "wait_ref", "wait_url", "wait_load", "wait_fn",
    "wait_response",
    # session-state reads
    "console_dump", "network_dump", "route_list", "handle_list",
    "download_list", "cookies", "storage_export", "humanize_status",
    "humanize_ambient", "vision_stats", "vision_budget",
})


def ambient_horizon(raw=None) -> float:
    """Idle seconds after the last verb before ambient goes quiet. An explicit
    value wins over VIBATCHIUM_AMBIENT_HORIZON; clamped to [5, 1800]."""
    if raw is None or raw == "":
        raw = os.environ.get("VIBATCHIUM_AMBIENT_HORIZON", "")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_HORIZON_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        log.warning("ambient: bad horizon %r — using default %s", raw,
                    DEFAULT_HORIZON_S)
        return DEFAULT_HORIZON_S
    if not math.isfinite(val):
        return DEFAULT_HORIZON_S
    return max(MIN_HORIZON_S, min(MAX_HORIZON_S, val))


# ─── seeding + per-session parameters (PURE) ─────────────────────────────────


def _machine_id() -> str:
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p, encoding="ascii") as fh:
                return fh.read().strip()
        except OSError:
            continue
    return ""


def session_seed(name: str, profile_dir, *, machine_id: str | None = None) -> int:
    """Stable 64-bit seed for one session on one box: the same session keeps its
    rhythm across daemon restarts, while two sessions — or the same session name
    on two hosts — get different rhythms (fleet de-twinning)."""
    mid = _machine_id() if machine_id is None else machine_id
    h = hashlib.sha256(f"vb-ambient\0{mid}\0{profile_dir}\0{name}".encode())
    return int.from_bytes(h.digest()[:8], "big")


@dataclass(frozen=True)
class AmbientParams:
    """One session's behavioural 'personality'. Drawn once from the seed."""
    seed: int
    first_delay_lo_s: float      # reaction time before the first burst after a verb
    first_delay_hi_s: float
    gap_median_s: float          # median idle seconds between bursts
    gap_sigma: float             # log-normal spread of those gaps
    settle_s: float              # gaps stretch as idle grows: gap *= 1 + idle/settle_s
    moves_continue_p: float      # P(another move in the same burst), geometric
    intra_pause_median_ms: float
    drift_median_px: float
    speed_px_s: float            # mean casual (non-target-directed) pointer speed
    base_ms: float               # fixed motor latency added to every move
    tremor_px: float             # 8-12 Hz physiological tremor amplitude
    overshoot_p: float
    micro_p: float
    reposition_p: float
    scroll_p: float
    scroll_down_p: float
    notch_px: int
    notch_interval_median_ms: float

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("seed", None)
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in d.items()}


def derive_params(seed: int) -> AmbientParams:
    r = random.Random(seed)
    lo = r.uniform(0.25, 0.6)
    return AmbientParams(
        seed=seed,
        first_delay_lo_s=lo,
        first_delay_hi_s=lo + r.uniform(0.5, 1.2),
        gap_median_s=r.uniform(3.0, 7.0),
        gap_sigma=r.uniform(0.45, 0.8),
        settle_s=r.uniform(45.0, 120.0),
        moves_continue_p=r.uniform(0.3, 0.55),
        intra_pause_median_ms=r.uniform(150.0, 400.0),
        drift_median_px=r.uniform(12.0, 45.0),
        speed_px_s=r.uniform(250.0, 700.0),
        base_ms=r.uniform(70.0, 140.0),
        tremor_px=r.uniform(0.3, 1.0),
        overshoot_p=r.uniform(0.1, 0.3),
        micro_p=r.uniform(0.15, 0.35),
        reposition_p=r.uniform(0.10, 0.25),
        scroll_p=r.uniform(0.08, 0.20),
        scroll_down_p=r.uniform(0.75, 0.92),
        notch_px=100,
        notch_interval_median_ms=r.uniform(40.0, 90.0),
    )


def next_gap_s(params: AmbientParams, rng: random.Random, idle_for_s: float,
               *, first: bool) -> float:
    """Seconds until the next burst. The first burst after a verb follows a short
    reaction delay; later ones are log-normal and stretch as the idle grows (a
    reader settles)."""
    if first:
        return rng.uniform(params.first_delay_lo_s, params.first_delay_hi_s)
    g = rng.lognormvariate(math.log(params.gap_median_s), params.gap_sigma)
    g *= 1.0 + max(0.0, idle_for_s) / params.settle_s
    return max(0.6, min(30.0, g))


def _min_jerk(tau: float) -> float:
    """Minimum-jerk position profile (Flash & Hogan 1985): bell-shaped velocity,
    zero velocity + acceleration at both ends."""
    return tau ** 3 * (10 - 15 * tau + 6 * tau * tau)


def _segment(start, end, rng: random.Random, params: AmbientParams,
             *, dur_ms: float | None = None) -> list[tuple[float, float, float]]:
    sx, sy = start
    ex, ey = end
    dx, dy = ex - sx, ey - sy
    dist = math.hypot(dx, dy)
    if dist < 0.5:
        return []
    if dur_ms is None:
        dur_ms = (params.base_ms + dist / params.speed_px_s * 1000.0) * rng.uniform(0.85, 1.2)
    n = max(2, int(round(dur_ms / FRAME_MS)))
    px, py = -dy / dist, dx / dist
    # Asymmetric bow: peak displaced off-centre by `skew`, magnitude a few % of D.
    bow = dist * rng.uniform(0.02, 0.10) * rng.choice((-1.0, 1.0))
    skew = rng.uniform(0.7, 1.4)
    freq = rng.uniform(8.0, 12.0)
    phase = rng.uniform(0.0, 2 * math.pi)
    out = []
    t_ms = 0.0
    for i in range(1, n + 1):
        tau = i / n
        dt = FRAME_MS * rng.uniform(0.9, 1.1)
        t_ms += dt
        s = _min_jerk(tau)
        off = bow * math.sin(math.pi * tau ** skew)
        # Tremor fades as the hand lands so the endpoint is the endpoint.
        trem = (params.tremor_px * math.sin(2 * math.pi * freq * t_ms / 1000.0 + phase)
                + rng.gauss(0.0, params.tremor_px * 0.3)) * (1.0 - tau)
        out.append((sx + dx * s + px * (off + trem), sy + dy * s + py * (off + trem), dt))
    out[-1] = (ex, ey, out[-1][2])
    return out


def plan_move(start, end, rng: random.Random, params: AmbientParams
              ) -> list[tuple[int, int, float]]:
    """Integer-pixel samples `(x, y, dt_ms_before)` from `start` (exclusive) to
    `end`. Minimum-jerk + asymmetric bow + tremor, with an occasional overshoot
    and corrective sub-move. Consecutive duplicate pixels are merged (their dt
    accumulates) — no zero-delta moves."""
    dist = math.hypot(end[0] - start[0], end[1] - start[1])
    raw: list[tuple[float, float, float]] = []
    if dist > 40 and rng.random() < params.overshoot_p:
        ux, uy = (end[0] - start[0]) / dist, (end[1] - start[1]) / dist
        k = dist * rng.uniform(0.03, 0.08)
        over = (end[0] + ux * k, end[1] + uy * k)
        raw += _segment(start, over, rng, params)
        corr = _segment(over, end, rng, params, dur_ms=rng.uniform(80.0, 160.0))
        if corr:
            # the corrective sub-movement starts after a short reaction pause
            x, y, dt = corr[0]
            corr[0] = (x, y, dt + rng.uniform(40.0, 110.0))
        raw += corr
    else:
        raw += _segment(start, end, rng, params)
    out: list[tuple[int, int, float]] = []
    last = (int(round(start[0])), int(round(start[1])))
    carry = 0.0
    for x, y, dt in raw:
        p = (int(round(x)), int(round(y)))
        carry += dt
        if p == last:
            continue
        out.append((p[0], p[1], carry))
        carry = 0.0
        last = p
    return out


def plan_micro(start, rng: random.Random) -> list[tuple[int, int, float]]:
    """A resting-hand correction: 1-3 px over 2-4 samples, slower than a move."""
    x, y = int(round(start[0])), int(round(start[1]))
    out = []
    for _ in range(rng.randint(2, 4)):
        nx = x + rng.choice((-1, 0, 1)) * rng.randint(1, 2)
        ny = y + rng.choice((-1, 0, 1))
        if (nx, ny) == (x, y):
            nx += 1
        x, y = nx, ny
        out.append((x, y, rng.uniform(16.0, 60.0)))
    return out


def drift_target(start, rng: random.Random, params: AmbientParams) -> tuple[float, float]:
    """A nearby point: log-normal distance around the session's drift size, angle
    biased horizontal (eyes — and an idle reader's hand — track lines)."""
    d = max(3.0, min(220.0, rng.lognormvariate(math.log(params.drift_median_px), 0.6)))
    if rng.random() < 0.75:
        ang = rng.gauss(0.0, 0.45) + (math.pi if rng.random() < 0.5 else 0.0)
    else:
        ang = rng.uniform(0.0, 2 * math.pi)
    return (start[0] + d * math.cos(ang), start[1] + d * math.sin(ang))


def block_point(block, rng: random.Random) -> tuple[float, float]:
    """A reading position inside a text block rect [x, y, w, h]: left-weighted
    along the line, away from the vertical edges."""
    x, y, w, h = block
    return (x + w * min(0.95, rng.betavariate(1.6, 2.6)), y + h * rng.uniform(0.25, 0.75))


def clamp_to_viewport(pt, vw: float, vh: float) -> tuple[float, float]:
    return (max(EDGE_MARGIN_PX + 1, min(vw - EDGE_MARGIN_PX - 1, pt[0])),
            max(TOP_MARGIN_PX + 1, min(vh - EDGE_MARGIN_PX - 1, pt[1])))


def scroll_column(anchor, dist: float, *, down: bool, vh: float,
                  step_px: float = 8.0) -> list[list[int]] | None:
    """The points that will slide under a still pointer at `anchor` while the page
    scrolls `dist` px (down: content at anchor.y+dist arrives at the pointer).
    Sampled every `step_px` so even a one-line link can't slip between samples.
    None if that column isn't fully on-screen (unverifiable → don't scroll)."""
    end_y = anchor[1] + dist if down else anchor[1] - dist
    if not (TOP_MARGIN_PX <= end_y <= vh - EDGE_MARGIN_PX):
        return None
    m = max(1, int(math.ceil(dist / step_px)))
    return [[int(anchor[0]), int(round(anchor[1] + (end_y - anchor[1]) * k / m))]
            for k in range(1, m + 1)]


def choose_action(params: AmbientParams, rng: random.Random, *, scroll_ok: bool) -> str:
    r = rng.random()
    if scroll_ok and r < params.scroll_p:
        return "scroll"
    r = rng.random()
    if r < params.micro_p:
        return "micro"
    if r < params.micro_p + params.reposition_p:
        return "reposition"
    return "drift"


def notch_count(rng: random.Random) -> int:
    r = rng.random()
    return 1 if r < 0.6 else (2 if r < 0.9 else 3)


# ─── in-page probes (isolated world — page JS can't observe them) ────────────

# Shared predicate: why a point is unsafe to put the pointer on (or null).
_UNSAFE_FN_JS = r"""
const __vbUnsafeTags = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','OPTION',
  'OPTGROUP','LABEL','SUMMARY','DETAILS','IFRAME','FRAME','EMBED','OBJECT','VIDEO',
  'AUDIO','CANVAS','MAP','AREA','DIALOG','MENU','NAV','SVG']);
const __vbUnsafeRoles = new Set(['button','link','menuitem','menuitemcheckbox',
  'menuitemradio','menu','menubar','tab','tablist','checkbox','radio','switch',
  'option','combobox','listbox','slider','spinbutton','textbox','searchbox',
  'treeitem','tree','grid','gridcell','tooltip','navigation','dialog',
  'alertdialog','scrollbar']);
const __vbUnsafeAttrs = ['onclick','ondblclick','onmousedown','onmouseup',
  'onmouseover','onmouseenter','onmouseout','onmouseleave','onmousemove',
  'onpointerover','onpointerenter','onpointerdown','onpointerup','onpointermove',
  'onwheel','oncontextmenu','aria-haspopup','aria-expanded','aria-controls',
  'contenteditable','draggable','tabindex','href','data-toggle','data-bs-toggle'];
const __vbUnsafeCursor = /^(pointer|grab|grabbing|move|zoom-in|zoom-out|[a-z-]*resize|crosshair|cell|copy|alias)$/;
const __vbDeep = (x, y) => {
  let el = document.elementFromPoint(x, y);
  for (let i = 0; el && el.shadowRoot && i < 8; i++) {
    const inner = el.shadowRoot.elementFromPoint(x, y);
    if (!inner || inner === el) break;
    el = inner;
  }
  return el;
};
const __vbUp = (n) => n.parentElement
  || (n.getRootNode && n.getRootNode() instanceof ShadowRoot ? n.getRootNode().host : null);
// -> null (safe) | {why, node}: the reason and the element responsible.
const __vbCulprit = (x, y, vw, vh) => {
  if (!(x >= __M && x <= vw - __M && y >= __T && y <= vh - __M))
    return {why: 'edge', node: null};
  const el = __vbDeep(x, y);
  if (!el) return {why: 'offscreen', node: null};
  for (let n = el, d = 0; n && n.nodeType === 1 && d < 40; n = __vbUp(n), d++) {
    const tag = (n.tagName || '').toUpperCase();
    if (__vbUnsafeTags.has(tag)) return {why: 'tag:' + tag.toLowerCase(), node: n};
    if (n.isContentEditable) return {why: 'contenteditable', node: n};
    const role = (n.getAttribute('role') || '').trim().toLowerCase();
    if (role && __vbUnsafeRoles.has(role)) return {why: 'role:' + role, node: n};
    for (const a of __vbUnsafeAttrs)
      if (n.hasAttribute(a)) return {why: 'attr:' + a, node: n};
  }
  try {
    // cursor is inherited, so the deepest element carries a link/button's pointer
    if (__vbUnsafeCursor.test(getComputedStyle(el).cursor)) return {why: 'cursor', node: el};
  } catch (_) {}
  return null;
};
const __vbUnsafe = (x, y, vw, vh) => {
  const c = __vbCulprit(x, y, vw, vh);
  return c ? c.why : null;
};
const __vbInnerScroller = (x, y) => {
  const se = document.scrollingElement || document.documentElement;
  for (let n = __vbDeep(x, y), d = 0; n && n.nodeType === 1 && d < 60; n = __vbUp(n), d++) {
    if (n === se || n === document.body || n === document.documentElement) continue;
    try {
      const cs = getComputedStyle(n);
      const oy = cs.overflowY, ox = cs.overflowX;
      if ((/(auto|scroll|overlay)/.test(oy) && n.scrollHeight > n.clientHeight + 1)
          || (/(auto|scroll|overlay)/.test(ox) && n.scrollWidth > n.clientWidth + 1))
        return 'inner-scroller:' + n.tagName.toLowerCase();
    } catch (_) {}
  }
  return null;
};
""".replace("__M", str(EDGE_MARGIN_PX)).replace("__T", str(TOP_MARGIN_PX))

# Pointer tracker, installed in the ISOLATED world (patchright's evaluate world —
# page JS can neither see the global nor enumerate the listeners). It records the
# last pointer position the document itself observed, from ANY source: a plain
# Playwright click/hover (which parks the pointer on the element centre without
# telling us), humanize, ambient. That is the only exact answer to "where is the
# pointer on this page" — `:hover` is NOT one: measured in headless Chrome it
# stays empty after plain moves and locator clicks. Idempotent per document.
_TRACKER_JS = r"""
  let T = window.__vbAmbPtr, fresh = false;
  if (!T) {
    T = window.__vbAmbPtr = {x: null, y: null};
    const rec = (e) => { T.x = e.clientX; T.y = e.clientY; };
    for (const ty of ['pointermove', 'pointerdown', 'pointerup', 'mousemove',
                      'mousedown', 'mouseup', 'wheel'])
      addEventListener(ty, rec, {capture: true, passive: true});
    fresh = true;
  }
"""

TRACK_JS = "() => {" + _TRACKER_JS + r"""
  return {fresh, ptr: T.x === null ? null : [T.x, T.y]};
}"""

PROBE_JS = "(a) => {" + _TRACKER_JS + r"""
  const vw = innerWidth, vh = innerHeight;
  const out = {visible: document.visibilityState === 'visible',
               focused: document.hasFocus(), scheme: location.protocol,
               vw, vh, blocks: [], scroll_y: 0, room_down: 0, room_up: 0,
               tracker_fresh: fresh, ptr: T.x === null ? null : [T.x, T.y]};
  const els = document.querySelectorAll(
    'p,li,h1,h2,h3,h4,h5,h6,blockquote,pre,td,dd,figcaption');
  for (let i = 0; i < els.length && i < 600 && out.blocks.length < 16; i++) {
    const r = els[i].getBoundingClientRect();
    if (r.width < 40 || r.height < 8) continue;
    if (r.bottom < __T || r.top > vh - __M || r.right < __M || r.left > vw - __M) continue;
    out.blocks.push([r.left, r.top, r.width, r.height]);
  }
  const se = document.scrollingElement || document.documentElement;
  out.scroll_y = se.scrollTop;
  out.room_down = Math.max(0, se.scrollHeight - vh - se.scrollTop);
  out.room_up = se.scrollTop;
  // Where the pointer really is: what this document saw last, else our record.
  out.start = out.ptr || (a && a.cursor) || null;
  return out;
}""".replace("__M", str(EDGE_MARGIN_PX)).replace("__T", str(TOP_MARGIN_PX))

# Check a whole planned path in one round trip; returns the first unsafe index.
# `a.from` (optional) is where the pointer rests now: if it's already on an
# interactive element (a plain click left it on a button), that element is
# ALREADY hovered — staying inside it changes no hover state, so a leading run of
# points on that same element is allowed. Once the path leaves it, every later
# point must be safe (re-entering would fire a fresh mouseover).
CHECK_JS = "(a) => {" + _UNSAFE_FN_JS + r"""
  const vw = innerWidth, vh = innerHeight;
  const home = a.from ? __vbCulprit(a.from[0], a.from[1], vw, vh) : null;
  let leaving = !!(home && home.node);
  for (let i = 0; i < a.pts.length; i++) {
    const c = __vbCulprit(a.pts[i][0], a.pts[i][1], vw, vh);
    if (leaving) {
      if (c && c.node === home.node) continue;
      leaving = false;
    }
    if (c) return {ok: false, index: i, reason: c.why};
  }
  if (a.wheel) {
    // the wheel lands on whatever is under the pointer: it must be safe AND
    // nothing between it and the document may be an inner scroller
    const why = __vbUnsafe(a.wheel[0], a.wheel[1], vw, vh)
             || __vbInnerScroller(a.wheel[0], a.wheel[1]);
    if (why) return {ok: false, index: -1, reason: why};
  }
  return {ok: true};
}"""


# ─── per-page cursor (shared with the humanize paths) ────────────────────────

_PAGE_CURSORS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def get_cursor(entry, page=None):
    """Where the pointer is on `page` (default: the session's active page). Falls
    back to the session-level position: a physical pointer doesn't move when the
    user switches tabs, so the last known screen position is the right start."""
    if page is None:
        page = getattr(getattr(entry, "session", None), "page", None)
    if page is not None:
        with contextlib.suppress(TypeError):
            pos = _PAGE_CURSORS.get(page)
            if pos is not None:
                return pos
    return entry.flags.get("_cursor") if entry is not None else None


def set_cursor(entry, pos, page=None) -> None:
    if entry is None or pos is None:
        return
    pos = (float(pos[0]), float(pos[1]))
    entry.flags["_cursor"] = pos
    if page is None:
        page = getattr(getattr(entry, "session", None), "page", None)
    if page is not None:
        with contextlib.suppress(TypeError):
            _PAGE_CURSORS[page] = pos


def clear_cursor(entry) -> None:
    entry.flags.pop("_cursor", None)
    page = getattr(getattr(entry, "session", None), "page", None)
    if page is not None:
        with contextlib.suppress(TypeError, KeyError):
            del _PAGE_CURSORS[page]


# ─── the scheduler ───────────────────────────────────────────────────────────


class _Yield(Exception):
    """Raised inside a burst the moment acting would be unsafe."""


@dataclass
class _State:
    entry: object
    params: AmbientParams
    rng: random.Random
    horizon_s: float
    scroll: bool
    seed_source: str
    busy: int = 0
    last_verb_end: float = field(default_factory=time.monotonic)
    bursts_since_verb: int = 0
    scroll_pinned: bool = False
    pointer_parked: bool = False
    last_wheel_at: float = 0.0    # monotonic; stamped before AND after each notch
    task: asyncio.Task | None = None
    burst: asyncio.Task | None = None
    arm: asyncio.Task | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    stopped: bool = False
    stats: dict = field(default_factory=lambda: {
        "bursts": 0, "moves": 0, "wheel_notches": 0, "yields": 0,
        "skipped": {}, "started_at": time.time()})

    def skip(self, reason: str) -> None:
        sk = self.stats["skipped"]
        sk[reason] = sk.get(reason, 0) + 1


class AmbientManager:
    """Owns every session's ambient task. One instance per Daemon."""

    def __init__(self, daemon) -> None:
        self.d = daemon
        self._st: dict[str, _State] = {}

    # ── public API (handlers + dispatcher hooks) ─────────────────────────

    def enable(self, name: str, entry, *, seed=None, horizon_s=None,
               scroll: bool = True) -> dict:
        sess = getattr(entry, "session", None)
        if sess is None:
            raise RuntimeError("ambient requires a running session — start one first")
        if getattr(sess, "mode", "launch") != "launch":
            raise RuntimeError(
                "ambient is refused on attach-mode sessions — that browser is the "
                "user's own and synthetic pointer input would land in their window")
        if getattr(sess, "headless", True) is False:
            raise RuntimeError(
                "ambient is refused on headed sessions — a visible window may "
                "have a human at it (`vb show` / `vb login`, a captcha hand-off), "
                "and synthetic pointer drift would fight their mouse. Use it on "
                "headless sessions")
        if entry.flags.get("backend", "patchright") != "patchright":
            raise RuntimeError("ambient needs the patchright backend")
        old = self._st.get(name)
        if old is not None:
            self._stop_state(old)
        if seed is None:
            seed_val = session_seed(name, entry.profile_dir)
            src = "derived"
        else:
            seed_val = int(seed)
            src = "explicit"
        params = derive_params(seed_val)
        st = _State(entry=entry, params=params,
                    rng=random.Random(seed_val ^ 0x5EED),
                    horizon_s=ambient_horizon(horizon_s), scroll=bool(scroll),
                    seed_source=src)
        self._st[name] = st
        entry.flags["ambient"] = True
        st.task = asyncio.create_task(self._run(name, st), name=f"vb-ambient-{name}")
        return self.status(name)

    def disable(self, name: str) -> dict:
        st = self._st.pop(name, None)
        if st is not None:
            self._stop_state(st)
            with contextlib.suppress(Exception):
                st.entry.flags.pop("ambient", None)
        return {"ambient": False, "session": name}

    def status(self, name: str) -> dict:
        st = self._st.get(name)
        if st is None:
            return {"ambient": False, "session": name}
        from . import freeze as _freeze
        eff = st.horizon_s
        if _freeze.freeze_enabled() and _freeze.eligible(st.entry):
            eff = min(eff, _freeze.freeze_after())
        idle = time.monotonic() - st.last_verb_end
        return {
            "ambient": True, "session": name,
            "seed": st.params.seed, "seed_source": st.seed_source,
            "horizon_s": st.horizon_s, "effective_horizon_s": eff,
            "scroll": st.scroll, "scroll_pinned": st.scroll_pinned,
            "pointer_parked": st.pointer_parked,
            "idle_s": round(idle, 1),
            # (`status` is itself a verb, so st.busy counts it — not used here)
            "active": idle < eff and not st.stopped and not st.pointer_parked
                      and not getattr(st.entry, "frozen", False),
            "stats": dict(st.stats, skipped=dict(st.stats["skipped"])),
            "params": st.params.summary(),
        }

    def is_on(self, name: str) -> bool:
        return name in self._st

    def verb_begin(self, name: str, cmd: str) -> None:
        st = self._st.get(name)
        if st is None:
            return
        st.busy += 1
        b = st.burst
        if b is not None and not b.done():
            b.cancel()
            st.stats["yields"] += 1
        st.wake.set()

    def verb_end(self, name: str, cmd: str, args: dict | None = None) -> None:
        st = self._st.get(name)
        if st is None:
            return
        st.busy = max(0, st.busy - 1)
        st.last_verb_end = time.monotonic()
        st.bursts_since_verb = 0
        if cmd in VIEWPORT_PINNING_VERBS:
            st.scroll_pinned = True
        elif cmd not in READ_ONLY_VERBS:
            st.scroll_pinned = False      # navigation / input: positions are stale anyway
        if pointer_parks(cmd, args):
            st.pointer_parked = True
        elif cmd not in READ_ONLY_VERBS:
            st.pointer_parked = False
        st.wake.set()
        if st.busy == 0 and (st.arm is None or st.arm.done()):
            # Arm the in-page pointer tracker right after the verb (a `go` just
            # made a new document) so it is listening BEFORE the agent's next
            # plain click parks the pointer somewhere. A read-only isolated-world
            # evaluate — never input — so it may overlap the next verb safely.
            st.arm = asyncio.create_task(self._arm_tracker(name, st))

    async def settle(self, name: str, cmd: str) -> float:
        """Before a coordinate-sensitive verb: wait out the tail of a wheel
        notch ambient sent just before the verb began (its scroll lands after
        the CDP call returns). Called after verb_begin, so no new notch can go
        out meanwhile. Returns the seconds waited (0 almost always)."""
        st = self._st.get(name)
        if st is None or cmd not in COORDINATE_VERBS:
            return 0.0
        wait = st.last_wheel_at + WHEEL_SETTLE_S - time.monotonic()
        if wait <= 0:
            return 0.0
        await asyncio.sleep(wait)
        return wait

    def reap(self) -> None:
        """Drop states whose session is gone (closed / deleted / replaced)."""
        for name, st in list(self._st.items()):
            if self.d.registry.peek(name) is not st.entry:
                self._st.pop(name, None)
                self._stop_state(st)

    async def shutdown(self) -> None:
        tasks = []
        for name in list(self._st):
            st = self._st.pop(name)
            self._stop_state(st)
            tasks += [t for t in (st.task, st.burst, st.arm) if t is not None]
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    # ── internals ────────────────────────────────────────────────────────

    def _stop_state(self, st: _State) -> None:
        st.stopped = True
        for t in (st.burst, st.task, st.arm):
            if t is not None and not t.done():
                t.cancel()
        with contextlib.suppress(Exception):
            st.entry.ambient_busy = False

    def _takeover_active(self, name: str) -> bool:
        srv = getattr(self.d, "_liveview_server", None)
        if srv is None or not getattr(srv, "takeover", False):
            return False
        clients = getattr(srv, "_clients", {}) or {}
        return bool(clients.get(name))

    def _blocked(self, name: str, st: _State) -> str | None:
        """Synchronous gate, re-evaluated immediately before EVERY CDP dispatch.
        Must contain no await — that is what makes check-then-dispatch atomic
        under asyncio."""
        if st.stopped:
            return "stopped"
        entry = st.entry
        if self.d.registry.peek(name) is not entry:
            return "closed"
        if st.busy > 0:
            return "verb"
        if entry.lock.locked():
            return "locked"
        if getattr(entry, "inflight", 0) > 0:
            return "inflight"
        if getattr(entry, "frozen", False):
            return "frozen"
        if time.monotonic() - st.last_verb_end >= st.horizon_s:
            return "horizon"
        if st.pointer_parked:
            return "parked"
        if entry.flags.get("_buttons_down"):
            return "button_held"
        if self._takeover_active(name):
            return "takeover"
        sess = getattr(entry, "session", None)
        page = getattr(sess, "page", None)
        if page is None:
            return "no_page"
        with contextlib.suppress(Exception):
            if page.is_closed():
                return "no_page"
        return None

    async def _run(self, name: str, st: _State) -> None:
        try:
            while not st.stopped:
                st.wake.clear()
                if self.d.registry.peek(name) is not st.entry:
                    self._st.pop(name, None)
                    st.stopped = True
                    return
                idle = time.monotonic() - st.last_verb_end
                reason = None
                if st.busy > 0:
                    reason = "verb"
                elif getattr(st.entry, "frozen", False):
                    reason = "frozen"
                elif idle >= st.horizon_s:
                    reason = "horizon"
                elif st.pointer_parked:
                    reason = "parked"     # hover/focus/mouse move: hands off
                if reason is not None:
                    # Parked: wait for the next verb. Re-check liveness now and
                    # then so a closed session's task doesn't linger.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(st.wake.wait(), timeout=30.0)
                    continue
                gap = next_gap_s(st.params, st.rng, idle,
                                 first=st.bursts_since_verb == 0)
                gap = min(gap, max(0.0, st.horizon_s - idle))
                woke = False
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(st.wake.wait(), timeout=gap)
                    woke = True
                if woke:
                    continue      # a verb began/ended — re-plan from scratch
                why = self._blocked(name, st)
                if why is not None:
                    st.skip(why)
                    continue
                st.burst = asyncio.create_task(self._burst(name, st))
                await asyncio.wait({st.burst})
                st.burst = None
                st.bursts_since_verb += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let ambient take anything down
            log.exception("ambient: loop crashed session=%s — disabled", name)
            self._st.pop(name, None)
            st.stopped = True

    async def _arm_tracker(self, name: str, st: _State) -> None:
        entry = st.entry
        if st.stopped or getattr(entry, "frozen", False):
            return
        if self.d.registry.peek(name) is not entry:
            return
        page = getattr(getattr(entry, "session", None), "page", None)
        if page is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(page.evaluate(TRACK_JS), CDP_TIMEOUT_S)

    async def _eval(self, name: str, st: _State, page, js: str, arg):
        if self._blocked(name, st):
            raise _Yield()
        return await asyncio.wait_for(page.evaluate(js, arg), CDP_TIMEOUT_S)

    async def _burst(self, name: str, st: _State) -> None:
        entry = st.entry
        entry.ambient_busy = True
        try:
            await self._burst_body(name, st)
            st.stats["bursts"] += 1
        except _Yield:
            st.stats["yields"] += 1
        except asyncio.CancelledError:
            pass          # verb_begin() cancelled us — that IS the yield
        except Exception as exc:  # noqa: BLE001 — navigation races, closed pages
            st.skip("error")
            log.debug("ambient: burst error session=%s: %s", name, exc)
        finally:
            entry.ambient_busy = False

    async def _burst_body(self, name: str, st: _State) -> None:
        entry = st.entry
        page = entry.session.page
        rng, params = st.rng, st.params
        cursor = get_cursor(entry, page)
        probe = await self._eval(name, st, page, PROBE_JS,
                                 {"cursor": list(cursor) if cursor else None})
        scheme = str(probe.get("scheme") or "")
        if scheme.startswith(("chrome", "devtools", "view-source", "edge")):
            st.skip("scheme")
            return
        if not probe.get("visible") or not probe.get("focused"):
            st.skip("hidden")
            return
        vw, vh = float(probe.get("vw") or 0), float(probe.get("vh") or 0)
        if vw < 2 * TOP_MARGIN_PX or vh < 2 * TOP_MARGIN_PX:
            st.skip("viewport")
            return
        start = probe.get("start")
        if start is not None:
            start = (float(start[0]), float(start[1]))
            if start != cursor:
                # The document saw the pointer somewhere else (a plain click /
                # hover moved it) — continue from THERE, never teleport back.
                set_cursor(entry, start, page)
        cursor = start
        blocks = probe.get("blocks") or []

        scroll_ok = (st.scroll and not st.scroll_pinned and cursor is not None
                     and (probe.get("room_down", 0) > params.notch_px
                          or probe.get("room_up", 0) > params.notch_px))
        action = choose_action(params, rng, scroll_ok=scroll_ok)

        if cursor is None:
            # Unknown position (no pointer event yet on this page): the pointer was
            # resting somewhere when the page loaded — its first event can be
            # anywhere, so appear over readable content and drift from there.
            if not blocks:
                st.skip("no_content")
                return
            first = clamp_to_viewport(block_point(rng.choice(blocks), rng), vw, vh)
            first = (int(round(first[0])), int(round(first[1])))
            chk = await self._eval(name, st, page, CHECK_JS, {"pts": [list(first)]})
            if not chk.get("ok"):
                st.skip("unsafe")
                return
            await self._move(name, st, page, [(first[0], first[1], 0.0)])
            cursor = first
            if action == "scroll":
                action = "drift"

        if action == "scroll":
            await self._scroll(name, st, page, probe, cursor, vw, vh, blocks)
            return

        n_moves = 1
        while n_moves < 4 and rng.random() < params.moves_continue_p:
            n_moves += 1
        for i in range(n_moves):
            if i:
                await self._sleep(name, st, rng.lognormvariate(
                    math.log(params.intra_pause_median_ms), 0.5) / 1000.0)
            path = None
            for _attempt in range(3):
                if action == "micro":
                    cand = plan_micro(cursor, rng)
                else:
                    if action == "reposition" and blocks:
                        tgt = block_point(rng.choice(blocks), rng)
                    else:
                        tgt = drift_target(cursor, rng, params)
                    tgt = clamp_to_viewport(tgt, vw, vh)
                    cand = plan_move(cursor, tgt, rng, params)
                if not cand:
                    continue
                chk = await self._eval(name, st, page, CHECK_JS,
                                       {"pts": [[x, y] for x, y, _ in cand],
                                        "from": [int(round(cursor[0])),
                                                 int(round(cursor[1]))]})
                if chk.get("ok"):
                    path = cand
                    break
            if path is None:
                st.skip("unsafe")
                return
            await self._move(name, st, page, path)
            cursor = (path[-1][0], path[-1][1])
            action = "drift" if rng.random() < 0.7 else "micro"

    async def _sleep(self, name: str, st: _State, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))
        if self._blocked(name, st):
            raise _Yield()

    async def _move(self, name: str, st: _State, page, path) -> None:
        for x, y, dt in path:
            if dt > 0:
                await asyncio.sleep(dt / 1000.0)
            if self._blocked(name, st):        # no await between this and dispatch
                raise _Yield()
            await asyncio.wait_for(page.mouse.move(x, y), CDP_TIMEOUT_S)
            set_cursor(st.entry, (x, y), page)
            st.stats["moves"] += 1

    async def _scroll(self, name: str, st: _State, page, probe: dict,
                      cursor, vw: float, vh: float, blocks: list) -> None:
        rng, params = st.rng, st.params
        down_room = probe.get("room_down", 0) > params.notch_px
        up_room = probe.get("room_up", 0) > params.notch_px
        down = down_room and (not up_room or rng.random() < params.scroll_down_p)
        dy = params.notch_px if down else -params.notch_px
        room = probe.get("room_down", 0) if down else probe.get("room_up", 0)
        # Scrolling slides content UNDER a still pointer, and Chrome re-runs hover
        # after a scroll (it fires mouseover on whatever arrives under the cursor —
        # measured). So everything that will pass under the pointer must be as safe
        # as a move target: hit-test the column that will slide under it, and the
        # wheel point itself (no inner scroller). If the column under the current
        # position isn't clean, a reader first moves the hand onto the text — try
        # two text-block anchors (move there, then scroll).
        n0 = notch_count(rng)
        anchors = [cursor] + [clamp_to_viewport(block_point(b, rng), vw, vh)
                              for b in rng.sample(blocks, min(2, len(blocks)))]
        chosen = None
        for anchor in anchors:
            anchor = (int(round(anchor[0])), int(round(anchor[1])))
            path = [] if anchor == (int(round(cursor[0])), int(round(cursor[1]))) \
                else plan_move(cursor, anchor, rng, params)
            for n in range(n0, 0, -1):
                col = scroll_column(anchor, n * params.notch_px, down=down, vh=vh)
                if col is None or n * params.notch_px > room:
                    continue
                chk = await self._eval(name, st, page, CHECK_JS, {
                    "pts": [[x, y] for x, y, _ in path] + col,
                    "from": [int(round(cursor[0])), int(round(cursor[1]))],
                    "wheel": list(anchor)})
                if chk.get("ok"):
                    chosen = (path, n)
                    break
            if chosen:
                break
        if chosen is None:
            st.skip("scroll_unsafe")
            return
        path, n = chosen
        if path:
            await self._move(name, st, page, path)
            await self._sleep(name, st, rng.uniform(0.12, 0.4))   # hand settles
        for i in range(n):
            if i:
                await asyncio.sleep(rng.lognormvariate(
                    math.log(params.notch_interval_median_ms), 0.35) / 1000.0)
            if self._blocked(name, st):
                raise _Yield()
            st.last_wheel_at = time.monotonic()   # covers a notch cancelled mid-call
            await asyncio.wait_for(page.mouse.wheel(0, dy), CDP_TIMEOUT_S)
            st.last_wheel_at = time.monotonic()
            st.stats["wheel_notches"] += 1
