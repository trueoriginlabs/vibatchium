"""0.14.0 — tests for the agent-extract layer.

PURE: the field-spec grammar parser (extract.parse_field_specs), the in-page JS
payload wrappers (dom_js), and the <form> structure signal.
LIVE: extract_fields (structured {name:selector}->JSON) and extract --mode
links/assets/main end-to-end against served fixtures.
"""
from __future__ import annotations

from vibatchium.extract import parse_field_specs, extract_with_signals
from vibatchium import dom_js
from vibatchium.client import call


# ─── PURE: parse_field_specs grammar ──────────────────────────────────────
def test_parse_bare_selector_is_text():
    (sp,) = parse_field_specs({"t": "h1"})
    assert sp == {"name": "t", "selector": "h1", "mode": "text", "array": False}


def test_parse_array_suffix():
    (sp,) = parse_field_specs({"tags[]": ".tag"})
    assert sp["name"] == "tags" and sp["array"] is True
    assert sp["selector"] == ".tag" and sp["mode"] == "text"


def test_parse_attr_suffix():
    (sp,) = parse_field_specs({"p": ".price@content"})
    assert sp["selector"] == ".price" and sp["mode"] == "content" and sp["array"] is False


def test_parse_html_suffix():
    (sp,) = parse_field_specs({"h": ".desc@html"})
    assert sp["selector"] == ".desc" and sp["mode"] == "html"


def test_parse_array_and_attr_combined():
    (sp,) = parse_field_specs({"imgs[]": "img@src"})
    assert sp["name"] == "imgs" and sp["array"] is True
    assert sp["selector"] == "img" and sp["mode"] == "src"


def test_parse_dashed_attr_name():
    (sp,) = parse_field_specs({"d": ".x@data-item-id"})
    assert sp["selector"] == ".x" and sp["mode"] == "data-item-id"


def test_parse_at_inside_attribute_value_not_split():
    # a legit `@` inside an attribute-value selector must NOT be treated as the
    # mode separator (suffix isn't a bare attr/html token).
    (sp,) = parse_field_specs({"e": 'a[title="x@y.com"]'})
    assert sp["selector"] == 'a[title="x@y.com"]' and sp["mode"] == "text"


def test_parse_leading_at_not_split():
    # a leading @ (rfind position 0) is left intact — `at > 0` guard.
    (sp,) = parse_field_specs({"e": "@e12"})
    assert sp["selector"] == "@e12" and sp["mode"] == "text"


def test_parse_multiple_fields_order_preserved():
    specs = parse_field_specs({"a": "h1", "b[]": "li", "c": ".x@href"})
    assert [s["name"] for s in specs] == ["a", "b", "c"]


# ─── PURE: dom_js payload wrappers ────────────────────────────────────────
def test_dom_js_page_and_el_differ():
    assert dom_js.FIELDS_PAGE != dom_js.FIELDS_EL
    assert "root = document" in dom_js.FIELDS_PAGE
    assert "root = el" in dom_js.FIELDS_EL


def test_dom_js_fields_core_shape():
    for src in (dom_js.FIELDS_PAGE, dom_js.FIELDS_EL):
        assert "querySelector" in src and "querySelectorAll" in src
        assert "misses" in src and "errors" in src and "matched" in src
        # must not read live input values — retry-safe / no credential leak
        assert ".value" not in src


def test_dom_js_links_and_assets_and_main():
    assert "a[href]" in dom_js.LINKS_PAGE
    assert "data:" in dom_js.ASSETS_PAGE          # guard present
    assert "linkLen" in dom_js.MAIN_PAGE          # density scorer present


# ─── PURE: <form> structure signal ────────────────────────────────────────
def test_form_counted_in_signals():
    _md, sig = extract_with_signals("<main><form><input></form><p>hi</p></main>")
    assert sig["forms"] == 1


def test_form_dropped_from_markdown_text():
    md, _sig = extract_with_signals("<form><input name='x'></form><p>visible</p>")
    assert "visible" in md and "name" not in md   # form subtree not emitted


def test_form_nested_in_nav_not_counted():
    # a form inside an already-skipped subtree (nav) is navigation chrome
    _md, sig = extract_with_signals("<nav><form></form></nav><p>x</p>")
    assert sig["forms"] == 0


def test_forms_alone_do_not_flag_structure_loss():
    _md, sig = extract_with_signals("<form><input></form><p>" + ("word " * 100) + "</p>")
    assert sig["forms"] == 1 and sig["structure_loss"] is False


