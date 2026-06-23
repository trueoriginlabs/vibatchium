"""0.11.0 — `vb bench` offline release gate.

Two tiers:
  - PURE: is_walled classification (incl. the PerimeterX branch + the legit
    "Access Denied" no-false-positive control) and the aggregation/render
    helpers. No daemon, no Chrome — deterministic.
  - INTEGRATION: run_bench against the four local fixtures over the conftest
    daemon, proving the detection + aggregation plumbing end to end. This is the
    file wired into publish.yml as a release-blocking gate: if the moat's wall
    detection regresses, a tagged build fails before it ships.

There's no real WAF locally, so a CF/DataDome/PerimeterX *title* page reads as
that defender (walled, passed=False) and the control reads as cleared — the test
asserts CLASSIFICATION, not evasion. Evasion is the manual `--live` lane.
"""
from __future__ import annotations

import json

from vibatchium import bench
from vibatchium.client import call
from vibatchium.daemon.backends import is_walled


# ─── PURE: is_walled classification + PerimeterX branch + no-false-positive ──

def test_is_walled_classifies_each_defender_by_title():
    assert is_walled("Just a moment...", None) == "cloudflare"
    assert is_walled("Checking your browser", None) == "cloudflare"
    assert is_walled("Blocked - DataDome", None) == "datadome"
    assert is_walled("You've been blocked", None) == "datadome"
    assert is_walled("Access to this page has been denied", None) == "perimeterx"
    assert is_walled("Please verify you are a human", None) == "perimeterx"


def test_is_walled_does_not_false_positive_on_legit_access_denied():
    # A bare "Access Denied" (Akamai/IIS/nginx 403) is NOT a bot wall — even
    # with a 403 status (which is_walled deliberately treats as inconclusive).
    assert is_walled("Access Denied", 403) is None
    assert is_walled("403 Forbidden", 403) is None
    assert is_walled("", None) is None
    assert is_walled("Example Domain", 200) is None


# ─── PURE: aggregation keyed on expected_waf (not runtime walled) ────────────

def _rows():
    return [
        {"target": "a", "expected_waf": "cloudflare", "walled": None,
         "passed": True, "error": None},
        {"target": "b", "expected_waf": "cloudflare", "walled": "cloudflare",
         "passed": False, "error": None},
        {"target": "c", "expected_waf": "datadome", "walled": None,
         "passed": True, "error": None},
        {"target": "d", "expected_waf": "perimeterx", "walled": None,
         "passed": None, "error": "RuntimeError: boom"},
        {"target": "e", "expected_waf": None, "walled": None,
         "passed": True, "error": None},
    ]


def test_cold_pass_rate_keys_on_expected_waf_not_runtime_walled():
    agg = bench.cold_pass_rate_by_waf(_rows())
    assert agg["cloudflare"] == {"total": 2, "tested": 2, "passed": 1,
                                 "errors": 0, "pass_rate": 0.5}
    assert agg["datadome"]["pass_rate"] == 1.0
    # the errored perimeterx row counts in total but not in the denominator
    assert agg["perimeterx"] == {"total": 1, "tested": 0, "passed": 0,
                                 "errors": 1, "pass_rate": None}
    # expected_waf=None buckets under 'control'
    assert agg["control"]["pass_rate"] == 1.0


def test_min_pass_rate_is_the_lowest_tested_bucket():
    assert bench.min_pass_rate(_rows()) == 0.5  # cloudflare bucket
    assert bench.min_pass_rate([]) is None
    assert bench.min_pass_rate(
        [{"expected_waf": "cloudflare", "passed": None, "error": "x"}]) is None


def test_min_pass_rate_excludes_the_control_bucket():
    # a walled control must NOT drag the WAF gate down — control is a baseline,
    # not an evasion target.
    rows = [
        {"expected_waf": "cloudflare", "passed": True, "error": None},
        {"expected_waf": None, "passed": False, "error": None},  # control walled
    ]
    assert bench.cold_pass_rate_by_waf(rows)["control"]["pass_rate"] == 0.0
    assert bench.min_pass_rate(rows) == 1.0  # only the cloudflare bucket counts


# ─── PURE: rendering + readme region + localhost gate ────────────────────────

