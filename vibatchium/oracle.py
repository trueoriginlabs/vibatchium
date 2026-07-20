"""vb oracle — self-hosted BEHAVIOURAL oracle (0.18.0).

`vb bench` / `vb evals` measure the STATIC axis (fingerprint scoreboards, wall
pass-rate). This measures the axis detection actually moved to in 2026: Cloudflare
Precursor, DataDome Agent Trust, Arkose Agent Trust Manager and HUMAN all shipped
session-lifetime BEHAVIOURAL scoring — none of them care about canvas/WebGL/
`Runtime.enable`. They score mouse trajectory, dwell, keystroke cadence and event
granularity over the life of a session.

There is no free self-serve behavioural oracle in the wild (the static axis has
sannysoft/CreepJS/pixelscan; the behavioural axis has essentially nothing, and the
one public toy — bot.incolumitas.com — collected zero samples under automation when
we live-inspected it 2026-07-20). So we build our own: instrument a page, drive the
same scripted gesture set with humanize OFF then ON, extract the features the vendors
publish, and grade each against a human-plausible band.

WHAT THIS IS — and IS NOT (do not strip these; they are the honesty of the tool):

  - It grades against OUR MODEL of what the vendors measure. The bands below are
    literature/heuristic until a real operator baseline (an actual human driving the
    instrumented page) replaces them via `load_baseline()`. It therefore CANNOT say
    "we beat DataDome". It CAN say whether humanize's output is obviously non-human on
    each measurable feature, and whether turning humanize on moves a feature from the
    synthetic band into the human band — which is the actionable half.

  - The GAP features (`kind="gap"`) are `raw_pointer_events` and `coalesced_max` —
    the raw pointer stream. Real hardware fires `pointerrawupdate` and batches
    coalesced samples into each dispatched `pointermove` (`getCoalescedEvents`); live
    measurement showed CDP-synthesised input produces NEITHER (pointerrawupdate count
    0, getCoalescedEvents absent) — the page sees only compositor-clocked pointermoves.
    BOTH humanize on AND off fail these rows BY CONSTRUCTION — the oracle CONFIRMS the
    gap `humanize.py:13-19` documents, it does not close it. Closing it needs
    attach-mode against a real headful Chrome, not synthetic input.

  - Two REPORTED-not-scored diagnostics, kept visible because each refutes an
    assumption rather than being quietly dropped: (1) `move_dt_cv` — we expected a
    fixed injection delay to read metronomic, but on `pointermove` the timing is
    Chrome's ~60Hz compositor clock (dt≈16.7ms, CV≈0.02) for real AND synthetic input
    alike; scoring it would false-positive a human, and jittering our injection does
    nothing (Chrome re-emits on its own tick). (2) `screen_eq_client` — we assumed a
    screenX==clientX CDP tell, but synthetic input carries a real screen offset
    (screenX=375, clientX=365), so that tell does not fire on this stack.

The offline test drives the pure extractor/scorer over synthetic buffers (a
teleport must read synthetic, a hand-shaped gesture must read human). The live lane
(`vb oracle run`) spins a throwaway ephemeral session, runs the gesture set twice,
and tears down — never a CI gate.
"""
from __future__ import annotations

import json as _json
import logging
import math
import statistics
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("vibatchium.oracle")

# The phrase typed during the keystroke-cadence gesture. Deliberately mundane and
# non-secret — it is echoed into an in-page <input> and captured as keydown timing.
TYPE_SAMPLE = "the quick brown fox jumps 1842"

# ─── in-page instrumentation (single source of truth, injected via eval) ──────
#
# Built as IIFEs so `page.evaluate` runs them directly and returns the value.

# Build a minimal instrumented page on about:blank and hand back target centres.
# Absolute positioning keeps the coordinates stable regardless of default UA
# margins; the tall spacer gives the wheel gesture somewhere to scroll.
_BUILD_PAGE_JS = r"""(() => {
  document.title = 'vb-oracle';
  document.body.style.margin = '0';
  document.body.innerHTML =
    '<div style="height:2000px;position:relative;font-family:sans-serif">' +
    '<input id="oracle-input" autocomplete="off" ' +
      'style="position:absolute;left:120px;top:90px;width:260px;height:30px">' +
    '<button id="oracle-btn" ' +
      'style="position:absolute;left:300px;top:280px;width:130px;height:44px">Go</button>' +
    '</div>';
  const r = (id) => {
    const b = document.getElementById(id).getBoundingClientRect();
    return {x: Math.round(b.x + b.width / 2), y: Math.round(b.y + b.height / 2)};
  };
  return {btn: r('oracle-btn'), input: r('oracle-input')};
})()"""

