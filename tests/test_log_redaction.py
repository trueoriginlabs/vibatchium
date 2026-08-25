"""0.6.11 — per-verb DEBUG-log redaction (server._redact_for_log).

The redactor strips sensitive *request args* before they hit the verb log
(enabled by `set_log_verbs`). The audit found the map keyed on fields that don't
exist in the real payloads — so the "redaction" silently protected nothing. These
tests pin the map to the ACTUAL arg field names.
"""
from __future__ import annotations

from vibatchium.daemon.server import _REDACTED_ARG_FIELDS, _redact_for_log


def test_route_add_redacts_headers_and_body_not_phantom_json():
    """route_add's handler reads args['body'] and args['headers'] (which can
    carry auth tokens). The old map keyed on a nonexistent 'json'."""
    assert _REDACTED_ARG_FIELDS["route_add"] == {"body", "headers"}
    out = _redact_for_log("route_add", {
        "pattern": "**/api", "body": "secret-mock",
        "headers": {"Authorization": "Bearer abc"}})
    assert out["body"] == "<redacted>"
    assert out["headers"] == "<redacted>"
    assert out["pattern"] == "**/api"  # non-sensitive field untouched


def test_vision_type_text_is_redacted():
    """vision_type's handler types args['text'] — same password-leak risk as the
    plain `type`/`fill` verbs, which were already redacted. It wasn't."""
    assert _REDACTED_ARG_FIELDS["vision_type"] == {"text"}
    out = _redact_for_log("vision_type", {"text": "hunter2", "target": "#pw"})
    assert out["text"] == "<redacted>"
    assert out["target"] == "#pw"


def test_secret_init_has_no_phantom_redaction_entry():
    """secret_init carries NO sensitive args (only prefer/force/print_key). The
    generated key (`key_b64`) is response-only, gated behind print_key, and the
    response isn't logged through this arg-only path. The old `{"key"}` entry
    redacted a field that never appears in args — a control that did nothing.
    There must be no entry (so we don't re-introduce dead config)."""
    assert "secret_init" not in _REDACTED_ARG_FIELDS
    # args pass through untouched (nothing sensitive to strip)
    args = {"prefer": "keyring", "force": True, "print_key": True}
    assert _redact_for_log("secret_init", args) == args


def test_existing_text_and_url_redactions_still_hold():
    """Guard the regressions don't touch the entries that already worked."""
    assert _redact_for_log("type", {"text": "pw"})["text"] == "<redacted>"
    assert _redact_for_log("fill", {"text": "pw"})["text"] == "<redacted>"
    assert _redact_for_log("proxy_set", {"url": "http://u:p@h"})["url"] == "<redacted>"


def test_redactor_returns_copy_not_mutating_input():
    """_redact_for_log must not mutate the caller's args dict — and must return
    a COPY even when there's nothing to redact (docstring contract)."""
    original = {"text": "pw", "x": 1}
    out = _redact_for_log("type", original)
    assert original["text"] == "pw"  # input untouched
    assert out["text"] == "<redacted>"
    # always-copy: a verb with no redaction map still returns a fresh dict
    plain = {"a": 1}
    assert _redact_for_log("status", plain) is not plain


def test_url_bearing_verbs_mask_userinfo_only():
    """0.6.11: go/verify_url/wait_url args may embed user:pass@host. We mask the
    userinfo (creds) but keep the host visible for debugging — not whole-field
    nuking. proxy_set stays whole-redacted (query params can also carry secrets)."""
    out = _redact_for_log("go", {"url": "https://user:s3cr3t@example.com/x?a=1"})
    assert "s3cr3t" not in out["url"]
    assert "user" not in out["url"]
    assert "example.com" in out["url"]      # host preserved for debugging
    # verify_url + wait_url likewise
    assert "pw" not in _redact_for_log("verify_url", {"url": "http://u:pw@h/"})["url"]
    assert "pw" not in _redact_for_log("wait_url", {"pattern": "http://u:pw@h/**"})["pattern"]
    # a credential-free URL is left intact (debuggable)
    assert _redact_for_log("go", {"url": "https://example.com/page"})["url"] \
        == "https://example.com/page"
    # proxy_set is still WHOLE-redacted (not userinfo-masked)
    assert _redact_for_log("proxy_set", {"url": "http://u:p@h:8080?token=x"})["url"] \
        == "<redacted>"


def test_search_proxy_is_redacted_but_the_query_survives():
    """0.19.0 — `search` takes the same `proxy` egress override as `fetch`, so
    it carries the same user:pass leak risk and needs its OWN entry (the map is
    keyed per verb; fetch's entry does not cover it). The query itself is not
    sensitive and must stay readable, or the verb log stops being an audit log."""
    assert _REDACTED_ARG_FIELDS["search"] == {"proxy"}
    out = _redact_for_log("search", {
        "query": "playwright stealth", "engine": "auto",
        "proxy": "http://user:pa55@proxy.example:8080"})
    assert out["proxy"] == "<redacted>"
    assert "pa55" not in str(out)
    assert out["query"] == "playwright stealth"
    assert out["engine"] == "auto"


def test_fetch_proxy_is_redacted_alongside_the_auth_bearing_fields():
    """0.19.0 — `fetch` gained a `proxy` egress override, whose URL can embed
    user:pass. It joins the existing auth-bearing fields rather than replacing
    them; the target URL keeps its userinfo-masking treatment separately."""
    assert "proxy" in _REDACTED_ARG_FIELDS["fetch"]
    out = _redact_for_log("fetch", {
        "url": "https://api.example/v1", "method": "GET",
        "proxy": "http://user:pa55@proxy.example:8080",
        "headers": {"Authorization": "Bearer abc"}})
    assert out["proxy"] == "<redacted>"
    assert out["headers"] == "<redacted>"
    assert "pa55" not in str(out)
    assert out["method"] == "GET"
