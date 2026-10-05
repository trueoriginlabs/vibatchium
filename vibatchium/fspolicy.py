"""Filesystem confinement for CALLER-SUPPLIED paths (and ``file:`` URLs).

Every verb that takes a path from the caller (``upload`` files, ``pdf`` /
``screenshot --path`` / ``download_save`` / ``record_stop`` / ``har_start`` /
``storage_export`` / ... output paths, ``storage_restore`` / ``proxy_set`` /
``skill_import`` input paths, ``start --profile <abs-dir>``) routes it through
:func:`check_read` or :func:`check_write` **in the daemon**, before touching the
disk. Every navigation entry point routes its URL through
:func:`check_nav_url`, so ``go file:///home/me/.ssh/id_ed25519`` is the same
read as ``upload ~/.ssh/id_ed25519``. The MCP server, the REST shim and the SDK
all reach the daemon over the socket, so enforcing here covers every surface.

Why: the caller is often an agent reading attacker-controlled page text. A
prompt-injected ``upload ~/.ssh/id_ed25519`` into a web form is exfiltration;
a ``pdf --path ~/.bashrc`` is code execution on the next login shell.

Policy:

* **Deny list (always on, every surface)** — credential/key stores, agent
  configs and histories, vibatchium's own config (vault key, vault, profiles),
  real-browser profiles (incl. Flatpak/Snap), keyrings, ``/proc``, ``/sys``,
  ``/dev``, ``/etc/shadow``, ``/etc/sudoers*``. Writes additionally deny shell
  rc files, editor/tool configs that execute code, autostart/systemd user
  units, ``~/.local/bin``, cron spools, system dirs, vibatchium's runtime dir
  (socket, pidfile, caches — except its screenshot/explore output dirs) and
  state dir, and — anywhere on disk — ``.git``/``.claude``/``.vscode``/
  ``.idea`` dirs, ``.mcp.json``/``.envrc`` files, ``site-packages``/
  ``dist-packages``, ``*.pth`` files and ``.venv*/bin``. Matching is
  case-insensitive (``~/.SSH`` is ``~/.ssh`` on a casefolded filesystem).
* **Symlinks are resolved first** (like playwright-mcp 0.0.81): both the
  lexical path and its realpath must clear the deny list, so neither a tmp
  symlink into ``~/.ssh`` nor a ``~/.ssh`` that is itself a symlink elsewhere
  slips through. For writes the target need not exist — the existing prefix is
  resolved and a dangling final symlink is followed to where it would write.
* **Roots** — two independent layers, both of which must pass:

  - ``VIBATCHIUM_FILE_ROOTS`` in the DAEMON's env (``os.pathsep``-separated;
    ``*`` or empty = off) confines every caller.
  - a per-call scope the agent-facing surfaces attach (MCP always, the REST
    shim when ``--caps``-restricted): the internal ``_fs_scope`` arg, set by
    the surface itself and stripped from caller input. It carries the
    surface's roots (default: :func:`default_agent_roots`) and its cwd, which
    relative caller paths resolve against. CLI/SDK calls carry none.

  The deny list still wins inside any root.

Internal paths vibatchium chooses itself (the screenshots cache, checkpoints,
explore output, profiles it manages) never go through this module.
"""
from __future__ import annotations

import contextvars
import fnmatch
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ENV_ROOTS = "VIBATCHIUM_FILE_ROOTS"
#: The internal arg an agent-facing surface attaches to every daemon call.
SCOPE_ARG = "_fs_scope"
#: ``VIBATCHIUM_FILE_ROOTS`` value meaning "no roots" (opt out of confinement).
ROOTS_OFF = "*"

# ─── deny rules (data, not code) ──────────────────────────────────────────
# Home-relative entries may be fnmatch globs ("*" matches within the name).

