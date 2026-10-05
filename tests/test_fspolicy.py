"""fspolicy — confinement of caller-supplied filesystem paths.

Unit tests run with HOME pointed at a tmp dir (fspolicy resolves ~ at call
time), so nothing here reads or writes the real home. The handler-level tests
call daemon handlers in-process (the policy fires before any session lookup)
and, for the end-to-end pair, go through the conftest's isolated daemon with
targets that are harmless even if the policy were broken.
"""
from __future__ import annotations

import os

import pytest

from vibatchium import fspolicy
from vibatchium.fspolicy import FileAccessDenied, check_read, check_write


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".ssh" / "id_ed25519").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv(fspolicy.ENV_ROOTS, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("VIBATCHIUM_VAULT_PATH", raising=False)
    return h


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "work"
    w.mkdir()
    return w


# ─── deny list ──────────────────────────────────────────────────────────

def test_denied_is_a_permission_error(home):
    with pytest.raises(PermissionError, match=r"~/\.ssh"):
        check_read(home / ".ssh" / "id_ed25519")


def test_tilde_is_expanded(home):
    with pytest.raises(FileAccessDenied, match="file access denied"):
        check_read("~/.ssh/id_ed25519", verb="upload")


@pytest.mark.parametrize("rel", [
    ".gnupg/secring.gpg", ".aws/credentials", ".config/gcloud/creds.db",
    ".kube/config", ".docker/config.json", ".netrc", ".pgpass",
    ".git-credentials", ".config/vibatchium/secrets.key",
    ".config/vibatchium/profiles/default/Cookies",
    ".config/google-chrome/Default/Login Data", ".mozilla/firefox/x/key4.db",
    ".config/BraveSoftware/Brave-Browser/Default/Cookies",
    ".config/chromium/Default/Cookies", ".local/share/keyrings/login.keyring",
])
def test_sensitive_home_locations_denied_both_ways(home, rel):
    with pytest.raises(FileAccessDenied):
        check_read(home / rel)
    with pytest.raises(FileAccessDenied):
        check_write(home / rel)


@pytest.mark.parametrize("p", ["/etc/shadow", "/etc/sudoers", "/etc/sudoers.d/x",
                               "/proc/self/environ", "/sys/kernel/x", "/dev/zero"])
def test_system_locations_denied(home, p):
    with pytest.raises(FileAccessDenied):
        check_read(p)


@pytest.mark.parametrize("rel", [
    ".bashrc", ".profile", ".zshrc", ".bash_profile",
    ".config/autostart/evil.desktop", ".config/systemd/user/evil.service",
    ".local/bin/vb",
])
def test_rc_and_autostart_write_denied_but_readable(home, rel):
    target = home / rel
    with pytest.raises(FileAccessDenied, match="shell rc"):
        check_write(target)
    # Reading your own .bashrc into an upload is not the threat model.
    assert check_read(target) == os.path.realpath(target)


def test_system_dirs_write_denied(home):
    with pytest.raises(FileAccessDenied):
        check_write("/etc/passwd")
    with pytest.raises(FileAccessDenied):
        check_write("/var/spool/cron/crontabs/me")
    assert check_read("/etc/hostname") == os.path.realpath("/etc/hostname")


def test_relocated_vault_denied(home, work, monkeypatch):
    vault = work / "vault" / "secrets.enc"
    monkeypatch.setenv("VIBATCHIUM_VAULT_PATH", str(vault))
    with pytest.raises(FileAccessDenied, match="VIBATCHIUM_VAULT_PATH"):
        check_read(vault)


def test_xdg_config_home_browser_profile_denied(home, work, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(work / "xdg"))
    with pytest.raises(FileAccessDenied):
        check_read(work / "xdg" / "google-chrome" / "Default" / "Cookies")


# ─── symlinks, traversal, relative paths ────────────────────────────────

def test_symlink_file_escape_into_ssh(home, work):
    link = work / "innocent.txt"
    link.symlink_to(home / ".ssh" / "id_ed25519")
    with pytest.raises(FileAccessDenied, match="resolves to"):
        check_read(link)


def test_symlinked_dir_escape_into_ssh(home, work):
    (work / "keys").symlink_to(home / ".ssh")
    with pytest.raises(FileAccessDenied):
        check_read(work / "keys" / "id_ed25519")


def test_ssh_dir_itself_a_symlink_elsewhere(home, tmp_path):
    """Lexical check: ~/.ssh -> /data/ssh still protects ~/.ssh/<key>."""
    real_ssh = tmp_path / "data-ssh"
    real_ssh.mkdir()
    (real_ssh / "id_rsa").write_text("k")
    (home / ".ssh" / "id_ed25519").unlink()
    (home / ".ssh").rmdir()
    (home / ".ssh").symlink_to(real_ssh)
    with pytest.raises(FileAccessDenied):
        check_read(home / ".ssh" / "id_rsa")


def test_dotdot_traversal(home, work):
    sneaky = f"{work}/sub/../../home/.ssh/id_ed25519"
    with pytest.raises(FileAccessDenied):
        check_read(sneaky)


def test_relative_paths_resolve_against_cwd(home, work, monkeypatch):
    monkeypatch.chdir(home)
    with pytest.raises(FileAccessDenied):
        check_read(".ssh/id_ed25519")
    monkeypatch.chdir(work)
    assert check_write("out.png") == os.path.join(os.path.realpath(work), "out.png")


def test_write_target_and_parent_need_not_exist(home, work):
    target = work / "new" / "deeper" / "out.pdf"
    assert check_write(target) == os.path.realpath(target)


def test_dangling_symlink_write_followed(home, work):
    """A dangling link to ~/.bashrc must not be writable through."""
    (work / "out.png").symlink_to(home / ".bashrc")       # .bashrc doesn't exist
    with pytest.raises(FileAccessDenied):
        check_write(work / "out.png")


def test_nonexistent_parent_via_symlinked_dir(home, work):
    """Parent resolution: link -> ~/.config/systemd/user (absent) still caught."""
    (work / "units").symlink_to(home / ".config" / "systemd" / "user")
    with pytest.raises(FileAccessDenied):
        check_write(work / "units" / "evil.service")


def test_normal_paths_pass_and_return_realpath(home, work):
    f = work / "photo.jpg"
    f.write_bytes(b"x")
    assert check_read(f) == os.path.realpath(f)
    assert check_read(home / "Documents" / "cv.pdf")     # plain home file: fine


def test_empty_path_rejected(home):
    with pytest.raises(ValueError):
        check_read("")


# ─── strict roots mode ──────────────────────────────────────────────────

def test_roots_confine(home, work, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(fspolicy.ENV_ROOTS, str(work))
    assert check_write(work / "a" / "out.pdf")
    with pytest.raises(FileAccessDenied, match="VIBATCHIUM_FILE_ROOTS"):
        check_write(other / "out.pdf")
    with pytest.raises(FileAccessDenied, match="VIBATCHIUM_FILE_ROOTS"):
        check_read(f"{work}/../other/x")


def test_roots_multiple_entries(home, work, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(fspolicy.ENV_ROOTS, os.pathsep.join([str(work), "", str(other)]))
    assert check_read(other / "f")
    assert check_read(work / "f")
    with pytest.raises(FileAccessDenied):
        check_read(tmp_path / "elsewhere")


def test_roots_symlink_out_of_root_denied(home, work, tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    (work / "link").symlink_to(outside)
    monkeypatch.setenv(fspolicy.ENV_ROOTS, str(work))
    with pytest.raises(FileAccessDenied):
        check_read(work / "link")


def test_deny_wins_over_roots(home, monkeypatch):
    monkeypatch.setenv(fspolicy.ENV_ROOTS, str(home))
    assert check_read(home / "notes.txt")
    with pytest.raises(FileAccessDenied, match="not overridable"):
        check_read(home / ".ssh" / "id_ed25519")


def test_empty_roots_env_is_off(home, tmp_path, monkeypatch):
    monkeypatch.setenv(fspolicy.ENV_ROOTS, "")
    assert check_read(tmp_path / "anything")


# ─── upload list + profile dirs ─────────────────────────────────────────

def test_check_upload_list(home, work):
    ok = work / "ok.txt"
    ok.write_text("x")
    assert fspolicy.check_upload([str(ok)]) == [os.path.realpath(ok)]
    with pytest.raises(FileAccessDenied):
        fspolicy.check_upload([str(ok), "~/.ssh/id_ed25519"])
    payload = {"name": "a.txt", "mimeType": "text/plain", "buffer": "eA=="}
    assert fspolicy.check_upload([payload]) == [payload]


def test_check_upload_directory_with_smuggling_symlink(home, work):
    d = work / "upload-me"
    d.mkdir()
    (d / "fine.txt").write_text("x")
    (d / "sneaky").symlink_to(home / ".ssh" / "id_ed25519")
    with pytest.raises(FileAccessDenied):
        fspolicy.check_upload([str(d)])


def test_check_profile_dir(home, work):
    managed = home / ".config" / "vibatchium" / "profiles"
    managed.mkdir(parents=True)
    assert fspolicy.check_profile_dir(managed / "work", managed_root=managed)
    assert fspolicy.check_profile_dir(work / "prof", managed_root=managed)
    with pytest.raises(FileAccessDenied):
        fspolicy.check_profile_dir(managed / ".." / ".." / ".." / ".ssh",
                                   managed_root=managed)
    with pytest.raises(FileAccessDenied):
        fspolicy.check_profile_dir(managed / ".." / "secrets",
                                   managed_root=managed)
    with pytest.raises(FileAccessDenied):
        fspolicy.check_profile_dir(home / ".config" / "google-chrome",
                                   managed_root=managed)


# ─── handler level, in-process (no Chrome: the check precedes the session) ──

def _daemon():
    from vibatchium.daemon.server import Daemon
    return Daemon()


async def test_upload_handler_refuses_ssh_key(home, work):
    d = _daemon()
    with pytest.raises(FileAccessDenied, match=r"upload read .*~/\.ssh"):
        await d._handlers["upload"](d, {"target": "#file",
                                        "files": ["~/.ssh/id_ed25519"]})
    link = work / "cv.pdf"
    link.symlink_to(home / ".ssh" / "id_ed25519")
    with pytest.raises(FileAccessDenied):
        await d._handlers["upload"](d, {"target": "#file", "files": str(link)})


@pytest.mark.parametrize("verb,args", [
    ("pdf", {"path": "~/.bashrc"}),
    ("download_save", {"index": 0, "path": "~/.ssh/authorized_keys"}),
    ("record_stop", {"path": "~/.config/autostart/x.desktop"}),
])
async def test_write_handlers_refuse(home, verb, args):
    d = _daemon()
    with pytest.raises(FileAccessDenied):
        await d._handlers[verb](d, dict(args))
    assert not (home / ".bashrc").exists()


async def test_proxy_set_and_skill_import_refuse(home, work):
    d = _daemon()
    with pytest.raises(FileAccessDenied):
        await d._handlers["proxy_set"](d, {"path": "~/.ssh/id_ed25519"})
    with pytest.raises(FileAccessDenied):
        await d._handlers["skill_import"](d, {"source": "~/.ssh"})


async def test_skill_import_skips_note_symlinked_out_of_source(home, work, tmp_path,
                                                               monkeypatch):
    from vibatchium.skills import store
    monkeypatch.setattr(store, "SKILLS_DIR", tmp_path / "skills")
    src = work / "domain-skills"
    (src / "kayak.com").mkdir(parents=True)
    (src / "kayak.com" / "ok.md").write_text("Prefer the search box.")
    outside = tmp_path / "private.md"
    outside.write_text("private notes")
    (src / "kayak.com" / "leak.md").symlink_to(outside)
    d = _daemon()
    res = await d._handlers["skill_import"](d, {"source": str(src)})
    assert "kayak.com/ok.md" in res["imported"]
    assert "kayak.com/leak.md" not in res["imported"]
    assert any("escapes" in s.get("reason", "") for s in res["skipped"])


# ─── end to end through the isolated test daemon (real Chrome) ─────────
# Targets are harmless if the policy regressed: a nonexistent key path (the
# upload would just fail to find it) and a symlink to /dev/null.

def test_e2e_upload_allowed_and_denied(local_server, tmp_path):
    from vibatchium.client import DaemonError, call
    call("go", {"url": f"{local_server}/simple.html"})
    f = tmp_path / "hello.txt"
    f.write_text("hi")
    res = call("upload", {"target": "#file", "files": [str(f)]})
    assert res["uploaded"] == [str(f)]
    name = call("eval", {"expr": "document.querySelector('#file').files[0].name"})
    assert "hello.txt" in str(name)
    with pytest.raises(DaemonError, match="file access denied"):
        call("upload", {"target": "#file",
                        "files": [os.path.expanduser("~/.ssh/vbtest-no-such-key")]})


def test_e2e_pdf_allowed_and_symlink_escape_denied(local_server, tmp_path):
    from vibatchium.client import DaemonError, call
    call("go", {"url": f"{local_server}/simple.html"})
    out = tmp_path / "page.pdf"
    call("pdf", {"path": str(out)})
    assert out.read_bytes()[:4] == b"%PDF"
    link = tmp_path / "evil.pdf"
    link.symlink_to("/dev/null")
    with pytest.raises(DaemonError, match="file access denied"):
        call("pdf", {"path": str(link)})


def test_e2e_screenshot_tile_dir_denied(local_server, tmp_path):
    from vibatchium.client import DaemonError, call
    call("go", {"url": f"{local_server}/simple.html"})
    (tmp_path / "proc").symlink_to("/proc/self")
    with pytest.raises(DaemonError, match="file access denied"):
        call("screenshot", {"tiles": True, "tile_dir": str(tmp_path / "proc")})


# ═══ 0.19.4 remediation: one regression block per review finding ═══════

@pytest.fixture
def clean_xdg(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("TMPDIR", raising=False)


# ─── finding 1: navigation to file: / local-ish schemes ─────────────────

@pytest.mark.parametrize("url", [
    "file://{home}/.ssh/id_ed25519",
    "file://{home}/.ssh/",                          # directory listing
    "file://{home}/.ssh",
    "FILE://{home}/.ssh/id_ed25519",                # scheme case
    "  file://{home}/.ssh/id_ed25519",              # leading C0/space stripped
    "fi\tle://{home}/.ssh/id_ed25519",              # tab removed by WHATWG
    "file://localhost{home}/.ssh/id_ed25519",
    "file://{home}/%2Essh/id_ed25519",              # percent-encoded dot
    "file://{home}/.ssh/id_ed25519?x=1#frag",       # query/fragment ignored
    "file://{home}\\.ssh\\id_ed25519",              # backslashes are slashes
    "file://{home}/.SSH/id_ed25519",                # case-folded deny
    "file:///proc/self/environ",
    "file://otherhost/etc/hostname",                # remote file host
    "view-source:file://{home}/.ssh/id_ed25519",
    "view-source:https://example.com",
    "chrome://settings/passwords", "chrome://version", "devtools://devtools/x",
    "filesystem:https://example.com/temporary/x", "javascript:alert(1)",
    "chrome-extension://abc/x.html", "about:config",
])
def test_nav_url_refused(home, url):
    with pytest.raises(FileAccessDenied):
        fspolicy.check_nav_url(url.format(home=home))


@pytest.mark.parametrize("url", [
    "https://example.com/a?b#c", "http://127.0.0.1:8080/", "HTTPS://EXAMPLE.COM",
    "data:text/html,<b>x</b>", "blob:https://example.com/uuid", "about:blank",
    "about:blank#x", "about:srcdoc", "chrome://crash", "example.com", "",
])
def test_nav_url_allowed(home, url):
    assert fspolicy.check_nav_url(url) is None


def test_nav_file_url_in_normal_dir_allowed(home, work):
    page = work / "page.html"
    page.write_text("<p>hi</p>")
    fspolicy.check_nav_url(page.as_uri())
    fspolicy.check_nav_url(work.as_uri() + "/")           # listing a work dir


def test_nav_file_url_obeys_scope_roots(home, work, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    tok = fspolicy.push_scope({"roots": [str(work)], "cwd": str(work)})
    try:
        fspolicy.check_nav_url((work / "a.html").as_uri())
        with pytest.raises(FileAccessDenied, match="file roots"):
            fspolicy.check_nav_url((other / "a.html").as_uri())
    finally:
        fspolicy.pop_scope(tok)


@pytest.mark.parametrize("verb", ["go", "explore"])
async def test_go_and_explore_refuse_file_url_before_session(home, verb):
    d = _daemon()
    with pytest.raises(FileAccessDenied):
        await d._handlers[verb](d, {"url": f"file://{home}/.ssh/id_ed25519"})
    assert d.registry.get("default") is None        # no Chrome was spent


def test_e2e_go_file_url(local_server, tmp_path):
    """Positive control + refusal through the isolated daemon. The refused
    target is a tmp symlink to /dev/null — harmless if the policy regressed."""
    from vibatchium.client import DaemonError, call
    page = tmp_path / "local.html"
    page.write_text("<html><body><p>" + "local file ok " * 20 + "</p></body></html>")
    call("go", {"url": page.as_uri()})
    assert "local file ok" in call("text", {})["text"]
    (tmp_path / "null.html").symlink_to("/dev/null")
    with pytest.raises(DaemonError, match="file access denied"):
        call("go", {"url": (tmp_path / "null.html").as_uri()})
    with pytest.raises(DaemonError, match="navigation refused"):
        call("go", {"url": "view-source:" + page.as_uri()})
    with pytest.raises(DaemonError, match="file access denied"):
        call("storage_restore", {"state": {"cookies": [], "origins": [
            {"origin": (tmp_path / "null.html").as_uri(),
             "localStorage": [{"name": "a", "value": "b"}]}]}})


# ─── finding 2a: agent-surface default roots ────────────────────────────

def test_default_agent_roots(home, work, clean_xdg, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(work / "t"))
    roots = fspolicy.default_agent_roots(str(work))
    real = os.path.realpath
    assert real(work) in roots and real("/tmp") in roots
    assert real(work / "t") in roots
    assert real(home / "Downloads") in roots
    assert any(r.endswith(os.sep + "screenshots") for r in roots)
    assert any(r.endswith(os.sep + "explores") for r in roots)
    # a cwd that covers all of $HOME (or /) is never a root
    assert real(home) not in fspolicy.default_agent_roots(str(home))
    assert "/" not in fspolicy.default_agent_roots("/")


def test_agent_scope_env(home, work, monkeypatch):
    monkeypatch.delenv(fspolicy.ENV_ROOTS, raising=False)
    sc = fspolicy.agent_scope(str(work))
    assert sc["cwd"] == str(work) and os.path.realpath(work) in sc["roots"]
    monkeypatch.setenv(fspolicy.ENV_ROOTS, "*")
    assert fspolicy.agent_scope(str(work))["roots"] is None       # opt-out
    monkeypatch.setenv(fspolicy.ENV_ROOTS, str(home / "only"))
    assert fspolicy.agent_scope(str(work))["roots"] == [os.path.realpath(home / "only")]


def test_daemon_env_roots_star_is_off(home, tmp_path, monkeypatch):
    monkeypatch.setenv(fspolicy.ENV_ROOTS, "*")
    assert fspolicy.roots() == []
    assert check_write(tmp_path / "x.png")


def test_scope_confines_and_resolves_relative(home, work, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    tok = fspolicy.push_scope({"roots": [str(work)], "cwd": str(work)})
    try:
        assert check_write("out.png") == os.path.join(os.path.realpath(work), "out.png")
        with pytest.raises(FileAccessDenied, match="file roots"):
            check_write(other / "out.png")
        with pytest.raises(FileAccessDenied, match="file roots"):
            check_read(home / "notes.txt")
        # nested scopes narrow, never widen
        tok2 = fspolicy.push_scope({"roots": [str(tmp_path)], "cwd": str(tmp_path)})
        try:
            with pytest.raises(FileAccessDenied):
                check_write(other / "out.png")
        finally:
            fspolicy.pop_scope(tok2)
        # an opt-out scope inside a confined one doesn't widen either
        tok3 = fspolicy.push_scope({"roots": None, "cwd": None})
        try:
            with pytest.raises(FileAccessDenied):
                check_write(other / "out.png")
        finally:
            fspolicy.pop_scope(tok3)
    finally:
        fspolicy.pop_scope(tok)
    assert check_write(other / "out.png")             # scope is per-call


@pytest.mark.parametrize("bad", ["*", ["relative/dir"], {"roots": "/tmp"},
                                 {"roots": ["rel"]}, {"roots": [], "cwd": "rel"}])
def test_scope_malformed_fails_closed(bad):
    with pytest.raises(ValueError):
        fspolicy.parse_scope(bad)


async def test_dispatch_applies_scope(home, work, tmp_path, monkeypatch):
    """Through Daemon.dispatch with a sessionless (unlocked) path verb."""
    from vibatchium.skills import store
    monkeypatch.setattr(store, "SKILLS_DIR", tmp_path / "skills-store")
    src = tmp_path / "elsewhere"
    (src / "kayak.com").mkdir(parents=True)
    (src / "kayak.com" / "n.md").write_text("note")
    d = _daemon()
    res = await d.dispatch({"id": "1", "cmd": "skill_import", "args": {
        "source": str(src), "_fs_scope": {"roots": [str(work)], "cwd": str(work)}}})
    assert not res["ok"] and "file roots" in res["error"]
    res = await d.dispatch({"id": "2", "cmd": "skill_import", "args": {
        "source": str(src), "_fs_scope": "garbage"}})
    assert not res["ok"] and "_fs_scope" in res["error"]
    # relative source resolves against the scope's cwd, not the daemon's
    res = await d.dispatch({"id": "3", "cmd": "skill_import", "args": {
        "source": "elsewhere", "_fs_scope": {"roots": [str(tmp_path)],
                                             "cwd": str(tmp_path)}}})
    assert res["ok"] and "kayak.com/n.md" in res["result"]["imported"]
    # CLI/SDK calls carry no scope → only the deny list applies
    res = await d.dispatch({"id": "4", "cmd": "skill_import",
                            "args": {"source": str(src)}})
    assert res["ok"], res
    assert fspolicy._scope_roots.get() == ()           # scope didn't leak


def test_mcp_strips_and_attaches_scope(home, work, monkeypatch):
    import asyncio

    from vibatchium import mcp_server as M
    rec = {}

    def fake_daemon_call(cmd, args=None, *, session=None, lease=None, **kw):
        rec.update(cmd=cmd, args=args)
        return {"ok": True}

    monkeypatch.setattr(M, "daemon_call", fake_daemon_call)
    monkeypatch.setattr(M, "daemon_is_running", lambda: True)
    monkeypatch.delenv(fspolicy.ENV_ROOTS, raising=False)
    monkeypatch.chdir(work)
    asyncio.run(M.call_tool("screenshot", {
        "path": "/tmp/x.png", "_fs_scope": {"roots": None, "cwd": "/"}}))
    scope = rec["args"]["_fs_scope"]
    assert scope["cwd"] == os.getcwd()
    assert scope["roots"] and os.path.realpath(work) in scope["roots"]
    monkeypatch.setenv(fspolicy.ENV_ROOTS, "*")
    asyncio.run(M.call_tool("screenshot", {"path": "/tmp/x.png"}))
    assert rec["args"]["_fs_scope"]["roots"] is None


def test_rest_caps_restricted_attaches_scope(home, work, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from vibatchium import client as C
    from vibatchium.rest import build_app
    rec = {}

    def fake_call(cmd, args=None, *, session=None, **kw):
        rec.update(cmd=cmd, args=dict(args or {}))
        return {"ok": True}

    monkeypatch.setattr(C, "call", fake_call)
    monkeypatch.setattr(C, "daemon_is_running", lambda: True)
    monkeypatch.delenv(fspolicy.ENV_ROOTS, raising=False)
    monkeypatch.chdir(work)
    smuggled = {"path": "/tmp/x.png", "_fs_scope": {"roots": None}}
    TestClient(build_app(require_auth=False, caps="vision")).post(
        "/v1/screenshot", json=dict(smuggled))
    assert rec["args"]["_fs_scope"]["roots"]                # confined
    TestClient(build_app(require_auth=False)).post("/v1/screenshot", json=dict(smuggled))
    assert "_fs_scope" not in rec["args"]                   # unrestricted: none


# ─── finding 2b: broadened always-on write deny ─────────────────────────

@pytest.mark.parametrize("rel", [
    ".git/config", ".git/hooks/pre-commit", "sub/.git/info/x",
    ".claude/settings.local.json", ".claude/settings.json", ".claude/hooks/x.sh",
    ".mcp.json", ".envrc", ".vscode/settings.json", ".idea/workspace.xml",
    "lib/python3.13/site-packages/evil.pth", "lib/site-packages/mod/__init__.py",
    "usr/dist-packages/x.py", "anywhere/foo.pth", ".venv/bin/python",
    ".venv-wt/bin/activate", ".GIT/config", "x/.Claude/settings.json",
    ".claude/worktrees/agent-1/.claude/settings.json",
])
def test_segment_write_deny_anywhere(home, work, rel):
    with pytest.raises(FileAccessDenied):
        check_write(work / rel)


def test_segment_write_deny_wins_inside_roots(home, work, monkeypatch):
    monkeypatch.setenv(fspolicy.ENV_ROOTS, str(work))
    with pytest.raises(FileAccessDenied, match="not overridable"):
        check_write(work / ".git" / "config")


@pytest.mark.parametrize("rel", [
    "assets/out.png", "gitstuff/out.png", "venv-notes/bin.txt", "claude/x.png",
    ".claude/worktrees/agent-1/src/app.py",       # agent worktree content
])
def test_segment_write_lookalikes_allowed(home, work, rel):
    assert check_write(work / rel)


def test_segment_rules_are_write_only(home, work):
    (work / ".git").mkdir()
    (work / ".git" / "config").write_text("[core]")
    assert check_read(work / ".git" / "config")


@pytest.mark.parametrize("rel", [
    ".local/share/systemd/user/x.service", ".local/share/applications/x.desktop",
    ".bashrc.d/x.sh", ".config/nvim/init.lua", ".vimrc", ".vim/plugin/x.vim",
    ".tmux.conf", ".emacs", ".emacs.d/init.el", ".config/pip/pip.conf",
    ".config/git/config", ".local/lib/python3.13/site-packages/x.pth",
])
def test_home_exec_write_deny(home, rel):
    with pytest.raises(FileAccessDenied):
        check_write(home / rel)


@pytest.mark.parametrize("var,rel", [
    ("XDG_CONFIG_HOME", "autostart/x.desktop"),
    ("XDG_CONFIG_HOME", "systemd/user/x.service"),
    ("XDG_DATA_HOME", "systemd/user/x.service"),
    ("XDG_DATA_HOME", "applications/x.desktop"),
])
def test_relocated_xdg_write_deny(home, work, clean_xdg, monkeypatch, var, rel):
    monkeypatch.setenv(var, str(work / "xdg"))
    with pytest.raises(FileAccessDenied):
        check_write(work / "xdg" / rel)


# ─── finding 3: read-deny gaps + profile dirs ───────────────────────────

@pytest.mark.parametrize("rel", [
    ".codex/auth.json", ".claude/settings.json", ".claude/projects/x.jsonl",
    ".bash_history", ".zsh_history", ".local/share/fish/fish_history",
    ".python_history", ".cargo/credentials", ".cargo/credentials.toml",
    ".config/rclone/rclone.conf", ".Xauthority",
    ".var/app/com.google.Chrome/config/google-chrome/Default/Cookies",
    ".var/app/org.mozilla.firefox/.mozilla/firefox/x/key4.db",
    "snap/chromium/common/chromium/Default/Cookies",
    "snap/firefox/common/.mozilla/firefox/x/logins.json",
    "snap/brave/current/x", ".config/vivaldi/Default/Cookies",
    ".config/vivaldi-snapshot/Default/Cookies", ".config/opera/Cookies",
    ".config/microsoft-edge-beta/Default/Cookies",
    ".config/google-chrome-beta/Default/Cookies",
])
def test_read_deny_gaps(home, rel):
    with pytest.raises(FileAccessDenied):
        check_read(home / rel)
    with pytest.raises(FileAccessDenied):
        fspolicy.check_upload([str(home / rel)])


@pytest.mark.parametrize("rel", [
    "../../../.var/app/com.google.Chrome/config/google-chrome",
    "../../vivaldi", "../../opera", "../../microsoft-edge-dev",
    "../../../snap/chromium/common/chromium", "../../../.codex",
])
def test_profile_dir_refuses_browser_and_agent_dirs(home, rel):
    managed = home / ".config" / "vibatchium" / "profiles"
    managed.mkdir(parents=True)
    with pytest.raises(FileAccessDenied):
        fspolicy.check_profile_dir(managed / rel, managed_root=managed)


# ─── finding 4: proxy_set must not echo file contents ───────────────────

@pytest.mark.parametrize("content", [
    "SECRET-TOKEN-abc123", "tok3n://SECRET-TOKEN-abc123", "http://",
    "http://SECRET-TOKEN-abc123:notaport", "",
])
async def test_proxy_set_file_error_hides_contents(home, work, content):
    f = work / "token"
    f.write_text(content)
    f.chmod(0o600)
    d = _daemon()
    with pytest.raises(ValueError) as ei:
        await d._handlers["proxy_set"](d, {"path": str(f)})
    assert "contents not shown" in str(ei.value)
    assert "SECRET" not in str(ei.value) and "tok3n" not in str(ei.value)


# ─── finding 5: vibatchium's runtime + state dirs ───────────────────────

def test_runtime_dir_write_denied_except_outputs(home, tmp_path, monkeypatch):
    from vibatchium.daemon import paths
    rt = tmp_path / "rt"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
    for base in (paths.CACHE_DIR, rt / "vibatchium", home / ".cache" / "vibatchium"):
        for name in ("daemon.sock", "daemon.pid", "daemon.lock",
                     "observe-cache.json", "vision-cache.json", "x/y"):
            with pytest.raises(FileAccessDenied, match="runtime dir"):
                check_write(base / name)
        for sub in ("screenshots", "explores"):
            assert check_write(base / sub / "shot.png")
    # reads of the runtime dir aren't the threat (no secrets live there)
    assert check_read(paths.CACHE_DIR / "observe-cache.json")


def test_state_dir_and_log_write_denied(home, tmp_path, monkeypatch):
    from vibatchium.daemon import paths
    monkeypatch.setenv("VIBATCHIUM_LOG_FILE", str(tmp_path / "logs" / "d.log"))
    for p in (paths.LOG_PATH, paths.STATE_DIR / "x",
              home / ".local" / "state" / "vibatchium" / "daemon.log",
              tmp_path / "logs" / "d.log"):
        with pytest.raises(FileAccessDenied, match="state dir"):
            check_write(p)


# ─── finding 6: case-insensitive filesystems ────────────────────────────

@pytest.mark.parametrize("rel,write", [
    (".SSH/id_ed25519", False), (".Ssh/id_ed25519", False),
    (".AWS/credentials", False), (".Config/Vibatchium/secrets.key", False),
    (".BASHRC", True), (".Config/Autostart/x.desktop", True),
])
def test_case_insensitive_deny(home, rel, write):
    with pytest.raises(FileAccessDenied):
        (check_write if write else check_read)(home / rel)


def test_case_insensitive_sys_deny(home):
    with pytest.raises(FileAccessDenied):
        check_read("/PROC/self/environ")
    with pytest.raises(FileAccessDenied):
        check_write("/ETC/x")


# ─── finding 7: skill_import of a local git source ──────────────────────

@pytest.mark.parametrize("spec", [
    "git+{home}/.ssh", "git+file://{home}/.ssh", "git+file://localhost{home}/.ssh",
    "git+~/.ssh", "git+{home}/.ssh#sub", "git+./../home/.ssh",
])
async def test_skill_import_local_git_checked(home, work, monkeypatch, spec):
    import subprocess
    monkeypatch.chdir(work)

    def no_clone(*a, **k):
        raise AssertionError("git clone ran before the path check")

    monkeypatch.setattr(subprocess, "call", no_clone)
    d = _daemon()
    with pytest.raises(FileAccessDenied):
        await d._handlers["skill_import"](d, {"source": spec.format(home=home)})


@pytest.mark.parametrize("spec,expected", [
    ("git+https://github.com/a/b#x", None), ("git+ssh://git@host/a/b", None),
    ("git@github.com:a/b.git", None), ("git+host:repo", None),
    ("https://github.com/a/b", None), ("git+/srv/repo", "/srv/repo"),
    ("git+file:///srv/r%20x", "/srv/r x"), ("git+./a:b", "./a:b"),
    ("git+weird://srv/repo", "srv/repo"),
])
def test_local_git_path_classification(spec, expected):
    from vibatchium.skills.handlers import _local_git_path
    assert _local_git_path(spec) == expected
