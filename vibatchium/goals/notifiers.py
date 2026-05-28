"""Lightweight event sinks. A notifier receives every goal event.

Three built-ins:
  - stdout://             → daemon log (local dev; the default)
  - webhook://<full-url>  → HTTP POST the event JSON to an external service.
                            The text after ``webhook://`` is the literal target
                            URL *including its scheme*, e.g.
                            ``webhook://https://hooks.example.com/goals``.
  - mcp_push://           → no-op sink; events are read back via ``goal_events``

Every event is durably persisted to the goals DB by the engine *before* the
notifier is invoked, so a sink is a pure side-channel — losing one never loses
an event. ``mcp_push://`` therefore needs no buffer: an MCP client polls
``goal_events`` (store-backed, survives restart) instead.

Telegram/Slack/Discord are intentionally out of scope — they need their own
ACL/budget machinery and pull vibatchium toward end-user UX (browser-use Box's
territory). Adding a new sink is ~one subclass.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request

log = logging.getLogger("vibatchium.goals.notify")


class Notifier:
    """Base sink. ``notify`` must never raise and must never block the caller —
    a sink failure or a slow endpoint can't break (or stall) a goal step."""
    def notify(self, goal_id: str, event: dict) -> None:  # pragma: no cover
        raise NotImplementedError


class StdoutNotifier(Notifier):
    def notify(self, goal_id: str, event: dict) -> None:
        try:
            log.info("goal %s [%s] %s", goal_id, event.get("kind"),
                     json.dumps(event.get("payload", {}))[:300])
        except Exception:  # noqa: BLE001
            pass


class NullNotifier(Notifier):
    """No-op sink. Used for ``mcp_push://`` — the event is already persisted by
    the engine, so the MCP client reads it back via ``goal_events``."""
    def notify(self, goal_id: str, event: dict) -> None:
        return None


class WebhookNotifier(Notifier):
    """POST each event to an HTTP endpoint.

    The POST runs on a short-lived daemon thread so a slow/unreachable endpoint
    cannot block the daemon's single async event loop (``notify`` returns as
    soon as the thread is spawned). Errors are logged inside the thread and
    never propagate to the caller.
    """
    def __init__(self, url: str, *, timeout: float = 5.0):
        self.url = url
        self.timeout = timeout

    def _post(self, goal_id: str, body: bytes) -> None:
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=self.timeout).close()
        except Exception as exc:  # noqa: BLE001
            log.warning("webhook notify failed for goal %s: %s", goal_id, exc)

    def notify(self, goal_id: str, event: dict) -> None:
        try:
            body = json.dumps({"goal_id": goal_id, **event}).encode()
        except Exception:  # noqa: BLE001
            return
        threading.Thread(
            target=self._post, args=(goal_id, body),
            name=f"webhook-notify-{goal_id[:8]}", daemon=True,
        ).start()


def build(uri: str | None) -> Notifier:
    """Construct a notifier from a URI. None / empty / stdout:// → StdoutNotifier."""
    if not uri or uri == "stdout://" or uri == "stdout":
        return StdoutNotifier()
    if uri.startswith("webhook://"):
        return WebhookNotifier(uri[len("webhook://"):])
    if uri.startswith("mcp_push"):
        return NullNotifier()
    # Unknown scheme — fall back to stdout rather than failing goal creation.
    log.warning("unknown notifier %r — falling back to stdout", uri)
    return StdoutNotifier()
