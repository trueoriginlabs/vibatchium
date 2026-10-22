"""0.18.0 — `vb oracle` offline gate + live plumbing.

Two tiers, same shape as test_bench_offline:
  - PURE: the feature extractor + scorer + baseline over SYNTHETIC event buffers.
    A hand-shaped gesture must read human on the score features; a teleport must
    read synthetic and must CONFIRM the two gap features (coalesced + CDP coord).
    No daemon, no Chrome — deterministic.
  - INTEGRATION: run_oracle over the conftest daemon, proving instrument → drive →
    drain → extract end to end, and that humanize ON moves the timing/trajectory
    features while the coalesced gap stays unclearable either way.

The bands are our MODEL of human, not a vendor's score — the tool cannot claim to
beat DataDome, and the tests assert exactly that scope: humanize's output leaves the
synthetic band, and the gap it can't close stays open.
"""
from __future__ import annotations

import json
import math

from vibatchium import oracle
from vibatchium.client import call


# ─── synthetic buffers ───────────────────────────────────────────────────────

def _human_buffer() -> dict:
    """A hand-shaped gesture: curved multi-sample approach with jittered timing and
    hardware coalescing, a ~100ms click dwell, varied typing cadence, a multi-tick
    scroll, and a real window offset (screenX != clientX)."""
    events = []
    t = 0.0
    n = 40
    for i in range(n):
        frac = i / (n - 1)
        x = 100 + 300 * frac
        y = 100 + 200 * frac - 60 * math.sin(math.pi * frac)  # bow out → curved
        dt = 10.0 if i % 2 else 6.5                            # jitter → CV > 0.15
        t += dt
        events.append({"type": "pmove", "t": t, "x": x, "y": y,
                       "co": 3 if i % 3 == 0 else 2, "ets": t})
        # real hardware also emits the un-coalesced raw stream
        events.append({"type": "praw", "t": t + 0.5, "x": x, "y": y, "co": None, "ets": t})
    t += 12
    for ty in ("pdown", "mdown"):
        events.append({"type": ty, "t": t, "x": 400, "y": 300, "btn": 0,
                       "sx": 1400, "cx": 400, "pxv": 400, "ets": t})  # sx != cx
    t += 100  # dwell
    for ty in ("pup", "mup"):
        events.append({"type": ty, "t": t, "x": 400, "y": 300, "btn": 0, "ets": t})
    for i in range(20):
        t += 130.0 if i % 2 else 95.0  # mean ~112, stdev ~17
        events.append({"type": "key", "t": t, "ets": t})
    for i in range(12):
        t += 16
        events.append({"type": "wheel", "t": t, "dx": 0, "dy": 40 + i, "ets": t})
    return {"events": events, "ua": "test"}


def _humanize_buffer() -> dict:
    """What humanize actually emits: the human buffer's curve + timing, but WITHOUT the
    raw pointer stream synthetic input cannot produce (no praw, co==1). It DOES carry a
    real screen offset (screenX != clientX) — the coordinate tell we once assumed does
    not fire on CDP input (measured). This is the realistic ON pass."""
    buf = _human_buffer()
    buf["events"] = [e for e in buf["events"] if e.get("type") != "praw"]  # no raw stream
    for e in buf["events"]:
        if "co" in e:
            e["co"] = 1
        if e.get("sx") is not None:
            e["sx"] = e["cx"] + 10          # real window offset, as CDP input carries
    return buf


def _teleport_buffer() -> dict:
    """Synthetic automation: one move, instant click, instant fill, one scroll jump, no
    coalescing, no raw stream. Carries a real screen offset (screenX != clientX) — like
    measured CDP input, which does NOT exhibit the screenX==clientX tell we assumed."""
    events = [{"type": "pmove", "t": 0.0, "x": 400, "y": 300, "co": 1, "ets": 0.0},
              {"type": "pdown", "t": 1.0, "x": 400, "y": 300, "btn": 0,
               "sx": 410, "cx": 400, "pxv": 400, "ets": 1.0},
              {"type": "pup", "t": 2.0, "x": 400, "y": 300, "btn": 0, "ets": 2.0}]
    t = 2.0
    for _ in range(20):
        t += 0.4
        events.append({"type": "key", "t": t, "ets": t})
    events.append({"type": "wheel", "t": t + 1, "dx": 0, "dy": 700, "ets": t + 1})
    return {"events": events, "ua": "test"}


