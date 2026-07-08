"""Plugin system — discovery, add_verb contract, lock-class routing, reload.

These are in-process tests: they construct a Daemon() and call
``daemon.dispatch(...)`` directly, with no socket and no Chrome. Plugin verbs
that declare ``lock="unlocked"`` need no browser session, so the full
register → dispatch path is exercised cheaply.
"""
from __future__ import annotations

import textwrap

import pytest

from vibatchium.plugins import registry
from vibatchium.plugins.api import (
    PluginError, VerbSpec, validate_verb_name,
)


# ─── a local-dir plugin written to a temp PLUGINS_DIR ────────────────────

_DEMO_PLUGIN = textwrap.dedent('''
    __version__ = "9.9.9"

    async def _echo(daemon, args):
        return {"echoed": args.get("msg", ""), "count": args.get("count", 0)}

    async def _needs_session(daemon, args):
        # lock="session" → dispatcher requires a running session first.
        from vibatchium.daemon.handlers import _need_session
        _need_session(daemon)
        return {"ok": True}

    def register(daemon):
        daemon.add_verb(
            name="demo.echo",
            handler=_echo,
            inputs_schema={"msg": "string", "count": "integer"},
            outputs_schema={"echoed": "string"},
            description="Echo a message back.",
            lock="unlocked",
        )
        daemon.add_verb(
            name="demo.act",
            handler=_needs_session,
            description="Needs a live session.",
            lock="session",
        )
''')


@pytest.fixture
def demo_plugin_dir(tmp_path, monkeypatch):
    """Create ~/.config/.../plugins/demo/__init__.py in a temp dir and point
    the registry at it. Also stub out entry-point discovery for determinism."""
    pdir = tmp_path / "plugins"
    (pdir / "demo").mkdir(parents=True)
    (pdir / "demo" / "__init__.py").write_text(_DEMO_PLUGIN)
    monkeypatch.setattr(registry, "PLUGINS_DIR", pdir)
    monkeypatch.setattr(registry, "_discover_entry_points", lambda: [])
    return pdir


def _fresh_daemon():
    from vibatchium.daemon.server import Daemon
    return Daemon()


# ─── verb-name validation ────────────────────────────────────────────────

def test_verb_name_must_be_namespaced():
    validate_verb_name("x.search")
    validate_verb_name("a.b.c")
    for bad in ["search", "Start", "x.", ".x", "x..y", "x.Search", "123.go", ""]:
        with pytest.raises(PluginError):
            validate_verb_name(bad)


def test_verbspec_rejects_bad_lock_and_handler():
    with pytest.raises(PluginError):
        VerbSpec(name="x.y", handler=lambda d, a: None, lock="weird")
    with pytest.raises(PluginError):
        VerbSpec(name="x.y", handler="not callable")


# ─── discovery + load ────────────────────────────────────────────────────

async def test_local_dir_plugin_loads_and_dispatches(demo_plugin_dir):
    d = _fresh_daemon()
    # plugin_list shows the demo plugin + its verbs
    res = await d.dispatch({"id": "1", "cmd": "plugin_list", "args": {}})
    assert res["ok"]
    plugins = {p["name"]: p for p in res["result"]["plugins"]}
    assert "demo" in plugins
    assert plugins["demo"]["source"] == "local_dir"
    assert plugins["demo"]["version"] == "9.9.9"
    assert set(plugins["demo"]["verbs"]) == {"demo.echo", "demo.act"}
    assert plugins["demo"]["error"] is None

    # the unlocked verb dispatches with no session
    r2 = await d.dispatch({"id": "2", "cmd": "demo.echo",
                           "args": {"msg": "hi", "count": 3}})
    assert r2["ok"]
    assert r2["result"] == {"echoed": "hi", "count": 3}


async def test_list_verbs_returns_plugin_specs(demo_plugin_dir):
    d = _fresh_daemon()
    res = await d.dispatch({"id": "1", "cmd": "list_verbs", "args": {}})
    specs = {s["name"]: s for s in res["result"]["verbs"]}
    assert "demo.echo" in specs
    assert specs["demo.echo"]["plugin"] == "demo"
    assert specs["demo.echo"]["inputs_schema"] == {"msg": "string", "count": "integer"}
    assert specs["demo.echo"]["lock"] == "unlocked"


async def test_session_lock_verb_rejected_without_session(demo_plugin_dir):
    d = _fresh_daemon()
    r = await d.dispatch({"id": "1", "cmd": "demo.act", "args": {}})
    assert not r["ok"]
    assert "no session" in r["error"].lower()


async def test_plugin_show(demo_plugin_dir):
    d = _fresh_daemon()
    r = await d.dispatch({"id": "1", "cmd": "plugin_show", "args": {"name": "demo"}})
    assert r["ok"]
    assert r["result"]["name"] == "demo"
    spec_names = {s["name"] for s in r["result"]["verb_specs"]}
    assert spec_names == {"demo.echo", "demo.act"}
    # unknown plugin
    r2 = await d.dispatch({"id": "2", "cmd": "plugin_show", "args": {"name": "nope"}})
    assert not r2["ok"]


