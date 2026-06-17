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
