"""@eN element-ref model built on Playwright's `page.aria_snapshot(mode='ai')`.

Playwright already produces a YAML-style accessibility-tree snapshot with stable
`[ref=eN]` markers that resolve via the `aria-ref=eN` selector engine. We use it
as-is, but display refs in Vibium's `@eN` style (substitute on output, reverse
on input) so this CLI feels native to anyone migrating from Vibium.

Public API:
- `take_snapshot(page) -> Snapshot`   — captures the AI-mode AX snapshot
- `Snapshot.text(indent)`             — Vibium-style human-readable map
- `resolve(page, ref)`                — returns a Locator for an `@eN` ref
- `diff(prev, new)`                   — line-based diff of two snapshots
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


REF_RE = re.compile(r"\[ref=(e\d+)\]")


def _to_vibium(yaml_text: str) -> str:
    """Substitute Playwright's `[ref=eN]` markers with Vibium-style `@eN`."""
    return REF_RE.sub(lambda m: f"@{m.group(1)}", yaml_text)


def _normalize_ref(ref: str) -> str:
    """Accept `@e3`, `e3`, or `[ref=e3]`; return canonical playwright form `e3`."""
    ref = ref.strip()
    if ref.startswith("@"):
        return ref[1:]
    m = REF_RE.match(ref)
    if m:
        return m.group(1)
    return ref


@dataclass
class Snapshot:
    url: str
    raw_yaml: str  # playwright's raw output, with [ref=eN] markers
    refs: list[str] = field(default_factory=list)  # ['e1', 'e2', ...] in document order

    def text(self, indent: bool = True) -> str:
        """Render in Vibium-friendly form (`@eN` notation).

        Playwright's output is already indented YAML; we substitute the ref
        notation and optionally flatten indents for compact listings.
        """
        out = _to_vibium(self.raw_yaml)
        if not indent:
            out = "\n".join(line.lstrip() for line in out.splitlines())
        return out

    def json(self) -> dict:
        return {"url": self.url, "yaml": self.text(indent=True), "refs": self.refs}


async def take_snapshot(page, depth: int | None = None) -> Snapshot:
    raw = await page.aria_snapshot(mode="ai", depth=depth)
    refs = REF_RE.findall(raw)
    return Snapshot(url=page.url, raw_yaml=raw, refs=refs)


def resolve(page, snap: Snapshot | None, ref: str):
    """Return a Playwright Locator for an `@eN`/`eN`/`[ref=eN]` reference.

    The snapshot arg is kept for API symmetry but isn't strictly required —
    Playwright's `aria-ref=` selector engine resolves against the page's
    current accessibility tree directly.
    """
    canonical = _normalize_ref(ref)
    return page.locator(f"aria-ref={canonical}")


# ─── semantic-prefix shortcuts (Wave 7.7.9) ────────────────────────────
#
# Three consecutive agent runs reached for `@text:Foo` / `@label:Foo` /
# `@role:button` syntax that didn't exist — they expected MCP-style shortcuts
# for Playwright's getByText / getByLabel / getByRole. Adding them now so the
# muscle-memory works.

_SEMANTIC_PREFIX_RE = re.compile(
    r"^@(text|label|role|placeholder|testid|title|alt):(.+)$",
    re.DOTALL,
)
_ROLE_WITH_NAME_RE = re.compile(r"^([a-z]+)(?:\[name=(.+)\])?$")

# CSS-pattern detectors that have to match an actual selector shape, not just
# a stray character — we need "Mr. Smith" and "More information..." to be
# treated as text, but "h1.title" and "input[type=text]" to stay as CSS.
_CSS_CLASS_PATTERN_RE = re.compile(r"\w\.\w")   # foo.bar
_CSS_ID_PATTERN_RE = re.compile(r"\w#\w")        # foo#bar
_PW_SELECTOR_PREFIXES = ("text=", "role=", "label=", "xpath=", "css=",
                         "data-testid=", "id=", "//", "..")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _looks_like_plain_text(target: str) -> bool:
    """Heuristic: should a bare quoted target be treated as visible text?

    Permissive — leans toward text when ambiguous. Specifically rejects:
    - Single-word strings (could be HTML tags)
    - Starts with CSS sigil: . # [ *
    - Contains [ or ] (attribute selectors)
    - Contains CSS combinators: ` > `, ` ~ `, ` + `
    - Starts with a Playwright selector-engine prefix or XPath
    - Contains a word-dot-word pattern (CSS class: `h1.title`, `button.submit`)
    - Contains a word-hash-word pattern (CSS id: `div#main`)

    Specifically ALLOWS (text with dots, commas, etc.):
    - "Mr. Smith", "More information...", "Hello, world!"
    """
    t = target.strip()
    if " " not in t:
        return False
    if t[0] in "#.[*":
        return False
    if any(p in t for p in (" > ", " ~ ", " + ", "[", "]")):
        return False
    if any(t.startswith(p) for p in _PW_SELECTOR_PREFIXES):
        return False
    if _CSS_CLASS_PATTERN_RE.search(t) or _CSS_ID_PATTERN_RE.search(t):
        return False
    return True


