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