# Install capture-phase listeners that buffer every pointer/mouse/key/wheel event
# with a high-resolution timestamp. `co` is the coalesced-sample count (the hardware
# tell); `sx/cx/pxv` are screenX/clientX/pageX for the button-down (the CDP
# coordinate tell). Bounded so a runaway gesture can't grow the buffer without limit.
_INSTRUMENT_JS = r"""(() => {
  const B = [];
  const MAX = 8000;
  const now = () => performance.now();
  const push = (o) => { if (B.length < MAX) B.push(o); };
  const co = (e) => {
    try {
      return (typeof e.getCoalescedEvents === 'function')
        ? e.getCoalescedEvents().length : null;
    } catch (_) { return null; }
  };
  const opt = {capture: true, passive: true};
  addEventListener('pointermove', e => push(
    {type: 'pmove', t: now(), x: e.clientX, y: e.clientY, co: co(e), ets: e.timeStamp}), opt);
  // pointerrawupdate is the un-coalesced raw input stream. Real hardware fires it;
  // CDP-synthesised input does not — so its COUNT is a genuine synthetic tell,
  // unlike pointermove (which Chrome re-emits on its regular ~60Hz compositor clock).
  addEventListener('pointerrawupdate', e => push(
    {type: 'praw', t: now(), x: e.clientX, y: e.clientY, co: co(e), ets: e.timeStamp}), opt);
  addEventListener('mousemove', e => push(
    {type: 'mmove', t: now(), x: e.clientX, y: e.clientY, ets: e.timeStamp}), opt);
  const du = (type) => (e) => push(
    {type, t: now(), x: e.clientX, y: e.clientY, btn: e.button,
     sx: e.screenX, cx: e.clientX, pxv: e.pageX, ets: e.timeStamp});
  addEventListener('pointerdown', du('pdown'), opt);
  addEventListener('pointerup', du('pup'), opt);
  addEventListener('mousedown', du('mdown'), opt);
  addEventListener('mouseup', du('mup'), opt);
  addEventListener('keydown', e => push({type: 'key', t: now(), ets: e.timeStamp}), opt);
  addEventListener('wheel', e => push(
    {type: 'wheel', t: now(), dx: e.deltaX, dy: e.deltaY, ets: e.timeStamp}), opt);
  window.__vbo = B;
  return {installed: true};
})()"""

# Return a copy of the buffer and reset it, so the two runs (off/on) don't bleed.
_DRAIN_JS = r"""(() => {
  const B = window.__vbo || [];
  const out = B.slice();
  if (window.__vbo) window.__vbo.length = 0;
  return {events: out, ua: navigator.userAgent};
})()"""


# ─── feature extraction (PURE — unit-tested, no browser) ─────────────────────


def _euclid(a: dict, b: dict) -> float:
    return math.hypot(b["x"] - a["x"], b["y"] - a["y"])


def _mean(xs: list[float]) -> float | None:
    return statistics.fmean(xs) if xs else None


def _pstdev(xs: list[float]) -> float | None:
    return statistics.pstdev(xs) if len(xs) >= 2 else (0.0 if xs else None)


def _moves(events: list[dict]) -> list[dict]:
    """The trajectory samples. Prefer pointermove (carries coalesced counts);
    fall back to mousemove only if no pointer events were seen at all."""
    pm = [e for e in events if e.get("type") == "pmove"]
    if pm:
        return pm
    return [e for e in events if e.get("type") == "mmove"]


def _first_down_t(events: list[dict]) -> float | None:
    for e in events:
        if e.get("type") in ("pdown", "mdown"):
            return e.get("t")
    return None


