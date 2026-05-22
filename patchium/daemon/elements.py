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
