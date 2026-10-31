"""A live daemon cut off from its clients (2026-10-30 outage).

A review script built `Daemon()` in-process under the real runtime dir and
called `shutdown()` on it. That instance never took the lock or bound the
socket, yet `shutdown()` unlinked SOCK_PATH and PID_PATH — the shared daemon
kept running on an unlinked socket, every client spawn lost the flock and
exited 2, and the client reported "daemon did not come up within 5.0s".

Each test runs its own daemon in a fresh runtime dir; nothing here can reach
the conftest daemon or a real one.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from vibatchium.client import DaemonNotRunning, call_on

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def rt():
    # Short path on purpose: AF_UNIX caps socket paths at ~107 bytes.
    d = Path(tempfile.mkdtemp(prefix="vbsl-"))
    (d / "home").mkdir()
    (d / "run").mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _env(rt: Path) -> dict:
    env = os.environ.copy()
    env.update({
        "XDG_RUNTIME_DIR": str(rt / "run"),
        "XDG_STATE_HOME": str(rt / "state"),
        "HOME": str(rt / "home"),
        "VIBATCHIUM_WARM": "off",
        "VIBATCHIUM_PLUGINS": "0",
        "PYTHONPATH": str(REPO),
    })
    env.pop("VIBATCHIUM_DAEMON_IDLE_TIMEOUT", None)
    return env


def _paths(rt: Path) -> tuple[Path, Path, Path]:
    base = rt / "run" / "vibatchium"
    return base / "daemon.sock", base / "daemon.pid", base / "daemon.lock"


def _spawn_daemon(rt: Path, watch: float | None = 0.5) -> subprocess.Popen:
    """Start a daemon in RT. `watch` sets the path-watch interval (None = 1h,
    i.e. the watchdog never fires within a test). The code string names the
    module, so /proc/<pid>/cmdline still contains `vibatchium.daemon.server`."""
    interval = 3600.0 if watch is None else watch
    code = ("import vibatchium.daemon.server as s\n"
            f"s._PATH_WATCH_INTERVAL = {interval}\n"
            "s.main()\n")
    proc = subprocess.Popen([sys.executable, "-c", code], env=_env(rt),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sock = _paths(rt)[0]
    deadline = time.time() + 20
    while time.time() < deadline:
        if _status_pid(sock) == proc.pid:
            return proc
        assert proc.poll() is None, f"daemon exited rc={proc.returncode}"
        time.sleep(0.1)
    proc.kill()
    raise AssertionError("test daemon did not come up")


def _status_pid(sock: Path) -> int | None:
    try:
        return call_on(sock, "status", timeout=3.0)["pid"]
    except (DaemonNotRunning, OSError):
        return None


def _wait_status_pid(sock: Path, timeout: float) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = _status_pid(sock)
        if pid is not None:
            return pid
        time.sleep(0.1)
    return None


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ─── 1. shutdown() only removes what this instance owns ───────────────────
def test_foreign_daemon_shutdown_leaves_live_daemon_reachable(rt):
    """The incident, verbatim: a second process builds Daemon() under the live
    daemon's runtime dir and calls shutdown() without ever running it."""
    proc = _spawn_daemon(rt, watch=None)
    sock, pidf, _ = _paths(rt)
    try:
        code = ("import asyncio\n"
                "from vibatchium.daemon.server import Daemon\n"
                "asyncio.run(Daemon().shutdown())\n")
        subprocess.run([sys.executable, "-c", code], env=_env(rt),
                       check=True, timeout=30, capture_output=True)
        assert sock.exists(), "a never-started Daemon unlinked the live socket"
        assert pidf.exists(), "a never-started Daemon unlinked the live pidfile"
        assert _status_pid(sock) == proc.pid
    finally:
        _stop(proc)


