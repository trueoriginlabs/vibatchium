"""Wave 5.1: multi-session foundation tests.

Verifies:
- Default session auto-resolves when no _session arg is sent
- Two concurrent sessions have isolated cookie jars
- session_new creates dir without launching Chrome
- session_list reports running + on-disk state
- session_close stops Chrome but preserves profile dir
- Per-session lock — concurrent verbs on different sessions don't serialize
- VIBATCHIUM_SESSION env var resolves the right session
- session_delete refuses to remove a running or active session
- profile_* legacy verbs still work (1:1 alias)
"""
from __future__ import annotations

import os
import shutil
import time
import threading

import pytest

from vibatchium.client import call, DaemonError
from vibatchium.daemon.paths import PROFILES_DIR, get_active_session_name


# ─── helpers ──────────────────────────────────────────────────────────────


def _ensure_clean(name: str) -> None:
    """Close + delete a session if it lingers from a prior test failure."""
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


# ─── tests ────────────────────────────────────────────────────────────────


def test_default_session_active_after_conftest_start():
    """The session-scoped conftest started a 'default' session — status confirms."""
    res = call("status")
    assert res["running"] is True
    assert res["session"] == "default"
    assert "default" in res["running_sessions"]


def test_session_new_creates_dir_without_launching():
    """session_new makes the on-disk dir; Chrome is NOT launched until `start`."""
    name = "vibatchium_test_w5_new"
    _ensure_clean(name)
    res = call("session_new", {"name": name})
    assert res["created"] is True
    assert (PROFILES_DIR / name).exists()
    # not running
    lst = call("session_list")["sessions"]
    row = next(s for s in lst if s["name"] == name)
    assert row["running"] is False
    # cleanup
    call("session_delete", {"name": name})


def test_session_new_idempotent():
    name = "vibatchium_test_w5_idem"
    _ensure_clean(name)
    a = call("session_new", {"name": name})
    b = call("session_new", {"name": name})
    assert a["created"] is True and a["exists"] is False
    assert b["created"] is False and b["exists"] is True
    call("session_delete", {"name": name})


def test_session_list_shows_default_running():
    res = call("session_list")
    names = [s["name"] for s in res["sessions"]]
    assert "default" in names
    default_row = next(s for s in res["sessions"] if s["name"] == "default")
    assert default_row["running"] is True


def test_close_delete_registered_underscore_session():
    """Regression (the _iv-1783253040 leak): internal underscore-prefixed sessions
    (_ex- explore, _iv- interactive-view) are creatable via `start`/the `_session`
    field — which does NOT validate the name — but validate_name rejects a leading
    underscore. So session_close/session_delete must operate on an ALREADY-REGISTERED
    session by exact name WITHOUT re-validating, or such sessions can't be closed and
    leak (headed, holding a slot). Only `stop` could close them before this fix."""
    name = "_iv-testclose"
    call("start", {"headless": True}, session=name)     # bypasses validation, like _iv-
    assert name in [s["name"] for s in call("session_list")["sessions"]]
    res = call("session_close", {"name": name})          # previously raised 'bad session name'
    assert res["closed"] is True and res["name"] == name
    assert call("session_delete", {"name": name})["deleted"] is True   # on-disk profile too


def test_session_close_still_rejects_unknown_bad_name():
    """The fix is scoped: a name that is NOT a live session (or on-disk profile) is
    still validated, so a malformed/never-existed name gets a clean error."""
    with pytest.raises(DaemonError):
        call("session_close", {"name": "_iv-neverexisted"})   # underscore + not a session
    with pytest.raises(DaemonError):
        call("session_close", {"name": "../evil"})            # path-traversal still blocked


def test_two_parallel_sessions_isolate_cookies(local_server):
    """Run two sessions in parallel; cookies in one don't leak to the other."""
    name_a, name_b = "vibatchium_test_w5_a", "vibatchium_test_w5_b"
    _ensure_clean(name_a)
    _ensure_clean(name_b)
    # Spin up both sessions (the daemon already runs 'default')
    call("session_new", {"name": name_a})
    call("session_new", {"name": name_b})
    call("start", {"headless": True}, session=name_a)
    call("start", {"headless": True}, session=name_b)
    try:
        # navigate both to the local fixture (same origin)
        call("go", {"url": f"{local_server}/simple.html"}, session=name_a)
        call("go", {"url": f"{local_server}/simple.html"}, session=name_b)
        # set a different localStorage flag on each
        call("eval", {"expr": "localStorage.setItem('who','aa')"}, session=name_a)
        call("eval", {"expr": "localStorage.setItem('who','bb')"}, session=name_b)
        # readback
        a_val = call("eval", {"expr": "localStorage.getItem('who')"}, session=name_a)["value"]
        b_val = call("eval", {"expr": "localStorage.getItem('who')"}, session=name_b)["value"]
        assert a_val == "aa"
        assert b_val == "bb"
        # cookies set in A should not appear in B
        call("eval", {
            "expr": "document.cookie='ses=A; path=/'"
        }, session=name_a)
        cookies_a = call("cookies", session=name_a)["cookies"]
        cookies_b = call("cookies", session=name_b)["cookies"]
        a_names = {c["name"] for c in cookies_a}
        b_names = {c["name"] for c in cookies_b}
        assert "ses" in a_names
        assert "ses" not in b_names
    finally:
        call("session_close", {"name": name_a})
        call("session_close", {"name": name_b})
        # profile dirs survive close
        assert (PROFILES_DIR / name_a).exists()
        assert (PROFILES_DIR / name_b).exists()
        call("session_delete", {"name": name_a})
        call("session_delete", {"name": name_b})


