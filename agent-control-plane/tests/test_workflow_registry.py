def _spec():
    return {
        "workflow_id": "nightly-scan",
        "description": "fixed zero-agent scan",
        "steps": [
            {"step_id": "parse", "capability_id": "DOCUMENT_PARSE"},
            {"step_id": "scan", "capability_id": "CONSISTENCY_SCAN"},
        ],
    }


def test_workflow_draft_version_release_and_runtime_resolution(client, headers):
    created = client.post(
        "/v1/workflows",
        headers=headers,
        json={"workflow_id": "nightly-scan", "spec": _spec()},
    )
    assert created.status_code == 201, created.text
    assert created.json()["revision"] == 1

    published = client.post(
        "/v1/workflows/nightly-scan/versions",
        headers=headers,
        json={"semantic_version": "1.0.0"},
    )
    assert published.status_code == 201, published.text
    version = published.json()
    assert version["plan"]["owner"] == "workflow"
    assert len(version["artifact_digest"]) == 64

    released = client.post(
        "/v1/workflows/nightly-scan/releases",
        headers=headers,
        json={"version_id": version["version_id"], "environment": "production"},
    )
    assert released.status_code == 201, released.text

    resolved = client.get(
        "/internal/v1/workflows/nightly-scan/resolve?environment=production",
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["version_id"] == version["version_id"]
    assert resolved.json()["artifact_digest"] == version["artifact_digest"]


def test_releasing_new_workflow_version_retires_previous_resolution(client, headers):
    client.post(
        "/v1/workflows",
        headers=headers,
        json={"workflow_id": "nightly-scan", "spec": _spec()},
    )
    versions = []
    for semantic in ("1.0.0", "1.1.0"):
        response = client.post(
            "/v1/workflows/nightly-scan/versions",
            headers=headers,
            json={"semantic_version": semantic},
        )
        versions.append(response.json())
        client.post(
            "/v1/workflows/nightly-scan/releases",
            headers=headers,
            json={"version_id": versions[-1]["version_id"], "environment": "production"},
        )
    resolved = client.get(
        "/internal/v1/workflows/nightly-scan/resolve?environment=production",
        headers={"X-Tenant-Id": "tenant-a"},
    )
    assert resolved.json()["version_id"] == versions[-1]["version_id"]
