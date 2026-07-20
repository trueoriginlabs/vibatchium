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


def test_secret_is_redacted_from_the_accessibility_map(page, vault):
    """The pixel mask hides the value from SCREENSHOTS, but map/diff_map return the
    accessibility snapshot, which renders a filled field's value inline — so a masked
    secret would still egress in cleartext in the tool response (forwarded to the
    model). The value must be stripped from the snapshot + diff text."""
    _build(FIELD)
    call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    m = call("map", {})["text"]
    assert "AAAAAAAAAAAA" not in m, "secret leaked in the map (accessibility) response"
    _build(FIELD)
    call("fill", {"target": "#f", "use_secret": f"{vault}:b"})
    dm = call("diff_map", {})["text"]
    assert "BBBBBBBBBBBB" not in dm, "secret leaked in the diff_map response"


def test_secret_survives_a_show_password_toggle(page, vault):
    """A show-password 'eye' flips type=password -> text; native dots vanish. The disc
    mask must be applied to password fields too (unconditionally) so the value stays
    hidden after the flip. Two different secrets -> identical pixels iff still masked."""
    _build(PW)
    call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    call("eval", {"expr": "document.getElementById('f').type='text';1"})
    a = _shot_sha()
    _build(PW)
    call("fill", {"target": "#f", "use_secret": f"{vault}:b"})
    call("eval", {"expr": "document.getElementById('f').type='text';1"})
    b = _shot_sha()
    assert a == b, "show-password toggle revealed the secret — the mask was not " \
                   "applied to the password field"


def test_vault_path_is_isolated_from_the_real_vault():
    """Guard: the suite must never resolve secrets.VAULT_PATH to the real
    ~/.config/vibatchium/secrets.enc — the fixed test key would re-key it and destroy
    every real entry. The conftest module-top isolation must have frozen it to a temp
    file at import (this is a pure assertion — no daemon needed)."""
    import tempfile
    from vibatchium import secrets
    vp = str(secrets.VAULT_PATH)
    assert "vbtest-vault-" in vp or vp.startswith(tempfile.gettempdir()), vp
    assert not vp.endswith(".config/vibatchium/secrets.enc"), vp


def test_secret_is_masked_before_its_value_is_written(page, vault):
    """0.18.6: the disc mask is applied to the EMPTY field FIRST, then the value
    is written — so a concurrent screenshot / 5fps live-view frame can never
    catch the plaintext in a gap (the old order filled, THEN masked a CDP
    round-trip later). An in-page input listener records — synchronously, at the
    moment the value is first written — whether the mask attribute is already
    present. 'yes' proves mask-first; 'no' would be the plaintext window."""
    _build(FIELD)
    call("eval", {"expr": (
        "const el=document.getElementById('f');"
        "el.addEventListener('input',function(){"
        " if(el.value && !el.hasAttribute('data-vb-write-seen')){"
        "  el.setAttribute('data-vb-write-seen','1');"
        "  el.setAttribute('data-vb-masked-at-write',"
        "   el.hasAttribute('data-vb-secret')?'yes':'no');}});1")})
    call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    at_write = call("eval", {"expr":
                    "document.getElementById('f')"
                    ".getAttribute('data-vb-masked-at-write')"})["value"]
    assert at_write == "yes", (
        "secret value was written before the mask was applied — plaintext "
        f"window open to screenshots/live-view (recorded={at_write!r})")


def test_secret_fill_leaves_the_field_masked_and_submittable(page, vault):
    """End state after a use_secret fill: the value reached the DOM (form still
    submits) but the field renders masked and is tagged for snapshot redaction —
    and the re-assert never reports a soft-failure (it fails closed instead)."""
    import json
    _build(FIELD)
    res = call("fill", {"target": "#f", "use_secret": f"{vault}:a"})
    assert res["render_masked"] in ("masked", "password")
    st = json.loads(call("eval", {"expr":
        "JSON.stringify({v:document.getElementById('f').value,"
        "sec:document.getElementById('f').hasAttribute('data-vb-secret'),"
        "ts:getComputedStyle(document.getElementById('f')).webkitTextSecurity})"
        })["value"])
    assert st["v"] == "AAAAAAAAAAAA"     # value intact — form still submits
    assert st["sec"] is True             # tagged for AX-snapshot redaction
    assert st["ts"] == "disc"            # rendered masked


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
