"""Ambient behaviour — live tests against the per-run test daemon + real Chrome.

1. The in-page safe-target filter (ambient.CHECK_JS) on the oracle's trap page:
   links, buttons, form controls, an onmouseover menu, nav and the exit-intent
   top edge are refused; plain text is allowed; a wheel over an inner scroller is
   refused.
2. Between two verbs, with ambient ON the page records pointer moves; with it
   OFF it records none — and in neither case a click / keydown / input / focus /
   selection / interactive hover / inner scroll. Run through the same oracle lane
   `vb oracle ambient` uses.
"""
from __future__ import annotations

import uuid

import pytest

from vibatchium import oracle
from vibatchium.client import call
from vibatchium.daemon import ambient


@pytest.fixture
def amb_session():
    name = f"amb_live_{uuid.uuid4().hex[:8]}"
    call("start", {"ephemeral": True, "headless": True}, session=name)
    try:
        yield name
    finally:
        for verb in ("session_close", "session_delete"):
            try:
                call(verb, {"name": name})
            except Exception:  # noqa: BLE001
                pass


def _check_expr() -> str:
    """CHECK_JS evaluated at the centre of named fixture elements."""
    return (
        "(() => { const f = " + ambient.CHECK_JS + ";\n"
        "const c = (id) => { const el = document.getElementById(id);"
        " el.scrollIntoView({block: 'center'}); const b = el"
        ".getBoundingClientRect(); return [Math.round(b.x + b.width / 2),"
        " Math.round(b.y + b.height / 2)]; };\n"
        "const p = document.querySelector('main p'); const pb = p.getBoundingClientRect();\n"
        "const text = [Math.round(pb.x + 20), Math.round(pb.y + pb.height / 2)];\n"
        "const out = {};\n"
        "for (const id of ['vbo-link', 'vbo-act', 'vbo-input', 'vbo-select', 'vbo-menu',"
        " 'vbo-tip', 'vbo-card'])"
        " out[id] = f({pts: [c(id)]});\n"
        "out.inner_move = f({pts: [c('vbo-inner')]});\n"
        "out.inner_wheel = f({pts: [], wheel: c('vbo-inner')});\n"
        "out.leave_button = f({pts: [c('vbo-act'), [c('vbo-act')[0], "
        "c('vbo-act')[1] - 40]], from: c('vbo-act')});\n"
        "scrollTo(0, 0);\n"
        "out.text = f({pts: [text]});\n"
        "out.top_edge = f({pts: [[text[0], 10]]});\n"
        "out.text_wheel = f({pts: [], wheel: text});\n"
        "return out; })()"
    )


def test_safe_target_filter_on_the_trap_page(amb_session):
    s = amb_session
    call("go", {"url": "about:blank"}, session=s)
    call("eval", {"expr": oracle._AMBIENT_PAGE_JS}, session=s)
    v = call("eval", {"expr": _check_expr()}, session=s)["value"]
    for trap, why in (("vbo-link", "tag:a"), ("vbo-act", "tag:button"),
                      ("vbo-input", "tag:input"), ("vbo-select", "tag:select"),
                      ("vbo-tip", "attr:onmouseenter"), ("vbo-card", "cursor"),
                      ("vbo-menu", "edge")):    # sticky nav sits in the top band
        assert v[trap]["ok"] is False, (trap, v[trap])
        assert v[trap]["reason"] == why, (trap, v[trap])
    assert v["text"] == {"ok": True}
    assert v["top_edge"]["ok"] is False and v["top_edge"]["reason"] == "edge"
    assert v["inner_move"] == {"ok": True}                    # moving over it is fine
    assert v["inner_wheel"]["ok"] is False
    assert v["inner_wheel"]["reason"].startswith("inner-scroller")
    assert v["text_wheel"] == {"ok": True}
    # resting on the clicked button, leaving it onto text is allowed …
    assert v["leave_button"] == {"ok": True}


def test_mouse_moves_between_verbs_only_with_ambient_on(amb_session):
    rows = oracle.run_ambient_oracle(call, idle_s=5.0, gaps=2, seed=20261006,
                                     session=amb_session)
    by = {r["ambient"]: r["features"] for r in rows}
    off, on = by[False], by[True]
    # OFF: a dead-still pointer between the two verbs (the agentic tell)
    assert off["idle_pointer_per_gap"] == 0 and off["zero_mouse_gap_frac"] == 1
    # ON: pointer samples in the gaps, continuing from where the click left it
    assert on["idle_pointer_per_gap"] >= 1, on
    assert on["entry_jump_px"] is not None and on["entry_jump_px"] <= 60, on
    assert on["idle_integer_coord_frac"] == 1
    # and never anything that could change page state
    for rec in (off, on):
        for k in ("idle_clicks", "idle_keys", "idle_inputs", "idle_focus_changes",
                  "idle_selections", "idle_interactive_hovers", "idle_inner_scrolls"):
            assert rec[k] == 0, (k, rec)
    st = next(r for r in rows if r["ambient"])["ambient_status"]
    assert st["ambient"] and st["stats"]["moves"] >= 1
    assert call("humanize_ambient", {"mode": "status"}, session=amb_session)["ambient"] is False
