"""0.16.0 idle-freeze — offline tests (no real Chrome, no real signals).

Covers: env knob parsing, eligibility gating (attach / headed / nodriver are
never frozen), SIGSTOP/SIGCONT apply/lift mechanics against monkeypatched
/proc + kill (incl. new-renderer-picked-up-on-next-pass, pid-reuse guard, and
none-found fail-safe), the dispatcher thawing before a verb runs, the close()
thaw, and peek() not stamping activity.
"""
from __future__ import annotations

import asyncio
import signal
import types
from pathlib import Path

from vibatchium.daemon import freeze
from vibatchium.daemon.registry import SessionEntry


# ─── fakes ────────────────────────────────────────────────────────────────

class FakeProc:
    """Monkeypatched process world: pid → starttime, plus a signal journal."""

    def __init__(self, procs: dict[int, int]):
        self.procs = dict(procs)      # pid -> starttime
        self.signals: list[tuple[int, int]] = []

    def find(self, profile_dir):  # replaces _find_renderers
        return sorted(self.procs)

    def starttime(self, pid):     # replaces _starttime
        return self.procs.get(pid)

    def kill(self, pid, sig):     # captures os.kill
        if pid not in self.procs:
            raise ProcessLookupError(pid)
        self.signals.append((pid, sig))


def _wire(monkeypatch, procs: dict[int, int]) -> FakeProc:
    fake = FakeProc(procs)
    monkeypatch.setattr(freeze, "_find_renderers", fake.find)
    monkeypatch.setattr(freeze, "_starttime", fake.starttime)
    monkeypatch.setattr(freeze.os, "kill", fake.kill)
    return fake


def _entry(mode="launch", headless=True, backend="patchright"):
    sess = types.SimpleNamespace(
        context=types.SimpleNamespace(pages=[]),
        page=types.SimpleNamespace(url="about:blank"),
        frame_ref=None, mode=mode, headless=headless, nav_allowlist=None)
    e = SessionEntry(name="t", profile_dir=Path("/tmp/vbtest-freeze"),
                     session=sess)
    e.flags["backend"] = backend
    return e


# ─── env knobs ────────────────────────────────────────────────────────────

