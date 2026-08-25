"""0.19.0 — pure unit tests for the search-lane helpers (vibatchium/search.py).

No daemon, no curl_cffi, no network. The SERP extractors run against saved
fixtures (tests/fixtures/serp_*.html, captured live) and the engine ladder runs
against an injected fetcher, so the policy that decides "this engine declined,
try the next one" is testable without waiting for an engine to actually wall us.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from vibatchium import search

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# ─── engine_url ──────────────────────────────────────────────────────────
def test_engine_url_encodes_the_query():
    url = search.engine_url("ddg", "playwright stealth")
    assert url == "https://html.duckduckgo.com/html/?q=playwright+stealth"


def test_engine_url_site_prepends_the_operator():
    url = search.engine_url("bing", "cdp leak", site="github.com")
    assert "q=site%3Agithub.com+cdp+leak" in url


def test_engine_url_rejects_unknown_engine():
    with pytest.raises(ValueError, match="unknown engine"):
        search.engine_url("google", "x")


def test_engine_url_rejects_empty_query():
    with pytest.raises(ValueError, match="non-empty query"):
        search.engine_url("ddg", "   ")


# ─── looks_walled ────────────────────────────────────────────────────────
def test_looks_walled_passes_a_clean_200():
    assert search.looks_walled(200, "<html>results</html>") is None


def test_looks_walled_rejects_202_rate_limit():
    # The regression that motivates the strict check: DuckDuckGo answers a
    # rate-limited request with 202, which a `2xx == ok` test waves through and
    # then reports as "no results" for a perfectly good query.
    assert search.looks_walled(202, "<html>challenge</html>") == "http 202"


def test_looks_walled_rejects_non_200_statuses():
    assert search.looks_walled(429, "") == "http 429"
    assert search.looks_walled(403, "") == "http 403"
    assert search.looks_walled(None, "") == "no status"


def test_looks_walled_catches_a_challenge_served_with_200():
    reason = search.looks_walled(200, "<p>Please solve this CAPTCHA to continue</p>")
    assert reason and "challenge marker" in reason


# ─── redirect unwrapping ─────────────────────────────────────────────────
def test_unwrap_ddg_decodes_the_uddg_param():
    href = ("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%2Db"
            "&rut=deadbeef")
    assert search.unwrap_ddg(href) == "https://example.com/a-b"


def test_unwrap_ddg_passes_through_a_direct_link():
    assert search.unwrap_ddg("https://example.com/x") == "https://example.com/x"


def test_unwrap_bing_decodes_the_base64url_u_param():
    # a1 + unpadded base64url of the target
    href = ("https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly9zY3JhcGZseS5pby9ibG9n"
            "L3Bvc3RzL3BsYXl3cmlnaHQtc3RlYWx0aC1ieXBhc3MtYm90LWRldGVjdGlvbg&ntb=1")
    assert search.unwrap_bing(href) == (
        "https://scrapfly.io/blog/posts/playwright-stealth-bypass-bot-detection")


def test_unwrap_bing_leaves_an_unrecognised_shape_alone():
    href = "https://www.bing.com/ck/a?!&&p=x&ntb=1"       # no u= param
    assert search.unwrap_bing(href) == href


def test_unwrap_bing_survives_undecodable_payload():
    href = "https://www.bing.com/ck/a?u=a1!!!not-base64!!!"
    assert search.unwrap_bing(href) == href


# ─── extraction, against live-captured fixtures ──────────────────────────
@pytest.mark.parametrize("engine,fixture", [
    ("ddg", "serp_ddg.html"),
    ("ddg-lite", "serp_ddg_lite.html"),
    ("bing", "serp_bing.html"),
])
def test_parse_extracts_organic_results(engine, fixture):
    rows = search.parse(engine, _fixture(fixture), max_results=5)
    assert len(rows) == 5
    assert [r["rank"] for r in rows] == [1, 2, 3, 4, 5]
    for r in rows:
        assert r["title"]
        assert r["url"].startswith("https://")
        assert "<" not in r["title"] and "<" not in r["snippet"]


@pytest.mark.parametrize("engine,fixture", [
    ("ddg", "serp_ddg.html"),
    ("ddg-lite", "serp_ddg_lite.html"),
    ("bing", "serp_bing.html"),
])
def test_parse_unwraps_every_redirect(engine, fixture):
    # A redirector leaking into the output would poison a citation trail — the
    # agent would record duckduckgo.com/bing.com as the source of the claim.
    rows = search.parse(engine, _fixture(fixture), max_results=10)
    for r in rows:
        assert "duckduckgo.com/l/" not in r["url"]
        assert "bing.com/ck/a" not in r["url"]


def test_parse_max_results_caps_output():
    assert len(search.parse("ddg", _fixture("serp_ddg.html"), max_results=2)) == 2


def test_parse_all_three_engines_agree_on_the_top_domain():
    """Cross-engine sanity: the fixtures are one query, so the extractors should
    surface overlapping sources. Catches an extractor that "works" but is
    reading the wrong part of the page."""
    per_engine = [
        {r["url"] for r in search.parse(e, _fixture(f), 10)}
        for e, f in (("ddg", "serp_ddg.html"), ("ddg-lite", "serp_ddg_lite.html"),
                     ("bing", "serp_bing.html"))
    ]
    assert set.intersection(*per_engine)


def test_parse_empty_body_returns_no_rows_without_raising():
    for engine in search.ENGINE_ORDER:
        assert search.parse(engine, "", 5) == []


def test_parse_rejects_unknown_engine():
    with pytest.raises(ValueError, match="unknown engine"):
        search.parse("google", "<html></html>", 5)


def test_missing_snippet_does_not_shift_later_descriptions():
    """The bug position-scoped pairing exists to prevent: index-zipping titles
    against snippets means one result without a description silently slides
    every later snippet onto the wrong link."""
    body = (
        '<a class="result__a" href="https://a.example/">A</a>'
        # no snippet for A
        '<a class="result__a" href="https://b.example/">B</a>'
        '<a class="result__snippet" href="#">about B</a>'
        '<a class="result__a" href="https://c.example/">C</a>'
        '<a class="result__snippet" href="#">about C</a>'
    )
    rows = search.parse("ddg", body, 10)
    assert [(r["title"], r["snippet"]) for r in rows] == [
        ("A", ""), ("B", "about B"), ("C", "about C")]


def test_bing_skips_carousel_tiles_whose_target_cannot_be_recovered():
    body = (
        '<li class="b_algo"></li>'
        '<h2><a href="https://www.bing.com/ck/a?!&amp;&amp;p=x&amp;ntb=1">'
        'Videos of Something</a></h2>'
        '<h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9yZWFsLmV4YW1wbGUv">'
        'Real Result</a></h2>'
    )
    rows = search.parse("bing", body, 10)
    assert [r["url"] for r in rows] == ["https://real.example/"]


def test_bing_ignores_headings_before_the_organic_list():
    """An answer-box <h2> sits above the first b_algo; emitting it would
    displace Bing's actual first organic hit from rank 1."""
    body = (
        '<h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9hbnN3ZXIuZXhhbXBsZS8">'
        'Answer Box</a></h2>'
        '<li class="b_algo"></li>'
        '<h2><a href="https://www.bing.com/ck/a?u=a1aHR0cHM6Ly9vcmdhbmljLmV4YW1wbGUv">'
        'First Organic</a></h2>'
    )
    rows = search.parse("bing", body, 10)
    assert [r["title"] for r in rows] == ["First Organic"]


