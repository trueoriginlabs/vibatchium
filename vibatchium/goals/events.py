"""The universal event vocabulary, shared by Goals, notifiers, and any
consumer. One schema so a webhook sink, an SSE tail, and the store all speak
the same language.
"""
from __future__ import annotations

import time

# Canonical event kinds. Keep this list authoritative — notifiers and the
# store validate against it.
EVENT_KINDS: tuple[str, ...] = (
    "step_start",
    "step_done",
    "step_failed",
    "observation",
    "plan_update",
    "question",
    "user_input",
    "artifact",
    "model_call",
    "budget_consumed",
    "session_attached",
    "session_released",
    "checkpoint_saved",
    "done",
    "failed",
    "cancelled",
)

# Terminal goal states (no further transitions).
TERMINAL_STATES: frozenset[str] = frozenset({"done", "failed", "cancelled"})

# All goal states.
STATES: tuple[str, ...] = (
    "pending", "running", "paused", "needs_input",
    "done", "failed", "cancelled",
)


class EventError(ValueError):
    pass


def make_event(kind: str, payload: dict | None = None, *, ts: float | None = None) -> dict:
    """Build an event dict. Raises EventError on an unknown kind."""
    if kind not in EVENT_KINDS:
        raise EventError(f"unknown event kind {kind!r} (valid: {EVENT_KINDS})")
    return {
        "kind": kind,
        "ts": ts if ts is not None else time.time(),
        "payload": payload or {},
    }
