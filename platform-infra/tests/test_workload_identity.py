from __future__ import annotations

import httpx

from platform_infra.identity import WorkloadTokenProvider


def test_client_credentials_token_is_cached(monkeypatch) -> None:
    calls: list[dict] = []

    def post(url, *, data, auth, timeout):
        calls.append({"url": url, "data": data, "auth": auth, "timeout": timeout})
        return httpx.Response(
            200,
            json={"access_token": "signed-workload-token", "expires_in": 300},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    provider = WorkloadTokenProvider(
        token_url="https://identity.example/token",
        client_id="agent-runtime",
        client_secret="secret",
        audience="agent-platform",
    )

    assert provider.authorization_header() == {"Authorization": "Bearer signed-workload-token"}
    assert provider.authorization_header() == {"Authorization": "Bearer signed-workload-token"}
    assert len(calls) == 1
    assert calls[0]["data"]["grant_type"] == "client_credentials"
    assert calls[0]["data"]["audience"] == "agent-platform"
