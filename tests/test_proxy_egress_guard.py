"""0.19.0 — egress guarantees for the curl_cffi lanes (`fetch`, `search`).

Both defects here were found by an adversarial pre-release review, and both were
things the code CLAIMED in user-facing help text while doing the opposite:

  1. "HTTP(S)_PROXY is not consulted, so an unset --proxy means direct." It was
     consulted. Passing `proxies=None` (or `{}`) leaves libcurl free to read
     HTTP(S)_PROXY / ALL_PROXY out of the environment the daemon was spawned
     with, so a user with HTTPS_PROXY exported egressed through it silently.
  2. "The proxy argument only yields a connection error, not content." For an
     `http://` target it returned the internal service's response body.
"""
from __future__ import annotations

import pytest

from vibatchium.daemon.handlers_extra import _DIRECT_PROXIES


def test_direct_sentinel_is_the_shape_that_actually_suppresses_env_proxies():
    """Empirically, only a mapping with EMPTY values stops libcurl reading the
    environment — None and {} both fall through to it."""
    assert _DIRECT_PROXIES == {"http": "", "https": ""}
    assert _DIRECT_PROXIES, "must be truthy, or callers' `or` fallback re-breaks it"


def test_sentinel_survives_the_or_fallback_callers_use():
    for explicit in (None, {}):
        assert (explicit or _DIRECT_PROXIES) == _DIRECT_PROXIES
    real = {"http": "http://p:8080", "https": "http://p:8080"}
    assert (real or _DIRECT_PROXIES) == real


@pytest.mark.parametrize("host", [
    "127.0.0.1", "localhost", "10.0.0.5", "192.168.1.10",
    "169.254.169.254",          # cloud metadata
])
def test_internal_proxy_hosts_are_recognised(host):
    """The guard is only as good as host_is_internal; pin the cases that matter."""
    from vibatchium.fetch import host_is_internal
    assert host_is_internal(host) is True


def test_public_proxy_host_is_not_flagged():
    from vibatchium.fetch import host_is_internal
    assert host_is_internal("example.com") is False


def test_both_curl_lanes_declare_proxy_redaction():
    """A proxy URL can embed user:pass; neither verb may log it."""
    from vibatchium.daemon.server import _REDACTED_ARG_FIELDS
    assert "proxy" in _REDACTED_ARG_FIELDS["fetch"]
    assert "proxy" in _REDACTED_ARG_FIELDS["search"]