# ─── LIVE: extract_fields ─────────────────────────────────────────────────
def test_extract_fields_basic_shapes(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    r = call("extract_fields", {"fields": {
        "title": "h1",
        "price": ".price@content",
        "tags[]": ".tag",
        "imgs[]": "img.thumb@src",
        "descHtml": ".desc@html",
        "nope": ".not-here",
        "arr[]": ".not-here-either",
        "bad": ":::",
    }})
    f = r["fields"]
    assert f["title"] == "Widget Pro"
    assert f["price"] == "42.00"                     # @content attribute
    assert f["tags"] == ["alpha", "beta", "gamma"]   # array
    assert len(f["imgs"]) == 2 and f["imgs"][0].endswith("/img/t1.png")  # raw src attr (static getAttribute, not resolved)
    assert "<b>Rich</b>" in f["descHtml"]            # @html
    assert f["nope"] is None and f["arr"] == []      # single miss -> null, array miss -> []
    assert r["matched"]["tags"] == 3
    assert "nope" in r["misses"] and "arr" in r["misses"]
    assert "bad" in r["errors"] and f["bad"] is None  # invalid selector -> error, not raise
    # token-frugal: no base64 payload
    assert "screenshot_b64" not in r and "png_b64" not in r


def test_extract_fields_node_cap_limits_array(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    # fixture has 5 .item nodes; node_cap must clamp the array
    r = call("extract_fields", {"fields": {"arr[]": ".item"}, "node_cap": 2})
    assert r["fields"]["arr"] == ["i1", "i2"]
    assert r["matched"]["arr"] == 2


def test_extract_fields_field_count_cap(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    # 65 fields > _MAX_FIELDS (64) must be rejected
    raised = False
    try:
        call("extract_fields", {"fields": {f"k{i}": "div" for i in range(65)}})
    except Exception:  # noqa: BLE001 — daemon surfaces the ValueError
        raised = True
    assert raised
    # boundary: exactly 64 is accepted (count guard does not trip)
    ok = call("extract_fields", {"fields": {f"k{i}": "h1" for i in range(64)}})
    assert "fields" in ok


def test_extract_fields_target_scopes(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    # scope to .desc: <b> resolves inside it, but <h1> does NOT (it's outside)
    r = call("extract_fields", {"target": ".desc", "fields": {"b": "b", "h": "h1"}})
    assert r["fields"]["b"] == "Rich"
    assert r["fields"]["h"] is None and "h" in r["misses"]


def test_extract_fields_rejects_empty_and_oversize(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    for bad in ({}, {"fields": {}}, {"fields": "notamap"}):
        raised = False
        try:
            call("extract_fields", bad)
        except Exception:  # noqa: BLE001 — daemon surfaces the ValueError
            raised = True
        assert raised, f"expected an error for {bad!r}"


# ─── LIVE: extract --mode links / assets / main ───────────────────────────
def test_extract_mode_links_absolute_and_filtered(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    r = call("extract", {"mode": "links"})
    assert r["mode"] == "links"
    urls = [ln["url"] for ln in r["links"]]
    assert any(u.endswith("/deep/page") for u in urls)        # relative -> absolute
    assert any("example.com/abs" in u for u in urls)          # absolute kept
    assert not any("javascript:" in u.lower() for u in urls)  # js link dropped
    # dedup: the duplicate /deep/page appears once
    assert sum(1 for u in urls if u.endswith("/deep/page")) == 1
    assert all("url" in ln and "text" in ln for ln in r["links"])


def test_extract_mode_assets_classifies_and_drops_data(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    r = call("extract", {"mode": "assets"})
    assert r["mode"] == "assets"
    by_url = {a["url"]: a for a in r["assets"]}
    assert any(u.endswith("/img/hero.png") and by_url[u]["type"] == "image" for u in by_url)
    assert any(u.endswith("/app.js") and by_url[u]["type"] == "script" for u in by_url)
    assert any(u.endswith("/style.css") and by_url[u]["type"] == "link" for u in by_url)
    # data: URI image dropped (no-base64 rule)
    assert not any(u.startswith("data:") for u in by_url)
    # dedup: /img/hero.png appears twice in the DOM (hero + dup) → once here
    assert sum(1 for a in r["assets"] if a["url"].endswith("/img/hero.png")) == 1


def test_extract_mode_main_picks_dense_block(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    r = call("extract", {"mode": "main"})
    assert r["mode"] == "main"
    assert r["main_content"] is True          # density path, not fallback
    assert "Widget Pro" in r["markdown"]
    assert "FOOTER BOILERPLATE" not in r["markdown"]
    # the density scorer picked <main>, so the sibling promo div is excluded —
    # a whole-page FALLBACK would have included it (proves the pick happened).
    assert "PROMO SIDEBAR JUNK" not in r["markdown"]


def test_extract_mode_main_falls_back_when_no_dense_block(local_server):
    # main_sparse.html has only short <p> (no candidate block >= 200 chars) →
    # the scorer finds nothing and returns the whole page instead of dropping it.
    call("go", {"url": f"{local_server}/main_sparse.html", "wait_until": "load"})
    r = call("extract", {"mode": "main"})
    assert r.get("main_fallback") is True
    assert r["main_content"] is False
    assert "alpha short paragraph" in r["markdown"]


def test_extract_mode_main_rejects_target(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    # main is document-level; a target must be rejected, not silently ignored
    raised = False
    try:
        call("extract", {"mode": "main", "selector": ".body"})
    except Exception:  # noqa: BLE001 — daemon surfaces the ValueError
        raised = True
    assert raised


def test_extract_default_surfaces_forms_hint(local_server):
    call("go", {"url": f"{local_server}/fields.html", "wait_until": "load"})
    r = call("extract", {})
    assert r.get("forms") == 1
    assert "forms_hint" in r and "map" in r["forms_hint"]