# No caller path may read OR write these.
_HOME_DENY_RW = (
    # credentials / keys
    ".ssh", ".gnupg", ".aws", ".azure", ".config/gcloud", ".kube",
    ".docker/config.json", ".netrc", ".pgpass", ".git-credentials",
    ".config/gh", ".password-store", ".vault-token", ".npmrc", ".pypirc",
    ".cargo/credentials", ".cargo/credentials.toml", ".config/rclone",
    ".Xauthority", ".local/share/keyrings",
    # agent configs + their tokens
    ".claude", ".claude.json", ".codex",
    # shell / REPL histories
    ".bash_history", ".zsh_history", ".local/share/fish/fish_history",
    ".python_history",
    # vibatchium itself: vault key, vault, profiles
    ".config/vibatchium",
    # real-browser profiles (native, Flatpak, Snap)
    ".config/google-chrome*", ".config/chromium", ".config/BraveSoftware",
    ".config/microsoft-edge*", ".config/vivaldi*", ".config/opera*",
    ".mozilla", ".var/app", "snap/chromium", "snap/firefox", "snap/brave",
)
# Additionally, no caller path may WRITE these (code execution on the next
# login / shell / editor / agent start, or PATH hijack).
_HOME_DENY_W = (
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".bashrc.d",
    ".profile", ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".config/fish",
    ".xprofile", ".xinitrc", ".xsessionrc", ".pam_environment",
    ".config/autostart", ".config/systemd", ".config/environment.d",
    ".local/share/systemd", ".local/share/applications",
    ".local/bin", "bin", ".gitconfig", ".config/git", ".config/pip", ".pip",
    ".vimrc", ".vim", ".config/nvim", ".tmux.conf", ".emacs*",
    ".cursor",
)
# Relocated XDG dirs (only consulted when the env var is an absolute path).
_XDG_CONFIG_DENY_RW = (
    "vibatchium", "google-chrome*", "chromium", "BraveSoftware",
    "microsoft-edge*", "vivaldi*", "opera*", "gcloud", "gh", "rclone",
)
_XDG_CONFIG_DENY_W = (
    "autostart", "systemd", "environment.d", "fish", "nvim", "git", "pip",
)
_XDG_DATA_DENY_RW = ("keyrings", "fish/fish_history")
_XDG_DATA_DENY_W = ("systemd", "applications")

# Absolute locations (globs allowed) no caller path may read OR write.
_SYS_DENY_RW = (
    "/proc", "/sys", "/dev", "/etc/shadow", "/etc/gshadow", "/etc/sudoers*",
    "/etc/ssh",
)
# Absolute locations no caller path may WRITE.
_SYS_DENY_W = (
    "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
    "/var/spool/cron",
)

# Anywhere on disk, inside roots too: no caller path may WRITE a path with one
# of these components (dirs that hold hooks / settings an agent or tool runs),
# or with one of these file names / suffixes.
_SEGMENT_DENY_W = (".git", ".claude", ".vscode", ".idea",
                   "site-packages", "dist-packages")
_NAME_DENY_W = (".mcp.json", ".envrc")
_SUFFIX_DENY_W = (".pth",)
_VENV_RE = re.compile(r"\.venv[^/]*", re.IGNORECASE)

# vibatchium's runtime dir: writable only in these output subdirs.
_RUNTIME_WRITABLE = ("screenshots", "explores")


class FileAccessDenied(PermissionError):
    """A caller-supplied path (or file: URL) was refused by the policy."""


# ─── per-call scope (agent surfaces) ──────────────────────────────────────

# A tuple of root-sets: a path must lie under some root of EVERY set. Nested
# scopes only ever append, so a call can narrow but never widen.
_scope_roots: contextvars.ContextVar[tuple[tuple[str, ...], ...]] = \
    contextvars.ContextVar("vb_fs_scope_roots", default=())
_scope_cwd: contextvars.ContextVar[str | None] = \
    contextvars.ContextVar("vb_fs_scope_cwd", default=None)


def parse_scope(raw) -> tuple[tuple[str, ...] | None, str | None]:
    """Validate an ``_fs_scope`` arg → (roots or None, cwd or None).

    Fails CLOSED: anything malformed raises, so a broken surface can't
    silently fall back to unconfined. ``roots: None`` = no roots (opt-out)."""
    if not isinstance(raw, dict):
        raise ValueError(f"bad {SCOPE_ARG}: expected an object")
    roots = raw.get("roots")
    cwd = raw.get("cwd")
    if roots is not None:
        if not isinstance(roots, (list, tuple)) or not all(
                isinstance(r, str) and os.path.isabs(r) for r in roots):
            raise ValueError(f"bad {SCOPE_ARG}: roots must be absolute paths")
        roots = tuple(os.path.realpath(r) for r in roots)
    if cwd is not None and not (isinstance(cwd, str) and os.path.isabs(cwd)):
        raise ValueError(f"bad {SCOPE_ARG}: cwd must be an absolute path")
    return roots, cwd


