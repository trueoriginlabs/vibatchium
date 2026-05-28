"""The plugin contract: ``VerbSpec`` and ``Daemon.add_verb`` metadata.

A plugin's ``register(daemon)`` calls ``daemon.add_verb(...)`` for each verb it
exposes. ``add_verb`` builds a :class:`VerbSpec`, registers the handler in the
daemon's dispatch table, and records the metadata so ``plugin_list`` /
``list_verbs`` (and therefore MCP) can surface it.

Handler signature mirrors the built-in handlers exactly::

    async def handler(daemon, args: dict) -> JSON-serializable

so a plugin can drive the live session in-process via ``daemon.session``
(no RPC round-trip back through the socket).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from collections.abc import Awaitable, Callable
from typing import Any

# A plugin verb MUST be namespaced: ``<namespace>.<verb>`` with at least one
# dot. Built-in verbs never contain a dot, so this guarantees a plugin verb can
# never shadow a built-in (start, go, click, ...). Each segment is a
# conservative identifier; the whole thing is capped so it can't blow out logs
# or schemas.
_VERB_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")
_VERB_MAX_LEN = 64

# Lock classes a plugin verb may request. Mirrors the daemon dispatcher's three
# routing buckets. Default is "session": the verb needs the per-session lock and
# a running browser session (the common case — a plugin drives the page).
LOCK_CLASSES = ("session", "registry", "unlocked")


class PluginError(RuntimeError):
    """Raised for malformed plugin registrations (bad verb name, dup, etc.)."""


def validate_verb_name(name: str) -> str:
    """Validate a plugin verb name; return it unchanged on success.

    Rules: lowercase, namespaced (must contain a dot so it can't shadow a
    built-in), each segment ``[a-z0-9_]`` starting with a letter for the first
    segment, max 64 chars.
    """
    if not isinstance(name, str) or not name:
        raise PluginError("verb name must be a non-empty string")
    if len(name) > _VERB_MAX_LEN:
        raise PluginError(f"verb name {name!r}: max {_VERB_MAX_LEN} chars")
    if not _VERB_RE.match(name):
        raise PluginError(
            f"verb name {name!r} invalid: must be lowercase and namespaced "
            f"like 'x.search' (segments [a-z0-9_], at least one dot, no "
            f"leading digit on the first segment)"
        )
    return name


@dataclass
class VerbSpec:
    """Everything the daemon knows about one plugin-registered verb.

    ``caps_required`` / ``secrets_required`` are descriptive only — see the
    module docstring in ``vibatchium/plugins/__init__.py`` for why the daemon
    cannot enforce them against in-process plugin code.
    """
    name: str
    handler: Callable[[Any, dict], Awaitable[Any]]
    inputs_schema: dict = field(default_factory=dict)
    outputs_schema: dict = field(default_factory=dict)
    caps_required: list[str] = field(default_factory=list)
    secrets_required: list[str] = field(default_factory=list)
    description: str = ""
    lock: str = "session"
    # Source plugin name; set by the loader when the plugin is registered.
    plugin: str | None = None

    def __post_init__(self) -> None:
        validate_verb_name(self.name)
        if self.lock not in LOCK_CLASSES:
            raise PluginError(
                f"verb {self.name!r}: lock={self.lock!r} must be one of "
                f"{LOCK_CLASSES}"
            )
        if not callable(self.handler):
            raise PluginError(f"verb {self.name!r}: handler must be callable")

    def public_meta(self) -> dict:
        """Serializable metadata for plugin_list / list_verbs / MCP."""
        return {
            "name": self.name,
            "description": self.description,
            "inputs_schema": self.inputs_schema,
            "outputs_schema": self.outputs_schema,
            "caps_required": list(self.caps_required),
            "secrets_required": list(self.secrets_required),
            "lock": self.lock,
            "plugin": self.plugin,
        }
