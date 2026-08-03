"""No route may authenticate a caller from a bare X-User-Id header alone —
every identity, anonymous or registered, must present a token this tenant's
own store actually issued."""
import os

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault(
    "AGENT_ENGINE_RESOURCE_NAME",
    "projects/test-project/locations/us-central1/reasoningEngines/123",
)
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import app  # noqa: E402

client = TestClient(app)

TENANT = "test-tenant"


@pytest.fixture(autouse=True)
def _isolate_rate_limit_state():
    from backend.app.rate_limiter import rate_limiter
    from customer_support_mas import rate_limiting

    rate_limiting._buckets.clear()
    rate_limiter.reset()
    yield
    rate_limiting._buckets.clear()
    rate_limiter.reset()


@pytest.fixture(autouse=True)
def _reset_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _wire_account_stores(wire_backend_account_stores):
    return wire_backend_account_stores


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("post", "/api/chat", {"json": {"message": "hi", "tenant_id": TENANT}}),
        ("get", "/api/sessions", {"params": {"tenant_id": TENANT}}),
        ("put", "/api/sessions/some-id/rename", {"json": {"session_name": "x"}, "params": {"tenant_id": TENANT}}),
        ("delete", "/api/sessions/some-id", {"params": {"tenant_id": TENANT}}),
        ("get", "/api/sessions/some-id/messages", {"params": {"tenant_id": TENANT}}),
    ],
)
def test_bare_x_user_id_no_longer_authenticates(method, path, kwargs):
    """A request with only X-User-Id (no Authorization header) must be
    rejected as unauthenticated — the exact bug this task fixes."""
    kwargs = dict(kwargs)
    kwargs["headers"] = {"X-User-Id": "demo-user-001"}

    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 401, (
        f"{method.upper()} {path} authenticated from a bare X-User-Id header "
        f"with no token — expected 401, got {response.status_code}: {response.text}"
    )
