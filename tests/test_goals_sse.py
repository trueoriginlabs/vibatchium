"""4.2 — `goal tail` over SSE.

The streaming logic lives in `rest.iter_goal_events_sse` (FastAPI-free) so it's
unit-testable with a stub daemon call. A second, import-guarded test drives the
actual `GET /v1/goals/<id>/events` route through FastAPI's TestClient when the
`rest` extra is installed.
"""
from __future__ import annotations

import json

import pytest

from vibatchium.rest import iter_goal_events_sse


def _parse_sse(frames: list[str]) -> list[dict]:
    out = []
    for f in frames:
        assert f.startswith("data: ") and f.endswith("\n\n"), repr(f)
        out.append(json.loads(f[len("data: "):].strip()))
    return out


async def test_sse_generator_streams_in_order_then_stops_on_terminal():
    batches = [
        {"events": [{"seq": 1, "kind": "step_start", "payload": {}},
                    {"seq": 2, "kind": "observation", "payload": {"x": 1}}]},
        {"events": [{"seq": 3, "kind": "step_done", "payload": {"step": 1}}]},
        {"events": [{"seq": 4, "kind": "done", "payload": {}}]},
        {"events": [{"seq": 5, "kind": "step_start", "payload": {}}]},  # never reached
    ]
    seen_args: list[dict] = []

    def fake_call(cmd, args=None, *, session=None, **kw):
        assert cmd == "goal_events"
        seen_args.append(args)
        return batches.pop(0) if batches else {"events": []}

    frames = []
    async for frame in iter_goal_events_sse(fake_call, "G", 0,
                                            poll_interval=0, idle_timeout=5):
        frames.append(frame)

    events = _parse_sse(frames)
    # streamed in order, stopped at the terminal `done` (seq 4); seq 5 not sent
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert events[-1]["kind"] == "done"
    # each poll advanced after_seq past the last seen event
    assert [a["after_seq"] for a in seen_args] == [0, 2, 3]


async def test_sse_generator_stops_on_idle_timeout():
    def fake_call(cmd, args=None, *, session=None, **kw):
        return {"events": []}

    frames = [f async for f in iter_goal_events_sse(
        fake_call, "G", 0, poll_interval=0, idle_timeout=0)]
    assert frames == []


def test_sse_http_route_streams_events(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from vibatchium import client, rest

    batches = [
        {"events": [{"seq": 1, "kind": "step_start", "payload": {}}]},
        {"events": [{"seq": 2, "kind": "done", "payload": {}}]},
    ]

    def fake_call(cmd, args=None, *, session=None, **kw):
        return batches.pop(0) if batches else {"events": []}

    monkeypatch.setattr(client, "call", fake_call)
    monkeypatch.setattr(client, "daemon_is_running", lambda: True)
    app = rest.build_app(require_auth=False)

    with TestClient(app) as tc:
        resp = tc.get("/v1/goals/G/events?after=0")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        seqs = [json.loads(line[len("data: "):])["seq"]
                for line in resp.text.splitlines() if line.startswith("data: ")]
        assert seqs == [1, 2]
