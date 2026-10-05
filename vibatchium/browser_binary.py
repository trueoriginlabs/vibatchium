"""Per-session browser executable (``vb start --browser-binary PATH``).

By default every session launches Patchright's ``channel="chrome"`` — the
system's real Google Chrome, which is the stealth recommendation (a branded,
auto-updating Chrome is the build real users run). Some callers need a
DIFFERENT Chromium: a pinned version for a regression, a Chrome for Testing
build, Chromium on a distro with no Google Chrome package, a vendor fork. This
swaps the executable and nothing else — the profile, flags, proxy, geo, GPU and
scale postures all apply unchanged.

RESOLUTION (first hit wins), re-read on every launch AND relaunch so a
self-heal comes back on the same binary — the same persist-never-re-derive rule
as display.json / gpu.json:

  1. ``browser.json`` ``{"path": "/abs/path"}`` in the session's profile dir,
     written by ``vb start --browser-binary PATH`` (``""`` removes it);
  2. ``VIBATCHIUM_BROWSER_BINARY`` in the DAEMON's environment — a daemon-wide
     default for every session without its own pin;
  3. neither → ``channel="chrome"``, byte-identical to before this existed.

SECURITY. Choosing which executable the daemon runs is code execution, so it is
operator-only:

  * ``start --browser-binary`` is refused on agent surfaces (any call carrying
    an ``_fs_scope`` — MCP, a ``--caps``-restricted REST shim). See
    :func:`vibatchium.fspolicy.check_exec`. It is deliberately NOT in the MCP
    ``start`` schema either: a parameter that is always refused there only
    costs tokens and invites an agent to try it.
  * A persisted ``browser.json`` is trusted on an agent surface only inside a
    profile dir vibatchium manages (``PROFILES_DIR``, which no caller path may
    read or write). An agent may point ``start --profile`` at a dir under its
    own roots (``/tmp``, its cwd), and an agent can get attacker-chosen bytes
    into such a dir (``download_save``) — so a ``browser.json`` there is
    refused on agent surfaces rather than run.
  * The env default is the operator's own configuration and is honoured on
    every surface.

Every resolved path is re-validated at launch (exists, regular file,
executable). A pinned binary that vanished FAILS the launch with a pointer to
the clear command instead of silently running a different browser — unlike a
corrupt scale, which degrades harmlessly, a swapped browser changes what the
caller is testing.

Patchright-only. The nodriver backend spawns Chrome itself; a per-session pin is
refused there, and the env default is ignored (``start`` reports
``browser_binary_ignored``).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("vibatchium.browser_binary")

ENV_BROWSER_BINARY = "VIBATCHIUM_BROWSER_BINARY"


def session_browser_path(profile_dir: Path) -> Path:
    return profile_dir / "browser.json"


def save_session_browser(profile_dir: Path, path: str | None) -> None:
    """Persist ``{"path": path}`` on the profile dir; ``None``/``""`` removes it.

    The caller validates ``path`` first (fspolicy.check_exec) — this only
    writes. Creates the profile dir so a start-time persist works on a brand-new
    profile, and keeps the 0600 invariant for vibatchium-written files.
    """
    p = session_browser_path(profile_dir)
    if not path:
        if p.exists():
            p.unlink()
        return
    profile_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"path": str(path)}))
    os.chmod(p, 0o600)


def load_session_browser(profile_dir: Path) -> str | None:
    """The persisted binary path, or None if unset/corrupt.

    A corrupt file (bad JSON, non-dict JSON, a non-string or relative path)
    reads as unset — it was never a valid pin, so there is nothing to honour.
    A well-formed pin to a binary that no longer exists is NOT treated as unset
    here; :func:`resolve_browser_binary` fails the launch on it.
    """
    p = session_browser_path(profile_dir)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        if not isinstance(raw, dict):
            return None
        path = raw.get("path")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(path, str) or not os.path.isabs(path):
        return None
    return path


def _is_managed(profile_dir: Path, managed_root: Path | None) -> bool:
    if managed_root is None:
        from .daemon.paths import PROFILES_DIR
        managed_root = PROFILES_DIR
    real = os.path.realpath(profile_dir)
    root = os.path.realpath(managed_root)
    return real != root and real.startswith(root.rstrip(os.sep) + os.sep)


def resolve_browser_binary(profile_dir: Path, *, name: str = "?",
                           managed_root: Path | None = None
                           ) -> tuple[str | None, str | None]:
    """Effective executable for this profile → ``(path, source)``.

    ``source`` is ``"session"`` (browser.json), ``"env"``
    (VIBATCHIUM_BROWSER_BINARY) or ``None`` (channel Chrome, path None).
    Raises ValueError for a configured binary that is not a runnable file, and
    FileAccessDenied for an untrusted browser.json on an agent surface.
    """
    from . import fspolicy
    pinned = load_session_browser(profile_dir)
    if pinned is not None:
        if fspolicy.in_agent_scope() and not _is_managed(profile_dir, managed_root):
            raise fspolicy.FileAccessDenied(
                f"session {name!r}: refusing the browser pin in {profile_dir} on "
                f"an agent surface — that profile dir is caller-chosen, so its "
                f"browser.json can't be trusted to name what the daemon runs. "
                f"Start this session from the CLI, or use a vibatchium-managed "
                f"profile (a session name / `--profile <name>`).")
        try:
            fspolicy.validate_executable(pinned, verb="browser.json")
        except ValueError as exc:
            raise ValueError(
                f"session {name!r}: pinned browser binary is unusable ({exc}). "
                f"`vb --session {name} start --browser-binary ''` clears the pin."
            ) from None
        log.info("session %s: browser binary %s (pinned)", name, pinned)
        return pinned, "session"
    env = (os.environ.get(ENV_BROWSER_BINARY) or "").strip()
    if env:
        if not os.path.isabs(env):
            raise ValueError(f"{ENV_BROWSER_BINARY}={env!r} must be an absolute path")
        try:
            fspolicy.validate_executable(env, verb=ENV_BROWSER_BINARY)
        except ValueError as exc:
            raise ValueError(f"{ENV_BROWSER_BINARY} is unusable: {exc}") from None
        log.info("session %s: browser binary %s (%s)", name, env, ENV_BROWSER_BINARY)
        return env, "env"
    return None, None
