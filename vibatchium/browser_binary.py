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

  1. ``browser.json`` ``{"path": "/abs/path"}``, written by
     ``vb start --browser-binary PATH`` (``""`` removes it) — in the session's
     profile dir when vibatchium manages it (``PROFILES_DIR/<name>``), else in
     the operator-only pins store (``~/.config/vibatchium/pins/<sha256 of the
     dir's realpath>/``, see :func:`vibatchium.daemon.paths.profile_config_dir`);
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
  * Trust follows WHO WROTE THE PIN, not who triggers the launch. Both places a
    pin can live are unreachable by every caller path (PROFILES_DIR and the
    pins store are under ``~/.config/vibatchium``, on fspolicy's read+write deny
    list), so whatever is there was written by the operator — and is honoured
    on every launch and self-heal relaunch, whichever surface caused it.
  * A ``browser.json`` found INSIDE a caller-chosen profile dir is never read:
    an agent may point ``start --profile`` at a dir under its own roots
    (``/tmp``, its cwd) and get attacker-chosen bytes into it
    (``download_save``). It is ignored with a warning, on every surface — an
    operator start or a self-heal relaunch included.
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


FILE_NAME = "browser.json"


def session_browser_path(profile_dir: Path, *, managed_root: Path | None = None,
                         pins_root: Path | None = None) -> Path:
    """Where this profile's pin lives (see the module docstring)."""
    from .daemon.paths import profile_config_dir
    return profile_config_dir(profile_dir, managed_root=managed_root,
                              pins_root=pins_root) / FILE_NAME


def save_session_browser(profile_dir: Path, path: str | None, *,
                         managed_root: Path | None = None,
                         pins_root: Path | None = None) -> None:
    """Persist ``{"path": path}`` for the profile dir; ``None``/``""`` removes it.

    The caller validates ``path`` first (fspolicy.check_exec) — this only
    writes. Creates the config dir so a start-time persist works on a brand-new
    profile, and keeps the 0600 invariant for vibatchium-written files.
    """
    p = session_browser_path(profile_dir, managed_root=managed_root,
                             pins_root=pins_root)
    if not path:
        if p.exists():
            p.unlink()
        return
    from .daemon.paths import ensure_profile_config_dir
    ensure_profile_config_dir(profile_dir, managed_root=managed_root,
                              pins_root=pins_root)
    p.write_text(json.dumps({"path": str(path)}))
    os.chmod(p, 0o600)


def load_session_browser(profile_dir: Path, *, managed_root: Path | None = None,
                         pins_root: Path | None = None) -> str | None:
    """The persisted binary path, or None if unset/corrupt.

    A corrupt file (bad JSON, non-dict JSON, a non-string or relative path)
    reads as unset — it was never a valid pin, so there is nothing to honour.
    A well-formed pin to a binary that no longer exists is NOT treated as unset
    here; :func:`resolve_browser_binary` fails the launch on it.
    """
    p = session_browser_path(profile_dir, managed_root=managed_root,
                             pins_root=pins_root)
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


def warn_untrusted_in_profile(profile_dir: Path, file_name: str, what: str, *,
                              name: str = "?",
                              managed_root: Path | None = None) -> bool:
    """Log (and return True) when a caller-chosen profile dir carries its own
    ``file_name`` — which is ignored: config for unmanaged dirs is read only
    from the operator-only pins store."""
    from .daemon.paths import is_managed_profile
    try:
        planted = (not is_managed_profile(profile_dir, managed_root)
                   and (Path(profile_dir) / file_name).exists())
    except OSError:
        return False
    if planted:
        log.warning("session %s: ignoring %s in caller-chosen profile dir %s — "
                    "%s for a profile outside vibatchium's profiles dir is read "
                    "only from the operator-only pins store", name, file_name,
                    profile_dir, what)
    return planted


def resolve_browser_binary(profile_dir: Path, *, name: str = "?",
                           managed_root: Path | None = None,
                           pins_root: Path | None = None
                           ) -> tuple[str | None, str | None]:
    """Effective executable for this profile → ``(path, source)``.

    ``source`` is ``"session"`` (browser.json), ``"env"``
    (VIBATCHIUM_BROWSER_BINARY) or ``None`` (channel Chrome, path None).
    Raises ValueError for a configured binary that is not a runnable file. A
    browser.json inside a caller-chosen profile dir is ignored (logged).
    """
    from . import fspolicy
    warn_untrusted_in_profile(profile_dir, FILE_NAME, "a browser pin", name=name,
                              managed_root=managed_root)
    pinned = load_session_browser(profile_dir, managed_root=managed_root,
                                  pins_root=pins_root)
    if pinned is not None:
        try:
            fspolicy.validate_executable(pinned, verb="browser.json")
        except ValueError as exc:
            from .daemon.paths import is_managed_profile
            prof = ("" if is_managed_profile(profile_dir, managed_root)
                    else f" --profile {profile_dir}")
            raise ValueError(
                f"session {name!r}: pinned browser binary is unusable ({exc}). "
                f"`vb --session {name} start{prof} --browser-binary ''` clears "
                f"the pin."
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
