"""Wave 7.7.9 — semantic-prefix selectors for click / type / fill / etc.

Three consecutive agent runs reached for `@text:Foo` / `@label:Foo` /
`@role:button` syntax that didn't exist — Codex via `@e3` (worked because
it ran `map` first), Codex/Claude on aave with same trial-and-error, and
Nemotron on opencode that burned 6 retries on `click @text:Sign Up` etc.
before falling back to `html | grep` to extract CSS IDs.

These tests pin the new prefix shortcuts and the plain-text auto-fallback
so the pattern Just Works.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from patchium.daemon import elements


# ─── prefix shortcuts ──────────────────────────────────────────────────


def test_resolve_text_prefix_routes_to_get_by_text():
    page = MagicMock()
    elements.resolve_target(page, None, "@text:Sign Up")
    page.get_by_text.assert_called_once_with("Sign Up")
    page.locator.assert_not_called()


def test_resolve_label_prefix_routes_to_get_by_label():
    page = MagicMock()
    elements.resolve_target(page, None, "@label:Email")
    page.get_by_label.assert_called_once_with("Email")


def test_resolve_placeholder_prefix():
    page = MagicMock()
    elements.resolve_target(page, None, "@placeholder:Search...")
    page.get_by_placeholder.assert_called_once_with("Search...")


def test_resolve_testid_prefix():
    page = MagicMock()
    elements.resolve_target(page, None, "@testid:submit-btn")
    page.get_by_test_id.assert_called_once_with("submit-btn")


def test_resolve_title_prefix():
    page = MagicMock()
    elements.resolve_target(page, None, "@title:Tooltip text")
    page.get_by_title.assert_called_once_with("Tooltip text")


def test_resolve_alt_prefix():
    page = MagicMock()
    elements.resolve_target(page, None, "@alt:Hero banner")
    page.get_by_alt_text.assert_called_once_with("Hero banner")


def test_resolve_role_prefix_bare():
    page = MagicMock()
    elements.resolve_target(page, None, "@role:button")
    page.get_by_role.assert_called_once_with("button")


def test_resolve_role_prefix_with_name():
    page = MagicMock()
    elements.resolve_target(page, None, "@role:button[name=Submit]")
    page.get_by_role.assert_called_once_with("button", name="Submit")


def test_resolve_role_prefix_with_quoted_name():
    page = MagicMock()
    elements.resolve_target(page, None, '@role:button[name="Save changes"]')
    page.get_by_role.assert_called_once_with("button", name="Save changes")


def test_resolve_text_prefix_strips_double_quotes():
    page = MagicMock()
    elements.resolve_target(page, None, '@text:"Sign Up"')
    page.get_by_text.assert_called_once_with("Sign Up")


def test_resolve_text_prefix_strips_single_quotes():
    page = MagicMock()
    elements.resolve_target(page, None, "@text:'Sign Up'")
    page.get_by_text.assert_called_once_with("Sign Up")


# ─── plain-text auto-fallback ──────────────────────────────────────────


def test_plain_text_with_space_auto_routes_to_get_by_text():
    """The killer feature: `click "Sign Up"` Just Works without @text: prefix."""
    page = MagicMock()
    elements.resolve_target(page, None, "Sign Up")
    page.get_by_text.assert_called_once_with("Sign Up")


def test_plain_text_with_punctuation_auto_routes():
    page = MagicMock()
    elements.resolve_target(page, None, "Welcome to the site")
    page.get_by_text.assert_called_once_with("Welcome to the site")


def test_single_word_does_not_auto_route():
    """Bare `button` is more likely a CSS tag selector than visible text."""
    page = MagicMock()
    elements.resolve_target(page, None, "button")
    page.locator.assert_called_once_with("button")
    page.get_by_text.assert_not_called()


# ─── raw CSS / Playwright selectors still work (backward compat) ───────


def test_id_selector_stays_raw():
    page = MagicMock()
    elements.resolve_target(page, None, "#new-account-email")
    page.locator.assert_called_once_with("#new-account-email")


def test_class_selector_stays_raw():
    page = MagicMock()
    elements.resolve_target(page, None, ".btn-primary")
    page.locator.assert_called_once_with(".btn-primary")


def test_attribute_selector_stays_raw():
    page = MagicMock()
    elements.resolve_target(page, None, "input[type=email]")
    page.locator.assert_called_once_with("input[type=email]")


def test_descendant_combinator_stays_raw():
    """`form > input.email` has `>` so it's CSS, not auto-text."""
    page = MagicMock()
    elements.resolve_target(page, None, "form > input.email")
    page.locator.assert_called_once_with("form > input.email")


def test_playwright_text_engine_stays_raw():
    """Playwright's native `text=Foo` should pass through, not double-wrap."""
    page = MagicMock()
    elements.resolve_target(page, None, "text=Sign Up")
    page.locator.assert_called_once_with("text=Sign Up")


def test_data_attribute_with_space_in_value_stays_raw():
    """`div[data-test='foo bar']` has `[` → CSS, not auto-text."""
    page = MagicMock()
    elements.resolve_target(page, None, "div[data-test='foo bar']")
    page.locator.assert_called_once_with("div[data-test='foo bar']")


# ─── helper coverage ───────────────────────────────────────────────────


def test_looks_like_plain_text_positive_cases():
    assert elements._looks_like_plain_text("Sign Up")
    assert elements._looks_like_plain_text("Welcome to Aave")
    assert elements._looks_like_plain_text("Create new account")
    # Text with punctuation that's NOT in a CSS pattern shape:
    assert elements._looks_like_plain_text("Mr. Smith")            # `. ` not `.\w`
    assert elements._looks_like_plain_text("More information...")  # trailing dots
    assert elements._looks_like_plain_text("Hello, world!")        # comma in text


def test_looks_like_plain_text_negative_cases():
    assert not elements._looks_like_plain_text("button")            # single word → could be tag
    assert not elements._looks_like_plain_text(".btn")              # leading class sigil
    assert not elements._looks_like_plain_text("#id")               # leading id sigil
    assert not elements._looks_like_plain_text("a > b")             # combinator
    assert not elements._looks_like_plain_text("text=Foo")          # PW selector prefix
    assert not elements._looks_like_plain_text("input[type=text]")  # attr selector
    assert not elements._looks_like_plain_text("h1.title")          # CSS class pattern
    assert not elements._looks_like_plain_text("button.submit")     # CSS class pattern
    assert not elements._looks_like_plain_text("div#main")          # CSS id pattern


def test_strip_quotes_handles_both_quote_styles():
    assert elements._strip_quotes('"hello"') == "hello"
    assert elements._strip_quotes("'hello'") == "hello"
    assert elements._strip_quotes("hello") == "hello"          # no quotes
    assert elements._strip_quotes('"unclosed') == '"unclosed'  # mismatched
    assert elements._strip_quotes("''") == ""                  # empty quotes


# ─── integration: @eN refs still work via the new path ────────────────


def test_eN_ref_still_delegates_to_aria_ref_resolver():
    page = MagicMock()
    snap = MagicMock(spec=elements.Snapshot)
    elements.resolve_target(page, snap, "@e3")
    # resolve() called page.locator("aria-ref=e3")
    page.locator.assert_called_once_with("aria-ref=e3")


def test_bare_eN_ref_form_works():
    page = MagicMock()
    snap = MagicMock(spec=elements.Snapshot)
    elements.resolve_target(page, snap, "e7")
    page.locator.assert_called_once_with("aria-ref=e7")
