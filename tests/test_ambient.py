"""Ambient behaviour (daemon/ambient.py) — offline tests, no Chrome.

A fake page records every CDP-level call with a timestamp; the real Daemon
dispatcher and the real AmbientManager drive it. Covers: seeding (stable per
session, different across sessions/hosts), the pure path/scroll planners, the
scheduler (emits while idle, yields the instant a verb starts and never holds
entry.lock, parks past the horizon, no-op when frozen / in-flight wait / held
button / liveview takeover / unsafe target), the dispatcher hooks, the freezer
interplay, and the refusal on attach / nodriver sessions.
"""
from __future__ import annotations

import asyncio
import dataclasses
import math
import random
import statistics
import time
import types
from pathlib import Path

import pytest

from vibatchium.daemon import ambient
from vibatchium.daemon.registry import SessionEntry

# ─── fakes ────────────────────────────────────────────────────────────────


class FakeMouse:
    def __init__(self, page):
        self.page = page

    async def move(self, x, y, steps=1):
        self.page.calls.append(("move", time.monotonic(), (x, y),
                                self.page.entry.lock.locked()))
        await asyncio.sleep(self.page.latency)

    async def wheel(self, dx, dy):
        self.page.calls.append(("wheel", time.monotonic(), (dx, dy),
                                self.page.entry.lock.locked()))
        await asyncio.sleep(self.page.latency)

    # Ambient must never call any of these.
    async def click(self, *a, **k):
        raise AssertionError("ambient clicked")

    async def dblclick(self, *a, **k):
        raise AssertionError("ambient dblclicked")

    async def down(self, *a, **k):
        raise AssertionError("ambient pressed a button")

    async def up(self, *a, **k):
        raise AssertionError("ambient released a button")


class FakeKeyboard:
    def __getattr__(self, name):
        raise AssertionError(f"ambient touched the keyboard ({name})")


class FakePage:
    def __init__(self, *, unsafe: bool = False, room_down: int = 3000):
        self.calls: list = []
        self.evals: list = []
        self.unsafe = unsafe
        self.room_down = room_down
        self.latency = 0.002
        self.ptr = None
        self.mouse = FakeMouse(self)
        self.keyboard = FakeKeyboard()
        self.entry = None
        self.url = "https://example.test/"

    def is_closed(self):
        return False

    async def goto(self, *a, **k):
        raise AssertionError("ambient navigated")

    async def evaluate(self, js, arg=None):
        self.evals.append(js)
        await asyncio.sleep(self.latency)
        if js is ambient.TRACK_JS:
            return {"fresh": False, "ptr": self.ptr}
        if js is ambient.PROBE_JS:
            cur = (arg or {}).get("cursor")
            return {"visible": True, "focused": True, "scheme": "https:",
                    "vw": 1280, "vh": 800,
                    "blocks": [[100, 120, 640, 300], [100, 450, 640, 200]],
                    "scroll_y": 0, "room_down": self.room_down, "room_up": 0,
                    "tracker_fresh": False, "ptr": self.ptr,
                    "start": self.ptr or cur}
        if js is ambient.CHECK_JS:
            if self.unsafe:
                return {"ok": False, "index": 0, "reason": "tag:a"}
            return {"ok": True}
        raise AssertionError("unexpected evaluate")

    def moves(self):
        return [c for c in self.calls if c[0] == "move"]


def _daemon(monkeypatch, page=None, *, mode="launch", backend="patchright"):
    monkeypatch.setenv("VIBATCHIUM_PLUGINS", "0")
    from vibatchium.daemon.server import Daemon
    d = Daemon()
    page = page or FakePage()
    sess = types.SimpleNamespace(
        context=types.SimpleNamespace(pages=[page]), page=page, frame_ref=None,
        mode=mode, headless=True, nav_allowlist=None)
    e = SessionEntry(name="t", profile_dir=Path("/tmp/vbtest-ambient"), session=sess)
    e.flags["backend"] = backend
    page.entry = e
    d.registry._entries["t"] = e
    return d, e, page


def _fast(st, **over):
    """Shrink the timing so a test sees bursts within ~100ms."""
    base = dict(first_delay_lo_s=0.01, first_delay_hi_s=0.02, gap_median_s=0.05,
                gap_sigma=0.1, intra_pause_median_ms=5.0, speed_px_s=4000.0,
                base_ms=20.0, scroll_p=0.0)
    base.update(over)
    st.params = dataclasses.replace(st.params, **base)


