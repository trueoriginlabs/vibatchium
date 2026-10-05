"""0.20.0 — `vb fleet-check` (cross-session twin measurement) + `vb persona`.

Pure tests first (vector diff/scoring, variant parsing, persona generation and its
coherence rules, persistence, launch-arg plumbing, registry seams) — no browser.
One live test at the end spawns two throwaway sessions on the test daemon and
asserts the probe runs end to end and reports.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from vibatchium import fleet, persona
from vibatchium.client import call


def _mk_async(val):
    async def _f(*a, **k):
        return val
    return _f


@pytest.fixture(autouse=True)
def stores(monkeypatch, tmp_path_factory):
    """In-process tests get a private managed-profiles dir and pins store —
    never the real ~/.config/vibatchium."""
    from vibatchium.daemon import paths
    out = SimpleNamespace(profiles=tmp_path_factory.mktemp("profiles"),
                          pins=tmp_path_factory.mktemp("pins"))
    monkeypatch.setattr(paths, "PROFILES_DIR", out.profiles)
    monkeypatch.setattr(paths, "PINS_DIR", out.pins)
    return out


# ─── vector diff / scoring ──────────────────────────────────────────────


def _rows_by_surface(rows):
    return {r["surface"]: r for r in rows}


def test_compare_groups_identical_partial_distinct():
    vecs = {
        "a": {"user_agent": "UA", "screen": {"w": 800}, "fonts": {"n": 3, "hash": "x"}},
        "b": {"user_agent": "UA", "screen": {"w": 1920}, "fonts": {"n": 3, "hash": "x"}},
        "c": {"user_agent": "UA", "screen": {"w": 1366}, "fonts": {"n": 2, "hash": "y"}},
    }
    rows = _rows_by_surface(fleet.compare_vectors(vecs))
    assert rows["user_agent"]["status"] == "identical"
    assert rows["user_agent"]["pair_equal"] == 1.0
    assert rows["screen"]["status"] == "distinct"
    assert rows["screen"]["pair_equal"] == 0.0
    assert rows["fonts"]["status"] == "partial"
    # 3 sessions = 3 pairs, one of which (a,b) is equal.
    assert rows["fonts"]["pair_equal"] == pytest.approx(1 / 3, abs=1e-3)
    assert rows["fonts"]["groups"] == [["a", "b"], ["c"]]


def test_compare_is_order_insensitive_for_dict_values():
    vecs = {"a": {"screen": {"w": 1, "h": 2}}, "b": {"screen": {"h": 2, "w": 1}}}
    assert _rows_by_surface(fleet.compare_vectors(vecs))["screen"]["status"] == "identical"


def test_compare_handles_missing_and_single_and_private_keys():
    vecs = {"a": {"screen": 1, "_url": "x"}, "b": {"_url": "y"}}
    rows = _rows_by_surface(fleet.compare_vectors(vecs))
    assert rows["screen"]["status"] == "single"
    assert rows["screen"]["pair_equal"] is None
    assert rows["window"]["status"] == "missing"
    assert "_url" not in rows, "underscore keys are probe metadata, not surfaces"


def test_compare_keeps_unknown_surfaces():
    rows = _rows_by_surface(fleet.compare_vectors({"a": {"novel": 1}, "b": {"novel": 1}}))
    assert rows["novel"]["status"] == "identical"
    assert rows["novel"]["verdict"] == "?"


def test_errors_compare_like_values():
    # A probe failure in both sessions is still a twin (same build, same failure).
    e = {"err": "no_gl_context"}
    rows = _rows_by_surface(fleet.compare_vectors({"a": {"webgl_pixels": e},
                                                  "b": {"webgl_pixels": dict(e)}}))
    assert rows["webgl_pixels"]["status"] == "identical"


def test_twin_score_all_vs_actionable_and_lies_excluded():
    vecs = {
        "a": {"user_agent": "UA", "screen": 1, "lies": []},
        "b": {"user_agent": "UA", "screen": 2, "lies": []},
    }
    rows = fleet.compare_vectors(vecs)
    # user_agent identical (1.0), screen distinct (0.0); lies never count.
    assert fleet.twin_score(rows) == 50.0
    # user_agent is intentionally shared (verdict d) -> only screen remains.
    assert "user_agent" in fleet.INTENTIONALLY_SHARED
    assert fleet.twin_score(rows, actionable_only=True) == 0.0


def test_twin_score_none_when_nothing_comparable():
    assert fleet.twin_score(fleet.compare_vectors({"a": {"screen": 1}})) is None


def test_full_fleet_of_identical_vectors_scores_100():
    v = {s: f"val-{s}" for s in fleet.SURFACES if s != "lies"}
    s = fleet.summarize({"a": dict(v), "b": dict(v), "c": dict(v)})
    assert s["twin_score"] == 100.0
    assert s["twin_score_actionable"] == 100.0
    assert s["counts"]["identical"] == len(v)


def test_lies_by_session_normalizes():
    out = fleet.lies_by_session({"a": {"lies": ["hc"]}, "b": {"lies": {"err": "t"}},
                                 "c": {}})
    assert out == {"a": ["hc"], "b": [{"err": "t"}], "c": []}


def test_every_surface_has_a_known_verdict():
    for name, (verdict, why) in fleet.SURFACES.items():
        assert verdict in {"a", "b", "c", "d", "-"}, name
        assert why, name
    # The two persona surfaces are the only 'b' — keep the catalogue honest.
    assert {k for k, (v, _) in fleet.SURFACES.items() if v == "b"} == {"screen", "window"}


def test_probe_js_has_fonts_inlined_and_every_surface():
    assert "%FONTS%" not in fleet.PROBE_JS
    assert json.dumps(list(fleet.FONT_LIST)) in fleet.PROBE_JS
    for s in fleet.SURFACES:
        assert f"'{s}'" in fleet.PROBE_JS or f"out.{s}" in fleet.PROBE_JS, s


def test_render_markdown_and_comparison_smoke():
    before = fleet.summarize({"a": {"screen": 1, "lies": []}, "b": {"screen": 1, "lies": ["hc"]}},
                             {"a": {"gpu": "intel"}})
    after = fleet.summarize({"a": {"screen": 1, "lies": []}, "b": {"screen": 2, "lies": []}})
    md = fleet.render_markdown(before)
    assert "twin score" in md and "| screen | identical |" in md
    assert '"b": ["hc"]' in md and "gpu=intel" in md
    cmp_md = fleet.render_comparison(before, after)
    assert "| screen | identical (1.0) | distinct (0.0) |" in cmp_md


@pytest.mark.parametrize("spec,want", [
    ("default", {}),
    ("", {}),
    ("gpu", {"gpu": True}),
    ("gpu=NVIDIA", {"gpu": "nvidia"}),
    ("geo=de, persona", {"geo": "DE", "persona": True}),
    ("tz=Europe/Berlin", {"tz": "Europe/Berlin"}),
])
def test_parse_variant(spec, want):
    assert fleet.parse_variant(spec) == want


@pytest.mark.parametrize("spec", ["nope", "geo", "tz=", "scale=2"])
def test_parse_variant_rejects(spec):
    with pytest.raises(ValueError):
        fleet.parse_variant(spec)


def test_run_fleet_check_cleans_up_spawned_sessions_on_error():
    calls = []

    def fake_call(cmd, args=None, *, session=None):
        calls.append((cmd, (args or {}).get("name") or session))
        if cmd == "start":
            raise RuntimeError("boom")
        return {}

    with pytest.raises(RuntimeError):
        fleet.run_fleet_check(fake_call, variants=[{}, {}], tag="t")
    closed = [n for c, n in calls if c in ("session_close", "session_delete")]
    assert closed == ["fleet-t-0", "fleet-t-0"], "spawned session leaked"


def test_run_fleet_check_probes_all_and_reports_errors():
    def fake_call(cmd, args=None, *, session=None):
        if cmd == "eval":
            if session == "fleet-t-1":
                raise RuntimeError("renderer gone")
            return {"value": {"screen": 1, "lies": []}}
        return {}

    res = fleet.run_fleet_check(fake_call, sessions=["live"], variants=[{}, {}],
                                tag="t", url="http://x/")
    assert sorted(res["sessions"]) == ["fleet-t-0", "live"]
    assert "fleet-t-1" in res["errors"]
    assert res["meta"]["live"] == {"existing": True}


# ─── persona: generation + coherence ────────────────────────────────────


def test_generate_persona_is_deterministic_per_seed():
    assert persona.generate_persona("abc") == persona.generate_persona("abc")
    seeds = {json.dumps(persona.generate_persona(f"s{i}"), sort_keys=True)
             for i in range(40)}
    assert len(seeds) > 30, "personas barely vary across seeds"


def test_every_generated_persona_is_coherent():
    for i in range(2000):
        _assert_coherent(persona.generate_persona(f"seed-{i}"))


def _assert_coherent(p):
    assert persona.validate_persona(p) is p
    sw, sh = p["screen"]
    ww, wh = p["window"]
    wx, wy = p["position"]
    top, bottom = p["work_area"]["top"], p["work_area"]["bottom"]
    assert (sw, sh) in {(w, h) for w, h, _ in persona.SCREENS}
    assert 0 <= wx and wx + ww <= sw
    assert top <= wy and wy + wh <= sh - bottom
    if (ww, wh) != (sw, sh - top - bottom):
        assert ww >= min(persona.MIN_WINDOW[0], sw)
        assert wh >= min(persona.MIN_WINDOW[1], sh - top - bottom)


def test_screen_distribution_is_plausible_and_bounded():
    for w, h, weight in persona.SCREENS:
        assert weight > 0 and w > h, (w, h)
        # every entry fits the smallest max viewport we've measured (UHD 620
        # 16384, SwiftShader 8192) by a wide margin and stays DPR-1 desktop
        assert w <= 2560 and h <= 1440


@pytest.mark.parametrize("bad", [
    {},
    {"v": 99},
    {"v": 1, "screen": [800, 600], "window": [900, 500], "position": [0, 0],
     "work_area": {"top": 0, "bottom": 0}},                 # window wider than screen
    {"v": 1, "screen": [1920, 1080], "window": [1920, 1080], "position": [0, 0],
     "work_area": {"top": 32, "bottom": 0}},                # window over the top bar
    {"v": 1, "screen": [1920, 1080], "window": [1000, 800], "position": [0, 300],
     "work_area": {"top": 0, "bottom": 44}},                # window under the panel
    {"v": 1, "screen": [99999, 1080], "window": [100, 100], "position": [0, 0],
     "work_area": {"top": 0, "bottom": 0}},                 # absurd screen
    {"v": 1, "screen": ["x", 1080], "window": [100, 100], "position": [0, 0],
     "work_area": {"top": 0, "bottom": 0}},
    {"v": 1, "screen": [1920, 1080], "window": [100, 100], "position": [0, 0],
     "work_area": {"top": 0, "bottom": 0}, "gpu_node": 7},
    [1, 2], "str", 42,
])
def test_validate_rejects_incoherent(bad):
    assert persona.validate_persona(bad) is None


def test_launch_args_shape():
    p = {"v": 1, "screen": [1920, 1080], "work_area": {"top": 32, "bottom": 0},
         "window": [1600, 900], "position": [40, 72], "gpu_node": None}
    assert persona.persona_launch_args(p) == [
        "--screen-info={1920x1080 workAreaTop=32}",
        "--window-size=1600,900", "--window-position=40,72"]
    p["work_area"] = {"top": 0, "bottom": 44}
    assert persona.persona_launch_args(p)[0] == "--screen-info={1920x1080 workAreaBottom=44}"
    p["work_area"] = {"top": 0, "bottom": 0}
    assert persona.persona_launch_args(p)[0] == "--screen-info={1920x1080}"


def test_gpu_node_balances_least_used():
    nodes = ["intel", "nvidia"]
    assert persona.generate_persona("x", gpu_nodes=nodes,
                                    node_usage={"intel": 2, "nvidia": 1})["gpu_node"] == "nvidia"
    assert persona.generate_persona("x", gpu_nodes=nodes,
                                    node_usage={"intel": 0, "nvidia": 1})["gpu_node"] == "intel"
    assert persona.generate_persona("x", gpu_nodes=[])["gpu_node"] is None
    # tie: seeded, but always one of the available nodes
    for i in range(20):
        assert persona.generate_persona(f"t{i}", gpu_nodes=nodes)["gpu_node"] in nodes


# ─── persona: persistence ───────────────────────────────────────────────


def test_save_load_round_trip_perms_and_none(stores):
    p = persona.generate_persona("rt")
    pdir = stores.profiles / "new"
    persona.save_session_persona(pdir, p)
    f = pdir / "persona.json"
    assert persona.load_session_persona(pdir) == p
    assert (f.stat().st_mode & 0o777) == 0o600
    persona.save_session_persona(pdir, None)
    assert not f.exists()
    assert persona.load_session_persona(pdir) is None


def test_unmanaged_profile_persona_lives_in_the_pins_store(tmp_path, stores):
    p = persona.generate_persona("pin")
    pdir = tmp_path / "custom"
    persona.save_session_persona(pdir, p)
    assert not (pdir / "persona.json").exists()
    [entry] = list(stores.pins.iterdir())
    assert (entry / "persona.json").exists()
    assert persona.resolve_persona(pdir) == p


def test_planted_persona_in_a_caller_chosen_dir_is_ignored(tmp_path, caplog):
    pdir = tmp_path / "custom"
    pdir.mkdir()
    (pdir / "persona.json").write_text(json.dumps(persona.generate_persona("x")))
    with caplog.at_level("WARNING"):
        assert persona.resolve_persona(pdir, name="s") is None
    assert "ignoring persona.json" in caplog.text


def test_save_refuses_incoherent(tmp_path):
    with pytest.raises(ValueError):
        persona.save_session_persona(tmp_path, {"v": 1})


@pytest.mark.parametrize("payload", ["not json", "42", '"x"', "[1]", '{"v": 1}'])
def test_load_degrades_to_none_on_corrupt(stores, payload):
    pdir = stores.profiles / "c"
    pdir.mkdir(exist_ok=True)
    (pdir / "persona.json").write_text(payload)
    assert persona.load_session_persona(pdir) is None
    assert persona.resolve_persona(pdir) is None


def test_ensure_creates_once_and_is_stable(tmp_path, monkeypatch):
    from vibatchium import gpu
    monkeypatch.setattr(gpu, "available_gpu_nodes", lambda: [])
    pdir = tmp_path / "profiles" / "work"
    a = persona.ensure_session_persona(pdir)
    b = persona.ensure_session_persona(pdir)
    assert a == b, "persona re-derived on a second call"
    c = persona.ensure_session_persona(pdir, reroll=True)
    assert c["seed"] != a["seed"]
    assert persona.load_session_persona(pdir) == c


def test_ensure_alternates_nodes_across_sibling_profiles(stores, monkeypatch):
    from vibatchium import gpu
    monkeypatch.setattr(gpu, "available_gpu_nodes", lambda: ["intel", "nvidia"])
    root = stores.profiles
    picked = []
    for i in range(4):
        pdir = root / f"acct{i}"
        gpu.save_session_gpu(pdir, {"on": True, "node": None})
        picked.append(persona.ensure_session_persona(pdir)["gpu_node"])
    assert sorted(picked) == ["intel", "intel", "nvidia", "nvidia"], picked
    # an explicit pin on a sibling counts toward usage too
    pinned = root / "pinned"
    gpu.save_session_gpu(pinned, {"on": True, "node": "nvidia"})
    usage = persona._node_usage(root)
    assert usage == {"intel": 2, "nvidia": 3}


def test_node_usage_never_scans_the_parent_of_a_custom_profile(tmp_path, stores,
                                                               monkeypatch):
    # `start --profile /tmp/x` used to make the balancer walk all of /tmp.
    from vibatchium import gpu
    monkeypatch.setattr(gpu, "available_gpu_nodes", lambda: ["intel", "nvidia"])
    for i in range(3):            # strangers next to the custom profile
        gpu.save_session_gpu(tmp_path / f"stranger{i}", {"on": True, "node": "intel"})
    custom = tmp_path / "custom"
    gpu.save_session_gpu(custom, {"on": True, "node": None})
    assert persona._node_usage(exclude=custom) == {}
    # ...but a custom profile configured through the pins store does count
    other = tmp_path / "other-custom"
    gpu.save_session_gpu(other, {"on": True, "node": None})
    persona.save_session_persona(other, persona.generate_persona(
        "o", gpu_nodes=["nvidia"]))
    assert persona._node_usage(exclude=custom) == {"nvidia": 1}
    assert persona.ensure_session_persona(custom)["gpu_node"] == "intel"


# ─── launch plumbing (no real Chrome) ───────────────────────────────────


class _FakePage:
    def on(self, *a, **k):
        pass


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]


class _FakePw:
    def __init__(self, sink):
        async def _lpc(**kw):
            sink.clear()
            sink.update(kw)
            return _FakeContext()
        self.chromium = SimpleNamespace(launch_persistent_context=_lpc)


async def _capture(monkeypatch, tmp_path, **kw):
    from vibatchium.daemon import browser as B
    sink = {}
    monkeypatch.setattr(B, "coherent_headless_ua", _mk_async(None))
    monkeypatch.setattr(B, "_wire_page_tracking", lambda s: None)
    monkeypatch.setattr(B._file_guard, "install", _mk_async(None))
    sess = await B.launch_session(tmp_path, pw=_FakePw(sink), **kw)
    return sink, sess


async def test_persona_launch_adds_switches_headless_only(monkeypatch, tmp_path):
    p = persona.generate_persona("plumb")
    sink, sess = await _capture(monkeypatch, tmp_path, headless=True, persona=p)
    for a in persona.persona_launch_args(p):
        assert a in sink["args"]
    assert sink["no_viewport"] is True
    assert sess.persona == p

    sink, sess = await _capture(monkeypatch, tmp_path, headless=False, persona=p)
    assert not any(a.startswith("--screen-info") for a in sink["args"] or [])
    assert sess.persona is None


async def test_default_launch_has_no_persona_switches(monkeypatch, tmp_path):
    sink, sess = await _capture(monkeypatch, tmp_path, headless=True)
    assert not any(a.startswith(("--screen-info", "--window-size", "--window-position"))
                   for a in sink["args"])
    assert sess.persona is None


async def test_scaled_launch_ignores_persona(monkeypatch, tmp_path):
    p = persona.generate_persona("scaled")
    sink, sess = await _capture(monkeypatch, tmp_path, headless=True, persona=p,
                                device_scale_factor=2)
    assert not any(a.startswith("--screen-info") for a in sink["args"])
    assert sess.persona is None


async def test_backends_forward_persona_only_when_set(monkeypatch, tmp_path):
    from vibatchium.daemon import backends as _backends
    seen = {}

    async def _fake(profile_dir, **kw):
        seen.clear()
        seen.update(kw)
        return SimpleNamespace()

    monkeypatch.setattr(_backends, "launch_session", _fake)
    await _backends.launch("patchright", tmp_path, headless=True)
    assert "persona" not in seen, "default launch grew a keyword (breaks fakes)"
    await _backends.launch("patchright", tmp_path, headless=True, persona={"v": 1})
    assert seen["persona"] == {"v": 1}


async def test_launch_for_rereads_persona_on_relaunch(monkeypatch, tmp_path):
    from vibatchium.daemon import backends as _backends
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    monkeypatch.setattr(reg, "_ensure_pw", _mk_async(object()))
    seen = {}

    async def _fake_launch(backend, profile_dir, **kw):
        seen.clear()
        seen.update(kw)
        return SimpleNamespace(mode="launch")

    monkeypatch.setattr(_backends, "launch", _fake_launch)
    await reg._launch_for("s", profile_dir=tmp_path, headless=True, backend="patchright")
    assert "persona" not in seen
    p = persona.generate_persona("relaunch")
    persona.save_session_persona(tmp_path, p)
    await reg._launch_for("s", profile_dir=tmp_path, headless=True, backend="patchright")
    assert seen["persona"] == p


async def test_persona_request_never_claims_a_prewarm(monkeypatch, tmp_path):
    from vibatchium.daemon import backends as _backends
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    warm = SimpleNamespace(profile_dir=tmp_path, headless=True, mode="launch")
    fresh = SimpleNamespace(profile_dir=tmp_path, headless=True, mode="launch")
    reg._warm_sessions["s"] = warm
    monkeypatch.setattr(_backends, "close", _mk_async(None))
    monkeypatch.setattr(reg, "_launch_for", _mk_async(fresh))
    persona.save_session_persona(tmp_path, persona.generate_persona("warm"))
    entry = await reg.create("s", profile_dir=tmp_path, headless=True)
    assert entry.session is fresh


def test_persona_node_fills_an_unpinned_gpu_session_only(monkeypatch, tmp_path):
    from vibatchium import gpu
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    monkeypatch.setattr(gpu, "gpu_available", lambda: True)
    monkeypatch.setattr(gpu, "egl_vendor_for_node",
                        lambda n: f"/x/{n}.json" if n in ("intel", "nvidia") else None)
    p = persona.generate_persona("node", gpu_nodes=["nvidia"])
    persona.save_session_persona(tmp_path, p)

    gpu.save_session_gpu(tmp_path, {"on": False, "node": None})
    assert reg._load_session_overrides("s", tmp_path)[2:] == (False, None)
    gpu.save_session_gpu(tmp_path, {"on": True, "node": None})
    assert reg._load_session_overrides("s", tmp_path)[2:] == (True, "nvidia")
    gpu.save_session_gpu(tmp_path, {"on": True, "node": "intel"})
    assert reg._load_session_overrides("s", tmp_path)[2:] == (True, "intel"), \
        "persona overrode an explicit gpu.json pin"


# ─── live: two throwaway sessions on the test daemon ────────────────────


def test_fleet_check_live_two_sessions():
    """The probe runs end to end on real Chrome and reports every surface. Two
    default sessions on one box MUST twin on the screen; one with a persona must
    not, and neither may show a main-vs-worker lie."""
    res = fleet.run_fleet_check(call, variants=[{}, {"persona": True}])
    try:
        assert res["errors"] == {}, res["errors"]
        assert len(res["sessions"]) == 2
        rows = _rows_by_surface(res["rows"])
        for s in ("user_agent", "screen", "window", "canvas_2d", "webgl_renderer",
                  "audio", "fonts", "hardware_concurrency", "lies"):
            assert rows[s]["n"] == 2, s
        assert rows["user_agent"]["status"] == "identical"
        assert rows["screen"]["status"] == "distinct", rows["screen"]["sample"]
        assert all(v == [] for v in res["lies"].values()), res["lies"]
        assert 0 < res["twin_score"] < 100
        md = fleet.render_markdown(res)
        assert "twin score" in md
    finally:
        # run_fleet_check already closes + deletes its sessions; make sure.
        from vibatchium.daemon.paths import PROFILES_DIR
        for n in res.get("sessions", []):
            assert not (PROFILES_DIR / n).exists(), f"leaked profile {n}"


# ─── persona verbs target the session's REAL profile dir ────────────────


def _daemon_with_session(monkeypatch, profile_dir):
    monkeypatch.setenv("VIBATCHIUM_PLUGINS", "0")
    from vibatchium.daemon.registry import SessionEntry
    from vibatchium.daemon.server import Daemon
    d = Daemon()
    if profile_dir is not None:
        sess = SimpleNamespace(mode="launch", headless=True, persona=None)
        d.registry._entries["pw"] = SessionEntry(name="pw", profile_dir=profile_dir,
                                                 session=sess)
    return d


async def test_persona_set_follows_a_running_custom_profile(tmp_path, monkeypatch):
    from vibatchium import gpu
    monkeypatch.setattr(gpu, "available_gpu_nodes", lambda: [])
    custom = tmp_path / "custom-prof"
    d = _daemon_with_session(monkeypatch, custom)
    out = await d.dispatch({"id": "1", "cmd": "persona_set",
                            "args": {"_session": "pw", "on": True}})
    assert out["ok"], out
    assert out["result"]["profile"] == str(custom)
    assert persona.load_session_persona(custom) is not None
    info = await d.dispatch({"id": "2", "cmd": "persona_info",
                             "args": {"_session": "pw"}})
    assert info["result"]["configured"] is True


async def test_persona_set_profile_arg_for_a_stopped_session(tmp_path, monkeypatch):
    from vibatchium import gpu
    monkeypatch.setattr(gpu, "available_gpu_nodes", lambda: [])
    custom = tmp_path / "later"
    d = _daemon_with_session(monkeypatch, None)
    out = await d.dispatch({"id": "1", "cmd": "persona_set",
                            "args": {"_session": "pw", "on": True,
                                     "profile": str(custom)}})
    assert out["ok"], out
    assert persona.load_session_persona(custom) is not None
    off = await d.dispatch({"id": "2", "cmd": "persona_set",
                            "args": {"_session": "pw", "on": False,
                                     "profile": str(custom)}})
    assert off["ok"] and persona.load_session_persona(custom) is None
