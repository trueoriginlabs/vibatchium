"""Session names are validated at dispatch (validate_session_ref).

Before 0.20.0 `session_dir()` accepted an absolute path as-is, so an MCP call
with `session="/abs/dir"` picked an arbitrary Chrome user-data-dir without the
fspolicy.check_profile_dir gate `start --profile` gets. The rule refuses every
path shape but stays loose enough for every name that already exists on a
long-lived box (including one with spaces).
"""
from __future__ import annotations

import pytest

from vibatchium import fspolicy
from vibatchium.client import call
from vibatchium.daemon import paths

# Shapes seen on the reference box + every prefix the code / tests mint.
KNOWN_GOOD = [
    "default", "_ex-1816-7", "whoisdave-real", "vbtest-abc", "fleet-1a2b3c-0",
    "amb_live_0123abcd", "bbin_live", "lane-eu1", "macro-ui-1440",
    "sdk_0123abcd", "flow_interno", "dave-repair-83279", "x.y",
    "Shunxing 39 Taiwan cable January 2025", "-dash-led", ".hidden-ok",
    "unicodé", "a" * 64,
]

BAD = ["/tmp/x", "/", "a/b", "../x", "..", ".", "a\\b", "a\x00b", "a\nb",
       "tab\there", "", "x" * 256]


@pytest.mark.parametrize("name", KNOWN_GOOD)
def test_accepts_known_names(name):
    assert paths.validate_session_ref(name) == name


def test_accepts_every_profile_already_on_this_box():
    # The rule must not strand an existing identity.
    bad = []
    for name in paths.list_session_names():
        try:
            paths.validate_session_ref(name)
        except ValueError:
            bad.append(name)
    assert bad == [], bad


@pytest.mark.parametrize("name", BAD)
def test_refuses_path_shapes(name):
    with pytest.raises(ValueError):
        paths.validate_session_ref(name)


@pytest.mark.parametrize("name", [None, 7, ["a"]])
def test_refuses_non_strings(name):
    with pytest.raises(ValueError):
        paths.validate_session_ref(name)


def test_session_dir_no_longer_takes_an_absolute_path(tmp_path):
    target = tmp_path / "chosen"
    with pytest.raises(ValueError, match="not a path"):
        paths.session_dir(str(target))
    assert not target.exists(), "session_dir created the caller's directory"


async def test_dispatcher_refuses_before_any_verb(monkeypatch, tmp_path):
    monkeypatch.setenv("VIBATCHIUM_PLUGINS", "0")
    from vibatchium.daemon.server import Daemon
    d = Daemon()
    ran = []

    async def probe(daemon, args):
        ran.append(args)
        return {}

    d._handlers["probe"] = probe
    for bad in (str(tmp_path), "../escape", "a/b"):
        out = await d.dispatch({"id": "1", "cmd": "probe",
                                "args": {"_session": bad}})
        assert out["ok"] is False and "session name" in out["error"], out
    assert ran == []
    # an empty selector still means "the active session", as before
    out = await d.dispatch({"id": "2", "cmd": "status", "args": {"_session": ""}})
    assert out["ok"] is True, out


def test_daemon_refuses_an_absolute_session_from_an_agent_surface(tmp_path):
    target = tmp_path / "planted-profile"
    with pytest.raises(Exception, match="not a path"):
        call("start", {"headless": True,
                       fspolicy.SCOPE_ARG: {"roots": [str(tmp_path)],
                                            "cwd": str(tmp_path)}},
             session=str(target))
    assert not target.exists()
