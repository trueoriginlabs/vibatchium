"""Goals — durable, resumable, budget-enforced long-running operations.

A Goal is a persistent record with a state machine, bound to one session, with
budget enforcement, crash-resumability, and a typed event stream. The daemon
does NOT run the LLM by default: an external driver (Claude Code, Codex, a
custom loop) calls ``goal_next`` / ``goal_step`` in a loop. This preserves
vibatchium's toolkit identity — the daemon is the durable substrate, the agent
is pluggable.

Modules:
  events.py    — the shared typed event vocabulary (the "spine")
  store.py     — SQLite persistence (goals / goal_events / goal_artifacts)
  engine.py    — state machine, budget, session ownership, idempotency
  notifiers.py — stdout / webhook / mcp_push event sinks
"""
from __future__ import annotations

from . import events, store

__all__ = ["events", "store"]
