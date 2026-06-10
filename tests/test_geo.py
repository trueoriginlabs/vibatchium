"""0.6.11 — timezone coherence (geo).

The host timezone vs the egress IP's geolocation is a louder bot tell than the
UA leak: a Chrome clock on `Australia/Sydney` behind a US proxy is trivially
flagged. These tests prove the timezone override (a) resolves + persists
correctly and (b) ACTUALLY reaches the browser — including worker threads —
without introducing a main-vs-worker mismatch (the reason locale/language is
deliberately NOT overridden; see geo.py).
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from vibatchium import geo
from vibatchium.client import call


# ─── resolution + validation (no Chrome) ────────────────────────────────


def test_resolve_country_maps_to_representative_timezone():
    assert geo.resolve_geo(country="us") == {"timezone_id": "America/New_York"}
    assert geo.resolve_geo(country="JP") == {"timezone_id": "Asia/Tokyo"}


def test_resolve_explicit_timezone_overrides_country():
    out = geo.resolve_geo(country="us", timezone_id="America/Los_Angeles")
    assert out == {"timezone_id": "America/Los_Angeles"}


def test_resolve_rejects_empty_unknown_and_bad_tz():
    with pytest.raises(geo.GeoParseError):
        geo.resolve_geo()
    with pytest.raises(geo.GeoParseError):
        geo.resolve_geo(country="zz")
    with pytest.raises(geo.GeoParseError):
        geo.resolve_geo(timezone_id="Bogus/Nowhere")


# ─── per-session storage round-trip (no Chrome) ─────────────────────────


def test_geo_storage_roundtrip_and_clear(tmp_path):
    assert geo.load_session_geo(tmp_path) is None
    geo.save_session_geo(tmp_path, {"timezone_id": "Europe/Berlin"})
    assert geo.load_session_geo(tmp_path) == {"timezone_id": "Europe/Berlin"}
    # 0600 — the per-profile file-mode invariant
    p = geo.session_geo_path(tmp_path)
    assert (p.stat().st_mode & 0o777) == 0o600
    # clear removes the file
    geo.save_session_geo(tmp_path, None)
    assert geo.load_session_geo(tmp_path) is None
    assert not p.exists()


def test_load_ignores_stale_locale_field(tmp_path):
    """A geo.json written by an earlier build may carry a `locale` key — load
    must ignore it (timezone-only) without choking."""
    geo.session_geo_path(tmp_path).write_text(
        '{"timezone_id": "Asia/Tokyo", "locale": "ja-JP"}')
    assert geo.load_session_geo(tmp_path) == {"timezone_id": "Asia/Tokyo"}


# ─── the teeth: real Chrome reports the tz — in workers too, coherently ──


async def test_geo_timezone_reaches_browser_and_workers_coherently():
    """Launch a real headless Chrome with a timezone override and prove:
    (1) the main thread reports it; (2) a Worker reports the SAME tz (the
    override propagates to workers — unlike locale, which is why we don't
    override language); (3) navigator.language is IDENTICAL in main and worker
    (we did NOT introduce a main-vs-worker mismatch). Two distinct zones make
    the tz proof host-independent and prove the clock offset actually shifts."""
    from vibatchium.daemon.browser import close_session, launch_session

    probe = """() => new Promise((resolve) => {
      const main = {tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
                    lang: navigator.language,
                    offset: new Date().getTimezoneOffset()};
      const code = "self.onmessage=()=>{postMessage({tz:Intl.DateTimeFormat()"
        + ".resolvedOptions().timeZone, lang:navigator.language});}";
      const w = new Worker(URL.createObjectURL(
        new Blob([code], {type:'application/javascript'})));
      const t = setTimeout(() => resolve({main, worker:'TIMEOUT'}), 4000);
      w.onmessage = e => { clearTimeout(t); resolve({main, worker:e.data}); };
      w.postMessage(0);
    })"""
    results = {}
    for tz in ("Europe/Berlin", "Asia/Tokyo"):
        tmp = Path(tempfile.mkdtemp(prefix="geotest_"))
        sess = await launch_session(tmp, headless=True, timezone_id=tz)
        try:
            await sess.page.goto("about:blank")
            r = await sess.page.evaluate(probe)
            results[tz] = r
            assert r["main"]["tz"] == tz, f"main tz override failed: {r}"
            assert r["worker"] != "TIMEOUT", "worker probe timed out"
            assert r["worker"]["tz"] == tz, (
                f"timezone override did NOT reach the worker: {r} — a worker "
                f"reporting a different tz is a main-vs-worker mismatch tell")
            # language must be coherent across main+worker (we don't override it)
            assert r["worker"]["lang"] == r["main"]["lang"], (
                f"navigator.language differs main vs worker ({r}) — the locale "
                f"override regression we deliberately avoid")
        finally:
            try:
                await close_session(sess)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    assert results["Europe/Berlin"]["main"]["offset"] \
        != results["Asia/Tokyo"]["main"]["offset"], (
        "wall-clock offset identical across two zones — only the label changed")
    assert results["Asia/Tokyo"]["main"]["offset"] == -540  # JST = UTC+9, no DST


# ─── full daemon path: geo.json → start → browser reports it ────────────


def test_geo_applied_end_to_end_via_daemon(local_server):
    """geo_set (RPC) → geo.json on disk → registry loads it → launch applies it →
    the browser reports it, and geo_info surfaces the live value."""
    name = "geo_e2e"
    try:
        call("session_new", {"name": name})
        call("geo_set", {"country": "jp"}, session=name)
        call("start", {"headless": True}, session=name)
        res = call("eval",
                   {"expr": "Intl.DateTimeFormat().resolvedOptions().timeZone"},
                   session=name)
        tz = res.get("value", res)
        assert tz == "Asia/Tokyo", f"daemon geo path did not apply tz: {tz!r}"
        info = call("geo_info", session=name)
        assert info["configured"] is True
        assert info["timezone_id"] == "Asia/Tokyo"
        assert info["browser_timezone"] == "Asia/Tokyo"
    finally:
        for cmd in (("session_close", {"name": name}),
                    ("session_delete", {"name": name})):
            try:
                call(cmd[0], cmd[1])
            except Exception:  # noqa: BLE001
                pass
