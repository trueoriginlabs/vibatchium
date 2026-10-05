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