def resolve_target(page, snap: Snapshot | None, target: str):
    """Map a target string to a Playwright Locator, recognizing:

    1. `@eN` / `eN` / `[ref=eN]`  → aria-ref selector (existing)
    2. `@text:Foo`                → page.get_by_text("Foo")
    3. `@label:Email`             → page.get_by_label("Email")
    4. `@role:button`             → page.get_by_role("button")
    5. `@role:button[name=Submit]`→ page.get_by_role("button", name="Submit")
    6. `@placeholder:Search`      → page.get_by_placeholder("Search")
    7. `@testid:foo`              → page.get_by_test_id("foo")
    8. `@title:Foo`               → page.get_by_title("Foo")
    9. `@alt:Foo`                 → page.get_by_alt_text("Foo")
    10. `"Multi word text"`       → page.get_by_text(...) auto-fallback
    11. anything else             → page.locator(...) raw selector

    `page` accepts either a Page or a Frame (both have get_by_* methods).
    """
    t = target.strip()

    # 1. @eN ref
    if t.startswith("@e") and len(t) > 2 and t[2].isdigit():
        return resolve(page, snap, t)
    if t.startswith("e") and len(t) > 1 and t[1:].isdigit():
        return resolve(page, snap, t)

    # 2-9. @prefix:value semantic shortcuts
    m = _SEMANTIC_PREFIX_RE.match(t)
    if m:
        kind, raw_value = m.group(1), m.group(2)
        value = _strip_quotes(raw_value)
        if kind == "text":
            return page.get_by_text(value)
        if kind == "label":
            return page.get_by_label(value)
        if kind == "placeholder":
            return page.get_by_placeholder(value)
        if kind == "testid":
            return page.get_by_test_id(value)
        if kind == "title":
            return page.get_by_title(value)
        if kind == "alt":
            return page.get_by_alt_text(value)
        if kind == "role":
            role_m = _ROLE_WITH_NAME_RE.match(value)
            if role_m:
                role = role_m.group(1)
                name = role_m.group(2)
                if name:
                    return page.get_by_role(role, name=_strip_quotes(name))
                return page.get_by_role(role)
            # Fall through to raw locator if format doesn't match

    # 10. Plain-text auto-fallback
    if _looks_like_plain_text(t):
        return page.get_by_text(t)

    # 11. Raw Playwright/CSS selector
    return page.locator(t)


# ─── compact one-liner render (0.14.1) ──────────────────────────────────────
#
# Parses each aria-snapshot line into `@eN role "name" [state…]`, PRESERVING the
# state brackets ([checked]/[disabled]/[expanded]/[selected]/[level=N]) that the
# old regex-scrape silently dropped. The `@eN` ref sits at the LINE END (before an
# optional `:` when the node has children), so we anchor on it and parse the head
# backward — robust to `@`, quotes and brackets appearing inside a name.

# The ref may be FOLLOWED by non-state bracket tokens Playwright appends in
# mode='ai' — notably `[cursor=pointer]` (emitted for every `<a href>` and any
# cursor:pointer element) and an optional `:` when the node has children. If the
# anchor demanded the ref be line-final it would silently drop every link/styled
# button, so tolerate trailing `[...]` groups + an optional colon after the ref.
_COMPACT_TAIL_RE = re.compile(r"\s*@(?P<ref>e\d+)(?:\s*\[[^\]]*\])*:?\s*$")
# name tolerates backslash-escaped quotes (Playwright renders names via JSON.stringify).
_COMPACT_HEAD_RE = re.compile(r'^(?P<role>\S+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?(?P<rest>.*)$')
_COMPACT_STATE_RE = re.compile(r"\[([^\]]+)\]")

# Actionable roles surfaced by `interactive=True` (state-bearing controls + links).
_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio",
    "combobox", "listbox", "option", "switch", "slider", "spinbutton",
    "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "treeitem",
})


def compact_lines(snap: Snapshot, *, interactive_only: bool = False) -> list[tuple[str, str]]:
    """Render a snapshot as `@eN role "name" [state…]` one-liners.

    Returns ``[(ref, line)]`` (ref without the ``@``) so a caller can attach
    per-ref data (e.g. a bounding box). Only lines carrying an ``@eN`` ref are
    emitted; with ``interactive_only`` non-actionable roles are skipped.
    """
    # Ground "addressable" in the TRUE `[ref=eN]` markers (from the raw YAML,
    # before _to_vibium rewrote them) — so literal `@eN`-looking page text at a
    # line end is not mistaken for a ref (phantom-ref guard).
    real_refs = set(REF_RE.findall(snap.raw_yaml))
    out: list[tuple[str, str]] = []
    for raw in snap.text(indent=True).splitlines():
        tail = _COMPACT_TAIL_RE.search(raw)
        if not tail:
            continue                                  # no ref → not addressable
        ref = tail.group("ref")
        if ref not in real_refs:
            continue                                  # literal '@eN' text, not a marker
        body = raw[: tail.start()].strip()
        if body.startswith("-"):
            body = body[1:].lstrip()
        head = _COMPACT_HEAD_RE.match(body)
        if not head:
            continue
        role = head.group("role").rstrip(":")
        if interactive_only and role not in _INTERACTIVE_ROLES:
            continue
        name = head.group("name")
        states = _COMPACT_STATE_RE.findall(head.group("rest") or "")
        parts = [f"@{ref}", role]
        if name is not None:
            parts.append(f'"{name}"')
        parts.extend(f"[{s}]" for s in states)
        out.append((ref, " ".join(parts)))
    return out


# ─── diff ──────────────────────────────────────────────────────────────────


def diff(prev: Snapshot | None, new: Snapshot) -> str:
    """Unified diff of two snapshots' YAML text.

    Format: standard `+`/`-` line markers. Empty string if identical.
    """
    prev_text = _to_vibium(prev.raw_yaml).splitlines() if prev else []
    new_text = _to_vibium(new.raw_yaml).splitlines()
    out_lines = []
    for line in difflib.unified_diff(prev_text, new_text, lineterm="", n=2):
        if line.startswith("---") or line.startswith("+++"):
            continue
        out_lines.append(line)
    return "\n".join(out_lines) if out_lines else "(no changes)"