def test_per_session_lock_does_not_serialize_across_sessions(local_server):
    """A long-running op (sleep) on session A must not block a verb on session B."""
    name_a = "vibatchium_test_w5_lock_a"
    name_b = "vibatchium_test_w5_lock_b"
    _ensure_clean(name_a)
    _ensure_clean(name_b)
    call("session_new", {"name": name_a})
    call("session_new", {"name": name_b})
    call("start", {"headless": True}, session=name_a)
    call("start", {"headless": True}, session=name_b)
    try:
        call("go", {"url": f"{local_server}/simple.html"}, session=name_a)
        call("go", {"url": f"{local_server}/simple.html"}, session=name_b)

        # On session A: hold a sleep that the lock serializes.
        # On session B: immediately fire a quick verb. If per-session locks
        # work, B finishes before A. (Note: `sleep` is in UNLOCKED_VERBS so
        # it doesn't even hold A's lock — but we use a verb that DOES hold
        # the lock to validate independence. `eval` is lock-acquiring.)
        results = {}

        def slow_a():
            t0 = time.time()
            call("eval", {"expr": "new Promise(r => setTimeout(r, 1500))"},
                 session=name_a)
            results["a_end"] = time.time() - t0

        def fast_b():
            t0 = time.time()
            time.sleep(0.1)  # let A start first
            call("title", session=name_b)
            results["b_end"] = time.time() - t0

        th_a = threading.Thread(target=slow_a)
        th_b = threading.Thread(target=fast_b)
        th_a.start(); th_b.start()
        th_a.join(); th_b.join()
        # B must finish well before A (B is just title fetch; A is 1.5s eval)
        assert results["b_end"] < 1.0, f"B took {results['b_end']:.2f}s — was it serialized?"
        assert results["a_end"] >= 1.4
    finally:
        call("session_close", {"name": name_a})
        call("session_close", {"name": name_b})
        call("session_delete", {"name": name_a})
        call("session_delete", {"name": name_b})


def test_session_via_env_var(local_server):
    """VIBATCHIUM_SESSION env var routes calls without the `session=` kwarg."""
    name = "vibatchium_test_w5_env"
    _ensure_clean(name)
    call("session_new", {"name": name})
    call("start", {"headless": True}, session=name)
    try:
        prior = os.environ.get("VIBATCHIUM_SESSION")
        os.environ["VIBATCHIUM_SESSION"] = name
        try:
            res = call("status")
            assert res["session"] == name
            assert res["running"] is True
        finally:
            if prior is None:
                os.environ.pop("VIBATCHIUM_SESSION", None)
            else:
                os.environ["VIBATCHIUM_SESSION"] = prior
    finally:
        call("session_close", {"name": name})
        call("session_delete", {"name": name})


def test_session_delete_refuses_active():
    """session_delete must NOT remove the active session."""
    active = get_active_session_name()
    with pytest.raises(DaemonError, match="active"):
        call("session_delete", {"name": active})


def test_session_delete_refuses_default():
    """session_delete cannot remove the special 'default' name."""
    with pytest.raises(DaemonError):
        call("session_delete", {"name": "default"})


def test_session_close_then_reopen_preserves_cookies(local_server):
    """The whole point: close session, reopen, cookies/storage still there."""
    name = "vibatchium_test_w5_persist"
    _ensure_clean(name)
    call("session_new", {"name": name})
    call("start", {"headless": True}, session=name)
    try:
        call("go", {"url": f"{local_server}/simple.html"}, session=name)
        call("eval", {"expr": "localStorage.setItem('persisted','yes')"},
             session=name)
        # close
        call("session_close", {"name": name})
        # re-open
        call("start", {"headless": True}, session=name)
        call("go", {"url": f"{local_server}/simple.html"}, session=name)
        val = call("eval", {"expr": "localStorage.getItem('persisted')"},
                   session=name)["value"]
        assert val == "yes"
    finally:
        call("session_close", {"name": name})
        call("session_delete", {"name": name})


def test_profile_legacy_aliases_still_work():
    """profile_list / profile_new / profile_delete remain functional aliases."""
    name = "vibatchium_test_w5_legacy"
    _ensure_clean(name)
    res = call("profile_new", {"name": name})
    assert res["created"] is True or res.get("exists")
    listed = call("profile_list")
    assert name in listed["profiles"]
    call("profile_delete", {"name": name})
    after = call("profile_list")
    assert name not in after["profiles"]
