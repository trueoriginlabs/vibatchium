"""Wave 6.1c — session checkpoint/restore tests.

Verifies:
- checkpoint_save captures tabs + cookies + viewport
- checkpoint_list returns the right metadata
- checkpoint_load restores cookies + localStorage (verified by eval)
- Cross-session load: save in session A, load in session B
- delete removes the file
- load on missing checkpoint errors clearly
"""
from __future__ import annotations

import shutil

import pytest

from patchium.client import call, DaemonError
from patchium.daemon.paths import PROFILES_DIR


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


def test_checkpoint_save_returns_metadata(local_server):
    """save returns counts that match what was captured."""
    call("go", {"url": f"{local_server}/simple.html"})
    call("eval", {"expr": "localStorage.setItem('cp_test', 'saved')"})
    res = call("checkpoint_save", {"name": "test_save_basic"})
    assert res["saved"] is True
    assert res["tabs"] >= 1
    assert res["bytes"] > 0
    # cleanup
    call("checkpoint_delete", {"name": "test_save_basic"})


def test_checkpoint_list_after_save(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    call("checkpoint_save", {"name": "test_list_a"})
    call("checkpoint_save", {"name": "test_list_b"})
    res = call("checkpoint_list")
    names = [c["name"] for c in res["checkpoints"]]
    assert "test_list_a" in names
    assert "test_list_b" in names
    call("checkpoint_delete", {"name": "test_list_a"})
    call("checkpoint_delete", {"name": "test_list_b"})


def test_checkpoint_load_restores_local_storage(local_server):
    """Round-trip: set LS → save → clear LS → load → LS is back."""
    call("go", {"url": f"{local_server}/simple.html"})
    call("eval", {"expr": "localStorage.setItem('rt_key', 'roundtrip_value')"})
    call("checkpoint_save", {"name": "test_rt_ls"})
    # Clear LS
    call("eval", {"expr": "localStorage.clear()"})
    val_before = call("eval", {"expr": "localStorage.getItem('rt_key')"})["value"]
    assert val_before is None
    # Load checkpoint
    call("checkpoint_load", {"name": "test_rt_ls"})
    val_after = call("eval", {"expr": "localStorage.getItem('rt_key')"})["value"]
    assert val_after == "roundtrip_value"
    call("checkpoint_delete", {"name": "test_rt_ls"})


def test_checkpoint_load_restores_cookies(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    call("eval", {"expr": "document.cookie='cp_ck=cookie_val; path=/'"})
    call("checkpoint_save", {"name": "test_rt_cookies"})
    # Clear cookies for the origin
    call("eval", {"expr": "document.cookie='cp_ck=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'"})
    cookies_before = call("cookies")["cookies"]
    assert not any(c["name"] == "cp_ck" for c in cookies_before)
    # Load
    call("checkpoint_load", {"name": "test_rt_cookies"})
    cookies_after = call("cookies")["cookies"]
    assert any(c["name"] == "cp_ck" and c["value"] == "cookie_val" for c in cookies_after)
    call("checkpoint_delete", {"name": "test_rt_cookies"})


def test_checkpoint_cross_session_load(local_server):
    """Save in session A → load into freshly-created session B."""
    name_a = "patchium_test_w6_ckpt_a"
    name_b = "patchium_test_w6_ckpt_b"
    _ensure_clean(name_a)
    _ensure_clean(name_b)
    call("session_new", {"name": name_a})
    call("start", {"headless": True}, session=name_a)
    try:
        call("go", {"url": f"{local_server}/simple.html"}, session=name_a)
        call("eval", {"expr": "localStorage.setItem('xs_key', 'from_A')"}, session=name_a)
        call("checkpoint_save", {"name": "transfer_me"}, session=name_a)
        # Now session B (fresh)
        call("session_new", {"name": name_b})
        call("start", {"headless": True}, session=name_b)
        call("go", {"url": f"{local_server}/simple.html"}, session=name_b)
        # Confirm B is blank
        assert call("eval", {"expr": "localStorage.getItem('xs_key')"},
                    session=name_b)["value"] is None
        # Load A's checkpoint INTO B
        call("checkpoint_load",
             {"name": "transfer_me", "from_session": name_a},
             session=name_b)
        val = call("eval", {"expr": "localStorage.getItem('xs_key')"},
                   session=name_b)["value"]
        assert val == "from_A"
    finally:
        try: call("session_close", {"name": name_a})
        except DaemonError: pass
        try: call("session_close", {"name": name_b})
        except DaemonError: pass
        _ensure_clean(name_a)
        _ensure_clean(name_b)


def test_checkpoint_load_missing_errors():
    with pytest.raises(DaemonError, match="no checkpoint"):
        call("checkpoint_load", {"name": "definitely_does_not_exist_xyz"})


def test_checkpoint_delete_idempotent(local_server):
    call("go", {"url": f"{local_server}/simple.html"})
    call("checkpoint_save", {"name": "test_del"})
    r1 = call("checkpoint_delete", {"name": "test_del"})
    assert r1["deleted"] is True
    r2 = call("checkpoint_delete", {"name": "test_del"})
    assert r2["deleted"] is False  # already gone


def test_checkpoint_multi_tab_restore(local_server):
    """Open 2 tabs, save, navigate them away, restore — both tabs come back."""
    call("go", {"url": f"{local_server}/simple.html"})
    call("page_new")
    call("go", {"url": f"{local_server}/second.html"})
    pages_before = call("pages")["pages"]
    urls_before = sorted(p["url"] for p in pages_before)
    call("checkpoint_save", {"name": "multi_tab"})
    # Navigate active tab away from second.html
    call("go", {"url": "about:blank"})
    # Load checkpoint — should restore both tabs
    call("checkpoint_load", {"name": "multi_tab"})
    pages_after = call("pages")["pages"]
    urls_after = sorted(p["url"] for p in pages_after if "blank" not in p["url"])
    # At minimum the original URLs should reappear
    assert any("simple.html" in u for u in urls_after)
    assert any("second.html" in u for u in urls_after)
    call("checkpoint_delete", {"name": "multi_tab"})


# ─── Wave 7.5b: path-traversal hardening ───────────────────────────────


@pytest.mark.parametrize("bad_name", [
    "../escape",
    "..",
    ".",
    ".hidden",
    "foo/bar",
    "foo\\bar",
    "name with spaces",
    "name;rm -rf",
    # NOTE: empty string is intentionally allowed on save — it resolves
    # to "default" for caller convenience. delete validates the raw arg.
    "a" * 65,  # over the 64-char cap
])
def test_checkpoint_rejects_unsafe_names(local_server, bad_name):
    """Checkpoint names that could escape the checkpoints/ dir or pollute
    the filesystem must be rejected at the verb boundary."""
    call("go", {"url": f"{local_server}/simple.html"})
    with pytest.raises(DaemonError):
        call("checkpoint_save", {"name": bad_name})
    with pytest.raises(DaemonError):
        call("checkpoint_delete", {"name": bad_name})


@pytest.mark.parametrize("bad_name", ["../etc", "..", "foo/bar"])
def test_checkpoint_load_rejects_unsafe_from_session(local_server, bad_name):
    """`from_session` is a name spliced into a PROFILES_DIR path — same
    rules apply as for the checkpoint name itself."""
    call("go", {"url": f"{local_server}/simple.html"})
    call("checkpoint_save", {"name": "trav_probe"})
    try:
        with pytest.raises(DaemonError):
            call("checkpoint_load", {
                "name": "trav_probe", "from_session": bad_name,
            })
    finally:
        call("checkpoint_delete", {"name": "trav_probe"})


@pytest.mark.parametrize("bad_name", [
    "../escape", "..", ".hidden", "foo/bar", "name with spaces", "",
])
def test_session_new_rejects_unsafe_names(bad_name):
    """session_new tightened to the same validator."""
    with pytest.raises(DaemonError):
        call("session_new", {"name": bad_name})
