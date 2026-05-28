"""Plugin discovery + loading.

Three load mechanisms (all discovered at daemon startup):

1. **Pip entry point** — a package declares
   ``[project.entry-points."vibatchium.plugins"]`` ``name = "pkg.module:register"``.
2. **Local directory** — ``~/.config/vibatchium/plugins/<name>/__init__.py``
   exposing a top-level ``register`` function (personal / prototyping).
3. **Git install** — ``vb plugin install git+https://...`` is a thin wrapper
   over pip, after which the package is discovered as an entry point (#1).

Loading is best-effort and isolated per plugin: a plugin that raises during
``register`` is logged and skipped — it never takes the daemon down.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import logging
import sys
from dataclasses import dataclass
from collections.abc import Callable

from ..daemon.paths import CONFIG_DIR

log = logging.getLogger("vibatchium.plugins")

ENTRY_POINT_GROUP = "vibatchium.plugins"
PLUGINS_DIR = CONFIG_DIR / "plugins"


@dataclass
class DiscoveredPlugin:
    name: str
    register: Callable[[object], None]
    source: str          # "entry_point" | "local_dir"
    version: str | None
    origin: str          # dotted path or filesystem path, for `plugin show`


def _discover_entry_points() -> list[DiscoveredPlugin]:
    out: list[DiscoveredPlugin] = []
    try:
        eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # noqa: BLE001
        log.warning("entry-point discovery failed: %s", exc)
        return out
    for ep in eps:
        try:
            register = ep.load()
        except Exception as exc:  # noqa: BLE001
            log.warning("plugin entry-point %r failed to load: %s", ep.name, exc)
            continue
        if not callable(register):
            log.warning("plugin entry-point %r: %r is not callable", ep.name, ep.value)
            continue
        version = None
        dist = getattr(ep, "dist", None)
        if dist is not None:
            version = getattr(dist, "version", None)
        out.append(DiscoveredPlugin(
            name=ep.name, register=register, source="entry_point",
            version=version, origin=ep.value,
        ))
    return out


def _discover_local_dirs() -> list[DiscoveredPlugin]:
    out: list[DiscoveredPlugin] = []
    if not PLUGINS_DIR.is_dir():
        return out
    for child in sorted(PLUGINS_DIR.iterdir()):
        init = child / "__init__.py"
        if not child.is_dir() or not init.is_file():
            continue
        name = child.name
        mod_name = f"vibatchium_plugin_{name}"
        # Drop any cached copy so `plugin reload` re-reads the file from disk.
        sys.modules.pop(mod_name, None)
        try:
            spec = importlib.util.spec_from_file_location(mod_name, init)
            if spec is None or spec.loader is None:
                log.warning("local plugin %r: could not build import spec", name)
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001
            log.warning("local plugin %r failed to import: %s", name, exc)
            sys.modules.pop(mod_name, None)
            continue
        register = getattr(module, "register", None)
        if not callable(register):
            log.warning("local plugin %r: no top-level register() function", name)
            continue
        version = getattr(module, "__version__", None)
        out.append(DiscoveredPlugin(
            name=name, register=register, source="local_dir",
            version=version, origin=str(init),
        ))
    return out


def discover() -> list[DiscoveredPlugin]:
    """All discovered plugins (entry points first, then local dirs).

    On a name collision (a local dir shadowing an installed package), the local
    dir wins — it's the more deliberate, edit-in-place choice.
    """
    eps = _discover_entry_points()
    local = _discover_local_dirs()
    by_name: dict[str, DiscoveredPlugin] = {p.name: p for p in eps}
    for p in local:
        if p.name in by_name:
            log.info("local plugin %r shadows installed entry point", p.name)
        by_name[p.name] = p
    return list(by_name.values())


def load_into(daemon) -> dict:
    """Discover and register every plugin into ``daemon``.

    Returns the daemon's plugin metadata map. Each plugin's ``register`` is
    called with the daemon; failures are isolated (logged + recorded with an
    ``error`` field) so one broken plugin doesn't block the rest.
    """
    discovered = discover()
    for dp in discovered:
        daemon._plugins[dp.name] = {
            "name": dp.name,
            "source": dp.source,
            "version": dp.version,
            "origin": dp.origin,
            "verbs": [],
            "error": None,
        }
        daemon._loading_plugin = dp.name
        try:
            dp.register(daemon)
            log.info("loaded plugin %r (%s) — verbs: %s",
                     dp.name, dp.source, daemon._plugins[dp.name]["verbs"])
        except Exception as exc:  # noqa: BLE001
            daemon._plugins[dp.name]["error"] = f"{type(exc).__name__}: {exc}"
            log.warning("plugin %r register() failed: %s", dp.name, exc)
        finally:
            daemon._loading_plugin = None
    return daemon._plugins


def reload_into(daemon) -> dict:
    """Remove all plugin verbs + metadata, then re-discover and re-load.

    Built-in verbs are untouched — only names tracked in ``daemon._plugin_verbs``
    are removed from the dispatch table.
    """
    for verb in list(daemon._plugin_verbs):
        daemon._handlers.pop(verb, None)
        daemon._verb_meta.pop(verb, None)
        daemon._verb_lock_class.pop(verb, None)
    daemon._plugin_verbs.clear()
    daemon._plugins.clear()
    return load_into(daemon)
