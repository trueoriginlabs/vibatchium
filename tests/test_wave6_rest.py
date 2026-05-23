"""Wave 6.4a — REST shim tests.

Verifies:
- /v1/health requires no auth
- Missing bearer token → 401
- Wrong bearer token → 403
- Correct bearer token + valid verb → 200 with daemon result
- Invalid body → 400
- session= query param routes to a specific session
- /v1/tools lists all verbs
- --insecure-no-auth disables auth (no 401)
"""
from __future__ import annotations

import pytest

try:
    from fastapi.testclient import TestClient
    _FASTAPI_OK = True
except ImportError:
    _FASTAPI_OK = False


pytestmark = pytest.mark.skipif(
    not _FASTAPI_OK, reason="fastapi not installed (patchium[rest] extra)"
)


from patchium.rest import build_app


@pytest.fixture
def client_auth():
    app = build_app(require_auth=True, token="test-token-1234")
    return TestClient(app)


@pytest.fixture
def client_noauth():
    app = build_app(require_auth=False)
    return TestClient(app)


def test_health_no_auth_required(client_auth):
    r = client_auth.get("/v1/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_missing_token_returns_401(client_auth):
    r = client_auth.post("/v1/status", json={})
    assert r.status_code == 401
    assert "missing bearer" in r.json()["detail"].lower()


def test_wrong_token_returns_403(client_auth):
    r = client_auth.post(
        "/v1/status", json={},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert r.status_code == 403


def test_valid_token_status_succeeds(client_auth, local_server):
    """conftest already started a default session, so status should succeed."""
    r = client_auth.post(
        "/v1/status", json={},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["result"]["running"] is True


def test_tools_list(client_auth):
    r = client_auth.get(
        "/v1/tools",
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200
    tools = r.json()["tools"]
    assert len(tools) > 80  # ~95+ at Wave 6.4
    names = {t["name"] for t in tools}
    # Sanity-check key verbs are present
    for need in ("start", "go", "click", "session_new", "fingerprint",
                 "secret_set", "vision_click", "safety_set", "liveview_start"):
        assert need in names, f"{need!r} missing from /v1/tools"


def test_session_query_param_routes(client_auth, local_server):
    """The session= query string should route the call to that session."""
    # default session is already running from conftest
    r = client_auth.post(
        "/v1/status?session=default", json={},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200
    assert r.json()["result"]["session"] == "default"


def test_invalid_body_returns_400(client_auth):
    """Non-object JSON body should 400."""
    r = client_auth.post(
        "/v1/status",
        # FastAPI's request.json() may raise on a bare scalar — we wrap and 400.
        content="[1, 2, 3]",
        headers={"Authorization": "Bearer test-token-1234",
                 "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_no_auth_mode_allows_unauth_calls(client_noauth):
    r = client_noauth.post("/v1/status", json={})
    # Without auth required: should succeed (or fail with daemon error, not auth)
    assert r.status_code == 200


def test_daemon_error_returns_500(client_auth):
    """A daemon-level error (e.g. unknown verb) should surface as 500 with details."""
    r = client_auth.post(
        "/v1/nonexistent_verb", json={},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 500
    assert "unknown command" in r.json()["detail"].lower()


# ─── Wave 7.3: /v1/stream/<session> WebSocket passthrough ──────────────


def test_stream_rejects_missing_token(client_auth, local_server):
    """WS without ?token=... must close immediately (no frames received)."""
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client_auth.websocket_connect("/v1/stream/default") as ws:
            # Should be closed by server before we can receive anything
            ws.receive()


def test_stream_rejects_bad_token(client_auth, local_server):
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client_auth.websocket_connect(
            "/v1/stream/default?token=wrong-token"
        ) as ws:
            ws.receive()


def test_stream_sends_hello_and_frames(client_auth, local_server):
    """With a valid token, we should get a hello envelope + at least one JPEG/PNG."""
    # Navigate so the screenshot has content
    # We can't use the daemon client directly here — the test client uses
    # the SAME daemon (via daemon_call inside the handler). Tell that
    # daemon to navigate via the REST shim itself.
    r = client_auth.post(
        "/v1/go", json={"url": f"{local_server}/simple.html"},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200

    with client_auth.websocket_connect(
        "/v1/stream/default?token=test-token-1234&fps=20"
    ) as ws:
        # First message is the hello JSON
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["session"] == "default"
        assert hello["fps"] == 20
        assert hello["takeover"] is False
        # Then we should get binary frames
        msg = ws.receive_bytes()
        # Could be PNG (89 50 4E 47) or, in some flows, JSON; verify PNG magic
        assert msg[:4] == b"\x89PNG"
        # Get one more for good measure
        msg2 = ws.receive_bytes()
        assert msg2[:4] == b"\x89PNG"


def test_stream_takeover_flag_forwarded_in_hello(client_auth, local_server):
    """?takeover=1 reflected in the hello envelope so the client knows to forward input."""
    r = client_auth.post(
        "/v1/go", json={"url": f"{local_server}/simple.html"},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200
    with client_auth.websocket_connect(
        "/v1/stream/default?token=test-token-1234&takeover=1"
    ) as ws:
        hello = ws.receive_json()
        assert hello["takeover"] is True


def test_stream_fps_clamped_to_30(client_auth, local_server):
    """fps > 30 clamps to 30 (no runaway CPU)."""
    r = client_auth.post(
        "/v1/go", json={"url": f"{local_server}/simple.html"},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200
    with client_auth.websocket_connect(
        "/v1/stream/default?token=test-token-1234&fps=1000"
    ) as ws:
        hello = ws.receive_json()
        assert hello["fps"] == 30


def test_stream_no_auth_mode_no_token_required(client_noauth, local_server):
    """With auth disabled, ?token is unnecessary."""
    r = client_noauth.post(
        "/v1/go", json={"url": f"{local_server}/simple.html"},
    )
    assert r.status_code == 200
    with client_noauth.websocket_connect("/v1/stream/default?fps=10") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"


# ─── Wave 7.5b: REST capability gating ─────────────────────────────────


@pytest.fixture
def client_caps_minimal():
    """REST with only `core,nav` — should refuse eval, secret_*, etc."""
    app = build_app(require_auth=True, token="test-token-1234",
                    caps="core,nav")
    return TestClient(app)


@pytest.fixture
def client_caps_vision():
    """REST with vision bucket — stream WS should be allowed; eval still 403."""
    app = build_app(require_auth=True, token="test-token-1234",
                    caps="core,nav,vision")
    return TestClient(app)


def test_caps_blocks_eval(client_caps_minimal):
    """`eval` is in `content` bucket, not `core,nav` → 403."""
    r = client_caps_minimal.post(
        "/v1/eval", json={"expr": "1+1"},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 403
    assert "not in allowed caps" in r.json()["detail"]


def test_caps_blocks_secret_set(client_caps_minimal):
    """`secret_set` is in `secrets` bucket — blocked under core,nav."""
    r = client_caps_minimal.post(
        "/v1/secret_set", json={"site": "x", "key": "y", "value": "z"},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 403


def test_caps_allows_in_bucket(client_caps_minimal, local_server):
    """`go` is in the `nav` bucket — should still be reachable."""
    r = client_caps_minimal.post(
        "/v1/go", json={"url": f"{local_server}/simple.html"},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200


def test_caps_status_always_exposed(client_caps_minimal):
    """`status` must be reachable regardless of cap filter (matches MCP)."""
    r = client_caps_minimal.post(
        "/v1/status", json={},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200


def test_caps_tools_endpoint_filters(client_caps_minimal):
    """/v1/tools should only show verbs the current caps allow."""
    r = client_caps_minimal.get(
        "/v1/tools",
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200
    body = r.json()
    names = {t["name"] for t in body["tools"]}
    assert "eval" not in names
    assert "secret_set" not in names
    assert "go" in names
    assert "status" in names
    assert body["caps"] == "core,nav"


def test_caps_unknown_bucket_raises():
    """An unknown cap name at build time should be a clear error."""
    import pytest as _pt
    with _pt.raises(ValueError, match="unknown REST caps"):
        build_app(require_auth=True, token="x", caps="bogus_bucket")


def test_caps_stream_requires_vision(client_caps_minimal, local_server):
    """Without `vision` cap, the WebSocket stream must close immediately."""
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client_caps_minimal.websocket_connect(
            "/v1/stream/default?token=test-token-1234"
        ) as ws:
            ws.receive()


def test_caps_stream_takeover_denied_without_input(
    client_caps_vision, local_server
):
    """With `vision` but NOT `input`, stream connects but takeover is denied
    and surfaced in the hello envelope."""
    r = client_caps_vision.post(
        "/v1/go", json={"url": f"{local_server}/simple.html"},
        headers={"Authorization": "Bearer test-token-1234"},
    )
    assert r.status_code == 200
    with client_caps_vision.websocket_connect(
        "/v1/stream/default?token=test-token-1234&takeover=1"
    ) as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["takeover"] is False
        assert hello["takeover_denied_reason"] == "input cap required"