# ─── PURE: extraction ────────────────────────────────────────────────────────

def test_extract_human_buffer_shapes():
    f = oracle.extract_features(_human_buffer())
    assert f["n_moves"] == 40
    assert 0.55 < f["straightness"] < 0.999          # curved, not a straight jump
    assert 90 <= f["dwell_ms_mean"] <= 110            # ~100ms hold
    assert f["n_clicks"] == 1
    assert 90 <= f["interkey_ms_mean"] <= 140
    assert f["interkey_ms_stdev"] > 12
    assert f["scroll_steps"] == 12
    assert f["coalesced_max"] == 3
    assert f["raw_pointer_events"] == 40             # hardware raw stream present
    assert f["screen_eq_client"] is False            # real window offset


def test_extract_teleport_buffer_shapes():
    f = oracle.extract_features(_teleport_buffer())
    assert f["n_moves"] == 1
    assert f["straightness"] is None                 # <2 samples → no trajectory
    assert f["move_dt_cv"] is None
    assert f["dwell_ms_mean"] < 5                     # instant click
    assert f["interkey_ms_mean"] < 5                  # instant fill
    assert f["scroll_steps"] == 1
    assert f["coalesced_max"] == 1
    assert f["raw_pointer_events"] == 0              # no raw stream from CDP input
    assert f["screen_eq_client"] is False            # synthetic input still carries an offset


def test_extract_is_total_on_empty():
    f = oracle.extract_features({"events": []})
    assert f["n_events"] == 0
    assert f["n_moves"] == 0
    assert f["dwell_ms_mean"] is None
    assert f["straightness"] is None


def test_extract_is_total_on_malformed():
    # the "never raises" contract must hold on CORRUPT buffers too, not just empty:
    # null dy, a non-comparable t, and coordinate-less move samples all crashed before.
    for buf in (
        {"events": [{"type": "wheel", "t": 0.0, "dy": None}]},          # null dy
        {"events": [{"type": "pmove", "t": None, "x": 1, "y": 2},       # t=None -> dropped
                    {"type": "pmove", "t": 5.0, "x": 3, "y": 4}]},
        {"events": [{"type": "pmove", "t": 0.0},                        # no x/y
                    {"type": "pmove", "t": 10.0}]},
    ):
        f = oracle.extract_features(buf)                                # must not raise
        assert isinstance(f, dict) and "n_events" in f


def test_extract_falls_back_to_mousemove_when_no_pointer():
    buf = {"events": [
        {"type": "mmove", "t": 0.0, "x": 0, "y": 0, "ets": 0.0},
        {"type": "mmove", "t": 9.0, "x": 5, "y": 8, "ets": 9.0},
        {"type": "mmove", "t": 20.0, "x": 12, "y": 15, "ets": 20.0},
    ]}
    f = oracle.extract_features(buf)
    assert f["n_moves"] == 3
    assert f["coalesced_max"] is None                # mousemove carries no `co`


# ─── PURE: scoring ───────────────────────────────────────────────────────────

def test_score_human_buffer_reads_human_and_no_gap_confirmed():
    s = oracle.score_features(oracle.extract_features(_human_buffer()))
    # every scored feature with a value lands in the human band
    assert s["n_human"] == s["n_scored"]
    assert s["n_scored"] >= 6
    # a coalesced-3, offset-window gesture confirms NEITHER gap
    assert s["gap_confirmed"] == []


def test_score_teleport_reads_synthetic_and_confirms_the_gaps():
    s = oracle.score_features(oracle.extract_features(_teleport_buffer()))
    assert s["n_human"] == 0
    assert s["n_scored"] >= 4
    # the raw pointer stream (pointerrawupdate + coalesced) is the structural gap;
    # the coordinate + timing rows are reported diagnostics, NOT gaps.
    assert set(s["gap_confirmed"]) == {"coalesced_max", "raw_pointer_events"}
    assert s["per_feature"]["screen_eq_client"]["kind"] == "report"
    assert s["per_feature"]["move_dt_cv"]["kind"] == "report"


