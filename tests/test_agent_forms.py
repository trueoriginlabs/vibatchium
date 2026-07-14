"""0.15.0 agent-forms — live tests for detect_forms + locator disambiguation.

All drive REAL Chrome against tests/fixtures/forms.html (a synthetic fixture that
mirrors real form markup — the lesson from the map_compact regression is that the
live real-Chrome path is what actually validates in-page DOM walks).
"""
from __future__ import annotations

import pytest

from vibatchium.client import call, DaemonError


def _forms(local_server, **kw):
    call("go", {"url": f"{local_server}/forms.html", "wait_until": "load"})
    return call("detect_forms", kw)


def _by_id(res):
    return {f.get("id") or ("formless" if f.get("formless") else f.get("index")): f
            for f in res["forms"]}


def _field(form, name):
    return next(f for f in form["fields"] if f.get("name") == name)


# ─── detect_forms: discovery ──────────────────────────────────────────────
def test_detect_forms_finds_forms_and_formless(local_server):
    res = _forms(local_server)
    forms = _by_id(res)
    assert "login" in forms and "prefs" in forms and "formless" in forms
    assert res["count"] == 3


def test_detect_forms_action_and_method(local_server):
    login = _by_id(_forms(local_server))["login"]
    assert login["action"].endswith("/session")
    assert login["method"] == "post"
    assert login["name"] == "loginForm"


# ─── detect_forms: field shape + ready-to-use locators ────────────────────
def test_detect_forms_locators_and_labels(local_server):
    login = _by_id(_forms(local_server))["login"]
    user = _field(login, "username")
    assert user["locator"] == "#user"          # id wins
    assert user["label"] == "Username"          # from label[for]
    assert user["required"] is True
    pwd = _field(login, "password")
    assert pwd["locator"] == 'input[name="password"]'   # no id → name-based
    assert pwd["label"] == "Password"           # wrapping label


def test_detect_forms_promo_uses_aria_label_and_ref_uses_placeholder(local_server):
    prefs = _by_id(_forms(local_server))["prefs"]
    assert _field(prefs, "promo")["label"] == "Promo code"     # aria-label
    assert _field(prefs, "ref")["label"] == "Referrer"          # placeholder


# ─── detect_forms: CREDENTIAL SAFETY (the divergence from obscura) ────────
def test_detect_forms_redacts_password_and_hidden(local_server):
    login = _by_id(_forms(local_server))["login"]
    pwd, csrf = _field(login, "password"), _field(login, "csrf")
    assert pwd["sensitive"] is True and "value" not in pwd
    assert csrf["sensitive"] is True and "value" not in csrf


def test_detect_forms_redacts_credit_card_by_autocomplete(local_server):
    card = _field(_by_id(_forms(local_server))["prefs"], "cardNumber")
    assert card["sensitive"] is True and "value" not in card


def test_detect_forms_withholds_free_text_value_by_default(local_server):
    user = _field(_by_id(_forms(local_server))["login"], "username")
    assert user["filled"] is True          # signals it has content …
    assert "value" not in user             # … WITHOUT leaking the content


def test_detect_forms_values_opt_in_reveals_nonsensitive(local_server):
    login = _by_id(_forms(local_server, values=True))["login"]
    assert _field(login, "username")["value"] == "alice"
    # a secret stays redacted even under values=true
    assert "value" not in _field(login, "password")


def test_detect_forms_widened_heuristic_redacts_apikey(local_server):
    # review-caught HIGH: the denylist missed api-key-style names; widened. Even with
    # values=true the apiKey field must be flagged sensitive and withhold its value.
    api = _field(_by_id(_forms(local_server, values=True))["prefs"], "apiKey")
    assert api["sensitive"] is True and "value" not in api


def test_detect_forms_sensitive_named_select_keeps_options(local_server):
    # review-caught LOW (#13): a sensitive-NAMED select is UI state, not a typed
    # secret — its options must still be emitted (only typed values get dropped).
    sel = _field(_by_id(_forms(local_server))["prefs"], "cardType")
    assert sel["sensitive"] is True
    assert {o["value"] for o in sel["options"]} == {"visa", "mc"}


def test_detect_forms_html5_form_attr_association_collected(local_server):
    # review-caught LOW (#6): a control associated by form= but placed OUTSIDE the
    # <form> must be collected (fm.elements), not silently dropped.
    prefs = _by_id(_forms(local_server))["prefs"]
    coupon = _field(prefs, "coupon")
    assert coupon["locator"] == 'input[name="coupon"]'