def push_scope(raw):
    """Enter a call scope (from :data:`SCOPE_ARG`). Returns a token for
    :func:`pop_scope`. Narrows any enclosing scope; never widens it."""
    roots, cwd = parse_scope(raw)
    t_roots = _scope_roots.set(_scope_roots.get() + ((roots,) if roots is not None else ()))
    t_cwd = _scope_cwd.set(cwd if cwd is not None else _scope_cwd.get())
    return (t_roots, t_cwd)


def pop_scope(token) -> None:
    t_roots, t_cwd = token
    _scope_cwd.reset(t_cwd)
    _scope_roots.reset(t_roots)


def _too_broad(path: str) -> bool:
    """A root that would cover the whole home (or the whole disk)."""
    real = os.path.realpath(path)
    home = os.path.realpath(_home())
    return real == os.sep or _under(home, real)


def default_agent_roots(cwd: str | None = None) -> list[str]:
    """Roots for an agent surface when ``VIBATCHIUM_FILE_ROOTS`` is unset: the
    surface's cwd (the agent's project dir — skipped if it is ``/`` or covers
    ``$HOME``), ``/tmp``, ``$TMPDIR``, ``~/Downloads`` and vibatchium's own
    screenshot/explore output dirs."""
    out: list[str] = []
    if cwd and not _too_broad(cwd):
        out.append(cwd)
    out.append("/tmp")
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir and os.path.isabs(tmpdir) and not _too_broad(tmpdir):
        out.append(tmpdir)
    out.append(str(_home() / "Downloads"))
    for d in _runtime_dirs():
        out.extend(os.path.join(d, sub) for sub in _RUNTIME_WRITABLE)
    seen: list[str] = []
    for r in out:
        r = os.path.realpath(r)
        if r not in seen:
            seen.append(r)
    return seen


def agent_scope(cwd: str | None = None) -> dict:
    """The :data:`SCOPE_ARG` value an agent-facing surface attaches to every
    daemon call, from ITS OWN env: ``VIBATCHIUM_FILE_ROOTS=*`` opts out,
    a path list replaces the defaults, unset/empty uses
    :func:`default_agent_roots`."""
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            cwd = None
    raw = (os.environ.get(ENV_ROOTS) or "").strip()
    if raw == ROOTS_OFF:
        roots = None
    elif raw:
        roots = _parse_roots(raw)
    else:
        roots = default_agent_roots(cwd)
    return {"roots": roots, "cwd": cwd}


# ─── matching ─────────────────────────────────────────────────────────────

def _home() -> Path:
    # expanduser honours $HOME at call time (tests monkeypatch it).
    return Path(os.path.expanduser("~"))


def _both(p: str) -> set[str]:
    """Lexical-normalized and symlink-resolved forms of an absolute path."""
    return {os.path.normpath(p), os.path.realpath(p)}


def _under(path: str, root: str) -> bool:
    root = root.rstrip(os.sep) or os.sep
    return path == root or path.startswith(root if root == os.sep else root + os.sep)


def _fold(p: str) -> str:
    return p.casefold()


def _runtime_dirs() -> list[str]:
    """vibatchium's runtime/cache dir(s): socket, pidfile, lock, caches."""
    out = []
    try:
        from .daemon import paths as _paths
        out.append(str(_paths.CACHE_DIR))
    except Exception:  # noqa: BLE001 — never let the policy fail open on import
        pass
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and os.path.isabs(rt):
        out.append(os.path.join(rt, "vibatchium"))
    out.append(str(_home() / ".cache" / "vibatchium"))
    return list(dict.fromkeys(out))


def _state_paths() -> list[str]:
    out = []
    try:
        from .daemon import paths as _paths
        out += [str(_paths.STATE_DIR), str(_paths.LOG_PATH)]
    except Exception:  # noqa: BLE001
        pass
    st = os.environ.get("XDG_STATE_HOME")
    if st and os.path.isabs(st):
        out.append(os.path.join(st, "vibatchium"))
    out.append(str(_home() / ".local" / "state" / "vibatchium"))
    log = os.environ.get("VIBATCHIUM_LOG_FILE")
    if log:
        out.append(os.path.abspath(os.path.expanduser(log)))
    return list(dict.fromkeys(out))


