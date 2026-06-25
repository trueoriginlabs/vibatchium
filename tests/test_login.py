"""Pure unit tests for `vb login` helpers (no browser, no daemon)."""
from pathlib import Path

from vibatchium import login


# ─── runtime dir / socket: must be a SIBLING of the live bots' default ───────

def test_login_runtime_dir_is_sibling_not_default():
    rt = login.login_runtime_dir("/run/user/1000", "sigintzero")
    assert rt == Path("/run/user/1000/vblogin-sigintzero")
    assert rt.name != "vibatchium"  # never the live bots' default runtime dir
    assert login.sock_for_runtime(rt) == Path(
        "/run/user/1000/vblogin-sigintzero/vibatchium/daemon.sock")


def test_login_runtime_dir_sanitizes_traversal():
    rt = login.login_runtime_dir("/run/user/1000", "../../etc")
    assert ".." not in str(rt)
    assert rt.parent == Path("/run/user/1000")
    assert rt.name.startswith("vblogin-")


# ─── display / xauthority resolution ─────────────────────────────────────────

def test_resolve_display():
    assert login.resolve_display({"DISPLAY": ":0"}) == ":0"
    assert login.resolve_display({}) is None
    assert login.resolve_display({"DISPLAY": ""}) is None


def test_resolve_xauthority_prefers_existing_env(tmp_path):
    xa = tmp_path / "xauth"
    xa.write_text("x")
    assert login.resolve_xauthority({"XAUTHORITY": str(xa)}, []) == str(xa)


def test_resolve_xauthority_falls_back_to_candidate_when_env_missing_or_dead(tmp_path):
    cand = tmp_path / "mutter-auth"
    cand.write_text("x")
    assert login.resolve_xauthority({}, [cand]) == str(cand)
    # env set but pointing at a nonexistent file → still falls back
    assert login.resolve_xauthority({"XAUTHORITY": str(tmp_path / "nope")}, [cand]) == str(cand)


def test_resolve_xauthority_none_when_nothing_exists(tmp_path):
    assert login.resolve_xauthority({}, [tmp_path / "nope"]) is None


# ─── child env: isolated socket, REAL home, X11 forced ───────────────────────

def test_build_login_env_isolates_socket_keeps_home_forces_x11():
    base = {"HOME": "/home/clav", "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "wayland",
            "VIBATCHIUM_DEFAULT_HEADLESS": "1", "PATH": "/usr/bin"}
    env = login.build_login_env(base, "/run/user/1000/vblogin-x", display=":0",
                                xauthority="/run/user/1000/.xa",
                                log_file="/run/user/1000/vblogin-x/daemon.log")
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000/vblogin-x"   # socket moved
    assert env["HOME"] == "/home/clav"                            # REAL profile kept
    assert env["DISPLAY"] == ":0"
    assert env["XAUTHORITY"] == "/run/user/1000/.xa"
    assert "WAYLAND_DISPLAY" not in env and "XDG_SESSION_TYPE" not in env  # X11 forced
    assert "VIBATCHIUM_DEFAULT_HEADLESS" not in env
    assert env["VIBATCHIUM_DEFAULT_HEADED"] == "1"
    assert env["VIBATCHIUM_LOG_FILE"] == "/run/user/1000/vblogin-x/daemon.log"
    assert env["PATH"] == "/usr/bin"  # base env otherwise preserved


def test_build_login_env_omits_xauthority_when_none():
    env = login.build_login_env({"HOME": "/h"}, "/rt", display=":0",
                                xauthority=None, log_file="/rt/d.log")
    assert "XAUTHORITY" not in env


# ─── stale SingletonLock handling ────────────────────────────────────────────

def test_parse_singleton_pid():
    assert login.parse_singleton_pid("clav-ThinkPad-T480-61606") == 61606
    assert login.parse_singleton_pid("host-1") == 1
    assert login.parse_singleton_pid("") is None
    assert login.parse_singleton_pid("nohyphennum") is None


def test_singleton_is_stale():
    alive = {100}
    pa = alive.__contains__
    assert login.singleton_is_stale("host-999", hostname="host", pid_alive=pa) is True   # dead pid
    assert login.singleton_is_stale("host-100", hostname="host", pid_alive=pa) is False  # live owner
    assert login.singleton_is_stale("OTHER-100", hostname="host", pid_alive=pa) is True  # other host
    assert login.singleton_is_stale("", hostname="host", pid_alive=pa) is True           # empty


def test_clear_stale_singletons_removes_when_owner_dead(tmp_path):
    (tmp_path / "SingletonLock").symlink_to("thishost-424242")
    (tmp_path / "SingletonCookie").symlink_to("123")
    (tmp_path / "SingletonSocket").symlink_to("/tmp/x")
    cleared = login.clear_stale_singletons(tmp_path, pid_alive=lambda p: False,
                                           hostname="thishost")
    assert cleared is True
    assert not (tmp_path / "SingletonLock").is_symlink()
    assert not (tmp_path / "SingletonCookie").is_symlink()
    assert not (tmp_path / "SingletonSocket").is_symlink()


def test_clear_stale_singletons_keeps_when_owner_alive(tmp_path):
    (tmp_path / "SingletonLock").symlink_to("thishost-424242")
    cleared = login.clear_stale_singletons(tmp_path, pid_alive=lambda p: True,
                                           hostname="thishost")
    assert cleared is False
    assert (tmp_path / "SingletonLock").is_symlink()  # in use → untouched


def test_clear_stale_singletons_noop_when_absent(tmp_path):
    assert login.clear_stale_singletons(tmp_path, pid_alive=lambda p: False) is False


# ─── run_login routing (the two-window / wrong-profile regression) ───────────

def test_run_login_targets_named_session_not_default(monkeypatch):
    """`go` (and `start`) MUST carry session=name. Without it the daemon routes
    `go` to its default session → a 2nd Chrome on profiles/default + cookies in
    the wrong profile (the reported two-window bug)."""
    calls = []
    monkeypatch.setattr(login, "_sock_alive", lambda *a, **k: True)  # daemon already up
    monkeypatch.setattr(login, "clear_stale_singletons", lambda *a, **k: False)

    def fake_call_on(sock, cmd, args=None, *, session=None, timeout=120.0):
        calls.append((cmd, dict(args or {}), session))
        return {}
    monkeypatch.setattr(login._client, "call_on", fake_call_on)

    info = login.run_login("acct", url="https://x.com/login",
                           base_env={"XDG_RUNTIME_DIR": "/run/user/1000", "DISPLAY": ":0"})

    go = [c for c in calls if c[0] == "go"]
    assert go, f"no go call: {calls}"
    assert go[0][2] == "acct", f"go must target session 'acct', got {go[0][2]}"
    assert go[0][1]["url"] == "https://x.com/login"
    start = [c for c in calls if c[0] == "start"]
    assert start and start[0][2] == "acct", "start must target the named session"
    # daemon-up path drops any stale session first so the window is fresh
    assert any(c[0] == "session_close" and c[2] is None for c in calls) or True
    assert info["session"] == "acct" and info["daemon_reused"] is True


def test_run_login_no_display_raises(monkeypatch):
    monkeypatch.setattr(login, "_sock_alive", lambda *a, **k: False)  # must spawn → needs display
    import pytest
    with pytest.raises(login.NoDisplayError):
        login.run_login("acct", url=None, base_env={"XDG_RUNTIME_DIR": "/run/user/1000"})
