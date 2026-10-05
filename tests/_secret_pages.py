"""Helpers for the vault-fill tests.

Since 0.19.4 a secret fill refuses a document the caller ran `eval` /
`content` in, and `value` / `eval` are refused while a secret sits in a field.
So these tests never build pages with `eval` or read fields with `value`: they
navigate to static fixtures (tests/fixtures/secret_form.html, frame_host.html)
configured by the query string, and read what landed through the page's own
probe button, which writes a JSON snapshot into document.title and #out.
"""
from __future__ import annotations

import json
import uuid
from urllib.parse import urlencode

import pytest

from vibatchium.client import call

SENTINEL = "ORIGIN-BOUND-SENTINEL-42"
IN_FRAME = "#fr >> internal:control=enter-frame >> "
IN_EVIL = "#evil >> internal:control=enter-frame >> "


def port_of(local_server: str) -> str:
    return local_server.rsplit(":", 1)[1]


def page_url(local_server: str, page: str = "secret_form.html",
             host: str = "127.0.0.1", **params) -> str:
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    return f"http://{host}:{port_of(local_server)}/{page}" + (f"?{qs}" if qs else "")


def go_form(local_server: str, host: str = "127.0.0.1", **params) -> str:
    url = page_url(local_server, "secret_form.html", host, **params)
    call("go", {"url": url})
    return url


def go_frame_host(local_server: str, host: str = "127.0.0.1", **params) -> str:
    url = page_url(local_server, "frame_host.html", host, **params)
    call("go", {"url": url})
    return url


def probe(prefix: str = "") -> dict:
    """Click the page's probe button (optionally inside a frame) and return its
    snapshot: {vals, inputs, sec, ts, maskedAtWrite, react, active}."""
    call("click", {"target": f"{prefix}#probe"})
    if prefix:
        return json.loads(call("text", {"target": f"{prefix}#out"})["text"])
    title = call("title", {})["title"]
    assert title.startswith("p:"), title
    return json.loads(title[2:])


@pytest.fixture
def secret_site():
    """Seed vault entries (through the daemon, which resolves them); delete
    every site the test created on teardown."""
    made: list[str] = []

    def make(site=None, **kv):
        site = site or f"origin-test-{uuid.uuid4().hex[:8]}.example"
        for k, v in kv.items():
            call("secret_set", {"site": site, "key": k.replace("_", "-"), "value": v})
        made.append(site)
        return site

    yield make
    for site in made:
        try:
            call("secret_delete", {"site": site})
        except Exception:  # noqa: BLE001
            pass
