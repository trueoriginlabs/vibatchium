"""Negative numbers and dash-leading text reach the daemon as arguments.

Plain click reads `-300` as an unknown option, so `vb mouse wheel 0 -300`
exited 2 before reaching the daemon, and a caller that ignored exit codes
(clipper) never scrolled up. Same for southern-hemisphere `geolocation` and
for text starting with a dash. A mistyped `--flag` must still error, not be
typed into the page.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from vibatchium import cli


@pytest.fixture
def sent(monkeypatch):
    calls: list = []

    def fake_call(cmd, args=None, **kw):
        calls.append((cmd, args))
        return {}

    monkeypatch.setattr(cli, "call", fake_call)
    return calls


def _run(argv):
    return CliRunner().invoke(cli.cli, argv)


@pytest.mark.parametrize("argv, want", [
    (["mouse", "wheel", "0", "-300"], {"action": "wheel", "dx": 0.0, "dy": -300.0}),
    (["mouse", "wheel", "--", "0", "-300"], {"action": "wheel", "dx": 0.0, "dy": -300.0}),
    (["mouse", "move", "-5", "10"], {"action": "move", "x": -5.0, "y": 10.0, "steps": 1}),
    (["mouse", "click", "-1.5", "20", "--button", "right"],
     {"action": "click", "x": -1.5, "y": 20.0, "button": "right"}),
])
def test_negative_mouse_numbers(sent, argv, want):
    r = _run(argv)
    assert r.exit_code == 0, r.output
    assert sent == [("mouse", want)]


def test_southern_hemisphere_geolocation(sent):
    r = _run(["geolocation", "-33.87", "151.21"])
    assert r.exit_code == 0, r.output
    assert sent[0][1]["lat"] == -33.87 and sent[0][1]["lng"] == 151.21


@pytest.mark.parametrize("argv, key, want", [
    (["fill", "#t", "-10 deaths"], "text", "-10 deaths"),
    (["fill", "#t", "-x"], "text", "-x"),
    (["type", "#t", "-dash first"], "text", "-dash first"),
    (["eval", "-1"], "expr", "-1"),
])
def test_dash_leading_text(sent, argv, key, want):
    r = _run(argv)
    assert r.exit_code == 0, r.output
    assert sent[0][1][key] == want


def test_real_options_still_parse(sent):
    r = _run(["type", "#t", "-x", "--delay", "40"])
    assert r.exit_code == 0, r.output
    assert sent[0][1]["delay_ms"] == 40
    r = _run(["fill", "--index=2", "#t", "-y"])
    assert r.exit_code == 0, r.output
    assert sent[1][1]["index"] == 2 and sent[1][1]["text"] == "-y"
    r = _run(["geolocation", "--clear"])
    assert r.exit_code == 0, r.output
    assert sent[2][1] == {"clear": True}


def test_help_still_works(sent):
    r = _run(["mouse", "wheel", "--help"])
    assert r.exit_code == 0 and "DX DY" in r.output and sent == []


def test_text_containing_h_is_not_help(sent):
    # click used to split '-10 deaths' into flags, find -h and print help (exit 0, nothing sent)
    r = _run(["type", "#t", "-oh hi"])
    assert r.exit_code == 0, r.output
    assert sent[0][1]["text"] == "-oh hi"


@pytest.mark.parametrize("argv", [
    ["type", "#t", "hello", "--delya", "40"],
    ["fill", "#t", "--use-secrt=site:password"],
    ["mouse", "wheel", "0", "100", "--fast"],
])
def test_mistyped_flags_still_error(sent, argv):
    r = _run(argv)
    assert r.exit_code == 2 and "No such option" in r.output
    assert sent == []
