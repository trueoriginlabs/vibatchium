# Daemon cut off from its clients — 2026-10-30

**TL;DR.** The shared daemon (pid 5475) went unreachable after a review script
called `shutdown()` on a `Daemon()` it had built in-process. That `shutdown()`
deleted the live daemon's socket and pidfile even though its own instance owned
neither. Three changes on branch `daemon-socket-loss`:

1. `shutdown()` deletes only paths its own daemon created.
2. A running daemon notices when its socket, pidfile or lockfile is gone and
   puts it back within about 2 s.
3. When the client finds a live lock holder that doesn't answer, its error
   names that pid and how to recover.

There are 14 new tests in `tests/test_daemon_socket_loss.py`. 13 fail on
master 748558c; the 14th is a regression guard that passes on both. The full
suite passes on the branch.

## What can cut the daemon off

Clients find the daemon by one path, `$XDG_RUNTIME_DIR/vibatchium/daemon.sock`,
and the singleton lives in one inode, `daemon.lock`. Anything that changes
either one, while the daemon keeps running, cuts it off:

| Cause | What happened on master | Now |
|-|-|-|
| A different process calls `Daemon().shutdown()` (the 10-30 incident; any script or harness outside pytest's conftest isolation) | Unlinked the live socket and pidfile. The daemon kept running unreachable, and every spawn exited 2 on the flock | Nothing is removed: that instance holds no lock |
| `rm` of the socket (by hand, or a cleanup script) | Unreachable until killed | The socket is rebound within 2 s; a client waiting 5 s in `spawn_daemon` connects |
| Socket replaced by another inode (a dead socket left by a stray bind) | Unreachable | Rebound. If the new inode answers a connect, another process is serving the path: the daemon logs it and keeps running, unreachable (see "left open") |
| A regular file or directory at the socket path | Unreachable | Can't be rebound (asyncio only replaces a stale socket). Logged once; rebound as soon as the path is removed |
| `vibatchium/` runtime dir deleted (`rm -r`, a cleanup tool) | Unreachable, **and** the next spawn creates a fresh lockfile inode and wins it, leaving two daemons; the old one is orphaned with its Chromes | Still one daemon either way. If no client calls in the ≤2 s before the next check, the dir, lock, socket and pidfile are put back. With active clients that is the exception: the first call recreates the dir and spawns a daemon that takes the new lock in about 0.3 s, so the old daemon shuts down and its sessions are lost (see "left open") |
| `XDG_RUNTIME_DIR` itself gone (logind cleanup; Linger=yes on this box, so it shouldn't happen here) | Unreachable forever | The daemon shuts down |
| `pkill` (2026-09-09), OOM kill | The daemon is gone and the next call spawns a fresh one. This was never a reachability bug; the damage was the lost sessions | Unchanged |
| pytest against the default socket (2026-07-15) | Fixed back then by conftest isolation | Unchanged |

The reason this went unnoticed: nothing in the daemon looked at its paths
after startup, and the client only knew that the spawn didn't come up, so it
blamed startup.

## What changed

`vibatchium/daemon/server.py`

- `_bind_socket()` binds, records the socket's `(st_dev, st_ino)` and writes
  the pidfile. `run()` and the watchdog both use it.
- `shutdown()` unlinks only if `self._lock_fd` is held, the socket only while
  `SOCK_PATH` is still the recorded inode, and the pidfile only while it holds
  this pid. The lockfile is still never unlinked (flock is bound to the inode,
  see 0.9.1).
- `_check_paths()`, run every `_PATH_WATCH_INTERVAL` (2 s) by
  `_path_watchdog()`:
  - Runtime dir gone: shut down.
  - Lock path no longer the inode this daemon holds: re-take the lock on the
    new inode. If another daemon holds that inode, log and shut down. If the
    new lockfile can't be opened (EACCES, EMFILE), keep the old lock and retry
    on the next tick.
  - Socket path missing or a different inode: rebind. If the new inode answers
    a connect, it belongs to someone else's listener; log once and leave it.
    If the bind fails (a non-socket file at the path), log once and retry
    only when the path changes.
  - Pidfile not ours: rewrite it.
- `run()` no longer keeps a `serve_forever()` task. The server is already
  serving on creation, and a rebind closes the old server, which would have
  ended `serve_forever` and with it the daemon. The daemon now waits only on
  `_stopping`. At exit it runs close, then `wait_closed()` for at most
  `_CLOSE_GRACE` (5 s), then `shutdown()`. On 3.12+ `wait_closed()` waits for
  every accepted connection, so before this one stalled client kept a
  SIGTERMed daemon and its Chromes alive indefinitely (master too).

`vibatchium/client.py`

- `spawn_daemon()`: on timeout, if the spawn exited 2 (lost the flock), it
  reads the pid the holder wrote into `daemon.lock` and checks
  `/proc/<pid>/cmdline` (or `ps` where there is no `/proc`, e.g. macOS) that
  it is a vibatchium daemon. It then raises, for example:
  `daemon pid 214897 holds …/daemon.lock but …/daemon.sock is gone, so it is
  running and unreachable. Recover with `kill 214897` (it closes its sessions
  and exits); the next call starts a fresh daemon. If other clients share this
  daemon, tell them first.`
  When the socket exists but doesn't answer, it says "busy or wedged"
  instead. Otherwise the old message is unchanged.

## Tests

`tests/test_daemon_socket_loss.py`. Each test runs its own daemon in a fresh
`/tmp/vbsl-*` runtime dir. Failures on master 748558c:

| Test | Master |
|-|-|
| `test_foreign_daemon_shutdown_leaves_live_daemon_reachable` (the incident, verbatim) | `a never-started Daemon unlinked the live socket` |
| `test_shutdown_does_not_unlink_a_socket_it_did_not_bind` | `'Daemon' object has no attribute '_bind_socket'` |
| `test_owner_shutdown_still_cleans_up` | passes (regression guard for the fix) |
| `test_daemon_rebinds_lost_socket[unlink]` / `[replace]` / `[wipe_dir]` | `daemon did not rebind its socket after …` |
| `test_wiped_dir_does_not_let_a_second_daemon_in` | socket never comes back |
| `test_daemon_exits_when_another_daemon_took_the_lock` | `'Daemon' object has no attribute '_bind_socket'` |
| `test_client_error_names_unreachable_lock_holder` | `daemon did not come up within 5.0s` |
| `test_shutdown_removes_its_socket_without_server_close` (the 3.11/3.12 cleanup path) | `'Daemon' object has no attribute '_bind_socket'` |
| `test_shutdown_without_the_lock_removes_nothing` | the socket and pidfile are unlinked |
| `test_failed_lock_reopen_keeps_the_lock` | `'Daemon' object has no attribute '_bind_socket'` |
| `test_unbindable_socket_path_is_reported_once` | `'Daemon' object has no attribute '_bind_socket'` |
| `test_stalled_connection_does_not_block_shutdown` | the SIGTERMed daemon is still alive after 15 s |

Several of these fail on master only because a method is missing. The behaviour
they cover is also shown by subprocess repros on master: two daemons alive
after a dir wipe, versus one on the branch.

## Left open

- **Daemons already running keep the old code.** The shared daemon heals only
  after this branch is merged and the daemon is restarted. Until then the new
  client message is the only help, and it works against an old daemon too,
  because it reads only `daemon.lock`.
- **A runtime-dir wipe usually costs the sessions.** Any client call in the
  ≤2 s before the old daemon re-takes the lock spawns a daemon that wins it,
  and the old one shuts down. With active clients that is the common outcome
  (a reviewer saw it in 5 of 6 trials), so the bot sessions must be
  recreated. That beats master (an orphaned daemon plus an empty one), but it
  is not a heal. The plain socket-unlink case, the one that happened, does
  not race: the lock survives, so the spawn exits 2 and the old daemon
  rebinds.
- **Another live listener on the socket path** leaves the daemon running and
  unreachable, with its sessions. It logs once and doesn't take the path
  over, because it can't tell a stray listener from a real daemon that has
  lost its lock. Nothing on this box creates one today.
- **A wedged event loop** (socket present, nothing answers) is still not
  detected by the daemon. The client now names the pid, but no automatic
  recovery happens.
- **Other runtime-dir contents** (screenshot/explore caches, the
  active-session file) are not restored after a wipe; only the lock, socket
  and pidfile are.
- **Non-pytest harnesses** can still build a `Daemon()` under the real
  runtime dir. Its `shutdown()` is harmless now, but anything else it does
  in-process (calling `run()`, handlers that write into `CACHE_DIR`) isn't
  guarded. Such harnesses should set their own `XDG_RUNTIME_DIR` before
  importing vibatchium, as `tests/conftest.py` does.
- **Phantom daemons from env mismatch.** A client with a different (or unset)
  `XDG_RUNTIME_DIR` talks to a different daemon. That isn't a socket-loss bug,
  but it looks like one from a bot's side.
- **A test leaks a daemon per suite run.** In `tests/test_wave7_codex_friction.py`,
  the explore-default-screenshot test runs `vb explore` with a temp HOME and
  no `XDG_RUNTIME_DIR`, and never shuts down the daemon that spawns. It's
  isolated, so it can't touch the shared socket, but it stays alive under
  `systemd --user` (pids 43396 and 86835 earlier; I killed 233969 from this
  branch's run). This predates the branch and isn't fixed here.
- `vb daemon list` still learns `live_pid` from the pidfile only. The watchdog
  now restores the pidfile, so this matters only for old daemons.