# ─── ladder ──────────────────────────────────────────────────────────────
def test_resolve_ladder_auto_is_the_full_order():
    assert search.resolve_ladder("auto") == list(search.ENGINE_ORDER)
    assert search.resolve_ladder(None) == list(search.ENGINE_ORDER)


def test_resolve_ladder_pins_a_single_engine():
    assert search.resolve_ladder("bing") == ["bing"]


def test_resolve_ladder_rejects_unknown():
    with pytest.raises(ValueError, match="unknown engine"):
        search.resolve_ladder("google")


def _fetcher(responses: dict[str, tuple]):
    """Build an async fetcher that answers by engine-host substring."""
    async def get(url):
        for key, resp in responses.items():
            if key in url:
                return resp
        return None, "", "transport: unexpected url"
    return get


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def test_ladder_stops_at_the_first_engine_that_answers():
    out = _run(search.run_ladder(
        _fetcher({"duckduckgo.com/html": (200, _fixture("serp_ddg.html"), None)}),
        "q", max_results=3))
    assert out["ok"] is True
    assert out["engine"] == "ddg"
    assert out["count"] == 3
    assert [a["engine"] for a in out["attempts"]] == ["ddg"]


def test_ladder_advances_past_a_rate_limited_engine():
    out = _run(search.run_ladder(
        _fetcher({
            "html.duckduckgo.com": (202, "<html>challenge</html>", None),
            "lite.duckduckgo.com": (429, "", None),
            "bing.com": (200, _fixture("serp_bing.html"), None),
        }), "q", max_results=3))
    assert out["ok"] is True and out["engine"] == "bing"
    assert [(a["engine"], a.get("rejected")) for a in out["attempts"]] == [
        ("ddg", "http 202"), ("ddg-lite", "http 429"), ("bing", None)]


