"""Wave 6.4a — REST shim.

FastAPI app that exposes every daemon verb at `POST /v1/<verb>` with the
same JSON body. Lets non-shell clients (Docker, language-agnostic agents,
hosted-mode integrations) drive vibatchium without speaking Unix-socket RPC.

### Auth

Bearer-token by default. Token is generated on first launch + persisted at
`~/.cache/vibatchium/rest-token` (mode 0600). All endpoints require
`Authorization: Bearer <token>` except `/v1/health`.

`--insecure-no-auth` (CLI flag) disables auth — explicit opt-in for dev only.

### Endpoints

  GET  /v1/health                 — health check; no auth
  GET  /v1/tools                  — list available verbs (post-caps filter)
  POST /v1/<verb>                 — invoke verb; body is the args dict
  POST /v1/<verb>?session=<name>  — invoke verb on a specific session
  GET  /v1/goals/<id>/events?after=N — SSE tail of a goal's event stream
  WS   /v1/stream/<session>       — live JPEG frames (token via ?token=...)

### Long-running verbs

`act`, `vision_click`, `wait_email_code`, etc. can take many seconds.
The shim awaits them inline (FastAPI is async); set the client timeout
appropriately.

### Capability gating (Wave 7.5b)

By default the REST shim exposes every daemon verb to authenticated
clients — including `eval`, `secret_*`, `wait_email_code`, and the file-
writing verbs (`screenshot` with path, `storage_export`, `download_save`,
`pdf`, `har_stop`, `network_dump`, `record_stop`). That gives any client
holding the bearer token **local-code-equivalent** access on the host.

For untrusted clients / hosted-mode deployments, restrict the surface
with `caps=<bucket,...>` (same bucket names as `mcp --caps`). Verbs
outside the allowed set return HTTP 403. The WebSocket stream also
respects caps: `vision` is required for screenshots; `input` is required
to forward clicks / keys back into the browser.
"""
import logging
import os
import secrets
from pathlib import Path

log = logging.getLogger("vibatchium.rest")


TOKEN_PATH = Path.home() / ".cache" / "vibatchium" / "rest-token"


def get_or_create_token() -> str:
    """Return the persisted bearer token; generate one if missing."""
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token)
    os.chmod(TOKEN_PATH, 0o600)
    return token


_TERMINAL_EVENT_KINDS = frozenset({"done", "failed", "cancelled"})


async def iter_goal_events_sse(call_fn, goal_id: str, after_seq: int = 0, *,
                               poll_interval: float = 0.5,
                               idle_timeout: float = 30.0):
    """Yield Server-Sent-Event frames for a goal's event stream.

    Polls the daemon (``call_fn('goal_events', {goal_id, after_seq})``) for
    events newer than the last seen sequence, formatting each as a
    ``data: <json>\\n\\n`` SSE frame in order. Terminates when a terminal event
    (done/failed/cancelled) is seen, or after ``idle_timeout`` seconds with no
    new events. FastAPI-free so it's unit-testable with a stub ``call_fn``.
    """
    import asyncio
    import json as _json
    import time as _time

    last = after_seq
    idle_start = _time.monotonic()
    while True:
        res = await asyncio.to_thread(
            call_fn, "goal_events", {"goal_id": goal_id, "after_seq": last})
        events = (res or {}).get("events", [])
        if events:
            idle_start = _time.monotonic()
            for ev in events:
                last = ev["seq"]
                yield f"data: {_json.dumps(ev)}\n\n"
                if ev.get("kind") in _TERMINAL_EVENT_KINDS:
                    return
        elif _time.monotonic() - idle_start > idle_timeout:
            return
        await asyncio.sleep(poll_interval)


def _allowed_verbs(caps: str | None) -> set[str] | None:
    """Resolve caps spec → set of allowed verb names, or None for unrestricted.

    Reuses the MCP capability buckets so the two surfaces stay aligned.
    `status` is always exposed (matches MCP behavior).
    """
    if not caps:
        return None  # unrestricted
    from .mcp_server import _CAP_BUCKETS, _ALWAYS_EXPOSED
    parts = {p.strip().lower() for p in caps.split(",") if p.strip()}
    if "all" in parts:
        return None
    bad = parts - set(_CAP_BUCKETS.keys())
    if bad:
        raise ValueError(
            f"unknown REST caps: {sorted(bad)}. "
            f"Available: {sorted(_CAP_BUCKETS.keys())}"
        )
    allowed: set[str] = set(_ALWAYS_EXPOSED)
    for bucket in parts:
        allowed |= _CAP_BUCKETS[bucket]
    return allowed


