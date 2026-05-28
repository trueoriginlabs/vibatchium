"""Built-in daemon verbs for managing plugins.

These are *built-in* verbs (not plugin verbs — they have no dotted namespace).
They're registered directly on the daemon at startup and routed as ``unlocked``
(no session, no registry mutate lock needed; they only read/rebuild the plugin
table, which is in-process state).

- ``plugin_list``  — installed plugins + their verb names
- ``plugin_show``  — one plugin's metadata + full verb specs
- ``plugin_reload``— rescan + re-register without restarting the daemon
- ``list_verbs``   — plugin verb specs (used by the MCP server to expose them)
"""
from __future__ import annotations

from . import registry


def register_admin_verbs(daemon) -> None:
    @daemon.handler("plugin_list")
    async def _plugin_list(d, args):
        return {"plugins": [dict(meta) for meta in d._plugins.values()]}

    @daemon.handler("plugin_show")
    async def _plugin_show(d, args):
        name = args.get("name")
        if not name or name not in d._plugins:
            raise ValueError(f"no plugin {name!r} loaded "
                             f"(have: {sorted(d._plugins)})")
        meta = dict(d._plugins[name])
        meta["verb_specs"] = [
            d._verb_meta[v].public_meta()
            for v in meta.get("verbs", []) if v in d._verb_meta
        ]
        return meta

    @daemon.handler("plugin_reload")
    async def _plugin_reload(d, args):
        plugins = registry.reload_into(d)
        return {"reloaded": True,
                "plugins": [dict(meta) for meta in plugins.values()]}

    @daemon.handler("list_verbs")
    async def _list_verbs(d, args):
        """Plugin verb specs — consumed by the MCP server to expose plugin
        verbs as tools. Built-in verbs are intentionally excluded (the MCP
        server already ships their rich static schemas)."""
        return {"verbs": [d._verb_meta[v].public_meta()
                          for v in sorted(d._plugin_verbs)
                          if v in d._verb_meta]}

    # These admin verbs are session-independent.
    for v in ("plugin_list", "plugin_show", "plugin_reload", "list_verbs"):
        daemon._verb_lock_class[v] = "unlocked"
