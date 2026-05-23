"""Wave 6.4a — REST shim.

FastAPI app that exposes every daemon verb at `POST /v1/<verb>` with the
same JSON body. Lets non-shell clients (Docker, language-agnostic agents,
hosted-mode integrations) drive patchium without speaking Unix-socket RPC.

### Auth

Bearer-token by default. Token is generated on first launch + persisted at
`~/.cache/patchium/rest-token` (mode 0600). All endpoints require
`Authorization: Bearer <token>` except `/v1/health`.

`--insecure-no-auth` (CLI flag) disables auth — explicit opt-in for dev only.

### Endpoints

  GET  /v1/health                 — health check; no auth
  GET  /v1/tools                  — list every available verb + schema
  POST /v1/<verb>                 — invoke verb; body is the args dict
  POST /v1/<verb>?session=<name>  — invoke verb on a specific session

### Long-running verbs

`act`, `vision_click`, `wait_email_code`, etc. can take many seconds.
The shim awaits them inline (FastAPI is async); set the client timeout
appropriately.
"""
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("patchium.rest")


TOKEN_PATH = Path.home() / ".cache" / "patchium" / "rest-token"


def get_or_create_token() -> str:
    """Return the persisted bearer token; generate one if missing."""
    if TOKEN_PATH.exists():
        return TOKEN_PATH.read_text().strip()
    token = secrets.token_urlsafe(32)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token)
    os.chmod(TOKEN_PATH, 0o600)
    return token


def build_app(*, require_auth: bool = True, token: Optional[str] = None):
    """Build the FastAPI app. `token` defaults to the persisted token."""
    try:
        from fastapi import FastAPI, HTTPException, Request, WebSocket
        from fastapi.responses import JSONResponse
    except ImportError as exc:
        raise RuntimeError(
            "REST shim requires `pip install patchium[rest]` "
            f"(import error: {exc})"
        ) from exc

    from .client import call as daemon_call, daemon_is_running, spawn_daemon
    from .mcp_server import TOOLS

    if require_auth and token is None:
        token = get_or_create_token()
    _expected_token = token  # closure

    app = FastAPI(title="patchium", version="0.3.0",
                  description="REST shim over the patchium daemon")

    def _check_auth(request) -> None:
        if not require_auth:
            return
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        provided = authorization[len("Bearer "):].strip()
        if not secrets.compare_digest(provided, _expected_token):
            raise HTTPException(status_code=403, detail="invalid token")

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
            ],
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

        Implementation: pulls screenshots via the existing RPC, so the REST
        shim and daemon stay decoupled (REST can even run in a sibling
        container as long as it has socket access).
        """
        # Query-param auth
        if require_auth:
            token_q = websocket.query_params.get("token")
            if not token_q or not secrets.compare_digest(token_q, _expected_token or ""):
                # 1008 = policy violation — closest to HTTP 403 for WS
                await websocket.close(code=1008, reason="bad token")
                return
        try:
            fps = int(websocket.query_params.get("fps", "5"))
        except ValueError:
            fps = 5
        fps = max(1, min(fps, 30))
        takeover = websocket.query_params.get("takeover") == "1"
        await websocket.accept()
        # Send hello envelope first (client expects this to set up scaling)
        try:
            await websocket.send_json({
                "type": "hello", "session": session_name,
                "fps": fps, "takeover": takeover,
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
                    continue  # discard; takeover mode off
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

    @app.post("/v1/{verb}")
    async def invoke(verb: str, request: Request):
        _check_auth(request)
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
                                detail=f"{type(exc).__name__}: {exc}")
        return JSONResponse({"ok": True, "result": result})

    return app


def serve(*, host: str = "127.0.0.1", port: int = 8000,
           require_auth: bool = True, token: Optional[str] = None) -> None:
    """Run the REST shim. Blocks until interrupted."""
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "REST shim requires `pip install patchium[rest]` "
            f"(import error: {exc})"
        ) from exc
    if require_auth and token is None:
        token = get_or_create_token()
        print(f"\n  patchium REST listening on http://{host}:{port}", flush=True)
        print(f"  bearer token: {token}", flush=True)
        print(f"  token file:   {TOKEN_PATH}\n", flush=True)
    elif not require_auth:
        print(f"\n  WARNING: REST shim listening on http://{host}:{port} WITHOUT AUTH", flush=True)
        print(f"  Don't expose to a public network!\n", flush=True)
    app = build_app(require_auth=require_auth, token=token)
    uvicorn.run(app, host=host, port=port, log_level="info")
