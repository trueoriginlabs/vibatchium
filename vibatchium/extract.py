"""Dependency-free HTML → Markdown extraction for the ``extract`` verb.

0.9.0 (competitive-landscape lesson): OSS scrapers (Crawl4AI, Firecrawl) win on
clean, RAG-ready markdown output. ``extract`` makes vibatchium a drop-in for the
*authenticated* pages those stateless tools can't reach — it returns LLM-ready
markdown (and selector-scoped text), NOT a base64 screenshot, so it never
re-creates the explore token-burn.

The converter is built on the stdlib ``html.parser`` with NO third-party
dependency, so ``extract`` works on a bare install and stays in the lean MCP
surface (LLM-ready markdown tolerates minor imperfection; we optimize for clean
structure — headings, links, lists, code — not byte-perfect rendering).
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

# Tags whose entire subtree is boilerplate/non-content — dropped wholesale.
_SKIP_SUBTREE = {
    "script", "style", "noscript", "template", "svg", "head", "title",
    "nav", "footer", "aside", "header", "form", "button", "select",
    "option", "iframe", "object", "embed", "canvas",
}
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class _MarkdownExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0          # >0 ⇒ inside a dropped subtree
        self._list_stack: list[list] = []   # each: [kind, counter]
        self._a_href: str | None = None
        self._a_text: list[str] = []
        self._pre_depth = 0

    # ── emit helpers ────────────────────────────────────────────────────
    def _emit(self, text: str) -> None:
        if self._a_href is not None:
            self._a_text.append(text)
        else:
            self._out.append(text)

    def _nl(self, n: int = 1) -> None:
        self._emit("\n" * n)

    # ── tags ────────────────────────────────────────────────────────────
    def handle_starttag(self, tag, attrs):
        if self._skip_depth:
            if tag in _SKIP_SUBTREE:
                self._skip_depth += 1
            return
        if tag in _SKIP_SUBTREE:
            self._skip_depth += 1
            return
        ad = dict(attrs)
        if tag in _HEADINGS:
            self._nl(2)
            self._emit("#" * _HEADINGS[tag] + " ")
        elif tag in ("p", "section", "article", "figure"):
            self._nl(2)
        elif tag == "br":
            self._nl(1)
        elif tag == "hr":
            self._nl(2); self._emit("---"); self._nl(2)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and self._pre_depth == 0:
            self._emit("`")
        elif tag == "pre":
            self._pre_depth += 1
            self._nl(2); self._emit("```"); self._nl(1)
        elif tag == "blockquote":
            self._nl(2); self._emit("> ")
        elif tag in ("ul", "ol"):
            self._list_stack.append([tag, 0])
        elif tag == "li":
            depth = max(0, len(self._list_stack) - 1)
            indent = "  " * depth
            self._nl(1)
            if self._list_stack and self._list_stack[-1][0] == "ol":
                self._list_stack[-1][1] += 1
                self._emit(f"{indent}{self._list_stack[-1][1]}. ")
            else:
                self._emit(f"{indent}- ")
        elif tag == "a":
            self._a_href = ad.get("href")
            self._a_text = []
        elif tag == "img":
            alt = ad.get("alt", "").strip()
            src = ad.get("src", "")
            if src:
                self._emit(f"![{alt}]({src})")
            elif alt:
                self._emit(alt)
        elif tag in ("td", "th"):
            self._emit(" | ")
        elif tag == "tr":
            self._nl(1)

    def handle_endtag(self, tag):
        if self._skip_depth:
            if tag in _SKIP_SUBTREE:
                self._skip_depth -= 1
            return
        if tag in _HEADINGS:
            self._nl(2)
        elif tag in ("p", "section", "article", "figure"):
            self._nl(2)
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "code" and self._pre_depth == 0:
            self._emit("`")
        elif tag == "pre":
            self._pre_depth = max(0, self._pre_depth - 1)
            self._nl(1); self._emit("```"); self._nl(2)
        elif tag == "blockquote":
            self._nl(2)
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            if not self._list_stack:
                self._nl(1)
        elif tag == "a":
            text = "".join(self._a_text).strip()
            href = self._a_href
            self._a_href = None
            self._a_text = []
            if text and href:
                self._out.append(f"[{text}]({href})")
            elif text:
                self._out.append(text)

    def _last_char(self) -> str:
        buf = self._a_text if self._a_href is not None else self._out
        for chunk in reversed(buf):
            if chunk:
                return chunk[-1]
        return ""

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._pre_depth:
            self._emit(data)
            return
        # collapse intra-run whitespace; block boundaries are emitted as newlines
        collapsed = re.sub(r"\s+", " ", data)
        # don't double a space already laid down by a marker / prior chunk
        if collapsed.startswith(" ") and self._last_char() in (" ", "\n", ""):
            collapsed = collapsed[1:]
        if collapsed:
            self._emit(collapsed)

    def result(self) -> str:
        md = "".join(self._out)
        md = re.sub(r"[ \t]+\n", "\n", md)         # strip trailing spaces at line ends
        md = re.sub(r"\n{3,}", "\n\n", md)         # collapse blank-line runs
        # NOTE: intra-line spaces are NOT globally collapsed — <pre> content is
        # emitted verbatim, so collapsing would corrupt code-block indentation.
        return md.strip()


def html_to_markdown(html: str) -> str:
    """Convert an HTML string to clean, LLM-ready Markdown. Boilerplate
    subtrees (script/style/nav/footer/aside/header/form/…) are dropped."""
    if not html:
        return ""
    parser = _MarkdownExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — never let a malformed page raise; return best-effort
        pass
    return parser.result()
