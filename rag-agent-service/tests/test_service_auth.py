from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import create_app


def test_service_auth_rejects_untrusted_permission_headers(monkeypatch) -> None:
    monkeypatch.setenv("RAG_REQUIRE_SERVICE_AUTH", "true")
    monkeypatch.setenv("RAG_SERVICE_API_KEY", "trusted-gateway")
    get_settings.cache_clear()
    app = create_app()
    client = TestClient(app)

    denied = client.post(
        "/api/v1/agent/run",
        headers={"X-Permissions": "rag:read,review:execute"},
        json={"task": "Find audit requirements"},
    )
    allowed = client.post(
        "/api/v1/agent/run",
        headers={"X-Rag-Agent-Key": "trusted-gateway", "X-Permissions": "rag:read"},
        json={"task": "Find audit requirements", "max_steps": 3},
    )

    assert denied.status_code == 401
    assert allowed.status_code == 200
    get_settings.cache_clear()
