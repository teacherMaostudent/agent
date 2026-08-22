import hashlib

from platform_sdk.contracts.artifacts import TaskArtifactCreate, TaskArtifactTextCreate

from app.context.artifact_store import TaskArtifactStore
from app.service_api.context_api import artifact_preview, compare_artifacts, create_text_artifact


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


def test_task_artifact_series_allocates_monotonic_versions():
    """同一逻辑产物形成可比较版本链，不同逻辑名称互不影响。"""
    store = TaskArtifactStore()
    first = store.create(
        "tenant-a",
        "user-a",
        TaskArtifactCreate(
            root_task_id="task-1",
            artifact_type="report",
            logical_name="scan-report",
            content_ref="s3://artifacts/task-1/report-v1.json",
            content_sha256="1" * 64,
        ),
    )
    second = store.create(
        "tenant-a",
        "user-a",
        TaskArtifactCreate(
            root_task_id="task-1",
            artifact_type="report",
            logical_name="scan-report",
            content_ref="s3://artifacts/task-1/report-v2.json",
            content_sha256="2" * 64,
        ),
    )
    independent = store.create(
        "tenant-a",
        "user-a",
        TaskArtifactCreate(
            root_task_id="task-1",
            artifact_type="report",
            logical_name="other-report",
            content_ref="s3://artifacts/task-1/other.json",
            content_sha256="3" * 64,
        ),
    )

    assert (first.version, first.previous_artifact_id) == (1, None)
    assert (second.version, second.previous_artifact_id) == (2, first.artifact_id)
    assert (independent.version, independent.previous_artifact_id) == (1, None)


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


def test_artifact_preview_and_comparison_read_only_bounded_text():
    """Preview verifies complete bodies and comparison is limited to one logical series."""
    from types import SimpleNamespace

    bodies: dict[str, bytes] = {}

    class Storage:
        bucket = "platform-artifacts"
        prefix = "agent-platform"

        def read_bounded(self, key, *, max_bytes):
            payload = bodies[key]
            return payload[:max_bytes], len(payload) > max_bytes

    store = TaskArtifactStore()
    first_body = b"line one\nline two\n"
    second_body = b"line one\nline changed\n"
    first_key = "agent-platform/tasks/task-1/report-v1.md"
    second_key = "agent-platform/tasks/task-1/report-v2.md"
    bodies[first_key] = first_body
    bodies[second_key] = second_body
    first = store.create(
        "tenant-a",
        "user-a",
        TaskArtifactCreate(
            root_task_id="task-1",
            artifact_type="report",
            logical_name="report",
            media_type="text/markdown",
            content_ref=f"s3://platform-artifacts/{first_key}",
            content_sha256=hashlib.sha256(first_body).hexdigest(),
        ),
    )
    second = store.create(
        "tenant-a",
        "user-a",
        TaskArtifactCreate(
            root_task_id="task-1",
            artifact_type="report",
            logical_name="report",
            media_type="text/markdown",
            content_ref=f"s3://platform-artifacts/{second_key}",
            content_sha256=hashlib.sha256(second_body).hexdigest(),
        ),
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(artifacts=store, artifact_delivery=Storage())
            )
        )
    )

    preview = artifact_preview("task-1", second.artifact_id, request, 50_000, "tenant-a")
    comparison = compare_artifacts(
        "task-1", second.artifact_id, first.artifact_id, request, 80_000, "tenant-a"
    )

    assert preview.sha256_verified is True
    assert "-line two" in comparison.diff
    assert "+line changed" in comparison.diff