def test_ladder_advances_past_a_transport_failure():
    out = _run(search.run_ladder(
        _fetcher({
            "html.duckduckgo.com": (None, "", "transport: connection reset"),
            "lite.duckduckgo.com": (200, _fixture("serp_ddg_lite.html"), None),
        }), "q", max_results=2))
    assert out["engine"] == "ddg-lite"
    assert out["attempts"][0]["rejected"] == "transport: connection reset"


def test_ladder_advances_when_a_200_parses_to_nothing():
    out = _run(search.run_ladder(
        _fetcher({
            "html.duckduckgo.com": (200, "<html><body>nothing here</body></html>", None),
            "lite.duckduckgo.com": (200, _fixture("serp_ddg_lite.html"), None),
        }), "q", max_results=2))
    assert out["engine"] == "ddg-lite"
    assert out["attempts"][0]["rejected"] == "no results parsed"


def test_ladder_exhausted_reports_ok_false_with_the_full_trace():
    out = _run(search.run_ladder(
        _fetcher({"duckduckgo.com": (403, "", None), "bing.com": (403, "", None)}),
        "q"))
    assert out["ok"] is False
    assert out["count"] == 0 and out["results"] == []
    assert len(out["attempts"]) == len(search.ENGINE_ORDER)
    assert out["hint"]


def test_ladder_pinned_engine_does_not_fall_through():
    out = _run(search.run_ladder(
        _fetcher({
            "html.duckduckgo.com": (403, "", None),
            "bing.com": (200, _fixture("serp_bing.html"), None),
        }), "q", engine="ddg"))
    assert out["ok"] is False
    assert [a["engine"] for a in out["attempts"]] == ["ddg"]


def test_ladder_threads_the_site_operator_through():
    seen: list[str] = []

    async def get(url):
        seen.append(url)
        return 200, _fixture("serp_ddg.html"), None

    out = _run(search.run_ladder(get, "cdp leak", site="github.com", max_results=1))
    assert out["site"] == "github.com"
    assert "site%3Agithub.com" in seen[0]


def test_exhausted_hint_names_the_per_ip_remedy():
    """A walled ladder is usually an egress problem, so the hint has to say so —
    an agent that reads 'retry later' will retry from the same IP and get the
    same wall."""
    out = _run(search.run_ladder(_fetcher({"": (403, "", None)}), "q"))
    assert out["ok"] is False
    assert "--proxy" in out["hint"] and "PER IP" in out["hint"]
