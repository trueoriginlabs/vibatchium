"""0.9.0 — tests for the LLM-ready `extract` verb.

PURE: the stdlib HTML→Markdown converter (vibatchium/extract.py).
LIVE: the `extract` verb end-to-end against a served fixture, proving it returns
markdown TEXT (never base64) with boilerplate stripped.
"""
from __future__ import annotations

from vibatchium.extract import html_to_markdown
from vibatchium.client import call


# ─── PURE: html_to_markdown ───────────────────────────────────────────────
_HTML = """
<html><head><title>t</title><style>.a{}</style><script>x=1</script></head>
<body>
<nav>NAV BOILERPLATE</nav>
<main>
<h1>Title</h1>
<p>A <strong>bold</strong> and <em>italic</em> line with a <a href="https://e.com/p">link</a>.</p>
<ul><li>one</li><li>two</li></ul>
<ol><li>first</li><li>second</li></ol>
<pre><code>def f():
    return 1</code></pre>
</main>
<footer>FOOTER BOILERPLATE</footer>
</body></html>
"""


def test_markdown_strips_boilerplate_and_scripts():
    md = html_to_markdown(_HTML)
    assert "NAV BOILERPLATE" not in md
    assert "FOOTER BOILERPLATE" not in md
    assert "x=1" not in md and ".a{}" not in md


def test_markdown_preserves_structure():
    md = html_to_markdown(_HTML)
    assert "# Title" in md
    assert "**bold**" in md and "*italic*" in md
    assert "[link](https://e.com/p)" in md
    assert "- one" in md and "- two" in md
    assert "1. first" in md and "2. second" in md


def test_markdown_preserves_code_indentation():
    md = html_to_markdown(_HTML)
    assert "```" in md
    assert "    return 1" in md          # pre indentation NOT collapsed


def test_markdown_empty_input():
    assert html_to_markdown("") == ""
    assert html_to_markdown(None) == ""  # type: ignore[arg-type]


def test_markdown_malformed_html_does_not_raise():
    # unbalanced tags must degrade, not throw
    assert isinstance(html_to_markdown("<p>oops<div><b>x"), str)


# ─── LIVE: the extract verb ───────────────────────────────────────────────
def test_extract_verb_returns_markdown_not_base64(local_server):
    call("go", {"url": f"{local_server}/article.html", "wait_until": "load"})
    r = call("extract", {})
    md = r["markdown"]
    assert "# The Main Title" in md
    assert "[deep link](https://example.com/deep)" in md
    assert "1. Step one" in md
    # boilerplate gone
    assert "SITE HEADER CHROME" not in md
    assert "COPYRIGHT FOOTER BOILERPLATE" not in md
    assert "RELATED LINKS SIDEBAR" not in md
    # token-frugal: text only, no image payload
    assert "screenshot_b64" not in r and "png_b64" not in r
    assert r["chars"] == len(md)
    assert "article.html" in r.get("url", "")


def test_extract_verb_max_chars_truncates(local_server):
    call("go", {"url": f"{local_server}/article.html", "wait_until": "load"})
    r = call("extract", {"max_chars": 20})
    assert len(r["markdown"]) == 20
    assert r.get("truncated") is True


def test_extract_verb_target_scopes_subtree(local_server):
    call("go", {"url": f"{local_server}/article.html", "wait_until": "load"})
    r = call("extract", {"target": "h1"})
    assert "The Main Title" in r["markdown"]
    assert "Step one" not in r["markdown"]   # scoped to the h1 only


# ─── 0.10.0 PURE: structure-loss signals ──────────────────────────────────
from vibatchium.extract import extract_with_signals


def test_signals_wide_data_table_flags_loss():
    # a genuinely wide table (3 cols x 3 rows) linearizes to ambiguous pipe-runs
    md, sig = extract_with_signals(
        "<table>"
        "<tr><th>A</th><th>B</th><th>C</th></tr>"
        "<tr><td>1</td><td>2</td><td>3</td></tr>"
        "<tr><td>4</td><td>5</td><td>6</td></tr></table>")
    assert sig["tables"] == 1 and sig["table_rows"] == 3 and sig["table_cells"] == 9
    assert sig["structure_loss"] is True


def test_signals_single_column_table_no_loss():
    # a 1-col "layout" table is NOT a multi-column data table → no loss flag
    _md, sig = extract_with_signals(
        "<table><tr><td>only</td></tr><tr><td>one</td></tr></table>")
    assert sig["structure_loss"] is False


def test_signals_two_column_table_no_loss():
    # a narrow 2-col table reads fine as markdown pipes — must NOT over-fire
    # (regression for the heuristic that used to flag any >=2-col table).
    _md, sig = extract_with_signals(
        "<table><tr><th>k</th><th>v</th></tr>"
        "<tr><td>1</td><td>2</td></tr></table>")
    assert sig["table_rows"] == 2 and sig["table_cells"] == 4
    assert sig["structure_loss"] is False


def test_signals_single_row_wide_layout_table_no_loss():
    # a 1-row table (cells across one row) is layout, not data → no loss
    _md, sig = extract_with_signals(
        "<table><tr><td>logo</td><td>nav</td><td>search</td><td>cta</td></tr></table>")
    assert sig["structure_loss"] is False


def test_signals_canvas_only_flags_loss():
    _md, sig = extract_with_signals("<p>hi</p><canvas></canvas>")
    assert sig["canvas"] == 1 and sig["svg"] == 0
    assert sig["structure_loss"] is True


