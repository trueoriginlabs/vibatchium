"""`vb start --browser-binary PATH` / VIBATCHIUM_BROWSER_BINARY.

Layered like test_device_scale: pure tests (validation, browser.json
persistence, resolution precedence, the agent-surface refusal, launch-kwargs
capture, backend dispatch, the self-heal re-read seam, the warm-claim guard) run
with no browser; the daemon tests prove the refusal end to end; one real launch
proves the plumbing by asking the browser which version it is.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from vibatchium import browser_binary as bb
from vibatchium import fspolicy
from vibatchium.client import call

AGENT_SCOPE = {"roots": None, "cwd": "/tmp"}


def _mk_async(val):
    async def _f(*a, **k):
        return val
    return _f


@pytest.fixture
def fake_bin(tmp_path):
    p = tmp_path / "fake-chrome"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    return p


@pytest.fixture
def agent_scope():
    tok = fspolicy.push_scope(AGENT_SCOPE)
    try:
        yield
    finally:
        fspolicy.pop_scope(tok)


@pytest.fixture(autouse=True)
def _no_env_default(monkeypatch):
    monkeypatch.delenv(bb.ENV_BROWSER_BINARY, raising=False)


@pytest.fixture(autouse=True)
def pins(monkeypatch, tmp_path_factory):
    """In-process tests never touch the real ~/.config/vibatchium/pins."""
    from vibatchium.daemon import paths
    root = tmp_path_factory.mktemp("pins")
    monkeypatch.setattr(paths, "PINS_DIR", root)
    return root


# ─── validation ─────────────────────────────────────────────────────────


def test_validate_accepts_an_executable_file(fake_bin):
    assert fspolicy.validate_executable(str(fake_bin)) == str(fake_bin)


def test_validate_keeps_the_symlink_path_not_its_target(fake_bin, tmp_path):
    # /usr/bin/chromium -> an alternatives target: pinning the link keeps
    # following package updates, pinning the target would freeze a version.
    link = tmp_path / "chromium"
    link.symlink_to(fake_bin)
    assert fspolicy.validate_executable(str(link)) == str(link)


@pytest.mark.parametrize("case,match", [
    ("missing", "does not exist"),
    ("dir", "not a regular file"),
    ("noexec", "not executable"),
    ("relative", "absolute"),
    ("empty", "empty"),
])
def test_validate_rejects(tmp_path, case, match):
    noexec = tmp_path / "noexec"
    noexec.write_text("x")
    noexec.chmod(0o644)
    path = {"missing": str(tmp_path / "nope"), "dir": str(tmp_path),
            "noexec": str(noexec), "relative": "chrome", "empty": ""}[case]
    with pytest.raises(ValueError, match=match):
        fspolicy.validate_executable(path)


def test_check_exec_passes_on_operator_surface(fake_bin):
    assert fspolicy.check_exec(str(fake_bin), verb="t") == str(fake_bin)


def test_check_exec_refused_on_agent_surface(fake_bin, agent_scope):
    # Even a perfectly valid binary, even with roots opted out (roots=None):
    # naming the program the daemon spawns is code execution.
    with pytest.raises(fspolicy.FileAccessDenied, match="operator-only"):
        fspolicy.check_exec(str(fake_bin), verb="start --browser-binary")


# ─── browser.json persistence ───────────────────────────────────────────


def test_save_load_round_trip_and_perms(tmp_path, fake_bin):
    bb.save_session_browser(tmp_path, str(fake_bin))
    assert bb.load_session_browser(tmp_path) == str(fake_bin)
    assert (bb.session_browser_path(tmp_path).stat().st_mode & 0o777) == 0o600


@pytest.mark.parametrize("clear", [None, ""])
def test_save_empty_removes_the_pin(tmp_path, fake_bin, clear):
    bb.save_session_browser(tmp_path, str(fake_bin))
    bb.save_session_browser(tmp_path, clear)
    assert not bb.session_browser_path(tmp_path).exists()


def test_save_creates_a_brand_new_profile_dir(tmp_path, fake_bin):
    fresh = tmp_path / "not-yet"
    bb.save_session_browser(fresh, str(fake_bin))
    assert bb.load_session_browser(fresh) == str(fake_bin)


@pytest.mark.parametrize("payload", ["42", "[1]", "null", "{bad", '{"path": 7}',
                                     '{"path": "relative/chrome"}', "{}"])
def test_load_corrupt_reads_as_unset(tmp_path, payload):
    prof = tmp_path / "s"
    prof.mkdir()
    bb.session_browser_path(prof, managed_root=tmp_path).write_text(payload)
    assert bb.load_session_browser(prof, managed_root=tmp_path) is None


# ─── resolution precedence ──────────────────────────────────────────────


def test_resolve_default_is_channel_chrome(tmp_path):
    assert bb.resolve_browser_binary(tmp_path, managed_root=tmp_path.parent) == \
        (None, None)


def test_resolve_env_default(tmp_path, fake_bin, monkeypatch):
    monkeypatch.setenv(bb.ENV_BROWSER_BINARY, str(fake_bin))
    assert bb.resolve_browser_binary(tmp_path, managed_root=tmp_path.parent) == \
        (str(fake_bin), "env")


def test_resolve_pin_beats_env(tmp_path, fake_bin, monkeypatch):
    other = tmp_path / "other"
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    monkeypatch.setenv(bb.ENV_BROWSER_BINARY, str(other))
    prof = tmp_path / "prof"
    bb.save_session_browser(prof, str(fake_bin), managed_root=tmp_path)
    assert bb.resolve_browser_binary(prof, managed_root=tmp_path) == \
        (str(fake_bin), "session")


def test_resolve_vanished_pin_fails_loudly_with_the_clear_hint(tmp_path, fake_bin):
    # Never silently run a different browser than the one pinned.
    prof = tmp_path / "prof"
    bb.save_session_browser(prof, str(fake_bin), managed_root=tmp_path)
    fake_bin.unlink()
    with pytest.raises(ValueError, match="start --browser-binary ''"):
        bb.resolve_browser_binary(prof, name="s", managed_root=tmp_path)


def test_vanished_pin_hint_names_the_custom_profile(tmp_path, fake_bin):
    # The clear has to reach the same (unmanaged) pin it complains about.
    managed = tmp_path / "managed"
    managed.mkdir()
    prof = tmp_path / "custom"
    bb.save_session_browser(prof, str(fake_bin), managed_root=managed)
    fake_bin.unlink()
    with pytest.raises(ValueError, match=f"--profile {prof} --browser-binary ''"):
        bb.resolve_browser_binary(prof, name="s", managed_root=managed)


@pytest.mark.parametrize("val", ["chrome", "/nonexistent/chrome"])
def test_resolve_bad_env_fails_loudly(tmp_path, monkeypatch, val):
    monkeypatch.setenv(bb.ENV_BROWSER_BINARY, val)
    with pytest.raises(ValueError, match=bb.ENV_BROWSER_BINARY):
        bb.resolve_browser_binary(tmp_path, managed_root=tmp_path.parent)


def test_managed_profile_keeps_its_pin_in_the_profile_dir(tmp_path, fake_bin, pins):
    # PROFILES_DIR is off-limits to every caller path, so browser.json there
    # can only have been written by the operator.
    prof = tmp_path / "s"
    bb.save_session_browser(prof, str(fake_bin), managed_root=tmp_path)
    assert (prof / "browser.json").exists()
    assert not any(pins.iterdir()), "managed pin leaked into the pins store"
    assert bb.resolve_browser_binary(prof, managed_root=tmp_path) == \
        (str(fake_bin), "session")


def test_unmanaged_profile_pin_lives_in_the_operator_only_store(tmp_path, fake_bin,
                                                                pins):
    managed = tmp_path / "managed"
    managed.mkdir()
    prof = tmp_path / "caller-chosen"
    bb.save_session_browser(prof, str(fake_bin), managed_root=managed)
    assert not (prof / "browser.json").exists(), \
        "an operator pin was written where a caller can rewrite it"
    [entry] = list(pins.iterdir())
    assert (entry / "browser.json").exists()
    assert (entry / "profile").read_text() == os.path.realpath(prof)
    assert (entry.stat().st_mode & 0o777) == 0o700
    assert ((entry / "browser.json").stat().st_mode & 0o777) == 0o600
    assert bb.resolve_browser_binary(prof, managed_root=managed) == \
        (str(fake_bin), "session")
    bb.save_session_browser(prof, "", managed_root=managed)
    assert bb.resolve_browser_binary(prof, managed_root=managed) == (None, None)


@pytest.mark.parametrize("scoped", [False, True])
def test_planted_pin_in_a_caller_chosen_dir_is_ignored_on_every_surface(
        tmp_path, fake_bin, scoped, caplog):
    # An agent can `start --profile /tmp/x` and get bytes into /tmp/x
    # (download_save). Trust follows who WROTE the file, not who triggers the
    # launch: an operator start or a self-heal relaunch must not honour it
    # either.
    managed = tmp_path / "managed"
    managed.mkdir()
    prof = tmp_path / "caller-chosen"
    prof.mkdir()
    (prof / "browser.json").write_text(json.dumps({"path": str(fake_bin)}))
    tok = fspolicy.push_scope(AGENT_SCOPE) if scoped else None
    try:
        with caplog.at_level("WARNING", logger="vibatchium.browser_binary"):
            assert bb.resolve_browser_binary(prof, name="s",
                                             managed_root=managed) == (None, None)
    finally:
        if tok is not None:
            fspolicy.pop_scope(tok)
    assert "ignoring browser.json" in caplog.text


def test_symlink_into_profiles_dir_is_judged_by_realpath(tmp_path, fake_bin):
    managed = tmp_path / "managed"
    (managed / "s").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = managed / "link"
    link.symlink_to(outside)
    (outside / "browser.json").write_text(json.dumps({"path": str(fake_bin)}))
    assert bb.resolve_browser_binary(link, managed_root=managed) == (None, None)


def test_agent_surface_honours_the_env_default(tmp_path, fake_bin, monkeypatch,
                                               agent_scope):
    monkeypatch.setenv(bb.ENV_BROWSER_BINARY, str(fake_bin))
    assert bb.resolve_browser_binary(tmp_path / "x", managed_root=tmp_path / "m")[0] \
        == str(fake_bin)


# ─── launch kwargs (no real Chrome) ─────────────────────────────────────


class _FakePage:
    def on(self, *a, **k):
        pass


class _FakeContext:
    def __init__(self):
        self.pages = [_FakePage()]
        self.browser = SimpleNamespace(version="148.0.1.2")


class _FakeChromium:
    def __init__(self, sink):
        self._sink = sink

    async def launch_persistent_context(self, **kw):
        self._sink.clear()
        self._sink.update(kw)
        return _FakeContext()


class _FakePw:
    def __init__(self, sink):
        self.chromium = _FakeChromium(sink)


async def _capture(monkeypatch, tmp_path, **kw):
    from vibatchium.daemon import browser as B
    sink, probes = {}, []

    async def _ua(pw, executable_path=None):
        probes.append(executable_path)
        return "Mozilla/5.0 TestUA"

    monkeypatch.setattr(B, "coherent_headless_ua", _ua)
    monkeypatch.setattr(B, "_wire_page_tracking", lambda s: None)
    sess = await B.launch_session(tmp_path, headless=True, pw=_FakePw(sink), **kw)
    return sink, sess, probes


async def test_default_launch_keeps_channel_chrome(monkeypatch, tmp_path):
    sink, sess, probes = await _capture(monkeypatch, tmp_path)
    assert sink["channel"] == "chrome"
    assert "executable_path" not in sink
    assert sess.browser_binary is None
    assert probes == [None]


async def test_binary_launch_swaps_channel_for_executable_path(monkeypatch, tmp_path):
    sink, sess, probes = await _capture(monkeypatch, tmp_path,
                                        executable_path="/opt/x/chrome")
    assert "channel" not in sink
    assert sink["executable_path"] == "/opt/x/chrome"
    assert sess.browser_binary == "/opt/x/chrome"
    assert sess.browser_version == "148.0.1.2"
    # The de-Headless'd UA must come from THIS binary, not channel Chrome.
    assert probes == ["/opt/x/chrome"]


async def test_ua_probe_is_cached_per_binary(monkeypatch):
    from vibatchium.daemon import browser as B
    monkeypatch.setattr(B, "_HEADLESS_UA_CACHE", {})
    launched = []

    class _Br:
        def __init__(self, ua):
            self._ua = ua

        async def new_page(self):
            ua = self._ua

            class _P:
                async def evaluate(self, _js):
                    return ua
            return _P()

        async def close(self):
            pass

    class _Chromium:
        async def launch(self, **kw):
            launched.append(kw)
            ver = "148" if kw.get("executable_path") else "153"
            return _Br(f"Mozilla/5.0 HeadlessChrome/{ver}.0")

    pw = SimpleNamespace(chromium=_Chromium())
    assert await B.coherent_headless_ua(pw) == "Mozilla/5.0 Chrome/153.0"
    assert await B.coherent_headless_ua(pw, "/opt/x/chrome") == "Mozilla/5.0 Chrome/148.0"
    assert await B.coherent_headless_ua(pw) == "Mozilla/5.0 Chrome/153.0"
    assert await B.coherent_headless_ua(pw, "/opt/x/chrome") == "Mozilla/5.0 Chrome/148.0"
    assert len(launched) == 2  # one probe per binary, then cached
    assert launched[0].get("channel") == "chrome"
    assert launched[1].get("executable_path") == "/opt/x/chrome"
    assert "channel" not in launched[1]


async def test_sandbox_abort_gets_an_actionable_error(monkeypatch, tmp_path):
    from vibatchium.daemon import browser as B

    class _Chromium:
        async def launch_persistent_context(self, **kw):
            raise RuntimeError("[err] FATAL:zygote_host_impl_linux.cc No usable "
                               "sandbox! ... a page of stack")

    monkeypatch.setattr(B, "coherent_headless_ua", _mk_async(None))
    with pytest.raises(RuntimeError, match="VIBATCHIUM_DISABLE_SANDBOX"):
        await B.launch_session(tmp_path, headless=True,
                               pw=SimpleNamespace(chromium=_Chromium()),
                               executable_path="/opt/x/chrome")


# ─── backend dispatch ───────────────────────────────────────────────────


async def test_backends_thread_binary_to_patchright(monkeypatch, tmp_path):
    from vibatchium.daemon import backends as B
    seen = {}

    async def _fake(profile_dir, **kw):
        seen.update(kw)
        return SimpleNamespace()

    monkeypatch.setattr(B, "launch_patchright_session", _fake)
    await B.launch("patchright", tmp_path, headless=True,
                   executable_path="/opt/x/chrome", executable_source="session")
    assert seen["executable_path"] == "/opt/x/chrome"


async def test_nodriver_refuses_a_session_pin(monkeypatch, tmp_path):
    from vibatchium.daemon import backends as B
    monkeypatch.setattr(B, "launch_nodriver_session", _mk_async(SimpleNamespace()))
    with pytest.raises(ValueError, match="patchright-only"):
        await B.launch("nodriver", tmp_path, headless=True,
                       executable_path="/opt/x/chrome", executable_source="session")


async def test_nodriver_skips_the_env_default(monkeypatch, tmp_path):
    # A daemon-wide default must not break every nodriver session.
    from vibatchium.daemon import backends as B
    seen = {}

    async def _fake(profile_dir, **kw):
        seen.update(kw)
        return SimpleNamespace()

    monkeypatch.setattr(B, "launch_nodriver_session", _fake)
    await B.launch("nodriver", tmp_path, headless=True,
                   executable_path="/opt/x/chrome", executable_source="env")
    assert "executable_path" not in seen


# ─── registry: self-heal re-read, warm-claim guard, no auto-install ─────


async def test_launch_for_rereads_browser_json_on_relaunch(monkeypatch, tmp_path,
                                                          fake_bin):
    from vibatchium.daemon import backends as _backends
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    monkeypatch.setattr(reg, "_ensure_pw", _mk_async(object()))
    seen = {}

    async def _fake_launch(backend, profile_dir, **kw):
        seen.clear()
        seen.update(kw)
        return SimpleNamespace(mode="launch")

    monkeypatch.setattr(_backends, "launch", _fake_launch)
    bb.save_session_browser(tmp_path, str(fake_bin))
    await reg._launch_for("s", profile_dir=tmp_path, headless=True,
                          backend="patchright")
    assert seen["executable_path"] == str(fake_bin)
    assert seen["executable_source"] == "session"

    bb.save_session_browser(tmp_path, None)
    await reg._launch_for("s", profile_dir=tmp_path, headless=True,
                          backend="patchright")
    assert "executable_path" not in seen


async def test_custom_binary_never_triggers_chrome_auto_install(monkeypatch, tmp_path,
                                                               fake_bin):
    from vibatchium.daemon import backends as _backends
    from vibatchium.daemon import registry as R
    reg = R.SessionRegistry()
    monkeypatch.setattr(reg, "_ensure_pw", _mk_async(object()))
    called = []

    async def _fake_install(exc):
        called.append(exc)
        return True

    async def _boom(backend, profile_dir, **kw):
        raise RuntimeError("Executable doesn't exist at /opt/x/chrome")

    monkeypatch.setattr(R, "_maybe_autoinstall_chrome", _fake_install)
    monkeypatch.setattr(_backends, "launch", _boom)
    bb.save_session_browser(tmp_path, str(fake_bin))
    with pytest.raises(RuntimeError, match="Executable"):
        await reg._launch_for("s", profile_dir=tmp_path, headless=True,
                              backend="patchright")
    assert called == []


async def test_pinned_request_never_claims_a_channel_prewarm(monkeypatch, tmp_path,
                                                            fake_bin):
    from vibatchium.daemon import backends as _backends
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    warm = SimpleNamespace(profile_dir=tmp_path, headless=True, mode="launch",
                           browser_binary=None)
    fresh = SimpleNamespace(profile_dir=tmp_path, headless=True, mode="launch")
    reg._warm_sessions["s"] = warm
    closed = []

    async def _fake_close(sess):
        closed.append(sess)

    monkeypatch.setattr(_backends, "close", _fake_close)
    monkeypatch.setattr(reg, "_launch_for", _mk_async(fresh))
    bb.save_session_browser(tmp_path, str(fake_bin))
    entry = await reg.create("s", profile_dir=tmp_path, headless=True)
    assert entry.session is fresh
    assert closed == [warm]


async def test_matching_prewarm_is_still_claimed(monkeypatch, tmp_path, fake_bin):
    from vibatchium.daemon.registry import SessionRegistry
    reg = SessionRegistry()
    bb.save_session_browser(tmp_path, str(fake_bin))
    warm = SimpleNamespace(profile_dir=tmp_path, headless=True, mode="launch",
                           browser_binary=str(fake_bin))
    reg._warm_sessions["s"] = warm
    entry = await reg.create("s", profile_dir=tmp_path, headless=True)
    assert entry.session is warm


# ─── CLI ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("given,want", [
    ("rel/chrome", os.path.join("{cwd}", "rel/chrome")),
    ("/opt/x/chrome", "/opt/x/chrome"),
    ("", ""),
])
def test_cli_absolutizes_before_sending(monkeypatch, tmp_path, given, want):
    # The daemon's cwd isn't the caller's, so the daemon only takes absolute
    # paths — the CLI resolves a relative one against the caller's cwd.
    from click.testing import CliRunner
    from vibatchium import cli as C
    sent = {}
    monkeypatch.setattr(C, "call", lambda cmd, args, **k: sent.update(args) or {})
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(C.cli, ["start", "--browser-binary", given])
    assert r.exit_code == 0, r.output
    assert sent["browser_binary"] == want.format(cwd=str(tmp_path))


def test_cli_bare_start_sends_no_binary(monkeypatch):
    from click.testing import CliRunner
    from vibatchium import cli as C
    sent = {}
    monkeypatch.setattr(C, "call", lambda cmd, args, **k: sent.update(args) or {})
    assert CliRunner().invoke(C.cli, ["start"]).exit_code == 0
    assert "browser_binary" not in sent


# ─── MCP surface ────────────────────────────────────────────────────────


def test_not_offered_in_the_mcp_start_schema():
    # Always refused over MCP, so offering it would only cost tokens and invite
    # an agent to try.
    from vibatchium.mcp_server import TOOLS
    schema = next(t[2] for t in TOOLS if t[0] == "start")
    assert "browser_binary" not in schema["properties"]


# ─── through the daemon ─────────────────────────────────────────────────


def _cleanup(name):
    for verb in ("session_close", "session_delete"):
        try:
            call(verb, {"name": name})
        except Exception:  # noqa: BLE001
            pass


def test_daemon_refuses_browser_binary_from_an_agent_surface(fake_bin):
    from vibatchium.daemon.paths import PROFILES_DIR
    name = "bbin_agent"
    try:
        call("session_new", {"name": name, "prewarm": False})
        for val in (str(fake_bin), ""):
            with pytest.raises(Exception, match="operator-only"):
                call("start", {"headless": True, "browser_binary": val,
                               fspolicy.SCOPE_ARG: AGENT_SCOPE}, session=name)
        assert not bb.session_browser_path(PROFILES_DIR / name).exists()
    finally:
        _cleanup(name)


@pytest.mark.parametrize("scoped", [True, False])
def test_daemon_ignores_a_planted_pin(tmp_path, fake_bin, scoped):
    # A browser.json planted in a caller-chosen profile names a "browser" that
    # exits at once: honouring it would fail the launch. Ignored, the session
    # comes up on channel Chrome — from an agent surface AND from the CLI.
    name = f"bbin_plant_{int(scoped)}"
    prof = tmp_path / "prof"
    prof.mkdir()
    (prof / "browser.json").write_text(json.dumps({"path": str(fake_bin)}))
    args = {"headless": True, "profile": str(prof)}
    if scoped:
        args[fspolicy.SCOPE_ARG] = {"roots": [str(tmp_path)], "cwd": str(tmp_path)}
    try:
        res = call("start", args, session=name)
        assert res.get("started") is True, res
        assert res.get("browser_binary") is None, res
    finally:
        _cleanup(name)


def test_daemon_rejects_a_missing_binary(tmp_path):
    name = "bbin_missing"
    try:
        call("session_new", {"name": name, "prewarm": False})
        with pytest.raises(Exception, match="does not exist"):
            call("start", {"headless": True,
                           "browser_binary": str(tmp_path / "nope")}, session=name)
    finally:
        _cleanup(name)


def _real_chromiums() -> list[str]:
    """Chromium executables on this host, best proof first: Playwright's own
    Chromium builds (a different version than channel Chrome proves the swap),
    then the branded Chrome's actual executable (what channel="chrome" runs)."""
    home = Path.home()
    out = []
    for pat in (str(home / ".cache/ms-playwright/chromium-*/chrome-linux*/chrome"),
                str(home / "Library/Caches/ms-playwright/chromium-*/chrome-mac*/"
                           "Chromium.app/Contents/MacOS/Chromium")):
        out += [p for p in sorted(glob.glob(pat), reverse=True)
                if os.access(p, os.X_OK)]
    out += [p for p in ("/opt/google/chrome/chrome",
                        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
            if os.access(p, os.X_OK)]
    return out


def _version_of(binary: str) -> str:
    out = subprocess.run([binary, "--version"], capture_output=True, text=True,
                         timeout=30).stdout
    m = re.search(r"\d+\.\d+\.\d+\.\d+", out)
    assert m, f"can't read {binary} --version: {out!r}"
    return m.group(0)


def test_live_launch_with_a_real_binary():
    name = "bbin_live"
    try:
        call("session_new", {"name": name, "prewarm": False})
        res = binary = None
        for cand in _real_chromiums():
            try:
                res = call("start", {"headless": True, "browser_binary": cand},
                           session=name)
            except Exception as exc:  # noqa: BLE001
                # A Chrome for Testing build can't sandbox under Ubuntu's
                # AppArmor userns restriction. The error must SAY so (the hint
                # launch_session adds) — then try the next candidate.
                if "sandbox" in str(exc):
                    assert "VIBATCHIUM_DISABLE_SANDBOX" in str(exc), str(exc)[:400]
                    continue
                raise
            binary = cand
            break
        if binary is None:
            pytest.skip("no launchable Chromium executable on this host")
        assert res.get("started") is True, res
        assert res["browser_binary"] == binary, res
        # The browser itself reports which build ran.
        want_version = _version_of(binary)
        assert res["browser_version"] == want_version, res
        ua = call("eval", {"expr": "navigator.userAgent"}, session=name)
        ua = str(ua.get("value") if isinstance(ua, dict) else ua)
        assert "HeadlessChrome" not in ua, ua
        assert f"Chrome/{want_version.split('.')[0]}." in ua, ua

        # A live browser can't switch binaries: say so, don't pretend.
        res2 = call("start", {"headless": True, "browser_binary": ""}, session=name)
        assert res2.get("already_started") is True
        assert res2["browser_binary"] == binary
        assert res2.get("browser_binary_pending", "x") is None
        assert "close + start" in (res2.get("note") or "")
    finally:
        _cleanup(name)