async def _enable(d, e, **kw):
    kw.setdefault("seed", 42)
    d._ambient.enable("t", e, **kw)
    st = d._ambient._st["t"]
    return st


# ─── seeding + params (pure) ──────────────────────────────────────────────


def test_seed_stable_per_session_and_distinct_across_sessions_and_hosts():
    a = ambient.session_seed("work", "/p/work", machine_id="m1")
    assert a == ambient.session_seed("work", "/p/work", machine_id="m1")
    assert a != ambient.session_seed("work-2", "/p/work-2", machine_id="m1")
    assert a != ambient.session_seed("work", "/p/work", machine_id="m2")  # fleet de-twin
    assert 0 <= a < 2 ** 64


def test_params_deterministic_per_seed_and_differ_across_seeds():
    p1, p2 = ambient.derive_params(1), ambient.derive_params(1)
    assert p1 == p2
    q = ambient.derive_params(2)
    assert p1.gap_median_s != q.gap_median_s
    assert p1.drift_median_px != q.drift_median_px


def test_params_stay_in_their_documented_ranges():
    for seed in range(200):
        p = ambient.derive_params(seed)
        assert 3.0 <= p.gap_median_s <= 7.0
        assert 0.25 <= p.first_delay_lo_s < p.first_delay_hi_s <= 1.8
        assert 12 <= p.drift_median_px <= 45
        assert 0.08 <= p.scroll_p <= 0.20
        assert p.notch_px == 100