def test_in_band_boolean_gap_logic():
    # screen_eq_client: human is False (they differ); True is the synthetic tell
    assert oracle._in_band(False, False, False) is True
    assert oracle._in_band(True, False, False) is False
    # coalesced_max: human is >= 2
    assert oracle._in_band(1, 2, None) is False
    assert oracle._in_band(3, 2, None) is True
    assert oracle._in_band(None, 2, None) is False


def test_headline_excludes_gap_and_report_features():
    # a gesture that is perfect on scored features but synthetic on the gaps must
    # still show n_human == n_scored (the gap rows never drag the headline).
    s = oracle.score_features(oracle.extract_features(_human_buffer()))
    per = s["per_feature"]
    assert per["coalesced_max"]["kind"] == "gap"
    assert per["raw_pointer_events"]["kind"] == "gap"
    assert per["peak_speed_px_ms"]["kind"] == "report"
    assert per["move_dt_cv"]["kind"] == "report"      # compositor clock, not a tell
    assert per["screen_eq_client"]["kind"] == "report"
    # report/gap features are present but not in the scored denominator
    scored_keys = [k for k, e in per.items()
                   if e["kind"] == "score" and e["value"] is not None]
    for k in ("coalesced_max", "raw_pointer_events", "peak_speed_px_ms",
              "move_dt_cv", "screen_eq_client"):
        assert k not in scored_keys
    assert s["n_scored"] == len(scored_keys)


# ─── PURE: baseline overlay ──────────────────────────────────────────────────

def test_load_baseline_defaults_without_file():
    base = oracle.load_baseline()
    assert base["dwell_ms_mean"]["lo"] == 40
    assert base["dwell_ms_mean"]["hi"] == 300


def test_load_baseline_overlays_recorded_p5_p95(tmp_path):
    # a recorded operator whose clicks dwell 200-260ms should re-band dwell to their
    # own distribution, superseding the literature default.
    rec = tmp_path / "human.json"
    rec.write_text(json.dumps({"dwell_ms_mean": list(range(200, 261))}))
    base = oracle.load_baseline(rec)
    assert base["dwell_ms_mean"]["lo"] >= 200
    assert base["dwell_ms_mean"]["hi"] <= 260
    assert "recorded operator baseline" in base["dwell_ms_mean"]["source"]
    # a 100ms (literature-human) dwell now reads OUT of this operator's band
    s = oracle.score_features({"dwell_ms_mean": 100}, base)
    assert s["per_feature"]["dwell_ms_mean"]["human"] is False


def test_load_baseline_missing_file_falls_back(tmp_path):
    base = oracle.load_baseline(tmp_path / "nope.json")
    assert base["dwell_ms_mean"]["lo"] == 40


def test_load_baseline_widens_degenerate_band(tmp_path):
    # an operator who dwells IDENTICALLY every click must not get a [V,V] knife-edge
    # that then rejects their own next 120.5ms reading as non-human.
    rec = tmp_path / "flat.json"
    rec.write_text(json.dumps({"dwell_ms_mean": [120.0] * 20}))
    base = oracle.load_baseline(rec)
    assert base["dwell_ms_mean"]["hi"] > base["dwell_ms_mean"]["lo"]   # widened
    s = oracle.score_features({"dwell_ms_mean": 120.5}, base)
    assert s["per_feature"]["dwell_ms_mean"]["human"] is True


def test_load_baseline_p95_not_over_trimmed_small_n(tmp_path):
    # with the old floor-truncated index, an n=8 operator's OWN slowest reading fell
    # outside the band (hi pinned to the 2nd-highest). Interpolated p95 must not.
    rec = tmp_path / "op.json"
    rec.write_text(json.dumps({"dwell_ms_mean": [180, 185, 190, 195, 200, 205, 210, 260]}))
    base = oracle.load_baseline(rec)
    assert base["dwell_ms_mean"]["hi"] > 210          # not pinned to the 2nd-highest


