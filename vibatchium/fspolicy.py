"""Filesystem confinement for CALLER-SUPPLIED paths.

Every verb that takes a path from the caller (``upload`` files, ``pdf`` /
``screenshot --path`` / ``download_save`` / ``record_stop`` / ``har_start`` /
``storage_export`` / ... output paths, ``storage_restore`` / ``proxy_set`` /
``skill_import`` input paths, ``start --profile <abs-dir>``) routes it through
:func:`check_read` or :func:`check_write` **in the daemon**, before touching the
disk. The MCP server, the REST shim and the SDK all reach the daemon over the
socket, so enforcing here covers every surface.

Why: the caller is often an agent reading attacker-controlled page text. A
prompt-injected ``upload ~/.ssh/id_ed25519`` into a web form is exfiltration;
a ``pdf --path ~/.bashrc`` is code execution on the next login shell.

Policy (always on):

* **Deny list** — credential/key stores, vibatchium's own config (vault key,
  vault, profiles), real-browser profiles, keyrings, ``/proc``, ``/sys``,
  ``/dev``, ``/etc/shadow``, ``/etc/sudoers*``. Writes additionally deny shell
  rc files, autostart/systemd user units, ``~/.local/bin``, agent configs, cron
  spools and system dirs.
* **Symlinks are resolved first** (like playwright-mcp 0.0.81): both the
  lexical path and its realpath must clear the deny list, so neither a tmp
  symlink into ``~/.ssh`` nor a ``~/.ssh`` that is itself a symlink elsewhere
  slips through. For writes the target need not exist — the existing prefix is
  resolved and a dangling final symlink is followed to where it would write.
* **Opt-in strict mode** — ``VIBATCHIUM_FILE_ROOTS`` (``os.pathsep``-separated)
  confines caller paths to those roots (by realpath). The deny list still wins
  inside a root.

Internal paths vibatchium chooses itself (the screenshots cache, checkpoints,
explore output, profiles it manages) never go through this module.

Relative paths resolve against the DAEMON's cwd, exactly as Playwright/``open``
already did; the CLI absolutizes against the client's cwd before sending.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

ENV_ROOTS = "VIBATCHIUM_FILE_ROOTS"

# Home-relative locations no caller path may read OR write.
_HOME_DENY_RW = (
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud", ".kube",
    ".docker/config.json", ".netrc", ".pgpass", ".git-credentials",
    ".config/gh", ".password-store", ".vault-token", ".npmrc", ".pypirc",
    ".config/vibatchium",                       # vault key, vault, profiles
    ".config/google-chrome", ".config/chromium", ".config/BraveSoftware",
    ".config/microsoft-edge", ".mozilla", ".local/share/keyrings",
    ".claude/.credentials.json", ".claude.json",
)
# Additional home-relative locations no caller path may WRITE (code execution
# on next login / shell / agent start, or PATH hijack).
_HOME_DENY_W = (
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile",
    ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".config/fish",
    ".xprofile", ".xinitrc", ".xsessionrc", ".pam_environment",
    ".config/autostart", ".config/systemd", ".config/environment.d",
    ".local/bin", "bin", ".gitconfig", ".config/git",
    ".claude", ".codex", ".cursor",
)
# Absolute locations (fnmatch patterns, matched against the path and each of
# its ancestors) no caller path may read OR write.
_SYS_DENY_RW = (
    "/proc", "/sys", "/dev", "/etc/shadow", "/etc/gshadow", "/etc/sudoers*",
    "/etc/ssh",
)
# Absolute locations no caller path may WRITE.
_SYS_DENY_W = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
    "/var/spool/cron",
)


class FileAccessDenied(PermissionError):
    """A caller-supplied path was refused by the file-access policy."""


def _home() -> Path:
    # expanduser honours $HOME at call time (tests monkeypatch it).
    return Path(os.path.expanduser("~"))


def _both(p: str) -> set[str]:
    """Lexical-normalized and symlink-resolved forms of an absolute path."""
    return {os.path.normpath(p), os.path.realpath(p)}


def _under(path: str, root: str) -> bool:
    root = root.rstrip(os.sep) or os.sep
    return path == root or path.startswith(root if root == os.sep else root + os.sep)


def _deny_entries(write: bool) -> list[tuple[str, str]]:
    """(match-pattern, label) pairs for the active deny list."""
    home = _home()
    rel = _HOME_DENY_RW + (_HOME_DENY_W if write else ())
    out: list[tuple[str, str]] = []
    for r in rel:
        for form in _both(str(home / r)):
            out.append((form, f"~/{r}"))
    # XDG_CONFIG_HOME relocates browser profiles / our own config dir.
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        for r in ("vibatchium", "google-chrome", "chromium", "BraveSoftware",
                  "microsoft-edge", "gcloud", "gh"):
            for form in _both(os.path.join(xdg, r)):
                out.append((form, f"$XDG_CONFIG_HOME/{r}"))
    # The vault can be relocated by env; never let a caller read/clobber it.
    vault = os.environ.get("VIBATCHIUM_VAULT_PATH")
    if vault:
        for form in _both(os.path.abspath(os.path.expanduser(vault))):
            out.append((form, "VIBATCHIUM_VAULT_PATH"))
    for pat in _SYS_DENY_RW + (_SYS_DENY_W if write else ()):
        out.append((pat, pat))
    return out


def _deny_hit(candidates: set[str], write: bool) -> str | None:
    for pat, label in _deny_entries(write):
        glob = any(c in pat for c in "*?[")
        for c in candidates:
            if glob:
                # match the path itself or any ancestor (so a file inside a
                # globbed dir — /etc/sudoers.d/x — is caught too)
                p = c
                while True:
                    if fnmatch.fnmatchcase(p, pat):
                        return label
                    parent = os.path.dirname(p)
                    if parent == p:
                        break
                    p = parent
            elif _under(c, pat):
                return label
    return None


def roots() -> list[str]:
    """Realpath'd VIBATCHIUM_FILE_ROOTS, or [] when strict mode is off."""
    raw = os.environ.get(ENV_ROOTS, "")
    out = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            out.append(os.path.realpath(os.path.abspath(os.path.expanduser(part))))
    return out