def _deny_entries(write: bool) -> list[tuple[str, str, tuple[str, ...]]]:
    """(pattern, label, exceptions) triples for the active deny list."""
    home = _home()
    out: list[tuple[str, str, tuple[str, ...]]] = []

    def add(path: str, label: str, exc: tuple[str, ...] = ()):
        for form in _both(path):
            out.append((form, label, exc))

    for r in _HOME_DENY_RW + (_HOME_DENY_W if write else ()):
        add(str(home / r), f"~/{r}")
    xdg_c = os.environ.get("XDG_CONFIG_HOME")
    if xdg_c and os.path.isabs(xdg_c):
        for r in _XDG_CONFIG_DENY_RW + (_XDG_CONFIG_DENY_W if write else ()):
            add(os.path.join(xdg_c, r), f"$XDG_CONFIG_HOME/{r}")
    xdg_d = os.environ.get("XDG_DATA_HOME")
    if xdg_d and os.path.isabs(xdg_d):
        for r in _XDG_DATA_DENY_RW + (_XDG_DATA_DENY_W if write else ()):
            add(os.path.join(xdg_d, r), f"$XDG_DATA_HOME/{r}")
    # The vault can be relocated by env; never let a caller read/clobber it.
    vault = os.environ.get("VIBATCHIUM_VAULT_PATH")
    if vault:
        add(os.path.abspath(os.path.expanduser(vault)), "VIBATCHIUM_VAULT_PATH")
    if write:
        # daemon.sock / pidfile / lock / observe+vision caches / log: a caller
        # path must not clobber them (DoS of every bot on the daemon, or a
        # poisoned observe cache). The two output dirs stay writable.
        for d in _runtime_dirs():
            exc = tuple(f for sub in _RUNTIME_WRITABLE
                        for f in _both(os.path.join(d, sub)))
            add(d, "vibatchium runtime dir", exc)
        for p in _state_paths():
            add(p, "vibatchium state dir")
    for pat in _SYS_DENY_RW + (_SYS_DENY_W if write else ()):
        out.append((pat, pat, ()))
    return out


def _matches(path: str, pat: str) -> bool:
    """Case-insensitive: is ``path`` (or an ancestor) ``pat``?"""
    path, pat = _fold(path), _fold(pat)
    if any(c in pat for c in "*?["):
        p = path
        while True:
            if fnmatch.fnmatchcase(p, pat):
                return True
            parent = os.path.dirname(p)
            if parent == p:
                return False
            p = parent
    return _under(path, pat)


def _segment_hit(path: str) -> str | None:
    parts = [_fold(x) for x in path.split(os.sep) if x]
    segs = {_fold(s) for s in _SEGMENT_DENY_W}
    for i, part in enumerate(parts):
        if part in segs:
            # Claude Code puts agent worktrees in <repo>/.claude/worktrees/<n>/;
            # their CONTENT is an ordinary checkout (its own .claude/ is still
            # caught as a later segment). Everything else under .claude/ —
            # settings*.json, hooks, commands, agents — stays denied.
            if part == ".claude" and len(parts) > i + 3 \
                    and parts[i + 1] == "worktrees":
                continue
            return f"a {part!r} directory"
        if _VENV_RE.fullmatch(part) and i + 1 < len(parts) and parts[i + 1] == "bin":
            return "a virtualenv's bin/"
    if parts:
        name = parts[-1]
        if name in {_fold(n) for n in _NAME_DENY_W}:
            return f"a {name!r} file"
        if name.endswith(tuple(_fold(s) for s in _SUFFIX_DENY_W)):
            return "a Python .pth file (runs at interpreter start)"
    return None


def _deny_hit(candidates: set[str], write: bool) -> str | None:
    for pat, label, exc in _deny_entries(write):
        for c in candidates:
            if _matches(c, pat) and not any(_matches(c, e) for e in exc):
                return label
    if write:
        for c in candidates:
            hit = _segment_hit(c)
            if hit:
                return hit
    return None


def _parse_roots(raw: str) -> list[str]:
    out = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            out.append(os.path.realpath(os.path.abspath(os.path.expanduser(part))))
    return out


def roots() -> list[str]:
    """Realpath'd daemon-env VIBATCHIUM_FILE_ROOTS, or [] when off."""
    raw = (os.environ.get(ENV_ROOTS) or "").strip()
    if raw == ROOTS_OFF:
        return []
    return _parse_roots(raw)