def test_load_baseline_is_total_on_malformed(tmp_path):
    # a hand-edited / partial baseline must never crash the run: bad features keep the
    # literature band, valid ones still overlay.
    rec = tmp_path / "bad.json"
    rec.write_text(json.dumps({
        "dwell_ms_mean": 99,                                # scalar not list -> skipped
        "n_moves": ["junk", None] + list(range(30, 60)),   # junk + 30 valid numbers
    }))
    base = oracle.load_baseline(rec)                        # must not raise
    assert base["dwell_ms_mean"]["lo"] == 40               # literature kept (scalar skipped)
    assert base["n_moves"]["lo"] != 8                      # overlaid from the numeric samples


def test_load_baseline_survives_bad_json(tmp_path):
    rec = tmp_path / "notjson.json"
    rec.write_text("{ this is not json ]")
    base = oracle.load_baseline(rec)                        # must not raise
    assert base["dwell_ms_mean"]["lo"] == 40               # literature default preserved


# ─── PURE: rendering ─────────────────────────────────────────────────────────

def _rows_from_buffers() -> list[dict]:
    def rec(humanize, buf):
        feats = oracle.extract_features(buf)
        return {"humanize": humanize, "n_events": feats["n_events"],
                "features": feats, "score": oracle.score_features(feats),
                "elapsed_ms": 1}
    return [rec(False, _teleport_buffer()), rec(True, _humanize_buffer())]


def test_render_markdown_carries_honesty_and_gap_note():
    md = oracle.render_markdown(_rows_from_buffers())
    assert "cannot" in md.lower() and "datadome" in md.lower()
    assert "attach-mode" in md.lower()
    assert "humanize.py:13-19" in md
    # OFF (teleport) confirms the gaps; the note names them
    assert "gap confirmed" in md.lower()
    assert "coalesced_max" in md


def test_render_json_labels_provenance():
    # no baseline -> literature-default
    payload = json.loads(oracle.render_json(_rows_from_buffers()))
    assert isinstance(payload["rows"], list) and len(payload["rows"]) == 2
    assert payload["baseline"] == "literature-default"
    # with a recorded baseline the label must reflect the source, not a hardcoded lie
    base = oracle.load_baseline()
    base["__source__"] = {"kind": "meta", "recorded": "/tmp/op.json"}
    labelled = json.loads(oracle.render_json(_rows_from_buffers(), base))
    assert labelled["baseline"] == "/tmp/op.json"


def test_render_empty():
    assert "no oracle" in oracle.render_markdown([])


# ─── INTEGRATION: run_oracle over the conftest daemon ────────────────────────

def test_run_oracle_contrast_and_gap(_daemon_lifecycle):
    """Drive the gesture set OFF then ON on a throwaway ephemeral session. Humanize
    must move trajectory + timing into the human range; the coalesced gap must stay
    unclearable either way; nothing may leak."""
    rows = oracle.run_oracle(call, headless=True)
    assert len(rows) == 2
    off = next(r for r in rows if r["humanize"] is False)["features"]
    on = next(r for r in rows if r["humanize"] is True)["features"]

    # trajectory: humanize turns a teleport into a many-sample curved approach
    assert on["n_moves"] > off["n_moves"]
    assert on["n_moves"] >= 8

    # dwell: instant off, human-band on
    assert off["dwell_ms_mean"] is None or off["dwell_ms_mean"] < 40
    assert 40 <= on["dwell_ms_mean"] <= 300

    # keystroke cadence: instant off, human-band on
    assert on["interkey_ms_mean"] >= 55
    assert on["interkey_ms_mean"] > (off["interkey_ms_mean"] or 0)

    # scroll: one jump off, several ticks on
    assert on["scroll_steps"] > off["scroll_steps"]

    # THE GAP: the raw pointer stream is unreachable via CDP either way — no
    # pointerrawupdate and no coalesced batching, humanize on or off
    assert off["raw_pointer_events"] == 0
    assert on["raw_pointer_events"] == 0
    assert off["coalesced_max"] in (None, 1)
    assert on["coalesced_max"] in (None, 1)
    on_gap = next(r for r in rows if r["humanize"])["score"]["gap_confirmed"]
    assert "coalesced_max" in on_gap
    assert "raw_pointer_events" in on_gap

    # ephemeral session cleaned up
    live = {s["name"] for s in call("session_list")["sessions"]}
    assert not any(n.startswith("vibatchium_oracle_") for n in live)


