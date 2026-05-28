"""Plugins — pluggable modules that add namespaced verbs to the daemon.

A plugin is a Python package (or local directory) exposing a top-level
``register(daemon)`` function. The daemon calls it once at startup; the plugin
registers verbs via :meth:`Daemon.add_verb`. Plugin verbs are addressable from
CLI (``vb x.search``), MCP, and REST exactly like the built-in verbs.

Trust model: a plugin is Python running as your user. ``caps_required`` /
``secrets_required`` on a verb are *descriptive metadata* — the daemon reports
them to operators but cannot enforce them against in-process plugin code (which
can read the vault DB, query the keyring, or read /proc/<pid>/environ directly).
Trust posture is exactly pip-package trust. Caps gating still applies to
*external* callers over the socket; it does not sandbox plugin code itself.

See ``vibatchium/plugins/api.py`` for the ``VerbSpec`` contract and
``registry.py`` for discovery/load mechanics.
"""
from __future__ import annotations

from .api import VerbSpec, validate_verb_name

__all__ = ["VerbSpec", "validate_verb_name"]
