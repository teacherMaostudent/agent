from platform_sdk.contracts.artifacts import TaskArtifactCreate

from app.context.artifact_store import TaskArtifactStore


def test_task_artifact_is_immutable_and_root_task_scoped():
    store = TaskArtifactStore()
    artifact = store.create(
        "tenant-a",
        "user-a",
        TaskArtifactCreate(
            root_task_id="task-1",
            artifact_type="evidence-set",
            content_ref="s3://artifacts/task-1/evidence.json",
            content_sha256="a" * 64,
        ),
    )
    assert store.get("tenant-a", "task-1", artifact.artifact_id) == artifact
    assert store.get("tenant-a", "other-task", artifact.artifact_id) is None
    assert store.get("other-tenant", "task-1", artifact.artifact_id) is None