# ─── recorder: aggregate_trials + record page ────────────────────────────────

def _click_trial(i):
    events, t = [], 0.0
    for k in range(12):
        t += 8 + (k % 3)
        events.append({"type": "pmove", "t": t, "x": 100 + k * 10,
                       "y": 100 + k * 5, "co": 2, "ets": t})
    t += 5
    events.append({"type": "pdown", "t": t, "x": 220, "y": 160, "btn": 0,
                   "sx": 1400, "cx": 220, "pxv": 220, "ets": t})
    t += 80 + i * 4  # vary dwell so p5/p95 is a real range
    events.append({"type": "pup", "t": t, "x": 220, "y": 160, "btn": 0, "ets": t})
    return {"kind": "click", "events": events}


def _type_trial(i):
    events, t = [], float(i)
    for k in range(15):
        t += 90.0 + (30 if k % 2 else 5)
        events.append({"type": "key", "t": t, "ets": t})
    return {"kind": "type", "events": events}


def _scroll_trial():
    events, t = [], 0.0
    for _ in range(9):
        t += 16
        events.append({"type": "wheel", "t": t, "dx": 0, "dy": 40, "ets": t})
    return {"kind": "scroll", "events": events}


def test_aggregate_trials_collects_per_feature_samples():
    trials = ([_click_trial(i) for i in range(6)]
              + [_type_trial(i) for i in range(6)]
              + [_scroll_trial() for _ in range(6)])
    agg = oracle.aggregate_trials(trials)
    assert len(agg["dwell_ms_mean"]) == 6       # one dwell per click trial
    assert len(agg["n_moves"]) == 6
    assert len(agg["straightness"]) == 6
    assert len(agg["interkey_ms_mean"]) == 6    # one per type trial, not click/scroll
    assert len(agg["interkey_ms_stdev"]) == 6
    # a scroll trial is many flicks long; scroll bands come from swipes only
    assert "scroll_steps" not in agg
    assert agg["_meta"]["trial_counts"] == {"click": 6, "type": 6, "scroll": 6}
    # cross-kind isolation: a click trial contributes no cadence, a type trial no dwell
    assert "interkey_ms_mean" not in oracle.aggregate_trials([_click_trial(0)])
    assert "dwell_ms_mean" not in oracle.aggregate_trials([_type_trial(0)])


def test_aggregate_feeds_load_baseline(tmp_path):
    agg = oracle.aggregate_trials([_click_trial(i) for i in range(8)])
    f = tmp_path / "b.json"
    f.write_text(json.dumps(agg))
    base = oracle.load_baseline(f)
    # dwell (8 samples) re-bands to the recorded operator's p5-p95
    assert base["dwell_ms_mean"]["lo"] is not None
    assert "recorded operator baseline" in base["dwell_ms_mean"]["source"]
    # _meta must not crash the overlay or become a phantom feature
    assert "_meta" not in base


def _wheel_burst(t0, dys, gap, dm=0):
    return [{"type": "wheel", "t": t0 + i * gap, "dx": 0, "dy": d, "dm": dm,
             "ets": t0 + i * gap} for i, d in enumerate(dys)]


def _swipe_trial(t0=0.0, dys=(120, 120, 120), gap=55.0, dm=0, device=None):
    # a stray pointermove rides along, as it does on the real page
    tr = {"kind": "swipe",
          "events": [{"type": "pmove", "t": t0, "x": 5, "y": 5}]
          + _wheel_burst(t0, list(dys), gap, dm)}
    if device:
        tr["device"] = device
    return tr


def test_split_wheel_bursts_cuts_at_silence():
    ev = (_wheel_burst(0, [120, 120], 50) + _wheel_burst(500, [120], 50)
          + [{"type": "pmove", "t": 20, "x": 1, "y": 1}])
    bursts = oracle.split_wheel_bursts(ev)
    assert [len(b) for b in bursts] == [2, 1]
    # a trackpad fling's 16ms tail stays one gesture
    assert len(oracle.split_wheel_bursts(_wheel_burst(0, [3.5] * 60, 16))) == 1


