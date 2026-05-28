"""CLI-level tests for the 0.6.3 goal ergonomics + version surfacing.

These drive the Click commands directly with `call`/`daemon_is_running`
monkeypatched, so no daemon or browser is needed.
"""
from __future__ import annotations

import json

from click.testing import CliRunner

from vibatchium import cli


# ─── #2: `goal new` accepts a positional description (and -d alias) ──────────

def test_goal_new_positional_description(monkeypatch):
    captured: dict = {}

    def fake_call(cmd, args=None, **kw):
        captured["args"] = args
        return {"id": "G1", "status": "pending",
                "description": args["description"]}

    monkeypatch.setattr(cli, "call", fake_call)
    r = CliRunner().invoke(cli.cli, ["goal", "new", "buy cheapest BTC",
                                     "--budget", "steps=5"])
    assert r.exit_code == 0, r.output
    assert captured["args"]["description"] == "buy cheapest BTC"


def test_goal_new_dash_d_alias(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(cli, "call",
                        lambda cmd, args=None, **kw: captured.update(args=args)
                        or {"id": "G", "status": "pending", "description": args["description"]})
    r = CliRunner().invoke(cli.cli, ["goal", "new", "-d", "via flag"])
    assert r.exit_code == 0, r.output
    assert captured["args"]["description"] == "via flag"


def test_goal_new_requires_description(monkeypatch):
    monkeypatch.setattr(cli, "call", lambda *a, **k: {})
    r = CliRunner().invoke(cli.cli, ["goal", "new"])
    assert r.exit_code != 0
    assert "description" in r.output.lower()


# ─── #3: `goal events --follow` stops on a terminal event ───────────────────

def test_goal_events_follow_stops_on_terminal(monkeypatch):
    # First (and only) batch already contains the terminal `done`, so follow
    # prints and returns without entering the polling sleep loop.
    def fake_call(cmd, args=None, **kw):
        return {"events": [{"seq": 1, "kind": "step_start", "payload": {}},
                           {"seq": 2, "kind": "done", "payload": {}}]}

    monkeypatch.setattr(cli, "call", fake_call)
    r = CliRunner().invoke(cli.cli, ["goal", "events", "G1", "--follow"])
    assert r.exit_code == 0, r.output
    assert "#1 step_start" in r.output
    assert "#2 done" in r.output


def test_goal_events_plain(monkeypatch):
    monkeypatch.setattr(cli, "call", lambda *a, **k: {"events": [
        {"seq": 1, "kind": "session_attached", "payload": {"session": "s"}}]})
    r = CliRunner().invoke(cli.cli, ["goal", "events", "G1"])
    assert r.exit_code == 0
    assert "#1 session_attached" in r.output


# ─── #1: status surfaces daemon vs client version + mismatch flag ───────────

def test_status_version_mismatch(monkeypatch):
    monkeypatch.setattr(cli, "daemon_is_running", lambda: True)
    monkeypatch.setattr(cli, "call", lambda *a, **k: {
        "running": False, "session": None, "mode": None, "pid": 1,
        "version": "0.0.1", "running_sessions": []})
    r = CliRunner().invoke(cli.cli, ["--json", "status"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["daemon_version"] == "0.0.1"
    assert out["client_version"] == cli.__version__
    assert out["version_mismatch"] is True


def test_status_version_match(monkeypatch):
    monkeypatch.setattr(cli, "daemon_is_running", lambda: True)
    monkeypatch.setattr(cli, "call", lambda *a, **k: {
        "running": False, "session": None, "mode": None, "pid": 1,
        "version": cli.__version__, "running_sessions": []})
    r = CliRunner().invoke(cli.cli, ["--json", "status"])
    out = json.loads(r.output)
    assert out["version_mismatch"] is False
    assert out["daemon_version"] == cli.__version__