def test_signals_content_svg_only_flags_loss():
    # a dimensionless / large svg is treated as chart content
    _md, sig = extract_with_signals("<p>hi</p><svg><path/></svg>")
    assert sig["svg"] == 1 and sig["svg_icon"] == 0
    assert sig["structure_loss"] is True


def test_signals_decorative_icon_svg_no_loss():
    # an icon-sized svg (<=64px both dims) is decorative, not dropped content
    md, sig = extract_with_signals(
        "<p>" + ("word " * 50) + "</p><svg width='16' height='16'><path/></svg>")
    assert sig["svg"] == 1 and sig["svg_icon"] == 1
    assert sig["structure_loss"] is False


def test_signals_image_heavy_thin_text_flags_loss():
    _md, sig = extract_with_signals(
        "<img src='a.png'><img src='b.png'><img src='c.png'><p>tiny</p>")
    assert sig["img"] == 3
    assert sig["structure_loss"] is True


def test_signals_prose_has_no_loss():
    md, sig = extract_with_signals("<h1>Title</h1><p>" + ("word " * 100) + "</p>")
    assert sig["structure_loss"] is False
    assert "# Title" in md
    assert sig["svg"] == 0 and sig["tables"] == 0


def test_signals_empty_input():
    md, sig = extract_with_signals("")
    assert md == "" and sig["structure_loss"] is False


def test_html_to_markdown_still_delegates():
    # backward-compat: html_to_markdown now delegates to extract_with_signals
    assert "# T" in html_to_markdown("<h1>T</h1>")
    assert html_to_markdown("") == ""


# ─── 0.10.0 LIVE: extract flags structure loss; screenshot --tiles ─────────
from pathlib import Path


def test_extract_verb_flags_structure_loss():
    call("go", {"url": "data:text/html,<table>"
                       "<tr><th>A</th><th>B</th><th>C</th></tr>"
                       "<tr><td>1</td><td>2</td><td>3</td></tr>"
                       "<tr><td>4</td><td>5</td><td>6</td></tr></table>"})
    r = call("extract", {})
    assert r.get("structure_loss") is True
    assert r["structure_signals"]["tables"] >= 1


def test_screenshot_tiles_writes_files_not_base64(tmp_path):
    call("go", {"url": "data:text/html,<h1>Tall</h1>" + ("<p>line</p>" * 300)})
    r = call("screenshot", {"tiles": True, "tile_height": 800,
                            "tile_dir": str(tmp_path)})
    assert r["count"] >= 1
    assert "png_b64" not in r and "tiles" in r
    for p in r["tiles"]:
        fp = Path(p)
        assert fp.exists() and fp.stat().st_size > 0


def test_screenshot_tiles_truncation_is_signalled(tmp_path):
    # an explicit small max_tiles on a multi-tile page caps the OUTPUT and the
    # result must SIGNAL it (truncated + total_tiles) — never silently drop.
    call("go", {"url": "data:text/html,<h1>Tall</h1>" + ("<p>line</p>" * 400)})
    r = call("screenshot", {"tiles": True, "tile_height": 300,
                            "max_tiles": 2, "tile_dir": str(tmp_path)})
    assert r["count"] == 2
    assert r.get("truncated") is True
    assert r.get("total_tiles", 0) > 2


def test_screenshot_height_cap_bounds_capture_and_signals(tmp_path):
    # 0.10.0: max_screenshot_px bounds the CAPTURED height (the decode), not just
    # tile count. Only the top N px is captured; totals come from the MEASURED
    # page (honest), so height_truncated + captured/total_height_px are reported.
    call("go", {"url": "data:text/html,<h1>Tall</h1>" + ("<p>line</p>" * 400)})
    r = call("screenshot", {"tiles": True, "tile_height": 300,
                            "max_screenshot_px": 900, "tile_dir": str(tmp_path)})
    assert r.get("height_truncated") is True
    assert r["captured_height_px"] == 900
    assert r["total_height_px"] > 900          # measured real page, not the clip
    assert r["count"] == 3                      # only the top 900px → 3x300 tiles
    assert r.get("truncated") is True and r["total_tiles"] > 3


def test_screenshot_plain_fullpage_height_cap_signalled(tmp_path):
    # the PLAIN (non-tiles) --full-page path is bounded + signalled too.
    call("go", {"url": "data:text/html,<h1>Tall</h1>" + ("<p>line</p>" * 400)})
    out = str(tmp_path / "full.png")
    r = call("screenshot", {"full_page": True, "path": out, "max_screenshot_px": 900})
    assert r.get("height_truncated") is True
    assert r["captured_height_px"] == 900 and r["total_height_px"] > 900
    assert Path(out).exists() and Path(out).stat().st_size > 0


def test_screenshot_height_cap_disabled_is_no_op(tmp_path):
    # max_screenshot_px=0 disables the ceiling; a short page is never truncated.
    call("go", {"url": "data:text/html,<h1>x</h1><p>short</p>"})
    r = call("screenshot", {"tiles": True, "tile_height": 300,
                            "max_screenshot_px": 0, "tile_dir": str(tmp_path)})
    assert "height_truncated" not in r


def test_screenshot_short_page_no_height_signal(tmp_path):
    # a page shorter than the (default) cap is a byte-identical no-op: no fields.
    call("go", {"url": "data:text/html,<h1>tiny</h1>"})
    out = str(tmp_path / "s.png")
    r = call("screenshot", {"full_page": True, "path": out})
    assert "height_truncated" not in r and r.get("path") == out
