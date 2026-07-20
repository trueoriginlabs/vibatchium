"""Three fixes from the 2026-07 competitive sweep.

- proxy country -> session timezone (host-TZ vs exit-IP is a bot tell that
  used to fire by default),
- action-cache key normalization (one `?utm_source=` busted every entry),
- MCP tool annotations (we shipped zero, while marketing on content safety).
"""
from __future__ import annotations

import pytest

# ─── proxy country -> timezone ───────────────────────────────────────────


def test_country_is_read_off_a_proxy_url():
    from vibatchium.geo import country_from_proxy_url as cc
    assert cc("brightdata://u:p@zone?country=de&session=7") == "de"
    assert cc("iproyal://u:p@geo.iproyal.com:12321?country=US&sticky=7d") == "us"


def test_unknown_or_absent_country_is_not_guessed():
    from vibatchium.geo import country_from_proxy_url as cc
    assert cc("http://u:p@host:8080") is None
    assert cc("http://u:p@host:8080?country=zz") is None, \
        "a country we cannot map must fall through to the warning, not guess"
    assert cc("not a url at all") is None


def test_inferred_country_maps_to_a_real_timezone():
    from vibatchium.geo import COUNTRY_TZ, country_from_proxy_url, resolve_geo
    cc = country_from_proxy_url("brightdata://u:p@z?country=jp")
    assert resolve_geo(country=cc)["timezone_id"] == COUNTRY_TZ["jp"]


# ─── action-cache key normalization ──────────────────────────────────────


def test_tracking_params_do_not_bust_the_cache():
    from vibatchium.daemon.observe import _cache_key
    base = _cache_key("https://x.com/p", "buy it")
    assert _cache_key("https://x.com/p?utm_source=email", "buy it") == base
    assert _cache_key("https://x.com/p?fbclid=abc&gclid=d", "buy it") == base
    assert _cache_key("https://x.com/p#section", "buy it") == base


def test_param_order_does_not_bust_the_cache():
    from vibatchium.daemon.observe import _cache_key
    assert _cache_key("https://x.com/p?a=1&b=2", "go") == \
           _cache_key("https://x.com/p?b=2&a=1", "go")


def test_meaningful_params_still_separate_entries():
    """The conservative half: `?id=42` selects different content, so
    collapsing it would serve a stale plan for the wrong page."""
    from vibatchium.daemon.observe import _cache_key
    assert _cache_key("https://x.com/p?id=42", "go") != \
           _cache_key("https://x.com/p?id=43", "go")
    assert _cache_key("https://x.com/p?page=2", "go") != \
           _cache_key("https://x.com/p", "go")


def test_hash_router_routes_are_separate_entries():
    """0.18.6: hash-router SPA views live under `#/…` / `#!/…` on one
    scheme+host+path. Dropping the fragment collapsed them to one key, so
    `act` on `#/invoices` could HIT the plan cached for `#/orders` and replay
    its durable selector on the wrong view."""
    from vibatchium.daemon.observe import _cache_key
    assert _cache_key("https://app.x.com/#/orders", "click New") != \
           _cache_key("https://app.x.com/#/invoices", "click New")
    assert _cache_key("https://app.x.com/#!/orders", "click New") != \
           _cache_key("https://app.x.com/#!/invoices", "click New")
    # a route also differs from the bare path
    assert _cache_key("https://app.x.com/#/orders", "click New") != \
           _cache_key("https://app.x.com/", "click New")


def test_scroll_anchor_fragment_still_collapses():
    """A `#section` scroll anchor does NOT select content, so it must still
    share one entry (the hit-rate win) — unlike a `#/route`."""
    from vibatchium.daemon.observe import _cache_key
    base = _cache_key("https://x.com/p", "go")
    assert _cache_key("https://x.com/p#section", "go") == base
    assert _cache_key("https://x.com/p#footer", "go") == base