def test_horizon_default_env_clamp_and_garbage(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_AMBIENT_HORIZON", raising=False)
    assert ambient.ambient_horizon() == ambient.DEFAULT_HORIZON_S
    monkeypatch.setenv("VIBATCHIUM_AMBIENT_HORIZON", "120")
    assert ambient.ambient_horizon() == 120.0
    assert ambient.ambient_horizon(30) == 30.0           # explicit wins
    assert ambient.ambient_horizon(1) == ambient.MIN_HORIZON_S
    assert ambient.ambient_horizon(10 ** 9) == ambient.MAX_HORIZON_S
    assert ambient.ambient_horizon("nope") == ambient.DEFAULT_HORIZON_S
    assert ambient.ambient_horizon(float("nan")) == ambient.DEFAULT_HORIZON_S


def test_gap_first_is_reaction_delay_and_later_gaps_stretch_with_idle():
    p = ambient.derive_params(5)
    r = random.Random(0)
    for _ in range(50):
        g = ambient.next_gap_s(p, r, 0.0, first=True)
        assert p.first_delay_lo_s <= g <= p.first_delay_hi_s
    early = statistics.median(ambient.next_gap_s(p, random.Random(i), 0.0, first=False)
                              for i in range(300))
    late = statistics.median(ambient.next_gap_s(p, random.Random(i), 120.0, first=False)
                             for i in range(300))
    assert late > early
    assert all(0.6 <= ambient.next_gap_s(p, random.Random(i), 0, first=False) <= 30
               for i in range(300))


# ─── path / scroll planners (pure) ────────────────────────────────────────


def _path_metrics(start, path):
    pts = [start] + [(x, y) for x, y, _ in path]
    seg = [math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    return math.dist(pts[0], pts[-1]) / sum(seg), seg


def test_plan_move_integer_pixels_lands_on_target_no_zero_moves():
    p = ambient.derive_params(3)
    for i in range(100):
        r = random.Random(i)
        path = ambient.plan_move((200.0, 300.0), (420.0, 330.0), r, p)
        assert path, "a 220px move must emit samples"
        assert path[-1][:2] == (420, 330)
        assert all(isinstance(x, int) and isinstance(y, int) for x, y, _ in path)
        prev = (200, 300)
        for x, y, dt in path:
            assert (x, y) != prev and dt > 0
            prev = (x, y)


def test_plan_move_shape_inside_the_oracle_bands():
    """Straightness in the oracle's literature band (0.55-0.999) and minimum-jerk
    deceleration into the target (oracle decel_ratio < 0.95)."""
    p = ambient.derive_params(9)
    straight, decel = [], []
    for i in range(200):
        path = ambient.plan_move((100.0, 100.0), (400.0, 160.0), random.Random(i), p)
        s, seg = _path_metrics((100.0, 100.0), path)
        straight.append(s)
        dts = [dt for _, _, dt in path]
        speeds = [seg[k] / dts[k] for k in range(len(seg))]
        tail = speeds[len(speeds) * 2 // 3:]
        decel.append(statistics.fmean(tail) / max(speeds))
    assert 0.55 <= statistics.median(straight) <= 0.999
    assert max(straight) < 1.0                                   # never ruler-straight
    assert statistics.median(decel) < 0.95


def test_plan_move_paths_are_not_identical_templates():
    p = ambient.derive_params(1)
    a = ambient.plan_move((0.0, 0.0), (300.0, 0.0), random.Random(1), p)
    b = ambient.plan_move((0.0, 0.0), (300.0, 0.0), random.Random(2), p)
    assert a != b


def test_plan_micro_is_tiny():
    for i in range(50):
        path = ambient.plan_micro((500.0, 500.0), random.Random(i))
        assert 2 <= len(path) <= 4
        assert all(abs(x - 500) <= 8 and abs(y - 500) <= 4 for x, y, _ in path)


def test_scroll_column_covers_every_8px_and_refuses_offscreen():
    col = ambient.scroll_column((300, 200), 200, down=True, vh=800)
    assert col[-1] == [300, 400] and len(col) == 25
    ys = [200] + [y for _, y in col]
    assert max(ys[i + 1] - ys[i] for i in range(len(ys) - 1)) <= 8
    up = ambient.scroll_column((300, 500), 100, down=False, vh=800)
    assert up[-1] == [300, 400]
    assert ambient.scroll_column((300, 700), 300, down=True, vh=800) is None
    assert ambient.scroll_column((300, 100), 100, down=False, vh=800) is None


def test_clamp_keeps_clear_of_exit_intent_top_edge():
    x, y = ambient.clamp_to_viewport((-50, -50), 1280, 800)
    assert y > ambient.TOP_MARGIN_PX and x > ambient.EDGE_MARGIN_PX


def test_js_probes_only_read_and_listen():
    """The in-page JS must never synthesise events, focus, select or navigate."""
    for js in (ambient.PROBE_JS, ambient.CHECK_JS, ambient.TRACK_JS):
        for bad in ("dispatchEvent", ".click(", ".focus(", "select(", "location.",
                    "scrollTo(", "scrollBy(", "scrollTop =", "innerHTML",
                    "submit(", "open("):
            if bad == "location." and "location.protocol" in js:
                assert js.count("location.") == js.count("location.protocol")
                continue
            assert bad not in js, f"{bad} in ambient JS"


# ─── scheduler ────────────────────────────────────────────────────────────


async def test_emits_moves_while_idle_and_only_moves(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    st = await _enable(d, e)
    _fast(st)
    used = e.last_used_at
    await asyncio.sleep(0.6)
    assert page.moves(), "no ambient movement while idle"
    assert {c[0] for c in page.calls} <= {"move", "wheel"}
    assert not any(c[3] for c in page.calls), "ambient held entry.lock"
    assert e.last_used_at == used, "ambient stamped activity (would block idle-freeze)"
    assert st.stats["moves"] == len(page.moves())
    d._ambient.disable("t")


async def test_yields_instantly_when_a_verb_starts(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    page.latency = 0.01
    st = await _enable(d, e)
    _fast(st)
    for _ in range(200):
        if page.moves():
            break
        await asyncio.sleep(0.01)
    assert page.moves()
    d._ambient.verb_begin("t", "click")
    t_begin = time.monotonic()
    await asyncio.sleep(0.5)
    after = [c for c in page.calls if c[1] > t_begin]
    assert after == [], f"ambient acted during a verb: {after[:3]}"
    assert st.burst is None or st.burst.done()
    d._ambient.verb_end("t", "click")
    n = len(page.calls)
    await asyncio.sleep(0.6)
    assert len(page.calls) > n, "ambient did not resume after the verb"
    d._ambient.disable("t")


async def test_never_acts_while_lock_held_by_anyone(monkeypatch):
    """gpu_info / the freezer / liveview takeover take entry.lock without a verb
    hook — the per-dispatch gate must still see it."""
    d, e, page = _daemon(monkeypatch)
    st = await _enable(d, e)
    _fast(st)
    async with e.lock:
        page.calls.clear()
        await asyncio.sleep(0.5)
        assert page.calls == []
    d._ambient.disable("t")


@pytest.mark.parametrize("block", ["frozen", "inflight", "button", "takeover", "unsafe"])
async def test_no_op_when_blocked(monkeypatch, block):
    page = FakePage(unsafe=(block == "unsafe"))
    d, e, page = _daemon(monkeypatch, page)
    if block == "frozen":
        e.frozen = True
    elif block == "inflight":
        e.inflight = 1
    elif block == "button":
        e.flags["_buttons_down"] = True
    elif block == "takeover":
        d._liveview_server = types.SimpleNamespace(takeover=True, _clients={"t": {object()}})
    st = await _enable(d, e)
    _fast(st)
    await asyncio.sleep(0.5)
    assert page.moves() == [] and not [c for c in page.calls if c[0] == "wheel"]
    if block == "frozen":
        # parked: not even probing a SIGSTOPped renderer
        assert ambient.PROBE_JS not in page.evals
    if block == "unsafe":
        assert st.stats["skipped"].get("unsafe", 0) >= 1
    d._ambient.disable("t")


async def test_stops_after_the_idle_horizon(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    st = await _enable(d, e)
    _fast(st)
    st.horizon_s = 0.4                       # below the user clamp, test-only
    await asyncio.sleep(0.4)
    await asyncio.sleep(0.15)                # let an in-flight burst yield
    n = len(page.calls)
    await asyncio.sleep(0.6)
    assert len(page.calls) == n, "ambient kept going past the horizon"
    assert d._ambient.status("t")["active"] is False
    # a new verb re-opens the window
    d._ambient.verb_begin("t", "go")
    d._ambient.verb_end("t", "go")
    await asyncio.sleep(0.3)
    assert len(page.calls) > n
    d._ambient.disable("t")


async def test_continues_from_the_position_the_page_saw(monkeypatch):
    """A plain Playwright click parked the pointer at (640, 300) behind our back:
    the next ambient sample must start next to it, not at our stale record."""
    d, e, page = _daemon(monkeypatch)
    ambient.set_cursor(e, (100.0, 700.0), page)
    page.ptr = [640.0, 300.0]
    st = await _enable(d, e)
    _fast(st, micro_p=0.0, reposition_p=0.0)
    for _ in range(200):
        if page.moves():
            break
        await asyncio.sleep(0.01)
    x, y = page.moves()[0][2]
    assert math.dist((x, y), (640, 300)) < 60
    d._ambient.disable("t")


async def test_scroll_wheel_only_when_column_is_clean(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    page.ptr = [400.0, 200.0]
    st = await _enable(d, e)
    _fast(st, scroll_p=1.0)
    await asyncio.sleep(0.5)
    wheels = [c for c in page.calls if c[0] == "wheel"]
    assert wheels and all(c[2][1] in (100, -100) for c in wheels)
    d._ambient.disable("t")

    page2 = FakePage(unsafe=True)
    d2, e2, page2 = _daemon(monkeypatch, page2)
    page2.ptr = [400.0, 200.0]
    st2 = await _enable(d2, e2)
    _fast(st2, scroll_p=1.0)
    await asyncio.sleep(0.4)
    assert not [c for c in page2.calls if c[0] == "wheel"]
    d2._ambient.disable("t")


async def test_scroll_suppressed_after_a_viewport_pinning_verb(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    page.ptr = [400.0, 200.0]
    st = await _enable(d, e)
    _fast(st, scroll_p=1.0)
    d._ambient.verb_begin("t", "screenshot")
    d._ambient.verb_end("t", "screenshot")
    assert st.scroll_pinned
    await asyncio.sleep(0.5)
    assert not [c for c in page.calls if c[0] == "wheel"]
    d._ambient.disable("t")


async def test_disable_and_reap_stop_the_task(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    st = await _enable(d, e)
    _fast(st)
    await asyncio.sleep(0.2)
    d._ambient.disable("t")
    await asyncio.sleep(0.05)
    n = len(page.calls)
    await asyncio.sleep(0.4)
    assert len(page.calls) == n and st.task.done()
    assert "ambient" not in e.flags

    st = await _enable(d, e)
    del d.registry._entries["t"]             # session closed
    d._ambient.reap()
    await asyncio.sleep(0.05)
    assert st.task.done() and not d._ambient.is_on("t")


async def test_refused_on_attach_and_nodriver(monkeypatch):
    d, e, _ = _daemon(monkeypatch, mode="attach")
    with pytest.raises(RuntimeError, match="attach"):
        d._ambient.enable("t", e)
    d, e, _ = _daemon(monkeypatch, backend="nodriver")
    with pytest.raises(RuntimeError, match="patchright"):
        d._ambient.enable("t", e)


async def test_seed_derived_from_session_when_not_given(monkeypatch):
    d, e, _ = _daemon(monkeypatch)
    out = d._ambient.enable("t", e)
    assert out["seed_source"] == "derived"
    assert out["seed"] == ambient.session_seed("t", e.profile_dir)
    d._ambient.disable("t")


# ─── dispatcher + freezer wiring ──────────────────────────────────────────


class _Recorder:
    def __init__(self):
        self.events = []

    def verb_begin(self, name, cmd):
        self.events.append(("begin", cmd))

    def verb_end(self, name, cmd, args=None):
        self.events.append(("end", cmd))

    def reap(self):
        self.events.append(("reap", None))

    def is_on(self, name):
        return False


async def test_dispatcher_hooks_session_and_page_wait_verbs_only(monkeypatch):
    d, e, _ = _daemon(monkeypatch)
    rec = _Recorder()
    d._ambient = rec

    async def ok(daemon, args):
        return {"ok": True}

    d._handlers["probe"] = ok
    d._handlers["wait_selector"] = ok
    await d.dispatch({"cmd": "probe", "args": {"_session": "t"}, "id": "1"})
    await d.dispatch({"cmd": "wait_selector", "args": {"_session": "t"}, "id": "2"})
    await d.dispatch({"cmd": "status", "args": {"_session": "t"}, "id": "3"})
    await d.dispatch({"cmd": "session_list", "args": {}, "id": "4"})
    assert rec.events == [("begin", "probe"), ("end", "probe"),
                          ("begin", "wait_selector"), ("end", "wait_selector"),
                          ("reap", None)]


async def test_dispatcher_hooks_unlocked_plugin_verbs(monkeypatch):
    # An `unlocked` plugin verb takes no entry.lock, so without the hook
    # nothing would stop ambient moving the pointer under it.
    d, e, _ = _daemon(monkeypatch)
    rec = _Recorder()
    d._ambient = rec

    async def ok(daemon, args):
        return {}

    d.add_verb("x.drive", ok, lock="unlocked")
    d.add_verb("x.config", ok, lock="registry")
    d.add_verb("x.locked", ok)
    for i, verb in enumerate(("x.drive", "x.config", "x.locked")):
        out = await d.dispatch({"cmd": verb, "args": {"_session": "t"}, "id": str(i)})
        assert out["ok"], out
    assert rec.events == [("begin", "x.drive"), ("end", "x.drive"), ("reap", None),
                          ("begin", "x.locked"), ("end", "x.locked")]


async def test_dispatcher_pairs_end_even_when_the_verb_fails(monkeypatch):
    d, e, _ = _daemon(monkeypatch)
    rec = _Recorder()
    d._ambient = rec

    async def boom(daemon, args):
        raise RuntimeError("x")

    d._handlers["probe"] = boom
    out = await d.dispatch({"cmd": "probe", "args": {"_session": "t"}, "id": "1"})
    assert out["ok"] is False
    assert rec.events == [("begin", "probe"), ("end", "probe")]


async def test_refused_calls_never_reach_the_ambient_hooks(monkeypatch):
    # The lease and goal-caps gates run first: a call they refuse must not
    # cancel a burst, reset the idle clock or clear the scroll pin.
    d, e, _ = _daemon(monkeypatch)
    rec = _Recorder()
    d._ambient = rec

    async def ok(daemon, args):
        return {}

    d._handlers["click"] = ok
    e.lease_grant("other-bot", 60)
    out = await d.dispatch({"cmd": "click", "args": {"_session": "t"}, "id": "1"})
    assert out["ok"] is False
    e.lease_clear()
    e.flags["goal_caps"] = "nav"
    out = await d.dispatch({"cmd": "click", "args": {"_session": "t"}, "id": "2"})
    assert out["ok"] is False and "goal caps" in out["error"]
    assert rec.events == []


async def test_status_poll_is_not_activity(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    st = await _enable(d, e)
    st.scroll_pinned = True
    st.last_verb_end -= 50
    before = st.last_verb_end
    out = await d.dispatch({"cmd": "humanize_ambient", "id": "1",
                            "args": {"_session": "t", "mode": "status"}})
    assert out["ok"] and out["result"]["idle_s"] >= 50
    assert st.last_verb_end == before and st.scroll_pinned and st.busy == 0
    d._ambient.disable("t")


async def test_scroll_pin_survives_reads_until_a_verb_acts(monkeypatch):
    # screenshot → extract → (idle) → coordinate click: the reads in between
    # must not let ambient scroll the page under the coordinates.
    d, e, page = _daemon(monkeypatch)
    page.ptr = [400.0, 200.0]
    st = await _enable(d, e)
    _fast(st, scroll_p=1.0)

    async def ok(daemon, args):
        return {}

    for verb in ("screenshot", "extract", "text", "eval", "url", "mouse", "click"):
        d._handlers[verb] = ok

    async def run(verb, **a):
        out = await d.dispatch({"cmd": verb, "id": verb,
                                "args": {"_session": "t", **a}})
        assert out["ok"], out

    await run("screenshot")
    for verb in ("extract", "text", "eval", "url"):
        await run(verb)
        assert st.scroll_pinned, f"{verb} unpinned the viewport"
    await run("humanize_ambient", mode="status")
    assert st.scroll_pinned
    await asyncio.sleep(0.5)
    assert not [c for c in page.calls if c[0] == "wheel"], "scrolled under a pin"
    await run("mouse", action="click", x=10, y=10)
    assert st.scroll_pinned                       # a coordinate verb re-pins
    await run("click", target="#x")
    assert not st.scroll_pinned                   # acting releases it
    d._ambient.disable("t")


async def test_no_motion_after_a_pointer_parking_verb(monkeypatch):
    # hover / focus / mouse move leave the pointer somewhere ON PURPOSE (a
    # hover-opened menu): no drift, no scroll until the next verb that acts —
    # reads in between keep it parked.
    d, e, page = _daemon(monkeypatch)
    page.ptr = [400.0, 200.0]
    st = await _enable(d, e)
    _fast(st, scroll_p=0.5)

    async def ok(daemon, args):
        return {}

    for verb in ("hover", "focus", "mouse", "text", "click"):
        d._handlers[verb] = ok

    async def run(verb, **a):
        out = await d.dispatch({"cmd": verb, "id": verb,
                                "args": {"_session": "t", **a}})
        assert out["ok"], out

    for park in (("hover", {}), ("focus", {}), ("mouse", {"action": "move"})):
        await run(park[0], **park[1])
        assert st.pointer_parked, park
        await run("text")
        assert st.pointer_parked, f"a read released the pointer after {park}"
        n = len(page.calls)
        await asyncio.sleep(0.4)
        assert len(page.calls) == n, f"ambient moved after {park}"
        assert d._ambient.status("t")["pointer_parked"] is True
        await run("click", target="#x")
        assert not st.pointer_parked
    await asyncio.sleep(0.4)
    assert page.calls, "ambient never resumed after the click"
    await run("mouse", action="click", x=5, y=5)
    assert not st.pointer_parked, "a mouse click is not a park"
    d._ambient.disable("t")


async def test_coordinate_verbs_wait_out_a_fresh_wheel_notch(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    st = await _enable(d, e)
    seen = {}

    async def stamp(daemon, args):
        seen["at"] = time.monotonic()
        return {}

    d._handlers["screenshot"] = stamp
    d._handlers["text"] = stamp
    st.last_wheel_at = time.monotonic()
    t0 = st.last_wheel_at
    out = await d.dispatch({"cmd": "screenshot", "args": {"_session": "t"}, "id": "1"})
    assert out["ok"]
    assert seen["at"] - t0 >= ambient.WHEEL_SETTLE_S, "acted on a still-scrolling page"
    # a non-coordinate verb doesn't wait, and an old notch costs nothing
    st.last_wheel_at = time.monotonic()
    t1 = time.monotonic()
    await d.dispatch({"cmd": "text", "args": {"_session": "t"}, "id": "2"})
    assert seen["at"] - t1 < ambient.WHEEL_SETTLE_S
    st.last_wheel_at = time.monotonic() - 1
    assert await d._ambient.settle("t", "screenshot") == 0.0
    d._ambient.disable("t")


async def test_wheel_notch_is_stamped(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    page.ptr = [400.0, 200.0]
    st = await _enable(d, e)
    _fast(st, scroll_p=1.0)
    await asyncio.sleep(0.5)
    wheels = [c for c in page.calls if c[0] == "wheel"]
    assert wheels, "no wheel in 0.5 s at scroll_p=1"
    assert st.last_wheel_at >= wheels[-1][1]
    d._ambient.disable("t")


async def test_freezer_skips_one_poll_mid_burst(monkeypatch):
    from vibatchium.daemon import freeze
    monkeypatch.setattr(freeze, "_find_renderers", lambda p: [100])
    monkeypatch.setattr(freeze, "_starttime", lambda pid: 5000)
    monkeypatch.setattr(freeze.os, "kill", lambda pid, sig: None)
    d, e, _ = _daemon(monkeypatch)
    e.last_used_at = time.time() - 999
    e.ambient_busy = True
    assert await d._freeze_if_idle("t", after=5.0) == 0 and not e.frozen
    e.ambient_busy = False
    assert await d._freeze_if_idle("t", after=5.0) == 1 and e.frozen


async def test_humanize_ambient_handler_on_status_off(monkeypatch):
    d, e, page = _daemon(monkeypatch)
    out = await d.dispatch({"cmd": "humanize_ambient", "id": "1",
                            "args": {"_session": "t", "mode": "on", "seed": 7,
                                     "horizon_s": 30}})
    assert out["ok"], out
    r = out["result"]
    assert r["ambient"] and r["seed"] == 7 and r["horizon_s"] == 30 and r["scroll"]
    st = await d.dispatch({"cmd": "humanize_ambient", "id": "2",
                           "args": {"_session": "t", "mode": "status"}})
    assert st["result"]["ambient"] is True and "stats" in st["result"]
    off = await d.dispatch({"cmd": "humanize_ambient", "id": "3",
                            "args": {"_session": "t", "mode": "off"}})
    assert off["result"]["ambient"] is False and not d._ambient.is_on("t")
    bad = await d.dispatch({"cmd": "humanize_ambient", "id": "4",
                            "args": {"_session": "t", "mode": "loud"}})
    assert bad["ok"] is False


async def test_agent_surface_gets_no_seed_and_a_capped_horizon(monkeypatch):
    from vibatchium import fspolicy
    monkeypatch.delenv("VIBATCHIUM_AMBIENT_HORIZON", raising=False)
    d, e, page = _daemon(monkeypatch)
    scope = {fspolicy.SCOPE_ARG: {"roots": None, "cwd": "/tmp"}}

    async def on(**a):
        return await d.dispatch({"cmd": "humanize_ambient", "id": "1",
                                 "args": {"_session": "t", "mode": "on", **a}})

    out = await on(seed=7, **scope)
    assert out["ok"] is False and "operator-only" in out["error"], out
    assert not d._ambient.is_on("t")
    out = await on(horizon_s=1800, **scope)
    assert out["ok"], out
    assert out["result"]["horizon_s"] == ambient.DEFAULT_HORIZON_S
    assert out["result"]["horizon_clamped"] is True
    assert out["result"]["seed_source"] == "derived"
    out = await on(horizon_s=30, **scope)                 # shorter is fine
    assert out["result"]["horizon_s"] == 30 and "horizon_clamped" not in out["result"]
    # the operator's configured horizon is the agent ceiling
    monkeypatch.setenv("VIBATCHIUM_AMBIENT_HORIZON", "600")
    out = await on(horizon_s=1800, **scope)
    assert out["result"]["horizon_s"] == 600
    # the CLI / SDK keep both knobs
    out = await on(seed=7, horizon_s=1800)
    assert out["ok"] and out["result"]["seed"] == 7 and out["result"]["horizon_s"] == 1800
    d._ambient.disable("t")


def test_mcp_schema_offers_no_seed():
    from vibatchium.mcp_server import TOOLS
    schema = next(t[2] for t in TOOLS if t[0] == "humanize_ambient")
    assert "seed" not in schema["properties"]
    assert "horizon_s" in schema["properties"]


def test_default_off_and_in_the_humanize_bucket():
    from vibatchium.caps import CAP_BUCKETS
    from vibatchium.mcp_server import TOOLS
    assert "humanize_ambient" in CAP_BUCKETS["input"]
    assert any(t[0] == "humanize_ambient" for t in TOOLS)
    e = SessionEntry(name="x", profile_dir=Path("/tmp/x"), session=None)
    assert "ambient" not in e.flags and e.ambient_busy is False


# ─── oracle ambient lane (pure) ───────────────────────────────────────────


def _drain(events, origin=1_000_000.0):
    return {"events": events, "origin": origin}


def test_oracle_ambient_features_off_vs_on_and_teleport():
    from vibatchium import oracle
    win = [(1_000_100.0, 1_000_500.0), (1_000_600.0, 1_001_000.0)]
    click = [{"type": "pmove", "t": 50.0, "x": 300.0, "y": 200.0},
             {"type": "pdown", "t": 51.0, "x": 300.0, "y": 200.0},
             {"type": "click", "t": 52.0}]
    off = oracle.extract_ambient_features(_drain(click), win)
    assert off["idle_pointer_per_gap"] == 0 and off["zero_mouse_gap_frac"] == 1.0
    assert off["entry_jump_px"] is None and off["idle_clicks"] == 0

    drift = [{"type": "pmove", "t": 200.0 + 17 * i, "x": 301 + 3 * i, "y": 200 + (i % 2)}
             for i in range(8)]
    drift2 = [{"type": "pmove", "t": 700.0 + 17 * i, "x": 330 + 2 * i, "y": 205}
              for i in range(5)]
    on = oracle.extract_ambient_features(_drain(click + drift + drift2), win)
    assert on["idle_pointer_per_gap"] == 6.5 and on["zero_mouse_gap_frac"] == 0.0
    assert on["entry_jump_px"] < 10 and on["idle_integer_coord_frac"] == 1.0
    s = oracle.score_ambient(on)
    assert s["safety_violations"] == []
    assert s["per_feature"]["entry_jump_px"]["human"]

    teleport = [{"type": "pmove", "t": 200.0, "x": 900, "y": 600}]
    tp = oracle.extract_ambient_features(_drain(click + teleport), win)
    assert tp["entry_jump_px"] > 60
    assert not oracle.score_ambient(tp)["per_feature"]["entry_jump_px"]["human"]


def test_oracle_ambient_safety_audit_counts_only_idle_windows():
    from vibatchium import oracle
    win = [(1_000_100.0, 1_000_500.0)]
    ev = [{"type": "click", "t": 10.0}, {"type": "key", "t": 20.0},       # verb time
          {"type": "click", "t": 150.0}, {"type": "key", "t": 160.0},     # idle!
          {"type": "iover", "t": 170.0}, {"type": "iscroll", "t": 180.0},
          {"type": "focus", "t": 190.0}, {"type": "sel", "t": 195.0},
          {"type": "input", "t": 199.0}]
    f = oracle.extract_ambient_features(_drain(ev), win)
    assert (f["idle_clicks"], f["idle_keys"], f["idle_interactive_hovers"],
            f["idle_inner_scrolls"], f["idle_focus_changes"], f["idle_selections"],
            f["idle_inputs"]) == (1, 1, 1, 1, 1, 1, 1)
    viol = oracle.score_ambient(f)["safety_violations"]
    assert set(viol) == {"idle_clicks", "idle_keys", "idle_interactive_hovers",
                         "idle_inner_scrolls", "idle_focus_changes",
                         "idle_selections", "idle_inputs"}
    md = oracle.render_ambient_markdown([
        {"ambient": False, "features": f, "score": oracle.score_ambient(f), "idle_s": 1}])
    assert "safety violations: idle_clicks" in md
    assert oracle.extract_ambient_features({}, win)["n_gaps"] == 1     # total, never raises
