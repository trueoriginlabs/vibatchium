"""Pure helpers for the ``search`` verb — SERP discovery over the same
curl_cffi lane ``fetch`` uses, so the *discovery* half of a research loop stops
depending on a hosted search API.

Why it exists (0.19.0): every agent research loop needs two capabilities — find
candidate URLs, then read them. vibatchium already owned the reading half
(``fetch``/``explore``). The finding half was outsourced to whatever hosted
search tool the harness happened to provide, which meant discovery went dark the
moment that tool ran out of budget — and the fallback everyone reached for, a
plain-HTTP scrape of an engine's HTML endpoint, is exactly the thing the engines
fingerprint-block. Search engines are themselves anti-bot walled, so putting
discovery on the impersonating lane is not scope creep: it is the same moat
applied one step earlier in the loop.

DESIGN NOTES, each one paid for:

* **Engine LADDER, not a single engine.** Reachability is time- and
  IP-dependent: the same DuckDuckGo HTML endpoint that serves 200s in one
  session answers a 202 challenge in the next, and Bing has been both the
  fallback that worked and the one that captcha'd. A single hard-coded engine
  is a coin flip, so ``parse``/``ENGINE_ORDER`` are built to be walked in order
  until one yields rows.
* **Sessionless by construction.** Unlike ``fetch``, this lane never reuses a
  browser session's cookies — you do not want a logged-in identity attached to
  your search traffic, and a SERP needs no authentication. That makes the verb
  strictly lower blast-radius than ``fetch``: fixed engine allowlist, GET only,
  no credentials.
* **No date filter is exposed.** DuckDuckGo's ``&df=`` parameter mislabels
  article dates badly enough to have burned a research pass (two hits it dated
  to one month were actually from four and nineteen months earlier). A filter
  that silently lies is worse than no filter; confirm dates by opening the page.

This module is import-safe with ZERO optional deps — stdlib only, no HTML
parser — so the extractors are unit-testable against saved fixtures without the
daemon, ``curl_cffi``, or a network. The handler owns transport; this owns
URL-building, unwrapping, and extraction.
"""
from __future__ import annotations

import base64
import binascii
import re
from html import unescape
from urllib.parse import parse_qs, quote_plus, urlsplit

# ─── engines ─────────────────────────────────────────────────────────────
#
# Ordered cheapest/most-parseable first. `ddg` carries the richest snippets;
# `ddg-lite` is the same index behind a much smaller table-based page (a useful
# second try, since the two endpoints rate-limit independently); `bing` is a
# different index entirely, which is the point of having it last — when DDG is
# challenging every request, another provider is the only thing that helps.
ENGINE_ORDER: tuple[str, ...] = ("ddg", "ddg-lite", "bing")

_ENGINE_ENDPOINTS: dict[str, str] = {
    "ddg": "https://html.duckduckgo.com/html/?q={q}",
    "ddg-lite": "https://lite.duckduckgo.com/lite/?q={q}",
    "bing": "https://www.bing.com/search?q={q}",
}

