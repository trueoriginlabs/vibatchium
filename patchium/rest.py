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
        from fastapi import FastAPI, HTTPException, Request
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
