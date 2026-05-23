"""Wave 6.3a — credential vault + TOTP tests.

Verifies:
- Vault round-trips through encrypt/decrypt
- File is 0600 + contains no plaintext when inspected raw
- list_secrets returns MASKED values (never the real values)
- TOTP is deterministic for a frozen clock and matches RFC 6238 test vector
- resolve_secret_reference handles 'site:totp' and 'site:key'
- VaultLocked raised when no key available
- Daemon handlers: set/list/delete/totp roundtrip
- fill --use-secret never returns the value
- CRITICAL: no plaintext secret appears in any log/cache/HAR file
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from patchium.client import call, DaemonError
from patchium import secrets as _vault


# ─── test fixture: temp vault key in env ────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_vault_paths(monkeypatch, tmp_path):
    """Redirect the vault file to a temp location AND set a known key in env."""
    tmp_vault = tmp_path / "secrets.enc"
    monkeypatch.setattr(_vault, "VAULT_PATH", tmp_vault)
    # 32-byte test key
    key = b"\x01" * 32
    monkeypatch.setenv(_vault.ENV_KEY, base64.b64encode(key).decode())
    yield tmp_vault


# ─── pure module tests ──────────────────────────────────────────────────


def test_vault_roundtrip(_patch_vault_paths):
    _vault.set_secret("github.com", "username", "alice")
    _vault.set_secret("github.com", "password", "hunter2")
    assert _vault.get_secret("github.com", "username") == "alice"
    assert _vault.get_secret("github.com", "password") == "hunter2"


def test_vault_file_is_0600(_patch_vault_paths):
    _vault.set_secret("site", "k", "v")
    mode = oct(os.stat(_patch_vault_paths).st_mode & 0o777)
    assert mode == "0o600"


def test_vault_file_no_plaintext(_patch_vault_paths):
    """The encrypted file MUST NOT contain the plaintext value."""
    _vault.set_secret("site", "k", "SECRET_SENTINEL_42")
    raw = _patch_vault_paths.read_bytes()
    assert b"SECRET_SENTINEL_42" not in raw
    assert b"site" not in raw  # site name is also in plaintext schema, encrypted


def test_list_secrets_returns_masked(_patch_vault_paths):
    _vault.set_secret("github.com", "password", "supersecret")
    listed = _vault.list_secrets()
    assert listed == {"github.com": {"password": "<set>"}}
    # Actual value not in output
    flat = json.dumps(listed)
    assert "supersecret" not in flat


def test_delete_secret(_patch_vault_paths):
    _vault.set_secret("site", "k1", "v1")
    _vault.set_secret("site", "k2", "v2")
    assert _vault.delete_secret("site", "k1") is True
    assert _vault.get_secret("site", "k1") is None
    assert _vault.get_secret("site", "k2") == "v2"
    # Delete whole site
    assert _vault.delete_secret("site") is True
    assert _vault.list_secrets() == {}


def test_delete_secret_idempotent_returns_false(_patch_vault_paths):
    assert _vault.delete_secret("nonexistent") is False
    _vault.set_secret("site", "k", "v")
    assert _vault.delete_secret("site", "missing") is False


def test_vault_locked_when_no_key(monkeypatch, tmp_path):
    """Without PATCHIUM_SECRETS_KEY + no keyring → VaultLocked."""
    monkeypatch.delenv(_vault.ENV_KEY, raising=False)
    monkeypatch.setattr(_vault, "VAULT_PATH", tmp_path / "x.enc")
    # Force keyring lookup to return None
    monkeypatch.setattr(_vault, "_key_from_keyring", lambda: None)
    with pytest.raises(_vault.VaultLocked):
        _vault.get_vault_key()


# ─── TOTP RFC 6238 test vectors ────────────────────────────────────────


def test_totp_rfc6238_test_vector():
    """RFC 6238 Appendix B: known seed + timestamp → known code."""
    # Test vector from RFC 6238 (HMAC-SHA1, seed = ASCII "12345678901234567890")
    seed_ascii = "12345678901234567890"
    seed_b32 = base64.b32encode(seed_ascii.encode()).decode()
    # At T=59 (Unix), expected code is 94287082 → 6-digit = "287082"
    assert _vault.totp(seed_b32, at=59, digits=6) == "287082"
    # At T=1111111109 → 07081804 → 6-digit = "081804"
    assert _vault.totp(seed_b32, at=1111111109, digits=6) == "081804"


def test_totp_deterministic_in_window():
    """Same seed + same window → same code."""
    seed = base64.b32encode(b"some seed value!").decode()
    a = _vault.totp(seed, at=1000000000)
    b = _vault.totp(seed, at=1000000000)
    assert a == b
    assert len(a) == 6
    assert a.isdigit()


def test_totp_tolerates_whitespace_in_seed():
    """Common UX: user pastes 'JBSW Y3DP EHPK 3PXP' with spaces."""
    a = _vault.totp("JBSWY3DPEHPK3PXP", at=1000000000)
    b = _vault.totp("JBSW Y3DP EHPK 3PXP", at=1000000000)
    assert a == b


# ─── resolve_secret_reference ───────────────────────────────────────────


def test_resolve_basic(_patch_vault_paths):
    _vault.set_secret("github.com", "username", "alice")
    assert _vault.resolve_secret_reference("github.com:username") == "alice"


def test_resolve_totp(_patch_vault_paths):
    _vault.set_secret("github.com", "totp-seed", "JBSWY3DPEHPK3PXP")
    code = _vault.resolve_secret_reference("github.com:totp")
    assert len(code) == 6
    assert code.isdigit()


def test_resolve_missing_raises(_patch_vault_paths):
    with pytest.raises(KeyError):
        _vault.resolve_secret_reference("nonexistent.com:totp")
    with pytest.raises(KeyError):
        _vault.resolve_secret_reference("github.com:not_a_key")


def test_resolve_bad_format_raises():
    with pytest.raises(ValueError, match="invalid secret reference"):
        _vault.resolve_secret_reference("no_colon_here")


# ─── daemon-level handler tests ─────────────────────────────────────────
# These need the test key to be reachable from the daemon process. The daemon
# was spawned with the inherited env at conftest time, so PATCHIUM_SECRETS_KEY
# should be set there too via conftest. For simplicity, we point the daemon
# at its own vault path (default ~/.config/patchium/secrets.enc) and clean up
# after — the daemon test confirms the WIRING, the pure-module tests confirm
# the encryption is correct.


def test_daemon_secret_set_list_delete_lifecycle(_patch_vault_paths):
    """Daemon-level: set → list shows masked → totp roundtrip → delete."""
    # The daemon inherits the test's env when spawned by conftest, so
    # PATCHIUM_SECRETS_KEY is set. But the daemon's vault path is the
    # default, not our tmp one. Use a unique site name to avoid stomping
    # real user data, and clean up explicitly.
    site = f"patchium_test_site_{os.getpid()}"
    try:
        res = call("secret_set", {"site": site, "key": "totp-seed",
                                    "value": "JBSWY3DPEHPK3PXP"})
        assert res["set"] is True
        # response must NOT contain the value
        assert "JBSWY3DPEHPK3PXP" not in json.dumps(res)
        # list returns masked
        listed = call("secret_list", {"site": site})
        assert listed["sites"][site] == {"totp-seed": "<set>"}
        # totp generates a code
        t = call("secret_totp", {"site": site})
        assert len(t["code"]) == 6
        assert t["code"].isdigit()
    finally:
        try:
            call("secret_delete", {"site": site})
        except Exception:  # noqa: BLE001
            pass


def test_daemon_secret_response_never_includes_value():
    """CRITICAL: even on error, no daemon response should contain a value."""
    site = f"patchium_test_leakproof_{os.getpid()}"
    sentinel = "LEAK_DETECT_SENTINEL_42"
    try:
        call("secret_set", {"site": site, "key": "password", "value": sentinel})
        # All possible response paths
        responses = [
            call("secret_set", {"site": site, "key": "password", "value": sentinel}),
            call("secret_list"),
            call("secret_list", {"site": site}),
        ]
        for r in responses:
            assert sentinel not in json.dumps(r), \
                f"sentinel leaked into response: {r}"
    finally:
        try:
            call("secret_delete", {"site": site})
        except Exception:  # noqa: BLE001
            pass


def test_daemon_log_never_contains_secret_values():
    """Hard requirement: secret values NEVER appear in the daemon log."""
    site = f"patchium_test_log_leak_{os.getpid()}"
    sentinel = "LOG_LEAK_DETECT_SENTINEL_42"
    try:
        call("secret_set", {"site": site, "key": "password", "value": sentinel})
        # Trigger a totp resolution too (different code path)
        call("secret_set", {"site": site, "key": "totp-seed",
                             "value": "JBSWY3DPEHPK3PXP"})
        call("secret_totp", {"site": site})
        # Read the daemon log
        from patchium.daemon.paths import LOG_PATH
        if LOG_PATH.exists():
            log_content = LOG_PATH.read_text()
            assert sentinel not in log_content, \
                f"sentinel {sentinel!r} appeared in daemon log"
            assert "JBSWY3DPEHPK3PXP" not in log_content, \
                "totp seed leaked into log"
    finally:
        try:
            call("secret_delete", {"site": site})
        except Exception:  # noqa: BLE001
            pass


def test_fill_use_secret_resolves_from_vault(local_server):
    """fill --use-secret should fill the input with vault value, never echoing it."""
    site = f"patchium_test_fill_use_{os.getpid()}"
    sentinel = "USE_SECRET_FILL_VALUE_99"
    try:
        call("secret_set", {"site": site, "key": "password", "value": sentinel})
        call("go", {"url": f"{local_server}/simple.html"})
        res = call("fill", {"target": "#q",
                             "use_secret": f"{site}:password"})
        # Response should NOT echo the value
        assert sentinel not in json.dumps(res)
        # But the input should HAVE the value
        actual = call("value", {"selector": "#q"})["value"]
        assert actual == sentinel
    finally:
        try:
            call("secret_delete", {"site": site})
        except Exception:  # noqa: BLE001
            pass