def test_classify_gesture_wheel_vs_trackpad():
    assert oracle.classify_gesture([120, 120, 240]) == "wheel"
    assert oracle.classify_gesture([53, 53]) == "wheel"
    assert oracle.classify_gesture([40.5, 22.1, 9.3, 2.2, 1.0]) == "trackpad"
    assert oracle.classify_gesture([12, 12, 12]) == "trackpad"   # too small for a notch


def test_swipe_trials_feed_per_gesture_scroll_bands():
    trials = [_swipe_trial(dys=[120] * (1 + i % 3), gap=40 + i) for i in range(6)]
    trials.append(_swipe_trial(dys=[-120, -120]))             # upward: not a swipe
    agg = oracle.aggregate_trials(trials)
    assert len(agg["scroll_steps"]) == 6
    assert agg["scroll_dy_max_step"] == [120] * 6
    assert len(agg["scroll_gap_ms_median"]) == 4              # 1-event swipes have no gap
    assert agg["_meta"]["trial_counts"] == {"swipe": 7}


def test_build_gesture_library_keeps_both_devices_and_skips():
    trials = [_swipe_trial(dys=[120, 120], gap=60) for _ in range(4)]
    trials.append(_swipe_trial(dys=[30.2, 11.5, 4.1, 1.2], gap=16))
    trials.append(_swipe_trial(dys=[-120]))                   # upward
    trials.append(_swipe_trial(dys=[3, 3], dm=1))             # Firefox line mode
    lib = oracle.build_gesture_library(trials, source="t.json")
    assert lib["devices"] == {"wheel": 4, "trackpad": 1}
    assert lib["n"] == 5 and lib["skipped"] == 2 and lib["mismatched"] == 0
    g = lib["gestures"][0]
    assert g == {"kind": "wheel", "t": [0, 60], "dy": [120, 120]}


def test_block_label_beats_the_classifier_and_is_counted():
    # a smooth-scrolling mouse can send fractional deltas; the block says wheel
    trials = [_swipe_trial(dys=[57.3, 61.0], device="wheel"),
              _swipe_trial(dys=[120, 120], device="wheel"),
              _swipe_trial(dys=[20.5, 8.1, 2.0], gap=16, device="trackpad")]
    lib = oracle.build_gesture_library(trials)
    assert [g["kind"] for g in lib["gestures"]] == ["wheel", "wheel", "trackpad"]
    assert lib["mismatched"] == 1


def test_swipe_bands_come_from_one_device():
    wheel = [_swipe_trial(dys=[120] * 2, device="wheel") for _ in range(6)]
    pad = [_swipe_trial(dys=[20.0, 9.0, 3.0, 1.0], gap=16, device="trackpad")
           for _ in range(5)]
    agg = oracle.aggregate_trials(wheel + pad)                # majority: wheel
    assert agg["scroll_dy_max_step"] == [120] * 6
    assert agg["_meta"]["swipe_device"] == "wheel"
    agg = oracle.aggregate_trials(wheel + pad, swipe_device="trackpad")
    assert agg["scroll_steps"] == [4] * 5
    assert agg["_meta"]["swipe_device"] == "trackpad"


def test_load_baseline_promotes_recorded_swipe_features(tmp_path):
    lit = oracle.load_baseline()
    assert lit["scroll_gap_ms_median"]["kind"] == "report"    # no literature band
    agg = oracle.aggregate_trials(
        [_swipe_trial(dys=[120] * 3, gap=40 + 5 * i) for i in range(8)])
    f = tmp_path / "b.json"
    f.write_text(json.dumps(agg))
    base = oracle.load_baseline(f)
    assert base["scroll_gap_ms_median"]["kind"] == "score"
    assert base["scroll_gap_ms_median"]["lo"] >= 40
    assert base["__source__"]["scroll_dy_median"] == 360
    assert base["__source__"]["swipe_device"] == "wheel"
    scored = oracle.score_features({"scroll_gap_ms_median": 16.0}, base)
    assert scored["per_feature"]["scroll_gap_ms_median"]["human"] is False


