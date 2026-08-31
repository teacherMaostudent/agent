"""Tenant Catalog tests keep the tenant aggregate separate from human identities."""

from __future__ import annotations


def _super_headers(headers: dict[str, str]) -> dict[str, str]:
    """Use a platform-wide role while retaining a normal caller tenant for request tracing."""
    return {**headers, "X-Roles": "agent-admin,platform-super-admin"}


def test_super_admin_creates_lists_and_suspends_tenant(client, headers) -> None:
    """A catalog row gets a default policy and transitions without ever deleting its tenant_id."""
    super_headers = _super_headers(headers)
    created = client.post(
        "/v1/tenants",
        headers=super_headers,
        json={"tenant_id": "acme-china", "display_name": "Acme 中国", "data_region": "cn"},
    )
    assert created.status_code == 201
    assert created.json()["status"] == "active"

    listed = client.get("/v1/tenants", headers=super_headers)
    assert listed.status_code == 200
    assert [item["tenant_id"] for item in listed.json()] == ["acme-china"]

    updated = client.put(
        "/v1/tenants/acme-china",
        headers=super_headers,
        json={
            "display_name": "Acme 中国",
            "data_region": "cn",
            "status": "suspended",
            "reason": "billing review",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["tenant_id"] == "acme-china"
    assert updated.json()["status"] == "suspended"


def test_tenant_catalog_rejects_non_super_admin(client, headers) -> None:
    """An ordinary tenant admin cannot enumerate or mint global tenant isolation boundaries."""
    assert client.get("/v1/tenants", headers=headers).status_code == 403
    assert client.post(
        "/v1/tenants",
        headers=headers,
        json={"tenant_id": "acme-china", "display_name": "Acme", "data_region": "cn"},
    ).status_code == 403
