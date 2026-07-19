"""A vault secret must not be readable off any screenshot.

Keeping the value out of the response, the log and the cache — which `_fill`
already did — was only half the problem. A secret typed into an ordinary
(non-password) input renders as plain text, and several paths turn the
viewport into bytes that leave the process: the `screenshot` verb, the tiles
lane, explore's fallback shot, live-view frames, and `vision_*`, which POSTs
the PNG to the Anthropic API. TOTP codes, recovery codes and API keys all
routinely go into non-password fields.

PROOF TECHNIQUE (no OCR): fill two DIFFERENT secrets of the SAME LENGTH and
compare screenshot bytes. If the rendering leaks the value the two images
differ; if the value is unrecoverable from the image they are byte-identical.
The control case asserts the unmasked field really does differ, so a test that
passes for the wrong reason (e.g. a blank page) is caught.
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from vibatchium.client import call

FIELD = ('<input id="f" style="font-size:28px;width:420px" '
         'autocomplete="off">')
PW = ('<input id="f" type="password" style="font-size:28px;width:420px">')


def _shot_sha():
    png = base64.b64decode(call("screenshot", {})["png_b64"])
    return hashlib.sha256(png).hexdigest()


def _build(markup):
    call("eval", {"expr": f"document.body.innerHTML = {markup!r}; 1"})


@pytest.fixture
def page(local_server):
    call("go", {"url": f"{local_server}/blank.html"})
    return None


@pytest.fixture
def vault():
    """Two same-length secrets in the REAL vault.

    The value is resolved inside the daemon process, so an in-test
    monkeypatch cannot reach it — the secrets have to genuinely exist. Unique
    site name so a real vault is never stomped; removed on teardown.
    """
    import uuid
    site = f"redaction-test-{uuid.uuid4().hex[:8]}"
    call("secret_set", {"site": site, "key": "a", "value": "AAAAAAAAAAAA"})
    call("secret_set", {"site": site, "key": "b", "value": "BBBBBBBBBBBB"})
    try:
        yield site
    finally:
        for k in ("a", "b"):
            try:
                call("secret_delete", {"site": site, "key": k})
            except Exception:  # noqa: BLE001
                pass


def test_control_unmasked_field_does_leak_the_value(page):
    """Sanity: without masking the two values ARE distinguishable. If this
    ever fails the other assertions prove nothing."""
    _build(FIELD)
    call("eval", {"expr": "document.getElementById('f').value='AAAAAAAAAAAA';1"})
    a = _shot_sha()
    call("eval", {"expr": "document.getElementById('f').value='BBBBBBBBBBBB';1"})
    b = _shot_sha()
    assert a != b, "control failed — screenshots are insensitive to field text"


def test_secret_fill_is_not_recoverable_from_a_screenshot(page, vault):
    _build(FIELD)
    res_a = call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    a = _shot_sha()
    _build(FIELD)
    res_b = call("fill", {"target": "#f", "use_secret": f"{vault}:b"})
    b = _shot_sha()

    assert res_a["render_masked"] == "masked"
    assert res_b["render_masked"] == "masked"
    assert a == b, "two different secrets produced different pixels — the " \
                   "value is still readable off the screenshot"


def test_secret_value_still_reaches_the_dom(page, vault):
    """Masking must change only the rendering — the form still submits."""
    _build(FIELD)
    call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    assert call("eval", {"expr": "document.getElementById('f').value"})["value"] \
        == "AAAAAAAAAAAA"


def test_secret_never_appears_in_the_response(page, vault):
    _build(FIELD)
    res = call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    assert "AAAAAAAAAAAA" not in repr(res)
    assert res["from_secret"] == f"{vault}:a"


def test_password_field_is_reported_as_already_masked(page, vault):
    _build(PW)
    res = call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    assert res["render_masked"] == "password"


def test_masking_survives_a_hostile_stylesheet(page, vault):
    """Set with `important` so a site's own CSS cannot reveal the field."""
    _build(FIELD)
    call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    call("eval", {"expr": "const s=document.createElement('style');"
                          "s.textContent='#f{-webkit-text-security:none!important}';"
                          "document.head.appendChild(s);1"})
    a = _shot_sha()
    _build(FIELD)
    call("fill", {"target": "#f", "use_secret": f"{vault}:b"})
    call("eval", {"expr": "const s=document.createElement('style');"
                          "s.textContent='#f{-webkit-text-security:none!important}';"
                          "document.head.appendChild(s);1"})
    b = _shot_sha()
    assert a == b, "a site stylesheet was able to unmask the secret"


def test_plain_fill_clears_a_previous_mask(page, vault):
    """Explicitly overwriting with caller-supplied plaintext un-masks — the
    caller already knows that value, and a permanently dotted field would be
    baffling."""
    _build(FIELD)
    call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    call("fill", {"target": "#f", "text": "plain-visible"})
    masked = call("eval", {"expr":
                           "getComputedStyle(document.getElementById('f'))"
                           ".webkitTextSecurity"})["value"]
    assert masked in (None, "", "none"), f"still masked: {masked!r}"