def test_ref_param_is_content_selecting_and_no_longer_stripped():
    """0.18.6: bare `ref` selects content on real sites (GitHub
    `?ref=<branch>`) — stripping it collapsed distinct pages. It stays in the
    key now; the unambiguous referral/campaign markers are still dropped."""
    from vibatchium.daemon.observe import _cache_key
    assert _cache_key("https://github.com/o/r?ref=main", "open") != \
           _cache_key("https://github.com/o/r?ref=dev", "open")
    base = _cache_key("https://x.com/p", "go")
    assert _cache_key("https://x.com/p?ref_src=twsrc", "go") == base
    assert _cache_key("https://x.com/p?referrer=foo", "go") == base
    assert _cache_key("https://x.com/p?source=newsletter", "go") == base


def test_intent_still_separates_entries():
    from vibatchium.daemon.observe import _cache_key
    assert _cache_key("https://x.com/p", "buy") != _cache_key("https://x.com/p", "sell")


# ─── MCP tool annotations ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def listed_tools():
    """Tools as a real MCP client sees them over a stdio handshake."""
    import asyncio

    async def go():
        from vibatchium import mcp_server as m
        return await m.list_tools()

    return {t.name: t for t in asyncio.run(go())}


def test_page_derived_content_is_marked_open_world(listed_tools):
    """The load-bearing hint: a host can taint this output instead of
    treating scraped text as instructions."""
    for name in ("explore", "extract", "map", "detect_forms", "text", "html"):
        t = listed_tools.get(name)
        if t is None:
            continue
        assert t.annotations is not None, f"{name} has no annotations"
        assert t.annotations.openWorldHint is True, \
            f"{name} returns page content but is not marked openWorldHint"


def test_probes_are_marked_read_only(listed_tools):
    for name in ("find", "count", "status"):
        t = listed_tools.get(name)
        if t is None:
            continue
        assert t.annotations is not None and t.annotations.readOnlyHint is True, \
            f"{name} is a pure probe but is not marked readOnlyHint"


def test_destructive_verbs_are_marked(listed_tools):
    for name in ("stop", "secret_delete", "storage_restore"):
        t = listed_tools.get(name)
        if t is None:
            continue
        assert t.annotations is not None and t.annotations.destructiveHint is True, \
            f"{name} destroys state but is not marked destructiveHint"


def test_mutating_verbs_are_not_claimed_read_only(listed_tools):
    """A wrong readOnlyHint is worse than none — a host may call it
    speculatively."""
    for name in ("click", "fill", "go", "type", "press", "stop"):
        t = listed_tools.get(name)
        if t is None or t.annotations is None:
            continue
        assert t.annotations.readOnlyHint is not True, \
            f"{name} mutates state but claims readOnlyHint"


def test_some_annotations_actually_ship(listed_tools):
    """Guard against the whole mechanism silently regressing to None."""
    annotated = [t for t in listed_tools.values() if t.annotations is not None]
    assert len(annotated) >= 10, \
        f"only {len(annotated)} annotated tools — wiring likely broken"


# ─── evals --update-readme has something to patch ────────────────────────


def test_readme_carries_the_evals_markers():
    """`vb evals --update-readme` returns False for BOTH 'no markers found'
    and 'already up to date', so a missing marker is a silent no-op rather
    than an error. The markers lived in evals.py's docstring and nowhere in
    README, so the flag had never done anything."""
    from pathlib import Path
    readme = Path(__file__).resolve().parent.parent / "README.md"
    body = readme.read_text()
    assert "<!-- vibatchium-evals -->" in body
    assert "<!-- /vibatchium-evals -->" in body


def test_update_readme_patches_and_is_idempotent(tmp_path):
    import shutil
    from pathlib import Path
    from vibatchium.evals import update_readme
    src = Path(__file__).resolve().parent.parent / "README.md"
    dst = tmp_path / "README.md"
    shutil.copy(src, dst)
    table = "| target | score |\n|---|---|\n| sannysoft | 100 |"
    assert update_readme(dst, table) is True, "markers not found in README"
    assert "| sannysoft | 100 |" in dst.read_text()
    assert update_readme(dst, table) is False, "second run should be a no-op"