# Markers that mean "this response is a wall, not a result page". Checked
# case-insensitively against the body. Kept deliberately short and specific:
# a false positive here silently skips a working engine.
_WALL_MARKERS: tuple[str, ...] = (
    "unusual traffic",
    "are you a robot",
    "verify you are human",
    "please solve this captcha",
    "/sorry/index",
    "detected unusual activity",
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Every inner-text capture below is BOUNDED (`.{0,_MAX_INNER}?`) rather than a
# bare `.*?`. With an unbounded lazy quantifier a single unbalanced `<a` makes
# each match attempt scan to end-of-document before failing, which is O(n²) over
# the whole page: measured 0.57s at 50 KB, 2.90s at 100 KB, 10.60s at 200 KB —
# four times the work per doubling, i.e. hours at the 5 MB body cap. This parse
# is plain synchronous CPU inside the daemon's coroutine, so that time blocks
# EVERY other session and verb, not just the search. The body is attacker-
# controlled (it is whatever a search endpoint returned), so the bound is a
# safety limit, not a tidiness one. No real result title or snippet approaches
# it; anything longer is malformed and deserves to be skipped.
_MAX_INNER = 4000

# The lazy bounded repeat every inner-span capture below shares. Spliced in by
# concatenation rather than an f-string: these patterns are dense with literal
# regex braces, and doubling each one to satisfy f-string escaping would make
# them harder to read — and to keep correct — than the bound is to inline.
_INNER = "(." + "{0," + str(_MAX_INNER) + "}?)"

_ANCHOR_RE = re.compile(r"<a\b([^>]*)>" + _INNER + r"</a>", re.S | re.I)
_H2_RE = re.compile(r"<h2\b[^>]*>" + _INNER + r"</h2>", re.S | re.I)
_DDG_SNIPPET_RE = re.compile(
    r"<a\b[^>]*class=[\"']result__snippet[\"'][^>]*>" + _INNER + r"</a>",
    re.S | re.I)
_LITE_SNIPPET_RE = re.compile(
    r"<td\b[^>]*class=[\"']result-snippet[\"'][^>]*>" + _INNER + r"</td>",
    re.S | re.I)
_BING_SNIPPET_RE = re.compile(
    r"<p\b[^>]*class=[\"'][^\"']*b_lineclamp[^\"']*[\"'][^>]*>"
    + _INNER + r"</p>", re.S | re.I)

#: Hosts that belong to the engines themselves. A result URL still pointing at
#: one of these is a redirector or an ad shim we failed to unwrap — emitting it
#: would record the search engine as the source of a claim.
_ENGINE_HOSTS = frozenset({
    "duckduckgo.com", "www.duckduckgo.com", "html.duckduckgo.com",
    "lite.duckduckgo.com", "bing.com", "www.bing.com",
})


def engines() -> tuple[str, ...]:
    """The engine tokens this build knows, in ladder order."""
    return ENGINE_ORDER


def engine_url(engine: str, query: str, *, site: str | None = None) -> str:
    """Build the SERP URL for ``engine``.

    ``site`` appends a ``site:`` operator — every supported engine honours it,
    so it belongs here rather than making callers hand-splice query syntax.
    """
    if engine not in _ENGINE_ENDPOINTS:
        raise ValueError(
            f"unknown engine {engine!r} — known: {', '.join(ENGINE_ORDER)}")
    q = query.strip()
    if not q:
        raise ValueError("search requires a non-empty query")
    if site:
        q = f"site:{site.strip()} {q}"
    return _ENGINE_ENDPOINTS[engine].format(q=quote_plus(q))


def looks_walled(status: int | None, body: str) -> str | None:
    """Return the reason this response is unusable, or None if it looks real.

    Anything other than a bare 200 is a wall. That is stricter than the usual
    2xx test on purpose: a rate-limited DuckDuckGo answers **202**, and treating
    that as success means parsing a challenge page into zero results and
    reporting "no matches" for a query that simply needed another engine.
    Redirects are followed by the transport, so a real SERP always lands on 200.

    Beyond the status we look for challenge text, because the interesting
    failure mode is a *200 that isn't a SERP* — an interstitial served with a
    success status, which a status check alone would wave through.
    """
    return status_wall(status) or marker_wall(body)


def status_wall(status: int | None) -> str | None:
    """The cheap half of ``looks_walled`` — status only, no body scan."""
    if status is None:
        return "no status"
    if int(status) != 200:
        return f"http {status}"
    return None


def marker_wall(body: str) -> str | None:
    """The body half — ONLY meaningful on a page that yielded no results.

    Every engine echoes the query back into the page (DuckDuckGo lite puts it in
    four separate places), and result titles quote it too. So scanning the whole
    body for challenge text means searching for "verify you are human" makes all
    three rungs reject a page containing five perfectly good results — verified
    against the saved fixture. Callers must parse first and consult this only
    when the parse came back empty, which is exactly when a challenge page and
    an empty result set need telling apart.
    """
    low = (body or "").lower()
    for marker in _WALL_MARKERS:
        if marker in low:
            return f"challenge marker: {marker}"
    return None


# ─── redirect unwrapping ─────────────────────────────────────────────────
#
# Neither engine links straight at the result: both bounce through a tracking
# redirector. Following those would cost one extra request per result AND leak
# the click back to the engine, so we decode them offline instead.

def unwrap_ddg(href: str) -> str:
    """Decode a DuckDuckGo ``/l/?uddg=<urlencoded>`` redirect to its target."""
    if not href:
        return ""
    if "uddg=" not in href:
        return href
    qs = parse_qs(urlsplit(href).query)
    target = (qs.get("uddg") or [""])[0]
    return target or href


def unwrap_bing(href: str) -> str:
    """Decode a Bing ``/ck/a?...&u=a1<base64url>`` redirect to its target.

    The ``u`` param is the real URL base64url-encoded behind a two-character
    ``a1`` type prefix, usually unpadded. Anything that doesn't decode to an
    http(s) URL is returned untouched — a redirector shape we don't recognise
    is better surfaced verbatim than silently mangled.
    """
    if not href:
        return ""
    qs = parse_qs(urlsplit(href).query)
    raw = (qs.get("u") or [""])[0]
    if not raw.startswith("a1"):
        return href
    token = raw[2:]
    token += "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(token).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return href
    return decoded if decoded.startswith(("http://", "https://")) else href


# ─── extraction ──────────────────────────────────────────────────────────

def _clean(fragment: str) -> str:
    """HTML fragment → single-line plain text (tags out, entities decoded)."""
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", fragment or ""))).strip()


def _attr(tag_attrs: str, name: str) -> str:
    """Read one attribute out of a raw tag's attribute text.

    The leading boundary is load-bearing: without it a request for `href`
    happily matched `data-href`, so an anchor could be selected and sourced from
    an attribute nobody meant to read.
    """
    m = re.search(rf"(?:^|\s){name}\s*=\s*[\"']([^\"']*)[\"']", tag_attrs, re.I)
    return unescape(m.group(1)) if m else ""


def _has_class(tag_attrs: str, cls: str) -> bool:
    return cls in _attr(tag_attrs, "class").split()


def _absolutise(url: str) -> str:
    """DDG emits protocol-relative hrefs (``//duckduckgo.com/...``)."""
    return "https:" + url if url.startswith("//") else url


def _pair(links: list[tuple[int, str, str]], snippets: list[tuple[int, str]],
          max_results: int) -> list[dict]:
    """Attach each title/url hit to the snippet that follows it in the document.

    Pairing is by POSITION IN THE PAGE, not by list index. Index-zipping looks
    equivalent and is subtly wrong: a single result rendered without a snippet
    shifts every later description onto the wrong link, and the output stays
    plausible enough that nobody notices. Scoping each snippet to the span
    between one result anchor and the next makes a missing snippet cost only its
    own row.
    """
    out: list[dict] = []
    for i, (pos, url, title) in enumerate(links):
        if max_results and len(out) >= max_results:
            break
        nxt = links[i + 1][0] if i + 1 < len(links) else None
        snippet = ""
        for spos, stext in snippets:
            if spos > pos and (nxt is None or spos < nxt):
                snippet = stext
                break
        out.append({
            "rank": len(out) + 1,
            "title": title,
            "url": url,
            "snippet": snippet,
        })
    return out


def _usable_result_url(url: str) -> bool:
    """Reject anything that must not be emitted as a source URL.

    The Bing path has always screened its output; the DuckDuckGo paths did not,
    so `javascript:` hrefs, bare relative paths, and DuckDuckGo's own `y.js` ad
    shim all passed straight through as "sources" — the precise outcome the Bing
    comment calls poisoning a citation trail. An engine-owned host here means we
    failed to unwrap a redirector, which is never a real result.
    """
    if not url.startswith(("http://", "https://")):
        return False
    return (urlsplit(url).hostname or "").lower() not in _ENGINE_HOSTS


def _anchors_with_class(body: str, cls: str) -> list[tuple[int, str, str]]:
    """Positioned ``(offset, url, title)`` for result anchors of one class."""
    hits: list[tuple[int, str, str]] = []
    for m in _ANCHOR_RE.finditer(body):
        if not _has_class(m.group(1), cls):
            continue
        url = unwrap_ddg(_absolutise(_attr(m.group(1), "href")))
        title = _clean(m.group(2))
        if title and _usable_result_url(url):
            hits.append((m.start(), url, title))
    return hits


def _positioned(pattern: re.Pattern[str], body: str) -> list[tuple[int, str]]:
    return [(m.start(), _clean(m.group(1))) for m in pattern.finditer(body)]


def _parse_ddg(body: str, max_results: int) -> list[dict]:
    return _pair(_anchors_with_class(body, "result__a"),
                 _positioned(_DDG_SNIPPET_RE, body), max_results)


def _parse_ddg_lite(body: str, max_results: int) -> list[dict]:
    return _pair(_anchors_with_class(body, "result-link"),
                 _positioned(_LITE_SNIPPET_RE, body), max_results)


def _parse_bing(body: str, max_results: int) -> list[dict]:
    # Scope to the organic list: take only <h2> headings that fall at or after
    # the first `b_algo` item. Everything before it is answer-box/inline-card
    # chrome, which would otherwise be emitted as rank 1 and silently displace
    # Bing's actual first organic hit.
    first_algo = body.find('class="b_algo"')
    floor = first_algo if first_algo >= 0 else 0
    links: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for h in _H2_RE.finditer(body):
        if h.start() < floor:
            continue
        m = _ANCHOR_RE.search(h.group(1))
        if not m:
            continue
        url = unwrap_bing(_attr(m.group(1), "href"))
        title = _clean(m.group(2))
        # An href that survives unwrapping still pointing at bing.com is a
        # carousel/"Videos of…" tile whose target we cannot recover — emitting
        # the redirector as if it were a source would poison a citation trail.
        if not url.startswith(("http://", "https://")) or not title:
            continue
        if urlsplit(url).hostname in ("www.bing.com", "bing.com"):
            continue
        if url in seen:
            continue
        seen.add(url)
        links.append((h.start(), url, title))
    return _pair(links, _positioned(_BING_SNIPPET_RE, body), max_results)


_PARSERS = {
    "ddg": _parse_ddg,
    "ddg-lite": _parse_ddg_lite,
    "bing": _parse_bing,
}


def parse(engine: str, body: str, max_results: int = 10) -> list[dict]:
    """Extract ``[{rank, title, url, snippet}]`` from an engine's SERP HTML.

    Returns an empty list when the page yields nothing — the caller treats that
    as "this engine didn't answer" and advances the ladder, which is why a
    silent-empty parse must never raise.
    """
    if engine not in _PARSERS:
        raise ValueError(
            f"unknown engine {engine!r} — known: {', '.join(ENGINE_ORDER)}")
    return _PARSERS[engine](body or "", max_results)


# ─── the ladder ──────────────────────────────────────────────────────────

def resolve_ladder(engine: str | None) -> list[str]:
    """Turn an ``engine`` argument into the ordered list of engines to try."""
    token = (engine or "auto").lower()
    if token == "auto":
        return list(ENGINE_ORDER)
    if token in ENGINE_ORDER:
        return [token]
    raise ValueError(
        f"unknown engine {token!r} — known: {', '.join(ENGINE_ORDER)}, "
        "or 'auto' for the full ladder")


async def run_ladder(fetcher, query: str, *, engine: str | None = "auto",
                     max_results: int = 10, site: str | None = None) -> dict:
    """Walk the engine ladder until one answers; return the result envelope.

    ``fetcher`` is an async ``(url) -> (status, body, error)`` callable, so the
    transport stays out of this module: the handler injects curl_cffi, and tests
    inject a dict. Policy (what counts as an answer, what order to try, what to
    record) lives here where it can be tested without a network.

    Every rejection is accumulated into ``attempts`` rather than swallowed —
    "DuckDuckGo rate-limited, Bing served this" is the difference between
    trusting a thin result set and re-running the query, and only the caller can
    make that judgement.
    """
    attempts: list[dict] = []
    answered = False        # did ANY engine serve a real SERP that simply had no hits?
    for eng in resolve_ladder(engine):
        url = engine_url(eng, query, site=site)
        status, body, error = await fetcher(url)
        if error:
            attempts.append({"engine": eng, "status": status, "results": 0,
                             "rejected": error})
            continue
        walled = status_wall(status)
        if walled:
            attempts.append({"engine": eng, "status": status, "results": 0,
                             "rejected": walled})
            continue
        # PARSE BEFORE scanning for challenge text. The body echoes the query
        # back, so a query containing a marker phrase would otherwise reject a
        # page full of valid results.
        rows = parse(eng, body, max_results)
        if not rows:
            marker = marker_wall(body)
            if marker is None:
                answered = True     # a real SERP; it just had nothing
            attempts.append({"engine": eng, "status": status, "results": 0,
                             "rejected": marker or "no results parsed"})
            continue
        attempts.append({"engine": eng, "status": status, "results": len(rows)})
        return {"query": query, "site": site or None, "engine": eng, "ok": True,
                "count": len(rows), "results": rows, "attempts": attempts,
                "reason": None}

    # Two very different outcomes share ok=false, and conflating them sent users
    # to change their proxy when the web simply had no match. Say which happened.
    if answered:
        reason, hint = "no_matches", (
            "at least one engine served a real result page with no hits — this "
            "is an empty result set, not a wall. Broaden the query or drop "
            "`--site`.")
    else:
        reason, hint = "all_declined", (
            "no engine served a result page. Engines rate-limit PER IP, so "
            "route through a different egress (`--proxy`), retry after a pause "
            "(the limit recovers), or run `explore` against a search URL to use "
            "a full browser. Check `attempts` — a transport error there is a "
            "client-side fault, not a wall.")
    return {
        "query": query, "site": site or None, "engine": None, "ok": False,
        "count": 0, "results": [], "attempts": attempts,
        "reason": reason, "hint": hint,
    }
