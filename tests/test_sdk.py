"""0.11.0 — the ergonomic SDK: vb.session() + vb.daemon(isolated=True).

The session-CM teardown contract is tested DETERMINISTICALLY with a fake
connection (no daemon, no Chrome) — that's where the load-bearing guarantees
live: ephemeral starts DIRECTLY (no prewarm), and close+delete ALWAYS run, even
when the body raises. The isolated-daemon test spawns a real private daemon (no
Chrome) and asserts the footprint stays off the shared ~/.config and is removed
on exit — RAM-gated so it self-skips on a memory-tight box.
"""
from __future__ import annotations

import pytest

import vibatchium as vb


class FakeConn:
    """Records (cmd, args, session) and returns plausible results. Optionally
    raises on a chosen verb to exercise error paths."""

    def __init__(self, fail_on: str | None = None):
        self.calls: list[tuple] = []
        self.fail_on = fail_on

    def __call__(self, cmd, args=None, *, session=None, **kw):
        self.calls.append((cmd, dict(args or {}), session))
        if self.fail_on and cmd == self.fail_on:
            raise RuntimeError(f"boom in {cmd}")
        if cmd == "text":
            return {"text": "hello"}
        return {"ok": True}

    def verbs(self):
        return [c[0] for c in self.calls]

    def first(self, cmd):
        return next(c for c in self.calls if c[0] == cmd)


# ─── session CM: the prewarm correction ──────────────────────────────────────

def test_session_ephemeral_starts_directly_without_session_new():
    conn = FakeConn()
    with vb.session(ephemeral=True, call=conn):
        pass
    assert "start" in conn.verbs()
    # the correction: NO session_new → no redundant prewarm Chrome
    assert "session_new" not in conn.verbs()
    assert conn.first("start")[1].get("ephemeral") is True


def test_session_persistent_uses_session_new_with_prewarm_false():
    conn = FakeConn()
    with vb.session(ephemeral=False, name="probe", call=conn):
        pass
    assert "session_new" in conn.verbs()
    assert conn.first("session_new")[1].get("prewarm") is False
    assert "start" in conn.verbs()


# ─── session CM: guaranteed teardown ─────────────────────────────────────────

def test_session_ephemeral_closes_and_deletes_on_normal_exit():
    conn = FakeConn()
    with vb.session(ephemeral=True, call=conn) as s:
        s.go("https://example.com")
        assert s.text() == "hello"
    assert conn.verbs().count("session_close") == 1
    assert "session_delete" in conn.verbs()  # belt-and-suspenders no-leak
    # session-scoped verbs targeted our minted name, not the active default
    assert conn.first("go")[2] == s.name
    assert s.name.startswith("sdk_")


def test_session_teardown_runs_on_exception_and_propagates():
    conn = FakeConn()
    with pytest.raises(ValueError, match="body blew up"):
        with vb.session(ephemeral=True, call=conn):
            raise ValueError("body blew up")
    # the original exception propagates AND teardown still ran
    assert "session_close" in conn.verbs()
    assert "session_delete" in conn.verbs()


def test_session_teardown_swallows_close_errors():
    # a failing close must not mask the body's success nor raise
    conn = FakeConn(fail_on="session_close")
    with vb.session(ephemeral=True, call=conn):
        pass
    assert "session_close" in conn.verbs()
    # delete still attempted after close failed
    assert "session_delete" in conn.verbs()


def test_session_persistent_keeps_profile_on_exit():
    conn = FakeConn()
    with vb.session(ephemeral=False, name="probe", call=conn):
        pass
    assert "session_close" in conn.verbs()
    assert "session_delete" not in conn.verbs()  # persistent → profile preserved


def test_session_rejects_traversal_name():
    # a caller-supplied name reaches `start` → session_dir() (which doesn't
    # validate); guard against traversal up front
    conn = FakeConn()
    with pytest.raises(ValueError):
        with vb.session(name="../../etc", call=conn):
            pass
    assert conn.calls == []  # rejected before any daemon call


# ─── isolated daemon: start() failure must NOT leak temp dirs (the CRITICAL) ──

def test_isolated_daemon_start_failure_cleans_up(monkeypatch):
    from vibatchium import sdk

    captured = {}
    real_mkdtemp = sdk.tempfile.mkdtemp

    def spy_mkdtemp(*a, **kw):
        p = real_mkdtemp(*a, **kw)
        captured.setdefault("dirs", []).append(p)
        return p

    monkeypatch.setattr(sdk.tempfile, "mkdtemp", spy_mkdtemp)
    # make the spawn blow up AFTER the temp dirs are minted
    monkeypatch.setattr(sdk.subprocess, "Popen",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

    d = vb.isolated_daemon(ram_floor_mb=0)
    with pytest.raises(RuntimeError, match="boom"):
        with d:
            pass

    # __enter__ raised → __exit__ never runs → start() must have cleaned up itself
    import os as _os
    assert captured.get("dirs"), "expected temp dirs to have been minted"
    for p in captured["dirs"]:
        assert not _os.path.exists(p), f"leaked temp dir {p}"


# ─── isolated daemon: footprint isolation + teardown (real subprocess) ──────

def test_isolated_daemon_isolates_footprint_and_tears_down():
    from vibatchium import sdk
    from vibatchium.daemon import paths as _paths

    avail = sdk._mem_available_mb()
    if avail is not None and avail < 700:
        pytest.skip(f"low memory ({avail}MB) — skip spawning isolated daemon")

    def shared_profiles():
        d = _paths.PROFILES_DIR
        return {p.name for p in d.iterdir()} if d.exists() else set()

    before = shared_profiles()
    d = vb.isolated_daemon(ram_floor_mb=0, max_ephemeral=0, warm=False)
    with d:
        rt, home = d.runtime_dir, d.home
        # answers on its OWN socket
        st = d.call("status")
        assert isinstance(st, dict)
        assert d.sock_path.exists()
        # a profile created on it lands under the PRIVATE home, not the shared one
        d.call("session_new", {"name": "isoprobe", "prewarm": False})
        priv = home / ".config" / "vibatchium" / "profiles" / "isoprobe"
        assert priv.exists(), "isolated profile should live under the private HOME"

    # after exit: minted temp dirs removed, nothing leaked into shared profiles
    assert not rt.exists()
    assert not home.exists()
    after = shared_profiles()
    assert "isoprobe" not in after
    assert before == after
