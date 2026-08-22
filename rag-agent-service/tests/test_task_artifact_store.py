from platform_sdk.contracts.artifacts import TaskArtifactCreate, TaskArtifactTextCreate

from app.context.artifact_store import TaskArtifactStore
from app.service_api.context_api import create_text_artifact


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


def test_task_artifact_index_is_root_task_scoped():
    """列表查询只能使用完整 tenant/root 前缀，不能把相邻任务混进结果。"""
    store = TaskArtifactStore()
    first = store.create(
        "tenant-a",
        "user-a",
        TaskArtifactCreate(
            root_task_id="task-1",
            artifact_type="report",
            content_ref="s3://artifacts/task-1/report.json",
            content_sha256="b" * 64,
        ),
    )
    store.create(
        "tenant-a",
        "user-a",
        TaskArtifactCreate(
            root_task_id="task-10",
            artifact_type="report",
            content_ref="s3://artifacts/task-10/report.json",
            content_sha256="c" * 64,
        ),
    )

    assert store.list("tenant-a", "task-1") == [first]


def test_final_text_artifact_writes_configured_storage_before_registering_reference():
    """最终报告只能在对象数据面写入成功后登记，避免出现不可下载的伪 Artifact。"""

    class Storage:
        """记录上传参数，不触发真实 S3 网络调用。"""

        bucket = "platform-artifacts"

        def put_stream(self, namespace, filename, stream, *, content_type):
            assert namespace == "tasks/tenant-a/task-1"
            assert filename == "final-report.md"
            assert content_type == "text/markdown; charset=utf-8"
            assert stream.read() == "完成报告".encode()
            return "agent-platform/tasks/tenant-a/task-1/report.md", "d" * 64

    from types import SimpleNamespace

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(artifact_delivery=Storage(), artifacts=TaskArtifactStore())
            )
        )
    )

    artifact = create_text_artifact(
        "task-1",
        TaskArtifactTextCreate(content="完成报告"),
        request,
        x_tenant_id="tenant-a",
        x_user_id="user-a",
    )

    assert artifact.content_ref == "s3://platform-artifacts/agent-platform/tasks/tenant-a/task-1/report.md"
    assert artifact.content_sha256 == "d" * 64
