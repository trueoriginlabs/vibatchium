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

# An svg this small in BOTH dimensions is a decorative icon (chevron, glyph,
# social/link icon), not dropped chart content — exclude it from the signal.
_ICON_SVG_MAX_PX = 64


def _px(value: str | None) -> float | None:
    """Parse a leading CSS pixel length (``16``, ``16px``, ``24 ``) to a float;
    None for missing / non-numeric (e.g. ``100%``, ``1em``) so it's treated as
    'unknown size' = potentially content."""
    if not value:
        return None
    m = re.match(r"\s*([0-9]*\.?[0-9]+)", value)
    return float(m.group(1)) if m else None


def _svg_is_icon(attrs) -> bool:
    """An svg with explicit width AND height both <= _ICON_SVG_MAX_PX is a
    decorative icon. Missing/relative dimensions => unknown => not an icon."""
    d = dict(attrs)
    w, h = _px(d.get("width")), _px(d.get("height"))
    return w is not None and h is not None and w <= _ICON_SVG_MAX_PX and h <= _ICON_SVG_MAX_PX


class _MarkdownExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0          # >0 ⇒ inside a dropped subtree
        self._list_stack: list[list] = []   # each: [kind, counter]
        self._a_href: str | None = None
        self._a_text: list[str] = []
        self._pre_depth = 0
        # 0.10.0: structure-loss accounting — count the visual/structural
        # elements that Markdown extraction degrades (tables linearized to
        # pipe-runs) or drops wholesale (svg/canvas charts), so a caller can
        # decide whether a pixel (VLM) read would recover lost signal.
        # `svg_icon` tracks decorative icon-sized svgs (ubiquitous chevrons /
        # glyphs) so they don't masquerade as dropped chart content.
        self.sig: dict[str, int] = {
            "tables": 0, "table_rows": 0, "table_cells": 0,
            "svg": 0, "svg_icon": 0, "canvas": 0, "img": 0, "forms": 0,
        }

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
            # count the structure-bearing visuals we DROP (vector/canvas charts)
            # before discarding the subtree. A decorative icon-sized <svg> is
            # not content, so tally it separately and net it out of the signal.
            if tag in ("svg", "canvas"):
                self.sig[tag] += 1
                if tag == "svg" and _svg_is_icon(attrs):
                    self.sig["svg_icon"] += 1
            # 0.14.0: <form> is dropped from markdown (it's interaction, not prose)
            # but count it so `extract` can HINT the agent to `map`/detect_forms
            # instead of silently swallowing the page's forms. A form nested in an
            # already-skipped subtree (nav>form) returns above, so only top-level
            # content forms are tallied.
            elif tag == "form":
                self.sig["forms"] += 1
            self._skip_depth += 1
            return
        ad = dict(attrs)
        # structure-loss accounting (independent of the emission chain below).
        if tag == "table":
            self.sig["tables"] += 1
        elif tag == "tr":
            self.sig["table_rows"] += 1
        elif tag in ("td", "th"):
            self.sig["table_cells"] += 1
        elif tag == "img":
            self.sig["img"] += 1
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
    md, _sig = extract_with_signals(html)
    return md


_EMPTY_SIGNALS: dict[str, object] = {
    "tables": 0, "table_rows": 0, "table_cells": 0, "svg": 0, "svg_icon": 0,
    "canvas": 0, "img": 0, "forms": 0, "text_chars": 0, "html_chars": 0,
    "structure_loss": False,
}

# A field-spec suffix after the last ``@`` is treated as the extraction mode only
# when it's a bare ``html`` or attribute-name token — so an ``@`` inside an
# attribute-value selector (``a[title="x@y"]``) is left intact.
_FIELD_MODE_RE = re.compile(r"^(?:html|[A-Za-z_][\w:-]*)$")


def parse_field_specs(fields: dict) -> list[dict]:
    """Parse a declarative ``{name: selector}`` field map into a normalized
    instruction list for the in-page extractor (:mod:`vibatchium.dom_js`).

    Grammar (obscura-compatible so agent knowledge transfers, plus our ``@html``):

      * a NAME ending in ``[]`` returns EVERY match as an array (``imgs[]``);
        otherwise the first match, or ``null``.
      * a SELECTOR may carry a trailing ``@<attr>`` to read that attribute or
        ``@html`` to read ``innerHTML``; with no ``@`` suffix the element's text
        (``innerText`` / ``textContent``) is returned.

    Only the LAST ``@`` splits, and only when its suffix is a bare
    attribute-name / ``html`` token. Pure — no I/O, unit-tested.

    Returns a list of ``{name, selector, mode, array}`` dicts (``mode`` is
    ``"text"``, ``"html"``, or an attribute name).
    """
    specs: list[dict] = []
    for raw_name, raw_sel in fields.items():
        name = str(raw_name)
        array = False
        if name.endswith("[]"):
            array = True
            name = name[:-2]
        sel = str(raw_sel).strip()
        mode = "text"
        at = sel.rfind("@")
        if at > 0:
            suffix = sel[at + 1:]
            if _FIELD_MODE_RE.match(suffix):
                mode = suffix  # "html" is handled specially in JS; attrs pass through
                sel = sel[:at].strip()
        specs.append({"name": name, "selector": sel, "mode": mode, "array": array})
    return specs


def _structure_loss(sig: dict, text_chars: int, html_chars: int) -> bool:
    """Heuristic: did Markdown extraction probably lose meaningful visual or
    structural signal that a pixel (VLM) read would recover?

    Deliberately conservative — a false positive nudges a caller toward an
    expensive pixel read, the very cost `extract` exists to avoid. Fires when:
      * a genuine DATA table is present — multi-row (>=2) AND wide (averaging
        >=3 cells/row) — which linearizes to ambiguous ``|`` runs with no
        alignment. Small / 2-column / single-row layout tables read fine as
        markdown, so they do NOT count;
      * a ``<canvas>`` or a non-icon ``<svg>`` is present — vector/canvas charts
        and diagrams are dropped wholesale; decorative icon-sized svgs don't;
      * the page is image-heavy (>=3 imgs) but yielded little text (alt-text only).
    """
    multicol_tables = (
        sig["tables"] >= 1
        and sig["table_rows"] >= 2
        and sig["table_cells"] >= 3 * sig["table_rows"]
    )
    content_svg = sig["svg"] - sig.get("svg_icon", 0)
    dropped_visuals = sig["canvas"] >= 1 or content_svg >= 1
    image_heavy_thin_text = sig["img"] >= 3 and text_chars < 400
    return bool(multicol_tables or dropped_visuals or image_heavy_thin_text)


def extract_with_signals(html: str) -> tuple[str, dict]:
    """Like :func:`html_to_markdown`, but also return *structure-loss signals*
    so a caller can decide whether to escalate to a pixel (VLM) read.

    Returns ``(markdown, signals)`` where ``signals`` carries the element
    counts plus ``text_chars``, ``html_chars`` and a ``structure_loss`` bool.
    Pure and cheap — a single parse pass, no extra dependency.
    """
    if not html:
        return "", dict(_EMPTY_SIGNALS)
    parser = _MarkdownExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 — never let a malformed page raise; best-effort
        pass
    md = parser.result()
    sig = dict(parser.sig)
    sig["text_chars"] = len(md)
    sig["html_chars"] = len(html)
    sig["structure_loss"] = _structure_loss(sig, len(md), len(html))
    return md, sig
