"""Skills — per-host Markdown field-notes the agent reads before driving a site.

A Skill is *memory/context*, not executable code (contrast: a Plugin registers
verbs). When the agent navigates to a host, the daemon surfaces matching notes
so the agent reads accumulated gotchas/selectors/"use the API here" advice
before inventing an approach. Format is deliberately loose and
browser-use-compatible (``<host>/<task>.md``) so their public domain-skills
directory is importable.

Opt-in: surfacing is off unless ``VIBATCHIUM_SKILLS`` is truthy (notes cost
context tokens and are a prompt-injection surface). vibatchium's differentiator
over browser-use: notes are run through ``safety.classify`` on read (injection)
and a secret-pattern scan on write/import (enforcing "no secrets" mechanically).
"""
from __future__ import annotations

from . import match, safety, store

__all__ = ["match", "safety", "store"]
