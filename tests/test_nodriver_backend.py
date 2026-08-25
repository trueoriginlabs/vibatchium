"""0.19.0 — nodriver backend CDP endpoint resolution.

The nodriver backend is vibatchium's documented "Hardened tier" escalation, and
it was broken end-to-end: the launcher picked a free port, handed it to
`uc.start(port=...)`, then connected Patchright to THAT port — but nodriver
treats `port` as a hint and binds its own, so every launch died with
`connect_over_cdp ... ECONNREFUSED` about a second in. No test caught it because
nothing exercised the backend; the eval matrix is what surfaced it.

These are pure unit tests over the resolver — no nodriver, no browser.
"""
from __future__ import annotations

from vibatchium.daemon.backends import _nodriver_cdp_url


class _Cfg:
    def __init__(self, host=None, port=None):
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port


class _Browser:
    def __init__(self, config=None):
        if config is not None:
            self.config = config


def test_prefers_the_port_nodriver_actually_bound():
    """The regression itself: nodriver ignored our requested port."""
    b = _Browser(_Cfg(host="127.0.0.1", port=50199))
    assert _nodriver_cdp_url(b, 38167) == "http://127.0.0.1:50199"


def test_falls_back_to_the_requested_port_when_config_has_none():
    assert _nodriver_cdp_url(_Browser(_Cfg(host="127.0.0.1")), 38167) == \
        "http://127.0.0.1:38167"


def test_falls_back_when_the_browser_exposes_no_config_at_all():
    """Older/newer nodriver may not carry `config` — degrade, never crash."""
    assert _nodriver_cdp_url(_Browser(), 38167) == "http://127.0.0.1:38167"


def test_honors_a_non_loopback_host():
    b = _Browser(_Cfg(host="0.0.0.0", port=9222))
    assert _nodriver_cdp_url(b, 1) == "http://0.0.0.0:9222"


def test_missing_host_defaults_to_loopback_not_a_bare_colon():
    assert _nodriver_cdp_url(_Browser(_Cfg(port=9222)), 1) == "http://127.0.0.1:9222"