# ─── collision / safety of add_verb ──────────────────────────────────────

async def test_add_verb_refuses_builtin_shadow_and_dups(demo_plugin_dir):
    d = _fresh_daemon()

    async def _h(daemon, args):
        return {}

    # built-in shadow (no dot anyway, but explicitly a built-in name)
    with pytest.raises(PluginError):
        d.add_verb(name="go", handler=_h, lock="unlocked")  # not namespaced
    # duplicate plugin verb
    with pytest.raises(PluginError):
        d.add_verb(name="demo.echo", handler=_h, lock="unlocked")


# ─── reload picks up new plugins ─────────────────────────────────────────

async def test_reload_discovers_new_plugin(demo_plugin_dir):
    d = _fresh_daemon()
    # add a second plugin dir, then reload
    new = demo_plugin_dir / "second"
    new.mkdir()
    (new / "__init__.py").write_text(textwrap.dedent('''
        async def _ping(daemon, args):
            return {"pong": True}
        def register(daemon):
            daemon.add_verb(name="second.ping", handler=_ping, lock="unlocked")
    '''))
    r = await d.dispatch({"id": "1", "cmd": "plugin_reload", "args": {}})
    assert r["ok"]
    names = {p["name"] for p in r["result"]["plugins"]}
    assert {"demo", "second"} <= names
    r2 = await d.dispatch({"id": "2", "cmd": "second.ping", "args": {}})
    assert r2["ok"] and r2["result"] == {"pong": True}


# ─── entry-point discovery path ──────────────────────────────────────────

async def test_entry_point_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "PLUGINS_DIR", tmp_path / "none")

    async def _h(daemon, args):
        return {"via": "entry_point"}

    def _register(daemon):
        daemon.add_verb(name="ep.verb", handler=_h, lock="unlocked",
                        description="from an entry point")

    dp = registry.DiscoveredPlugin(
        name="eppkg", register=_register, source="entry_point",
        version="1.2.3", origin="eppkg.plugin:register",
    )
    monkeypatch.setattr(registry, "_discover_entry_points", lambda: [dp])

    d = _fresh_daemon()
    r = await d.dispatch({"id": "1", "cmd": "plugin_list", "args": {}})
    plugins = {p["name"]: p for p in r["result"]["plugins"]}
    assert plugins["eppkg"]["source"] == "entry_point"
    assert plugins["eppkg"]["version"] == "1.2.3"
    r2 = await d.dispatch({"id": "2", "cmd": "ep.verb", "args": {}})
    assert r2["ok"] and r2["result"]["via"] == "entry_point"


async def test_broken_plugin_is_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "PLUGINS_DIR", tmp_path / "none")

    def _bad_register(daemon):
        raise RuntimeError("boom")

    dp = registry.DiscoveredPlugin(
        name="bad", register=_bad_register, source="entry_point",
        version=None, origin="bad:register",
    )
    monkeypatch.setattr(registry, "_discover_entry_points", lambda: [dp])
    # Daemon construction must not raise even though the plugin blows up.
    d = _fresh_daemon()
    r = await d.dispatch({"id": "1", "cmd": "plugin_list", "args": {}})
    bad = {p["name"]: p for p in r["result"]["plugins"]}["bad"]
    assert bad["error"] and "boom" in bad["error"]


# ─── CLI dotted-verb token parsing (pure function) ───────────────────────

def test_cli_passthrough_token_parsing(monkeypatch):
    from vibatchium import cli
    # no daemon: schema empty → positionals collect under "args"
    monkeypatch.setattr(cli, "_plugin_verb_schema", lambda verb: {})
    args = cli._parse_passthrough_tokens(
        "x.search", ["$BTC", "--count", "20", "--verbose"])
    assert args == {"count": "20", "verbose": True, "args": ["$BTC"]}


def test_cli_passthrough_positional_maps_to_schema(monkeypatch):
    from vibatchium import cli
    monkeypatch.setattr(cli, "_plugin_verb_schema",
                        lambda verb: {"query": "string", "count": "integer"})
    args = cli._parse_passthrough_tokens("x.search", ["$BTC", "--count", "20"])
    assert args == {"query": "$BTC", "count": 20}  # count coerced to int


# ─── 0.4 plugin install survives PEP 668 ─────────────────────────────────

class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_install_pep668_retries_with_break_system_packages(monkeypatch):
    """Plain pip aborts with the externally-managed error; the helper retries
    with --break-system-packages and reports the exact command."""
    from vibatchium import cli
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: False)
    calls: list[list[str]] = []

    def fake_run(cmd, *, capture):
        calls.append(list(cmd))
        if "--break-system-packages" in cmd:
            return _FakeCompleted(returncode=0, stdout="Successfully installed")
        return _FakeCompleted(
            returncode=1,
            stderr="error: externally-managed-environment\n× This environment …")

    monkeypatch.setattr(cli, "_run", fake_run)
    rc, note = cli._install_plugin_dist("somepkg")
    assert rc == 0
    assert len(calls) == 2                       # plain attempt, then fallback
    assert "--break-system-packages" not in calls[0]
    assert "--break-system-packages" in calls[1]
    assert calls[1][:4] == [cli.sys.executable, "-m", "pip", "install"]
    # actionable message names the exact retry command
    assert "--break-system-packages" in note
    assert "somepkg" in note


