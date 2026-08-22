"""Workspace Artifact 索引的资源授权与脱敏投影测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from platform_sdk.contracts.artifacts import TaskArtifact

from agent_runtime_service.service_api.runtime_api import (
    get_run_artifact_download,
    list_run_artifacts,
)


def test_workspace_artifact_index_uses_run_access_and_hides_content_reference() -> None:
    """共享读取可见元数据，但对象存储位置绝不能进入 Workspace 响应。"""

    artifact = TaskArtifact(
        artifact_id="art-a",
        tenant_id="tenant-a",
        root_task_id="root-a",
        artifact_type="report",
        content_ref="s3://private-bucket/root-a/report.json",
        content_sha256="a" * 64,
        media_type="application/json",
        created_by="owner-a",
        created_at=datetime.now(UTC),
    )

    class Store:
        """模拟被显式共享给当前用户的 Run。"""

        def get(self, *_: object):
            return SimpleNamespace(
                run_id="run-a", user_id="owner-a", context=SimpleNamespace(root_task_id="root-a")
            )

        def is_shared_with(self, *_: object) -> bool:
            return True

    context = SimpleNamespace(list_task_artifacts=lambda *_args, **_kwargs: [artifact])
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(
                    run_store=Store(),
                    runtime_context=SimpleNamespace(context=context),
                    settings=SimpleNamespace(oidc_enabled=False),
                )
            )
        ),
        scope={},
    )

    body = list_run_artifacts(
        "run-a", request, x_tenant_id="tenant-a", x_user_id="reader-a"
    )

    assert body["items"][0]["artifact_id"] == "art-a"
    assert "content_ref" not in body["items"][0]


def test_workspace_artifact_download_requires_run_relation_before_signing() -> None:
    """不存在 owner/share 关系时，不得触发 Context 的签名 URL 请求。"""

    class Store:
        """模拟属于另一位用户且未共享的运行。"""

        def get(self, *_: object):
            return SimpleNamespace(
                run_id="run-a", user_id="owner-a", context=SimpleNamespace(root_task_id="root-a")
            )

        def is_shared_with(self, *_: object) -> bool:
            return False

    context = SimpleNamespace(
        artifact_download_url=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("foreign reader reached signing client")
        )
    )
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(
                    run_store=Store(),
                    runtime_context=SimpleNamespace(context=context),
                    settings=SimpleNamespace(oidc_enabled=False),
                )
            )
        ),
        scope={},
    )

    with pytest.raises(HTTPException) as captured:
        get_run_artifact_download(
            "run-a", "art-a", request, x_tenant_id="tenant-a", x_user_id="other-user"
        )
    assert captured.value.status_code == 404