def test_render_markdown_carries_the_upper_bound_caveat():
    md = bench.render_markdown(_rows())
    assert "optimistic upper bound" in md.lower()
    assert "Cold pass-rate by WAF" in md
    assert "cloudflare" in md


def test_render_json_round_trips_with_aggregate():
    payload = json.loads(bench.render_json(_rows()))
    assert payload["upper_bound"] is True
    assert payload["by_waf"]["cloudflare"]["pass_rate"] == 0.5
    assert payload["min_pass_rate"] == 0.5
    assert isinstance(payload["rows"], list)


def test_update_readme_is_idempotent(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(
        "intro\n<!-- vibatchium-bench -->\nOLD\n<!-- /vibatchium-bench -->\nrest\n")
    md = bench.render_markdown(_rows())
    assert bench.update_readme(readme, md) is True
    assert "OLD" not in readme.read_text()
    # second run with the same data is a no-op
    assert bench.update_readme(readme, md) is False
    # no markers → no change, returns False
    nomark = tmp_path / "NOMARK.md"
    nomark.write_text("nothing here\n")
    assert bench.update_readme(nomark, md) is False


def test_is_localhost_gate():
    assert bench.is_localhost("http://127.0.0.1:8000/x") is True
    assert bench.is_localhost("http://127.0.0.5:8000/x") is True  # 127.0.0.0/8
    assert bench.is_localhost("http://localhost:9/x") is True
    assert bench.is_localhost("http://[::1]:9/x") is True
    assert bench.is_localhost("https://example.com/x") is False
    assert bench.is_localhost("https://nope.cloudflare-site.test") is False
    # the masquerade the `127.` prefix used to wave through → must be non-local
    assert bench.is_localhost("http://127.0.0.1.evil.com/x") is False
    assert bench.is_localhost("http://localhost.evil.com/x") is False


def test_offline_targets_shape(local_server):
    targets = bench.offline_targets(local_server)
    names = {t["name"] for t in targets}
    assert names == {"cloudflare", "datadome", "perimeterx", "control"}
    assert all(bench.is_localhost(t["url"]) for t in targets)
    assert {t["expected_waf"] for t in targets} == {
        "cloudflare", "datadome", "perimeterx", None}


# ─── INTEGRATION: the release gate — run_bench over the 4 local fixtures ─────

def test_bench_offline_classifies_fixtures(local_server, tmp_path):
    """The moat regression-blocker: a cold go on each fixture must classify its
    defender correctly, the legit 'Access Denied' must read as cleared, and the
    aggregate must bucket on expected_waf. Spawns 4 sequential ephemeral Chromes
    (each torn down before the next), evidence PNGs to a tmp dir."""
    targets = bench.offline_targets(local_server)
    rows = bench.run_bench(call, targets, tier="standard", settle_ms=3000,
                           evidence_dir=tmp_path / "shots")
    by_name = {r["target"]: r for r in rows}

    # every target ran without harness error
    assert all(r["error"] is None for r in rows), \
        [r for r in rows if r["error"]]

    # detection: each WAF-title fixture reads as that defender → walled, not passed
    assert by_name["cloudflare"]["walled"] == "cloudflare"
    assert by_name["cloudflare"]["passed"] is False
    assert by_name["datadome"]["walled"] == "datadome"
    assert by_name["perimeterx"]["walled"] == "perimeterx"

    # no false positive: a legit "Access Denied" reads as cleared
    assert by_name["control"]["walled"] is None
    assert by_name["control"]["passed"] is True

    # evidence written for EVERY target (incl. the wall pages, not just control),
    # aggregation keys on the a-priori label
    for r in rows:
        assert r["evidence_path"] is not None, f"no evidence for {r['target']}"
        assert r["evidence_path"].endswith(".png")
    agg = bench.cold_pass_rate_by_waf(rows)
    assert agg["cloudflare"]["pass_rate"] == 0.0   # static wall page, nothing to clear
    assert agg["control"]["pass_rate"] == 1.0

    # ephemeral sessions left nothing behind
    listing = call("session_list")
    live = {s["name"] for s in listing["sessions"]}
    assert not any(n.startswith("vibatchium_bench_") for n in live)