def _check(path, *, write: bool, verb: str | None) -> str:
    if path is None or (isinstance(path, str) and not path.strip()):
        raise ValueError("empty path")
    raw = os.fspath(path)
    expanded = os.path.expanduser(raw)
    cwd = _scope_cwd.get()
    if cwd and not os.path.isabs(expanded):
        expanded = os.path.join(cwd, expanded)
    absolute = os.path.abspath(expanded)
    real = os.path.realpath(absolute)
    what = f"{verb} " if verb else ""
    mode = "write" if write else "read"

    hit = _deny_hit(_both(absolute), write)   # lexical + realpath forms
    if hit is not None:
        shown = f"{raw!r}" + (f" (resolves to {real!r})" if real != raw else "")
        raise FileAccessDenied(
            f"file access denied: {what}{mode} of {shown} — inside protected "
            f"location {hit}. vibatchium never lets a caller-supplied path "
            f"{mode} credential stores, agent configs, its own vault/profiles, "
            f"browser profiles or system files"
            + (", shell rc / autostart / editor / VCS / venv files, its "
               "runtime dir or system dirs" if write else "")
            + f". This is not overridable; {ENV_ROOTS} only narrows access "
            f"further. Use a path in a normal working dir (e.g. /tmp, ~/Downloads).")

    allowed = roots()
    if allowed and not any(_under(real, r) for r in allowed):
        raise FileAccessDenied(
            f"file access denied: {what}{mode} of {raw!r} (resolves to {real!r}) "
            f"is outside {ENV_ROOTS}={os.pathsep.join(allowed)}. Use a path "
            f"inside one of those roots, or add a root to {ENV_ROOTS} "
            f"({os.pathsep!r}-separated) in the daemon's environment.")
    for scope in _scope_roots.get():
        if not any(_under(real, r) for r in scope):
            raise FileAccessDenied(
                f"file access denied: {what}{mode} of {raw!r} (resolves to "
                f"{real!r}) is outside this agent surface's file roots "
                f"({os.pathsep.join(scope) or 'none'}). Agent surfaces (MCP, a "
                f"--caps-restricted REST shim) confine caller paths to their "
                f"cwd, /tmp, ~/Downloads and vibatchium's output dirs. Use a "
                f"path there, or set {ENV_ROOTS} in the MCP server's env "
                f"(a path list, or {ROOTS_OFF!r} to opt out).")
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


# ─── navigation URLs ──────────────────────────────────────────────────────

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")
_NAV_OK_SCHEMES = ("http", "https", "data", "blob")
_ABOUT_OK = ("about:blank", "about:srcdoc")
# Renderer debug URLs: they kill only this session's own renderer (self-heal
# revives it) and read nothing — kept so crash recovery stays testable.
_CHROME_OK = ("chrome://crash", "chrome://kill")


def _normalize_url(url: str) -> str:
    # WHATWG URL parsing: strip leading/trailing C0 controls + space, drop
    # every ASCII tab/newline — Chrome does, so "fi\tle:" is "file:".
    url = url.strip("".join(chr(i) for i in range(0x21)))
    return re.sub(r"[\t\n\r]", "", url)


def file_url_path(url: str) -> str:
    """The local path a ``file:`` URL reads (raises on a remote host)."""
    parts = urlsplit(_normalize_url(url).replace("\\", "/"))
    host = (parts.hostname or "").casefold()
    if host not in ("", "localhost"):
        raise FileAccessDenied(
            f"navigation refused: file URL with a remote host {host!r}")
    return unquote(parts.path) or "/"


def check_nav_url(url, *, verb: str = "go") -> None:
    """Refuse navigations that read the local disk past the file policy.

    ``http(s)``, ``data:``, ``blob:``, ``about:blank``/``about:srcdoc`` pass.
    ``file:`` URLs are mapped to a path and judged by :func:`check_read` (a
    directory URL — which lists the dir — checks the dir itself). Everything
    else that names a scheme (``view-source:``, ``chrome:``, ``devtools:``,
    ``filesystem:``, ``javascript:``, ``chrome-extension:`` …) is refused.
    A string with no scheme is left to the browser, which rejects it."""
    if not isinstance(url, str):
        return
    norm = _normalize_url(url)
    m = _SCHEME_RE.match(norm)
    if not m:
        return
    scheme = m.group(1).casefold()
    if scheme in _NAV_OK_SCHEMES:
        return
    low = norm.casefold()
    if scheme == "about" and low.split("#", 1)[0].split("?", 1)[0] in _ABOUT_OK:
        return
    if scheme == "chrome" and low.rstrip("/") in _CHROME_OK:
        return
    if scheme == "file":
        check_read(file_url_path(norm), verb=f"{verb} file:")
        return
    raise FileAccessDenied(
        f"navigation refused: {verb} to a {scheme!r} URL. vibatchium navigates "
        f"http(s), data:, blob: and about:blank only (file: URLs are checked "
        f"against the file-access policy); {scheme}: can expose local files "
        f"or browser internals to page text.")