def test_enabled_default_and_off_values(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_IDLE_FREEZE", raising=False)
    assert freeze.freeze_enabled()
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("VIBATCHIUM_IDLE_FREEZE", off)
        assert not freeze.freeze_enabled()
    monkeypatch.setenv("VIBATCHIUM_IDLE_FREEZE", "1")
    assert freeze.freeze_enabled()


def test_after_default_clamp_and_garbage(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_IDLE_FREEZE_AFTER", raising=False)
    assert freeze.freeze_after() == freeze.DEFAULT_AFTER
    monkeypatch.setenv("VIBATCHIUM_IDLE_FREEZE_AFTER", "30")
    assert freeze.freeze_after() == 30.0
    monkeypatch.setenv("VIBATCHIUM_IDLE_FREEZE_AFTER", "1")
    assert freeze.freeze_after() == 5.0
    monkeypatch.setenv("VIBATCHIUM_IDLE_FREEZE_AFTER", "soon")
    assert freeze.freeze_after() == freeze.DEFAULT_AFTER


# ─── eligibility ──────────────────────────────────────────────────────────

def test_eligible_matrix():
    assert freeze.eligible(_entry())
    assert not freeze.eligible(_entry(mode="attach"))     # human's browser?
    assert not freeze.eligible(_entry(headless=False))    # human-driven?
    assert not freeze.eligible(_entry(backend="nodriver"))
    none_sess = SessionEntry(name="n", profile_dir=Path("/tmp/x"), session=None)
    assert not freeze.eligible(none_sess)


# ─── apply / lift ─────────────────────────────────────────────────────────

def test_apply_stops_each_renderer_once(monkeypatch):
    fake = _wire(monkeypatch, {100: 5000, 101: 5001})
    e = _entry()
    fresh = asyncio.run(freeze.apply(e))
    assert fresh == 2 and e.frozen
    assert fake.signals == [(100, signal.SIGSTOP), (101, signal.SIGSTOP)]
    assert e.freeze_pids == [(100, 5000), (101, 5001)]
    # second pass: already covered — no new signals
    fresh = asyncio.run(freeze.apply(e))
    assert fresh == 0 and len(fake.signals) == 2


def test_apply_picks_up_new_renderer_on_next_pass(monkeypatch):
    fake = _wire(monkeypatch, {100: 5000})
    e = _entry()
    asyncio.run(freeze.apply(e))
    fake.procs[102] = 6000  # OOPIF/popup renderer appeared while parked
    fresh = asyncio.run(freeze.apply(e))
    assert fresh == 1 and (102, 6000) in e.freeze_pids


def test_apply_none_found_is_failsafe(monkeypatch):
    _wire(monkeypatch, {})
    e = _entry()
    fresh = asyncio.run(freeze.apply(e))
    assert fresh == 0 and not e.frozen and e.freeze_pids == []


def test_apply_skips_renderer_that_died_mid_scan(monkeypatch):
    fake = _wire(monkeypatch, {100: 5000, 101: 5001})
    real_starttime = fake.starttime

    def flaky(pid):
        return None if pid == 100 else real_starttime(pid)
    monkeypatch.setattr(freeze, "_starttime", flaky)
    e = _entry()
    fresh = asyncio.run(freeze.apply(e))
    assert fresh == 1 and e.freeze_pids == [(101, 5001)]


def test_lift_conts_and_clears(monkeypatch):
    fake = _wire(monkeypatch, {100: 5000})
    e = _entry()
    asyncio.run(freeze.apply(e))
    asyncio.run(freeze.lift(e))
    assert not e.frozen and e.freeze_pids == []
    assert fake.signals == [(100, signal.SIGSTOP), (100, signal.SIGCONT)]


def test_lift_never_signals_recycled_pid(monkeypatch):
    fake = _wire(monkeypatch, {100: 5000})
    e = _entry()
    asyncio.run(freeze.apply(e))
    fake.procs[100] = 9999  # pid recycled: different starttime
    asyncio.run(freeze.lift(e))
    # SIGSTOP from apply only — no SIGCONT to the impostor
    assert fake.signals == [(100, signal.SIGSTOP)]
    assert not e.frozen and e.freeze_pids == []


def test_lift_survives_vanished_pid(monkeypatch):
    fake = _wire(monkeypatch, {100: 5000})
    e = _entry()
    asyncio.run(freeze.apply(e))
    del fake.procs[100]  # renderer gone entirely
    asyncio.run(freeze.lift(e))  # must not raise
    assert not e.frozen and e.freeze_pids == []


def test_lift_noop_when_not_frozen(monkeypatch):
    fake = _wire(monkeypatch, {})
    e = _entry()
    asyncio.run(freeze.lift(e))
    assert fake.signals == [] and not e.frozen


# ─── dispatcher / registry integration ───────────────────────────────────

def _make_daemon_entry(monkeypatch):
    # monkeypatch (not os.environ) — a bare env write here leaks into test
    # files that sort after this one (test_plugins) and breaks them.
    monkeypatch.setenv("VIBATCHIUM_PLUGINS", "0")
    from vibatchium.daemon.server import Daemon
    d = Daemon()
    e = _entry()
    d.registry._entries["t"] = e
    return d, e


def test_dispatcher_thaws_before_verb(monkeypatch):
    monkeypatch.delenv("VIBATCHIUM_SELF_HEAL", raising=False)
    fake = _wire(monkeypatch, {100: 5000})
    d, e = _make_daemon_entry(monkeypatch)
    asyncio.run(freeze.apply(e))
    assert e.frozen
    seen = {}

    async def probe(daemon, args):
        seen["frozen_during_verb"] = e.frozen
        return {"ok": True}

    d._handlers["probe"] = probe
    out = asyncio.run(d._run_session_verb_with_recovery("probe", {}, e, "t"))
    assert out["ok"] and seen["frozen_during_verb"] is False
    assert (100, signal.SIGCONT) in fake.signals
    assert not e.frozen and e.freeze_pids == []


def test_dispatcher_thaws_with_selfheal_off(monkeypatch):
    monkeypatch.setenv("VIBATCHIUM_SELF_HEAL", "0")
    _wire(monkeypatch, {100: 5000})
    d, e = _make_daemon_entry(monkeypatch)
    asyncio.run(freeze.apply(e))

    async def probe(daemon, args):
        return {"frozen": e.frozen}

    d._handlers["probe"] = probe
    out = asyncio.run(d._run_session_verb_with_recovery("probe", {}, e, "t"))
    assert out["frozen"] is False


def test_registry_close_thaws_first(monkeypatch):
    fake = _wire(monkeypatch, {100: 5000})
    d, e = _make_daemon_entry(monkeypatch)
    asyncio.run(freeze.apply(e))

    async def fake_close(sess):
        # by the time the browser teardown runs, the renderer must be CONTd
        assert (100, signal.SIGCONT) in fake.signals
    from vibatchium.daemon import backends as _backends
    monkeypatch.setattr(_backends, "close", fake_close)
    closed = asyncio.run(d.registry.close("t"))
    assert closed and not e.frozen


def test_peek_does_not_touch(monkeypatch):
    d, e = _make_daemon_entry(monkeypatch)
    before = e.last_used_at
    assert d.registry.peek("t") is e
    assert e.last_used_at == before
    d.registry.get("t")
    assert e.last_used_at >= before