def test_shutdown_does_not_unlink_a_socket_it_did_not_bind(rt, monkeypatch):
    """Even the lock holder must not remove a socket inode it didn't create."""
    from vibatchium.daemon import server as srv
    sock, pidf, lock = (rt / "s.sock", rt / "d.pid", rt / "d.lock")
    monkeypatch.setattr(srv, "SOCK_PATH", sock)
    monkeypatch.setattr(srv, "PID_PATH", pidf)
    monkeypatch.setattr(srv, "LOCK_PATH", lock)

    async def go():
        d = srv.Daemon()
        assert d._acquire_singleton_lock()
        await d._bind_socket()
        # someone replaces the socket and the pidfile behind our back
        sock.unlink()
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other.bind(str(sock))
        pidf.write_text("999999")
        await d.shutdown()
        other.close()

    asyncio.run(go())
    assert sock.exists(), "shutdown unlinked a socket another process bound"
    assert pidf.read_text() == "999999", "shutdown unlinked another pid's pidfile"


def test_owner_shutdown_still_cleans_up(rt):
    """Regression guard for the fix above: a normal SIGTERM still removes the
    socket and pidfile the daemon itself created."""
    proc = _spawn_daemon(rt, watch=None)
    sock, pidf, lock = _paths(rt)
    proc.terminate()
    assert proc.wait(timeout=15) == 0
    assert not sock.exists()
    assert not pidf.exists()
    assert lock.exists(), "the lockfile must never be unlinked (flock is inode-bound)"


# ─── 2. the daemon notices its socket path is gone or replaced ────────────
@pytest.mark.parametrize("damage", ["unlink", "replace", "wipe_dir"])
def test_daemon_rebinds_lost_socket(rt, damage):
    proc = _spawn_daemon(rt, watch=0.5)
    sock, pidf, lock = _paths(rt)
    try:
        if damage == "unlink":
            sock.unlink()
            pidf.unlink()
        elif damage == "replace":
            # a dead socket inode at our path: nothing is listening on it
            sock.unlink()
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(sock))
            stale.close()
            assert _status_pid(sock) is None
        else:
            shutil.rmtree(sock.parent)
        assert _wait_status_pid(sock, timeout=10) == proc.pid, (
            f"daemon did not rebind its socket after {damage}")
        assert pidf.read_text().strip() == str(proc.pid)
        if damage == "wipe_dir":
            assert lock.exists()
    finally:
        _stop(proc)


def test_wiped_dir_does_not_let_a_second_daemon_in(rt):
    """After the runtime dir is wiped, the live daemon re-takes the lock on the
    new inode, so a fresh daemon must still refuse to start."""
    proc = _spawn_daemon(rt, watch=0.5)
    sock, _, _ = _paths(rt)
    try:
        shutil.rmtree(sock.parent)
        assert _wait_status_pid(sock, timeout=10) == proc.pid
        second = subprocess.run([sys.executable, "-m", "vibatchium.daemon.server"],
                                env=_env(rt), capture_output=True, text=True,
                                timeout=30)
        assert second.returncode == 2, second.stderr[-600:]
        assert "holds" in second.stderr
    finally:
        _stop(proc)


def test_daemon_exits_when_another_daemon_took_the_lock(rt, monkeypatch):
    """If the lock path now names an inode another daemon holds, this daemon is
    unreachable for good: it must stop (closing its Chromes) rather than linger
    as an orphan."""
    import fcntl

    from vibatchium.daemon import server as srv
    lock = rt / "d.lock"
    monkeypatch.setattr(srv, "SOCK_PATH", rt / "s.sock")
    monkeypatch.setattr(srv, "PID_PATH", rt / "d.pid")
    monkeypatch.setattr(srv, "LOCK_PATH", lock)

    async def go():
        d = srv.Daemon()
        assert d._acquire_singleton_lock()
        await d._bind_socket()
        lock.unlink()
        fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # the "other daemon"
        try:
            await d._check_paths()
            assert d._stopping.is_set()
        finally:
            os.close(fd)
            await d.shutdown()

    asyncio.run(go())


# ─── 3. the client names a live-but-unreachable lock holder ───────────────
def test_client_error_names_unreachable_lock_holder(rt):
    proc = _spawn_daemon(rt, watch=None)          # no self-heal: stay cut off
    sock, pidf, _ = _paths(rt)
    try:
        sock.unlink()
        pidf.unlink()
        code = ("from vibatchium.client import call, DaemonError\n"
                "try:\n"
                "    call('status')\n"
                "except DaemonError as e:\n"
                "    print(e)\n")
        out = subprocess.run([sys.executable, "-c", code], env=_env(rt),
                             capture_output=True, text=True, timeout=60)
        msg = out.stdout
        assert str(proc.pid) in msg, msg + out.stderr[-600:]
        assert f"kill {proc.pid}" in msg, msg
        assert "did not come up" not in msg, msg
    finally:
        _stop(proc)