def _check(path, *, write: bool, verb: str | None) -> str:
    if path is None or (isinstance(path, str) and not path.strip()):
        raise ValueError("empty path")
    raw = os.fspath(path)
    absolute = os.path.abspath(os.path.expanduser(raw))
    real = os.path.realpath(absolute)
    what = f"{verb} " if verb else ""
    mode = "write" if write else "read"

    hit = _deny_hit(_both(absolute), write)   # lexical + realpath forms
    if hit is not None:
        shown = f"{raw!r}" + (f" (resolves to {real!r})" if real != raw else "")
        raise FileAccessDenied(
            f"file access denied: {what}{mode} of {shown} — inside protected "
            f"location {hit}. vibatchium never lets a caller-supplied path "
            f"{mode} credential stores, its own vault/profiles, browser "
            f"profiles or system files"
            + (", shell rc / autostart files or system dirs" if write else "")
            + f". This is not overridable; {ENV_ROOTS} only narrows access "
            f"further. Use a path in a normal working dir (e.g. /tmp, ~/Downloads).")

    allowed = roots()
    if allowed and not any(_under(real, r) for r in allowed):
        raise FileAccessDenied(
            f"file access denied: {what}{mode} of {raw!r} (resolves to {real!r}) "
            f"is outside {ENV_ROOTS}={os.pathsep.join(allowed)}. Use a path "
            f"inside one of those roots, or add a root to {ENV_ROOTS} "
            f"({os.pathsep!r}-separated) in the daemon's environment.")
    return real


def check_read(path, *, verb: str | None = None) -> str:
    """Validate a caller-supplied path the daemon will READ (upload, restore,
    import). Returns the resolved realpath — read from that, not the input, so
    a symlink swapped after the check can't redirect the read elsewhere."""
    return _check(path, write=False, verb=verb)


def check_write(path, *, verb: str | None = None) -> str:
    """Validate a caller-supplied path the daemon will WRITE (or a directory it
    will write into). The target need not exist: the existing prefix is
    resolved, and a dangling final symlink is followed. Returns the resolved
    realpath — write to that."""
    return _check(path, write=True, verb=verb)


def check_upload(files, *, verb: str = "upload") -> list:
    """check_read every path in an upload list. A directory (webkitdirectory
    inputs) is checked as a whole AND file-by-file, so a symlink inside it
    can't smuggle a key out. Non-string entries (in-memory payload dicts) carry
    no path and pass through unchanged. Returns the list with paths resolved."""
    out = []
    for f in files:
        if not isinstance(f, (str, os.PathLike)):
            out.append(f)
            continue
        real = check_read(f, verb=verb)
        if os.path.isdir(real):
            for dirpath, _dirs, names in os.walk(real, followlinks=False):
                for n in names:
                    check_read(os.path.join(dirpath, n), verb=verb)
                for dn in _dirs:
                    dp = os.path.join(dirpath, dn)
                    if os.path.islink(dp):
                        check_read(dp, verb=verb)
        out.append(real)
    return out


def check_profile_dir(path, *, managed_root: Path | str) -> str:
    """Validate a caller-supplied Chrome user-data-dir. Dirs inside
    vibatchium's own ``managed_root`` (PROFILES_DIR) are its business and
    pass; anything else is a write target — pointing a session at
    ``~/.config/google-chrome`` would hand the agent the user's real logins."""
    absolute = os.path.abspath(os.path.expanduser(os.fspath(path)))
    real = os.path.realpath(absolute)
    managed = os.path.realpath(os.fspath(managed_root))
    if _under(real, managed) and real != managed:
        return real
    return check_write(path, verb="start --profile")