def _trajectory_features(events: list[dict]) -> dict:
    """Shape + timing of the approach: everything before the first button-down."""
    moves = _moves(events)
    cut = _first_down_t(events)
    if cut is not None:
        approach = [m for m in moves if m["t"] <= cut]
    else:
        approach = moves
    f: dict[str, Any] = {
        "n_moves": len(approach),
        "straightness": None,
        "peak_speed_px_ms": None,
        "decel_ratio": None,
        "move_dt_cv": None,
        "coalesced_max": None,
        "coalesced_mean": None,
    }
    # Coalescing is a per-sample property — compute it even for a single move (a
    # teleport still carries co==1, which is the whole point of the gap row).
    cos = [m["co"] for m in approach if m.get("co") is not None]
    if cos:
        f["coalesced_max"] = max(cos)
        f["coalesced_mean"] = round(statistics.fmean(cos), 3)
    if len(approach) < 2:
        return f
    seglen = [_euclid(approach[i], approach[i + 1]) for i in range(len(approach) - 1)]
    path_len = sum(seglen)
    chord = _euclid(approach[0], approach[-1])
    f["straightness"] = round(chord / path_len, 4) if path_len > 0 else None

    dts = [approach[i + 1]["t"] - approach[i]["t"] for i in range(len(approach) - 1)]
    speeds = [seglen[i] / dt for i, dt in enumerate(dts) if dt > 0]
    if speeds:
        peak = max(speeds)
        f["peak_speed_px_ms"] = round(peak, 4)
        # Deceleration into the target: mean speed over the final third vs the peak.
        # A real hand slows as it lands (< 1); a constant-rate synthetic path ~1.
        tail = speeds[max(1, len(speeds) * 2 // 3):]
        if tail and peak > 0:
            f["decel_ratio"] = round(statistics.fmean(tail) / peak, 4)
    pos_dts = [d for d in dts if d > 0]
    if len(pos_dts) >= 2:
        m = statistics.fmean(pos_dts)
        f["move_dt_cv"] = round(statistics.pstdev(pos_dts) / m, 4) if m > 0 else None
    return f


def _dwell_features(events: list[dict]) -> dict:
    """down→up hold time for the click gesture. Pointer pair preferred."""
    downs_t = "pdown" if any(e.get("type") == "pdown" for e in events) else "mdown"
    ups_t = "pup" if downs_t == "pdown" else "mup"
    dwell: list[float] = []
    pending: float | None = None
    for e in events:
        if e.get("type") == downs_t:
            pending = e["t"]
        elif e.get("type") == ups_t and pending is not None:
            dwell.append(e["t"] - pending)
            pending = None
    return {"dwell_ms_mean": round(_mean(dwell), 3) if dwell else None,
            "n_clicks": len(dwell)}


def _interkey_features(events: list[dict]) -> dict:
    """keydown→keydown intervals for the typing gesture."""
    ks = [e["t"] for e in events if e.get("type") == "key"]
    ks.sort()
    gaps = [ks[i + 1] - ks[i] for i in range(len(ks) - 1)]
    return {
        "interkey_ms_mean": round(_mean(gaps), 3) if gaps else None,
        "interkey_ms_stdev": round(_pstdev(gaps), 3) if gaps else None,
        "n_keys": len(ks),
    }


def _scroll_features(events: list[dict]) -> dict:
    """Wheel-tick count + delta shape for one scroll gesture."""
    wheels = [e for e in events if e.get("type") == "wheel"]
    deltas = [abs(e.get("dy", 0)) for e in wheels]
    return {
        "scroll_steps": len(wheels),
        "scroll_dy_total": round(sum(deltas), 2) if deltas else None,
        "scroll_dy_max_step": round(max(deltas), 2) if deltas else None,
    }


def _raw_pointer_features(events: list[dict]) -> dict:
    """Count of pointerrawupdate events. Real hardware emits this un-coalesced raw
    stream; CDP-synthesised input emits none — so the count discriminates directly,
    where pointermove (compositor-clocked) does not."""
    return {"raw_pointer_events": sum(1 for e in events if e.get("type") == "praw")}


def _cdp_coord_signature(events: list[dict]) -> dict:
    """The CDP coordinate tell on the button-down: synthetic input dispatched via
    `Input.*` yields screenX == clientX (no real window offset). Recorded for BOTH
    paths — it is a gap, not a humanize pass/fail."""
    for e in events:
        if e.get("type") in ("pdown", "mdown") and e.get("sx") is not None:
            return {"screen_eq_client": e.get("sx") == e.get("cx"),
                    "screenX": e.get("sx"), "clientX": e.get("cx"),
                    "pageX": e.get("pxv")}
    return {"screen_eq_client": None, "screenX": None, "clientX": None, "pageX": None}


def extract_features(drain: dict) -> dict:
    """Turn a raw drained buffer (`{"events": [...], ...}`) into the flat feature
    dict the scorer grades. Pure + total: an empty buffer yields all-None, never
    raises."""
    events = drain.get("events", []) if isinstance(drain, dict) else list(drain)
    events = [e for e in events if isinstance(e, dict) and "t" in e]
    events.sort(key=lambda e: e["t"])
    f: dict[str, Any] = {"n_events": len(events)}
    f.update(_trajectory_features(events))
    f.update(_raw_pointer_features(events))
    f.update(_dwell_features(events))
    f.update(_interkey_features(events))
    f.update(_scroll_features(events))
    f.update(_cdp_coord_signature(events))
    return f


# ─── the baseline (literature bands until a real operator baseline replaces) ──
#
# Each entry: (kind, lo, hi, human, source).
#   kind  — "score": counts toward the human/synthetic headline.
#           "gap":   a limit synthetic input cannot clear (shown, never scored as a
#                    humanize win/loss).
#           "report": measured + shown, too device-dependent to band.
#   lo/hi — inclusive human-plausible band; None = unbounded on that side.
# These are the honest defaults. Replace per-feature with an operator's recorded
# distribution ([p5, p95]) via load_baseline() the moment one exists.

HUMAN_BASELINE: dict[str, dict] = {
    "n_moves": {
        "kind": "score", "lo": 8, "hi": None,
        "human": "a deliberate move emits many pointer samples (dozens+)",
        "source": "60-120Hz pointer sampling over a multi-hundred-ms move"},
    "straightness": {
        "kind": "score", "lo": 0.55, "hi": 0.999,
        "human": "a real hand curves; chord/arc sits below a perfect straight line",
        "source": "human pointer paths are never perfectly linear (Fitts-law arc)"},
    "move_dt_cv": {
        # REPORTED, not scored. Measured on pointermove this is the COMPOSITOR CLOCK,
        # not our injection timing: Chrome re-emits injected moves on its ~60Hz tick,
        # so dt pins to ~16.7ms (CV≈0.02) for real AND synthetic input alike. Scoring
        # it would false-positive a real human. The real timing tell is raw_pointer.
        "kind": "report", "lo": None, "hi": None,
        "human": "= compositor delivery cadence (~60Hz), identical real/synthetic",
        "source": "pointermove dt is display-refresh-clocked, not a humanize tell"},
    "decel_ratio": {
        "kind": "report", "lo": None, "hi": 0.95,
        "human": "movement decelerates into the target (< 1)",
        "source": "ballistic-then-corrective motor control"},
    "peak_speed_px_ms": {
        "kind": "report", "lo": None, "hi": None,
        "human": "device/distance dependent — reported, not banded",
        "source": "-"},
    "dwell_ms_mean": {
        "kind": "score", "lo": 40, "hi": 300,
        "human": "mouse-button hold ~50-150ms; a teleport click is ~0ms",
        "source": "typical click dwell (mirrors humanize sample_dwell_ms clamp)"},
    "interkey_ms_mean": {
        "kind": "score", "lo": 55, "hi": 450,
        "human": "typing cadence ~90-250ms/char; instant fill is ~0ms",
        "source": "keystroke-dynamics inter-key intervals (40-90 WPM)"},
    "interkey_ms_stdev": {
        "kind": "score", "lo": 12, "hi": None,
        "human": "human cadence varies key-to-key; a constant delay has ~0 stdev",
        "source": "keystroke-dynamics dwell/flight variance"},
    "scroll_steps": {
        "kind": "score", "lo": 2, "hi": None,
        "human": "a wheel flick emits several ticks; a jump is one",
        "source": "mouse-wheel notch cadence"},
    "raw_pointer_events": {
        "kind": "gap", "lo": 1, "hi": None,
        "human": "real hardware fires pointerrawupdate; CDP synthetic input fires none",
        "source": "pointerrawupdate count — 0 for all synthetic input (measured)"},
    "coalesced_max": {
        "kind": "gap", "lo": 2, "hi": None,
        "human": "real hardware batches >1 sample per dispatched pointermove",
        "source": "getCoalescedEvents is absent/≤1 on CDP-synthesised input (on & off)"},
    "screen_eq_client": {
        # REPORTED, not scored: we hypothesised screenX==clientX as a CDP tell, but
        # measured synthetic input carries a real screen offset (screenX != clientX),
        # so it does NOT discriminate here. Kept visible because it refutes the
        # assumption in humanize.py's "pageX==screenX" note — the tell isn't firing.
        "kind": "report", "lo": None, "hi": None,
        "human": "screenX != clientX (a window offset) — but synthetic input shows one too",
        "source": "coordinate diagnostic; empirically not a synthetic tell on this stack"},
}


def load_baseline(path: str | Path | None = None) -> dict:
    """Return the active baseline. With no path (or a missing file) this is the
    literature default; given a JSON file of recorded operator features it overlays
    [p5, p95] score bands per feature onto the defaults, so a real human's
    distribution supersedes the heuristic. The overlaid file is the deliverable that
    turns this from "our model of human" toward "measured human"."""
    base = {k: dict(v) for k, v in HUMAN_BASELINE.items()}
    if not path:
        return base
    p = Path(path)
    if not p.exists():
        log.warning("baseline file %s not found — using literature defaults", p)
        return base
    recorded = _json.loads(p.read_text())
    # recorded: {feature: [samples...]} — band each scored feature to its [p5, p95].
    for feat, samples in recorded.items():
        if feat not in base or base[feat]["kind"] != "score":
            continue
        vals = sorted(float(s) for s in samples if s is not None)
        if len(vals) < 5:
            continue
        lo = vals[max(0, int(0.05 * (len(vals) - 1)))]
        hi = vals[min(len(vals) - 1, int(0.95 * (len(vals) - 1)))]
        base[feat]["lo"], base[feat]["hi"] = lo, hi
        base[feat]["source"] = f"recorded operator baseline (n={len(vals)}, p5-p95)"
    base["__source__"] = {"kind": "meta", "recorded": str(p)}
    return base


# ─── scoring (PURE) ──────────────────────────────────────────────────────────


def _in_band(value: Any, lo: Any, hi: Any) -> bool:
    if isinstance(value, bool) or isinstance(lo, bool) or isinstance(hi, bool):
        # boolean gap feature: human value is exactly `lo` (== hi)
        return value == lo
    if value is None:
        return False
    if lo is not None and value < lo:
        return False
    if hi is not None and value > hi:
        return False
    return True


def score_features(features: dict, baseline: dict | None = None) -> dict:
    """Grade a feature dict against the baseline. Returns per-feature verdicts plus a
    headline over the SCORE features only (gap/report features are shown, never
    counted as a humanize win or loss)."""
    baseline = baseline or HUMAN_BASELINE
    per: dict[str, dict] = {}
    n_human = n_scored = 0
    for feat, spec in baseline.items():
        if spec.get("kind") == "meta":
            continue
        value = features.get(feat)
        human = _in_band(value, spec.get("lo"), spec.get("hi"))
        entry = {
            "value": value, "kind": spec["kind"],
            "band": [spec.get("lo"), spec.get("hi")],
            "human": human, "note": spec.get("human"),
        }
        per[feat] = entry
        if spec["kind"] == "score" and value is not None:
            n_scored += 1
            if human:
                n_human += 1
    return {
        "per_feature": per,
        "n_human": n_human,
        "n_scored": n_scored,
        "gap_confirmed": [f for f, e in per.items()
                          if e["kind"] == "gap" and not e["human"]],
    }


# ─── live runner (spins an ephemeral session, drives the gesture set) ─────────


def _run_one(client_call: Callable[..., Any], session: str, humanize: bool,
             *, type_sample: str, scroll_dy: int, baseline: dict | None = None) -> dict:
    """One measurement pass in an already-started session: (re)build + instrument
    the page, run click → type → scroll, drain, extract, score."""
    client_call("go", {"url": "about:blank"}, session=session)
    targets = client_call("eval", {"expr": _BUILD_PAGE_JS}, session=session)["value"]
    client_call("eval", {"expr": _INSTRUMENT_JS}, session=session)
    if humanize:
        client_call("humanize_on", session=session)
    else:
        client_call("humanize_off", session=session)

    btn = targets["btn"]
    client_call("mouse", {"action": "click", "x": btn["x"], "y": btn["y"]},
                session=session)
    client_call("type", {"target": "#oracle-input", "text": type_sample},
                session=session)
    # Route scroll through the `mouse` verb (wheel action honours humanize; the
    # `scroll` verb dispatches a bare wheel and would flatten the on/off contrast).
    client_call("mouse", {"action": "wheel", "dx": 0, "dy": scroll_dy},
                session=session)

    drain = client_call("eval", {"expr": _DRAIN_JS}, session=session)["value"]
    features = extract_features(drain)
    scored = score_features(features, baseline)
    return {"humanize": humanize, "n_events": features["n_events"],
            "features": features, "score": scored}


def run_oracle(client_call: Callable[..., Any], *, headless: bool = True,
               type_sample: str = TYPE_SAMPLE, scroll_dy: int = 700,
               baseline: dict | None = None) -> list[dict]:
    """Drive the gesture set with humanize OFF then ON in a single throwaway
    ephemeral session (same environment both passes — controls for it), returning
    two records. Cleans the session up even on error, like `run_bench`."""
    sname = f"vibatchium_oracle_{uuid.uuid4().hex[:8]}"
    rows: list[dict] = []
    try:
        client_call("start", {"ephemeral": True, "headless": headless},
                    session=sname)
        for humanize in (False, True):
            t0 = time.time()
            rec = _run_one(client_call, sname, humanize,
                           type_sample=type_sample, scroll_dy=scroll_dy,
                           baseline=baseline)
            rec["elapsed_ms"] = int((time.time() - t0) * 1000)
            rows.append(rec)
    finally:
        for verb in ("session_close", "session_delete"):
            try:
                client_call(verb, {"name": sname})
            except Exception:  # noqa: BLE001
                pass
    return rows


# ─── rendering ───────────────────────────────────────────────────────────────

_HONESTY = (
    "_Graded against **our model** of human — literature bands until a recorded "
    "operator baseline replaces them. This **cannot** say \"we beat DataDome\"; it "
    "says whether humanize's output is obviously non-human per feature. The "
    "**structural gap** is the RAW POINTER STREAM (`raw_pointer_events`, "
    "`coalesced_max`): real hardware fires `pointerrawupdate` and batches coalesced "
    "samples into each `pointermove`; CDP-synthesised input produces **neither** — "
    "the page sees only compositor-clocked `pointermove`s. Unreachable by construction "
    "(`humanize.py:13-19`), closed only by attach-mode against a real headful Chrome. "
    "`move_dt_cv` is a diagnostic (reported, not scored): on `pointermove` it is the "
    "~60Hz display clock (dt≈16.7ms), identical for real and synthetic input — scoring "
    "it would false-positive a human. `screen_eq_client` is likewise reported — synthetic "
    "input here carries a real screen offset, so the screenX==clientX tell we assumed "
    "does **not** fire._"
)


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _band_str(spec_band: list) -> str:
    lo, hi = spec_band
    if isinstance(lo, bool) or isinstance(hi, bool):
        return f"={lo}"
    if lo is not None and hi is not None:
        return f"{_fmt(lo)}..{_fmt(hi)}"
    if lo is not None:
        return f"≥{_fmt(lo)}"
    if hi is not None:
        return f"≤{_fmt(hi)}"
    return "-"


def render_markdown(rows: list[dict], baseline: dict | None = None) -> str:
    """A per-feature OFF vs ON table with the human band and a verdict mark. Pass the
    same baseline the rows were scored against so the band column matches the marks."""
    if not rows:
        return "_no oracle runs_\n"
    by_h = {r["humanize"]: r for r in rows}
    off, on = by_h.get(False), by_h.get(True)
    baseline = baseline or HUMAN_BASELINE

    def cell(rec: dict | None, feat: str) -> str:
        if not rec:
            return "-"
        e = rec["score"]["per_feature"].get(feat, {})
        val = _fmt(e.get("value"))
        if e.get("kind") == "score" and e.get("value") is not None:
            val += " ✅" if e.get("human") else " ❌"
        elif e.get("kind") == "gap":
            val += " ⛔" if not e.get("human") else " ✅"
        return val

    lines = ["| Feature | Human band | OFF | ON | Kind |",
             "|---|---|---|---|---|"]
    for feat, spec in baseline.items():
        if spec.get("kind") == "meta":
            continue
        lines.append(
            f"| `{feat}` | {_band_str([spec.get('lo'), spec.get('hi')])} "
            f"| {cell(off, feat)} | {cell(on, feat)} | {spec['kind']} |")
    lines.append("")

    def headline(rec: dict | None, label: str) -> str:
        if not rec:
            return ""
        s = rec["score"]
        return (f"- **humanize {label}**: {s['n_human']}/{s['n_scored']} scored "
                f"features human-plausible · {rec['n_events']} events captured")
    lines.append("**Headline (score features only; gap features excluded):**")
    lines.append("")
    lines.append(headline(off, "OFF"))
    lines.append(headline(on, "ON"))
    if on:
        gap = on["score"]["gap_confirmed"]
        if gap:
            lines.append(f"- **gap confirmed** (unchanged by humanize): "
                         f"{', '.join('`' + g + '`' for g in gap)}")
    lines.append("")
    lines.append("Legend: ✅ in human band · ❌ outside it · ⛔ gap synthetic input can't clear")
    lines.append("")
    lines.append(_HONESTY)
    return "\n".join(lines) + "\n"


def render_json(rows: list[dict]) -> str:
    return _json.dumps({
        "rows": rows,
        "baseline": "literature-default",
        "generated_at": time.time(),
    }, indent=2)


# ─── human baseline recorder (real mouse, real browser) ──────────────────────
#
# The baseline that turns the literature bands into MEASURED bands cannot come
# through the daemon — CDP synthetic input is exactly what we're measuring against.
# It has to be the operator driving their OWN browser with a real mouse (which is
# also the only way to capture the raw pointer stream the gap is about). So we ship
# a self-contained page: the operator opens it, does a guided set of click / type /
# scroll trials, downloads the raw trials, and `vb oracle ingest` runs the SAME
# extractor per trial and aggregates per-feature sample lists for `load_baseline`.

# Which extracted feature each trial-kind contributes a sample of. Only these reach
# the baseline; a 'type' trial has no meaningful dwell, a 'click' trial no cadence.
_TRIAL_KIND_FEATURES = {
    "click": ["n_moves", "straightness", "dwell_ms_mean", "move_dt_cv",
              "decel_ratio", "peak_speed_px_ms", "coalesced_max", "raw_pointer_events"],
    "type": ["interkey_ms_mean", "interkey_ms_stdev"],
    "scroll": ["scroll_steps"],
}


def aggregate_trials(trials: list[dict]) -> dict:
    """Run `extract_features` over each recorded trial and collect per-feature sample
    lists, keyed by feature. The result is exactly the `{feature: [samples]}` shape
    `load_baseline()` overlays as p5–p95 bands. Pure. A `_meta` key records trial
    counts (ignored by `load_baseline`, which only touches known score features)."""
    samples: dict[str, list] = {}
    counts: dict[str, int] = {}
    for tr in trials:
        if not isinstance(tr, dict):
            continue
        kind = tr.get("kind")
        counts[kind] = counts.get(kind, 0) + 1
        feats = extract_features(tr)
        for feat in _TRIAL_KIND_FEATURES.get(kind, []):
            val = feats.get(feat)
            if val is not None:
                samples.setdefault(feat, []).append(val)
    samples["_meta"] = {"trial_counts": counts, "n_trials": len(trials)}
    return samples


_RECORD_PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>vb oracle — mouse baseline recorder</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, sans-serif; line-height: 1.5; }
  #hud { position: fixed; inset: 0 0 auto 0; padding: 10px 16px; background: Canvas;
         border-bottom: 1px solid GrayText; z-index: 10; font-size: 15px; }
  #hud b { color: Highlight; }
  #stage { position: relative; min-height: 100vh; }
  #target { position: absolute; width: 46px; height: 46px; border-radius: 50%;
            background: #3b82f6; cursor: pointer; display: none;
            box-shadow: 0 0 0 6px rgba(59,130,246,.25); }
  .panel { display: none; padding: 72px 24px 24px; max-width: 680px; }
  #phrase { font-size: 22px; margin: 14px 0; user-select: none; letter-spacing: .3px; }
  #tin { font-size: 19px; width: 100%; padding: 9px; }
  .filler { height: 240vh; display: flex; align-items: flex-end; }
  button { font-size: 16px; padding: 11px 20px; cursor: pointer; }
  code { background: rgba(128,128,128,.2); padding: 2px 6px; border-radius: 5px; }
