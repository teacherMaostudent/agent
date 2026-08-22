"""Review Assignment 的服务端授权边界测试。"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from platform_sdk.contracts.runtime_api import (
    AgentResumeRequest,
    ReviewAssignmentRequest,
    ReviewCommentRequest,
)

from agent_runtime_service.service_api.runtime_api import (
    add_review_collaborator,
    add_review_comment,
    assign_run_reviewer,
    cancel_run,
    get_review_evidence,
    get_review_run,
    list_review_runs,
    resume_run,
)


def _request(store: object) -> SimpleNamespace:
    """构造未启用 OIDC 的最小本地请求，身份仍由函数的受信任入口统一解析。"""
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                container=SimpleNamespace(
                    run_store=store,
                    settings=SimpleNamespace(oidc_enabled=False),
                    agent_harness=SimpleNamespace(cancel=lambda *_: None),
                )
            )
        ),
        scope={},
    )


def test_review_assignment_requires_explicit_assignment_permission() -> None:
    """不能把普通 Review 读取权限误当成扩大他人可见范围的指派权限。"""

    class Store:
        """若授权检查失败，存储层不应被调用。"""

        def assign_reviewer(self, *_: object) -> None:
            raise AssertionError("unauthorized request reached store")

    with pytest.raises(HTTPException) as captured:
        assign_run_reviewer(
            "run-a",
            ReviewAssignmentRequest(reviewer_id="reviewer-a", reason="需要复核"),
            _request(Store()),
            x_tenant_id="tenant-a",
            x_user_id="manager-a",
            x_permissions="agent:review",
        )

    assert captured.value.status_code == 403


def test_review_queue_is_bound_to_current_reviewer() -> None:
    """Review 队列不接受浏览器传入的 reviewer_id，只使用认证后的当前用户。"""

    class Store:
        """记录底层查询，验证权限检查后的身份投影。"""

        arguments: tuple[str, str, int] | None = None

        def list_for_reviewer(self, tenant_id: str, reviewer_id: str, *, limit: int):
            self.arguments = (tenant_id, reviewer_id, limit)
            return []

    store = Store()
    body = list_review_runs(
        _request(store),
        limit=999,
        x_tenant_id="tenant-a",
        x_user_id="reviewer-a",
        x_permissions="agent:review",
    )

    assert body == {"items": []}
    assert store.arguments == ("tenant-a", "reviewer-a", 100)


def test_cancel_rejects_another_users_run_before_harness_call() -> None:
    """取消是状态变更；同租户的其他用户也不能仅凭 run_id 停止任务。"""

    class Store:
        """返回属于其他用户的 Run。"""

        def get(self, *_: object):
            return SimpleNamespace(user_id="owner-a")

    with pytest.raises(HTTPException) as captured:
        cancel_run(
            "run-a",
            _request(Store()),
            x_tenant_id="tenant-a",
            x_user_id="other-user",
        )

    assert captured.value.status_code == 404


def test_review_detail_requires_assignment_before_loading_run() -> None:
    """单项详情不能只凭 Review scope 读取；必须再次验证该 Run 的资源关系。"""

    class Store:
        """没有 Assignment 时读取 Run 代表授权顺序倒置。"""

        def review_assignment(self, *_: object):
            return None

        def get(self, *_: object):
            raise AssertionError("unassigned reviewer reached run read")

    with pytest.raises(HTTPException) as captured:
        get_review_run(
            "run-a",
            _request(Store()),
            x_tenant_id="tenant-a",
            x_user_id="reviewer-a",
            x_permissions="agent:review",
        )

    assert captured.value.status_code == 404


def test_review_approval_requires_explicit_assignment_and_permission() -> None:
    """Reviewer 不能仅凭同租户与 Run ID 恢复他人的等待审批任务。"""

    class Store:
        """模拟另一个 owner 的 Run，并拒绝其未被指派的审查关系。"""

        def get(self, *_: object):
            return SimpleNamespace(user_id="owner-a")

        def is_assigned_reviewer(self, *_: object) -> bool:
            return False

    with pytest.raises(HTTPException) as captured:
        resume_run(
            "run-a",
            AgentResumeRequest(approved=True, approval_id="approval-a"),
            _request(Store()),
            x_tenant_id="tenant-a",
            x_user_id="reviewer-a",
            x_permissions="agent:review,run:review:approve",
        )

    assert captured.value.status_code == 404


def test_review_comment_requires_current_assignment_and_permission() -> None:
    """协作备注不能成为转交后仍可写入的旁路。"""

    class Store:
        """如果权限缺失，存储写入绝不能发生。"""

        def add_review_comment(self, *_: object):
            raise AssertionError("unauthorized reviewer reached comment store")

    with pytest.raises(HTTPException) as captured:
        add_review_comment(
            "run-a",
            ReviewCommentRequest(message="请重点核对证据覆盖率"),
            _request(Store()),
            x_tenant_id="tenant-a",
            x_user_id="reviewer-a",
            x_permissions="agent:review",
        )

    assert captured.value.status_code == 403


def test_review_evidence_requires_assignment_and_data_domain_permission() -> None:
    """查看证据正文必须同时满足 Run Assignment 与证据所属数据域权限。"""

    class Store:
        def is_assigned_reviewer(self, *_: object) -> bool:
            return True

        def get(self, *_: object):
            return SimpleNamespace(
                result={
                    "evidence": [
                        {
                            "evidence_id": "evidence-a",
                            "knowledge_base": "finance",
                            "content": "approved evidence",
                            "source": "ledger",
                        }
                    ]
                }
            )

    with pytest.raises(HTTPException) as captured:
        get_review_evidence(
            "run-a",
            "evidence-a",
            _request(Store()),
            x_tenant_id="tenant-a",
            x_user_id="reviewer-a",
            x_permissions="agent:review,evidence:content:read",
        )
    assert captured.value.status_code == 403

    body = get_review_evidence(
        "run-a",
        "evidence-a",
        _request(Store()),
        x_tenant_id="tenant-a",
        x_user_id="reviewer-a",
        x_permissions="agent:review,evidence:content:read,data-domain:finance:read",
    )
    assert body["content"] == "approved evidence"
    assert len(body["content_sha256"]) == 64


def test_review_collaborator_keeps_current_assignment_and_requires_explicit_scope() -> None:
    """共同审查是新增 Assignment，不应复用会移除当前人的 transfer 操作。"""

    class Store:
        assigned: tuple[object, ...] | None = None

        def is_assigned_reviewer(self, *_: object) -> bool:
            return True

        def assign_reviewer(self, *arguments: object) -> None:
            self.assigned = arguments

    store = Store()
    add_review_collaborator(
        "run-a",
        ReviewAssignmentRequest(reviewer_id="reviewer-b", reason="复核财务域证据"),
        _request(store),
        x_tenant_id="tenant-a",
        x_user_id="reviewer-a",
        x_permissions="agent:review,run:review:assign",
    )
    assert store.assigned == (
        "tenant-a",
        "run-a",
        "reviewer-b",
        "reviewer-a",
        "复核财务域证据",
    )