def test_run_oracle_wheels_the_recorded_swipe_size():
    wheels, ons = [], []

    def fake_call(verb, args=None, session=None):
        if verb == "humanize_on":
            ons.append(args)
        if verb == "eval":
            return {"value": {"btn": {"x": 1, "y": 1}, "input": {"x": 1, "y": 1},
                              "events": []}}
        if verb == "mouse" and args["action"] == "wheel":
            wheels.append(args["dy"])
        return {}

    base = {**oracle.HUMAN_BASELINE,
            "__source__": {"kind": "meta", "scroll_dy_median": 240.0,
                           "swipe_device": "trackpad"}}
    oracle.run_oracle(fake_call, baseline=base)
    assert wheels == [240, 240]
    assert ons == [{"scroll_device": "trackpad"}]   # replays the banded device
    wheels.clear(), ons.clear()
    oracle.run_oracle(fake_call)
    assert wheels == [700, 700]
    assert ons == [{}]


def test_record_page_swipe_phase_and_skips():
    html = oracle.record_page_html(n_clicks=0, n_type=0, n_scroll=0, n_swipe=12)
    assert "N_SWIPE = 12" in html and "__N_SWIPE__" not in html
    assert 'SWIPE_DEVICES = ["wheel", "trackpad"]' in html
    assert "SPLIT_MS = 300" in html
    assert "if (N_CLICKS <= 0) return startType();" in html
    assert "push('swipe', {device: SWIPE_DEVICES[block]})" in html
    one = oracle.record_page_html(n_swipe=5, swipe_devices=("trackpad",))
    assert 'SWIPE_DEVICES = ["trackpad"]' in one
    assert "dm: e.deltaMode" in html                         # line-mode is detectable


def test_record_page_is_self_contained_and_instrumented():
    html = oracle.record_page_html(n_clicks=5, n_type=3, n_scroll=2)
    assert html.strip().startswith("<!doctype html>")
    assert "N_CLICKS = 5" in html and "N_TYPE = 3" in html and "N_SCROLL = 2" in html
    assert "__INSTRUMENT__" not in html          # placeholder filled
    assert "pointerrawupdate" in html            # SAME capture as the runner
    assert "getCoalescedEvents" in html
    assert "window.__vboExport" in html
    # scroll distance is randomised per rep (fixes the fixed-distance practice
    # effect — a learned distance lets the operator speed up rep-to-rep)
    assert "randDist" in html and "filler.style.height" in html
    # no external resources (CSP-free, opens from file://)
    assert "http://" not in html and "https://" not in html
    assert "src=" not in html


def test_record_page_click_pipe(_daemon_lifecycle, tmp_path):
    """Drive the record page's click phase via the daemon over file://, then export
    and ingest — proving the page's state machine + drain + export + aggregate all
    connect. (CDP clicks are synthetic, so values aren't human; we assert the pipe
    produces the right per-feature sample counts, not human-ness.)"""
    page = tmp_path / "rec.html"
    page.write_text(oracle.record_page_html(n_clicks=3, n_type=2, n_scroll=2))
    s = "vibatchium_oraclerec_test"
    call("start", {"ephemeral": True, "headless": True}, session=s)
    try:
        # startClicks runs on the load event, so wait for load (not just DCL)
        call("go", {"url": page.as_uri(), "wait_until": "load"}, session=s)
        rect_js = ("(() => { const t=document.getElementById('target');"
                   " const b=t.getBoundingClientRect();"
                   " return {x:b.x+b.width/2, y:b.y+b.height/2,"
                   " disp:getComputedStyle(t).display}; })()")
        for _ in range(3):
            r = call("eval", {"expr": rect_js}, session=s)["value"]
            if r["disp"] == "none":
                break
            call("mouse", {"action": "click", "x": r["x"], "y": r["y"]}, session=s)
        # read the cross-world DOM mirror (eval is isolated-world; can't see the
        # page's main-world window.__vboExport, but DOM is shared)
        export = call("eval", {"expr": "document.getElementById('vbo-export').value"},
                      session=s)["value"]
        trials = json.loads(export)["trials"]
        assert len(trials) >= 3
        assert all(t["kind"] == "click" for t in trials[:3])
        agg = oracle.aggregate_trials(trials)
        assert len(agg.get("dwell_ms_mean", [])) >= 3   # one dwell captured per click
        assert len(agg.get("n_moves", [])) >= 3
    finally:
        call("session_close", {"name": s})
        call("session_delete", {"name": s})