# ─── 4. review follow-ups ─────────────────────────────────────────────────
def _patch_paths(rt: Path, monkeypatch):
    from vibatchium.daemon import server as srv
    paths = (rt / "s.sock", rt / "d.pid", rt / "d.lock")
    for name, p in zip(("SOCK_PATH", "PID_PATH", "LOCK_PATH"), paths, strict=True):
        monkeypatch.setattr(srv, name, p)
    return srv, paths


def test_shutdown_removes_its_socket_without_server_close(rt, monkeypatch):
    """On 3.11/3.12 closing the server leaves the socket file behind, so
    shutdown()'s own unlink is the only cleanup. Exercise it directly."""
    srv, (sock, pidf, _) = _patch_paths(rt, monkeypatch)

    async def go():
        d = srv.Daemon()
        assert d._acquire_singleton_lock()
        await d._bind_socket()
        await d.shutdown()

    asyncio.run(go())
    assert not sock.exists()
    assert not pidf.exists()


def test_shutdown_without_the_lock_removes_nothing(rt, monkeypatch):
    """A Daemon that never won the lock owns no path, even if its recorded
    socket inode and the pidfile happen to look like its own."""
    srv, (sock, pidf, _) = _patch_paths(rt, monkeypatch)
    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    other.bind(str(sock))
    pidf.write_text(str(os.getpid()))
    try:
        d = srv.Daemon()
        d._sock_id = srv._path_id(sock)
        asyncio.run(d.shutdown())
        assert sock.exists()
        assert pidf.exists()
    finally:
        other.close()


def test_failed_lock_reopen_keeps_the_lock(rt, monkeypatch):
    """If the replacement lockfile can't be opened (EACCES, EMFILE), the daemon
    must keep the lock it holds and retry, not drop it and go blind."""
    srv, (_, _, lock) = _patch_paths(rt, monkeypatch)

    async def go():
        d = srv.Daemon()
        assert d._acquire_singleton_lock()
        await d._bind_socket()
        held = d._lock_fd
        lock.unlink()
        real = d._acquire_singleton_lock

        def fail():
            raise PermissionError(13, "Permission denied", str(lock))
        d._acquire_singleton_lock = fail
        with pytest.raises(PermissionError):
            await d._check_paths()
        assert d._lock_fd == held
        d._acquire_singleton_lock = real
        await d._check_paths()
        st = os.fstat(d._lock_fd)
        assert srv._path_id(lock) == (st.st_dev, st.st_ino)
        await d.shutdown()

    asyncio.run(go())


def test_unbindable_socket_path_is_reported_once(rt, monkeypatch, caplog):
    """A regular file at the socket path can't be rebound. Log it once, not
    every tick, and rebind as soon as the path is cleared."""
    srv, (sock, _, _) = _patch_paths(rt, monkeypatch)

    async def go():
        d = srv.Daemon()
        assert d._acquire_singleton_lock()
        await d._bind_socket()
        sock.unlink()
        sock.write_text("not a socket")
        for _ in range(3):
            await d._check_paths()
        assert sum("cannot rebind" in r.getMessage() for r in caplog.records) == 1
        sock.unlink()
        await d._check_paths()
        assert srv._path_id(sock) == d._sock_id
        await d.shutdown()

    with caplog.at_level("ERROR", logger="vibatchium.server"):
        asyncio.run(go())


def test_stalled_connection_does_not_block_shutdown(rt):
    """One client that connects and never sends must not keep a SIGTERMed
    daemon (and its Chromes) alive: 3.12+'s wait_closed() waits for it."""
    proc = _spawn_daemon(rt, watch=None)
    sock, _, _ = _paths(rt)
    idle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    idle.connect(str(sock))
    try:
        time.sleep(0.3)
        proc.terminate()
        assert proc.wait(timeout=15) == 0
    finally:
        idle.close()
        _stop(proc)