</style></head>
<body>
<div id="hud">vb oracle · mouse baseline recorder — <span id="status">loading…</span></div>
<div id="stage">
  <div id="target"></div>
  <div id="typepanel" class="panel">
    <p>Type each line exactly, then press <b>Enter</b>. Type at your natural pace.</p>
    <div id="phrase"></div>
    <input id="tin" autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
  </div>
  <div id="scrollpanel" class="panel">
    <p>Scroll down to <b>Continue</b> and click it. The distance changes each time —
       just scroll the way you normally would; don't race it.</p>
    <div class="filler"><button id="contBtn">Continue</button></div>
  </div>
  <div id="donepanel" class="panel">
    <h2>Done — thank you.</h2>
    <p>Download the recording, then from the repo run:</p>
    <p><code>vb oracle ingest oracle-trials.json -o baseline.json</code><br>
       <code>vb oracle run --baseline baseline.json</code></p>
    <button id="dl">Download oracle-trials.json</button>
  </div>
</div>
<!-- DOM mirror of the export so an isolated-world eval (Patchright) or any
     automated harness can read the trials without the download button -->
<textarea id="vbo-export" style="display:none" aria-hidden="true"></textarea>
<script>__INSTRUMENT__</script>
<script>
(() => {
  const N_CLICKS = __N_CLICKS__, N_TYPE = __N_TYPE__, N_SCROLL = __N_SCROLL__;
  const TRIALS = [];
  window.__vboTrials = TRIALS;
  window.__vboExport = () => JSON.stringify({trials: TRIALS, ua: navigator.userAgent});
  const $ = (id) => document.getElementById(id);
  const status = $('status');
  const drain = () => { const d = (window.__vbo || []).slice();
                        if (window.__vbo) window.__vbo.length = 0; return d; };
  const push = (kind) => {
    TRIALS.push({kind, events: drain()});
    const ex = document.getElementById('vbo-export');  // cross-world DOM mirror
    if (ex) ex.value = window.__vboExport();
  };
  const PHRASES = [
    'the quick brown fox jumps over 13',
    'pack my box with five dozen jugs',
    'sphinx of black quartz judge my vow',
    'how vexingly quick daft zebras jump',
    'the five boxing wizards jump quickly',
    'bright vixens jump; dozy fowl quack 7',
    'jackdaws love my big sphinx of quartz',
    'we promptly judged antique ivory buckles',
    'a wizard’s job is to vex chumps quickly',
    'crazy fredrick bought many exotic opals',
  ];

  // phase 1 — clicks (approach trajectory + dwell)
  let clicks = 0;
  const target = $('target');
  const place = () => {
    const m = 90, w = Math.max(50, innerWidth - m * 2), h = Math.max(50, innerHeight - m * 2 - 60);
    target.style.left = (m + Math.random() * w) + 'px';
    target.style.top = (m + 60 + Math.random() * h) + 'px';
  };
  const setStatus = (label, n, total) =>
    status.innerHTML = label + ' <b>' + n + '/' + total + '</b>';
  const startClicks = () => {
    setStatus('Click the blue dot each time it moves.', 0, N_CLICKS);
    target.style.display = 'block';
    drain(); place();
    target.addEventListener('pointerup', () => {
      push('click'); clicks++;
      setStatus('Click the blue dot each time it moves.', clicks, N_CLICKS);
      if (clicks >= N_CLICKS) { target.style.display = 'none'; startType(); }
      else place();
    });
  };

  // phase 2 — typing (inter-key cadence)
  let typed = 0;
  const startType = () => {
    $('typepanel').style.display = 'block';
    const tin = $('tin'), phrase = $('phrase');
    const next = () => { phrase.textContent = PHRASES[typed % PHRASES.length];
                         tin.value = ''; tin.focus(); drain(); };
    setStatus('Type each line + Enter.', 0, N_TYPE);
    next();
    tin.addEventListener('keydown', (e) => {
      if (e.key !== 'Enter') return;
      push('type'); typed++;
      setStatus('Type each line + Enter.', typed, N_TYPE);
      if (typed >= N_TYPE) { $('typepanel').style.display = 'none'; startScroll(); }
      else next();
    });
  };

  // phase 3 — scroll (wheel cadence)
  let scrolls = 0;
  // Randomise the scroll distance each rep. A FIXED distance gets learned and the
  // operator speeds up rep-to-rep (a practice-effect confound Dima caught on the
  // first recording) — the samples then measure the page, not natural scrolling.
  const filler = document.querySelector('.filler');
  const randDist = () => { filler.style.height = (140 + Math.floor(Math.random() * 260)) + 'vh'; };
  const startScroll = () => {
    $('scrollpanel').style.display = 'block';
    setStatus('Scroll down + click Continue.', 0, N_SCROLL);
    randDist(); scrollTo(0, 0); drain();
    $('contBtn').addEventListener('click', () => {
      push('scroll'); scrolls++;
      setStatus('Scroll down + click Continue.', scrolls, N_SCROLL);
      if (scrolls >= N_SCROLL) { $('scrollpanel').style.display = 'none'; finish(); }
      else { randDist(); scrollTo(0, 0); drain(); }
    });
  };

  const finish = () => {
    status.textContent = 'complete — ' + TRIALS.length + ' trials recorded';
    $('donepanel').style.display = 'block';
    $('dl').addEventListener('click', () => {
      const blob = new Blob([window.__vboExport()], {type: 'application/json'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob); a.download = 'oracle-trials.json'; a.click();
    });
  };

  addEventListener('load', startClicks);
})();
</script>
</body></html>"""


def record_page_html(*, n_clicks: int = 20, n_type: int = 8, n_scroll: int = 8) -> str:
    """The self-contained recorder page, with the SAME capture instrumentation the
    live runner uses (single event schema) and the trial counts baked in."""
    return (_RECORD_PAGE_TEMPLATE
            .replace("__INSTRUMENT__", _INSTRUMENT_JS)
            .replace("__N_CLICKS__", str(int(n_clicks)))
            .replace("__N_TYPE__", str(int(n_type)))
            .replace("__N_SCROLL__", str(int(n_scroll))))


def write_record_page(path: str | Path, **counts: int) -> Path:
    p = Path(path)
    p.write_text(record_page_html(**counts))
    return p
