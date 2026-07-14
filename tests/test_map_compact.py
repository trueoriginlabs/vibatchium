"""0.14.1 — tests for the state-preserving compact map renderer.

PURE: elements.compact_lines against REALISTIC patchright mode='ai' output
(state brackets, the `[cursor=pointer]` token Playwright appends AFTER the ref,
parent-node colons, `text:` roles, escaped quotes, phantom `@eN` page text).
LIVE: map_compact preserves state AND keeps links (cursor:pointer) end-to-end.
"""
from __future__ import annotations

from vibatchium.daemon.elements import Snapshot, compact_lines
from vibatchium.client import call


# A snapshot as Playwright ACTUALLY renders it in mode='ai':
#  - [ref=eN] markers (rewritten to @eN by Snapshot.text);
#  - state brackets [checked]/[level=N] BEFORE the ref;
#  - `[cursor=pointer]` appended AFTER the ref for every <a href> / cursor:pointer
#    element (the token that used to make the whole line get dropped);
#  - a parent node ending in `:`; a `text:` role; a name with an embedded '@' and
#    one with embedded '[ ]'; and a literal '@e99' inside page text (no marker).
_YAML = (
    '- button "Sign in" [ref=e5]\n'
    '- checkbox "Remember me" [checked] [ref=e6]\n'
    '- textbox "Email" [ref=e7]\n'
    '- heading "Title" [level=1] [ref=e2]\n'
    '- link [ref=e4]\n'
    '- link "Email me @ home" [ref=e9]\n'
    '- link "Home" [ref=e8] [cursor=pointer]\n'
    '- button "Save [draft]" [ref=e10] [cursor=pointer]\n'
    '- list "Menu" [ref=e3]:\n'
    '  - listitem "Docs" [ref=e11]\n'
    '- text: "footnote" [ref=e12]\n'
    '- text: see comment @e99\n'
    '- text: "no ref, skip me"\n'
)


def _by_ref(yaml=_YAML, **kw):
    return {ref: line for ref, line in compact_lines(Snapshot(url="u", raw_yaml=yaml), **kw)}


# ─── PURE: the state-drop bug is fixed ────────────────────────────────────
def test_compact_preserves_state_brackets():
    d = _by_ref()
    assert d["e6"] == '@e6 checkbox "Remember me" [checked]'   # THE original bug fix
    assert d["e2"] == '@e2 heading "Title" [level=1]'


# ─── PURE: the [cursor=pointer]-after-ref regression (0.14.1 review) ───────
def test_compact_cursor_pointer_element_not_dropped():
    d = _by_ref()
    # links/styled buttons carry `[cursor=pointer]` AFTER the ref; they MUST
    # still appear (with the cursor token stripped from the line).
    assert d["e8"] == '@e8 link "Home"'
    assert "e8" in d and "e10" in d
    assert "cursor" not in d["e8"] and "cursor" not in d["e10"]


def test_compact_bracket_in_name_is_not_read_as_state():
    d = _by_ref()
    # a name containing "[draft]" must stay in the NAME, not become a [state]
    assert d["e10"] == '@e10 button "Save [draft]"'


# ─── PURE: parse edges (parent colon, text: role, @-in-name, escaped quote) ─
def test_compact_parent_colon_and_text_role():
    d = _by_ref()
    assert d["e3"] == '@e3 list "Menu"'          # trailing ':' swallowed
    assert d["e11"] == '@e11 listitem "Docs"'
    assert d["e12"] == '@e12 text "footnote"'    # role colon stripped


def test_compact_ref_parsed_from_end_not_name_at():
    assert _by_ref()["e9"] == '@e9 link "Email me @ home"'


def test_compact_escaped_quote_in_name_not_truncated():
    d = _by_ref('- button "say \\"hi\\"" [disabled] [ref=e13]\n')
    assert d["e13"].startswith('@e13 button "say')
    assert d["e13"].endswith("[disabled]")       # state after an escaped-quote name


def test_compact_plain_and_nameless():
    d = _by_ref()
    assert d["e5"] == '@e5 button "Sign in"'
    assert d["e7"] == '@e7 textbox "Email"'
    assert d["e4"] == '@e4 link'                 # no name → role only


# ─── PURE: phantom refs and reffless lines ────────────────────────────────
def test_compact_phantom_ref_text_dropped():
    # '- text: see comment @e99' has NO [ref=e99] marker → must NOT be emitted
    refs = {r for r, _ in compact_lines(Snapshot(url="u", raw_yaml=_YAML))}
    assert "e99" not in refs


def test_compact_skips_reffless_lines_and_counts():
    pairs = compact_lines(Snapshot(url="u", raw_yaml=_YAML))
    assert "no ref, skip me" not in "\n".join(line for _r, line in pairs)
    # 11 real [ref=eN] markers (e99 phantom + the reffless line excluded)
    assert len(pairs) == 11


def test_compact_interactive_filter_drops_non_controls():
    refs = {r for r, _ in compact_lines(Snapshot(url="u", raw_yaml=_YAML),
                                        interactive_only=True)}
    assert {"e2", "e3", "e11", "e12"}.isdisjoint(refs)   # heading/list/listitem/text out
    assert {"e5", "e6", "e7", "e4", "e8", "e9", "e10"} <= refs


def test_compact_every_line_starts_with_ref():
    pairs = compact_lines(Snapshot(url="u", raw_yaml=_YAML))
    assert all(line.startswith("@e") for _r, line in pairs)


# ─── LIVE: map_compact end-to-end ─────────────────────────────────────────
def test_map_compact_preserves_live_state(local_server):
    call("go", {"url": f"{local_server}/astate.html", "wait_until": "load"})
    m = call("map_compact")
    # the checked checkbox and disabled button retain their state (the fix)
    assert "[checked]" in m["text"]
    assert "[disabled]" in m["text"]
    # the link (cursor:pointer in real Chrome) MUST survive — regression guard
    # for the [cursor=pointer]-after-ref drop the 0.14.1 review caught.
    assert "Home" in m["text"] and "link" in m["text"]
    assert all(ln.startswith("@e") for ln in m["text"].splitlines() if ln.strip())


def test_map_compact_interactive_excludes_heading(local_server):
    call("go", {"url": f"{local_server}/astate.html", "wait_until": "load"})
    m = call("map_compact", {"interactive": True})
    assert "heading" not in m["text"]
    assert "button" in m["text"] and "checkbox" in m["text"] and "link" in m["text"]


def test_map_compact_bbox_appends_coords(local_server):
    call("go", {"url": f"{local_server}/astate.html", "wait_until": "load"})
    m = call("map_compact", {"bbox": True})
    assert "bbox=" in m["text"]
    bline = next(ln for ln in m["text"].splitlines() if "bbox=" in ln)
    coords = bline.split("bbox=")[1].split(",")
    assert len(coords) == 4 and all(c.strip().lstrip("-").isdigit() for c in coords)


def test_map_compact_no_bbox_by_default(local_server):
    call("go", {"url": f"{local_server}/astate.html", "wait_until": "load"})
    m = call("map_compact")
    assert "bbox=" not in m["text"]
