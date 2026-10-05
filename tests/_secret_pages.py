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


def _probe_out(prefix: str) -> str:
    if prefix:
        return call("text", {"target": f"{prefix}#out"})["text"]
    return call("title", {})["title"]


def probe(prefix: str = "") -> dict:
    """Click the page's probe button (optionally inside a frame) and return its
    snapshot: {vals, inputs, sec, ts, maskedAtWrite, react, active}.

    Waits for the page's ready marker, then confirms the click actually ran the
    handler (the output changed) and re-clicks if not. A click into a
    cross-origin (out-of-process) iframe can miss when it lands while the frame
    is still settling after being scrolled into view — the handler never runs
    and #out stays empty or stale. The snapshot carries a run counter, so a
    changed output is proof of a fresh run."""
    import time
    call("wait_selector", {"selector": f"{prefix}html[data-ready]", "state": "attached",
                           "timeout_ms": 10_000})
    before = _probe_out(prefix)
    out = before
    for _attempt in range(4):
        call("click", {"target": f"{prefix}#probe"})
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            out = _probe_out(prefix)
            if out != before and (out.startswith("p:") or (prefix and out.strip())):
                break
            time.sleep(0.05)
        else:
            continue
        break
    if prefix:
        return json.loads(out)
    assert out.startswith("p:"), out
    return json.loads(out[2:])


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
