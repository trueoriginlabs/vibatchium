"""0.7.0 exclusive session leases.

PURE tests cover lease.py + the SessionEntry lease helpers. LIVE tests drive a
real daemon (conftest fixture) and clean up every lease/session in finally.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from vibatchium.client import call, DaemonError
from vibatchium.daemon import lease as L
from vibatchium.daemon.registry import SessionEntry


@pytest.fixture(autouse=True)
def _no_lease_env(monkeypatch):
    # Make sure a stray VIBATCHIUM_LEASE from another test never auto-presents.
    monkeypatch.delenv("VIBATCHIUM_LEASE", raising=False)


# ─── PURE: lease.py ──────────────────────────────────────────────────────
def test_clamp_ttl_floors_and_caps():
    assert L.clamp_ttl(0) == L.LEASE_MIN_TTL_S
    assert L.clamp_ttl(10) == 10
    assert L.clamp_ttl(10**9) == L.LEASE_MAX_TTL_S
    assert L.clamp_ttl("garbage") == L.LEASE_DEFAULT_TTL_S
    assert L.clamp_ttl(None) == L.LEASE_DEFAULT_TTL_S


def test_mint_token_unique():
    toks = {L.mint_token() for _ in range(1000)}
    assert len(toks) == 1000
    assert all(t for t in toks)


def test_is_expired_boundary():
    lease = {"expires_at": 100.0}
    assert L.is_expired(lease, now=100.0) is True   # >= boundary
    assert L.is_expired(lease, now=99.999) is False


def test_holder_token_from_args_ignores_env(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_LEASE", "ENV_SHOULD_BE_IGNORED")
    assert L.holder_token_from_args({}) is None
    assert L.holder_token_from_args({"_lease": "X"}) == "X"


def test_check_access_matrix():
    now = 1000.0
    lease = {"owner": "botA", "token": "TKN", "expires_at": now + 50,
             "acquired_at": now}
    assert L.check_access(None, None, "s") == (True, None)          # unleased
    assert L.check_access(lease, "TKN", "s", now) == (True, None)   # holder
    ok, reason = L.check_access(lease, "WRONG", "s", now)           # wrong tok
    assert ok is False and "botA" in reason
    ok, reason = L.check_access(lease, None, "s", now)              # no tok
    assert ok is False


def test_lease_public_strips_token():
    assert L.lease_public(None) is None
    now = 1000.0
    pub = L.lease_public({"owner": "o", "token": "SECRET",
                          "expires_at": now + 30, "acquired_at": now}, now=now)
    assert "token" not in pub
    assert pub["owner"] == "o"
    assert pub["expires_in_s"] == 30
    assert "acquired_at" in pub


def test_entry_lease_lazy_reap():
    e = SessionEntry(name="x", profile_dir=Path("/tmp/x"), session=None)
    e.lease_grant("bot", 100)
    assert e.lease_active() is not None
    e.lease["expires_at"] = time.time() - 1      # force expiry
    assert e.lease_active() is None
    assert e.lease is None                        # reaped


def test_entry_renew_with_token_keeps_it_steal_rotates():
    e = SessionEntry(name="x", profile_dir=Path("/tmp/x"), session=None)
    g1 = e.lease_grant("bot", 100)
    # holder renewal (presents the active token) keeps the token + acquired_at
    g2 = e.lease_grant("bot", 200, presented=g1["token"])
    assert g2["token"] == g1["token"]
    assert g2["acquired_at"] == g1["acquired_at"]
    assert g2["expires_at"] > g1["expires_at"]
    # a steal (active lease, non-matching token) ROTATES the token + resets
    # acquired_at — the prior holder is revoked and never learns the new secret
    g3 = e.lease_grant("other", 100, presented="not-the-token")
    assert g3["token"] != g1["token"]
    assert g3["owner"] == "other"


# ─── LIVE helpers ────────────────────────────────────────────────────────
def _fresh(name):
    _close(name)
    call("start", {"headless": True, "ephemeral": True}, session=name)


def _close(name):
    try:
        call("session_release", {"name": name, "force": True})
    except DaemonError:
        pass
    try:
        call("session_close", {"name": name})
    except DaemonError:
        pass


# ─── LIVE: lease semantics ───────────────────────────────────────────────
def test_lease_grant_returns_token():
    name = "vbtest-lease-grant"
    try:
        _fresh(name)
        r = call("session_lease", {"name": name, "ttl_s": 60, "owner": "botA"})
        assert r["token"] and r["owner"] == "botA" and r["session"] == name
        assert r["expires_in_s"] > 0
    finally:
        _close(name)


def test_nonholder_session_verb_denied():
    name = "vbtest-lease-deny"
    try:
        _fresh(name)
        r = call("session_lease", {"name": name, "owner": "botA", "ttl_s": 60})
        tok = r["token"]
        with pytest.raises(DaemonError, match="busy"):
            call("url", session=name)                 # no token → busy
        out = call("url", session=name, lease=tok)     # holder token → ok
        assert isinstance(out, dict)
    finally:
        _close(name)


def test_ttl_expiry_reopens():
    name = "vbtest-lease-ttl"
    try:
        _fresh(name)
        call("session_lease", {"name": name, "owner": "botA", "ttl_s": 1})
        with pytest.raises(DaemonError, match="busy"):
            call("url", session=name)
        time.sleep(1.3)
        assert isinstance(call("url", session=name), dict)   # expired → open
        assert call("session_lease_info", {"name": name})["leased"] is False
    finally:
        _close(name)


def test_release_by_holder_then_open():
    name = "vbtest-lease-release"
    try:
        _fresh(name)
        tok = call("session_lease", {"name": name, "owner": "botA"})["token"]
        res = call("session_release", {"name": name}, lease=tok)
        assert res["released"] is True
        assert isinstance(call("url", session=name), dict)
        assert call("session_lease_info", {"name": name})["leased"] is False
    finally:
        _close(name)


def test_release_wrong_token_refused_force_breaks():
    name = "vbtest-lease-force"
    try:
        _fresh(name)
        call("session_lease", {"name": name, "owner": "botA"})
        with pytest.raises(DaemonError):
            call("session_release", {"name": name}, lease="WRONG-TOKEN")
        res = call("session_release", {"name": name, "force": True})
        assert res["released"] is True
    finally:
        _close(name)


def test_disruptive_registry_verb_gated():
    name = "vbtest-lease-gated"
    try:
        _fresh(name)
        call("session_lease", {"name": name, "owner": "botA"})
        with pytest.raises(DaemonError, match="busy"):
            call("stop", session=name)                    # guarded registry verb
        with pytest.raises(DaemonError, match="busy"):
            call("session_close", {"name": name})         # guarded
    finally:
        _close(name)


def test_close_all_bypasses_lease():
    name = "vbtest-lease-closeall"
    try:
        _fresh(name)
        call("session_lease", {"name": name, "owner": "botA"})
        # session_close_all is an operator sledgehammer — NOT lease-gated.
        call("session_close_all")
        running = [s["name"] for s in call("session_list")["sessions"]
                   if s["running"]]
        assert name not in running
    finally:
        # restore the fixture's default session for the rest of the suite
        try:
            call("start", {"profile": "/tmp/vibatchium-test-profile",
                           "headless": True})
        except DaemonError:
            pass
        _close(name)


def test_steal_rotates_token_and_revokes_old():
    name = "vbtest-lease-steal"
    try:
        _fresh(name)
        tokA = call("session_lease", {"name": name, "owner": "botA"})["token"]
        with pytest.raises(DaemonError, match="busy"):
            call("session_lease", {"name": name, "owner": "botB"})  # no steal
        r = call("session_lease", {"name": name, "owner": "botB", "steal": True})
        assert r["owner"] == "botB"
        assert r["token"] != tokA                  # steal ROTATES the token …
        with pytest.raises(DaemonError, match="busy"):
            call("url", session=name, lease=tokA)  # … revoking the prior holder
        assert isinstance(call("url", session=name, lease=r["token"]), dict)
    finally:
        _close(name)


def test_renew_as_holder_keeps_token():
    name = "vbtest-lease-renew"
    try:
        _fresh(name)
        tok = call("session_lease", {"name": name, "owner": "botA", "ttl_s": 5})["token"]
        # renew as the holder (present the token) → same token, extended
        r2 = call("session_lease", {"name": name, "owner": "botA", "ttl_s": 60},
                  lease=tok)
        assert r2["token"] == tok
        assert isinstance(call("url", session=name, lease=tok), dict)
    finally:
        _close(name)


def test_lease_info_and_list_hide_token():
    name = "vbtest-lease-hide"
    try:
        _fresh(name)
        call("session_lease", {"name": name, "owner": "botA"})
        info = call("session_lease_info", {"name": name})
        assert info["leased"] is True
        assert "token" not in json.dumps(info)
        sl = call("session_list")
        row = next(s for s in sl["sessions"] if s["name"] == name)
        assert row.get("lease") and row["lease"]["owner"] == "botA"
        assert "token" not in json.dumps(row["lease"])
    finally:
        _close(name)


def test_no_lease_is_fully_open():
    name = "vbtest-lease-open"
    try:
        _fresh(name)                                   # never leased
        assert isinstance(call("url", session=name), dict)
    finally:
        _close(name)


# ─── UNIT: MCP threads the lease token per-call, never via process env ───
def test_mcp_lease_token_threaded_not_env(monkeypatch):
    from vibatchium import mcp_server as M
    rec = {}

    def fake_daemon_call(cmd, args=None, *, session=None, lease=None, **kw):
        rec.update(cmd=cmd, args=args, session=session, lease=lease)
        return {"ok": True}

    monkeypatch.setattr(M, "daemon_call", fake_daemon_call)
    monkeypatch.setattr(M, "daemon_is_running", lambda: True)
    monkeypatch.delenv("VIBATCHIUM_LEASE", raising=False)
    asyncio.run(M.call_tool("session_list", {"session": "s1", "lease": "TOK"}))
    assert rec["lease"] == "TOK"
    assert rec["session"] == "s1"
    assert "VIBATCHIUM_LEASE" not in os.environ      # never poisons process env
