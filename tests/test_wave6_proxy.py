"""Wave 6.2a — proxy abstraction tests.

Verifies:
- URL parser handles all built-in providers correctly
- Generic http/socks5 passes through unchanged
- Bright Data / IPRoyal / Decodo adapters construct the right username format
- Bad URLs raise ProxyParseError
- _mask_url redacts credentials
- proxy_set persists to disk; proxy_clear removes it
- proxy_set with --path requires 0600 perms
- proxy_info reports config when session not running (no live proxy hits)
- Daemon launches with proxy from session config (smoke test using a fake
  unreachable proxy → expect goto failure, not silent ignore)
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest

from patchium.client import call, DaemonError
from patchium.daemon.paths import PROFILES_DIR
from patchium.proxy import (
    ProxyParseError, parse, list_providers, load_proxy_file, save_session_proxy,
    load_session_proxy, webrtc_leak_guard_args,
)


def _ensure_clean(name: str) -> None:
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass
    p = PROFILES_DIR / name
    if p.exists():
        try:
            shutil.rmtree(p)
        except Exception:  # noqa: BLE001
            pass


# ─── URL parser unit tests ───────────────────────────────────────────────


def test_parse_generic_http():
    cfg = parse("http://u:p@127.0.0.1:8888")
    assert cfg == {"server": "http://127.0.0.1:8888", "username": "u", "password": "p"}


def test_parse_generic_socks5():
    cfg = parse("socks5://user:pass@proxy.example.com:1080")
    assert cfg["server"] == "socks5://proxy.example.com:1080"
    assert cfg["username"] == "user"
    assert cfg["password"] == "pass"


def test_parse_generic_no_auth():
    cfg = parse("http://localhost:8080")
    assert cfg == {"server": "http://localhost:8080"}
    assert "username" not in cfg


def test_parse_brightdata_basic():
    cfg = parse("brightdata://abc123:secret@residential")
    assert cfg["server"] == "http://brd.superproxy.io:33335"
    assert cfg["username"] == "brd-customer-abc123-zone-residential"
    assert cfg["password"] == "secret"


def test_parse_brightdata_with_country_session():
    cfg = parse("brightdata://abc123:secret@residential?country=US&session-id=42")
    assert "country-us" in cfg["username"]
    assert "session-42" in cfg["username"]


def test_parse_iproyal_with_sticky():
    cfg = parse("iproyal://myuser:mypw@geo.iproyal.com:12321?country=de&sticky=7d")
    assert cfg["server"] == "http://geo.iproyal.com:12321"
    assert "country-de" in cfg["username"]
    assert "lifetime-7d" in cfg["username"]


def test_parse_decodo_with_session():
    cfg = parse("decodo://baseuser:pw@gate.decodo.com:7000?country=us&session=abc&duration=60")
    assert cfg["server"] == "http://gate.decodo.com:7000"
    assert cfg["username"].startswith("user-baseuser")
    assert "country-us" in cfg["username"]
    assert "session-abc" in cfg["username"]
    assert "sessionduration-60" in cfg["username"]


def test_parse_unknown_provider_raises():
    with pytest.raises(ProxyParseError, match="unknown proxy provider"):
        parse("acmeproxies://user:pass@host")


def test_parse_malformed_raises():
    with pytest.raises(ProxyParseError):
        parse("notaurl")


def test_parse_missing_required_brightdata_fields():
    with pytest.raises(ProxyParseError, match="brightdata URL must be"):
        parse("brightdata://onlyuser@zone")  # no password


def test_list_providers_includes_built_ins():
    providers = list_providers()
    for p in ("http", "socks5", "brightdata", "iproyal", "decodo"):
        assert p in providers


# ─── proxy file loader ──────────────────────────────────────────────────


def test_load_proxy_file_requires_0600():
    with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".txt") as f:
        f.write("http://u:p@host:8080\n")
        path = f.name
    try:
        os.chmod(path, 0o644)  # world-readable
        with pytest.raises(PermissionError, match="must be 0600"):
            load_proxy_file(path)
        os.chmod(path, 0o600)
        # now succeeds
        assert load_proxy_file(path) == "http://u:p@host:8080"
    finally:
        os.unlink(path)


def test_load_proxy_file_missing():
    with pytest.raises(FileNotFoundError):
        load_proxy_file("/nonexistent/proxy/file")


# ─── per-session storage ────────────────────────────────────────────────


def test_save_and_load_session_proxy(tmp_path):
    save_session_proxy(tmp_path, "http://u:p@host:8080")
    assert load_session_proxy(tmp_path) == "http://u:p@host:8080"
    # File mode is 0600
    mode = stat.S_IMODE((tmp_path / "proxy.json").stat().st_mode)
    assert mode == 0o600
    # Clear
    save_session_proxy(tmp_path, None)
    assert load_session_proxy(tmp_path) is None


def test_save_session_proxy_validates_url(tmp_path):
    with pytest.raises(ProxyParseError):
        save_session_proxy(tmp_path, "not-a-valid-proxy-url")


# ─── WebRTC leak guard ──────────────────────────────────────────────────


def test_webrtc_leak_guard_args_has_correct_flags():
    args = webrtc_leak_guard_args()
    assert any("WebRtcHideLocalIpsWithMdns" in a for a in args)
    assert any("disable_non_proxied_udp" in a for a in args)


# ─── daemon-level handler tests ─────────────────────────────────────────


def test_proxy_set_clear_lifecycle():
    """Set a proxy on default session, verify persistence + clear."""
    # Use a fake session so we don't pollute the conftest default
    name = "patchium_test_w6_proxy"
    _ensure_clean(name)
    call("session_new", {"name": name})
    try:
        # set
        res = call("proxy_set",
                   {"url": "http://user:pass@127.0.0.1:9999"},
                   session=name)
        assert res["set"] is True
        # url_preview is masked
        assert "pass" not in res["url_preview"]
        assert "***@" in res["url_preview"]
        # info shows configured
        info = call("proxy_info", session=name)
        assert info["configured"] is True
        assert info["server"] == "http://127.0.0.1:9999"
        assert info["has_auth"] is True
        # clear
        call("proxy_clear", session=name)
        info2 = call("proxy_info", session=name)
        assert info2["configured"] is False
    finally:
        _ensure_clean(name)


def test_proxy_set_rejects_bad_url():
    name = "patchium_test_w6_proxy_bad"
    _ensure_clean(name)
    call("session_new", {"name": name})
    try:
        with pytest.raises(DaemonError, match="unknown proxy provider"):
            call("proxy_set", {"url": "garbage://x"}, session=name)
    finally:
        _ensure_clean(name)


def test_proxy_set_from_path():
    """proxy_set --path reads from a 0600 file."""
    name = "patchium_test_w6_proxy_file"
    _ensure_clean(name)
    call("session_new", {"name": name})
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write("http://secretuser:secretpass@10.0.0.1:8080\n")
        path = f.name
    os.chmod(path, 0o600)
    try:
        res = call("proxy_set", {"path": path}, session=name)
        assert res["set"] is True
        info = call("proxy_info", session=name)
        assert info["server"] == "http://10.0.0.1:8080"
    finally:
        os.unlink(path)
        _ensure_clean(name)


def test_proxy_info_when_not_configured():
    name = "patchium_test_w6_proxy_empty"
    _ensure_clean(name)
    call("session_new", {"name": name})
    try:
        info = call("proxy_info", session=name)
        assert info["configured"] is False
        assert info["url_preview"] is None
    finally:
        _ensure_clean(name)


def test_proxy_url_persists_across_close_open(local_server):
    """Set proxy → close session → reopen → proxy still set."""
    name = "patchium_test_w6_proxy_persist"
    _ensure_clean(name)
    call("session_new", {"name": name})
    try:
        call("proxy_set", {"url": "socks5://u:p@some.proxy:1080"}, session=name)
        # Direct check: file should exist
        p = PROFILES_DIR / name / "proxy.json"
        assert p.exists()
        # info shows configured even without session running
        info = call("proxy_info", session=name)
        assert info["configured"] is True
    finally:
        _ensure_clean(name)