def build_app(*, require_auth: bool = True, token: str | None = None,
              caps: str | None = None):
    """Build the FastAPI app. `token` defaults to the persisted token.

    Args:
      caps: comma-separated capability buckets (`core,nav,input,...`).
            None = unrestricted (every verb exposed). Same buckets as MCP.
    """
    try:
        from fastapi import FastAPI, HTTPException, Request, WebSocket
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise RuntimeError(
            "REST shim requires `pip install vibatchium[rest]` "
            f"(import error: {exc})"
        ) from exc

    from .client import call as daemon_call, daemon_is_running, spawn_daemon
    from .mcp_server import TOOLS

    if require_auth and token is None:
        token = get_or_create_token()
    _expected_token = token  # closure
    _allowed = _allowed_verbs(caps)  # None = unrestricted

    from . import __version__ as _pkg_version
    app = FastAPI(title="vibatchium", version=_pkg_version,
                  description="REST shim over the vb daemon")

    def _check_auth(request) -> None:
        if not require_auth:
            return
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        provided = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, _expected_token):
            raise HTTPException(status_code=403, detail="invalid token")

    def _check_cap(verb: str) -> None:
        """Wave 7.5b: capability gate. 403 if verb isn't in the allowed set."""
        if _allowed is not None and verb not in _allowed:
            raise HTTPException(
                status_code=403,
                detail=f"verb {verb!r} not in allowed caps: "
                       f"add the bucket via --caps to expose it"
            )

    @app.get("/v1/health")
    async def health():
        return {"status": "ok", "daemon": daemon_is_running()}

    @app.get("/v1/tools")
    async def tools_list(request: Request):
        _check_auth(request)
        return {
            "tools": [
                {"name": t[0], "description": t[1], "input_schema": t[2]}
                for t in TOOLS
                if _allowed is None or t[0] in _allowed
            ],
            "caps": caps,
        }

    @app.websocket("/v1/stream/{session_name}")
    async def stream(websocket: WebSocket, session_name: str):
        """Wave 7.3: live-view passthrough over the REST shim.

        Auth via `?token=...` query param (browsers can't set Authorization
        on WS upgrade). Stream is binary JPEG frames + JSON envelopes for
        errors. Frame rate set by `?fps=N` (default 5, max 30).

        Optional `?takeover=1` accepts inbound JSON
        `{type:click,x,y,button}` / `{type:type,text}` / `{type:key,code}` /
        `{type:scroll,dx,dy}` events from the client and forwards them
        through `daemon_call('mouse'|'keys'|...)`.

        Wave 7.5b: capability-aware. Requires `vision` bucket for frame
        capture (calls `screenshot`); requires `input` bucket to accept
        takeover events (calls `mouse`/`keys`).
        """
        # Query-param auth
        if require_auth:
            token_q = websocket.query_params.get("token")
            if not token_q or not secrets.compare_digest(token_q, _expected_token or ""):
                # 1008 = policy violation — closest to HTTP 403 for WS
                await websocket.close(code=1008, reason="bad token")
                return
        # Capability gate: stream requires `vision` (for screenshot calls).
        # Takeover additionally requires `input` (for mouse/keys forwarding).
        if _allowed is not None and "screenshot" not in _allowed:
            await websocket.close(code=1008, reason="vision cap required for /v1/stream")
            return
        try:
            fps = int(websocket.query_params.get("fps", "5"))
        except ValueError:
            fps = 5
        fps = max(1, min(fps, 30))
        takeover_requested = websocket.query_params.get("takeover") == "1"
        takeover_allowed = _allowed is None or {"mouse", "keys"} <= _allowed
        takeover = takeover_requested and takeover_allowed
        await websocket.accept()
        # Send hello envelope first (client expects this to set up scaling)
        try:
            await websocket.send_json({
                "type": "hello", "session": session_name,
                "fps": fps, "takeover": takeover,
                # Surface denial reason so a misconfigured client gets a hint
                "takeover_denied_reason": (
                    "input cap required"
                    if takeover_requested and not takeover_allowed else None
                ),
            })
        except Exception:  # noqa: BLE001
            return

        import asyncio
        import base64
        interval = 1.0 / fps
        stop = asyncio.Event()

        async def _send_frames():
            """Frame loop — capture + send until cancelled."""
            while not stop.is_set():
                try:
                    result = await asyncio.to_thread(
                        daemon_call, "screenshot", {"full_page": False},
                        session=session_name,
                    )
                    png_b64 = result.get("png_b64")
                    if png_b64:
                        await websocket.send_bytes(base64.b64decode(png_b64))
                except Exception as exc:  # noqa: BLE001
                    try:
                        await websocket.send_json({"type": "error",
                                                    "error": f"{type(exc).__name__}: {exc}"})
                    except Exception:  # noqa: BLE001
                        stop.set()
                        return
                await asyncio.sleep(interval)

        async def _receive_takeover():
            """Pump inbound events to the daemon. Cancelled when stop fires."""
            import json as _json
            # Tiny delay so the sender gets the first frame queued before we
            # potentially see the test client's immediate disconnect.
            await asyncio.sleep(0.05)
            while not stop.is_set():
                try:
                    msg = await websocket.receive_text()
                except Exception:  # noqa: BLE001
                    stop.set()
                    return
                if not takeover:
                    continue  # discard; takeover mode off or denied
                try:
                    ev = _json.loads(msg)
                except Exception:  # noqa: BLE001
                    continue
                etype = ev.get("type")
                try:
                    if etype == "click":
                        await asyncio.to_thread(
                            daemon_call, "mouse",
                            {"action": "click", "x": float(ev["x"]),
                             "y": float(ev["y"]), "button": ev.get("button", "left")},
                            session=session_name,
                        )
                    elif etype == "type":
                        await asyncio.to_thread(
                            daemon_call, "keys", {"keys": ev.get("text", "")},
                            session=session_name,
                        )
                    elif etype == "key":
                        await asyncio.to_thread(
                            daemon_call, "keys", {"keys": ev.get("code", "")},
                            session=session_name,
                        )
                    elif etype == "scroll":
                        await asyncio.to_thread(
                            daemon_call, "mouse",
                            {"action": "wheel", "dx": float(ev.get("dx", 0)),
                             "dy": float(ev.get("dy", 0))},
                            session=session_name,
                        )
                except Exception:  # noqa: BLE001
                    pass

        # Sender always runs; receiver only when takeover requested (otherwise
        # the receiver's idle await blocks shutdown cleanup and can cancel
        # mid-frame on TestClient teardown).
        sender = asyncio.create_task(_send_frames())
        tasks = {sender}
        if takeover:
            tasks.add(asyncio.create_task(_receive_takeover()))
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            stop.set()
            for t in pending:
                t.cancel()
                try:
                    await t
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                await websocket.close()
            except Exception:  # noqa: BLE001
                pass

    @app.get("/v1/goals/{goal_id}/events")
    async def goal_events_stream(goal_id: str, request: Request):
        """SSE tail of a goal's event stream (`goal tail`). Emits each event as
        a `data: <json>` frame in order; closes on a terminal event or idle."""
        from fastapi.responses import StreamingResponse
        _check_auth(request)
        _check_cap("goal_events")
        after = int(request.query_params.get("after", "0"))
        if not daemon_is_running():
            spawn_daemon()
        return StreamingResponse(
            iter_goal_events_sse(daemon_call, goal_id, after),
            media_type="text/event-stream")

    @app.post("/v1/{verb}")
    async def invoke(verb: str, request: Request):
        _check_auth(request)
        _check_cap(verb)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        # Optional session targeting via query string OR body field
        session = request.query_params.get("session") or body.pop("session", None)
        # Spawn daemon if not running (mirror MCP behavior)
        if not daemon_is_running():
            spawn_daemon()
        # Daemon call is sync — run in thread so we don't block the event loop
        import asyncio
        try:
            result = await asyncio.to_thread(daemon_call, verb, body,
                                              session=session)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500,
                                detail=f"{type(exc).__name__}: {exc}") from exc
        return JSONResponse({"ok": True, "result": result})

    return app


def serve(*, host: str = "127.0.0.1", port: int = 8000,
           require_auth: bool = True, token: str | None = None,
           caps: str | None = None) -> None:
    """Run the REST shim. Blocks until interrupted."""
    # Import-checks BEFORE any banner output. Print-then-crash would mislead
    # log-watchers into thinking the server is listening when it isn't.
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "REST shim requires `pip install vibatchium[rest]` "
            f"(import error: {exc})"
        ) from exc
    # build_app() does its own fastapi import-check; surface that before banners.
    app = build_app(require_auth=require_auth, token=token, caps=caps)
    if require_auth and token is None:
        token = get_or_create_token()
        print(f"\n  vibatchium REST listening on http://{host}:{port}", flush=True)
        print(f"  bearer token: {token}", flush=True)
        print(f"  token file:   {TOKEN_PATH}", flush=True)
        if caps:
            print(f"  caps:         {caps}", flush=True)
        else:
            print("  caps:         (unrestricted — clients have local-code-equivalent access)", flush=True)
        print(flush=True)
    elif not require_auth:
        print(f"\n  WARNING: REST shim listening on http://{host}:{port} WITHOUT AUTH", flush=True)
        print("  Don't expose to a public network!\n", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