def test_install_routes_through_pipx_when_pipx_managed(monkeypatch):
    from vibatchium import cli
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: True)
    calls: list[list[str]] = []

    def fake_run(cmd, *, capture):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(cli, "_run", fake_run)
    rc, note = cli._install_plugin_dist("somepkg")
    assert rc == 0
    assert calls == [["pipx", "inject", "vibatchium", "somepkg"]]
    assert "pipx" in note


def test_install_no_fallback_when_pip_succeeds(monkeypatch):
    from vibatchium import cli
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: False)
    calls: list[list[str]] = []

    def fake_run(cmd, *, capture):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0, stdout="Successfully installed")

    monkeypatch.setattr(cli, "_run", fake_run)
    rc, note = cli._install_plugin_dist("somepkg")
    assert rc == 0
    assert len(calls) == 1                        # no PEP-668 retry
    assert note == ""


def test_remove_pep668_fallback_uninstall(monkeypatch):
    from vibatchium import cli
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: False)
    calls: list[list[str]] = []

    def fake_run(cmd, *, capture):
        calls.append(list(cmd))
        if "--break-system-packages" in cmd:
            return _FakeCompleted(returncode=0)
        return _FakeCompleted(returncode=1,
                              stderr="error: externally-managed-environment")

    monkeypatch.setattr(cli, "_run", fake_run)
    rc, note = cli._remove_plugin_dist("somepkg")
    assert rc == 0
    assert calls[1][:4] == [cli.sys.executable, "-m", "pip", "uninstall"]
    assert "--break-system-packages" in calls[1]
    assert "-y" in calls[1] and "somepkg" in calls[1]


# ─── vb update (self-upgrade) ────────────────────────────────────────────

def test_update_pip_latest(monkeypatch):
    from vibatchium import cli
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: False)
    # 0.12.0: _update_dist now also branches on uv-venv / editable install before
    # the pip path — pin both to False so this test exercises the pip branch
    # regardless of the dev environment it runs in (the repo's own venv is uv).
    monkeypatch.setattr(cli, "_is_uv_venv", lambda: False)
    monkeypatch.setattr(cli, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    calls: list[list[str]] = []

    def fake_run(cmd, *, capture):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0, stdout="Successfully installed")

    monkeypatch.setattr(cli, "_run", fake_run)
    rc, note = cli._update_dist(None)
    assert rc == 0
    assert calls[0] == [cli.sys.executable, "-m", "pip", "install", "-U", "vibatchium"]


def test_update_pipx_latest(monkeypatch):
    from vibatchium import cli
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: True)
    # editable is checked before pipx in _update_dist — pin it off so the pipx
    # branch runs even when the test executes from the repo's editable venv.
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run",
                        lambda cmd, *, capture: calls.append(list(cmd)) or _FakeCompleted(0))
    rc, note = cli._update_dist(None)
    assert rc == 0
    assert calls == [["pipx", "upgrade", "vibatchium"]]
    assert "pipx" in note


def test_update_pipx_pinned_version(monkeypatch):
    from vibatchium import cli
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: True)
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_run",
                        lambda cmd, *, capture: calls.append(list(cmd)) or _FakeCompleted(0))
    rc, note = cli._update_dist("0.6.2")
    assert rc == 0
    assert calls == [["pipx", "install", "--force", "vibatchium==0.6.2"]]


def test_update_pip_pinned_pep668_fallback(monkeypatch):
    from vibatchium import cli
    monkeypatch.setattr(cli, "_is_pipx_install", lambda: False)
    # See test_update_pip_latest: pin the new uv/editable predicates to False so
    # the pip (PEP-668 fallback) branch is exercised in any dev environment.
    monkeypatch.setattr(cli, "_is_uv_venv", lambda: False)
    monkeypatch.setattr(cli, "_is_uv_tool_install", lambda: False)
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    calls: list[list[str]] = []

    def fake_run(cmd, *, capture):
        calls.append(list(cmd))
        if "--break-system-packages" in cmd:
            return _FakeCompleted(returncode=0)
        return _FakeCompleted(returncode=1,
                              stderr="error: externally-managed-environment")

    monkeypatch.setattr(cli, "_run", fake_run)
    rc, note = cli._update_dist("0.6.2")
    assert rc == 0
    assert len(calls) == 2
    assert calls[1] == [cli.sys.executable, "-m", "pip", "install",
                        "--break-system-packages", "vibatchium==0.6.2"]
    assert "--break-system-packages" in note