def test_detect_forms_placeholder_only_field_uses_placeholder_locator(local_server):
    # review-caught MEDIUM (#2): a placeholder/title label must emit @placeholder:/
    # @title:, NOT @label: (get_by_label can't match a placeholder).
    formless = _by_id(_forms(local_server))["formless"]
    ph = next(f for f in formless["fields"] if f.get("label") == "Search products")
    assert ph["locator"] == "@placeholder:Search products"


# ─── detect_forms: UI state (always safe) ─────────────────────────────────
def test_detect_forms_select_options_with_selected(local_server):
    country = _field(_by_id(_forms(local_server))["prefs"], "country")
    opts = {o["value"]: o for o in country["options"]}
    assert opts["au"]["selected"] is True
    assert opts["us"]["selected"] is False
    assert opts["au"]["label"] == "Australia"


def test_detect_forms_checkbox_and_radio_state(local_server):
    forms = _by_id(_forms(local_server))
    assert _field(forms["login"], "remember")["checked"] is True
    plans = [f for f in forms["prefs"]["fields"] if f.get("name") == "plan"]
    checked = {p["value"]: p["checked"] for p in plans}
    assert checked == {"free": True, "pro": False}


def test_detect_forms_submit_locator_is_text_based(local_server):
    submit = _by_id(_forms(local_server))["login"]["submit"]
    assert submit["label"] == "Sign in"
    assert submit["locator"] == "@text:Sign in"     # button → get_by_text, not @label


def test_detect_forms_formless_group_holds_loose_control(local_server):
    formless = _by_id(_forms(local_server))["formless"]
    assert formless["formless"] is True
    q = formless["fields"][0]
    assert q["locator"] == "#q" and q["label"] == "Search"


def test_detect_forms_target_scopes_to_subtree(local_server):
    call("go", {"url": f"{local_server}/forms.html", "wait_until": "load"})
    res = call("detect_forms", {"target": "#login"})
    # scoped to the login subtree: only that one form, no prefs/formless
    ids = {f.get("id") for f in res["forms"]}
    assert ids == {"login"}


# ─── locator disambiguation: candidates + index ───────────────────────────
def test_candidates_lists_every_match(local_server):
    call("go", {"url": f"{local_server}/forms.html", "wait_until": "load"})
    res = call("candidates", {"target": ".del"})
    assert res["count"] == 3
    assert [c["index"] for c in res["candidates"]] == [0, 1, 2]
    assert all(c["text"] == "Delete" for c in res["candidates"])
    assert all("bbox" in c for c in res["candidates"])    # real geometry


def test_candidates_resolves_all_three_ref_spellings(local_server):
    # review2 NIT: the [ref=eN] bracket spelling must resolve via aria-ref like @eN /
    # eN (not a CSS [ref=…] attribute match), so candidates and click --index agree
    # on every spelling. Pre-fix, [ref=eN] fell through to a CSS selector → count 0.
    call("go", {"url": f"{local_server}/forms.html", "wait_until": "load"})
    ref = call("map_compact", {"interactive": True})["text"].splitlines()[0].split()[0]
    bare = ref.lstrip("@")                              # e.g. "e5"
    assert call("candidates", {"target": ref})["count"] == 1
    assert call("candidates", {"target": bare})["count"] == 1
    assert call("candidates", {"target": f"[ref={bare}]"})["count"] == 1


def test_click_ambiguous_without_index_hints_at_candidates(local_server):
    call("go", {"url": f"{local_server}/forms.html", "wait_until": "load"})
    with pytest.raises(DaemonError) as ei:
        call("click", {"target": ".del", "timeout_ms": 3000})
    msg = str(ei.value).lower()
    assert "candidates" in msg and "index" in msg    # actionable hint, not a bare trace


def test_click_index_acts_on_the_nth_match(local_server):
    call("go", {"url": f"{local_server}/forms.html", "wait_until": "load"})
    call("click", {"target": ".del", "index": 1})
    # the 2nd Delete button's onclick sets the title to its data-i
    assert call("title")["title"] == "del-1"


def test_fill_index_targets_the_nth_match(local_server):
    call("go", {"url": f"{local_server}/forms.html", "wait_until": "load"})
    # #prefs [type=text] matches promo(0), ref(1), cardNumber(2), apiKey(3) in order.
    call("fill", {"target": "#prefs input[type=text]", "index": 1, "text": "REFERRED"})
    assert call("value", {"target": "#prefs [name=ref]"})["value"] == "REFERRED"


def test_click_index_out_of_range_errors_clearly(local_server):
    # review-caught LOW (#12): an out-of-range index must give an actionable error,
    # not a silent full-timeout hang.
    call("go", {"url": f"{local_server}/forms.html", "wait_until": "load"})
    with pytest.raises(DaemonError) as ei:
        call("click", {"target": ".del", "index": 9})
    msg = str(ei.value).lower()
    assert "out of range" in msg and "candidates" in msg
